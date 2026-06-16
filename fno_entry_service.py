"""
Scalping F&O entry sheet for Indian index options.

Strategy: EMA + RSI + OI Confluence  +  VWAP Bounce  (combo)
───────────────────────────────────────────────────────────────
Signal fires only when multiple layers agree:

Layer 1 — VWAP:  Price vs Volume-Weighted Avg Price
          Above VWAP = bullish zone, below = bearish zone
          Bounce off VWAP = high-probability scalp entry

Layer 2 — EMA:   9-EMA vs 21-EMA on 15m candles
          9 > 21 = micro uptrend, 9 < 21 = micro downtrend

Layer 3 — RSI:   7-period RSI on 15m
          > 55 confirms bullish momentum, < 45 confirms bearish

Layer 4 — OI:    PCR, ATM OI change, nearby OI walls (NSE live)
          Confirms smart-money positioning for direction

Confluence scoring:
  Each layer votes +weight (CE) or −weight (PE).
  STRONG = 3-4 layers agree. MODERATE = 2 layers. LIGHT = 1 or less.
  Always picks a side — but labels conviction so you size accordingly.

Data: Yahoo 15m candles + NSE option chain (jugaad-data).
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from jugaad_data.nse import NSELive

from market_service import fetch_snapshot

load_dotenv()

log = logging.getLogger(__name__)

FNO_INDICES: list[dict[str, Any]] = [
    {
        "nse": "NIFTY", "yahoo": "^NSEI", "name": "Nifty 50",
        "step": 50, "prem_min": 55, "prem_max": 160,
    },
    {
        "nse": "BANKNIFTY", "yahoo": "^NSEBANK", "name": "Bank Nifty",
        "step": 100, "prem_min": 220, "prem_max": 520,
    },
    {
        "nse": "FINNIFTY", "yahoo": "NIFTY_FIN_SERVICE.NS", "name": "Fin Nifty",
        "step": 50, "prem_min": 120, "prem_max": 380,
    },
    {
        "nse": "MIDCPNIFTY", "yahoo": "NIFTY_MID_SELECT.NS", "name": "Midcap Nifty",
        "step": 25, "prem_min": 80, "prem_max": 260,
    },
]

# Risk:Reward for scalps (applied on entry premium)
SL_MULT = 0.86      # SL = entry * 0.86  → risk ~14%
T1_MULT = 1.20      # T1 = entry * 1.20  → reward ~20%  (book 50%)
T2_MULT = 1.35      # T2 = entry * 1.35  → reward ~35%  (trail rest)


# ────────────────── Technical helpers ──────────────────

def _round_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def _rsi(series: pd.Series, period: int = 7) -> float | None:
    if series is None or len(series) < period + 1:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    last_loss = loss.iloc[-1]
    if last_loss == 0 or math.isnan(last_loss):
        return 100.0
    rs = gain.iloc[-1] / last_loss
    val = 100 - (100 / (1 + rs))
    return float(val) if not math.isnan(val) else None


def _ema(series: pd.Series, span: int) -> float | None:
    if series is None or len(series) < span:
        return None
    val = series.ewm(span=span, adjust=False).mean().iloc[-1]
    return float(val) if not math.isnan(val) else None


def _vwap(df: pd.DataFrame) -> float | None:
    """Cumulative VWAP from intraday OHLCV."""
    try:
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        volume = df["Volume"]
        if volume.sum() == 0:
            return None
        vwap = (typical * volume).cumsum() / volume.cumsum()
        val = float(vwap.iloc[-1])
        return val if not math.isnan(val) else None
    except Exception:
        return None


# ────────────────── Data fetching ──────────────────

def _fetch_scalp_technicals(yahoo_symbol: str) -> dict[str, Any]:
    """15m intraday candles → VWAP, EMA9/21, RSI7, momentum."""
    out: dict[str, Any] = {"yahoo": yahoo_symbol}
    try:
        t = yf.Ticker(yahoo_symbol)
        df = t.history(period="5d", interval="15m")
        use_intraday = df is not None and not df.empty and len(df) >= 25
        if not use_intraday:
            df = t.history(period="30d", interval="1d")
        if df is None or df.empty or len(df) < 10:
            return out

        close = df["Close"].dropna()
        last = float(close.iloc[-1])

        rsi_val = _rsi(close, 7 if use_intraday else 14)
        ema9 = _ema(close, 9)
        ema21 = _ema(close, 21 if use_intraday else 20)

        vwap_val = _vwap(df) if use_intraday else None

        lookback = 4 if use_intraday else 2
        ref = float(close.iloc[-1 - lookback]) if len(close) > lookback else last
        mom = (last - ref) / ref * 100.0 if ref else 0.0

        snap = fetch_snapshot(yahoo_symbol, yahoo_symbol)
        if snap:
            last = float(snap["last"])
            day_pct = float(snap["pct_change"])
        else:
            prev = float(close.iloc[-2]) if len(close) >= 2 else last
            day_pct = (last - prev) / prev * 100.0 if prev else 0.0

        out.update({
            "spot": round(last, 2),
            "pct_change": round(day_pct, 3),
            "mom_pct": round(mom, 3),
            "rsi": round(rsi_val, 1) if rsi_val is not None else None,
            "ema9": round(ema9, 2) if ema9 is not None else None,
            "ema21": round(ema21, 2) if ema21 is not None else None,
            "vwap": round(vwap_val, 2) if vwap_val is not None else None,
            "timeframe": "15m" if use_intraday else "1d",
        })
    except Exception as e:
        log.warning("Scalp technicals failed for %s: %s", yahoo_symbol, e)
    return out


def _parse_option_chain(nse_symbol: str) -> dict[str, Any] | None:
    try:
        nse = NSELive()
        raw = nse.index_option_chain(nse_symbol)
        rec = raw.get("records")
        if not rec:
            return None
        spot = float(rec["underlyingValue"])
        expiry = rec["expiryDates"][0]
        rows = [d for d in rec.get("data", []) if d.get("expiryDates") == expiry]
        if not rows:
            return None
        return {"spot": spot, "expiry": expiry, "rows": rows}
    except Exception as e:
        log.warning("Option chain failed for %s: %s", nse_symbol, e)
        return None


def _leg_quote(row: dict, side: str) -> dict[str, Any]:
    leg = row.get(side) or {}
    bid = float(leg.get("buyPrice1") or 0)
    ask = float(leg.get("sellPrice1") or 0)
    ltp = float(leg.get("lastPrice") or 0)
    if ltp <= 0 and bid > 0 and ask > 0:
        ltp = round((bid + ask) / 2, 2)
    elif ltp <= 0 and ask > 0:
        ltp = ask
    elif ltp <= 0 and bid > 0:
        ltp = bid
    return {
        "ltp": ltp,
        "oi": int(leg.get("openInterest") or 0),
        "chg_oi": int(leg.get("changeinOpenInterest") or 0),
        "iv": float(leg.get("impliedVolatility") or 0),
        "volume": int(leg.get("totalTradedVolume") or 0),
    }


# ────────────────── OI analysis ──────────────────

def _oi_analysis(rows: list[dict], spot: float, step: int) -> dict[str, Any]:
    """PCR, OI walls (support/resistance), ATM OI change bias."""
    atm = _round_strike(spot, step)
    total_ce_oi = total_pe_oi = 0
    ce_chg = pe_chg = 0
    best_pe_strike = best_ce_strike = atm
    best_pe_oi = best_ce_oi = -1
    atm_ce_chg = atm_pe_chg = 0

    for row in rows:
        strike = int(row["strikePrice"])
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        c_oi = int(ce.get("openInterest") or 0)
        p_oi = int(pe.get("openInterest") or 0)
        total_ce_oi += c_oi
        total_pe_oi += p_oi
        ce_chg += int(ce.get("changeinOpenInterest") or 0)
        pe_chg += int(pe.get("changeinOpenInterest") or 0)
        if p_oi > best_pe_oi:
            best_pe_oi = p_oi
            best_pe_strike = strike
        if c_oi > best_ce_oi:
            best_ce_oi = c_oi
            best_ce_strike = strike
        if strike == atm:
            atm_ce_chg = int(ce.get("changeinOpenInterest") or 0)
            atm_pe_chg = int(pe.get("changeinOpenInterest") or 0)

    pcr = total_pe_oi / total_ce_oi if total_ce_oi else 1.0
    return {
        "pcr": round(pcr, 2),
        "support": best_pe_strike,
        "resistance": best_ce_strike,
        "net_ce_chg": ce_chg,
        "net_pe_chg": pe_chg,
        "atm_ce_chg": atm_ce_chg,
        "atm_pe_chg": atm_pe_chg,
    }


# ────────────────── Confluence strategy ──────────────────

def _confluence_score(
    tech: dict[str, Any],
    oi: dict[str, Any],
) -> tuple[str, str, int, list[str]]:
    """
    4-layer confluence:
      VWAP   → is price above or below VWAP? bounce?
      EMA    → is EMA9 > EMA21 (or below)?
      RSI    → is momentum confirming (>55 bull, <45 bear)?
      OI     → does PCR + ATM OI change + wall proximity agree?

    Returns (side, strength, layers_agreed, reasons).
    """
    spot = tech.get("spot")
    vwap = tech.get("vwap")
    ema9 = tech.get("ema9")
    ema21 = tech.get("ema21")
    rsi = tech.get("rsi")
    mom = float(tech.get("mom_pct") or 0)
    day_pct = float(tech.get("pct_change") or 0)
    tf = tech.get("timeframe", "15m")

    pcr = float(oi.get("pcr") or 1)
    atm_ce_chg = int(oi.get("atm_ce_chg") or 0)
    atm_pe_chg = int(oi.get("atm_pe_chg") or 0)
    support = oi.get("support")
    resistance = oi.get("resistance")

    votes = 0      # positive = CE layers, negative = PE layers
    layers = 0     # how many layers fired
    reasons: list[str] = []

    # ── Layer 1: VWAP ──
    if spot and vwap:
        dist_pct = (spot - vwap) / vwap * 100
        if spot > vwap and dist_pct < 0.15:
            votes += 1; layers += 1
            reasons.append(f"VWAP bounce — price {spot} just above VWAP {vwap}")
        elif spot > vwap:
            votes += 1; layers += 1
            reasons.append(f"Above VWAP ({vwap}) — bullish zone")
        elif spot < vwap and dist_pct > -0.15:
            votes -= 1; layers += 1
            reasons.append(f"VWAP reject — price {spot} just below VWAP {vwap}")
        elif spot < vwap:
            votes -= 1; layers += 1
            reasons.append(f"Below VWAP ({vwap}) — bearish zone")
    else:
        if day_pct > 0.15:
            votes += 1; layers += 1
            reasons.append(f"Day +{day_pct:.2f}% (VWAP N/A, using day trend)")
        elif day_pct < -0.15:
            votes -= 1; layers += 1
            reasons.append(f"Day {day_pct:.2f}% (VWAP N/A, using day trend)")

    # ── Layer 2: EMA crossover ──
    if spot and ema9 and ema21:
        if ema9 > ema21 and spot > ema9:
            votes += 1; layers += 1
            reasons.append(f"EMA9({ema9:.0f}) > EMA21({ema21:.0f}), price above both")
        elif ema9 < ema21 and spot < ema9:
            votes -= 1; layers += 1
            reasons.append(f"EMA9({ema9:.0f}) < EMA21({ema21:.0f}), price below both")
        elif ema9 > ema21:
            votes += 1; layers += 1
            reasons.append(f"EMA9 > EMA21 — uptrend ({tf})")
        elif ema9 < ema21:
            votes -= 1; layers += 1
            reasons.append(f"EMA9 < EMA21 — downtrend ({tf})")
    else:
        if mom > 0.1:
            votes += 1; layers += 1
            reasons.append(f"Momentum +{mom:.2f}% (EMA N/A)")
        elif mom < -0.1:
            votes -= 1; layers += 1
            reasons.append(f"Momentum {mom:.2f}% (EMA N/A)")

    # ── Layer 3: RSI confirmation ──
    if rsi is not None:
        if rsi >= 55:
            votes += 1; layers += 1
            reasons.append(f"RSI-7 = {rsi:.0f} — bullish momentum")
        elif rsi <= 45:
            votes -= 1; layers += 1
            reasons.append(f"RSI-7 = {rsi:.0f} — bearish momentum")
        else:
            reasons.append(f"RSI-7 = {rsi:.0f} — neutral (no layer vote)")

    # ── Layer 4: OI confluence ──
    oi_vote = 0
    oi_reasons: list[str] = []
    if pcr > 1.15:
        oi_vote += 1
        oi_reasons.append(f"PCR {pcr:.2f} (PE heavy → bullish)")
    elif pcr < 0.85:
        oi_vote -= 1
        oi_reasons.append(f"PCR {pcr:.2f} (CE heavy → bearish)")

    atm_net = atm_ce_chg - atm_pe_chg
    if atm_net > 500:
        oi_vote += 1
        oi_reasons.append("ATM: CE OI adding faster → buying pressure")
    elif atm_net < -500:
        oi_vote -= 1
        oi_reasons.append("ATM: PE OI adding faster → selling pressure")

    if spot and support and resistance:
        dist_to_sup = abs(spot - support) / spot * 100 if support else 99
        dist_to_res = abs(spot - resistance) / spot * 100 if resistance else 99
        if dist_to_sup < 0.5:
            oi_vote += 1
            oi_reasons.append(f"Near PE-OI wall {support} (support)")
        elif dist_to_res < 0.5:
            oi_vote -= 1
            oi_reasons.append(f"Near CE-OI wall {resistance} (resistance)")

    if oi_vote != 0:
        layers += 1
        if oi_vote > 0:
            votes += 1
        else:
            votes -= 1
    for r in oi_reasons:
        reasons.append(r)

    # ── Decision ──
    agreed = abs(votes)
    side = "CE" if votes >= 0 else "PE"
    if votes == 0:
        side = "CE" if day_pct >= 0 else "PE"
        reasons.append("No clear confluence — siding with day trend")

    if agreed >= 3:
        strength = "STRONG"
    elif agreed >= 2:
        strength = "MODERATE"
    else:
        strength = "WEAK"

    return side, strength, agreed, reasons


# ────────────────── Strike selection ──────────────────

def _pick_scalp_strike(
    rows: list[dict],
    spot: float,
    step: int,
    side: str,
    prem_min: float,
    prem_max: float,
) -> tuple[int, dict[str, Any]]:
    atm = _round_strike(spot, step)
    by_strike = {int(r["strikePrice"]): r for r in rows}
    min_tradeable = max(12.0, prem_min * 0.22)

    offsets = (0, -1, -2, 1, -3, 2, -4) if side == "CE" else (0, 1, 2, -1, 3, -2, 4)

    candidates: list[tuple[int, dict, float]] = []
    for off in offsets:
        strike = atm + off * step if side == "CE" else atm - off * step
        row = by_strike.get(strike)
        if not row:
            continue
        q = _leg_quote(row, side)
        if q["ltp"] < min_tradeable:
            continue
        mid = (prem_min + prem_max) / 2
        in_band = prem_min <= q["ltp"] <= prem_max
        band_bonus = 40 if in_band else -abs(q["ltp"] - mid) * 0.5
        score = band_bonus + q["oi"] * 2 + q["volume"] * 5 - abs(strike - atm) * 0.3
        candidates.append((strike, q, score))

    if not candidates:
        for off in offsets:
            strike = atm + off * step if side == "CE" else atm - off * step
            row = by_strike.get(strike)
            if not row:
                continue
            q = _leg_quote(row, side)
            if q["ltp"] > 0:
                candidates.append((strike, q, -abs(strike - atm)))

    if not candidates:
        return atm, _leg_quote(by_strike.get(atm, {}), side)

    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[0][0], candidates[0][1]


# ────────────────── Exit levels ──────────────────

def _scalp_exits(entry_premium: float) -> dict[str, float]:
    prem = max(entry_premium, 5.0)
    sl = round(prem * SL_MULT, 2)
    t1 = round(prem * T1_MULT, 2)
    t2 = round(prem * T2_MULT, 2)
    return {
        "entry": round(prem, 2),
        "sl": sl,
        "t1": t1,
        "t2": t2,
        "sl_pts": round(prem - sl, 2),
        "t1_pts": round(t1 - prem, 2),
        "t2_pts": round(t2 - prem, 2),
        "rr": round((t1 - prem) / (prem - sl), 1) if prem > sl else 0.0,
    }


# ────────────────── Main analysis ──────────────────

def analyze_index(cfg: dict[str, Any]) -> dict[str, Any]:
    tech = _fetch_scalp_technicals(cfg["yahoo"])
    chain = _parse_option_chain(cfg["nse"])
    if not chain:
        return {
            "name": cfg["name"], "nse": cfg["nse"],
            "error": "Option chain unavailable", "tech": tech,
        }

    spot = float(chain["spot"])
    rows = chain["rows"]
    oi = _oi_analysis(rows, spot, cfg["step"])
    side, strength, agreed, reasons = _confluence_score(tech, oi)
    strike, leg = _pick_scalp_strike(
        rows, spot, cfg["step"], side, cfg["prem_min"], cfg["prem_max"]
    )
    entry_prem = float(leg.get("ltp") or 0)
    exits = _scalp_exits(entry_prem)

    return {
        "name": cfg["name"],
        "nse": cfg["nse"],
        "expiry": chain["expiry"],
        "spot": round(spot, 2),
        "tech": tech,
        "oi": oi,
        "side": side,
        "strength": strength,
        "layers_agreed": agreed,
        "reasons": reasons,
        "strike": strike,
        "premium": round(entry_prem, 2),
        "leg_oi": leg.get("oi", 0),
        "leg_chg_oi": leg.get("chg_oi", 0),
        "leg_volume": leg.get("volume", 0),
        "leg_iv": leg.get("iv", 0),
        "exits": exits,
    }


def build_all_entries() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for cfg in FNO_INDICES:
        try:
            results.append(analyze_index(cfg))
        except Exception as e:
            log.exception("Scalp entry failed for %s: %s", cfg["name"], e)
            results.append({"name": cfg["name"], "nse": cfg["nse"], "error": str(e)})
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return {
        "as_of_ist": now.strftime("%Y-%m-%d %H:%M IST"),
        "strategy": "EMA + RSI + OI Confluence  ·  VWAP Bounce",
        "indices": results,
    }


async def build_all_entries_async() -> dict[str, Any]:
    return await asyncio.to_thread(build_all_entries)


# ────────────────── Telegram formatting ──────────────────

def _ru(amount: float) -> str:
    return html.escape(f"₹{amount:.2f}")


def _format_one_index_html(r: dict[str, Any]) -> str:
    name = html.escape(r["name"])
    nse = html.escape(r["nse"])

    if r.get("error"):
        tech = r.get("tech") or {}
        spot_line = f"\nSpot: <code>{tech['spot']}</code>" if tech.get("spot") else ""
        return (
            f"<b>⚡ {name}</b> <code>{nse}</code>\n"
            f"⚠️ {html.escape(r['error'])}{spot_line}\n"
        )

    side = r["side"]
    side_emoji = "🟢" if side == "CE" else "🔴"
    tech = r["tech"]
    oi = r["oi"]
    ex = r["exits"]
    tf = tech.get("timeframe", "15m")
    vwap = tech.get("vwap")
    vwap_str = f"<code>{vwap}</code>" if vwap else "N/A"

    strength_emoji = {"STRONG": "🔥", "MODERATE": "⚡", "WEAK": "💤"}.get(r["strength"], "")
    layers_bar = "●" * r["layers_agreed"] + "○" * (4 - r["layers_agreed"])

    reasons_html = "\n".join(
        f"  ‣ {html.escape(x)}" for x in (r.get("reasons") or [])[:5]
    )

    return (
        f"{'━' * 28}\n"
        f"{side_emoji} <b>{name}</b>  <code>{nse}</code>\n"
        f"{strength_emoji} <b>{r['strength']}</b> signal  ·  Confluence {layers_bar} ({r['layers_agreed']}/4)\n"
        f"\n"
        f"Spot <code>{r['spot']}</code>  ·  VWAP {vwap_str}  ·  PCR <code>{oi['pcr']}</code>\n"
        f"RSI <code>{tech.get('rsi', '—')}</code>  ·  Mom <code>{tech.get('mom_pct', 0):+.2f}%</code>"
        f"  ·  Day <code>{tech.get('pct_change', 0):+.2f}%</code>\n"
        f"OI walls:  Support <code>{oi['support']}</code>  ·  Resistance <code>{oi['resistance']}</code>\n"
        f"\n"
        f"┌─────────────────────────┐\n"
        f"│  📌 <b>ENTRY</b>  <code>{r['strike']} {side}</code>  @  <b><u>{_ru(ex['entry'])}</u></b>\n"
        f"│  📊 OI <code>{r['leg_oi']:,}</code> · ΔOI <code>{r['leg_chg_oi']:+,}</code>"
        f" · Vol <code>{r['leg_volume']:,}</code>\n"
        f"│  📅 Expiry  <code>{html.escape(r['expiry'])}</code>\n"
        f"│\n"
        f"│  🎯 <b>T1  {_ru(ex['t1'])}</b>  <i>+{ex['t1_pts']:.2f} pts  (book 50%)</i>\n"
        f"│  🎯 <b>T2  {_ru(ex['t2'])}</b>  <i>+{ex['t2_pts']:.2f} pts  (trail rest)</i>\n"
        f"│  🔻 <b>SL  {_ru(ex['sl'])}</b>  <i>−{ex['sl_pts']:.2f} pts  (hard exit)</i>\n"
        f"│  📐 R:R  <b>{ex['rr']}x</b>\n"
        f"└─────────────────────────┘\n"
        f"\n"
        f"{reasons_html}\n"
    )


def format_entry_telegram(payload: dict[str, Any]) -> str:
    """Plain-text fallback (strips HTML tags)."""
    text = format_entry_telegram_html(payload)
    for tag in ("<b>", "</b>", "<u>", "</u>", "<i>", "</i>", "<code>", "</code>"):
        text = text.replace(tag, "")
    return text


def format_entry_telegram_html(payload: dict[str, Any]) -> str:
    parts = [
        "<b>⚡ INDEX OPTIONS — SCALP SHEET</b>",
        f"<b>Strategy:</b> {html.escape(payload.get('strategy', ''))}",
        f"<i>Updated {html.escape(payload['as_of_ist'])}</i>",
        "",
        "Book 50% at T1 · trail rest to T2 · hard SL · no averaging",
        "",
    ]
    for r in payload["indices"]:
        parts.append(_format_one_index_html(r))
    parts.append(
        "<i>⚠️ Scalping only · Not advice · Max hold 5–20 min\n"
        "Win rate improves with 3-4 layer confluence (STRONG signals)</i>"
    )
    return "\n".join(parts).strip()
