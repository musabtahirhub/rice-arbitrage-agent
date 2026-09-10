"""
Decoupled email transport interface supporting Mock, SMTP, and Resend API.
"""

from __future__ import annotations

import smtplib
from abc import ABC, abstractmethod
from email.mime.text import MIMEText
from typing import Optional

import httpx
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class EmailTransport(ABC):
    @abstractmethod
    def send(self, to_email: str, subject: str, body: str) -> bool:
        """Send email message."""
        pass


class MockEmailTransport(EmailTransport):
    def send(self, to_email: str, subject: str, body: str) -> bool:
        logger.info(f"[MOCK EMAIL OUTBOUND] To: {to_email} | Subject: {subject}\nBody: {body[:150]}...")
        return True


class SmtpEmailTransport(EmailTransport):
    def send(self, to_email: str, subject: str, body: str) -> bool:
        if not settings.smtp_user or not settings.smtp_password:
            logger.warning("SMTP credentials missing; falling back to mock delivery.")
            return False
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = settings.smtp_user
            msg["To"] = to_email

            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
            logger.info(f"[SMTP EMAIL SENT] To: {to_email} | Subject: {subject}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email via SMTP: {e}")
            return False


class ResendEmailTransport(EmailTransport):
    def send(self, to_email: str, subject: str, body: str) -> bool:
        if not settings.resend_api_key:
            logger.warning("Resend API key missing; falling back to mock delivery.")
            return False
        try:
            url = "https://api.resend.com/emails"
            headers = {
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "from": "trading@commoditydesk.io",
                "to": [to_email],
                "subject": subject,
                "text": body,
            }
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
            logger.info(f"[RESEND EMAIL SENT] To: {to_email} | Subject: {subject}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email via Resend: {e}")
            return False


def get_email_transport() -> EmailTransport:
    """Factory returning configured email transport."""
    provider = settings.email_provider.lower()
    if provider == "smtp":
        return SmtpEmailTransport()
    elif provider == "resend":
        return ResendEmailTransport()
    return MockEmailTransport()
