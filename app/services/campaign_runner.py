"""Autonomous demo: play buyer, supplier and risk officer to run a campaign to completion (never sends email)."""
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from langgraph.types import Command
from sqlalchemy.orm import Session

import app.db.database
from app.config import settings
from app.db.db_models import CampaignModel, desk_party, record_exchange
from app.directory import get_buyers_for_commodity, get_suppliers_for_commodity, select_primary_buyer
from app.graph import workflow
from app.logger import get_logger
from app.models import Campaign, coerce_campaign
from app.pricing.formulas import calculate_landed_cost, resolve_buffer_usd
from app.schemas import ApprovalResumeDecision, AuditTranscriptEntry, AutonomousRunnerResult, model_from_state
from app.telemetry import build_trace_config, flush_telemetry, get_langfuse_handler

logger = get_logger("arbitrage_desk")


def _desk_mailbox(title: str) -> str:
    return f"{title} <trading@{settings.desk_name.lower().replace(' ', '')}.com>"


def build_buyer_interest_email(buyer_name: str, buyer_email: str, campaign: Campaign, cif_price: float) -> str:
    return (
        f"Subject: Re: Soft Corporate Offer (SCO) — {campaign.commodity} CIF {campaign.destination_port}\n\n"
        f"To: {_desk_mailbox('Trading Desk')}\n"
        f"From: {buyer_name} <{buyer_email}>\n\n"
        f"Dear Trading Desk,\n\n"
        f"We acknowledge receipt of your SCO. We are interested in contracting {campaign.target_volume_mt:,.0f} MT "
        f"of {campaign.commodity} for {campaign.destination_port}. "
        f"However, your indicative pitch is above our procurement budget. "
        f"We submit a firm counter-bid of USD {cif_price:.2f}/MT CIF {campaign.destination_port}. "
        f"Payment via 100% LC at sight.\n\n"
        f"Best regards,\nProcurement Team, {buyer_name}"
    )


def build_supplier_quote_email(supp_name: str, supp_email: str, campaign: Campaign, fob_price: float) -> str:
    return (
        f"Subject: Quotation — {campaign.commodity} FOB {campaign.origin_port_default}\n\n"
        f"To: {_desk_mailbox('Procurement Desk')}\n"
        f"From: {supp_name} <{supp_email}>\n\n"
        f"Dear Procurement Desk,\n\n"
        f"In response to your RFQ, we quote {campaign.target_volume_mt:,.0f} MT of export grade {campaign.commodity} "
        f"at USD {fob_price:.2f}/MT FOB {campaign.origin_port_default}. "
        f"Payment terms: {settings.default_payment_terms}. Ready for prompt loading.\n\n"
        f"Best regards,\nExport Sales, {supp_name}"
    )


def build_concession_email(buyer_name: str, buyer_email: str, campaign: Campaign, target_cif: float) -> str:
    return (
        f"Subject: Re: Price Adjustment Request — Concession Agreement\n\n"
        f"To: {_desk_mailbox('Trading Desk')}\n"
        f"From: {buyer_name} <{buyer_email}>\n\n"
        f"Dear Trading Desk,\n\n"
        f"Following your counter-proposal and updated corridor freight analysis, we agree to revise our CIF bid "
        f"to USD {target_cif:.2f}/MT CIF {campaign.destination_port} for {campaign.target_volume_mt:,.0f} MT.\n\n"
        f"Please confirm allocation lock and issue the final SCO acceptance.\n\n"
        f"Best regards,\nProcurement Team, {buyer_name}"
    )


@dataclass(frozen=True)
class _SimulationRun:
    """Everything the steps of one autonomous run share."""

    campaign_id: str
    campaign: Campaign
    buyer_name: str
    buyer_email: str
    supplier_name: str
    supplier_email: str
    landed_cost_baseline: float
    config: dict
    db: Session

    @property
    def buyer(self) -> str:
        return f"{self.buyer_name} <{self.buyer_email}>"

    @property
    def supplier(self) -> str:
        return f"{self.supplier_name} <{self.supplier_email}>"


def _record(
    state: dict,
    run: _SimulationRun,
    turn: int,
    sender: str,
    recipient: str,
    role: str,
    action: str,
    subject: str,
    message: str,
    direction: str,
    price: Optional[float],
) -> None:
    record_exchange(
        state, run.db, run.campaign_id, turn=turn, sender=sender, recipient=recipient, role=role, action=action,
        subject=subject, message=message, direction=direction, counterparty_price=price,
        net_spread=state.get("net_spread_usd"),
    )


def _invoke_turn(state: dict, run: _SimulationRun, email_text: str, role: str) -> dict:
    """Feed one simulated email to the graph; auto-approve if it stops at the human gate."""
    state = workflow.get_trade_graph().invoke(
        {**state, "latest_email": email_text, "active_role": role}, config=run.config
    )
    # Simulation only: the run sets skip_email_dispatch, so this approval can never confirm a live deal.
    if workflow.is_awaiting_approval(run.config):
        decision = ApprovalResumeDecision(approved=True, reviewer_notes="Autonomous campaign auto-approval")
        state = workflow.get_trade_graph().invoke(
            Command(resume=decision.model_dump(exclude_none=True)), config=run.config
        )
    return state


def _run_buyer_bid(state: dict, run: _SimulationRun) -> dict:
    camp = run.campaign
    bid = round(run.landed_cost_baseline + camp.min_profit_per_mt_hard + settings.sco_anchor_premium_usd_per_mt, 2)
    bid_email = build_buyer_interest_email(run.buyer_name, run.buyer_email, camp, bid)
    state = _invoke_turn(state, run, bid_email, "buyer")
    _record(state, run, 1, run.buyer, desk_party(), "buyer", "INBOUND_BID",
            f"Re: SCO — {camp.commodity} CIF {camp.destination_port}", bid_email, "INBOUND", bid)
    _record(state, run, 1, desk_party("Procurement Desk"), run.supplier, "agent", "OUTBOUND_RFQ",
            f"Urgent RFQ — {camp.commodity} FOB {camp.origin_port_default}", state.get("supplier_draft", ""),
            "OUTBOUND", state.get("target_fob_ceiling"))
    return state


