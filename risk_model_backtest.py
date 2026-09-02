"""
Risk-model A/B backtest — old fixed-percent exits vs new ATR/R exits
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Entries are generated once and fed to BOTH exit models, so any difference in
the results comes from exit management alone — which is the thing being
changed. Comparing two strategies with different entries would prove nothing.

OLD: stop at a flat 5% of entry, targets at +8%/+12%, and after T1 a flat 1%
     trailing stop (0.43 ATR against the universe's 2.30% median ATR).
NEW: stop at profile.stop_atr ATRs or just under the swing low, whichever is
     further; targets as R-multiples; stop to breakeven at breakeven_at_r,
     then a chandelier trail of profile.trail_atr ATRs from the peak.

Run:  python risk_model_backtest.py [--years 2] [--limit 0]
"""

from __future__ import annotations

import argparse
import logging
import os
import pickle
import sys
import warnings
from datetime import datetime

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import pandas as pd

import risk_model as rm
import swing_service as sv

CACHE = ".risk_bt_cache.pkl"

# Old model's hard-coded geometry, kept here so the comparison is explicit.
OLD_SL_PCT = 0.05
OLD_T1_PCT = 0.08
OLD_T2_PCT = 0.12
OLD_TRAIL_PCT = 0.01
OLD_TIME_STOP = 25


def load_history(symbols: list[str], period: str) -> dict[str, pd.DataFrame]:
    """Download once, cache to disk — the sweep is re-run often."""
    cache: dict[str, pd.DataFrame] = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as fh:
                cache = pickle.load(fh)
        except Exception:
            cache = {}

    out: dict[str, pd.DataFrame] = {}
    fetched = 0
    for i, sym in enumerate(symbols, 1):
        key = f"{sym}:{period}"
        if key in cache:
            out[sym] = cache[key]
            continue
        df = sv.fetch_history(sym, period=period, interval="1d")
        if df is not None and len(df) > 260:
            df = sv.compute_indicators(df)
            cache[key] = df
            out[sym] = df
            fetched += 1
        if i % 25 == 0:
            print(f"  loaded {i}/{len(symbols)} ({fetched} fetched)", flush=True)

    try:
        with open(CACHE, "wb") as fh:
            pickle.dump(cache, fh)
    except Exception:
        pass
    return out


def entry_signal(df: pd.DataFrame, i: int) -> tuple[bool, str, float]:
    """Faithful re-implementation of the live scanner's entry rules.

    score_stock() recomputes indicators over the whole frame on every call,
    which is far too slow to run once per bar per symbol. These are the same
    conditions read off a frame whose indicators were computed once.
    Returns (fired, entry_type, score).
    """
    row = df.iloc[i]
    prev = df.iloc[i - 1]

    price = float(row["Close"])
    ema200 = float(row.get("EMA200", price))
    ema50 = float(row.get("EMA50", price))
    ema9 = float(row.get("EMA9", price))
    ema21 = float(row.get("EMA21", price))
    rsi = float(row.get("RSI", 50))
    prev_rsi = float(prev.get("RSI", 50))
    vol_ratio = float(row.get("VOL_RATIO", 1))
    atr_pct = float(row.get("ATR_PCT", 0))
    bb_pct = float(row.get("BB_PCT", 0.5))
    mom10 = float(row.get("MOM_10", 0))
    mom20 = float(row.get("MOM_20", 0))
    dist_high = float(row.get("DIST_FROM_HIGH", 1))

    # Mandatory: uptrend, and not mid-crash.
    if price < ema200:
        return False, "", 0.0
    if i >= 3:
        r3 = (price - float(df.iloc[i - 3]["Close"])) / float(df.iloc[i - 3]["Close"])
        if r3 < -0.05:
            return False, "", 0.0

    recent_high = float(df["High"].iloc[max(0, i - 6):i].max()) if i >= 2 else price

    # MOMENTUM
    if (price >= ema50 and ema9 > ema21 and mom10 > 0.02 and mom20 > 0.01
            and vol_ratio > 1.3 and 45 < rsi < 75 and price >= recent_high * 0.97):
        score = 80.0
        if vol_ratio > 2.0:
            score += 10
        elif vol_ratio > 1.5:
            score += 5
        if 0.01 < atr_pct < 0.03:
            score += 5
        return True, "MOMENTUM", min(score, 100.0)

    # 52-WEEK BREAKOUT
    if dist_high < 0.02 and vol_ratio > 1.3 and rsi < 80 and mom20 > 0.01:
        score = 75.0 + (15 if dist_high < 0.005 else 0)
        if vol_ratio > 2.0:
            score += 10
        return True, "52WK_BREAK", min(score, 100.0)

    # MULTI-TIMEFRAME CONFLUENCE
    signals = 0
    if price >= ema50:
        signals += 1
    if ema9 > ema21 > ema50:
        signals += 1
    if 45 < rsi < 65:
        signals += 1
    if rsi > prev_rsi:
        signals += 1
    if bb_pct < 0.5:
        signals += 1
    if vol_ratio > 1.0:
        signals += 1
    if mom10 > 0:
        signals += 1
    if 0.01 < atr_pct < 0.03:
        signals += 1
    if signals >= 6:
        score = 60.0 + (signals - 6) * 8
        if 0.01 < atr_pct < 0.03:
            score += 5
        return True, "MULTI_TF", min(score, 100.0)

    # MEAN REVERSION
    rsi_was_low = any(float(df.iloc[j].get("RSI", 50)) < 42
                      for j in range(max(0, i - 6), i))
    if (rsi_was_low and rsi > prev_rsi and rsi < 55 and bb_pct < 0.40
            and vol_ratio > 0.8):
        return True, "MEAN_REV", 65.0

    return False, "", 0.0


