"""Node: proactive outreach. Prices the opening anchor and pitches a cold SCO to the primary buyer."""
from typing import Optional

from langchain_core.runnables import RunnableConfig

from app.agents import fallback_drafts
from app.agents.prompts import PROACTIVE_COLD_SCO_PROMPT
from app.agents.state import DealState, coerce_campaign, is_outreach_started
from app.agents.updates import ProactiveOutreachUpdate
from app.config import settings
from app.logger import get_logger
from app.models import AuditTranscriptEntry, Campaign
from app.services.directory import get_buyers_for_commodity, get_suppliers_for_commodity, select_primary_buyer
from app.services.freight import estimate_freight
from app.services.llm import generate_email_draft
from app.services.market import get_benchmark_rate
from app.services.pricing import DynamicBounds, calculate_campaign_bounds, resolve_buffer_usd
from app.services.smtp import send_email
from app.utils.email_text import make_message_id, split_subject_and_body

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


def generate_proactive_sco_draft(
    campaign: Campaign, anchor_cif: float, buyer_name: str = "Procurement Partner", config: Optional[dict] = None
) -> str:
    prompt = PROACTIVE_COLD_SCO_PROMPT.format(
        desk_name=settings.desk_name,
        buyer_name=buyer_name,
        destination_port=campaign.destination_port,
        commodity=campaign.commodity,
        broken_percentage=campaign.broken_percentage,
        target_volume_mt=campaign.target_volume_mt,
        anchor_cif_usd=anchor_cif,
        payment_terms=settings.default_payment_terms,
    )
    return generate_email_draft(prompt, *fallback_drafts.sco_draft(campaign, anchor_cif, buyer_name), config=config)


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
    subject, _ = split_subject_and_body(draft, default_subject=fallback_drafts.sco_subject(campaign))
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
