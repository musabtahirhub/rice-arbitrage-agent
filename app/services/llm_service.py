import json
import re
from typing import Optional

from app.config import settings
from app.logger import get_logger
from app.models import Campaign, ParsedEmail
from app.prompts import (
    EMAIL_PARSER_PROMPT,
    PROACTIVE_COLD_SCO_PROMPT,
)

logger = get_logger("arbitrage_desk.workflow")


def _extract_subject_from_headers(raw: str) -> Optional[tuple[str, str]]:
    sub_match = re.search(r"^SUBJECT:\s*(.+)$", raw, re.MULTILINE | re.IGNORECASE)
    body_match = re.search(r"BODY:\s*\n?(.*)$", raw, re.DOTALL | re.IGNORECASE)
    if sub_match and body_match:
        return sub_match.group(1).strip(), body_match.group(1).strip()
    return None


def _extract_subject_from_prefixed_blocks(raw: str) -> Optional[tuple[str, str]]:
    if raw.lower().startswith("subject:"):
        parts = raw.split("\n\n", 1)
        sub = re.sub(r"^subject:\s*", "", parts[0], flags=re.IGNORECASE).strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        return sub, body
    return None


def _extract_subject_from_first_line(raw: str) -> Optional[tuple[str, str]]:
    first_line, _, rest = raw.partition("\n")
    if first_line.lower().startswith("subject:"):
        sub = re.sub(r"^subject:\s*", "", first_line, flags=re.IGNORECASE).strip()
        return sub, rest.strip()
    return None


def split_subject_and_body(raw_text: str, default_subject: str = "Trade Correspondence") -> tuple[str, str]:
    if not raw_text:
        return default_subject, ""

    raw = raw_text.strip()
    header_res = _extract_subject_from_headers(raw)
    if header_res:
        return header_res

    prefix_res = _extract_subject_from_prefixed_blocks(raw)
    if prefix_res:
        return prefix_res

    first_line_res = _extract_subject_from_first_line(raw)
    if first_line_res:
        return first_line_res

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


def _extract_terms_via_gemini_llm(
    raw_text: str,
    role: str,
    last_proposed_price: float,
    commodity: str,
    default_volume_mt: float,
) -> Optional[ParsedEmail]:
    if not settings.gemini_api_key:
        return None
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
        return None


def _extract_price_and_intent(raw_text: str, last_proposed_price: float) -> tuple[float, bool, str]:
    p_match = re.search(r"(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)", raw_text, re.IGNORECASE)
    if not p_match:
        p_match = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:USD|US\$|\$|per\s*MT|/\s*MT)", raw_text, re.IGNORECASE)
    price = float(p_match.group(1).replace(",", "")) if p_match else 0.0

    is_acceptance = bool(re.search(r"\b(accept|agreed|agreement|confirm|proceed|deal|order|allocation)\b", raw_text, re.IGNORECASE))
    intent = "ACCEPTANCE" if is_acceptance else ("COUNTER_OFFER" if price > 0 else "INQUIRY")
    if price <= 0.0 and is_acceptance:
        price = last_proposed_price

    return price, is_acceptance, intent


def _extract_quantity_from_text(raw_text: str, default_volume_mt: float) -> float:
    qty_match = re.search(r"(\d[\d,]*)\s*(?:MT|metric\s*ton)", raw_text, re.IGNORECASE)
    return float(qty_match.group(1).replace(",", "")) if qty_match else default_volume_mt


def _extract_incoterm_and_port(raw_text: str, role: str) -> tuple[str, str]:
    incoterm = "CIF" if "cif" in raw_text.lower() else ("FOB" if "fob" in raw_text.lower() else ("CIF" if role == "buyer" else "FOB"))
    port = settings.default_destination_port if role == "buyer" else settings.default_origin_port

    text_lower = raw_text.lower()
    if "jebel ali" in text_lower:
        port = "Jebel Ali"
    elif "dammam" in text_lower:
        port = "Dammam"
    elif "karachi" in text_lower:
        port = "Karachi"
    elif "mundra" in text_lower:
        port = "Mundra"
    elif "bangkok" in text_lower:
        port = "Bangkok"

    return incoterm, port


def _extract_terms_via_regex_fallback(
    raw_text: str,
    role: str,
    last_proposed_price: float,
    commodity: str,
    default_volume_mt: float,
) -> ParsedEmail:
    price, is_acceptance, intent = _extract_price_and_intent(raw_text, last_proposed_price)
    quantity = _extract_quantity_from_text(raw_text, default_volume_mt)
    incoterm, port = _extract_incoterm_and_port(raw_text, role)

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


def _parse_email_content(raw_text: str, role: str, deal_context: Optional[dict] = None) -> ParsedEmail:
    ctx = deal_context or {}
    last_proposed_price = ctx.get("last_proposed_price", 1111.0)
    commodity = ctx.get("commodity", settings.default_commodity)
    default_volume_mt = ctx.get("default_volume_mt", settings.default_target_volume_mt)

    llm_result = _extract_terms_via_gemini_llm(
        raw_text, role, last_proposed_price, commodity, default_volume_mt
    )
    if llm_result is not None:
        return llm_result

    return _extract_terms_via_regex_fallback(
        raw_text, role, last_proposed_price, commodity, default_volume_mt
    )
