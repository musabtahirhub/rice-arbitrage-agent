from typing import Optional
from app.config import settings
from app.models import Campaign, ParsedEmail
from app.schemas import DealEvaluation, DynamicBounds


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


def normalize_terms(terms: ParsedEmail, freight: float) -> tuple[float, float]:
    if terms.sender_role == "buyer":
        cif_price = terms.price_usd_per_mt if terms.incoterm.upper() == "CIF" else terms.price_usd_per_mt + freight
        return cif_price, 0.0
    fob_price = terms.price_usd_per_mt if terms.incoterm.upper() == "FOB" else max(terms.price_usd_per_mt - freight, 0.0)
    return 0.0, fob_price


def _check_hard_floor_rejection(
    net_spread: float,
    hard_floor: float,
    landed_cost: float,
    buyer_cif: float,
    net_margin_pct: float,
) -> Optional[DealEvaluation]:
    if net_spread >= hard_floor:
        return None
    return DealEvaluation(
        action="REJECT_HARD",
        viable=False,
        landed_cost=landed_cost,
        net_spread=net_spread,
        net_margin_pct=net_margin_pct,
        reason=(
            f"Net spread ${net_spread:.2f}/MT is below non-negotiable hard floor of "
            f"${hard_floor:.2f}/MT (Landed: ${landed_cost:.2f}, Buyer CIF: ${buyer_cif:.2f}). Deal rejected."
        ),
    )


def _check_buyer_direct_acceptance(
    buyer_accepted: bool,
    net_spread: float,
    hard_floor: float,
    landed_cost: float,
    buyer_cif: float,
    net_margin_pct: float,
) -> Optional[DealEvaluation]:
    if not buyer_accepted:
        return None
    return DealEvaluation(
        action="ACCEPT_AND_CLOSE",
        viable=True,
        landed_cost=landed_cost,
        net_spread=net_spread,
        net_margin_pct=net_margin_pct,
        reason=(
            f"Deal accepted: Buyer accepted desk proposal at net spread of ${net_spread:.2f}/MT "
            f"(Hard floor: ${hard_floor:.2f}/MT, Landed: ${landed_cost:.2f}, Buyer CIF: ${buyer_cif:.2f})."
        ),
    )


def _check_concession_counter(
    net_spread: float,
    soft_target: float,
    hard_floor: float,
    round_num: int,
    max_rounds: int,
    landed_cost: float,
    net_margin_pct: float,
) -> Optional[DealEvaluation]:
    if not (net_spread < soft_target and round_num < max_rounds):
        return None
    return DealEvaluation(
        action="COUNTER_TO_MAXIMIZE",
        viable=True,
        landed_cost=landed_cost,
        net_spread=net_spread,
        net_margin_pct=net_margin_pct,
        reason=(
            f"Net spread ${net_spread:.2f}/MT clears hard floor (${hard_floor:.2f}/MT) "
            f"but is below soft target (${soft_target:.2f}/MT) at round {round_num}/{max_rounds}. "
            f"Countering to maximize margin."
        ),
    )


def _build_acceptance_decision(
    net_spread: float,
    soft_target: float,
    hard_floor: float,
    round_num: int,
    max_rounds: int,
    landed_cost: float,
    net_margin_pct: float,
    supplier_fob: float,
    buyer_cif: float,
) -> DealEvaluation:
    is_optimal = net_spread >= soft_target
    close_desc = (
        f"clears soft target of ${soft_target:.2f}/MT"
        if is_optimal
        else f"accepted at round {round_num}/{max_rounds} above hard floor (${hard_floor:.2f}/MT)"
    )
    return DealEvaluation(
        action="ACCEPT_AND_CLOSE",
        viable=True,
        landed_cost=landed_cost,
        net_spread=net_spread,
        net_margin_pct=net_margin_pct,
        reason=(
            f"Deal viable with net spread of ${net_spread:.2f}/MT ({close_desc}). "
            f"Supplier FOB: ${supplier_fob:.2f}, Landed: ${landed_cost:.2f}, Buyer CIF: ${buyer_cif:.2f}."
        ),
    )


