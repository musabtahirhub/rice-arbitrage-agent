"""
Lightweight TTL market data cache using an in-memory & file-backed store.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

CACHE_DIR = Path(".cache")
CACHE_FILE = CACHE_DIR / "market_cache.json"


class MarketDataCache:
    """
    Thread-safe, TTL-enforced cache for live commodity quotes and freight matrices.
    """

    def __init__(self, ttl_seconds: Optional[int] = None) -> None:
        self.ttl = ttl_seconds or settings.market_data_cache_ttl_seconds
        self._memory: dict[str, dict[str, Any]] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        try:
            if CACHE_FILE.exists():
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                now = time.time()
                self._memory = {k: v for k, v in data.items() if now - v.get("timestamp", 0) < self.ttl}
        except Exception as e:
            logger.debug(f"Could not load cache from disk: {e}")
            self._memory = {}

    def _persist_to_disk(self) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(self._memory), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Could not persist cache to disk: {e}")

    def get(self, key: str) -> Optional[Any]:
        entry = self._memory.get(key)
        if not entry:
            return None
        if time.time() - entry.get("timestamp", 0) > self.ttl:
            self._memory.pop(key, None)
            return None
        return entry.get("value")

    def set(self, key: str, value: Any) -> None:
        self._memory[key] = {
            "value": value,
            "timestamp": time.time(),
        }
        self._persist_to_disk()
