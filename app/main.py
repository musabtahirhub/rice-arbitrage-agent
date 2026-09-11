"""
FastAPI web application exposing 3 clean REST endpoints for the arbitrage system:
1. POST /api/campaigns   - Create campaign and discover counterparties
2. POST /api/negotiate   - Run LangGraph negotiation turn from an incoming email
3. GET  /api/campaigns/{id} - View live campaign ledger state and metrics
"""
import os
from pathlib import Path
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.directory import get_all_counterparties, get_buyers_for_commodity, get_suppliers_for_commodity
from app.market import get_benchmark_rate, estimate_freight
from app.math_engine import calculate_dynamic_bounds
from app.models import Campaign, CreateCampaignRequest, DealState, SimulateTurnRequest
from app.workflow import trade_graph

app = FastAPI(
    title="Commodity Arbitrage Multi-Agent System",
    description="Educational Mid-Level Autonomous Physical Commodity Arbitrage Agent",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory ledger storing campaign states
CAMPAIGN_LEDGER: dict[str, DealState] = {}

# Locate static UI directory
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if not STATIC_DIR.exists():
    STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/directory")
def list_directory():
    """List all registered buyers and suppliers."""
    return get_all_counterparties()


@app.post("/api/campaigns")
def create_campaign(req: CreateCampaignRequest):
    """
    Endpoint 1: Create a new arbitrage campaign, discover counterparties,
    and initialize dynamic pricing benchmarks.
    """
    campaign_id = f"CAMP-{uuid.uuid4().hex[:6].upper()}"
    campaign = Campaign(
        campaign_id=campaign_id,
        commodity=req.commodity,
        target_volume_mt=req.target_volume_mt,
        target_margin_pct=req.target_margin_pct,
        max_variance_from_benchmark_pct=req.max_variance_from_benchmark_pct,
        destination_port=req.destination_port,
    )

    # Initial market lookup
    benchmark_fob = get_benchmark_rate(campaign.commodity)
    freight = estimate_freight(campaign.origin_port_default, campaign.destination_port)
    bounds = calculate_dynamic_bounds(
        benchmark_fob=benchmark_fob,
        freight=freight,
        max_variance_pct=campaign.max_variance_from_benchmark_pct,
        target_margin_pct=campaign.target_margin_pct,
        buffer_usd=campaign.buffer_usd_per_mt,
    )

    # Discover matching buyers and suppliers
    buyers = get_buyers_for_commodity(campaign.commodity)
    suppliers = get_suppliers_for_commodity(campaign.commodity)

    # Initialize DealState in ledger
    initial_state: DealState = {
        "campaign": campaign,
        "benchmark_fob_usd": benchmark_fob,
        "freight_cost_usd": freight,
        "dynamic_fob_ceiling": bounds["dynamic_fob_ceiling"],
        "dynamic_cif_floor": bounds["dynamic_cif_floor"],
        "buyer_terms": None,
        "supplier_terms": None,
        "negotiation_round": 0,
        "deal_status": "prospecting",
        "is_deal_viable": False,
        "net_margin_pct": 0.0,
        "evaluation_reason": f"Discovered {len(buyers)} buyers and {len(suppliers)} suppliers. Awaiting initial offers.",
        "latest_email": "",
        "active_role": "buyer",
        "buyer_draft": "",
        "supplier_draft": "",
    }
    CAMPAIGN_LEDGER[campaign_id] = initial_state

    return {
        "campaign_id": campaign_id,
        "commodity": campaign.commodity,
        "benchmark_fob_usd": benchmark_fob,
        "freight_cost_usd": freight,
        "dynamic_fob_ceiling": bounds["dynamic_fob_ceiling"],
        "dynamic_cif_floor": bounds["dynamic_cif_floor"],
        "discovered_buyers": [b.model_dump() for b in buyers],
        "discovered_suppliers": [s.model_dump() for s in suppliers],
        "status": "initialized",
    }


@app.post("/api/negotiate")
def negotiate_turn(req: SimulateTurnRequest):
    """
    Endpoint 2: Simulate a negotiation turn from an incoming buyer or supplier email.
    Passes state through the LangGraph StateGraph.
    """
    state = CAMPAIGN_LEDGER.get(req.campaign_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Campaign {req.campaign_id} not found.")

    # Prepare input state for LangGraph
    turn_state = dict(state)
    turn_state["latest_email"] = req.raw_email
    turn_state["active_role"] = req.sender_role

    # Execute LangGraph StateGraph
    updated_state = trade_graph.invoke(turn_state)

    # Persist updated state in memory ledger
    CAMPAIGN_LEDGER[req.campaign_id] = updated_state

    # Format terms for JSON response
    buyer_t = updated_state.get("buyer_terms")
    supp_t = updated_state.get("supplier_terms")

    return {
        "campaign_id": req.campaign_id,
        "negotiation_round": updated_state.get("negotiation_round", 1),
        "deal_status": updated_state.get("deal_status"),
        "is_deal_viable": updated_state.get("is_deal_viable"),
        "net_margin_pct": updated_state.get("net_margin_pct"),
        "evaluation_reason": updated_state.get("evaluation_reason"),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": updated_state.get("buyer_draft"),
        "supplier_draft": updated_state.get("supplier_draft"),
    }


@app.get("/api/campaigns/{campaign_id}")
def get_campaign_status(campaign_id: str):
    """
    Endpoint 3: View live status, profit metrics, and boundary checks for a campaign.
    """
    state = CAMPAIGN_LEDGER.get(campaign_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    buyer_t = state.get("buyer_terms")
    supp_t = state.get("supplier_terms")

    return {
        "campaign_id": campaign_id,
        "commodity": state["campaign"].commodity,
        "target_margin_pct": state["campaign"].target_margin_pct,
        "negotiation_round": state.get("negotiation_round", 0),
        "deal_status": state.get("deal_status"),
        "is_deal_viable": state.get("is_deal_viable"),
        "net_margin_pct": state.get("net_margin_pct"),
        "evaluation_reason": state.get("evaluation_reason"),
        "dynamic_fob_ceiling": state.get("dynamic_fob_ceiling"),
        "dynamic_cif_floor": state.get("dynamic_cif_floor"),
        "benchmark_fob_usd": state.get("benchmark_fob_usd"),
        "freight_cost_usd": state.get("freight_cost_usd"),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": state.get("buyer_draft"),
        "supplier_draft": state.get("supplier_draft"),
    }


# ---------------------------------------------------------------------------
# Frontend Dashboard
# ---------------------------------------------------------------------------

@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Commodity Arbitrage API is running. Visit /docs for Swagger documentation."}
