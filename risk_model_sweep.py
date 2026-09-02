"""
Parameter sweep — find exit geometry that fits the actual entry edge
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The edge test showed the signal edge is small (+0.12% over baseline at 10
days) and concentrated in MEAN_REV / MOMENTUM / 52WK_BREAK. MULTI_TF fires
72% of the time and returns less than buying on a random day, so it is
excluded here rather than tuned.

A small edge cannot pay for a wide stop and a distant target. This sweep
finds the stop/target/trail combination that the edge can actually support,
scored on expectancy in R (the only measure that is comparable across
different stop widths).

Run:  python risk_model_sweep.py [--years 2] [--limit 25] [--types MOMENTUM,MEAN_REV]
"""

from __future__ import annotations

import argparse
import itertools
import logging
import sys
import warnings

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import risk_model as rm
import risk_model_backtest as bt
import swing_service as sv

# Entry types worth trading, from the edge test. MULTI_TF is excluded.
GOOD_TYPES = ("MEAN_REV", "MOMENTUM", "52WK_BREAK")


def collect_entries(hist: dict, allowed: tuple[str, ...]) -> list[dict]:
    """Every qualifying entry with the context needed to re-simulate exits.

    High/Low are pulled out as numpy arrays once per symbol. Indexing a pandas
    frame per bar made a full sweep take hours for no benefit.
    """
    entries = []
    for sym, df in hist.items():
        highs = df["High"].to_numpy(dtype=float)
        lows = df["Low"].to_numpy(dtype=float)
        closes = df["Close"].to_numpy(dtype=float)
        for i in range(260, len(df) - 2):
            fired, etype, score = bt.entry_signal(df, i)
            if not fired or etype not in allowed:
                continue
            row = df.iloc[i]
            atr = float(row.get("ATR", 0))
            entry = float(closes[i])
            if not atr or not entry:
                continue
            entries.append({
                "highs": highs, "lows": lows, "closes": closes, "n": len(df),
                "i": i, "entry": entry, "atr": atr,
                "atr_pct": float(row.get("ATR_PCT", 0)),
                "swing_low": float(row.get("SWING_LOW_10", 0)) or None,
                "etype": etype, "score": score,
            })
    return entries


def simulate_fast(e: dict, profile: rm.Profile) -> dict | None:
    """simulate_new over numpy arrays. Same rules, no pandas in the hot loop."""
    levels = rm.compute_levels(e["entry"], e["atr"], profile, e["swing_low"])
    if levels is None:
        return None

    entry, atr = e["entry"], e["atr"]
    r = levels.r_value
    frac = profile.partial_at_t1
    highs, lows, closes = e["highs"], e["lows"], e["closes"]
    i, n = e["i"], e["n"]

    stop = levels.stop
    peak = entry
    t1_hit = False
    be_done = False
    booked_r = 0.0
    remaining = 1.0

    last = min(i + profile.time_stop_days + 1, n)
    for j in range(i + 1, last):
        hi = highs[j]
        lo = lows[j]

        if lo <= stop:
            reason = "TRAIL" if t1_hit else ("BE" if be_done else "SL")
            return {"reason": reason, "days": j - i,
                    "r": booked_r + remaining * (stop - entry) / r}
        if hi >= levels.t2:
            return {"reason": "T2", "days": j - i,
                    "r": booked_r + remaining * profile.t2_r}

        if not t1_hit and hi >= levels.t1 and frac > 0:
            booked_r += frac * profile.t1_r
            remaining -= frac

        if hi > peak:
            peak = hi
        if not be_done and hi >= entry + profile.breakeven_at_r * r:
            be_done = True
            if entry > stop:
                stop = entry
        if not t1_hit and hi >= entry + profile.t1_r * r:
            t1_hit = True
        if t1_hit:
            chandelier = peak - profile.trail_atr * atr
            if chandelier > stop:
                stop = chandelier
        if stop > peak:
            stop = peak

    j = min(i + profile.time_stop_days, n - 1)
    return {"reason": "TIME", "days": j - i,
            "r": booked_r + remaining * (closes[j] - entry) / r}


