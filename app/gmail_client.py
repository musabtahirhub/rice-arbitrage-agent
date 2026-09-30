import base64
import email
from email.message import EmailMessage
import email.utils
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import settings
from app.email_service import is_simulated_email, normalize_thread_subject, parse_email_draft

logger = logging.getLogger("gmail_client")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def get_gmail_service():
    """
    Authenticates using credentials.json and caches tokens in token.json
    using scopes ['https://www.googleapis.com/auth/gmail.modify', 'https://www.googleapis.com/auth/gmail.send'].
    Handles token refresh automatically.
    """
    creds = None
    token_path = settings.google_token_json_path
    credentials_path = settings.google_credentials_json_path

    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            logger.error(f"[GMAIL CLIENT] Error loading token from {token_path}: {e}")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.error(f"[GMAIL CLIENT] Error refreshing token: {e}")
                creds = None

        if not creds:
            if os.path.exists(credentials_path):
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
            else:
                raise FileNotFoundError(
                    f"Gmail credentials not found at {credentials_path}. "
                    f"Please provide credentials.json or a valid token.json."
                )

        if creds:
            try:
                with open(token_path, "w", encoding="utf-8") as token_file:
                    token_file.write(creds.to_json())
            except Exception as e:
                logger.warning(f"[GMAIL CLIENT] Could not write cached token to {token_path}: {e}")

    return build("gmail", "v1", credentials=creds)


def setup_gmail_watch(topic_name: str) -> dict:
    """
    Registers a push notification watch on the user's inbox with Google Cloud Pub/Sub.
    Calls users().watch(userId='me', body={'topicName': topic_name, 'labelIds': ['INBOX']}).execute().
    """
    service = get_gmail_service()
    body = {
        "topicName": topic_name,
        "labelIds": ["INBOX"],
    }
    watch_response = service.users().watch(userId="me", body=body).execute()
    logger.info(f"[GMAIL WATCH] Watch registered for topic '{topic_name}': {watch_response}")
    return watch_response


def _extract_body_from_payload(payload: dict) -> str:
    """
    Extracts and decodes UTF-8 text content from a Gmail message payload.
    Supports single-part plain text, multipart payloads, and HTML fallbacks.
    """
    if not payload:
        return ""

    body_data = payload.get("body", {}).get("data")
    mime_type = payload.get("mimeType", "")

    def decode_data(b64_str: str) -> str:
        if not b64_str:
            return ""
        try:
            padded = b64_str + "=" * ((4 - len(b64_str) % 4) % 4)
            return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
        except Exception:
            return ""

    if body_data and mime_type.startswith("text/plain"):
        decoded = decode_data(body_data)
        if decoded:
            return decoded

    parts = payload.get("parts", [])
    text_plain_parts = []
    text_html_parts = []

    def walk_parts(subparts):
        for part in subparts:
            p_mime = part.get("mimeType", "")
            p_data = part.get("body", {}).get("data")
            if p_data:
                decoded = decode_data(p_data)
                if decoded:
                    if p_mime == "text/plain":
                        text_plain_parts.append(decoded)
                    elif p_mime == "text/html":
                        text_html_parts.append(decoded)
            if "parts" in part:
                walk_parts(part["parts"])

    if parts:
        walk_parts(parts)

    if text_plain_parts:
        return "\n".join(text_plain_parts)

    if text_html_parts:
        html_content = "\n".join(text_html_parts)
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")
            return soup.get_text()
        except Exception:
            return html_content

    if body_data:
        decoded = decode_data(body_data)
        if decoded:
            return decoded

    return payload.get("snippet", "")


def _parse_gmail_message(full_msg: dict) -> dict:
    """
    Extracts RFC 5322 headers and body from a Gmail API message representation.
    """
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


PROCESSED_GMAIL_MESSAGE_IDS: set[str] = set()


