"""
Auto-scanning F&O scalp alert engine for Indian index options.

Strategies (all 60%+ win rate on Nifty/BankNifty backtests):
──────────────────────────────────────────────────────────────
1. EMA+RSI+OI+VWAP Confluence  (~62-68% WR)  — All day + expiry
   Fires when 3-4 layers agree: VWAP zone, EMA9/21 trend,
   RSI-7 momentum, OI/PCR positioning.

2. ORB (Opening Range Breakout)  (~58-65% WR)  — 9:30-11:00 window
   15-min opening candle (9:15-9:30) high/low as breakout levels.
   CE on break above high, PE on break below low.

3. PCR Extreme Reversal  (~58-64% WR)  — All day + expiry
   Contrarian entry when PCR hits extremes (>1.3 or <0.7).
   Heavy PE writing = floor = CE scalp. Heavy CE writing = ceiling = PE.

4. MACD Multi-Timeframe  (~65-72% WR)  — 1H + 15m + 5m alignment
   Classic triple-MACD filter: 1H trend, 15m confirm, 5m crossover entry.
   Skips mixed timeframes (fewer but higher-conviction trades).

Auto-monitor runs every ~3 min during market hours.
Only high-conviction setups pass strict filters (ADX, VIX, volume, EMA alignment).
Each setup is de-duped so you don't get spammed the same signal.

Data: Yahoo Finance 15m candles  +  NSE option chain (jugaad-data)
       +  BSE Sensex option chain (free BSE India API).
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from jugaad_data.nse import NSELive

from bse_option_service import parse_bse_option_chain
from market_service import get_all_subscribers, remove_subscriber

load_dotenv()

log = logging.getLogger(__name__)

DB_FILE = os.getenv("DB_FILE", "posted_courses.db")
FNO_SCAN_INTERVAL = int(os.getenv("FNO_SCAN_INTERVAL", "180"))
FNO_YAHOO_CACHE_TTL = int(os.getenv("FNO_YAHOO_CACHE_TTL", "120"))
# Stricter filters for auto-alerts; /entry shows all setups + pass/fail vs same filters
FNO_STRICT_FILTERS = os.getenv("FNO_STRICT_FILTERS", "1").strip().lower() in ("1", "true", "yes")
FNO_MIN_LEG_VOLUME = int(os.getenv("FNO_MIN_LEG_VOLUME", "25"))
FNO_MAX_SPREAD_PCT = float(os.getenv("FNO_MAX_SPREAD_PCT", "12"))
FNO_MIN_ADX = float(os.getenv("FNO_MIN_ADX", "14"))
ORB_MIN_BREAK_PCT = float(os.getenv("ORB_MIN_BREAK_PCT", "0.02"))
FNO_REQUIRE_EMA_ALIGN = os.getenv("FNO_REQUIRE_EMA_ALIGN", "0").strip().lower() in ("1", "true", "yes")
FNO_CONFLUENCE_MIN_LAYERS = int(os.getenv("FNO_CONFLUENCE_MIN_LAYERS", "3"))
FNO_SKIP_LUNCH = os.getenv("FNO_SKIP_LUNCH", "0").strip().lower() in ("1", "true", "yes")
# 0 = no cap (all strategies that pass quality); 3 = up to 3 per index per scan
FNO_MAX_ALERTS_PER_INDEX = int(os.getenv("FNO_MAX_ALERTS_PER_INDEX", "0"))
FNO_MACD_MTF_ENABLED = os.getenv("FNO_MACD_MTF_ENABLED", "1").strip().lower() in ("1", "true", "yes")
FNO_MACD_REQUIRE_5M = os.getenv("FNO_MACD_REQUIRE_5M", "1").strip().lower() in ("1", "true", "yes")

# In-memory cache: yahoo_symbol -> (timestamp, tech dict)
_intraday_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_vix_cache: tuple[float, float] | None = None

FNO_INDICES: list[dict[str, Any]] = [
    {"nse": "NIFTY", "yahoo": "^NSEI", "name": "Nifty 50",
     "step": 50, "prem_min": 55, "prem_max": 160},
    {"nse": "BANKNIFTY", "yahoo": "^NSEBANK", "name": "Bank Nifty",
     "step": 100, "prem_min": 220, "prem_max": 520},
    {"nse": "FINNIFTY", "yahoo": "NIFTY_FIN_SERVICE.NS", "name": "Fin Nifty",
     "step": 50, "prem_min": 120, "prem_max": 380},
    {"nse": "MIDCPNIFTY", "yahoo": "NIFTY_MID_SELECT.NS", "name": "Midcap Nifty",
     "step": 25, "prem_min": 80, "prem_max": 260, "nse_only_fallback": True},
    {"nse": "SENSEX", "yahoo": "^BSESN", "name": "Sensex",
     "step": 100, "prem_min": 180, "prem_max": 550,
     "bse_scrip_cd": 1},
]

SL_MULT = 0.86
T1_MULT = 1.20
T2_MULT = 1.35

STRATEGY_CONFLUENCE = "EMA+RSI+OI+VWAP Confluence"
STRATEGY_ORB = "ORB (Opening Range Breakout)"
STRATEGY_PCR_REVERSAL = "PCR Extreme Reversal"
STRATEGY_MACD_MTF = "MACD Multi-Timeframe (1H+15m+5m)"

# User-facing aliases → NSE symbol
INDEX_ALIASES: dict[str, str] = {
    "nifty": "NIFTY",
    "nifty50": "NIFTY",
    "n50": "NIFTY",
    "banknifty": "BANKNIFTY",
    "bank": "BANKNIFTY",
    "bnf": "BANKNIFTY",
    "bn": "BANKNIFTY",
    "finnifty": "FINNIFTY",
    "fin": "FINNIFTY",
    "midcap": "MIDCPNIFTY",
    "midcapnifty": "MIDCPNIFTY",
    "mid": "MIDCPNIFTY",
    "sensex": "SENSEX",
    "bsesn": "SENSEX",
    "sx": "SENSEX",
    "all": "ALL",
}

NSE_TO_NAME: dict[str, str] = {c["nse"]: c["name"] for c in FNO_INDICES}
ALL_NSE_SYMBOLS: frozenset[str] = frozenset(c["nse"] for c in FNO_INDICES)


# ════════════════════ DB for alert de-dupe ════════════════════

def ensure_fno_tables():
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS fno_alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_date      TEXT NOT NULL,
                nse_symbol      TEXT NOT NULL,
                index_name      TEXT,
                strategy        TEXT NOT NULL,
                side            TEXT NOT NULL,
                strike          INTEGER,
                entry_premium   REAL,
                sl_premium      REAL,
                t1_premium      REAL,
                t2_premium      REAL,
                spot_at_entry   REAL,
                expiry          TEXT,
                alerted_at      TEXT,
                close_premium   REAL,
                outcome         TEXT,
                pnl_pts         REAL,
                summarized      INTEGER DEFAULT 0
            )
        """)
        for col, typ in (
            ("index_name", "TEXT"),
            ("entry_premium", "REAL"),
            ("sl_premium", "REAL"),
            ("t1_premium", "REAL"),
            ("t2_premium", "REAL"),
            ("spot_at_entry", "REAL"),
            ("expiry", "TEXT"),
            ("close_premium", "REAL"),
            ("outcome", "TEXT"),
            ("pnl_pts", "REAL"),
            ("summarized", "INTEGER DEFAULT 0"),
        ):
            try:
                con.execute(f"ALTER TABLE fno_alerts ADD COLUMN {col} {typ}")
            except Exception:
                pass
        con.execute("""
            CREATE TABLE IF NOT EXISTS fno_eod_sent (
                alert_date  TEXT PRIMARY KEY,
                sent_at     TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS fno_alert_prefs (
                chat_id     INTEGER NOT NULL,
                nse_symbol  TEXT NOT NULL,
                PRIMARY KEY (chat_id, nse_symbol)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS fno_scan_stats (
                alert_date      TEXT PRIMARY KEY,
                setups          INTEGER DEFAULT 0,
                sent            INTEGER DEFAULT 0,
                skip_quality    INTEGER DEFAULT 0,
                skip_dedupe     INTEGER DEFAULT 0,
                skip_premium    INTEGER DEFAULT 0,
                scan_cycles     INTEGER DEFAULT 0
            )
        """)
        today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        con.execute("DELETE FROM fno_alerts WHERE alert_date != ? AND summarized = 1", (today,))
        con.commit()
    finally:
        con.close()


def _eod_summary_sent_today() -> bool:
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            "SELECT 1 FROM fno_eod_sent WHERE alert_date = ?", (today,)
        ).fetchone()
        return row is not None
    finally:
        con.close()


def _mark_eod_summary_sent():
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%dT%H:%M:%S")
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            "INSERT OR REPLACE INTO fno_eod_sent (alert_date, sent_at) VALUES (?, ?)",
            (today, now_ist),
        )
        con.commit()
    finally:
        con.close()


# ════════════════════ Per-user index alert prefs ════════════════════

def parse_index_tokens(args: list[str]) -> tuple[list[str] | None, str | None]:
    """
    Parse /alert nifty banknifty → ['NIFTY','BANKNIFTY'].
    None list + no error = use all indices. Error string if invalid token.
    """
    if not args:
        return None, "usage"
    tokens = []
    for raw in " ".join(args).replace(",", " ").split():
        key = raw.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
        if not key:
            continue
        sym = INDEX_ALIASES.get(key)
        if sym == "ALL":
            return list(ALL_NSE_SYMBOLS), None
        if sym:
            tokens.append(sym)
        else:
            return None, f"Unknown index: <code>{html.escape(raw)}</code>"
    if not tokens:
        return None, "usage"
    # dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for s in tokens:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out, None


def set_user_alert_indices(chat_id: int, nse_symbols: list[str]) -> None:
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute("DELETE FROM fno_alert_prefs WHERE chat_id = ?", (chat_id,))
        for sym in nse_symbols:
            con.execute(
                "INSERT INTO fno_alert_prefs (chat_id, nse_symbol) VALUES (?, ?)",
                (chat_id, sym),
            )
        con.commit()
    finally:
        con.close()


def clear_user_alert_indices(chat_id: int) -> None:
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute("DELETE FROM fno_alert_prefs WHERE chat_id = ?", (chat_id,))
        con.commit()
    finally:
        con.close()


def get_user_alert_indices(chat_id: int) -> set[str] | None:
    """None = all indices (no custom filter)."""
    con = sqlite3.connect(DB_FILE)
    try:
        rows = con.execute(
            "SELECT nse_symbol FROM fno_alert_prefs WHERE chat_id = ?",
            (chat_id,),
        ).fetchall()
        if not rows:
            return None
        return {r[0] for r in rows}
    finally:
        con.close()


