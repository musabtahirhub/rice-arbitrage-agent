"""
Ocean freight matrix and container logistics estimator.
"""

from __future__ import annotations

from typing import Optional

# Container freight rates (USD / MT in 20ft lots)
_FREIGHT_MATRIX: dict[tuple[str, str], float] = {
    ("mundra", "jebel_ali"): 55.0,
    ("nhava_sheva", "jebel_ali"): 60.0,
    ("kandla", "jebel_ali"): 55.0,
    ("karachi", "jebel_ali"): 45.0,
    ("mundra", "dammam"): 70.0,
    ("nhava_sheva", "dammam"): 75.0,
    ("karachi", "dammam"): 65.0,
    ("bangkok", "jebel_ali"): 85.0,
    ("laem_chabang", "jebel_ali"): 85.0,
    ("ho_chi_minh", "jebel_ali"): 90.0,
    ("hai_phong", "jebel_ali"): 95.0,
    ("bangkok", "dammam"): 95.0,
    ("ho_chi_minh", "dammam"): 100.0,
    ("mundra", "mombasa"): 100.0,
    ("nhava_sheva", "mombasa"): 105.0,
}

_DEFAULT_FREIGHT_USD = 80.0

_PORT_ALIASES: dict[str, str] = {
    "jebel ali": "jebel_ali",
    "jebelali": "jebel_ali",
    "dubai": "jebel_ali",
    "dammam": "dammam",
    "karachi": "karachi",
    "mundra": "mundra",
    "kandla": "kandla",
    "nhava sheva": "nhava_sheva",
    "nhavasheva": "nhava_sheva",
    "mumbai": "nhava_sheva",
    "bangkok": "bangkok",
    "laem chabang": "laem_chabang",
    "laemchabang": "laem_chabang",
    "ho chi minh": "ho_chi_minh",
    "saigon": "ho_chi_minh",
    "hai phong": "hai_phong",
    "mombasa": "mombasa",
}


def normalize_port(port: Optional[str]) -> str:
    """Normalize user or email port names to canonical slugs."""
    if not port:
        return "jebel_ali"
    cleaned = port.strip().lower()
    return _PORT_ALIASES.get(cleaned, cleaned.replace(" ", "_"))


class FreightEngine:
    """
    Ocean freight calculator for containerized physical commodities.
    """

    def estimate_freight(
        self,
        origin_port: str,
        destination_port: str,
    ) -> tuple[float, str]:
        """
        Estimate freight cost per metric ton between two ports.
        Returns (rate_usd, source).
        """
        orig = normalize_port(origin_port)
        dest = normalize_port(destination_port)

        lane = (orig, dest)
        if lane in _FREIGHT_MATRIX:
            return _FREIGHT_MATRIX[lane], "fallback_cache"

        lane_rev = (dest, orig)
        if lane_rev in _FREIGHT_MATRIX:
            return _FREIGHT_MATRIX[lane_rev], "fallback_cache"

        return _DEFAULT_FREIGHT_USD, "fallback_cache_default"
