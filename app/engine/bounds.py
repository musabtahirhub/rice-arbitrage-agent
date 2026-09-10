"""
Dynamic pricing boundaries calculator based on live market benchmarks.
"""

from __future__ import annotations

from app.models.campaign import CampaignConfig


def compute_dynamic_bounds(
    campaign: CampaignConfig,
    benchmark_fob: float,
    freight_cost: float,
) -> dict[str, float]:
    """
    Derive market-relative buy ceiling and sell floor:
    - max_buy_fob: FOB benchmark index + campaign variance tolerance
    - min_sell_cif: (FOB benchmark + ocean freight) + minimum sell spread
    """
    max_buy_fob = benchmark_fob * (
        1 + campaign.max_acceptable_variance_from_benchmark_pct / 100
    )
    landed_benchmark_cif = benchmark_fob + freight_cost
    min_sell_cif = landed_benchmark_cif * (
        1 + campaign.min_sell_margin_above_benchmark_pct / 100
    )

    return {
        "benchmark_fob": round(benchmark_fob, 2),
        "freight_cost": round(freight_cost, 2),
        "landed_benchmark_cif": round(landed_benchmark_cif, 2),
        "max_buy_fob": round(max_buy_fob, 2),
        "min_sell_cif": round(min_sell_cif, 2),
    }
