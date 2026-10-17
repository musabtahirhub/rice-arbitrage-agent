"""Nodes: human approval gate (interrupt), confirm (supplier lock first, buyer second), reject."""
from typing import Optional

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, interrupt

from app.config import settings
from app.logger import get_logger
from app.models import Campaign, DealState, ParsedEmail, coerce_campaign, coerce_terms
from app.prompts import (
    DEAL_CLOSE_OUT_PROMPT,
    DEAL_CONFIRMATION_PROMPT,
    DEAL_REJECTION_PROMPT,
)
from app.schemas import (
    ApprovalResumeDecision,
    ApprovalReviewPayload,
    ConfirmDealUpdate,
    RejectDealUpdate,
)
from app.services.llm_service import (
    generate_email_draft,
)

logger = get_logger("arbitrage_desk.workflow")


def _build_approval_review_payload(state: DealState) -> ApprovalReviewPayload:
    campaign = coerce_campaign(state["campaign"])
    buyer = coerce_terms(state.get("buyer_terms"))
    supplier = coerce_terms(state.get("supplier_terms"))

    buyer_bid_cif = float(buyer.price_usd_per_mt) if buyer else 0.0
    deal_vol = float(buyer.quantity_mt) if (buyer and buyer.quantity_mt) else float(campaign.target_volume_mt or 0.0)
    return ApprovalReviewPayload(
        campaign_id=campaign.campaign_id,
        commodity=campaign.commodity,
        volume_mt=deal_vol,
        buyer_bid_cif=buyer_bid_cif,
        supplier_ask_fob=float(supplier.price_usd_per_mt) if supplier else 0.0,
        freight_cost_usd=float(state.get("freight_cost_usd", 0.0)),
        net_spread_usd=float(state.get("net_spread_usd", 0.0)),
        net_margin_pct=float(state.get("net_margin_pct", 0.0)),
        total_deal_value=round(buyer_bid_cif * deal_vol, 2),
    )


def _parse_resume_decision(raw_decision) -> ApprovalResumeDecision:
    if isinstance(raw_decision, ApprovalResumeDecision):
        return raw_decision
    if isinstance(raw_decision, dict):
        return ApprovalResumeDecision(**raw_decision)
    return ApprovalResumeDecision()


def _resolve_approval_command(decision: ApprovalResumeDecision) -> Command:
    if decision.approved is True:
        return Command(
            goto="confirm_deal",
            update={"deal_approved_by_human": True, "reviewer_notes": decision.reviewer_notes},
        )
    if decision.approved is False and decision.override_cif_price:
        return Command(
            goto="counter_buyer",
            update={
                "deal_approved_by_human": False,
                "override_cif_price": float(decision.override_cif_price),
                "evaluation_reason": decision.reviewer_notes or "Trader floor override",
            },
        )
    return Command(
        goto="reject_deal",
        update={
            "deal_approved_by_human": False,
            "evaluation_reason": decision.reviewer_notes or "Declined by risk officer",
            "deal_status": "rejected",
        },
    )


def approval_gate_node(state: DealState, config: Optional[RunnableConfig] = None):
    review = _build_approval_review_payload(state)
    logger.info(
        f"[WORKFLOW:Approval Gate] Interrupting for human governance review on campaign '{review.campaign_id}': "
        f"{review.commodity} {review.volume_mt:,.0f} MT | Bid: ${review.buyer_bid_cif:.2f} CIF | "
        f"Ask: ${review.supplier_ask_fob:.2f} FOB | Spread: ${review.net_spread_usd:.2f}/MT "
        f"({review.net_margin_pct:.2f}%) | Total Value: ${review.total_deal_value:.2f}"
    )

    decision = _parse_resume_decision(interrupt(review.model_dump()))
    logger.info(f"[WORKFLOW:Approval Gate] Received review decision for campaign '{review.campaign_id}': {decision}")
    return _resolve_approval_command(decision)


def _supplier_lock_draft(commodity: str, quantity: float, price: float, incoterm: str) -> tuple[str, str]:
    body = (
        f"Dear Supplier,\n\n"
        f"We are pleased to accept your offer of USD {price:.2f}/MT {incoterm} "
        f"for {quantity:,.0f} MT. Please consider this volume formally locked.\n"
        f"Kindly issue the Proforma Invoice (PI) and banking coordinates.\n\n"
        f"Best regards,\n{settings.desk_name}"
    )
    return f"Deal Confirmation & Volume Lock — {commodity}", body


