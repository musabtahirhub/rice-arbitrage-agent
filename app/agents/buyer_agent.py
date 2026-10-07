import email.utils

from app.config import settings
from app.directory import get_buyers_for_commodity, get_suppliers_for_commodity
from app.email_service import send_email
from app.logger import get_logger
from app.market import get_benchmark_rate, estimate_freight
from app.math_engine import calculate_dynamic_bounds
from app.models import Campaign, DealState
from app.prompts import BUYER_COUNTER_PROMPT
from app.services.llm_service import (
    split_subject_and_body,
    generate_dynamic_llm_draft,
    generate_proactive_sco_draft,
)

logger = get_logger("arbitrage_desk.workflow")


def proactive_outreach_node(state: DealState) -> dict:
    campaign: Campaign = state["campaign"]
    commodity = campaign.commodity
    volume = campaign.target_volume_mt
    dest_port = campaign.destination_port
    origin_port = campaign.origin_port_default

    if state.get("pipeline_step", 0) >= 1 or state.get("deal_status") in ["prospecting", "counter_sent", "approved", "closed", "rejected"]:
        logger.info(
            f"[WORKFLOW:Proactive Outreach] Campaign '{campaign.campaign_id}' has already initiated outreach "
            f"(status: '{state.get('deal_status')}', step: {state.get('pipeline_step')}). Waiting for counterparty reply."
        )
        return dict(state)

    buyers = get_buyers_for_commodity(commodity)
    suppliers = get_suppliers_for_commodity(commodity)
    me_buyers = [b for b in buyers if b.country in ["UAE", "Saudi Arabia", "Qatar", "Kuwait", "Oman", "Bahrain"]]
    primary_buyer = me_buyers[0] if me_buyers else (buyers[0] if buyers else None)

    buyer_name = state.get("target_buyer_name") or (primary_buyer.name if primary_buyer else "Procurement Partner")
    buyer_email = primary_buyer.contact_email if primary_buyer else "procurement@domain.com"
    target_buyer_email = state.get("target_buyer_email") or settings.my_test_email or buyer_email

    benchmark_fob = state.get("benchmark_fob_usd") or get_benchmark_rate(commodity, campaign.broken_percentage)
    freight = state.get("freight_cost_usd") or estimate_freight(origin_port, dest_port)
    buffer_usd = campaign.buffer_usd_per_mt if campaign.buffer_usd_per_mt is not None else settings.default_buffer_usd_per_mt

    bounds = calculate_dynamic_bounds(
        benchmark_fob=benchmark_fob,
        freight=freight,
        max_variance_pct=campaign.max_variance_from_benchmark_pct,
        target_margin_pct=campaign.target_margin_pct,
        buffer_usd=buffer_usd,
    )

    anchor_cif = state.get("anchor_cif_usd") or round(benchmark_fob + freight + buffer_usd + campaign.min_profit_per_mt_soft + 30.0, 2)

    initial_buyer_draft = generate_proactive_sco_draft(
        campaign=campaign,
        anchor_cif=anchor_cif,
        buyer_name=buyer_name,
        buyer_email=target_buyer_email,
    )
    sco_subject, _ = split_subject_and_body(
        initial_buyer_draft,
        default_subject=f"Soft Corporate Offer (SCO) — {commodity} CIF {dest_port}",
    )

    clean_desk_user = settings.email_user.strip()
    desk_domain = clean_desk_user.split("@")[1] if "@" in clean_desk_user else "gmail.com"
    sco_msg_id = email.utils.make_msgid(domain=desk_domain)

    if not state.get("skip_email_dispatch", False) and target_buyer_email:
        send_email(
            to_email=target_buyer_email,
            raw_draft=initial_buyer_draft,
            thread_subject=None,
            custom_message_id=sco_msg_id,
        )
        if bool(settings.email_user and settings.email_pass):
            logger.info(
                f"[CAMPAIGN START] Dispatched initial SCO to '{target_buyer_email}' | "
                f"Campaign: '{campaign.campaign_id}' | Subject: '{sco_subject}' | Message-ID: '{sco_msg_id}'"
            )
        else:
            logger.info(
                f"[CAMPAIGN START] Initial Cold SCO drafted for '{buyer_name}' ({target_buyer_email}). "
                f"Live SMTP skipped (transport credentials not configured)."
            )

    outbound_sco_entry = {
        "turn": 0,
        "sender": f"Trading Desk ({settings.desk_name})",
        "recipient": f"{buyer_name} <{target_buyer_email}>",
        "role": "agent",
        "action": "OUTBOUND_SCO",
        "subject": sco_subject,
        "message": initial_buyer_draft,
    }

    transcript = list(state.get("audit_transcript") or [])
    transcript.append(outbound_sco_entry)

    logger.info(
        f"[WORKFLOW:Proactive Outreach] Campaign '{campaign.campaign_id}' pitched {commodity} ({volume:,.0f} MT) "
        f"to {buyer_name} ({target_buyer_email}) at anchor USD {anchor_cif:.2f}/MT CIF {dest_port}"
    )

    return {
        "campaign": campaign,
        "benchmark_fob_usd": benchmark_fob,
        "freight_cost_usd": freight,
        "dynamic_fob_ceiling": bounds["dynamic_fob_ceiling"],
        "dynamic_cif_floor": bounds["dynamic_cif_floor"],
        "target_fob_ceiling": 0.0,
        "anchor_cif_usd": anchor_cif,
        "buyer_terms": None,
        "supplier_terms": None,
        "negotiation_round": 0,
        "deal_status": "prospecting",
        "action": None,
        "pipeline_step": 1,
        "is_deal_viable": False,
        "net_spread_usd": 0.0,
        "net_margin_pct": 0.0,
        "evaluation_reason": f"Proactive SCO dispatched to {buyer_name} at USD {anchor_cif:.2f}/MT CIF. Discovered {len(buyers)} buyers and {len(suppliers)} suppliers.",
        "buyer_draft": initial_buyer_draft,
        "supplier_draft": "",
        "audit_transcript": transcript,
        "thread_subject": sco_subject,
        "last_buyer_message_id": sco_msg_id,
        "buyer_references": sco_msg_id,
        "last_supplier_message_id": None,
        "supplier_references": None,
        "discovered_buyers": [b.model_dump() for b in buyers],
        "discovered_suppliers": [s.model_dump() for s in suppliers],
        "target_buyer_email": target_buyer_email,
        "target_buyer_name": buyer_name,
    }


