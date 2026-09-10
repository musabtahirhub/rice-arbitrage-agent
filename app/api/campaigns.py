"""
Campaign lifecycle and directory API routes.
"""

from __future__ import annotations

import uuid
from fastapi import APIRouter, HTTPException

from app.agents.discovery import run_discovery
from app.fixtures import VIABLE_BUYER_EMAIL, MATCHING_SUPPLIER_EMAIL, LOWBALL_BUYER_EMAIL
from app.models.campaign import CampaignConfig
from app.models.trade import CreateCampaignRequest
from app.services.directory import get_all_counterparties
from app.services.ledger import get_campaign_state, save_campaign_state, serialize_state
from app.services.market import MarketDataService

router = APIRouter()


@router.get("/directory")
async def get_directory():
    """Return all registered buyers and suppliers from directory."""
    return get_all_counterparties()


@router.get("/fixtures")
async def get_fixtures():
    """Return preset trade email fixtures for test simulation."""
    return {
        "viable_buyer": VIABLE_BUYER_EMAIL,
        "matching_supplier": MATCHING_SUPPLIER_EMAIL,
        "lowball_buyer": LOWBALL_BUYER_EMAIL,
    }


@router.post("/campaigns")
async def create_campaign(req: CreateCampaignRequest):
    """
    Create a new campaign and automatically ignite autonomous counterparty discovery.
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

    market_svc = MarketDataService()
    benchmark_fob, bench_source = market_svc.get_benchmark_rate(req.benchmark_index, 5.0)
    freight_cost, freight_source = market_svc.estimate_freight("Mundra", "Jebel Ali")

    discovery_res = run_discovery(
        campaign=config,
        benchmark_fob=benchmark_fob,
        freight_cost=freight_cost,
        target_volume_mt=req.target_volume_mt,
    )

    primary_b = discovery_res.get("primary_buyer")
    primary_s = discovery_res.get("primary_supplier")

    state = {
        "campaign": config,
        "benchmark_fob_usd": benchmark_fob,
        "benchmark_source": bench_source,
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
        "buyer_draft": discovery_res["buyer_outreach_drafts"].get(primary_b["id"], "") if primary_b else "",
        "supplier_draft": discovery_res["supplier_rfq_drafts"].get(primary_s["id"], "") if primary_s else "",
        "raw_email": "",
        "discovered_buyers": discovery_res["discovered_buyers"],
        "discovered_suppliers": discovery_res["discovered_suppliers"],
        "buyer_outreach_drafts": discovery_res["buyer_outreach_drafts"],
        "supplier_rfq_drafts": discovery_res["supplier_rfq_drafts"],
        "primary_buyer": primary_b,
        "primary_supplier": primary_s,
        "indicative_buyer_cif": discovery_res["indicative_buyer_cif"],
        "supplier_target_fob": discovery_res["supplier_target_fob"],
    }

    save_campaign_state(campaign_id, state)

    return {
        "campaign_id": campaign_id,
        "commodity": req.commodity,
        "target_margin_pct": req.target_margin_pct,
        "benchmark_fob_usd": benchmark_fob,
        "benchmark_source": bench_source,
        "freight_cost_usd": freight_cost,
        "freight_source": freight_source,
        "status": "ignited",
        "discovered_buyers_count": len(discovery_res["discovered_buyers"]),
        "discovered_suppliers_count": len(discovery_res["discovered_suppliers"]),
        "primary_buyer": primary_b,
        "primary_supplier": primary_s,
        "indicative_buyer_cif": discovery_res["indicative_buyer_cif"],
        "supplier_target_fob": discovery_res["supplier_target_fob"],
        "buyer_outreach_preview": state["buyer_draft"][:160] + "...",
        "supplier_rfq_preview": state["supplier_draft"][:160] + "...",
    }


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str):
    state = get_campaign_state(campaign_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")
    return serialize_state(state)
