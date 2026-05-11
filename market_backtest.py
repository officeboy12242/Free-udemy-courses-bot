"""
Backtest: fixed ₹ investment on each session when daily drop >= dip% vs prior close,
vs fixed monthly SIP on the first trading day of each month.
Uses Yahoo Finance daily history (free tier; not transaction-cost adjusted).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    ticker: str
    start: str
    end: str
    dip_percent: float
    amount_per_event: float
    dip_events: int
    dip_shares: float
    dip_invested: float
    dip_final_value: float
    monthly_events: int
    monthly_shares: float
    monthly_invested: float
    monthly_final_value: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "start": self.start,
            "end": self.end,
            "dip_percent": self.dip_percent,
            "amount_per_event": self.amount_per_event,
            "dip_strategy": {
                "buy_events": self.dip_events,
                "total_invested": round(self.dip_invested, 2),
                "shares_accumulated": round(self.dip_shares, 6),
                "value_at_last_close": round(self.dip_final_value, 2),
            },
            "monthly_sip": {
                "buy_events": self.monthly_events,
                "total_invested": round(self.monthly_invested, 2),
                "shares_accumulated": round(self.monthly_shares, 6),
                "value_at_last_close": round(self.monthly_final_value, 2),
            },
        }


def _normalize_close(df: pd.DataFrame) -> pd.Series:
    if isinstance(df.columns, pd.MultiIndex):
        if "Close" in df.columns.get_level_values(0):
            s = df["Close"]
            return s.iloc[:, 0].dropna() if isinstance(s, pd.DataFrame) else s.dropna()
        flat = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df = df.copy()
        df.columns = flat
    return df["Close"].dropna()


def run_backtest(
    ticker: str = "^NSEI",
    start: str = "2015-01-01",
    amount_inr: float = 5000.0,
    dip_percent: float = 1.0,
) -> BacktestResult | None:
    try:
        raw = yf.download(ticker, start=start, progress=False, auto_adjust=False)
    except Exception as e:
        log.warning("Backtest download failed: %s", e)
        return None

    if raw is None or raw.empty:
        return None

    close = _normalize_close(raw)
    if len(close) < 3:
        return None

    rets = close.pct_change()
    dip_mask = rets <= -(dip_percent / 100.0)

    dip_shares = 0.0
    dip_invested = 0.0
    dip_events = 0
    for dt, hit in dip_mask.items():
        if not hit or pd.isna(rets.loc[dt]):
            continue
        price = float(close.loc[dt])
        if price <= 0:
            continue
        dip_shares += amount_inr / price
        dip_invested += amount_inr
        dip_events += 1

    idx = close.index
    monthly_shares = 0.0
    monthly_invested = 0.0
    monthly_events = 0
    seen_months: set[tuple[int, int]] = set()
    for dt in idx:
        key = (dt.year, dt.month)
        if key in seen_months:
            continue
        seen_months.add(key)
        price = float(close.loc[dt])
        if price <= 0:
            continue
        monthly_shares += amount_inr / price
        monthly_invested += amount_inr
        monthly_events += 1

    last_price = float(close.iloc[-1])
    dip_final = dip_shares * last_price
    monthly_final = monthly_shares * last_price

    start_s = str(close.index[0].date())
    end_s = str(close.index[-1].date())

    return BacktestResult(
        ticker=ticker,
        start=start_s,
        end=end_s,
        dip_percent=dip_percent,
        amount_per_event=amount_inr,
        dip_events=dip_events,
        dip_shares=dip_shares,
        dip_invested=dip_invested,
        dip_final_value=dip_final,
        monthly_events=monthly_events,
        monthly_shares=monthly_shares,
        monthly_invested=monthly_invested,
        monthly_final_value=monthly_final,
    )
