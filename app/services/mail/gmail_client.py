"""Gmail push-mode inbox: watch registration, history listing, and the Primary-inbox filter."""
from collections.abc import Iterable
from typing import Optional

from app.logger import get_logger
from app.services.gmail_auth import get_gmail_service
from app.services.gmail_processed_cache import is_gmail_message_processed
from app.utils.gmail_payload import parse_gmail_message

logger = get_logger("arbitrage_desk.gmail_client")

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
