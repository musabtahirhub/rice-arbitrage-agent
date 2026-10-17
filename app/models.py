"""Domain models shared by the graph, services and API.

Campaign and ParsedEmail are stored in LangGraph checkpoints, which allow-list them by module path
("app.models", see app/agents/graph.py). Keep them in this module so existing checkpoints still load.
"""
from typing import Optional

from pydantic import BaseModel, Field

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


class AuditTranscriptEntry(BaseModel):
    """A single entry in the audit_transcript list."""

    turn: int = 0
    sender: str = ""
    recipient: str = ""
    role: str = ""
    action: str = ""
    subject: str = ""
    message: str = ""
    id: Optional[str] = None
