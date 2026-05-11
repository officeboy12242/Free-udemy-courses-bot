"""
Indian market tracking, dip detection vs previous close, and Telegram alerts.
Uses Yahoo Finance via yfinance (symbols may be delayed; not official NSE feed).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DB_FILE = os.getenv("DB_FILE", "posted_courses.db")

# Yahoo Finance tickers (Indian markets)
DEFAULT_TRACKED: list[tuple[str, str]] = [
    ("^NSEI", "Nifty 50"),
    ("^BSESN", "Sensex"),
    ("NIFTYBEES.NS", "Nifty BeES"),
]

# If primary symbol fails (NaN, gaps), try these Yahoo tickers in order; alerts still use the canonical (primary) symbol.
SYMBOL_FALLBACKS: dict[str, tuple[str, ...]] = {
    "NIFTYBEES.NS": ("NIFTYBEES.BO",),
}

MARKET_ALERT_CHAT_ID = os.getenv("MARKET_ALERT_CHAT_ID", "")
DIP_THRESHOLD_PERCENT = float(os.getenv("DIP_THRESHOLD_PERCENT", "1.0"))
MARKET_POLL_INTERVAL = int(os.getenv("MARKET_POLL_INTERVAL", "120"))
MARKET_FEATURES_ENABLED = os.getenv("MARKET_FEATURES_ENABLED", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def tracked_symbols() -> list[tuple[str, str]]:
    raw = os.getenv("MARKET_SYMBOLS")
    if not raw:
        return list(DEFAULT_TRACKED)
    out: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            sym, name = part.split(":", 1)
            out.append((sym.strip(), name.strip()))
        else:
            out.append((part, part))
    return out or list(DEFAULT_TRACKED)


def ensure_market_tables():
    con = sqlite3.connect(DB_FILE)
    try:
        # New schema: multiple alerts per day allowed (up to MAX_ALERTS_PER_DAY)
        con.execute("""
            CREATE TABLE IF NOT EXISTS market_dip_alerts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol     TEXT NOT NULL,
                alert_date TEXT NOT NULL,
                pct_change REAL,
                alerted_at TEXT
            )
        """)
        # Migrate old single-row-per-day schema if alerted_at column is missing
        try:
            con.execute("ALTER TABLE market_dip_alerts ADD COLUMN alerted_at TEXT")
        except Exception:
            pass
        # Remove old unique primary key constraint by recreating if needed (best effort)
        con.commit()
    finally:
        con.close()


def _today_ist() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


# Minimum additional drop (%) beyond the last alerted pct to trigger another alert
MIN_DEEPER_STEP = float(os.getenv("MIN_DEEPER_STEP", "0.5"))


def last_alerted_pct(symbol: str, alert_date: str) -> float | None:
    """Return the pct_change of the most recent alert for this symbol today, or None."""
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            """SELECT pct_change FROM market_dip_alerts
               WHERE symbol = ? AND alert_date = ?
               ORDER BY pct_change ASC LIMIT 1""",
            (symbol, alert_date),
        ).fetchone()
        return float(row[0]) if row else None
    finally:
        con.close()


def should_alert(symbol: str, alert_date: str, current_pct: float) -> bool:
    """
    Alert if:
    - No alert sent today yet, OR
    - Current dip is at least MIN_DEEPER_STEP% deeper than the last alerted dip.
    """
    last = last_alerted_pct(symbol, alert_date)
    if last is None:
        return True
    return current_pct <= last - MIN_DEEPER_STEP


def record_alert(symbol: str, alert_date: str, pct_change: float):
    con = sqlite3.connect(DB_FILE)
    try:
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%dT%H:%M:%S")
        con.execute(
            """INSERT INTO market_dip_alerts (symbol, alert_date, pct_change, alerted_at)
               VALUES (?, ?, ?, ?)""",
            (symbol, alert_date, pct_change, now_ist),
        )
        con.commit()
    finally:
        con.close()


def alert_count_today(symbol: str, alert_date: str) -> int:
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM market_dip_alerts WHERE symbol = ? AND alert_date = ?",
            (symbol, alert_date),
        ).fetchone()
        return row[0] if row else 0
    finally:
        con.close()


def _closes_from_history(t: yf.Ticker) -> tuple[float, float] | None:
    """Last two daily closes: (previous_session, last_session)."""
    try:
        df = t.history(period="15d")
        if df is None or df.empty or len(df) < 2:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            close = df["Close"]
            s = close.iloc[:, 0] if isinstance(close, pd.DataFrame) else close
        else:
            s = df["Close"]
        s = s.dropna()
        if len(s) < 2:
            return None
        prev_f = float(s.iloc[-2])
        last_f = float(s.iloc[-1])
        if prev_f == 0 or math.isnan(last_f) or math.isnan(prev_f):
            return None
        return prev_f, last_f
    except Exception:
        return None


def _try_snapshot_one_yahoo(yahoo_symbol: str) -> tuple[float, float, float] | None:
    """Return (last, prev_close, pct) or None."""
    try:
        t = yf.Ticker(yahoo_symbol)
        fi = getattr(t, "fast_info", {}) or {}
        last = fi.get("last_price")
        if last is None:
            last = fi.get("regular_market_price")
        prev = fi.get("previous_close")

        last_f: float | None = None
        prev_f: float | None = None

        if last is not None and prev is not None:
            last_f = float(last)
            prev_f = float(prev)
            if math.isnan(last_f) or math.isnan(prev_f) or prev_f == 0:
                last_f = prev_f = None

        if last_f is None:
            pair = _closes_from_history(t)
            if pair is None:
                return None
            prev_f, last_f = pair  # history order: we stored (prev_session, last) — fix naming

        # _closes_from_history returns (iloc[-2], iloc[-1]) as prev_f, last_f — correct
        pct = (last_f - prev_f) / prev_f * 100.0
        if math.isnan(pct):
            return None
        return last_f, prev_f, pct
    except Exception as e:
        log.debug("Snapshot attempt failed for %s: %s", yahoo_symbol, e)
        return None


def fetch_snapshot(canonical_symbol: str, display_name: str) -> dict[str, Any] | None:
    """Latest vs previous session close; tries SYMBOL_FALLBACKS if primary fails."""
    candidates = (canonical_symbol,) + SYMBOL_FALLBACKS.get(canonical_symbol, ())
    for yahoo_sym in candidates:
        triplet = _try_snapshot_one_yahoo(yahoo_sym)
        if not triplet:
            continue
        last_f, prev_f, pct = triplet
        return {
            "symbol": canonical_symbol,
            "quote_symbol": yahoo_sym,
            "name": display_name,
            "last": round(last_f, 2),
            "previous_close": round(prev_f, 2),
            "pct_change": round(pct, 3),
        }
    log.warning("Market snapshot failed for %s (tried %s)", display_name, candidates)
    return None


def fetch_all_snapshots() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sym, name in tracked_symbols():
        snap = fetch_snapshot(sym, name)
        if snap:
            out.append(snap)
    return out


async def fetch_all_snapshots_async() -> list[dict[str, Any]]:
    return await asyncio.to_thread(fetch_all_snapshots)


def format_dip_alert(name: str, pct_change: float, threshold: float) -> str:
    down = abs(pct_change)

    if down >= 3:
        intensity = "\U0001f6a8\U0001f6a8 MAJOR DIP \U0001f6a8\U0001f6a8"
        bar = "\u2588" * 10
        arrow = "\U0001f4a5"
    elif down >= 2:
        intensity = "\u26a0\ufe0f STRONG DIP \u26a0\ufe0f"
        bar = "\u2588" * 7 + "\u2591" * 3
        arrow = "\U0001f53b"
    else:
        intensity = "\U0001f4c9 DIP ALERT \U0001f4c9"
        bar = "\u2588" * 4 + "\u2591" * 6
        arrow = "\u2b07\ufe0f"

    return (
        f"{arrow} {intensity}\n"
        f"\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\n"
        f"\U0001f3af  {name}\n"
        f"\U0001f4ca  Drop: -{down:.2f}%  [{bar}]\n"
        f"\u23f0  Threshold: {threshold:g}%\n"
        f"\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\n"
        f"\U0001f4b0 Time to SIP the Dip!\n\n"
        f"\U0001f9fe  Consider your planned equity allocation\n"
        f"\u2714\ufe0f  Stay within your risk budget & goals\n"
        f"\U0001f680  Every dip is an opportunity\n\n"
        f"\u26a1 Powered by @CoursesDrivee"
    )


def format_test_dip_alert(
    name: str = "Nifty 50",
    pct_change: float = -1.25,
    threshold: float | None = None,
) -> str:
    """Same wording as a real dip alert, with a clear TEST banner (plain text)."""
    th = threshold if threshold is not None else DIP_THRESHOLD_PERCENT
    body = format_dip_alert(name, pct_change, th)
    return (
        "\U0001f9ea TEST ALERT \u2014 not a live market signal\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
        f"{body}"
    )


def build_dip_status(threshold_percent: float | None = None) -> dict[str, Any]:
    """
    Fresh Yahoo snapshot + same dip rule as run_market_monitor.
    Use this to see “would it alert right now?” without waiting for the poll interval.
    """
    th = float(threshold_percent if threshold_percent is not None else DIP_THRESHOLD_PERCENT)
    day = _today_ist()
    quotes = fetch_all_snapshots()
    instruments: list[dict[str, Any]] = []
    for q in quotes:
        pct = float(q["pct_change"])
        meets = pct <= -th
        
        last_alert_pct = last_alerted_pct(q["symbol"], day)
        if last_alert_pct is None:
            alerted_today = False
            would_send = meets
            need_more = max(0.0, th + pct)
        else:
            alerted_today = True
            would_send = pct <= last_alert_pct - MIN_DEEPER_STEP
            need_more = max(0.0, (last_alert_pct - MIN_DEEPER_STEP) - pct) if not would_send else 0.0

        instruments.append(
            {
                "symbol": q["symbol"],
                "name": q["name"],
                "quote_symbol": q.get("quote_symbol", q["symbol"]),
                "last": q["last"],
                "previous_close": q["previous_close"],
                "pct_change_vs_prev_close": pct,
                "dip_threshold_percent": th,
                "condition_pct_vs_prev_close_lte_neg_threshold": meets,
                "already_alerted_today_ist": alerted_today,
                "last_alerted_pct": last_alert_pct,
                "would_send_telegram_now": would_send,
                "percent_points_more_decline_to_hit_threshold": round(need_more, 4),
            }
        )
    return {
        "as_of_utc": datetime.utcnow().isoformat() + "Z",
        "calendar_day_ist": day,
        "market_poll_interval_seconds": MARKET_POLL_INTERVAL,
        "data_source": "Yahoo Finance via yfinance (often delayed vs NSE live ticks; not official).",
        "dip_rule_plain": (
            f"Fire when change vs previous session close is <= -{th:g}% "
            f"(at least {th:g}% down). Subsequent alerts fire every {MIN_DEEPER_STEP}% deeper drop."
        ),
        "dip_threshold_percent": th,
        "instruments": instruments,
    }


def format_dip_status_telegram(status: dict[str, Any]) -> str:
    lines = [
        "📊 Market dip check (fresh pull)",
        status["data_source"],
        "",
        f"Rule: {status['dip_rule_plain']}",
        f"IST date: {status['calendar_day_ist']}",
        f"Poll interval (background bot): {status['market_poll_interval_seconds']}s",
        "",
    ]
    for i in status["instruments"]:
        lines.append(f"• {i['name']} ({i['quote_symbol']})")
        lines.append(
            f"  Last {i['last']} · Prev close {i['previous_close']} "
            f"· Δ {i['pct_change_vs_prev_close']:+.3f}%"
        )
        if i["would_send_telegram_now"]:
            lines.append("  → Would ALERT on next monitor tick ✅")
        elif i["already_alerted_today_ist"]:
            extra = i["percent_points_more_decline_to_hit_threshold"]
            last_pct = i["last_alerted_pct"]
            lines.append(f"  → Already alerted today at {last_pct:.2f}% ⏸")
            lines.append(f"  → Needs to drop to {last_pct - MIN_DEEPER_STEP:.2f}% for next alert (~{extra:.2f}% more)")
        else:
            extra = i["percent_points_more_decline_to_hit_threshold"]
            lines.append(
                f"  → No alert yet (~{extra:.2f} percentage points more decline needed)"
            )
        lines.append("")
    lines.append("(Not financial advice.)")
    return "\n".join(lines).strip()


async def build_dip_status_async(threshold_percent: float | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(build_dip_status, threshold_percent)


async def run_market_monitor(bot, alert_chat_id: str | None = None):
    """Poll markets; alert every time dip grows deeper by MIN_DEEPER_STEP%."""
    if not MARKET_FEATURES_ENABLED:
        log.info("Market features disabled (MARKET_FEATURES_ENABLED).")
        return

    ensure_market_tables()
    chat = alert_chat_id or MARKET_ALERT_CHAT_ID
    threshold = DIP_THRESHOLD_PERCENT
    interval = max(30, MARKET_POLL_INTERVAL)

    log.info(
        "Market monitor: chat=%s dip_threshold=%s%% poll=%ss",
        chat,
        threshold,
        interval,
    )

    from telegram.error import TelegramError

    while True:
        try:
            snaps = await fetch_all_snapshots_async()
            day = _today_ist()
            for s in snaps:
                pct = s["pct_change"]
                if pct > -threshold:
                    continue
                sym = s["symbol"]
                if not should_alert(sym, day, pct):
                    log.debug(
                        "Dip alert skipped for %s (%.2f%%) — needs %.2f%% deeper than last alert",
                        s["name"], pct, MIN_DEEPER_STEP,
                    )
                    continue
                count = alert_count_today(sym, day) + 1
                text = format_dip_alert(s["name"], pct, threshold)
                try:
                    await bot.send_message(chat_id=chat, text=text)
                    record_alert(sym, day, pct)
                    log.info("Dip alert #%d sent: %s %.2f%%", count, s["name"], pct)
                except TelegramError as e:
                    log.error(
                        "Telegram dip alert FAILED for %s → chat=%s | error: %s | "
                        "Fix: user must /start the bot, or use a numeric chat_id.",
                        sym, chat, e,
                    )
        except Exception as e:
            log.exception("Market monitor iteration error: %s", e)

        await asyncio.sleep(interval)
