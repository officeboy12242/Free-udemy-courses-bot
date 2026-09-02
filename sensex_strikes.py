"""
SENSEX strike ladder with theoretical premiums
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BSE moved its option-chain API and the replacement is not reachable: the old
endpoint 302s to error_Bse.html, roughly thirty name and parameter guesses
found nothing, a headless browser gets "Access Denied" from BSE's bot
protection, and the ScraperAPI fallback is out of credits. So there is no live
SENSEX chain, and therefore no real bid/ask, no open interest, no volume.

What is still available, and exact:

  spot          Yahoo's ^BSESN, full 5m history
  expiry        BSE's /ddlExpiry/w endpoint, which does still work
  strikes       arithmetic: round spot to the 100-point strike grid
  levels        the sweep's entry/stop/target in SENSEX points

Only the premium is unknown, so this prices it with Black-Scholes using India
VIX as the volatility input. That is an estimate and it will not match your
broker's screen: it carries no bid-ask spread, no skew across strikes, and VIX
is NIFTY's implied volatility standing in for SENSEX's.

Treat the strike and the index levels as tradeable, and the premium as a
sizing guide only. Place the order against the real premium your broker shows.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
STRIKE_STEP = 100          # SENSEX options trade on a 100-point grid
LOT_SIZE = 20
RISK_FREE = 0.065          # ~India 1y T-bill; premium is barely sensitive to it


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf, so no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(spot: float, strike: float, days: float, iv: float,
                  option: str = "CE", r: float = RISK_FREE) -> dict[str, float]:
    """European option price and greeks. Index options are European, so this is
    the right model rather than an approximation of an American one."""
    if spot <= 0 or strike <= 0 or iv <= 0:
        return {}

    # Never let time hit zero: on expiry day the formula degenerates and the
    # option is worth its intrinsic value.
    t = max(days, 0.5) / 365.0
    sig = iv / 100.0

    d1 = (math.log(spot / strike) + (r + 0.5 * sig * sig) * t) / (sig * math.sqrt(t))
    d2 = d1 - sig * math.sqrt(t)
    disc = math.exp(-r * t)

    if option.upper() == "CE":
        price = spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        intrinsic = max(0.0, spot - strike)
    else:
        price = strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
        intrinsic = max(0.0, strike - spot)

    price = max(price, intrinsic)
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return {
        "price": round(price, 2),
        "delta": round(delta, 3),
        "intrinsic": round(intrinsic, 2),
        "extrinsic": round(price - intrinsic, 2),
        "theta_per_day": round(
            -(spot * pdf * sig) / (2 * math.sqrt(t)) / 365.0, 2
        ),
    }


def days_to_expiry(expiry: str, now: datetime | None = None) -> float | None:
    """Calendar days until a BSE expiry string such as '03 Sep 2026'."""
    now = now or datetime.now(IST)
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            exp = datetime.strptime(expiry.strip(), fmt).replace(tzinfo=IST)
            break
        except ValueError:
            continue
    else:
        return None
    # Contracts settle at the 15:30 close, not midnight.
    exp = exp.replace(hour=15, minute=30)
    return max((exp - now).total_seconds() / 86400.0, 0.0)


def atm_strike(spot: float, step: int = STRIKE_STEP) -> int:
    return int(round(spot / step) * step)


def strike_ladder(spot: float, expiry: str, iv: float = 12.0,
                  width: int = 3, step: int = STRIKE_STEP,
                  now: datetime | None = None) -> dict:
    """ATM +/- ``width`` strikes with estimated premiums for both sides."""
    days = days_to_expiry(expiry, now)
    if days is None:
        return {"error": f"could not parse expiry {expiry!r}"}

    atm = atm_strike(spot, step)
    rows = []
    for k in range(-width, width + 1):
        strike = atm + k * step
        ce = black_scholes(spot, strike, days, iv, "CE")
        pe = black_scholes(spot, strike, days, iv, "PE")
        if not ce or not pe:
            continue
        rows.append({
            "strike": strike,
            "moneyness": "ATM" if k == 0 else ("ITM-CE/OTM-PE" if k < 0
                                               else "OTM-CE/ITM-PE"),
            "ce": ce["price"], "ce_delta": ce["delta"],
            "pe": pe["price"], "pe_delta": pe["delta"],
            "ce_cost": round(ce["price"] * LOT_SIZE, 0),
            "pe_cost": round(pe["price"] * LOT_SIZE, 0),
            "theta_day": ce["theta_per_day"],
        })
    return {
        "spot": round(spot, 2), "expiry": expiry, "days_to_expiry": round(days, 2),
        "iv_used": iv, "atm": atm, "lot": LOT_SIZE, "rows": rows,
        "estimated": True,
    }


def premium_for_move(spot: float, target_spot: float, strike: int, expiry: str,
                     iv: float = 12.0, option: str = "CE",
                     now: datetime | None = None) -> dict | None:
    """What a premium becomes if the index reaches ``target_spot``.

    This is the part that actually matters for a trade: it converts the sweep's
    entry, stop and target in index points into approximate premium levels, so
    the same R-multiples can be applied on the option.
    """
    days = days_to_expiry(expiry, now)
    if days is None:
        return None
    a = black_scholes(spot, strike, days, iv, option)
    b = black_scholes(target_spot, strike, days, iv, option)
    if not a or not b:
        return None
    return {
        "from": a["price"], "to": b["price"],
        "change": round(b["price"] - a["price"], 2),
        "change_pct": round((b["price"] - a["price"]) / a["price"] * 100, 1)
        if a["price"] > 0 else 0.0,
        "per_lot": round((b["price"] - a["price"]) * LOT_SIZE, 0),
    }


def format_ladder(lad: dict) -> str:
    """Plain-text ladder for a Telegram message."""
    if lad.get("error"):
        return lad["error"]
    out = [
        f"SENSEX {lad['spot']}  |  expiry {lad['expiry']}  "
        f"({lad['days_to_expiry']:.1f}d)  |  IV {lad['iv_used']:.1f}%  lot {lad['lot']}",
        f"{'strike':>7s} {'CE':>8s} {'CE/lot':>9s} {'delta':>6s} | "
        f"{'PE':>8s} {'PE/lot':>9s} {'delta':>6s}",
    ]
    for r in lad["rows"]:
        mark = " <- ATM" if r["strike"] == lad["atm"] else ""
        out.append(
            f"{r['strike']:7d} {r['ce']:8.2f} {r['ce_cost']:9,.0f} {r['ce_delta']:6.2f} | "
            f"{r['pe']:8.2f} {r['pe_cost']:9,.0f} {r['pe_delta']:6.2f}{mark}"
        )
    out.append("ESTIMATED premiums (Black-Scholes, India VIX as the IV input).")
    out.append("No live BSE chain: no bid/ask, no OI, no skew. Verify at your broker.")
    return "\n".join(out)
