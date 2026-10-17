"""Nodes: parse inbound email (a first buyer bid triggers the RFQ), market data, deterministic risk
evaluation, and the post-evaluation router."""
from typing import Literal, Optional

from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.directory import (
    resolve_supplier_contact,
)
from app.logger import get_logger
from app.models import Campaign, DealState, ParsedEmail, coerce_campaign, coerce_terms
from app.pricing.evaluation import evaluate_deal
from app.pricing.formulas import calculate_campaign_bounds, resolve_buffer_usd
from app.pricing.market import estimate_freight, get_benchmark_rate
from app.prompts import (
    SUPPLIER_RFQ_PROMPT,
)
from app.schemas import (
    EvaluateRiskUpdate,
    FetchMarketDataUpdate,
    ParsedBuyerTermsUpdate,
    ParsedNoTermsUpdate,
    ParsedSupplierTermsUpdate,
)
from app.services.llm_service import (
    DEFAULT_PROPOSED_PRICE,
    extract_email_terms,
    generate_email_draft,
)
from app.services.mail.email_utils import rfq_subject, strip_quoted_reply

logger = get_logger("arbitrage_desk.workflow")


def _supplier_rfq_draft(
    campaign: Campaign, supplier_name: str, quantity_mt: float, fob_ceiling: float
) -> tuple[str, str]:
    subject = f"Urgent RFQ — {campaign.commodity} FOB {campaign.origin_port_default}"
    body = (
        f"Dear Export Team at {supplier_name},\n\n"
        f"We have an active firm export requirement for {quantity_mt:,.0f} MT of {campaign.commodity} "
        f"for shipment to {campaign.destination_port}.\n\n"
        f"Our target acquisition ceiling for this volume is USD {fob_ceiling:.2f}/MT FOB "
        f"{campaign.origin_port_default}.\n"
        f"• Payment Terms: {settings.default_payment_terms}\n"
        f"• Quality: Max {campaign.broken_percentage}% Broken grain\n"
        f"• Volume: {quantity_mt:,.0f} MT\n\n"
        f"Please confirm earliest shipping readiness and provide your quotation at or below "
        f"USD {fob_ceiling:.2f}/MT FOB.\n\n"
        f"Best regards,\nProcurement Desk, {settings.desk_name}"
    )
    return subject, body


def resolve_desk_proposed_rate(state: DealState, role: str) -> float:
    """The rate the desk last put to this counterparty: what a bare "accepted" from them agrees to.
    Buyers see CIF prices, suppliers FOB prices, so the two sides never share a rate."""
    if role == "supplier":
        return (
            state.get("last_counter_fob_usd")
            or state.get("target_fob_ceiling")
            or state.get("dynamic_fob_ceiling")
            or DEFAULT_PROPOSED_PRICE
        )
    return (
        state.get("last_counter_cif_usd")
        or state.get("dynamic_cif_floor")
        or state.get("anchor_cif_usd")
        or DEFAULT_PROPOSED_PRICE
    )


def _generate_initial_supplier_rfq(
    campaign: Campaign, terms: ParsedEmail, freight: float, config: Optional[RunnableConfig] = None
) -> tuple[float, str]:
    """(target FOB ceiling, RFQ draft): the ceiling keeps the soft-target spread against the buyer's bid."""
    target_fob_ceiling = round(
        terms.price_usd_per_mt - freight - resolve_buffer_usd(campaign) - campaign.min_profit_per_mt_soft, 2
    )
    supp_name, _ = resolve_supplier_contact(campaign.commodity)
    prompt = SUPPLIER_RFQ_PROMPT.format(
        desk_name=settings.desk_name,
        supplier_name=supp_name,
        origin_port=campaign.origin_port_default,
        commodity=campaign.commodity,
        broken_percentage=campaign.broken_percentage,
        target_volume_mt=terms.quantity_mt,
        fob_ceiling=target_fob_ceiling,
        supplier_offered_text="None yet (Initial Sourcing RFQ)",
        action="INITIAL_RFQ",
        evaluation_reason=(
            f"Active export enquiry from buyer at USD {terms.price_usd_per_mt:.2f}/MT CIF {campaign.destination_port}."
        ),
    )
    fallback = _supplier_rfq_draft(campaign, supp_name, terms.quantity_mt, target_fob_ceiling)
    return target_fob_ceiling, generate_email_draft(prompt, *fallback, config=config)


def _process_parsed_buyer_terms(
    terms: ParsedEmail,
    state: DealState,
    campaign: Campaign,
    desk_proposed_rate: float,
    config: Optional[RunnableConfig] = None,
) -> dict:
    # Always written explicitly (never None) so a previous acceptance cannot survive a later counter-offer.
    buyer_accepted = bool(terms.is_acceptance or terms.intent == "ACCEPTANCE")
    if buyer_accepted and terms.price_usd_per_mt <= 0.0:
        terms.price_usd_per_mt = desk_proposed_rate

    logger.info(
        f"[WORKFLOW:Node 1] Ingested buyer terms: price=${terms.price_usd_per_mt:.2f}/MT, "
        f"intent='{terms.intent}', buyer_accepted={buyer_accepted}"
    )

    update = ParsedBuyerTermsUpdate(
        buyer_terms=terms, pipeline_step=2, buyer_accepted=buyer_accepted, inbound_intent=terms.intent
    )
    if not state.get("supplier_terms"):
        freight = state.get("freight_cost_usd") or estimate_freight(
            campaign.origin_port_default, campaign.destination_port
        )
        update.target_fob_ceiling, update.supplier_draft = _generate_initial_supplier_rfq(
            campaign, terms, freight, config=config
        )
        update.supplier_thread_subject = state.get("supplier_thread_subject") or rfq_subject(
            campaign.campaign_id, campaign.commodity, campaign.origin_port_default
        )
        update.pipeline_step = 3

    res = update.model_dump(exclude_none=True)
    res["buyer_terms"] = terms
    return res


