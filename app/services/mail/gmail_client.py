"""Gmail API: headless credentials, watch/history (push mode), message parsing, and sending replies."""
import base64
import json
import os
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import settings
from app.logger import get_logger
from app.services.mail.email_utils import (
    build_mime_message,
    is_simulated_email,
    normalize_thread_subject,
    split_subject_and_body,
)
from app.services.mail.smtp import send_email

logger = get_logger("arbitrage_desk.gmail_client")


SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


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


def _refresh_credentials(creds: Credentials) -> None:
    if not creds.refresh_token:
        raise RuntimeError(f"Gmail credentials have no refresh token. {AUTH_SETUP_HINT}")
    try:
        creds.refresh(Request())
    except Exception as e:
        raise RuntimeError(
            f"Gmail refresh token was rejected ({e}). It may be expired or revoked. {AUTH_SETUP_HINT}"
        ) from e


def _load_credentials() -> Credentials:
    """Non-interactive Gmail credentials: GMAIL_REFRESH_TOKEN from settings, then token.json.

    Access tokens are refreshed as needed and kept in memory, so the token endpoint is hit about once an hour.
    """
    global _cached_credentials
    creds = _cached_credentials or _credentials_from_refresh_token() or _credentials_from_token_file()
    if creds is None:
        if not os.path.exists(settings.google_credentials_json_path):
            raise FileNotFoundError(f"Gmail is not configured. {AUTH_SETUP_HINT}")
        raise RuntimeError(f"No Gmail refresh token available. {AUTH_SETUP_HINT}")

    if not creds.valid:
        try:
            _refresh_credentials(creds)
        except RuntimeError:
            _cached_credentials = None
            raise

    _cached_credentials = creds
    return creds


def get_gmail_service():
    return build("gmail", "v1", credentials=_load_credentials(), cache_discovery=False)


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


PROCESSED_GMAIL_MESSAGE_IDS: dict[str, None] = _load_processed_ids()


def is_gmail_message_processed(msg_id: str) -> bool:
    return msg_id in PROCESSED_GMAIL_MESSAGE_IDS


def _persist_processed_ids() -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(PROCESSED_GMAIL_MESSAGE_IDS), f)
    except Exception as e:
        logger.warning(f"Failed saving processed gmail id: {e}")


def mark_gmail_message_processed(msg_id: str) -> None:
    """Record a handled (or deliberately skipped) message so later history listings don't re-fetch it."""
    if not msg_id:
        return
    PROCESSED_GMAIL_MESSAGE_IDS.pop(msg_id, None)
    PROCESSED_GMAIL_MESSAGE_IDS[msg_id] = None
    while len(PROCESSED_GMAIL_MESSAGE_IDS) > PROCESSED_CACHE_LIMIT:  # evict oldest first
        PROCESSED_GMAIL_MESSAGE_IDS.pop(next(iter(PROCESSED_GMAIL_MESSAGE_IDS)))
    _persist_processed_ids()


def decode_base64url(data: str) -> str:
    if not data:
        return ""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _collect_text_parts(parts: list[dict], plain_parts: list[str], html_parts: list[str]) -> None:
    for part in parts:
        decoded = decode_base64url(part.get("body", {}).get("data"))
        if decoded and part.get("mimeType") == "text/plain":
            plain_parts.append(decoded)
        elif decoded and part.get("mimeType") == "text/html":
            html_parts.append(decoded)
        _collect_text_parts(part.get("parts", []), plain_parts, html_parts)


def html_to_text(html_content: str) -> str:
    try:
        return BeautifulSoup(html_content, "html.parser").get_text()
    except Exception:
        return html_content


def extract_payload_body(payload: dict) -> str:
    """Prefer text/plain, then text/html converted to text, then the top-level body data."""
    if not payload:
        return ""
    body_data = payload.get("body", {}).get("data")
    if body_data and payload.get("mimeType", "").startswith("text/plain"):
        decoded = decode_base64url(body_data)
        if decoded:
            return decoded

    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_text_parts(payload.get("parts", []), plain_parts, html_parts)
    if plain_parts:
        return "\n".join(plain_parts)
    if html_parts:
        return html_to_text("\n".join(html_parts))
    return decode_base64url(body_data)


