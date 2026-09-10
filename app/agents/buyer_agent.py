
"""
Buyer Agent — handles Middle East client relations.

Responsibilities:
  1. Extract structured CIF specs from buyer inquiry emails.
  2. Draft non-binding Soft Corporate Offers (SCO) citing market levels.
  3. Draft counter-offers when the buyer's bid is below the dynamic CIF floor.

The Buyer Agent never finalizes terms autonomously; finalization requires
approval from the deterministic Risk Worker in the Orchestrator.
"""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.prompts import BUYER_SCO_SYSTEM_PROMPT, EMAIL_EXTRACTION_SYSTEM_PROMPT
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
# Node: parse_buyer_email
# ---------------------------------------------------------------------------

def parse_buyer_email(state: dict[str, Any]) -> dict[str, Any]:
    """
    Use Gemini Flash to extract structured trade fields from a buyer email.

    The LLM returns a JSON object matching ``ParsedTradeEmail``; we parse
    it with Pydantic so downstream code always works with validated types.
    Updates ``buyer_terms`` and ``buyer_thread_status`` in state.
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
        "buyer_terms": parsed,
        "buyer_thread_status": "inquiry_parsed",
    }


# ---------------------------------------------------------------------------
# Node: draft_buyer_sco
# ---------------------------------------------------------------------------

def draft_buyer_sco(state: dict[str, Any]) -> dict[str, Any]:
    """
    Draft a non-binding Soft Corporate Offer for the buyer.

    Cites prevailing market benchmark levels when available.
    Called when the deal is approved and the Orchestrator routes to SCO issuance.
    """
    llm = _get_llm()
    campaign = state.get("campaign", {})
    buyer = state.get("buyer_terms", {})

    # Build market context for the LLM to cite
    benchmark_fob = state.get("benchmark_fob_usd", 0.0)
    freight_cost = state.get("freight_cost_usd", 0.0)
    benchmark_cif = benchmark_fob + freight_cost if benchmark_fob else 0.0

    context = (
        f"Buyer inquiry: {json.dumps(buyer if isinstance(buyer, dict) else buyer.model_dump(), default=str)}\n"
        f"Campaign: {json.dumps(campaign if isinstance(campaign, dict) else campaign.model_dump(), default=str)}\n"
        f"Evaluation: {state.get('evaluation_reason', '')}\n"
        f"--- Market Data ---\n"
        f"Current FOB benchmark index: ${benchmark_fob:.2f}/MT (source: {state.get('benchmark_source', 'N/A')})\n"
        f"Estimated freight: ${freight_cost:.2f}/MT (source: {state.get('freight_source', 'N/A')})\n"
        f"Landed CIF benchmark: ${benchmark_cif:.2f}/MT\n"
        f"Use these prevailing market levels to justify the offer price."
    )

    response = llm.invoke([
        SystemMessage(content=BUYER_SCO_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])

    return {
        **state,
        "buyer_draft": response.content,
        "buyer_thread_status": "terms_accepted",
    }


# ---------------------------------------------------------------------------
# Node: draft_buyer_counter
# ---------------------------------------------------------------------------

def draft_buyer_counter(state: dict[str, Any]) -> dict[str, Any]:
    """
    Draft a counter-offer to the buyer when their CIF bid is below the
    dynamic floor.

    Cites landed CIF benchmark indices to justify the counter-price.
    """
    llm = _get_llm()
    campaign = state.get("campaign", {})
    buyer = state.get("buyer_terms", {})

    # Build market context
    benchmark_fob = state.get("benchmark_fob_usd", 0.0)
    freight_cost = state.get("freight_cost_usd", 0.0)
    benchmark_cif = benchmark_fob + freight_cost if benchmark_fob else 0.0

    context = (
        f"Buyer inquiry: {json.dumps(buyer if isinstance(buyer, dict) else buyer.model_dump(), default=str)}\n"
        f"Campaign: {json.dumps(campaign if isinstance(campaign, dict) else campaign.model_dump(), default=str)}\n"
        f"Evaluation: {state.get('evaluation_reason', '')}\n"
        f"--- Market Data ---\n"
        f"Current FOB benchmark index: ${benchmark_fob:.2f}/MT\n"
        f"Estimated freight: ${freight_cost:.2f}/MT\n"
        f"Landed CIF benchmark: ${benchmark_cif:.2f}/MT\n"
        f"The buyer's bid is below our minimum CIF floor. Draft a professional "
        f"counter-offer citing current CIF market indices of ${benchmark_cif:.2f}/MT "
        f"to justify a higher price."
    )

    response = llm.invoke([
        SystemMessage(content=BUYER_SCO_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])

    return {
        **state,
        "buyer_draft": response.content,
        "buyer_thread_status": "counter_sent",
    }
