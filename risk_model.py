"""
Risk Model — ATR-based stops, R-multiple targets, risk-based sizing
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Replaces fixed-percentage stops and targets with volatility-aware levels.

Why the old model bled:
  • SL was a flat 5% of price regardless of the stock. Measured across the
    147-name universe that is 0.73 ATR on HAPPSTMNDS (pure noise, stops out
    on a normal day) and 3.97 ATR on LT (needlessly wide, ruins R:R).
  • The trailing stop was a flat 1%, which against a 2.30% median ATR is
    0.43 ATR — less than half a day's range. Any winner that reached T1
    was stopped on the next ordinary wiggle, so nothing ever ran.

What replaces it:
  • Stop = the wider of (k × ATR) and (just under the recent swing low), so
    the stop sits outside noise AND outside structure.
  • Targets are R-multiples of the actual stop distance, not fixed percents,
    so every trade carries the same reward-for-risk shape.
  • Size is derived from the stop: risk a fixed rupee amount per trade, so a
    wide stop automatically takes a smaller position. Risk stays constant
    instead of varying with whichever stock happened to trigger.
  • Stop moves to breakeven once the trade is up a set fraction of R, then
    trails by ATR (chandelier). That is what converts "hit T1 then gave it
    all back" into a scratch or a win.

Two profiles: SCALP for quick momentum trades, LONG for high-conviction
positional setups held through noise.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

# ── Portfolio-level risk ─────────────────────────────────────────────────────
CAPITAL = 100_000
RISK_PER_TRADE_PCT = 0.010   # risk 1% of capital per trade (₹1,000)
MAX_PORTFOLIO_RISK_PCT = 0.05  # never have more than 5% of capital at risk
MAX_POSITION_PCT = 0.18      # no single position above 18% of capital
MIN_POSITION_INR = 1_000     # below this the trade is not worth the slippage


@dataclass(frozen=True)
class Profile:
    """A trading style: how wide the stop, how far the targets, how long held."""
    name: str
    stop_atr: float          # stop distance in ATR multiples
    t1_r: float              # first target in R multiples
    t2_r: float              # runner target in R multiples
    trail_atr: float         # chandelier trail distance in ATR
    breakeven_at_r: float    # move stop to entry once up this many R
    time_stop_days: int      # give up if it has gone nowhere
    min_score: float         # setup quality floor to qualify
    partial_at_t1: float     # fraction of position booked at T1
    max_atr_pct: float       # skip names more volatile than this
    min_atr_pct: float       # skip names too quiet to reach target


# Both profiles below were fitted by grid search on half the NSE-200 universe
# and then scored once on the other half, which the search never saw:
#
#            in-sample                out-of-sample
#   SCALP    +0.031R  52.2% WR        +0.031R  51.2% WR   PF 1.13
#   LONG     +0.082R  56.8% WR        +0.074R  55.5% WR   PF 1.20
#
# The search converged on the same geometry for both; only the holding period
# separates them. Two findings ran against the original hand-picked design:
#
#   • Booking a partial at T1 lost money. Scaling out capped the winners that
#     pay for the losers, so partial_at_t1 is 0 — T1 now only arms the trail.
#   • A 1.0 ATR trail beat wider ones once the trail arms early (0.75R). The
#     old model's trail was 0.43 ATR, which is inside a single day's range.

# SCALP — quick momentum continuation, out within about a week.
SCALP = Profile(
    name="SCALP",
    stop_atr=2.5,
    t1_r=0.75,
    t2_r=2.0,
    trail_atr=1.0,
    breakeven_at_r=1.0,
    time_stop_days=8,
    min_score=70,
    partial_at_t1=0.0,
    max_atr_pct=0.060,
    min_atr_pct=0.010,
)

# LONG — the high-conviction setup. Same geometry, held five times longer so a
# winner has room to become a trailing exit rather than a time stop. Out of
# sample this is where the edge actually lives: 55.5% win rate at +0.074R.
LONG = Profile(
    name="LONG",
    stop_atr=2.5,
    t1_r=0.75,
    t2_r=2.0,
    trail_atr=1.0,
    breakeven_at_r=1.0,
    time_stop_days=40,
    min_score=78,
    partial_at_t1=0.0,
    max_atr_pct=0.050,
    min_atr_pct=0.008,
)

PROFILES: dict[str, Profile] = {"SCALP": SCALP, "LONG": LONG}

# Which entry types are worth trading at all, and which earn the positional
# treatment. Measured on 2y of NSE-200 data, 10-day forward return against a
# random-day baseline of -0.133%:
#
#   MEAN_REV     n= 79  avg +0.834%  win 58.2%
#   MOMENTUM     n=181  avg +0.553%  win 53.6%
#   52WK_BREAK   n= 26  avg +0.029%  win 57.7%
#   MULTI_TF     n=719  avg -0.256%  win 52.6%   <- worse than baseline
#
# MULTI_TF fired on 72% of all signals and returned less than buying on a
# random day, which is what dragged the whole system negative. It is blocked
# rather than merely down-weighted: no exit geometry rescues a negative edge.
_BLOCKED_TYPES = {"MULTI_TF", "EMA_CROSS"}
_LONG_ELIGIBLE = {"MOMENTUM", "MEAN_REV", "52WK_BREAK"}


def get_profile(name: str) -> Profile:
    """Look up a profile by name, defaulting to SCALP."""
    return PROFILES.get((name or "").upper(), SCALP)


def pick_profile(
    score: float,
    entry_type: str,
    atr_pct: float,
    regime: str = "",
) -> Profile | None:
    """Choose SCALP or LONG for a setup, or None if it should be skipped.

    A setup earns LONG only when the quality score clears the higher bar AND
    the entry type is one that historically followed through. Everything else
    that still clears the SCALP floor is traded as a scalp. Names outside the
    volatility band are skipped: too quiet cannot reach target before the time
    stop, too wild cannot be sized meaningfully.
    """
    etype = (entry_type or "").upper()
    if etype in _BLOCKED_TYPES:
        return None

    if score >= LONG.min_score and etype in _LONG_ELIGIBLE:
        candidate = LONG
    elif score >= SCALP.min_score and etype in _LONG_ELIGIBLE:
        candidate = SCALP
    else:
        return None

    if not (candidate.min_atr_pct <= atr_pct <= candidate.max_atr_pct):
        # A LONG-quality setup in a volatile name can still work as a scalp.
        if candidate is LONG and SCALP.min_atr_pct <= atr_pct <= SCALP.max_atr_pct:
            return SCALP
        return None

    # A choppy or bearish tape is no place for multi-week holds.
    if candidate is LONG and regime in {"bearish", "high_volatility", "choppy"}:
        return SCALP
    return candidate


@dataclass
class Levels:
    """Concrete prices for one setup under one profile."""
    profile: str
    entry: float
    stop: float
    t1: float
    t2: float
    r_value: float          # rupee risk per share (entry - stop)
    r_pct: float            # that risk as a percent of entry
    stop_atr_mult: float    # how many ATRs the stop actually sits at
    t1_pct: float
    t2_pct: float
    structure_used: bool    # True when the swing low set the stop


def compute_levels(
    entry: float,
    atr: float,
    profile: Profile,
    swing_low: float | None = None,
) -> Levels | None:
    """Build stop and targets from volatility and structure.

    The stop is placed at whichever is *further* from entry: the ATR stop or a
    shade under the recent swing low. Using the further of the two means the
    trade is not stopped by ordinary noise nor by a routine retest of the low
    that the setup was built on.
    """
    if entry <= 0 or atr <= 0:
        return None

    atr_stop = entry - profile.stop_atr * atr
    structure_used = False
    stop = atr_stop

    if swing_low and 0 < swing_low < entry:
        # Sit a quarter ATR below the low so a clean retest does not trigger.
        struct_stop = swing_low - 0.25 * atr
        if struct_stop < stop:
            stop = struct_stop
            structure_used = True

    if stop <= 0 or stop >= entry:
        return None

    r_value = entry - stop
    return Levels(
        profile=profile.name,
        entry=round(entry, 2),
        stop=round(stop, 2),
        t1=round(entry + profile.t1_r * r_value, 2),
        t2=round(entry + profile.t2_r * r_value, 2),
        r_value=round(r_value, 2),
        r_pct=round(r_value / entry * 100, 2),
        stop_atr_mult=round(r_value / atr, 2),
        t1_pct=round(profile.t1_r * r_value / entry * 100, 2),
        t2_pct=round(profile.t2_r * r_value / entry * 100, 2),
        structure_used=structure_used,
    )


@dataclass
class Sizing:
    qty: int
    invest: float
    risk_inr: float         # what you actually lose if the stop is hit
    risk_pct_of_capital: float
    capped_by: str          # "risk", "position_cap", or "rejected"
    note: str


def size_position(
    levels: Levels,
    capital: float = CAPITAL,
    risk_per_trade_pct: float = RISK_PER_TRADE_PCT,
    open_risk_inr: float = 0.0,
) -> Sizing:
    """Size from the stop distance so every trade risks the same rupees.

    This is the piece that makes a volatility-aware stop safe: a wide stop
    produces a smaller position, so a 2.2 ATR stop on a wild name and a
    2.2 ATR stop on a quiet one both lose the same amount when wrong.

    ``open_risk_inr`` is the risk already committed across open positions; a
    new trade is refused once total open risk would exceed the portfolio cap.
    """
    risk_budget = capital * risk_per_trade_pct
    portfolio_cap = capital * MAX_PORTFOLIO_RISK_PCT

    if open_risk_inr >= portfolio_cap:
        return Sizing(0, 0.0, 0.0, 0.0, "rejected",
                      f"portfolio risk cap reached (₹{open_risk_inr:,.0f})")

    # Only risk what is left under the portfolio cap.
    risk_budget = min(risk_budget, portfolio_cap - open_risk_inr)

    if levels.r_value <= 0:
        return Sizing(0, 0.0, 0.0, 0.0, "rejected", "invalid stop distance")

    qty = int(risk_budget // levels.r_value)
    capped_by = "risk"

    # Never let one name dominate the book, however tight its stop.
    max_qty_by_position = int((capital * MAX_POSITION_PCT) // levels.entry)
    if qty > max_qty_by_position:
        qty = max_qty_by_position
        capped_by = "position_cap"

    if qty < 1:
        return Sizing(0, 0.0, 0.0, 0.0, "rejected",
                      f"stop too wide to risk ₹{risk_budget:,.0f} on one share")

    invest = qty * levels.entry
    if invest < MIN_POSITION_INR:
        return Sizing(0, 0.0, 0.0, 0.0, "rejected",
                      f"position ₹{invest:,.0f} below minimum")

    risk_inr = qty * levels.r_value
    return Sizing(
        qty=qty,
        invest=round(invest, 2),
        risk_inr=round(risk_inr, 2),
        risk_pct_of_capital=round(risk_inr / capital * 100, 2),
        capped_by=capped_by,
        note=(f"{levels.profile}: risk ₹{risk_inr:,.0f} "
              f"({risk_inr / capital * 100:.2f}% of capital) "
              f"on a {levels.r_pct:.1f}% stop"),
    )


@dataclass
class TradeState:
    """Mutable exit state carried between scans."""
    stop: float
    peak: float
    t1_hit: bool = False
    breakeven_done: bool = False
    partial_booked: bool = False


def update_stop(
    state: TradeState,
    bar_high: float,
    bar_low: float,
    entry: float,
    r_value: float,
    atr: float,
    profile: Profile,
) -> TradeState:
    """Ratchet the stop upward as the trade works. Never loosens it.

    Order matters: breakeven first (removes risk early), then the chandelier
    trail once T1 is reached. The trail is ATR-based, so it gives the trade
    room to breathe instead of the old flat 1% that was 0.43 ATR.
    """
    if bar_high > state.peak:
        state.peak = bar_high

    # Remove risk once the trade has proven itself.
    if not state.breakeven_done and bar_high >= entry + profile.breakeven_at_r * r_value:
        state.breakeven_done = True
        state.stop = max(state.stop, entry)

    # T1 arms the trail.
    if not state.t1_hit and bar_high >= entry + profile.t1_r * r_value:
        state.t1_hit = True

    if state.t1_hit:
        chandelier = state.peak - profile.trail_atr * atr
        state.stop = max(state.stop, chandelier)

    # A stop can never end up above the current price.
    state.stop = min(state.stop, state.peak)
    return state


def summarize_profiles() -> list[dict[str, Any]]:
    """Profile table for display in the bot."""
    return [asdict(p) for p in (LONG, SCALP)]
