"""Exactly-once handling of inbound emails across processes and ingestion paths.

Pub/Sub delivers at least once (and redelivers backlogs out of order), uvicorn may run several
workers, and IMAP polling can see the same email as the webhook. Every path claims an email by its
key in the database before running the graph; only the first claim wins.
"""
import hashlib
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import CampaignModel, ProcessedInboundEmailModel
from app.logger import get_logger
from app.services.inbound_state import InboundEmail
from app.services.inbound_turn import run_inbound_turn

logger = get_logger("arbitrage_desk.inbound_dedupe")


def message_key(rfc_message_id: Optional[str], fallback_id: Optional[str] = None) -> Optional[str]:
    """Stable key for an email: its RFC 822 Message-ID (same for push and IMAP), else a provider id."""
    key = (rfc_message_id or "").strip().strip("<>").strip().lower()
    if key:
        return key
    fallback = (fallback_id or "").strip()
    return f"id:{fallback}" if fallback else None


def inbound_message_key(inbound: InboundEmail) -> str:
    """The RFC Message-ID key; without one, a hash of the email's content."""
    key = message_key(inbound.message_id)
    if key:
        return key
    digest = hashlib.sha256("\x1f".join((inbound.sender, inbound.subject, inbound.body)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def claim_message(db: Session, key: str, source: str, campaign_id: Optional[str] = None) -> bool:
    """Atomically claim an email. False means another process or path already has it."""
    db.add(ProcessedInboundEmailModel(message_key=key, campaign_id=campaign_id, source=source, status="processing"))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def complete_message(db: Session, key: str) -> None:
    db.query(ProcessedInboundEmailModel).filter(ProcessedInboundEmailModel.message_key == key).update(
        {"status": "done"}
    )
    db.commit()


def release_message(db: Session, key: str) -> None:
    """Drop a claim after a failure so a later delivery can retry the email."""
    try:
        db.rollback()
        db.query(ProcessedInboundEmailModel).filter(ProcessedInboundEmailModel.message_key == key).delete()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(f"[DEDUPE] Could not release claim for '{key}': {exc}")


def process_inbound_message_transaction(
    db_camp: CampaignModel, inbound: InboundEmail, desk_emails: set[str], db: Session, use_gmail_api: bool = True
) -> None:
    """Handle one inbound email exactly once. The only entry point from inbound mail into the graph."""
    key = inbound_message_key(inbound)
    source = "push" if use_gmail_api else "poll"
    if not claim_message(db, key, source, campaign_id=db_camp.id):
        logger.info(
            f"[GMAIL EVENT] Email '{key}' already processed or in progress ({source}); skipping duplicate delivery."
        )
        return
    try:
        run_inbound_turn(db_camp, inbound, desk_emails, db, use_gmail_api)
    except Exception:
        release_message(db, key)
        raise
    complete_message(db, key)
