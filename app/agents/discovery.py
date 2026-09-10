"""
Autonomous Campaign Ignition & Counterparty Discovery Node.

Discovers relevant Middle East buyers and Asian suppliers from the directory,
computes dynamic market-grounded indicative pricing, and drafts targeted
cold outreach emails and RFQs to initiate parallel trade negotiation threads.
"""

from __future__ import annotations

from typing import Any
from app.directory import (
    Counterparty,
    get_buyers_for_commodity,
    get_suppliers_for_commodity,
)
from app.schemas import CampaignConfig


def draft_buyer_cold_outreach(
    buyer: Counterparty,
    commodity: str,
    volume_mt: float,
    indicative_cif: float,
    benchmark_cif: float,
) -> str:
    """
    Draft a personalized cold outreach email to a prospective Middle East buyer.
    Cites indicative CIF pricing derived from prevailing market indices.
    """
    return (
        f"Subject: Trade Inquiry: Premium {commodity} CIF {buyer.port}\n\n"
        f"Dear {buyer.contact_name},\n\n"
        f"We hope this message finds you well at {buyer.name}.\n\n"
        f"Our trading desk is currently arranging export allocations for premium {commodity} "
        f"and can supply up to {volume_mt:,.0f} MT with direct delivery to {buyer.port}.\n\n"
        f"In line with current market benchmarks of ${benchmark_cif:.2f}/MT CIF {buyer.port}, "
        f"we are pleased to propose an indicative offer of USD {indicative_cif:.2f} per MT CIF {buyer.port}.\n\n"
        f"Key Indicative Specifications:\n"
        f"  - Commodity: {commodity}\n"
        f"  - Volume: {volume_mt:,.0f} MT in 20ft container lots\n"
        f"  - Packing: 50 kg BOPP / PP woven export bags\n"
        f"  - Inspection: SGS / Bureau Veritas at loading port\n"
        f"  - Payment Terms: {buyer.payment_terms}\n"
        f"  - Shipment: Within 25-30 days from LC establishment\n\n"
        f"Should this match your current procurement schedule, please reply with your target volume "
        f"and acceptance of this indicative price so we may issue our formal Soft Corporate Offer (SCO).\n\n"
        f"Best regards,\n"
        f"International Commodity Desk\n"
        f"Rice Arbitrage Network"
    )


def draft_supplier_rfq(
    supplier: Counterparty,
    commodity: str,
    volume_mt: float,
    target_fob: float,
    benchmark_fob: float,
) -> str:
    """
    Draft a targeted Request for Quotation (RFQ) to a verified Asian rice mill / exporter.
    Requests their best FOB quotation for the origin port.
    """
    return (
        f"Subject: Urgent RFQ: {volume_mt:,.0f} MT {commodity} FOB {supplier.port}\n\n"
        f"Dear {supplier.contact_name},\n\n"
        f"We are sourcing {volume_mt:,.0f} MT of {commodity} for immediate shipment to our Middle East accounts.\n\n"
        f"We are inviting {supplier.name} to submit your most competitive FOB quotation ex-{supplier.port}.\n\n"
        f"Requirements:\n"
        f"  - Commodity: {commodity}\n"
        f"  - Quantity: {volume_mt:,.0f} MT (+/- 5% buyer's option)\n"
        f"  - Delivery Basis: FOB {supplier.port}\n"
        f"  - Quality Specs: Max 5% broken, max 14% moisture, export sortex cleaned\n"
        f"  - Packing: Standard 50kg export bags, seaworthy stuffing\n"
        f"  - Payment: 100% Irrevocable Letter of Credit at Sight from a top-tier bank\n\n"
        f"Prevailing market benchmark for this specification is approximately ${benchmark_fob:.2f}/MT FOB. "
        f"Our target acquisition ceiling is around ${target_fob:.2f}/MT FOB.\n\n"
        f"Kindly confirm your best firm offer, earliest loading readiness date, and available tonnage.\n\n"
        f"Thank you,\n"
        f"Global Sourcing Team\n"
        f"Rice Arbitrage Network"
    )


def run_discovery(
    campaign: CampaignConfig,
    benchmark_fob: float,
    freight_cost: float,
    target_volume_mt: float = 500.0,
) -> dict[str, Any]:
    """
    Execute autonomous counterparty discovery and automated outreach ignition.

    1. Discovers buyers and suppliers matching the campaign commodity.
    2. Computes market-grounded indicative buyer CIF and supplier target FOB.
    3. Drafts initial cold outreach emails and supplier RFQs.
    4. Prepares initial campaign thread structures.
    """
    landed_benchmark_cif = benchmark_fob + freight_cost
    
    # Buyer indicative offer: CIF benchmark + campaign target margin + minimum sell margin
    margin_addon = (campaign.target_profit_margin_pct + campaign.min_sell_margin_above_benchmark_pct) / 100.0
    indicative_buyer_cif = landed_benchmark_cif * (1 + margin_addon)

    # Supplier target max FOB: benchmark FOB + max variance
    supplier_target_fob = benchmark_fob * (1 + campaign.max_acceptable_variance_from_benchmark_pct / 100.0)

    # Query directory
    buyers = get_buyers_for_commodity(campaign.commodity)
    suppliers = get_suppliers_for_commodity(campaign.commodity)

    # Generate drafts
    buyer_drafts: dict[str, str] = {}
    for b in buyers:
        buyer_drafts[b.id] = draft_buyer_cold_outreach(
            buyer=b,
            commodity=campaign.commodity,
            volume_mt=target_volume_mt,
            indicative_cif=round(indicative_buyer_cif, 2),
            benchmark_cif=round(landed_benchmark_cif, 2),
        )

    supplier_rfqs: dict[str, str] = {}
    for s in suppliers:
        supplier_rfqs[s.id] = draft_supplier_rfq(
            supplier=s,
            commodity=campaign.commodity,
            volume_mt=target_volume_mt,
            target_fob=round(supplier_target_fob, 2),
            benchmark_fob=round(benchmark_fob, 2),
        )

    # Select primary counterparties for the active thread
    primary_buyer = buyers[0] if buyers else None
    primary_supplier = suppliers[0] if suppliers else None

    return {
        "discovered_buyers": [b.model_dump() for b in buyers],
        "discovered_suppliers": [s.model_dump() for s in suppliers],
        "buyer_outreach_drafts": buyer_drafts,
        "supplier_rfq_drafts": supplier_rfqs,
        "primary_buyer": primary_buyer.model_dump() if primary_buyer else None,
        "primary_supplier": primary_supplier.model_dump() if primary_supplier else None,
        "indicative_buyer_cif": round(indicative_buyer_cif, 2),
        "supplier_target_fob": round(supplier_target_fob, 2),
        "benchmark_cif": round(landed_benchmark_cif, 2),
        "target_volume_mt": target_volume_mt,
    }
