"""Buyer-side nodes: proactive SCO outreach and buyer counters."""
from typing import Optional

from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.directory import (
    get_buyers_for_commodity,
    get_suppliers_for_commodity,
    select_primary_buyer,
)
from app.logger import get_logger
from app.models import Campaign, DealState, ParsedEmail, coerce_campaign, coerce_terms, is_outreach_started
from app.pricing.formulas import calculate_campaign_bounds, resolve_buffer_usd
from app.pricing.market import estimate_freight, get_benchmark_rate
from app.prompts import (
    BUYER_COUNTER_PROMPT,
)
from app.schemas import (
    AuditTranscriptEntry,
    CounterBuyerUpdate,
    DynamicBounds,
    ProactiveOutreachUpdate,
)
from app.services.llm_service import (
    generate_email_draft,
    generate_proactive_sco_draft,
    sco_subject,
)
from app.services.mail.email_utils import make_message_id, split_subject_and_body
from app.services.mail.smtp import send_email

logger = get_logger("arbitrage_desk.workflow")


def _resolve_target_buyer_contact(commodity: str, state: DealState) -> tuple[str, str]:
    primary_buyer = select_primary_buyer(commodity)
    buyer_name = state.get("target_buyer_name") or (primary_buyer.name if primary_buyer else "Procurement Partner")
    directory_email = primary_buyer.contact_email if primary_buyer else "procurement@domain.com"
    return buyer_name, state.get("target_buyer_email") or settings.my_test_email or directory_email


def _calculate_pricing_parameters(
    campaign: Campaign, state: DealState, origin_port: str, dest_port: str
) -> tuple[float, float, DynamicBounds, float]:
    benchmark_fob = state.get("benchmark_fob_usd") or get_benchmark_rate(campaign.commodity, campaign.broken_percentage)
    freight = state.get("freight_cost_usd") or estimate_freight(origin_port, dest_port)
    bounds = calculate_campaign_bounds(campaign, benchmark_fob, freight)

    anchor_cif = state.get("anchor_cif_usd")
    if not anchor_cif:
        spread_anchor = (
            benchmark_fob + freight + resolve_buffer_usd(campaign)
            + campaign.min_profit_per_mt_soft + settings.sco_anchor_premium_usd_per_mt
        )
        # Never open below our own minimum CIF floor; otherwise a later floor-defense counter would
        # ask the buyer for more than we first offered.
        anchor_cif = round(max(spread_anchor, bounds.dynamic_cif_floor), 2)
    return benchmark_fob, freight, bounds, anchor_cif


def _dispatch_sco_email(
    state: DealState, to_email: str, draft: str, subject: str, msg_id: str, campaign_id: str
) -> None:
    if state.get("skip_email_dispatch", False) or not to_email:
        return
    send_email(to_email=to_email, raw_draft=draft, thread_subject=None, custom_message_id=msg_id)
    if settings.email_user and settings.email_pass:
        logger.info(
            f"[CAMPAIGN START] Dispatched initial SCO to '{to_email}' | "
            f"Campaign: '{campaign_id}' | Subject: '{subject}' | Message-ID: '{msg_id}'"
        )
    else:
        logger.info(f"[CAMPAIGN START] Initial Cold SCO drafted for '{to_email}'. Live SMTP skipped (no credentials).")


def proactive_outreach_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    campaign = coerce_campaign(state["campaign"])
    if is_outreach_started(state):
        logger.info(
            f"[WORKFLOW:Proactive Outreach] Campaign '{campaign.campaign_id}' has already initiated outreach "
            f"(status: '{state.get('deal_status')}', step: {state.get('pipeline_step')}). "
            f"Waiting for counterparty reply."
        )
        return {}

    buyer_name, buyer_email = _resolve_target_buyer_contact(campaign.commodity, state)
    buyers = get_buyers_for_commodity(campaign.commodity)
    suppliers = get_suppliers_for_commodity(campaign.commodity)
    benchmark_fob, freight, bounds, anchor_cif = _calculate_pricing_parameters(
        campaign, state, campaign.origin_port_default, campaign.destination_port
    )

    draft = generate_proactive_sco_draft(campaign, anchor_cif, buyer_name, config=config)
    subject, _ = split_subject_and_body(draft, default_subject=sco_subject(campaign))
    sco_msg_id = make_message_id(settings.email_user.strip())
    _dispatch_sco_email(state, buyer_email, draft, subject, sco_msg_id, campaign.campaign_id)

    sco_entry = AuditTranscriptEntry(
        turn=0,
        sender=f"Trading Desk ({settings.desk_name})",
        recipient=f"{buyer_name} <{buyer_email}>",
        role="agent",
        action="OUTBOUND_SCO",
        subject=subject,
        message=draft,
    )
    logger.info(
        f"[WORKFLOW:Proactive Outreach] Campaign '{campaign.campaign_id}' pitched {campaign.commodity} "
        f"({campaign.target_volume_mt:,.0f} MT) to {buyer_name} ({buyer_email}) at anchor USD {anchor_cif:.2f}/MT "
        f"CIF {campaign.destination_port}"
    )

    update = ProactiveOutreachUpdate(
        campaign=campaign,
        benchmark_fob_usd=benchmark_fob,
        freight_cost_usd=freight,
        dynamic_fob_ceiling=bounds.dynamic_fob_ceiling,
        dynamic_cif_floor=bounds.dynamic_cif_floor,
        anchor_cif_usd=anchor_cif,
        evaluation_reason=(
            f"Proactive SCO dispatched to {buyer_name} at USD {anchor_cif:.2f}/MT CIF. "
            f"Discovered {len(buyers)} buyers and {len(suppliers)} suppliers."
        ),
        buyer_draft=draft,
        audit_transcript=[*(state.get("audit_transcript") or []), sco_entry.model_dump(exclude_none=True)],
        thread_subject=subject,
        last_buyer_message_id=sco_msg_id,
        buyer_references=sco_msg_id,
        discovered_buyers=[b.model_dump() for b in buyers],
        discovered_suppliers=[s.model_dump() for s in suppliers],
        target_buyer_email=buyer_email,
        target_buyer_name=buyer_name,
    )
    res = update.model_dump(exclude_none=True)
    res["campaign"] = campaign
    return res


