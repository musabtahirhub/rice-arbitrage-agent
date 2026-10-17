"""Nodes: confirm an approved deal (supplier lock first, buyer acceptance second) or reject it."""
from typing import Optional

from langchain_core.runnables import RunnableConfig

from app.agents import fallback_drafts
from app.agents.prompts import DEAL_CONFIRMATION_PROMPT, DEAL_REJECTION_PROMPT
from app.agents.state import DealState, coerce_campaign, coerce_terms
from app.agents.updates import ConfirmDealUpdate, RejectDealUpdate
from app.config import settings
from app.logger import get_logger
from app.models import Campaign, ParsedEmail
from app.services.llm import generate_email_draft

logger = get_logger("arbitrage_desk.workflow")


def _confirmation_terms(
    terms: Optional[ParsedEmail], campaign: Campaign, default_incoterm: str, default_port: str
) -> tuple[str, float, float, str, str]:
    """(commodity, quantity, price, incoterm, port) agreed with one counterparty, with campaign fallbacks."""
    if not terms:
        return campaign.commodity, campaign.target_volume_mt, 0.0, default_incoterm, default_port
    return terms.commodity, terms.quantity_mt, terms.price_usd_per_mt, terms.incoterm, terms.port or default_port


def _generate_confirmation_draft(
    role: str, recipient_name: str, terms: Optional[ParsedEmail], campaign: Campaign, default_incoterm: str,
    default_port: str, config: Optional[RunnableConfig],
) -> str:
    commodity, quantity, price, incoterm, port = _confirmation_terms(terms, campaign, default_incoterm, default_port)
    prompt = DEAL_CONFIRMATION_PROMPT.format(
        desk_name=settings.desk_name,
        recipient_role=role,
        recipient_name=recipient_name,
        commodity=commodity,
        quantity_mt=quantity,
        agreed_price=price,
        incoterm=incoterm,
        port=port,
        payment_terms=settings.default_payment_terms,
    )
    template = fallback_drafts.supplier_lock_draft if role == "supplier" else fallback_drafts.buyer_acceptance_draft
    return generate_email_draft(prompt, *template(commodity, quantity, price, incoterm), config=config)


def confirm_deal_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    campaign = coerce_campaign(state["campaign"])
    buyer = coerce_terms(state.get("buyer_terms"))
    supplier = coerce_terms(state.get("supplier_terms"))

    # Zero-risk invariant: the supplier lock is drafted (and later dispatched) before the buyer acceptance.
    supplier_msg = _generate_confirmation_draft(
        "supplier", "Asian Rice Exporters", supplier, campaign, "FOB", campaign.origin_port_default, config
    )
    buyer_msg = _generate_confirmation_draft(
        "buyer", "Procurement Partner", buyer, campaign, "CIF", campaign.destination_port, config
    )

    logger.info(
        f"[WORKFLOW:Node 4A Confirm] Deal viable & approved! Locked supplier allocation at "
        f"${supplier.price_usd_per_mt if supplier else 0.0:.2f}/MT {supplier.incoterm if supplier else 'FOB'}, "
        f"accepted buyer at ${buyer.price_usd_per_mt if buyer else 0.0:.2f}/MT {buyer.incoterm if buyer else 'CIF'}. "
        f"Deal CLOSED."
    )
    update = ConfirmDealUpdate(
        deal_status="closed",
        pipeline_step=4,
        action="ACCEPT_AND_CLOSE",
        supplier_draft=supplier_msg,
        buyer_draft=buyer_msg,
    )
    return update.model_dump()


def _resolve_rejected_offer_price(
    active_role: str, buyer: Optional[ParsedEmail], supplier: Optional[ParsedEmail]
) -> float:
    if active_role == "buyer" and buyer:
        return buyer.price_usd_per_mt
    return supplier.price_usd_per_mt if supplier else 0.0


def reject_deal_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    campaign = coerce_campaign(state["campaign"])
    reason = state.get("evaluation_reason", "Spread does not satisfy minimum hard hurdle.")
    active_role = state.get("active_role", "buyer")
    offered_price = _resolve_rejected_offer_price(
        active_role, coerce_terms(state.get("buyer_terms")), coerce_terms(state.get("supplier_terms"))
    )

    prompt = DEAL_REJECTION_PROMPT.format(
        desk_name=settings.desk_name,
        recipient_role=active_role,
        recipient_name="Trading Partner",
        commodity=campaign.commodity,
        offered_price=offered_price,
        hard_floor_spread=campaign.min_profit_per_mt_hard,
        evaluation_reason=reason,
    )
    decline_msg = generate_email_draft(prompt, *fallback_drafts.deal_decline_draft(campaign, reason), config=config)

    logger.warning(
        f"[WORKFLOW:Node 4D Reject] Deal rejected hard for role='{active_role}': "
        f"action='REJECT_HARD', reason='{reason}'"
    )
    update = RejectDealUpdate(
        deal_status="rejected",
        is_deal_viable=False,
        action="REJECT_HARD",
        buyer_draft=decline_msg if active_role == "buyer" else None,
        supplier_draft=decline_msg if active_role != "buyer" else None,
    )
    return update.model_dump(exclude_none=True)
