"""
Human-in-the-loop operator authorization API routes.
"""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.services.ledger import get_campaign_state, list_campaign_ids, save_campaign_state

logger = get_logger(__name__)
router = APIRouter()


class DealDecisionRequest(BaseModel):
    operator_name: str = Field(default="Senior Desk Trader")
    notes: Optional[str] = Field(default="Authorized for formal contract execution and banking LC.")


@router.get("/deals/pending")
async def list_pending_deals():
    """List all campaigns in approved status awaiting operator execution."""
    pending = []
    for cid in list_campaign_ids():
        state = get_campaign_state(cid)
        if state and state.get("deal_status") in ("approved", "closed"):
            pending.append({
                "campaign_id": cid,
                "commodity": state["campaign"].commodity,
                "net_margin_pct": state.get("net_margin_pct"),
                "deal_status": state.get("deal_status"),
                "is_deal_viable": state.get("is_deal_viable"),
            })
    return {"pending_deals": pending}


@router.post("/deals/{campaign_id}/authorize")
async def authorize_deal(campaign_id: str, req: DealDecisionRequest = DealDecisionRequest()):
    """
    Human operator authorizes binding execution of an approved deal.
    Locks allocation, marks deal binding, and triggers formal banking instructions.
    """
    state = get_campaign_state(campaign_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    if not state.get("is_deal_viable"):
        raise HTTPException(status_code=400, detail="Cannot authorize a non-viable deal.")

    state["deal_status"] = "closed"
    state["operator_authorization"] = {
        "authorized": True,
        "operator": req.operator_name,
        "notes": req.notes,
    }
    save_campaign_state(campaign_id, state)

    logger.info(f"[DEAL AUTHORIZED BY OPERATOR] Campaign: {campaign_id} by {req.operator_name}")
    return {
        "status": "authorized",
        "campaign_id": campaign_id,
        "operator": req.operator_name,
        "notes": req.notes,
        "deal_status": "closed",
    }


@router.post("/deals/{campaign_id}/reject")
async def reject_deal(campaign_id: str, req: DealDecisionRequest = DealDecisionRequest()):
    """
    Human operator manual override to reject an offer.
    """
    state = get_campaign_state(campaign_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    state["deal_status"] = "rejected"
    state["evaluation_reason"] = f"Operator rejection by {req.operator_name}: {req.notes}"
    save_campaign_state(campaign_id, state)

    logger.info(f"[DEAL OVERRIDDEN/REJECTED BY OPERATOR] Campaign: {campaign_id}")
    return {
        "status": "rejected",
        "campaign_id": campaign_id,
        "operator": req.operator_name,
        "reason": state["evaluation_reason"],
    }
