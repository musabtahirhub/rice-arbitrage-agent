"""
Thread reference encoder and decoder for tracking negotiation campaigns in email subjects.
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

THREAD_PATTERN = re.compile(
    r"\[Ref:\s*([A-Za-z0-9_-]+)\s*\|\s*Thread:\s*([A-Za-z0-9_-]+)\]",
    re.IGNORECASE,
)


def generate_thread_id() -> str:
    """Generate a clean short thread identifier."""
    return f"TH-{uuid.uuid4().hex[:6].upper()}"


def encode_thread_subject(subject: str, campaign_id: str, thread_id: str) -> str:
    """
    Append or ensure a tracking token exists in the email subject.
    Example: 'Urgent RFQ [Ref: CAMP-001 | Thread: TH-8F21]'
    """
    token = f"[Ref: {campaign_id} | Thread: {thread_id}]"
    if token.lower() in subject.lower():
        return subject
    clean_sub = THREAD_PATTERN.sub("", subject).strip()
    return f"{clean_sub} {token}".strip()


def extract_thread_ref(subject: str) -> tuple[Optional[str], Optional[str]]:
    """
    Extract (campaign_id, thread_id) from an email subject header.
    Returns (None, None) if no valid signature is present.
    """
    match = THREAD_PATTERN.search(subject)
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2).strip()
