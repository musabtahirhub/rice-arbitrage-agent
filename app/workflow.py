import email.utils
import json
import re
from typing import Literal, Optional
from langgraph.graph import StateGraph, END

from app.config import settings
from app.directory import get_buyers_for_commodity, get_suppliers_for_commodity
from app.email_service import send_email
from app.logger import get_logger
from app.market import get_benchmark_rate, estimate_freight
from app.math_engine import calculate_dynamic_bounds, evaluate_deal
from app.models import Campaign, DealState, ParsedEmail
from app.prompts import (
    BUYER_COUNTER_PROMPT,
    DEAL_CONFIRMATION_PROMPT,
    DEAL_REJECTION_PROMPT,
    EMAIL_PARSER_PROMPT,
    PROACTIVE_COLD_SCO_PROMPT,
    SUPPLIER_RFQ_PROMPT,
)

logger = get_logger("arbitrage_desk.workflow")


def split_subject_and_body(raw_text: str, default_subject: str = "Trade Correspondence") -> tuple[str, str]:
    if not raw_text:
        return default_subject, ""

    raw = raw_text.strip()

    sub_match = re.search(r"^SUBJECT:\s*(.+)$", raw, re.MULTILINE | re.IGNORECASE)
    body_match = re.search(r"BODY:\s*\n?(.*)$", raw, re.DOTALL | re.IGNORECASE)
    if sub_match and body_match:
        subject = sub_match.group(1).strip()
        body = body_match.group(1).strip()
        return subject, body

    if raw.lower().startswith("subject:"):
        parts = raw.split("\n\n", 1)
        sub = re.sub(r"^subject:\s*", "", parts[0], flags=re.IGNORECASE).strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        return sub, body

    first_line, _, rest = raw.partition("\n")
    if first_line.lower().startswith("subject:"):
        sub = re.sub(r"^subject:\s*", "", first_line, flags=re.IGNORECASE).strip()
        return sub, rest.strip()

    return default_subject, raw


def generate_dynamic_llm_draft(prompt: str, fallback_subject: str, fallback_body: str) -> str:
    if settings.gemini_api_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=settings.gemini_api_key)
            model = genai.GenerativeModel(
                settings.gemini_model,
                generation_config={"temperature": settings.llm_temperature},
            )
            resp = model.generate_content(prompt)
            if resp and resp.text:
                subject, body = split_subject_and_body(resp.text, default_subject=fallback_subject)
                if body:
                    return f"Subject: {subject}\n\n{body}"
        except Exception:
            pass

    return f"Subject: {fallback_subject}\n\n{fallback_body}"


