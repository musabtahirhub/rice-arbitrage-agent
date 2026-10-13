import base64
import json
import logging
from fastapi import APIRouter, BackgroundTasks, Request
from app.services.gmail_worker import process_incoming_gmail_event

logger = logging.getLogger("arbitrage_desk")

router = APIRouter()

def _extract_pubsub_history_id(body: dict) -> str:
    message = body.get("message") if isinstance(body, dict) else {}
    if not isinstance(message, dict):
        return ""
    data_b64 = message.get("data", "")
    if not data_b64:
        return ""
    try:
        missing_padding = len(data_b64) % 4
        if missing_padding:
            data_b64 += "=" * (4 - missing_padding)
        decoded_bytes = base64.b64decode(data_b64)
        decoded_str = decoded_bytes.decode("utf-8")
        try:
            data_json = json.loads(decoded_str)
            return str(data_json.get("historyId", ""))
        except Exception:
            return decoded_str.strip()
    except Exception as e:
        logger.error(f"[GMAIL WEBHOOK] Error decoding Pub/Sub data: {e}")
        return ""

from app.schemas import WebhookResponse

@router.post("/api/webhooks/gmail")
async def gmail_webhook_endpoint(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"[GMAIL WEBHOOK] Invalid JSON received: {e}")
        return WebhookResponse(status="error", message="Invalid JSON").model_dump()

    history_id = _extract_pubsub_history_id(body)
    logger.info(f"[GMAIL WEBHOOK] Received Pub/Sub webhook notification. historyId: '{history_id}'")

    if history_id:
        background_tasks.add_task(process_incoming_gmail_event, history_id)

    return WebhookResponse(status="ok", historyId=history_id).model_dump(exclude_none=True)
