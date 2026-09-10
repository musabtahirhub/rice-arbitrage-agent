"""
Email simulation and multi-turn negotiation API routes.
"""

from __future__ import annotations

import re
from fastapi import APIRouter, HTTPException

from app.agents.nodes import evaluate_risk, fetch_market_data, finalize_deal
from app.models.campaign import CampaignConfig
from app.models.trade import (
    ParsedTradeEmail,
    SimulateEmailRequest,
    SimulateEmailResponse,
    SimulateResponsesRequest,
)
from app.services.directory import get_counterparty_by_id
from app.services.ledger import get_campaign_state, save_campaign_state

router = APIRouter()


def _parse_email_regex(raw_email: str, sender_role: str) -> ParsedTradeEmail:
    text = raw_email
    comm_match = re.search(r"(?:commodity|product)\s*[:=]\s*(.+?)(?:\n|,\s*\d)", text, re.IGNORECASE)
    commodity = comm_match.group(1).strip() if comm_match else "Basmati 1121 Sella Rice 5% broken"

    qty_match = re.search(r"(?:quantity|qty)\s*[:=]\s*(\d[\d,]*)\s*(?:MT|metric\s*ton)", text, re.IGNORECASE)
    quantity = float(qty_match.group(1).replace(",", "")) if qty_match else 500.0

    p_match = re.search(r"(?:price|target\s*price|at|quote)?\s*[:=]?\s*(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)\s*(?:per\s*MT|/\s*MT)?", text, re.IGNORECASE)
    if not p_match or not p_match.group(1):
        p_match = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:USD|US\$|\$)\s*(?:per\s*MT|/\s*MT)", text, re.IGNORECASE)
    price = float(p_match.group(1).replace(",", "")) if p_match and p_match.group(1) else 0.0

    incoterm = "CIF"
    if re.search(r"\bFOB\b", text, re.IGNORECASE):
        incoterm = "FOB"
    elif re.search(r"\bCFR\b|\bC\s*&\s*F\b", text, re.IGNORECASE):
        incoterm = "CFR"

    port_match = re.search(r"(?:CIF|FOB|CFR|C&F)\s+(\w[\w\s]*?)(?:\n|,|\.|$)", text, re.IGNORECASE)
    port = port_match.group(1).strip() if port_match else None

    return ParsedTradeEmail(
        sender_role=sender_role,
        commodity_type=commodity,
        quantity_mt=quantity,
        price_usd_per_mt=price,
        incoterm=incoterm,
        port=port,
    )


def _generate_draft_response(state: dict, sender_role: str) -> str:
    deal_status = state.get("deal_status", "")
    benchmark_fob = state.get("benchmark_fob_usd", 0.0)
    freight_cost = state.get("freight_cost_usd", 0.0)
    benchmark_cif = benchmark_fob + freight_cost
    reason = state.get("evaluation_reason", "")

    if deal_status == "closed":
        if sender_role == "buyer":
            buyer = state.get("buyer_terms")
            p = buyer.price_usd_per_mt if buyer else 0
            return (
                f"SOFT CORPORATE OFFER (Non-Binding)\n\n"
                f"We are pleased to offer on a non-binding basis subject to supplier confirmation:\n"
                f"Commodity: {state['campaign'].commodity}\n"
                f"Quantity: {buyer.quantity_mt if buyer else 'TBD'} MT\n"
                f"Price: USD {p:.2f}/MT CIF {buyer.port if buyer else 'TBD'}\n"
                f"Payment: Irrevocable Letter of Credit at Sight\n"
                f"Benchmark CIF reference: ${benchmark_cif:.2f}/MT"
            )
        else:
            supplier = state.get("supplier_terms")
            return (
                f"We are pleased to confirm acceptance at USD {supplier.price_usd_per_mt:.2f}/MT FOB.\n"
                f"Kindly share your Proforma Invoice and banking details."
            )
    elif deal_status == "negotiating_buyer":
        min_sell = benchmark_cif * (1 + state["campaign"].min_sell_margin_above_benchmark_pct / 100)
        return f"Counter-Offer to Buyer: Our minimum viable offer is USD {min_sell:.2f}/MT CIF.\n{reason}"
    elif deal_status == "negotiating_supplier":
        max_buy = benchmark_fob * (1 + state["campaign"].max_acceptable_variance_from_benchmark_pct / 100)
        return f"Counter-Offer to Supplier: Our ceiling acquisition price is USD {max_buy:.2f}/MT FOB.\n{reason}"
    return f"Status: {deal_status}\n{reason}"


