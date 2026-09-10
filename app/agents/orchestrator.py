"""
Orchestrator Agent — central coordination graph wiring for multi-agent arbitrage.
"""

from __future__ import annotations

from langgraph.graph import StateGraph, END

from app.agents.buyer_agent import (
    parse_buyer_email,
    draft_buyer_counter,
    draft_buyer_sco,
)
from app.agents.supplier_agent import (
    parse_supplier_email,
    draft_supplier_counter,
    draft_supplier_confirm,
)
from app.agents.nodes import (
    ingest_emails,
    fetch_market_data,
    evaluate_risk,
    finalize_deal,
    route_after_risk,
)


def build_orchestrator() -> StateGraph:
    """Construct and compile the multi-agent state graph."""
    graph = StateGraph(dict)

    # Register nodes
    graph.add_node("ingest_emails", ingest_emails)
    graph.add_node("buyer_agent", parse_buyer_email)
    graph.add_node("supplier_agent", parse_supplier_email)
    graph.add_node("fetch_market_data", fetch_market_data)
    graph.add_node("evaluate_risk", evaluate_risk)
    graph.add_node("finalize_deal", finalize_deal)
    graph.add_node("draft_buyer_counter", draft_buyer_counter)
    graph.add_node("draft_supplier_counter", draft_supplier_counter)

    # Edge layout
    graph.set_entry_point("ingest_emails")
    graph.add_edge("ingest_emails", "buyer_agent")
    graph.add_edge("buyer_agent", "supplier_agent")
    graph.add_edge("supplier_agent", "fetch_market_data")
    graph.add_edge("fetch_market_data", "evaluate_risk")

    # Conditional branching
    graph.add_conditional_edges(
        "evaluate_risk",
        route_after_risk,
        {
            "finalize_deal": "finalize_deal",
            "draft_buyer_counter": "draft_buyer_counter",
            "draft_supplier_counter": "draft_supplier_counter",
            END: END,
        },
    )

    graph.add_edge("finalize_deal", END)
    graph.add_edge("draft_buyer_counter", END)
    graph.add_edge("draft_supplier_counter", END)

    return graph


__all__ = [
    "build_orchestrator",
    "evaluate_risk",
    "fetch_market_data",
    "finalize_deal",
    "ingest_emails",
    "route_after_risk",
]
