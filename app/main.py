"""FastAPI app: lifespan (DB, checkpointer, one inbound-mail path), CORS, routers and the static UI."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from app.config import STATIC_DIR, settings
from app.db.database import init_db
from app.graph.workflow import setup_checkpointer
from app.logger import setup_logger
from app.routes.campaigns import router as campaigns_router
from app.routes.negotiation import router as negotiation_router
from app.routes.webhooks import router as webhooks_router
from app.services.mail.gmail_client import setup_gmail_watch, stop_gmail_watch
from app.services.mail.gmail_worker import email_polling_worker, seed_history_cursor

logger = setup_logger("arbitrage_desk")


async def _register_gmail_watch(topic: str) -> None:
    logger.info(f"[LIFESPAN] Registering Gmail watch for topic: {topic}")
    try:
        watch_result = await asyncio.to_thread(setup_gmail_watch, topic)
    except Exception as e:
        logger.error(f"[LIFESPAN] Failed to set up Gmail watch on boot: {e}")
        return
    if not watch_result:
        logger.error("[LIFESPAN ERROR] Failed to register Gmail watch.")
        return
    seed_history_cursor(watch_result.get("historyId"))
    logger.info(
        f"[LIFESPAN] Gmail watch active! Expiration: {watch_result.get('expiration')}, "
        f"initial historyId: {watch_result.get('historyId')}"
    )


async def _stop_leftover_gmail_watch() -> None:
    """In polling mode, stop any Gmail watch left from a push-mode run (watches last up to 7 days),
    so Pub/Sub stops pushing notifications. Best effort: never blocks startup."""
    if not settings.google_pubsub_topic:
        return
    try:
        await asyncio.to_thread(stop_gmail_watch)
        logger.info("[LIFESPAN] Polling mode: stopped any active Gmail push watch for this mailbox.")
    except Exception as e:
        logger.warning(
            f"[LIFESPAN] Polling mode: could not stop Gmail push watch ({e}). Webhook calls are ignored anyway."
        )


def _start_polling_worker() -> asyncio.Task | None:
    if settings.email_user and settings.email_pass:
        logger.info(
            f"[LIFESPAN] Email credentials configured ({settings.email_user}). "
            f"Starting background email polling worker..."
        )
        return asyncio.create_task(email_polling_worker())
    logger.warning(
        "[LIFESPAN] Email credentials not configured (EMAIL_USER/EMAIL_PASS). Live email polling worker disabled."
    )
    return None


async def _stop_polling_worker(worker_task: asyncio.Task | None) -> None:
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
    logger.info(f"[LIFESPAN] Email ingestion mode: {'push' if settings.use_push_webhooks else 'poll'} "
                f"(USE_PUSH_WEBHOOKS={settings.use_push_webhooks})")
    if settings.use_push_webhooks:
        if settings.google_pubsub_topic:
            await _register_gmail_watch(settings.google_pubsub_topic)
        else:
            logger.info("[LIFESPAN] Push webhooks enabled (USE_PUSH_WEBHOOKS=True). Polling worker disabled.")
    else:
        await _stop_leftover_gmail_watch()
        worker_task = _start_polling_worker()

    yield

    await _stop_polling_worker(worker_task)


app = FastAPI(title=f"{settings.desk_name} - Physical Commodity Arbitrage", version="2.0.0", lifespan=lifespan)


@app.exception_handler(OperationalError)
async def database_unavailable_handler(request: Request, exc: OperationalError) -> JSONResponse:
    """Postgres down or unreachable: answer 503 with a clear message instead of a bare 500 and a long traceback."""
    logger.error(f"[DATABASE] {request.method} {request.url.path} failed, database unavailable: {exc.orig or exc}")
    detail = "Database unavailable. Check DATABASE_URL and that Postgres is running."
    return JSONResponse(status_code=503, content={"detail": detail})

cors_allows_any_origin = "*" in settings.cors_origins
if cors_allows_any_origin:
    logger.warning(
        "[CORS] CORS_ORIGINS contains '*': any website can call this API. Credentials are disabled for "
        "wildcard CORS; list explicit origins instead."
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # With "*", Starlette echoes the caller's origin when credentials are allowed, i.e. credentialed
    # access for every site. Only allow credentials for explicit origin lists.
    allow_credentials=not cors_allows_any_origin,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (campaigns_router, webhooks_router, negotiation_router):
    app.include_router(router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