def simulate_old(df: pd.DataFrame, i: int, entry: float) -> dict | None:
    """Flat 5% stop, +8%/+12% targets, 1% trail after T1."""
    sl = entry * (1 - OLD_SL_PCT)
    t1 = entry * (1 + OLD_T1_PCT)
    t2 = entry * (1 + OLD_T2_PCT)
    peak = entry
    t1_hit = False

    for j in range(i + 1, min(i + OLD_TIME_STOP + 1, len(df))):
        hi = float(df.iloc[j]["High"])
        lo = float(df.iloc[j]["Low"])
        peak = max(peak, hi)

        if lo <= sl:
            return {"exit": sl, "reason": "SL", "days": j - i}
        if hi >= t2:
            return {"exit": t2, "reason": "T2", "days": j - i}
        if not t1_hit and hi >= t1:
            t1_hit = True
            continue
        if t1_hit:
            trail = peak * (1 - OLD_TRAIL_PCT)
            if lo <= trail:
                return {"exit": trail, "reason": "TRAIL", "days": j - i}

    j = min(i + OLD_TIME_STOP, len(df) - 1)
    return {"exit": float(df.iloc[j]["Close"]), "reason": "TIME", "days": j - i}


def simulate_new(df: pd.DataFrame, i: int, entry: float,
                 profile: rm.Profile, atr: float, swing_low: float) -> dict | None:
    """ATR/structure stop, R-multiple targets, breakeven then chandelier.

    Books ``profile.partial_at_t1`` of the position at T1 and trails the rest.
    Without the partial, a winner that reaches T1 but never reaches T2 gives
    everything back to the trail or the time stop — which is what the first
    A/B run showed: T2 hits collapsed and TIME exits ballooned.
    """
    levels = rm.compute_levels(entry, atr, profile, swing_low)
    if levels is None:
        return None

    r = levels.r_value
    frac = profile.partial_at_t1
    state = rm.TradeState(stop=levels.stop, peak=entry)
    booked_r = 0.0          # R already banked from the partial
    remaining = 1.0

    for j in range(i + 1, min(i + profile.time_stop_days + 1, len(df))):
        hi = float(df.iloc[j]["High"])
        lo = float(df.iloc[j]["Low"])

        if lo <= state.stop:
            reason = "TRAIL" if state.t1_hit else ("BE" if state.breakeven_done else "SL")
            tail_r = (state.stop - entry) / r
            return {"reason": reason, "days": j - i,
                    "r": booked_r + remaining * tail_r}

        if hi >= levels.t2:
            return {"reason": "T2", "days": j - i,
                    "r": booked_r + remaining * profile.t2_r}

        # Book the partial the first time T1 trades.
        if not state.t1_hit and hi >= levels.t1 and frac > 0:
            booked_r += frac * profile.t1_r
            remaining -= frac

        state = rm.update_stop(state, hi, lo, entry, r, atr, profile)

    j = min(i + profile.time_stop_days, len(df) - 1)
    px = float(df.iloc[j]["Close"])
    return {"reason": "TIME", "days": j - i,
            "r": booked_r + remaining * (px - entry) / r}


