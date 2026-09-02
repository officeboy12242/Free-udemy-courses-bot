"""
F&O intraday strategy backtest — measured on the underlying index
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The live service ships hard-coded win rates (_STRATEGY_BASE_WIN: confluence
66%, MACD-MTF 69%, ORB 62%, PCR 61%, mean-rev 58%). Those are display
constants, never measured against data. This measures them.

Everything is scored on the index itself, not on option premium. An option
trade's outcome is driven by the underlying move; premium adds delta, theta
and IV noise that obscures whether the *signal* was right. Results are in
R-multiples, so a NIFTY setup and a BANKNIFTY setup are comparable despite
very different point values.

Strategies covered:
  SWEEP_LONG / SWEEP_SHORT  liquidity sweep — the new one
  ORB                       opening range breakout
  MACD_MTF                  MACD alignment across timeframes
  MEAN_REV                  Bollinger/VWAP fade

Intraday rules applied to all: no entry before 09:45 (opening auction noise)
or after 14:45, and every position is flat by 15:15.

Run:  python fno_backtest.py [--interval 5m] [--index NIFTY]
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import statistics as st
import sys
import warnings

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import numpy as np
import pandas as pd
import yfinance as yf

IST = "Asia/Kolkata"

INDICES = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
}

FIRST_ENTRY = dt.time(9, 45)
LAST_ENTRY = dt.time(14, 45)
FLAT_BY = dt.time(15, 15)


# ── data ─────────────────────────────────────────────────────────────────────

def load(symbol: str, interval: str) -> pd.DataFrame | None:
    df = yf.download(symbol, interval=interval, period="60d",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.dropna(subset=["Close"])
    df.index = df.index.tz_convert(IST)
    return df


def add_indicators(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    c, h, l = d["Close"], d["High"], d["Low"]

    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["ATR"] = tr.rolling(14).mean()

    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    d["MACD"] = e12 - e26
    d["MACD_SIG"] = d["MACD"].ewm(span=9, adjust=False).mean()
    d["EMA20"] = c.ewm(span=20, adjust=False).mean()
    d["EMA50"] = c.ewm(span=50, adjust=False).mean()

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    d["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    d["BB_UP"] = mid + 2 * sd
    d["BB_DN"] = mid - 2 * sd

    # Session VWAP, reset each day.
    tp = (h + l + c) / 3
    dates = pd.Series(d.index.date, index=d.index)
    d["VWAP"] = (tp * d["Volume"]).groupby(dates).cumsum() / \
                d["Volume"].groupby(dates).cumsum().replace(0, np.nan)
    d["VWAP"] = d["VWAP"].fillna(c)
    return d


# ── liquidity sweep ──────────────────────────────────────────────────────────
# A swing low is where stop-loss orders accumulate: everyone long from that
# level rests a stop just beneath it. A sweep is price trading *through* that
# level — filling those stops — and then closing back above it. The stops are
# gone, the sellers are exhausted, and price reverses. If instead price stays
# below, the level genuinely broke and there is no trade.
#
# The distinction that matters is wick versus close: a sweep pierces the level
# intrabar but closes back on the original side. A breakdown closes beyond it.

def swing_points(d: pd.DataFrame, left: int = 3, right: int = 3):
    """Confirmed pivot highs and lows. A pivot needs `right` bars after it to
    exist, so index i is only usable from bar i + right onward."""
    h, l = d["High"].to_numpy(), d["Low"].to_numpy()
    n = len(d)
    hi = np.zeros(n, dtype=bool)
    lo = np.zeros(n, dtype=bool)
    for i in range(left, n - right):
        w_h, w_l = h[i - left:i + right + 1], l[i - left:i + right + 1]
        if h[i] == w_h.max() and (w_h == h[i]).sum() == 1:
            hi[i] = True
        if l[i] == w_l.min() and (w_l == l[i]).sum() == 1:
            lo[i] = True
    return hi, lo


def find_sweeps(d: pd.DataFrame, *, left: int = 3, right: int = 3,
                lookback: int = 60, min_pierce_atr: float = 0.10,
                max_reclaim_bars: int = 3) -> list[dict]:
    """Every liquidity sweep in the frame.

    A long sweep: price pierces a prior confirmed swing low by at least
    min_pierce_atr ATRs, then closes back above that low within
    max_reclaim_bars. Entry is the reclaim close, stop below the sweep wick.
    """
    hi_piv, lo_piv = swing_points(d, left, right)
    highs = d["High"].to_numpy()
    lows = d["Low"].to_numpy()
    closes = d["Close"].to_numpy()
    atr = d["ATR"].to_numpy()
    times = d.index
    dates = np.array([t.date() for t in times])

    out = []
    for i in range(lookback, len(d)):
        a = atr[i]
        if not a or np.isnan(a):
            continue

        # Only levels formed earlier the same day; overnight levels behave
        # differently and are handled by the gap, not by a sweep.
        lo_lv = hi_lv = None
        for j in range(i - right - 1, max(0, i - lookback), -1):
            if dates[j] != dates[i]:
                break
            if lo_lv is None and lo_piv[j]:
                lo_lv = (j, lows[j])
            if hi_lv is None and hi_piv[j]:
                hi_lv = (j, highs[j])
            if lo_lv and hi_lv:
                break

        # ── bullish: sweep the lows, reclaim ──
        if lo_lv:
            j, level = lo_lv
            pierce = level - lows[i]
            if pierce >= min_pierce_atr * a and closes[i] < level:
                for k in range(i, min(i + max_reclaim_bars + 1, len(d))):
                    if dates[k] != dates[i]:
                        break
                    if closes[k] > level:
                        out.append({
                            "i": k, "dir": 1, "strategy": "SWEEP_LONG",
                            "entry": closes[k],
                            "stop": min(lows[i:k + 1]) - 0.1 * a,
                            "level": level, "pierce_atr": pierce / a,
                            "reclaim_bars": k - i,
                        })
                        break
                    if lows[k] < min(lows[i:k + 1]):
                        break  # still falling — a real breakdown

        # ── bearish: sweep the highs, reject ──
        if hi_lv:
            j, level = hi_lv
            pierce = highs[i] - level
            if pierce >= min_pierce_atr * a and closes[i] > level:
                for k in range(i, min(i + max_reclaim_bars + 1, len(d))):
                    if dates[k] != dates[i]:
                        break
                    if closes[k] < level:
                        out.append({
                            "i": k, "dir": -1, "strategy": "SWEEP_SHORT",
                            "entry": closes[k],
                            "stop": max(highs[i:k + 1]) + 0.1 * a,
                            "level": level, "pierce_atr": pierce / a,
                            "reclaim_bars": k - i,
                        })
                        break
                    if highs[k] > max(highs[i:k + 1]):
                        break
    return out


# ── the incumbent strategies ─────────────────────────────────────────────────

def find_orb(d: pd.DataFrame, or_bars: int = 6) -> list[dict]:
    """Break of the first 30 minutes' range (6 x 5m bars)."""
    out = []
    for day, g in d.groupby(d.index.date):
        if len(g) < or_bars + 6:
            continue
        opening = g.iloc[:or_bars]
        hi, lo = float(opening["High"].max()), float(opening["Low"].min())
        idx = d.index.get_indexer(g.index)
        done = False
        for pos, i in enumerate(idx[or_bars:], start=or_bars):
            a = d["ATR"].iat[i]
            if not a or np.isnan(a) or done:
                continue
            c = float(d["Close"].iat[i])
            if c > hi:
                out.append({"i": i, "dir": 1, "strategy": "ORB",
                            "entry": c, "stop": lo})
                done = True
            elif c < lo:
                out.append({"i": i, "dir": -1, "strategy": "ORB",
                            "entry": c, "stop": hi})
                done = True
    return out


