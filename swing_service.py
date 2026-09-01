"""
Swing Trading Alert Service
━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Screens NSE-50 stocks using RSI, Bollinger Bands, EMA, Volume, ATR
• Backtest engine: validates strategy on historical data
• P&L tracker: logs paper/live trades to MongoDB

Target: 2-5% per swing trade over 2-10 days.
Capital: ₹1,00,000 with max 20% deployed per signal batch.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# ── MongoDB helpers (lazy, same pattern as user_enroller) ─────────────────────
_client = None
_db = None
MONGODB_URI = os.getenv("MONGODB_URI", "")

# ── NSE 50 Liquid Stocks ─────────────────────────────────────────────────────
# Large-cap, high-liquidity stocks suitable for swing trading.
NSE50 = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "KOTAKBANK.NS",
    "LT.NS", "AXISBANK.NS", "ASIANPAINT.NS", "MARUTI.NS", "TITAN.NS",
    "SUNPHARMA.NS", "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TATAMOTORS.NS",
    "ULTRACEMCO.NS", "ONGC.NS", "NTPC.NS", "POWERGRID.NS", "M&M.NS",
    "JSWSTEEL.NS", "TATASTEEL.NS", "ADANIENT.NS", "ADANIPORTS.NS", "TECHM.NS",
    "BAJAJFINSV.NS", "INDUSINDBK.NS", "HDFCLIFE.NS", "SBILIFE.NS", "DIVISLAB.NS",
    "DRREDDY.NS", "CIPLA.NS", "APOLLOHOSP.NS", "NESTLEIND.NS", "EICHERMOT.NS",
    "COALINDIA.NS", "GRASIM.NS", "TATACONSUM.NS", "BRITANNIA.NS", "HEROMOTOCO.NS",
    "BPCL.NS", "HINDALCO.NS", "SHRIRAMFIN.NS", "BAJAJ-AUTO.NS", "LTIM.NS",
]

# ── Screener Parameters ──────────────────────────────────────────────────────
RSI_PERIOD = 14
RSI_OVERSOLD = 35        # RSI below this = potential bounce
BB_PERIOD = 20
BB_STD = 2.0
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 200          # Long-term trend filter
VOLUME_SPIKE_RATIO = 1.5 # Volume must be 1.5x average
ATR_PERIOD = 14
MIN_MARKET_CAP_CR = 5000 # Filter penny / micro caps
MAX_POSITIONS = 5        # Max concurrent positions
CAPITAL = 100_000        # ₹1 Lakh
POSITION_PCT = 0.20      # Max 20% per batch = ₹20,000 across 5 stocks
SL_PCT = 0.02            # 2% stop-loss
TARGET_PRIMARY = 0.03    # 3% primary target
TARGET_SECONDARY = 0.05  # 5% secondary target
TIME_STOP_DAYS = 10      # Exit if no target hit in 10 days

# Position sizing ranges based on win rate
# (min_alloc_pct, max_alloc_pct) of capital per stock
POSITION_TIERS = {
    # win_rate_threshold: (per_stock_pct, description)
    0:   (0.015, "Conservative — win rate unknown/low"),   # ₹1,500 per stock
    40:  (0.020, "Normal — win rate 40-50%"),              # ₹2,000 per stock
    50:  (0.025, "Moderate — win rate 50-60%"),            # ₹2,500 per stock
    60:  (0.035, "Aggressive — win rate 60-70%"),          # ₹3,500 per stock
    70:  (0.045, "Confident — win rate 70%+"),             # ₹4,500 per stock
}


def get_historical_win_rate() -> tuple[float, int]:
    """Get win rate and total closed trades from paper trade history.

    Returns (win_rate_percent, total_closed).
    """
    try:
        db = _get_db()
        closed = list(db.swing_trades.find({"status": "closed"}))
        if not closed:
            return 0.0, 0
        wins = sum(1 for t in closed if t.get("pnl_pct", 0) > 0)
        return round((wins / len(closed)) * 100, 1), len(closed)
    except Exception:
        return 0.0, 0


def calc_position_size(score: float, price: float) -> tuple[int, float, str]:
    """Calculate position size based on historical win rate + setup score.

    Returns (qty, invest_amount, tier_description).
    """
    win_rate, total_closed = get_historical_win_rate()

    # Find the right tier
    tier_pct = 0.015
    tier_desc = "Conservative — win rate unknown"
    for threshold in sorted(POSITION_TIERS.keys(), reverse=True):
        if win_rate >= threshold:
            tier_pct, tier_desc = POSITION_TIERS[threshold]
            break

    # Boost allocation for high-confidence setups (score 70+)
    if score >= 80:
        tier_pct *= 1.3   # +30% for top-tier setups
        tier_desc += " + High confidence boost"
    elif score >= 70:
        tier_pct *= 1.15  # +15% for strong setups
        tier_desc += " + Strong setup boost"

    # Cap at 10% of capital per stock (risk management)
    tier_pct = min(tier_pct, 0.10)

    per_stock = CAPITAL * tier_pct
    qty = max(1, int(per_stock / price))
    invest = qty * price

    return qty, invest, tier_desc


def _get_db():
    """Lazy MongoDB connection (same pattern as user_enroller)."""
    global _client, _db
    if not MONGODB_URI:
        raise ValueError("MONGODB_URI not set")
    if _client is not None:
        try:
            _client.admin.command("ping")
            return _db
        except Exception:
            _client = None
            _db = None
    try:
        import certifi
        from pymongo import MongoClient
        _client = MongoClient(
            MONGODB_URI,
            tls=True, tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
        )
        _db = _client.get_default_database()
        log.info("Swing: MongoDB connected")
        return _db
    except Exception as e:
        log.error("Swing MongoDB error: %s", e)
        raise


def _ensure_indexes():
    """Create indexes for swing trade collection."""
    db = _get_db()
    db.swing_trades.create_index([("user_id", 1), ("entered_at", -1)])
    db.swing_trades.create_index([("status", 1)])
    db.swing_trades.create_index([("symbol", 1), ("entered_at", -1)])


# ── Data Fetching ─────────────────────────────────────────────────────────────

def fetch_history(symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame | None:
    """Fetch OHLCV history for a symbol via yfinance. Always closes session."""
    try:
        raw = yf.download(symbol, period=period, interval=interval,
                          progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            return None
        # Flatten MultiIndex columns if present
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
        return raw
    except Exception as e:
        log.warning("fetch_history %s failed: %s", symbol, e)
        return None


# ── Technical Indicators ──────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI, Bollinger Bands, EMAs, ATR, Volume ratio to OHLCV DataFrame."""
    df = df.copy()

    # RSI
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta.clip(upper=0))
    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    df["RSI"] = 100 - (100 / (1 + rs))

    # Bollinger Bands
    df["BB_MID"] = df["Close"].rolling(BB_PERIOD).mean()
    bb_std = df["Close"].rolling(BB_PERIOD).std()
    df["BB_UPPER"] = df["BB_MID"] + BB_STD * bb_std
    df["BB_LOWER"] = df["BB_MID"] - BB_STD * bb_std
    df["BB_PCT"] = (df["Close"] - df["BB_LOWER"]) / (df["BB_UPPER"] - df["BB_LOWER"]).replace(0, 1e-10)

    # EMAs
    df["EMA9"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=EMA_TREND, adjust=False).mean()

    # ATR (Average True Range)
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift(1)).abs()
    low_close = (df["Low"] - df["Close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["ATR"] = true_range.rolling(ATR_PERIOD).mean()
    df["ATR_PCT"] = df["ATR"] / df["Close"]

    # Volume ratio (current / 20-day average)
    df["VOL_AVG"] = df["Volume"].rolling(20).mean()
    df["VOL_RATIO"] = df["Volume"] / df["VOL_AVG"].replace(0, 1)

    # Daily change %
    df["CHANGE_PCT"] = df["Close"].pct_change() * 100

    return df


# ── Scoring Engine ────────────────────────────────────────────────────────────

@dataclass
class SwingSetup:
    symbol: str
    name: str
    score: float           # 0-100 composite score
    entry: float           # suggested entry price
    stop_loss: float       # 2% below entry
    target_1: float        # 3% above entry
    target_2: float        # 5% above entry
    rsi: float
    bb_pct: float
    atr_pct: float
    vol_ratio: float
    ema_trend: str         # "ABOVE" or "BELOW" 200 EMA
    change_today: float
    reasons: list[str]     # why this stock scored well
    suggested_qty: int     # number of shares
    suggested_invest: float
    expected_profit_t1: float  # ₹ profit if T1 (+3%) hits
    expected_profit_t2: float  # ₹ profit if T2 (+5%) hits
    expected_loss_sl: float    # ₹ loss if SL (-2%) hits
    risk_reward: str           # e.g. "1:1.5"
    sizing_tier: str           # why this allocation size


def score_stock(symbol: str, df: pd.DataFrame) -> SwingSetup | None:
    """Score a single stock based on swing trading criteria. Returns None if data insufficient."""
    if df is None or len(df) < 60:
        return None

    df = compute_indicators(df)
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest

    price = float(latest["Close"])
    rsi = float(latest.get("RSI", 50))
    bb_pct = float(latest.get("BB_PCT", 0.5))
    atr_pct = float(latest.get("ATR_PCT", 0))
    vol_ratio = float(latest.get("VOL_RATIO", 1))
    ema200 = float(latest.get("EMA200", price))
    ema9 = float(latest.get("EMA9", price))
    ema21 = float(latest.get("EMA21", price))
    change = float(latest.get("CHANGE_PCT", 0))

    score = 0.0
    reasons = []

    # ── RSI oversold bounce (max 25 pts) ──
    if rsi < RSI_OVERSOLD:
        score += 25
        reasons.append(f"RSI oversold ({rsi:.0f})")
    elif rsi < 40:
        score += 15
        reasons.append(f"RSI low ({rsi:.0f})")
    elif rsi < 45:
        score += 8
        reasons.append(f"RSI neutral-low ({rsi:.0f})")

    # RSI turning up from oversold = bonus
    prev_rsi = float(prev.get("RSI", 50)) if "RSI" in prev.index else 50
    if rsi > prev_rsi and rsi < 45:
        score += 5
        reasons.append("RSI turning up")

    # ── Bollinger Band near lower (max 20 pts) ──
    if bb_pct < 0.1:
        score += 20
        reasons.append(f"Near lower BB ({bb_pct:.2f})")
    elif bb_pct < 0.25:
        score += 12
        reasons.append(f"Lower BB zone ({bb_pct:.2f})")
    elif bb_pct < 0.35:
        score += 5

    # ── Volume spike (max 15 pts) ──
    if vol_ratio > 2.5:
        score += 15
        reasons.append(f"Volume spike ({vol_ratio:.1f}x)")
    elif vol_ratio > VOLUME_SPIKE_RATIO:
        score += 10
        reasons.append(f"Above-avg volume ({vol_ratio:.1f}x)")

    # ── Trend filter: above 200 EMA (max 15 pts) ──
    ema_trend = "ABOVE" if price > ema200 else "BELOW"
    if price > ema200:
        score += 10
        reasons.append("Above 200 EMA (uptrend)")
    # EMA 9 > 21 = short-term bullish
    if ema9 > ema21:
        score += 5
        reasons.append("EMA9 > EMA21 (bullish cross)")

    # ── Price near support / mean reversion (max 10 pts) ──
    # Check if price bounced from recent low
    recent_low = float(df["Low"].tail(10).min())
    dist_from_low = (price - recent_low) / price
    if dist_from_low < 0.02:
        score += 10
        reasons.append("Near 10-day low (bounce setup)")
    elif dist_from_low < 0.04:
        score += 5

    # ── Volatility sweet spot (max 10 pts) ──
    # ATR 1-3% = good swing range
    if 0.01 < atr_pct < 0.03:
        score += 10
        reasons.append(f"Good volatility ({atr_pct*100:.1f}% ATR)")
    elif 0.005 < atr_pct < 0.04:
        score += 5

    # ── Penalty: don't buy into a falling knife ──
    # If today's drop > 5% AND RSI < 25, likely fundamental issue
    if change < -5 and rsi < 25:
        score -= 15
        reasons.append("⚠ Sharp drop — possible fundamental issue")

    # ── Bonus: positive breadth ──
    # Price above EMA21 and EMA21 above EMA200 = aligned trend
    if ema21 > ema200 and price > ema21:
        score += 5
        reasons.append("Multi-EMA alignment (bullish)")

    # Skip if score is too low
    if score < 25:
        return None

    # Cap score at 100
    score = min(score, 100)

    # Dynamic position sizing based on win rate + score
    qty, invest, sizing_tier = calc_position_size(score, price)

    entry = round(price, 2)
    sl = round(price * (1 - SL_PCT), 2)
    t1 = round(price * (1 + TARGET_PRIMARY), 2)
    t2 = round(price * (1 + TARGET_SECONDARY), 2)

    # Expected P&L
    profit_t1 = round((TARGET_PRIMARY * price) * qty, 0)
    profit_t2 = round((TARGET_SECONDARY * price) * qty, 0)
    loss_sl = round((SL_PCT * price) * qty, 0)
    # Risk:Reward = potential loss : potential gain (at T2)
    rr = f"1:{TARGET_SECONDARY / SL_PCT:.1f}" if SL_PCT > 0 else "—"

    # Clean symbol name for display
    name = symbol.replace(".NS", "")

    return SwingSetup(
        symbol=symbol, name=name, score=round(score, 1),
        entry=entry, stop_loss=sl, target_1=t1, target_2=t2,
        rsi=round(rsi, 1), bb_pct=round(bb_pct, 3),
        atr_pct=round(atr_pct * 100, 2), vol_ratio=round(vol_ratio, 1),
        ema_trend=ema_trend, change_today=round(change, 2),
        reasons=reasons,
        suggested_qty=qty, suggested_invest=round(invest, 0),
        expected_profit_t1=profit_t1, expected_profit_t2=profit_t2,
        expected_loss_sl=loss_sl, risk_reward=rr, sizing_tier=sizing_tier,
    )


# ── Daily Scanner ─────────────────────────────────────────────────────────────

def scan_nse50(top_n: int = 5) -> list[SwingSetup]:
    """Scan all NSE-50 stocks and return top N swing setups sorted by score."""
    all_setups: list[SwingSetup] = []

    for symbol in NSE50:
        df = fetch_history(symbol, period="6mo", interval="1d")
        if df is None or df.empty:
            continue
        setup = score_stock(symbol, df)
        if setup:
            all_setups.append(setup)

    all_setups.sort(key=lambda s: s.score, reverse=True)
    return all_setups[:top_n]


# ── Backtest Engine ───────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_pct: float = 0.0
    pnl_inr: float = 0.0
    holding_days: int = 0


@dataclass
class BacktestResult:
    period: str
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    avg_winner_pct: float = 0.0
    avg_loser_pct: float = 0.0
    max_win_pct: float = 0.0
    max_loss_pct: float = 0.0
    total_return_pct: float = 0.0
    profit_factor: float = 0.0
    avg_holding_days: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"📊 Backtest: {self.period}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Total trades: {self.total_trades}\n"
            f"Win rate: {self.win_rate:.0f}% ({self.winners}W / {self.losers}L)\n"
            f"Avg return: {self.avg_return_pct:+.2f}%\n"
            f"Avg winner: +{self.avg_winner_pct:.2f}%  |  Avg loser: {self.avg_loser_pct:.2f}%\n"
            f"Best: +{self.max_win_pct:.2f}%  |  Worst: {self.max_loss_pct:.2f}%\n"
            f"Total P&L: {self.total_return_pct:+.2f}%\n"
            f"Profit factor: {self.profit_factor:.2f}\n"
            f"Avg holding: {self.avg_holding_days:.1f} days\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Capital: ₹{CAPITAL:,.0f}  |  SL: {SL_PCT*100:.0f}%  |  T1: {TARGET_PRIMARY*100:.0f}%  |  T2: {TARGET_SECONDARY*100:.0f}%"
        )


def backtest_stock(symbol: str, start: str = "2025-01-01", end: str | None = None) -> list[BacktestTrade]:
    """Backtest swing strategy on a single stock over historical data."""
    df = fetch_history(symbol, period="1y", interval="1d")
    if df is None or len(df) < 60:
        return []

    df = compute_indicators(df)

    # Filter to backtest period
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]

    trades: list[BacktestTrade] = []
    in_trade = False
    entry_price = 0.0
    entry_date = ""
    entry_idx = 0

    for i in range(EMA_TREND + 1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]

        if not in_trade:
            # ── Entry logic: same scoring as live ──
            rsi = float(row.get("RSI", 50))
            bb_pct = float(row.get("BB_PCT", 0.5))
            vol_ratio = float(row.get("VOL_RATIO", 1))
            price = float(row["Close"])
            ema200 = float(row.get("EMA200", price))
            ema9 = float(row.get("EMA9", price))
            ema21 = float(row.get("EMA21", price))
            prev_rsi = float(prev_row.get("RSI", 50))

            # Simplified entry conditions
            entry_signal = False
            if rsi < 40 and bb_pct < 0.3 and vol_ratio > 1.3 and price > ema200:
                entry_signal = True
            if rsi < RSI_OVERSOLD and bb_pct < 0.2:
                entry_signal = True
            # RSI turning up from oversold
            if rsi > prev_rsi and rsi < 42 and bb_pct < 0.35 and price > ema200:
                entry_signal = True

            if entry_signal:
                entry_price = float(row["Open"])  # Buy at next day open
                entry_date = str(df.index[i].date())
                entry_idx = i
                in_trade = True
        else:
            # ── Exit logic: check SL, targets, time stop ──
            high = float(row["High"])
            low = float(row["Low"])
            close = float(row["Close"])
            days_held = i - entry_idx

            sl_price = entry_price * (1 - SL_PCT)
            t1_price = entry_price * (1 + TARGET_PRIMARY)
            t2_price = entry_price * (1 + TARGET_SECONDARY)

            exit_price = None
            exit_reason = None

            # Stop-loss hit
            if low <= sl_price:
                exit_price = sl_price
                exit_reason = "SL"

            # Primary target hit
            elif high >= t1_price and exit_price is None:
                # 50% booked at T1, trail rest to T2
                if high >= t2_price:
                    exit_price = t2_price
                    exit_reason = "T2"
                else:
                    # After T1 hit, use trailing SL at breakeven
                    trail_sl = max(sl_price, entry_price)  # move SL to breakeven
                    if low <= trail_sl:
                        exit_price = entry_price
                        exit_reason = "T1_TRAIL_BE"
                    elif days_held >= TIME_STOP_DAYS:
                        exit_price = close
                        exit_reason = "TIME"

            # Time stop
            elif days_held >= TIME_STOP_DAYS:
                exit_price = close
                exit_reason = "TIME"

            if exit_price is not None:
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                pnl_inr = (CAPITAL * POSITION_PCT / MAX_POSITIONS) * (pnl_pct / 100)
                trades.append(BacktestTrade(
                    symbol=symbol,
                    entry_date=entry_date,
                    entry_price=round(entry_price, 2),
                    exit_date=str(df.index[i].date()),
                    exit_price=round(exit_price, 2),
                    exit_reason=exit_reason,
                    pnl_pct=round(pnl_pct, 2),
                    pnl_inr=round(pnl_inr, 0),
                    holding_days=days_held,
                ))
                in_trade = False

    return trades


