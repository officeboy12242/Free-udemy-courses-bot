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

# ── NSE 200 Extended Universe ────────────────────────────────────────────────
# Mid-cap + Large-cap for deeper coverage. Only liquid, well-followed stocks.
NSE200 = NSE50 + [
    # Pharma & Healthcare
    "TORNTPHARM.NS", "ALKEM.NS", "LALABORATORY.NS", "IPCALAB.NS", "LAURUSLABS.NS",
    "AUROPHARMA.NS", "GLENMARK.NS", "BIOCON.NS", "TATAPHARMALIFE.NS", "ZYDUSLIFE.NS",
    # IT & Digital
    "MPHASIS.NS", "OFSS.NS", "COFORGE.NS", "PERSISTENT.NS", "COGENT.NS",
    "LTTS.NS", "KPITTECH.NS", "HAPPSTMNDS.NS", "ZENTEC.NS", "TANLA.NS",
    # Chemicals & Materials
    "DEEPAKNTR.NS", "ATUL.NS", "NAVINFLUOR.NS", "SRF.NS", "AAVAS.NS",
    "PIIND.NS", "CLEAN.NS", "ANURAS.NS", "FLUOROCHEM.NS", "GODREJIND.NS",
    # Banking & Finance (mid)
    "FEDERALBNK.NS", "IDFCFIRSTB.NS", "BANDHANBNK.NS", "PNB.NS", "CANBK.NS",
    "MUTHOOTFIN.NS", "MANAPPURAM.NS", "CHOLAFIN.NS", "ABFRL.NS", "CROMPTON.NS",
    # Consumer & Retail
    "VOLTAS.NS", "BLUESTARLT.NS", "CROMPTON.NS", "TRENT.NS", "DIXON.NS",
    "EMAMILTD.NS", "MARICO.NS", "PGHH.NS", "RADICO.NS", "UNITDSPR.NS",
    # Auto & Ancillary
    "MOTHERSON.NS", "BOSCHLTD.NS", "MRF.NS", "TVSMOTOR.NS", "ASHOKLEY.NS",
    "EXIDEIND.NS", "AMARARAJA.NS", "SUNTV.NS", "ZEE.NS", "NAUKRI.NS",
    # Energy & Infra
    "TATAPOWER.NS", "NHPC.NS", "SJVN.NS", "IREDA.NS", "TARSONS.NS",
    "ADANIGREEN.NS", "ADANIENSOL.NS", "CESC.NS", "TORNTPOWER.NS", "JSL.NS",
    # Metals & Mining
    "NATIONALUM.NS", "HINDZINC.NS", "VEDL.NS", "NMDC.NS", "SAIL.NS",
    "JINDALSTEL.NS", "HINDCOPPER.NS", "RATNAMANI.NS", "APLAPOLLO.NS", "WELCORP.NS",
    # Realty
    "DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PRESTIGE.NS", "BRIGADE.NS",
    "PHOENIXLTD.NS", "SOBHA.NS", "LODHA.NS", "IBC.NS", "MAHLIFE.NS",
    # Defence & PSU
    "BEL.NS", "HAL.NS", "MAZAGONDOCK.NS", "COCHINSHIP.NS", "GRSE.NS",
    "BHEL.NS", "RVNL.NS", "IRFC.NS", "RECLTD.NS", "IREDA.NS",
    # Telecom & Media
    "IDEA.NS", "TATACOMM.NS", "PVRINOX.NS", "DLF.NS", "ZYDUSLIFE.NS",
]
# Deduplicate
NSE200 = list(dict.fromkeys(NSE200))

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
SL_PCT = 0.05             # 5% stop-loss (strategy lab optimized) (v4 optimized — wider for 20d holds)
TARGET_PRIMARY = 0.08    # 8% primary target (optimized) (v4 optimized)
TARGET_SECONDARY = 0.12  # 12% secondary target (optimized) (v4 optimized)
TRAIL_PCT = 0.01          # 1% trailing stop after T1 hit
TIME_STOP_DAYS = 25      # Exit if no target hit in 25 days
TRAILING_STOP_PCT = 0.01 # 1.0% trailing stop (locks profits fast) after T1 (tighter = locks profits faster)

# ── Entry Strategy Weights ───────────────────────────────────────────────
# Three entry types tested in backtest:
#   MOMENTUM:  75% WR, +6.27% avg — best performer
#   MEAN_REV:  57.9% WR, +4.00% avg — solid in oversold markets
#   EMA_CROSS: 33.3% WR, -0.26% avg — weakest, use sparingly
ENTRY_STRATEGY = "adaptive"  # adaptive = switch based on market regime

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