def _buyer_acceptance_draft(commodity: str, quantity: float, price: float, incoterm: str) -> tuple[str, str]:
    body = (
        f"Dear Buyer,\n\n"
        f"We confirm acceptance of your purchase order at USD {price:.2f}/MT {incoterm} "
        f"for {quantity:,.0f} MT. Supplier allocation has been secured.\n"
        f"Our contract team will dispatch the formal SCO and draft LC instructions shortly.\n\n"
        f"Best regards,\n{settings.desk_name}"
    )
    return f"Soft Corporate Offer (SCO) Acceptance — {commodity}", body


def _deal_decline_draft(campaign: Campaign, reason: str) -> tuple[str, str]:
    body = (
        f"Dear Trading Partner,\n\n"
        f"Thank you for your proposal regarding {campaign.commodity}. Following formal evaluation through our risk "
        f"engine, the net arbitrage spread fails our desk's non-negotiable floor of USD "
        f"{campaign.min_profit_per_mt_hard:.2f}/MT.\n\n"
        f"Desk Finding: {reason}\n\n"
        f"We must respectfully decline this transaction under current pricing parameters.\n\n"
        f"Best regards,\nRisk & Arbitrage Desk, {settings.desk_name}"
    )
    return f"Commercial Proposal Status — {campaign.commodity}", body


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
    template = _supplier_lock_draft if role == "supplier" else _buyer_acceptance_draft
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


def _close_out_draft(campaign: Campaign, recipient_declined: bool) -> tuple[str, str]:
    subject = f"Enquiry Closed — {campaign.commodity}"
    if recipient_declined:
        middle = (
            f"Thank you for letting us know. We have noted your decision on this {campaign.commodity} parcel "
            f"and closed the enquiry on our side.\n\n"
            f"We value the relationship and hope to work together on a future shipment."
        )
    else:
        middle = (
            f"We regret to advise that we are unable to proceed with this {campaign.commodity} parcel at this time "
            f"and are withdrawing our offer.\n\n"
            f"We appreciate your interest and will be in touch with fresh availability."
        )
    body = f"Dear Trading Partner,\n\n{middle}\n\nBest regards,\nTrading Desk, {settings.desk_name}"
    return subject, body


def _draft_close_out(
    campaign: Campaign, declining_role: str, recipient_role: str, situation: str,
    config: Optional[RunnableConfig] = None,
) -> str:
    prompt = DEAL_CLOSE_OUT_PROMPT.format(
        desk_name=settings.desk_name,
        commodity=campaign.commodity,
        declining_party=declining_role,
        recipient_role=recipient_role,
        recipient_name="Trading Partner",
        situation=situation,
    )
    fallback = _close_out_draft(campaign, recipient_declined=declining_role == recipient_role)
    return generate_email_draft(prompt, *fallback, config=config)


def _close_out_counterparty_decline(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    """The counterparty walked away: acknowledge them, and withdraw from the other side if it was engaged
    (the buyer holds terms, or the supplier was sent an RFQ), so nobody is left waiting."""
    campaign = coerce_campaign(state["campaign"])
    declining_role = state.get("active_role", "buyer")
    reason = state.get("evaluation_reason", f"{declining_role.capitalize()} declined the deal.")
    drafts = {declining_role: _draft_close_out(campaign, declining_role, declining_role, reason, config=config)}

    other_role = "supplier" if declining_role == "buyer" else "buyer"
    other_engaged = (
        bool(state.get("buyer_terms")) if other_role == "buyer"
        else bool(state.get("supplier_terms") or state.get("target_fob_ceiling"))
    )
    if other_engaged:
        drafts[other_role] = _draft_close_out(campaign, declining_role, other_role, reason, config=config)

    logger.warning(
        f"[WORKFLOW:Node 4D Reject] {declining_role.capitalize()} declined; closing deal "
        f"(close-out to: {', '.join(drafts)}). Reason: '{reason}'"
    )
    update = RejectDealUpdate(
        deal_status="rejected",
        is_deal_viable=False,
        action="REJECT_HARD",
        buyer_draft=drafts.get("buyer"),
        supplier_draft=drafts.get("supplier"),
    )
    return update.model_dump(exclude_none=True)


def reject_deal_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    if state.get("inbound_intent") == "REJECTION":
        return _close_out_counterparty_decline(state, config=config)
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
    decline_msg = generate_email_draft(prompt, *_deal_decline_draft(campaign, reason), config=config)

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
