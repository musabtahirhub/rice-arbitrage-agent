"""
Orchestrator Agent — central coordination for the multi-agent arbitrage system.

Manages state handoffs between the Buyer Agent and Supplier Agent, invokes
the deterministic Risk Worker (arbitrage engine) as a finalization barrier,
and routes to deal completion or renegotiation.

Graph topology
--------------
                    ┌────────────────┐
                    │ ingest_emails  │  (routes buyer/supplier emails)
                    └───────┬────────┘
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
      ┌────────────────┐       ┌──────────────────┐
      │  buyer_agent   │       │ supplier_agent    │
      │ (parse email)  │       │  (parse email)    │
      └───────┬────────┘       └────────┬──────────┘
              │                         │
              └────────────┬────────────┘
                           ▼
                 ┌───────────────────┐
                 │ fetch_market_data  │  ← MarketDataService
                 └────────┬──────────┘
                          ▼
                 ┌───────────────────┐
                 │  evaluate_risk    │  ← deterministic barrier
                 └────────┬──────────┘
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
         approved    counter_buyer  counter_supplier
         (lock deal) (buyer agent)  (supplier agent)
              │           │               │
              ▼           ▼               ▼
             END         END             END

Core invariants:
  - Neither Agent can finalize terms without Risk Worker approval.
  - The Buyer Agent must never confirm an order until the Supplier Agent
    has locked allocation (zero-risk short squeeze rule).
  - All spread comparisons and margin checks execute in deterministic Python.
"""

from __future__ import annotations

