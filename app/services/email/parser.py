"""
Inbound email and webhook payload normalizer.
"""

from __future__ import annotations

from typing import Any, Optional
from app.services.email.thread_tracker import extract_thread_ref


def parse_inbound_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize diverse inbound email webhook formats (Resend, SendGrid, raw JSON)
    into a canonical domain structure.
    """
    subject = payload.get("subject") or payload.get("headers", {}).get("subject") or ""
    sender = payload.get("from") or payload.get("sender") or ""
    body = payload.get("text") or payload.get("body") or payload.get("raw_email") or ""

    campaign_id, thread_id = extract_thread_ref(subject)

    # Detect role from sender email or body keywords if not explicitly stated
    sender_role = payload.get("sender_role")
    if not sender_role:
        lower_body = (subject + " " + body).lower()
        if "purchase" in lower_body or "cif" in lower_body or "inquiry" in lower_body:
            sender_role = "buyer"
        else:
            sender_role = "supplier"

    return {
        "campaign_id": campaign_id or payload.get("campaign_id", ""),
        "thread_id": thread_id or payload.get("thread_id", ""),
        "sender": sender,
        "subject": subject,
        "body": body,
        "sender_role": sender_role,
    }
