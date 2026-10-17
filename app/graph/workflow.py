"""Runtime handle on the compiled trade graph and its checkpointer, plus thread-state helpers.

Always fetch the graph through get_trade_graph() at call time: setup_checkpointer() (app startup) and
tests replace ``trade_graph`` / ``checkpointer`` on this module.
"""
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver

from app.config import settings
from app.db.database import pool
from app.graph.graph import build_checkpoint_serde, build_trade_graph
from app.logger import get_logger

logger = get_logger("arbitrage_desk.workflow")


checkpointer = InMemorySaver(serde=build_checkpoint_serde())


trade_graph = build_trade_graph(checkpointer=checkpointer)


def get_trade_graph():
    return trade_graph


def _install_checkpointer(saver):
    global checkpointer, trade_graph
    checkpointer = saver
    trade_graph = build_trade_graph(checkpointer=saver)
    return saver


def _open_postgres_saver() -> PostgresSaver:
    if pool.closed:
        pool.open()
    saver = PostgresSaver(pool, serde=build_checkpoint_serde())
    saver.setup()
    return saver


def setup_checkpointer():
    """Install the configured backend (CHECKPOINTER_BACKEND) and rebuild the graph on it."""
    if settings.checkpointer_backend.lower() != "postgres":
        return _install_checkpointer(InMemorySaver(serde=build_checkpoint_serde()))
    try:
        saver = _install_checkpointer(_open_postgres_saver())
        logger.info("[CHECKPOINTER] Postgres checkpointer tables initialized/verified.")
        return saver
    except Exception as exc:
        if not settings.checkpointer_allow_volatile_fallback:
            logger.error(
                f"[CHECKPOINTER] PostgreSQL checkpointer setup failed and volatile fallback is disabled: {exc}"
            )
            raise RuntimeError(f"PostgreSQL checkpointer setup failed: {exc}") from exc
        logger.warning(f"[CHECKPOINTER] Postgres checkpointer setup failed, falling back to InMemorySaver: {exc}")
        return _install_checkpointer(InMemorySaver(serde=build_checkpoint_serde()))


def get_state_values(config: dict) -> dict:
    """A mutable copy of the thread's checkpointed state, or {} for an unknown thread."""
    snapshot = get_trade_graph().get_state(config)
    return dict(snapshot.values) if snapshot and snapshot.values else {}


def is_awaiting_approval(config: dict) -> bool:
    """True when the thread is paused at the human-approval interrupt."""
    snapshot = get_trade_graph().get_state(config)
    return bool(snapshot and snapshot.next and "approval_gate" in snapshot.next)


def mark_pending_approval(config: dict, state: dict) -> bool:
    """Record 'pending_approval' on the thread and in ``state`` when the turn stopped at the gate."""
    if not is_awaiting_approval(config):
        return False
    get_trade_graph().update_state(config, {"deal_status": "pending_approval"})
    state["deal_status"] = "pending_approval"
    return True
