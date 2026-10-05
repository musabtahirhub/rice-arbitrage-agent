import base64
import json
import logging
from fastapi import APIRouter, BackgroundTasks, Request
from app.services.gmail_worker import process_incoming_gmail_event

logger = logging.getLogger("arbitrage_desk")

router = APIRouter()

@router.post("/api/webhooks/gmail")
async def gmail_webhook_endpoint(request: Request, background_tasks: BackgroundTasks):
    """
    Real-time Google Cloud Pub/Sub push notification endpoint for Gmail watch.
    Reads JSON payload: {"message": {"data": "<base64_encoded>"}}, acknowledges Pub/Sub with 200 immediately,
    and offloads processing to process_incoming_gmail_event(history_id).
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"[GMAIL WEBHOOK] Invalid JSON received: {e}")
        return {"status": "error", "message": "Invalid JSON"}

    message = body.get("message") if isinstance(body, dict) else {}
    if not isinstance(message, dict):
        message = {}

    data_b64 = message.get("data", "")
    history_id = ""

    if data_b64:
        try:
            missing_padding = len(data_b64) % 4
            if missing_padding:
                data_b64 += "=" * (4 - missing_padding)
            decoded_bytes = base64.b64decode(data_b64)
            decoded_str = decoded_bytes.decode("utf-8")
            try:
                data_json = json.loads(decoded_str)
                history_id = str(data_json.get("historyId", ""))
            except Exception:
                history_id = decoded_str.strip()
        except Exception as e:
            logger.error(f"[GMAIL WEBHOOK] Error decoding Pub/Sub data: {e}")

    logger.info(f"[GMAIL WEBHOOK] Received Pub/Sub webhook notification. historyId: '{history_id}'")

    if history_id:
        background_tasks.add_task(process_incoming_gmail_event, history_id)

    return {"status": "ok", "historyId": history_id}
