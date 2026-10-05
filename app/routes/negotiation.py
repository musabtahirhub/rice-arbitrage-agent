import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from langgraph.types import Command

from app.config import settings
from app.database import get_db
from app.db_models import CampaignModel, TradeAuditModel
from app.directory import get_buyers_for_commodity, get_suppliers_for_commodity
import app.email_service as email_service
from app.models import ApprovalPayload, SimulateTurnRequest
from app.workflow import trade_graph

logger = logging.getLogger("arbitrage_desk")
router = APIRouter()

@router.post("/api/negotiate")
def negotiate_turn(req: SimulateTurnRequest, db: Session = Depends(get_db)):
    logger.info(f"[API NEGOTIATE] Inbound turn for campaign '{req.campaign_id}' from role='{req.sender_role}'")
    db_camp = db.query(CampaignModel).filter(CampaignModel.id == req.campaign_id).first()
    if not db_camp:
        logger.warning(f"[API NEGOTIATE] Campaign '{req.campaign_id}' not found in database.")
        raise HTTPException(status_code=404, detail=f"Campaign {req.campaign_id} not found.")

    config = {"configurable": {"thread_id": req.campaign_id}}
    snapshot = trade_graph.get_state(config)
    state = dict(snapshot.values) if snapshot and snapshot.values else {}
    if not state:
        logger.warning(f"[API NEGOTIATE] State for campaign '{req.campaign_id}' not found.")
        raise HTTPException(status_code=404, detail=f"State for campaign {req.campaign_id} not found.")

    turn_state = dict(state)
    turn_state["latest_email"] = req.raw_email
    turn_state["active_role"] = req.sender_role

    updated_state = trade_graph.invoke(turn_state, config=config)

    snapshot = trade_graph.get_state(config)
    if snapshot and snapshot.next and "approval_gate" in snapshot.next:
        trade_graph.update_state(config, {"deal_status": "pending_approval"})
        updated_state["deal_status"] = "pending_approval"

    buyer_t = updated_state.get("buyer_terms")
    supp_t = updated_state.get("supplier_terms")
    price = (
        buyer_t.price_usd_per_mt
        if (req.sender_role == "buyer" and buyer_t)
        else (supp_t.price_usd_per_mt if supp_t else None)
    )

    inbound_audit = TradeAuditModel(
        campaign_id=req.campaign_id,
        thread_id=req.campaign_id,
        role=req.sender_role,
        counterparty_price=price,
        net_spread=updated_state.get("net_spread_usd"),
        raw_message=req.raw_email,
        direction="INBOUND",
    )
    db.add(inbound_audit)

    outbound_msg = (
        updated_state.get("buyer_draft")
        if req.sender_role == "supplier" or updated_state.get("action") in ["ACCEPT_AND_CLOSE", "REJECT_HARD", "COUNTER_TO_MAXIMIZE"]
        else updated_state.get("supplier_draft")
    )
    if outbound_msg:
        outbound_audit = TradeAuditModel(
            campaign_id=req.campaign_id,
            thread_id=req.campaign_id,
            role="agent",
            counterparty_price=updated_state.get("anchor_cif_usd"),
            net_spread=updated_state.get("net_spread_usd"),
            raw_message=outbound_msg,
            direction="OUTBOUND",
        )
        db.add(outbound_audit)

    db_camp.deal_status = updated_state.get("deal_status", db_camp.deal_status)
    db.commit()

    logger.info(
        f"[API NEGOTIATE] Turn completed for '{req.campaign_id}' | Round {updated_state.get('negotiation_round')} | "
        f"Action: {updated_state.get('action')} | Status: {updated_state.get('deal_status')} | "
        f"Spread: ${updated_state.get('net_spread_usd', 0.0):.2f}/MT | Margin: {updated_state.get('net_margin_pct', 0.0):.2f}%"
    )

    return {
        "campaign_id": req.campaign_id,
        "negotiation_round": updated_state.get("negotiation_round", 1),
        "deal_status": updated_state.get("deal_status"),
        "action": updated_state.get("action"),
        "pipeline_step": updated_state.get("pipeline_step", 1),
        "is_deal_viable": updated_state.get("is_deal_viable"),
        "net_spread_usd": updated_state.get("net_spread_usd", 0.0),
        "net_margin_pct": updated_state.get("net_margin_pct"),
        "evaluation_reason": updated_state.get("evaluation_reason"),
        "target_fob_ceiling": updated_state.get("target_fob_ceiling", 0.0),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": updated_state.get("buyer_draft"),
        "supplier_draft": updated_state.get("supplier_draft"),
        "audit_transcript": updated_state.get("audit_transcript", []),
    }