def parse_gmail_message(full_msg: dict) -> dict:
    """Flatten a full Gmail message into the keys the inbound pipeline reads (header-style names)."""
    payload = full_msg.get("payload", {})
    headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
    return {
        "id": full_msg.get("id", ""),
        "threadId": full_msg.get("threadId", ""),
        "labelIds": list(full_msg.get("labelIds", [])),
        "Message-ID": headers.get("message-id", ""),
        "In-Reply-To": headers.get("in-reply-to", ""),
        "References": headers.get("references", ""),
        "Subject": headers.get("subject", ""),
        "From": headers.get("from", ""),
        "body": extract_payload_body(payload) or full_msg.get("snippet", ""),
    }


def setup_gmail_watch(topic_name: str) -> dict:
    body = {"topicName": topic_name, "labelIds": ["INBOX"]}
    watch_response = get_gmail_service().users().watch(userId="me", body=body).execute()
    logger.info(f"[GMAIL WATCH] Watch registered for topic '{topic_name}': {watch_response}")
    return watch_response


def stop_gmail_watch() -> None:
    """Stop push notifications for this mailbox (users.stop). Safe to call when no watch is active."""
    get_gmail_service().users().stop(userId="me").execute()
    logger.info("[GMAIL WATCH] Push notifications stopped for this mailbox.")


def _list_added_inbox_message_ids(service, start_history_id: str) -> list[str]:
    """Ids of messages added to INBOX since the cursor, in order, without duplicates."""
    if not start_history_id:
        return []
    try:
        history = service.users().history().list(
            userId="me",
            startHistoryId=str(start_history_id),
            historyTypes=["messageAdded"],
            labelId="INBOX",
        ).execute().get("history", [])
    except Exception as e:
        logger.warning(f"[GMAIL] Failed to list history for startHistoryId={start_history_id}: {e}")
        return []
    added = (item.get("message", {}).get("id") for record in history for item in record.get("messagesAdded", []))
    return list(dict.fromkeys(msg_id for msg_id in added if msg_id))


def _fetch_message(service, msg_id: str) -> Optional[dict]:
    try:
        full_msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        return parse_gmail_message(full_msg)
    except Exception as e:
        logger.error(f"[GMAIL] Failed to fetch message {msg_id}: {e}")
        return None


def fetch_latest_messages_by_history(start_history_id: str) -> list[dict]:
    """Parsed INBOX messages added since the cursor that this process has not handled yet."""
    service = get_gmail_service()
    new_ids = [m for m in _list_added_inbox_message_ids(service, start_history_id) if not is_gmail_message_processed(m)]
    return [msg for msg in (_fetch_message(service, msg_id) for msg_id in new_ids) if msg]


OUTBOUND_DEFAULT_SUBJECT = "Commodity Arbitrage Trade Correspondence"


def _execute_send(send_body: dict[str, Any], to_addr: str, subject: str, outbound_msg_id: str) -> bool:
    """Send via the API. Simulated recipients count as delivered when Gmail is unavailable."""
    try:
        get_gmail_service().users().messages().send(userId="me", body=send_body).execute()
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
            logger.info(f"[GMAIL MOCK SEND] Simulation fallback for {to_addr} following API error: {e}")
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
    to_addr = (to_email or "").strip()
    if not to_addr:
        logger.warning("[GMAIL SEND] Cannot send reply: recipient address is empty.")
        return False

    parsed_sub, body = split_subject_and_body(raw_draft, default_subject=OUTBOUND_DEFAULT_SUBJECT)
    subject = normalize_thread_subject(parsed_sub, thread_subject) if thread_subject else parsed_sub
    from_addr = settings.email_user or settings.desk_email or "desk@arbitrage.com"
    msg, outbound_msg_id = build_mime_message(from_addr, to_addr, subject, body, in_reply_to, references)

    send_body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")}
    if thread_id:
        send_body["threadId"] = thread_id
    return _execute_send(send_body, to_addr, subject, outbound_msg_id)


def send_reply(
    to_email: str,
    draft: str,
    in_reply_to: Optional[str],
    references: Optional[str],
    thread_subject: Optional[str],
    thread_id: Optional[str] = None,
    use_gmail_api: bool = False,
) -> bool:
    if use_gmail_api:
        return bool(send_gmail_reply(
            to_email,
            draft,
            in_reply_to=in_reply_to,
            references=references,
            thread_subject=thread_subject,
            thread_id=thread_id or None,
        ))
    return bool(send_email(
        to_email=to_email,
        raw_draft=draft,
        in_reply_to=in_reply_to,
        references=references,
        thread_subject=thread_subject,
    ))
