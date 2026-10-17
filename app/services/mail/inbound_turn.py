"""Run one graph turn for an inbound email exactly once, then send the replies it drafted.

process_inbound_message_transaction is the only path from inbound mail into the graph.
"""
import hashlib
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.db.db_models import CampaignModel, add_trade_audit, desk_party, record_exchange
from app.graph import workflow
from app.logger import get_logger
from app.models import Campaign, ParsedEmail
from app.pricing.market import get_benchmark_rate
from app.services.campaign_runner import build_supplier_quote_email
from app.services.mail.email_utils import (
    normalize_thread_subject,
    rfq_subject,
    split_subject_and_body,
    with_draft_subject,
)
from app.services.mail.gmail_client import send_reply
from app.services.mail.inbound_dedupe import claim_message, complete_message, message_key, release_message
from app.services.mail.inbound_state import (
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
from app.telemetry import flush_telemetry

logger = get_logger("arbitrage_desk")


def _inbound_message_key(inbound: InboundEmail) -> str:
    """The RFC Message-ID key; without one, a hash of the email's content."""
    key = message_key(inbound.message_id)
    if key:
        return key
    digest = hashlib.sha256("\x1f".join((inbound.sender, inbound.subject, inbound.body)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def process_inbound_message_transaction(
    db_camp: CampaignModel, inbound: InboundEmail, desk_emails: set[str], db: Session, use_gmail_api: bool = True
) -> None:
    """Handle one inbound email exactly once. The only entry point from inbound mail into the graph."""
    key = _inbound_message_key(inbound)
    source = "push" if use_gmail_api else "poll"
    if not claim_message(db, key, source, campaign_id=db_camp.id):
        logger.info(
            f"[GMAIL EVENT] Email '{key}' already processed or in progress ({source}); skipping duplicate delivery."
        )
        return
    try:
        _run_inbound_message_transaction(db_camp, inbound, desk_emails, db, use_gmail_api)
    except Exception:
        release_message(db, key)
        raise
    complete_message(db, key)


def _supplier_reply_action(had_supplier_terms: bool, action: Optional[str], inbound_intent: Optional[str]) -> str:
    if action == "REJECT_HARD":
        return "REJECT_SUPPLIER"
    if inbound_intent == "INQUIRY":
        return "ANSWER_INQUIRY"
    return "COUNTER_SUPPLIER" if had_supplier_terms else "SUPPLIER_RFQ"


def _send_supplier_reply(
    updated_state: dict, had_supplier_terms: bool, campaign_id: str, contacts: DealContacts, msg_id: str,
    thread_id: str, curr_round: int, db: Session, use_gmail_api: bool,
) -> None:
    # Drafts are cleared before each turn, so a non-empty supplier_draft (RFQ, counter or decline) is new.
    supp_msg = updated_state.get("supplier_draft")
    if not supp_msg:
        return

    camp: Campaign = updated_state["campaign"]
    thread_subject = updated_state.get("supplier_thread_subject") or rfq_subject(
        campaign_id, camp.commodity, camp.origin_port_default
    )
    if updated_state.get("last_supplier_message_id"):
        # The supplier has written in: answer in their thread as "Re: <campaign RFQ subject>".
        send_reply(
            contacts.supplier_email, supp_msg, updated_state.get("last_supplier_message_id"),
            updated_state.get("supplier_references"), thread_subject, thread_id, use_gmail_api,
        )
    else:
        # First contact for this campaign: a new conversation under the campaign's own subject, no "Re:".
        supp_msg = with_draft_subject(supp_msg, thread_subject)
        send_reply(contacts.supplier_email, supp_msg, None, None, None, thread_id, use_gmail_api)
    supp_sub, _ = split_subject_and_body(supp_msg, default_subject=thread_subject)
    supp_terms = updated_state.get("supplier_terms")
    record_exchange(
        updated_state, db, campaign_id,
        turn=curr_round,
        sender=desk_party(),
        recipient=f"{contacts.supplier_name} <{contacts.supplier_email}>",
        role="agent",
        action=_supplier_reply_action(
            had_supplier_terms, updated_state.get("action"), updated_state.get("inbound_intent")
        ),
        subject=supp_sub,
        message=supp_msg,
        direction="OUTBOUND",
        counterparty_price=supp_terms.price_usd_per_mt if supp_terms else None,
        net_spread=updated_state.get("net_spread_usd"),
        msg_id=msg_id,
    )


def _default_buyer_subject(action: Optional[str], camp: Campaign) -> str:
    if action == "ACCEPT_AND_CLOSE":
        return f"Soft Corporate Offer (SCO) Acceptance — {camp.commodity}"
    if action == "REJECT_HARD":
        return f"Commercial Proposal Status — {camp.commodity}"
    return f"Counter-Offer — {camp.commodity} CIF {camp.destination_port}"


def _resolve_buyer_thread_subject(
    action: Optional[str], camp: Campaign, buyer_msg: str, thread_subject: Optional[str]
) -> str:
    buyer_sub, _ = split_subject_and_body(buyer_msg, default_subject=_default_buyer_subject(action, camp))
    return normalize_thread_subject(buyer_sub, thread_subject or buyer_sub)


def _send_buyer_reply(
    updated_state: dict, campaign_id: str, contacts: DealContacts, thread_id: str, curr_round: int, db: Session,
    use_gmail_api: bool,
) -> None:
    buyer_msg = updated_state.get("buyer_draft")
    if not buyer_msg:
        return

    action = updated_state.get("action")
    thread_sub = _resolve_buyer_thread_subject(
        action, updated_state["campaign"], buyer_msg, updated_state.get("thread_subject")
    )
    send_reply(
        contacts.buyer_email, buyer_msg, updated_state.get("last_buyer_message_id"),
        updated_state.get("buyer_references"), thread_sub, thread_id, use_gmail_api,
    )
    logger.info(
        f"[GMAIL EVENT] Dispatched buyer response to '{contacts.buyer_email}' via "
        f"{'Gmail REST API' if use_gmail_api else 'SMTP'} | Action: {action} | Subject: '{thread_sub}'"
    )
    record_exchange(
        updated_state, db, campaign_id,
        turn=curr_round,
        sender=desk_party(),
        recipient=f"{contacts.buyer_name} <{contacts.buyer_email}>",
        role="agent",
        action=(
            "ANSWER_INQUIRY" if updated_state.get("inbound_intent") == "INQUIRY" else (action or "COUNTER_BUYER")
        ),
        subject=thread_sub,
        message=buyer_msg,
        direction="OUTBOUND",
        counterparty_price=updated_state.get("anchor_cif_usd"),
        net_spread=updated_state.get("net_spread_usd"),
    )


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
    _send_supplier_reply(
        updated_state, had_supplier_terms, db_camp.id, contacts, inbound.message_id,
        inbound.thread_id if role == "supplier" else "", curr_round, db, use_gmail_api,
    )
    _send_buyer_reply(
        updated_state, db_camp.id, contacts,
        inbound.thread_id if role == "buyer" else (updated_state.get("gmail_thread_id") or ""),
        curr_round, db, use_gmail_api,
    )


def _run_inbound_message_transaction(
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