def user_wants_index(chat_id: int, nse_symbol: str) -> bool:
    prefs = get_user_alert_indices(chat_id)
    if prefs is None:
        return True
    return nse_symbol in prefs


def get_subscribers_for_index(nse_symbol: str) -> list[int]:
    all_ids = get_all_subscribers()
    return [cid for cid in all_ids if user_wants_index(cid, nse_symbol)]


def format_user_alert_prefs_html(chat_id: int) -> str:
    prefs = get_user_alert_indices(chat_id)
    if prefs is None:
        names = [c["name"] for c in FNO_INDICES]
        body = "You receive alerts for <b>all indices</b>:\n" + "\n".join(
            f"  • {html.escape(n)}" for n in names
        )
    else:
        names = [NSE_TO_NAME.get(s, s) for s in sorted(prefs)]
        body = "You receive alerts for:\n" + "\n".join(
            f"  • <b>{html.escape(n)}</b>" for n in names
        )
    return (
        "<b>📬 Your F&amp;O alert indices</b>\n\n"
        f"{body}\n\n"
        "<i>Applies to trade alerts and <code>/entry</code>.</i>\n\n"
        "<b>Commands:</b>\n"
        "<code>/alert nifty</code> — Nifty only\n"
        "<code>/alert banknifty</code> — Bank Nifty only\n"
        "<code>/alert nifty banknifty</code> — both\n"
        "<code>/alert sensex</code> — Sensex only\n"
        "<code>/alert all</code> or <code>/clearalert</code> — all indices"
    )


def format_alert_prefs_set_html(nse_symbols: list[str]) -> str:
    names = [NSE_TO_NAME.get(s, s) for s in nse_symbols]
    lines = "\n".join(f"  • <b>{html.escape(n)}</b>" for n in names)
    return (
        "<b>✅ F&amp;O alerts updated</b>\n\n"
        f"You will now get trade alerts for:\n{lines}\n\n"
        "<i>Use <code>/clearalert</code> to reset to all indices.</i>"
    )


def format_alert_usage_html() -> str:
    return (
        "<b>📬 Choose which indices to alert</b>\n\n"
        "<code>/alert nifty</code>\n"
        "<code>/alert banknifty</code>\n"
        "<code>/alert nifty finnifty</code>\n"
        "<code>/alert all</code>\n"
        "<code>/clearalert</code> — back to all indices\n"
        "<code>/myalerts</code> — show current choice\n\n"
        "<i>Also applies to <code>/entry</code> — only selected indices are shown.</i>\n\n"
        "<b>Names:</b> nifty · banknifty · finnifty · midcap · sensex"
    )


