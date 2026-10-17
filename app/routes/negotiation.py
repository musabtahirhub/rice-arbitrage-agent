"""Negotiation routes: simulate one counterparty email (POST /api/negotiate), and the human approval
(POST /api/campaigns/{id}/approve). Zero-risk invariant: the supplier volume lock is dispatched first;
the buyer is confirmed only once it went out.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from langgraph.types import Command
from sqlalchemy.orm import Session

from app.config import settings
from app.db.database import get_db
from app.db.db_models import CampaignModel, add_trade_audit
from app.directory import resolve_supplier_contact, select_primary_buyer
from app.graph import workflow
from app.logger import get_logger
from app.models import ApprovalPayload, SimulateTurnRequest
from app.schemas import ApproveDealResponse, NegotiateTurnResponse, model_from_state
from app.services.mail.gmail_client import send_reply
from app.telemetry import build_trace_config, flush_telemetry, get_langfuse_handler

logger = get_logger("arbitrage_desk")
router = APIRouter()


def _prepare_turn_state(base_state: dict, raw_email: str, sender_role: str) -> dict:
    return {
        **base_state,
        "latest_email": raw_email,
        "active_role": sender_role,
        "buyer_draft": "",
        "supplier_draft": "",
    }


def _record_inbound_turn(req: SimulateTurnRequest, updated_state: dict, db: Session) -> None:
    buyer_terms, supplier_terms = updated_state.get("buyer_terms"), updated_state.get("supplier_terms")
    terms = buyer_terms if (req.sender_role == "buyer" and buyer_terms) else supplier_terms
    add_trade_audit(db, req.campaign_id, req.sender_role, req.raw_email, "INBOUND",
                    counterparty_price=terms.price_usd_per_mt if terms else None,
                    net_spread=updated_state.get("net_spread_usd"))


def _record_outbound_turn(req: SimulateTurnRequest, updated_state: dict, db: Session) -> None:
    # Drafts are cleared at the start of each turn, so any non-empty draft was produced by this turn.
    # Prefer the reply to the counterparty who wrote in, then fall back to the other side (e.g. a supplier RFQ).
    if req.sender_role == "supplier":
        outbound_msg = updated_state.get("supplier_draft") or updated_state.get("buyer_draft")
    else:
        outbound_msg = updated_state.get("buyer_draft") or updated_state.get("supplier_draft")
    if outbound_msg and updated_state.get("deal_status") != "pending_approval":
        add_trade_audit(db, req.campaign_id, "agent", outbound_msg, "OUTBOUND",
                        counterparty_price=updated_state.get("anchor_cif_usd"),
                        net_spread=updated_state.get("net_spread_usd"))


def _load_negotiable_state(req: SimulateTurnRequest, config: dict) -> dict:
    state = workflow.get_state_values(config)
    if not state:
        logger.warning(f"[API NEGOTIATE] State for campaign '{req.campaign_id}' not found.")
        raise HTTPException(status_code=404, detail=f"State for campaign {req.campaign_id} not found.")
    if workflow.is_awaiting_approval(config):
        # Running new input now would start a fresh run and bypass the pending human-approval interrupt.
        raise HTTPException(
            status_code=409,
            detail=f"Campaign {req.campaign_id} is awaiting human approval; "
            f"resolve it via /api/campaigns/{req.campaign_id}/approve first.",
        )
    return state


@router.post("/api/negotiate")
def negotiate_turn(req: SimulateTurnRequest, db: Session = Depends(get_db)):
    logger.info(f"[API NEGOTIATE] Inbound turn for campaign '{req.campaign_id}' from role='{req.sender_role}'")
    db_camp = db.query(CampaignModel).filter(CampaignModel.id == req.campaign_id).first()
    if not db_camp:
        logger.warning(f"[API NEGOTIATE] Campaign '{req.campaign_id}' not found in database.")
        raise HTTPException(status_code=404, detail=f"Campaign {req.campaign_id} not found.")

    config = build_trace_config(
        req.campaign_id, "negotiation-turn", get_langfuse_handler(),
        metadata={"sender_role": req.sender_role}, tags=["negotiation-turn"],
    )
    state = _load_negotiable_state(req, config)
    updated_state = workflow.get_trade_graph().invoke(
        _prepare_turn_state(state, req.raw_email, req.sender_role), config=config
    )
    flush_telemetry()
    workflow.mark_pending_approval(config, updated_state)

    _record_inbound_turn(req, updated_state, db)
    _record_outbound_turn(req, updated_state, db)
    db_camp.deal_status = updated_state.get("deal_status", db_camp.deal_status)
    db.commit()

    logger.info(
        f"[API NEGOTIATE] Turn completed for '{req.campaign_id}' | Round {updated_state.get('negotiation_round')} | "
        f"Action: {updated_state.get('action')} | Status: {updated_state.get('deal_status')} | "
        f"Spread: ${updated_state.get('net_spread_usd', 0.0):.2f}/MT | "
        f"Margin: {updated_state.get('net_margin_pct', 0.0):.2f}%"
    )
    return model_from_state(NegotiateTurnResponse, updated_state, campaign_id=req.campaign_id).model_dump()


def _send_via_configured_transport(
    to_email: str, draft: str, in_reply_to: Optional[str], references: Optional[str],
    thread_subject: Optional[str], gmail_thread_id: Optional[str] = None,
) -> bool:
    return send_reply(to_email, draft, in_reply_to, references, thread_subject, gmail_thread_id,
                      use_gmail_api=settings.use_push_webhooks)


def _commodity_of(resumed_state: dict, db_camp: CampaignModel) -> str:
    return getattr(resumed_state.get("campaign"), "commodity", None) or db_camp.commodity


def _dispatch_buyer_approval_email(resumed_state: dict, db_camp: CampaignModel) -> bool:
    buyer_msg = resumed_state.get("buyer_draft")
    if not buyer_msg or resumed_state.get("skip_email_dispatch", False):
        return True
    primary_buyer = select_primary_buyer(_commodity_of(resumed_state, db_camp))
    buyer_email = (
        resumed_state.get("target_buyer_email")
        or (settings.my_test_email.strip() if settings.my_test_email else None)
        or (primary_buyer.contact_email if primary_buyer else "procurement@domain.com")
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
    commodity = _commodity_of(resumed_state, db_camp)
    _, supp_email = resolve_supplier_contact(commodity, resumed_state.get("target_supplier_email"))
    return _send_via_configured_transport(
        supp_email,
        supp_msg,
        resumed_state.get("last_supplier_message_id"),
        resumed_state.get("supplier_references"),
        f"Deal Confirmation & Volume Lock — {commodity}",
    )


def _load_pending_approval(campaign_id: str, config: dict) -> None:
    snapshot = workflow.get_trade_graph().get_state(config)
    if not snapshot or not snapshot.values:
        logger.warning(f"[API APPROVE] State for campaign '{campaign_id}' not found.")
        raise HTTPException(status_code=404, detail=f"State for campaign {campaign_id} not found.")
    if not workflow.is_awaiting_approval(config):
        logger.warning(
            f"[API APPROVE] Campaign '{campaign_id}' has no pending approval "
            f"(status: '{snapshot.values.get('deal_status')}')."
        )
        raise HTTPException(status_code=409, detail=f"Campaign {campaign_id} is not awaiting approval.")


def _dispatch_confirmations(campaign_id: str, resumed_state: dict, db_camp: CampaignModel, config: dict) -> None:
    if not _dispatch_supplier_approval_email(resumed_state, db_camp):
        logger.error(
            f"[API APPROVE] Supplier volume-lock dispatch failed for campaign '{campaign_id}'. "
            f"Buyer confirmation withheld to avoid an uncovered short position."
        )
        workflow.get_trade_graph().update_state(config, {"deal_status": "supplier_lock_failed"})
        resumed_state["deal_status"] = "supplier_lock_failed"
        resumed_state["buyer_draft"] = ""
    elif not _dispatch_buyer_approval_email(resumed_state, db_camp):
        logger.error(f"[API APPROVE] Buyer email dispatch failed for campaign '{campaign_id}'.")


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

    config = build_trace_config(
        campaign_id, "hitl-approval", get_langfuse_handler(),
        metadata={"action": "HITL_DECISION", "approved": payload.approved}, tags=["hitl-approval"],
    )
    _load_pending_approval(campaign_id, config)
    resumed_state = workflow.get_trade_graph().invoke(Command(resume=payload.model_dump()), config=config)
    flush_telemetry()
    _dispatch_confirmations(campaign_id, resumed_state, db_camp, config)

    buyer_msg = resumed_state.get("buyer_draft")
    if buyer_msg:
        add_trade_audit(
            db,
            campaign_id,
            "agent",
            buyer_msg,
            "OUTBOUND",
            counterparty_price=resumed_state.get("last_counter_cif_usd") or resumed_state.get("anchor_cif_usd"),
            net_spread=resumed_state.get("net_spread_usd"),
        )
    db_camp.deal_status = resumed_state.get("deal_status", db_camp.deal_status)
    db.commit()

    trail = resumed_state.get("audit_transcript", [])
    return model_from_state(
        ApproveDealResponse, resumed_state,
        campaign_id=campaign_id, status=resumed_state.get("deal_status"), buyer_draft=buyer_msg,
        audit_transcript=trail, execution_trail=trail,
    ).model_dump()
