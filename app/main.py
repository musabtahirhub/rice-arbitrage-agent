import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.config import settings
from app.database import init_db
from app.gmail_client import fetch_latest_messages_by_history, send_gmail_reply, setup_gmail_watch, stop_gmail_watch
from app.workflow import setup_checkpointer, get_trade_graph, trade_graph
from app.telemetry import get_langfuse_handler
from app.services import gmail_worker
from app.services.gmail_worker import email_polling_worker
from app.services.email_matcher import match_email_to_campaign as _match_email_to_campaign
from app.logger import setup_logger

from app.routes.campaigns import router as campaigns_router, create_campaign, get_campaign_status, list_directory
from app.routes.webhooks import router as webhooks_router, gmail_webhook_endpoint
from app.routes.negotiation import router as negotiation_router, approve_campaign_deal, negotiate_turn

logger = setup_logger("arbitrage_desk")

async def _initialize_gmail_watch(topic: str):
    logger.info(f"[LIFESPAN] Registering Gmail watch for topic: {topic}")
    try:
        watch_result = await asyncio.to_thread(setup_gmail_watch, topic)
        if watch_result:
            if "historyId" in watch_result:
                gmail_worker.LAST_SEEN_HISTORY_ID = str(watch_result["historyId"])
            logger.info(
                f"[LIFESPAN] Gmail watch active! Expiration: {watch_result.get('expiration')}, "
                f"initial historyId: {gmail_worker.LAST_SEEN_HISTORY_ID}"
            )
        else:
            logger.error("[LIFESPAN ERROR] Failed to register Gmail watch.")
    except Exception as e:
        logger.error(f"[LIFESPAN] Failed to set up Gmail watch on boot: {e}")

async def _stop_leftover_gmail_watch():
    """In polling mode, stop any Gmail watch left from a push-mode run (watches last up to 7 days),
    so Pub/Sub stops pushing notifications. Best effort: never blocks startup."""
    if not settings.google_pubsub_topic:
        return
    try:
        await asyncio.to_thread(stop_gmail_watch)
        logger.info("[LIFESPAN] Polling mode: stopped any active Gmail push watch for this mailbox.")
    except Exception as e:
        logger.warning(f"[LIFESPAN] Polling mode: could not stop Gmail push watch ({e}). Webhook calls are ignored anyway.")


def _start_email_worker_if_configured():
    if settings.email_user and settings.email_pass:
        logger.info(f"[LIFESPAN] Email credentials configured ({settings.email_user}). Starting background email polling worker...")
        return asyncio.create_task(email_polling_worker())
    logger.warning("[LIFESPAN] Email credentials not configured (EMAIL_USER/EMAIL_PASS). Live email polling worker disabled.")
    return None

async def _shutdown_worker_task(worker_task):
    if not worker_task:
        return
    logger.info("[LIFESPAN] Stopping background email polling worker...")
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    setup_checkpointer()
    worker_task = None

    # Exactly one ingestion path per deployment: push (Pub/Sub webhook) or poll (IMAP).
    mode = "push" if settings.use_push_webhooks else "poll"
    logger.info(f"[LIFESPAN] Email ingestion mode: {mode} (USE_PUSH_WEBHOOKS={settings.use_push_webhooks})")
    if settings.use_push_webhooks:
        topic = settings.google_pubsub_topic
        if topic:
            await _initialize_gmail_watch(topic)
        else:
            logger.info("[LIFESPAN] Push webhooks enabled (USE_PUSH_WEBHOOKS=True). Polling worker disabled.")
    else:
        await _stop_leftover_gmail_watch()
        worker_task = _start_email_worker_if_configured()

    yield

    await _shutdown_worker_task(worker_task)


app = FastAPI(
    title=f"{settings.desk_name} - Physical Commodity Arbitrage",
    version="2.0.0",
    lifespan=lifespan,
)

cors_list = settings.cors_origins if isinstance(settings.cors_origins, list) else [s.strip() for s in settings.cors_origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(campaigns_router)
app.include_router(webhooks_router)
app.include_router(negotiation_router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if not STATIC_DIR.exists():
    STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
