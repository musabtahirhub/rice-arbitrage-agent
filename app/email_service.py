import email
import email.header
import email.utils
from email.message import EmailMessage
import imaplib
import logging
import re
import smtplib
from typing import Optional

from app.config import settings

logger = logging.getLogger("email_service")

PROCESSED_MESSAGE_IDS: set[str] = set()
LAST_OUTBOUND_MESSAGE_ID: Optional[str] = None

SIMULATED_DOMAINS = (
    "@domain.com",
    "@example.com",
    "@test.com",
    "@invalid",
    "@local",
    "@gulffood.ae",
    "@albarakah-foods.sa",
    "@emiratesgrain.ae",
    "@indusrice.pk",
    "@thaigrain.co.th",
    "@mekongdelta-agro.vn",
    "@supplier.com",
    "@buyer.com",
)


def is_simulated_email(email_addr: str) -> bool:
    addr = (email_addr or "").strip().lower()
    if not addr:
        return True
    if settings.my_test_email and addr == settings.my_test_email.strip().lower():
        return False
    if settings.email_user and addr == settings.email_user.strip().lower():
        return False
    return any(addr.endswith(d) for d in SIMULATED_DOMAINS)


def normalize_thread_subject(subject: str, thread_subject: Optional[str] = None) -> str:
    base = (thread_subject or subject or "Commodity Trade Negotiation").strip()
    clean_base = re.sub(r"^(?:re|fwd|fw):\s*", "", base, flags=re.IGNORECASE).strip()
    return f"Re: {clean_base}"


def is_automated_or_bounce_message(from_header: str, subject_str: str) -> bool:
    from_low = (from_header or "").lower()
    sub_low = (subject_str or "").lower()

    daemon_indicators = (
        "mailer-daemon",
        "postmaster",
        "no-reply",
        "noreply",
        "notifications@",
        "notification@",
        "googlemail.com",
        "google.com",
        "instagram.com",
        "openai.com",
    )
    if any(ind in from_low for ind in daemon_indicators):
        return True

    bounce_subjects = (
        "delivery status notification",
        "failure",
        "undeliverable",
        "returned mail",
        "mail delivery failed",
        "address not found",
    )
    return any(bs in sub_low for bs in bounce_subjects)


def parse_email_draft(raw_draft: str, default_subject: str = "Commodity Arbitrage Trade Correspondence") -> tuple[str, str]:
    raw = (raw_draft or "").strip()
    if not raw:
        return default_subject, ""

    sub_match = re.search(r"^SUBJECT:\s*(.+)$", raw, re.MULTILINE | re.IGNORECASE)
    body_match = re.search(r"BODY:\s*\n?(.*)$", raw, re.DOTALL | re.IGNORECASE)
    if sub_match and body_match:
        subject = sub_match.group(1).strip()
        body = body_match.group(1).strip()
        return subject, body

    if raw.lower().startswith("subject:"):
        parts = raw.split("\n\n", 1)
        subject_line = parts[0]
        subject = re.sub(r"^subject:\s*", "", subject_line, flags=re.IGNORECASE).strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        return subject, body

    first_line, _, rest = raw.partition("\n")
    if first_line.lower().startswith("subject:"):
        subject = re.sub(r"^subject:\s*", "", first_line, flags=re.IGNORECASE).strip()
        return subject, rest.strip()

    return default_subject, raw


class EmailReply(str):
    subject: str = ""
    sender: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""

    def __new__(
        cls,
        body: str,
        subject: str = "",
        sender: str = "",
        message_id: str = "",
        in_reply_to: str = "",
        references: str = "",
    ):
        obj = super().__new__(cls, body)
        obj.subject = subject
        obj.sender = sender
        obj.message_id = message_id
        obj.in_reply_to = in_reply_to
        obj.references = references
        return obj


