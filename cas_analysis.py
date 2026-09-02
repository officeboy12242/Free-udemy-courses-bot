"""
Closing Auction Session (CAS) — does the auction print create a tradeable edge?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SEBI introduced CAS for F&O stocks on 3 August 2026. Instead of the close being
a VWAP of the last half hour, all orders from 3:15 collect into a single auction
that prints one clearing price between 3:30 and 3:35.

That print can sit away from where the stock was trading at 3:15 — the band is
±3% of the 3:00-3:15 reference VWAP. The question this measures: when the
auction drags a stock away from its 3:15 price, does that dislocation persist
or revert the next morning?

Method, using 15-minute bars:
  reference   = the 15:00 bar (covers 15:00-15:15, the reference window)
  auction     = the 15:15 bar close, which equals the official daily close
  dislocation = (auction - reference) / reference
  outcome     = next day's open gap, and next day's open-to-close move

A reverting dislocation is a fade setup: sell the stretch, buy it back at the
open. A persisting one is a momentum setup. Neither is assumed here.

The pre-CAS window (before 3 Aug) is measured the same way as a control, so
any effect can be checked against how the same statistic behaved when the
close was still a VWAP.

CAVEAT: only ~23 sessions exist since launch. Cross-sectional breadth across
147 names gives many observations, but they share one month of market regime.
Treat anything here as a hypothesis to keep measuring, not a validated edge.

Run:  python cas_analysis.py [--limit 60]
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

import pandas as pd
import yfinance as yf

import swing_service as sv

CAS_START = dt.date(2026, 8, 3)
IST = "Asia/Kolkata"


def fetch_intraday(symbol: str) -> pd.DataFrame | None:
    df = yf.download(symbol, interval="15m", period="60d",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.dropna(subset=["Close"])
    if df.empty:
        return None
    df.index = df.index.tz_convert(IST)
    return df


def day_records(symbol: str, df: pd.DataFrame) -> list[dict]:
    """One record per session: the 3:15 reference, the auction print, next day."""
    out: list[dict] = []
    by_day: dict[dt.date, pd.DataFrame] = {
        d: g for d, g in df.groupby(df.index.date)
    }
    days = sorted(by_day)

    for k, day in enumerate(days[:-1]):
        g = by_day[day]
        times = {t.strftime("%H:%M"): i for i, t in enumerate(g.index)}
        if "15:00" not in times or "15:15" not in times:
            continue

        ref_bar = g.iloc[times["15:00"]]
        auc_bar = g.iloc[times["15:15"]]

        # Typical price of the reference bar stands in for the 3:00-3:15 VWAP.
        reference = float((ref_bar["High"] + ref_bar["Low"] + ref_bar["Close"]) / 3)
        auction = float(auc_bar["Close"])
        if reference <= 0 or auction <= 0:
            continue

        nxt = by_day[days[k + 1]]
        nxt_open = float(nxt.iloc[0]["Open"])
        nxt_close = float(nxt.iloc[-1]["Close"])
        if nxt_open <= 0:
            continue

        out.append({
            "symbol": symbol,
            "date": day,
            "reference": reference,
            "auction": auction,
            "dislocation": (auction - reference) / reference * 100,
            "auction_vol": float(auc_bar["Volume"]),
            "day_vol": float(g["Volume"].sum()),
            # What the dislocation does next: the gap, then the session.
            "next_gap": (nxt_open - auction) / auction * 100,
            "next_o2c": (nxt_close - nxt_open) / nxt_open * 100,
            "next_c2c": (nxt_close - auction) / auction * 100,
            "post_cas": day >= CAS_START,
        })
    return out


def bucket_report(recs: list[dict], label: str) -> None:
    if len(recs) < 40:
        print(f"\n{label}: only {len(recs)} observations — skipping")
        return

    disloc = [r["dislocation"] for r in recs]
    print(f"\n{label}  (n={len(recs)}, {len(set(r['date'] for r in recs))} sessions)")
    print(f"  dislocation: mean {st.mean(disloc):+.3f}%  "
          f"median {st.median(disloc):+.3f}%  "
          f"stdev {st.pstdev(disloc):.3f}%  "
          f"|max| {max(abs(d) for d in disloc):.2f}%")

    share = sum(1 for d in disloc if abs(d) > 0.5) / len(disloc) * 100
    print(f"  sessions where the auction moved price >0.5% off the 3:15 ref: {share:.1f}%")

    # Does a stretched auction revert? Sort into quintiles by dislocation.
    ordered = sorted(recs, key=lambda r: r["dislocation"])
    q = max(1, len(ordered) // 5)
    print(f"  {'quintile':<10s} {'dislocation':>12s} {'next gap':>10s} "
          f"{'next O2C':>10s} {'next C2C':>10s}")
    for n, name in enumerate(["Q1 (down)", "Q2", "Q3", "Q4", "Q5 (up)"]):
        chunk = ordered[n * q: (n + 1) * q] if n < 4 else ordered[4 * q:]
        if not chunk:
            continue
        print(f"  {name:<10s} {st.mean(r['dislocation'] for r in chunk):+11.3f}% "
              f"{st.mean(r['next_gap'] for r in chunk):+9.3f}% "
              f"{st.mean(r['next_o2c'] for r in chunk):+9.3f}% "
              f"{st.mean(r['next_c2c'] for r in chunk):+9.3f}%")

    # A fade edge would show as a negative slope between the two.
    n = len(recs)
    mx = st.mean(disloc)
    my = st.mean(r["next_gap"] for r in recs)
    cov = sum((r["dislocation"] - mx) * (r["next_gap"] - my) for r in recs) / n
    sx = st.pstdev(disloc)
    sy = st.pstdev([r["next_gap"] for r in recs])
    corr = cov / (sx * sy) if sx and sy else 0.0
    print(f"  corr(dislocation, next gap) = {corr:+.4f}"
          f"   {'-> fades' if corr < -0.05 else '-> persists' if corr > 0.05 else '-> no relationship'}")

    # Extremes are where a setup would actually trigger.
    for thresh in (0.5, 1.0):
        up = [r for r in recs if r["dislocation"] > thresh]
        dn = [r for r in recs if r["dislocation"] < -thresh]
        if len(up) >= 15 and len(dn) >= 15:
            print(f"  |dislocation| > {thresh}%:  "
                  f"up n={len(up)} next_gap {st.mean(r['next_gap'] for r in up):+.3f}%  |  "
                  f"down n={len(dn)} next_gap {st.mean(r['next_gap'] for r in dn):+.3f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()

    universe = sv.NSE200[: args.limit] if args.limit else sv.NSE200
    print(f"Fetching 15m bars for {len(universe)} F&O names...", flush=True)

    recs: list[dict] = []
    ok = 0
    for i, sym in enumerate(universe, 1):
        df = fetch_intraday(sym)
        if df is None:
            continue
        r = day_records(sym, df)
        if r:
            recs.extend(r)
            ok += 1
        if i % 20 == 0:
            print(f"  {i}/{len(universe)} ({ok} usable, {len(recs)} records)", flush=True)

    print(f"\nUsable symbols: {ok}   Total session records: {len(recs)}")
    if not recs:
        print("No data.")
        return 1

    pre = [r for r in recs if not r["post_cas"]]
    post = [r for r in recs if r["post_cas"]]

    print("=" * 78)
    bucket_report(pre, "BEFORE CAS  (close = VWAP of last 30 min)")
    bucket_report(post, "AFTER CAS   (close = auction print, from 3 Aug 2026)")
    print("=" * 78)

    if pre and post:
        pre_d = [abs(r["dislocation"]) for r in pre]
        post_d = [abs(r["dislocation"]) for r in post]
        print(f"\nMean |dislocation| before {st.mean(pre_d):.3f}%  ->  "
              f"after {st.mean(post_d):.3f}%  "
              f"({(st.mean(post_d)/st.mean(pre_d)-1)*100:+.1f}%)")
        print("A rise means the auction is moving the close further from the 3:15")
        print("price than the old VWAP method did — bigger dislocations to trade,")
        print("and a bigger distortion in any daily close the scanner reads.")

    print(f"\nSessions after CAS launch: {len(set(r['date'] for r in post))}. "
          "Too few to validate a strategy on; this sizes the effect only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