def _process_parsed_supplier_terms(terms: ParsedEmail) -> dict:
    logger.info(
        f"[WORKFLOW:Node 1] Ingested supplier terms: price=${terms.price_usd_per_mt:.2f}/MT FOB, "
        f"port='{terms.port}', qty={terms.quantity_mt:,.0f}MT"
    )
    res = ParsedSupplierTermsUpdate(
        supplier_terms=terms, pipeline_step=3, inbound_intent=terms.intent
    ).model_dump(exclude_none=True)
    res["supplier_terms"] = terms
    return res


def parse_incoming_email_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    role = state.get("active_role", "buyer")
    campaign = coerce_campaign(state["campaign"])
    desk_proposed_rate = resolve_desk_proposed_rate(state, role)
    deal_context = {
        "last_proposed_price": desk_proposed_rate,
        "commodity": campaign.commodity,
        "default_volume_mt": campaign.target_volume_mt,
    }

    raw_email = state.get("latest_email", "")
    # Only the counterparty's new text: quoted history repeats the desk's own prices.
    reply_text = strip_quoted_reply(raw_email) or raw_email
    terms = extract_email_terms(reply_text, role, deal_context=deal_context, config=config)
    if terms.intent in ("REJECTION", "INQUIRY"):
        return _process_priceless_reply(terms, role)
    if role == "buyer":
        return _process_parsed_buyer_terms(terms, state, campaign, desk_proposed_rate, config=config)
    return _process_parsed_supplier_terms(terms)


def _process_priceless_reply(terms: ParsedEmail, role: str) -> dict:
    """A decline or a question: keep the counterparty's previous terms (a $0 price would read as a
    free supplier quote or a worthless buyer bid) and let route_after_parse close the deal or answer the question."""
    party = "Buyer" if role == "buyer" else "Supplier"
    summary = terms.summary if terms.summary and not terms.summary.startswith("Extracted via") else ""
    if terms.intent == "REJECTION":
        reason = f"{party} declined the deal."
    else:
        reason = f"{party} asked a question with no price; answered from the deal facts."
    reason += f" {summary}" if summary else ""
    logger.info(f"[WORKFLOW:Node 1] {party} email has intent '{terms.intent}': terms unchanged. {reason}")
    return ParsedNoTermsUpdate(inbound_intent=terms.intent, evaluation_reason=reason).model_dump()


def route_after_parse(state: DealState) -> Literal["fetch_market_data", "reject_deal", "handle_general_inquiry"]:
    intent = state.get("inbound_intent")
    if intent == "REJECTION":
        return "reject_deal"
    if intent == "INQUIRY":
        return "handle_general_inquiry"
    return "fetch_market_data"


def fetch_market_data_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    campaign = coerce_campaign(state["campaign"])
    benchmark_fob = get_benchmark_rate(campaign.commodity, campaign.broken_percentage)
    freight = estimate_freight(campaign.origin_port_default, campaign.destination_port)
    bounds = calculate_campaign_bounds(campaign, benchmark_fob, freight)

    update = FetchMarketDataUpdate(
        benchmark_fob_usd=benchmark_fob,
        freight_cost_usd=freight,
        dynamic_fob_ceiling=bounds.dynamic_fob_ceiling,
        dynamic_cif_floor=bounds.dynamic_cif_floor,
    )
    return update.model_dump()


def evaluate_risk_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    curr_round = state.get("negotiation_round", 0) + 1
    result = evaluate_deal(
        campaign=coerce_campaign(state["campaign"]),
        buyer_terms=coerce_terms(state.get("buyer_terms")),
        supplier_terms=coerce_terms(state.get("supplier_terms")),
        benchmark_fob=state.get("benchmark_fob_usd", 900.0),
        freight=state.get("freight_cost_usd", 50.0),
        round_num=curr_round,
        buyer_accepted=bool(state.get("buyer_accepted", False)),
    )

    logger.info(
        f"[WORKFLOW:Node 3 Risk] Round {curr_round} Evaluation: action='{result.action}', "
        f"viable={result.viable}, net_spread=${result.net_spread:.2f}/MT, margin={result.net_margin_pct:.2f}%, "
        f"reason='{result.reason}'"
    )

    update = EvaluateRiskUpdate(
        is_deal_viable=result.viable,
        action=result.action,
        net_spread_usd=result.net_spread,
        net_margin_pct=result.net_margin_pct,
        evaluation_reason=result.reason,
        negotiation_round=curr_round,
    )
    return update.model_dump()


def _counter_node_for(active_role: str) -> Literal["counter_buyer", "counter_supplier"]:
    return "counter_buyer" if active_role == "buyer" else "counter_supplier"


def route_after_evaluation(
    state: DealState,
) -> Literal["approval_gate", "confirm_deal", "counter_buyer", "counter_supplier", "reject_deal"]:
    action = state.get("action")
    has_supplier = state.get("supplier_terms") is not None
    active_role = state.get("active_role", "buyer")

    if action == "REJECT_HARD":
        return "reject_deal"
    if action == "ACCEPT_AND_CLOSE" and has_supplier:
        return "approval_gate"
    if action == "COUNTER_TO_MAXIMIZE":
        return _counter_node_for(active_role)
    if state.get("is_deal_viable") and has_supplier:
        return "approval_gate"
    return _counter_node_for(active_role)