def _run_supplier_quote(state: dict, run: _SimulationRun) -> dict:
    camp = run.campaign
    fob = state["benchmark_fob_usd"]
    quote_email = build_supplier_quote_email(run.supplier_name, run.supplier_email, camp, fob)
    state = _invoke_turn(state, run, quote_email, "supplier")
    _record(
        state,
        run,
        state.get("negotiation_round", 2),
        run.supplier,
        desk_party("Procurement Desk"),
        "supplier",
        "INBOUND_QUOTE",
        f"Quotation — {camp.commodity} FOB {camp.origin_port_default}",
        quote_email,
        "INBOUND",
        fob,
    )
    return state


def _run_concession_round(state: dict, run: _SimulationRun) -> dict:
    counter_draft = state.get("buyer_draft") if state.get("active_role") == "buyer" else state.get("supplier_draft")
    turn = state.get("negotiation_round", 2)
    _record(state, run, turn, desk_party(), run.buyer, "agent", "OUTBOUND_COUNTER",
            f"Negotiation Round {turn}: Tactical Adjustment Request",
            counter_draft or f"Tactical counter to maximize margin: {state.get('evaluation_reason')}",
            "OUTBOUND", state.get("last_counter_cif_usd"))

    target_cif = round(run.landed_cost_baseline + run.campaign.min_profit_per_mt_soft, 2)
    concession_email = build_concession_email(run.buyer_name, run.buyer_email, run.campaign, target_cif)
    state = _invoke_turn(state, run, concession_email, "buyer")
    _record(state, run, state.get("negotiation_round", turn + 1), run.buyer, desk_party(), "buyer",
            "INBOUND_CONCESSION", "Re: Price Adjustment Request — Concession Agreement", concession_email,
            "INBOUND", target_cif)
    return state


def _run_concession_loop(state: dict, run: _SimulationRun) -> dict:
    # Hard cap on top of the graph's own round limit so a parser that never moves the price cannot loop forever.
    for _ in range(run.campaign.max_negotiation_rounds + 2):
        if state.get("deal_status") in ("closed", "rejected") or state.get("action") != "COUNTER_TO_MAXIMIZE":
            break
        state = _run_concession_round(state, run)
    return state


def _record_closing_emails(state: dict, run: _SimulationRun) -> None:
    if state.get("action") != "ACCEPT_AND_CLOSE" and state.get("deal_status") != "closed":
        return
    turn = state.get("negotiation_round", 3)
    supplier_terms, buyer_terms = state.get("supplier_terms"), state.get("buyer_terms")
    commodity = run.campaign.commodity
    _record(state, run, turn, desk_party("Procurement Desk"), run.supplier, "agent", "LOCK_SUPPLIER_ALLOCATION",
            f"Deal Confirmation & Volume Lock — {commodity}", state.get("supplier_draft", ""), "OUTBOUND",
            supplier_terms.price_usd_per_mt if supplier_terms else None)
    _record(state, run, turn, desk_party(), run.buyer, "agent", "ACCEPT_AND_CLOSE",
            f"Soft Corporate Offer (SCO) Acceptance — {commodity}", state.get("buyer_draft", ""), "OUTBOUND",
            buyer_terms.price_usd_per_mt if buyer_terms else None)


def _persist_final_state(state: dict, db_camp: CampaignModel, run: _SimulationRun) -> None:
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


def _start_run(campaign_id: str, state: dict, config: dict, db: Session) -> _SimulationRun:
    campaign = coerce_campaign(state["campaign"])
    buyer_name, buyer_email = _party(
        select_primary_buyer(campaign.commodity), "Procurement Partner", "procurement@domain.com"
    )
    suppliers = get_suppliers_for_commodity(campaign.commodity)
    supplier_name, supplier_email = _party(
        suppliers[0] if suppliers else None, "Asian Rice Exporters", "export@supplier.com"
    )
    landed = calculate_landed_cost(state["benchmark_fob_usd"], state["freight_cost_usd"], resolve_buffer_usd(campaign))
    return _SimulationRun(
        campaign_id, campaign, buyer_name, buyer_email, supplier_name, supplier_email, landed, config, db
    )


def _ensure_opening_transcript(state: dict, run: _SimulationRun) -> None:
    if state.get("audit_transcript"):
        return
    entry = AuditTranscriptEntry(
        turn=0, sender=desk_party(), recipient=run.buyer, role="agent", action="OUTBOUND_SCO",
        subject=f"Soft Corporate Offer (SCO) — {run.campaign.commodity} CIF {run.campaign.destination_port}",
        message=state.get("buyer_draft", ""),
    )
    state["audit_transcript"] = [entry.model_dump(exclude_none=True)]


def _build_result(state: dict, run: _SimulationRun) -> dict:
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
    state = _run_buyer_bid(state, run)
    state = _run_supplier_quote(state, run)
    state = _run_concession_loop(state, run)
    _record_closing_emails(state, run)
    _persist_final_state(state, db_camp, run)
    result = _build_result(state, run)
    flush_telemetry()
    return result


def run_full_autonomous_campaign(campaign_id: str, db: Optional[Session] = None) -> dict:
    if db is not None:
        return _run_campaign(campaign_id, db)
    with app.db.database.SessionLocal() as own_db:
        return _run_campaign(campaign_id, own_db)