def evaluate_deal_strategy(
    buyer_cif: float,
    supplier_fob: float,
    freight: float,
    buffer_usd: float,
    round_num: int,
    campaign: Campaign,
    buyer_accepted: bool = False,
) -> DealEvaluation:
    landed_cost = round(supplier_fob + freight + buffer_usd, 2)
    net_spread = round(buyer_cif - landed_cost, 2)
    net_margin_pct = calculate_net_margin(buyer_cif, landed_cost)

    rejection = _check_hard_floor_rejection(
        net_spread, campaign.min_profit_per_mt_hard, landed_cost, buyer_cif, net_margin_pct
    )
    if rejection:
        return rejection

    direct_acceptance = _check_buyer_direct_acceptance(
        buyer_accepted, net_spread, campaign.min_profit_per_mt_hard, landed_cost, buyer_cif, net_margin_pct
    )
    if direct_acceptance:
        return direct_acceptance

    concession = _check_concession_counter(
        net_spread,
        campaign.min_profit_per_mt_soft,
        campaign.min_profit_per_mt_hard,
        round_num,
        campaign.max_negotiation_rounds,
        landed_cost,
        net_margin_pct,
    )
    if concession:
        return concession

    return _build_acceptance_decision(
        net_spread,
        campaign.min_profit_per_mt_soft,
        campaign.min_profit_per_mt_hard,
        round_num,
        campaign.max_negotiation_rounds,
        landed_cost,
        net_margin_pct,
        supplier_fob,
        buyer_cif,
    )


def _check_missing_terms(
    supplier_terms: Optional[ParsedEmail],
    buyer_terms: Optional[ParsedEmail],
    bounds: DynamicBounds,
) -> Optional[DealEvaluation]:
    if not supplier_terms:
        return DealEvaluation(
            viable=False,
            action=None,
            net_spread=0.0,
            net_margin_pct=0.0,
            landed_cost=0.0,
            reason="Supplier allocation not yet secured. Zero-Risk Invariant prevents commitment to buyer.",
            dynamic_fob_ceiling=bounds.dynamic_fob_ceiling,
            landed_cost_at_ceiling=bounds.landed_cost_at_ceiling,
            dynamic_cif_floor=bounds.dynamic_cif_floor,
        )
    if not buyer_terms:
        return DealEvaluation(
            viable=False,
            action=None,
            net_spread=0.0,
            net_margin_pct=0.0,
            landed_cost=0.0,
            reason="Buyer inquiry pending. Awaiting buyer price quote.",
            dynamic_fob_ceiling=bounds.dynamic_fob_ceiling,
            landed_cost_at_ceiling=bounds.landed_cost_at_ceiling,
            dynamic_cif_floor=bounds.dynamic_cif_floor,
        )
    return None


def _validate_terms_constraints(
    buyer_terms: ParsedEmail,
    supplier_terms: ParsedEmail,
    buyer_cif: float,
    supplier_fob: float,
    fob_ceiling: float,
    cif_floor: float,
    benchmark_fob: float,
    max_variance_pct: float,
    round_num: int,
    bounds: DynamicBounds,
) -> Optional[DealEvaluation]:
    if buyer_terms.quantity_mt != supplier_terms.quantity_mt:
        return DealEvaluation(
            viable=False,
            action="REJECT_HARD",
            net_spread=0.0,
            net_margin_pct=0.0,
            landed_cost=0.0,
            reason=f"Quantity mismatch: Buyer requested {buyer_terms.quantity_mt} MT, but supplier offered {supplier_terms.quantity_mt} MT.",
            dynamic_fob_ceiling=bounds.dynamic_fob_ceiling,
            landed_cost_at_ceiling=bounds.landed_cost_at_ceiling,
            dynamic_cif_floor=bounds.dynamic_cif_floor,
        )

    if supplier_fob > fob_ceiling:
        return DealEvaluation(
            viable=False,
            action="REJECT_HARD",
            net_spread=0.0,
            net_margin_pct=0.0,
            landed_cost=0.0,
            reason=f"Supplier FOB ${supplier_fob:.2f}/MT exceeds dynamic ceiling of ${fob_ceiling:.2f}/MT (Benchmark: ${benchmark_fob:.2f} + {max_variance_pct}%).",
            dynamic_fob_ceiling=bounds.dynamic_fob_ceiling,
            landed_cost_at_ceiling=bounds.landed_cost_at_ceiling,
            dynamic_cif_floor=bounds.dynamic_cif_floor,
        )

    if round_num == 0 and buyer_cif < cif_floor:
        return DealEvaluation(
            viable=False,
            action="REJECT_HARD",
            net_spread=0.0,
            net_margin_pct=0.0,
            landed_cost=0.0,
            reason=f"Buyer CIF ${buyer_cif:.2f}/MT is below minimum viable floor of ${cif_floor:.2f}/MT.",
            dynamic_fob_ceiling=bounds.dynamic_fob_ceiling,
            landed_cost_at_ceiling=bounds.landed_cost_at_ceiling,
            dynamic_cif_floor=bounds.dynamic_cif_floor,
        )

    return None


def _assemble_deal_evaluation_response(strategy: DealEvaluation, bounds: DynamicBounds) -> DealEvaluation:
    return DealEvaluation(
        viable=strategy.viable,
        action=strategy.action,
        net_spread=strategy.net_spread,
        net_margin_pct=strategy.net_margin_pct,
        landed_cost=strategy.landed_cost,
        reason=strategy.reason,
        dynamic_fob_ceiling=bounds.dynamic_fob_ceiling,
        landed_cost_at_ceiling=bounds.landed_cost_at_ceiling,
        dynamic_cif_floor=bounds.dynamic_cif_floor,
    )


def evaluate_deal(
    campaign: Campaign,
    buyer_terms: Optional[ParsedEmail],
    supplier_terms: Optional[ParsedEmail],
    benchmark_fob: float,
    freight: float,
    round_num: int = 0,
    buyer_accepted: bool = False,
) -> DealEvaluation:
    bounds = calculate_dynamic_bounds(
        benchmark_fob=benchmark_fob,
        freight=freight,
        max_variance_pct=campaign.max_variance_from_benchmark_pct,
        target_margin_pct=campaign.target_margin_pct,
        buffer_usd=campaign.buffer_usd_per_mt,
    )

    missing_result = _check_missing_terms(supplier_terms, buyer_terms, bounds)
    if missing_result:
        return missing_result

    buyer_cif, _ = normalize_terms(buyer_terms, freight)
    _, supplier_fob = normalize_terms(supplier_terms, freight)

    constraint_violation = _validate_terms_constraints(
        buyer_terms=buyer_terms,
        supplier_terms=supplier_terms,
        buyer_cif=buyer_cif,
        supplier_fob=supplier_fob,
        fob_ceiling=bounds.dynamic_fob_ceiling,
        cif_floor=bounds.dynamic_cif_floor,
        benchmark_fob=benchmark_fob,
        max_variance_pct=campaign.max_variance_from_benchmark_pct,
        round_num=round_num,
        bounds=bounds,
    )
    if constraint_violation:
        return constraint_violation

    buffer = campaign.buffer_usd_per_mt if campaign.buffer_usd_per_mt is not None else settings.default_buffer_usd_per_mt
    strategy = evaluate_deal_strategy(
        buyer_cif=buyer_cif,
        supplier_fob=supplier_fob,
        freight=freight,
        buffer_usd=buffer,
        round_num=round_num,
        campaign=campaign,
        buyer_accepted=buyer_accepted,
    )
    return _assemble_deal_evaluation_response(strategy, bounds)
