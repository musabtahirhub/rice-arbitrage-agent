"""Full deal evaluation: zero-risk gating and term constraints, then the margin strategy."""
from typing import Optional

from app.config import settings
from app.models import Campaign, ParsedEmail
from app.pricing.formulas import calculate_campaign_bounds, normalize_terms, resolve_buffer_usd
from app.pricing.strategy import evaluate_deal_strategy
from app.schemas import DealAction, DealEvaluation, DynamicBounds


def _unviable(reason: str, bounds: DynamicBounds, action: Optional[DealAction] = None) -> DealEvaluation:
    return DealEvaluation(viable=False, action=action, reason=reason, **bounds.model_dump())


def _check_missing_terms(
    supplier_terms: Optional[ParsedEmail], buyer_terms: Optional[ParsedEmail], bounds: DynamicBounds
) -> Optional[DealEvaluation]:
    if not supplier_terms:
        return _unviable(
            "Supplier allocation not yet secured. Zero-Risk Invariant prevents commitment to buyer.", bounds
        )
    if not buyer_terms:
        return _unviable("Buyer inquiry pending. Awaiting buyer price quote.", bounds)
    return None


def _negotiable_violation(reason: str, round_num: int, max_rounds: int, bounds: DynamicBounds) -> DealEvaluation:
    """A fixable problem (price above ceiling, volume mismatch): counter while rounds remain, then reject."""
    if round_num >= max_rounds:
        return _unviable(f"{reason} Negotiation rounds exhausted ({round_num}/{max_rounds}).", bounds, "REJECT_HARD")
    return _unviable(reason, bounds)


def _quantities_match(buyer_qty: float, supplier_qty: float) -> bool:
    tolerance = max(buyer_qty, supplier_qty) * settings.quantity_tolerance_pct / 100.0
    return abs(buyer_qty - supplier_qty) <= tolerance


def _validate_terms_constraints(
    campaign: Campaign,
    buyer_terms: ParsedEmail,
    supplier_terms: ParsedEmail,
    buyer_cif: float,
    supplier_fob: float,
    benchmark_fob: float,
    round_num: int,
    bounds: DynamicBounds,
) -> Optional[DealEvaluation]:
    max_rounds = campaign.max_negotiation_rounds
    if not _quantities_match(buyer_terms.quantity_mt, supplier_terms.quantity_mt):
        return _negotiable_violation(
            f"Quantity mismatch: Buyer requested {buyer_terms.quantity_mt} MT, "
            f"but supplier offered {supplier_terms.quantity_mt} MT.",
            round_num, max_rounds, bounds,
        )

    fob_ceiling = bounds.dynamic_fob_ceiling
    if supplier_fob > fob_ceiling:
        return _negotiable_violation(
            f"Supplier FOB ${supplier_fob:.2f}/MT exceeds dynamic ceiling of ${fob_ceiling:.2f}/MT "
            f"(Benchmark: ${benchmark_fob:.2f} + {campaign.max_variance_from_benchmark_pct}%).",
            round_num, max_rounds, bounds,
        )

    # Pre-negotiation screening only (direct evaluate_deal calls with the default round_num=0).
    # Inside the graph evaluate_risk_node passes round >= 1, where a low bid is countered at the floor instead.
    cif_floor = bounds.dynamic_cif_floor
    if round_num == 0 and buyer_cif < cif_floor:
        return _unviable(
            f"Buyer CIF ${buyer_cif:.2f}/MT is below minimum viable floor of ${cif_floor:.2f}/MT.",
            bounds,
            "REJECT_HARD",
        )
    return None


def evaluate_deal(
    campaign: Campaign,
    buyer_terms: Optional[ParsedEmail],
    supplier_terms: Optional[ParsedEmail],
    benchmark_fob: float,
    freight: float,
    round_num: int = 0,
    buyer_accepted: bool = False,
) -> DealEvaluation:
    bounds = calculate_campaign_bounds(campaign, benchmark_fob, freight)

    missing_result = _check_missing_terms(supplier_terms, buyer_terms, bounds)
    if missing_result:
        return missing_result

    buyer_cif, _ = normalize_terms(buyer_terms, freight)
    _, supplier_fob = normalize_terms(supplier_terms, freight)

    constraint_violation = _validate_terms_constraints(
        campaign, buyer_terms, supplier_terms, buyer_cif, supplier_fob, benchmark_fob, round_num, bounds
    )
    if constraint_violation:
        return constraint_violation

    strategy = evaluate_deal_strategy(
        buyer_cif=buyer_cif,
        supplier_fob=supplier_fob,
        freight=freight,
        buffer_usd=resolve_buffer_usd(campaign),
        round_num=round_num,
        campaign=campaign,
        buyer_accepted=buyer_accepted,
    )
    return strategy.model_copy(update=bounds.model_dump())