import re
from typing import Any

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
from app.arbitrage_engine import evaluate_deal
from app.market_data import MarketDataService
from app.schemas import CampaignConfig, ParsedTradeEmail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_broken_pct(commodity: str) -> float:
    """Extract broken percentage from a commodity description string."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*broken", commodity.lower())
    if match:
        return float(match.group(1))
    return 5.0  # default


def _get_port(state: dict[str, Any], terms_key: str, default: str) -> str:
    """Extract port from parsed terms in state, with a fallback default."""
    terms = state.get(terms_key)
    if terms is None:
        return default
    if isinstance(terms, dict):
        return terms.get("port") or default
    if hasattr(terms, "port"):
        return terms.port or default
    return default


# ---------------------------------------------------------------------------
# Node: ingest_emails — route raw emails to the correct agent
# ---------------------------------------------------------------------------

def ingest_emails(state: dict[str, Any]) -> dict[str, Any]:
    """
    Determine whether the raw email is from a buyer or supplier based on
    keywords, and set routing flags.

    This is a lightweight pre-processing step — the actual LLM extraction
    happens in the respective agent nodes.
    """
    raw = state.get("raw_email", "").lower()
    updates: dict[str, Any] = {}

    # Simple heuristic: buyer emails mention "buy", "purchase", "inquiry",
    # "requirement"; supplier emails mention "offer", "supply", "quote"
    buyer_signals = ["buy", "purchase", "inquiry", "requirement", "looking to"]
    supplier_signals = ["offer", "supply", "quote", "pleased to", "we are pleased"]

    buyer_score = sum(1 for s in buyer_signals if s in raw)
    supplier_score = sum(1 for s in supplier_signals if s in raw)

    if buyer_score >= supplier_score:
        updates["_email_route"] = "buyer"
    else:
        updates["_email_route"] = "supplier"

    return {**state, **updates}


# ---------------------------------------------------------------------------
# Node: fetch_market_data — deterministic, no LLM
# ---------------------------------------------------------------------------

def fetch_market_data(state: dict[str, Any]) -> dict[str, Any]:
    """
    Fetch live benchmark FOB prices and freight estimates via
    ``MarketDataService``.  Populates the state with market data
    that the Risk Worker uses to compute dynamic price bounds.
    """
    campaign_data = state.get("campaign", {})
    campaign = (
        campaign_data
        if isinstance(campaign_data, CampaignConfig)
        else CampaignConfig(**campaign_data)
    )

    market_svc = MarketDataService()

    # --- Benchmark FOB price ---
    broken_pct = _extract_broken_pct(campaign.commodity)
    benchmark_fob, benchmark_source = market_svc.get_benchmark_rate(
        commodity=campaign.benchmark_index_name,
        broken_pct=broken_pct,
    )

    # --- Freight estimate ---
    origin_port = _get_port(state, "supplier_terms", default="Mundra")
    dest_port = _get_port(state, "buyer_terms", default="Jebel Ali")
    freight_cost, freight_source = market_svc.estimate_freight(
        origin_port=origin_port,
        destination_port=dest_port,
    )

    return {
        **state,
        "benchmark_fob_usd": benchmark_fob,
        "benchmark_source": benchmark_source,
        "freight_cost_usd": freight_cost,
        "freight_source": freight_source,
        "deal_status": "evaluating",
    }


# ---------------------------------------------------------------------------
# Node: evaluate_risk — deterministic Risk Worker barrier
# ---------------------------------------------------------------------------

def evaluate_risk(state: dict[str, Any]) -> dict[str, Any]:
    """
    Run the deterministic arbitrage engine.  No LLM is involved.

    Uses live benchmark and freight data from state to compute dynamic
    price boundaries.  If either counterparty term is missing we cannot
    evaluate yet, so we skip and leave the state unchanged.

    This is the finalization barrier — neither agent can proceed to deal
    completion without passing this gate.
    """
    campaign_data = state.get("campaign")
    buyer_data = state.get("buyer_terms")
    supplier_data = state.get("supplier_terms")

    if not buyer_data or not supplier_data:
        # Cannot evaluate without both sides — stay in prospecting
        return {
            **state,
            "deal_status": "prospecting",
            "evaluation_reason": "Awaiting terms from both buyer and supplier.",
        }

    # Reconstruct Pydantic models if state contains raw dicts
    campaign = (
        campaign_data
        if isinstance(campaign_data, CampaignConfig)
        else CampaignConfig(**campaign_data)
    )
    buyer = (
        buyer_data
        if isinstance(buyer_data, ParsedTradeEmail)
        else ParsedTradeEmail(**buyer_data)
    )
    supplier = (
        supplier_data
        if isinstance(supplier_data, ParsedTradeEmail)
        else ParsedTradeEmail(**supplier_data)
    )

    # Retrieve market data from state
    benchmark_fob = state.get("benchmark_fob_usd", 0.0)
    freight_cost = state.get("freight_cost_usd", 0.0)

    result = evaluate_deal(campaign, buyer, supplier, benchmark_fob, freight_cost)

    round_num = state.get("negotiation_round", 0) + 1

    if result["viable"]:
        deal_status = "approved"
        buyer_status = "terms_accepted"
        supplier_status = "allocation_locked"
    else:
        # Decide which side to counter based on the rejection reason
        reason_lower = result["reason"].lower()
        if "supplier" in reason_lower or "ceiling" in reason_lower or "premium" in reason_lower:
            deal_status = "negotiating_supplier"
            buyer_status = state.get("buyer_thread_status", "inquiry_parsed")
            supplier_status = "counter_sent"
        elif "buyer" in reason_lower or "floor" in reason_lower:
            deal_status = "negotiating_buyer"
            buyer_status = "counter_sent"
            supplier_status = state.get("supplier_thread_status", "quote_parsed")
        else:
            deal_status = "rejected"
            buyer_status = state.get("buyer_thread_status", "inquiry_parsed")
            supplier_status = state.get("supplier_thread_status", "quote_parsed")

    return {
        **state,
        "net_margin_pct": result["margin_pct"],
        "is_deal_viable": result["viable"],
        "evaluation_reason": result["reason"],
        "deal_status": deal_status,
        "buyer_thread_status": buyer_status,
        "supplier_thread_status": supplier_status,
        "negotiation_round": round_num,
    }


# ---------------------------------------------------------------------------
# Conditional routing (pure Python — no LLM)
# ---------------------------------------------------------------------------

def route_after_evaluation(state: dict[str, Any]) -> str:
    """
    Deterministic router executed after ``evaluate_risk``.
    Returns the name of the next node (or END).
    """
    deal_status = state.get("deal_status", "rejected")

    if deal_status == "approved":
        return "finalize_deal"
    if deal_status == "negotiating_buyer":
        return "counter_buyer"
    if deal_status == "negotiating_supplier":
        return "counter_supplier"
    # "rejected", "prospecting", or any unknown → terminate
    return END


def route_email(state: dict[str, Any]) -> str:
    """Route inbound email to the correct agent based on heuristic classification."""
    route = state.get("_email_route", "buyer")
    if route == "supplier":
        return "parse_supplier"
    return "parse_buyer"


# ---------------------------------------------------------------------------
# Node: finalize_deal — issues SCO + locks allocation
# ---------------------------------------------------------------------------

def finalize_deal(state: dict[str, Any]) -> dict[str, Any]:
    """
    Finalize an approved deal:
    - Supplier Agent confirms allocation (must happen FIRST per zero-risk rule).
    - Buyer Agent issues SCO (only after supplier allocation is locked).

    In this synchronous simulation, both happen in sequence within one node.
    The key invariant is that supplier_thread_status = 'allocation_locked'
    before buyer_thread_status = 'terms_accepted'.
    """
    return {
        **state,
        "supplier_thread_status": "allocation_locked",
        "buyer_thread_status": "terms_accepted",
        "deal_status": "closed",
    }


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_orchestrator() -> StateGraph:
    """
    Construct and compile the multi-agent LangGraph state graph.

    Topology:
        ingest_emails → (buyer_parse | supplier_parse) → fetch_market_data
        → evaluate_risk → (conditional) → finalize | counter_buyer | counter_supplier | END
    """
    graph = StateGraph(dict)

    # Register nodes
    graph.add_node("ingest_emails", ingest_emails)
    graph.add_node("parse_buyer", parse_buyer_email)
    graph.add_node("parse_supplier", parse_supplier_email)
    graph.add_node("fetch_market_data", fetch_market_data)
    graph.add_node("evaluate_risk", evaluate_risk)
    graph.add_node("finalize_deal", finalize_deal)
    graph.add_node("counter_buyer", draft_buyer_counter)
    graph.add_node("counter_supplier", draft_supplier_counter)

    # Entry point
    graph.set_entry_point("ingest_emails")

    # Route email to correct agent
    graph.add_conditional_edges(
        "ingest_emails",
        route_email,
        {
            "parse_buyer": "parse_buyer",
            "parse_supplier": "parse_supplier",
        },
    )

    # Both parse nodes flow to market data fetch
    graph.add_edge("parse_buyer", "fetch_market_data")
    graph.add_edge("parse_supplier", "fetch_market_data")

    # Market data → risk evaluation
    graph.add_edge("fetch_market_data", "evaluate_risk")

    # Conditional routing after risk evaluation
    graph.add_conditional_edges(
        "evaluate_risk",
        route_after_evaluation,
        {
            "finalize_deal": "finalize_deal",
            "counter_buyer": "counter_buyer",
            "counter_supplier": "counter_supplier",
            END: END,
        },
    )

    # Terminal nodes
    graph.add_edge("finalize_deal", END)
    graph.add_edge("counter_buyer", END)
    graph.add_edge("counter_supplier", END)

    return graph.compile()
