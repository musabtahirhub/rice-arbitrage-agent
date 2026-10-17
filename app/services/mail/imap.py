"""IMAP polling client: find the next unseen Primary-inbox reply from an allowed sender."""
import email
import email.header
import email.message
import email.utils
import imaplib
from typing import Optional

from app.config import settings
from app.logger import get_logger
from app.services.mail.email_utils import (
    PRIMARY_INBOX_QUERY,
    decode_mime_header,
    extract_reply_text,
    is_automated_or_bounce_message,
)
from app.services.mail.smtp import get_desk_login

logger = get_logger("arbitrage_desk.email_service")


# IMAP sequence numbers and RFC Message-IDs this process has already handled or deliberately skipped.
PROCESSED_MESSAGE_IDS: set[str] = set()


class EmailReply(str):
    """The reply body as a str, carrying the headers needed to thread the desk's answer."""

    def __new__(cls, body: str, subject: str = "", sender: str = "", message_id: str = "",
                in_reply_to: str = "", references: str = ""):
        obj = super().__new__(cls, body)
        obj.subject = subject
        obj.sender = sender
        obj.message_id = message_id
        obj.in_reply_to = in_reply_to
        obj.references = references
        return obj


def _is_gmail_imap() -> bool:
    server = settings.imap_server.lower()
    return "gmail" in server or "googlemail" in server


def _unseen_search_criteria() -> tuple[str, ...]:
    """UNSEEN, restricted to the Primary tab on Gmail (X-GM-RAW accepts Gmail search syntax)."""
    if _is_gmail_imap():
        return ("UNSEEN", "X-GM-RAW", f'"{PRIMARY_INBOX_QUERY}"')
    return ("UNSEEN",)


def _connect_imap_client() -> Optional[imaplib.IMAP4_SSL]:
    if not settings.email_user or not settings.email_pass:
        return None
    try:
        mail = imaplib.IMAP4_SSL(settings.imap_server, timeout=settings.email_timeout_seconds)
        mail.login(*get_desk_login())
        mail.select(settings.imap_mailbox_folder)
        return mail
    except Exception as e:
        logger.error(f"[IMAP CLIENT ERROR] Failed to connect: {e}")
        return None


def _disconnect(mail: imaplib.IMAP4_SSL) -> None:
    for step in (mail.close, mail.logout):
        try:
            step()
        except Exception:
            pass


def _search_unseen_ids(mail: imaplib.IMAP4_SSL) -> list[str]:
    status, messages = mail.search(None, *_unseen_search_criteria())
    if status != "OK" or not messages or not messages[0]:
        return []
    return [m.decode() if isinstance(m, bytes) else str(m) for m in messages[0].split()]


def get_unseen_message_ids() -> set[str]:
    mail = _connect_imap_client()
    if not mail:
        return set()
    try:
        return set(_search_unseen_ids(mail))
    except Exception as e:
        logger.error(f"[IMAP SNAPSHOT ERROR] Failed fetching unseen IDs: {e}")
        return set()
    finally:
        _disconnect(mail)


def initialize_unseen_snapshot() -> set[str]:
    """Ignore mail that was already unread at startup; only newer replies are processed."""
    unseen = get_unseen_message_ids()
    PROCESSED_MESSAGE_IDS.update(unseen)
    logger.info(f"[IMAP SNAPSHOT] Baseline unseen messages snapshot registered ({len(unseen)} IDs ignored).")
    return PROCESSED_MESSAGE_IDS


def _is_unwanted_sender(from_header: str, subject: str, allowed_senders: Optional[set[str]]) -> bool:
    if is_automated_or_bounce_message(from_header, subject):
        return True
    desk_user = settings.email_user.strip().lower()
    if desk_user and desk_user in from_header.lower():
        return True
    return bool(allowed_senders) and not any(a.lower() in from_header.lower() for a in allowed_senders)


def _mark_seen(mail: imaplib.IMAP4_SSL, seq_id: str, rfc_msg_id: str) -> None:
    mail.store(seq_id, "+FLAGS", "\\Seen")
    PROCESSED_MESSAGE_IDS.add(seq_id)
    if rfc_msg_id:
        PROCESSED_MESSAGE_IDS.add(rfc_msg_id)


def _read_reply(mail, seq_id: str, msg: email.message.Message, subject_snippet: str,
                allowed_senders: Optional[set[str]]) -> Optional[EmailReply]:
    subject = decode_mime_header(msg.get("Subject", ""))
    from_header = str(msg.get("From", ""))
    rfc_msg_id = str(msg.get("Message-ID", "")).strip()
    if rfc_msg_id and rfc_msg_id in PROCESSED_MESSAGE_IDS:
        return None
    if _is_unwanted_sender(from_header, subject, allowed_senders):
        _mark_seen(mail, seq_id, rfc_msg_id)
        return None
    if subject_snippet and subject_snippet not in subject.lower():
        return None
    body = extract_reply_text(msg)
    if not body.strip():
        return None

    _mark_seen(mail, seq_id, rfc_msg_id)
    return EmailReply(body=body, subject=subject, sender=from_header, message_id=rfc_msg_id or seq_id,
                      in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
                      references=str(msg.get("References", "")).strip())


def _fetch_messages(mail: imaplib.IMAP4_SSL, seq_id: str) -> list[email.message.Message]:
    res, msg_data = mail.fetch(seq_id, "(RFC822)")
    if res != "OK" or not msg_data:
        return []
    return [email.message_from_bytes(part[1]) for part in msg_data if isinstance(part, tuple)]


def check_latest_reply(
    expected_subject_snippet: str = "",
    ignore_message_ids: Optional[set[str]] = None,
    allowed_senders: Optional[set[str]] = None,
) -> Optional[EmailReply]:
    """Newest unseen, unprocessed Primary-inbox reply from an allowed sender, or None."""
    mail = _connect_imap_client()
    if not mail:
        return None
    subject_snippet = expected_subject_snippet.lower().strip()
    skip_ids = PROCESSED_MESSAGE_IDS | (ignore_message_ids or set())
    try:
        for seq_id in reversed(_search_unseen_ids(mail)):
            if seq_id in skip_ids:
                continue
            for msg in _fetch_messages(mail, seq_id):
                reply = _read_reply(mail, seq_id, msg, subject_snippet, allowed_senders)
                if reply:
                    return reply
        return None
    except Exception as e:
        logger.error(f"[IMAP CHECK ERROR] Error polling inbox: {e}")
        return None
    finally:
        _disconnect(mail)