def run_backtest(symbols: list[str] | None = None, start: str = "2025-01-01") -> BacktestResult:
    """Run backtest across multiple stocks and aggregate results."""
    if symbols is None:
        symbols = NSE50[:20]  # Top 20 liquid stocks for speed

    all_trades: list[BacktestTrade] = []
    for sym in symbols:
        trades = backtest_stock(sym, start=start)
        all_trades.extend(trades)

    if not all_trades:
        return BacktestResult(period=f"{start} to now", total_trades=0)

    winners = [t for t in all_trades if t.pnl_pct > 0]
    losers = [t for t in all_trades if t.pnl_pct <= 0]

    total_win = sum(t.pnl_pct for t in winners)
    total_loss = abs(sum(t.pnl_pct for t in losers))

    avg_return = sum(t.pnl_pct for t in all_trades) / len(all_trades)
    avg_winner = (sum(t.pnl_pct for t in winners) / len(winners)) if winners else 0
    avg_loser = (sum(t.pnl_pct for t in losers) / len(losers)) if losers else 0

    total_return = sum(t.pnl_pct for t in all_trades)
    total_inr = sum(t.pnl_inr for t in all_trades)
    profit_factor = (total_win / total_loss) if total_loss > 0 else float("inf")

    return BacktestResult(
        period=f"{start} to now",
        total_trades=len(all_trades),
        winners=len(winners),
        losers=len(losers),
        win_rate=(len(winners) / len(all_trades)) * 100,
        avg_return_pct=round(avg_return, 2),
        avg_winner_pct=round(avg_winner, 2),
        avg_loser_pct=round(avg_loser, 2),
        max_win_pct=round(max(t.pnl_pct for t in all_trades), 2),
        max_loss_pct=round(min(t.pnl_pct for t in all_trades), 2),
        total_return_pct=round(total_return, 2),
        profit_factor=round(profit_factor, 2),
        avg_holding_days=round(sum(t.holding_days for t in all_trades) / len(all_trades), 1),
        trades=all_trades,
    )