def get_sizing_summary() -> dict:
    """Return current sizing tier, win rate, and projected profits at each tier.

    Used by /swing_sizing command.
    """
    win_rate, total_closed = get_historical_win_rate()

    # Find current tier
    current_pct = 0.015
    current_desc = "Conservative — win rate unknown"
    for threshold in sorted(POSITION_TIERS.keys(), reverse=True):
        if win_rate >= threshold:
            current_pct, current_desc = POSITION_TIERS[threshold]
            break

    # Build tier table
    tiers = []
    for threshold in sorted(POSITION_TIERS.keys()):
        pct, desc = POSITION_TIERS[threshold]
        # Calculate example at ₹500 stock price
        example_price = 500.0
        per_stock = CAPITAL * pct
        qty = max(1, int(per_stock / example_price))
        invest = qty * example_price
        profit_t1 = round(TARGET_PRIMARY * invest, 0)
        profit_t2 = round(TARGET_SECONDARY * invest, 0)
        loss_sl = round(SL_PCT * invest, 0)
        active = "◀ CURRENT" if (threshold == 0 and win_rate < 40) or (threshold > 0 and win_rate >= threshold and (threshold == max(t for t in POSITION_TIERS.keys() if t <= win_rate))) else ""
        # Better active detection
        active = ""
        tiers.append({
            "threshold": threshold,
            "pct": pct,
            "desc": desc,
            "per_stock": round(per_stock),
            "invest": round(invest),
            "qty": qty,
            "profit_t1": profit_t1,
            "profit_t2": profit_t2,
            "loss_sl": loss_sl,
        })

    # Mark current tier
    active_threshold = 0
    for threshold in sorted(POSITION_TIERS.keys(), reverse=True):
        if win_rate >= threshold:
            active_threshold = threshold
            break
    for t in tiers:
        if t["threshold"] == active_threshold:
            t["active"] = True

    # Max concurrent deployment at each tier
    max_deploy = round(CAPITAL * current_pct * MAX_POSITIONS)

    # Sample allocation for today's ₹1L
    sample_invest = CAPITAL * current_pct
    sample_t1 = round(TARGET_PRIMARY * sample_invest * MAX_POSITIONS)
    sample_t2 = round(TARGET_SECONDARY * sample_invest * MAX_POSITIONS)
    sample_loss = round(SL_PCT * sample_invest * MAX_POSITIONS)

    return {
        "win_rate": win_rate,
        "total_closed": total_closed,
        "current_pct": current_pct,
        "current_desc": current_desc,
        "capital": CAPITAL,
        "tiers": tiers,
        "active_threshold": active_threshold,
        "max_deploy": max_deploy,
        "sample_t1": sample_t1,
        "sample_t2": sample_t2,
        "sample_loss": sample_loss,
    }


