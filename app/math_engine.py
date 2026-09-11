"""
Deterministic mathematical calculations and risk evaluation engine.
All pricing, margin formulas, and validation gates run in pure Python (never in the LLM).
"""
from typing import Optional
from app.models import Campaign, ParsedEmail


def calculate_landed_cost(supplier_fob: float, freight: float, buffer_usd: float = 20.0) -> float:
    """
    Compute total landed cost at destination port in USD/MT.
    Landed Cost = Supplier FOB + Ocean Freight + Operating/Financing Buffer
    """
    return round(supplier_fob + freight + buffer_usd, 2)


def calculate_net_margin(buyer_cif: float, landed_cost: float) -> float:
    """
    Compute net profit margin percentage based on landed cost:
    Net Margin % = ((Buyer CIF - Landed Cost) / Landed Cost) * 100
    """
    if landed_cost <= 0:
        return 0.0
    return round(((buyer_cif - landed_cost) / landed_cost) * 100, 2)


def calculate_dynamic_bounds(
    benchmark_fob: float,
    freight: float,
    max_variance_pct: float = 5.0,
    target_margin_pct: float = 10.0,
    buffer_usd: float = 20.0,
) -> dict[str, float]:
    """
    Calculate dynamic FOB ceiling (for supplier) and CIF floor (for buyer)
    grounded in live benchmark market rates.
    """
    # 1. Supplier FOB Ceiling: Benchmark + acceptable variance
    dynamic_fob_ceiling = benchmark_fob * (1.0 + max_variance_pct / 100.0)

    # 2. Maximum possible landed cost if bought at the absolute ceiling
    landed_at_ceiling = dynamic_fob_ceiling + freight + buffer_usd

    # 3. Buyer CIF Floor: Minimum selling price required to yield target margin
    dynamic_cif_floor = landed_at_ceiling * (1.0 + target_margin_pct / 100.0)

    return {
        "dynamic_fob_ceiling": round(dynamic_fob_ceiling, 2),
        "landed_cost_at_ceiling": round(landed_at_ceiling, 2),
        "dynamic_cif_floor": round(dynamic_cif_floor, 2),
    }


def normalize_terms(terms: ParsedEmail, freight: float) -> tuple[float, float]:
    """
    Normalize trade terms into standard comparison basis:
    - Buyer price converted to CIF destination
    - Supplier price converted to FOB origin
    """
    if terms.sender_role == "buyer":
        # If buyer quoted FOB, convert to CIF by adding freight
        cif_price = terms.price_usd_per_mt if terms.incoterm.upper() == "CIF" else terms.price_usd_per_mt + freight
        return cif_price, 0.0
    else:
        # If supplier quoted CIF, convert to FOB by subtracting freight
        fob_price = terms.price_usd_per_mt if terms.incoterm.upper() == "FOB" else max(terms.price_usd_per_mt - freight, 0.0)
        return 0.0, fob_price


def evaluate_deal(
    campaign: Campaign,
    buyer_terms: Optional[ParsedEmail],
    supplier_terms: Optional[ParsedEmail],
    benchmark_fob: float,
    freight: float,
) -> dict:
    """
    Deterministic gatekeeper evaluating deal viability against dynamic boundaries.
    Enforces the Zero-Risk Invariant and target profit margin hurdle.
    """
    bounds = calculate_dynamic_bounds(
        benchmark_fob=benchmark_fob,
        freight=freight,
        max_variance_pct=campaign.max_variance_from_benchmark_pct,
        target_margin_pct=campaign.target_margin_pct,
        buffer_usd=campaign.buffer_usd_per_mt,
    )
    fob_ceiling = bounds["dynamic_fob_ceiling"]
    cif_floor = bounds["dynamic_cif_floor"]

    # Rule 1: Zero-Risk Invariant — cannot approve deal if supplier terms are missing
    if not supplier_terms:
        return {
            "viable": False,
            "net_margin_pct": 0.0,
            "landed_cost": 0.0,
            "reason": "Supplier allocation not yet secured. Zero-Risk Invariant prevents commitment to buyer.",
            **bounds,
        }

    # Rule 2: Cannot evaluate full spread if buyer terms are missing
    if not buyer_terms:
        return {
            "viable": False,
            "net_margin_pct": 0.0,
            "landed_cost": 0.0,
            "reason": "Buyer inquiry pending. Awaiting buyer price quote.",
            **bounds,
        }

    # Normalize prices to standard basis
    buyer_cif, _ = normalize_terms(buyer_terms, freight)
    _, supplier_fob = normalize_terms(supplier_terms, freight)

    # Rule 3: Quantity alignment
    if buyer_terms.quantity_mt != supplier_terms.quantity_mt:
        return {
            "viable": False,
            "net_margin_pct": 0.0,
            "landed_cost": 0.0,
            "reason": f"Quantity mismatch: Buyer requested {buyer_terms.quantity_mt} MT, but supplier offered {supplier_terms.quantity_mt} MT.",
            **bounds,
        }

    # Rule 4: Supplier ceiling check
    if supplier_fob > fob_ceiling:
        return {
            "viable": False,
            "net_margin_pct": 0.0,
            "landed_cost": 0.0,
            "reason": f"Supplier FOB ${supplier_fob:.2f}/MT exceeds dynamic ceiling of ${fob_ceiling:.2f}/MT (Benchmark: ${benchmark_fob:.2f} + {campaign.max_variance_from_benchmark_pct}%).",
            **bounds,
        }

    # Rule 5: Buyer floor check
    if buyer_cif < cif_floor:
        return {
            "viable": False,
            "net_margin_pct": 0.0,
            "landed_cost": 0.0,
            "reason": f"Buyer CIF ${buyer_cif:.2f}/MT is below minimum viable floor of ${cif_floor:.2f}/MT.",
            **bounds,
        }

    # Rule 6: Landed cost & net profit margin check
    landed_cost = calculate_landed_cost(supplier_fob, freight, campaign.buffer_usd_per_mt)
    margin_pct = calculate_net_margin(buyer_cif, landed_cost)

    if margin_pct < campaign.target_margin_pct:
        return {
            "viable": False,
            "net_margin_pct": margin_pct,
            "landed_cost": landed_cost,
            "reason": f"Net margin {margin_pct:.2f}% is below target hurdle of {campaign.target_margin_pct:.2f}%. Landed cost: ${landed_cost:.2f}/MT, Buyer CIF: ${buyer_cif:.2f}/MT.",
            **bounds,
        }

    # All gates cleared!
    return {
        "viable": True,
        "net_margin_pct": margin_pct,
        "landed_cost": landed_cost,
        "reason": f"Deal is fully viable with net margin of {margin_pct:.2f}% (Target: {campaign.target_margin_pct:.2f}%). Supplier FOB: ${supplier_fob:.2f}, Landed: ${landed_cost:.2f}, Buyer CIF: ${buyer_cif:.2f}.",
        **bounds,
    }
