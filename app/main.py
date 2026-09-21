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

from app.config import settings
from app.directory import get_all_counterparties, get_buyers_for_commodity, get_suppliers_for_commodity
from app.market import get_benchmark_rate, estimate_freight
from app.math_engine import calculate_dynamic_bounds
from app.models import Campaign, CreateCampaignRequest, DealState, SimulateTurnRequest
from app.workflow import generate_proactive_sco_draft, split_subject_and_body, trade_graph

app = FastAPI(
    title=f"{settings.desk_name} - Physical Commodity Arbitrage",
    description="Educational Mid-Level Autonomous Physical Commodity Arbitrage Agent",
    version="2.0.0",
)

cors_list = settings.cors_origins if isinstance(settings.cors_origins, list) else [s.strip() for s in settings.cors_origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_list,
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
    Endpoint 1: Create a new arbitrage campaign, proactively discover Middle East buyers,
    calculate initial anchor CIF pitch, and auto-draft outbound Cold Soft Corporate Offer (SCO).
    """
    campaign_id = f"CAMP-{uuid.uuid4().hex[:6].upper()}"
    campaign = Campaign(
        campaign_id=campaign_id,
        commodity=req.commodity,
        target_volume_mt=req.target_volume_mt,
        target_margin_pct=req.target_margin_pct,
        max_variance_from_benchmark_pct=req.max_variance_from_benchmark_pct,
        destination_port=req.destination_port,
        min_profit_per_mt_hard=req.min_profit_per_mt_hard,
        min_profit_per_mt_soft=req.min_profit_per_mt_soft,
        max_negotiation_rounds=req.max_negotiation_rounds,
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

    # Middle East buyer targeting
    me_buyers = [b for b in buyers if b.country in ["UAE", "Saudi Arabia", "Qatar", "Kuwait", "Oman", "Bahrain"]]
    primary_buyer = me_buyers[0] if me_buyers else (buyers[0] if buyers else None)
    buyer_name = primary_buyer.name if primary_buyer else "Procurement Partner"
    buyer_email = primary_buyer.contact_email if primary_buyer else "procurement@domain.com"

    buffer_usd = campaign.buffer_usd_per_mt if campaign.buffer_usd_per_mt is not None else settings.default_buffer_usd_per_mt
    # Initial high-anchor CIF pitch: benchmark_fob + freight + buffer_usd + campaign.min_profit_per_mt_soft + 30.0
    anchor_cif = round(benchmark_fob + freight + buffer_usd + campaign.min_profit_per_mt_soft + 30.0, 2)

    # Auto-draft outbound Cold Soft Corporate Offer (SCO) to the buyer dynamically
    initial_buyer_draft = generate_proactive_sco_draft(
        campaign=campaign,
        anchor_cif=anchor_cif,
        buyer_name=buyer_name,
        buyer_email=buyer_email,
    )
    sco_subject, _ = split_subject_and_body(
        initial_buyer_draft,
        default_subject=f"Soft Corporate Offer (SCO) — {campaign.commodity} CIF {campaign.destination_port}",
    )

    # Initialize DealState in ledger with Pipeline Step 1
    outbound_sco_entry = {
        "turn": 0,
        "sender": f"Trading Desk ({settings.desk_name})",
        "recipient": f"{buyer_name} <{buyer_email}>",
        "role": "agent",
        "action": "OUTBOUND_SCO",
        "subject": sco_subject,
        "message": initial_buyer_draft,
    }

    initial_state: DealState = {
        "campaign": campaign,
        "benchmark_fob_usd": benchmark_fob,
        "freight_cost_usd": freight,
        "dynamic_fob_ceiling": bounds["dynamic_fob_ceiling"],
        "dynamic_cif_floor": bounds["dynamic_cif_floor"],
        "target_fob_ceiling": 0.0,
        "anchor_cif_usd": anchor_cif,
        "buyer_terms": None,
        "supplier_terms": None,
        "negotiation_round": 0,
        "deal_status": "prospecting",
        "action": None,
        "pipeline_step": 1,
        "is_deal_viable": False,
        "net_spread_usd": 0.0,
        "net_margin_pct": 0.0,
        "evaluation_reason": f"Proactive SCO dispatched to {buyer_name} at USD {anchor_cif:.2f}/MT CIF. Discovered {len(buyers)} buyers and {len(suppliers)} suppliers.",
        "latest_email": "",
        "active_role": "buyer",
        "buyer_draft": initial_buyer_draft,
        "supplier_draft": "",
        "audit_transcript": [outbound_sco_entry],
    }
    CAMPAIGN_LEDGER[campaign_id] = initial_state

    # If auto_run is requested, execute the entire negotiation lifecycle end-to-end automatically
    if req.auto_run:
        return run_full_autonomous_campaign(campaign_id)

    return {
        "campaign_id": campaign_id,
        "commodity": campaign.commodity,
        "benchmark_fob_usd": benchmark_fob,
        "freight_cost_usd": freight,
        "dynamic_fob_ceiling": bounds["dynamic_fob_ceiling"],
        "dynamic_cif_floor": bounds["dynamic_cif_floor"],
        "anchor_cif_usd": anchor_cif,
        "buyer_draft": initial_buyer_draft,
        "pipeline_step": 1,
        "min_profit_per_mt_hard": campaign.min_profit_per_mt_hard,
        "min_profit_per_mt_soft": campaign.min_profit_per_mt_soft,
        "max_negotiation_rounds": campaign.max_negotiation_rounds,
        "discovered_buyers": [b.model_dump() for b in buyers],
        "discovered_suppliers": [s.model_dump() for s in suppliers],
        "audit_transcript": initial_state["audit_transcript"],
        "status": "initialized",
    }


def run_full_autonomous_campaign(campaign_id: str) -> dict:
    """
    Autonomous Multi-Turn Runner:
    Executes the entire physical commodity arbitrage negotiation lifecycle end-to-end:
    - Step 1: Initialize the state with buyer & supplier selected from app/directory.py.
    - Step 2: Ingest initial buyer interest (realistic trade fixture with healthy initial counter-bid).
    - Step 3: Loop through negotiation rounds automatically:
      * Run trade_graph.invoke() for each turn.
      * Ingest supplier quotation against auto-drafted RFQ.
      * Evaluate strategy with evaluate_deal_strategy().
      * If action == "COUNTER_TO_MAXIMIZE", feed tactical concessions into next turn automatically.
      * If action == "ACCEPT_AND_CLOSE", lock supplier allocation first (Zero-Risk Invariant),
        close deal, and break the loop.
    - Store full correspondence audit transcript and final metrics in the ledger.
    """
    state = CAMPAIGN_LEDGER.get(campaign_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    campaign: Campaign = state["campaign"]
    commodity = campaign.commodity
    volume = campaign.target_volume_mt
    dest_port = campaign.destination_port
    origin_port = campaign.origin_port_default
    buffer_usd = campaign.buffer_usd_per_mt if campaign.buffer_usd_per_mt is not None else settings.default_buffer_usd_per_mt

    # Step 1: Select counterparties from directory
    buyers = get_buyers_for_commodity(commodity)
    suppliers = get_suppliers_for_commodity(commodity)
    me_buyers = [b for b in buyers if b.country in ["UAE", "Saudi Arabia", "Qatar", "Kuwait", "Oman", "Bahrain"]]
    primary_buyer = me_buyers[0] if me_buyers else (buyers[0] if buyers else None)
    target_supplier = suppliers[0] if suppliers else None

    buyer_name = primary_buyer.name if primary_buyer else "Procurement Partner"
    buyer_email = primary_buyer.contact_email if primary_buyer else "procurement@domain.com"
    supp_name = target_supplier.name if target_supplier else "Asian Rice Exporters"
    supp_email = target_supplier.contact_email if target_supplier else "export@supplier.com"

    benchmark_fob = state["benchmark_fob_usd"]
    freight = state["freight_cost_usd"]
    landed_cost_baseline = round(benchmark_fob + freight + buffer_usd, 2)

    if "audit_transcript" not in state or not state["audit_transcript"]:
        state["audit_transcript"] = [{
            "turn": 0,
            "sender": f"Trading Desk ({settings.desk_name})",
            "recipient": f"{buyer_name} <{buyer_email}>",
            "role": "agent",
            "action": "OUTBOUND_SCO",
            "subject": f"Soft Corporate Offer (SCO) — {commodity} CIF {dest_port}",
            "message": state.get("buyer_draft", ""),
        }]

    # Step 2: Ingest initial buyer interest (realistic trade fixture with firm counter-bid)
    initial_buyer_cif = round(landed_cost_baseline + campaign.min_profit_per_mt_hard + 30.0, 2)
    buyer_interest_email = (
        f"Subject: Re: Soft Corporate Offer (SCO) — {commodity} CIF {dest_port}\n\n"
        f"To: Trading Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
        f"From: {buyer_name} <{buyer_email}>\n\n"
        f"Dear Trading Desk,\n\n"
        f"We acknowledge receipt of your SCO. We are interested in contracting {volume:,.0f} MT of {commodity} for {dest_port}. "
        f"However, your indicative pitch is above our procurement budget. "
        f"We submit a firm counter-bid of USD {initial_buyer_cif:.2f}/MT CIF {dest_port}. Payment via 100% LC at sight.\n\n"
        f"Best regards,\nProcurement Team, {buyer_name}"
    )

    state["latest_email"] = buyer_interest_email
    state["active_role"] = "buyer"
    state = trade_graph.invoke(state)

    state["audit_transcript"].append({
        "turn": 1,
        "sender": f"{buyer_name} <{buyer_email}>",
        "recipient": f"Trading Desk ({settings.desk_name})",
        "role": "buyer",
        "action": "INBOUND_BID",
        "subject": f"Re: SCO — {commodity} CIF {dest_port}",
        "message": buyer_interest_email,
    })

    # Outbound RFQ to supplier auto-drafted during Turn 1
    rfq_msg = state.get("supplier_draft", "")
    state["audit_transcript"].append({
        "turn": 1,
        "sender": f"Procurement Desk ({settings.desk_name})",
        "recipient": f"{supp_name} <{supp_email}>",
        "role": "agent",
        "action": "OUTBOUND_RFQ",
        "subject": f"Urgent RFQ — {commodity} FOB {origin_port}",
        "message": rfq_msg,
    })

    # Step 3: Loop through negotiation rounds automatically
    # Ingest supplier quotation against auto-drafted RFQ
    supplier_fob = benchmark_fob
    supplier_quote_email = (
        f"Subject: Quotation — {commodity} FOB {origin_port}\n\n"
        f"To: Procurement Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
        f"From: {supp_name} <{supp_email}>\n\n"
        f"Dear Procurement Desk,\n\n"
        f"In response to your RFQ, we quote {volume:,.0f} MT of export grade {commodity} at "
        f"USD {supplier_fob:.2f}/MT FOB {origin_port}. Payment terms: 100% LC at sight. Ready for prompt loading.\n\n"
        f"Best regards,\nExport Sales, {supp_name}"
    )

    state["latest_email"] = supplier_quote_email
    state["active_role"] = "supplier"
    state = trade_graph.invoke(state)

    curr_round = state.get("negotiation_round", 2)
    state["audit_transcript"].append({
        "turn": curr_round,
        "sender": f"{supp_name} <{supp_email}>",
        "recipient": f"Procurement Desk ({settings.desk_name})",
        "role": "supplier",
        "action": "INBOUND_QUOTE",
        "subject": f"Quotation — {commodity} FOB {origin_port}",
        "message": supplier_quote_email,
    })

    # Multi-turn concession loop until deal is closed or rejected
    while state.get("deal_status") not in ["closed", "rejected"]:
        action = state.get("action")
        if action == "ACCEPT_AND_CLOSE":
            break
        elif action == "REJECT_HARD":
            break
        elif action == "COUNTER_TO_MAXIMIZE":
            # Record outbound counter message
            counter_draft = state.get("buyer_draft") if state.get("active_role") == "buyer" else state.get("supplier_draft")
            state["audit_transcript"].append({
                "turn": state.get("negotiation_round", curr_round),
                "sender": f"Trading Desk ({settings.desk_name})",
                "recipient": f"{buyer_name} <{buyer_email}>",
                "role": "agent",
                "action": "OUTBOUND_COUNTER",
                "subject": f"Negotiation Round {state.get('negotiation_round')}: Tactical Adjustment Request",
                "message": counter_draft or f"Tactical counter to maximize margin: {state.get('evaluation_reason')}",
            })

            # Tactical concession: Buyer agrees to target CIF price that clears soft target ($120/MT)
            target_buyer_cif = round(landed_cost_baseline + campaign.min_profit_per_mt_soft, 2)
            concession_email = (
                f"Subject: Re: Price Adjustment Request — Concession Agreement\n\n"
                f"To: Trading Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
                f"From: {buyer_name} <{buyer_email}>\n\n"
                f"Dear Trading Desk,\n\n"
                f"Following your counter-proposal and updated corridor freight analysis, we agree to revise our CIF bid "
                f"to USD {target_buyer_cif:.2f}/MT CIF {dest_port} for {volume:,.0f} MT.\n\n"
                f"Please confirm allocation lock and issue the final SCO acceptance.\n\n"
                f"Best regards,\nProcurement Team, {buyer_name}"
            )

            state["latest_email"] = concession_email
            state["active_role"] = "buyer"
            state = trade_graph.invoke(state)

            curr_round = state.get("negotiation_round", curr_round + 1)
            state["audit_transcript"].append({
                "turn": curr_round,
                "sender": f"{buyer_name} <{buyer_email}>",
                "recipient": f"Trading Desk ({settings.desk_name})",
                "role": "buyer",
                "action": "INBOUND_CONCESSION",
                "subject": f"Re: Price Adjustment Request — Concession Agreement",
                "message": concession_email,
            })
        else:
            break

    # If deal reached closing terms, record the final correspondence locking supplier and accepting buyer
    if state.get("action") == "ACCEPT_AND_CLOSE" or state.get("deal_status") == "closed":
        # 1. Lock supplier allocation first (Zero-Risk Invariant)
        state["audit_transcript"].append({
            "turn": state.get("negotiation_round", 3),
            "sender": f"Procurement Desk ({settings.desk_name})",
            "recipient": f"{supp_name} <{supp_email}>",
            "role": "agent",
            "action": "LOCK_SUPPLIER_ALLOCATION",
            "subject": f"Deal Confirmation & Volume Lock — {commodity}",
            "message": state.get("supplier_draft", ""),
        })
        # 2. Accept and close deal with buyer
        state["audit_transcript"].append({
            "turn": state.get("negotiation_round", 3),
            "sender": f"Trading Desk ({settings.desk_name})",
            "recipient": f"{buyer_name} <{buyer_email}>",
            "role": "agent",
            "action": "ACCEPT_AND_CLOSE",
            "subject": f"Soft Corporate Offer (SCO) Acceptance — {commodity}",
            "message": state.get("buyer_draft", ""),
        })

    # Store finalized state into the ledger
    CAMPAIGN_LEDGER[campaign_id] = state

    buyer_t = state.get("buyer_terms")
    supp_t = state.get("supplier_terms")

    return {
        "campaign_id": campaign_id,
        "commodity": campaign.commodity,
        "deal_status": state.get("deal_status", "closed"),
        "negotiation_rounds_completed": state.get("negotiation_round", 0),
        "final_net_spread_usd": state.get("net_spread_usd", 0.0),
        "final_net_margin_pct": state.get("net_margin_pct", 0.0),
        "action": state.get("action", "ACCEPT_AND_CLOSE"),
        "is_deal_viable": state.get("is_deal_viable", True),
        "pipeline_step": state.get("pipeline_step", 4),
        "evaluation_reason": state.get("evaluation_reason", ""),
        "audit_transcript": state.get("audit_transcript", []),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": state.get("buyer_draft"),
        "supplier_draft": state.get("supplier_draft"),
        "benchmark_fob_usd": state.get("benchmark_fob_usd"),
        "freight_cost_usd": state.get("freight_cost_usd"),
        "dynamic_fob_ceiling": state.get("dynamic_fob_ceiling"),
        "dynamic_cif_floor": state.get("dynamic_cif_floor"),
        "anchor_cif_usd": state.get("anchor_cif_usd"),
        "discovered_buyers": [b.model_dump() for b in buyers],
        "discovered_suppliers": [s.model_dump() for s in suppliers],
        "status": state.get("deal_status", "closed"),
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
        "action": updated_state.get("action"),
        "pipeline_step": updated_state.get("pipeline_step", 1),
        "is_deal_viable": updated_state.get("is_deal_viable"),
        "net_spread_usd": updated_state.get("net_spread_usd", 0.0),
        "net_margin_pct": updated_state.get("net_margin_pct"),
        "evaluation_reason": updated_state.get("evaluation_reason"),
        "target_fob_ceiling": updated_state.get("target_fob_ceiling", 0.0),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": updated_state.get("buyer_draft"),
        "supplier_draft": updated_state.get("supplier_draft"),
        "audit_transcript": updated_state.get("audit_transcript", []),
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
        "min_profit_per_mt_hard": state["campaign"].min_profit_per_mt_hard,
        "min_profit_per_mt_soft": state["campaign"].min_profit_per_mt_soft,
        "max_negotiation_rounds": state["campaign"].max_negotiation_rounds,
        "negotiation_round": state.get("negotiation_round", 0),
        "negotiation_rounds_completed": state.get("negotiation_round", 0),
        "deal_status": state.get("deal_status"),
        "action": state.get("action"),
        "pipeline_step": state.get("pipeline_step", 1),
        "is_deal_viable": state.get("is_deal_viable"),
        "net_spread_usd": state.get("net_spread_usd", 0.0),
        "final_net_spread_usd": state.get("net_spread_usd", 0.0),
        "net_margin_pct": state.get("net_margin_pct"),
        "final_net_margin_pct": state.get("net_margin_pct"),
        "evaluation_reason": state.get("evaluation_reason"),
        "dynamic_fob_ceiling": state.get("dynamic_fob_ceiling"),
        "dynamic_cif_floor": state.get("dynamic_cif_floor"),
        "target_fob_ceiling": state.get("target_fob_ceiling", 0.0),
        "anchor_cif_usd": state.get("anchor_cif_usd", 0.0),
        "benchmark_fob_usd": state.get("benchmark_fob_usd"),
        "freight_cost_usd": state.get("freight_cost_usd"),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": state.get("buyer_draft"),
        "supplier_draft": state.get("supplier_draft"),
        "audit_transcript": state.get("audit_transcript", []),
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
