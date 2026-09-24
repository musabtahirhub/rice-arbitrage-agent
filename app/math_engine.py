from typing import Optional
from app.config import settings
from app.models import Campaign, ParsedEmail


def calculate_landed_cost(supplier_fob: float, freight: float, buffer_usd: Optional[float] = None) -> float:
    if buffer_usd is None:
        buffer_usd = settings.default_buffer_usd_per_mt
    return round(supplier_fob + freight + buffer_usd, 2)


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
) -> dict[str, float]:
    if max_variance_pct is None:
        max_variance_pct = settings.default_max_variance_pct
    if target_margin_pct is None:
        target_margin_pct = settings.default_target_margin_pct
    if buffer_usd is None:
        buffer_usd = settings.default_buffer_usd_per_mt
    dynamic_fob_ceiling = benchmark_fob * (1.0 + max_variance_pct / 100.0)

    landed_at_ceiling = dynamic_fob_ceiling + freight + buffer_usd

    dynamic_cif_floor = landed_at_ceiling * (1.0 + target_margin_pct / 100.0)

    return {
        "dynamic_fob_ceiling": round(dynamic_fob_ceiling, 2),
        "landed_cost_at_ceiling": round(landed_at_ceiling, 2),
        "dynamic_cif_floor": round(dynamic_cif_floor, 2),
    }


def normalize_terms(terms: ParsedEmail, freight: float) -> tuple[float, float]:
    if terms.sender_role == "buyer":
        cif_price = terms.price_usd_per_mt if terms.incoterm.upper() == "CIF" else terms.price_usd_per_mt + freight
        return cif_price, 0.0
    else:
        fob_price = terms.price_usd_per_mt if terms.incoterm.upper() == "FOB" else max(terms.price_usd_per_mt - freight, 0.0)
        return 0.0, fob_price


def evaluate_deal_strategy(
    buyer_cif: float,
    supplier_fob: float,
    freight: float,
    buffer_usd: float,
    round_num: int,
    campaign: Campaign,
    buyer_accepted: bool = False,
) -> dict:
    landed_cost = round(supplier_fob + freight + buffer_usd, 2)
    net_spread = round(buyer_cif - landed_cost, 2)
    net_margin_pct = calculate_net_margin(buyer_cif, landed_cost)

    if net_spread < campaign.min_profit_per_mt_hard:
        return {
            "action": "REJECT_HARD",
            "viable": False,
            "landed_cost": landed_cost,
            "net_spread": net_spread,
            "net_margin_pct": net_margin_pct,
            "reason": (
                f"Net spread ${net_spread:.2f}/MT is below non-negotiable hard floor of "
                f"${campaign.min_profit_per_mt_hard:.2f}/MT (Landed: ${landed_cost:.2f}, Buyer CIF: ${buyer_cif:.2f}). Deal rejected."
            ),
        }

    if buyer_accepted:
        return {
            "action": "ACCEPT_AND_CLOSE",
            "viable": True,
            "landed_cost": landed_cost,
            "net_spread": net_spread,
            "net_margin_pct": net_margin_pct,
            "reason": (
                f"Deal accepted: Buyer accepted desk proposal at net spread of ${net_spread:.2f}/MT "
                f"(Hard floor: ${campaign.min_profit_per_mt_hard:.2f}/MT, Landed: ${landed_cost:.2f}, Buyer CIF: ${buyer_cif:.2f})."
            ),
        }

    if net_spread < campaign.min_profit_per_mt_soft and round_num < campaign.max_negotiation_rounds:
        return {
            "action": "COUNTER_TO_MAXIMIZE",
            "viable": True,
            "landed_cost": landed_cost,
            "net_spread": net_spread,
            "net_margin_pct": net_margin_pct,
            "reason": (
                f"Net spread ${net_spread:.2f}/MT clears hard floor (${campaign.min_profit_per_mt_hard:.2f}/MT) "
                f"but is below soft target (${campaign.min_profit_per_mt_soft:.2f}/MT) at round {round_num}/{campaign.max_negotiation_rounds}. "
                f"Countering to maximize margin."
            ),
        }

    is_optimal = net_spread >= campaign.min_profit_per_mt_soft
    close_desc = (
        f"clears soft target of ${campaign.min_profit_per_mt_soft:.2f}/MT"
        if is_optimal
        else f"accepted at round {round_num}/{campaign.max_negotiation_rounds} above hard floor (${campaign.min_profit_per_mt_hard:.2f}/MT)"
    )
    return {
        "action": "ACCEPT_AND_CLOSE",
        "viable": True,
        "landed_cost": landed_cost,
        "net_spread": net_spread,
        "net_margin_pct": net_margin_pct,
        "reason": (
            f"Deal viable with net spread of ${net_spread:.2f}/MT ({close_desc}). "
            f"Supplier FOB: ${supplier_fob:.2f}, Landed: ${landed_cost:.2f}, Buyer CIF: ${buyer_cif:.2f}."
        ),
    }


