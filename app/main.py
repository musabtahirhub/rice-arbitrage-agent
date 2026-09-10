"""
FastAPI backend for the multi-agent commodity arbitrage system.

Endpoints:
  GET  /                     Serves the single-page test dashboard.
  POST /api/campaigns        Creates a new campaign with config parameters.
  POST /api/simulate-email   Simulates an inbound email through the pipeline.
  GET  /api/campaigns/{id}   Retrieves current campaign state.
  GET  /api/fixtures         Returns sample email fixtures for quick-fill.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.arbitrage_engine import evaluate_deal
from app.agents.orchestrator import (
    evaluate_risk,
    fetch_market_data,
    finalize_deal,
)
from app.agents.discovery import (
    run_discovery,
    draft_buyer_cold_outreach,
    draft_supplier_rfq,
)
from app.directory import (
    get_all_counterparties,
    get_buyers_for_commodity,
    get_suppliers_for_commodity,
    get_counterparty_by_id,
    Counterparty,
)
from app.fixtures import VIABLE_BUYER_EMAIL, MATCHING_SUPPLIER_EMAIL, LOWBALL_BUYER_EMAIL
from app.market_data import MarketDataService
from app.schemas import CampaignConfig, ParsedTradeEmail

load_dotenv()

app = FastAPI(
    title="Rice Arbitrage Multi-Agent System",
    description="Physical commodity arbitrage agent with dynamic market grounding.",
    version="2.0.0",
)


# ---------------------------------------------------------------------------
# In-memory campaign ledger
# ---------------------------------------------------------------------------

_campaigns: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateCampaignRequest(BaseModel):
    commodity: str = Field(
        default="Basmati 1121 Sella Rice 5% broken",
        description="Commodity variety and spec.",
    )
    target_volume_mt: float = Field(
        default=500.0,
        description="Target volume in metric tons.",
    )
    target_margin_pct: float = Field(
        default=5.0,
        description="Minimum acceptable profit margin %.",
    )
    max_buy_variance_pct: float = Field(
        default=5.0,
        description="Max acceptable % above FOB benchmark for buy side.",
    )
    min_sell_margin_pct: float = Field(
        default=2.0,
        description="Min acceptable % above CIF benchmark for sell side.",
    )
    benchmark_index: str = Field(
        default="basmati_1121",
        description="Market benchmark index slug.",
    )


class SimulateEmailRequest(BaseModel):
    campaign_id: str
    thread_id: str = Field(default="")
    raw_email: str
    sender_role: str = Field(
        ...,
        description="'buyer' or 'supplier'",
    )


class SimulateEmailResponse(BaseModel):
    extracted_terms: Optional[dict] = None
    deal_viable: bool = False
    margin_pct: float = 0.0
    evaluation_reason: str = ""
    drafted_response: str = ""
    campaign_status: str = "prospecting"
    benchmark_fob_usd: float = 0.0
    freight_cost_usd: float = 0.0
    buyer_thread_status: str = ""
    supplier_thread_status: str = ""
    negotiation_round: int = 0


# ---------------------------------------------------------------------------
# Regex-based email parser (no LLM required)
# ---------------------------------------------------------------------------

def _parse_email_regex(raw_email: str, sender_role: str) -> ParsedTradeEmail:
    """
    Extract trade terms from an email using regex patterns.
    Works offline without an LLM -- ideal for the test dashboard.
    """
    text = raw_email

    # Commodity
    commodity_match = re.search(
        r"(?:commodity|product)\s*[:=]\s*(.+?)(?:\n|,\s*\d)",
        text, re.IGNORECASE,
    )
    commodity = commodity_match.group(1).strip() if commodity_match else "Basmati 1121 Sella Rice 5% broken"

    # Quantity
    qty_match = re.search(
        r"(?:quantity|qty)\s*[:=]\s*(\d[\d,]*)\s*(?:MT|metric\s*ton)",
        text, re.IGNORECASE,
    )
    quantity = float(qty_match.group(1).replace(",", "")) if qty_match else 500.0

    # Price
    price_match = re.search(
        r"(?:price|target\s*price)\s*[:=]\s*(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)\s*(?:per\s*MT|/\s*MT)",
        text, re.IGNORECASE,
    )
    price = float(price_match.group(1).replace(",", "")) if price_match else 0.0

    # Incoterm
    incoterm = "CIF"
    if re.search(r"\bFOB\b", text, re.IGNORECASE):
        incoterm = "FOB"
    elif re.search(r"\bCIF\b", text, re.IGNORECASE):
        incoterm = "CIF"
    elif re.search(r"\bCFR\b|\bC\s*&\s*F\b", text, re.IGNORECASE):
        incoterm = "CFR"

    # Port
    port_match = re.search(
        r"(?:CIF|FOB|CFR|C&F)\s+(\w[\w\s]*?)(?:\n|,|\.|$)",
        text, re.IGNORECASE,
    )
    port = port_match.group(1).strip() if port_match else None

    return ParsedTradeEmail(
        sender_role=sender_role,
        commodity_type=commodity,
        quantity_mt=quantity,
        price_usd_per_mt=price,
        incoterm=incoterm,
        port=port,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the single-page test dashboard."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard HTML not found.")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/directory")
async def get_directory():
    """Return all registered buyers and suppliers from the marketplace directory."""
    return get_all_counterparties()


@app.post("/api/campaigns")
async def create_campaign(req: CreateCampaignRequest):
    """
    Create a new campaign and automatically ignite autonomous counterparty discovery.
    Queries the directory for matching buyers & suppliers, drafts cold outreach and RFQs,
    and initializes parallel negotiation threads.
    """
    campaign_id = f"CAMP-{uuid.uuid4().hex[:8].upper()}"

    config = CampaignConfig(
        campaign_id=campaign_id,
        commodity=req.commodity,
        target_profit_margin_pct=req.target_margin_pct,
        max_acceptable_variance_from_benchmark_pct=req.max_buy_variance_pct,
        min_sell_margin_above_benchmark_pct=req.min_sell_margin_pct,
        benchmark_index_name=req.benchmark_index,
    )

    # Fetch initial market data
    market_svc = MarketDataService()
    broken_pct = _extract_broken_pct(req.commodity)
    benchmark_fob, benchmark_source = market_svc.get_benchmark_rate(
        req.benchmark_index, broken_pct
    )
    freight_cost, freight_source = market_svc.estimate_freight("Mundra", "Jebel Ali")

    # Autonomous Campaign Ignition: Discover counterparties & draft initial outreach
    discovery_res = run_discovery(
        campaign=config,
        benchmark_fob=benchmark_fob,
        freight_cost=freight_cost,
        target_volume_mt=req.target_volume_mt,
    )

    state = {
        "campaign": config,
        "benchmark_fob_usd": benchmark_fob,
        "benchmark_source": benchmark_source,
        "freight_cost_usd": freight_cost,
        "freight_source": freight_source,
        "buyer_terms": None,
        "supplier_terms": None,
        "buyer_thread_status": "outreach_sent",
        "supplier_thread_status": "rfq_sent",
        "deal_status": "prospecting",
        "negotiation_round": 0,
        "net_margin_pct": 0.0,
        "is_deal_viable": False,
        "evaluation_reason": f"Discovered {len(discovery_res['discovered_buyers'])} buyer(s) and {len(discovery_res['discovered_suppliers'])} supplier(s). Automated outreach initiated.",
        "buyer_draft": discovery_res["buyer_outreach_drafts"].get(
            discovery_res["primary_buyer"]["id"], ""
        ) if discovery_res.get("primary_buyer") else "",
        "supplier_draft": discovery_res["supplier_rfq_drafts"].get(
            discovery_res["primary_supplier"]["id"], ""
        ) if discovery_res.get("primary_supplier") else "",
        "raw_email": "",
        "discovered_buyers": discovery_res["discovered_buyers"],
        "discovered_suppliers": discovery_res["discovered_suppliers"],
        "buyer_outreach_drafts": discovery_res["buyer_outreach_drafts"],
        "supplier_rfq_drafts": discovery_res["supplier_rfq_drafts"],
        "primary_buyer": discovery_res["primary_buyer"],
        "primary_supplier": discovery_res["primary_supplier"],
        "indicative_buyer_cif": discovery_res["indicative_buyer_cif"],
        "supplier_target_fob": discovery_res["supplier_target_fob"],
    }

    _campaigns[campaign_id] = state

    return {
        "campaign_id": campaign_id,
        "commodity": req.commodity,
        "target_margin_pct": req.target_margin_pct,
        "benchmark_fob_usd": benchmark_fob,
        "benchmark_source": benchmark_source,
        "freight_cost_usd": freight_cost,
        "freight_source": freight_source,
        "status": "ignited",
        "discovered_buyers_count": len(discovery_res["discovered_buyers"]),
        "discovered_suppliers_count": len(discovery_res["discovered_suppliers"]),
        "primary_buyer": discovery_res["primary_buyer"],
        "primary_supplier": discovery_res["primary_supplier"],
        "indicative_buyer_cif": discovery_res["indicative_buyer_cif"],
        "supplier_target_fob": discovery_res["supplier_target_fob"],
        "buyer_outreach_preview": state["buyer_draft"][:160] + "...",
        "supplier_rfq_preview": state["supplier_draft"][:160] + "...",
    }


@app.get("/api/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str):
    """Retrieve current campaign state."""
    if campaign_id not in _campaigns:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")
    state = _campaigns[campaign_id]
    return _serialize_state(state)


class SimulateResponsesRequest(BaseModel):
    scenario: str = Field(
        default="viable",
        description="'viable', 'lowball_buyer', or 'high_supplier'",
    )
    buyer_id: Optional[str] = None
    supplier_id: Optional[str] = None


@app.post("/api/campaigns/{campaign_id}/simulate-responses")
async def simulate_responses(campaign_id: str, req: SimulateResponsesRequest = SimulateResponsesRequest()):
    """
    Simulate realistic inbound email responses from discovered directory counterparties
    and advance the negotiation through the LangGraph orchestrator and risk evaluator.
    """
    if campaign_id not in _campaigns:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    state = _campaigns[campaign_id]
    campaign: CampaignConfig = state["campaign"]

    # Identify counterparty records
    buyer_info = state.get("primary_buyer") or {}
    supplier_info = state.get("primary_supplier") or {}
    if req.buyer_id:
        cp = get_counterparty_by_id(req.buyer_id)
        if cp:
            buyer_info = cp.model_dump()
    if req.supplier_id:
        cp = get_counterparty_by_id(req.supplier_id)
        if cp:
            supplier_info = cp.model_dump()

    benchmark_fob = state.get("benchmark_fob_usd", 900.0)
    freight_cost = state.get("freight_cost_usd", 55.0)
    landed_cif = benchmark_fob + freight_cost

    buyer_port = buyer_info.get("port", "Jebel Ali")
    supplier_port = supplier_info.get("port", "Karachi")
    target_volume = float(getattr(campaign, "target_volume_mt", 500.0) if hasattr(campaign, "target_volume_mt") else 500.0)

    # Determine simulated prices based on scenario
    if req.scenario == "lowball_buyer":
        # Buyer offers 10% below benchmark CIF
        buyer_price = round(landed_cif * 0.90, 2)
        supplier_price = round(benchmark_fob * 1.01, 2)
    elif req.scenario == "high_supplier":
        # Supplier asks 15% above benchmark FOB (violating max variance)
        buyer_price = round(landed_cif * 1.15, 2)
        supplier_price = round(benchmark_fob * 1.15, 2)
    else:  # "viable"
        # Profitable spread
        supplier_price = round(benchmark_fob * 1.01, 2)  # e.g., $909 FOB
        # Buyer agrees to indicative CIF level ensuring healthy margin
        buyer_price = round(landed_cif * (1 + (campaign.target_profit_margin_pct + 4.0) / 100), 2)

    # Formulate realistic incoming emails
    simulated_supplier_email = (
        f"From: {supplier_info.get('contact_name', 'Export Team')} <{supplier_info.get('contact_email', 'sales@supplier.com')}>\n"
        f"Subject: RE: Urgent RFQ - Quotation for {campaign.commodity}\n\n"
        f"Thank you for your RFQ. We are pleased to submit our firm quotation:\n"
        f"Commodity: {campaign.commodity}\n"
        f"Quantity: {target_volume:,.0f} MT\n"
        f"Price: USD {supplier_price:.2f} per MT, FOB {supplier_port} Port\n"
        f"Payment: 100% Irrevocable LC at Sight\n"
        f"Shipment: Within 20 days\n"
    )

    simulated_buyer_email = (
        f"From: {buyer_info.get('contact_name', 'Procurement Desk')} <{buyer_info.get('contact_email', 'trade@buyer.ae')}>\n"
        f"Subject: RE: Trade Inquiry Acceptance - {campaign.commodity}\n\n"
        f"We have reviewed your indicative offer and would like to confirm our firm purchase order:\n"
        f"Commodity: {campaign.commodity}\n"
        f"Quantity: {target_volume:,.0f} MT\n"
        f"Price: USD {buyer_price:.2f} per MT, CIF {buyer_port}\n"
        f"Payment: Irrevocable Letter of Credit at Sight\n"
    )

    # Parse terms
    supplier_parsed = _parse_email_regex(simulated_supplier_email, "supplier")
    buyer_parsed = _parse_email_regex(simulated_buyer_email, "buyer")

    state["supplier_terms"] = supplier_parsed
    state["buyer_terms"] = buyer_parsed
    state["buyer_thread_status"] = "inquiry_parsed"
    state["supplier_thread_status"] = "quote_parsed"
    state["negotiation_round"] = state.get("negotiation_round", 0) + 1

    # Refresh market data and evaluate risk
    state = fetch_market_data(state)
    state = evaluate_risk(state)

    if state.get("deal_status") == "approved":
        state = finalize_deal(state)

    # Generate drafted responses for both sides
    state["buyer_draft"] = _generate_draft_response(state, "buyer")
    state["supplier_draft"] = _generate_draft_response(state, "supplier")

    _campaigns[campaign_id] = state

    return {
        "campaign_id": campaign_id,
        "negotiation_round": state["negotiation_round"],
        "deal_status": state.get("deal_status"),
        "is_deal_viable": state.get("is_deal_viable"),
        "net_margin_pct": state.get("net_margin_pct"),
        "evaluation_reason": state.get("evaluation_reason"),
        "counterparties": {
            "buyer": buyer_info,
            "supplier": supplier_info,
        },
        "incoming_responses": {
            "buyer_email": simulated_buyer_email,
            "supplier_email": simulated_supplier_email,
            "buyer_terms": buyer_parsed.model_dump(),
            "supplier_terms": supplier_parsed.model_dump(),
        },
        "agent_outbound_drafts": {
            "buyer_draft": state["buyer_draft"],
            "supplier_draft": state["supplier_draft"],
        },
        "market_context": {
            "benchmark_fob": state.get("benchmark_fob_usd"),
            "freight_cost": state.get("freight_cost_usd"),
        },
    }



@app.post("/api/simulate-email", response_model=SimulateEmailResponse)
async def simulate_email(req: SimulateEmailRequest):
    """
    Simulate an inbound trade email through the multi-agent pipeline.

    Steps:
    1. Parse the email using regex extraction (no LLM needed).
    2. Fetch/refresh market benchmark rates.
    3. Run the deterministic risk evaluator.
    4. Generate a drafted response summary.
    """
    if req.campaign_id not in _campaigns:
        raise HTTPException(
            status_code=404,
            detail=f"Campaign {req.campaign_id} not found. Create one first.",
        )

    state = _campaigns[req.campaign_id]

    # 1. Parse the email
    parsed = _parse_email_regex(req.raw_email, req.sender_role)

    if req.sender_role == "buyer":
        state["buyer_terms"] = parsed
        state["buyer_thread_status"] = "inquiry_parsed"
    else:
        state["supplier_terms"] = parsed
        state["supplier_thread_status"] = "quote_parsed"

    state["raw_email"] = req.raw_email

    # 2. Refresh market data (re-fetch with correct ports)
    state = fetch_market_data(state)

    # 3. Run risk evaluation if both sides present
    if state.get("buyer_terms") and state.get("supplier_terms"):
        state = evaluate_risk(state)

        # 4. If approved, finalize
        if state.get("deal_status") == "approved":
            state = finalize_deal(state)

        # Generate drafted response
        drafted = _generate_draft_response(state, req.sender_role)
        state["buyer_draft" if req.sender_role == "buyer" else "supplier_draft"] = drafted
    else:
        state["deal_status"] = "prospecting"
        state["evaluation_reason"] = (
            f"{'Buyer' if not state.get('buyer_terms') else 'Supplier'} "
            f"terms still needed to evaluate the deal."
        )
        drafted = ""

    _campaigns[req.campaign_id] = state

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


@app.get("/api/fixtures")
async def get_fixtures():
    """Return sample email fixtures for the quick-fill buttons."""
    return {
        "viable_buyer": VIABLE_BUYER_EMAIL,
        "matching_supplier": MATCHING_SUPPLIER_EMAIL,
        "lowball_buyer": LOWBALL_BUYER_EMAIL,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_broken_pct(commodity: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*broken", commodity.lower())
    return float(match.group(1)) if match else 5.0


def _generate_draft_response(state: dict, sender_role: str) -> str:
    """Generate a deterministic draft response based on deal status."""
    deal_status = state.get("deal_status", "")
    benchmark_fob = state.get("benchmark_fob_usd", 0.0)
    freight_cost = state.get("freight_cost_usd", 0.0)
    benchmark_cif = benchmark_fob + freight_cost
    reason = state.get("evaluation_reason", "")
    margin = state.get("net_margin_pct", 0.0)

    if deal_status == "closed":
        if sender_role == "buyer":
            buyer = state.get("buyer_terms")
            price = buyer.price_usd_per_mt if buyer else 0
            return (
                f"SOFT CORPORATE OFFER (Non-Binding)\n\n"
                f"We are pleased to offer the following on a non-binding basis, "
                f"subject to final supplier confirmation:\n\n"
                f"  Commodity: {state['campaign'].commodity}\n"
                f"  Quantity: {buyer.quantity_mt if buyer else 'TBD'} MT\n"
                f"  Price: USD {price:.2f}/MT CIF {buyer.port if buyer else 'TBD'}\n"
                f"  Payment: Irrevocable Letter of Credit at Sight\n"
                f"  Validity: 48 hours from issuance\n\n"
                f"In line with current CIF market indices of ${benchmark_cif:.2f}/MT, "
                f"this offer represents competitive market-level pricing.\n\n"
                f"Please confirm your interest to proceed with binding terms."
            )
        else:
            supplier = state.get("supplier_terms")
            return (
                f"We are pleased to confirm acceptance of your offer at "
                f"USD {supplier.price_usd_per_mt:.2f}/MT FOB {supplier.port or 'TBD'}.\n\n"
                f"Kindly share your Proforma Invoice and banking details."
            )

    elif deal_status == "negotiating_buyer":
        min_sell = benchmark_cif * (1 + state["campaign"].min_sell_margin_above_benchmark_pct / 100)
        return (
            f"Counter-Offer to Buyer:\n\n"
            f"Thank you for your inquiry. Based on prevailing CIF market indices "
            f"of ${benchmark_cif:.2f}/MT, we are unable to meet your requested price.\n\n"
            f"Our best offer is USD {min_sell:.2f}/MT CIF, reflecting current "
            f"benchmark levels plus standard logistics and handling.\n\n"
            f"Evaluation: {reason}"
        )

    elif deal_status == "negotiating_supplier":
        max_buy = benchmark_fob * (1 + state["campaign"].max_acceptable_variance_from_benchmark_pct / 100)
        return (
            f"Counter-Offer to Supplier:\n\n"
            f"In line with current FOB indices of ${benchmark_fob:.2f}/MT for this "
            f"specification, we request a revised price closer to ${max_buy:.2f}/MT FOB.\n\n"
            f"Current market conditions do not support the quoted level.\n\n"
            f"Evaluation: {reason}"
        )

    elif deal_status == "rejected":
        return f"Deal rejected.\n\nReason: {reason}"

    return f"Awaiting additional information.\n\n{reason}"


def _serialize_state(state: dict) -> dict:
    """Serialize state dict for JSON response."""
    result = {}
    for k, v in state.items():
        if isinstance(v, CampaignConfig):
            result[k] = v.model_dump()
        elif isinstance(v, ParsedTradeEmail):
            result[k] = v.model_dump()
        elif hasattr(v, "model_dump"):
            result[k] = v.model_dump()
        else:
            result[k] = v
    return result