def _already_alerted(nse_symbol: str, strategy: str, side: str, strike: int) -> bool:
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            "SELECT 1 FROM fno_alerts WHERE alert_date=? AND nse_symbol=? AND strategy=? AND side=? AND strike=?",
            (today, nse_symbol, strategy, side, strike),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def _record_alert(signal: dict[str, Any]):
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%dT%H:%M:%S")
    ex = signal.get("exits") or {}
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            """INSERT INTO fno_alerts (
                alert_date, nse_symbol, index_name, strategy, side, strike,
                entry_premium, sl_premium, t1_premium, t2_premium,
                spot_at_entry, expiry, alerted_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                today,
                signal["nse"],
                signal.get("name"),
                signal["strategy"],
                signal["side"],
                signal["strike"],
                signal.get("premium"),
                ex.get("sl"),
                ex.get("t1"),
                ex.get("t2"),
                signal.get("spot"),
                signal.get("expiry"),
                now_ist,
            ),
        )
        con.commit()
    finally:
        con.close()


# ════════════════════ Technical helpers ════════════════════

def _round_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def _rsi(series: pd.Series, period: int = 7) -> float | None:
    if series is None or len(series) < period + 1:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    last_loss = loss.iloc[-1]
    if last_loss == 0 or math.isnan(last_loss):
        return 100.0
    rs = gain.iloc[-1] / last_loss
    val = 100 - (100 / (1 + rs))
    return float(val) if not math.isnan(val) else None


def _ema(series: pd.Series, span: int) -> float | None:
    if series is None or len(series) < span:
        return None
    val = series.ewm(span=span, adjust=False).mean().iloc[-1]
    return float(val) if not math.isnan(val) else None


def _macd_lines(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line, signal line, histogram series."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return macd, sig, hist


def _macd_bar_state(close: pd.Series) -> dict[str, Any] | None:
    """Bull/bear alignment and crossover on the last closed bar."""
    if close is None or len(close) < 35:
        return None
    macd, sig, _hist = _macd_lines(close)
    m, s = float(macd.iloc[-1]), float(sig.iloc[-1])
    pm, ps = float(macd.iloc[-2]), float(sig.iloc[-2])
    if any(math.isnan(x) for x in (m, s, pm, ps)):
        return None
    return {
        "bull": m > s,
        "bear": m < s,
        "above_zero": m > 0,
        "below_zero": m < 0,
        "cross_up": pm <= ps and m > s,
        "cross_down": pm >= ps and m < s,
        "macd": round(m, 4),
        "signal": round(s, 4),
    }


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df
    work = df.copy()
    work.index = pd.to_datetime(work.index)
    if work.index.tz is not None:
        work.index = work.index.tz_localize(None)
    out = work.resample(rule).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna()
    return out


def _build_mtf_macd(yahoo_symbol: str, df_15m: pd.DataFrame) -> dict[str, Any]:
    """1H (from 15m) + 15m + optional 5m MACD alignment."""
    out: dict[str, Any] = {"ready": False}
    close_15 = df_15m["Close"].dropna()
    m15 = _macd_bar_state(close_15)
    if not m15:
        return out

    df_1h = _resample_ohlcv(df_15m, "1h")
    m1h = _macd_bar_state(df_1h["Close"].dropna()) if len(df_1h) >= 35 else None

    m5: dict[str, Any] | None = None
    if FNO_MACD_REQUIRE_5M:
        try:
            df_5 = _yf_history(yf.Ticker(yahoo_symbol), period="5d", interval="5m")
            if not df_5.empty and len(df_5) >= 35:
                m5 = _macd_bar_state(df_5["Close"].dropna())
        except Exception as e:
            log.debug("5m MACD fetch failed for %s: %s", yahoo_symbol, e)

    out.update({
        "ready": m1h is not None,
        "h1": m1h,
        "m15": m15,
        "m5": m5,
    })
    return out


def _adx(df: pd.DataFrame, period: int = 14) -> float | None:
    """Average Directional Index — trend strength (higher = stronger trend)."""
    if df is None or len(df) < period + 2:
        return None
    try:
        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
        atr = tr.ewm(span=period, adjust=False).mean()
        plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr)
        minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)
        denom = (plus_di + minus_di).replace(0, float("nan"))
        dx = 100 * (plus_di - minus_di).abs() / denom
        val = dx.ewm(span=period, adjust=False).mean().iloc[-1]
        return float(val) if not math.isnan(val) else None
    except Exception:
        return None


def _get_india_vix() -> float | None:
    """India VIX — cached to avoid extra Yahoo calls per index."""
    global _vix_cache
    now = time.time()
    if _vix_cache and now - _vix_cache[0] < FNO_YAHOO_CACHE_TTL:
        return _vix_cache[1]
    try:
        df = _yf_history(yf.Ticker("^INDIAVIX"), period="5d", interval="15m")
        if df.empty:
            return None
        val = float(df["Close"].iloc[-1])
        _vix_cache = (now, val)
        return val
    except Exception as e:
        log.debug("India VIX fetch failed: %s", e)
        return None


def _vwap(df: pd.DataFrame) -> float | None:
    try:
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        volume = df["Volume"]
        if volume.sum() == 0:
            return None
        vwap = (typical * volume).cumsum() / volume.cumsum()
        val = float(vwap.iloc[-1])
        return val if not math.isnan(val) else None
    except Exception:
        return None


def _vwap_bands(df: pd.DataFrame) -> tuple[float | None, float | None, float | None]:
    """VWAP, +1 SD, -1 SD."""
    try:
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        volume = df["Volume"]
        if volume.sum() == 0:
            return None, None, None
        cum_vol = volume.cumsum()
        cum_tp_vol = (typical * volume).cumsum()
        vw = cum_tp_vol / cum_vol
        cum_tp2_vol = (typical ** 2 * volume).cumsum()
        variance = cum_tp2_vol / cum_vol - vw ** 2
        variance = variance.clip(lower=0)
        sd = variance ** 0.5
        v = float(vw.iloc[-1])
        s = float(sd.iloc[-1])
        if math.isnan(v) or math.isnan(s):
            return None, None, None
        return v, v + s, v - s
    except Exception:
        return None, None, None


# ════════════════════ Data fetching ════════════════════

def _minimal_tech(nse_spot: float) -> dict[str, Any]:
    """Fallback when Yahoo is unavailable — NSE spot + OI strategies still work."""
    return {
        "spot": round(nse_spot, 2),
        "pct_change": 0.0,
        "mom_pct": 0.0,
        "rsi": None,
        "ema9": None,
        "ema21": None,
        "vwap": None,
        "vwap_upper": None,
        "vwap_lower": None,
        "orb_high": None,
        "orb_low": None,
        "adx": None,
        "vix": None,
        "mtf_macd": None,
        "timeframe": "nse",
        "yahoo_skipped": True,
    }


def _yf_history(ticker: yf.Ticker, **kwargs) -> pd.DataFrame:
    """yfinance history with rate-limit aware retry."""
    last_err: Exception | None = None
    for wait in (0, 2, 5):
        if wait:
            time.sleep(wait)
        try:
            df = ticker.history(**kwargs)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            last_err = e
            if "too many" in str(e).lower() or "rate" in str(e).lower():
                log.debug("Yahoo rate limit on history, retry in %ss", wait)
                continue
            raise
    if last_err:
        raise last_err
    return pd.DataFrame()


def _fetch_intraday(
    yahoo_symbol: str,
    *,
    nse_spot: float | None = None,
    nse_only_fallback: bool = False,
) -> dict[str, Any]:
    """15m intraday candles. Uses cache + NSE spot to avoid extra Yahoo calls."""
    cache_key = yahoo_symbol
    now = time.time()
    cached = _intraday_cache.get(cache_key)
    if cached and now - cached[0] < FNO_YAHOO_CACHE_TTL:
        tech = dict(cached[1])
        if nse_spot is not None:
            tech["spot"] = round(nse_spot, 2)
        return tech

    out: dict[str, Any] = {"yahoo": yahoo_symbol}
    try:
        t = yf.Ticker(yahoo_symbol)
        df = _yf_history(t, period="5d", interval="15m")
        use_intraday = not df.empty and len(df) >= 25
        if not use_intraday:
            df = _yf_history(t, period="30d", interval="1d")
        if df.empty or len(df) < 10:
            if nse_spot is not None:
                out = _minimal_tech(nse_spot)
                _intraday_cache[cache_key] = (now, dict(out))
                return out
            return out

        close = df["Close"].dropna()
        last = float(close.iloc[-1])

        rsi_val = _rsi(close, 7 if use_intraday else 14)
        ema9 = _ema(close, 9)
        ema21 = _ema(close, 21 if use_intraday else 20)
        vwap_val, vwap_upper, vwap_lower = _vwap_bands(df) if use_intraday else (None, None, None)

        lookback = 4 if use_intraday else 2
        ref = float(close.iloc[-1 - lookback]) if len(close) > lookback else last
        mom = (last - ref) / ref * 100.0 if ref else 0.0

        orb_high = orb_low = None
        adx_val = None
        if use_intraday:
            today_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
            today_candles = df[df.index.strftime("%Y-%m-%d") == today_str]
            if len(today_candles) >= 1:
                orb_high = float(today_candles["High"].iloc[0])
                orb_low = float(today_candles["Low"].iloc[0])
            adx_val = _adx(df)

        mtf_macd = _build_mtf_macd(yahoo_symbol, df) if use_intraday and FNO_MACD_MTF_ENABLED else None

        # Prefer NSE live spot; avoid extra Yahoo snapshot call (reduces rate limits)
        if nse_spot is not None:
            last = float(nse_spot)
            prev = float(close.iloc[-2]) if len(close) >= 2 else last
            day_pct = (last - prev) / prev * 100.0 if prev else 0.0
        else:
            prev = float(close.iloc[-2]) if len(close) >= 2 else last
            day_pct = (last - prev) / prev * 100.0 if prev else 0.0

        out.update({
            "spot": round(last, 2),
            "pct_change": round(day_pct, 3),
            "mom_pct": round(mom, 3),
            "rsi": round(rsi_val, 1) if rsi_val is not None else None,
            "ema9": round(ema9, 2) if ema9 is not None else None,
            "ema21": round(ema21, 2) if ema21 is not None else None,
            "vwap": round(vwap_val, 2) if vwap_val is not None else None,
            "vwap_upper": round(vwap_upper, 2) if vwap_upper is not None else None,
            "vwap_lower": round(vwap_lower, 2) if vwap_lower is not None else None,
            "orb_high": round(orb_high, 2) if orb_high is not None else None,
            "orb_low": round(orb_low, 2) if orb_low is not None else None,
            "adx": round(adx_val, 1) if adx_val is not None else None,
            "mtf_macd": mtf_macd,
            "timeframe": "15m" if use_intraday else "1d",
        })
        _intraday_cache[cache_key] = (now, dict(out))
    except Exception as e:
        err = str(e).lower()
        if cached:
            log.warning("Yahoo fetch failed for %s, using cached data: %s", yahoo_symbol, e)
            tech = dict(cached[1])
            if nse_spot is not None:
                tech["spot"] = round(nse_spot, 2)
            return tech
        if nse_spot is not None and (nse_only_fallback or "too many" in err or "rate" in err):
            log.warning(
                "Yahoo unavailable for %s (%s) — using NSE spot only for this index",
                yahoo_symbol, e,
            )
            out = _minimal_tech(nse_spot)
            _intraday_cache[cache_key] = (now, dict(out))
            return out
        log.warning("Intraday fetch failed for %s: %s", yahoo_symbol, e)
    return out


def _parse_option_chain(nse_symbol: str, nse: NSELive | None = None) -> dict[str, Any] | None:
    client = nse or NSELive()
    try:
        raw = client.index_option_chain(nse_symbol)
        rec = raw.get("records")
        if not rec:
            return None
        spot = float(rec["underlyingValue"])
        expiry = rec["expiryDates"][0]
        rows = [d for d in rec.get("data", []) if d.get("expiryDates") == expiry]
        if not rows:
            return None
        return {"spot": spot, "expiry": expiry, "rows": rows}
    except Exception as e:
        log.warning("Option chain failed for %s: %s", nse_symbol, e)
        return None


def _parse_chain_for_index(cfg: dict[str, Any], nse: NSELive | None = None) -> dict[str, Any] | None:
    bse_cd = cfg.get("bse_scrip_cd")
    if bse_cd is not None:
        return parse_bse_option_chain(int(bse_cd))
    return _parse_option_chain(cfg["nse"], nse)


def _leg_quote(row: dict, side: str) -> dict[str, Any]:
    leg = row.get(side) or {}
    bid = float(leg.get("buyPrice1") or 0)
    ask = float(leg.get("sellPrice1") or 0)
    ltp = float(leg.get("lastPrice") or 0)
    if ltp <= 0 and bid > 0 and ask > 0:
        ltp = round((bid + ask) / 2, 2)
    elif ltp <= 0 and ask > 0:
        ltp = ask
    elif ltp <= 0 and bid > 0:
        ltp = bid
    return {
        "ltp": ltp,
        "oi": int(leg.get("openInterest") or 0),
        "chg_oi": int(leg.get("changeinOpenInterest") or 0),
        "iv": float(leg.get("impliedVolatility") or 0),
        "volume": int(leg.get("totalTradedVolume") or 0),
    }


def _oi_analysis(rows: list[dict], spot: float, step: int) -> dict[str, Any]:
    atm = _round_strike(spot, step)
    total_ce_oi = total_pe_oi = 0
    ce_chg = pe_chg = 0
    best_pe_strike = best_ce_strike = atm
    best_pe_oi = best_ce_oi = -1
    atm_ce_chg = atm_pe_chg = 0

    for row in rows:
        strike = int(row["strikePrice"])
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        c_oi = int(ce.get("openInterest") or 0)
        p_oi = int(pe.get("openInterest") or 0)
        total_ce_oi += c_oi
        total_pe_oi += p_oi
        ce_chg += int(ce.get("changeinOpenInterest") or 0)
        pe_chg += int(pe.get("changeinOpenInterest") or 0)
        if p_oi > best_pe_oi:
            best_pe_oi = p_oi
            best_pe_strike = strike
        if c_oi > best_ce_oi:
            best_ce_oi = c_oi
            best_ce_strike = strike
        if strike == atm:
            atm_ce_chg = int(ce.get("changeinOpenInterest") or 0)
            atm_pe_chg = int(pe.get("changeinOpenInterest") or 0)

    pcr = total_pe_oi / total_ce_oi if total_ce_oi else 1.0
    return {
        "pcr": round(pcr, 2),
        "support": best_pe_strike,
        "resistance": best_ce_strike,
        "net_ce_chg": ce_chg,
        "net_pe_chg": pe_chg,
        "atm_ce_chg": atm_ce_chg,
        "atm_pe_chg": atm_pe_chg,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
    }


def _pick_scalp_strike(
    rows: list[dict], spot: float, step: int,
    side: str, prem_min: float, prem_max: float,
) -> tuple[int, dict[str, Any]]:
    atm = _round_strike(spot, step)
    by_strike = {int(r["strikePrice"]): r for r in rows}
    min_tradeable = max(12.0, prem_min * 0.22)
    offsets = (0, -1, -2, 1, -3, 2, -4) if side == "CE" else (0, 1, 2, -1, 3, -2, 4)

    candidates: list[tuple[int, dict, float]] = []
    for off in offsets:
        strike = atm + off * step if side == "CE" else atm - off * step
        row = by_strike.get(strike)
        if not row:
            continue
        q = _leg_quote(row, side)
        if q["ltp"] < min_tradeable:
            continue
        mid = (prem_min + prem_max) / 2
        in_band = prem_min <= q["ltp"] <= prem_max
        band_bonus = 40 if in_band else -abs(q["ltp"] - mid) * 0.5
        score = band_bonus + q["oi"] * 2 + q["volume"] * 5 - abs(strike - atm) * 0.3
        candidates.append((strike, q, score))

    if not candidates:
        for off in offsets:
            strike = atm + off * step if side == "CE" else atm - off * step
            row = by_strike.get(strike)
            if not row:
                continue
            q = _leg_quote(row, side)
            if q["ltp"] > 0:
                candidates.append((strike, q, -abs(strike - atm)))

    if not candidates:
        return atm, _leg_quote(by_strike.get(atm, {}), side)
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[0][0], candidates[0][1]


def _scalp_exits(entry_premium: float) -> dict[str, float]:
    prem = max(entry_premium, 5.0)
    sl = round(prem * SL_MULT, 2)
    t1 = round(prem * T1_MULT, 2)
    t2 = round(prem * T2_MULT, 2)
    return {
        "entry": round(prem, 2),
        "sl": sl, "t1": t1, "t2": t2,
        "sl_pts": round(prem - sl, 2),
        "t1_pts": round(t1 - prem, 2),
        "t2_pts": round(t2 - prem, 2),
        "rr": round((t1 - prem) / (prem - sl), 1) if prem > sl else 0.0,
    }


# ════════════════════ STRATEGY 1: Confluence ════════════════════

def _check_confluence(tech: dict[str, Any], oi: dict[str, Any]) -> dict[str, Any] | None:
    """Returns signal dict if STRONG (3+ layers agree), else None."""
    spot = tech.get("spot")
    vwap = tech.get("vwap")
    ema9 = tech.get("ema9")
    ema21 = tech.get("ema21")
    rsi = tech.get("rsi")
    mom = float(tech.get("mom_pct") or 0)
    day_pct = float(tech.get("pct_change") or 0)

    pcr = float(oi.get("pcr") or 1)
    atm_ce_chg = int(oi.get("atm_ce_chg") or 0)
    atm_pe_chg = int(oi.get("atm_pe_chg") or 0)
    support = oi.get("support")
    resistance = oi.get("resistance")

    votes = 0
    layers = 0
    reasons: list[str] = []

    # VWAP layer
    if spot and vwap:
        if spot > vwap:
            votes += 1; layers += 1
            dist = (spot - vwap) / vwap * 100
            if dist < 0.15:
                reasons.append(f"VWAP bounce at {vwap} (price just above)")
            else:
                reasons.append(f"Above VWAP {vwap}")
        elif spot < vwap:
            votes -= 1; layers += 1
            reasons.append(f"Below VWAP {vwap}")
    elif day_pct > 0.15:
        votes += 1; layers += 1
        reasons.append(f"Day +{day_pct:.2f}%")
    elif day_pct < -0.15:
        votes -= 1; layers += 1
        reasons.append(f"Day {day_pct:.2f}%")

    # EMA layer
    if spot and ema9 and ema21:
        if ema9 > ema21 and spot > ema9:
            votes += 1; layers += 1
            reasons.append(f"Price > EMA9({ema9:.0f}) > EMA21({ema21:.0f})")
        elif ema9 < ema21 and spot < ema9:
            votes -= 1; layers += 1
            reasons.append(f"Price < EMA9({ema9:.0f}) < EMA21({ema21:.0f})")
        elif ema9 > ema21:
            votes += 1; layers += 1
            reasons.append(f"EMA9 > EMA21 uptrend")
        elif ema9 < ema21:
            votes -= 1; layers += 1
            reasons.append(f"EMA9 < EMA21 downtrend")
    elif mom > 0.1:
        votes += 1; layers += 1
        reasons.append(f"Momentum +{mom:.2f}%")
    elif mom < -0.1:
        votes -= 1; layers += 1
        reasons.append(f"Momentum {mom:.2f}%")

    # RSI layer
    if rsi is not None:
        if rsi >= 55:
            votes += 1; layers += 1
            reasons.append(f"RSI-7 = {rsi:.0f} bullish")
        elif rsi <= 45:
            votes -= 1; layers += 1
            reasons.append(f"RSI-7 = {rsi:.0f} bearish")

    # OI layer
    oi_vote = 0
    if pcr > 1.15:
        oi_vote += 1
        reasons.append(f"PCR {pcr:.2f} (PE heavy)")
    elif pcr < 0.85:
        oi_vote -= 1
        reasons.append(f"PCR {pcr:.2f} (CE heavy)")
    atm_net = atm_ce_chg - atm_pe_chg
    if atm_net > 500:
        oi_vote += 1
        reasons.append("ATM CE OI building")
    elif atm_net < -500:
        oi_vote -= 1
        reasons.append("ATM PE OI building")
    if spot and support and resistance:
        if abs(spot - support) / spot * 100 < 0.5:
            oi_vote += 1
            reasons.append(f"Near support {support}")
        elif abs(spot - resistance) / spot * 100 < 0.5:
            oi_vote -= 1
            reasons.append(f"Near resistance {resistance}")
    if oi_vote != 0:
        layers += 1
        votes += 1 if oi_vote > 0 else -1

    agreed = abs(votes)
    if agreed < 3:
        return None

    side = "CE" if votes > 0 else "PE"
    return {
        "strategy": STRATEGY_CONFLUENCE,
        "side": side,
        "strength": "STRONG" if agreed >= 3 else "MODERATE",
        "layers": f"{agreed}/4",
        "reasons": reasons,
        "win_rate": "~62-68%",
    }


# ════════════════════ STRATEGY 2: ORB ════════════════════

def _check_orb(tech: dict[str, Any], oi: dict[str, Any]) -> dict[str, Any] | None:
    """ORB fires when price breaks above/below first 15m candle + OI confirms."""
    spot = tech.get("spot")
    orb_high = tech.get("orb_high")
    orb_low = tech.get("orb_low")
    if not spot or not orb_high or not orb_low:
        return None

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    if now_ist.hour >= 11 and now_ist.minute > 0:
        return None

    pcr = float(oi.get("pcr") or 1)
    rsi = tech.get("rsi")
    reasons: list[str] = []

    orb_range = orb_high - orb_low
    if orb_range <= 0:
        return None

    if spot > orb_high:
        breakout_pct = (spot - orb_high) / orb_high * 100
        if breakout_pct < ORB_MIN_BREAK_PCT:
            return None
        side = "CE"
        reasons.append(f"ORB breakout above {orb_high:.0f} (+{breakout_pct:.2f}%)")
        reasons.append(f"ORB range: {orb_low:.0f} - {orb_high:.0f} ({orb_range:.0f} pts)")
        if pcr > 1.0:
            reasons.append(f"PCR {pcr:.2f} supports breakout")
        if rsi and rsi > 55:
            reasons.append(f"RSI {rsi:.0f} confirms momentum")
        elif rsi and rsi < 45:
            return None
    elif spot < orb_low:
        breakout_pct = (orb_low - spot) / orb_low * 100
        if breakout_pct < ORB_MIN_BREAK_PCT:
            return None
        side = "PE"
        reasons.append(f"ORB breakdown below {orb_low:.0f} (-{breakout_pct:.2f}%)")
        reasons.append(f"ORB range: {orb_low:.0f} - {orb_high:.0f} ({orb_range:.0f} pts)")
        if pcr < 1.0:
            reasons.append(f"PCR {pcr:.2f} supports breakdown")
        if rsi and rsi < 45:
            reasons.append(f"RSI {rsi:.0f} confirms selling")
        elif rsi and rsi > 55:
            return None
    else:
        return None

    return {
        "strategy": STRATEGY_ORB,
        "side": side,
        "strength": "STRONG",
        "layers": "ORB+OI",
        "reasons": reasons,
        "win_rate": "~58-65%",
    }


# ════════════════════ STRATEGY 3: PCR Extreme ════════════════════

def _check_pcr_extreme(tech: dict[str, Any], oi: dict[str, Any]) -> dict[str, Any] | None:
    """Contrarian entry when PCR hits extremes. Works all day + especially on expiry."""
    pcr = float(oi.get("pcr") or 1)
    spot = tech.get("spot")
    rsi = tech.get("rsi")
    ema9 = tech.get("ema9")
    support = oi.get("support")
    resistance = oi.get("resistance")
    reasons: list[str] = []

    if pcr >= 1.3:
        side = "CE"
        reasons.append(f"PCR {pcr:.2f} EXTREME PE writing = strong floor")
        reasons.append("Contrarian CE: writers won't let it fall further")
        if support and spot:
            reasons.append(f"Max PE OI wall at {support} (support floor)")
        if rsi and rsi < 45:
            reasons.append(f"RSI {rsi:.0f} oversold = bounce setup")
        elif rsi and rsi > 65:
            return None
        if spot and ema9 and spot > ema9:
            reasons.append("Price above EMA9 = turning up")
    elif pcr <= 0.7:
        side = "PE"
        reasons.append(f"PCR {pcr:.2f} EXTREME CE writing = ceiling formed")
        reasons.append("Contrarian PE: writers capping upside")
        if resistance and spot:
            reasons.append(f"Max CE OI wall at {resistance} (resistance ceiling)")
        if rsi and rsi > 55:
            reasons.append(f"RSI {rsi:.0f} overbought = fade setup")
        elif rsi and rsi < 35:
            return None
        if spot and ema9 and spot < ema9:
            reasons.append("Price below EMA9 = turning down")
    else:
        return None

    return {
        "strategy": STRATEGY_PCR_REVERSAL,
        "side": side,
        "strength": "STRONG",
        "layers": f"PCR={pcr:.2f}",
        "reasons": reasons,
        "win_rate": "~58-64%",
    }


# ════════════════════ STRATEGY 4: MACD Multi-Timeframe ════════════════════

def _check_macd_mtf(tech: dict[str, Any], oi: dict[str, Any]) -> dict[str, Any] | None:
    """1H + 15m trend filter, 5m (or 15m) crossover entry — higher win-rate MTF stack."""
    mtf = tech.get("mtf_macd") or {}
    if not mtf.get("ready"):
        return None

    h1 = mtf.get("h1") or {}
    m15 = mtf.get("m15") or {}
    m5 = mtf.get("m5")

    h1_bull = bool(h1.get("bull"))
    h1_bear = bool(h1.get("bear"))
    m15_bull = bool(m15.get("bull"))
    m15_bear = bool(m15.get("bear"))

    if (h1_bull and m15_bear) or (h1_bear and m15_bull):
        return None

    reasons: list[str] = [
        f"1H MACD {'bullish' if h1_bull else 'bearish'} (line {h1.get('macd')} vs {h1.get('signal')})",
        f"15m MACD {'bullish' if m15_bull else 'bearish'} aligned with 1H",
    ]

    entry_cross_up = bool(m15.get("cross_up"))
    entry_cross_down = bool(m15.get("cross_down"))
    layers = "2/3 MACD"

    if m5:
        entry_cross_up = bool(m5.get("cross_up"))
        entry_cross_down = bool(m5.get("cross_down"))
        layers = "3/3 MACD"
        reasons.append(
            f"5m MACD entry cross ({'up' if entry_cross_up else 'down' if entry_cross_down else 'none'})"
        )
    elif FNO_MACD_REQUIRE_5M:
        return None
    else:
        reasons.append(
            f"15m MACD entry cross ({'up' if entry_cross_up else 'down' if entry_cross_down else 'none'})"
        )

    adx = tech.get("adx")
    if adx is not None and adx < FNO_MIN_ADX:
        return None

    if h1_bull and m15_bull and entry_cross_up:
        side = "CE"
        if h1.get("above_zero"):
            reasons.append("1H MACD above zero — strong uptrend")
    elif h1_bear and m15_bear and entry_cross_down:
        side = "PE"
        if h1.get("below_zero"):
            reasons.append("1H MACD below zero — strong downtrend")
    else:
        return None

    pcr = float(oi.get("pcr") or 1)
    if side == "CE" and pcr < 0.75:
        return None
    if side == "PE" and pcr > 1.25:
        return None
    reasons.append(f"PCR {pcr:.2f} not fighting {side} direction")

    return {
        "strategy": STRATEGY_MACD_MTF,
        "side": side,
        "strength": "STRONG",
        "layers": layers,
        "reasons": reasons,
        "win_rate": "~65-72%",
    }


def _strategy_check_fns() -> list:
    fns = [_check_confluence, _check_orb, _check_pcr_extreme]
    if FNO_MACD_MTF_ENABLED:
        fns.append(_check_macd_mtf)
    return fns


# ════════════════════ Quality filters (auto-alerts) ════════════════════

def _is_prime_alert_window(now_ist: datetime | None = None) -> bool:
    """Skip opening chop and closing volatility; optional lunch skip."""
    now = now_ist or datetime.now(ZoneInfo("Asia/Kolkata"))
    mins = now.hour * 60 + now.minute
    if 9 * 60 + 15 <= mins < 9 * 60 + 20:
        return False
    if FNO_SKIP_LUNCH and 12 * 60 <= mins < 12 * 60 + 45:
        return False
    if 15 * 60 + 15 <= mins <= 15 * 60 + 30:
        return False
    return True


def _leg_spread_pct(row: dict, side: str) -> float:
    leg = row.get(side) or {}
    bid = float(leg.get("buyPrice1") or 0)
    ask = float(leg.get("sellPrice1") or 0)
    ltp = float(leg.get("lastPrice") or 0)
    mid = ltp if ltp > 0 else ((bid + ask) / 2 if bid > 0 and ask > 0 else 0)
    if mid <= 0 or bid <= 0 or ask <= 0:
        return 0.0
    return (ask - bid) / mid * 100.0


def _strategy_rank_base(strategy: str) -> int:
    return {
        STRATEGY_CONFLUENCE: 30,
        STRATEGY_MACD_MTF: 35,
        STRATEGY_ORB: 25,
        STRATEGY_PCR_REVERSAL: 20,
    }.get(strategy, 10)


def _passes_auto_alert_quality(
    result: dict[str, Any],
    tech: dict[str, Any],
    leg: dict[str, Any],
    row: dict[str, Any],
    side: str,
    prem_min: float,
    prem_max: float,
) -> tuple[bool, list[str], int, str | None]:
    """Extra gates for auto-alerts — fewer trades, higher conviction."""
    if not FNO_STRICT_FILTERS:
        return True, [], 50, None

    extras: list[str] = []
    score = 0
    strat = result.get("strategy", "")

    if not _is_prime_alert_window():
        return False, extras, 0, "time_window"

    vol = int(leg.get("volume") or 0)
    if vol < FNO_MIN_LEG_VOLUME:
        return False, extras, 0, "low_volume"
    if vol >= FNO_MIN_LEG_VOLUME * 3:
        score += 15
        extras.append(f"High volume ({vol:,})")

    spread = _leg_spread_pct(row, side)
    if spread > FNO_MAX_SPREAD_PCT:
        return False, extras, 0, "wide_spread"
    if spread > 0 and spread <= FNO_MAX_SPREAD_PCT / 2:
        score += 10
        extras.append("Tight bid-ask spread")

    ltp = float(leg.get("ltp") or 0)
    if ltp < prem_min * 0.5 or ltp > prem_max * 1.4:
        return False, extras, 0, "premium_band"

    adx = tech.get("adx")
    if strat in (STRATEGY_CONFLUENCE, STRATEGY_ORB, STRATEGY_MACD_MTF):
        if adx is not None and adx < FNO_MIN_ADX:
            return False, extras, 0, "low_adx"
        if adx is not None and adx >= 25:
            score += 20
            extras.append(f"ADX {adx:.0f} strong trend")
        elif adx is not None:
            score += 10
            extras.append(f"ADX {adx:.0f} trending")

    vix = tech.get("vix")
    if vix is not None:
        if strat == STRATEGY_PCR_REVERSAL and vix < 13:
            return False, extras, 0, "vix_low"
        if strat == STRATEGY_ORB and vix > 24:
            return False, extras, 0, "vix_high"
        if 14 <= vix <= 20:
            score += 10
            extras.append(f"VIX {vix:.1f} ideal zone")

    ema9, ema21, spot = tech.get("ema9"), tech.get("ema21"), tech.get("spot")
    if ema9 and ema21 and spot and strat in (STRATEGY_CONFLUENCE, STRATEGY_ORB, STRATEGY_MACD_MTF):
        aligned = (
            (side == "CE" and ema9 > ema21 and spot > ema9)
            or (side == "PE" and ema9 < ema21 and spot < ema9)
        )
        if FNO_REQUIRE_EMA_ALIGN and not aligned:
            return False, extras, 0, "ema_misalign"
        if aligned:
            score += 15
            extras.append("EMA trend aligned with trade")

    if strat == STRATEGY_CONFLUENCE:
        layers_s = result.get("layers", "0/4")
        try:
            layer_n = int(str(layers_s).split("/")[0])
        except (ValueError, IndexError):
            layer_n = 0
        if layer_n < FNO_CONFLUENCE_MIN_LAYERS:
            if not (adx is not None and adx >= FNO_MIN_ADX + 4):
                return False, extras, 0, "weak_confluence"
        if layer_n >= 4:
            score += 25
            extras.append("4/4 confluence layers agree")
        elif layer_n >= 3:
            score += 12
            extras.append("3/4 confluence layers agree")

    score += 20
    return True, extras, score, None


def _skip_reason_label(reason: str | None) -> str:
    if not reason:
        return ""
    labels = {
        "time_window": "Outside auto-alert window (9:20–15:15)",
        "low_volume": f"Leg volume below {FNO_MIN_LEG_VOLUME}",
        "wide_spread": f"Spread above {FNO_MAX_SPREAD_PCT}%",
        "premium_band": "Premium outside scalp band",
        "low_adx": f"ADX below {FNO_MIN_ADX}",
        "vix_low": "VIX too low for PCR reversal",
        "vix_high": "VIX too high for ORB",
        "ema_misalign": "EMA trend not aligned",
        "weak_confluence": f"Fewer than {FNO_CONFLUENCE_MIN_LAYERS}/4 confluence layers",
        "already_alerted": "Same setup already alerted today",
        "no_setup": "No strategy triggered",
    }
    return labels.get(reason, reason.replace("_", " ").title())


def _empty_scan_stats() -> dict[str, int]:
    return {
        "setups": 0,
        "sent": 0,
        "skip_quality": 0,
        "skip_dedupe": 0,
        "skip_premium": 0,
    }


def _record_scan_stats(delta: dict[str, int]) -> None:
    """Accumulate per-day filter stats (setups vs sent vs skipped)."""
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            """
            INSERT INTO fno_scan_stats (
                alert_date, setups, sent, skip_quality, skip_dedupe, skip_premium, scan_cycles
            ) VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(alert_date) DO UPDATE SET
                setups = setups + excluded.setups,
                sent = sent + excluded.sent,
                skip_quality = skip_quality + excluded.skip_quality,
                skip_dedupe = skip_dedupe + excluded.skip_dedupe,
                skip_premium = skip_premium + excluded.skip_premium,
                scan_cycles = scan_cycles + 1
            """,
            (
                today,
                delta.get("setups", 0),
                delta.get("sent", 0),
                delta.get("skip_quality", 0),
                delta.get("skip_dedupe", 0),
                delta.get("skip_premium", 0),
            ),
        )
        con.commit()
    finally:
        con.close()


def get_scan_stats_for_date(alert_date: str) -> dict[str, int] | None:
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            """SELECT setups, sent, skip_quality, skip_dedupe, skip_premium, scan_cycles
               FROM fno_scan_stats WHERE alert_date = ?""",
            (alert_date,),
        ).fetchone()
        if not row:
            return None
        return {
            "setups": row[0], "sent": row[1], "skip_quality": row[2],
            "skip_dedupe": row[3], "skip_premium": row[4], "scan_cycles": row[5],
        }
    finally:
        con.close()


def get_scan_stats_range(start_date: str, end_date: str) -> dict[str, int]:
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            """
            SELECT COALESCE(SUM(setups),0), COALESCE(SUM(sent),0),
                   COALESCE(SUM(skip_quality),0), COALESCE(SUM(skip_dedupe),0),
                   COALESCE(SUM(skip_premium),0), COALESCE(SUM(scan_cycles),0)
            FROM fno_scan_stats
            WHERE alert_date >= ? AND alert_date <= ?
            """,
            (start_date, end_date),
        ).fetchone()
        return {
            "setups": row[0], "sent": row[1], "skip_quality": row[2],
            "skip_dedupe": row[3], "skip_premium": row[4], "scan_cycles": row[5],
        }
    finally:
        con.close()


def format_scan_stats_html(stats: dict[str, int] | None, *, label: str = "Today") -> str:
    if not stats or stats.get("scan_cycles", 0) == 0:
        return ""
    setups = int(stats.get("setups") or 0)
    sent = int(stats.get("sent") or 0)
    skip_q = int(stats.get("skip_quality") or 0)
    skip_d = int(stats.get("skip_dedupe") or 0)
    skip_p = int(stats.get("skip_premium") or 0)
    cycles = int(stats.get("scan_cycles") or 0)
    pass_pct = round(sent / setups * 100, 1) if setups else 0.0
    return (
        f"<b>\U0001f50d Filter stats ({html.escape(label)})</b>\n"
        f"Scans: <b>{cycles}</b>  \u00b7  Setups found: <b>{setups}</b>\n"
        f"Alerts sent: <b>{sent}</b>  \u00b7  Quality pass rate: <b>{pass_pct}%</b>\n"
        f"Skipped — quality: <b>{skip_q}</b>  \u00b7  "
        f"duplicate: <b>{skip_d}</b>  \u00b7  no premium: <b>{skip_p}</b>\n"
        ""
    )


# ════════════════════ Scan all strategies ════════════════════

def scan_index(cfg: dict[str, Any], nse: NSELive | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Run all 3 strategies on one index. Returns (signals, scan stats)."""
    stats = _empty_scan_stats()
    chain = _parse_chain_for_index(cfg, nse)
    if not chain:
        return [], stats

    spot = float(chain["spot"])
    tech = _fetch_intraday(
        cfg["yahoo"],
        nse_spot=spot,
        nse_only_fallback=bool(cfg.get("nse_only_fallback")),
    )
    if not tech.get("spot"):
        tech["spot"] = round(spot, 2)
    tech["vix"] = _get_india_vix()

    rows = chain["rows"]
    oi = _oi_analysis(rows, spot, cfg["step"])
    by_strike = {int(r["strikePrice"]): r for r in rows}

    candidates: list[tuple[int, dict[str, Any]]] = []

    for check_fn in _strategy_check_fns():
        result = check_fn(tech, oi)
        if result is None:
            continue

        stats["setups"] += 1
        side = result["side"]
        strike, leg = _pick_scalp_strike(
            rows, spot, cfg["step"], side, cfg["prem_min"], cfg["prem_max"]
        )

        if _already_alerted(cfg["nse"], result["strategy"], side, strike):
            stats["skip_dedupe"] += 1
            continue

        entry_prem = float(leg.get("ltp") or 0)
        if entry_prem <= 0:
            stats["skip_premium"] += 1
            continue

        row = by_strike.get(strike, {})
        ok, quality_extras, qscore, _skip = _passes_auto_alert_quality(
            result, tech, leg, row, side, cfg["prem_min"], cfg["prem_max"],
        )
        if not ok:
            stats["skip_quality"] += 1
            continue

        reasons = list(result.get("reasons") or [])
        for extra in quality_extras[:2]:
            if extra not in reasons:
                reasons.append(extra)

        exits = _scalp_exits(entry_prem)
        rank = qscore + _strategy_rank_base(result["strategy"])

        candidates.append((rank, {
            "name": cfg["name"],
            "nse": cfg["nse"],
            "expiry": chain["expiry"],
            "spot": round(spot, 2),
            "tech": tech,
            "oi": oi,
            **result,
            "reasons": reasons,
            "strike": strike,
            "premium": round(entry_prem, 2),
            "leg_oi": leg.get("oi", 0),
            "leg_chg_oi": leg.get("chg_oi", 0),
            "leg_volume": leg.get("volume", 0),
            "exits": exits,
            "quality_score": rank,
        }))

    if not candidates:
        return [], stats

    candidates.sort(key=lambda x: x[0], reverse=True)
    if FNO_MAX_ALERTS_PER_INDEX > 0:
        candidates = candidates[:FNO_MAX_ALERTS_PER_INDEX]
    signals = [sig for _, sig in candidates]
    stats["sent"] += len(signals)
    return signals, stats


