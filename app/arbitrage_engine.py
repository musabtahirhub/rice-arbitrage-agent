"""
Deterministic arbitrage engine — pure Python, zero LLM involvement.

Per the architecture spec every unit-price evaluation, margin check,
Incoterm freight conversion, and trade-boundary gate is computed here.
The LLM is never allowed to perform math or approve/reject a deal.

Price boundaries are computed dynamically from live market benchmarks
and campaign-level variance tolerances — no hardcoded dollar thresholds.
"""

from __future__ import annotations

from app.schemas import CampaignConfig, ParsedTradeEmail


def evaluate_deal(
    campaign: CampaignConfig,
    buyer: ParsedTradeEmail,
    supplier: ParsedTradeEmail,
    benchmark_fob: float,
    freight_cost: float,
) -> dict:
    """
    Evaluate whether a buyer–supplier pair forms a viable back-to-back deal.

    Dynamic bound computation
    -------------------------
    All buy/sell thresholds are derived from the live ``benchmark_fob`` and
    ``freight_cost`` rather than static config values:

    * ``max_buy_fob`` = benchmark × (1 + max_variance%)  — reject supplier
      if they charge an excessive premium above the market index.
    * ``min_sell_cif`` = (benchmark + freight) × (1 + min_sell_margin%)  —
      reject buyer if their offer doesn't clear the landed benchmark plus
      minimum spread.

    Normalization logic
    -------------------
    * Buyer quotes in **CIF / CFR** are treated as the revenue price directly.
    * Buyer quotes in **FOB** have freight added to derive an equivalent CIF
      revenue expectation.
    * Supplier quotes in **FOB** are the base cost; freight is added to derive
      the effective landed cost.
    * Supplier quotes in **CIF / CFR** are treated as the total cost directly.

    Boundary gates (checked in order)
    ----------------------------------
    1. Supplier FOB vs. dynamic max buy ceiling  →  reject if excessive premium.
    2. Buyer CIF vs. dynamic min sell floor      →  reject if below landed benchmark + spread.
    3. Quantity mismatch                         →  reject.
    4. Net margin < campaign target              →  reject.

    Returns
    -------
    dict with keys:
        ``viable`` (bool), ``margin_pct`` (float), ``reason`` (str),
        ``benchmark_fob`` (float), ``freight_cost`` (float),
        ``max_buy_fob`` (float), ``min_sell_cif`` (float).
    """

    # ------------------------------------------------------------------
    # 0. Compute dynamic bounds from benchmark + campaign tolerances
    # ------------------------------------------------------------------
    max_buy_fob = benchmark_fob * (
        1 + campaign.max_acceptable_variance_from_benchmark_pct / 100
    )
    landed_benchmark_cif = benchmark_fob + freight_cost
    min_sell_cif = landed_benchmark_cif * (
        1 + campaign.min_sell_margin_above_benchmark_pct / 100
    )

    market_context = {
        "benchmark_fob": round(benchmark_fob, 2),
        "freight_cost": round(freight_cost, 2),
        "max_buy_fob": round(max_buy_fob, 2),
        "min_sell_cif": round(min_sell_cif, 2),
    }

    # ------------------------------------------------------------------
    # 1. Normalize buyer revenue to a CIF-equivalent price
    # ------------------------------------------------------------------
    if buyer.incoterm in ("CIF", "CFR"):
        buyer_cif_price = buyer.price_usd_per_mt
    else:
        # Buyer quoted FOB — add freight to understand what the CIF
        # equivalent would be (unusual but possible).
        buyer_cif_price = buyer.price_usd_per_mt + freight_cost

    # ------------------------------------------------------------------
    # 2. Normalize supplier cost to a CIF-equivalent (landed) cost
    # ------------------------------------------------------------------
    if supplier.incoterm == "FOB":
        effective_cost = supplier.price_usd_per_mt + freight_cost
    else:
        # Supplier quoted CIF/CFR — price already includes freight.
        effective_cost = supplier.price_usd_per_mt

    # ------------------------------------------------------------------
    # 3. Gate: supplier FOB price vs. dynamic max buy ceiling
    # ------------------------------------------------------------------
    supplier_fob_price = (
        supplier.price_usd_per_mt
        if supplier.incoterm == "FOB"
        else supplier.price_usd_per_mt - freight_cost
    )
    if supplier_fob_price > max_buy_fob:
        premium_pct = ((supplier_fob_price - benchmark_fob) / benchmark_fob) * 100
        return {
            "viable": False,
            "margin_pct": 0.0,
            "reason": (
                f"Supplier FOB price ${supplier_fob_price:.2f}/MT is "
                f"{premium_pct:.1f}% above the market benchmark of "
                f"${benchmark_fob:.2f}/MT (max allowed: "
                f"{campaign.max_acceptable_variance_from_benchmark_pct:.1f}%). "
                f"Dynamic ceiling: ${max_buy_fob:.2f}/MT."
            ),
            **market_context,
        }

    # ------------------------------------------------------------------
    # 4. Gate: buyer CIF price vs. dynamic min sell floor
    # ------------------------------------------------------------------
    if buyer_cif_price < min_sell_cif:
        shortfall_pct = ((min_sell_cif - buyer_cif_price) / min_sell_cif) * 100
        return {
            "viable": False,
            "margin_pct": 0.0,
            "reason": (
                f"Buyer CIF-equivalent price ${buyer_cif_price:.2f}/MT is below "
                f"the dynamic floor of ${min_sell_cif:.2f}/MT "
                f"(benchmark CIF ${landed_benchmark_cif:.2f} + "
                f"{campaign.min_sell_margin_above_benchmark_pct:.1f}% margin). "
                f"Shortfall: {shortfall_pct:.1f}%."
            ),
            **market_context,
        }

    # ------------------------------------------------------------------
    # 5. Gate: quantity mismatch
    # ------------------------------------------------------------------
    if buyer.quantity_mt != supplier.quantity_mt:
        return {
            "viable": False,
            "margin_pct": 0.0,
            "reason": (
                f"Quantity mismatch — buyer requests {buyer.quantity_mt} MT "
                f"but supplier offers {supplier.quantity_mt} MT."
            ),
            **market_context,
        }

    # ------------------------------------------------------------------
    # 6. Calculate net margin
    # ------------------------------------------------------------------
    if buyer_cif_price == 0:
        return {
            "viable": False,
            "margin_pct": 0.0,
            "reason": "Buyer CIF price is zero — cannot compute margin.",
            **market_context,
        }

    margin_pct = ((buyer_cif_price - effective_cost) / buyer_cif_price) * 100

    # ------------------------------------------------------------------
    # 7. Gate: margin vs. target
    # ------------------------------------------------------------------
    if margin_pct < campaign.target_profit_margin_pct:
        return {
            "viable": False,
            "margin_pct": round(margin_pct, 4),
            "reason": (
                f"Net margin {margin_pct:.2f}% is below the target of "
                f"{campaign.target_profit_margin_pct:.2f}%. "
                f"Benchmark FOB: ${benchmark_fob:.2f}/MT, "
                f"freight: ${freight_cost:.2f}/MT."
            ),
            **market_context,
        }

    # ------------------------------------------------------------------
    # All gates passed — deal is viable
    # ------------------------------------------------------------------
    return {
        "viable": True,
        "margin_pct": round(margin_pct, 4),
        "reason": (
            f"Deal is viable with a net margin of {margin_pct:.2f}% "
            f"(target ≥ {campaign.target_profit_margin_pct:.2f}%). "
            f"Buyer CIF ${buyer_cif_price:.2f}/MT, "
            f"effective cost ${effective_cost:.2f}/MT. "
            f"Market benchmark FOB: ${benchmark_fob:.2f}/MT, "
            f"freight: ${freight_cost:.2f}/MT."
        ),
        **market_context,
    }
