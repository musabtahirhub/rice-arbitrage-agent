"""Pydantic V2 schemas for structured data flowing through the arbitrage desk.

Pricing results, graph node state updates, HITL interrupt/resume payloads, audit transcript entries and
API response models. DealState stays a TypedDict (required by StateGraph) in app/models.py.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, TypeVar

from pydantic import BaseModel, Field

# ── Pricing results ─────────────────────────────────────────────


DealAction = Literal["REJECT_HARD", "COUNTER_TO_MAXIMIZE", "ACCEPT_AND_CLOSE"]


class DynamicBounds(BaseModel):
    """Result of ``calculate_dynamic_bounds``."""

    dynamic_fob_ceiling: float
    landed_cost_at_ceiling: float
    dynamic_cif_floor: float


class DealEvaluation(BaseModel):
    """Result of ``evaluate_deal`` / ``evaluate_deal_strategy``."""

    viable: bool
    action: Optional[DealAction] = None
    net_spread: float = 0.0
    net_margin_pct: float = 0.0
    landed_cost: float = 0.0
    reason: str = ""
    # Dynamic bounds, merged in by evaluate_deal
    dynamic_fob_ceiling: Optional[float] = None
    landed_cost_at_ceiling: Optional[float] = None
    dynamic_cif_floor: Optional[float] = None


# ── Audit transcript ────────────────────────────────────────────


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


# ── Graph node state updates and HITL payloads ──────────────────


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
    inbound_intent: str = "COUNTER_OFFER"
    target_fob_ceiling: Optional[float] = None
    supplier_draft: Optional[str] = None
    supplier_thread_subject: Optional[str] = None


class ParsedSupplierTermsUpdate(BaseModel):
    """State update after parsing a supplier email."""

    model_config = {"arbitrary_types_allowed": True}

    supplier_terms: Any  # ParsedEmail instance
    pipeline_step: int = 3
    inbound_intent: str = "COUNTER_OFFER"


class ParsedNoTermsUpdate(BaseModel):
    """State update for a decline or a price-less question: the counterparty's stored terms stay as they were."""

    inbound_intent: str
    evaluation_reason: str


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
    last_counter_fob_usd: Optional[float] = None


class ConfirmDealUpdate(BaseModel):
    deal_status: str = "closed"
    pipeline_step: int = 4
    action: str = "ACCEPT_AND_CLOSE"
    supplier_draft: str = ""
    buyer_draft: str = ""


class GeneralInquiryUpdate(BaseModel):
    """Answer to a counterparty question. Only the drafts change: terms, round and deal status are kept."""

    buyer_draft: str = ""
    supplier_draft: str = ""


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


# ── API responses ───────────────────────────────────────────────


class CreateCampaignResponse(BaseModel):
    """``POST /api/campaigns`` (auto_run=False)."""

    campaign_id: str
    commodity: str
    benchmark_fob_usd: Optional[float] = None
    freight_cost_usd: Optional[float] = None
    dynamic_fob_ceiling: Optional[float] = None
    dynamic_cif_floor: Optional[float] = None
    anchor_cif_usd: Optional[float] = None
    buyer_draft: Optional[str] = None
    pipeline_step: int = 1
    deal_status: str = "prospecting"
    min_profit_per_mt_hard: float = 0.0
    min_profit_per_mt_soft: float = 0.0
    max_negotiation_rounds: int = 3
    discovered_buyers: list[dict[str, Any]] = Field(default_factory=list)
    discovered_suppliers: list[dict[str, Any]] = Field(default_factory=list)
    audit_transcript: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "initialized"


class CampaignStatusResponse(BaseModel):
    """``GET /api/campaigns/{campaign_id}``."""

    campaign_id: str
    commodity: str
    target_margin_pct: float = 0.0
    min_profit_per_mt_hard: float = 0.0
    min_profit_per_mt_soft: float = 0.0
    max_negotiation_rounds: int = 3
    negotiation_round: int = 0
    negotiation_rounds_completed: int = 0
    deal_status: str = ""
    action: Optional[str] = None
    pipeline_step: int = 1
    is_deal_viable: Optional[bool] = None
    net_spread_usd: float = 0.0
    final_net_spread_usd: float = 0.0
    net_margin_pct: Optional[float] = None
    final_net_margin_pct: Optional[float] = None
    evaluation_reason: Optional[str] = None
    dynamic_fob_ceiling: Optional[float] = None
    dynamic_cif_floor: Optional[float] = None
    target_fob_ceiling: float = 0.0
    anchor_cif_usd: float = 0.0
    benchmark_fob_usd: Optional[float] = None
    freight_cost_usd: Optional[float] = None
    buyer_terms: Optional[dict[str, Any]] = None
    supplier_terms: Optional[dict[str, Any]] = None
    buyer_draft: Optional[str] = None
    supplier_draft: Optional[str] = None
    audit_transcript: list[dict[str, Any]] = Field(default_factory=list)
