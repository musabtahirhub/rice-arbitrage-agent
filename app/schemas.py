"""Pydantic models for graph node state updates and the human-in-the-loop interrupt/resume payloads.

DealState stays a TypedDict (required by StateGraph); every node builds its return value from one of
these models and returns ``model.model_dump(...)``.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.services.pricing import DealAction


class ProactiveOutreachUpdate(BaseModel):
    """State update returned by ``proactive_outreach_node``."""

    model_config = {"arbitrary_types_allowed": True}

    campaign: Any  # Campaign instance, re-attached after dumping so it stays a model
    benchmark_fob_usd: float
    freight_cost_usd: float
    dynamic_fob_ceiling: float
    dynamic_cif_floor: float
    target_fob_ceiling: float = 0.0
    anchor_cif_usd: float
    buyer_terms: None = None
    supplier_terms: None = None
    negotiation_round: int = 0
    deal_status: str = "prospecting"
    action: None = None
    pipeline_step: int = 1
    is_deal_viable: bool = False
    net_spread_usd: float = 0.0
    net_margin_pct: float = 0.0
    evaluation_reason: str = ""
    buyer_draft: str = ""
    supplier_draft: str = ""
    audit_transcript: list[dict[str, Any]] = Field(default_factory=list)
    thread_subject: Optional[str] = None
    last_buyer_message_id: Optional[str] = None
    buyer_references: Optional[str] = None
    last_supplier_message_id: None = None
    supplier_references: None = None
    discovered_buyers: list[dict[str, Any]] = Field(default_factory=list)
    discovered_suppliers: list[dict[str, Any]] = Field(default_factory=list)
    target_buyer_email: Optional[str] = None
    target_buyer_name: Optional[str] = None


class ParsedBuyerTermsUpdate(BaseModel):
    """State update after parsing a buyer email."""

    model_config = {"arbitrary_types_allowed": True}

    buyer_terms: Any  # ParsedEmail instance
    pipeline_step: int = 2
    buyer_accepted: bool = False
    target_fob_ceiling: Optional[float] = None
    supplier_draft: Optional[str] = None


class ParsedSupplierTermsUpdate(BaseModel):
    """State update after parsing a supplier email."""

    model_config = {"arbitrary_types_allowed": True}

    supplier_terms: Any  # ParsedEmail instance
    pipeline_step: int = 3


class FetchMarketDataUpdate(BaseModel):
    benchmark_fob_usd: float
    freight_cost_usd: float
    dynamic_fob_ceiling: float
    dynamic_cif_floor: float


class EvaluateRiskUpdate(BaseModel):
    is_deal_viable: bool
    action: Optional[DealAction] = None
    net_spread_usd: float = 0.0
    net_margin_pct: float = 0.0
    evaluation_reason: str = ""
    negotiation_round: int = 1


class CounterBuyerUpdate(BaseModel):
    deal_status: str = "counter_sent"
    buyer_draft: str = ""
    last_counter_cif_usd: Optional[float] = None
    override_cif_price: Optional[float] = None  # always dumped as None to consume a one-shot override


class CounterSupplierUpdate(BaseModel):
    deal_status: str = "counter_sent"
    supplier_draft: str = ""


class ConfirmDealUpdate(BaseModel):
    deal_status: str = "closed"
    pipeline_step: int = 4
    action: str = "ACCEPT_AND_CLOSE"
    supplier_draft: str = ""
    buyer_draft: str = ""


class RejectDealUpdate(BaseModel):
    deal_status: str = "rejected"
    is_deal_viable: bool = False
    action: str = "REJECT_HARD"
    buyer_draft: Optional[str] = None
    supplier_draft: Optional[str] = None


class ApprovalReviewPayload(BaseModel):
    """Sent into ``interrupt()`` by ``approval_gate_node`` for the human reviewer."""

    campaign_id: str
    commodity: str
    volume_mt: float
    buyer_bid_cif: float
    supplier_ask_fob: float
    freight_cost_usd: float
    net_spread_usd: float
    net_margin_pct: float
    total_deal_value: float


class ApprovalResumeDecision(BaseModel):
    """The human decision, resumed via ``Command(resume=...)``."""

    approved: Optional[bool] = None
    override_cif_price: Optional[float] = None
    reviewer_notes: Optional[str] = None
