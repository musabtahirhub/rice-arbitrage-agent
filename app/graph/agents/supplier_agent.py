"""Supplier-side node: counter the supplier for a discount or to enforce the FOB ceiling."""
from typing import Optional

from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.logger import get_logger
from app.models import Campaign, DealState, ParsedEmail, coerce_campaign, coerce_terms
from app.prompts import (
    SUPPLIER_RFQ_PROMPT,
)
from app.schemas import (
    CounterSupplierUpdate,
)
from app.services.llm_service import (
    generate_email_draft,
)

logger = get_logger("arbitrage_desk.workflow")


def _counter_supplier_draft(
    campaign: Campaign, supplier: Optional[ParsedEmail], maximizing: bool, curr_round: int, target_ceiling: float,
    fob_ceiling: float, reason: str,
) -> tuple[str, str]:
    port = campaign.origin_port_default
    if maximizing and supplier:
        subject = f"Negotiation Round {curr_round}: Volume Discount Request — {campaign.commodity} FOB {port}"
        body = (
            f"Dear Exporter,\n\n"
            f"Thank you for your quotation of USD {supplier.price_usd_per_mt:.2f}/MT. Given our export volume "
            f"commitment and ready LC facility, we request an export bulk discount to USD {target_ceiling:.2f}/MT "
            f"FOB {port}.\n\n"
            f"Desk Analysis: {reason}\n\n"
            f"Please confirm if you can confirm allocation at this revised level.\n\n"
            f"Warm regards,\nProcurement Desk, {settings.desk_name}"
        )
        return subject, body
    subject = f"Counter-Bid — {campaign.commodity} FOB {port}"
    body = (
        f"Dear Exporter,\n\n"
        f"Thank you for your quotation. Based on our container freight and volume commitments, "
        f"our ceiling acquisition price for this parcel is USD {fob_ceiling:.2f}/MT FOB.\n\n"
        f"Desk Note: {reason}\n\n"
        f"Please confirm if you can meet our target price to lock this allocation.\n\n"
        f"Warm regards,\nProcurement Desk, {settings.desk_name}"
    )
    return subject, body


def _calculate_target_supplier_ceiling(
    supplier: Optional[ParsedEmail], action: Optional[str], fob_ceiling: float
) -> float:
    if action == "COUNTER_TO_MAXIMIZE" and supplier:
        return round(supplier.price_usd_per_mt - settings.supplier_counter_step_usd_per_mt, 2)
    return fob_ceiling


def _build_counter_supplier_prompt(
    campaign: Campaign, supplier: Optional[ParsedEmail], target_ceiling: float, action: Optional[str], reason: str
) -> str:
    return SUPPLIER_RFQ_PROMPT.format(
        desk_name=settings.desk_name,
        supplier_name="Export Team",
        commodity=campaign.commodity,
        broken_percentage=campaign.broken_percentage,
        target_volume_mt=supplier.quantity_mt if supplier else campaign.target_volume_mt,
        origin_port=campaign.origin_port_default,
        fob_ceiling=target_ceiling,
        supplier_offered_text=f"USD {supplier.price_usd_per_mt:.2f}/MT FOB" if supplier else "Pending",
        action=action or "CEILING_ENFORCEMENT",
        evaluation_reason=reason,
    )


def counter_supplier_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    fob_ceiling = state.get("dynamic_fob_ceiling", 945.0)
    campaign = coerce_campaign(state["campaign"])
    supplier = coerce_terms(state.get("supplier_terms"))
    reason = state.get("evaluation_reason", "")
    curr_round = state.get("negotiation_round", 1)
    action = state.get("action")

    target_ceiling = _calculate_target_supplier_ceiling(supplier, action, fob_ceiling)
    prompt = _build_counter_supplier_prompt(campaign, supplier, target_ceiling, action, reason)
    fallback = _counter_supplier_draft(
        campaign, supplier, action == "COUNTER_TO_MAXIMIZE", curr_round, target_ceiling, fob_ceiling, reason
    )
    supplier_msg = generate_email_draft(prompt, *fallback, config=config)

    logger.info(
        f"[WORKFLOW:Node 4C Counter Supplier] Round {curr_round}: Action='{action}', "
        f"countering supplier with target FOB ceiling USD {target_ceiling:.2f}/MT"
    )
    return CounterSupplierUpdate(
        deal_status="counter_sent", supplier_draft=supplier_msg, last_counter_fob_usd=target_ceiling
    ).model_dump()