def run_combo(entries: list[dict], profile: rm.Profile) -> dict:
    """Simulate every entry under one profile; report expectancy in R."""
    rs, days, reasons = [], [], {}
    for e in entries:
        if not (profile.min_atr_pct <= e["atr_pct"] <= profile.max_atr_pct):
            continue
        out = simulate_fast(e, profile)
        if out is None:
            continue
        rs.append(out["r"])
        days.append(out["days"])
        reasons[out["reason"]] = reasons.get(out["reason"], 0) + 1

    if len(rs) < 30:
        return {"n": len(rs), "exp": -99.0}

    wins = [r for r in rs if r > 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(r for r in rs if r <= 0))
    return {
        "n": len(rs),
        "exp": sum(rs) / len(rs),          # expectancy in R — the objective
        "wr": len(wins) / len(rs) * 100,
        "pf": (gross_win / gross_loss) if gross_loss else 99.0,
        "total_r": sum(rs),
        "hold": sum(days) / len(days),
        "reasons": reasons,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=2)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--types", type=str, default=",".join(GOOD_TYPES))
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    allowed = tuple(t.strip().upper() for t in args.types.split(",") if t.strip())
    universe = sv.NSE200[: args.limit] if args.limit else sv.NSE200

    print(f"Loading {len(universe)} symbols, {args.years}y...", flush=True)
    hist = bt.load_history(universe, f"{args.years}y")
    print(f"Collecting entries for {allowed}...", flush=True)
    entries = collect_entries(hist, allowed)
    print(f"Entries: {len(entries)}\n", flush=True)
    if not entries:
        print("No entries — nothing to sweep.")
        return 1

    grid = {
        "stop_atr": [1.0, 1.5, 2.0, 2.5],
        "t1_r": [0.75, 1.0, 1.5, 2.0],
        "t2_r": [2.0, 3.0, 4.0],
        "trail_atr": [1.0, 1.5, 2.5],
        "breakeven_at_r": [0.5, 1.0, 99.0],   # 99 = breakeven disabled
        "time_stop_days": [8, 15, 25],
        "partial_at_t1": [0.0, 0.5],
    }
    keys = list(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))
    print(f"Testing {len(combos)} combinations...", flush=True)

    results = []
    for n, values in enumerate(combos, 1):
        cfg = dict(zip(keys, values))
        if cfg["t2_r"] <= cfg["t1_r"]:
            continue
        profile = rm.Profile(
            name="SWEEP",
            stop_atr=cfg["stop_atr"],
            t1_r=cfg["t1_r"],
            t2_r=cfg["t2_r"],
            trail_atr=cfg["trail_atr"],
            breakeven_at_r=cfg["breakeven_at_r"],
            time_stop_days=cfg["time_stop_days"],
            min_score=0,
            partial_at_t1=cfg["partial_at_t1"],
            max_atr_pct=0.06,
            min_atr_pct=0.008,
        )
        r = run_combo(entries, profile)
        if r["exp"] > -90:
            results.append((r, cfg))
        if n % 200 == 0:
            print(f"  {n}/{len(combos)}", flush=True)

    results.sort(key=lambda x: -x[0]["exp"])
    print(f"\nTOP {args.top} BY EXPECTANCY (R per trade)")
    print("=" * 118)
    hdr = (f"{'exp/R':>7s} {'WR%':>6s} {'PF':>5s} {'n':>5s} {'hold':>5s} | "
           f"{'stop':>5s} {'T1':>5s} {'T2':>4s} {'trail':>5s} {'BE':>5s} {'time':>5s} {'part':>5s}")
    print(hdr)
    for r, c in results[: args.top]:
        be = "off" if c["breakeven_at_r"] > 50 else f"{c['breakeven_at_r']:.1f}"
        print(f"{r['exp']:+7.3f} {r['wr']:6.1f} {r['pf']:5.2f} {r['n']:5d} {r['hold']:5.1f} | "
              f"{c['stop_atr']:5.1f} {c['t1_r']:5.2f} {c['t2_r']:4.1f} "
              f"{c['trail_atr']:5.1f} {be:>5s} {c['time_stop_days']:5d} {c['partial_at_t1']:5.1f}")
    print("=" * 118)

    if results:
        best_r, best_c = results[0]
        print("\nBest combination:")
        for k, v in best_c.items():
            print(f"  {k:18s} {v}")
        print(f"\n  expectancy {best_r['exp']:+.3f}R  win rate {best_r['wr']:.1f}%  "
              f"PF {best_r['pf']:.2f}  over {best_r['n']} trades  "
              f"avg hold {best_r['hold']:.1f}d")
        print(f"  exits: " + "  ".join(f"{k}={v}" for k, v in
                                       sorted(best_r["reasons"].items(), key=lambda kv: -kv[1])))
        if best_r["exp"] <= 0:
            print("\n  WARNING: even the best combination has negative expectancy.")
            print("  Exit tuning cannot rescue these entries — the signal needs work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
