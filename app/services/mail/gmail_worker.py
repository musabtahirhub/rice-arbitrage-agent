"""Inbound email ingestion: Gmail push (Pub/Sub historyId) and IMAP polling.

Only Gmail Primary-inbox mail is processed (see email_utils.is_primary_inbox_message).
"""
import asyncio
from typing import Optional

from sqlalchemy.orm import Session

import app.db.database
from app.config import settings
from app.db.db_models import CampaignModel, CounterpartyModel
from app.directory import (
    get_buyers_for_commodity,
)
from app.graph import workflow
from app.logger import get_logger
from app.models import CLOSED_DEAL_STATUSES
from app.services.mail.email_matcher import find_open_campaign
from app.services.mail.email_utils import (
    get_desk_email_addresses,
    is_automated_or_bounce_message,
    is_from_desk,
    is_primary_inbox_message,
)
from app.services.mail.gmail_client import fetch_latest_messages_by_history, mark_gmail_message_processed
from app.services.mail.imap import EmailReply, check_latest_reply, initialize_unseen_snapshot
from app.services.mail.inbound_state import InboundEmail
from app.services.mail.inbound_turn import process_inbound_message_transaction

logger = get_logger("arbitrage_desk")


LAST_SEEN_HISTORY_ID: Optional[str] = None


GMAIL_EVENT_LOCK = asyncio.Lock()


def seed_history_cursor(history_id: Optional[str]) -> None:
    """Start listing from the watch's historyId (set at startup when the watch is registered)."""
    global LAST_SEEN_HISTORY_ID
    if history_id:
        LAST_SEEN_HISTORY_ID = str(history_id)


def _push_message_to_inbound_email(msg_data: dict) -> InboundEmail:
    return InboundEmail(
        body=msg_data.get("body") or "",
        sender=msg_data.get("From") or "",
        subject=msg_data.get("Subject") or "",
        message_id=msg_data.get("Message-ID") or "",
        in_reply_to=msg_data.get("In-Reply-To") or "",
        references=msg_data.get("References") or "",
        thread_id=msg_data.get("threadId") or "",
    )


def _should_skip_message(msg_data: dict, inbound: InboundEmail, desk_emails: set[str]) -> bool:
    if not is_primary_inbox_message(msg_data.get("labelIds")):
        logger.debug(f"Skipping non-primary inbox message: {msg_data.get('id')}")
        return True
    if is_from_desk(inbound.sender, desk_emails):
        logger.info(f"[GMAIL EVENT] Ignoring message from desk's own email: {inbound.sender}")
        return True
    if is_automated_or_bounce_message(inbound.sender, inbound.subject):
        logger.info(
            f"[GMAIL EVENT] Ignoring automated/bounce message from {inbound.sender} | Subject: '{inbound.subject}'"
        )
        return True
    return False


def _handle_single_push_message(msg_data: dict, desk_emails: set[str]) -> None:
    """Run one fetched Gmail message through the graph, unless it is filtered out. Raises on failure."""
    inbound = _push_message_to_inbound_email(msg_data)
    if _should_skip_message(msg_data, inbound, desk_emails):
        return

    logger.info(
        f"[GMAIL EVENT] Inbound email detected from '{inbound.sender}' | Subject: '{inbound.subject}' | "
        f"Message-ID: '{inbound.message_id}'"
    )
    with app.db.database.SessionLocal() as db:
        db_camp = find_open_campaign(inbound.subject, inbound.body, inbound.sender, db)
        if db_camp:
            logger.info(f"[GMAIL EVENT] Processing inbound email for campaign: {db_camp.id}")
            process_inbound_message_transaction(db_camp, inbound, desk_emails, db, use_gmail_api=True)


def _is_stale_history_id(new_history_id: str, cursor: Optional[str]) -> bool:
    """True when a notification's historyId is not newer than the cursor we've already listed from."""
    if not (new_history_id and cursor):
        return False
    try:
        return int(new_history_id) <= int(cursor)
    except ValueError:
        return False