def _format_smtp_references(in_reply_to: Optional[str], references: Optional[str]) -> tuple[str, str]:
    clean_irt = ""
    if in_reply_to:
        clean_irt = in_reply_to.strip()
        if not clean_irt.startswith("<"):
            clean_irt = f"<{clean_irt}>"

    clean_refs = ""
    if references:
        clean_refs = references.strip()
        if clean_irt and clean_irt not in clean_refs:
            clean_refs = f"{clean_refs} {clean_irt}".strip()
    elif clean_irt:
        clean_refs = clean_irt

    return clean_irt, clean_refs


def _build_smtp_mime_message(
    clean_user: str,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: Optional[str],
    references: Optional[str],
    custom_message_id: Optional[str],
) -> tuple[EmailMessage, str]:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = clean_user
    msg["To"] = to_addr

    domain = clean_user.split("@")[1] if "@" in clean_user else "gmail.com"
    outbound_msg_id = custom_message_id or email.utils.make_msgid(domain=domain)
    msg["Message-ID"] = outbound_msg_id

    clean_irt, clean_refs = _format_smtp_references(in_reply_to, references)
    if clean_irt:
        msg["In-Reply-To"] = clean_irt
    if clean_refs:
        msg["References"] = clean_refs

    msg.set_content(body)
    return msg, outbound_msg_id


def _dispatch_smtp_transport(
    msg: EmailMessage,
    clean_user: str,
    clean_pass: str,
    to_addr: str,
    subject: str,
    outbound_msg_id: str,
) -> bool:
    try:
        with smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=settings.email_timeout_seconds) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(clean_user, clean_pass)
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
    global LAST_OUTBOUND_MESSAGE_ID
    parsed_sub, body = parse_email_draft(raw_draft)
    to_addr = (to_email or "").strip()
    if not to_addr:
        logger.warning("Cannot send email: recipient address is empty.")
        return False

    subject = normalize_thread_subject(parsed_sub, thread_subject) if thread_subject else parsed_sub

    if not settings.email_user or not settings.email_pass or is_simulated_email(to_addr):
        logger.info(
            f"[MOCK SEND] Safely simulated dispatch to {to_addr} (no live SMTP packet sent)\n"
            f"Subject: {subject}\nBody length: {len(body)} chars."
        )
        return True

    clean_user = settings.email_user.strip()
    clean_pass = settings.email_pass.replace(" ", "").strip()

    msg, outbound_msg_id = _build_smtp_mime_message(
        clean_user, to_addr, subject, body, in_reply_to, references, custom_message_id
    )
    LAST_OUTBOUND_MESSAGE_ID = outbound_msg_id

    return _dispatch_smtp_transport(msg, clean_user, clean_pass, to_addr, subject, outbound_msg_id)


def _connect_imap_client() -> Optional[imaplib.IMAP4_SSL]:
    if not settings.email_user or not settings.email_pass:
        return None
    clean_user = settings.email_user.strip()
    clean_pass = settings.email_pass.replace(" ", "").strip()
    try:
        mail = imaplib.IMAP4_SSL(settings.imap_server, timeout=settings.email_timeout_seconds)
        mail.login(clean_user, clean_pass)
        mail.select(settings.imap_mailbox_folder)
        return mail
    except Exception as e:
        logger.error(f"[IMAP CLIENT ERROR] Failed to connect: {e}")
        return None


def get_unseen_message_ids() -> set[str]:
    mail = _connect_imap_client()
    if not mail:
        return set()
    try:
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
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass


def initialize_unseen_snapshot() -> set[str]:
    global PROCESSED_MESSAGE_IDS
    unseen = get_unseen_message_ids()
    PROCESSED_MESSAGE_IDS.update(unseen)
    logger.info(f"[IMAP SNAPSHOT] Baseline unseen messages snapshot registered ({len(unseen)} IDs ignored).")
    return PROCESSED_MESSAGE_IDS


def _clean_email_reply_text(raw_text: str) -> str:
    lines = raw_text.strip().splitlines()
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^On\s+.+wrote:$", stripped, re.IGNORECASE):
            break
        if stripped.startswith("-----Original Message-----"):
            break
        clean_lines.append(line)
    return "\n".join(clean_lines).strip()


def _extract_plaintext_body(msg: email.message.Message) -> str:
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


