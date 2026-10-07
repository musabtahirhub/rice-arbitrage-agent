from langgraph.types import Command, interrupt

from app.config import settings
from app.logger import get_logger
from app.models import Campaign, DealState
from app.prompts import DEAL_CONFIRMATION_PROMPT, DEAL_REJECTION_PROMPT
from app.services.llm_service import generate_dynamic_llm_draft

logger = get_logger("arbitrage_desk.workflow")


def approval_gate_node(state: DealState):
    campaign: Campaign = state["campaign"]
    cid = getattr(campaign, "campaign_id", None) or (campaign.get("campaign_id") if isinstance(campaign, dict) else "unknown")
    comm = getattr(campaign, "commodity", None) or (campaign.get("commodity") if isinstance(campaign, dict) else settings.default_commodity)
    vol = float(getattr(campaign, "target_volume_mt", 0.0) or (campaign.get("target_volume_mt", 0.0) if isinstance(campaign, dict) else 0.0))

    buyer = state.get("buyer_terms")
    supplier = state.get("supplier_terms")

    buyer_bid_cif = float(buyer.price_usd_per_mt) if buyer else 0.0
    supplier_ask_fob = float(supplier.price_usd_per_mt) if supplier else 0.0
    freight = float(state.get("freight_cost_usd", 0.0))
    net_spread = float(state.get("net_spread_usd", 0.0))
    net_margin = float(state.get("net_margin_pct", 0.0))
    deal_vol = float(buyer.quantity_mt) if (buyer and buyer.quantity_mt) else vol
    total_deal_val = round(buyer_bid_cif * deal_vol, 2)

    review_payload = {
        "campaign_id": cid,
        "commodity": comm,
        "volume_mt": deal_vol,
        "buyer_bid_cif": buyer_bid_cif,
        "supplier_ask_fob": supplier_ask_fob,
        "freight_cost_usd": freight,
        "net_spread_usd": net_spread,
        "net_margin_pct": net_margin,
        "total_deal_value": total_deal_val,
    }

    logger.info(
        f"[WORKFLOW:Approval Gate] Interrupting for human governance review on campaign '{cid}': "
        f"{comm} {deal_vol:,.0f} MT | Bid: ${buyer_bid_cif:.2f} CIF | Ask: ${supplier_ask_fob:.2f} FOB | "
        f"Spread: ${net_spread:.2f}/MT ({net_margin:.2f}%) | Total Value: ${total_deal_val:,.2f}"
    )

    decision = interrupt(review_payload)
    if not isinstance(decision, dict):
        decision = {}

    logger.info(f"[WORKFLOW:Approval Gate] Received review decision for campaign '{cid}': {decision}")

    if decision.get("approved") is True:
        return Command(
            goto="confirm_deal",
            update={
                "deal_approved_by_human": True,
                "reviewer_notes": decision.get("reviewer_notes"),
            },
        )
    elif decision.get("approved") is False and decision.get("override_cif_price"):
        return Command(
            goto="counter_buyer",
            update={
                "deal_approved_by_human": False,
                "last_counter_cif_usd": float(decision["override_cif_price"]),
                "evaluation_reason": decision.get("reviewer_notes", "Trader floor override"),
            },
        )
    else:
        return Command(
            goto="reject_deal",
            update={
                "deal_approved_by_human": False,
                "evaluation_reason": decision.get("reviewer_notes", "Declined by risk officer"),
                "deal_status": "rejected",
            },
        )


