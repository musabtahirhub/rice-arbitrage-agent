from typing import Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, interrupt

from app.config import settings
from app.logger import get_logger
from app.models import Campaign, DealState, ParsedEmail
from app.prompts import DEAL_CONFIRMATION_PROMPT, DEAL_REJECTION_PROMPT
from app.schemas import (
    ApprovalResumeDecision,
    ApprovalReviewPayload,
    ConfirmDealUpdate,
    RejectDealUpdate,
)
from app.services.llm_service import generate_dynamic_llm_draft

logger = get_logger("arbitrage_desk.workflow")


def _build_approval_review_payload(state: DealState) -> ApprovalReviewPayload:
    campaign: Campaign = state["campaign"]
    cid = getattr(campaign, "campaign_id", None) or (campaign.get("campaign_id") if isinstance(campaign, dict) else "unknown")
    comm = getattr(campaign, "commodity", None) or (campaign.get("commodity") if isinstance(campaign, dict) else settings.default_commodity)
    vol = float(getattr(campaign, "target_volume_mt", 0.0) or (campaign.get("target_volume_mt", 0.0) if isinstance(campaign, dict) else 0.0))

    buyer = state.get("buyer_terms")
    if isinstance(buyer, dict):
        buyer = ParsedEmail(**buyer)
    supplier = state.get("supplier_terms")
    if isinstance(supplier, dict):
        supplier = ParsedEmail(**supplier)

    buyer_bid_cif = float(buyer.price_usd_per_mt) if buyer else 0.0
    supplier_ask_fob = float(supplier.price_usd_per_mt) if supplier else 0.0
    freight = float(state.get("freight_cost_usd", 0.0))
    net_spread = float(state.get("net_spread_usd", 0.0))
    net_margin = float(state.get("net_margin_pct", 0.0))
    deal_vol = float(buyer.quantity_mt) if (buyer and buyer.quantity_mt) else vol
    total_deal_val = round(buyer_bid_cif * deal_vol, 2)

    return ApprovalReviewPayload(
        campaign_id=cid,
        commodity=comm,
        volume_mt=deal_vol,
        buyer_bid_cif=buyer_bid_cif,
        supplier_ask_fob=supplier_ask_fob,
        freight_cost_usd=freight,
        net_spread_usd=net_spread,
        net_margin_pct=net_margin,
        total_deal_value=total_deal_val,
    )


