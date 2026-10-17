"""Domain models, graph state (DealState) and API request bodies.

Campaign and ParsedEmail are stored in LangGraph checkpoints, which allow-list them by module path
("app.models", see app/graph.py). Keep them in this module so existing checkpoints still load.
"""
import re
from typing import Any, Literal, Optional, TypedDict

from pydantic import BaseModel, Field, field_validator

from app.config import settings

# Deal statuses after which a campaign takes no further inbound email.
CLOSED_DEAL_STATUSES = ("closed", "rejected")


class Campaign(BaseModel):
    campaign_id: str
    commodity: str = Field(default_factory=lambda: settings.default_commodity)
    target_volume_mt: float = Field(default_factory=lambda: settings.default_target_volume_mt, gt=0)
    target_margin_pct: float = Field(default_factory=lambda: settings.default_target_margin_pct, ge=0.0)
    max_variance_from_benchmark_pct: float = Field(default_factory=lambda: settings.default_max_variance_pct, ge=0.0)
    buffer_usd_per_mt: float = Field(default_factory=lambda: settings.default_buffer_usd_per_mt, ge=0.0)
    destination_port: str = Field(default_factory=lambda: settings.default_destination_port)
    origin_port_default: str = Field(default_factory=lambda: settings.default_origin_port)
    broken_percentage: float = Field(default_factory=lambda: settings.default_broken_percentage, ge=0.0)
    min_profit_per_mt_hard: float = Field(default_factory=lambda: settings.default_min_profit_per_mt_hard, ge=0.0)
    min_profit_per_mt_soft: float = Field(default_factory=lambda: settings.default_min_profit_per_mt_soft, ge=0.0)
    max_negotiation_rounds: int = Field(default_factory=lambda: settings.default_max_negotiation_rounds, ge=1)


class ParsedEmail(BaseModel):
    sender_role: str
    commodity: str = Field(default_factory=lambda: settings.default_commodity)
    quantity_mt: float = Field(default_factory=lambda: settings.default_target_volume_mt, gt=0)
    price_usd_per_mt: float = Field(..., ge=0)
    incoterm: str = Field(default="CIF")
    port: Optional[str] = Field(default=None)
    payment_terms: Optional[str] = Field(default_factory=lambda: settings.default_payment_terms)
    intent: Optional[str] = Field(default="COUNTER_OFFER")
    is_acceptance: bool = Field(default=False)
    summary: Optional[str] = Field(default=None)


class Counterparty(BaseModel):
    id: str
    name: str
    role: str
    country: str
    primary_port: str
    preferred_commodities: list[str]
    contact_email: str
    reputation_score: float = Field(default=4.5, ge=1.0, le=5.0)


# Statuses that mean outreach already happened, so a turn without an inbound email must not pitch again.
OUTREACH_STARTED_STATUSES = ("prospecting", "counter_sent", "approved", "closed", "rejected")


class DealState(TypedDict, total=False):
    campaign: Campaign
    benchmark_fob_usd: float
    freight_cost_usd: float
    dynamic_fob_ceiling: float
    dynamic_cif_floor: float
    target_fob_ceiling: float
    anchor_cif_usd: float
    buyer_terms: Optional[ParsedEmail]
    supplier_terms: Optional[ParsedEmail]
    negotiation_round: int
    deal_status: str
    action: Optional[Literal["REJECT_HARD", "COUNTER_TO_MAXIMIZE", "ACCEPT_AND_CLOSE"]]
    pipeline_step: int
    is_deal_viable: bool
    net_spread_usd: float
    net_margin_pct: float
    evaluation_reason: str
    latest_email: str
    active_role: str
    buyer_draft: str
    supplier_draft: str
    audit_transcript: list[dict]
    thread_subject: Optional[str]
    last_buyer_message_id: Optional[str]
    buyer_references: Optional[str]
    last_supplier_message_id: Optional[str]
    supplier_references: Optional[str]
    supplier_thread_subject: Optional[str]
    last_counter_cif_usd: Optional[float]
    last_counter_fob_usd: Optional[float]
    buyer_accepted: Optional[bool]
    inbound_intent: Optional[str]
    target_buyer_name: Optional[str]
    target_buyer_email: Optional[str]
    target_supplier_name: Optional[str]
    target_supplier_email: Optional[str]
    gmail_thread_id: Optional[str]
    discovered_buyers: Optional[list[dict]]
    discovered_suppliers: Optional[list[dict]]
    trigger: Optional[str]
    skip_email_dispatch: Optional[bool]
    deal_approved_by_human: Optional[bool]
    reviewer_notes: Optional[str]
    # One-shot human override for the next buyer counter; consumed (reset to None) by counter_buyer_node.
    override_cif_price: Optional[float]


def coerce_campaign(value: Any) -> Campaign:
    """Checkpointed state may hold a dict in place of the model after serialization."""
    return Campaign(**value) if isinstance(value, dict) else value


def coerce_terms(value: Any) -> Optional[ParsedEmail]:
    return ParsedEmail(**value) if isinstance(value, dict) else value


def campaign_id_of(state: DealState) -> str:
    campaign = state.get("campaign")
    if isinstance(campaign, dict):
        return campaign.get("campaign_id") or "unknown"
    return getattr(campaign, "campaign_id", None) or "unknown"


def is_outreach_started(state: DealState) -> bool:
    return state.get("pipeline_step", 0) >= 1 or state.get("deal_status") in OUTREACH_STARTED_STATUSES


class ApprovalPayload(BaseModel):
    approved: bool
    override_cif_price: Optional[float] = None
    reviewer_notes: Optional[str] = None


class CreateCampaignRequest(BaseModel):
    commodity: str = Field(default_factory=lambda: settings.default_commodity)
    target_volume_mt: float = Field(default_factory=lambda: settings.default_target_volume_mt)
    target_margin_pct: float = Field(default_factory=lambda: settings.default_target_margin_pct)
    max_variance_from_benchmark_pct: float = Field(default_factory=lambda: settings.default_max_variance_pct)
    destination_port: str = Field(default_factory=lambda: settings.default_destination_port)
    min_profit_per_mt_hard: float = Field(default_factory=lambda: settings.default_min_profit_per_mt_hard)
    min_profit_per_mt_soft: float = Field(default_factory=lambda: settings.default_min_profit_per_mt_soft)
    max_negotiation_rounds: int = Field(default_factory=lambda: settings.default_max_negotiation_rounds)
    auto_run: bool = Field(default=False)
    # Saved contacts (POST /api/counterparties) to negotiate with. None = test inbox, then the directory.
    buyer_id: Optional[str] = None
    supplier_id: Optional[str] = None


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CreateCounterpartyRequest(BaseModel):
    role: Literal["buyer", "supplier"]
    name: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., max_length=320)
    country: Optional[str] = Field(default=None, max_length=100)
    port: Optional[str] = Field(default=None, max_length=100)

    @field_validator("name", "country", "port")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        value = value.strip() if value else value
        return value or None

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("enter a valid email address")
        return value


class SimulateTurnRequest(BaseModel):
    campaign_id: str
    sender_role: str
    raw_email: str