# ── P&L Trade Tracker ────────────────────────────────────────────────────────

def log_swing_trade(
    user_id: int,
    symbol: str,
    entry_price: float,
    qty: int,
    status: str = "open",  # open | closed
    exit_price: float | None = None,
    exit_reason: str | None = None,
    notes: str = "",
) -> dict:
    """Log a swing trade to MongoDB."""
    db = _get_db()
    _ensure_indexes()
    now = datetime.utcnow()
    pnl_pct = 0.0
    pnl_inr = 0.0
    if exit_price and entry_price:
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        pnl_inr = (exit_price - entry_price) * qty

    doc = {
        "user_id": user_id,
        "symbol": symbol,
        "entry_price": entry_price,
        "qty": qty,
        "status": status,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_inr": round(pnl_inr, 0),
        "notes": notes,
        "entered_at": now,
        "exited_at": now if status == "closed" else None,
    }
    result = db.swing_trades.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc


def close_swing_trade(
    trade_id: str,
    exit_price: float,
    exit_reason: str = "manual",
    notes: str = "",
) -> bool:
    """Close an open swing trade."""
    db = _get_db()
    from bson import ObjectId
    trade = db.swing_trades.find_one({"_id": ObjectId(trade_id), "status": "open"})
    if not trade:
        return False

    pnl_pct = ((exit_price - trade["entry_price"]) / trade["entry_price"]) * 100
    pnl_inr = (exit_price - trade["entry_price"]) * trade["qty"]

    db.swing_trades.update_one(
        {"_id": ObjectId(trade_id)},
        {"$set": {
            "status": "closed",
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_inr": round(pnl_inr, 0),
            "exited_at": datetime.utcnow(),
            "notes": notes or trade.get("notes", ""),
        }},
    )
    return True


