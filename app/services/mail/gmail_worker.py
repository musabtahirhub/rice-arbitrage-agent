"""Run one graph turn for an inbound email and send whatever replies it drafted.

Only reached through inbound_dedupe.process_inbound_message_transaction, which guarantees each email
is handled once.
"""
from sqlalchemy.orm import Session

from app.agents import workflow
from app.config import settings
from app.db import CampaignModel
from app.logger import get_logger
from app.models import Campaign, ParsedEmail
from app.services.audit import add_trade_audit, desk_party, record_exchange
from app.services.inbound_replies import send_buyer_reply, send_supplier_reply
from app.services.inbound_state import (
    DealContacts,
    InboundEmail,
    build_inbound_config,
    classify_sender_role,
    counterparty_label,
    load_campaign_state,
    prepare_graph_state,
    record_contacts_in_state,
    resolve_deal_contacts,
)
from app.services.market import get_benchmark_rate
from app.services.simulated_emails import build_supplier_quote_email
from app.utils.telemetry import flush_telemetry

logger = get_logger("arbitrage_desk")


def _simulate_supplier_quote(state: dict, campaign_id: str, contacts: DealContacts, db: Session) -> None:
    """SIMULATE_SUPPLIER demo mode: fabricate a benchmark-priced supplier quote if none exists yet."""
    if state.get("supplier_terms") is not None:
        return
    camp: Campaign = state["campaign"]
    benchmark_fob = state.get("benchmark_fob_usd") or get_benchmark_rate(camp.commodity)
    logger.info(
        f"[SUPPLIER AGENT] Securing allocation for campaign '{campaign_id}': "
        f"{camp.target_volume_mt:,.0f} MT {camp.commodity} @ USD {benchmark_fob:.2f}/MT FOB {camp.origin_port_default}"
    )
    state["supplier_terms"] = ParsedEmail(
        sender_role="supplier",
        commodity=camp.commodity,
        quantity_mt=camp.target_volume_mt,
        price_usd_per_mt=benchmark_fob,
        incoterm="FOB",
        port=camp.origin_port_default,
        payment_terms=settings.default_payment_terms,
    )
    record_exchange(
        state, db, campaign_id,
        turn=state.get("negotiation_round", 0),
        sender=f"{contacts.supplier_name} <{contacts.supplier_email}>",
        recipient=desk_party(),
        role="supplier",
        action="INCOMING_SUPPLIER_QUOTE",
        subject=f"Quotation — {camp.commodity} FOB {camp.origin_port_default}",
        message=build_supplier_quote_email(contacts.supplier_name, contacts.supplier_email, camp, benchmark_fob),
        direction="INBOUND",
        counterparty_price=benchmark_fob,
    )


def _record_inbound_email(updated_state: dict, campaign_id: str, role: str, sender_label: str, inbound: InboundEmail,
                          db: Session) -> None:
    camp: Campaign = updated_state["campaign"]
    terms = updated_state.get("supplier_terms" if role == "supplier" else "buyer_terms")
    default_sub = (
        f"Re: {camp.commodity} FOB {camp.origin_port_default}" if role == "supplier"
        else f"Re: {camp.commodity} CIF {camp.destination_port}"
    )
    record_exchange(
        updated_state, db, campaign_id,
        turn=updated_state.get("negotiation_round", 1),
        sender=sender_label,
        recipient=desk_party(),
        role=role,
        action="INCOMING_SUPPLIER_QUOTE" if role == "supplier" else "INCOMING_COUNTER",
        subject=inbound.subject or default_sub,
        message=inbound.body,
        direction="INBOUND",
        counterparty_price=terms.price_usd_per_mt if terms else None,
        net_spread=updated_state.get("net_spread_usd"),
    )


def _record_inbound_while_awaiting_approval(
    db_camp: CampaignModel, role: str, inbound: InboundEmail, db: Session
) -> None:
    logger.warning(
        f"[GMAIL EVENT] Campaign {db_camp.id} is awaiting human approval; inbound {role} email from "
        f"'{inbound.sender}' recorded but not run through the graph (it would bypass the pending approval)."
    )
    add_trade_audit(db, db_camp.id, role, inbound.body, "INBOUND")
    db.commit()


def _send_turn_replies(updated_state: dict, had_supplier_terms: bool, db_camp: CampaignModel, contacts: DealContacts,
                       inbound: InboundEmail, role: str, db: Session, use_gmail_api: bool) -> None:
    curr_round = updated_state.get("negotiation_round", 1)
    # Gmail thread ids only belong to the thread of the counterparty who wrote in.
    send_supplier_reply(
        updated_state, had_supplier_terms, db_camp.id, contacts, inbound.message_id,
        inbound.thread_id if role == "supplier" else "", curr_round, db, use_gmail_api,
    )
    send_buyer_reply(
        updated_state, db_camp.id, contacts,
        inbound.thread_id if role == "buyer" else (updated_state.get("gmail_thread_id") or ""),
        curr_round, db, use_gmail_api,
    )


def run_inbound_turn(
    db_camp: CampaignModel, inbound: InboundEmail, desk_emails: set[str], db: Session, use_gmail_api: bool
) -> None:
    config = build_inbound_config(db_camp)
    state = load_campaign_state(db_camp, config)
    camp: Campaign = state["campaign"]
    role = classify_sender_role(inbound.sender, state, camp.commodity)

    if workflow.is_awaiting_approval(config):
        _record_inbound_while_awaiting_approval(db_camp, role, inbound, db)
        return

    state = prepare_graph_state(state, inbound, role)
    contacts = resolve_deal_contacts(camp, inbound.sender, desk_emails, state, role)
    record_contacts_in_state(state, contacts)
    if role == "buyer" and settings.simulate_supplier:
        _simulate_supplier_quote(state, db_camp.id, contacts, db)
    had_supplier_terms = bool(state.get("supplier_terms"))

    updated_state = workflow.get_trade_graph().invoke(state, config=config)
    flush_telemetry()
    awaiting_approval = workflow.mark_pending_approval(config, updated_state)

    sender_label = counterparty_label(contacts, role, inbound.sender)
    _record_inbound_email(updated_state, db_camp.id, role, sender_label, inbound, db)

    if awaiting_approval:
        logger.info(f"[GMAIL EVENT] Campaign {db_camp.id} is awaiting human approval. Halting outbound dispatch.")
    else:
        _send_turn_replies(updated_state, had_supplier_terms, db_camp, contacts, inbound, role, db, use_gmail_api)

    deal_status = updated_state.get("deal_status")
    db_camp.deal_status = deal_status or db_camp.deal_status
    db.commit()
    logger.info(
        f"[GMAIL EVENT] Campaign {db_camp.id} updated to status: {deal_status} "
        f"(action: {updated_state.get('action')}, role: {role})"
    )
