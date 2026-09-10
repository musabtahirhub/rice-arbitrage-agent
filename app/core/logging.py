"""
Structured logging configuration for commodity trading desk.
"""

from __future__ import annotations

import logging
import sys
from app.core.config import settings


def setup_logging() -> None:
    """Configure system-wide formatted logging."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    """Return a named logger with standard format."""
    return logging.getLogger(name)


# Initialize on import
setup_logging()
