"""
BSE India free option chain for Sensex (replaces paid Dhan API).

Uses the same public JSON endpoints as bseindia.com (no API key).
Note: BSE may block non-India / cloud IPs — Sensex F&O is skipped when chain is unavailable.
Optional: set SCRAPER_API_KEY with country_code=in for some cloud hosts (best-effort).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

BSE_DERIV_BASE = "https://api.bseindia.com/BseIndiaAPI/api/Derivative"
BSE_REFERER = "https://www.bseindia.com/markets/Derivatives/DeriReports/DeriOptionchain.html"
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "").strip()
SCRAPER_API_URL = "http://api.scraperapi.com"

_CHAIN_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
_EXPIRY_CACHE: dict[int, tuple[float, list[str]]] = {}
_CHAIN_TTL = float(os.getenv("BSE_CHAIN_CACHE_TTL", "120"))
_EXPIRY_TTL = 3600.0
_MIN_GAP = 1.0
_last_fetch = 0.0

_session: requests.Session | None = None


def _session_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": BSE_REFERER,
        "Origin": "https://www.bseindia.com",
        "Host": "api.bseindia.com",
    }


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(_session_headers())
    return _session


def _rate_limit() -> None:
    global _last_fetch
    now = time.time()
    wait = _MIN_GAP - (now - _last_fetch)
    if wait > 0:
        time.sleep(wait)
    _last_fetch = time.time()


def _fetch_url(url: str, params: dict[str, str] | None = None) -> str | None:
    """GET JSON from BSE; optional ScraperAPI proxy on failure."""
    _rate_limit()
    full_url = url
    if params:
        qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        full_url = f"{url}?{qs}"

    def _direct() -> requests.Response:
        return _get_session().get(full_url, timeout=30)

    try:
        resp = _direct()
        text = resp.text or ""
        if resp.status_code == 200 and _looks_like_json(text):
            return text
    except Exception as e:
        log.debug("BSE direct fetch failed: %s", e)
        text = ""

    if not SCRAPER_API_KEY:
        return None

    try:
        proxy_params = {
            "api_key": SCRAPER_API_KEY,
            "url": full_url,
            "country_code": "in",
        }
        resp = requests.get(SCRAPER_API_URL, params=proxy_params, timeout=90)
        text = resp.text or ""
        if resp.status_code == 200 and _looks_like_json(text):
            return text
        log.debug("BSE via ScraperAPI non-JSON (%s): %s", resp.status_code, text[:120])
    except Exception as e:
        log.debug("BSE ScraperAPI fetch failed: %s", e)
    return None


def _looks_like_json(text: str) -> bool:
    t = text.strip()
    if not t or t.startswith("<"):
        return False
    if "error_Bse" in t.lower():
        return False
    return t[0] in "{["


def _parse_json(text: str) -> Any:
    return json.loads(text)


def fetch_bse_expiries(scrip_cd: int = 1) -> list[str]:
    """Nearest-first expiry strings as returned by BSE."""
    now = time.time()
    cached = _EXPIRY_CACHE.get(scrip_cd)
    if cached and now - cached[0] < _EXPIRY_TTL:
        return cached[1]

    text = _fetch_url(f"{BSE_DERIV_BASE}/getexpiry/w", {"scrip_cd": str(scrip_cd), "ProductType": "IO"})
    if not text:
        return []

    data = _parse_json(text)
    expiries: list[str] = []
    if isinstance(data, list):
        expiries = [str(x) for x in data if x]
    elif isinstance(data, dict):
        for key in ("Table", "Expiry", "expiry", "data", "Data"):
            val = data.get(key)
            if isinstance(val, list):
                expiries = [str(x.get("Expiry") or x.get("EXPIRY") or x) for x in val if x]
                break
            if isinstance(val, str) and val:
                expiries = [val]
                break

    if expiries:
        _EXPIRY_CACHE[scrip_cd] = (now, expiries)
    return expiries


def _normalize_expiry_for_chain(expiry: str) -> str:
    """BSE chain API usually expects DD/MM/YYYY."""
    expiry = expiry.strip()
    if re.match(r"^\d{2}/\d{2}/\d{4}$", expiry):
        return expiry
    if re.match(r"^\d{4}-\d{2}-\d{2}$", expiry):
        y, m, d = expiry.split("-")
        return f"{d}/{m}/{y}"
    return expiry


def fetch_bse_option_chain_raw(scrip_cd: int, expiry: str) -> dict[str, Any] | None:
    expiry_param = _normalize_expiry_for_chain(expiry)
    text = _fetch_url(
        f"{BSE_DERIV_BASE}/getOptionChain/w",
        {"scrip_cd": str(scrip_cd), "strprice": "", "Expiry": expiry_param},
    )
    if not text:
        return None
    data = _parse_json(text)
    return data if isinstance(data, dict) else None


def _fval(val: Any) -> float:
    try:
        if val is None or val == "":
            return 0.0
        return float(str(val).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _ival(val: Any) -> int:
    return int(_fval(val))


def _bse_row_to_nse(row: dict[str, Any]) -> dict[str, Any] | None:
    strike_raw = (
        row.get("Strike_Price") or row.get("strikePrice") or row.get("StrikePrice")
        or row.get("STRIKE") or row.get("strike")
    )
    if strike_raw is None:
        return None
    strike = int(_fval(strike_raw))

    ce_oi = _ival(row.get("C_oi") or row.get("CE_OI") or row.get("C_Open_Interest") or row.get("ce_oi"))
    pe_oi = _ival(row.get("P_oi") or row.get("PE_OI") or row.get("P_Open_Interest") or row.get("pe_oi"))
    ce_chg = _ival(row.get("C_chg_oi") or row.get("CE_CHG_OI") or row.get("C_Change_OI") or row.get("ce_chg_oi"))
    pe_chg = _ival(row.get("P_chg_oi") or row.get("PE_CHG_OI") or row.get("P_Change_OI") or row.get("pe_chg_oi"))

    return {
        "strikePrice": strike,
        "CE": {
            "lastPrice": _fval(row.get("C_LTP") or row.get("CE_LTP") or row.get("C_LastPrice") or row.get("ce_ltp")),
            "buyPrice1": _fval(row.get("C_Bid") or row.get("CE_Bid") or row.get("C_BidPrice")),
            "sellPrice1": _fval(row.get("C_Ask") or row.get("CE_Ask") or row.get("C_OfferPrice")),
            "openInterest": ce_oi,
            "changeinOpenInterest": ce_chg,
            "impliedVolatility": _fval(row.get("C_IV") or row.get("CE_IV")),
            "totalTradedVolume": _ival(row.get("C_Volume") or row.get("CE_Volume") or row.get("C_TradedQty")),
        },
        "PE": {
            "lastPrice": _fval(row.get("P_LTP") or row.get("PE_LTP") or row.get("P_LastPrice") or row.get("pe_ltp")),
            "buyPrice1": _fval(row.get("P_Bid") or row.get("PE_Bid") or row.get("P_BidPrice")),
            "sellPrice1": _fval(row.get("P_Ask") or row.get("PE_Ask") or row.get("P_OfferPrice")),
            "openInterest": pe_oi,
            "changeinOpenInterest": pe_chg,
            "impliedVolatility": _fval(row.get("P_IV") or row.get("PE_IV")),
            "totalTradedVolume": _ival(row.get("P_Volume") or row.get("PE_Volume") or row.get("P_TradedQty")),
        },
    }


def _extract_chain_rows(data: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    spot = _fval(
        data.get("UnderlyingValue") or data.get("underlyingValue")
        or data.get("SpotPrice") or data.get("spot") or data.get("UlaValue")
    )
    rows_raw: list[Any] = []
    for key in ("Table", "table", "DerivativeData", "Data", "data", "OptionChain"):
        val = data.get(key)
        if isinstance(val, list) and val:
            rows_raw = val
            break

    rows: list[dict[str, Any]] = []
    for item in rows_raw:
        if not isinstance(item, dict):
            continue
        parsed = _bse_row_to_nse(item)
        if parsed:
            rows.append(parsed)

    if not spot and rows:
        # infer spot from max combined OI strikes — rough; Yahoo fills spot later
        pass

    rows.sort(key=lambda r: r["strikePrice"])
    return spot, rows


def parse_bse_option_chain(scrip_cd: int = 1) -> dict[str, Any] | None:
    """Sensex option chain in NSE-compatible row format."""
    now = time.time()
    cached = _CHAIN_CACHE.get(scrip_cd)
    if cached and now - cached[0] < _CHAIN_TTL:
        return cached[1]

    expiries = fetch_bse_expiries(scrip_cd)
    if not expiries:
        log.warning("BSE Sensex: no expiries (API unreachable or blocked from this host)")
        return None

    expiry = expiries[0]
    raw = fetch_bse_option_chain_raw(scrip_cd, expiry)
    if not raw:
        log.warning("BSE Sensex: option chain fetch failed for expiry %s", expiry)
        return None

    spot, rows = _extract_chain_rows(raw)
    if not rows:
        log.warning("BSE Sensex: empty option chain for expiry %s", expiry)
        return None

    result = {"spot": spot, "expiry": expiry, "rows": rows}
    _CHAIN_CACHE[scrip_cd] = (now, result)
    return result
