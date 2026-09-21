"""
Email transport service providing real-world live negotiation testing over Gmail.
Uses standard library smtplib and imaplib to dispatch dynamically generated drafts
and retrieve incoming counter-offers from counterparties.
"""
import email
import email.header
from email.message import EmailMessage
import imaplib
import logging
import re
import smtplib
from typing import Optional

from app.config import settings

logger = logging.getLogger("email_service")


def parse_email_draft(raw_draft: str, default_subject: str = "Commodity Arbitrage Trade Correspondence") -> tuple[str, str]:
    """
    Parse the subject and body from a dynamic agent-generated email draft.
    Supports both:
      1. 'SUBJECT: ... \nBODY:\n ...'
      2. 'Subject: ... \n\n ...'
      3. Plain unstructured text (returns default subject)
    """
    raw = (raw_draft or "").strip()
    if not raw:
        return default_subject, ""

    # Case 1: STRICT SUBJECT / BODY format
    sub_match = re.search(r"^SUBJECT:\s*(.+)$", raw, re.MULTILINE | re.IGNORECASE)
    body_match = re.search(r"BODY:\s*\n?(.*)$", raw, re.DOTALL | re.IGNORECASE)
    if sub_match and body_match:
        subject = sub_match.group(1).strip()
        body = body_match.group(1).strip()
        return subject, body

    # Case 2: Standard RFC/Markdown header 'Subject: <subject>\n\n<body>'
    if raw.lower().startswith("subject:"):
        parts = raw.split("\n\n", 1)
        subject_line = parts[0]
        subject = re.sub(r"^subject:\s*", "", subject_line, flags=re.IGNORECASE).strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        return subject, body

    # Case 3: Subject on first line without blank line delimiter
    first_line, _, rest = raw.partition("\n")
    if first_line.lower().startswith("subject:"):
        subject = re.sub(r"^subject:\s*", "", first_line, flags=re.IGNORECASE).strip()
        return subject, rest.strip()

    return default_subject, raw


def send_email(to_email: str, raw_draft: str) -> bool:
    """
    Dispatches an email draft to the destination address using SMTP STARTTLS.
    Parses dynamic SUBJECT and BODY from raw_draft.
    Returns True if successfully dispatched, False otherwise.
    """
    subject, body = parse_email_draft(raw_draft)
    to_addr = (to_email or "").strip()
    if not to_addr:
        logger.warning("Cannot send email: recipient address is empty.")
        return False

    if not settings.email_user or not settings.email_pass:
        logger.info(
            f"[MOCK SEND] Email credentials not configured. Mock dispatch to {to_addr}\n"
            f"Subject: {subject}\nBody length: {len(body)} chars."
        )
        return True

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = settings.email_user
        msg["To"] = to_addr
        msg.set_content(body)

        with smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(settings.email_user, settings.email_pass)
            server.send_message(msg)

        logger.info(f"[EMAIL SENT] Successfully dispatched to {to_addr} with subject: '{subject}'")
        return True
    except Exception as e:
        logger.error(f"[EMAIL SEND ERROR] Failed sending to {to_addr}: {e}")
        return False


def get_unseen_message_ids() -> set[str]:
    """Return set of current UNSEEN message IDs in INBOX (useful to snapshot existing emails)."""
    if not settings.email_user or not settings.email_pass:
        return set()
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(settings.imap_server, timeout=15)
        mail.login(settings.email_user, settings.email_pass)
        mail.select("INBOX")
        status, messages = mail.search(None, "UNSEEN")
        if status == "OK" and messages and messages[0]:
            return {
                m.decode() if isinstance(m, bytes) else str(m)
                for m in messages[0].split()
            }
        return set()
    except Exception as e:
        logger.error(f"[IMAP SNAPSHOT ERROR] Failed fetching unseen IDs: {e}")
        return set()
    finally:
        if mail:
            try:
                mail.close()
            except Exception:
                pass
            try:
                mail.logout()
            except Exception:
                pass


def check_latest_reply(
    expected_subject_snippet: str = "",
    ignore_message_ids: Optional[set[str]] = None,
) -> Optional[str]:
    """
    Connects to IMAP inbox, searches for unseen replies containing expected_subject_snippet,
    extracts the clean plaintext body, marks as read, and returns the string.
    Returns None if no matching unseen message is found or if credentials are not configured.
    """
    if not settings.email_user or not settings.email_pass:
        logger.debug("[IMAP CHECK] Email credentials not configured, skipping IMAP poll.")
        return None

    mail = None
    try:
        mail = imaplib.IMAP4_SSL(settings.imap_server, timeout=15)
        mail.login(settings.email_user, settings.email_pass)
        mail.select("INBOX")

        # Search for UNSEEN messages
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK" or not messages or not messages[0]:
            return None

        msg_ids = messages[0].split()
        target_snippet = expected_subject_snippet.lower().strip()

        # Inspect most recent messages first
        for msg_id in reversed(msg_ids):
            msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            if ignore_message_ids and msg_id_str in ignore_message_ids:
                continue

            res, msg_data = mail.fetch(msg_id, "(RFC822)")
            if res != "OK" or not msg_data:
                continue

            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    raw_subject = msg.get("Subject", "")

                    # Decode RFC 2047 subject headers
                    decoded_pieces = email.header.decode_header(raw_subject)
                    subject_str = "".join(
                        piece.decode(enc or "utf-8", errors="replace") if isinstance(piece, bytes) else str(piece)
                        for piece, enc in decoded_pieces
                    )

                    from_header = str(msg.get("From", "")).lower()

                    # Prevent picking up our own outbound dispatched emails when testing with self
                    is_from_desk = bool(settings.email_user and settings.email_user.lower() in from_header)
                    has_reply_pattern = (
                        subject_str.lower().startswith("re:")
                        or " re:" in subject_str.lower()
                        or bool(msg.get("In-Reply-To"))
                    )
                    if is_from_desk and not has_reply_pattern:
                        # This is our own outbound dispatch sitting in inbox, not a buyer reply
                        continue

                    # Verify subject snippet if provided
                    if not target_snippet or target_snippet in subject_str.lower():
                        body = _extract_plaintext_body(msg)
                        if not body.strip():
                            continue
                        # Mark email as read
                        mail.store(msg_id, "+FLAGS", "\\Seen")
                        return body
        return None
    except Exception as e:
        logger.error(f"[IMAP CHECK ERROR] Error polling inbox: {e}")
        return None
    finally:
        if mail:
            try:
                mail.close()
            except Exception:
                pass
            try:
                mail.logout()
            except Exception:
                pass


def _extract_plaintext_body(msg: email.message.Message) -> str:
    """Extract clean plaintext content from multipart or singlepart email message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(charset, errors="replace")
                    break
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(charset, errors="replace")

    return _clean_email_reply_text(body)


def _clean_email_reply_text(raw_text: str) -> str:
    """Strip quoted reply headers (e.g. 'On ... wrote:', '>' prefixes)."""
    lines = raw_text.strip().splitlines()
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        # Common email quote indicators
        if stripped.startswith(">"):
            continue
        if re.match(r"^On\s+.+wrote:$", stripped, re.IGNORECASE):
            break
        if stripped.startswith("-----Original Message-----"):
            break
        clean_lines.append(line)
    return "\n".join(clean_lines).strip()