def find_macd_mtf(d: pd.DataFrame) -> list[dict]:
    """MACD cross on this timeframe, aligned with the higher-timeframe trend."""
    out = []
    macd = d["MACD"].to_numpy()
    sig = d["MACD_SIG"].to_numpy()
    e20, e50 = d["EMA20"].to_numpy(), d["EMA50"].to_numpy()
    c, atr = d["Close"].to_numpy(), d["ATR"].to_numpy()
    for i in range(60, len(d)):
        a = atr[i]
        if not a or np.isnan(a) or np.isnan(e50[i]):
            continue
        up = macd[i - 1] <= sig[i - 1] and macd[i] > sig[i]
        dn = macd[i - 1] >= sig[i - 1] and macd[i] < sig[i]
        if up and e20[i] > e50[i] and c[i] > e20[i]:
            out.append({"i": i, "dir": 1, "strategy": "MACD_MTF",
                        "entry": c[i], "stop": c[i] - 1.5 * a})
        elif dn and e20[i] < e50[i] and c[i] < e20[i]:
            out.append({"i": i, "dir": -1, "strategy": "MACD_MTF",
                        "entry": c[i], "stop": c[i] + 1.5 * a})
    return out


def find_mean_rev(d: pd.DataFrame) -> list[dict]:
    """Fade a stretch outside the Bollinger band, back toward VWAP."""
    out = []
    c = d["Close"].to_numpy()
    up, dn = d["BB_UP"].to_numpy(), d["BB_DN"].to_numpy()
    rsi, atr = d["RSI"].to_numpy(), d["ATR"].to_numpy()
    for i in range(60, len(d)):
        a = atr[i]
        if not a or np.isnan(a) or np.isnan(up[i]) or np.isnan(rsi[i]):
            continue
        if c[i - 1] < dn[i - 1] and c[i] > dn[i] and rsi[i] < 40:
            out.append({"i": i, "dir": 1, "strategy": "MEAN_REV",
                        "entry": c[i], "stop": c[i] - 1.5 * a})
        elif c[i - 1] > up[i - 1] and c[i] < up[i] and rsi[i] > 60:
            out.append({"i": i, "dir": -1, "strategy": "MEAN_REV",
                        "entry": c[i], "stop": c[i] + 1.5 * a})
    return out