def evaluate_deal(
    campaign: Campaign,
    buyer_terms: Optional[ParsedEmail],
    supplier_terms: Optional[ParsedEmail],
    benchmark_fob: float,
    freight: float,
    round_num: int = 0,
    buyer_accepted: bool = False,
) -> dict:
    bounds = calculate_dynamic_bounds(
        benchmark_fob=benchmark_fob,
        freight=freight,
        max_variance_pct=campaign.max_variance_from_benchmark_pct,
        target_margin_pct=campaign.target_margin_pct,
        buffer_usd=campaign.buffer_usd_per_mt,
    )
    fob_ceiling = bounds["dynamic_fob_ceiling"]
    cif_floor = bounds["dynamic_cif_floor"]

    if not supplier_terms:
        return {
            "viable": False,
            "action": None,
            "net_spread": 0.0,
            "net_margin_pct": 0.0,
            "landed_cost": 0.0,
            "reason": "Supplier allocation not yet secured. Zero-Risk Invariant prevents commitment to buyer.",
            **bounds,
        }

    if not buyer_terms:
        return {
            "viable": False,
            "action": None,
            "net_spread": 0.0,
            "net_margin_pct": 0.0,
            "landed_cost": 0.0,
            "reason": "Buyer inquiry pending. Awaiting buyer price quote.",
            **bounds,
        }

    buyer_cif, _ = normalize_terms(buyer_terms, freight)
    _, supplier_fob = normalize_terms(supplier_terms, freight)

    if buyer_terms.quantity_mt != supplier_terms.quantity_mt:
        return {
            "viable": False,
            "action": "REJECT_HARD",
            "net_spread": 0.0,
            "net_margin_pct": 0.0,
            "landed_cost": 0.0,
            "reason": f"Quantity mismatch: Buyer requested {buyer_terms.quantity_mt} MT, but supplier offered {supplier_terms.quantity_mt} MT.",
            **bounds,
        }

    if supplier_fob > fob_ceiling:
        return {
            "viable": False,
            "action": "REJECT_HARD",
            "net_spread": 0.0,
            "net_margin_pct": 0.0,
            "landed_cost": 0.0,
            "reason": f"Supplier FOB ${supplier_fob:.2f}/MT exceeds dynamic ceiling of ${fob_ceiling:.2f}/MT (Benchmark: ${benchmark_fob:.2f} + {campaign.max_variance_from_benchmark_pct}%).",
            **bounds,
        }

    if round_num == 0 and buyer_cif < cif_floor:
        return {
            "viable": False,
            "action": "REJECT_HARD",
            "net_spread": 0.0,
            "net_margin_pct": 0.0,
            "landed_cost": 0.0,
            "reason": f"Buyer CIF ${buyer_cif:.2f}/MT is below minimum viable floor of ${cif_floor:.2f}/MT.",
            **bounds,
        }

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

    return {
        "viable": strategy["viable"],
        "action": strategy["action"],
        "net_spread": strategy["net_spread"],
        "net_margin_pct": strategy["net_margin_pct"],
        "landed_cost": strategy["landed_cost"],
        "reason": strategy["reason"],
        **bounds,
    }
