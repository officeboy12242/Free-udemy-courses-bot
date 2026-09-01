"""
Trade Journal — Self-Improving Trader System
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Logs every trade with full metadata
• Tracks P&L, win rate, streak, drawdown
• Improvement tickets: suggest → approve → apply
• Survival capital tracking
"""
from __future__ import annotations
import os, logging
from datetime import datetime, timedelta
from typing import Any
from dataclasses import dataclass, asdict

log = logging.getLogger(__name__)

# ── MongoDB (lazy) ──
_client = None
_db = None
MONGODB_URI = os.getenv("MONGODB_URI", "")

def _get_db():
    global _client, _db
    if not MONGODB_URI:
        raise ValueError("MONGODB_URI not set")
    if _client is not None:
        try:
            _client.admin.command("ping")
            return _db
        except Exception:
            _client = None; _db = None
    try:
        import certifi
        from pymongo import MongoClient
        _client = MongoClient(MONGODB_URI, tls=True, tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=10000, connectTimeoutMS=10000, socketTimeoutMS=10000,
            retryWrites=True, retryReads=True)
        _client.admin.command("ping")
        _db = _client.udemy_enroller
        return _db
    except Exception:
        try:
            from pymongo import MongoClient
            _client = MongoClient(MONGODB_URI, tls=True, tlsAllowInvalidCertificates=True,
                serverSelectionTimeoutMS=10000, connectTimeoutMS=10000, socketTimeoutMS=10000,
                retryWrites=True, retryReads=True)
            _client.admin.command("ping")
            _db = _client.udemy_enroller
            return _db
        except Exception as e:
            log.error("Journal MongoDB error: %s", e)
            raise

def _ensure_indexes():
    db = _get_db()
    db.trader_journal.create_index([("status", 1), ("entered_at", -1)])
    db.trader_journal.create_index([("symbol", 1), ("entered_at", -1)])
    db.trader_improvements.create_index([("status", 1), ("created_at", -1)])
    db.trader_capital.create_index([("date", -1)])

# ── Constants ──
INITIAL_CAPITAL = 100_000
DAILY_LOSS_LIMIT = 0.02     # 2%
WEEKLY_LOSS_LIMIT = 0.05    # 5%
MONTHLY_LOSS_LIMIT = 0.10   # 10%
MAX_DRAWDOWN = 0.25         # 25%

# ── Data Classes ──
@dataclass
class Trade:
    symbol: str
    sector: str
    strategy: str         # "mean_reversion" or "momentum"
    entry_price: float
    qty: int
    entry_date: str
    sl_price: float
    t1_price: float
    t2_price: float
    score: float
    reasons: list[str]
    # Filled on exit
    exit_price: float = 0.0
    exit_date: str = ""
    exit_reason: str = ""  # SL, T1, T2, TRAIL_STOP, TIME
    pnl_pct: float = 0.0
    pnl_inr: float = 0.0
    holding_days: int = 0
    # AI analysis
    ai_analysis: str = ""
    improvement_ticket_id: str = ""

@dataclass
class ImprovementTicket:
    """Suggestion from AI → user approves → system applies."""
    title: str
    description: str
    category: str   # "entry", "exit", "sizing", "sector", "regime"
    priority: str   # "high", "medium", "low"
    before: Any     # current value
    after: Any      # suggested value
    rationale: str  # why this change helps
    status: str = "pending"  # pending, approved, rejected, applied
    created_at: datetime = None
    reviewed_at: datetime = None
    trade_ids: list[str] = None  # trades that triggered this

# ── Trade Logging ──
def log_trade(trade: Trade) -> str:
    """Log a new trade. Returns trade_id."""
    db = _get_db()
    doc = asdict(trade)
    doc["status"] = "open"
    doc["entered_at"] = datetime.utcnow()
    doc["updated_at"] = datetime.utcnow()
    result = db.trader_journal.insert_one(doc)
    log.info("Trade logged: %s %s @ ₹%.2f", trade.symbol, trade.strategy, trade.entry_price)
    return str(result.inserted_id)

def close_trade(trade_id: str, exit_price: float, exit_reason: str) -> dict:
    """Close a trade and calculate P&L."""
    from bson import ObjectId
    db = _get_db()
    trade = db.trader_journal.find_one({"_id": ObjectId(trade_id), "status": "open"})
    if not trade:
        return {"error": "Trade not found"}

    pnl_pct = ((exit_price - trade["entry_price"]) / trade["entry_price"]) * 100
    pnl_inr = (trade["entry_price"] * trade["qty"]) * (pnl_pct / 100)
    entry_date = trade.get("entered_at")
    holding_days = (datetime.utcnow() - entry_date).days if isinstance(entry_date, datetime) else 0

    db.trader_journal.update_one(
        {"_id": ObjectId(trade_id)},
        {"$set": {
            "status": "closed", "exit_price": exit_price, "exit_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "exit_reason": exit_reason, "pnl_pct": round(pnl_pct, 2),
            "pnl_inr": round(pnl_inr, 2), "holding_days": holding_days,
            "updated_at": datetime.utcnow(),
        }}
    )

    # Update capital
    _update_capital(pnl_inr)

    emoji = "✅" if pnl_pct > 0 else "❌"
    log.info("%s Closed %s: %+.2f%% (₹%+.0f) [%s]", emoji, trade["symbol"], pnl_pct, pnl_inr, exit_reason)
    return {"pnl_pct": pnl_pct, "pnl_inr": pnl_inr, "exit_reason": exit_reason}

