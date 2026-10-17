"""Deterministic pricing formulas: landed cost, net margin, dynamic FOB ceiling / CIF floor.

Landed = Supplier FOB + Freight + Buffer.  Net Margin % = (Buyer CIF - Landed) / Landed x 100.
FOB ceiling = benchmark x (1 + max_variance%).  CIF floor = landed cost at the ceiling x (1 + target_margin%).
"""
from typing import Optional

from app.config import settings
from app.models import Campaign, ParsedEmail
from app.schemas import DynamicBounds


def resolve_buffer_usd(campaign: Campaign) -> float:
    if campaign.buffer_usd_per_mt is not None:
        return campaign.buffer_usd_per_mt
    return settings.default_buffer_usd_per_mt


def calculate_landed_cost(supplier_fob: float, freight: float, buffer_usd: Optional[float] = None) -> float:
    effective_buffer = buffer_usd if buffer_usd is not None else settings.default_buffer_usd_per_mt
    return round(supplier_fob + freight + effective_buffer, 2)


def calculate_net_margin(buyer_cif: float, landed_cost: float) -> float:
    if landed_cost <= 0:
        return 0.0
    return round(((buyer_cif - landed_cost) / landed_cost) * 100, 2)


def calculate_dynamic_bounds(
    benchmark_fob: float,
    freight: float,
    max_variance_pct: Optional[float] = None,
    target_margin_pct: Optional[float] = None,
    buffer_usd: Optional[float] = None,
) -> DynamicBounds:
    variance = max_variance_pct if max_variance_pct is not None else settings.default_max_variance_pct
    margin = target_margin_pct if target_margin_pct is not None else settings.default_target_margin_pct
    buffer = buffer_usd if buffer_usd is not None else settings.default_buffer_usd_per_mt

    dynamic_fob_ceiling = benchmark_fob * (1.0 + variance / 100.0)
    landed_at_ceiling = dynamic_fob_ceiling + freight + buffer
    dynamic_cif_floor = landed_at_ceiling * (1.0 + margin / 100.0)

    return DynamicBounds(
        dynamic_fob_ceiling=round(dynamic_fob_ceiling, 2),
        landed_cost_at_ceiling=round(landed_at_ceiling, 2),
        dynamic_cif_floor=round(dynamic_cif_floor, 2),
    )


def calculate_campaign_bounds(campaign: Campaign, benchmark_fob: float, freight: float) -> DynamicBounds:
    return calculate_dynamic_bounds(
        benchmark_fob=benchmark_fob,
        freight=freight,
        max_variance_pct=campaign.max_variance_from_benchmark_pct,
        target_margin_pct=campaign.target_margin_pct,
        buffer_usd=campaign.buffer_usd_per_mt,
    )


def normalize_terms(terms: ParsedEmail, freight: float) -> tuple[float, float]:
    """(buyer CIF, supplier FOB): a buyer price converted to CIF, or a supplier price converted to FOB."""
    if terms.sender_role == "buyer":
        cif_price = terms.price_usd_per_mt if terms.incoterm.upper() == "CIF" else terms.price_usd_per_mt + freight
        return cif_price, 0.0
    fob_price = (
        terms.price_usd_per_mt if terms.incoterm.upper() == "FOB" else max(terms.price_usd_per_mt - freight, 0.0)
    )
    return 0.0, fob_price
