"""
Email services package for transport, parsing, and thread reference tracking.
"""

from app.services.email.parser import parse_inbound_webhook_payload
from app.services.email.thread_tracker import (
    encode_thread_subject,
    extract_thread_ref,
    generate_thread_id,
)
from app.services.email.transport import (
    EmailTransport,
    MockEmailTransport,
    get_email_transport,
)

__all__ = [
    "EmailTransport",
    "MockEmailTransport",
    "encode_thread_subject",
    "extract_thread_ref",
    "generate_thread_id",
    "get_email_transport",
    "parse_inbound_webhook_payload",
]
