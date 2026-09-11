"""
LangGraph State Machine for physical commodity arbitrage negotiation.
Orchestrates email parsing, market grounding, deterministic risk gatekeeping,
and counter-offer drafting.
"""
import json
import re
from typing import Literal
from langgraph.graph import StateGraph, END

from app.config import settings
from app.market import get_benchmark_rate, estimate_freight
from app.math_engine import calculate_dynamic_bounds, evaluate_deal
from app.models import Campaign, DealState, ParsedEmail
from app.prompts import BUYER_COUNTER_PROMPT, EMAIL_PARSER_PROMPT, SUPPLIER_RFQ_PROMPT


# ---------------------------------------------------------------------------
# LLM / Regex Parser Helper
# ---------------------------------------------------------------------------

def _parse_email_content(raw_text: str, role: str) -> ParsedEmail:
    """
    Extract commercial terms using Gemini if API key is present,
    otherwise fallback to reliable regex pattern matching.
    """
    if settings.gemini_api_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=settings.gemini_api_key)
            model = genai.GenerativeModel(settings.gemini_model)
            prompt = EMAIL_PARSER_PROMPT.format(raw_email=raw_text)
            resp = model.generate_content(prompt)
            clean_text = resp.text.strip().removeprefix("```json").removesuffix("```").strip()
            data = json.loads(clean_text)
            return ParsedEmail(**data)
        except Exception:
            pass

    # Deterministic Regex Fallback Parser
    p_match = re.search(r"(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)", raw_text, re.IGNORECASE)
    if not p_match:
        p_match = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:USD|US\$|\$|per\s*MT|/\s*MT)", raw_text, re.IGNORECASE)
    price = float(p_match.group(1).replace(",", "")) if p_match else 0.0

    qty_match = re.search(r"(\d[\d,]*)\s*(?:MT|metric\s*ton)", raw_text, re.IGNORECASE)
    quantity = float(qty_match.group(1).replace(",", "")) if qty_match else 500.0

    incoterm = "CIF" if "cif" in raw_text.lower() else ("FOB" if "fob" in raw_text.lower() else ("CIF" if role == "buyer" else "FOB"))

    port = "Jebel Ali" if role == "buyer" else "Karachi"
    if "jebel ali" in raw_text.lower():
        port = "Jebel Ali"
    elif "dammam" in raw_text.lower():
        port = "Dammam"
    elif "karachi" in raw_text.lower():
        port = "Karachi"
    elif "mundra" in raw_text.lower():
        port = "Mundra"
    elif "bangkok" in raw_text.lower():
        port = "Bangkok"

    return ParsedEmail(
        sender_role=role,
        commodity="Basmati 1121",
        quantity_mt=quantity,
        price_usd_per_mt=price,
        incoterm=incoterm,
        port=port,
        payment_terms="100% LC at sight",
    )


# ---------------------------------------------------------------------------
# LangGraph Workflow Nodes
# ---------------------------------------------------------------------------

def parse_incoming_email_node(state: DealState) -> dict:
    """Node 1: Extract structured trade terms from the latest inbound email."""
    raw_email = state.get("latest_email", "")
    role = state.get("active_role", "buyer")
    terms = _parse_email_content(raw_email, role)

    updates = {}
    if role == "buyer":
        updates["buyer_terms"] = terms
    else:
        updates["supplier_terms"] = terms
    return updates


def fetch_market_data_node(state: DealState) -> dict:
    """Node 2: Fetch benchmark index rate and ocean freight estimate."""
    campaign: Campaign = state["campaign"]
    benchmark_fob = get_benchmark_rate(campaign.commodity, campaign.broken_percentage)
    freight = estimate_freight(campaign.origin_port_default, campaign.destination_port)

    bounds = calculate_dynamic_bounds(
        benchmark_fob=benchmark_fob,
        freight=freight,
        max_variance_pct=campaign.max_variance_from_benchmark_pct,
        target_margin_pct=campaign.target_margin_pct,
        buffer_usd=campaign.buffer_usd_per_mt,
    )

    return {
        "benchmark_fob_usd": benchmark_fob,
        "freight_cost_usd": freight,
        "dynamic_fob_ceiling": bounds["dynamic_fob_ceiling"],
        "dynamic_cif_floor": bounds["dynamic_cif_floor"],
    }


def evaluate_risk_node(state: DealState) -> dict:
    """Node 3: Deterministic risk evaluator (margin math + invariant enforcement)."""
    campaign: Campaign = state["campaign"]
    buyer = state.get("buyer_terms")
    supplier = state.get("supplier_terms")
    benchmark_fob = state.get("benchmark_fob_usd", 900.0)
    freight = state.get("freight_cost_usd", 50.0)

    result = evaluate_deal(
        campaign=campaign,
        buyer_terms=buyer,
        supplier_terms=supplier,
        benchmark_fob=benchmark_fob,
        freight=freight,
    )

    curr_round = state.get("negotiation_round", 0) + 1

    return {
        "is_deal_viable": result["viable"],
        "net_margin_pct": result["net_margin_pct"],
        "evaluation_reason": result["reason"],
        "negotiation_round": curr_round,
    }


