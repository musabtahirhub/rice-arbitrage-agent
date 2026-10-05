import logging
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.db_models import CampaignModel, TradeAuditModel
from app.directory import get_all_counterparties
from app.models import Campaign, CreateCampaignRequest, DealState
from app.workflow import trade_graph
from app.services.campaign_runner import run_full_autonomous_campaign

logger = logging.getLogger("arbitrage_desk")

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
if not STATIC_DIR.exists():
    STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("/api/directory")
def list_directory():
    return get_all_counterparties()


@router.post("/api/campaigns")
def create_campaign(req: CreateCampaignRequest, db: Session = Depends(get_db)):
    campaign_id = f"CAMP-{uuid.uuid4().hex[:6].upper()}"
    logger.info(
        f"[CAMPAIGN CREATE] Received request for '{req.commodity}' ({req.target_volume_mt:,.0f} MT) "
        f"-> Port: {req.destination_port} | Target Margin: {req.target_margin_pct}% | Auto-run: {req.auto_run}"
    )
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

    # Persist the new campaign row into CampaignModel via a SQLAlchemy session
    db_campaign = CampaignModel(
        id=campaign_id,
        commodity=req.commodity,
        target_volume_mt=req.target_volume_mt,
        destination_port=req.destination_port,
        origin_port_default=settings.default_origin_port,
        deal_status="initiating",
        anchor_cif_usd=None,
    )
    db.add(db_campaign)
    db.commit()
    db.refresh(db_campaign)

    initial_state: DealState = {
        "campaign": campaign,
        "negotiation_round": 0,
        "deal_status": "initiating",
        "action": None,
        "pipeline_step": 0,
        "is_deal_viable": False,
        "net_spread_usd": 0.0,
        "net_margin_pct": 0.0,
        "latest_email": "",
        "active_role": "buyer",
        "buyer_terms": None,
        "supplier_terms": None,
        "buyer_draft": "",
        "supplier_draft": "",
        "audit_transcript": [],
    }

    config = {"configurable": {"thread_id": campaign.campaign_id}}
    launched_state = trade_graph.invoke(initial_state, config=config)

    # Update deal status and anchor_cif_usd
    db_campaign.deal_status = launched_state.get("deal_status", "prospecting")
    db_campaign.anchor_cif_usd = launched_state.get("anchor_cif_usd")

    # Persist initial outbound SCO draft to TradeAuditModel
    if launched_state.get("buyer_draft"):
        sco_audit = TradeAuditModel(
            campaign_id=campaign_id,
            thread_id=campaign_id,
            role="agent",
            counterparty_price=launched_state.get("anchor_cif_usd"),
            net_spread=0.0,
            raw_message=launched_state.get("buyer_draft"),
            direction="OUTBOUND",
        )
        db.add(sco_audit)
    db.commit()

    logger.info(
        f"[CAMPAIGN CREATED] ID='{campaign_id}' | Benchmark FOB: ${launched_state.get('benchmark_fob_usd', 0.0):.2f}/MT | "
        f"Freight: ${launched_state.get('freight_cost_usd', 0.0):.2f}/MT | Dynamic FOB Ceiling: ${launched_state.get('dynamic_fob_ceiling', 0.0):.2f}/MT | "
        f"Anchor CIF: ${launched_state.get('anchor_cif_usd', 0.0):.2f}/MT"
    )

    if req.auto_run:
        logger.info(f"[AUTONOMOUS CAMPAIGN] Auto-running campaign '{campaign_id}' through full negotiation lifecycle...")
        res = run_full_autonomous_campaign(campaign_id, db=db)
        logger.info(f"[AUTONOMOUS CAMPAIGN] Campaign '{campaign_id}' completed with status: {res.get('deal_status')}")
        return res

    return {
        "campaign_id": campaign_id,
        "commodity": campaign.commodity,
        "benchmark_fob_usd": launched_state.get("benchmark_fob_usd"),
        "freight_cost_usd": launched_state.get("freight_cost_usd"),
        "dynamic_fob_ceiling": launched_state.get("dynamic_fob_ceiling"),
        "dynamic_cif_floor": launched_state.get("dynamic_cif_floor"),
        "anchor_cif_usd": launched_state.get("anchor_cif_usd"),
        "buyer_draft": launched_state.get("buyer_draft"),
        "pipeline_step": launched_state.get("pipeline_step", 1),
        "deal_status": launched_state.get("deal_status", "prospecting"),
        "min_profit_per_mt_hard": campaign.min_profit_per_mt_hard,
        "min_profit_per_mt_soft": campaign.min_profit_per_mt_soft,
        "max_negotiation_rounds": campaign.max_negotiation_rounds,
        "discovered_buyers": launched_state.get("discovered_buyers", []),
        "discovered_suppliers": launched_state.get("discovered_suppliers", []),
        "audit_transcript": launched_state.get("audit_transcript", []),
        "status": "initialized",
    }


@router.get("/api/campaigns/{campaign_id}")
def get_campaign_status(campaign_id: str, db: Session = Depends(get_db)):
    db_camp = db.query(CampaignModel).filter(CampaignModel.id == campaign_id).first()
    if not db_camp:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    config = {"configurable": {"thread_id": campaign_id}}
    snapshot = trade_graph.get_state(config)
    state = dict(snapshot.values) if snapshot and snapshot.values else {}

    audits = (
        db.query(TradeAuditModel)
        .filter(TradeAuditModel.campaign_id == campaign_id)
        .order_by(TradeAuditModel.id.asc())
        .all()
    )

    audit_transcript = state.get("audit_transcript") or []
    if not audit_transcript and audits:
        for a in audits:
            audit_transcript.append({
                "turn": 0,
                "sender": a.role,
                "recipient": "trading_desk",
                "role": a.role,
                "action": a.direction,
                "message": a.raw_message,
            })

    campaign_obj = state.get("campaign")
    commodity = campaign_obj.commodity if campaign_obj else db_camp.commodity
    target_margin_pct = campaign_obj.target_margin_pct if campaign_obj else settings.default_target_margin_pct
    min_profit_per_mt_hard = campaign_obj.min_profit_per_mt_hard if campaign_obj else 50.0
    min_profit_per_mt_soft = campaign_obj.min_profit_per_mt_soft if campaign_obj else 120.0
    max_negotiation_rounds = campaign_obj.max_negotiation_rounds if campaign_obj else 3

    buyer_t = state.get("buyer_terms")
    supp_t = state.get("supplier_terms")

    return {
        "campaign_id": campaign_id,
        "commodity": commodity,
        "target_margin_pct": target_margin_pct,
        "min_profit_per_mt_hard": min_profit_per_mt_hard,
        "min_profit_per_mt_soft": min_profit_per_mt_soft,
        "max_negotiation_rounds": max_negotiation_rounds,
        "negotiation_round": state.get("negotiation_round", 0),
        "negotiation_rounds_completed": state.get("negotiation_round", 0),
        "deal_status": state.get("deal_status", db_camp.deal_status),
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
        "anchor_cif_usd": state.get("anchor_cif_usd", db_camp.anchor_cif_usd or 0.0),
        "benchmark_fob_usd": state.get("benchmark_fob_usd"),
        "freight_cost_usd": state.get("freight_cost_usd"),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": state.get("buyer_draft"),
        "supplier_draft": state.get("supplier_draft"),
        "audit_transcript": audit_transcript,
    }


@router.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Commodity Arbitrage API is running. Visit /docs for Swagger documentation."}
