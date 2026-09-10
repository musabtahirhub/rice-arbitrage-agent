"""
Pydantic v2 schemas for the multi-agent physical commodity arbitrage system.

All trade parameters, parsed email structures, and workflow state are defined
here as strict Pydantic models so that deterministic Python code — never the
LLM — owns every numeric field and boundary check.

Price boundaries are computed dynamically from live market benchmarks and
variance tolerances configured per campaign.

State supports parallel buyer/supplier negotiation threads managed by the
Orchestrator, with a global deal_status controlled by the deterministic
Risk Worker barrier.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Campaign configuration — set once per deal thread by the trader
# ---------------------------------------------------------------------------

class CampaignConfig(BaseModel):
    """
    Immutable parameters that define the boundaries of a single arbitrage
    campaign (one commodity, one buyer–supplier matching attempt).

    Price boundaries are expressed as *relative tolerances* against live
    market benchmarks — not hardcoded dollar values.
    """

    campaign_id: str = Field(
        ...,
        description="Unique identifier for this arbitrage campaign.",
    )
    commodity: str = Field(
        ...,
        description="Commodity being traded, e.g. 'Basmati 1121 Sella Rice 5% broken'.",
    )

    # --- Market-relative pricing parameters ---
    target_profit_margin_pct: float = Field(
        ...,
        description="Minimum acceptable net profit margin as a percentage.",
    )
    max_acceptable_variance_from_benchmark_pct: float = Field(
        default=5.0,
        description=(
            "Maximum acceptable premium over the FOB benchmark index for buy "
            "side, expressed as a percentage (e.g. 5.0 means we reject supplier "
            "FOB quotes more than 5% above the market index)."
        ),
    )
    min_sell_margin_above_benchmark_pct: float = Field(
        default=2.0,
        description=(
            "Minimum acceptable margin above the landed CIF benchmark for sell "
            "side, expressed as a percentage (e.g. 2.0 means we reject buyer CIF "
            "offers less than 2% above the benchmark CIF cost)."
        ),
    )
    benchmark_index_name: str = Field(
        default="basmati_1121",
        description=(
            "Canonical slug identifying which market benchmark index to use "
            "for this campaign (e.g. 'basmati_1121', 'thai_white', 'vietnam_5')."
        ),
    )


# ---------------------------------------------------------------------------
# Parsed trade email — structured extraction target for the LLM
# ---------------------------------------------------------------------------

class ParsedTradeEmail(BaseModel):
    """
    Structured representation of a single negotiation email, extracted by the
    LLM from raw unstructured text.  The LLM is only responsible for text
    extraction — all numeric evaluation happens downstream in pure Python.
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
        description="Incoterm basis for the quoted price.",
    )
    port: Optional[str] = Field(
        default=None,
        description="Loading or destination port, if mentioned.",
    )


# ---------------------------------------------------------------------------
# Thread status literals — per-agent negotiation state
# ---------------------------------------------------------------------------

BuyerThreadStatus = Literal[
    "awaiting_inquiry",     # no buyer email received yet
    "inquiry_parsed",       # buyer email parsed, awaiting risk evaluation
    "counter_sent",         # counter-offer drafted and sent to buyer
    "terms_accepted",       # buyer terms pass risk evaluation
]

SupplierThreadStatus = Literal[
    "awaiting_quote",       # no supplier email received yet
    "quote_parsed",         # supplier email parsed, awaiting risk evaluation
    "counter_sent",         # counter-offer drafted and sent to supplier
    "allocation_locked",    # supplier terms pass risk evaluation + allocation confirmed
]

DealStatus = Literal[
    "prospecting",          # initial state — gathering quotes
    "evaluating",           # risk worker is evaluating the deal
    "negotiating_buyer",    # buyer terms need renegotiation
    "negotiating_supplier", # supplier terms need renegotiation
    "approved",             # deal passes all gates — ready for SCO
    "rejected",             # deal fails unrecoverably
    "closed",               # SCO issued, allocation locked
]


# ---------------------------------------------------------------------------
# Workflow state — the single mutable object threaded through LangGraph
# ---------------------------------------------------------------------------

class TradeState(BaseModel):
    """
    Full mutable state for the multi-agent arbitrage workflow.

    The Orchestrator manages global deal_status.  Each agent (Buyer, Supplier)
    has its own thread status, and neither can finalize terms without approval
    from the deterministic Risk Worker.
    """

    # --- Identity ---
    campaign: CampaignConfig
    thread_id: str = Field(
        default="",
        description="Conversation / thread identifier for message continuity.",
    )

    # --- Raw input ---
    raw_email: str = Field(
        default="",
        description="The latest raw email text to be parsed.",
    )

    # --- Parsed counterparty terms ---
    buyer_terms: Optional[ParsedTradeEmail] = Field(
        default=None,
        description="Structured buyer inquiry extracted by the Buyer Agent.",
    )
    supplier_terms: Optional[ParsedTradeEmail] = Field(
        default=None,
        description="Structured supplier quote extracted by the Supplier Agent.",
    )

    # --- Per-agent thread status ---
    buyer_thread_status: BuyerThreadStatus = Field(
        default="awaiting_inquiry",
        description="Current state of the buyer-side negotiation thread.",
    )
    supplier_thread_status: SupplierThreadStatus = Field(
        default="awaiting_quote",
        description="Current state of the supplier-side negotiation thread.",
    )

    # --- Global deal status (Orchestrator-controlled) ---
    deal_status: DealStatus = Field(
        default="prospecting",
        description="Global deal status managed by the Orchestrator.",
    )
    negotiation_round: int = Field(
        default=0,
        description="Current negotiation round for multi-turn tracking.",
    )

    # --- Live market data (populated by fetch_market_data node) ---
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

    # --- Deterministic evaluation results ---
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

    # --- Drafted messages ---
    buyer_draft: str = Field(
        default="",
        description="Latest drafted message/SCO for the buyer.",
    )
    supplier_draft: str = Field(
        default="",
        description="Latest drafted message for the supplier.",
    )

    # --- Discovery & Outbound Campaigns ---
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

