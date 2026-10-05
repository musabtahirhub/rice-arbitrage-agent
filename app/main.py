import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.config import settings
from app.database import init_db
from app.gmail_client import setup_gmail_watch
from app.workflow import setup_checkpointer
from app.services import gmail_worker
from app.services.gmail_worker import email_polling_worker
from app.logger import setup_logger

from app.routes.campaigns import router as campaigns_router
from app.routes.webhooks import router as webhooks_router
from app.routes.negotiation import router as negotiation_router

logger = setup_logger("arbitrage_desk")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. DB & Checkpointer init
    init_db()
    setup_checkpointer()
    worker_task = None

    # 2. Setup Push Webhook Watch or Polling Worker
    if settings.use_push_webhooks:
        topic = settings.google_pubsub_topic
        if topic:
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
        else:
            logger.info("[LIFESPAN] Push webhooks enabled (USE_PUSH_WEBHOOKS=True). Polling worker disabled.")
    else:
        if settings.email_user and settings.email_pass:
            logger.info(f"[LIFESPAN] Email credentials configured ({settings.email_user}). Starting background email polling worker...")
            worker_task = asyncio.create_task(email_polling_worker())
        else:
            logger.warning("[LIFESPAN] Email credentials not configured (EMAIL_USER/EMAIL_PASS). Live email polling worker disabled.")

    yield

    if worker_task:
        logger.info("[LIFESPAN] Stopping background email polling worker...")
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title=f"{settings.desk_name} - Physical Commodity Arbitrage",
    description="Educational Mid-Level Autonomous Physical Commodity Arbitrage Agent",
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
