"""
Supplier Agent — handles Southeast Asian exporter relations.

Responsibilities:
  1. Extract structured FOB quotes from supplier emails.
  2. Draft counter-offers when the supplier's FOB price exceeds the dynamic
     benchmark ceiling, citing prevailing FOB indices.
  3. Draft confirmation emails requesting Proforma Invoice when terms are
     within acceptable range.

The Supplier Agent never finalizes terms autonomously; finalization requires
approval from the deterministic Risk Worker in the Orchestrator.
"""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.prompts import (
    EMAIL_EXTRACTION_SYSTEM_PROMPT,
    SUPPLIER_NEGOTIATION_SYSTEM_PROMPT,
)
from app.schemas import CampaignConfig, ParsedTradeEmail

load_dotenv()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_llm() -> ChatGoogleGenerativeAI:
    """Instantiate Gemini 1.5 Flash via the free-tier API key."""
    return ChatGoogleGenerativeAI(
        model="gemini-1.5-flash",
        google_api_key=os.getenv("GEMINI_API_KEY", ""),
        temperature=0.2,
        convert_system_message_to_human=True,
    )


# ---------------------------------------------------------------------------
# Node: parse_supplier_email
# ---------------------------------------------------------------------------

def parse_supplier_email(state: dict[str, Any]) -> dict[str, Any]:
    """
    Use Gemini Flash to extract structured trade fields from a supplier email.

    The LLM returns a JSON object matching ``ParsedTradeEmail``; we parse
    it with Pydantic so downstream code always works with validated types.
    Updates ``supplier_terms`` and ``supplier_thread_status`` in state.
    """
    llm = _get_llm()
    raw = state.get("raw_email", "")
    if not raw:
        return state

    response = llm.invoke([
        SystemMessage(content=EMAIL_EXTRACTION_SYSTEM_PROMPT),
        HumanMessage(content=f"Extract structured trade data from this email:\n\n{raw}"),
    ])

    # Strip markdown code fences if the model wraps its output
    text = response.content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()

    parsed = ParsedTradeEmail(**json.loads(text))

    return {
        **state,
        "supplier_terms": parsed,
        "supplier_thread_status": "quote_parsed",
    }


# ---------------------------------------------------------------------------
# Node: draft_supplier_counter
# ---------------------------------------------------------------------------

def draft_supplier_counter(state: dict[str, Any]) -> dict[str, Any]:
    """
    Draft a counter-offer to the supplier when their FOB price exceeds the
    dynamic benchmark ceiling.

    Cites prevailing FOB benchmark indices to justify the target price.
    """
    llm = _get_llm()
    campaign = state.get("campaign", {})
    supplier = state.get("supplier_terms", {})

    # Build market context
    benchmark_fob = state.get("benchmark_fob_usd", 0.0)
    max_variance = (
        campaign.get("max_acceptable_variance_from_benchmark_pct", 5.0)
        if isinstance(campaign, dict)
        else campaign.max_acceptable_variance_from_benchmark_pct
    )
    max_buy_fob = benchmark_fob * (1 + max_variance / 100) if benchmark_fob else 0.0

    context = (
        f"Supplier quote: {json.dumps(supplier if isinstance(supplier, dict) else supplier.model_dump(), default=str)}\n"
        f"Campaign: {json.dumps(campaign if isinstance(campaign, dict) else campaign.model_dump(), default=str)}\n"
        f"Evaluation: {state.get('evaluation_reason', '')}\n"
        f"--- Market Data ---\n"
        f"Current FOB benchmark index: ${benchmark_fob:.2f}/MT (source: {state.get('benchmark_source', 'N/A')})\n"
        f"Dynamic FOB ceiling (benchmark + {max_variance:.1f}%): ${max_buy_fob:.2f}/MT\n"
        f"The supplier's FOB price exceeds our maximum. Draft a professional "
        f"counter-offer citing current FOB indices of ${benchmark_fob:.2f}/MT "
        f"(e.g., 'In line with current FOB indices of ${benchmark_fob:.2f}/MT...')."
    )

    response = llm.invoke([
        SystemMessage(content=SUPPLIER_NEGOTIATION_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])

    return {
        **state,
        "supplier_draft": response.content,
        "supplier_thread_status": "counter_sent",
    }


# ---------------------------------------------------------------------------
# Node: draft_supplier_confirm
# ---------------------------------------------------------------------------

def draft_supplier_confirm(state: dict[str, Any]) -> dict[str, Any]:
    """
    Draft a confirmation email to the supplier when their FOB price is within
    the acceptable range.

    Requests a formal Proforma Invoice and banking details.
    """
    llm = _get_llm()
    campaign = state.get("campaign", {})
    supplier = state.get("supplier_terms", {})

    benchmark_fob = state.get("benchmark_fob_usd", 0.0)

    context = (
        f"Supplier quote: {json.dumps(supplier if isinstance(supplier, dict) else supplier.model_dump(), default=str)}\n"
        f"Campaign: {json.dumps(campaign if isinstance(campaign, dict) else campaign.model_dump(), default=str)}\n"
        f"Evaluation: {state.get('evaluation_reason', '')}\n"
        f"--- Market Data ---\n"
        f"Current FOB benchmark index: ${benchmark_fob:.2f}/MT\n"
        f"The supplier's price is within acceptable range. Draft a confirmation "
        f"email requesting a formal Proforma Invoice and banking details."
    )

    response = llm.invoke([
        SystemMessage(content=SUPPLIER_NEGOTIATION_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])

    return {
        **state,
        "supplier_draft": response.content,
        "supplier_thread_status": "allocation_locked",
    }
