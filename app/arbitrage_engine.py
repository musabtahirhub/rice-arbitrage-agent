"""
Legacy arbitrage engine module — maintained as a backward-compatibility shim.
All engine calculations have been modularized under `app.engine.*`.
"""

from app.engine import compute_dynamic_bounds, evaluate_deal

__all__ = ["evaluate_deal", "compute_dynamic_bounds"]