def stats(trades: list[dict], label: str) -> dict:
    if not trades:
        return {"label": label, "n": 0}
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    gross_win = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "label": label,
        "n": len(trades),
        "wr": len(wins) / len(trades) * 100,
        "avg": sum(t["pnl_pct"] for t in trades) / len(trades),
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "pf": (gross_win / gross_loss) if gross_loss else float("inf"),
        "total": sum(t["pnl_pct"] for t in trades),
        "hold": sum(t["days"] for t in trades) / len(trades),
        "reasons": reasons,
    }


def show(s: dict) -> None:
    if not s["n"]:
        print(f"  {s['label']}: no trades")
        return
    print(f"  {s['label']:<26s} trades={s['n']:4d}  WR={s['wr']:5.1f}%  "
          f"avg={s['avg']:+6.2f}%  win={s['avg_win']:+6.2f}%  loss={s['avg_loss']:+6.2f}%  "
          f"PF={s['pf']:5.2f}  total={s['total']:+8.1f}%  hold={s['hold']:4.1f}d")
    order = sorted(s["reasons"].items(), key=lambda kv: -kv[1])
    print(f"  {'':26s} exits: " + "  ".join(f"{k}={v}" for k, v in order))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0, help="cap universe size (0 = all)")
    args = ap.parse_args()

    universe = sv.NSE200[: args.limit] if args.limit else sv.NSE200
    period = f"{args.years}y"
    print(f"Loading {len(universe)} symbols, {period} daily bars...", flush=True)
    hist = load_history(universe, period)
    print(f"Usable symbols: {len(hist)}\n", flush=True)

    old_trades: list[dict] = []
    new_trades: list[dict] = []
    new_by_profile: dict[str, list[dict]] = {"LONG": [], "SCALP": []}
    skipped = 0

    for sym, df in hist.items():
        i = 260                      # warm up the 252-day high
        while i < len(df) - 2:
            fired, etype, score = entry_signal(df, i)
            if not fired:
                i += 1
                continue

            row = df.iloc[i]
            entry = float(row["Close"])
            atr = float(row.get("ATR", 0))
            atr_pct = float(row.get("ATR_PCT", 0))
            swing_low = float(row.get("SWING_LOW_10", 0)) or None
            if not atr or not entry:
                i += 1
                continue

            profile = rm.pick_profile(score, etype, atr_pct)
            if profile is None:
                skipped += 1
                i += 1
                continue

            levels = rm.compute_levels(entry, atr, profile, swing_low)
            if levels is None:
                i += 1
                continue

            o = simulate_old(df, i, entry)
            n = simulate_new(df, i, entry, profile, atr, swing_low)
            if not o or not n:
                i += 1
                continue

            o_rec = {"pnl_pct": (o["exit"] - entry) / entry * 100,
                     "reason": o["reason"], "days": o["days"]}
            # A partially-booked position has no single exit price, so value it
            # from the blended R multiple times the stop distance in percent.
            r_mult = n.get("r", 0.0)
            n_rec = {"pnl_pct": r_mult * levels.r_pct,
                     "reason": n["reason"], "days": n["days"], "r": r_mult}
            old_trades.append(o_rec)
            new_trades.append(n_rec)
            new_by_profile[profile.name].append(n_rec)

            # Skip ahead past the longer of the two holds so trades do not overlap.
            i += max(o["days"], n["days"], 1)

    print(f"Entries taken: {len(old_trades)}   (skipped by risk filter: {skipped})\n")
    print("SAME ENTRIES, DIFFERENT EXIT MANAGEMENT")
    print("=" * 108)
    so = stats(old_trades, "OLD fixed 5%/8%/12%")
    sn = stats(new_trades, "NEW ATR + R-multiple")
    show(so)
    print()
    show(sn)
    print()
    for name in ("LONG", "SCALP"):
        show(stats(new_by_profile[name], f"NEW {name} only"))
    print("=" * 108)

    if so["n"] and sn["n"]:
        print(f"\nDelta: win rate {sn['wr'] - so['wr']:+.1f}pp   "
              f"avg/trade {sn['avg'] - so['avg']:+.2f}pp   "
              f"profit factor {sn['pf'] - so['pf']:+.2f}   "
              f"total {sn['total'] - so['total']:+.1f}pp")
        rs = [t["r"] for t in new_trades if "r" in t]
        if rs:
            print(f"Expectancy (new): {sum(rs) / len(rs):+.3f}R per trade")
    return 0


if __name__ == "__main__":
    sys.exit(main())