def get_open_trades(user_id: int | None = None) -> list[dict]:
    """Get all open swing trades."""
    db = _get_db()
    q: dict[str, Any] = {"status": "open"}
    if user_id:
        q["user_id"] = user_id
    return list(db.swing_trades.find(q).sort("entered_at", -1))


def get_trade_summary(user_id: int, days: int = 30) -> dict:
    """Get P&L summary for a user over last N days."""
    db = _get_db()
    since = datetime.utcnow() - timedelta(days=days)
    trades = list(db.swing_trades.find({
        "user_id": user_id,
        "entered_at": {"$gte": since},
    }).sort("entered_at", -1))

    closed = [t for t in trades if t["status"] == "closed"]
    open_trades = [t for t in trades if t["status"] == "open"]

    total_pnl = sum(t.get("pnl_inr", 0) for t in closed)
    winners = [t for t in closed if t.get("pnl_pct", 0) > 0]
    losers = [t for t in closed if t.get("pnl_pct", 0) <= 0]

    return {
        "total_trades": len(trades),
        "closed": len(closed),
        "open": len(open_trades),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": (len(winners) / len(closed) * 100) if closed else 0,
        "total_pnl_inr": round(total_pnl, 0),
        "avg_pnl_pct": round(
            sum(t.get("pnl_pct", 0) for t in closed) / len(closed), 2
        ) if closed else 0,
        "trades": trades,
    }


