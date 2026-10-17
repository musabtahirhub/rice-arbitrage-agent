"""Send desk email over SMTP (STARTTLS). Simulated recipients and missing credentials are logged, not sent."""
import smtplib
from email.message import EmailMessage
from typing import Optional

from app.config import settings
from app.logger import get_logger
from app.services.mail.email_utils import (
    build_mime_message,
    is_simulated_email,
    normalize_thread_subject,
    split_subject_and_body,
)

logger = get_logger("arbitrage_desk.email_service")


OUTBOUND_DEFAULT_SUBJECT = "Commodity Arbitrage Trade Correspondence"


def get_desk_login() -> tuple[str, str]:
    """(EMAIL_USER, EMAIL_PASS) cleaned for SMTP/IMAP login; app passwords are often pasted with spaces."""
    return settings.email_user.strip(), settings.email_pass.replace(" ", "").strip()


def _deliver_via_smtp(msg: EmailMessage, to_addr: str, subject: str, outbound_msg_id: str) -> bool:
    user, password = get_desk_login()
    try:
        with smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=settings.email_timeout_seconds) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(user, password)
            server.send_message(msg)
        logger.info(
            f"[EMAIL SENT] Successfully dispatched to {to_addr} | Subject: '{subject}' | Message-ID: {outbound_msg_id}"
        )
        return True
    except Exception as e:
        logger.error(f"[EMAIL SEND ERROR] Failed sending to {to_addr}: {e}")
        return False


def send_email(
    to_email: str,
    raw_draft: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    thread_subject: Optional[str] = None,
    custom_message_id: Optional[str] = None,
) -> bool:
    to_addr = (to_email or "").strip()
    if not to_addr:
        logger.warning("Cannot send email: recipient address is empty.")
        return False

    parsed_sub, body = split_subject_and_body(raw_draft, default_subject=OUTBOUND_DEFAULT_SUBJECT)
    subject = normalize_thread_subject(parsed_sub, thread_subject) if thread_subject else parsed_sub

    if not settings.email_user or not settings.email_pass or is_simulated_email(to_addr):
        logger.info(
            f"[MOCK SEND] Safely simulated dispatch to {to_addr} (no live SMTP packet sent)\n"
            f"Subject: {subject}\nBody length: {len(body)} chars."
        )
        return True

    msg, outbound_msg_id = build_mime_message(
        get_desk_login()[0], to_addr, subject, body, in_reply_to, references, custom_message_id
    )
    return _deliver_via_smtp(msg, to_addr, subject, outbound_msg_id)
