"""Gemini access. The LLM only extracts terms from text and drafts prose; every draft has a template fallback."""
import json
import re
from collections.abc import Iterator
from typing import Any, Optional

from app.config import settings
from app.logger import get_logger
from app.models import Campaign, ParsedEmail
from app.prompts import EMAIL_PARSER_PROMPT, PROACTIVE_COLD_SCO_PROMPT
from app.services.mail.email_utils import split_subject_and_body

logger = get_logger("arbitrage_desk.workflow")


def get_chat_model():
    """ChatGoogleGenerativeAI when GEMINI_API_KEY is set, else None."""
    if not settings.gemini_api_key:
        return None
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.gemini_api_key,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    except Exception:
        return None


def extract_message_text(content: Any) -> str:
    """Message content (str or list of parts) as one string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif hasattr(item, "text"):
                parts.append(str(item.text))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content) if content is not None else ""


def _invoke_langchain(prompt: str, config: Optional[dict]) -> str:
    llm = get_chat_model()
    if not llm:
        return ""
    resp = llm.invoke(prompt, config=config)
    return extract_message_text(resp.content if hasattr(resp, "content") else resp)


def _invoke_genai_sdk(prompt: str) -> str:
    import google.generativeai as genai

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(settings.gemini_model, generation_config={"temperature": settings.llm_temperature})
    resp = model.generate_content(prompt, request_options={"timeout": settings.llm_timeout_seconds})
    return resp.text if resp else ""


def iter_gemini_responses(prompt: str, config: Optional[dict] = None) -> Iterator[str]:
    """Yield Gemini response texts: LangChain first (traced via config), then the google-generativeai SDK.

    Callers stop at the first response they can use; a failed or empty attempt moves on to the next one.
    """
    attempts = [lambda: _invoke_langchain(prompt, config)]
    if settings.gemini_api_key:
        attempts.append(lambda: _invoke_genai_sdk(prompt))
    for attempt in attempts:
        try:
            text = attempt()
        except Exception as exc:
            logger.debug(f"[LLM] Gemini attempt failed ({exc.__class__.__name__}: {exc})")
            continue
        if text:
            yield text


def generate_email_draft(prompt: str, fallback_subject: str, fallback_body: str, config: Optional[dict] = None) -> str:
    """'Subject: ...\\n\\nbody' from Gemini, or from the template when no usable response arrives."""
    for text in iter_gemini_responses(prompt, config):
        subject, body = split_subject_and_body(text, default_subject=fallback_subject)
        if body:
            return f"Subject: {subject}\n\n{body}"
    return f"Subject: {fallback_subject}\n\n{fallback_body}"


def sco_subject(campaign: Campaign) -> str:
    return f"Soft Corporate Offer (SCO) — {campaign.commodity} CIF {campaign.destination_port}"


def _sco_draft(campaign: Campaign, anchor_cif: float, buyer_name: str) -> tuple[str, str]:
    subject = sco_subject(campaign)
    body = (
        f"Dear Procurement Team at {buyer_name},\n\n"
        f"We are pleased to extend this formal Soft Corporate Offer (SCO) for prime export grade "
        f"{campaign.commodity}:\n\n"
        f"• Commodity Variety: {campaign.commodity} (Max {campaign.broken_percentage}% Broken grain)\n"
        f"• Parcel Volume: {campaign.target_volume_mt:,.0f} MT (Containerized 20ft FCL)\n"
        f"• Indicative Offer Price: USD {anchor_cif:.2f}/MT CIF {campaign.destination_port}\n"
        f"• Delivery Term: CIF {campaign.destination_port}\n"
        f"• Payment Terms: {settings.default_payment_terms}\n"
        f"• Inspection: SGS / Bureau Veritas quality certificate at load port\n\n"
        f"Please reply with your target acceptance CIF level or counter-bid to secure shipment allocation.\n\n"
        f"Warm regards,\nTrading Desk, {settings.desk_name}"
    )
    return subject, body


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
    return generate_email_draft(prompt, *_sco_draft(campaign, anchor_cif, buyer_name), config=config)


# Explicit acceptance phrases only. Generic trade words ("confirm your price", "order", "deal",
# "allocation") appear in nearly every counter-offer and must not be read as acceptance.
_ACCEPTANCE_RE = re.compile(
    r"\b(?:we|i)\s+(?:hereby\s+)?accept\b"
    r"|\baccepted\b"
    r"|\bagreed\b"
    r"|\bwe\s+agree\b"
    r"|\b(?:we\s+)?confirm(?:ed)?\s+(?:the|this|our|your)\s+(?:order|deal|offer|contract|purchase)\b"
    r"|\b(?:please\s+)?proceed\s+with\s+(?:the|this)\s+(?:order|deal|contract|shipment)\b",
    re.IGNORECASE,
)


_NEGATED_ACCEPTANCE_RE = re.compile(
    r"\b(?:not|cannot|can't|unable\s+to|won't|will\s+not|do\s+not|don't)\s+(?:\w+\s+){0,2}(?:accept|agree|proceed|confirm)",
    re.IGNORECASE,
)


_PRICE_AFTER_CURRENCY_RE = re.compile(r"(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)


_PRICE_BEFORE_UNIT_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:USD|US\$|\$|per\s*MT|/\s*MT)", re.IGNORECASE)


_QUANTITY_RE = re.compile(r"(\d[\d,]*)\s*(?:MT|metric\s*ton)", re.IGNORECASE)


_KNOWN_PORTS = ("Jebel Ali", "Dammam", "Karachi", "Mundra", "Bangkok")


def _extract_price(raw_text: str) -> float:
    match = _PRICE_AFTER_CURRENCY_RE.search(raw_text) or _PRICE_BEFORE_UNIT_RE.search(raw_text)
    return float(match.group(1).replace(",", "")) if match else 0.0


def _is_explicit_acceptance(raw_text: str) -> bool:
    return bool(_ACCEPTANCE_RE.search(raw_text)) and not _NEGATED_ACCEPTANCE_RE.search(raw_text)


def _extract_quantity(raw_text: str, default_volume_mt: float) -> float:
    match = _QUANTITY_RE.search(raw_text)
    return float(match.group(1).replace(",", "")) if match else default_volume_mt


def _extract_incoterm(raw_text: str, role: str) -> str:
    has_cif = re.search(r"\bCIF\b", raw_text, re.IGNORECASE) is not None
    has_fob = re.search(r"\bFOB\b", raw_text, re.IGNORECASE) is not None
    if has_cif != has_fob:
        return "CIF" if has_cif else "FOB"
    return "CIF" if role == "buyer" else "FOB"


def _extract_port(raw_text: str, role: str) -> str:
    text_lower = raw_text.lower()
    for port in _KNOWN_PORTS:
        if port.lower() in text_lower:
            return port
    return settings.default_destination_port if role == "buyer" else settings.default_origin_port


def extract_terms_with_regex(
    raw_text: str,
    role: str,
    last_proposed_price: float,
    commodity: str,
    default_volume_mt: float,
) -> ParsedEmail:
    price = _extract_price(raw_text)
    is_acceptance = _is_explicit_acceptance(raw_text)
    intent = "ACCEPTANCE" if is_acceptance else ("COUNTER_OFFER" if price > 0 else "INQUIRY")
    if price <= 0.0 and is_acceptance:
        price = last_proposed_price

    parsed = ParsedEmail(
        sender_role=role,
        commodity=commodity,
        quantity_mt=_extract_quantity(raw_text, default_volume_mt),
        price_usd_per_mt=price,
        incoterm=_extract_incoterm(raw_text, role),
        port=_extract_port(raw_text, role),
        payment_terms=settings.default_payment_terms,
        intent=intent,
        is_acceptance=is_acceptance,
        summary="Extracted via deterministic fallback parser",
    )
    logger.info(
        f"[REGEX PARSER] Fallback extracted terms for '{role}': intent='{parsed.intent}', "
        f"is_acceptance={parsed.is_acceptance}, price=${parsed.price_usd_per_mt:.2f}/MT, "
        f"qty={parsed.quantity_mt:,.0f}MT, port='{parsed.port}'"
    )
    return parsed


DEFAULT_PROPOSED_PRICE = 1111.0


def _parse_terms_json(text: str, role: str) -> ParsedEmail:
    data = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
    if isinstance(data, dict):
        data.setdefault("sender_role", role)
    return ParsedEmail(**data)


def extract_terms_with_llm(
    raw_text: str,
    role: str,
    last_proposed_price: float,
    commodity: str,
    default_volume_mt: float,
    config: Optional[dict] = None,
) -> Optional[ParsedEmail]:
    if not settings.gemini_api_key:
        return None
    prompt = EMAIL_PARSER_PROMPT.format(
        role=role,
        last_proposed_price=last_proposed_price,
        commodity=commodity,
        default_volume_mt=default_volume_mt,
        raw_email=raw_text,
    )
    for text in iter_gemini_responses(prompt, config):
        try:
            parsed = _parse_terms_json(text, role)
        except Exception as exc:
            logger.warning(f"[LLM PARSER] Unusable extraction response ({exc.__class__.__name__}: {exc}).")
            continue
        logger.info(
            f"[LLM PARSER] Inbound email parsed for role='{role}': "
            f"intent='{parsed.intent}', is_acceptance={parsed.is_acceptance}, "
            f"price=${parsed.price_usd_per_mt:.2f}/MT, qty={parsed.quantity_mt:,.0f}MT, "
            f"port='{parsed.port}', summary='{parsed.summary}'"
        )
        return parsed
    logger.warning("[LLM PARSER] Semantic LLM extraction unavailable. Switching to deterministic regex parser.")
    return None


def extract_email_terms(
    raw_text: str,
    role: str,
    deal_context: Optional[dict] = None,
    config: Optional[dict] = None,
) -> ParsedEmail:
    ctx = deal_context or {}
    last_proposed_price = ctx.get("last_proposed_price", DEFAULT_PROPOSED_PRICE)
    commodity = ctx.get("commodity", settings.default_commodity)
    default_volume_mt = ctx.get("default_volume_mt", settings.default_target_volume_mt)

    llm_result = extract_terms_with_llm(
        raw_text, role, last_proposed_price, commodity, default_volume_mt, config=config or ctx.get("config")
    )
    if llm_result is not None:
        return llm_result
    return extract_terms_with_regex(raw_text, role, last_proposed_price, commodity, default_volume_mt)
