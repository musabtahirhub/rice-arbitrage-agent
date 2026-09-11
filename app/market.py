"""
Market benchmark price lookup and port-to-port freight rate estimator.
"""
from typing import Tuple

# FOB benchmark rates in USD per Metric Ton (at standard 5% broken grain)
BENCHMARK_RATES: dict[str, float] = {
    "basmati 1121": 900.0,
    "super kernel basmati": 980.0,
    "thai white 5%": 520.0,
    "jasmine rice": 780.0,
    "vietnam 5%": 490.0,
}

# Container freight rates per MT in USD
FREIGHT_MATRIX: dict[tuple[str, str], float] = {
    ("karachi", "jebel ali"): 45.0,
    ("karachi", "dammam"): 50.0,
    ("mundra", "jebel ali"): 55.0,
    ("mundra", "dammam"): 60.0,
    ("bangkok", "jebel ali"): 75.0,
    ("bangkok", "dammam"): 80.0,
    ("ho chi minh", "jebel ali"): 70.0,
    ("ho chi minh", "dammam"): 75.0,
}

DEFAULT_BENCHMARK = 900.0
DEFAULT_FREIGHT = 50.0


def get_benchmark_rate(commodity: str, broken_pct: float = 5.0) -> float:
    """
    Look up the FOB benchmark rate for a commodity variety.
    Applies a simple quality discount if broken grain percentage exceeds standard 5%.
    """
    comm_key = commodity.lower().strip()
    rate = BENCHMARK_RATES.get(comm_key)

    if rate is None:
        for key, price in BENCHMARK_RATES.items():
            if key in comm_key or comm_key in key:
                rate = price
                break

    if rate is None:
        rate = DEFAULT_BENCHMARK

    # Adjust price downwards if broken percentage exceeds standard 5%
    if broken_pct > 5.0:
        penalty_factor = (broken_pct - 5.0) * 0.005  # 0.5% discount per 1% excess broken
        rate = rate * (1.0 - min(penalty_factor, 0.20))

    return round(rate, 2)


def estimate_freight(origin_port: str, destination_port: str) -> float:
    """
    Estimate container ocean freight per metric ton between two ports.
    """
    orig = origin_port.lower().strip()
    dest = destination_port.lower().strip()

    if (orig, dest) in FREIGHT_MATRIX:
        return FREIGHT_MATRIX[(orig, dest)]

    # Fuzzy match port pairs
    for (o, d), freight in FREIGHT_MATRIX.items():
        if o in orig and d in dest:
            return freight

    return DEFAULT_FREIGHT
