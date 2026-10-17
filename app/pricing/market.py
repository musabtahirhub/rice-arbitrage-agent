"""Commodity benchmark rates (live Yahoo Finance or scraped feed, cached) and port-to-port freight estimates."""
import json
import re
import time
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.config import PROJECT_ROOT, settings
from app.logger import get_logger

logger = get_logger("arbitrage_desk.market")


CACHE_FILE = PROJECT_ROOT / settings.market_cache_file


CACHE_TTL_SECONDS = settings.market_cache_ttl_seconds


def load_cache() -> Optional[dict]:
    """The cached payload while it is younger than the TTL, else None."""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("timestamp", 0) < CACHE_TTL_SECONDS:
            return data
    except Exception as e:
        logger.warning(f"Failed to read market cache: {e}")
    return None


def save_cache(payload: dict) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write market cache: {e}")


def save_benchmark_rates(rates: dict[str, float], source: str) -> None:
    """Replace the cached rates, keeping any freight quotes already recorded."""
    existing = load_cache() or {}
    save_cache({
        "timestamp": time.time(),
        "source": source,
        "rates": rates,
        "freight_quotes": existing.get("freight_quotes", {}),
    })


def save_freight_quote(lane_key: str, quote: dict, default_rates: dict[str, float]) -> None:
    cached = load_cache() or {"timestamp": time.time(), "rates": default_rates, "freight_quotes": {}}
    cached.setdefault("freight_quotes", {})[lane_key] = {**quote, "updated_at": time.time()}
    save_cache(cached)


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


YAHOO_RICE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/ZR=F?interval=1d&range=5d"


CWT_TO_MT_FACTOR = 22.046


_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
}


# Row index in the Thai Rice Exporters price table -> benchmark keys it feeds.
_THAI_PRICE_ROWS = {3: ("pathumthani fragrant",), 5: ("thai white 5%",), 8: ("thai white 25%",)}


def parse_yahoo_finance_json(data: dict) -> tuple[float, dict[str, float]]:
    """Scale every static benchmark by today's CBOT rough rice (ZR=F) move against the configured base."""
    result = data.get("chart", {}).get("result", [{}])[0]
    cwt_price = result.get("meta", {}).get("regularMarketPrice")
    if cwt_price is None:
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        closes = [c for c in quote.get("close", []) if c is not None]
        cwt_price = closes[-1] if closes else settings.base_rough_rice_cwt_usd

    cwt_price = float(cwt_price)
    rough_rice_usd_mt = round(cwt_price * CWT_TO_MT_FACTOR, 2)
    scale_ratio = cwt_price / settings.base_rough_rice_cwt_usd
    scaled_rates = {commodity: round(price * scale_ratio, 2) for commodity, price in STATIC_BENCHMARKS.items()}
    scaled_rates["rough rice cbot"] = rough_rice_usd_mt
    return rough_rice_usd_mt, scaled_rates


def _price_in_row(rows: list, row_idx: int) -> Optional[float]:
    if row_idx >= len(rows):
        return None
    cells = [td.get_text(strip=True) for td in rows[row_idx].find_all("td")]
    numeric_cells = [c for c in cells if re.match(r"^\d+(\.\d+)?$", c)]
    return float(numeric_cells[-1]) if numeric_cells else None


def _extract_thai_rice_prices(rows: list) -> dict[str, float]:
    extracted = {}
    hom_mali = _price_in_row(rows, 1) or _price_in_row(rows, 2)
    if hom_mali:
        extracted["thai hom mali"] = hom_mali
        extracted["jasmine rice"] = hom_mali
    for row_idx, keys in _THAI_PRICE_ROWS.items():
        price = _price_in_row(rows, row_idx)
        if price:
            extracted.update(dict.fromkeys(keys, price))
    return extracted


