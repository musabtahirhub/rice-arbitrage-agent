"""Gemini access. The LLM only extracts terms from text and drafts prose; every draft has a template fallback."""
from collections.abc import Iterator
from typing import Any, Optional

from app.config import settings
from app.logger import get_logger
from app.utils.email_text import split_subject_and_body

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
    resp = model.generate_content(prompt)
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