# ── simulation ───────────────────────────────────────────────────────────────

def simulate(d: pd.DataFrame, sig: dict, *, t1_r: float, t2_r: float,
             trail_atr: float, be_at_r: float) -> dict | None:
    """Forward-test one signal. Stop first within a bar, flat by session end."""
    i, direction = sig["i"], sig["dir"]
    entry, stop = sig["entry"], sig["stop"]
    r = (entry - stop) * direction
    if r <= 0:
        return None

    a = float(d["ATR"].iat[i])
    if not a or np.isnan(a):
        return None
    # Reject setups whose stop is absurdly wide or tight relative to noise.
    if not (0.3 * a <= r <= 6 * a):
        return None

    highs, lows = d["High"].to_numpy(), d["Low"].to_numpy()
    closes = d["Close"].to_numpy()
    times = d.index
    day = times[i].date()

    cur_stop = stop
    peak = entry
    t1_hit = be_done = False

    for k in range(i + 1, len(d)):
        if times[k].date() != day or times[k].time() >= FLAT_BY:
            px = closes[k - 1]
            return {**sig, "r": (px - entry) * direction / r, "reason": "EOD",
                    "bars": k - i}

        hi, lo = highs[k], lows[k]

        if direction == 1:
            if lo <= cur_stop:
                return {**sig, "r": (cur_stop - entry) / r,
                        "reason": "TRAIL" if t1_hit else ("BE" if be_done else "SL"),
                        "bars": k - i}
            if hi >= entry + t2_r * r:
                return {**sig, "r": t2_r, "reason": "T2", "bars": k - i}
            peak = max(peak, hi)
            if not be_done and hi >= entry + be_at_r * r:
                be_done, cur_stop = True, max(cur_stop, entry)
            if not t1_hit and hi >= entry + t1_r * r:
                t1_hit = True
            if t1_hit:
                cur_stop = max(cur_stop, peak - trail_atr * a)
        else:
            if hi >= cur_stop:
                return {**sig, "r": (entry - cur_stop) / r,
                        "reason": "TRAIL" if t1_hit else ("BE" if be_done else "SL"),
                        "bars": k - i}
            if lo <= entry - t2_r * r:
                return {**sig, "r": t2_r, "reason": "T2", "bars": k - i}
            peak = min(peak, lo)
            if not be_done and lo <= entry - be_at_r * r:
                be_done, cur_stop = True, min(cur_stop, entry)
            if not t1_hit and lo <= entry - t1_r * r:
                t1_hit = True
            if t1_hit:
                cur_stop = min(cur_stop, peak + trail_atr * a)

    px = closes[-1]
    return {**sig, "r": (px - entry) * direction / r, "reason": "EOD",
            "bars": len(d) - 1 - i}