def parse_thai_rice_html(html_text: str) -> dict[str, float]:
    soup = BeautifulSoup(html_text, "html.parser")
    for img in soup.find_all("img"):
        if "price_eng-1" not in img.get("src", ""):
            continue
        tr = img.find_parent("tr")
        table = tr.find_parent("table") if tr else None
        if table:
            return _extract_thai_rice_prices(table.find_all("tr"))
    return {}


def _fetch_market_page(target_url: str, timeout: float) -> Optional[httpx.Response]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(target_url, headers=_BROWSER_HEADERS)
            if resp.status_code == 200:
                return resp
    except Exception as exc:
        logger.warning(f"Could not fetch live market rates from {target_url} ({exc}). Using cached/baseline values.")
    return None


def _parse_market_response(resp: httpx.Response) -> tuple[dict[str, float], str]:
    if "json" in resp.headers.get("content-type", "") or resp.text.strip().startswith("{"):
        rough_price, live_rates = parse_yahoo_finance_json(resp.json())
        logger.info(f"Successfully fetched live rough rice benchmark from Yahoo Finance: ${rough_price}/MT")
        return live_rates, f"live_yahoo_finance_cbot_zrf (${rough_price}/MT)"
    live_rates = parse_thai_rice_html(resp.text)
    return live_rates, "live_thairiceexporters_web" if live_rates else "baseline_fallback"


def fetch_live_market_data(force_refresh: bool = False, timeout: Optional[float] = None) -> dict[str, float]:
    if not force_refresh:
        cached = load_cache()
        if cached and "rates" in cached:
            return cached["rates"]

    rates = dict(STATIC_BENCHMARKS)
    source = "baseline_fallback"
    resp = _fetch_market_page(settings.market_source_url or YAHOO_RICE_URL,
                              timeout or settings.market_fetch_timeout_seconds)
    if resp is not None:
        parsed_rates, parsed_source = _parse_market_response(resp)
        if parsed_rates:
            rates.update(parsed_rates)
            source = parsed_source

    save_benchmark_rates(rates, source)
    return rates


def _lookup_commodity_price(rates: dict[str, float], commodity_key: str) -> float:
    rate = rates.get(commodity_key)
    if rate is not None:
        return rate
    for key, price in rates.items():
        if key in commodity_key or commodity_key in key:
            return price
    return settings.default_benchmark_rate


def _apply_broken_penalty(rate: float, broken_pct: float) -> float:
    base_broken = settings.default_broken_percentage
    if broken_pct <= base_broken:
        return rate
    penalty_factor = (broken_pct - base_broken) * settings.broken_penalty_rate
    return rate * (1.0 - min(penalty_factor, settings.broken_penalty_max_cap))


def get_benchmark_rate(commodity: str, broken_pct: float = 5.0) -> float:
    rates = fetch_live_market_data()
    base_rate = _lookup_commodity_price(rates, commodity.lower().strip())
    return round(_apply_broken_penalty(base_rate, broken_pct), 2)


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


def _resolve_base_freight_rate(origin_port: str, destination_port: str) -> float:
    direct_rate = BASE_FREIGHT_MATRIX.get((origin_port, destination_port))
    if direct_rate is not None:
        return direct_rate
    for (orig_candidate, dest_candidate), rate in BASE_FREIGHT_MATRIX.items():
        if orig_candidate in origin_port and dest_candidate in destination_port:
            return rate
    return settings.default_freight_rate


def estimate_freight(origin_port: str, destination_port: str, fuel_surcharge_pct: Optional[float] = None) -> float:
    surcharge = fuel_surcharge_pct if fuel_surcharge_pct is not None else settings.default_fuel_surcharge_pct
    orig = origin_port.lower().strip()
    dest = destination_port.lower().strip()

    base_rate = _resolve_base_freight_rate(orig, dest)
    effective_freight = round(base_rate * (1.0 + surcharge / 100.0), 2)
    save_freight_quote(
        f"{orig}->{dest}",
        {"base_rate_usd": base_rate, "fuel_surcharge_pct": surcharge, "effective_freight_usd": effective_freight},
        STATIC_BENCHMARKS,
    )
    return effective_freight
