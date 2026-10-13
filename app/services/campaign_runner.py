import logging
from typing import Optional
from fastapi import HTTPException
from sqlalchemy.orm import Session
from langgraph.types import Command

from app.config import settings
import app.database
from app.db_models import CampaignModel, TradeAuditModel
from app.directory import get_buyers_for_commodity, get_suppliers_for_commodity
from app.models import Campaign
import app.workflow
from app.logger import setup_logger
from app.telemetry import get_langfuse_handler

logger = setup_logger("arbitrage_desk")


from app.schemas import (
    ApprovalResumeDecision,
    AuditTranscriptEntry,
    AutonomousRunnerResult,
)


def _resolve_autonomous_counterparties(commodity: str) -> tuple:
    buyers = get_buyers_for_commodity(commodity)
    suppliers = get_suppliers_for_commodity(commodity)
    me_buyers = [b for b in buyers if b.country in ["UAE", "Saudi Arabia", "Qatar", "Kuwait", "Oman", "Bahrain"]]
    primary_buyer = me_buyers[0] if me_buyers else (buyers[0] if buyers else None)
    target_supplier = suppliers[0] if suppliers else None

    buyer_name = primary_buyer.name if primary_buyer else "Procurement Partner"
    buyer_email = primary_buyer.contact_email if primary_buyer else "procurement@domain.com"
    supp_name = target_supplier.name if target_supplier else "Asian Rice Exporters"
    supp_email = target_supplier.contact_email if target_supplier else "export@supplier.com"
    return buyers, suppliers, buyer_name, buyer_email, supp_name, supp_email


def _ensure_initial_transcript(state: dict, campaign: Campaign, buyer_name: str, buyer_email: str) -> None:
    if "audit_transcript" not in state or not state["audit_transcript"]:
        entry = AuditTranscriptEntry(
            turn=0,
            sender=f"Trading Desk ({settings.desk_name})",
            recipient=f"{buyer_name} <{buyer_email}>",
            role="agent",
            action="OUTBOUND_SCO",
            subject=f"Soft Corporate Offer (SCO) — {campaign.commodity} CIF {campaign.destination_port}",
            message=state.get("buyer_draft", ""),
        )
        state["audit_transcript"] = [entry.model_dump(exclude_none=True)]


def _append_audit_and_transcript(
    state: dict,
    db: Session,
    campaign_id: str,
    turn: int,
    sender: str,
    recipient: str,
    role: str,
    action: str,
    subject: str,
    message: str,
    direction: str,
    counterparty_price: Optional[float] = None,
) -> None:
    entry = AuditTranscriptEntry(
        turn=turn,
        sender=sender,
        recipient=recipient,
        role=role,
        action=action,
        subject=subject,
        message=message,
    )
    state["audit_transcript"].append(entry.model_dump(exclude_none=True))
    db.add(TradeAuditModel(
        campaign_id=campaign_id,
        thread_id=campaign_id,
        role=role,
        counterparty_price=counterparty_price,
        net_spread=state.get("net_spread_usd"),
        raw_message=message or "",
        direction=direction,
    ))

def _resume_graph_if_awaiting_approval(state: dict, config: dict) -> dict:
    snapshot = app.workflow.get_trade_graph().get_state(config)
    if snapshot and snapshot.next and "approval_gate" in snapshot.next:
        resume_payload = ApprovalResumeDecision(approved=True, reviewer_notes="Autonomous campaign auto-approval")
        return app.workflow.get_trade_graph().invoke(
            Command(resume=resume_payload.model_dump(exclude_none=True)),
            config=config,
        )
    return state

def _build_buyer_interest_email(buyer_name: str, buyer_email: str, commodity: str, dest_port: str, volume: float, cif_price: float) -> str:
    return (
        f"Subject: Re: Soft Corporate Offer (SCO) — {commodity} CIF {dest_port}\n\n"
        f"To: Trading Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
        f"From: {buyer_name} <{buyer_email}>\n\n"
        f"Dear Trading Desk,\n\n"
        f"We acknowledge receipt of your SCO. We are interested in contracting {volume:,.0f} MT of {commodity} for {dest_port}. "
        f"However, your indicative pitch is above our procurement budget. "
        f"We submit a firm counter-bid of USD {cif_price:.2f}/MT CIF {dest_port}. Payment via 100% LC at sight.\n\n"
        f"Best regards,\nProcurement Team, {buyer_name}"
    )

