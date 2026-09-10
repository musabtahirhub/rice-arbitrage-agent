"""
Legacy directory module — maintained as a backward-compatibility shim.
All directory services have been modularized under `app.services.directory`
and `app.models.counterparty`.
"""

from app.models.counterparty import Counterparty
from app.services.directory import (
    BUYERS_DIRECTORY,
    SUPPLIERS_DIRECTORY,
    get_all_counterparties,
    get_buyers_for_commodity,
    get_counterparty_by_id,
    get_suppliers_for_commodity,
)

__all__ = [
    "BUYERS_DIRECTORY",
    "Counterparty",
    "SUPPLIERS_DIRECTORY",
    "get_all_counterparties",
    "get_buyers_for_commodity",
    "get_counterparty_by_id",
    "get_suppliers_for_commodity",
]
