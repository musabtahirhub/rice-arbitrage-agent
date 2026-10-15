import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from langgraph.types import Command

from app.config import settings
from app.database import get_db
from app.db_models import CampaignModel, TradeAuditModel
from app.directory import get_buyers_for_commodity, resolve_supplier_contact
import app.email_service as email_service
from app.models import ApprovalPayload, SimulateTurnRequest
import app.workflow
from app.telemetry import build_trace_config, flush_telemetry, get_langfuse_handler

logger = logging.getLogger("arbitrage_desk")
router = APIRouter()


def _prepare_turn_state(base_state: dict, raw_email: str, sender_role: str) -> dict:
    turn_state = dict(base_state)
    turn_state["latest_email"] = raw_email
    turn_state["active_role"] = sender_role
    turn_state["buyer_draft"] = ""
    turn_state["supplier_draft"] = ""
    return turn_state


def _mark_pending_approval_if_needed(config: dict, updated_state: dict) -> None:
    if app.workflow.is_awaiting_approval(config):
        app.workflow.get_trade_graph().update_state(config, {"deal_status": "pending_approval"})
        updated_state["deal_status"] = "pending_approval"


def _record_inbound_turn_audit(req: SimulateTurnRequest, updated_state: dict, db: Session) -> None:
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


def _record_outbound_turn_audit(req: SimulateTurnRequest, updated_state: dict, db: Session) -> None:
    # Drafts are cleared at the start of each turn, so any non-empty draft was produced by this turn.
    # Prefer the reply to the counterparty who wrote in, then fall back to the other side (e.g. a supplier RFQ).
    if req.sender_role == "supplier":
        outbound_msg = updated_state.get("supplier_draft") or updated_state.get("buyer_draft")
    else:
        outbound_msg = updated_state.get("buyer_draft") or updated_state.get("supplier_draft")
    if outbound_msg and updated_state.get("deal_status") != "pending_approval":
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


from app.schemas import ApproveDealResponse, NegotiateTurnResponse


def _build_negotiate_turn_response(campaign_id: str, updated_state: dict) -> dict:
    buyer_t = updated_state.get("buyer_terms")
    supp_t = updated_state.get("supplier_terms")
    resp = NegotiateTurnResponse(
        campaign_id=campaign_id,
        negotiation_round=updated_state.get("negotiation_round", 1),
        deal_status=updated_state.get("deal_status"),
        action=updated_state.get("action"),
        pipeline_step=updated_state.get("pipeline_step", 1),
        is_deal_viable=updated_state.get("is_deal_viable"),
        net_spread_usd=updated_state.get("net_spread_usd", 0.0),
        net_margin_pct=updated_state.get("net_margin_pct"),
        evaluation_reason=updated_state.get("evaluation_reason"),
        target_fob_ceiling=updated_state.get("target_fob_ceiling", 0.0),
        buyer_terms=buyer_t.model_dump() if buyer_t else None,
        supplier_terms=supp_t.model_dump() if supp_t else None,
        buyer_draft=updated_state.get("buyer_draft"),
        supplier_draft=updated_state.get("supplier_draft"),
        audit_transcript=updated_state.get("audit_transcript", []),
    )
    return resp.model_dump()


