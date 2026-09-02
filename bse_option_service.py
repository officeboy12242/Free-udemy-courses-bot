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
from datetime import datetime
from zoneinfo import ZoneInfo
import time
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

BSE_API_BASE = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_DERIV_BASE = f"{BSE_API_BASE}/Derivative"
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

    # BSE moved the derivative API: /Derivative/getexpiry/w now 302s to
    # error_Bse.html ("The Page you are looking for has been moved"), which is
    # why this returned nothing and SENSEX looked IP-blocked. /ddlExpiry/w is
    # the live endpoint and returns JSON under a "Table" key.
    text = _fetch_url(f"{BSE_API_BASE}/ddlExpiry/w",
                      {"scrip_cd": str(scrip_cd), "ProductType": "IO"})
    if not text:
        text = _fetch_url(f"{BSE_DERIV_BASE}/getexpiry/w",
                          {"scrip_cd": str(scrip_cd), "ProductType": "IO"})
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
                expiries = [
                    str(x.get("Expiry") or x.get("EXPIRY") or x.get("eXPIRY")
                        or x.get("expiry") or x)
                    for x in val if x
                ]
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
    # The live endpoint is DerivOptionChain_IV/w, found in the Angular
    # controller behind beta.bseindia.com's option-chain page. The old
    # Derivative/getOptionChain/w 302s to error_Bse.html, and plain
    # DerivOptionChain/w (without _IV) is also gone.
    #
    # Expiry goes in as BSE returns it from ddlExpiry ("03 Sep 2026"); the
    # DD/MM/YYYY normalisation the old endpoint wanted makes this one fail.
    text = _fetch_url(
        f"{BSE_API_BASE}/DerivOptionChain_IV/w",
        {"scrip_cd": str(scrip_cd), "Expiry": expiry.strip(), "strprice": ""},
    )
    if not text:
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
    """Map one DerivOptionChain_IV row into the NSE shape the rest of the code reads.

    The BSE payload is asymmetric, which is easy to get wrong: call fields carry
    a C_ prefix (C_Last_Trd_Price, C_BidPrice, C_Open_Interest) while the put
    fields for the same strike are unprefixed (Last_Trd_Price, BidPrice,
    Open_Interest). Change in OI is Absolute_Change_OI, not Change_OI.

    Numbers arrive as strings with thousands separators ("79,200.00") and empty
    strings for untraded strikes, both handled by _fval.
    """
    strike_raw = (
        row.get("Strike_Price") or row.get("strikePrice") or row.get("StrikePrice")
        or row.get("STRIKE") or row.get("strike")
    )
    if strike_raw is None:
        return None
    strike = int(_fval(strike_raw))
    if strike <= 0:
        return None

    return {
        "strikePrice": strike,
        "CE": {
            "lastPrice": _fval(row.get("C_Last_Trd_Price") or row.get("C_LTP")),
            "buyPrice1": _fval(row.get("C_BidPrice") or row.get("C_Bid")),
            "sellPrice1": _fval(row.get("C_OfferPrice") or row.get("C_Ask")),
            "bidQty": _ival(row.get("C_BIdQty")),
            "askQty": _ival(row.get("C_OfferQty")),
            "openInterest": _ival(row.get("C_Open_Interest")),
            "changeinOpenInterest": _ival(row.get("C_Absolute_Change_OI")),
            "impliedVolatility": _fval(row.get("C_IV")),
            "totalTradedVolume": _ival(row.get("C_Vol_Traded")),
            "change": _fval(row.get("C_NetChange")),
            "identifier": row.get("C_Series_Code") or "",
        },
        # Put side is the unprefixed half of the same row.
        "PE": {
            "lastPrice": _fval(row.get("Last_Trd_Price") or row.get("P_LTP")),
            "buyPrice1": _fval(row.get("BidPrice") or row.get("P_BidPrice")),
            "sellPrice1": _fval(row.get("OfferPrice") or row.get("P_OfferPrice")),
            "bidQty": _ival(row.get("BIdQty")),
            "askQty": _ival(row.get("OfferQty")),
            "openInterest": _ival(row.get("Open_Interest")),
            "changeinOpenInterest": _ival(row.get("Absolute_Change_OI")),
            "impliedVolatility": _fval(row.get("IV")),
            "totalTradedVolume": _ival(row.get("Vol_Traded")),
            "change": _fval(row.get("NetChange")),
            "identifier": row.get("p_Series_Code") or "",
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
        # DerivOptionChain_IV repeats the underlying on every row rather than
        # sending it once at the top level.
        if not spot:
            spot = _fval(item.get("UlaValue"))
        parsed = _bse_row_to_nse(item)
        if parsed:
            rows.append(parsed)

    if not spot and rows:
        # infer spot from max combined OI strikes — rough; Yahoo fills spot later
        pass

    rows.sort(key=lambda r: r["strikePrice"])
    return spot, rows


_BSE_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _parse_bse_as_on(stamp: str) -> datetime | None:
    """Parse BSE's "02 Sep 2026 | 19:01" stamp into an IST-aware datetime."""
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})\s*\|\s*(\d{1,2}):(\d{2})",
                 (stamp or "").strip())
    if not m:
        return None
    mon = _BSE_MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    try:
        return datetime(int(m.group(3)), mon, int(m.group(1)),
                        int(m.group(4)), int(m.group(5)), tzinfo=_IST)
    except ValueError:
        return None


def _chain_freshness(as_of: datetime | None) -> tuple[str, int | None]:
    """How usable the snapshot is right now.

    Outside market hours the data is final rather than stale, which is a
    different thing from a feed that has gone quiet mid-session; only the
    latter is worth refusing to quote from.
    """
    if as_of is None:
        return "unknown", None
    now = datetime.now(_IST)
    age = int((now - as_of).total_seconds() // 60)
    mins = now.hour * 60 + now.minute
    market_open = now.weekday() < 5 and (9 * 60 + 15) <= mins <= (15 * 60 + 30)
    if not market_open:
        return "closed", age
    return ("live", age) if age <= 15 else ("stale", age)


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

    # BSE keeps serving the last snapshot after the session ends, and the only
    # marker is the ASON stamp. Without surfacing it a settlement price reads as
    # a live quote: the 03-Sep 76600 CE prints 153.15 from the 19:01 snapshot
    # while the live screen showed 166.90.
    as_on = ((raw.get("ASON") or {}).get("DT_TM") or "").strip()
    as_of = _parse_bse_as_on(as_on)
    state, age_min = _chain_freshness(as_of)

    result = {
        "spot": spot,
        "expiry": expiry,
        "rows": rows,
        "as_on": as_on,
        "as_of": as_of,
        "freshness": state,      # live | stale | closed | unknown
        "age_minutes": age_min,
        "is_live": state == "live",
    }
    _CHAIN_CACHE[scrip_cd] = (now, result)
    return result
