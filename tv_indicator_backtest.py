"""
Do the TradingView indicators actually work?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Two of the five links carry rules precise enough to reimplement:

  BOOMING_BULL   open-source. "First 15 min candle high-low must be less than
                 .75%" and "next candle is crossing either high or low".
                 A narrow-opening-range breakout.

  SWAPPY_3C      closed-source but fully described. Two candles with bodies
                 >=80% of range in the same direction, then a pullback candle
                 that does not close beyond the pair's extreme. A continuation
                 pattern.

The other three cannot be tested: Black Gold gives only generic order-block
theory, IITian Trader discloses no rules at all, and the second Guardeer link
is a dead 404.

Neither published set says what stop or target to use, so both are scored on
the same R-framework already used for the sweep: stop from structure, targets
as R-multiples, and the same train/test split by date. That keeps the
comparison against the liquidity sweep honest.

Run:  python tv_indicator_backtest.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import warnings

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import numpy as np

import fno_backtest as fb


# ── Booming Bull: narrow first candle, then a break of it ────────────────────

def find_booming_bull(d, max_range_pct: float = 0.75,
                      bars_per_candle: int = 3,
                      strict_next_bar: bool = True) -> list[dict]:
    """First 15m candle of the day is narrow, next candle breaks it.

    On 5m data a "15 minute candle" is three bars, so the opening candle is
    bars 0-2 and the next candle is bars 3-5. ``strict_next_bar`` follows the
    published rule literally (only the immediately following candle may
    trigger); relaxing it lets any later bar in the session trigger, which is
    how most opening-range systems are actually traded.
    """
    out = []
    highs, lows, closes = (d["High"].to_numpy(dtype=float),
                           d["Low"].to_numpy(dtype=float),
                           d["Close"].to_numpy(dtype=float))
    atr = d["ATR"].to_numpy(dtype=float)

    for day, g in d.groupby(d.index.date):
        idx = d.index.get_indexer(g.index)
        if len(idx) < bars_per_candle * 3:
            continue

        first = idx[:bars_per_candle]
        hi = float(highs[first].max())
        lo = float(lows[first].min())
        if lo <= 0:
            continue

        # the narrowness filter is the whole point of the setup
        rng_pct = (hi - lo) / lo * 100
        if rng_pct >= max_range_pct:
            continue

        window = (idx[bars_per_candle:bars_per_candle * 2] if strict_next_bar
                  else idx[bars_per_candle:])

        for i in window:
            a = atr[i]
            if not a or np.isnan(a):
                continue
            c = float(closes[i])
            if c > hi:
                out.append({"i": int(i), "dir": 1, "strategy": "BOOMING_BULL",
                            "entry": c, "stop": lo, "range_pct": rng_pct})
                break
            if c < lo:
                out.append({"i": int(i), "dir": -1, "strategy": "BOOMING_BULL",
                            "entry": c, "stop": hi, "range_pct": rng_pct})
                break
    return out


# ── Swappy: two conviction candles, then a shallow pullback ──────────────────

def find_swappy_3c(d, body_pct: float = 0.80) -> list[dict]:
    """Two same-direction candles with body >= body_pct of range, then a
    counter candle that does not close beyond the pair's extreme.

    The body filter is the author's proxy for conviction: a candle that closes
    at its extreme with almost no wick means one side was in control for the
    whole bar. The third candle is the pullback that must hold.
    """
    o, h, l, c = (d["Open"].to_numpy(dtype=float), d["High"].to_numpy(dtype=float),
                  d["Low"].to_numpy(dtype=float), d["Close"].to_numpy(dtype=float))
    atr = d["ATR"].to_numpy(dtype=float)
    dates = d.index.date

    rng = np.where((h - l) > 0, h - l, np.nan)
    body = np.abs(c - o) / rng
    bull = (c > o) & (body >= body_pct)
    bear = (c < o) & (body >= body_pct)

    out = []
    for i in range(2, len(d)):
        a = atr[i]
        if not a or np.isnan(a):
            continue
        # all three candles must sit in the same session
        if not (dates[i] == dates[i - 1] == dates[i - 2]):
            continue

        # bullish continuation
        if bull[i - 2] and bull[i - 1] and c[i] < o[i]:
            floor = min(l[i - 2], l[i - 1])
            if c[i] > floor:                       # pullback held
                stop = min(floor, l[i]) - 0.1 * a
                out.append({"i": i, "dir": 1, "strategy": "SWAPPY_3C",
                            "entry": float(c[i]), "stop": float(stop)})

        # bearish continuation
        elif bear[i - 2] and bear[i - 1] and c[i] > o[i]:
            ceil_ = max(h[i - 2], h[i - 1])
            if c[i] < ceil_:
                stop = max(ceil_, h[i]) + 0.1 * a
                out.append({"i": i, "dir": -1, "strategy": "SWAPPY_3C",
                            "entry": float(c[i]), "stop": float(stop)})
    return out


# ── scoring ──────────────────────────────────────────────────────────────────

def score(rows, m=25):
    if len(rows) < m:
        return None
    r = [x["r"] for x in rows]
    w = [x for x in r if x > 0]
    gl = abs(sum(x for x in r if x <= 0))
    return {"n": len(r), "wr": len(w) / len(r) * 100, "exp": sum(r) / len(r),
            "pf": (sum(w) / gl) if gl else 99.0, "tot": sum(r)}


def fmt(s):
    return "    too few trades" if not s else (
        f"n={s['n']:4d} WR={s['wr']:5.1f}% exp={s['exp']:+.3f}R "
        f"PF={s['pf']:5.2f} tot={s['tot']:+7.1f}R")


def evaluate(label, rows, cut):
    print(f"\n  {label}")
    print(f"    all   {fmt(score(rows))}")
    print(f"    train {fmt(score([r for r in rows if r['date'] <= cut]))}")
    print(f"    TEST  {fmt(score([r for r in rows if r['date'] > cut], m=15))}")
    for nm in fb.INDICES:
        sel = [r for r in rows if r["index"] == nm]
        if len(sel) >= 15:
            print(f"      {nm:10s} {fmt(score(sel, m=15))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--t1", type=float, default=0.75)
    ap.add_argument("--t2", type=float, default=1.25)
    args = ap.parse_args()

    frames = {}
    for name, sym in fb.INDICES.items():
        d = fb.load(sym, args.interval)
        if d is not None:
            frames[name] = fb.add_indicators(d)

    variants = {
        "BOOMING_BULL (strict next candle)":
            lambda d: find_booming_bull(d, strict_next_bar=True),
        "BOOMING_BULL (any bar in session)":
            lambda d: find_booming_bull(d, strict_next_bar=False),
        "SWAPPY_3C (body>=80%)":
            lambda d: find_swappy_3c(d, 0.80),
        "SWAPPY_3C (body>=90%)":
            lambda d: find_swappy_3c(d, 0.90),
    }

    collected: dict[str, list[dict]] = {k: [] for k in variants}
    for name, d in frames.items():
        for label, fn in variants.items():
            for s in fn(d):
                if not fb.tradeable(d, s):
                    continue
                o = fb.simulate(d, s, t1_r=args.t1, t2_r=args.t2,
                                trail_atr=1.5, be_at_r=99.0)
                if o:
                    o["index"] = name
                    o["date"] = d.index[s["i"]].date()
                    collected[label].append(o)

    all_dates = sorted({r["date"] for rows in collected.values() for r in rows})
    if not all_dates:
        print("no signals at all")
        return 1
    cut = all_dates[int(len(all_dates) * 0.65)]

    print(f"interval {args.interval}, targets T1 {args.t1}R / T2 {args.t2}R, "
          f"trail 1.5 ATR")
    print(f"split at {cut}  "
          f"(train {len([d for d in all_dates if d <= cut])} sessions, "
          f"test {len([d for d in all_dates if d > cut])})")
    print("=" * 96)
    for label in variants:
        evaluate(label, collected[label], cut)

    print("\n" + "=" * 96)
    print("BENCHMARK - the liquidity sweep already shipped, same framework:")
    print("    all   n= 450 WR= 50.7% exp=+0.123R PF= 1.28")
    print("    TEST  n= 165 WR= 50.3% exp=+0.124R PF= 1.28")
    print("\nAnything below PF 1.0 loses money. Anything that beats the sweep")
    print("out-of-sample is worth adding; anything that does not is noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
