"""
Deterministic margin, parity normalization, and trade boundary evaluation gates.
"""

from __future__ import annotations

from app.engine.bounds import compute_dynamic_bounds
from app.models.campaign import CampaignConfig
from app.models.trade import ParsedTradeEmail


def evaluate_deal(
    campaign: CampaignConfig,
    buyer: ParsedTradeEmail,
    supplier: ParsedTradeEmail,
    benchmark_fob: float,
    freight_cost: float,
) -> dict:
    """
    Evaluate back-to-back deal viability against dynamic market gates.
    """
    bounds = compute_dynamic_bounds(campaign, benchmark_fob, freight_cost)
    market_context = {
        "benchmark_fob": bounds["benchmark_fob"],
        "freight_cost": bounds["freight_cost"],
        "max_buy_fob": bounds["max_buy_fob"],
        "min_sell_cif": bounds["min_sell_cif"],
    }

    # 1. Normalize buyer CIF equivalent price
    if buyer.incoterm in ("CIF", "CFR"):
        buyer_cif_price = buyer.price_usd_per_mt
    else:
        buyer_cif_price = buyer.price_usd_per_mt + freight_cost

    # 2. Normalize supplier landed cost
    if supplier.incoterm == "FOB":
        effective_cost = supplier.price_usd_per_mt + freight_cost
    else:
        effective_cost = supplier.price_usd_per_mt

    # 3. Gate: supplier FOB ceiling
    supplier_fob_price = (
        supplier.price_usd_per_mt
        if supplier.incoterm == "FOB"
        else supplier.price_usd_per_mt - freight_cost
    )
    if supplier_fob_price > bounds["max_buy_fob"]:
        premium_pct = ((supplier_fob_price - benchmark_fob) / benchmark_fob) * 100
        return {
            "viable": False,
            "margin_pct": 0.0,
            "reason": (
                f"Supplier FOB price ${supplier_fob_price:.2f}/MT exceeds the "
                f"dynamic ceiling of ${bounds['max_buy_fob']:.2f}/MT (benchmark "
                f"${benchmark_fob:.2f} + "
                f"{campaign.max_acceptable_variance_from_benchmark_pct:.1f}%). "
                f"Supplier premium is {premium_pct:.1f}% over the index."
            ),
            **market_context,
        }

    # 4. Gate: buyer CIF floor
    if buyer_cif_price < bounds["min_sell_cif"]:
        shortfall_pct = ((bounds["min_sell_cif"] - buyer_cif_price) / bounds["min_sell_cif"]) * 100
        return {
            "viable": False,
            "margin_pct": 0.0,
            "reason": (
                f"Buyer CIF-equivalent price ${buyer_cif_price:.2f}/MT is below "
                f"the dynamic floor of ${bounds['min_sell_cif']:.2f}/MT (benchmark CIF "
                f"${bounds['landed_benchmark_cif']:.2f} + "
                f"{campaign.min_sell_margin_above_benchmark_pct:.1f}% margin). "
                f"Shortfall: {shortfall_pct:.1f}%."
            ),
            **market_context,
        }

    # 5. Gate: quantity matching
    if buyer.quantity_mt != supplier.quantity_mt:
        return {
            "viable": False,
            "margin_pct": 0.0,
            "reason": (
                f"Quantity mismatch: buyer wants {buyer.quantity_mt} MT, "
                f"supplier offers {supplier.quantity_mt} MT."
            ),
            **market_context,
        }

    # 6. Gate: margin requirement
    if buyer_cif_price == 0:
        return {
            "viable": False,
            "margin_pct": 0.0,
            "reason": "Buyer CIF price is zero — cannot compute margin.",
            **market_context,
        }

    gross_profit_per_mt = buyer_cif_price - effective_cost
    net_margin_pct = (gross_profit_per_mt / buyer_cif_price) * 100

    if net_margin_pct < campaign.target_profit_margin_pct:

        return {
            "viable": False,
            "margin_pct": round(net_margin_pct, 4),
            "reason": (
                f"Net margin {net_margin_pct:.2f}% is below target "
                f"{campaign.target_profit_margin_pct:.2f}%. "
                f"Spread: ${gross_profit_per_mt:.2f}/MT on landed cost "
                f"${effective_cost:.2f}/MT."
            ),
            **market_context,
        }

    return {
        "viable": True,
        "margin_pct": round(net_margin_pct, 4),
        "reason": (
            f"Deal is viable with a net margin of {net_margin_pct:.2f}% "
            f"(target >= {campaign.target_profit_margin_pct:.2f}%). "
            f"Buyer CIF ${buyer_cif_price:.2f}/MT, effective cost "
            f"${effective_cost:.2f}/MT. Market benchmark FOB: "
            f"${benchmark_fob:.2f}/MT, freight: ${freight_cost:.2f}/MT."
        ),
        **market_context,
    }