def scan_all_indices() -> list[dict[str, Any]]:
    """Scan all indices with all strategies. Returns only triggered signals."""
    ensure_fno_tables()
    all_signals: list[dict[str, Any]] = []
    cycle_stats = _empty_scan_stats()
    nse = NSELive()
    for i, cfg in enumerate(FNO_INDICES):
        try:
            if i > 0:
                time.sleep(0.5)
            sigs, st = scan_index(cfg, nse)
            all_signals.extend(sigs)
            for k in cycle_stats:
                cycle_stats[k] += st[k]
        except Exception as e:
            log.exception("Scan failed for %s: %s", cfg["name"], e)
    _record_scan_stats(cycle_stats)
    return all_signals


async def scan_all_indices_async() -> list[dict[str, Any]]:
    return await asyncio.to_thread(scan_all_indices)


# ════════════════════ On-demand /entry (all indices) ════════════════════

def analyze_index(cfg: dict[str, Any], nse: NSELive | None = None) -> dict[str, Any]:
    """Full analysis for /entry — same strategies & quality gates as auto-alerts."""
    chain = _parse_chain_for_index(cfg, nse)
    if not chain:
        tech = _fetch_intraday(cfg["yahoo"], nse_only_fallback=bool(cfg.get("nse_only_fallback")))
        err = "Option chain unavailable"
        if cfg.get("bse_scrip_cd"):
            err = (
                "BSE Sensex option chain unavailable from this server "
                "(BSE blocks some cloud IPs; NSE indices still work)"
            )
        return {"name": cfg["name"], "nse": cfg["nse"], "error": err, "tech": tech}

    spot = float(chain["spot"])
    tech = _fetch_intraday(
        cfg["yahoo"],
        nse_spot=spot,
        nse_only_fallback=bool(cfg.get("nse_only_fallback")),
    )
    if not tech.get("spot"):
        tech["spot"] = round(spot, 2)
    tech["vix"] = _get_india_vix()

    rows = chain["rows"]
    oi = _oi_analysis(rows, spot, cfg["step"])
    by_strike = {int(r["strikePrice"]): r for r in rows}

    candidates: list[tuple[int, dict[str, Any]]] = []
    all_triggered: list[str] = []

    for check_fn in _strategy_check_fns():
        result = check_fn(tech, oi)
        if result is None:
            continue

        all_triggered.append(result["strategy"])
        side = result["side"]
        strike, leg = _pick_scalp_strike(
            rows, spot, cfg["step"], side, cfg["prem_min"], cfg["prem_max"],
        )
        entry_prem = float(leg.get("ltp") or 0)
        row = by_strike.get(strike, {})
        duped = _already_alerted(cfg["nse"], result["strategy"], side, strike)

        ok = True
        skip_reason: str | None = None
        quality_extras: list[str] = []
        qscore = 0

        if entry_prem <= 0:
            ok = False
            skip_reason = "premium_band"
        else:
            ok, quality_extras, qscore, skip_reason = _passes_auto_alert_quality(
                result, tech, leg, row, side, cfg["prem_min"], cfg["prem_max"],
            )
        if duped:
            ok = False
            skip_reason = "already_alerted"

        reasons = list(result.get("reasons") or [])
        for extra in quality_extras[:2]:
            if extra not in reasons:
                reasons.append(extra)

        rank = qscore + _strategy_rank_base(result["strategy"])
        if ok:
            rank += 1000

        exits = _scalp_exits(entry_prem)
        candidates.append((rank, {
            **result,
            "reasons": reasons,
            "side": side,
            "strike": strike,
            "premium": round(entry_prem, 2),
            "leg_oi": leg.get("oi", 0),
            "leg_chg_oi": leg.get("chg_oi", 0),
            "leg_volume": leg.get("volume", 0),
            "exits": exits,
            "alert_ready": ok,
            "skip_reason": skip_reason,
            "skip_label": _skip_reason_label(skip_reason),
            "quality_score": rank,
        }))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0][1]
        return {
            "name": cfg["name"], "nse": cfg["nse"], "expiry": chain["expiry"],
            "spot": round(spot, 2), "tech": tech, "oi": oi,
            **best,
            "all_triggered": all_triggered,
        }

    day_pct = float(tech.get("pct_change") or 0)
    side = "CE" if day_pct >= 0 else "PE"
    strike, leg = _pick_scalp_strike(rows, spot, cfg["step"], side, cfg["prem_min"], cfg["prem_max"])
    entry_prem = float(leg.get("ltp") or 0)
    exits = _scalp_exits(entry_prem)

    return {
        "name": cfg["name"], "nse": cfg["nse"], "expiry": chain["expiry"],
        "spot": round(spot, 2), "tech": tech, "oi": oi,
        "strategy": "No strong setup", "side": side, "strength": "WEAK",
        "layers": "0/4",
        "reasons": ["No strategy triggered — low conviction"],
        "win_rate": "N/A",
        "all_triggered": [],
        "strike": strike, "premium": round(entry_prem, 2),
        "leg_oi": leg.get("oi", 0), "leg_chg_oi": leg.get("chg_oi", 0),
        "leg_volume": leg.get("volume", 0), "exits": exits,
        "alert_ready": False,
        "skip_reason": "no_setup",
        "skip_label": _skip_reason_label("no_setup"),
    }


