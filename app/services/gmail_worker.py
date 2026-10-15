import asyncio
import email.utils
import hashlib
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
import app.database
from app.db_models import CampaignModel, TradeAuditModel
from app.directory import get_buyers_for_commodity, get_suppliers_for_commodity, resolve_supplier_contact
from app.email_service import (
    check_latest_reply,
    initialize_unseen_snapshot,
    is_automated_or_bounce_message,
    normalize_thread_subject,
    send_email,
)
from app.gmail_client import (
    fetch_latest_messages_by_history,
    mark_gmail_message_processed,
    send_gmail_reply,
)
from app.market import get_benchmark_rate
from app.models import Campaign, ParsedEmail
import app.workflow
from app.workflow import split_subject_and_body
from app.services.email_matcher import match_email_to_campaign
from app.services.inbound_dedupe import claim_message, complete_message, message_key, release_message
from app.telemetry import build_trace_config, flush_telemetry, get_langfuse_handler

logger = logging.getLogger("arbitrage_desk")

LAST_SEEN_HISTORY_ID: Optional[str] = None
GMAIL_EVENT_LOCK = asyncio.Lock()


def _get_desk_email_set() -> set[str]:
    return {
        e.strip().lower()
        for e in [settings.email_user, settings.desk_email]
        if e
    }


def _is_message_from_desk(sender: str, desk_emails: set[str]) -> bool:
    sender_lower = sender.strip().lower()
    return any(desk_e in sender_lower for desk_e in desk_emails)


def _collect_allowed_senders(db: Session) -> set[str]:
    allowed_senders: set[str] = set()
    for test_inbox in (settings.my_test_email, settings.my_test_supplier_email):
        if test_inbox:
            allowed_senders.add(test_inbox.strip().lower())

    active_campaigns = (
        db.query(CampaignModel)
        .filter(~CampaignModel.deal_status.in_(["closed", "rejected"]))
        .all()
    )
    for camp_obj in active_campaigns:
        for b in get_buyers_for_commodity(camp_obj.commodity):
            if b.contact_email:
                allowed_senders.add(b.contact_email.strip().lower())
    return allowed_senders


def _build_inbound_config(db_camp: CampaignModel) -> tuple[dict, Optional[object]]:
    langfuse_handler = get_langfuse_handler()
    config = build_trace_config(
        db_camp.id,
        "gmail-inbound-turn",
        langfuse_handler,
        metadata={
            "commodity": db_camp.commodity,
            "target_volume_mt": db_camp.target_volume_mt,
            "source": "gmail_inbound",
        },
        tags=[db_camp.commodity, "gmail-inbound"],
    )
    return config, langfuse_handler


def _load_campaign_state(db_camp: CampaignModel, config: dict) -> dict:
    snapshot = app.workflow.get_trade_graph().get_state(config)
    state = dict(snapshot.values) if snapshot and snapshot.values else {}
    if "campaign" not in state:
        state["campaign"] = Campaign(
            campaign_id=db_camp.id,
            commodity=db_camp.commodity,
            target_volume_mt=db_camp.target_volume_mt,
            destination_port=db_camp.destination_port,
            origin_port_default=db_camp.origin_port_default,
        )
    elif isinstance(state["campaign"], dict):
        state["campaign"] = Campaign(**state["campaign"])
    return state


def _classify_sender_role(sender: str, state: dict, commodity: str) -> str:
    """Supplier if the sender is a known supplier contact for this campaign; otherwise buyer."""
    addr = email.utils.parseaddr(sender)[1].strip().lower()
    if not addr:
        return "buyer"
    supplier_addrs = {s.contact_email.strip().lower() for s in get_suppliers_for_commodity(commodity) if s.contact_email}
    for known in (state.get("target_supplier_email"), settings.my_test_supplier_email):
        if known:
            supplier_addrs.add(str(known).strip().lower())
    return "supplier" if addr in supplier_addrs else "buyer"