# ── Auto Paper Trading ───────────────────────────────────────────────────────

def check_open_trades_for_exits() -> list[dict]:
    """Check all open paper trades against current prices.

    Closes trades that hit SL, T1+trail, T2, or time stop.
    Returns list of closed trade summaries.
    """
    db = _get_db()
    _ensure_indexes()
    open_trades = list(db.swing_trades.find({"status": "open"}))
    if not open_trades:
        return []

    # Group by symbol to avoid duplicate fetches
    symbols = list({t["symbol"] for t in open_trades})
    price_cache: dict[str, dict] = {}
    for sym in symbols:
        df = fetch_history(sym, period="5d", interval="1d")
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            price_cache[sym] = {
                "close": float(latest["Close"]),
                "high": float(latest["High"]),
                "low": float(latest["Low"]),
            }

    closed_trades = []
    for trade in open_trades:
        sym = trade["symbol"]
        prices = price_cache.get(sym)
        if not prices:
            continue

        entry = trade["entry_price"]
        qty = trade["qty"]
        high = prices["high"]
        low = prices["low"]
        close = prices["close"]

        sl = entry * (1 - SL_PCT)
        t1 = entry * (1 + TARGET_PRIMARY)
        t2 = entry * (1 + TARGET_SECONDARY)

        # Calculate holding days
        entered = trade["entered_at"]
        if isinstance(entered, datetime):
            days_held = (datetime.utcnow() - entered).days
        else:
            days_held = 0

        exit_price = None
        exit_reason = None

        # Stop-loss hit
        if low <= sl:
            exit_price = sl
            exit_reason = "SL"

        # T2 hit
        elif high >= t2:
            exit_price = t2
            exit_reason = "T2"

        # T1 hit — trail to breakeven
        elif high >= t1:
            if low <= entry:  # Trailing SL at breakeven hit
                exit_price = entry
                exit_reason = "T1_TRAIL_BE"
            elif days_held >= TIME_STOP_DAYS:
                exit_price = close
                exit_reason = "TIME"

        # Time stop
        elif days_held >= TIME_STOP_DAYS:
            exit_price = close
            exit_reason = "TIME"

        if exit_price is not None:
            pnl_pct = ((exit_price - entry) / entry) * 100
            pnl_inr = (exit_price - entry) * qty
            from bson import ObjectId
            db.swing_trades.update_one(
                {"_id": ObjectId(trade["_id"])},
                {"$set": {
                    "status": "closed",
                    "exit_price": round(exit_price, 2),
                    "exit_reason": exit_reason,
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_inr": round(pnl_inr, 0),
                    "exited_at": datetime.utcnow(),
                }},
            )
            closed_trades.append({
                "symbol": sym,
                "entry": entry,
                "exit": round(exit_price, 2),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_inr": round(pnl_inr, 0),
                "reason": exit_reason,
                "days_held": days_held,
            })

    return closed_trades