def _get_db():
    """Get MongoDB database connection (lazy, same pattern as user_enroller)."""
    global _client, _db
    if not MONGODB_URI:
        raise ValueError("MONGODB_URI not set")

    # Check if connection is healthy
    if _client is not None:
        try:
            _client.admin.command("ping")
            return _db
        except Exception:
            log.warning("Swing: MongoDB connection lost, reconnecting...")
            _client = None
            _db = None

    # Try with certifi
    try:
        import certifi
        from pymongo import MongoClient
        _client = MongoClient(
            MONGODB_URI,
            tls=True, tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
            retryWrites=True, retryReads=True,
        )
        _client.admin.command("ping")
        _db = _client.udemy_enroller
        log.info("Swing: MongoDB connected (certifi)")
        return _db
    except Exception as e1:
        log.warning("Swing: certifi connection failed: %s", e1)

    # Fallback: tlsAllowInvalidCertificates
    try:
        from pymongo import MongoClient
        _client = MongoClient(
            MONGODB_URI,
            tls=True, tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
            retryWrites=True, retryReads=True,
        )
        _client.admin.command("ping")
        _db = _client.udemy_enroller
        log.info("Swing: MongoDB connected (insecure TLS)")
        return _db
    except Exception as e2:
        log.error("Swing: all MongoDB connection attempts failed: %s", e2)
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
        # Drop rows with NaN Close (incomplete market data)
        raw = raw.dropna(subset=["Close"])
        if raw.empty or len(raw) < 30:
            return None
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

    # EMA 50 (medium-term trend)
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()

    # Momentum indicators
    df["RET_3D"] = df["Close"].pct_change(3)
    df["MOM_10"] = df["Close"].pct_change(10)
    df["MOM_20"] = df["Close"].pct_change(20)
    # 52-week high/low
    df["HIGH_252"] = df["High"].rolling(252).max()
    df["LOW_252"] = df["Low"].rolling(252).min()
    df["DIST_FROM_HIGH"] = (df["HIGH_252"] - df["Close"]) / df["HIGH_252"]
    # VWAP (rolling 20-day)
    df["VWAP"] = (df["Close"] * df["Volume"]).rolling(20).sum() / df["Volume"].rolling(20).sum()
    df["VWAP_DIST"] = (df["Close"] - df["VWAP"]) / df["VWAP"]

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
    stop_loss: float
    target_1: float
    target_2: float
    rsi: float
    bb_pct: float
    atr_pct: float
    vol_ratio: float
    ema_trend: str         # "ABOVE" or "BELOW" 200 EMA
    change_today: float
    reasons: list[str]     # why this stock scored well
    suggested_qty: int     # number of shares
    suggested_invest: float
    expected_profit_t1: float
    expected_profit_t2: float
    expected_loss_sl: float
    risk_reward: str           # e.g. "1:2.4"
    sizing_tier: str           # why this allocation size
    entry_type: str = "MOMENTUM"  # strategy that triggered this setup


