
from typing import Any, Optional

from app.config import settings
from app.logger import get_logger

try:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler
except ImportError:  # tracing is optional; the app runs without the package
    Langfuse = None
    CallbackHandler = None

logger = get_logger("arbitrage_desk.telemetry")

_langfuse_client: Optional["Langfuse"] = None


def _langfuse_configured() -> bool:
    return Langfuse is not None and bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def get_langfuse_client() -> Optional["Langfuse"]:
   
    global _langfuse_client
    if _langfuse_client is None and _langfuse_configured():
        _langfuse_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_host,
        )
    return _langfuse_client


def get_langfuse_handler() -> Optional["CallbackHandler"]:

    if CallbackHandler is None or get_langfuse_client() is None:
        return None
    return CallbackHandler(public_key=settings.langfuse_public_key)


def flush_telemetry() -> None:
    
    client = get_langfuse_client()
    if client:
        try:
            client.flush()
        except Exception as exc:
            logger.warning(f"Failed to flush telemetry: {exc}")


def build_trace_config(
    campaign_id: str,
    trace_name: str,
    langfuse_handler: Optional[Any] = None,
    metadata: Optional[dict[str, Any]] = None,
    tags: Optional[list[str]] = None,
) -> dict[str, Any]:
  
    return {
        "configurable": {"thread_id": campaign_id},
        "callbacks": [langfuse_handler] if langfuse_handler else [],
        "metadata": {
            "langfuse_session_id": campaign_id,
            "langfuse_trace_name": trace_name,
            "campaign_id": campaign_id,
            **(metadata or {}),
        },
        "tags": ["commodity-arbitrage", *(tags or [])],
    }