def _build_supplier_quote_email(supp_name: str, supp_email: str, commodity: str, origin_port: str, volume: float, fob_price: float) -> str:
    return (
        f"Subject: Quotation — {commodity} FOB {origin_port}\n\n"
        f"To: Procurement Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
        f"From: {supp_name} <{supp_email}>\n\n"
        f"Dear Procurement Desk,\n\n"
        f"In response to your RFQ, we quote {volume:,.0f} MT of export grade {commodity} at "
        f"USD {fob_price:.2f}/MT FOB {origin_port}. Payment terms: 100% LC at sight. Ready for prompt loading.\n\n"
        f"Best regards,\nExport Sales, {supp_name}"
    )

def _build_concession_email(buyer_name: str, buyer_email: str, commodity: str, dest_port: str, volume: float, target_cif: float) -> str:
    return (
        f"Subject: Re: Price Adjustment Request — Concession Agreement\n\n"
        f"To: Trading Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
        f"From: {buyer_name} <{buyer_email}>\n\n"
        f"Dear Trading Desk,\n\n"
        f"Following your counter-proposal and updated corridor freight analysis, we agree to revise our CIF bid "
        f"to USD {target_cif:.2f}/MT CIF {dest_port} for {volume:,.0f} MT.\n\n"
        f"Please confirm allocation lock and issue the final SCO acceptance.\n\n"
        f"Best regards,\nProcurement Team, {buyer_name}"
    )

def _execute_buyer_bid_step(
    state: dict,
    campaign_id: str,
    campaign: Campaign,
    buyer_name: str,
    buyer_email: str,
    landed_cost_baseline: float,
    supp_name: str,
    supp_email: str,
    config: dict,
    db: Session,
) -> dict:
    initial_buyer_cif = round(landed_cost_baseline + campaign.min_profit_per_mt_hard + settings.sco_anchor_premium_usd_per_mt, 2)
    buyer_interest_email = _build_buyer_interest_email(
        buyer_name, buyer_email, campaign.commodity, campaign.destination_port, campaign.target_volume_mt, initial_buyer_cif
    )

    state["latest_email"] = buyer_interest_email
    state["active_role"] = "buyer"
    state = app.workflow.get_trade_graph().invoke(state, config=config)

    _append_audit_and_transcript(
        state, db, campaign_id, 1, f"{buyer_name} <{buyer_email}>", f"Trading Desk ({settings.desk_name})",
        "buyer", "INBOUND_BID", f"Re: SCO — {campaign.commodity} CIF {campaign.destination_port}",
        buyer_interest_email, "INBOUND", initial_buyer_cif
    )

    rfq_msg = state.get("supplier_draft", "")
    _append_audit_and_transcript(
        state, db, campaign_id, 1, f"Procurement Desk ({settings.desk_name})", f"{supp_name} <{supp_email}>",
        "agent", "OUTBOUND_RFQ", f"Urgent RFQ — {campaign.commodity} FOB {campaign.origin_port_default}",
        rfq_msg, "OUTBOUND", state.get("target_fob_ceiling")
    )
    return state

def _execute_supplier_quote_step(
    state: dict,
    campaign_id: str,
    campaign: Campaign,
    supp_name: str,
    supp_email: str,
    benchmark_fob: float,
    config: dict,
    db: Session,
) -> dict:
    supplier_quote_email = _build_supplier_quote_email(
        supp_name, supp_email, campaign.commodity, campaign.origin_port_default, campaign.target_volume_mt, benchmark_fob
    )

    state["latest_email"] = supplier_quote_email
    state["active_role"] = "supplier"
    state = app.workflow.get_trade_graph().invoke(state, config=config)
    state = _resume_graph_if_awaiting_approval(state, config)

    curr_round = state.get("negotiation_round", 2)
    _append_audit_and_transcript(
        state, db, campaign_id, curr_round, f"{supp_name} <{supp_email}>", f"Procurement Desk ({settings.desk_name})",
        "supplier", "INBOUND_QUOTE", f"Quotation — {campaign.commodity} FOB {campaign.origin_port_default}",
        supplier_quote_email, "INBOUND", benchmark_fob
    )
    return state