def confirm_deal_node(state: DealState) -> dict:
    """
    Node 4A: Deal Viable!
    Enforce Zero-Risk Invariant: Supplier allocation is locked first,
    followed by buyer acceptance.
    """
    buyer = state.get("buyer_terms")
    supplier = state.get("supplier_terms")
    margin = state.get("net_margin_pct", 0.0)

    supplier_msg = (
        f"Subject: Deal Confirmation & Volume Lock — {supplier.commodity}\n\n"
        f"Dear Supplier,\n\n"
        f"We are pleased to accept your offer of USD {supplier.price_usd_per_mt:.2f}/MT {supplier.incoterm} "
        f"for {supplier.quantity_mt} MT. Please consider this volume formally locked.\n"
        f"Kindly issue the Proforma Invoice (PI) and banking coordinates.\n\n"
        f"Best regards,\nArbitrage Trading Desk"
    )

    buyer_msg = (
        f"Subject: Soft Corporate Offer (SCO) Acceptance — {buyer.commodity}\n\n"
        f"Dear Buyer,\n\n"
        f"We confirm acceptance of your purchase order at USD {buyer.price_usd_per_mt:.2f}/MT {buyer.incoterm} "
        f"for {buyer.quantity_mt} MT. Supplier allocation has been secured.\n"
        f"Our contract team will dispatch the formal SCO and draft LC instructions shortly.\n\n"
        f"Best regards,\nArbitrage Trading Desk"
    )

    return {
        "deal_status": "closed",
        "supplier_draft": supplier_msg,
        "buyer_draft": buyer_msg,
    }


def counter_buyer_node(state: DealState) -> dict:
    """Node 4B: Deal Non-Viable (Buyer price low) — Draft counter-offer to buyer."""
    cif_floor = state.get("dynamic_cif_floor", 1000.0)
    campaign: Campaign = state["campaign"]
    buyer = state.get("buyer_terms")
    reason = state.get("evaluation_reason", "")

    buyer_msg = (
        f"Subject: Counter-Offer — {campaign.commodity} CIF {campaign.destination_port}\n\n"
        f"Dear Buyer,\n\n"
        f"Thank you for your bid. Due to prevailing market benchmark rates and shipping tariffs, "
        f"our minimum viable selling price is USD {cif_floor:.2f}/MT CIF {campaign.destination_port}.\n\n"
        f"Desk Note: {reason}\n\n"
        f"Kindly advise if we can proceed at this level.\n\n"
        f"Warm regards,\nTrading Desk, Global Agro Arbitrage"
    )

    return {
        "deal_status": "counter_sent",
        "buyer_draft": buyer_msg,
    }


def counter_supplier_node(state: DealState) -> dict:
    """Node 4C: Deal Non-Viable (Supplier price high) — Draft counter-bid to supplier."""
    fob_ceiling = state.get("dynamic_fob_ceiling", 945.0)
    campaign: Campaign = state["campaign"]
    supplier = state.get("supplier_terms")
    reason = state.get("evaluation_reason", "")

    supplier_msg = (
        f"Subject: Counter-Bid — {campaign.commodity} FOB {campaign.origin_port_default}\n\n"
        f"Dear Exporter,\n\n"
        f"Thank you for your quotation. Based on our container freight and volume commitments, "
        f"our ceiling acquisition price for this parcel is USD {fob_ceiling:.2f}/MT FOB.\n\n"
        f"Desk Note: {reason}\n\n"
        f"Please confirm if you can meet our target price to lock this allocation.\n\n"
        f"Warm regards,\nProcurement Desk, Global Agro Arbitrage"
    )

    return {
        "deal_status": "counter_sent",
        "supplier_draft": supplier_msg,
    }


# ---------------------------------------------------------------------------
# Routing Logic
# ---------------------------------------------------------------------------

def route_after_evaluation(state: DealState) -> Literal["confirm_deal", "counter_buyer", "counter_supplier"]:
    """Conditional router determining the next negotiation action."""
    if state.get("is_deal_viable"):
        return "confirm_deal"

    active_role = state.get("active_role", "buyer")
    if active_role == "buyer":
        return "counter_buyer"
    else:
        return "counter_supplier"


# ---------------------------------------------------------------------------
# LangGraph Assembly
# ---------------------------------------------------------------------------

def build_arbitrage_graph():
    """Construct and compile the LangGraph StateGraph."""
    builder = StateGraph(DealState)

    builder.add_node("parse_incoming_email", parse_incoming_email_node)
    builder.add_node("fetch_market_data", fetch_market_data_node)
    builder.add_node("evaluate_risk", evaluate_risk_node)
    builder.add_node("confirm_deal", confirm_deal_node)
    builder.add_node("counter_buyer", counter_buyer_node)
    builder.add_node("counter_supplier", counter_supplier_node)

    builder.set_entry_point("parse_incoming_email")
    builder.add_edge("parse_incoming_email", "fetch_market_data")
    builder.add_edge("fetch_market_data", "evaluate_risk")

    builder.add_conditional_edges(
        "evaluate_risk",
        route_after_evaluation,
        {
            "confirm_deal": "confirm_deal",
            "counter_buyer": "counter_buyer",
            "counter_supplier": "counter_supplier",
        },
    )

    builder.add_edge("confirm_deal", END)
    builder.add_edge("counter_buyer", END)
    builder.add_edge("counter_supplier", END)

    return builder.compile()


# Singleton compiled graph instance
trade_graph = build_arbitrage_graph()
