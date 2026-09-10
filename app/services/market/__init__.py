"""
Market services package providing benchmark rates and freight estimation.
"""

from __future__ import annotations

from typing import Optional
from app.services.market.benchmark_fetcher import BenchmarkFetcher
from app.services.market.cache import MarketDataCache
from app.services.market.freight_engine import FreightEngine


class MarketDataService:
    """
    Unified market service combining live fetchers, ocean freight engine, and TTL caching.
    """

    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.fetcher = BenchmarkFetcher(timeout_seconds=timeout_seconds)
        self.freight_engine = FreightEngine()
        self.cache = MarketDataCache()

    def get_benchmark_rate(
        self,
        commodity_slug: str,
        broken_pct: float = 5.0,
    ) -> tuple[float, str]:
        cache_key = f"bench:{commodity_slug}:{broken_pct}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached[0], cached[1]

        rate, source = self.fetcher.get_benchmark_rate(commodity_slug, broken_pct)
        self.cache.set(cache_key, (rate, source))
        return rate, source

    def estimate_freight(
        self,
        origin_port: str,
        destination_port: str,
    ) -> tuple[float, str]:
        cache_key = f"freight:{origin_port}:{destination_port}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached[0], cached[1]

        rate, source = self.freight_engine.estimate_freight(origin_port, destination_port)
        self.cache.set(cache_key, (rate, source))
        return rate, source


__all__ = ["MarketDataService", "BenchmarkFetcher", "FreightEngine", "MarketDataCache"]