def _prepare_campaign_graph_state(
    state: dict,
    raw_body: str,
    role: str,
    msg_id: str,
    references: str,
    sub: str,
    thread_id: str,
) -> dict:
    state["latest_email"] = raw_body
    state["active_role"] = role
    state["buyer_draft"] = ""
    state["supplier_draft"] = ""

    id_key, refs_key = (
        ("last_supplier_message_id", "supplier_references") if role == "supplier"
        else ("last_buyer_message_id", "buyer_references")
    )
    if msg_id:
        state[id_key] = msg_id
        existing_refs = state.get(refs_key) or references or ""
        if msg_id not in existing_refs:
            state[refs_key] = f"{existing_refs} {msg_id}".strip()
    elif references and not state.get(refs_key):
        state[refs_key] = references

    if role == "buyer":
        if not state.get("thread_subject") and sub:
            clean_sub = re.sub(r"^(?:re|fwd|fw):\s*", "", sub, flags=re.IGNORECASE).strip()
            state["thread_subject"] = clean_sub
        if thread_id:
            state["gmail_thread_id"] = thread_id

    return state


def _resolve_deal_counterparties(camp: Campaign, sender: str, desk_emails: set[str], state: dict, role: str) -> tuple:
    buyers = get_buyers_for_commodity(camp.commodity)
    me_buyers = [b for b in buyers if b.country in ["UAE", "Saudi Arabia", "Qatar", "Kuwait", "Oman", "Bahrain"]]
    primary_buyer = me_buyers[0] if me_buyers else (buyers[0] if buyers else None)

    buyer_name = primary_buyer.name if primary_buyer else "Procurement Partner"
    buyer_email = settings.my_test_email or (primary_buyer.contact_email if primary_buyer else "procurement@domain.com")
    supp_name, supp_email = resolve_supplier_contact(
        camp.commodity, state.get("target_supplier_email"), state.get("target_supplier_name")
    )

    parsed_sender_email = email.utils.parseaddr(sender)[1]
    sender_is_external = bool(parsed_sender_email) and not any(desk_e in parsed_sender_email.lower() for desk_e in desk_emails)
    if role == "supplier":
        target_buyer_email = state.get("target_buyer_email") or buyer_email
        if sender_is_external:
            supp_email = parsed_sender_email
    else:
        target_buyer_email = parsed_sender_email if sender_is_external else buyer_email
    return buyer_name, target_buyer_email, supp_name, supp_email


from app.schemas import AuditTranscriptEntry

def _append_worker_audit_and_transcript(
    state: dict,
    db: Session,
    camp_id: str,
    turn: int,
    sender: str,
    recipient: str,
    role: str,
    action: str,
    subject: str,
    message: str,
    direction: str,
    counterparty_price: Optional[float] = None,
    net_spread: Optional[float] = None,
    msg_id: Optional[str] = None,
) -> None:
    if "audit_transcript" not in state or state["audit_transcript"] is None:
        state["audit_transcript"] = []
    entry = AuditTranscriptEntry(
        turn=turn,
        sender=sender,
        recipient=recipient,
        role=role,
        action=action,
        subject=subject,
        message=message,
        id=msg_id,
    )
    state["audit_transcript"].append(entry.model_dump(exclude_none=True))
    db.add(TradeAuditModel(
        campaign_id=camp_id,
        thread_id=camp_id,
        role=role,
        counterparty_price=counterparty_price,
        net_spread=net_spread,
        raw_message=message or "",
        direction=direction,
    ))

def _dispatch_outbound_reply_transport(
    to_email: str,
    draft: str,
    in_reply_to: Optional[str],
    references: Optional[str],
    thread_subject: str,
    thread_id: Optional[str],
    use_gmail_api: bool,
) -> None:
    if use_gmail_api:
        send_gmail_reply(
            to_email,
            draft,
            in_reply_to=in_reply_to,
            references=references,
            thread_subject=thread_subject,
            thread_id=thread_id or None,
        )
    else:
        send_email(
            to_email=to_email,
            raw_draft=draft,
            in_reply_to=in_reply_to,
            references=references,
            thread_subject=thread_subject,
        )

def _build_mock_supplier_quote(supp_name: str, supp_email: str, camp: Campaign, benchmark_fob: float) -> str:
    return (
        f"Subject: Quotation — {camp.commodity} FOB {camp.origin_port_default}\n\n"
        f"To: Procurement Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
        f"From: {supp_name} <{supp_email}>\n\n"
        f"Dear Procurement Desk,\n\n"
        f"In response to your RFQ, we quote {camp.target_volume_mt:,.0f} MT of export grade {camp.commodity} at "
        f"USD {benchmark_fob:.2f}/MT FOB {camp.origin_port_default}. Payment terms: {settings.default_payment_terms}. Ready for prompt loading.\n\n"
        f"Best regards,\nExport Sales, {supp_name}"
    )