def _resolve_approval_command(decision: ApprovalResumeDecision) -> Command:
    if decision.approved is True:
        return Command(
            goto="confirm_deal",
            update={
                "deal_approved_by_human": True,
                "reviewer_notes": decision.reviewer_notes,
            },
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
    review_payload: ApprovalReviewPayload = _build_approval_review_payload(state)
    cid = review_payload.campaign_id
    comm = review_payload.commodity
    deal_vol = review_payload.volume_mt
    buyer_bid = review_payload.buyer_bid_cif
    supplier_ask = review_payload.supplier_ask_fob
    net_spread = review_payload.net_spread_usd
    net_margin = review_payload.net_margin_pct
    total_val = review_payload.total_deal_value

    logger.info(
        f"[WORKFLOW:Approval Gate] Interrupting for human governance review on campaign '{cid}': "
        f"{comm} {deal_vol:,.0f} MT | Bid: ${buyer_bid:.2f} CIF | Ask: ${supplier_ask:.2f} FOB | "
        f"Spread: ${net_spread:.2f}/MT ({net_margin:.2f}%) | Total Value: ${total_val:.2f}"
    )

    raw_decision = interrupt(review_payload.model_dump())
    if isinstance(raw_decision, ApprovalResumeDecision):
        decision = raw_decision
    elif isinstance(raw_decision, dict):
        decision = ApprovalResumeDecision(**raw_decision)
    else:
        decision = ApprovalResumeDecision()

    logger.info(f"[WORKFLOW:Approval Gate] Received review decision for campaign '{cid}': {decision}")
    return _resolve_approval_command(decision)


def _generate_supplier_lock_draft(campaign: Campaign, supplier: Optional[ParsedEmail], config: Optional[RunnableConfig] = None) -> str:
    commodity = supplier.commodity if supplier else campaign.commodity
    quantity = supplier.quantity_mt if supplier else campaign.target_volume_mt
    price = supplier.price_usd_per_mt if supplier else 0.0
    incoterm = supplier.incoterm if supplier else "FOB"
    port = supplier.port if (supplier and supplier.port) else campaign.origin_port_default

    supp_prompt = DEAL_CONFIRMATION_PROMPT.format(
        desk_name=settings.desk_name,
        recipient_role="supplier",
        recipient_name="Asian Rice Exporters",
        commodity=commodity,
        quantity_mt=quantity,
        agreed_price=price,
        incoterm=incoterm,
        port=port,
        payment_terms=settings.default_payment_terms,
    )
    fallback_sub = f"Deal Confirmation & Volume Lock — {commodity}"
    fallback_body = (
        f"Dear Supplier,\n\n"
        f"We are pleased to accept your offer of USD {price:.2f}/MT {incoterm} "
        f"for {quantity:,.0f} MT. Please consider this volume formally locked.\n"
        f"Kindly issue the Proforma Invoice (PI) and banking coordinates.\n\n"
        f"Best regards,\n{settings.desk_name}"
    )
    return generate_dynamic_llm_draft(supp_prompt, fallback_sub, fallback_body, config=config)


def _generate_buyer_acceptance_draft(campaign: Campaign, buyer: Optional[ParsedEmail], config: Optional[RunnableConfig] = None) -> str:
    commodity = buyer.commodity if buyer else campaign.commodity
    quantity = buyer.quantity_mt if buyer else campaign.target_volume_mt
    price = buyer.price_usd_per_mt if buyer else 0.0
    incoterm = buyer.incoterm if buyer else "CIF"
    port = buyer.port if (buyer and buyer.port) else campaign.destination_port

    buyer_prompt = DEAL_CONFIRMATION_PROMPT.format(
        desk_name=settings.desk_name,
        recipient_role="buyer",
        recipient_name="Procurement Partner",
        commodity=commodity,
        quantity_mt=quantity,
        agreed_price=price,
        incoterm=incoterm,
        port=port,
        payment_terms=settings.default_payment_terms,
    )
    fallback_sub = f"Soft Corporate Offer (SCO) Acceptance — {commodity}"
    fallback_body = (
        f"Dear Buyer,\n\n"
        f"We confirm acceptance of your purchase order at USD {price:.2f}/MT {incoterm} "
        f"for {quantity:,.0f} MT. Supplier allocation has been secured.\n"
        f"Our contract team will dispatch the formal SCO and draft LC instructions shortly.\n\n"
        f"Best regards,\n{settings.desk_name}"
    )
    return generate_dynamic_llm_draft(buyer_prompt, fallback_sub, fallback_body, config=config)


def confirm_deal_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    buyer = state.get("buyer_terms")
    if isinstance(buyer, dict):
        buyer = ParsedEmail(**buyer)
    supplier = state.get("supplier_terms")
    if isinstance(supplier, dict):
        supplier = ParsedEmail(**supplier)
    campaign: Campaign = state["campaign"]
    if isinstance(campaign, dict):
        campaign = Campaign(**campaign)

    supplier_msg = _generate_supplier_lock_draft(campaign, supplier, config=config)
    buyer_msg = _generate_buyer_acceptance_draft(campaign, buyer, config=config)

    logger.info(
        f"[WORKFLOW:Node 4A Confirm] Deal viable & approved! "
        f"Locked supplier allocation at ${supplier.price_usd_per_mt if supplier else 0.0:.2f}/MT {supplier.incoterm if supplier else 'FOB'}, "
        f"accepted buyer at ${buyer.price_usd_per_mt if buyer else 0.0:.2f}/MT {buyer.incoterm if buyer else 'CIF'}. Deal CLOSED."
    )

    update = ConfirmDealUpdate(
        deal_status="closed",
        pipeline_step=4,
        action="ACCEPT_AND_CLOSE",
        supplier_draft=supplier_msg,
        buyer_draft=buyer_msg,
    )
    return update.model_dump()


def _resolve_rejected_offer_price(active_role: str, buyer: Optional[ParsedEmail], supplier: Optional[ParsedEmail]) -> float:
    if active_role == "buyer" and buyer:
        return buyer.price_usd_per_mt
    if supplier:
        return supplier.price_usd_per_mt
    return 0.0


def _generate_deal_decline_draft(campaign: Campaign, active_role: str, offered_price: float, reason: str, config: Optional[RunnableConfig] = None) -> str:
    rej_prompt = DEAL_REJECTION_PROMPT.format(
        desk_name=settings.desk_name,
        recipient_role=active_role,
        recipient_name="Trading Partner",
        commodity=campaign.commodity,
        offered_price=offered_price,
        hard_floor_spread=campaign.min_profit_per_mt_hard,
        evaluation_reason=reason,
    )
    fallback_sub = f"Commercial Proposal Status — {campaign.commodity}"
    fallback_body = (
        f"Dear Trading Partner,\n\n"
        f"Thank you for your proposal regarding {campaign.commodity}. Following formal evaluation through our risk engine, "
        f"the net arbitrage spread fails our desk's non-negotiable floor of USD {campaign.min_profit_per_mt_hard:.2f}/MT.\n\n"
        f"Desk Finding: {reason}\n\n"
        f"We must respectfully decline this transaction under current pricing parameters.\n\n"
        f"Best regards,\nRisk & Arbitrage Desk, {settings.desk_name}"
    )
    return generate_dynamic_llm_draft(rej_prompt, fallback_sub, fallback_body, config=config)


def reject_deal_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    campaign: Campaign = state["campaign"]
    if isinstance(campaign, dict):
        campaign = Campaign(**campaign)
    reason = state.get("evaluation_reason", "Spread does not satisfy minimum hard hurdle.")
    active_role = state.get("active_role", "buyer")
    buyer = state.get("buyer_terms")
    if isinstance(buyer, dict):
        buyer = ParsedEmail(**buyer)
    supplier = state.get("supplier_terms")
    if isinstance(supplier, dict):
        supplier = ParsedEmail(**supplier)

    offered_price = _resolve_rejected_offer_price(active_role, buyer, supplier)
    decline_msg = _generate_deal_decline_draft(campaign, active_role, offered_price, reason, config=config)

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
