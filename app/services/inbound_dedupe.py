"""Exactly-once handling of inbound emails across processes and ingestion paths.

Pub/Sub delivers at least once (and redelivers backlogs out of order), uvicorn may run several
workers, and IMAP polling can see the same email as the webhook. Every path claims an email by its
key in the database before running the graph; only the first claim wins.
"""
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db_models import ProcessedInboundEmailModel
from app.logger import get_logger

logger = get_logger("arbitrage_desk.inbound_dedupe")


def message_key(rfc_message_id: Optional[str], fallback_id: Optional[str] = None) -> Optional[str]:
    """Stable key for an email: its RFC 822 Message-ID (same for push and IMAP), else a provider id."""
    key = (rfc_message_id or "").strip().strip("<>").strip().lower()
    if key:
        return key
    fallback = (fallback_id or "").strip()
    return f"id:{fallback}" if fallback else None


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