def generate_proactive_sco_draft(
    campaign: Campaign,
    anchor_cif: float,
    buyer_name: str = "Procurement Partner",
    buyer_email: str = "procurement@domain.com",
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
    fallback_sub = f"Soft Corporate Offer (SCO) — {campaign.commodity} CIF {campaign.destination_port}"
    fallback_body = (
        f"Dear Procurement Team at {buyer_name},\n\n"
        f"We are pleased to extend this formal Soft Corporate Offer (SCO) for prime export grade {campaign.commodity}:\n\n"
        f"• Commodity Variety: {campaign.commodity} (Max {campaign.broken_percentage}% Broken grain)\n"
        f"• Parcel Volume: {campaign.target_volume_mt:,.0f} MT (Containerized 20ft FCL)\n"
        f"• Indicative Offer Price: USD {anchor_cif:.2f}/MT CIF {campaign.destination_port}\n"
        f"• Delivery Term: CIF {campaign.destination_port}\n"
        f"• Payment Terms: {settings.default_payment_terms}\n"
        f"• Inspection: SGS / Bureau Veritas quality certificate at load port\n\n"
        f"Please reply with your target acceptance CIF level or counter-bid to secure shipment allocation.\n\n"
        f"Warm regards,\nTrading Desk, {settings.desk_name}"
    )
    return generate_dynamic_llm_draft(prompt, fallback_sub, fallback_body)


def _parse_email_content(raw_text: str, role: str, deal_context: Optional[dict] = None) -> ParsedEmail:
    ctx = deal_context or {}
    last_proposed_price = ctx.get("last_proposed_price", 1111.0)
    commodity = ctx.get("commodity", settings.default_commodity)
    default_volume_mt = ctx.get("default_volume_mt", settings.default_target_volume_mt)

    if settings.gemini_api_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=settings.gemini_api_key)
            model = genai.GenerativeModel(
                settings.gemini_model,
                generation_config={"temperature": settings.llm_temperature},
            )
            prompt = EMAIL_PARSER_PROMPT.format(
                role=role,
                last_proposed_price=last_proposed_price,
                commodity=commodity,
                default_volume_mt=default_volume_mt,
                raw_email=raw_text,
            )
            resp = model.generate_content(prompt)
            clean_text = resp.text.strip().removeprefix("```json").removesuffix("```").strip()
            data = json.loads(clean_text)
            parsed = ParsedEmail(**data)
            logger.info(
                f"[LLM PARSER] Inbound email parsed for role='{role}': "
                f"intent='{parsed.intent}', is_acceptance={parsed.is_acceptance}, "
                f"price=${parsed.price_usd_per_mt:.2f}/MT, qty={parsed.quantity_mt:,.0f}MT, "
                f"port='{parsed.port}', summary='{parsed.summary}'"
            )
            return parsed
        except Exception as exc:
            logger.warning(
                f"[LLM PARSER] Semantic LLM extraction unavailable ({exc.__class__.__name__}: {exc}). "
                f"Switching to deterministic regex parser."
            )

    p_match = re.search(r"(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)", raw_text, re.IGNORECASE)
    if not p_match:
        p_match = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:USD|US\$|\$|per\s*MT|/\s*MT)", raw_text, re.IGNORECASE)
    price = float(p_match.group(1).replace(",", "")) if p_match else 0.0

    is_acceptance = bool(re.search(r"\b(accept|agreed|agreement|confirm|proceed|deal|order|allocation)\b", raw_text, re.IGNORECASE))
    intent = "ACCEPTANCE" if is_acceptance else ("COUNTER_OFFER" if price > 0 else "INQUIRY")
    if price <= 0.0 and is_acceptance:
        price = last_proposed_price

    qty_match = re.search(r"(\d[\d,]*)\s*(?:MT|metric\s*ton)", raw_text, re.IGNORECASE)
    quantity = float(qty_match.group(1).replace(",", "")) if qty_match else default_volume_mt

    incoterm = "CIF" if "cif" in raw_text.lower() else ("FOB" if "fob" in raw_text.lower() else ("CIF" if role == "buyer" else "FOB"))

    port = settings.default_destination_port if role == "buyer" else settings.default_origin_port
    if "jebel ali" in raw_text.lower():
        port = "Jebel Ali"
    elif "dammam" in raw_text.lower():
        port = "Dammam"
    elif "karachi" in raw_text.lower():
        port = "Karachi"
    elif "mundra" in raw_text.lower():
        port = "Mundra"
    elif "bangkok" in raw_text.lower():
        port = "Bangkok"

    fallback_parsed = ParsedEmail(
        sender_role=role,
        commodity=commodity,
        quantity_mt=quantity,
        price_usd_per_mt=price,
        incoterm=incoterm,
        port=port,
        payment_terms=settings.default_payment_terms,
        intent=intent,
        is_acceptance=is_acceptance,
        summary="Extracted via deterministic fallback parser",
    )
    logger.info(
        f"[REGEX PARSER] Fallback extracted terms for '{role}': intent='{fallback_parsed.intent}', "
        f"is_acceptance={fallback_parsed.is_acceptance}, price=${fallback_parsed.price_usd_per_mt:.2f}/MT, "
        f"qty={fallback_parsed.quantity_mt:,.0f}MT, port='{fallback_parsed.port}'"
    )
    return fallback_parsed


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
    }


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


def counter_buyer_node(state: DealState) -> dict:
    cif_floor = state.get("dynamic_cif_floor", 1000.0)
    campaign: Campaign = state["campaign"]
    buyer = state.get("buyer_terms")
    reason = state.get("evaluation_reason", "")
    curr_round = state.get("negotiation_round", 1)
    action = state.get("action")

    target_price = round(buyer.price_usd_per_mt + 25.0, 2) if (action == "COUNTER_TO_MAXIMIZE" and buyer) else cif_floor

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


