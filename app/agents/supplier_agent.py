from app.config import settings
from app.logger import get_logger
from app.models import Campaign, DealState
from app.prompts import SUPPLIER_RFQ_PROMPT
from app.services.llm_service import generate_dynamic_llm_draft

logger = get_logger("arbitrage_desk.workflow")


def counter_supplier_node(state: DealState) -> dict:
    fob_ceiling = state.get("dynamic_fob_ceiling", 945.0)
    campaign: Campaign = state["campaign"]
    supplier = state.get("supplier_terms")
    reason = state.get("evaluation_reason", "")
    curr_round = state.get("negotiation_round", 1)
    action = state.get("action")

    target_ceiling = round(supplier.price_usd_per_mt - 20.0, 2) if (action == "COUNTER_TO_MAXIMIZE" and supplier) else fob_ceiling

    supp_prompt = SUPPLIER_RFQ_PROMPT.format(
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

    if action == "COUNTER_TO_MAXIMIZE" and supplier:
        fallback_sub = f"Negotiation Round {curr_round}: Volume Discount Request — {campaign.commodity} FOB {campaign.origin_port_default}"
        fallback_body = (
            f"Dear Exporter,\n\n"
            f"Thank you for your quotation of USD {supplier.price_usd_per_mt:.2f}/MT. Given our export volume commitment "
            f"and ready LC facility, we request an export bulk discount to USD {target_ceiling:.2f}/MT FOB {campaign.origin_port_default}.\n\n"
            f"Desk Analysis: {reason}\n\n"
            f"Please confirm if you can confirm allocation at this revised level.\n\n"
            f"Warm regards,\nProcurement Desk, {settings.desk_name}"
        )
    else:
        fallback_sub = f"Counter-Bid — {campaign.commodity} FOB {campaign.origin_port_default}"
        fallback_body = (
            f"Dear Exporter,\n\n"
            f"Thank you for your quotation. Based on our container freight and volume commitments, "
            f"our ceiling acquisition price for this parcel is USD {fob_ceiling:.2f}/MT FOB.\n\n"
            f"Desk Note: {reason}\n\n"
            f"Please confirm if you can meet our target price to lock this allocation.\n\n"
            f"Warm regards,\nProcurement Desk, {settings.desk_name}"
        )

    supplier_msg = generate_dynamic_llm_draft(supp_prompt, fallback_sub, fallback_body)

    logger.info(
        f"[WORKFLOW:Node 4C Counter Supplier] Round {curr_round}: Action='{action}', "
        f"countering supplier with target FOB ceiling USD {target_ceiling:.2f}/MT"
    )

    return {
        "deal_status": "counter_sent",
        "supplier_draft": supplier_msg,
    }