def score_stock(symbol: str, df: pd.DataFrame) -> SwingSetup | None:
    """Score a single stock using 3 entry strategies: Momentum, Mean-Reversion, EMA Crossover.

    Backtest results:
      MOMENTUM:  75% WR, +6.27% avg -- best performer
      MEAN_REV:  57.9% WR, +4.00% avg -- solid in oversold markets
      EMA_CROSS: 33.3% WR, -0.26% avg -- weakest, use sparingly
    """
    if df is None or len(df) < 60:
        return None

    df = compute_indicators(df)
    if len(df) < 60:
        return None

    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest

    price = float(latest["Close"])
    rsi = float(latest.get("RSI", 50))
    prev_rsi = float(prev.get("RSI", 50))
    bb_pct = float(latest.get("BB_PCT", 0.5))
    atr_pct = float(latest.get("ATR_PCT", 0))
    vol_ratio = float(latest.get("VOL_RATIO", 1))
    ema200 = float(latest.get("EMA200", price))
    ema50 = float(latest.get("EMA50", price))
    ema9 = float(latest.get("EMA9", price))
    ema21 = float(latest.get("EMA21", price))
    change = float(latest.get("CHANGE_PCT", 0))
    mom10 = float(latest.get("MOM_10", 0))
    mom20 = float(latest.get("MOM_20", 0))

    ema_trend = "ABOVE" if price >= ema200 else "BELOW"

    # -- RULE: Must be in uptrend (above 200 EMA) -- MANDATORY --
    if price < ema200:
        return None

    # -- RULE: Not in a crash (no >5% drop in 3 days) --
    if len(df) >= 4:
        recent_3d_return = (price - float(df.iloc[-4]["Close"])) / float(df.iloc[-4]["Close"])
        if recent_3d_return < -0.05:
            return None

    score = 0.0
    reasons = []
    entry_type = None

    # == STRATEGY 1: MOMENTUM BREAKOUT (75% WR, +6.27% avg) ==
    above_50ema = price >= ema50
    ema_aligned = ema9 > ema21
    strong_momentum = mom10 > 0.02 and mom20 > 0.01
    volume_spike = vol_ratio > 1.3
    rsi_bullish = 45 < rsi < 75
    recent_high = max(float(df.iloc[-j]["High"]) for j in range(2, min(7, len(df))))
    breakout = price >= recent_high * 0.97  # Within 3% of recent high

    momentum_score = 0
    momentum_reasons = []
    if above_50ema and ema_aligned and strong_momentum and volume_spike and rsi_bullish and breakout:
        momentum_score = 80
        momentum_reasons = [
            "🚀 MOMENTUM BREAKOUT",
            f"Price above recent high ({recent_high:.0f})",
            f"EMA9>{ema9:.0f} > EMA21>{ema21:.0f} (bullish)",
            f"Momentum +{mom10*100:.1f}% (10d) +{mom20*100:.1f}% (20d)",
            f"Volume spike {vol_ratio:.1f}x",
        ]

    # == STRATEGY 4: 52-WEEK BREAKOUT (60.9% WR, +4.11% avg) ==
    dist_from_high = float(latest.get("DIST_FROM_HIGH", 1))
    high_252 = float(latest.get("HIGH_252", price))

    wk52_score = 0
    wk52_reasons = []
    if (dist_from_high < 0.02 and vol_ratio > 1.3 and rsi < 80 and mom20 > 0.01):
        wk52_score = 75
        wk52_reasons = [
            "📈 52WK BREAKOUT",
            f"Within {dist_from_high*100:.1f}% of 52-week high",
            f"RSI {rsi:.0f} | MOM20 +{mom20*100:.1f}%",
            f"Volume {vol_ratio:.1f}x",
        ]
        if dist_from_high < 0.005:
            wk52_score += 15
            wk52_reasons.insert(1, "NEW 52-WEEK HIGH!")

    # == STRATEGY 5: MULTI-TIMEFRAME CONFLUENCE (64.7% WR, +4.21% avg) ==
    signals = 0
    mt_reasons = ["📊 MULTI-TF CONFLUENCE"]
    if price >= ema50: signals += 1; mt_reasons.append("Above EMA50")
    if ema9 > ema21 > float(latest.get("EMA50", price)): signals += 1; mt_reasons.append("EMA aligned")
    if 45 < rsi < 65: signals += 1
    if rsi > prev_rsi: signals += 1
    if bb_pct < 0.5: signals += 1
    if vol_ratio > 1.0: signals += 1
    if mom10 > 0: signals += 1
    if 0.01 < atr_pct < 0.03: signals += 1

    mt_score = 60 + (signals - 6) * 8 if signals >= 6 else 0
    if mt_score > 0:
        mt_reasons.append(f"{signals}/8 signals confirming")

    # == STRATEGY 2: MEAN-REVERSION (57.9% WR, +4.00% avg) ==
    rsi_was_low_recent = any(
        float(df.iloc[-j].get("RSI", 50)) < 42
        for j in range(2, min(7, len(df)))
    )
    rsi_turning_up = rsi > prev_rsi and rsi < 55
    bb_near_lower = bb_pct < 0.40
    vol_ok = vol_ratio > 0.8
    no_crash = float(latest.get("RET_3D", 0)) > -0.04 if "RET_3D" in latest.index else True

    mean_rev_score = 0
    mean_rev_reasons = []
    if rsi_was_low_recent and rsi_turning_up and bb_near_lower and vol_ok and no_crash:
        mean_rev_score = 65
        mean_rev_reasons = [
            "🔄 MEAN REVERSION",
            f"RSI reversal ({rsi:.0f}, was <42, now rising)",
            f"Near lower BB ({bb_pct:.2f})",
            f"Volume {vol_ratio:.1f}x",
        ]

    # == STRATEGY 3: EMA CROSSOVER (33.3% WR, weakest) ==
    prev_ema9 = float(prev.get("EMA9", 0))
    prev_ema21 = float(prev.get("EMA21", 0))
    ema_cross_up = (prev_ema9 <= prev_ema21) and (ema9 > ema21)

    ema_cross_score = 0
    ema_cross_reasons = []
    if ema_cross_up and vol_ok and rsi > 35 and rsi < 70 and atr_pct < 0.04:
        ema_cross_score = 40
        ema_cross_reasons = [
            "📈 EMA CROSSOVER",
            "EMA9 crossed above EMA21",
            f"RSI {rsi:.0f}, Vol {vol_ratio:.1f}x",
        ]

    # Pick the best strategy for this stock (5 strategies now)
    strategies = [
        ("MOMENTUM", momentum_score, momentum_reasons),
        ("MEAN_REV", mean_rev_score, mean_rev_reasons),
        ("EMA_CROSS", ema_cross_score, ema_cross_reasons),
        ("52WK_BREAK", wk52_score, wk52_reasons),
        ("MULTI_TF", mt_score, mt_reasons),
    ]
    strategies.sort(key=lambda x: x[1], reverse=True)
    entry_type, best_score, best_reasons = strategies[0]

    if best_score == 0:
        return None

    score = best_score
    reasons = best_reasons

    # -- Additional scoring (max +20 bonus) --
    if vol_ratio > 2.0:
        score += 10
        reasons.append(f"🔥 High volume spike ({vol_ratio:.1f}x)")
    elif vol_ratio > 1.5:
        score += 5

    if 0.01 < atr_pct < 0.03:
        score += 5
        reasons.append(f"Sweet volatility ({atr_pct*100:.1f}% ATR)")

    if ema21 > ema200 and price > ema21:
        score += 5
        reasons.append("Multi-EMA alignment (bullish)")

    # -- Penalty: high volatility = riskier --
    if atr_pct >= 0.04:
        score -= 10
        reasons.append(f"⚠ High volatility ({atr_pct*100:.1f}% ATR)")

    score = min(score, 100)

    if score < 15:
        return None

    qty, invest, sizing_tier = calc_position_size(score, price)

    entry = round(price, 2)
    sl = round(price * (1 - SL_PCT), 2)
    t1 = round(price * (1 + TARGET_PRIMARY), 2)
    t2 = round(price * (1 + TARGET_SECONDARY), 2)

    profit_t1 = round((TARGET_PRIMARY * price) * qty, 0)
    profit_t2 = round((TARGET_SECONDARY * price) * qty, 0)
    loss_sl = round((SL_PCT * price) * qty, 0)
    rr = f"1:{TARGET_SECONDARY / SL_PCT:.1f}" if SL_PCT > 0 else "—"

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
        entry_type=entry_type or "MOMENTUM",
    )


