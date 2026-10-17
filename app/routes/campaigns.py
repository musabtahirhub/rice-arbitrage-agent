"""Campaign routes: launch, status, counterparty directory, and the bundled UI."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import STATIC_DIR, settings
from app.db.database import get_db
from app.db.db_models import CampaignModel, CounterpartyModel, TradeAuditModel, add_trade_audit, desk_party
from app.directory import get_all_counterparties
from app.graph import workflow
from app.logger import get_logger
from app.models import Campaign, CreateCampaignRequest, CreateCounterpartyRequest, DealState
from app.schemas import (
    AuditTranscriptEntry,
    CampaignStatusResponse,
    CounterpartyResponse,
    CreateCampaignResponse,
    model_from_state,
)
from app.services.campaign_runner import run_full_autonomous_campaign
from app.services.mail.email_utils import get_desk_email_addresses, split_subject_and_body
from app.telemetry import build_trace_config, flush_telemetry, get_langfuse_handler

logger = get_logger("arbitrage_desk")
router = APIRouter()


def _audit_to_transcript_entry(turn: int, audit: TradeAuditModel) -> dict:
    message = audit.raw_message or ""
    subject = split_subject_and_body(message, default_subject="")[0] if message.lower().startswith("subject:") else ""
    inbound = audit.direction == "INBOUND"
    entry = AuditTranscriptEntry(
        turn=turn,
        sender=audit.role if inbound else desk_party(),
        recipient=desk_party() if inbound else "counterparty",
        role=audit.role,
        action=audit.direction,
        subject=subject,
        message=message,
    )
    return entry.model_dump(exclude_none=True)


def _load_or_reconstruct_transcript(state: dict, campaign_id: str, db: Session) -> list:
    """The most complete history available.

    Graph state only holds entries written by graph nodes (e.g. the opening SCO), while every API and
    email turn is recorded in trade_audits. Use whichever source has more entries.
    """
    state_transcript = state.get("audit_transcript") or []
    audits = (
        db.query(TradeAuditModel)
        .filter(TradeAuditModel.campaign_id == campaign_id)
        .order_by(TradeAuditModel.id.asc())
        .all()
    )
    if len(state_transcript) >= len(audits):
        return state_transcript
    return [_audit_to_transcript_entry(turn, audit) for turn, audit in enumerate(audits)]


_CAMPAIGN_FIELDS = (
    "commodity", "target_volume_mt", "target_margin_pct", "max_variance_from_benchmark_pct", "destination_port",
    "min_profit_per_mt_hard", "min_profit_per_mt_soft", "max_negotiation_rounds",
)


@router.get("/api/directory")
def list_directory():
    return get_all_counterparties()


def _counterparty_response(row: CounterpartyModel) -> dict:
    return CounterpartyResponse(
        id=row.id, role=row.role, name=row.name, email=row.email, country=row.country, port=row.port
    ).model_dump()


@router.get("/api/counterparties")
def list_counterparties(db: Session = Depends(get_db)):
    """The desk's saved contact book, split by role."""
    rows = db.query(CounterpartyModel).order_by(CounterpartyModel.created_at.asc()).all()
    return {
        "buyers": [_counterparty_response(r) for r in rows if r.role == "buyer"],
        "suppliers": [_counterparty_response(r) for r in rows if r.role == "supplier"],
    }


