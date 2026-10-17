"""POST /api/webhooks/gmail: Pub/Sub push notifications for new Gmail history."""
import base64
import json

from fastapi import APIRouter, BackgroundTasks, Request

from app.api.schemas import WebhookResponse
from app.config import settings
from app.logger import get_logger
from app.services.inbound_push import process_incoming_gmail_event

logger = get_logger("arbitrage_desk")

router = APIRouter()


def _extract_pubsub_history_id(body: dict) -> str:
    message = body.get("message") if isinstance(body, dict) else {}
    data_b64 = message.get("data", "") if isinstance(message, dict) else ""
    if not data_b64:
        return ""
    try:
        decoded_str = base64.b64decode(data_b64 + "=" * (-len(data_b64) % 4)).decode("utf-8")
    except Exception as e:
        logger.error(f"[GMAIL WEBHOOK] Error decoding Pub/Sub data: {e}")
        return ""
    try:
        return str(json.loads(decoded_str).get("historyId", ""))
    except Exception:
        return decoded_str.strip()


@router.post("/api/webhooks/gmail")
async def gmail_webhook_endpoint(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"[GMAIL WEBHOOK] Invalid JSON received: {e}")
        return WebhookResponse(status="error", message="Invalid JSON").model_dump()

    history_id = _extract_pubsub_history_id(body)

    if not settings.use_push_webhooks:
        # IMAP polling owns inbound mail in this mode. A Gmail watch can outlive a mode switch by up to
        # 7 days; processing its notifications too would answer each email twice. Ack so Pub/Sub stops retrying.
        logger.debug(f"[GMAIL WEBHOOK] Ignoring notification historyId='{history_id}' (polling mode).")
        return WebhookResponse(status="ignored", historyId=history_id).model_dump(exclude_none=True)

    logger.info(f"[GMAIL WEBHOOK] Received Pub/Sub webhook notification. historyId: '{history_id}'")
    if history_id:
        background_tasks.add_task(process_incoming_gmail_event, history_id)
    return WebhookResponse(status="ok", historyId=history_id).model_dump(exclude_none=True)
