from typing import Literal

from app.config import settings
from app.directory import get_suppliers_for_commodity
from app.logger import get_logger
from app.market import get_benchmark_rate, estimate_freight
from app.math_engine import calculate_dynamic_bounds, evaluate_deal
from app.models import Campaign, DealState, ParsedEmail
from app.prompts import SUPPLIER_RFQ_PROMPT
from app.services.llm_service import _parse_email_content, generate_dynamic_llm_draft

logger = get_logger("arbitrage_desk.workflow")


def parse_incoming_email_node(state: DealState) -> dict:
    raw_email = state.get("latest_email", "")
    role = state.get("active_role", "buyer")
    campaign: Campaign = state["campaign"]

    desk_proposed_rate = (
        state.get("last_counter_cif_usd")
        or state.get("dynamic_cif_floor")
        or state.get("anchor_cif_usd")
        or 1111.0
    )
    deal_context = {
        "last_proposed_price": desk_proposed_rate,
        "commodity": campaign.commodity,
        "default_volume_mt": campaign.target_volume_mt,
    }

    terms = _parse_email_content(raw_email, role, deal_context=deal_context)
    updates = {}
    if role == "buyer":
        if terms.is_acceptance or terms.intent == "ACCEPTANCE":
            updates["buyer_accepted"] = True
            if terms.price_usd_per_mt <= 0.0:
                terms.price_usd_per_mt = desk_proposed_rate

        updates["buyer_terms"] = terms
        updates["pipeline_step"] = 2
        logger.info(
            f"[WORKFLOW:Node 1] Ingested buyer terms: price=${terms.price_usd_per_mt:.2f}/MT, "
            f"intent='{terms.intent}', buyer_accepted={updates.get('buyer_accepted', False)}"
        )
        if not state.get("supplier_terms"):
            freight = state.get("freight_cost_usd") or estimate_freight(campaign.origin_port_default, campaign.destination_port)
            buffer_usd = campaign.buffer_usd_per_mt if campaign.buffer_usd_per_mt is not None else settings.default_buffer_usd_per_mt
            target_fob_ceiling = round(terms.price_usd_per_mt - freight - buffer_usd - campaign.min_profit_per_mt_soft, 2)
            updates["target_fob_ceiling"] = target_fob_ceiling
            updates["pipeline_step"] = 3

            suppliers = get_suppliers_for_commodity(campaign.commodity)
            target_supp = suppliers[0] if suppliers else None
            supp_name = target_supp.name if target_supp else "Asian Rice Exporters"

            rfq_prompt = SUPPLIER_RFQ_PROMPT.format(
                desk_name=settings.desk_name,
                supplier_name=supp_name,
                origin_port=campaign.origin_port_default,
                commodity=campaign.commodity,
                broken_percentage=campaign.broken_percentage,
                target_volume_mt=terms.quantity_mt,
                fob_ceiling=target_fob_ceiling,
                supplier_offered_text="None yet (Initial Sourcing RFQ)",
                action="INITIAL_RFQ",
                evaluation_reason=f"Active export enquiry from buyer at USD {terms.price_usd_per_mt:.2f}/MT CIF {campaign.destination_port}.",
            )
            fallback_rfq_sub = f"Urgent RFQ — {campaign.commodity} FOB {campaign.origin_port_default}"
            fallback_rfq_body = (
                f"Dear Export Team at {supp_name},\n\n"
                f"We have an active firm export requirement for {terms.quantity_mt:,.0f} MT of {campaign.commodity} "
                f"for shipment to {campaign.destination_port}.\n\n"
                f"Our target acquisition ceiling for this volume is USD {target_fob_ceiling:.2f}/MT FOB {campaign.origin_port_default}.\n"
                f"• Payment Terms: {settings.default_payment_terms}\n"
                f"• Quality: Max {campaign.broken_percentage}% Broken grain\n"
                f"• Volume: {terms.quantity_mt:,.0f} MT\n\n"
                f"Please confirm earliest shipping readiness and provide your quotation at or below USD {target_fob_ceiling:.2f}/MT FOB.\n\n"
                f"Best regards,\nProcurement Desk, {settings.desk_name}"
            )
            updates["supplier_draft"] = generate_dynamic_llm_draft(rfq_prompt, fallback_rfq_sub, fallback_rfq_body)
    else:
        updates["supplier_terms"] = terms
        updates["pipeline_step"] = 3
        logger.info(
            f"[WORKFLOW:Node 1] Ingested supplier terms: price=${terms.price_usd_per_mt:.2f}/MT FOB, "
            f"port='{terms.port}', qty={terms.quantity_mt:,.0f}MT"
        )

    return updates


def fetch_market_data_node(state: DealState) -> dict:
    campaign: Campaign = state["campaign"]
    benchmark_fob = get_benchmark_rate(campaign.commodity, campaign.broken_percentage)
    freight = estimate_freight(campaign.origin_port_default, campaign.destination_port)

    bounds = calculate_dynamic_bounds(
        benchmark_fob=benchmark_fob,
        freight=freight,
        max_variance_pct=campaign.max_variance_from_benchmark_pct,
        target_margin_pct=campaign.target_margin_pct,
        buffer_usd=campaign.buffer_usd_per_mt,
    )

    return {
        "benchmark_fob_usd": benchmark_fob,
        "freight_cost_usd": freight,
        "dynamic_fob_ceiling": bounds["dynamic_fob_ceiling"],
        "dynamic_cif_floor": bounds["dynamic_cif_floor"],
    }


def evaluate_risk_node(state: DealState) -> dict:
    campaign: Campaign = state["campaign"]
    buyer = state.get("buyer_terms")
    supplier = state.get("supplier_terms")
    benchmark_fob = state.get("benchmark_fob_usd", 900.0)
    freight = state.get("freight_cost_usd", 50.0)
    curr_round = state.get("negotiation_round", 0) + 1
    buyer_accepted = bool(state.get("buyer_accepted", False))

    result = evaluate_deal(
        campaign=campaign,
        buyer_terms=buyer,
        supplier_terms=supplier,
        benchmark_fob=benchmark_fob,
        freight=freight,
        round_num=curr_round,
        buyer_accepted=buyer_accepted,
    )

    action = result.get("action")
    net_spread = result.get("net_spread", 0.0)
    net_margin = result.get("net_margin_pct", 0.0)
    reason = result.get("reason", "")

    logger.info(
        f"[WORKFLOW:Node 3 Risk] Round {curr_round} Evaluation: action='{action}', "
        f"viable={result['viable']}, net_spread=${net_spread:.2f}/MT, margin={net_margin:.2f}%, "
        f"reason='{reason}'"
    )

    return {
        "is_deal_viable": result["viable"],
        "action": action,
        "net_spread_usd": net_spread,
        "net_margin_pct": net_margin,
        "evaluation_reason": reason,
        "negotiation_round": curr_round,
    }


def route_after_evaluation(state: DealState) -> Literal["approval_gate", "confirm_deal", "counter_buyer", "counter_supplier", "reject_deal"]:
    action = state.get("action")
    has_supplier = state.get("supplier_terms") is not None
    active_role = state.get("active_role", "buyer")

    if action == "REJECT_HARD":
        return "reject_deal"

    if action == "ACCEPT_AND_CLOSE" and has_supplier:
        return "approval_gate"

    if action == "COUNTER_TO_MAXIMIZE":
        if active_role == "buyer":
            return "counter_buyer"
        else:
            return "counter_supplier"

    if state.get("is_deal_viable") and has_supplier:
        return "approval_gate"

    if active_role == "buyer":
        return "counter_buyer"
    else:
        return "counter_supplier"
