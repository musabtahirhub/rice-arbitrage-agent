"""
Campaign domain models and thread status literals.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class CampaignConfig(BaseModel):
    """
    Immutable parameters defining the boundaries of a single arbitrage campaign.
    Price boundaries are expressed as relative tolerances against live benchmarks.
    """

    campaign_id: str = Field(
        ...,
        description="Unique identifier for this arbitrage campaign.",
    )
    commodity: str = Field(
        ...,
        description="Commodity being traded, e.g. 'Basmati 1121 Sella Rice 5% broken'.",
    )
    target_profit_margin_pct: float = Field(
        ...,
        description="Minimum acceptable net profit margin as a percentage.",
    )
    max_acceptable_variance_from_benchmark_pct: float = Field(
        default=5.0,
        description="Maximum acceptable premium over FOB benchmark index for buy side (%).",
    )
    min_sell_margin_above_benchmark_pct: float = Field(
        default=2.0,
        description="Minimum acceptable margin above landed CIF benchmark for sell side (%).",
    )
    benchmark_index_name: str = Field(
        default="basmati_1121",
        description="Canonical slug identifying the market benchmark index.",
    )


# Thread status types for parallel negotiation tracking
BuyerThreadStatus = Literal[
    "awaiting_inquiry",
    "outreach_sent",
    "inquiry_parsed",
    "counter_sent",
    "terms_accepted",
    "rejected",
]

SupplierThreadStatus = Literal[
    "awaiting_quote",
    "rfq_sent",
    "quote_parsed",
    "counter_sent",
    "allocation_locked",
    "rejected",
]

DealStatus = Literal[
    "prospecting",
    "evaluating",
    "negotiating_buyer",
    "negotiating_supplier",
    "approved",
    "closed",
    "rejected",
]