@router.post("/api/campaigns/{campaign_id}/approve")
def approve_campaign_deal(campaign_id: str, payload: ApprovalPayload, db: Session = Depends(get_db)):
    logger.info(
        f"[API APPROVE] Inbound approval decision for campaign '{campaign_id}': "
        f"approved={payload.approved}, override_cif={payload.override_cif_price}, notes='{payload.reviewer_notes}'"
    )
    db_camp = db.query(CampaignModel).filter(CampaignModel.id == campaign_id).first()
    if not db_camp:
        logger.warning(f"[API APPROVE] Campaign '{campaign_id}' not found in database.")
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    config = {"configurable": {"thread_id": campaign_id}}
    snapshot = trade_graph.get_state(config)
    if not snapshot or not snapshot.values:
        logger.warning(f"[API APPROVE] State for campaign '{campaign_id}' not found.")
        raise HTTPException(status_code=404, detail=f"State for campaign {campaign_id} not found.")

    resumed_state = trade_graph.invoke(Command(resume=payload.model_dump()), config=config)

    buyer_msg = resumed_state.get("buyer_draft")
    if buyer_msg and not resumed_state.get("skip_email_dispatch", False):
        camp = resumed_state.get("campaign")
        comm = getattr(camp, "commodity", None) or db_camp.commodity
        buyers = get_buyers_for_commodity(comm)
        buyer_email = (
            resumed_state.get("target_buyer_email")
            or (buyers[0].contact_email if buyers else (settings.my_test_email or "procurement@domain.com"))
        )

        in_reply_to = resumed_state.get("last_buyer_message_id")
        references = resumed_state.get("buyer_references")
        thread_subject = resumed_state.get("thread_subject")

        email_service.send_email(
            buyer_email,
            buyer_msg,
            in_reply_to=in_reply_to,
            references=references,
            thread_subject=thread_subject,
        )

    supp_msg = resumed_state.get("supplier_draft")
    if supp_msg and resumed_state.get("deal_status") == "closed" and not resumed_state.get("skip_email_dispatch", False):
        camp = resumed_state.get("campaign")
        comm = getattr(camp, "commodity", None) or db_camp.commodity
        suppliers = get_suppliers_for_commodity(comm)
        supp_email = suppliers[0].contact_email if suppliers else (settings.my_test_email or "export@supplier.com")
        email_service.send_email(
            supp_email,
            supp_msg,
            in_reply_to=resumed_state.get("last_supplier_message_id"),
            references=resumed_state.get("supplier_references"),
            thread_subject=f"Deal Confirmation & Volume Lock — {comm}",
        )

    if buyer_msg:
        outbound_audit = TradeAuditModel(
            campaign_id=campaign_id,
            thread_id=campaign_id,
            role="agent",
            counterparty_price=resumed_state.get("last_counter_cif_usd") or resumed_state.get("anchor_cif_usd"),
            net_spread=resumed_state.get("net_spread_usd"),
            raw_message=buyer_msg,
            direction="OUTBOUND",
        )
        db.add(outbound_audit)

    db_camp.deal_status = resumed_state.get("deal_status", db_camp.deal_status)
    db.commit()

    trail = resumed_state.get("audit_transcript", [])
    buyer_t = resumed_state.get("buyer_terms")
    supp_t = resumed_state.get("supplier_terms")

    return {
        "campaign_id": campaign_id,
        "deal_status": resumed_state.get("deal_status"),
        "status": resumed_state.get("deal_status"),
        "action": resumed_state.get("action"),
        "deal_approved_by_human": resumed_state.get("deal_approved_by_human"),
        "reviewer_notes": resumed_state.get("reviewer_notes"),
        "override_cif_price": resumed_state.get("override_cif_price"),
        "last_counter_cif_usd": resumed_state.get("last_counter_cif_usd"),
        "evaluation_reason": resumed_state.get("evaluation_reason"),
        "is_deal_viable": resumed_state.get("is_deal_viable"),
        "net_spread_usd": resumed_state.get("net_spread_usd", 0.0),
        "net_margin_pct": resumed_state.get("net_margin_pct"),
        "pipeline_step": resumed_state.get("pipeline_step"),
        "negotiation_round": resumed_state.get("negotiation_round"),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": buyer_msg,
        "supplier_draft": resumed_state.get("supplier_draft"),
        "audit_transcript": trail,
        "execution_trail": trail,
    }