def counter_supplier_node(state: DealState) -> dict:
    fob_ceiling = state.get("dynamic_fob_ceiling", 945.0)
    campaign: Campaign = state["campaign"]
    supplier = state.get("supplier_terms")
    reason = state.get("evaluation_reason", "")
    curr_round = state.get("negotiation_round", 1)
    action = state.get("action")

    target_ceiling = round(supplier.price_usd_per_mt - 20.0, 2) if (action == "COUNTER_TO_MAXIMIZE" and supplier) else fob_ceiling

    supp_prompt = SUPPLIER_RFQ_PROMPT.format(
        desk_name=settings.desk_name,
        supplier_name="Export Team",
        commodity=campaign.commodity,
        broken_percentage=campaign.broken_percentage,
        target_volume_mt=supplier.quantity_mt if supplier else campaign.target_volume_mt,
        origin_port=campaign.origin_port_default,
        fob_ceiling=target_ceiling,
        supplier_offered_text=f"USD {supplier.price_usd_per_mt:.2f}/MT FOB" if supplier else "Pending",
        action=action or "CEILING_ENFORCEMENT",
        evaluation_reason=reason,
    )

    if action == "COUNTER_TO_MAXIMIZE" and supplier:
        fallback_sub = f"Negotiation Round {curr_round}: Volume Discount Request — {campaign.commodity} FOB {campaign.origin_port_default}"
        fallback_body = (
            f"Dear Exporter,\n\n"
            f"Thank you for your quotation of USD {supplier.price_usd_per_mt:.2f}/MT. Given our export volume commitment "
            f"and ready LC facility, we request an export bulk discount to USD {target_ceiling:.2f}/MT FOB {campaign.origin_port_default}.\n\n"
            f"Desk Analysis: {reason}\n\n"
            f"Please confirm if you can confirm allocation at this revised level.\n\n"
            f"Warm regards,\nProcurement Desk, {settings.desk_name}"
        )
    else:
        fallback_sub = f"Counter-Bid — {campaign.commodity} FOB {campaign.origin_port_default}"
        fallback_body = (
            f"Dear Exporter,\n\n"
            f"Thank you for your quotation. Based on our container freight and volume commitments, "
            f"our ceiling acquisition price for this parcel is USD {fob_ceiling:.2f}/MT FOB.\n\n"
            f"Desk Note: {reason}\n\n"
            f"Please confirm if you can meet our target price to lock this allocation.\n\n"
            f"Warm regards,\nProcurement Desk, {settings.desk_name}"
        )

    supplier_msg = generate_dynamic_llm_draft(supp_prompt, fallback_sub, fallback_body)

    logger.info(
        f"[WORKFLOW:Node 4C Counter Supplier] Round {curr_round}: Action='{action}', "
        f"countering supplier with target FOB ceiling USD {target_ceiling:.2f}/MT"
    )

    return {
        "deal_status": "counter_sent",
        "supplier_draft": supplier_msg,
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


def route_after_evaluation(state: DealState) -> Literal["confirm_deal", "counter_buyer", "counter_supplier", "reject_deal"]:
    action = state.get("action")
    has_supplier = state.get("supplier_terms") is not None
    active_role = state.get("active_role", "buyer")

    if action == "REJECT_HARD":
        return "reject_deal"

    if action == "ACCEPT_AND_CLOSE" and has_supplier:
        return "confirm_deal"

    if action == "COUNTER_TO_MAXIMIZE":
        if active_role == "buyer":
            return "counter_buyer"
        else:
            return "counter_supplier"

    if state.get("is_deal_viable") and has_supplier:
        return "confirm_deal"

    if active_role == "buyer":
        return "counter_buyer"
    else:
        return "counter_supplier"


def route_entry_point(state: DealState) -> Literal["proactive_outreach", "parse_incoming_email", "__end__"]:
    latest_email = (state.get("latest_email") or "").strip()
    if latest_email:
        return "parse_incoming_email"

    if state.get("pipeline_step", 0) >= 1 or state.get("deal_status") in ["prospecting", "counter_sent", "approved", "closed", "rejected"]:
        camp = state.get("campaign")
        cid = getattr(camp, "campaign_id", None) or (camp.get("campaign_id") if isinstance(camp, dict) else "unknown")
        logger.info(
            f"[WORKFLOW:Router] Campaign '{cid}' is waiting for counterparty reply (status: '{state.get('deal_status')}'). "
            f"No inbound email present; halting at END without duplicate outreach."
        )
        return END

    return "proactive_outreach"


def build_trade_graph():
    builder = StateGraph(DealState)

    builder.add_node("proactive_outreach", proactive_outreach_node)
    builder.add_node("parse_incoming_email", parse_incoming_email_node)
    builder.add_node("fetch_market_data", fetch_market_data_node)
    builder.add_node("evaluate_risk", evaluate_risk_node)
    builder.add_node("confirm_deal", confirm_deal_node)
    builder.add_node("counter_buyer", counter_buyer_node)
    builder.add_node("counter_supplier", counter_supplier_node)
    builder.add_node("reject_deal", reject_deal_node)

    builder.set_conditional_entry_point(
        route_entry_point,
        {
            "proactive_outreach": "proactive_outreach",
            "parse_incoming_email": "parse_incoming_email",
            END: END,
        },
    )

    builder.add_edge("proactive_outreach", END)

    builder.add_edge("parse_incoming_email", "fetch_market_data")
    builder.add_edge("fetch_market_data", "evaluate_risk")

    builder.add_conditional_edges(
        "evaluate_risk",
        route_after_evaluation,
        {
            "confirm_deal": "confirm_deal",
            "counter_buyer": "counter_buyer",
            "counter_supplier": "counter_supplier",
            "reject_deal": "reject_deal",
        },
    )

    builder.add_edge("confirm_deal", END)
    builder.add_edge("counter_buyer", END)
    builder.add_edge("counter_supplier", END)
    builder.add_edge("reject_deal", END)

    return builder.compile()


build_arbitrage_graph = build_trade_graph
trade_graph = build_trade_graph()


def run_full_autonomous_campaign(campaign_id: str):
    from app.main import run_full_autonomous_campaign as _runner
    return _runner(campaign_id)