def _execute_single_concession_round(
    state: dict,
    campaign_id: str,
    campaign: Campaign,
    buyer_name: str,
    buyer_email: str,
    landed_cost_baseline: float,
    config: dict,
    db: Session,
) -> dict:
    counter_draft = state.get("buyer_draft") if state.get("active_role") == "buyer" else state.get("supplier_draft")
    turn_num = state.get("negotiation_round", 2)
    _append_audit_and_transcript(
        state, db, campaign_id, turn_num, f"Trading Desk ({settings.desk_name})", f"{buyer_name} <{buyer_email}>",
        "agent", "OUTBOUND_COUNTER", f"Negotiation Round {turn_num}: Tactical Adjustment Request",
        counter_draft or f"Tactical counter to maximize margin: {state.get('evaluation_reason')}",
        "OUTBOUND", state.get("last_counter_cif_usd")
    )

    target_buyer_cif = round(landed_cost_baseline + campaign.min_profit_per_mt_soft, 2)
    concession_email = _build_concession_email(
        buyer_name, buyer_email, campaign.commodity, campaign.destination_port, campaign.target_volume_mt, target_buyer_cif
    )

    state["latest_email"] = concession_email
    state["active_role"] = "buyer"
    state = app.workflow.get_trade_graph().invoke(state, config=config)
    state = _resume_graph_if_awaiting_approval(state, config)

    concession_turn = state.get("negotiation_round", turn_num + 1)
    _append_audit_and_transcript(
        state, db, campaign_id, concession_turn, f"{buyer_name} <{buyer_email}>", f"Trading Desk ({settings.desk_name})",
        "buyer", "INBOUND_CONCESSION", "Re: Price Adjustment Request — Concession Agreement",
        concession_email, "INBOUND", target_buyer_cif
    )
    return state

def _run_concession_loop(
    state: dict,
    campaign_id: str,
    campaign: Campaign,
    buyer_name: str,
    buyer_email: str,
    landed_cost_baseline: float,
    config: dict,
    db: Session,
) -> dict:
    while state.get("deal_status") not in ["closed", "rejected"]:
        action = state.get("action")
        if action in ["ACCEPT_AND_CLOSE", "REJECT_HARD"]:
            break
        if action == "COUNTER_TO_MAXIMIZE":
            state = _execute_single_concession_round(
                state, campaign_id, campaign, buyer_name, buyer_email, landed_cost_baseline, config, db
            )
        else:
            break
    return state

def _record_closed_deal_audit_entries(
    state: dict,
    campaign_id: str,
    campaign: Campaign,
    supp_name: str,
    supp_email: str,
    buyer_name: str,
    buyer_email: str,
    db: Session,
) -> None:
    if state.get("action") != "ACCEPT_AND_CLOSE" and state.get("deal_status") != "closed":
        return

    turn_round = state.get("negotiation_round", 3)
    supp_price = state.get("supplier_terms").price_usd_per_mt if state.get("supplier_terms") else None
    buyer_price = state.get("buyer_terms").price_usd_per_mt if state.get("buyer_terms") else None

    _append_audit_and_transcript(
        state, db, campaign_id, turn_round, f"Procurement Desk ({settings.desk_name})", f"{supp_name} <{supp_email}>",
        "agent", "LOCK_SUPPLIER_ALLOCATION", f"Deal Confirmation & Volume Lock — {campaign.commodity}",
        state.get("supplier_draft", ""), "OUTBOUND", supp_price
    )
    _append_audit_and_transcript(
        state, db, campaign_id, turn_round, f"Trading Desk ({settings.desk_name})", f"{buyer_name} <{buyer_email}>",
        "agent", "ACCEPT_AND_CLOSE", f"Soft Corporate Offer (SCO) Acceptance — {campaign.commodity}",
        state.get("buyer_draft", ""), "OUTBOUND", buyer_price
    )


def _sync_final_graph_and_db_state(
    state: dict,
    db_camp: CampaignModel,
    config: dict,
    db: Session,
) -> None:
    db_camp.deal_status = state.get("deal_status", "closed")
    db.commit()

    try:
        app.workflow.get_trade_graph().update_state(
            config,
            {
                "audit_transcript": state.get("audit_transcript", []),
                "deal_status": state.get("deal_status", "closed"),
                "action": state.get("action", "ACCEPT_AND_CLOSE"),
                "buyer_draft": state.get("buyer_draft"),
                "supplier_draft": state.get("supplier_draft"),
            },
        )
    except Exception as exc:
        logger.warning(f"Could not update graph state: {exc}")


