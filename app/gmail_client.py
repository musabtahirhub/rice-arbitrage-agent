import base64
import email
from email.message import EmailMessage
import email.utils
import json
import os
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import settings
from app.logger import get_logger
from app.email_service import is_simulated_email, normalize_thread_subject, parse_email_draft

logger = get_logger("arbitrage_desk.gmail_client")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

CACHE_FILE = Path(".cache/processed_gmail_ids.json")


PROCESSED_CACHE_LIMIT = 2000


def _load_processed_ids() -> dict[str, None]:
    """Insertion-ordered set (dict keys) of Gmail ids already handled by this process."""
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return dict.fromkeys(data)
    except Exception as e:
        logger.warning(f"Failed loading processed gmail ids: {e}")
    return {}


def mark_gmail_message_processed(msg_id: str) -> None:
    """Cheap per-process pre-filter, called after a message was handled. The processed_inbound_emails
    table is the source of truth across processes; this only avoids re-fetching known messages."""
    if not msg_id:
        return
    PROCESSED_GMAIL_MESSAGE_IDS.pop(msg_id, None)
    PROCESSED_GMAIL_MESSAGE_IDS[msg_id] = None
    while len(PROCESSED_GMAIL_MESSAGE_IDS) > PROCESSED_CACHE_LIMIT:  # evict oldest first
        PROCESSED_GMAIL_MESSAGE_IDS.pop(next(iter(PROCESSED_GMAIL_MESSAGE_IDS)))
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(PROCESSED_GMAIL_MESSAGE_IDS), f)
    except Exception as e:
        logger.warning(f"Failed saving processed gmail id: {e}")


PROCESSED_GMAIL_MESSAGE_IDS: dict[str, None] = _load_processed_ids()


GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_SETUP_HINT = (
    "Run `python scripts/gmail_auth.py` once and put GMAIL_REFRESH_TOKEN (plus GMAIL_CLIENT_ID / "
    "GMAIL_CLIENT_SECRET, or keep credentials.json) in .env."
)

_cached_credentials: Optional[Credentials] = None


def _client_id_and_secret() -> tuple[str, str]:
    """From settings, falling back to the OAuth client in credentials.json."""
    client_id, client_secret = settings.gmail_client_id.strip(), settings.gmail_client_secret.strip()
    if client_id and client_secret:
        return client_id, client_secret
    path = settings.google_credentials_json_path
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            client = data.get("installed") or data.get("web") or {}
            return client_id or client.get("client_id", ""), client_secret or client.get("client_secret", "")
        except Exception as e:
            logger.warning(f"[GMAIL CLIENT] Could not read OAuth client from {path}: {e}")
    return client_id, client_secret


def _credentials_from_refresh_token() -> Optional[Credentials]:
    refresh_token = settings.gmail_refresh_token.strip()
    if not refresh_token:
        return None
    client_id, client_secret = _client_id_and_secret()
    if not (client_id and client_secret):
        raise RuntimeError(
            "GMAIL_REFRESH_TOKEN is set but no OAuth client id/secret was found "
            "(set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET, or provide credentials.json)."
        )
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=GOOGLE_TOKEN_URI,
        scopes=SCOPES,
    )


def _credentials_from_token_file() -> Optional[Credentials]:
    """Legacy fallback: an authorized-user token.json (e.g. from an earlier browser sign-in)."""
    token_path = settings.google_token_json_path
    if not os.path.exists(token_path):
        return None
    try:
        return Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception as e:
        logger.error(f"[GMAIL CLIENT] Error loading token from {token_path}: {e}")
        return None


def _load_credentials() -> Credentials:
    """Non-interactive Gmail credentials; never opens a browser.

    Order: GMAIL_REFRESH_TOKEN from settings, then token.json. Access tokens are refreshed as needed and
    kept in memory, so the token endpoint is only hit about once an hour.
    """
    global _cached_credentials
    creds = _cached_credentials or _credentials_from_refresh_token() or _credentials_from_token_file()
    if creds is None:
        if not os.path.exists(settings.google_credentials_json_path):
            raise FileNotFoundError(f"Gmail is not configured. {AUTH_SETUP_HINT}")
        raise RuntimeError(f"No Gmail refresh token available. {AUTH_SETUP_HINT}")

    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError(f"Gmail credentials have no refresh token. {AUTH_SETUP_HINT}")
        try:
            creds.refresh(Request())
        except Exception as e:
            _cached_credentials = None
            raise RuntimeError(
                f"Gmail refresh token was rejected ({e}). It may be expired or revoked. {AUTH_SETUP_HINT}"
            ) from e

    _cached_credentials = creds
    return creds