def run_paper_scan() -> dict:
    """Full paper trade cycle:
    1. Close trades that hit SL/T1/T2/time-stop
    2. Scan NSE-50 for new setups
    3. Open paper trades for top setups (if not already open)
    4. Return summary of actions taken.
    """
    db = _get_db()
    _ensure_indexes()
    actions = {
        "closed": [],
        "opened": [],
        "already_open": [],
        "scan_failed": False,
    }

    # Step 1: Check existing open trades for exits
    try:
        actions["closed"] = check_open_trades_for_exits()
    except Exception as e:
        log.warning("Paper trade exit check failed: %s", e)

    # Step 2: Get currently open symbols so we don't double-enter
    open_trades = list(db.swing_trades.find({"status": "open"}))
    open_symbols = {t["symbol"] for t in open_trades}

    # Step 3: Count how many slots are free
    open_count = len(open_trades)
    slots_free = MAX_POSITIONS - open_count

    if slots_free <= 0:
        return actions  # All slots filled

    # Step 4: Scan for new setups
    try:
        setups = scan_nse50(slots_free + 2)  # fetch extra in case some are already open
    except Exception as e:
        log.warning("Paper scan NSE50 failed: %s", e)
        actions["scan_failed"] = True
        return actions

    per_stock = (CAPITAL * POSITION_PCT) / MAX_POSITIONS

    for setup in setups:
        if len(actions["opened"]) >= slots_free:
            break
        if setup.symbol in open_symbols:
            actions["already_open"].append(setup.symbol)
            continue

        # Open paper trade
        qty = max(1, int(per_stock / setup.entry))
        invest = qty * setup.entry
        try:
            log_swing_trade(
                user_id=0,  # paper trade (owner)
                symbol=setup.symbol,
                entry_price=setup.entry,
                qty=qty,
                status="open",
                notes=f"score={setup.score} reasons={','.join(setup.reasons[:3])}",
            )
            actions["opened"].append({
                "symbol": setup.symbol,
                "entry": setup.entry,
                "qty": qty,
                "invest": round(invest, 0),
                "score": setup.score,
                "sl": setup.stop_loss,
                "t1": setup.target_1,
                "t2": setup.target_2,
            })
        except Exception as e:
            log.warning("Paper trade open failed %s: %s", setup.symbol, e)

    return actions


