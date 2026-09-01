"""
Market Regime Detection — Trending / Ranging / Volatile
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Detects current market regime using Nifty 50
• Selects optimal strategy for each regime
• Adjusts position sizing based on volatility
"""
from __future__ import annotations
import os, logging
from datetime import datetime

log = logging.getLogger(__name__)

def detect_regime() -> dict:
    """Detect current market regime from Nifty 50 data.
    
    Returns:
        regime: "trending_up", "trending_down", "ranging", "volatile"
        strategy: recommended strategy for this regime
        confidence: 0-100
        details: explanation
    """
    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np
        
        # Fetch Nifty 50 data
        nifty = yf.download("^NSEI", period="6mo", interval="1d", progress=False)
        if nifty is None or nifty.empty:
            return {"regime": "unknown", "strategy": "mean_reversion", "confidence": 0, "details": "No data"}
        
        # Flatten MultiIndex if present
        if hasattr(nifty.columns, 'levels') and len(nifty.columns.levels) > 1:
            nifty.columns = [c[0] if isinstance(c, tuple) else c for c in nifty.columns]
        
        close = nifty["Close"].values
        price = float(close[-1])
        
        # 200 EMA (use what we have, approximate if <200 days)
        if len(close) >= 200:
            ema200 = float(pd.Series(close).ewm(span=200).mean().iloc[-1])
        else:
            ema200 = float(pd.Series(close).ewm(span=min(len(close), 50)).mean().iloc[-1])
        
        # RSI
        delta = pd.Series(close).diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = float((100 - (100 / (1 + rs))).iloc[-1])
        
        # ATR for volatility
        high = nifty["High"].values
        low = nifty["Low"].values
        tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
        atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))
        atr_pct = (atr / price) * 100
        
        # VIX (India VIX)
        try:
            vix_data = yf.download("^INDIAVIX", period="5d", interval="1d", progress=False)
            if vix_data is not None and not vix_data.empty:
                if hasattr(vix_data.columns, 'levels'):
                    vix_data.columns = [c[0] if isinstance(c, tuple) else c for c in vix_data.columns]
                vix = float(vix_data["Close"].iloc[-1])
            else:
                vix = 15.0
        except:
            vix = 15.0
        
        # 20-day momentum
        if len(close) >= 20:
            momentum_20d = ((price - float(close[-20])) / float(close[-20])) * 100
        else:
            momentum_20d = 0
        
        # ── Regime Detection Logic ──
        above_ema200 = price > ema200
        strong_trend = abs(momentum_20d) > 3  # >3% in 20 days = strong
        high_vol = atr_pct > 1.5 or vix > 20
        oversold = rsi < 30
        overbought = rsi > 70
        
        if high_vol and not strong_trend:
            regime = "volatile"
            strategy = "cash"  # Stay out in volatile markets
            confidence = 80 if vix > 25 else 60
            details = f"High volatility: ATR {atr_pct:.1f}%, VIX {vix:.0f}. Stay in cash."
        elif above_ema200 and strong_trend and momentum_20d > 0:
            regime = "trending_up"
            strategy = "momentum"
            confidence = 75 if momentum_20d > 5 else 60
            details = f"Trending UP: +{momentum_20d:.1f}% in 20d, above 200 EMA. Use momentum breakout."
        elif not above_ema200 and strong_trend and momentum_20d < 0:
            regime = "trending_down"
            strategy = "cash"
            confidence = 70
            details = f"Trending DOWN: {momentum_20d:.1f}% in 20d, below 200 EMA. Stay in cash."
        elif oversold and not high_vol:
            regime = "ranging_oversold"
            strategy = "mean_reversion"
            confidence = 65
            details = f"Ranging + oversold: RSI {rsi:.0f}. Use mean-reversion (buy dips)."
        elif overbought and not high_vol:
            regime = "ranging_overbought"
            strategy = "reduce"
            confidence = 60
            details = f"Ranging + overbought: RSI {rsi:.0f}. Reduce positions."
        else:
            regime = "ranging"
            strategy = "mean_reversion"
            confidence = 50
            details = f"Ranging: Price near EMA, RSI {rsi:.0f}. Use mean-reversion."
        
        return {
            "regime": regime,
            "strategy": strategy,
            "confidence": confidence,
            "details": details,
            "price": round(price, 0),
            "ema200": round(ema200, 0),
            "rsi": round(rsi, 1),
            "atr_pct": round(atr_pct, 2),
            "vix": round(vix, 1),
            "momentum_20d": round(momentum_20d, 2),
            "above_ema200": above_ema200,
        }
    except Exception as e:
        log.error("Regime detection failed: %s", e)
        return {"regime": "unknown", "strategy": "mean_reversion", "confidence": 0, "details": str(e)}

def get_regime_emoji(regime: str) -> str:
    """Get emoji for regime."""
    return {
        "trending_up": "🟢📈", "trending_down": "🔴📉",
        "ranging": "🟡↔️", "ranging_oversold": "🟡🔵",
        "ranging_overbought": "🟡🔴", "volatile": "🟠⚡",
        "unknown": "⚪❓",
    }.get(regime, "⚪")

def get_strategy_description(strategy: str) -> str:
    """Get human-readable strategy description."""
    return {
        "momentum": "Momentum Breakout — Buy stocks breaking out with volume",
        "mean_reversion": "Mean Reversion — Buy oversold bounces at support",
        "cash": "Cash — Stay out, protect capital",
        "reduce": "Reduce — Take profits, reduce exposure",
    }.get(strategy, "Unknown")
