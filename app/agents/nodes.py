"""
Graph nodes and routing logic for the LangGraph orchestrator.
"""

from __future__ import annotations

import re
from typing import Any
from langgraph.graph import END

from app.engine import evaluate_deal
from app.services.market import MarketDataService
from app.services.notification import dispatch_operator_alert


def _extract_broken_pct(commodity: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*broken", commodity.lower())
    return float(match.group(1)) if match else 5.0


def _get_port(state: dict[str, Any], terms_key: str, default: str) -> str:
    terms = state.get(terms_key)
    if terms is None:
        return default
    if isinstance(terms, dict):
        return terms.get("port") or default
    if hasattr(terms, "port"):
        return terms.port or default
    return default


def ingest_emails(state: dict[str, Any]) -> dict[str, Any]:
    raw = state.get("raw_email", "").lower()
    is_buyer = any(w in raw for w in ["cif", "looking to purchase", "inquiry", "procurement"])
    is_supplier = any(w in raw for w in ["fob", "pleased to offer", "quotation", "export"])
    return {
        **state,
        "_route_buyer": is_buyer,
        "_route_supplier": is_supplier,
    }


def fetch_market_data(state: dict[str, Any]) -> dict[str, Any]:
    campaign = state.get("campaign")
    index_name = getattr(campaign, "benchmark_index_name", "basmati_1121") if campaign else "basmati_1121"
    commodity = getattr(campaign, "commodity", "") if campaign else ""

    broken_pct = _extract_broken_pct(commodity)
    origin_port = _get_port(state, "supplier_terms", "Mundra")
    destination_port = _get_port(state, "buyer_terms", "Jebel Ali")

    svc = MarketDataService()
    benchmark_fob, bench_source = svc.get_benchmark_rate(index_name, broken_pct)
    freight_cost, freight_source = svc.estimate_freight(origin_port, destination_port)

    return {
        **state,
        "benchmark_fob_usd": benchmark_fob,
        "benchmark_source": bench_source,
        "freight_cost_usd": freight_cost,
        "freight_source": freight_source,
        "deal_status": "evaluating",
    }


def evaluate_risk(state: dict[str, Any]) -> dict[str, Any]:
    campaign = state["campaign"]
    buyer = state.get("buyer_terms")
    supplier = state.get("supplier_terms")
    benchmark_fob = state["benchmark_fob_usd"]
    freight_cost = state["freight_cost_usd"]

    result = evaluate_deal(
        campaign=campaign,
        buyer=buyer,
        supplier=supplier,
        benchmark_fob=benchmark_fob,
        freight_cost=freight_cost,
    )

    round_num = state.get("negotiation_round", 0) + 1

    if result["viable"]:
        deal_status = "approved"
        buyer_status = "terms_accepted"
        supplier_status = "allocation_locked"
        buyer_dict = buyer.model_dump() if hasattr(buyer, "model_dump") else (buyer or {})
        supplier_dict = supplier.model_dump() if hasattr(supplier, "model_dump") else (supplier or {})
        cid = getattr(campaign, "campaign_id", "N/A")
        dispatch_operator_alert(cid, result["margin_pct"], buyer_dict, supplier_dict, result["reason"])
    else:
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
        "is_deal_viable": result["viable"],
        "net_margin_pct": result["margin_pct"],
        "evaluation_reason": result["reason"],
        "deal_status": deal_status,
        "buyer_thread_status": buyer_status,
        "supplier_thread_status": supplier_status,
        "negotiation_round": round_num,
    }



def finalize_deal(state: dict[str, Any]) -> dict[str, Any]:
    return {
        **state,
        "supplier_thread_status": "allocation_locked",
        "buyer_thread_status": "terms_accepted",
        "deal_status": "closed",
    }


def route_after_risk(state: dict[str, Any]) -> str:
    status = state.get("deal_status", "")
    if status == "approved":
        return "finalize_deal"
    elif status == "negotiating_buyer":
        return "draft_buyer_counter"
    elif status == "negotiating_supplier":
        return "draft_supplier_counter"
    return END
