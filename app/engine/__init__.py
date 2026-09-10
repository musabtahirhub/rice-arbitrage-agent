"""
Deterministic arbitrage and risk engine package.
"""

from app.engine.bounds import compute_dynamic_bounds
from app.engine.margin import evaluate_deal

__all__ = ["evaluate_deal", "compute_dynamic_bounds"]