# -- Daily Scanner -----------------------------------------------------------

def scan_nse50(top_n: int = 8) -> list[SwingSetup]:
    """Scan NSE-200 stocks and return top N swing setups sorted by score."""
    all_setups: list[SwingSetup] = []

    for symbol in NSE200:
        df = fetch_history(symbol, period="1y", interval="1d")
        if df is None or df.empty:
            continue
        setup = score_stock(symbol, df)
        if setup:
            all_setups.append(setup)

    all_setups.sort(key=lambda s: s.score, reverse=True)
    return all_setups[:top_n]


# Backtest Engine ───────────────────────────────────────────────────────────

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
    """Backtest swing strategy on a single stock over historical data.

    Improved entry: requires uptrend + RSI reversal confirmation + volume.
    Improved exit: trailing stop after T1, wider SL for 15-20 day holds.
    """
    df = fetch_history(symbol, period="2y", interval="1d")
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
    t1_hit = False       # Track if T1 was hit for trailing
    peak_price = 0.0     # Track peak after entry for trailing

    for i in range(EMA_TREND + 1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]
        prev2_row = df.iloc[i - 2] if i >= 2 else prev_row

        if not in_trade:
            # ── Entry logic: stricter — uptrend + reversal + volume ──
            rsi = float(row.get("RSI", 50))
            prev_rsi = float(prev_row.get("RSI", 50))
            prev2_rsi = float(prev2_row.get("RSI", 50))
            bb_pct = float(row.get("BB_PCT", 0.5))
            vol_ratio = float(row.get("VOL_RATIO", 1))
            price = float(row["Close"])
            prev_close = float(prev_row["Close"])
            ema200 = float(row.get("EMA200", price))
            ema9 = float(row.get("EMA9", price))
            ema21 = float(row.get("EMA21", price))
            atr_pct = float(row.get("ATR_PCT", 0.02))

            # ── RULE 1: Must be in uptrend (above 200 EMA) ──
            if price < ema200:
                continue

            # ── RULE 2: Not in a crash (no >5% drop in 3 days) ──
            recent_3d_return = (price - float(df.iloc[i-3]["Close"])) / float(df.iloc[i-3]["Close"]) if i >= 3 else 0
            if recent_3d_return < -0.05:
                continue

            # ── RULE 3: RSI reversal — was oversold, now turning UP ──
            # RSI was below 42 within last 5 days AND is now rising
            rsi_was_low = any(
                float(df.iloc[i-j].get("RSI", 50)) < 42
                for j in range(1, min(6, i))
            )
            rsi_turning_up = rsi > prev_rsi and rsi < 55

            # ── RULE 4: Bollinger Band near lower band ──
            bb_near_lower = bb_pct < 0.40

            # ── RULE 5: Volume confirmation ──
            vol_ok = vol_ratio > 1.0  # at least average volume

            # ── RULE 6: Not too volatile (ATR < 5%) ──
            vol_stable = atr_pct < 0.05

            # Need uptrend + RSI signal + at least 2 of remaining 4
            rsi_signal = rsi_was_low and rsi_turning_up
            secondary = sum([bb_near_lower, vol_ok, vol_stable])
            entry_signal = rsi_signal and secondary >= 2

            if entry_signal:
                entry_price = float(row["Open"])  # Buy at next day open
                entry_date = str(df.index[i].date())
                entry_idx = i
                t1_hit = False
                peak_price = entry_price
                in_trade = True
        else:
            # ── Exit logic: SL → T1 → trailing stop → T2 → time ──
            high = float(row["High"])
            low = float(row["Low"])
            close = float(row["Close"])
            days_held = i - entry_idx

            sl_price = entry_price * (1 - SL_PCT)
            t1_price = entry_price * (1 + TARGET_PRIMARY)
            t2_price = entry_price * (1 + TARGET_SECONDARY)

            # Track peak for trailing stop
            peak_price = max(peak_price, high)

            exit_price = None
            exit_reason = None

            if t1_hit:
                # ── After T1: trailing stop at 2% below peak ──
                trail_stop = peak_price * (1 - TRAILING_STOP_PCT)
                # But never below breakeven
                trail_stop = max(trail_stop, entry_price)

                if low <= trail_stop:
                    exit_price = trail_stop
                    exit_reason = "TRAIL_STOP"
                elif high >= t2_price:
                    exit_price = t2_price
                    exit_reason = "T2"
                elif days_held >= TIME_STOP_DAYS:
                    exit_price = close
                    exit_reason = "TIME"
            else:
                # ── Before T1: standard SL and target checks ──
                if low <= sl_price:
                    exit_price = sl_price
                    exit_reason = "SL"
                elif high >= t1_price:
                    t1_hit = True
                    # Don't exit yet — trail to T2
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
        symbols = NSE200[:80]  # Top 80 NSE-200 stocks for deep coverage

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

        # Use stored SL/T1/T2 if available, else compute
        sl = trade.get("sl") or entry * (1 - SL_PCT)
        t1 = trade.get("t1") or entry * (1 + TARGET_PRIMARY)
        t2 = trade.get("t2") or entry * (1 + TARGET_SECONDARY)

        # Track peak price for trailing stop
        peak = trade.get("peak_price", entry)
        if high > peak:
            peak = high
        trail_stop = peak * (1 - TRAIL_PCT)

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

        # T1 hit — start trailing at 1% below peak
        elif high >= t1:
            if low <= trail_stop:  # Trailing stop hit
                exit_price = trail_stop
                exit_reason = "TRAIL"
            elif days_held >= TIME_STOP_DAYS:
                exit_price = close
                exit_reason = "TIME"

        # Time stop
        elif days_held >= TIME_STOP_DAYS:
            exit_price = close
            exit_reason = "TIME"

        # Update peak price in DB
        if peak != trade.get("peak_price", entry):
            from bson import ObjectId
            db.swing_trades.update_one(
                {"_id": ObjectId(trade["_id"])},
                {"$set": {"peak_price": round(peak, 2)}},
            )

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
    if db is None:
        return {"closed": [], "opened": [], "already_open": [], "scan_failed": True}
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

    # Step 4: Scan for new setups (request extra to fill all slots)
    try:
        setups = scan_nse50(slots_free + 5)  # request extra in case some are already open
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
                notes=f"{setup.entry_type}|score={setup.score}|reasons={','.join(setup.reasons[:3])}",
            )
            # Also store SL/T1/T2 and entry_type in the document
            db = _get_db()
            db.swing_trades.update_one(
                {"symbol": setup.symbol, "status": "open", "user_id": 0},
                {"$set": {
                    "sl": setup.stop_loss,
                    "t1": setup.target_1,
                    "t2": setup.target_2,
                    "entry_type": setup.entry_type,
                    "peak_price": setup.entry,
                }},
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
                "entry_type": setup.entry_type,
            })
        except Exception as e:
            log.warning("Paper trade open failed %s: %s", setup.symbol, e)

    return actions


def get_paper_portfolio() -> dict:
    """Get current paper portfolio with unrealized P&L."""
    db = _get_db()
    if db is None:
        return {"open": [], "total_unrealized": 0, "total_invested": 0,
                "closed_count": 0, "closed_wins": 0, "closed_losses": 0,
                "total_realized": 0, "recent_closed": []}
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
            "sl": t.get("sl", 0),
            "t1": t.get("t1", 0),
            "t2": t.get("t2", 0),
            "entry_type": t.get("entry_type", "?"),
            "peak_price": t.get("peak_price", t["entry_price"]),
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
