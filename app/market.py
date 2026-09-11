"""
Real-world live market benchmark pricing scraper and dynamic container freight estimator.
Fetches FOB export rates from public commodity boards (Thai Rice Exporters Association),
with a 6-hour local JSON cache (market_cache.json) and graceful baseline fallbacks.
"""
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Cache file path and 6-hour Time-To-Live
CACHE_FILE = Path(__file__).resolve().parent.parent / "market_cache.json"
CACHE_TTL_SECONDS = 6 * 3600  # 6 hours

# Baseline static rates in USD/MT FOB (used as fallback when web feeds are offline)
BASELINE_BENCHMARKS: dict[str, float] = {
    "basmati 1121": 900.0,
    "super kernel basmati": 980.0,
    "thai white 5%": 496.0,
    "thai white 25%": 397.0,
    "jasmine rice": 780.0,
    "thai hom mali": 1170.0,
    "pathumthani fragrant": 478.0,
    "vietnam 5%": 490.0,
    "parboiled rice 5%": 499.0,
}

# Base container ocean freight matrix per MT in USD (standard 20ft container loads)
BASE_FREIGHT_MATRIX: dict[tuple[str, str], float] = {
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
LIVE_URL = "http://www.thairiceexporters.or.th/price_eng.html"


def _load_cache() -> Optional[dict]:
    """Read cached market data if it exists and has not expired."""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cached_time = data.get("timestamp", 0)
        if time.time() - cached_time < CACHE_TTL_SECONDS:
            return data
    except Exception as e:
        logger.warning(f"Failed to read market cache: {e}")
    return None


def _save_cache(payload: dict) -> None:
    """Persist market data and timestamps to market_cache.json."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write market cache: {e}")


def parse_thai_rice_html(html_text: str) -> dict[str, float]:
    """
    Parse HTML table from Thai Rice Exporters Association to extract FOB export prices.
    Table rows:
      Row 1: Hom Mali 100% Grade B
      Row 2: Hom Mali (crop 2024/25)
      Row 3: Pathumthani Fragrant Rice
      Row 4: White Rice 100% Grade B
      Row 5: White Rice 5%
      Row 8: White Rice 25%
    """
    soup = BeautifulSoup(html_text, "html.parser")
    extracted = {}

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "price_eng-1" in src:
            tr = img.find_parent("tr")
            if not tr:
                continue
            tbody = tr.find_parent("table")
            if not tbody:
                continue
            rows = tbody.find_all("tr")

            def get_latest_price(row_idx: int) -> Optional[float]:
                if row_idx >= len(rows):
                    return None
                cells = [td.get_text(strip=True) for td in rows[row_idx].find_all("td")]
                numeric_cells = [c for c in cells if re.match(r"^\d+(\.\d+)?$", c)]
                if numeric_cells:
                    return float(numeric_cells[-1])
                return None

            hom_mali = get_latest_price(1) or get_latest_price(2)
            if hom_mali:
                extracted["thai hom mali"] = hom_mali
                extracted["jasmine rice"] = hom_mali

            pathum = get_latest_price(3)
            if pathum:
                extracted["pathumthani fragrant"] = pathum

            white_5 = get_latest_price(5)
            if white_5:
                extracted["thai white 5%"] = white_5

            white_25 = get_latest_price(8)
            if white_25:
                extracted["thai white 25%"] = white_25

    return extracted


def fetch_live_market_data(force_refresh: bool = False, timeout: float = 3.5) -> dict[str, float]:
    """
    Fetch live benchmark prices from public market board with 6-hour caching
    and robust fallback to baseline values if the site times out or is down.
    """
    if not force_refresh:
        cached = _load_cache()
        if cached and "rates" in cached:
            return cached["rates"]

    rates = dict(BASELINE_BENCHMARKS)
    source = "baseline_fallback"

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(LIVE_URL, headers=headers)
            if resp.status_code == 200:
                live_rates = parse_thai_rice_html(resp.text)
                if live_rates:
                    rates.update(live_rates)
                    source = "live_thairiceexporters_web"
                    logger.info(f"Successfully scraped {len(live_rates)} live benchmark rates from {LIVE_URL}")
    except Exception as exc:
        logger.warning(f"Could not fetch live market rates from {LIVE_URL} ({exc}). Using cached/baseline values.")

    # Persist in local cache file
    cache_payload = {
        "timestamp": time.time(),
        "source": source,
        "rates": rates,
        "freight_quotes": {},
    }
    # Preserve existing freight quotes if available
    existing = _load_cache()
    if existing and "freight_quotes" in existing:
        cache_payload["freight_quotes"] = existing["freight_quotes"]

    _save_cache(cache_payload)
    return rates


def get_benchmark_rate(commodity: str, broken_pct: float = 5.0) -> float:
    """
    Look up the live FOB benchmark rate for a commodity variety.
    Applies quality discount if broken grain percentage exceeds standard 5%.
    """
    rates = fetch_live_market_data()
    comm_key = commodity.lower().strip()

    rate = rates.get(comm_key)
    if rate is None:
        for key, price in rates.items():
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


def estimate_freight(origin_port: str, destination_port: str, fuel_surcharge_pct: float = 0.0) -> float:
    """
    Dynamic container freight estimator calculating port-to-port ocean rates
    with lane averages, fuel surcharges, and caching in market_cache.json.
    """
    orig = origin_port.lower().strip()
    dest = destination_port.lower().strip()

    base_rate = None
    if (orig, dest) in BASE_FREIGHT_MATRIX:
        base_rate = BASE_FREIGHT_MATRIX[(orig, dest)]
    else:
        for (o, d), rate in BASE_FREIGHT_MATRIX.items():
            if o in orig and d in dest:
                base_rate = rate
                break

    if base_rate is None:
        base_rate = DEFAULT_FREIGHT

    # Apply dynamic fuel/bunker surcharge (BAF)
    effective_freight = round(base_rate * (1.0 + fuel_surcharge_pct / 100.0), 2)

    # Store calculation in market cache
    cached = _load_cache() or {"timestamp": time.time(), "rates": BASELINE_BENCHMARKS, "freight_quotes": {}}
    lane_key = f"{orig}->{dest}"
    if "freight_quotes" not in cached:
        cached["freight_quotes"] = {}
    cached["freight_quotes"][lane_key] = {
        "base_rate_usd": base_rate,
        "fuel_surcharge_pct": fuel_surcharge_pct,
        "effective_freight_usd": effective_freight,
        "updated_at": time.time(),
    }
    _save_cache(cached)

    return effective_freight