async def _fetch_new_messages(history_id: str) -> list[dict]:
    """Messages since the cursor; advances the cursor. Holds the lock so the cursor only moves forward."""
    global LAST_SEEN_HISTORY_ID
    async with GMAIL_EVENT_LOCK:
        cursor = LAST_SEEN_HISTORY_ID
        new_history_id = str(history_id) if history_id else ""
        logger.info(
            f"[GMAIL EVENT] Processing incoming Gmail event: new_history_id='{new_history_id}', "
            f"LAST_SEEN_HISTORY_ID='{cursor}'"
        )
        if _is_stale_history_id(new_history_id, cursor):
            # Pub/Sub redelivers backlogs out of order. Listing from the newer cursor already covered
            # everything up to this id, so re-listing from an older cursor would only re-fetch old mail.
            logger.info(
                f"[GMAIL EVENT] Skipping stale notification historyId='{new_history_id}' "
                f"(cursor already at '{cursor}')."
            )
            return []
        try:
            messages = await asyncio.to_thread(fetch_latest_messages_by_history, cursor or new_history_id or "")
        except Exception as e:
            logger.error(
                f"[GMAIL EVENT ERROR] Failed fetching messages for history_id {history_id}: {e}", exc_info=True
            )
            return []
        if new_history_id:
            LAST_SEEN_HISTORY_ID = new_history_id  # only reached when new > cursor: never moves backwards
        return messages


async def process_incoming_gmail_event(history_id: str) -> None:
    messages = await _fetch_new_messages(history_id)
    if not messages:
        logger.info(f"[GMAIL EVENT] No new messages returned for history_id='{history_id}'")
        return

    desk_emails = get_desk_email_addresses()
    for msg_data in messages:
        try:
            await asyncio.to_thread(_handle_single_push_message, msg_data, desk_emails)
        except Exception as e:
            # Not marked processed: the DB claim was released, so a later notification can retry it.
            logger.error(f"[GMAIL EVENT ERROR] Failed handling message {msg_data.get('id')}: {e}", exc_info=True)
            continue
        mark_gmail_message_processed(msg_data.get("id", ""))


def _collect_allowed_senders(db: Session) -> set[str]:
    """Test inboxes, the desk's saved contacts, and every open campaign's own contacts and commodity buyers."""
    allowed = {inbox.strip().lower() for inbox in (settings.my_test_email, settings.my_test_supplier_email) if inbox}
    allowed.update(row.email for row in db.query(CounterpartyModel.email).all())
    open_campaigns = db.query(CampaignModel).filter(~CampaignModel.deal_status.in_(CLOSED_DEAL_STATUSES)).all()
    for campaign in open_campaigns:
        allowed.update(
            b.contact_email.strip().lower() for b in get_buyers_for_commodity(campaign.commodity) if b.contact_email
        )
        # A picked contact deleted from the book after launch still belongs to its running campaign.
        state = workflow.get_state_values({"configurable": {"thread_id": campaign.id}})
        allowed.update(
            str(state[key]).strip().lower() for key in ("target_buyer_email", "target_supplier_email") if state.get(key)
        )
    return allowed


def _reply_to_inbound_email(reply: EmailReply) -> InboundEmail:
    return InboundEmail(
        body=str(reply).strip(),
        sender=reply.sender,
        subject=reply.subject,
        message_id=reply.message_id,
        in_reply_to=reply.in_reply_to,
        references=reply.references,
    )


async def _poll_once(desk_emails: set[str]) -> None:
    with app.db.database.SessionLocal() as db:
        allowed_senders = _collect_allowed_senders(db)

    reply = await asyncio.to_thread(check_latest_reply, allowed_senders=allowed_senders or None)
    if not reply:
        return
    inbound = _reply_to_inbound_email(reply)
    logger.info(
        f"[EMAIL WORKER] Inbound email detected from '{inbound.sender}' | Subject: '{inbound.subject}' | "
        f"Message-ID: '{inbound.message_id}'"
    )
    with app.db.database.SessionLocal() as db:
        db_camp = find_open_campaign(inbound.subject, inbound.body, inbound.sender, db)
        if db_camp:
            await asyncio.to_thread(
                process_inbound_message_transaction, db_camp, inbound, desk_emails, db, False
            )


async def email_polling_worker():
    poll_interval = settings.email_poll_interval_seconds
    logger.info(f"[EMAIL WORKER] Initialized live email polling worker (interval: {poll_interval}s)")
    await asyncio.to_thread(initialize_unseen_snapshot)
    desk_emails = get_desk_email_addresses()

    while True:
        try:
            await _poll_once(desk_emails)
        except asyncio.CancelledError:
            logger.info("[EMAIL WORKER] Email polling worker cancelled.")
            break
        except Exception as e:
            logger.error(f"[EMAIL WORKER ERROR] Unexpected error in polling cycle: {e}", exc_info=True)
        await asyncio.sleep(poll_interval)