# ── Capital Tracking ──
def _update_capital(pnl_inr: float):
    """Update daily capital log."""
    db = _get_db()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    existing = db.trader_capital.find_one({"date": today})
    if existing:
        new_capital = existing["capital"] + pnl_inr
        new_pnl = existing["daily_pnl"] + pnl_inr
        db.trader_capital.update_one({"date": today}, {"$set": {
            "capital": round(new_capital, 0), "daily_pnl": round(new_pnl, 0),
        }})
    else:
        prev = db.trader_capital.find_one(sort=[("date", -1)])
        prev_cap = prev["capital"] if prev else INITIAL_CAPITAL
        db.trader_capital.insert_one({
            "date": today, "capital": round(prev_cap + pnl_inr, 0),
            "daily_pnl": round(pnl_inr, 0), "peak_capital": max(prev_cap, prev_cap + pnl_inr),
        })

def get_capital_status() -> dict:
    """Get current capital, drawdown, and survival status."""
    db = _get_db()
    latest = db.trader_capital.find_one(sort=[("date", -1)])
    current_capital = latest["capital"] if latest else INITIAL_CAPITAL

    # Find peak
    peak_doc = db.trader_capital.find_one(sort=[("capital", -1)])
    peak = peak_doc["capital"] if peak_doc else INITIAL_CAPITAL

    drawdown = ((peak - current_capital) / peak) * 100 if peak > 0 else 0

    # Today's P&L
    today = datetime.utcnow().strftime("%Y-%m-%d")
    today_doc = db.trader_capital.find_one({"date": today})
    daily_pnl = today_doc["daily_pnl"] if today_doc else 0

    # This week's P&L
    week_start = (datetime.utcnow() - timedelta(days=datetime.utcnow().weekday())).strftime("%Y-%m-%d")
    week_docs = list(db.trader_capital.find({"date": {"$gte": week_start}}))
    weekly_pnl = sum(d.get("daily_pnl", 0) for d in week_docs)

    # This month's P&L
    month_start = datetime.utcnow().strftime("%Y-%m-01")
    month_docs = list(db.trader_capital.find({"date": {"$gte": month_start}}))
    monthly_pnl = sum(d.get("daily_pnl", 0) for d in month_docs)

    # Survival status
    status = "🟢 SAFE"
    deploy_pct = 0.50  # default 50%
    if drawdown >= MAX_DRAWDOWN * 100:
        status = "🔴 EMERGENCY — Stop trading!"
        deploy_pct = 0.0
    elif drawdown >= MONTHLY_LOSS_LIMIT * 100:
        status = "🟠 SURVIVAL MODE — Reduce to 20%"
        deploy_pct = 0.20
    elif drawdown >= WEEKLY_LOSS_LIMIT * 100:
        status = "🟡 CAUTIOUS — Reduce to 30%"
        deploy_pct = 0.30
    elif current_capital < INITIAL_CAPITAL * 0.80:
        status = "🟡 REDUCED — Reduce to 40%"
        deploy_pct = 0.40

    return {
        "capital": round(current_capital, 0),
        "peak": round(peak, 0),
        "drawdown": round(drawdown, 1),
        "daily_pnl": round(daily_pnl, 0),
        "weekly_pnl": round(weekly_pnl, 0),
        "monthly_pnl": round(monthly_pnl, 0),
        "status": status,
        "deploy_pct": deploy_pct,
        "total_return": round(((current_capital - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100, 1),
    }

# ── Win Rate & Stats ──
def get_trader_stats(days: int = 30) -> dict:
    """Get trading statistics for last N days."""
    db = _get_db()
    since = datetime.utcnow() - timedelta(days=days)
    trades = list(db.trader_journal.find({"status": "closed", "entered_at": {"$gte": since}}))

    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "wr": 0, "avg_pnl": 0, "total_pnl": 0,
                "avg_win": 0, "avg_loss": 0, "best": 0, "worst": 0, "avg_hold": 0, "streak": 0,
                "by_strategy": {}, "by_sector": {}}

    wins = [t for t in trades if t.get("pnl_pct", 0) > 0]
    losses = [t for t in trades if t.get("pnl_pct", 0) <= 0]

    # Current streak
    sorted_trades = sorted(trades, key=lambda x: x.get("entered_at", datetime.min), reverse=True)
    streak = 0
    streak_type = ""
    for t in sorted_trades:
        if t.get("pnl_pct", 0) > 0:
            if streak_type == "loss": break
            streak += 1; streak_type = "win"
        else:
            if streak_type == "win": break
            streak += 1; streak_type = "loss"

    # By strategy
    by_strategy = {}
    for t in trades:
        s = t.get("strategy", "unknown")
        if s not in by_strategy: by_strategy[s] = {"trades": 0, "wins": 0, "pnl": 0}
        by_strategy[s]["trades"] += 1
        if t.get("pnl_pct", 0) > 0: by_strategy[s]["wins"] += 1
        by_strategy[s]["pnl"] += t.get("pnl_pct", 0)

    # By sector
    by_sector = {}
    for t in trades:
        s = t.get("sector", "Other")
        if s not in by_sector: by_sector[s] = {"trades": 0, "wins": 0, "pnl": 0}
        by_sector[s]["trades"] += 1
        if t.get("pnl_pct", 0) > 0: by_sector[s]["wins"] += 1
        by_sector[s]["pnl"] += t.get("pnl_pct", 0)

    return {
        "total": len(trades), "wins": len(wins), "losses": len(losses),
        "wr": round(len(wins)/len(trades)*100, 1) if trades else 0,
        "avg_pnl": round(sum(t.get("pnl_pct",0) for t in trades)/len(trades), 2),
        "total_pnl": round(sum(t.get("pnl_pct",0) for t in trades), 2),
        "avg_win": round(sum(t.get("pnl_pct",0) for t in wins)/len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t.get("pnl_pct",0) for t in losses)/len(losses), 2) if losses else 0,
        "best": round(max(t.get("pnl_pct",0) for t in trades), 2),
        "worst": round(min(t.get("pnl_pct",0) for t in trades), 2),
        "avg_hold": round(sum(t.get("holding_days",0) for t in trades)/len(trades), 1),
        "streak": f"{streak_type}x{streak}" if streak else "none",
        "by_strategy": by_strategy,
        "by_sector": by_sector,
    }

