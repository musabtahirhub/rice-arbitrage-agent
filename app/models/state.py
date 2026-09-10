"""
LangGraph TradeState domain model for multi-agent negotiation tracking.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field

from app.models.campaign import (
    BuyerThreadStatus,
    CampaignConfig,
    DealStatus,
    SupplierThreadStatus,
)
from app.models.trade import ParsedTradeEmail


class TradeState(BaseModel):
    """
    State passed through the LangGraph multi-agent orchestrator.
    Maintains parallel tracking for buyer and supplier threads.
    """

    # Identity
    campaign: CampaignConfig
    thread_id: str = Field(
        default="",
        description="Conversation / thread identifier for message continuity.",
    )

    # Raw input
    raw_email: str = Field(
        default="",
        description="The latest raw email text to be parsed.",
    )

    # Parsed counterparty terms
    buyer_terms: Optional[ParsedTradeEmail] = Field(
        default=None,
        description="Structured buyer inquiry extracted by the Buyer Agent.",
    )
    supplier_terms: Optional[ParsedTradeEmail] = Field(
        default=None,
        description="Structured supplier quote extracted by the Supplier Agent.",
    )

    # Per-agent thread status
    buyer_thread_status: BuyerThreadStatus = Field(
        default="awaiting_inquiry",
        description="Current state of the buyer-side negotiation thread.",
    )
    supplier_thread_status: SupplierThreadStatus = Field(
        default="awaiting_quote",
        description="Current state of the supplier-side negotiation thread.",
    )

    # Global deal status
    deal_status: DealStatus = Field(
        default="prospecting",
        description="Global deal status managed by the Orchestrator.",
    )
    negotiation_round: int = Field(
        default=0,
        description="Current negotiation round for multi-turn tracking.",
    )

    # Live market data
    benchmark_fob_usd: float = Field(
        default=0.0,
        description="FOB benchmark price fetched from market data service (USD/MT).",
    )
    benchmark_source: str = Field(
        default="",
        description="Source of the benchmark price: 'live' or 'fallback_cache'.",
    )
    freight_cost_usd: float = Field(
        default=0.0,
        description="Estimated container freight cost per MT (USD).",
    )
    freight_source: str = Field(
        default="",
        description="Source of the freight estimate: 'live' or 'fallback_cache'.",
    )

    # Deterministic evaluation results
    net_margin_pct: float = Field(
        default=0.0,
        description="Net profit margin calculated by the arbitrage engine.",
    )
    is_deal_viable: bool = Field(
        default=False,
        description="Whether the deal passes all deterministic boundary checks.",
    )
    evaluation_reason: str = Field(
        default="",
        description="Human-readable explanation from the arbitrage engine.",
    )

    # Drafted messages
    buyer_draft: str = Field(
        default="",
        description="Latest drafted message/SCO for the buyer.",
    )
    supplier_draft: str = Field(
        default="",
        description="Latest drafted message for the supplier.",
    )

    # Discovery & Outbound Campaigns
    discovered_buyers: list[dict] = Field(
        default_factory=list,
        description="Matching buyers discovered from the directory.",
    )
    discovered_suppliers: list[dict] = Field(
        default_factory=list,
        description="Matching suppliers discovered from the directory.",
    )
    buyer_outreach_drafts: dict[str, str] = Field(
        default_factory=dict,
        description="Personalized outbound cold outreach drafts per buyer ID.",
    )
    supplier_rfq_drafts: dict[str, str] = Field(
        default_factory=dict,
        description="Targeted RFQs drafted per supplier ID.",
    )
    primary_buyer: Optional[dict] = Field(
        default=None,
        description="Primary active buyer counterparty.",
    )
    primary_supplier: Optional[dict] = Field(
        default=None,
        description="Primary active supplier counterparty.",
    )
