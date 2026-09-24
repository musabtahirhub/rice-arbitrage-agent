from typing import Literal, Optional, TypedDict
from pydantic import BaseModel, Field

from app.config import settings


class Campaign(BaseModel):
    campaign_id: str = Field(..., description="Unique campaign ID (e.g. CAMP-001)")
    commodity: str = Field(default_factory=lambda: settings.default_commodity, description="Commodity variety")
    target_volume_mt: float = Field(default_factory=lambda: settings.default_target_volume_mt, gt=0, description="Target volume in Metric Tons")
    target_margin_pct: float = Field(default_factory=lambda: settings.default_target_margin_pct, ge=0.0, description="Minimum acceptable net profit margin %")
    max_variance_from_benchmark_pct: float = Field(default_factory=lambda: settings.default_max_variance_pct, ge=0.0, description="Max acceptable variance from benchmark %")
    buffer_usd_per_mt: float = Field(default_factory=lambda: settings.default_buffer_usd_per_mt, ge=0.0, description="Operating buffer and financing cost per MT")
    destination_port: str = Field(default_factory=lambda: settings.default_destination_port, description="Destination port for buyer delivery")
    origin_port_default: str = Field(default_factory=lambda: settings.default_origin_port, description="Default supplier origin port")
    broken_percentage: float = Field(default_factory=lambda: settings.default_broken_percentage, ge=0.0, description="Max broken grain tolerance %")
    min_profit_per_mt_hard: float = Field(default=50.0, ge=0.0, description="Non-negotiable floor; reject any deal yielding < $50/MT net spread")
    min_profit_per_mt_soft: float = Field(default=120.0, ge=0.0, description="Target ideal spread; push for price improvement if spread is below this")
    max_negotiation_rounds: int = Field(default=3, ge=1, description="Max bargaining rounds to attempt before settling at the hard floor")


class ParsedEmail(BaseModel):
    sender_role: str = Field(..., description="'buyer' or 'supplier'")
    commodity: str = Field(default="Basmati 1121")
    quantity_mt: float = Field(default=500.0, gt=0)
    price_usd_per_mt: float = Field(..., ge=0, description="Unit price quoted in USD per Metric Ton")
    incoterm: str = Field(default="CIF", description="'FOB' or 'CIF'")
    port: Optional[str] = Field(default=None, description="Port cited in the quote")
    payment_terms: Optional[str] = Field(default="LC", description="e.g., 'LC at sight', 'CAD'")
    intent: Optional[str] = Field(default="COUNTER_OFFER", description="'ACCEPTANCE', 'COUNTER_OFFER', 'REJECTION', 'INQUIRY'")
    is_acceptance: bool = Field(default=False, description="Whether sender accepts desk terms")
    summary: Optional[str] = Field(default=None, description="Semantic summary of message intent")


class Counterparty(BaseModel):
    id: str
    name: str
    role: str
    country: str
    primary_port: str
    preferred_commodities: list[str]
    contact_email: str
    reputation_score: float = Field(default=4.5, ge=1.0, le=5.0)


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
    last_counter_cif_usd: Optional[float]
    buyer_accepted: Optional[bool]
    target_buyer_name: Optional[str]
    target_buyer_email: Optional[str]
    discovered_buyers: Optional[list[dict]]
    discovered_suppliers: Optional[list[dict]]
    trigger: Optional[str]
    skip_email_dispatch: Optional[bool]


class CreateCampaignRequest(BaseModel):
    commodity: str = Field(default_factory=lambda: settings.default_commodity)
    target_volume_mt: float = Field(default_factory=lambda: settings.default_target_volume_mt)
    target_margin_pct: float = Field(default_factory=lambda: settings.default_target_margin_pct)
    max_variance_from_benchmark_pct: float = Field(default_factory=lambda: settings.default_max_variance_pct)
    destination_port: str = Field(default_factory=lambda: settings.default_destination_port)
    min_profit_per_mt_hard: float = Field(default=50.0)
    min_profit_per_mt_soft: float = Field(default=120.0)
    max_negotiation_rounds: int = Field(default=3)
    auto_run: bool = Field(default=True, description="Whether to run the entire negotiation lifecycle end-to-end automatically")


class SimulateTurnRequest(BaseModel):
    campaign_id: str
    sender_role: str
    raw_email: str