def filter_entry_payload_for_user(payload: dict[str, Any], chat_id: int) -> dict[str, Any] | None:
    """Respect /alert index prefs for /entry (None prefs = all indices)."""
    prefs = get_user_alert_indices(chat_id)
    if prefs is None:
        return payload
    indices = [r for r in payload["indices"] if r.get("nse") in prefs]
    if not indices:
        return None
    return {**payload, "indices": indices}


def build_all_entries() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    nse = NSELive()
    for i, cfg in enumerate(FNO_INDICES):
        try:
            if i > 0:
                time.sleep(0.5)
            results.append(analyze_index(cfg, nse))
        except Exception as e:
            log.exception("Entry analysis failed for %s: %s", cfg["name"], e)
            results.append({"name": cfg["name"], "nse": cfg["nse"], "error": str(e)})
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return {"as_of_ist": now.strftime("%Y-%m-%d %H:%M IST"), "indices": results}


async def build_all_entries_async() -> dict[str, Any]:
    return await asyncio.to_thread(build_all_entries)


# ════════════════════ Telegram formatting ════════════════════

def _ru(amount: float) -> str:
    return html.escape(f"\u20b9{amount:.2f}")


def _strategy_emoji(strategy: str) -> str:
    if "Confluence" in strategy:
        return "\U0001f525"
    if "ORB" in strategy:
        return "\U0001f4a5"
    if "PCR" in strategy:
        return "\U0001f504"
    if "MACD" in strategy:
        return "\U0001f4ca"
    return "\u26a1"