class NegotiateTurnResponse(BaseModel):
    """``POST /api/negotiate``."""

    campaign_id: str
    negotiation_round: int = 1
    deal_status: Optional[str] = None
    action: Optional[str] = None
    pipeline_step: int = 1
    is_deal_viable: Optional[bool] = None
    net_spread_usd: float = 0.0
    net_margin_pct: Optional[float] = None
    evaluation_reason: Optional[str] = None
    target_fob_ceiling: float = 0.0
    buyer_terms: Optional[dict[str, Any]] = None
    supplier_terms: Optional[dict[str, Any]] = None
    buyer_draft: Optional[str] = None
    supplier_draft: Optional[str] = None
    audit_transcript: list[dict[str, Any]] = Field(default_factory=list)


class ApproveDealResponse(BaseModel):
    """``POST /api/campaigns/{campaign_id}/approve``."""

    campaign_id: str
    deal_status: Optional[str] = None
    status: Optional[str] = None
    action: Optional[str] = None
    deal_approved_by_human: Optional[bool] = None
    reviewer_notes: Optional[str] = None
    override_cif_price: Optional[float] = None
    last_counter_cif_usd: Optional[float] = None
    evaluation_reason: Optional[str] = None
    is_deal_viable: Optional[bool] = None
    net_spread_usd: float = 0.0
    net_margin_pct: Optional[float] = None
    pipeline_step: Optional[int] = None
    negotiation_round: Optional[int] = None
    buyer_terms: Optional[dict[str, Any]] = None
    supplier_terms: Optional[dict[str, Any]] = None
    buyer_draft: Optional[str] = None
    supplier_draft: Optional[str] = None
    audit_transcript: list[dict[str, Any]] = Field(default_factory=list)
    execution_trail: list[dict[str, Any]] = Field(default_factory=list)


class AutonomousRunnerResult(BaseModel):
    """``POST /api/campaigns`` with auto_run=True."""

    campaign_id: str
    commodity: str
    deal_status: str = "closed"
    negotiation_rounds_completed: int = 0
    final_net_spread_usd: float = 0.0
    final_net_margin_pct: float = 0.0
    action: str = "ACCEPT_AND_CLOSE"
    is_deal_viable: bool = True
    pipeline_step: int = 4
    evaluation_reason: str = ""
    audit_transcript: list[dict[str, Any]] = Field(default_factory=list)
    buyer_terms: Optional[dict[str, Any]] = None
    supplier_terms: Optional[dict[str, Any]] = None
    buyer_draft: Optional[str] = None
    supplier_draft: Optional[str] = None
    benchmark_fob_usd: Optional[float] = None
    freight_cost_usd: Optional[float] = None
    dynamic_fob_ceiling: Optional[float] = None
    dynamic_cif_floor: Optional[float] = None
    anchor_cif_usd: Optional[float] = None
    discovered_buyers: list[dict[str, Any]] = Field(default_factory=list)
    discovered_suppliers: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "closed"


class WebhookResponse(BaseModel):
    """``POST /api/webhooks/gmail``."""

    status: str = "ok"
    historyId: str = ""
    message: Optional[str] = None


ModelT = TypeVar("ModelT", bound=BaseModel)


def _to_plain(value: Any) -> Any:
    return value.model_dump() if isinstance(value, BaseModel) else value


def model_from_state(model_cls: type[ModelT], state: dict, **overrides: Any) -> ModelT:
    """Build a response model from same-named graph state keys; missing keys fall back to model defaults.

    Nested models in state (e.g. ParsedEmail terms) are dumped to dicts. Keyword overrides win.
    """
    values = {name: _to_plain(state[name]) for name in model_cls.model_fields if name in state}
    values.update(overrides)
    return model_cls(**values)
