from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import StateGraph, END

from app.config import settings
from app.database import pool
from app.logger import get_logger
from app.models import DealState

from app.agents.buyer_agent import proactive_outreach_node, counter_buyer_node
from app.agents.supplier_agent import counter_supplier_node
from app.agents.risk_worker import (
    parse_incoming_email_node,
    fetch_market_data_node,
    evaluate_risk_node,
    route_after_evaluation,
)
from app.agents.finalization import approval_gate_node, confirm_deal_node, reject_deal_node

logger = get_logger("arbitrage_desk.workflow")


def route_entry_point(state: DealState) -> Literal["proactive_outreach", "parse_incoming_email", "__end__"]:
    latest_email = (state.get("latest_email") or "").strip()
    if latest_email:
        return "parse_incoming_email"

    if state.get("pipeline_step", 0) >= 1 or state.get("deal_status") in ["prospecting", "counter_sent", "approved", "closed", "rejected"]:
        camp = state.get("campaign")
        cid = getattr(camp, "campaign_id", None) or (camp.get("campaign_id") if isinstance(camp, dict) else "unknown")
        logger.info(
            f"[WORKFLOW:Router] Campaign '{cid}' is waiting for counterparty reply (status: '{state.get('deal_status')}'). "
            f"No inbound email present; halting at END without duplicate outreach."
        )
        return END

    return "proactive_outreach"


def _sync_workflow_exports(cp, graph):
    import sys
    wf = sys.modules.get("app.workflow")
    if wf:
        wf.checkpointer = cp
        wf.trade_graph = graph


# Our Pydantic models stored in DealState. LangGraph only deserializes allow-listed types from
# checkpoints (unregistered types will be blocked in a future version), so register them here.
CHECKPOINT_ALLOWED_TYPES = [
    ("app.models", "Campaign"),
    ("app.models", "ParsedEmail"),
]


def build_checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_ALLOWED_TYPES)


def _initialize_checkpointer():
    return InMemorySaver(serde=build_checkpoint_serde())


checkpointer = _initialize_checkpointer()


def get_checkpointer():
    import sys
    wf = sys.modules.get("app.workflow")
    if wf and "checkpointer" in wf.__dict__ and wf.__dict__["checkpointer"] is not None:
        return wf.__dict__["checkpointer"]
    return globals().get("checkpointer")


def get_trade_graph():
    import sys
    wf = sys.modules.get("app.workflow")
    if wf and "trade_graph" in wf.__dict__ and wf.__dict__["trade_graph"] is not None:
        return wf.__dict__["trade_graph"]
    return globals().get("trade_graph")


def setup_checkpointer():
    global checkpointer, trade_graph
    backend = getattr(settings, "checkpointer_backend", "memory").lower()
    if backend == "postgres":
        try:
            if hasattr(pool, "closed") and pool.closed:
                pool.open()
            saver = PostgresSaver(pool, serde=build_checkpoint_serde())
            saver.setup()
            checkpointer = saver
            trade_graph = build_trade_graph(checkpointer=checkpointer)
            _sync_workflow_exports(checkpointer, trade_graph)
            logger.info("[CHECKPOINTER] Postgres checkpointer tables initialized/verified.")
            return checkpointer
        except Exception as exc:
            allow_fallback = getattr(settings, "checkpointer_allow_volatile_fallback", False)
            if not allow_fallback:
                logger.error(f"[CHECKPOINTER] PostgreSQL checkpointer setup failed and volatile fallback is disabled: {exc}")
                raise RuntimeError(f"PostgreSQL checkpointer setup failed: {exc}") from exc
            logger.warning(f"[CHECKPOINTER] Postgres checkpointer setup failed, falling back to InMemorySaver: {exc}")
            checkpointer = InMemorySaver(serde=build_checkpoint_serde())
            trade_graph = build_trade_graph(checkpointer=checkpointer)
            _sync_workflow_exports(checkpointer, trade_graph)
            return checkpointer

    checkpointer = InMemorySaver(serde=build_checkpoint_serde())
    trade_graph = build_trade_graph(checkpointer=checkpointer)
    _sync_workflow_exports(checkpointer, trade_graph)
    return checkpointer


def build_trade_graph(checkpointer=None):
    if checkpointer is None:
        checkpointer = get_checkpointer()
    builder = StateGraph(DealState)

    builder.add_node("proactive_outreach", proactive_outreach_node)
    builder.add_node("parse_incoming_email", parse_incoming_email_node)
    builder.add_node("fetch_market_data", fetch_market_data_node)
    builder.add_node("evaluate_risk", evaluate_risk_node)
    builder.add_node("approval_gate", approval_gate_node)
    builder.add_node("confirm_deal", confirm_deal_node)
    builder.add_node("counter_buyer", counter_buyer_node)
    builder.add_node("counter_supplier", counter_supplier_node)
    builder.add_node("reject_deal", reject_deal_node)

    builder.set_conditional_entry_point(
        route_entry_point,
        {
            "proactive_outreach": "proactive_outreach",
            "parse_incoming_email": "parse_incoming_email",
            END: END,
        },
    )

    builder.add_edge("proactive_outreach", END)

    builder.add_edge("parse_incoming_email", "fetch_market_data")
    builder.add_edge("fetch_market_data", "evaluate_risk")

    builder.add_conditional_edges(
        "evaluate_risk",
        route_after_evaluation,
        {
            "approval_gate": "approval_gate",
            "confirm_deal": "confirm_deal",
            "counter_buyer": "counter_buyer",
            "counter_supplier": "counter_supplier",
            "reject_deal": "reject_deal",
        },
    )

    builder.add_edge("confirm_deal", END)
    builder.add_edge("counter_buyer", END)
    builder.add_edge("counter_supplier", END)
    builder.add_edge("reject_deal", END)

    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


build_arbitrage_graph = build_trade_graph
trade_graph = build_trade_graph(checkpointer=checkpointer)


def __getattr__(name: str):
    if name == "trade_graph":
        return get_trade_graph()
    if name == "checkpointer":
        return get_checkpointer()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