@router.post("/api/negotiate")
def negotiate_turn(req: SimulateTurnRequest, db: Session = Depends(get_db)):
    logger.info(f"[API NEGOTIATE] Inbound turn for campaign '{req.campaign_id}' from role='{req.sender_role}'")
    db_camp = db.query(CampaignModel).filter(CampaignModel.id == req.campaign_id).first()
    if not db_camp:
        logger.warning(f"[API NEGOTIATE] Campaign '{req.campaign_id}' not found in database.")
        raise HTTPException(status_code=404, detail=f"Campaign {req.campaign_id} not found.")

    langfuse_handler = get_langfuse_handler()
    config = build_trace_config(
        req.campaign_id,
        "negotiation-turn",
        langfuse_handler,
        metadata={"sender_role": req.sender_role},
        tags=["negotiation-turn"],
    )
    snapshot = app.workflow.get_trade_graph().get_state(config)
    state = dict(snapshot.values) if snapshot and snapshot.values else {}
    if not state:
        logger.warning(f"[API NEGOTIATE] State for campaign '{req.campaign_id}' not found.")
        raise HTTPException(status_code=404, detail=f"State for campaign {req.campaign_id} not found.")

    if app.workflow.is_awaiting_approval(config):
        # Running new input now would start a fresh run and bypass the pending human-approval interrupt.
        raise HTTPException(
            status_code=409,
            detail=f"Campaign {req.campaign_id} is awaiting human approval; resolve it via /api/campaigns/{req.campaign_id}/approve first.",
        )

    turn_state = _prepare_turn_state(state, req.raw_email, req.sender_role)
    updated_state = app.workflow.get_trade_graph().invoke(turn_state, config=config)
    flush_telemetry()
    _mark_pending_approval_if_needed(config, updated_state)

    _record_inbound_turn_audit(req, updated_state, db)
    _record_outbound_turn_audit(req, updated_state, db)

    db_camp.deal_status = updated_state.get("deal_status", db_camp.deal_status)
    db.commit()

    logger.info(
        f"[API NEGOTIATE] Turn completed for '{req.campaign_id}' | Round {updated_state.get('negotiation_round')} | "
        f"Action: {updated_state.get('action')} | Status: {updated_state.get('deal_status')} | "
        f"Spread: ${updated_state.get('net_spread_usd', 0.0):.2f}/MT | Margin: {updated_state.get('net_margin_pct', 0.0):.2f}%"
    )

    return _build_negotiate_turn_response(req.campaign_id, updated_state)


def _send_via_configured_transport(
    to_email: str,
    draft: str,
    in_reply_to: Optional[str],
    references: Optional[str],
    thread_subject: Optional[str],
    gmail_thread_id: Optional[str] = None,
) -> bool:
    if settings.use_push_webhooks:
        from app.gmail_client import send_gmail_reply
        return bool(send_gmail_reply(
            to_email=to_email,
            raw_draft=draft,
            in_reply_to=in_reply_to,
            references=references,
            thread_subject=thread_subject,
            thread_id=gmail_thread_id,
        ))
    return bool(email_service.send_email(
        to_email,
        draft,
        in_reply_to=in_reply_to,
        references=references,
        thread_subject=thread_subject,
    ))


def _dispatch_buyer_approval_email(resumed_state: dict, db_camp: CampaignModel) -> bool:
    buyer_msg = resumed_state.get("buyer_draft")
    if not buyer_msg or resumed_state.get("skip_email_dispatch", False):
        return True

    camp = resumed_state.get("campaign")
    comm = getattr(camp, "commodity", None) or db_camp.commodity
    buyers = get_buyers_for_commodity(comm)
    buyer_email = (
        resumed_state.get("target_buyer_email")
        or (settings.my_test_email.strip() if settings.my_test_email else None)
        or (buyers[0].contact_email if buyers else "procurement@domain.com")
    )
    return _send_via_configured_transport(
        buyer_email,
        buyer_msg,
        resumed_state.get("last_buyer_message_id"),
        resumed_state.get("buyer_references"),
        resumed_state.get("thread_subject"),
        resumed_state.get("gmail_thread_id"),
    )


def _dispatch_supplier_approval_email(resumed_state: dict, db_camp: CampaignModel) -> bool:
    supp_msg = resumed_state.get("supplier_draft")
    if not supp_msg or resumed_state.get("deal_status") != "closed" or resumed_state.get("skip_email_dispatch", False):
        return True

    camp = resumed_state.get("campaign")
    comm = getattr(camp, "commodity", None) or db_camp.commodity
    _, supp_email = resolve_supplier_contact(comm, resumed_state.get("target_supplier_email"))
    return _send_via_configured_transport(
        supp_email,
        supp_msg,
        resumed_state.get("last_supplier_message_id"),
        resumed_state.get("supplier_references"),
        f"Deal Confirmation & Volume Lock — {comm}",
    )


