"""
Tune on one half of the universe, validate on the other
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Picking parameters and reporting their score on the same data measures how
well the grid memorised that data, not whether the settings work. Symbols are
split: the sweep only sees the training half, and the chosen settings are then
scored once on symbols the sweep never touched.

SCALP and LONG are tuned separately because they are different bets — a scalp
is judged on quick, frequent, high-hit-rate trades, a positional hold on
letting a winner run.

Run:  python risk_model_validate.py [--years 2]
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
import risk_model_sweep as sw
import swing_service as sv


def make_profile(name: str, cfg: dict, max_atr: float = 0.06) -> rm.Profile:
    return rm.Profile(
        name=name,
        stop_atr=cfg["stop_atr"],
        t1_r=cfg["t1_r"],
        t2_r=cfg["t2_r"],
        trail_atr=cfg["trail_atr"],
        breakeven_at_r=cfg["breakeven_at_r"],
        time_stop_days=cfg["time_stop_days"],
        min_score=0,
        partial_at_t1=cfg["partial_at_t1"],
        max_atr_pct=max_atr,
        min_atr_pct=0.008,
    )


def sweep(entries: list[dict], grid: dict, min_trades: int = 60) -> list[tuple]:
    keys = list(grid)
    out = []
    for values in itertools.product(*(grid[k] for k in keys)):
        cfg = dict(zip(keys, values))
        if cfg["t2_r"] <= cfg["t1_r"]:
            continue
        r = sw.run_combo(entries, make_profile("SWEEP", cfg))
        if r["exp"] > -90 and r["n"] >= min_trades:
            out.append((r, cfg))
    out.sort(key=lambda x: -x[0]["exp"])
    return out


def line(tag: str, r: dict) -> str:
    return (f"  {tag:<22s} exp={r['exp']:+.3f}R  WR={r['wr']:5.1f}%  PF={r['pf']:5.2f}  "
            f"n={r['n']:4d}  hold={r['hold']:4.1f}d")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=2)
    args = ap.parse_args()

    universe = sv.NSE200
    split = len(universe) // 2
    train_syms, test_syms = universe[:split], universe[split:]

    print(f"Loading {len(universe)} symbols ({args.years}y)...", flush=True)
    hist = bt.load_history(universe, f"{args.years}y")
    train = {s: d for s, d in hist.items() if s in set(train_syms)}
    test = {s: d for s, d in hist.items() if s in set(test_syms)}
    print(f"train symbols {len(train)} | test symbols {len(test)}", flush=True)

    e_train = sw.collect_entries(train, sw.GOOD_TYPES)
    e_test = sw.collect_entries(test, sw.GOOD_TYPES)
    print(f"train entries {len(e_train)} | test entries {len(e_test)}\n", flush=True)

    common = {
        "stop_atr": [1.0, 1.5, 2.0, 2.5, 3.0],
        "t1_r": [0.75, 1.0, 1.5, 2.0],
        "t2_r": [2.0, 3.0, 4.0],
        "trail_atr": [1.0, 1.5, 2.0, 2.5],
        "breakeven_at_r": [0.5, 1.0, 99.0],
        "partial_at_t1": [0.0, 0.5],
    }

    specs = {
        "SCALP": dict(common, time_stop_days=[3, 5, 8]),
        "LONG": dict(common, time_stop_days=[15, 25, 40]),
    }

    chosen: dict[str, dict] = {}
    for name, grid in specs.items():
        print(f"Sweeping {name} ({len(list(itertools.product(*grid.values())))} combos)...",
              flush=True)
        ranked = sweep(e_train, grid)
        if not ranked:
            print(f"  no {name} combination cleared the trade minimum")
            continue

        # Among the near-best by expectancy, prefer the highest win rate: the
        # brief was accuracy, and these settings are statistically tied.
        best_exp = ranked[0][0]["exp"]
        near = [(r, c) for r, c in ranked if r["exp"] >= best_exp - 0.010]
        near.sort(key=lambda x: -x[0]["wr"])
        r_tr, cfg = near[0]
        chosen[name] = cfg

        print(f"  {len(near)} combos within 0.01R of best; picked highest win rate")
        print(line(f"{name} IN-SAMPLE", r_tr))
        r_te = sw.run_combo(e_test, make_profile(name, cfg))
        if r_te["exp"] > -90:
            print(line(f"{name} OUT-OF-SAMPLE", r_te))
        else:
            print(f"  {name} OUT-OF-SAMPLE: too few trades")
        print(f"    stop {cfg['stop_atr']} ATR | T1 {cfg['t1_r']}R | T2 {cfg['t2_r']}R | "
              f"trail {cfg['trail_atr']} ATR | "
              f"BE {'off' if cfg['breakeven_at_r'] > 50 else cfg['breakeven_at_r']} | "
              f"{cfg['time_stop_days']}d | partial {cfg['partial_at_t1']}")
        print(f"    exits: " + "  ".join(f"{k}={v}" for k, v in
                                         sorted(r_te.get("reasons", {}).items(),
                                                key=lambda kv: -kv[1])))
        print()

    print("=" * 96)
    print("CURRENT SHIPPED PROFILES, scored out-of-sample:")
    for name, prof in (("LONG", rm.LONG), ("SCALP", rm.SCALP)):
        r = sw.run_combo(e_test, prof)
        if r["exp"] > -90:
            print(line(f"{name} (shipped)", r))
    print("=" * 96)

    if chosen:
        print("\nPaste into risk_model.py:")
        for name, cfg in chosen.items():
            print(f"  {name}: stop_atr={cfg['stop_atr']}, t1_r={cfg['t1_r']}, "
                  f"t2_r={cfg['t2_r']}, trail_atr={cfg['trail_atr']}, "
                  f"breakeven_at_r={cfg['breakeven_at_r']}, "
                  f"time_stop_days={cfg['time_stop_days']}, "
                  f"partial_at_t1={cfg['partial_at_t1']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