def confirm_deal_node(state: DealState) -> dict:
    buyer = state.get("buyer_terms")
    supplier = state.get("supplier_terms")
    campaign: Campaign = state["campaign"]

    supp_prompt = DEAL_CONFIRMATION_PROMPT.format(
        desk_name=settings.desk_name,
        recipient_role="supplier",
        recipient_name="Asian Rice Exporters",
        commodity=supplier.commodity if supplier else campaign.commodity,
        quantity_mt=supplier.quantity_mt if supplier else campaign.target_volume_mt,
        agreed_price=supplier.price_usd_per_mt if supplier else 0.0,
        incoterm=supplier.incoterm if supplier else "FOB",
        port=supplier.port if (supplier and supplier.port) else campaign.origin_port_default,
        payment_terms=settings.default_payment_terms,
    )
    fallback_supp_sub = f"Deal Confirmation & Volume Lock — {supplier.commodity if supplier else campaign.commodity}"
    fallback_supp_body = (
        f"Dear Supplier,\n\n"
        f"We are pleased to accept your offer of USD {supplier.price_usd_per_mt:.2f}/MT {supplier.incoterm} "
        f"for {supplier.quantity_mt:,.0f} MT. Please consider this volume formally locked.\n"
        f"Kindly issue the Proforma Invoice (PI) and banking coordinates.\n\n"
        f"Best regards,\n{settings.desk_name}"
    )
    supplier_msg = generate_dynamic_llm_draft(supp_prompt, fallback_supp_sub, fallback_supp_body)

    buyer_prompt = DEAL_CONFIRMATION_PROMPT.format(
        desk_name=settings.desk_name,
        recipient_role="buyer",
        recipient_name="Procurement Partner",
        commodity=buyer.commodity if buyer else campaign.commodity,
        quantity_mt=buyer.quantity_mt if buyer else campaign.target_volume_mt,
        agreed_price=buyer.price_usd_per_mt if buyer else 0.0,
        incoterm=buyer.incoterm if buyer else "CIF",
        port=buyer.port if (buyer and buyer.port) else campaign.destination_port,
        payment_terms=settings.default_payment_terms,
    )
    fallback_buyer_sub = f"Soft Corporate Offer (SCO) Acceptance — {buyer.commodity if buyer else campaign.commodity}"
    fallback_buyer_body = (
        f"Dear Buyer,\n\n"
        f"We confirm acceptance of your purchase order at USD {buyer.price_usd_per_mt:.2f}/MT {buyer.incoterm} "
        f"for {buyer.quantity_mt:,.0f} MT. Supplier allocation has been secured.\n"
        f"Our contract team will dispatch the formal SCO and draft LC instructions shortly.\n\n"
        f"Best regards,\n{settings.desk_name}"
    )
    buyer_msg = generate_dynamic_llm_draft(buyer_prompt, fallback_buyer_sub, fallback_buyer_body)

    logger.info(
        f"[WORKFLOW:Node 4A Confirm] Deal viable & approved! "
        f"Locked supplier allocation at ${supplier.price_usd_per_mt if supplier else 0.0:.2f}/MT {supplier.incoterm if supplier else 'FOB'}, "
        f"accepted buyer at ${buyer.price_usd_per_mt if buyer else 0.0:.2f}/MT {buyer.incoterm if buyer else 'CIF'}. Deal CLOSED."
    )

    return {
        "deal_status": "closed",
        "pipeline_step": 4,
        "action": "ACCEPT_AND_CLOSE",
        "supplier_draft": supplier_msg,
        "buyer_draft": buyer_msg,
    }


def reject_deal_node(state: DealState) -> dict:
    campaign: Campaign = state["campaign"]
    reason = state.get("evaluation_reason", "Spread does not satisfy minimum hard hurdle.")
    active_role = state.get("active_role", "buyer")
    buyer = state.get("buyer_terms")
    supplier = state.get("supplier_terms")

    offered_price = buyer.price_usd_per_mt if (active_role == "buyer" and buyer) else (supplier.price_usd_per_mt if supplier else 0.0)

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

    decline_msg = generate_dynamic_llm_draft(rej_prompt, fallback_sub, fallback_body)

    logger.warning(
        f"[WORKFLOW:Node 4D Reject] Deal rejected hard for role='{active_role}': "
        f"action='REJECT_HARD', reason='{reason}'"
    )

    updates = {
        "deal_status": "rejected",
        "is_deal_viable": False,
        "action": "REJECT_HARD",
    }
    if active_role == "buyer":
        updates["buyer_draft"] = decline_msg
    else:
        updates["supplier_draft"] = decline_msg

    return updates
