"""
Inbound email webhook ingestion API route.
"""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, HTTPException, Request

from app.agents.nodes import evaluate_risk, fetch_market_data, finalize_deal
from app.api.simulation import _generate_draft_response, _parse_email_regex
from app.services.email.parser import parse_inbound_webhook_payload
from app.services.email.transport import get_email_transport
from app.services.ledger import get_campaign_state, save_campaign_state

router = APIRouter()


@router.post("/webhooks/inbound-email")
async def handle_inbound_email_webhook(request: Request):
    """
    Webhook endpoint for real-world email providers (Resend, SendGrid, etc.).
    Extracts campaign thread token, updates state, and advances negotiation.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    normalized = parse_inbound_webhook_payload(payload)
    campaign_id = normalized["campaign_id"]
    if not campaign_id:
        raise HTTPException(
            status_code=400,
            detail="No campaign reference found in email headers or body.",
        )

    state = get_campaign_state(campaign_id)
    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"Referenced campaign {campaign_id} does not exist in ledger.",
        )

    # Parse terms
    sender_role = normalized["sender_role"]
    parsed = _parse_email_regex(normalized["body"], sender_role)

    if sender_role == "buyer":
        state["buyer_terms"] = parsed
        state["buyer_thread_status"] = "inquiry_parsed"
    else:
        state["supplier_terms"] = parsed
        state["supplier_thread_status"] = "quote_parsed"

    state["raw_email"] = normalized["body"]
    state["negotiation_round"] = state.get("negotiation_round", 0) + 1

    state = fetch_market_data(state)
    if state.get("buyer_terms") and state.get("supplier_terms"):
        state = evaluate_risk(state)
        if state.get("deal_status") == "approved":
            state = finalize_deal(state)

        drafted = _generate_draft_response(state, sender_role)
        state["buyer_draft" if sender_role == "buyer" else "supplier_draft"] = drafted

        # Dispatch outbound reply via configured email transport
        transport = get_email_transport()
        recipient = normalized.get("sender") or "counterparty@market.com"
        transport.send(
            to_email=recipient,
            subject=f"RE: {normalized.get('subject', 'Trade Negotiation')}",
            body=drafted,
        )

    save_campaign_state(campaign_id, state)

    return {
        "status": "processed",
        "campaign_id": campaign_id,
        "negotiation_round": state["negotiation_round"],
        "deal_status": state.get("deal_status"),
        "is_deal_viable": state.get("is_deal_viable"),
        "net_margin_pct": state.get("net_margin_pct"),
    }
