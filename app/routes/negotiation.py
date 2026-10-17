"""POST /api/negotiate: run one simulated counterparty email through the graph."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agents import workflow
from app.api.schemas import NegotiateTurnResponse, SimulateTurnRequest
from app.db import CampaignModel, get_db
from app.logger import get_logger
from app.services.audit import add_trade_audit
from app.utils.state_mapping import model_from_state
from app.utils.telemetry import build_trace_config, flush_telemetry, get_langfuse_handler

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
