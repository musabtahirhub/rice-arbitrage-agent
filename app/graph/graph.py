"""The negotiation StateGraph: a deterministic workflow. Routing is plain Python; the LLM never picks steps."""
from typing import Literal

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, StateGraph

from app.graph.agents.buyer_agent import counter_buyer_node, proactive_outreach_node
from app.graph.agents.finalization import (
    approval_gate_node,
    confirm_deal_node,
    reject_deal_node,
)
from app.graph.agents.inquiry_agent import handle_general_inquiry_node
from app.graph.agents.risk_worker import (
    evaluate_risk_node,
    fetch_market_data_node,
    parse_incoming_email_node,
    route_after_evaluation,
    route_after_parse,
)
from app.graph.agents.supplier_agent import counter_supplier_node
from app.logger import get_logger
from app.models import DealState, campaign_id_of, is_outreach_started

logger = get_logger("arbitrage_desk.workflow")


# Our Pydantic models stored in DealState. LangGraph only deserializes allow-listed types from
# checkpoints (unregistered types will be blocked in a future version), so register them here.
CHECKPOINT_ALLOWED_TYPES = [
    ("app.models", "Campaign"),
    ("app.models", "ParsedEmail"),
]


def build_checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_ALLOWED_TYPES)


def route_entry_point(state: DealState) -> Literal["proactive_outreach", "parse_incoming_email", "__end__"]:
    if (state.get("latest_email") or "").strip():
        return "parse_incoming_email"
    if is_outreach_started(state):
        logger.info(
            f"[WORKFLOW:Router] Campaign '{campaign_id_of(state)}' is waiting for counterparty reply "
            f"(status: '{state.get('deal_status')}'). No inbound email present; "
            f"halting at END without duplicate outreach."
        )
        return END
    return "proactive_outreach"


def build_trade_graph(checkpointer=None):
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
    builder.add_node("handle_general_inquiry", handle_general_inquiry_node)

    builder.set_conditional_entry_point(
        route_entry_point,
        {"proactive_outreach": "proactive_outreach", "parse_incoming_email": "parse_incoming_email", END: END},
    )
    builder.add_edge("proactive_outreach", END)
    # A decline closes the deal and a price-less question is answered from the deal facts; neither is priced.
    builder.add_conditional_edges(
        "parse_incoming_email",
        route_after_parse,
        {node: node for node in ("fetch_market_data", "reject_deal", "handle_general_inquiry")},
    )
    builder.add_edge("fetch_market_data", "evaluate_risk")
    builder.add_conditional_edges(
        "evaluate_risk",
        route_after_evaluation,
        {node: node for node in ("approval_gate", "confirm_deal", "counter_buyer", "counter_supplier", "reject_deal")},
    )
    for terminal in ("confirm_deal", "counter_buyer", "counter_supplier", "reject_deal", "handle_general_inquiry"):
        builder.add_edge(terminal, END)

    return builder.compile(checkpointer=checkpointer) if checkpointer is not None else builder.compile()
