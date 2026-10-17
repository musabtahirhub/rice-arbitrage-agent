"""Node: answer a counterparty's general question (payment, shipment, packing, specs) from the deal facts.

The answer never changes terms and never uses a negotiation round: the stored terms, round count and
deal status stay as they were, and the email closes by restating the desk's standing offer for that side.
"""
from typing import Optional

from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.graph.agents.risk_worker import resolve_desk_proposed_rate
from app.logger import get_logger
from app.models import Campaign, DealState, coerce_campaign, coerce_terms
from app.prompts import GENERAL_INQUIRY_PROMPT
from app.schemas import GeneralInquiryUpdate
from app.services.llm_service import generate_email_draft
from app.services.mail.email_utils import strip_quoted_reply

logger = get_logger("arbitrage_desk.workflow")


def _deal_facts(state: DealState, campaign: Campaign, role: str) -> dict:
    """Facts the answer may state. A supplier sees our FOB side only, never the buyer's CIF price."""
    own_terms = coerce_terms(state.get("buyer_terms" if role == "buyer" else "supplier_terms"))
    is_buyer = role == "buyer"
    return {
        "commodity": campaign.commodity,
        "broken_percentage": campaign.broken_percentage,
        "volume_mt": own_terms.quantity_mt if own_terms else campaign.target_volume_mt,
        "incoterm": "CIF" if is_buyer else "FOB",
        "origin_port": campaign.origin_port_default,
        "destination_port": campaign.destination_port,
        "price_port": campaign.destination_port if is_buyer else campaign.origin_port_default,
        "payment_terms": settings.default_payment_terms,
        "packing_spec": settings.default_packing_spec,
        "shipment_window": settings.default_shipment_window,
        "inspection": settings.default_inspection,
        "standing_price": resolve_desk_proposed_rate(state, role),
    }


def _inquiry_answer_draft(facts: dict, role: str) -> tuple[str, str]:
    """Template used when Gemini is unavailable: restate the standing terms, which cover the usual questions."""
    subject = f"Re: {facts['commodity']} — Terms Clarification"
    body = (
        f"Dear {'Buyer' if role == 'buyer' else 'Exporter'},\n\n"
        f"Thank you for your question. For clarity, our standing terms for this parcel are:\n\n"
        f"• Commodity: {facts['commodity']} (max {facts['broken_percentage']}% broken)\n"
        f"• Volume: {facts['volume_mt']:,.0f} MT\n"
        f"• Port of Loading: {facts['origin_port']} | Port of Discharge: {facts['destination_port']}\n"
        f"• Payment: {facts['payment_terms']}\n"
        f"• Packing: {facts['packing_spec']}\n"
        f"• Shipment: {facts['shipment_window']}\n"
        f"• Inspection: {facts['inspection']}\n\n"
        f"If your question is not covered above, our operations team will confirm and revert shortly.\n\n"
        f"Our offer stands at USD {facts['standing_price']:.2f}/MT {facts['incoterm']} {facts['price_port']}; "
        f"we would be glad to proceed on your confirmation.\n\n"
        f"Warm regards,\nTrading Desk, {settings.desk_name}"
    )
    return subject, body


def handle_general_inquiry_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    campaign = coerce_campaign(state["campaign"])
    role = state.get("active_role", "buyer")
    raw_email = state.get("latest_email", "")
    question = strip_quoted_reply(raw_email) or raw_email
    facts = _deal_facts(state, campaign, role)

    prompt = GENERAL_INQUIRY_PROMPT.format(
        desk_name=settings.desk_name, recipient_role=role, question=question, **facts
    )
    answer = generate_email_draft(prompt, *_inquiry_answer_draft(facts, role), config=config)

    logger.info(
        f"[WORKFLOW:General Inquiry] Answered {role} question for campaign '{campaign.campaign_id}'; "
        f"standing offer USD {facts['standing_price']:.2f}/MT {facts['incoterm']} restated, "
        f"round stays {state.get('negotiation_round', 0)}."
    )
    update = GeneralInquiryUpdate(
        buyer_draft=answer if role == "buyer" else "",
        supplier_draft=answer if role != "buyer" else "",
    )
    return update.model_dump()