def fetch_latest_messages_by_history(start_history_id: str) -> list[dict]:
    """
    Calls users().history().list(userId='me', startHistoryId=start_history_id, historyTypes=['messageAdded']).
    If history returns empty or if startHistoryId fails/expires, falls back to fetching recent INBOX messages directly:
        service.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=3).execute()
    Extracts full MIME payload, decodes UTF-8 text, captures RFC 5322 headers, and checks sender.
    Prevents duplicate calls using PROCESSED_GMAIL_MESSAGE_IDS.
    """
    global PROCESSED_GMAIL_MESSAGE_IDS
    service = get_gmail_service()
    history_records = []
    seen_ids = set()
    messages_out = []

    desk_emails = {
        e.strip().lower()
        for e in [settings.email_user, settings.desk_email, "musabtahir2@gmail.com"]
        if e
    }

    if start_history_id:
        try:
            history_res = service.users().history().list(
                userId="me",
                startHistoryId=str(start_history_id),
                historyTypes=["messageAdded"],
            ).execute()
            history_records = history_res.get("history", [])
        except Exception as e:
            logger.warning(f"[GMAIL] Failed to list history for startHistoryId={start_history_id}: {e}. Falling back to direct inbox message listing.")

    if history_records:
        for record in history_records:
            added = record.get("messagesAdded", [])
            for item in added:
                msg_stub = item.get("message", {})
                msg_id = msg_stub.get("id")
                if not msg_id or msg_id in seen_ids or msg_id in PROCESSED_GMAIL_MESSAGE_IDS:
                    continue
                seen_ids.add(msg_id)

                try:
                    full_msg = service.users().messages().get(
                        userId="me",
                        id=msg_id,
                        format="full",
                    ).execute()
                    PROCESSED_GMAIL_MESSAGE_IDS.add(msg_id)
                    messages_out.append(_parse_gmail_message(full_msg))
                except Exception as e:
                    logger.error(f"[GMAIL] Failed to fetch message {msg_id}: {e}")
                    continue
        if messages_out:
            return messages_out

    # Direct fallback to fetch recent inbox messages when history is empty or expired:
    logger.info("[GMAIL] History query returned no records; executing direct fallback to fetch recent INBOX messages.")
    try:
        res = service.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=3).execute()
        msg_list = res.get("messages", [])
        for msg_stub in msg_list:
            msg_id = msg_stub.get("id")
            if not msg_id or msg_id in seen_ids or msg_id in PROCESSED_GMAIL_MESSAGE_IDS:
                continue
            seen_ids.add(msg_id)

            try:
                full_msg = service.users().messages().get(
                    userId="me",
                    id=msg_id,
                    format="full",
                ).execute()
                PROCESSED_GMAIL_MESSAGE_IDS.add(msg_id)
                parsed = _parse_gmail_message(full_msg)
                sender = parsed.get("From") or parsed.get("from") or parsed.get("sender") or ""
                sender_lower = sender.strip().lower()
                if any(desk_e in sender_lower for desk_e in desk_emails):
                    logger.info(f"[GMAIL] Skipping self-sent message from desk ({sender}) in fallback inbox list.")
                    continue
                messages_out.append(parsed)
            except Exception as e:
                logger.error(f"[GMAIL] Failed to fetch fallback message {msg_id}: {e}")
                continue
    except Exception as e:
        logger.error(f"[GMAIL] Direct fallback to fetch recent inbox messages failed: {e}")

    # Maintain cache bounds
    if len(PROCESSED_GMAIL_MESSAGE_IDS) > 2000:
        PROCESSED_GMAIL_MESSAGE_IDS = set(list(PROCESSED_GMAIL_MESSAGE_IDS)[-1000:])

    return messages_out


def send_gmail_reply(
    to_email: str,
    raw_draft: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    thread_subject: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> bool:
    """
    Constructs an email.message.EmailMessage, sets threading headers,
    URL-safe base64 encodes it, and sends it via users().messages().send().
    """
    parsed_sub, body = parse_email_draft(raw_draft)
    to_addr = (to_email or "").strip()
    if not to_addr:
        logger.warning("[GMAIL SEND] Cannot send reply: recipient address is empty.")
        return False

    if thread_subject:
        subject = normalize_thread_subject(parsed_sub, thread_subject)
    else:
        subject = parsed_sub

    msg = EmailMessage()
    msg["Subject"] = subject
    from_addr = settings.email_user or settings.desk_email or "desk@arbitrage.com"
    msg["From"] = from_addr
    msg["To"] = to_addr

    domain = from_addr.split("@")[1] if "@" in from_addr else "gmail.com"
    outbound_msg_id = email.utils.make_msgid(domain=domain)
    msg["Message-ID"] = outbound_msg_id

    if in_reply_to:
        clean_irt = in_reply_to.strip()
        if not clean_irt.startswith("<"):
            clean_irt = f"<{clean_irt}>"
        msg["In-Reply-To"] = clean_irt

    if references:
        clean_refs = references.strip()
        if in_reply_to and in_reply_to not in clean_refs:
            clean_refs = f"{clean_refs} {msg['In-Reply-To']}".strip()
        msg["References"] = clean_refs
    elif in_reply_to:
        msg["References"] = msg["In-Reply-To"]

    msg.set_content(body)

    encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    send_body: dict[str, Any] = {"raw": encoded_message}
    if thread_id:
        send_body["threadId"] = thread_id

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
