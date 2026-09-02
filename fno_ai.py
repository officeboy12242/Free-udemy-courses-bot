"""
Async AI commentary for F&O trade messages.

Reuses the AI API keys from the whatsapp-bot project (.env):
  GEMINI_API_KEY  -> generativelanguage.googleapis.com (gemini-2.5-flash)
  GROQ_API_KEY    -> api.groq.com/openai/v1/chat/completions (openai/gpt-oss-120b)

Every call is best-effort: short timeout, graceful None on any failure so the
trading bot never depends on the LLM being up.
"""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
AI_TIMEOUT = float(os.getenv("FNO_AI_TIMEOUT", "12"))

_SYSTEM_PROMPT = (
    "You are a disciplined Nifty index-options scalp trader's assistant. "
    "Reply in 1-2 short lines, plain text, no markdown, no emojis, no disclaimers. "
    "Be decisive: state HOLD or EXIT with ONE reason and the exact price level to watch."
)


def _clean(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    # Strip stray markdown fences some models insist on adding.
    text = text.replace("```", "").strip()
    return text or None


async def _gemini(system: str, user: str) -> str | None:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 250},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=AI_TIMEOUT),
        ) as resp:
            data = await resp.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", []) or []
    return _clean("".join(p.get("text", "") for p in parts))


async def _groq(system: str, user: str) -> str | None:
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 250,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=AI_TIMEOUT),
        ) as resp:
            data = await resp.json()
    choices = data.get("choices") or [{}]
    return _clean(choices[0].get("message", {}).get("content", ""))


async def ai_trade_commentary(
    kind: str,
    *,
    name: str = "",
    side: str = "",
    strike: str | int = "",
    strategy: str = "",
    spot: float | None = None,
    entry: float | None = None,
    live: float | None = None,
    sl: float | None = None,
    pnl_rs: float | None = None,
    capital: float | None = None,
    breaches: int = 0,
) -> str | None:
    """One short actionable line for entry / hold / exit decisions. None = no AI."""
    if not GEMINI_API_KEY and not GROQ_API_KEY:
        return None

    sym = f"{name} {side} {strike}".strip()
    entry_s = f"{entry:.2f}" if entry is not None else "?"
    live_s = f"{live:.2f}" if live is not None else "?"
    sl_s = f"{sl:.2f}" if sl is not None else "?"
    pnl_s = f"{pnl_rs:+,.0f} rs" if pnl_rs is not None else "?"
    cap_s = f"{capital:,.0f} rs" if capital is not None else "?"

    if kind == "entry":
        user = (
            f"{sym} scalp ENTRY @ {entry_s}, strategy: {strategy or '?'}, spot: {spot or '?'}. "
            f"One-line outlook and the key price level that invalidates the trade."
        )
    elif kind == "sl_touch":
        user = (
            f"{sym} entered @ {entry_s}, now {live_s}, SL {sl_s}, first tick below SL. "
            f"Bot rule: a first tick within 4% below SL is a wick - HOLD, exit only on a "
            f"2nd consecutive check below SL or a deeper breach. Given the setup, is there "
            f"any reason to exit NOW? One decisive HOLD or EXIT with the exact level that "
            f"forces the exit."
        )
    elif kind == "status":
        user = (
            f"{sym} entered @ {entry_s}, now {live_s}, SL {sl_s}, P&L {pnl_s}. "
            f"HOLD or BOOK part? One line: action + the level that changes the plan."
        )
    else:  # exit recap
        user = (
            f"{sym} entered @ {entry_s}, closed near {live_s}, P&L {pnl_s}, "
            f"day capital left {cap_s}. One-line recap and one rule for the next trade."
        )

    try:
        if GEMINI_API_KEY:
            try:
                return await asyncio.wait_for(
                    _gemini(_SYSTEM_PROMPT, user), timeout=AI_TIMEOUT + 3,
                )
            except Exception as e:
                log.warning("Gemini commentary failed: %s", e)
        if GROQ_API_KEY:
            try:
                return await asyncio.wait_for(
                    _groq(_SYSTEM_PROMPT, user), timeout=AI_TIMEOUT + 3,
                )
            except Exception as e:
                log.warning("Groq commentary failed: %s", e)
    except Exception as e:
        log.warning("AI commentary unavailable: %s", e)
    return None
