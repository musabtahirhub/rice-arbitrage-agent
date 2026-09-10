"""
Legacy market data module — maintained as a backward-compatibility shim.
All market data logic has been modularized under `app.services.market.*`.
"""

from app.services.market import (
    BenchmarkFetcher,
    FreightEngine,
    MarketDataCache,
    MarketDataService,
)

__all__ = [
    "BenchmarkFetcher",
    "FreightEngine",
    "MarketDataCache",
    "MarketDataService",
]
