"""
Trade term schemas and API request/response models.
"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class ParsedTradeEmail(BaseModel):
    """
    Structured representation of a single negotiation email extracted from raw text.
    """

    sender_role: Literal["buyer", "supplier"] = Field(
        ...,
        description="Whether the sender is a buyer or a supplier.",
    )
    commodity_type: str = Field(
        ...,
        description="Specific commodity mentioned, e.g. 'Jasmine Rice 5% broken'.",
    )
    quantity_mt: float = Field(
        ...,
        description="Quantity in metric tons.",
    )
    price_usd_per_mt: float = Field(
        ...,
        description="Quoted price in USD per metric ton.",
    )
    incoterm: Literal["FOB", "CIF", "CFR"] = Field(
        ...,
        description="Incoterm governing the quoted price.",
    )
    port: Optional[str] = Field(
        default=None,
        description="Port named in the email.",
    )


class CreateCampaignRequest(BaseModel):
    commodity: str = Field(
        default="Basmati 1121 Sella Rice 5% broken",
        description="Commodity variety and spec.",
    )
    target_volume_mt: float = Field(
        default=500.0,
        description="Target volume in metric tons.",
    )
    target_margin_pct: float = Field(
        default=5.0,
        description="Minimum acceptable profit margin %.",
    )
    max_buy_variance_pct: float = Field(
        default=5.0,
        description="Max acceptable % above FOB benchmark for buy side.",
    )
    min_sell_margin_pct: float = Field(
        default=2.0,
        description="Min acceptable % above CIF benchmark for sell side.",
    )
    benchmark_index: str = Field(
        default="basmati_1121",
        description="Market benchmark index slug.",
    )


class SimulateEmailRequest(BaseModel):
    campaign_id: str
    thread_id: str = Field(default="")
    raw_email: str
    sender_role: str = Field(..., description="'buyer' or 'supplier'")


class SimulateEmailResponse(BaseModel):
    extracted_terms: Optional[dict] = None
    deal_viable: bool = False
    margin_pct: float = 0.0
    evaluation_reason: str = ""
    drafted_response: str = ""
    campaign_status: str = "prospecting"
    benchmark_fob_usd: float = 0.0
    freight_cost_usd: float = 0.0
    buyer_thread_status: str = ""
    supplier_thread_status: str = ""
    negotiation_round: int = 0


class SimulateResponsesRequest(BaseModel):
    scenario: str = Field(
        default="viable",
        description="'viable', 'lowball_buyer', or 'high_supplier'",
    )
    buyer_id: Optional[str] = None
    supplier_id: Optional[str] = None