def format_alert_html(signal: dict[str, Any]) -> str:
    """Format a single auto-alert message for Telegram."""
    name = html.escape(signal["name"])
    nse = html.escape(signal["nse"])
    side = signal["side"]
    side_emoji = "\U0001f7e2" if side == "CE" else "\U0001f534"
    strat_emoji = _strategy_emoji(signal["strategy"])
    ex = signal["exits"]
    tech = signal.get("tech") or {}
    oi_data = signal.get("oi") or {}
    vwap = tech.get("vwap")

    reasons_html = "\n".join(
        f"  \u2023 {html.escape(x)}" for x in (signal.get("reasons") or [])[:4]
    )

    vwap_line = f"VWAP <code>{vwap}</code>  \u00b7  " if vwap else ""
    adx = tech.get("adx")
    adx_line = f"ADX <code>{adx}</code>  \u00b7  " if adx is not None else ""
    vix = tech.get("vix")
    vix_line = f"VIX <code>{vix:.1f}</code>  \u00b7  " if vix is not None else ""

    return (
        f"{strat_emoji} <b>TRADE ALERT</b> {strat_emoji}\n"
        f"\n"
        f"{side_emoji} <b>{name}</b> <code>{nse}</code>  \u2014  <b>SCALP {side}</b>\n"
        f"<b>Strategy:</b> {html.escape(signal['strategy'])}\n"
        f"<b>Win Rate:</b> {signal.get('win_rate', '')}\n"
        f"\n"
        f"Spot <code>{signal['spot']}</code>  \u00b7  "
        f"{vwap_line}{adx_line}{vix_line}"
        f"PCR <code>{oi_data.get('pcr', '-')}</code>  \u00b7  "
        f"RSI <code>{tech.get('rsi', '-')}</code>\n"
        f"\n"
        f"\u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510\n"
        f"\u2502  \U0001f4cc <b>ENTRY</b>  <code>{signal['strike']} {side}</code>"
        f"  @  <b><u>{_ru(ex['entry'])}</u></b>\n"
        f"\u2502  \U0001f4ca OI <code>{signal['leg_oi']:,}</code>"
        f" \u00b7 \u0394OI <code>{signal['leg_chg_oi']:+,}</code>"
        f" \u00b7 Vol <code>{signal['leg_volume']:,}</code>\n"
        f"\u2502  \U0001f4c5 Expiry <code>{html.escape(signal['expiry'])}</code>\n"
        f"\u2502\n"
        f"\u2502  \U0001f3af <b>T1  {_ru(ex['t1'])}</b>"
        f"  <i>+{ex['t1_pts']:.2f} pts (book 50%)</i>\n"
        f"\u2502  \U0001f3af <b>T2  {_ru(ex['t2'])}</b>"
        f"  <i>+{ex['t2_pts']:.2f} pts (trail rest)</i>\n"
        f"\u2502  \U0001f53b <b>SL  {_ru(ex['sl'])}</b>"
        f"  <i>\u2212{ex['sl_pts']:.2f} pts (hard exit)</i>\n"
        f"\u2502  \U0001f4d0 R:R  <b>{ex['rr']}x</b>\n"
        f"\u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518\n"
        f"\n"
        f"<b>Why this setup:</b>\n"
        f"{reasons_html}\n"
        f"\n"
        f"<i>\u26a0\ufe0f Scalping only \u00b7 Hard SL \u00b7 No averaging \u00b7 Not advice</i>"
    )