def get_gmail_service():
    return build("gmail", "v1", credentials=_load_credentials(), cache_discovery=False)


def setup_gmail_watch(topic_name: str) -> dict:
    service = get_gmail_service()
    body = {
        "topicName": topic_name,
        "labelIds": ["INBOX"],
    }
    watch_response = service.users().watch(userId="me", body=body).execute()
    logger.info(f"[GMAIL WATCH] Watch registered for topic '{topic_name}': {watch_response}")
    return watch_response


def stop_gmail_watch() -> None:
    """Stop push notifications for this mailbox (users.stop). Safe to call when no watch is active."""
    get_gmail_service().users().stop(userId="me").execute()
    logger.info("[GMAIL WATCH] Push notifications stopped for this mailbox.")


def _decode_base64_urlsafe(b64_str: str) -> str:
    if not b64_str:
        return ""
    try:
        padded = b64_str + "=" * ((4 - len(b64_str) % 4) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _collect_parts_recursively(parts: list[dict], plain_parts: list[str], html_parts: list[str]):
    for part in parts:
        p_mime = part.get("mimeType", "")
        p_data = part.get("body", {}).get("data")
        if p_data:
            decoded = _decode_base64_urlsafe(p_data)
            if decoded:
                if p_mime == "text/plain":
                    plain_parts.append(decoded)
                elif p_mime == "text/html":
                    html_parts.append(decoded)
        if "parts" in part:
            _collect_parts_recursively(part["parts"], plain_parts, html_parts)


def _convert_html_to_plain_text(html_content: str) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")
        return soup.get_text()
    except Exception:
        return html_content


def _extract_body_from_payload(payload: dict) -> str:
    if not payload:
        return ""

    body_data = payload.get("body", {}).get("data")
    mime_type = payload.get("mimeType", "")

    if body_data and mime_type.startswith("text/plain"):
        decoded = _decode_base64_urlsafe(body_data)
        if decoded:
            return decoded

    parts = payload.get("parts", [])
    if parts:
        plain_parts: list[str] = []
        html_parts: list[str] = []
        _collect_parts_recursively(parts, plain_parts, html_parts)
        if plain_parts:
            return "\n".join(plain_parts)
        if html_parts:
            return _convert_html_to_plain_text("\n".join(html_parts))

    if body_data:
        decoded = _decode_base64_urlsafe(body_data)
        if decoded:
            return decoded

    return payload.get("snippet", "")


def _parse_gmail_message(full_msg: dict) -> dict:
    msg_id = full_msg.get("id", "")
    thread_id = full_msg.get("threadId", "")
    payload = full_msg.get("payload", {})
    headers = payload.get("headers", [])

    header_map = {}
    for h in headers:
        header_map[h.get("name", "").lower()] = h.get("value", "")

    msg_id_hdr = header_map.get("message-id", "")
    in_reply_to = header_map.get("in-reply-to", "")
    references = header_map.get("references", "")
    subject = header_map.get("subject", "")
    from_hdr = header_map.get("from", "")

    body = _extract_body_from_payload(payload)

    return {
        "id": msg_id,
        "threadId": thread_id,
        "thread_id": thread_id,
        "Message-ID": msg_id_hdr,
        "message_id": msg_id_hdr,
        "In-Reply-To": in_reply_to,
        "in_reply_to": in_reply_to,
        "References": references,
        "references": references,
        "Subject": subject,
        "subject": subject,
        "From": from_hdr,
        "from": from_hdr,
        "sender": from_hdr,
        "body": body,
        "snippet": full_msg.get("snippet", ""),
        "payload": payload,
    }


def _fetch_and_parse_single_message(service, msg_id: str) -> Optional[dict]:
    try:
        full_msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="full",
        ).execute()
        return _parse_gmail_message(full_msg)
    except Exception as e:
        logger.error(f"[GMAIL] Failed to fetch message {msg_id}: {e}")
        return None


