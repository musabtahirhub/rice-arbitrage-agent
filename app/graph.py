from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import StateGraph, END

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


checkpointer = InMemorySaver()


def setup_checkpointer():
    try:
        if hasattr(pool, "closed") and pool.closed:
            pool.open()
        if hasattr(checkpointer, "setup"):
            checkpointer.setup()
            logger.info("[CHECKPOINTER] Postgres checkpointer tables initialized/verified.")
    except Exception as exc:
        logger.warning(f"[CHECKPOINTER] Checkpointer setup notice or error: {exc}")


def build_trade_graph(checkpointer=checkpointer):
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
