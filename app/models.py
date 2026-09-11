"""
Domain data models and schemas for the commodity arbitrage system.
"""
from typing import Optional, TypedDict
from pydantic import BaseModel, Field


class Campaign(BaseModel):
    """Configuration parameters for a back-to-back commodity arbitrage campaign."""
    campaign_id: str = Field(..., description="Unique campaign ID (e.g. CAMP-001)")
    commodity: str = Field(default="Basmati 1121", description="Commodity variety")
    target_volume_mt: float = Field(default=500.0, gt=0, description="Target volume in Metric Tons")
    target_margin_pct: float = Field(default=10.0, ge=0.0, description="Minimum acceptable net profit margin %")
    max_variance_from_benchmark_pct: float = Field(default=5.0, ge=0.0, description="Max acceptable variance from benchmark %")
    buffer_usd_per_mt: float = Field(default=20.0, ge=0.0, description="Operating buffer and financing cost per MT")
    destination_port: str = Field(default="Jebel Ali", description="Destination port for buyer delivery")
    origin_port_default: str = Field(default="Karachi", description="Default supplier origin port")
    broken_percentage: float = Field(default=5.0, ge=0.0, description="Max broken grain tolerance %")


class ParsedEmail(BaseModel):
    """Structured commercial terms extracted from incoming trade correspondence."""
    sender_role: str = Field(..., description="'buyer' or 'supplier'")
    commodity: str = Field(default="Basmati 1121")
    quantity_mt: float = Field(default=500.0, gt=0)
    price_usd_per_mt: float = Field(..., ge=0, description="Unit price quoted in USD per Metric Ton")
    incoterm: str = Field(default="CIF", description="'FOB' or 'CIF'")
    port: Optional[str] = Field(default=None, description="Port cited in the quote")
    payment_terms: Optional[str] = Field(default="LC", description="e.g., 'LC at sight', 'CAD'")


class Counterparty(BaseModel):
    """Buyer or supplier listed in the marketplace directory."""
    id: str
    name: str
    role: str  # 'buyer' or 'supplier'
    country: str
    primary_port: str
    preferred_commodities: list[str]
    contact_email: str
    reputation_score: float = Field(default=4.5, ge=1.0, le=5.0)


class DealState(TypedDict, total=False):
    """Complete LangGraph state dictionary shared across workflow nodes."""
    campaign: Campaign
    benchmark_fob_usd: float
    freight_cost_usd: float
    dynamic_fob_ceiling: float
    dynamic_cif_floor: float
    buyer_terms: Optional[ParsedEmail]
    supplier_terms: Optional[ParsedEmail]
    negotiation_round: int
    deal_status: str  # 'prospecting', 'counter_sent', 'approved', 'closed', 'rejected'
    is_deal_viable: bool
    net_margin_pct: float
    evaluation_reason: str
    latest_email: str
    active_role: str  # 'buyer' or 'supplier'
    buyer_draft: str
    supplier_draft: str


# API Request/Response Models
class CreateCampaignRequest(BaseModel):
    commodity: str = "Basmati 1121"
    target_volume_mt: float = 500.0
    target_margin_pct: float = 10.0
    max_variance_from_benchmark_pct: float = 5.0
    destination_port: str = "Jebel Ali"


class SimulateTurnRequest(BaseModel):
    campaign_id: str
    sender_role: str  # 'buyer' or 'supplier'
    raw_email: str
