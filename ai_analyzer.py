"""
AI Trade Analyzer — Gemini-Powered Self-Improvement
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Analyzes winning and losing trades
• Generates improvement suggestions as tickets
• Searches for better strategies
• Learns from mistakes
"""
from __future__ import annotations
import os, json, logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Model was hard-coded to gemini-2.0-flash, which Google retired — every call
# returned HTTP 404 and the whole analyzer silently produced nothing. Read the
# same env vars fno_ai.py uses so there is one place to bump the model.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()

# Sentinel returned when no provider could answer. Callers that parse JSON must
# check this rather than feeding an error string to json.loads().
AI_UNAVAILABLE = "AI_UNAVAILABLE"


def _call_groq(prompt: str, system: str = "") -> str | None:
    """Fallback provider. Returns None if unavailable so callers can degrade."""
    if not GROQ_API_KEY:
        return None
    try:
        import requests
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.3},
            timeout=30,
        )
        if resp.status_code != 200:
            log.error("Groq API error: %s %s", resp.status_code, resp.text[:200])
            return None
        choices = resp.json().get("choices") or [{}]
        return (choices[0].get("message", {}).get("content") or "").strip() or None
    except Exception as e:
        log.error("Groq call failed: %s", e)
        return None


def _call_gemini(prompt: str, system: str = "") -> str:
    """Call Gemini, falling back to Groq. Returns AI_UNAVAILABLE on total failure."""
    if GEMINI_API_KEY:
        try:
            import requests
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{GEMINI_MODEL}:generateContent"
            )
            payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
            if system:
                payload["systemInstruction"] = {"parts": [{"text": system}]}
            resp = requests.post(
                url, json=payload, timeout=30,
                headers={"Content-Type": "application/json",
                         "x-goog-api-key": GEMINI_API_KEY},
            )
            if resp.status_code == 200:
                parts = (resp.json().get("candidates", [{}])[0]
                         .get("content", {}).get("parts", []) or [])
                text = "".join(p.get("text", "") for p in parts).strip()
                if text:
                    return text
                log.warning("Gemini returned an empty response; trying Groq")
            else:
                log.error("Gemini API error: %s %s — trying Groq",
                          resp.status_code, resp.text[:200])
        except Exception as e:
            log.error("Gemini call failed: %s — trying Groq", e)

    fallback = _call_groq(prompt, system)
    if fallback:
        return fallback

    log.error("No AI provider available (Gemini and Groq both failed)")
    return AI_UNAVAILABLE

def analyze_trade(trade: dict) -> str:
    """Analyze a single trade with Gemini AI."""
    system = """You are an expert Indian stock market swing trader. Analyze trades with specific, actionable insights.
Focus on: entry timing, sector context, technical setup quality, risk-reward assessment.
Be concise (3-4 sentences max). Use Indian market context (NSE, sectors, FII/DII flows)."""

    prompt = f"""Analyze this swing trade:

Symbol: {trade.get('symbol', '?')}
Sector: {trade.get('sector', '?')}
Strategy: {trade.get('strategy', '?')}
Entry: ₹{trade.get('entry_price', 0)} on {trade.get('entry_date', '?')}
Exit: ₹{trade.get('exit_price', 0)} on {trade.get('exit_date', '?')}
P&L: {trade.get('pnl_pct', 0):+.2f}%
Exit Reason: {trade.get('exit_reason', '?')}
Holding Days: {trade.get('holding_days', 0)}
Score: {trade.get('score', 0)}
Entry Reasons: {', '.join(trade.get('reasons', []))}

What went {'right' if trade.get('pnl_pct', 0) > 0 else 'wrong'}? 
What should the trader do differently next time?
Rate the setup quality 1-10."""

    return _call_gemini(prompt, system)

def generate_improvement_suggestions(recent_trades: list[dict], stats: dict) -> list[dict]:
    """Analyze recent trades and generate improvement suggestions."""
    if not recent_trades or len(recent_trades) < 5:
        return []

    system = """You are a quantitative trading strategist. Analyze trade data and suggest specific parameter changes.
Return JSON array of suggestions. Each suggestion has: title, category (entry/exit/sizing/sector/regime), 
priority (high/medium/low), before (current value), after (suggested value), rationale (why).
Be specific and data-driven. Focus on changes that would improve win rate or risk-reward."""

    # Format trade data for AI
    trade_summary = []
    for t in recent_trades[:20]:
        trade_summary.append({
            "symbol": t.get("symbol"), "sector": t.get("sector"),
            "strategy": t.get("strategy"), "pnl": t.get("pnl_pct"),
            "exit_reason": t.get("exit_reason"), "days": t.get("holding_days"),
            "score": t.get("score"), "reasons": t.get("reasons", []),
        })

    prompt = f"""Analyze these recent trades and suggest improvements:

RECENT TRADES ({len(recent_trades)} total):
{json.dumps(trade_summary, indent=1, default=str)}

STATS:
- Win Rate: {stats.get('wr', 0)}%
- Avg Return: {stats.get('avg_pnl', 0)}%
- Avg Win: +{stats.get('avg_win', 0)}%
- Avg Loss: {stats.get('avg_loss', 0)}%
- Best: +{stats.get('best', 0)}%
- Worst: {stats.get('worst', 0)}%
- Streak: {stats.get('streak', 'none')}

BY STRATEGY: {json.dumps(stats.get('by_strategy', {}), default=str)}
BY SECTOR: {json.dumps(stats.get('by_sector', {}), default=str)}

Return ONLY a JSON array (no markdown) with 2-4 improvement suggestions.
Each: {{"title": "...", "category": "entry|exit|sizing|sector|regime", "priority": "high|medium|low", "before": "current value", "after": "suggested value", "rationale": "data-driven reason"}}"""

    response = _call_gemini(prompt, system)
    if response == AI_UNAVAILABLE:
        log.warning("Skipping improvement suggestions — no AI provider reachable")
        return []

    # Parse JSON from response
    try:
        # Try to extract JSON from response
        import re
        json_match = re.search(r'\[[\s\S]*?\]', response)
        if json_match:
            suggestions = json.loads(json_match.group())
            return suggestions
        else:
            log.warning("No JSON found in AI response: %s", response[:200])
            return []
    except json.JSONDecodeError as e:
        log.warning("Failed to parse AI suggestions: %s", e)
        return []