def _build_autonomous_runner_result(
    campaign_id: str,
    campaign: Campaign,
    state: dict,
    buyers: list,
    suppliers: list,
) -> dict:
    buyer_t = state.get("buyer_terms")
    supp_t = state.get("supplier_terms")
    resp = AutonomousRunnerResult(
        campaign_id=campaign_id,
        commodity=campaign.commodity,
        deal_status=state.get("deal_status", "closed"),
        negotiation_rounds_completed=state.get("negotiation_round", 0),
        final_net_spread_usd=state.get("net_spread_usd", 0.0),
        final_net_margin_pct=state.get("net_margin_pct", 0.0),
        action=state.get("action", "ACCEPT_AND_CLOSE"),
        is_deal_viable=state.get("is_deal_viable", True),
        pipeline_step=state.get("pipeline_step", 4),
        evaluation_reason=state.get("evaluation_reason", ""),
        audit_transcript=state.get("audit_transcript", []),
        buyer_terms=buyer_t.model_dump() if buyer_t else None,
        supplier_terms=supp_t.model_dump() if supp_t else None,
        buyer_draft=state.get("buyer_draft"),
        supplier_draft=state.get("supplier_draft"),
        benchmark_fob_usd=state.get("benchmark_fob_usd"),
        freight_cost_usd=state.get("freight_cost_usd"),
        dynamic_fob_ceiling=state.get("dynamic_fob_ceiling"),
        dynamic_cif_floor=state.get("dynamic_cif_floor"),
        anchor_cif_usd=state.get("anchor_cif_usd"),
        discovered_buyers=[b.model_dump() for b in buyers],
        discovered_suppliers=[s.model_dump() for s in suppliers],
        status=state.get("deal_status", "closed"),
    )
    return resp.model_dump()


def run_full_autonomous_campaign(campaign_id: str, db: Optional[Session] = None) -> dict:
    should_close = False
    if db is None:
        db = app.database.SessionLocal()
        should_close = True

    try:
        db_camp = db.query(CampaignModel).filter(CampaignModel.id == campaign_id).first()
        if not db_camp:
            raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

        langfuse_handler = get_langfuse_handler()
        config = {
            "configurable": {"thread_id": campaign_id},
            "callbacks": [langfuse_handler] if langfuse_handler else [],
            "metadata": {
                "campaign_id": campaign_id,
                "mode": "autonomous",
            },
            "tags": ["commodity-arbitrage", "autonomous"],
        }
        snapshot = app.workflow.get_trade_graph().get_state(config)
        state = dict(snapshot.values) if snapshot and snapshot.values else {}
        if not state:
            raise HTTPException(status_code=404, detail=f"State for campaign {campaign_id} not found.")

        campaign: Campaign = state["campaign"]
        buffer_usd = campaign.buffer_usd_per_mt if campaign.buffer_usd_per_mt is not None else settings.default_buffer_usd_per_mt
        landed_cost_baseline = round(state["benchmark_fob_usd"] + state["freight_cost_usd"] + buffer_usd, 2)

        buyers, suppliers, buyer_name, buyer_email, supp_name, supp_email = _resolve_autonomous_counterparties(campaign.commodity)
        _ensure_initial_transcript(state, campaign, buyer_name, buyer_email)

        state = _execute_buyer_bid_step(
            state, campaign_id, campaign, buyer_name, buyer_email, landed_cost_baseline, supp_name, supp_email, config, db
        )
        state = _execute_supplier_quote_step(
            state, campaign_id, campaign, supp_name, supp_email, state["benchmark_fob_usd"], config, db
        )
        state = _run_concession_loop(
            state, campaign_id, campaign, buyer_name, buyer_email, landed_cost_baseline, config, db
        )
        _record_closed_deal_audit_entries(
            state, campaign_id, campaign, supp_name, supp_email, buyer_name, buyer_email, db
        )
        _sync_final_graph_and_db_state(state, db_camp, config, db)

        res = _build_autonomous_runner_result(campaign_id, campaign, state, buyers, suppliers)
        if langfuse_handler:
            langfuse_handler.flush()
        return res
    finally:
        if should_close:
            db.close()
