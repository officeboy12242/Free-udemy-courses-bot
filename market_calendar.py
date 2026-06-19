"""NSE/BSE session calendar — trading days, market hours, smart sleep when closed."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN_H, MARKET_OPEN_M = 9, 15
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 30
EXIT_MONITOR_CLOSE_H, EXIT_MONITOR_CLOSE_M = 15, 35
EOD_START_H, EOD_START_M = 15, 32
EOD_END_H, EOD_END_M = 16, 15

# Official NSE capital-market holidays (Equity + F&O). Update yearly.
NSE_HOLIDAYS: dict[int, frozenset[date]] = {
    2025: frozenset({
        date(2025, 2, 26),
        date(2025, 3, 14),
        date(2025, 3, 31),
        date(2025, 4, 10),
        date(2025, 4, 14),
        date(2025, 4, 18),
        date(2025, 5, 1),
        date(2025, 8, 15),
        date(2025, 8, 27),
        date(2025, 10, 2),
        date(2025, 10, 21),
        date(2025, 10, 22),
        date(2025, 11, 5),
        date(2025, 12, 25),
    }),
    2026: frozenset({
        date(2026, 1, 15),   # Maharashtra municipal elections
        date(2026, 1, 26),
        date(2026, 3, 3),
        date(2026, 3, 26),
        date(2026, 3, 31),
        date(2026, 4, 3),
        date(2026, 4, 14),
        date(2026, 5, 1),
        date(2026, 5, 28),
        date(2026, 6, 26),
        date(2026, 9, 14),
        date(2026, 10, 2),
        date(2026, 10, 20),
        date(2026, 11, 10),
        date(2026, 11, 24),
        date(2026, 12, 25),
    }),
}

_extra_holidays_cache: frozenset[date] | None = None


def _extra_holidays() -> frozenset[date]:
    global _extra_holidays_cache
    if _extra_holidays_cache is not None:
        return _extra_holidays_cache
    out: set[date] = set()
    raw = os.getenv("NSE_EXTRA_HOLIDAYS", "").strip()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(date.fromisoformat(part))
        except ValueError:
            continue
    _extra_holidays_cache = frozenset(out)
    return _extra_holidays_cache


def is_nse_holiday(d: date) -> bool:
    return d in NSE_HOLIDAYS.get(d.year, frozenset()) or d in _extra_holidays()


def is_nse_trading_day(d: date | None = None) -> bool:
    d = d or datetime.now(IST).date()
    return d.weekday() < 5 and not is_nse_holiday(d)


def _session_bounds(
    now: datetime,
    *,
    for_exit: bool = False,
) -> tuple[datetime, datetime]:
    close_h = EXIT_MONITOR_CLOSE_H if for_exit else MARKET_CLOSE_H
    close_m = EXIT_MONITOR_CLOSE_M if for_exit else MARKET_CLOSE_M
    open_t = now.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0, microsecond=0)
    close_t = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return open_t, close_t


def is_market_hours(now: datetime | None = None, *, for_exit: bool = False) -> bool:
    """True during NSE regular session on a trading day."""
    now = now or datetime.now(IST)
    if not is_nse_trading_day(now.date()):
        return False
    open_t, close_t = _session_bounds(now, for_exit=for_exit)
    return open_t <= now <= close_t


def is_eod_summary_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    if not is_nse_trading_day(now.date()):
        return False
    start = now.replace(hour=EOD_START_H, minute=EOD_START_M, second=0, microsecond=0)
    end = now.replace(hour=EOD_END_H, minute=EOD_END_M, second=0, microsecond=0)
    return start <= now <= end


def next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_nse_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def next_market_open(now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(IST)
    open_t, _ = _session_bounds(now)
    if is_nse_trading_day(now.date()) and now < open_t:
        return open_t
    d = next_trading_day(now.date())
    return datetime(d.year, d.month, d.day, MARKET_OPEN_H, MARKET_OPEN_M, tzinfo=IST)


def market_gate_sleep_seconds(
    now: datetime | None = None,
    poll_interval: int = 180,
    *,
    max_sleep: int = 3600,
) -> int:
    """Longer sleep when market is closed (weekends, holidays, after hours)."""
    now = now or datetime.now(IST)
    poll_interval = max(5, poll_interval)
    if is_market_hours(now) or is_market_hours(now, for_exit=True):
        return poll_interval

    if is_nse_trading_day(now.date()):
        open_t, _ = _session_bounds(now)
        if now < open_t:
            secs = int((open_t - now).total_seconds())
            return max(poll_interval, min(secs, max_sleep))

    nxt = next_market_open(now)
    if nxt:
        secs = int((nxt - now).total_seconds())
        return max(poll_interval, min(secs, max_sleep))
    return min(max_sleep, poll_interval * 10)


def market_status_label(now: datetime | None = None) -> str:
    now = now or datetime.now(IST)
    d = now.date()
    if d.weekday() >= 5:
        return "weekend"
    if is_nse_holiday(d):
        return "holiday"
    open_t, close_t = _session_bounds(now)
    if now < open_t:
        return "pre_market"
    if now > close_t:
        return "after_hours"
    return "open"


_gate_logged: dict[str, str] = {}


def log_market_gate(logger, component: str, now: datetime | None = None) -> None:
    """Log once when session state changes (open/closed/holiday/weekend)."""
    now = now or datetime.now(IST)
    status = market_status_label(now)
    if _gate_logged.get(component) == status:
        return
    _gate_logged[component] = status
    nxt = next_market_open(now)
    nxt_s = nxt.strftime("%a %d-%b %H:%M") if nxt else "unknown"
    logger.info(
        "%s idle — market %s (%s IST). Next session: %s",
        component,
        status.replace("_", " "),
        now.strftime("%a %H:%M"),
        nxt_s,
    )
