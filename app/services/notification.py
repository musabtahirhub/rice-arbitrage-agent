"""
Operator notification dispatcher for high-value deal authorization alerts.
"""

from __future__ import annotations

from typing import Any
import httpx
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def dispatch_operator_alert(
    campaign_id: str,
    margin_pct: float,
    buyer_info: dict[str, Any],
    supplier_info: dict[str, Any],
    reason: str,
) -> bool:
    """
    Notify human operator desk when a deal satisfies all boundaries and requires execution approval.
    """
    message = (
        f"[!] DEAL AUTHORIZATION REQUIRED | Campaign: {campaign_id}\n"
        f"- Net Profit Margin: {margin_pct:.2f}%\n"
        f"- Buyer: {buyer_info.get('name', 'N/A')} ({buyer_info.get('port', 'N/A')})\n"
        f"- Supplier: {supplier_info.get('name', 'N/A')} ({supplier_info.get('port', 'N/A')})\n"
        f"- Details: {reason}\n"
        f"- Action: Review and authorize at /api/deals/{campaign_id}/authorize"
    )

    logger.info(f"[OPERATOR ALERT DISPATCHED]\n{message}")

    webhook_url = settings.operator_notification_webhook
    if webhook_url:
        try:
            with httpx.Client(timeout=4.0) as client:
                resp = client.post(webhook_url, json={"text": message})
                return resp.status_code < 400
        except Exception as e:
            logger.warning(f"Failed to deliver operator webhook alert: {e}")
            return False

    return True
