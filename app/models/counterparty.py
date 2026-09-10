"""
Counterparty domain models for market directory participants.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class Counterparty(BaseModel):
    """
    Representation of a registered market participant in the commodity directory.
    """

    id: str = Field(..., description="Unique counterparty identifier, e.g. BUYER-GULF-01")
    name: str = Field(..., description="Company name")
    role: Literal["buyer", "supplier"] = Field(..., description="Role: 'buyer' or 'supplier'")
    country: str = Field(..., description="Country of registration")
    port: str = Field(..., description="Primary port (destination for buyers, origin for suppliers)")
    preferred_commodities: list[str] = Field(
        default_factory=list,
        description="Commodity varieties traded or produced",
    )
    contact_name: str = Field(..., description="Key contact person / title")
    contact_email: str = Field(..., description="Contact email for trade correspondence")
    typical_volume_mt: float = Field(default=500.0, description="Typical shipment volume in MT")
    reputation_score: float = Field(default=4.8, description="Trading reputation rating out of 5.0")
    payment_terms: str = Field(
        default="LC at Sight",
        description="Standard preferred payment mechanism",
    )