@router.post("/simulate-email", response_model=SimulateEmailResponse)
async def simulate_email(req: SimulateEmailRequest):
    state = get_campaign_state(req.campaign_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Campaign {req.campaign_id} not found.")

    parsed = _parse_email_regex(req.raw_email, req.sender_role)
    if req.sender_role == "buyer":
        state["buyer_terms"] = parsed
        state["buyer_thread_status"] = "inquiry_parsed"
    else:
        state["supplier_terms"] = parsed
        state["supplier_thread_status"] = "quote_parsed"

    state["raw_email"] = req.raw_email
    state = fetch_market_data(state)

    if state.get("buyer_terms") and state.get("supplier_terms"):
        state = evaluate_risk(state)
        if state.get("deal_status") == "approved":
            state = finalize_deal(state)
        drafted = _generate_draft_response(state, req.sender_role)
        state["buyer_draft" if req.sender_role == "buyer" else "supplier_draft"] = drafted
    else:
        state["deal_status"] = "prospecting"
        state["evaluation_reason"] = "Terms needed to evaluate deal."
        drafted = ""

    save_campaign_state(req.campaign_id, state)

    return SimulateEmailResponse(
        extracted_terms=parsed.model_dump() if parsed else None,
        deal_viable=state.get("is_deal_viable", False),
        margin_pct=state.get("net_margin_pct", 0.0),
        evaluation_reason=state.get("evaluation_reason", ""),
        drafted_response=drafted,
        campaign_status=state.get("deal_status", "prospecting"),
        benchmark_fob_usd=state.get("benchmark_fob_usd", 0.0),
        freight_cost_usd=state.get("freight_cost_usd", 0.0),
        buyer_thread_status=state.get("buyer_thread_status", ""),
        supplier_thread_status=state.get("supplier_thread_status", ""),
        negotiation_round=state.get("negotiation_round", 0),
    )


@router.post("/campaigns/{campaign_id}/simulate-responses")
async def simulate_responses(campaign_id: str, req: SimulateResponsesRequest = SimulateResponsesRequest()):
    state = get_campaign_state(campaign_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    campaign: CampaignConfig = state["campaign"]
    buyer_info = state.get("primary_buyer") or {}
    supplier_info = state.get("primary_supplier") or {}
    if req.buyer_id:
        cp = get_counterparty_by_id(req.buyer_id)
        if cp: buyer_info = cp.model_dump()
    if req.supplier_id:
        cp = get_counterparty_by_id(req.supplier_id)
        if cp: supplier_info = cp.model_dump()

    benchmark_fob = state.get("benchmark_fob_usd", 900.0)
    freight_cost = state.get("freight_cost_usd", 55.0)
    landed_cif = benchmark_fob + freight_cost
    target_vol = 500.0

    if req.scenario == "lowball_buyer":
        buyer_price, supplier_price = round(landed_cif * 0.90, 2), round(benchmark_fob * 1.01, 2)
    elif req.scenario == "high_supplier":
        buyer_price, supplier_price = round(landed_cif * 1.15, 2), round(benchmark_fob * 1.15, 2)
    else:
        supplier_price = round(benchmark_fob * 1.01, 2)
        buyer_price = round(landed_cif * (1 + (campaign.target_profit_margin_pct + 4.0) / 100), 2)

    supplier_email = f"Offer: {campaign.commodity}\nQuantity: {target_vol} MT\nPrice: USD {supplier_price:.2f}/MT FOB"
    buyer_email = f"Inquiry: {campaign.commodity}\nQuantity: {target_vol} MT\nPrice: USD {buyer_price:.2f}/MT CIF"

    state["supplier_terms"] = _parse_email_regex(supplier_email, "supplier")
    state["buyer_terms"] = _parse_email_regex(buyer_email, "buyer")
    state["buyer_thread_status"] = "inquiry_parsed"
    state["supplier_thread_status"] = "quote_parsed"
    state["negotiation_round"] = state.get("negotiation_round", 0) + 1

    state = fetch_market_data(state)
    state = evaluate_risk(state)
    if state.get("deal_status") == "approved":
        state = finalize_deal(state)

    state["buyer_draft"] = _generate_draft_response(state, "buyer")
    state["supplier_draft"] = _generate_draft_response(state, "supplier")
    save_campaign_state(campaign_id, state)

    return {
        "campaign_id": campaign_id,
        "negotiation_round": state["negotiation_round"],
        "deal_status": state.get("deal_status"),
        "is_deal_viable": state.get("is_deal_viable"),
        "net_margin_pct": state.get("net_margin_pct"),
        "evaluation_reason": state.get("evaluation_reason"),
        "counterparties": {"buyer": buyer_info, "supplier": supplier_info},
        "agent_outbound_drafts": {"buyer_draft": state["buyer_draft"], "supplier_draft": state["supplier_draft"]},
        "market_context": {"benchmark_fob": state.get("benchmark_fob_usd"), "freight_cost": state.get("freight_cost_usd")},
    }