def _decode_mime_subject(raw_subject: str) -> str:
    decoded_pieces = email.header.decode_header(raw_subject)
    return "".join(
        piece.decode(enc or "utf-8", errors="replace") if isinstance(piece, bytes) else str(piece)
        for piece, enc in decoded_pieces
    )


def _should_ignore_inbound_message(
    from_header: str,
    subject_str: str,
    clean_user: str,
    allowed_senders: Optional[set[str]],
) -> bool:
    if is_automated_or_bounce_message(from_header, subject_str):
        return True
    if clean_user and clean_user.lower() in from_header.lower():
        return True
    if allowed_senders:
        if not any(allowed.lower() in from_header.lower() for allowed in allowed_senders):
            return True
    return False


def _mark_imap_message_seen(mail, msg_id, msg_id_str: str, rfc_msg_id: str):
    global PROCESSED_MESSAGE_IDS
    mail.store(msg_id, "+FLAGS", "\\Seen")
    PROCESSED_MESSAGE_IDS.add(msg_id_str)
    if rfc_msg_id:
        PROCESSED_MESSAGE_IDS.add(rfc_msg_id)

def _build_email_reply_object(msg, msg_id_str: str, rfc_msg_id: str, subject_str: str, from_header: str, body: str) -> EmailReply:
    raw_irt = str(msg.get("In-Reply-To", "")).strip()
    raw_refs = str(msg.get("References", "")).strip()
    return EmailReply(
        body=body,
        subject=subject_str,
        sender=from_header,
        message_id=rfc_msg_id or msg_id_str,
        in_reply_to=raw_irt,
        references=raw_refs,
    )

def _parse_imap_response_part(
    mail,
    msg_id,
    msg_id_str: str,
    response_part,
    target_snippet: str,
    clean_user: str,
    allowed_senders: Optional[set[str]],
) -> Optional[EmailReply]:
    global PROCESSED_MESSAGE_IDS
    if not isinstance(response_part, tuple):
        return None

    msg = email.message_from_bytes(response_part[1])
    raw_subject = msg.get("Subject", "")
    subject_str = _decode_mime_subject(raw_subject)
    from_header = str(msg.get("From", ""))
    rfc_msg_id = str(msg.get("Message-ID", "")).strip()

    if rfc_msg_id and rfc_msg_id in PROCESSED_MESSAGE_IDS:
        return None

    if _should_ignore_inbound_message(from_header, subject_str, clean_user, allowed_senders):
        _mark_imap_message_seen(mail, msg_id, msg_id_str, rfc_msg_id)
        return None

    if target_snippet and target_snippet not in subject_str.lower():
        return None

    body = _extract_plaintext_body(msg)
    if not body.strip():
        return None

    _mark_imap_message_seen(mail, msg_id, msg_id_str, rfc_msg_id)
    return _build_email_reply_object(msg, msg_id_str, rfc_msg_id, subject_str, from_header, body)


def check_latest_reply(
    expected_subject_snippet: str = "",
    ignore_message_ids: Optional[set[str]] = None,
    allowed_senders: Optional[set[str]] = None,
) -> Optional[EmailReply]:
    mail = _connect_imap_client()
    if not mail:
        return None

    clean_user = settings.email_user.strip()
    target_snippet = expected_subject_snippet.lower().strip()

    try:
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK" or not messages or not messages[0]:
            return None

        msg_ids = messages[0].split()
        for msg_id in reversed(msg_ids):
            msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            if msg_id_str in PROCESSED_MESSAGE_IDS:
                continue
            if ignore_message_ids and msg_id_str in ignore_message_ids:
                continue

            res, msg_data = mail.fetch(msg_id, "(RFC822)")
            if res != "OK" or not msg_data:
                continue

            for response_part in msg_data:
                reply = _parse_imap_response_part(
                    mail, msg_id, msg_id_str, response_part, target_snippet, clean_user, allowed_senders
                )
                if reply:
                    return reply
        return None
    except Exception as e:
        logger.error(f"[IMAP CHECK ERROR] Error polling inbox: {e}")
        return None
    finally:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass
