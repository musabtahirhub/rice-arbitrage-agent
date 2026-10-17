"""Margin strategy once both sides have quoted: hard-floor reject, accept, or counter for more spread."""
from typing import Optional

from app.models import Campaign
from app.pricing.formulas import calculate_landed_cost, calculate_net_margin
from app.schemas import DealEvaluation


def _check_hard_floor_rejection(
    net_spread: float, hard_floor: float, landed_cost: float, buyer_cif: float, net_margin_pct: float
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
    buyer_accepted: bool, net_spread: float, hard_floor: float, landed_cost: float, buyer_cif: float,
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
    net_spread: float, campaign: Campaign, round_num: int, landed_cost: float, net_margin_pct: float
) -> Optional[DealEvaluation]:
    soft_target, max_rounds = campaign.min_profit_per_mt_soft, campaign.max_negotiation_rounds
    if not (net_spread < soft_target and round_num < max_rounds):
        return None
    return DealEvaluation(
        action="COUNTER_TO_MAXIMIZE",
        viable=True,
        landed_cost=landed_cost,
        net_spread=net_spread,
        net_margin_pct=net_margin_pct,
        reason=(
            f"Net spread ${net_spread:.2f}/MT clears hard floor (${campaign.min_profit_per_mt_hard:.2f}/MT) "
            f"but is below soft target (${soft_target:.2f}/MT) at round {round_num}/{max_rounds}. "
            f"Countering to maximize margin."
        ),
    )


def _build_acceptance_decision(
    net_spread: float, campaign: Campaign, round_num: int, landed_cost: float, net_margin_pct: float,
    supplier_fob: float, buyer_cif: float,
) -> DealEvaluation:
    soft_target = campaign.min_profit_per_mt_soft
    close_desc = (
        f"clears soft target of ${soft_target:.2f}/MT"
        if net_spread >= soft_target
        else f"accepted at round {round_num}/{campaign.max_negotiation_rounds} above hard floor "
        f"(${campaign.min_profit_per_mt_hard:.2f}/MT)"
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
    landed_cost = calculate_landed_cost(supplier_fob, freight, buffer_usd)
    net_spread = round(buyer_cif - landed_cost, 2)
    net_margin_pct = calculate_net_margin(buyer_cif, landed_cost)
    hard_floor = campaign.min_profit_per_mt_hard

    return (
        _check_hard_floor_rejection(net_spread, hard_floor, landed_cost, buyer_cif, net_margin_pct)
        or _check_buyer_direct_acceptance(
            buyer_accepted, net_spread, hard_floor, landed_cost, buyer_cif, net_margin_pct
        )
        or _check_concession_counter(net_spread, campaign, round_num, landed_cost, net_margin_pct)
        or _build_acceptance_decision(
            net_spread, campaign, round_num, landed_cost, net_margin_pct, supplier_fob, buyer_cif
        )
    )
