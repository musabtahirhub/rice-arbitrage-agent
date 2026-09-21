"""
LangGraph State Machine for physical commodity arbitrage negotiation.
Orchestrates email parsing, market grounding, deterministic risk gatekeeping,
and counter-offer drafting.
"""
import json
import re
from typing import Literal
from langgraph.graph import StateGraph, END

from app.config import settings
from app.directory import get_suppliers_for_commodity
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


# ---------------------------------------------------------------------------
# Dynamic LLM Drafting & Parsing Helpers
# ---------------------------------------------------------------------------

def split_subject_and_body(raw_text: str, default_subject: str = "Trade Correspondence") -> tuple[str, str]:
    """
    Extract SUBJECT and BODY from dynamic LLM generation.
    Handles 'SUBJECT: ...\nBODY:\n...' format as well as standard RFC Subject headers.
    """
    if not raw_text:
        return default_subject, ""

    raw = raw_text.strip()

    # Format 1: Strict SUBJECT: ... and BODY: ... format
    sub_match = re.search(r"^SUBJECT:\s*(.+)$", raw, re.MULTILINE | re.IGNORECASE)
    body_match = re.search(r"BODY:\s*\n?(.*)$", raw, re.DOTALL | re.IGNORECASE)
    if sub_match and body_match:
        subject = sub_match.group(1).strip()
        body = body_match.group(1).strip()
        return subject, body

    # Format 2: Standard RFC/Markdown 'Subject: ...\n\n...'
    if raw.lower().startswith("subject:"):
        parts = raw.split("\n\n", 1)
        sub = re.sub(r"^subject:\s*", "", parts[0], flags=re.IGNORECASE).strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        return sub, body

    # Format 3: Subject line on first line without empty line
    first_line, _, rest = raw.partition("\n")
    if first_line.lower().startswith("subject:"):
        sub = re.sub(r"^subject:\s*", "", first_line, flags=re.IGNORECASE).strip()
        return sub, rest.strip()

    return default_subject, raw


def generate_dynamic_llm_draft(prompt: str, fallback_subject: str, fallback_body: str) -> str:
    """
    Invoke Gemini LLM dynamically to write contextual subject and authentic body.
    If LLM API is available and succeeds, returns formatted draft 'Subject: <subject>\n\n<body>'.
    Falls back to safe deterministic fallback if API key is missing or call fails/times out.
    """
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
    """
    Generate dynamic proactive Cold SCO using Gemini LLM (PROACTIVE_COLD_SCO_PROMPT).
    """
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


# ---------------------------------------------------------------------------
# LLM / Regex Parser Helper
# ---------------------------------------------------------------------------

def _parse_email_content(raw_text: str, role: str) -> ParsedEmail:
    """
    Extract commercial terms using Gemini if API key is present,
    otherwise fallback to reliable regex pattern matching.
    """
    if settings.gemini_api_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=settings.gemini_api_key)
            model = genai.GenerativeModel(
                settings.gemini_model,
                generation_config={"temperature": settings.llm_temperature},
            )
            prompt = EMAIL_PARSER_PROMPT.format(raw_email=raw_text)
            resp = model.generate_content(prompt)
            clean_text = resp.text.strip().removeprefix("```json").removesuffix("```").strip()
            data = json.loads(clean_text)
            return ParsedEmail(**data)
        except Exception:
            pass

    # Deterministic Regex Fallback Parser
    p_match = re.search(r"(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)", raw_text, re.IGNORECASE)
    if not p_match:
        p_match = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:USD|US\$|\$|per\s*MT|/\s*MT)", raw_text, re.IGNORECASE)
    price = float(p_match.group(1).replace(",", "")) if p_match else 0.0

    qty_match = re.search(r"(\d[\d,]*)\s*(?:MT|metric\s*ton)", raw_text, re.IGNORECASE)
    quantity = float(qty_match.group(1).replace(",", "")) if qty_match else settings.default_target_volume_mt

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

    return ParsedEmail(
        sender_role=role,
        commodity=settings.default_commodity,
        quantity_mt=quantity,
        price_usd_per_mt=price,
        incoterm=incoterm,
        port=port,
        payment_terms=settings.default_payment_terms,
    )


# ---------------------------------------------------------------------------
# LangGraph Workflow Nodes
# ---------------------------------------------------------------------------

def parse_incoming_email_node(state: DealState) -> dict:
    """
    Node 1: Extract structured trade terms from the latest inbound email.
    When buyer signals interest, lock buyer_terms and auto-draft supplier RFQ with target FOB ceiling.
    """
    raw_email = state.get("latest_email", "")
    role = state.get("active_role", "buyer")
    terms = _parse_email_content(raw_email, role)
    campaign: Campaign = state["campaign"]

    updates = {}
    if role == "buyer":
        updates["buyer_terms"] = terms
        updates["pipeline_step"] = 2
        # Sequential Sourcing Progression: If supplier terms not yet locked, auto-draft RFQ to Asian mill
        if not state.get("supplier_terms"):
            freight = state.get("freight_cost_usd") or estimate_freight(campaign.origin_port_default, campaign.destination_port)
            buffer_usd = campaign.buffer_usd_per_mt if campaign.buffer_usd_per_mt is not None else settings.default_buffer_usd_per_mt
            # Target FOB acquisition ceiling based on buyer CIF minus ocean freight, buffer, and soft margin
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

    return updates


