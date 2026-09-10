"""
In-memory deal and campaign ledger service.
"""

from __future__ import annotations

from typing import Any, Optional
from app.models.campaign import CampaignConfig
from app.models.trade import ParsedTradeEmail

# In-memory storage for active campaigns
_CAMPAIGNS: dict[str, dict[str, Any]] = {}


def get_campaign_state(campaign_id: str) -> Optional[dict[str, Any]]:
    return _CAMPAIGNS.get(campaign_id)


def save_campaign_state(campaign_id: str, state: dict[str, Any]) -> None:
    _CAMPAIGNS[campaign_id] = state


def list_campaign_ids() -> list[str]:
    return list(_CAMPAIGNS.keys())


def serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Serialize Pydantic models in state dict for JSON responses."""
    result = {}
    for k, v in state.items():
        if isinstance(v, (CampaignConfig, ParsedTradeEmail)) or hasattr(v, "model_dump"):
            result[k] = v.model_dump()
        else:
            result[k] = v
    return result