def search_strategy_improvements(current_strategy: str, recent_performance: dict) -> str:
    """Search for better strategies using Gemini."""
    system = """You are a quantitative finance researcher. Suggest evidence-based strategy improvements.
Focus on: parameter optimization, regime detection, sector rotation, risk management.
Be specific with numbers and timeframes. Reference Indian market conditions."""

    prompt = f"""Current strategy: {current_strategy}

Recent performance:
- Win Rate: {recent_performance.get('wr', 0)}%
- Avg Return: {recent_performance.get('avg_pnl', 0)}%
- Profit Factor: {recent_performance.get('pf', 'N/A')}
- Max Drawdown: {recent_performance.get('mdd', 'N/A')}%

The trader needs to make 6-7% monthly from ₹1L capital.

Suggest 3 specific improvements with:
1. What to change (parameter or rule)
2. Expected impact (quantified if possible)
3. Risk of the change
4. Implementation complexity (low/medium/high)

Focus on changes that increase trade frequency WITHOUT sacrificing win rate."""

    return _call_gemini(prompt, system)

def daily_market_analysis(nifty_data: dict) -> str:
    """Get AI analysis of current market conditions."""
    system = """You are an Indian market analyst. Analyze Nifty 50 data and give a 2-sentence market outlook.
Focus on: trend direction, key levels, sector rotation hints. Be actionable."""

    prompt = f"""Current Nifty 50 data:
- Price: {nifty_data.get('price', '?')}
- RSI: {nifty_data.get('rsi', '?')}
- 200 EMA: {nifty_data.get('ema200', '?')}
- Position vs 200 EMA: {'Above' if nifty_data.get('price', 0) > nifty_data.get('ema200', 0) else 'Below'}
- VIX: {nifty_data.get('vix', '?')}
- Today's change: {nifty_data.get('change', '?')}%

What market regime is this? (trending up, ranging, trending down, volatile)
Which sectors should the trader focus on today?"""

    return _call_gemini(prompt, system)

def analyze_win_loss_pattern(trades: list[dict]) -> dict:
    """Find patterns in winning vs losing trades."""
    wins = [t for t in trades if t.get("pnl_pct", 0) > 0]
    losses = [t for t in trades if t.get("pnl_pct", 0) <= 0]

    if not wins or not losses:
        return {"pattern": "Insufficient data", "insights": []}

    # Common patterns
    win_sectors = {}
    loss_sectors = {}
    for t in wins:
        s = t.get("sector", "Other")
        win_sectors[s] = win_sectors.get(s, 0) + 1
    for t in losses:
        s = t.get("sector", "Other")
        loss_sectors[s] = loss_sectors.get(s, 0) + 1

    win_strategies = {}
    loss_strategies = {}
    for t in wins:
        s = t.get("strategy", "unknown")
        win_strategies[s] = win_strategies.get(s, 0) + 1
    for t in losses:
        s = t.get("strategy", "unknown")
        loss_strategies[s] = loss_strategies.get(s, 0) + 1

    # Avg holding days
    avg_win_hold = sum(t.get("holding_days", 0) for t in wins) / len(wins) if wins else 0
    avg_loss_hold = sum(t.get("holding_days", 0) for t in losses) / len(losses) if losses else 0

    # Exit reason analysis
    loss_reasons = {}
    for t in losses:
        r = t.get("exit_reason", "unknown")
        loss_reasons[r] = loss_reasons.get(r, 0) + 1

    insights = []
    if win_sectors:
        best_sector = max(win_sectors, key=win_sectors.get)
        insights.append(f"Best sector: {best_sector} ({win_sectors[best_sector]} wins)")
    if loss_sectors:
        worst_sector = max(loss_reasons, key=loss_reasons.get) if loss_reasons else "?"
        insights.append(f"Most losses from: {worst_sector}")
    if avg_win_hold > 0 and avg_loss_hold > 0:
        insights.append(f"Winners hold {avg_win_hold:.1f}d vs losers {avg_loss_hold:.1f}d")
    if loss_reasons:
        top_loss_reason = max(loss_reasons, key=loss_reasons.get)
        insights.append(f"Top loss reason: {top_loss_reason} ({loss_reasons[top_loss_reason]}x)")

    return {
        "win_sectors": win_sectors,
        "loss_sectors": loss_sectors,
        "win_strategies": win_strategies,
        "loss_strategies": loss_strategies,
        "avg_win_hold": round(avg_win_hold, 1),
        "avg_loss_hold": round(avg_loss_hold, 1),
        "loss_reasons": loss_reasons,
        "insights": insights,
    }
