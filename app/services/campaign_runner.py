"""Autonomous demo: play buyer, supplier and risk officer to run a campaign to completion (never sends email)."""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import app.db
from app.agents import workflow
from app.agents.state import coerce_campaign
from app.api.schemas import AutonomousRunnerResult
from app.db import CampaignModel
from app.logger import get_logger
from app.models import AuditTranscriptEntry
from app.services.audit import desk_party
from app.services.campaign_steps import (
    SimulationRun,
    record_closing_emails,
    run_buyer_bid,
    run_concession_loop,
    run_supplier_quote,
)
from app.services.directory import get_buyers_for_commodity, get_suppliers_for_commodity, select_primary_buyer
from app.services.pricing import calculate_landed_cost, resolve_buffer_usd
from app.utils.state_mapping import model_from_state
from app.utils.telemetry import build_trace_config, flush_telemetry, get_langfuse_handler

logger = get_logger("arbitrage_desk")


def _persist_final_state(state: dict, db_camp: CampaignModel, run: SimulationRun) -> None:
    db_camp.deal_status = state.get("deal_status", "closed")
    run.db.commit()
    final_keys = ("audit_transcript", "deal_status", "action", "buyer_draft", "supplier_draft")
    defaults = {"audit_transcript": [], "deal_status": "closed", "action": "ACCEPT_AND_CLOSE"}
    try:
        workflow.get_trade_graph().update_state(run.config, {k: state.get(k, defaults.get(k)) for k in final_keys})
    except Exception as exc:
        logger.warning(f"Could not update graph state: {exc}")


def _party(counterparty, fallback_name: str, fallback_email: str) -> tuple[str, str]:
    return (counterparty.name, counterparty.contact_email) if counterparty else (fallback_name, fallback_email)


def _start_run(campaign_id: str, state: dict, config: dict, db: Session) -> SimulationRun:
    campaign = coerce_campaign(state["campaign"])
    buyer_name, buyer_email = _party(
        select_primary_buyer(campaign.commodity), "Procurement Partner", "procurement@domain.com"
    )
    suppliers = get_suppliers_for_commodity(campaign.commodity)
    supplier_name, supplier_email = _party(
        suppliers[0] if suppliers else None, "Asian Rice Exporters", "export@supplier.com"
    )
    landed = calculate_landed_cost(state["benchmark_fob_usd"], state["freight_cost_usd"], resolve_buffer_usd(campaign))
    return SimulationRun(
        campaign_id, campaign, buyer_name, buyer_email, supplier_name, supplier_email, landed, config, db
    )


def _ensure_opening_transcript(state: dict, run: SimulationRun) -> None:
    if state.get("audit_transcript"):
        return
    entry = AuditTranscriptEntry(
        turn=0, sender=desk_party(), recipient=run.buyer, role="agent", action="OUTBOUND_SCO",
        subject=f"Soft Corporate Offer (SCO) — {run.campaign.commodity} CIF {run.campaign.destination_port}",
        message=state.get("buyer_draft", ""),
    )
    state["audit_transcript"] = [entry.model_dump(exclude_none=True)]


def _build_result(state: dict, run: SimulationRun) -> dict:
    commodity = run.campaign.commodity
    return model_from_state(
        AutonomousRunnerResult, state,
        campaign_id=run.campaign_id,
        commodity=commodity,
        negotiation_rounds_completed=state.get("negotiation_round", 0),
        final_net_spread_usd=state.get("net_spread_usd", 0.0),
        final_net_margin_pct=state.get("net_margin_pct", 0.0),
        discovered_buyers=[b.model_dump() for b in get_buyers_for_commodity(commodity)],
        discovered_suppliers=[s.model_dump() for s in get_suppliers_for_commodity(commodity)],
        status=state.get("deal_status", "closed"),
    ).model_dump()


def _run_campaign(campaign_id: str, db: Session) -> dict:
    db_camp = db.query(CampaignModel).filter(CampaignModel.id == campaign_id).first()
    if not db_camp:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")
    config = build_trace_config(campaign_id, "autonomous-campaign", get_langfuse_handler(),
                                metadata={"mode": "autonomous"}, tags=["autonomous"])
    state = workflow.get_state_values(config)
    if not state:
        raise HTTPException(status_code=404, detail=f"State for campaign {campaign_id} not found.")

    run = _start_run(campaign_id, state, config, db)
    state["skip_email_dispatch"] = True
    _ensure_opening_transcript(state, run)
    state = run_buyer_bid(state, run)
    state = run_supplier_quote(state, run)
    state = run_concession_loop(state, run)
    record_closing_emails(state, run)
    _persist_final_state(state, db_camp, run)
    result = _build_result(state, run)
    flush_telemetry()
    return result


def run_full_autonomous_campaign(campaign_id: str, db: Optional[Session] = None) -> dict:
    if db is not None:
        return _run_campaign(campaign_id, db)
    with app.db.SessionLocal() as own_db:
        return _run_campaign(campaign_id, own_db)