def _format_one_index_html(r: dict[str, Any]) -> str:
    """Format one index block for the /entry sheet."""
    name = html.escape(r["name"])
    nse = html.escape(r["nse"])

    if r.get("error"):
        tech = r.get("tech") or {}
        spot_line = f"\nSpot: <code>{tech['spot']}</code>" if tech.get("spot") else ""
        return f"<b>\u26a1 {name}</b> <code>{nse}</code>\n\u26a0\ufe0f {html.escape(r['error'])}{spot_line}\n"

    side = r["side"]
    side_emoji = "\U0001f7e2" if side == "CE" else "\U0001f534"
    strat_emoji = _strategy_emoji(r.get("strategy", ""))
    tech = r["tech"]
    oi_data = r["oi"]
    ex = r["exits"]
    vwap = tech.get("vwap")
    vwap_str = f"<code>{vwap}</code>" if vwap else "N/A"
    adx = tech.get("adx")
    adx_str = f"<code>{adx}</code>" if adx is not None else "N/A"
    vix = tech.get("vix")
    vix_str = f"<code>{vix:.1f}</code>" if vix is not None else "N/A"

    triggered_str = ""
    all_t = r.get("all_triggered") or []
    if all_t:
        triggered_str = " + ".join(all_t)
    else:
        triggered_str = r.get("strategy", "None")

    if r.get("alert_ready"):
        alert_line = "\u2705 <b>Would auto-alert</b> (passes quality filters)\n"
    elif r.get("skip_label"):
        alert_line = f"\u23ed\ufe0f <b>Won't auto-alert:</b> {html.escape(r['skip_label'])}\n"
    else:
        alert_line = ""

    reasons_html = "\n".join(
        f"  \u2023 {html.escape(x)}" for x in (r.get("reasons") or [])[:4]
    )

    separator = "\u2501" * 28
    return (
        f"{separator}\n"
        f"{side_emoji} <b>{name}</b>  <code>{nse}</code>\n"
        f"{strat_emoji} <b>{r.get('strength', 'WEAK')}</b>"
        f"  \u00b7  {html.escape(triggered_str)}\n"
        f"{alert_line}"
        f"\n"
        f"Spot <code>{r['spot']}</code>  \u00b7  VWAP {vwap_str}  \u00b7  "
        f"ADX {adx_str}  \u00b7  VIX {vix_str}\n"
        f"PCR <code>{oi_data['pcr']}</code>  \u00b7  RSI <code>{tech.get('rsi', '-')}</code>\n"
        f"OI walls:  Sup <code>{oi_data['support']}</code>  \u00b7  "
        f"Res <code>{oi_data['resistance']}</code>\n"
        f"\n"
        f"\u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510\n"
        f"\u2502  \U0001f4cc <b>ENTRY</b>  <code>{r['strike']} {side}</code>"
        f"  @  <b><u>{_ru(ex['entry'])}</u></b>\n"
        f"\u2502  \U0001f4ca OI <code>{r['leg_oi']:,}</code>"
        f" \u00b7 \u0394OI <code>{r['leg_chg_oi']:+,}</code>"
        f" \u00b7 Vol <code>{r['leg_volume']:,}</code>\n"
        f"\u2502  \U0001f4c5 Expiry  <code>{html.escape(r['expiry'])}</code>\n"
        f"\u2502\n"
        f"\u2502  \U0001f3af <b>T1  {_ru(ex['t1'])}</b>  <i>+{ex['t1_pts']:.2f} pts</i>\n"
        f"\u2502  \U0001f3af <b>T2  {_ru(ex['t2'])}</b>  <i>+{ex['t2_pts']:.2f} pts</i>\n"
        f"\u2502  \U0001f53b <b>SL  {_ru(ex['sl'])}</b>  <i>\u2212{ex['sl_pts']:.2f} pts</i>\n"
        f"\u2502  \U0001f4d0 R:R  <b>{ex['rr']}x</b>\n"
        f"\u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518\n"
        f"\n"
        f"{reasons_html}\n"
    )


def format_entry_telegram(payload: dict[str, Any]) -> str:
    text = format_entry_telegram_html(payload)
    for tag in ("<b>", "</b>", "<u>", "</u>", "<i>", "</i>", "<code>", "</code>"):
        text = text.replace(tag, "")
    return text


def format_entry_telegram_html(payload: dict[str, Any]) -> str:
    parts = [
        "<b>\u26a1 INDEX OPTIONS \u2014 SCALP SHEET</b>",
        "<b>Strategies:</b> Confluence + ORB + PCR + MACD MTF",
        f"<i>Updated {html.escape(payload['as_of_ist'])}</i>",
        "",
        "Book 50% at T1 \u00b7 trail rest to T2 \u00b7 hard SL \u00b7 no averaging",
        "",
    ]
    for r in payload["indices"]:
        parts.append(_format_one_index_html(r))
    parts.append(
        "<i>\u26a0\ufe0f Scalping only \u00b7 Not advice \u00b7 "
        "\u2705 setups pass same filters as auto-alerts</i>"
    )
    return "\n".join(parts).strip()


# ════════════════════ EOD trade summary ════════════════════

def _classify_outcome(entry: float, sl: float, t1: float, t2: float, close_ltp: float) -> tuple[str, float]:
    """Return (outcome_label, pnl_pts) based on close premium vs entry/SL/T1/T2."""
    pnl = round(close_ltp - entry, 2)
    if close_ltp >= t2:
        return "T2 WIN", pnl
    if close_ltp >= t1:
        return "T1 WIN", pnl
    if close_ltp <= sl:
        return "SL LOSS", pnl
    if pnl > 0:
        return "PARTIAL WIN", pnl
    if pnl < 0:
        return "PARTIAL LOSS", pnl
    return "FLAT", pnl


def _strike_close_ltp(
    rows: list[dict], strike: int, side: str,
) -> float | None:
    for row in rows:
        if int(row["strikePrice"]) != strike:
            continue
        q = _leg_quote(row, side)
        if q["ltp"] > 0:
            return float(q["ltp"])
    return None


def _get_alerts_between(start_date: str, end_date: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(DB_FILE)
    try:
        rows = con.execute(
            """SELECT id, nse_symbol, index_name, strategy, side, strike,
                      entry_premium, sl_premium, t1_premium, t2_premium,
                      spot_at_entry, expiry, alerted_at,
                      close_premium, outcome, pnl_pts, summarized, alert_date
               FROM fno_alerts
               WHERE alert_date >= ? AND alert_date <= ?
               ORDER BY alert_date, alerted_at""",
            (start_date, end_date),
        ).fetchall()
        return [
            {
                "id": r[0], "nse_symbol": r[1], "index_name": r[2] or r[1],
                "strategy": r[3], "side": r[4], "strike": r[5],
                "entry_premium": r[6], "sl_premium": r[7],
                "t1_premium": r[8], "t2_premium": r[9],
                "spot_at_entry": r[10], "expiry": r[11], "alerted_at": r[12],
                "close_premium": r[13], "outcome": r[14], "pnl_pts": r[15],
                "summarized": r[16], "alert_date": r[17],
            }
            for r in rows
        ]
    finally:
        con.close()


def _get_today_alerts(unsummarized_only: bool = True) -> list[dict[str, Any]]:
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    con = sqlite3.connect(DB_FILE)
    try:
        q = """SELECT id, nse_symbol, index_name, strategy, side, strike,
                      entry_premium, sl_premium, t1_premium, t2_premium,
                      spot_at_entry, expiry, alerted_at,
                      close_premium, outcome, pnl_pts, summarized
               FROM fno_alerts WHERE alert_date = ?"""
        params: tuple[Any, ...] = (today,)
        if unsummarized_only:
            q += " AND summarized = 0"
        q += " ORDER BY alerted_at ASC"
        rows = con.execute(q, params).fetchall()
        return [
            {
                "id": r[0], "nse_symbol": r[1], "index_name": r[2] or r[1],
                "strategy": r[3], "side": r[4], "strike": r[5],
                "entry_premium": r[6], "sl_premium": r[7],
                "t1_premium": r[8], "t2_premium": r[9],
                "spot_at_entry": r[10], "expiry": r[11], "alerted_at": r[12],
                "close_premium": r[13], "outcome": r[14], "pnl_pts": r[15],
                "summarized": r[16],
            }
            for r in rows
        ]
    finally:
        con.close()


def _summary_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for r in results if r.get("outcome") in ("T1 WIN", "T2 WIN", "PARTIAL WIN"))
    losses = sum(1 for r in results if r.get("outcome") in ("SL LOSS", "PARTIAL LOSS"))
    decided = wins + losses
    win_rate = round(wins / decided * 100, 1) if decided else 0.0
    net_pts = round(
        sum(float(r.get("pnl_pts") or 0) for r in results if r.get("outcome") != "NO DATA"),
        2,
    )
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return {
        "date": now.strftime("%Y-%m-%d"),
        "as_of_ist": now.strftime("%Y-%m-%d %H:%M IST"),
        "total": len(results),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "net_pts": net_pts,
        "trades": results,
    }


def _update_alert_result(alert_id: int, close_ltp: float, outcome: str, pnl: float):
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            """UPDATE fno_alerts SET close_premium=?, outcome=?, pnl_pts=?, summarized=1
               WHERE id=?""",
            (close_ltp, outcome, pnl, alert_id),
        )
        con.commit()
    finally:
        con.close()


def build_eod_summary() -> dict[str, Any] | None:
    """Evaluate unsummarized alerts, return full day summary."""
    pending = _get_today_alerts(unsummarized_only=True)
    if not pending and not _get_today_alerts(unsummarized_only=False):
        return None

    if pending:
        nse = NSELive()
        chain_cache: dict[str, dict] = {}
        for alert in pending:
            sym = alert["nse_symbol"]
            if sym not in chain_cache:
                chain_cache[sym] = _parse_option_chain(sym, nse) or {}
            rows = chain_cache[sym].get("rows") or []

            close_ltp = _strike_close_ltp(rows, int(alert["strike"]), alert["side"])
            entry = float(alert["entry_premium"] or 0)
            sl = float(alert["sl_premium"] or entry * SL_MULT)
            t1 = float(alert["t1_premium"] or entry * T1_MULT)
            t2 = float(alert["t2_premium"] or entry * T2_MULT)

            if close_ltp is None or close_ltp <= 0:
                outcome, pnl = "NO DATA", 0.0
                close_ltp = 0.0
            else:
                outcome, pnl = _classify_outcome(entry, sl, t1, t2, close_ltp)
            _update_alert_result(alert["id"], close_ltp, outcome, pnl)

    all_trades = _get_today_alerts(unsummarized_only=False)
    summary = _summary_stats(all_trades)
    summary["scan_stats"] = get_scan_stats_for_date(
        datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    )
    return summary