def counter_buyer_node(state: DealState) -> dict:
    cif_floor = state.get("dynamic_cif_floor", 1000.0)
    campaign: Campaign = state["campaign"]
    buyer = state.get("buyer_terms")
    reason = state.get("evaluation_reason", "")
    curr_round = state.get("negotiation_round", 1)
    action = state.get("action")
    if state.get("last_counter_cif_usd") is not None:
        target_price = float(state["last_counter_cif_usd"])
    elif action == "COUNTER_TO_MAXIMIZE" and buyer:
        target_price = round(buyer.price_usd_per_mt + 25.0, 2)
    else:
        target_price = cif_floor

    buyer_prompt = BUYER_COUNTER_PROMPT.format(
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

    if action == "COUNTER_TO_MAXIMIZE" and buyer:
        fallback_sub = f"Negotiation Round {curr_round}: Price Adjustment Request — {campaign.commodity} CIF {campaign.destination_port}"
        fallback_body = (
            f"Dear Buyer,\n\n"
            f"Thank you for your proposal at USD {buyer.price_usd_per_mt:.2f}/MT. Due to tightened carrier container allocations "
            f"and updated logistics/freight surcharges on the corridor, our desk requests a modest price adjustment "
            f"to USD {target_price:.2f}/MT CIF {campaign.destination_port}.\n\n"
            f"Desk Analysis: {reason}\n\n"
            f"Please confirm if you can accommodate this revised rate to execute the contract.\n\n"
            f"Warm regards,\nTrading Desk, {settings.desk_name}"
        )
    else:
        fallback_sub = f"Counter-Offer — {campaign.commodity} CIF {campaign.destination_port}"
        fallback_body = (
            f"Dear Buyer,\n\n"
            f"Thank you for your bid. Due to prevailing market benchmark rates and shipping tariffs, "
            f"our minimum viable selling price is USD {cif_floor:.2f}/MT CIF {campaign.destination_port}.\n\n"
            f"Desk Note: {reason}\n\n"
            f"Kindly advise if we can proceed at this level.\n\n"
            f"Warm regards,\nTrading Desk, {settings.desk_name}"
        )

    buyer_msg = generate_dynamic_llm_draft(buyer_prompt, fallback_sub, fallback_body)

    logger.info(
        f"[WORKFLOW:Node 4B Counter Buyer] Round {curr_round}: Action='{action}', "
        f"countering buyer at USD {target_price:.2f}/MT CIF {campaign.destination_port} (Reason: {reason})"
    )

    return {
        "deal_status": "counter_sent",
        "buyer_draft": buyer_msg,
        "last_counter_cif_usd": target_price,
    }
