"""
Which liquidity pools are worth sweeping?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The shipped sweep only watches minor intraday swing lows. That is the weakest
pool on the chart: a pivot formed forty minutes ago has a handful of stops
under it. The levels everyone can see — yesterday's low, the opening range,
the session low, a double bottom — hold far more resting orders, so a sweep
through them should flush more and reverse harder.

This measures each pool type separately on the same engine and the same
train/test split, so "more alerts" can be judged against "better alerts"
rather than assumed.

Pools tested:
  SWING      a confirmed intraday pivot low          (what ships today)
  PDL        previous day's low
  ORL        low of the first 30 minutes
  SESSION    the running low of the day so far
  EQUAL      two pivot lows within 0.25 ATR of each other — a double bottom,
             the textbook liquidity magnet

Run:  python fno_sweep_levels.py [--interval 5m]
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

T1_R, T2_R, TRAIL_ATR, BE_R = 0.75, 1.25, 1.5, 99.0
MIN_PIERCE_ATR = 0.10
MAX_RECLAIM = 3


def build_levels(d) -> list[dict]:
    """Every (bar, level, kind) pair a sweep could later trigger against."""
    highs = d["High"].to_numpy(dtype=float)
    lows = d["Low"].to_numpy(dtype=float)
    atr = d["ATR"].to_numpy(dtype=float)
    dates = d.index.date
    n = len(d)

    _, lo_piv = fb.swing_points(d, 3, 3)

    # previous day's low and the opening-range low, per session
    day_low: dict = {}
    orl: dict = {}
    for day, g in d.groupby(dates):
        day_low[day] = float(g["Low"].min())
        orl[day] = float(g["Low"].iloc[:6].min()) if len(g) >= 6 else None
    ordered_days = sorted(day_low)
    prev_low = {day: day_low[ordered_days[k - 1]] if k else None
                for k, day in enumerate(ordered_days)}

    out = []
    for i in range(60, n):
        a = atr[i]
        if not a or np.isnan(a):
            continue
        today = dates[i]

        # running low of the session, excluding the current bar
        same = [j for j in range(i - 1, max(0, i - 80), -1) if dates[j] == today]
        session_low = min((lows[j] for j in same), default=None)

        cands: list[tuple[str, float]] = []

        # nearest confirmed pivot low earlier today
        piv = []
        for j in range(i - 4, max(0, i - 60), -1):
            if dates[j] != today:
                break
            if lo_piv[j]:
                piv.append(float(lows[j]))
                if len(piv) >= 4:
                    break
        if piv:
            cands.append(("SWING", max(piv)))
            # two pivots within a quarter ATR of each other = equal lows
            for x in range(len(piv)):
                for y in range(x + 1, len(piv)):
                    if abs(piv[x] - piv[y]) <= 0.25 * a:
                        cands.append(("EQUAL", max(piv[x], piv[y])))
                        break
                else:
                    continue
                break

        if prev_low.get(today):
            cands.append(("PDL", prev_low[today]))
        if orl.get(today) is not None and i - int(np.argmax(dates == today)) > 6:
            cands.append(("ORL", orl[today]))
        if session_low is not None:
            cands.append(("SESSION", session_low))

        for kind, lvl in cands:
            if lvl and lvl < highs[i]:
                out.append({"i": i, "kind": kind, "level": float(lvl), "atr": a})
    return out


def sweeps_for(d, levels: list[dict]) -> list[dict]:
    """Turn level candidates into sweep signals: pierce, then reclaim."""
    lows = d["Low"].to_numpy(dtype=float)
    closes = d["Close"].to_numpy(dtype=float)
    dates = d.index.date
    n = len(d)

    seen: set = set()
    out = []
    for c in levels:
        i, lvl, a = c["i"], c["level"], c["atr"]
        pierce = lvl - lows[i]
        if pierce < MIN_PIERCE_ATR * a or closes[i] >= lvl:
            continue

        for k in range(i, min(i + MAX_RECLAIM + 1, n)):
            if dates[k] != dates[i]:
                break
            if closes[k] > lvl:
                key = (c["kind"], k)
                if key in seen:
                    break
                seen.add(key)
                stop = float(min(lows[i:k + 1])) - 0.1 * a
                entry = float(closes[k])
                r = entry - stop
                if r > 0 and 0.3 * a <= r <= 6 * a:
                    out.append({
                        "i": k, "dir": 1, "strategy": c["kind"],
                        "entry": entry, "stop": stop, "level": lvl,
                        "pierce_atr": pierce / a, "reclaim_bars": k - i,
                    })
                break
            if lows[k] < min(lows[i:k + 1]):
                break
    return out


def score(rows, m=25):
    if len(rows) < m:
        return None
    r = [x["r"] for x in rows]
    w = [x for x in r if x > 0]
    gl = abs(sum(x for x in r if x <= 0))
    return {"n": len(r), "wr": len(w) / len(r) * 100, "exp": sum(r) / len(r),
            "pf": (sum(w) / gl) if gl else 99.0, "tot": sum(r)}


def fmt(s):
    return "     too few" if not s else (
        f"n={s['n']:4d} WR={s['wr']:5.1f}% exp={s['exp']:+.3f}R "
        f"PF={s['pf']:4.2f} tot={s['tot']:+6.1f}R")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="5m")
    args = ap.parse_args()

    rows: list[dict] = []
    for name, sym in fb.INDICES.items():
        d = fb.load(sym, args.interval)
        if d is None:
            continue
        d = fb.add_indicators(d)
        sigs = [s for s in sweeps_for(d, build_levels(d)) if fb.tradeable(d, s)]
        for s in sigs:
            o = fb.simulate(d, s, t1_r=T1_R, t2_r=T2_R,
                            trail_atr=TRAIL_ATR, be_at_r=BE_R)
            if o:
                o["index"] = name
                o["date"] = d.index[s["i"]].date()
                rows.append(o)
        print(f"{name}: {len(sigs)} sweeps", flush=True)

    dates = sorted({r["date"] for r in rows})
    cut = dates[int(len(dates) * 0.65)]
    print(f"\nsplit at {cut} — train {len([d for d in dates if d <= cut])} sessions, "
          f"test {len([d for d in dates if d > cut])}\n")

    print("BY LIQUIDITY POOL  (T1 0.75R, T2 1.25R, trail 1.5 ATR)")
    print("=" * 104)
    order = ["SWING", "EQUAL", "PDL", "ORL", "SESSION"]
    keep = []
    for kind in order:
        sel = [r for r in rows if r["strategy"] == kind]
        tr = [r for r in sel if r["date"] <= cut]
        te = [r for r in sel if r["date"] > cut]
        s_all, s_te = score(sel), score(te, m=20)
        print(f"\n  {kind}")
        print(f"    all   {fmt(s_all)}")
        print(f"    train {fmt(score(tr))}")
        print(f"    TEST  {fmt(s_te)}")
        if s_all and s_te and s_all["pf"] > 1.05 and s_te["pf"] > 1.0:
            keep.append(kind)

    print("\n" + "=" * 104)
    print(f"Pools positive both overall and out-of-sample: {keep or 'none'}")

    if keep:
        sel = [r for r in rows if r["strategy"] in keep]
        # One alert per bar: if several pools are swept at once that is one
        # trade, not several. Rank by pierce depth and keep the deepest.
        best: dict = {}
        for r in sel:
            k = (r["index"], r["date"], r["i"])
            if k not in best or r["pierce_atr"] > best[k]["pierce_atr"]:
                best[k] = r
        ded = list(best.values())
        print("\nCOMBINED, deduplicated to one alert per bar")
        print(f"  all   {fmt(score(ded))}")
        print(f"  train {fmt(score([r for r in ded if r['date'] <= cut]))}")
        print(f"  TEST  {fmt(score([r for r in ded if r['date'] > cut], m=20))}")
        for nm in fb.INDICES:
            print(f"    {nm:10s} {fmt(score([r for r in ded if r['index'] == nm], m=15))}")

        print("\n  alerts per session, per index:")
        n_days = len(dates)
        for nm in fb.INDICES:
            c = len([r for r in ded if r["index"] == nm])
            print(f"    {nm:10s} {c / n_days:4.1f}")

        print("\n  quality ranking — deeper pierce first:")
        for lo, hi in ((0.10, 0.25), (0.25, 0.50), (0.50, 1.00), (1.00, 99)):
            band = [r for r in ded if lo <= r["pierce_atr"] < hi]
            print(f"    pierce {lo:.2f}-{hi:.2f} ATR  {fmt(score(band, m=20))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