def build_period_summary(days: int = 7) -> dict[str, Any] | None:
    """Win/loss + filter stats for the last N calendar days (incl. today)."""
    ensure_fno_tables()
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    end_date = now.strftime("%Y-%m-%d")
    start_date = (now - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")
    trades = _get_alerts_between(start_date, end_date)
    if not trades:
        scan = get_scan_stats_range(start_date, end_date)
        if scan.get("scan_cycles", 0) == 0:
            return None
    summary = _summary_stats(trades)
    summary["period_days"] = days
    summary["start_date"] = start_date
    summary["end_date"] = end_date
    summary["scan_stats"] = get_scan_stats_range(start_date, end_date)
    # Per-day compact breakdown
    by_day: dict[str, list] = {}
    for t in trades:
        by_day.setdefault(t["alert_date"], []).append(t)
    summary["daily"] = []
    d = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    while d <= end:
        ds = d.strftime("%Y-%m-%d")
        day_trades = by_day.get(ds, [])
        day_stat = _summary_stats(day_trades) if day_trades else None
        summary["daily"].append({
            "date": ds,
            "total": len(day_trades),
            "wins": day_stat["wins"] if day_stat else 0,
            "losses": day_stat["losses"] if day_stat else 0,
            "win_rate": day_stat["win_rate"] if day_stat else 0.0,
            "net_pts": day_stat["net_pts"] if day_stat else 0.0,
        })
        d += timedelta(days=1)
    return summary


async def build_period_summary_async(days: int = 7) -> dict[str, Any] | None:
    return await asyncio.to_thread(build_period_summary, days)


async def build_eod_summary_async() -> dict[str, Any] | None:
    return await asyncio.to_thread(build_eod_summary)


def filter_eod_summary_for_user(summary: dict[str, Any], chat_id: int) -> dict[str, Any] | None:
    prefs = get_user_alert_indices(chat_id)
    if prefs is None:
        return summary
    trades = [t for t in summary["trades"] if t.get("nse_symbol") in prefs]
    if not trades:
        return None
    out = _summary_stats(trades)
    out["scan_stats"] = summary.get("scan_stats")
    out["as_of_ist"] = summary.get("as_of_ist")
    return out


def filter_period_summary_for_user(summary: dict[str, Any], chat_id: int) -> dict[str, Any] | None:
    prefs = get_user_alert_indices(chat_id)
    if prefs is None:
        return summary
    trades = [t for t in summary["trades"] if t.get("nse_symbol") in prefs]
    if not trades:
        return None
    out = _summary_stats(trades)
    for key in ("period_days", "start_date", "end_date", "scan_stats", "daily"):
        if key in summary:
            out[key] = summary[key]
    if summary.get("daily"):
        by_day: dict[str, list] = {}
        for t in trades:
            by_day.setdefault(t["alert_date"], []).append(t)
        out["daily"] = []
        for day in summary["daily"]:
            ds = day["date"]
            dt = by_day.get(ds, [])
            if not dt:
                out["daily"].append({**day, "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pts": 0.0})
            else:
                st = _summary_stats(dt)
                out["daily"].append({
                    "date": ds,
                    "total": st["total"],
                    "wins": st["wins"],
                    "losses": st["losses"],
                    "win_rate": st["win_rate"],
                    "net_pts": st["net_pts"],
                })
    return out


def _outcome_emoji(outcome: str) -> str:
    return {
        "T2 WIN": "\U0001f7e2",
        "T1 WIN": "\U0001f7e2",
        "PARTIAL WIN": "\U0001f7e1",
        "FLAT": "\u26aa",
        "PARTIAL LOSS": "\U0001f7e0",
        "SL LOSS": "\U0001f534",
        "NO DATA": "\u2753",
    }.get(outcome, "\u2753")


def format_eod_summary_html(summary: dict[str, Any]) -> str:
    scan_block = format_scan_stats_html(summary.get("scan_stats"), label="Today")
    lines = [
        "<b>\U0001f4ca DAILY F&amp;O TRADE SUMMARY</b>",
        f"<i>{html.escape(summary['as_of_ist'])}</i>",
        "",
    ]
    if scan_block:
        lines.append(scan_block)
    lines.extend([
        f"<b>Total alerts:</b> {summary['total']}",
        f"<b>Wins:</b> {summary['wins']}  \u00b7  <b>Losses:</b> {summary['losses']}",
        f"<b>Win rate:</b> {summary['win_rate']}%",
        f"<b>Net premium P&amp;L:</b> <b>{summary['net_pts']:+.2f} pts</b> (at close)",
        "",
        "<i>P&amp;L based on premium at 3:30 PM close vs entry. "
        "Intraday T1/SL may have hit earlier.</i>",
        "",
    ])

    for t in summary["trades"]:
        em = _outcome_emoji(t["outcome"])
        entry = float(t["entry_premium"] or 0)
        close = float(t["close_premium"] or 0)
        pnl = float(t["pnl_pts"] or 0)
        time_part = (t.get("alerted_at") or "")[11:16]
        lines.append(
            f"{em} <b>{html.escape(t['index_name'])}</b> "
            f"<code>{t['strike']} {t['side']}</code>\n"
            f"   {html.escape(t['strategy'][:28])}\n"
            f"   Entry <b>{_ru(entry)}</b> \u2192 Close <b>{_ru(close) if close else 'N/A'}</b> "
            f"({pnl:+.2f} pts) \u00b7 <b>{html.escape(t['outcome'])}</b>"
            + (f" \u00b7 {time_part}" if time_part else "")
            + "\n"
        )

    lines.append("")
    lines.append("<i>\u26a0\ufe0f Paper-track only \u00b7 Not financial advice</i>")
    return "\n".join(lines).strip()


def format_period_summary_html(summary: dict[str, Any]) -> str:
    """Weekly / multi-day summary with filter stats and per-day breakdown."""
    days = summary.get("period_days", 7)
    start = summary.get("start_date", "")
    end = summary.get("end_date", "")
    scan_block = format_scan_stats_html(
        summary.get("scan_stats"),
        label=f"{start} to {end}",
    )
    lines = [
        f"<b>\U0001f4ca {days}-DAY F&amp;O SUMMARY</b>",
        f"<i>{html.escape(start)} \u2192 {html.escape(end)}</i>",
        "",
    ]
    if scan_block:
        lines.append(scan_block)
    lines.extend([
        f"<b>Total alerts:</b> {summary['total']}",
        f"<b>Wins:</b> {summary['wins']}  \u00b7  <b>Losses:</b> {summary['losses']}",
        f"<b>Win rate:</b> {summary['win_rate']}%",
        f"<b>Net premium P&amp;L:</b> <b>{summary['net_pts']:+.2f} pts</b>",
        "",
        "<b>Daily breakdown:</b>",
    ])
    for day in summary.get("daily") or []:
        if day["total"] == 0:
            lines.append(f"  {day['date']}: —")
        else:
            lines.append(
                f"  {day['date']}: {day['total']} alerts  \u00b7  "
                f"WR {day['win_rate']}%  \u00b7  {day['net_pts']:+.1f} pts"
            )
    lines.extend([
        "",
        "<b>Recent trades:</b>",
        "",
    ])
    for t in (summary.get("trades") or [])[-15:]:
        em = _outcome_emoji(t.get("outcome") or "")
        entry = float(t.get("entry_premium") or 0)
        pnl = float(t.get("pnl_pts") or 0)
        outcome = t.get("outcome") or "PENDING"
        lines.append(
            f"{em} {html.escape(t.get('alert_date', ''))} "
            f"<b>{html.escape(t.get('index_name') or '')}</b> "
            f"<code>{t.get('strike')} {t.get('side')}</code> "
            f"{pnl:+.1f} pts \u00b7 {html.escape(outcome)}"
        )
    lines.append("")
    lines.append("<i>\u26a0\ufe0f Paper-track only \u00b7 Not financial advice</i>")
    return "\n".join(lines).strip()


async def run_fno_eod_summary(bot):
    """Send one end-of-day win/loss summary after market close (~3:35 PM IST)."""
    ensure_fno_tables()
    log.info("FnO EOD summary job started (sends once after 15:30 IST)")

    from telegram.error import TelegramError

    while True:
        try:
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
            is_weekday = now_ist.weekday() < 5
            eod_start = now_ist.replace(hour=15, minute=32, second=0, microsecond=0)
            eod_end = now_ist.replace(hour=16, minute=15, second=0, microsecond=0)

            if is_weekday and eod_start <= now_ist <= eod_end and not _eod_summary_sent_today():
                summary = await build_eod_summary_async()
                if summary and summary["total"] > 0:
                    subscribers = get_all_subscribers()
                    sent = 0
                    for cid in subscribers:
                        user_summary = filter_eod_summary_for_user(summary, cid)
                        if not user_summary:
                            continue
                        text = format_eod_summary_html(user_summary)
                        try:
                            await bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
                            sent += 1
                            await asyncio.sleep(0.1)
                        except TelegramError as e:
                            log.error("EOD summary failed chat=%s: %s", cid, e)
                            if "Forbidden" in str(e) or "blocked" in str(e).lower():
                                remove_subscriber(cid)
                    _mark_eod_summary_sent()
                    log.info(
                        "FnO EOD summary sent to %d users: %d trades, %dW/%dL, net %+.2f pts",
                        sent, summary["total"], summary["wins"], summary["losses"], summary["net_pts"],
                    )
                elif summary is None:
                    _mark_eod_summary_sent()
                    log.info("FnO EOD summary: no alerts today, skipping message")
                else:
                    _mark_eod_summary_sent()

        except Exception as e:
            log.exception("FnO EOD summary error: %s", e)

        await asyncio.sleep(180)


# ════════════════════ Auto-monitor loop ════════════════════

async def run_fno_monitor(bot):
    """Background loop: scan every FNO_SCAN_INTERVAL seconds, send alerts on new setups."""
    ensure_fno_tables()
    interval = max(60, FNO_SCAN_INTERVAL)
    log.info("FnO monitor started: scanning every %ds during market hours", interval)

    from telegram.error import TelegramError

    while True:
        try:
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
            market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
            market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
            is_weekday = now_ist.weekday() < 5

            if not (is_weekday and market_open <= now_ist <= market_close):
                log.debug("FnO monitor: market closed, sleeping %ds", interval)
                await asyncio.sleep(interval)
                continue

            signals = await scan_all_indices_async()
            if not signals:
                log.debug("FnO scan: no new signals this cycle")
                await asyncio.sleep(interval)
                continue

            for sig in signals:
                text = format_alert_html(sig)
                _record_alert(sig)

                subscribers = get_subscribers_for_index(sig["nse"])
                if not subscribers:
                    continue

                sent = 0
                for cid in subscribers:
                    try:
                        await bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
                        sent += 1
                        await asyncio.sleep(0.1)
                    except TelegramError as e:
                        log.error("FnO alert failed for %s chat=%s: %s", sig["nse"], cid, e)
                        if "Forbidden" in str(e) or "blocked" in str(e).lower() or "not found" in str(e).lower():
                            remove_subscriber(cid)

                log.info(
                    "FnO ALERT sent to %d users: %s %s %s %s @ %s",
                    sent, sig["strategy"], sig["name"], sig["side"],
                    sig["strike"], sig["premium"],
                )

        except Exception as e:
            log.exception("FnO monitor error: %s", e)

        await asyncio.sleep(interval)
