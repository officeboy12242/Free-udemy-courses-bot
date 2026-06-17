"""
Dhan HQ v2 API — BSE Sensex option chain (NSE indices use jugaad-data in fno_entry_service).

Requires env:
  DHAN_ACCESS_TOKEN
  DHAN_CLIENT_ID

Rate limit: 1 unique option-chain request per 3 seconds (Dhan policy).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DHAN_BASE = "https://api.dhan.co/v2"
_MIN_INTERVAL_SEC = 3.1
_last_request_at = 0.0
_expiry_cache: dict[tuple[int, str], tuple[float, list[str]]] = {}
_EXPIRY_CACHE_TTL = 3600.0


def dhan_configured() -> bool:
    return bool(os.getenv("DHAN_ACCESS_TOKEN", "").strip() and os.getenv("DHAN_CLIENT_ID", "").strip())


def _headers() -> dict[str, str] | None:
    token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
    client_id = os.getenv("DHAN_CLIENT_ID", "").strip()
    if not token or not client_id:
        return None
    return {
        "access-token": token,
        "client-id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _rate_limit() -> None:
    global _last_request_at
    now = time.time()
    wait = _MIN_INTERVAL_SEC - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.time()


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
    hdrs = _headers()
    if not hdrs:
        return None
    _rate_limit()
    try:
        resp = requests.post(f"{DHAN_BASE}/{path}", headers=hdrs, json=payload, timeout=30)
        body = resp.json()
        if body.get("status") != "success":
            log.warning("Dhan %s failed (%s): %s", path, resp.status_code, body)
            return None
        return body.get("data")
    except Exception as e:
        log.warning("Dhan %s error: %s", path, e)
        return None


def verify_dhan_credentials() -> bool:
    """Quick profile check — logs result, returns True if token is valid."""
    hdrs = _headers()
    if not hdrs:
        log.info("Dhan API: not configured (set DHAN_ACCESS_TOKEN + DHAN_CLIENT_ID)")
        return False
    try:
        resp = requests.get(f"{DHAN_BASE}/profile", headers={"access-token": hdrs["access-token"]}, timeout=20)
        if resp.status_code == 200:
            prof = resp.json()
            log.info(
                "Dhan API OK — client %s, data plan %s",
                prof.get("dhanClientId"),
                prof.get("dataPlan"),
            )
            return True
        log.warning("Dhan profile check failed (%s): %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        log.warning("Dhan profile check error: %s", e)
        return False


def fetch_expiry_list(underlying_id: int, segment: str = "IDX_I") -> list[str]:
    cache_key = (underlying_id, segment)
    now = time.time()
    cached = _expiry_cache.get(cache_key)
    if cached and now - cached[0] < _EXPIRY_CACHE_TTL:
        return cached[1]

    data = _post(
        "optionchain/expirylist",
        {"UnderlyingScrip": underlying_id, "UnderlyingSeg": segment},
    )
    if not isinstance(data, list) or not data:
        return []
    _expiry_cache[cache_key] = (now, data)
    return data


def fetch_option_chain(underlying_id: int, expiry: str, segment: str = "IDX_I") -> dict[str, Any] | None:
    data = _post(
        "optionchain",
        {"UnderlyingScrip": underlying_id, "UnderlyingSeg": segment, "Expiry": expiry},
    )
    return data if isinstance(data, dict) else None


def _dhan_leg_to_nse(leg: dict[str, Any]) -> dict[str, Any]:
    oi = int(leg.get("oi") or 0)
    prev_oi = int(leg.get("previous_oi") or 0)
    chg = leg.get("oi_change")
    if chg is None:
        chg = oi - prev_oi
    return {
        "lastPrice": float(leg.get("last_price") or 0),
        "buyPrice1": float(leg.get("top_bid_price") or 0),
        "sellPrice1": float(leg.get("top_ask_price") or 0),
        "openInterest": oi,
        "changeinOpenInterest": int(chg or 0),
        "impliedVolatility": float(leg.get("implied_volatility") or 0),
        "totalTradedVolume": int(leg.get("volume") or 0),
    }


def dhan_chain_to_nse_format(chain: dict[str, Any], expiry: str) -> dict[str, Any] | None:
    """Convert Dhan option chain payload to NSE-compatible rows for fno_entry_service."""
    oc = chain.get("oc") or {}
    if not oc:
        return None
    spot = float(chain.get("last_price") or 0)
    rows: list[dict[str, Any]] = []
    for strike_str, legs in oc.items():
        strike = int(float(strike_str))
        ce = legs.get("ce") or {}
        pe = legs.get("pe") or {}
        rows.append({
            "strikePrice": strike,
            "expiryDates": expiry,
            "CE": _dhan_leg_to_nse(ce),
            "PE": _dhan_leg_to_nse(pe),
        })
    rows.sort(key=lambda r: r["strikePrice"])
    return {"spot": spot, "expiry": expiry, "rows": rows}


def parse_dhan_option_chain(underlying_id: int, segment: str = "IDX_I") -> dict[str, Any] | None:
    """Nearest expiry Sensex (or other BSE index) chain in NSE row format."""
    if not dhan_configured():
        return None
    expiries = fetch_expiry_list(underlying_id, segment)
    if not expiries:
        return None
    expiry = expiries[0]
    chain = fetch_option_chain(underlying_id, expiry, segment)
    if not chain:
        return None
    return dhan_chain_to_nse_format(chain, expiry)
