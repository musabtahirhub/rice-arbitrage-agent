import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional
import httpx
from bs4 import BeautifulSoup

from app.config import settings

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).resolve().parent.parent / settings.market_cache_file
CACHE_TTL_SECONDS = settings.market_cache_ttl_seconds

STATIC_BENCHMARKS: dict[str, float] = {
    "basmati 1121": settings.default_benchmark_rate,
    "super kernel basmati": 980.0,
    "thai white 5%": 496.0,
    "thai white 25%": 397.0,
    "jasmine rice": 780.0,
    "thai hom mali": 1170.0,
    "pathumthani fragrant": 478.0,
    "vietnam 5%": 490.0,
    "parboiled rice 5%": 499.0,
}
BASELINE_BENCHMARKS = STATIC_BENCHMARKS

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

DEFAULT_BENCHMARK = settings.default_benchmark_rate
DEFAULT_FREIGHT = settings.default_freight_rate
YAHOO_RICE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/ZR=F?interval=1d&range=5d"
LIVE_URL = settings.market_source_url
CWT_TO_MT_FACTOR = 22.046
BASE_ROUGH_RICE_CWT = 16.0


def _load_cache() -> Optional[dict]:
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
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write market cache: {e}")


def parse_yahoo_finance_json(data: dict) -> tuple[float, dict[str, float]]:
    meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
    cwt_price = meta.get("regularMarketPrice")

    if cwt_price is None:
        indicators = data.get("chart", {}).get("result", [{}])[0].get("indicators", {}).get("quote", [{}])[0]
        closes = [c for c in indicators.get("close", []) if c is not None]
        cwt_price = closes[-1] if closes else BASE_ROUGH_RICE_CWT

    cwt_price = float(cwt_price)
    rough_rice_usd_mt = round(cwt_price * CWT_TO_MT_FACTOR, 2)

    scale_ratio = cwt_price / BASE_ROUGH_RICE_CWT

    scaled_rates = {}
    for commodity, base_price in STATIC_BENCHMARKS.items():
        scaled_rates[commodity] = round(base_price * scale_ratio, 2)

    scaled_rates["rough rice cbot"] = rough_rice_usd_mt
    return rough_rice_usd_mt, scaled_rates


def parse_thai_rice_html(html_text: str) -> dict[str, float]:
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


def fetch_live_market_data(force_refresh: bool = False, timeout: Optional[float] = None) -> dict[str, float]:
    timeout = timeout or settings.market_fetch_timeout_seconds
    if not force_refresh:
        cached = _load_cache()
        if cached and "rates" in cached:
            return cached["rates"]

    rates = dict(STATIC_BENCHMARKS)
    source = "baseline_fallback"
    target_url = LIVE_URL if "yahoo" in LIVE_URL else YAHOO_RICE_URL

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(target_url, headers=headers)
            if resp.status_code == 200:
                if "json" in resp.headers.get("content-type", "") or resp.text.strip().startswith("{"):
                    rough_price, live_rates = parse_yahoo_finance_json(resp.json())
                    if live_rates:
                        rates.update(live_rates)
                        source = f"live_yahoo_finance_cbot_zrf (${rough_price}/MT)"
                        logger.info(f"Successfully fetched live rough rice benchmark from Yahoo Finance: ${rough_price}/MT")
                else:
                    live_rates = parse_thai_rice_html(resp.text)
                    if live_rates:
                        rates.update(live_rates)
                        source = "live_thairiceexporters_web"
    except Exception as exc:
        logger.warning(f"Could not fetch live market rates from {target_url} ({exc}). Using cached/baseline values.")

    cache_payload = {
        "timestamp": time.time(),
        "source": source,
        "rates": rates,
        "freight_quotes": {},
    }
    existing = _load_cache()
    if existing and "freight_quotes" in existing:
        cache_payload["freight_quotes"] = existing["freight_quotes"]

    _save_cache(cache_payload)
    return rates


def get_benchmark_rate(commodity: str, broken_pct: float = 5.0) -> float:
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

    if broken_pct > 5.0:
        penalty_factor = (broken_pct - 5.0) * 0.005
        rate = rate * (1.0 - min(penalty_factor, 0.20))

    return round(rate, 2)


def estimate_freight(origin_port: str, destination_port: str, fuel_surcharge_pct: Optional[float] = None) -> float:
    if fuel_surcharge_pct is None:
        fuel_surcharge_pct = settings.default_fuel_surcharge_pct

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

    effective_freight = round(base_rate * (1.0 + fuel_surcharge_pct / 100.0), 2)

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