def _counter_buyer_draft(
    campaign: Campaign, buyer: Optional[ParsedEmail], maximizing: bool, curr_round: int, target_price: float,
    cif_floor: float, reason: str,
) -> tuple[str, str]:
    port = campaign.destination_port
    if maximizing and buyer:
        subject = f"Negotiation Round {curr_round}: Price Adjustment Request — {campaign.commodity} CIF {port}"
        body = (
            f"Dear Buyer,\n\n"
            f"Thank you for your proposal at USD {buyer.price_usd_per_mt:.2f}/MT. Due to tightened carrier container "
            f"allocations and updated logistics/freight surcharges on the corridor, our desk requests a modest price "
            f"adjustment to USD {target_price:.2f}/MT CIF {port}.\n\n"
            f"Desk Analysis: {reason}\n\n"
            f"Please confirm if you can accommodate this revised rate to execute the contract.\n\n"
            f"Warm regards,\nTrading Desk, {settings.desk_name}"
        )
        return subject, body
    subject = f"Counter-Offer — {campaign.commodity} CIF {port}"
    body = (
        f"Dear Buyer,\n\n"
        f"Thank you for your bid. Due to prevailing market benchmark rates and shipping tariffs, "
        f"our minimum viable selling price is USD {cif_floor:.2f}/MT CIF {port}.\n\n"
        f"Desk Note: {reason}\n\n"
        f"Kindly advise if we can proceed at this level.\n\n"
        f"Warm regards,\nTrading Desk, {settings.desk_name}"
    )
    return subject, body


def _resolve_target_counter_cif(
    state: DealState, buyer: Optional[ParsedEmail], action: Optional[str], cif_floor: float
) -> float:
    # A trader override applies to this counter only; last_counter_cif_usd is a record, not an input,
    # otherwise every later counter would repeat the first one.
    if state.get("override_cif_price") is not None:
        return float(state["override_cif_price"])
    if action == "COUNTER_TO_MAXIMIZE" and buyer:
        return round(buyer.price_usd_per_mt + settings.buyer_counter_step_usd_per_mt, 2)
    return cif_floor


def counter_buyer_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    cif_floor = state.get("dynamic_cif_floor", 1000.0)
    campaign = coerce_campaign(state["campaign"])
    buyer = coerce_terms(state.get("buyer_terms"))
    reason = state.get("evaluation_reason", "")
    curr_round = state.get("negotiation_round", 1)
    action = state.get("action")

    target_price = _resolve_target_counter_cif(state, buyer, action, cif_floor)
    prompt = BUYER_COUNTER_PROMPT.format(
        desk_name=settings.desk_name,
        buyer_name="Procurement Team",
        commodity=campaign.commodity,
        destination_port=campaign.destination_port,
        buyer_offered_cif=buyer.price_usd_per_mt if buyer else 0.0,
        target_cif=target_price,
        curr_round=curr_round,
        max_rounds=campaign.max_negotiation_rounds,
        action=action or "FLOOR_DEFENSE",
        evaluation_reason=reason,
    )
    fallback = _counter_buyer_draft(
        campaign, buyer, action == "COUNTER_TO_MAXIMIZE", curr_round, target_price, cif_floor, reason
    )
    buyer_msg = generate_email_draft(prompt, *fallback, config=config)

    logger.info(
        f"[WORKFLOW:Node 4B Counter Buyer] Round {curr_round}: Action='{action}', "
        f"countering buyer at USD {target_price:.2f}/MT CIF {campaign.destination_port} (Reason: {reason})"
    )
    update = CounterBuyerUpdate(deal_status="counter_sent", buyer_draft=buyer_msg, last_counter_cif_usd=target_price)
    return update.model_dump()