@router.post("/api/counterparties", status_code=201)
def create_counterparty(req: CreateCounterpartyRequest, db: Session = Depends(get_db)):
    if req.email in get_desk_email_addresses():
        raise HTTPException(status_code=422, detail="That is the desk's own address; add the counterparty's email.")
    # One address, one role: replies are classified buyer/supplier by sender address, so an address saved
    # as both could never be told apart.
    existing = db.query(CounterpartyModel).filter(CounterpartyModel.email == req.email).first()
    if existing and existing.role == req.role:
        raise HTTPException(status_code=409, detail=f"A {req.role} with email {req.email} is already saved.")
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"{req.email} is already saved as a {existing.role} ({existing.name}). "
            f"A buyer and a supplier cannot share an email address.",
        )
    row = CounterpartyModel(id=f"CP-{uuid.uuid4().hex[:6].upper()}", **req.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(f"[CONTACTS] Saved {row.role} '{row.name}' <{row.email}> as {row.id}")
    return _counterparty_response(row)


@router.delete("/api/counterparties/{counterparty_id}")
def delete_counterparty(counterparty_id: str, db: Session = Depends(get_db)):
    """Remove a contact from the book. Campaigns already launched with it keep their copy in state."""
    row = db.query(CounterpartyModel).filter(CounterpartyModel.id == counterparty_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Contact {counterparty_id} not found.")
    db.delete(row)
    db.commit()
    logger.info(f"[CONTACTS] Deleted {row.role} '{row.name}' <{row.email}> ({counterparty_id})")
    return {"deleted": counterparty_id}


def _saved_contact(contact_id: Optional[str], role: str, db: Session) -> Optional[CounterpartyModel]:
    if not contact_id:
        return None
    row = db.query(CounterpartyModel).filter(CounterpartyModel.id == contact_id).first()
    if not row or row.role != role:
        raise HTTPException(status_code=422, detail=f"No saved {role} with id {contact_id}.")
    return row


def _resolve_campaign_contacts(req: CreateCampaignRequest, db: Session) -> dict:
    """State fields for the picked buyer/supplier. Inbound mail from a supplier address is classified as
    supplier, so the picked buyer must not share an address with the supplier side."""
    buyer = _saved_contact(req.buyer_id, "buyer", db)
    supplier = _saved_contact(req.supplier_id, "supplier", db)
    buyer_addr = buyer.email if buyer else settings.my_test_email.strip().lower()
    supplier_addr = supplier.email if supplier else settings.my_test_supplier_email.strip().lower()
    if (buyer or supplier) and buyer_addr and buyer_addr == supplier_addr:
        raise HTTPException(
            status_code=422,
            detail="The buyer and supplier must use different email addresses, or replies cannot be told apart.",
        )
    contacts = {}
    if buyer:
        contacts.update(target_buyer_name=buyer.name, target_buyer_email=buyer.email)
    if supplier:
        contacts.update(target_supplier_name=supplier.name, target_supplier_email=supplier.email)
    return contacts


def _persist_new_campaign(req: CreateCampaignRequest, campaign_id: str, db: Session) -> tuple[Campaign, CampaignModel]:
    campaign = Campaign(campaign_id=campaign_id, **req.model_dump(include=set(_CAMPAIGN_FIELDS)))
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
    return campaign, db_campaign


def _launch_campaign_graph(campaign: Campaign, contacts: Optional[dict] = None) -> dict:
    """First graph turn (no inbound email): proactive outreach drafts and sends the opening SCO."""
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
        **(contacts or {}),
    }
    config = build_trace_config(
        campaign.campaign_id,
        "campaign-launch",
        get_langfuse_handler(),
        metadata={"commodity": campaign.commodity, "target_volume_mt": campaign.target_volume_mt},
        tags=[campaign.commodity],
    )
    return workflow.get_trade_graph().invoke(initial_state, config=config)


def _record_launch(campaign_id: str, db_campaign: CampaignModel, launched_state: dict, db: Session) -> None:
    db_campaign.deal_status = launched_state.get("deal_status", "prospecting")
    db_campaign.anchor_cif_usd = launched_state.get("anchor_cif_usd")
    if launched_state.get("buyer_draft"):
        add_trade_audit(db, campaign_id, "agent", launched_state["buyer_draft"], "OUTBOUND",
                        counterparty_price=launched_state.get("anchor_cif_usd"), net_spread=0.0)
    db.commit()


@router.post("/api/campaigns")
def create_campaign(req: CreateCampaignRequest, db: Session = Depends(get_db)):
    campaign_id = f"CAMP-{uuid.uuid4().hex[:6].upper()}"
    logger.info(
        f"[CAMPAIGN CREATE] Received request for '{req.commodity}' ({req.target_volume_mt:,.0f} MT) "
        f"-> Port: {req.destination_port} | Target Margin: {req.target_margin_pct}% | Auto-run: {req.auto_run}"
    )
    contacts = _resolve_campaign_contacts(req, db)
    campaign, db_campaign = _persist_new_campaign(req, campaign_id, db)
    launched = _launch_campaign_graph(campaign, contacts)
    _record_launch(campaign_id, db_campaign, launched, db)
    logger.info(
        f"[CAMPAIGN CREATED] ID='{campaign_id}' | Benchmark FOB: ${launched.get('benchmark_fob_usd', 0.0):.2f}/MT | "
        f"Freight: ${launched.get('freight_cost_usd', 0.0):.2f}/MT | "
        f"Dynamic FOB Ceiling: ${launched.get('dynamic_fob_ceiling', 0.0):.2f}/MT | "
        f"Anchor CIF: ${launched.get('anchor_cif_usd', 0.0):.2f}/MT"
    )

    if req.auto_run:
        logger.info(
            f"[AUTONOMOUS CAMPAIGN] Auto-running campaign '{campaign_id}' through full negotiation lifecycle..."
        )
        res = run_full_autonomous_campaign(campaign_id, db=db)
        flush_telemetry()
        logger.info(f"[AUTONOMOUS CAMPAIGN] Campaign '{campaign_id}' completed with status: {res.get('deal_status')}")
        return res

    resp = model_from_state(
        CreateCampaignResponse, launched,
        campaign_id=campaign_id,
        commodity=campaign.commodity,
        min_profit_per_mt_hard=campaign.min_profit_per_mt_hard,
        min_profit_per_mt_soft=campaign.min_profit_per_mt_soft,
        max_negotiation_rounds=campaign.max_negotiation_rounds,
        status="initialized",
    )
    flush_telemetry()
    return resp.model_dump()


def _campaign_limits(state: dict) -> dict:
    """Commodity and margin limits from the checkpointed campaign, else the desk defaults."""
    campaign = state.get("campaign")
    if campaign:
        return {
            name: getattr(campaign, name)
            for name in (
                "commodity",
                "target_margin_pct",
                "min_profit_per_mt_hard",
                "min_profit_per_mt_soft",
                "max_negotiation_rounds",
            )
        }
    return {
        "target_margin_pct": settings.default_target_margin_pct,
        "min_profit_per_mt_hard": settings.default_min_profit_per_mt_hard,
        "min_profit_per_mt_soft": settings.default_min_profit_per_mt_soft,
        "max_negotiation_rounds": settings.default_max_negotiation_rounds,
    }


@router.get("/api/campaigns/{campaign_id}")
def get_campaign_status(campaign_id: str, db: Session = Depends(get_db)):
    db_camp = db.query(CampaignModel).filter(CampaignModel.id == campaign_id).first()
    if not db_camp:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    state = workflow.get_state_values({"configurable": {"thread_id": campaign_id}})
    limits = {"commodity": db_camp.commodity, **_campaign_limits(state)}
    resp = model_from_state(
        CampaignStatusResponse, state,
        **limits,
        campaign_id=campaign_id,
        negotiation_rounds_completed=state.get("negotiation_round", 0),
        deal_status=state.get("deal_status", db_camp.deal_status),
        final_net_spread_usd=state.get("net_spread_usd", 0.0),
        final_net_margin_pct=state.get("net_margin_pct"),
        anchor_cif_usd=state.get("anchor_cif_usd", db_camp.anchor_cif_usd or 0.0),
        audit_transcript=_load_or_reconstruct_transcript(state, campaign_id, db),
    )
    return resp.model_dump()


@router.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Commodity Arbitrage API is running. Visit /docs for Swagger documentation."}