def tradeable(d: pd.DataFrame, sig: dict) -> bool:
    t = d.index[sig["i"]].time()
    return FIRST_ENTRY <= t <= LAST_ENTRY


def report(rows: list[dict], label: str) -> dict | None:
    if len(rows) < 20:
        print(f"  {label:<16s} n={len(rows):4d}  — too few to judge")
        return None
    rs = [x["r"] for x in rows]
    wins = [x for x in rs if x > 0]
    gl = abs(sum(x for x in rs if x <= 0))
    pf = (sum(wins) / gl) if gl else 99.0
    exp = sum(rs) / len(rs)
    reasons: dict[str, int] = {}
    for x in rows:
        reasons[x["reason"]] = reasons.get(x["reason"], 0) + 1
    top = "  ".join(f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:4])
    print(f"  {label:<16s} n={len(rows):4d}  WR={len(wins)/len(rs)*100:5.1f}%  "
          f"exp={exp:+.3f}R  PF={pf:5.2f}  bars={st.mean(x['bars'] for x in rows):4.1f}  {top}")
    return {"n": len(rows), "wr": len(wins) / len(rs) * 100, "exp": exp, "pf": pf}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--index", default="ALL")
    ap.add_argument("--t1", type=float, default=1.0)
    ap.add_argument("--t2", type=float, default=2.0)
    ap.add_argument("--trail", type=float, default=1.5)
    ap.add_argument("--be", type=float, default=99.0)
    args = ap.parse_args()

    names = list(INDICES) if args.index.upper() == "ALL" else [args.index.upper()]
    all_rows: dict[str, list[dict]] = {}

    for name in names:
        d = load(INDICES[name], args.interval)
        if d is None:
            print(f"{name}: no data")
            continue
        d = add_indicators(d)
        sigs = (find_sweeps(d) + find_orb(d) + find_macd_mtf(d) + find_mean_rev(d))
        sigs = [s for s in sigs if tradeable(d, s)]

        print(f"\n{name}  ({len(d)} bars, {len(set(d.index.date))} sessions, "
              f"{len(sigs)} raw signals)")
        per: dict[str, list[dict]] = {}
        for s in sigs:
            out = simulate(d, s, t1_r=args.t1, t2_r=args.t2,
                           trail_atr=args.trail, be_at_r=args.be)
            if out:
                per.setdefault(out["strategy"], []).append(out)
                all_rows.setdefault(out["strategy"], []).append(out)
        for strat in sorted(per):
            report(per[strat], strat)

    print("\n" + "=" * 96)
    print(f"ALL INDICES COMBINED   (T1 {args.t1}R, T2 {args.t2}R, "
          f"trail {args.trail} ATR, BE {'off' if args.be > 50 else args.be})")
    print("=" * 96)
    claims = {"MACD_MTF": 69, "ORB": 62, "MEAN_REV": 58}
    summary = {}
    for strat in sorted(all_rows):
        s = report(all_rows[strat], strat)
        if s:
            summary[strat] = s
    print("-" * 96)
    print("Claimed vs measured win rate (_STRATEGY_BASE_WIN in fno_entry_service):")
    for strat, claimed in claims.items():
        if strat in summary:
            got = summary[strat]["wr"]
            print(f"  {strat:<12s} claimed {claimed}%   measured {got:.1f}%   "
                  f"({got - claimed:+.1f}pp)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
