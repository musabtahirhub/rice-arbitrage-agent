"""
Pydantic V2 schemas for all structured data flowing through the
commodity arbitrage multi-agent system.

These schemas replace loose ``dict`` constructions across workflow nodes,
API routes, HITL interrupt/resume payloads, math engine results, and
audit transcript entries.

NOTE: ``DealState`` remains a ``TypedDict`` (required by LangGraph's
``StateGraph``).  Schemas here model *partial state updates* returned by
individual nodes and *API request/response payloads*.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ── Math Engine Results ─────────────────────────────────────────────


class DynamicBounds(BaseModel):
    """Result of ``calculate_dynamic_bounds``."""
    dynamic_fob_ceiling: float
    landed_cost_at_ceiling: float
    dynamic_cif_floor: float

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


class DealEvaluation(BaseModel):
    """Result of ``evaluate_deal`` / ``evaluate_deal_strategy``."""
    viable: bool
    action: Optional[Literal["REJECT_HARD", "COUNTER_TO_MAXIMIZE", "ACCEPT_AND_CLOSE"]] = None
    net_spread: float = 0.0
    net_margin_pct: float = 0.0
    landed_cost: float = 0.0
    reason: str = ""
    # Dynamic bounds fields (merged from calculate_dynamic_bounds when present)
    dynamic_fob_ceiling: Optional[float] = None
    landed_cost_at_ceiling: Optional[float] = None
    dynamic_cif_floor: Optional[float] = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


# ── Audit Transcript Entry ──────────────────────────────────────────


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


# ── Workflow Node State Updates ─────────────────────────────────────


class ProactiveOutreachUpdate(BaseModel):
    """State update returned by ``proactive_outreach_node``."""
    campaign: Any  # Campaign model instance (not serialized here)
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

    model_config = {"arbitrary_types_allowed": True}


class ParsedBuyerTermsUpdate(BaseModel):
    """State update from ``_process_parsed_buyer_terms``."""
    buyer_terms: Any  # ParsedEmail instance
    pipeline_step: int = 2
    buyer_accepted: bool = False
    target_fob_ceiling: Optional[float] = None
    supplier_draft: Optional[str] = None

    model_config = {"arbitrary_types_allowed": True}


class ParsedSupplierTermsUpdate(BaseModel):
    """State update from ``_process_parsed_supplier_terms``."""
    supplier_terms: Any  # ParsedEmail instance
    pipeline_step: int = 3

    model_config = {"arbitrary_types_allowed": True}


class FetchMarketDataUpdate(BaseModel):
    """State update returned by ``fetch_market_data_node``."""
    benchmark_fob_usd: float
    freight_cost_usd: float
    dynamic_fob_ceiling: float
    dynamic_cif_floor: float


class EvaluateRiskUpdate(BaseModel):
    """State update returned by ``evaluate_risk_node``."""
    is_deal_viable: bool
    action: Optional[Literal["REJECT_HARD", "COUNTER_TO_MAXIMIZE", "ACCEPT_AND_CLOSE"]] = None
    net_spread_usd: float = 0.0
    net_margin_pct: float = 0.0
    evaluation_reason: str = ""
    negotiation_round: int = 1


class CounterBuyerUpdate(BaseModel):
    """State update returned by ``counter_buyer_node``."""
    deal_status: str = "counter_sent"
    buyer_draft: str = ""
    last_counter_cif_usd: Optional[float] = None
    override_cif_price: Optional[float] = None  # always dumped as None to consume a one-shot override


class CounterSupplierUpdate(BaseModel):
    """State update returned by ``counter_supplier_node``."""
    deal_status: str = "counter_sent"
    supplier_draft: str = ""


class ConfirmDealUpdate(BaseModel):
    """State update returned by ``confirm_deal_node``."""
    deal_status: str = "closed"
    pipeline_step: int = 4
    action: str = "ACCEPT_AND_CLOSE"
    supplier_draft: str = ""
    buyer_draft: str = ""


class RejectDealUpdate(BaseModel):
    """State update returned by ``reject_deal_node``."""
    deal_status: str = "rejected"
    is_deal_viable: bool = False
    action: str = "REJECT_HARD"
    buyer_draft: Optional[str] = None
    supplier_draft: Optional[str] = None


# ── HITL Interrupt / Resume Payloads ────────────────────────────────


class ApprovalReviewPayload(BaseModel):
    """Payload passed to ``interrupt()`` inside ``approval_gate_node``.
    Presented to the human reviewer for governance sign-off."""
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
    """Schema for the dict received from ``interrupt()`` when a human
    submits their decision via ``Command(resume=...)``."""
    approved: Optional[bool] = None
    override_cif_price: Optional[float] = None
    reviewer_notes: Optional[str] = None


# ── API Response Schemas ────────────────────────────────────────────


class CreateCampaignResponse(BaseModel):
    """Response body for ``POST /api/campaigns`` (non-auto_run)."""
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
    """Response body for ``GET /api/campaigns/{campaign_id}``."""
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
    """Response body for ``POST /api/negotiate``."""
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
    """Response body for ``POST /api/campaigns/{campaign_id}/approve``."""
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
    """Response body for ``run_full_autonomous_campaign`` / auto_run=True."""
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


class DeskConfigResponse(BaseModel):
    """Response body for ``GET /api/config``."""
    desk_name: str
    desk_email: str
    default_commodity: str
    default_target_volume_mt: float
    default_target_margin_pct: float
    default_max_variance_pct: float
    default_buffer_usd_per_mt: float
    default_destination_port: str
    default_origin_port: str
    default_payment_terms: str
    default_broken_percentage: float
    min_profit_per_mt_hard: float
    min_profit_per_mt_soft: float
    max_negotiation_rounds: int


class WebhookResponse(BaseModel):
    """Response body for ``POST /api/webhooks/gmail``."""
    status: str = "ok"
    historyId: str = ""
    message: Optional[str] = None