def _get_history_records(service, start_history_id: str) -> list[dict]:
    if not start_history_id:
        return []
    try:
        history_res = service.users().history().list(
            userId="me",
            startHistoryId=str(start_history_id),
            historyTypes=["messageAdded"],
        ).execute()
        return history_res.get("history", [])
    except Exception as e:
        logger.warning(f"[GMAIL] Failed to list history for startHistoryId={start_history_id}: {e}")
        return []


def fetch_latest_messages_by_history(start_history_id: str) -> list[dict]:
    service = get_gmail_service()
    history_records = _get_history_records(service, start_history_id)
    seen_ids = set()
    messages_out = []

    for record in history_records:
        for item in record.get("messagesAdded", []):
            msg_stub = item.get("message", {})
            msg_id = msg_stub.get("id")
            if not msg_id or msg_id in seen_ids or msg_id in PROCESSED_GMAIL_MESSAGE_IDS:
                continue
            seen_ids.add(msg_id)
            parsed = _fetch_and_parse_single_message(service, msg_id)
            if parsed:
                messages_out.append(parsed)

    return messages_out


def _format_reply_references(in_reply_to: Optional[str], references: Optional[str]) -> tuple[str, str]:
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


def _build_mime_reply(
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: Optional[str],
    references: Optional[str],
) -> tuple[EmailMessage, str]:
    msg = EmailMessage()
    msg["Subject"] = subject
    from_addr = settings.email_user or settings.desk_email or "desk@arbitrage.com"
    msg["From"] = from_addr
    msg["To"] = to_addr

    domain = from_addr.split("@")[1] if "@" in from_addr else "gmail.com"
    outbound_msg_id = email.utils.make_msgid(domain=domain)
    msg["Message-ID"] = outbound_msg_id

    clean_irt, clean_refs = _format_reply_references(in_reply_to, references)
    if clean_irt:
        msg["In-Reply-To"] = clean_irt
    if clean_refs:
        msg["References"] = clean_refs

    msg.set_content(body)
    return msg, outbound_msg_id


def _dispatch_gmail_request(
    send_body: dict[str, Any],
    to_addr: str,
    subject: str,
    outbound_msg_id: str,
) -> bool:
    try:
        service = get_gmail_service()
        service.users().messages().send(userId="me", body=send_body).execute()
        logger.info(
            f"[GMAIL SENT] Dispatched reply to {to_addr} | Subject: '{subject}' | Message-ID: {outbound_msg_id}"
        )
        return True
    except FileNotFoundError:
        if is_simulated_email(to_addr):
            logger.info(
                f"[GMAIL MOCK SEND] Safely simulated dispatch to {to_addr} (credentials not configured)\n"
                f"Subject: {subject}\nMessage-ID: {outbound_msg_id}"
            )
            return True
        logger.error(f"[GMAIL SEND ERROR] Credentials file not found and recipient '{to_addr}' is not simulated.")
        return False
    except Exception as e:
        if is_simulated_email(to_addr):
            logger.info(
                f"[GMAIL MOCK SEND] Simulation fallback for {to_addr} following API error: {e}"
            )
            return True
        logger.error(f"[GMAIL SEND ERROR] Failed sending to {to_addr}: {e}")
        return False


def send_gmail_reply(
    to_email: str,
    raw_draft: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    thread_subject: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> bool:
    parsed_sub, body = parse_email_draft(raw_draft)
    to_addr = (to_email or "").strip()
    if not to_addr:
        logger.warning("[GMAIL SEND] Cannot send reply: recipient address is empty.")
        return False

    subject = normalize_thread_subject(parsed_sub, thread_subject) if thread_subject else parsed_sub
    msg, outbound_msg_id = _build_mime_reply(to_addr, subject, body, in_reply_to, references)

    encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    send_body: dict[str, Any] = {"raw": encoded_message}
    if thread_id:
        send_body["threadId"] = thread_id

    return _dispatch_gmail_request(send_body, to_addr, subject, outbound_msg_id)