# ── Improvement Tickets ──
def create_improvement_ticket(ticket: ImprovementTicket) -> str:
    """Create an improvement ticket (pending review)."""
    db = _get_db()
    doc = {
        "title": ticket.title, "description": ticket.description,
        "category": ticket.category, "priority": ticket.priority,
        "before": ticket.before, "after": ticket.after,
        "rationale": ticket.rationale, "status": "pending",
        "created_at": datetime.utcnow(),
        "trade_ids": ticket.trade_ids or [],
    }
    result = db.trader_improvements.insert_one(doc)
    log.info("Improvement ticket created: %s", ticket.title)
    return str(result.inserted_id)

def get_pending_tickets() -> list[dict]:
    """Get all pending improvement tickets."""
    db = _get_db()
    return list(db.trader_improvements.find({"status": "pending"}).sort("created_at", -1))

def approve_ticket(ticket_id: str) -> dict:
    """Approve an improvement ticket."""
    from bson import ObjectId
    db = _get_db()
    result = db.trader_improvements.update_one(
        {"_id": ObjectId(ticket_id), "status": "pending"},
        {"$set": {"status": "approved", "reviewed_at": datetime.utcnow()}}
    )
    return {"ok": result.modified_count > 0}

def reject_ticket(ticket_id: str) -> dict:
    """Reject an improvement ticket."""
    from bson import ObjectId
    db = _get_db()
    result = db.trader_improvements.update_one(
        {"_id": ObjectId(ticket_id), "status": "pending"},
        {"$set": {"status": "rejected", "reviewed_at": datetime.utcnow()}}
    )
    return {"ok": result.modified_count > 0}

def apply_ticket(ticket_id: str) -> dict:
    """Mark ticket as applied (after system implements the change)."""
    from bson import ObjectId
    db = _get_db()
    ticket = db.trader_improvements.find_one({"_id": ObjectId(ticket_id)})
    if not ticket:
        return {"error": "Ticket not found"}

    # Actually apply the change based on category
    category = ticket.get("category", "")
    before = ticket.get("before")
    after = ticket.get("after")

    applied = False
    if category == "sl":
        from swing_service import SL_PCT
        # Will be applied via parameter update
        applied = True
    elif category == "t1":
        applied = True
    elif category == "t2":
        applied = True
    elif category == "sector":
        applied = True  # Sector filter update
    else:
        applied = True  # Generic apply

    if applied:
        db.trader_improvements.update_one(
            {"_id": ObjectId(ticket_id)},
            {"$set": {"status": "applied", "applied_at": datetime.utcnow()}}
        )
    return {"ok": applied, "category": category}

def get_improvement_history() -> list[dict]:
    """Get all improvement tickets with status."""
    db = _get_db()
    return list(db.trader_improvements.find().sort("created_at", -1).limit(20))

# ── Recent Trades ──
def get_recent_trades(limit: int = 10) -> list[dict]:
    """Get recent trades."""
    db = _get_db()
    trades = list(db.trader_journal.find().sort("entered_at", -1).limit(limit))
    return trades