def _ensure_supplier_allocation(
    state: dict,
    db_camp: CampaignModel,
    supp_name: str,
    supp_email: str,
    db: Session,
) -> None:
    if state.get("supplier_terms") is not None:
        return

    camp: Campaign = state["campaign"]
    benchmark_fob = state.get("benchmark_fob_usd") or get_benchmark_rate(camp.commodity)
    logger.info(
        f"[SUPPLIER AGENT] Securing allocation for campaign '{db_camp.id}': "
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
    supplier_quote_email = _build_mock_supplier_quote(supp_name, supp_email, camp, benchmark_fob)
    _append_worker_audit_and_transcript(
        state, db, db_camp.id, state.get("negotiation_round", 0),
        f"{supp_name} <{supp_email}>", f"Trading Desk ({settings.desk_name})",
        "supplier", "INCOMING_SUPPLIER_QUOTE", f"Quotation — {camp.commodity} FOB {camp.origin_port_default}",
        supplier_quote_email, "INBOUND", benchmark_fob, None
    )

def _record_inbound_audit(
    updated_state: dict,
    db_camp: CampaignModel,
    role: str,
    counterparty_name: str,
    sender: str,
    sub: str,
    raw_body: str,
    db: Session,
) -> None:
    curr_round = updated_state.get("negotiation_round", 1)
    camp = updated_state["campaign"]
    terms = updated_state.get("supplier_terms" if role == "supplier" else "buyer_terms")
    inbound_price = terms.price_usd_per_mt if terms else None
    default_sub = (
        f"Re: {camp.commodity} FOB {camp.origin_port_default}" if role == "supplier"
        else f"Re: {camp.commodity} CIF {camp.destination_port}"
    )
    _append_worker_audit_and_transcript(
        updated_state, db, db_camp.id, curr_round,
        f"{counterparty_name} <{sender}>", f"Trading Desk ({settings.desk_name})",
        role, "INCOMING_SUPPLIER_QUOTE" if role == "supplier" else "INCOMING_COUNTER",
        sub or default_sub, raw_body, "INBOUND", inbound_price, updated_state.get("net_spread_usd")
    )


def _dispatch_supplier_reply_if_required(
    updated_state: dict,
    state: dict,
    db_camp: CampaignModel,
    supp_name: str,
    supp_email: str,
    msg_id: str,
    thread_id: str,
    curr_round: int,
    db: Session,
    use_gmail_api: bool = True,
) -> None:
    action = updated_state.get("action")
    # Drafts are cleared before each turn, so a non-empty supplier_draft (RFQ, counter or decline) is new.
    supp_msg = updated_state.get("supplier_draft")
    if not supp_msg:
        return

    camp = updated_state["campaign"]
    supp_thread_sub = f"Urgent RFQ — {camp.commodity} FOB {camp.origin_port_default}"
    _dispatch_outbound_reply_transport(
        supp_email, supp_msg, updated_state.get("last_supplier_message_id"),
        updated_state.get("supplier_references"), supp_thread_sub, thread_id, use_gmail_api
    )

    supp_sub, _ = split_subject_and_body(
        supp_msg,
        default_subject=f"Urgent RFQ / Volume Lock — {camp.commodity} FOB {camp.origin_port_default}",
    )
    supp_action = "SUPPLIER_RFQ" if not state.get("supplier_terms") else (
        "REJECT_SUPPLIER" if action == "REJECT_HARD" else "COUNTER_SUPPLIER"
    )
    supp_terms = updated_state.get("supplier_terms")
    supp_price = supp_terms.price_usd_per_mt if supp_terms else None
    _append_worker_audit_and_transcript(
        updated_state, db, db_camp.id, curr_round,
        f"Trading Desk ({settings.desk_name})", f"{supp_name} <{supp_email}>",
        "agent", supp_action, supp_sub, supp_msg, "OUTBOUND",
        supp_price, updated_state.get("net_spread_usd"), msg_id
    )

def _resolve_buyer_reply_subject(action: Optional[str], camp: Campaign, buyer_msg: str, state_thread_sub: Optional[str]) -> tuple[str, str]:
    default_sub = (
        f"Soft Corporate Offer (SCO) Acceptance — {camp.commodity}"
        if action == "ACCEPT_AND_CLOSE"
        else (
            f"Commercial Proposal Status — {camp.commodity}"
            if action == "REJECT_HARD"
            else f"Counter-Offer — {camp.commodity} CIF {camp.destination_port}"
        )
    )
    buyer_sub, _ = split_subject_and_body(buyer_msg, default_subject=default_sub)
    thread_sub = state_thread_sub or buyer_sub
    return buyer_sub, normalize_thread_subject(buyer_sub, thread_sub)

def _dispatch_buyer_reply_if_required(
    updated_state: dict,
    db_camp: CampaignModel,
    buyer_name: str,
    buyer_email: str,
    thread_id: str,
    curr_round: int,
    db: Session,
    use_gmail_api: bool = True,
) -> None:
    buyer_msg = updated_state.get("buyer_draft")
    if not buyer_msg:
        return

    action = updated_state.get("action")
    camp = updated_state["campaign"]
    buyer_sub, actual_thread_sub = _resolve_buyer_reply_subject(
        action, camp, buyer_msg, updated_state.get("thread_subject")
    )

    _dispatch_outbound_reply_transport(
        buyer_email, buyer_msg, updated_state.get("last_buyer_message_id"),
        updated_state.get("buyer_references"), actual_thread_sub, thread_id, use_gmail_api
    )

    logger.info(
        f"[GMAIL EVENT] Dispatched buyer response to '{buyer_email}' via {'Gmail REST API' if use_gmail_api else 'SMTP'} | "
        f"Action: {action} | Subject: '{actual_thread_sub}'"
    )

    _append_worker_audit_and_transcript(
        updated_state, db, db_camp.id, curr_round,
        f"Trading Desk ({settings.desk_name})", f"{buyer_name} <{buyer_email}>",
        "agent", action or "COUNTER_BUYER", actual_thread_sub, buyer_msg,
        "OUTBOUND", updated_state.get("anchor_cif_usd"), updated_state.get("net_spread_usd")
    )


def _record_inbound_while_awaiting_approval(
    db_camp: CampaignModel, role: str, sender: str, raw_body: str, db: Session
) -> None:
    logger.warning(
        f"[GMAIL EVENT] Campaign {db_camp.id} is awaiting human approval; inbound {role} email from '{sender}' "
        f"recorded but not run through the graph (it would bypass the pending approval)."
    )
    db.add(TradeAuditModel(
        campaign_id=db_camp.id,
        thread_id=db_camp.id,
        role=role,
        raw_message=raw_body or "",
        direction="INBOUND",
    ))
    db.commit()


def _inbound_message_key(msg_id: str, sender: str, sub: str, raw_body: str) -> str:
    """RFC Message-ID (identical for push and IMAP); without one, a hash of the email's content."""
    key = message_key(msg_id)
    if key:
        return key
    digest = hashlib.sha256("\x1f".join((sender, sub, raw_body)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _process_inbound_message_transaction(
    db_camp: CampaignModel,
    raw_body: str,
    sender: str,
    sub: str,
    msg_id: str,
    in_reply_to: str,
    references: str,
    thread_id: str,
    desk_emails: set[str],
    db: Session,
    use_gmail_api: bool = True,
) -> None:
    """Handle one inbound email exactly once, whichever path (push/poll) or process delivered it."""
    key = _inbound_message_key(msg_id, sender, sub, raw_body)
    source = "push" if use_gmail_api else "poll"
    if not claim_message(db, key, source, campaign_id=db_camp.id):
        logger.info(f"[GMAIL EVENT] Email '{key}' already processed or in progress ({source}); skipping duplicate delivery.")
        return
    try:
        _run_inbound_message_transaction(
            db_camp, raw_body, sender, sub, msg_id, references, thread_id, desk_emails, db, use_gmail_api
        )
    except Exception:
        release_message(db, key)
        raise
    complete_message(db, key)


def _run_inbound_message_transaction(
    db_camp: CampaignModel,
    raw_body: str,
    sender: str,
    sub: str,
    msg_id: str,
    references: str,
    thread_id: str,
    desk_emails: set[str],
    db: Session,
    use_gmail_api: bool,
) -> None:
    config, langfuse_handler = _build_inbound_config(db_camp)
    state = _load_campaign_state(db_camp, config)
    camp: Campaign = state["campaign"]
    role = _classify_sender_role(sender, state, camp.commodity)

    if app.workflow.is_awaiting_approval(config):
        _record_inbound_while_awaiting_approval(db_camp, role, sender, raw_body, db)
        return

    state = _prepare_campaign_graph_state(state, raw_body, role, msg_id, references, sub, thread_id)
    buyer_name, buyer_email, supp_name, supp_email = _resolve_deal_counterparties(camp, sender, desk_emails, state, role)
    state["target_buyer_email"] = buyer_email
    state["target_supplier_email"] = supp_email
    state["target_supplier_name"] = supp_name

    if role == "buyer" and settings.simulate_supplier:
        _ensure_supplier_allocation(state, db_camp, supp_name, supp_email, db)

    updated_state = app.workflow.get_trade_graph().invoke(state, config=config)
    flush_telemetry()

    awaiting_approval = app.workflow.is_awaiting_approval(config)
    if awaiting_approval:
        app.workflow.get_trade_graph().update_state(config, {"deal_status": "pending_approval"})
        updated_state["deal_status"] = "pending_approval"

    counterparty_name = supp_name if role == "supplier" else buyer_name
    _record_inbound_audit(updated_state, db_camp, role, counterparty_name, sender or buyer_email, sub, raw_body, db)

    curr_round = updated_state.get("negotiation_round", 1)
    deal_status = updated_state.get("deal_status")
    action = updated_state.get("action")

    if awaiting_approval:
        logger.info(
            f"[GMAIL EVENT] Campaign {db_camp.id} is awaiting human approval. "
            f"Halting outbound dispatch until approval is confirmed."
        )
    else:
        # Gmail thread ids only belong to the thread of the counterparty who wrote in.
        _dispatch_supplier_reply_if_required(
            updated_state, state, db_camp, supp_name, supp_email, msg_id,
            thread_id if role == "supplier" else "", curr_round, db, use_gmail_api
        )
        _dispatch_buyer_reply_if_required(
            updated_state, db_camp, buyer_name, buyer_email,
            thread_id if role == "buyer" else (updated_state.get("gmail_thread_id") or ""),
            curr_round, db, use_gmail_api
        )

    db_camp.deal_status = deal_status or db_camp.deal_status
    db.commit()
    logger.info(f"[GMAIL EVENT] Campaign {db_camp.id} updated to status: {deal_status} (action: {action}, role: {role})")


async def email_polling_worker():
    poll_interval = settings.email_poll_interval_seconds
    logger.info(f"[EMAIL WORKER] Initialized live email polling worker (interval: {poll_interval}s)")

    await asyncio.to_thread(initialize_unseen_snapshot)
    desk_emails = _get_desk_email_set()

    while True:
        try:
            with app.database.SessionLocal() as db:
                allowed_senders = _collect_allowed_senders(db)

            incoming = await asyncio.to_thread(check_latest_reply, allowed_senders=allowed_senders or None)
            if incoming:
                raw_body = str(incoming).strip()
                sub = getattr(incoming, "subject", "")
                sender = getattr(incoming, "sender", "")
                msg_id = getattr(incoming, "message_id", "")
                in_reply_to = getattr(incoming, "in_reply_to", "")
                references = getattr(incoming, "references", "")
                logger.info(f"[EMAIL WORKER] Inbound email detected from '{sender}' | Subject: '{sub}' | Message-ID: '{msg_id}'")

                with app.database.SessionLocal() as db:
                    matched_cid = match_email_to_campaign(sub, raw_body, sender, db=db)
                    db_camp = db.query(CampaignModel).filter(CampaignModel.id == matched_cid).first() if matched_cid else None
                    if db_camp:
                        if db_camp.deal_status in ["closed", "rejected"]:
                            logger.info(f"[EMAIL WORKER] Campaign {matched_cid} is already {db_camp.deal_status}. Skipping.")
                        else:
                            await asyncio.to_thread(
                                _process_inbound_message_transaction,
                                db_camp, raw_body, sender, sub, msg_id, in_reply_to, references, "", desk_emails, db, False
                            )
                    else:
                        logger.warning(f"[EMAIL WORKER] No matching active campaign found for subject: '{sub}'")

        except asyncio.CancelledError:
            logger.info("[EMAIL WORKER] Email polling worker cancelled.")
            break
        except Exception as e:
            logger.error(f"[EMAIL WORKER ERROR] Unexpected error in polling cycle: {e}", exc_info=True)

        await asyncio.sleep(poll_interval)


def _handle_single_push_message(msg_data: dict, desk_emails: set[str]) -> None:
    sender = msg_data.get("From") or msg_data.get("from") or msg_data.get("sender") or ""
    if _is_message_from_desk(sender, desk_emails):
        logger.info(f"[GMAIL EVENT] Ignoring message from desk's own email: {sender}")
        return

    sub = msg_data.get("Subject") or msg_data.get("subject") or ""
    if is_automated_or_bounce_message(sender, sub):
        logger.info(f"[GMAIL EVENT] Ignoring automated/bounce message from {sender} | Subject: '{sub}'")
        return

    raw_body = msg_data.get("body") or ""
    msg_id = msg_data.get("Message-ID") or msg_data.get("message_id") or ""
    in_reply_to = msg_data.get("In-Reply-To") or msg_data.get("in_reply_to") or ""
    references = msg_data.get("References") or msg_data.get("references") or ""
    thread_id = msg_data.get("threadId") or msg_data.get("thread_id") or ""

    logger.info(f"[GMAIL EVENT] Inbound email detected from '{sender}' | Subject: '{sub}' | Message-ID: '{msg_id}'")

    with app.database.SessionLocal() as db:
        matched_cid = match_email_to_campaign(sub, raw_body, sender, db=db)
        db_camp = db.query(CampaignModel).filter(CampaignModel.id == matched_cid).first() if matched_cid else None
        if not db_camp:
            logger.warning(f"[GMAIL EVENT] Inbound email detected, but no matching active campaign found in database. Subject: '{sub}'")
            return

        if db_camp.deal_status in ["closed", "rejected"]:
            logger.info(f"[GMAIL EVENT] Campaign {matched_cid} is already {db_camp.deal_status}. Skipping.")
            return

        logger.info(f"[GMAIL EVENT] Processing inbound email for campaign: {matched_cid}")
        _process_inbound_message_transaction(
            db_camp, raw_body, sender, sub, msg_id, in_reply_to, references, thread_id, desk_emails, db, True
        )


def _is_stale_history_id(new_history_id: str, cursor: Optional[str]) -> bool:
    """True when a notification's historyId is not newer than the cursor we've already listed from."""
    if not (new_history_id and cursor):
        return False
    try:
        return int(new_history_id) <= int(cursor)
    except ValueError:
        return False


async def process_incoming_gmail_event(history_id: str):
    async with GMAIL_EVENT_LOCK:
        global LAST_SEEN_HISTORY_ID
        start_history_id = LAST_SEEN_HISTORY_ID
        new_history_id = str(history_id) if history_id else ""
        logger.info(
            f"[GMAIL EVENT] Processing incoming Gmail event: new_history_id='{new_history_id}', "
            f"LAST_SEEN_HISTORY_ID='{start_history_id}'"
        )

        if _is_stale_history_id(new_history_id, start_history_id):
            # Pub/Sub redelivers backlogs out of order. Listing from the newer cursor already covered
            # everything up to this id, so re-listing from an older cursor would only re-fetch old mail.
            logger.info(
                f"[GMAIL EVENT] Skipping stale notification historyId='{new_history_id}' "
                f"(cursor already at '{start_history_id}')."
            )
            return

        query_id = start_history_id or new_history_id
        try:
            messages = await asyncio.to_thread(fetch_latest_messages_by_history, query_id or "")
        except Exception as e:
            logger.error(f"[GMAIL EVENT ERROR] Failed fetching messages for history_id {history_id}: {e}", exc_info=True)
            return

        if new_history_id:
            LAST_SEEN_HISTORY_ID = new_history_id  # only reached when new > cursor: never moves backwards

        if not messages:
            logger.info(f"[GMAIL EVENT] No new messages returned for history_id='{history_id}'")
            return

    desk_emails = _get_desk_email_set()
    for msg_data in messages:
        try:
            await asyncio.to_thread(_handle_single_push_message, msg_data, desk_emails)
        except Exception as e:
            # Not marked processed: the DB claim was released, so a later notification can retry it.
            logger.error(f"[GMAIL EVENT ERROR] Failed handling message {msg_data.get('id')}: {e}", exc_info=True)
            continue
        mark_gmail_message_processed(msg_data.get("id", ""))
