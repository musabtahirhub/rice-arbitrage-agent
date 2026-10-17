"""Email helpers shared by every mail path: draft subject/body parsing, sender filters
(simulated, desk, bounce, Gmail Primary inbox) and MIME building/reading.
"""
import email
import email.header
import email.message
import email.utils
import re
from collections.abc import Iterable
from email.message import EmailMessage
from typing import Optional

from app.config import settings

DEFAULT_DRAFT_SUBJECT = "Trade Correspondence"


_LABELED_SUBJECT_RE = re.compile(r"^SUBJECT:\s*(.+)$", re.MULTILINE | re.IGNORECASE)


_LABELED_BODY_RE = re.compile(r"BODY:\s*\n?(.*)$", re.DOTALL | re.IGNORECASE)


_SUBJECT_PREFIX_RE = re.compile(r"^subject:\s*", re.IGNORECASE)


_REPLY_PREFIX_RE = re.compile(r"^(?:re|fwd|fw):\s*", re.IGNORECASE)


_ATTRIBUTION_LINE_RE = re.compile(r"^On\s+.+wrote:$", re.IGNORECASE)


def _split_labeled_draft(raw: str) -> Optional[tuple[str, str]]:
    """'SUBJECT: ...' plus 'BODY: ...' blocks, the format the LLM prompts ask for."""
    sub_match = _LABELED_SUBJECT_RE.search(raw)
    body_match = _LABELED_BODY_RE.search(raw)
    if sub_match and body_match:
        return sub_match.group(1).strip(), body_match.group(1).strip()
    return None


def _split_subject_block(raw: str) -> Optional[tuple[str, str]]:
    """'Subject: ...' first line, body after the first blank line."""
    if not raw.lower().startswith("subject:"):
        return None
    head, _, body = raw.partition("\n\n")
    return _SUBJECT_PREFIX_RE.sub("", head).strip(), body.strip()


def split_subject_and_body(raw_text: str, default_subject: str = DEFAULT_DRAFT_SUBJECT) -> tuple[str, str]:
    raw = (raw_text or "").strip()
    if not raw:
        return default_subject, ""
    return _split_labeled_draft(raw) or _split_subject_block(raw) or (default_subject, raw)


def strip_reply_prefix(subject: str) -> str:
    return _REPLY_PREFIX_RE.sub("", subject or "").strip()


def normalize_thread_subject(subject: str, thread_subject: Optional[str] = None) -> str:
    base = (thread_subject or subject or "Commodity Trade Negotiation").strip()
    return f"Re: {strip_reply_prefix(base)}"


def strip_quoted_reply(raw_text: str) -> str:
    """Drop quoted lines ('> ...') and everything from the reply attribution or forwarded original onward."""
    clean_lines = []
    for line in raw_text.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if _ATTRIBUTION_LINE_RE.match(stripped) or stripped.startswith("-----Original Message-----"):
            break
        clean_lines.append(line)
    return "\n".join(clean_lines).strip()


def make_message_id(from_addr: str) -> str:
    domain = from_addr.split("@")[1] if "@" in from_addr else "gmail.com"
    return email.utils.make_msgid(domain=domain)


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


_DAEMON_SENDER_MARKERS = (
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


_BOUNCE_SUBJECT_MARKERS = (
    "delivery status notification",
    "failure",
    "undeliverable",
    "returned mail",
    "mail delivery failed",
    "address not found",
)


def is_simulated_email(email_addr: str) -> bool:
    """True for directory/demo addresses that must never receive live mail. Configured test inboxes are real."""
    addr = (email_addr or "").strip().lower()
    if not addr:
        return True
    live_inboxes = (settings.my_test_email, settings.my_test_supplier_email, settings.email_user)
    if any(inbox and addr == inbox.strip().lower() for inbox in live_inboxes):
        return False
    return any(addr.endswith(d) for d in SIMULATED_DOMAINS)


def is_automated_or_bounce_message(from_header: str, subject_str: str) -> bool:
    from_low = (from_header or "").lower()
    if any(marker in from_low for marker in _DAEMON_SENDER_MARKERS):
        return True
    sub_low = (subject_str or "").lower()
    return any(marker in sub_low for marker in _BOUNCE_SUBJECT_MARKERS)


def get_desk_email_addresses() -> set[str]:
    return {e.strip().lower() for e in (settings.email_user, settings.desk_email) if e}


def is_from_desk(sender: str, desk_emails: set[str]) -> bool:
    sender_lower = (sender or "").strip().lower()
    return any(desk_email in sender_lower for desk_email in desk_emails)


# Gmail search syntax for the Primary tab. Gmail's history API has no `q` filter, so push mode lists
# INBOX history and checks labels per message (is_primary_inbox_message); IMAP polling passes this
# query to Gmail's X-GM-RAW search extension.
PRIMARY_INBOX_QUERY = "label:INBOX category:primary -label:SPAM -label:TRASH"


NON_PRIMARY_LABELS = frozenset({
    "SPAM",
    "TRASH",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
})


def is_primary_inbox_message(label_ids: Optional[Iterable[str]]) -> bool:
    """True when the message is in INBOX and carries no spam, trash or non-Primary category label."""
    labels = set(label_ids or ())
    return "INBOX" in labels and not labels & NON_PRIMARY_LABELS


def format_reply_headers(in_reply_to: Optional[str], references: Optional[str]) -> tuple[str, str]:
    """Angle-bracketed In-Reply-To, and References that always end with it."""
    clean_irt = (in_reply_to or "").strip()
    if clean_irt and not clean_irt.startswith("<"):
        clean_irt = f"<{clean_irt}>"

    clean_refs = (references or "").strip()
    if clean_refs and clean_irt and clean_irt not in clean_refs:
        clean_refs = f"{clean_refs} {clean_irt}"
    elif not clean_refs:
        clean_refs = clean_irt
    return clean_irt, clean_refs


def build_mime_message(
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    message_id: Optional[str] = None,
) -> tuple[EmailMessage, str]:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    outbound_msg_id = message_id or make_message_id(from_addr)
    msg["Message-ID"] = outbound_msg_id

    clean_irt, clean_refs = format_reply_headers(in_reply_to, references)
    if clean_irt:
        msg["In-Reply-To"] = clean_irt
    if clean_refs:
        msg["References"] = clean_refs

    msg.set_content(body)
    return msg, outbound_msg_id


def decode_mime_header(raw_value: str) -> str:
    return "".join(
        piece.decode(enc or "utf-8", errors="replace") if isinstance(piece, bytes) else str(piece)
        for piece, enc in email.header.decode_header(raw_value)
    )


def _is_inline_plaintext(part: email.message.Message) -> bool:
    disposition = str(part.get("Content-Disposition", ""))
    return part.get_content_type() == "text/plain" and "attachment" not in disposition


def _decode_first_plaintext(msg: email.message.Message) -> str:
    candidates = [p for p in msg.walk() if _is_inline_plaintext(p)] if msg.is_multipart() else [msg]
    for part in candidates:
        payload = part.get_payload(decode=True)
        if payload:
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def extract_reply_text(msg: email.message.Message) -> str:
    """Plain-text body with quoted history removed."""
    return strip_quoted_reply(_decode_first_plaintext(msg))