def _record_approval_audit_trail(campaign_id: str, buyer_msg: Optional[str], resumed_state: dict, db: Session) -> None:
    if not buyer_msg:
        return
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


def _build_approve_deal_response(campaign_id: str, resumed_state: dict, buyer_msg: Optional[str]) -> dict:
    trail = resumed_state.get("audit_transcript", [])
    buyer_t = resumed_state.get("buyer_terms")
    supp_t = resumed_state.get("supplier_terms")
    resp = ApproveDealResponse(
        campaign_id=campaign_id,
        deal_status=resumed_state.get("deal_status"),
        status=resumed_state.get("deal_status"),
        action=resumed_state.get("action"),
        deal_approved_by_human=resumed_state.get("deal_approved_by_human"),
        reviewer_notes=resumed_state.get("reviewer_notes"),
        override_cif_price=resumed_state.get("override_cif_price"),
        last_counter_cif_usd=resumed_state.get("last_counter_cif_usd"),
        evaluation_reason=resumed_state.get("evaluation_reason"),
        is_deal_viable=resumed_state.get("is_deal_viable"),
        net_spread_usd=resumed_state.get("net_spread_usd", 0.0),
        net_margin_pct=resumed_state.get("net_margin_pct"),
        pipeline_step=resumed_state.get("pipeline_step"),
        negotiation_round=resumed_state.get("negotiation_round"),
        buyer_terms=buyer_t.model_dump() if buyer_t else None,
        supplier_terms=supp_t.model_dump() if supp_t else None,
        buyer_draft=buyer_msg,
        supplier_draft=resumed_state.get("supplier_draft"),
        audit_transcript=trail,
        execution_trail=trail,
    )
    return resp.model_dump()


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

    langfuse_handler = get_langfuse_handler()
    config = build_trace_config(
        campaign_id,
        "hitl-approval",
        langfuse_handler,
        metadata={"action": "HITL_DECISION", "approved": payload.approved},
        tags=["hitl-approval"],
    )
    snapshot = app.workflow.get_trade_graph().get_state(config)
    if not snapshot or not snapshot.values:
        logger.warning(f"[API APPROVE] State for campaign '{campaign_id}' not found.")
        raise HTTPException(status_code=404, detail=f"State for campaign {campaign_id} not found.")
    if not app.workflow.is_awaiting_approval(config):
        logger.warning(f"[API APPROVE] Campaign '{campaign_id}' has no pending approval (status: '{snapshot.values.get('deal_status')}').")
        raise HTTPException(status_code=409, detail=f"Campaign {campaign_id} is not awaiting approval.")

    resumed_state = app.workflow.get_trade_graph().invoke(Command(resume=payload.model_dump()), config=config)
    flush_telemetry()

    # Zero-risk invariant: lock the supplier first; the buyer is only confirmed once that lock was dispatched.
    if not _dispatch_supplier_approval_email(resumed_state, db_camp):
        logger.error(
            f"[API APPROVE] Supplier volume-lock dispatch failed for campaign '{campaign_id}'. "
            f"Buyer confirmation withheld to avoid an uncovered short position."
        )
        app.workflow.get_trade_graph().update_state(config, {"deal_status": "supplier_lock_failed"})
        resumed_state["deal_status"] = "supplier_lock_failed"
        resumed_state["buyer_draft"] = ""
    elif not _dispatch_buyer_approval_email(resumed_state, db_camp):
        logger.error(f"[API APPROVE] Buyer email dispatch failed for campaign '{campaign_id}'.")

    buyer_msg = resumed_state.get("buyer_draft")
    _record_approval_audit_trail(campaign_id, buyer_msg, resumed_state, db)

    db_camp.deal_status = resumed_state.get("deal_status", db_camp.deal_status)
    db.commit()

    return _build_approve_deal_response(campaign_id, resumed_state, buyer_msg)