def get_paper_portfolio() -> dict:
    """Get current paper portfolio with unrealized P&L."""
    db = _get_db()
    open_trades = list(db.swing_trades.find({"status": "open"}).sort("entered_at", -1))
    closed_trades = list(db.swing_trades.find({"status": "closed"}).sort("exited_at", -1).limit(50))

    # Fetch live prices for open trades
    symbols = list({t["symbol"] for t in open_trades})
    prices: dict[str, float] = {}
    for sym in symbols:
        df = fetch_history(sym, period="5d", interval="1d")
        if df is not None and not df.empty:
            prices[sym] = float(df.iloc[-1]["Close"])

    portfolio = []
    total_unrealized = 0.0
    total_invested = 0.0
    for t in open_trades:
        current = prices.get(t["symbol"], t["entry_price"])
        unrealized_pct = ((current - t["entry_price"]) / t["entry_price"]) * 100
        unrealized_inr = (current - t["entry_price"]) * t["qty"]
        total_unrealized += unrealized_inr
        total_invested += t["entry_price"] * t["qty"]

        entered = t["entered_at"]
        days_held = (datetime.utcnow() - entered).days if isinstance(entered, datetime) else 0

        portfolio.append({
            "symbol": t["symbol"],
            "entry": t["entry_price"],
            "current": round(current, 2),
            "qty": t["qty"],
            "unrealized_pct": round(unrealized_pct, 2),
            "unrealized_inr": round(unrealized_inr, 0),
            "days_held": days_held,
            "notes": t.get("notes", ""),
        })

    # Closed trade stats
    closed_wins = [t for t in closed_trades if t.get("pnl_pct", 0) > 0]
    closed_losses = [t for t in closed_trades if t.get("pnl_pct", 0) <= 0]
    total_realized = sum(t.get("pnl_inr", 0) for t in closed_trades)

    return {
        "open": portfolio,
        "total_unrealized": round(total_unrealized, 0),
        "total_invested": round(total_invested, 0),
        "closed_count": len(closed_trades),
        "closed_wins": len(closed_wins),
        "closed_losses": len(closed_losses),
        "total_realized": round(total_realized, 0),
        "recent_closed": closed_trades[:10],
    }