def fetch_market_data_node(state: DealState) -> dict:
    """Node 2: Fetch benchmark index rate and ocean freight estimate."""
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
    """Node 3: Deterministic risk evaluator (margin math + invariant enforcement + tactical hurdles)."""
    campaign: Campaign = state["campaign"]
    buyer = state.get("buyer_terms")
    supplier = state.get("supplier_terms")
    benchmark_fob = state.get("benchmark_fob_usd", 900.0)
    freight = state.get("freight_cost_usd", 50.0)
    curr_round = state.get("negotiation_round", 0) + 1

    result = evaluate_deal(
        campaign=campaign,
        buyer_terms=buyer,
        supplier_terms=supplier,
        benchmark_fob=benchmark_fob,
        freight=freight,
        round_num=curr_round,
    )

    return {
        "is_deal_viable": result["viable"],
        "action": result.get("action"),
        "net_spread_usd": result.get("net_spread", 0.0),
        "net_margin_pct": result["net_margin_pct"],
        "evaluation_reason": result["reason"],
        "negotiation_round": curr_round,
    }


def confirm_deal_node(state: DealState) -> dict:
    """
    Node 4A: Deal Viable & Approved (Spread meets soft target or finalized after bargaining rounds).
    Enforces Zero-Risk Invariant: Supplier allocation is locked first, followed by buyer acceptance.
    Both correspondence pieces are dynamically composed by Gemini LLM.
    """
    buyer = state.get("buyer_terms")
    supplier = state.get("supplier_terms")
    campaign: Campaign = state["campaign"]

    # 1. Dynamic Supplier Volume Lock Confirmation
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

    # 2. Dynamic Buyer SCO Acceptance Confirmation
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

    return {
        "deal_status": "closed",
        "pipeline_step": 4,
        "action": "ACCEPT_AND_CLOSE",
        "supplier_draft": supplier_msg,
        "buyer_draft": buyer_msg,
    }


def counter_buyer_node(state: DealState) -> dict:
    """Node 4B: Deal Non-Viable or Tactical Margin Concession — Draft counter-offer to buyer."""
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

    return {
        "deal_status": "counter_sent",
        "buyer_draft": buyer_msg,
    }


def counter_supplier_node(state: DealState) -> dict:
    """Node 4C: Deal Non-Viable or Tactical Margin Concession — Draft counter-bid to supplier."""
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

    return {
        "deal_status": "counter_sent",
        "supplier_draft": supplier_msg,
    }


def reject_deal_node(state: DealState) -> dict:
    """Node 4D: Hard Limit Rejection — Trade yields less than hard profit hurdle."""
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


# ---------------------------------------------------------------------------
# Routing Logic
# ---------------------------------------------------------------------------

def route_after_evaluation(state: DealState) -> Literal["confirm_deal", "counter_buyer", "counter_supplier", "reject_deal"]:
    """Conditional router determining the next negotiation action."""
    action = state.get("action")
    has_supplier = state.get("supplier_terms") is not None
    active_role = state.get("active_role", "buyer")

    # Hard Limit Check
    if action == "REJECT_HARD":
        return "reject_deal"

    # Optimal or Final Accept
    if action == "ACCEPT_AND_CLOSE" and has_supplier:
        return "confirm_deal"

    # Concession Counter
    if action == "COUNTER_TO_MAXIMIZE":
        if active_role == "buyer":
            return "counter_buyer"
        else:
            return "counter_supplier"

    # Invariant fallback: Missing supplier allocation
    if state.get("is_deal_viable") and has_supplier:
        return "confirm_deal"

    if active_role == "buyer":
        return "counter_buyer"
    else:
        return "counter_supplier"


# ---------------------------------------------------------------------------
# LangGraph Assembly
# ---------------------------------------------------------------------------

def build_arbitrage_graph():
    """Construct and compile the LangGraph StateGraph."""
    builder = StateGraph(DealState)

    builder.add_node("parse_incoming_email", parse_incoming_email_node)
    builder.add_node("fetch_market_data", fetch_market_data_node)
    builder.add_node("evaluate_risk", evaluate_risk_node)
    builder.add_node("confirm_deal", confirm_deal_node)
    builder.add_node("counter_buyer", counter_buyer_node)
    builder.add_node("counter_supplier", counter_supplier_node)
    builder.add_node("reject_deal", reject_deal_node)

    builder.set_entry_point("parse_incoming_email")
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


# Singleton compiled graph instance
trade_graph = build_arbitrage_graph()


def run_full_autonomous_campaign(campaign_id: str):
    """
    Autonomous Multi-Turn Runner delegating to app.main's ledger runner.
    Allows importing run_full_autonomous_campaign directly from app.workflow.
    """
    from app.main import run_full_autonomous_campaign as _runner
    return _runner(campaign_id)

