"""
F&O alert / summary persistence — MongoDB (survives Render redeploy) or SQLite fallback.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DB_FILE = os.getenv("DB_FILE", "posted_courses.db")
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
FNO_USE_MONGODB = os.getenv("FNO_USE_MONGODB", "auto").strip().lower()
FNO_ALERT_RETENTION_DAYS = int(os.getenv("FNO_ALERT_RETENTION_DAYS", "30"))

_mongo_client = None
_mongo_db = None


def use_mongodb() -> bool:
    if not MONGODB_URI:
        return False
    if FNO_USE_MONGODB in ("0", "false", "no", "off"):
        return False
    return True


def storage_backend_label() -> str:
    return "mongodb" if use_mongodb() else f"sqlite:{DB_FILE}"


def _ist_today() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")


def _retention_cutoff() -> str:
    days = max(1, FNO_ALERT_RETENTION_DAYS)
    d = datetime.now(ZoneInfo("Asia/Kolkata")).date() - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _get_mongo_db():
    global _mongo_client, _mongo_db
    if _mongo_db is not None:
        try:
            _mongo_client.admin.command("ping")
            return _mongo_db
        except Exception:
            _mongo_client = None
            _mongo_db = None

    from pymongo import MongoClient

    log.info("F&O storage: connecting to MongoDB...")
    try:
        import certifi

        _mongo_client = MongoClient(
            MONGODB_URI,
            tls=True,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            retryWrites=True,
            retryReads=True,
        )
        _mongo_client.admin.command("ping")
    except Exception as e1:
        log.warning("MongoDB certifi connect failed (%s), retrying...", e1)
        _mongo_client = MongoClient(
            MONGODB_URI,
            tls=True,
            tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            retryWrites=True,
            retryReads=True,
        )
        _mongo_client.admin.command("ping")

    _mongo_db = _mongo_client.udemy_enroller
    log.info("F&O storage: using MongoDB (udemy_enroller DB, fno_* collections)")
    return _mongo_db


def _next_alert_id(db) -> int:
    from pymongo import ReturnDocument

    doc = db.fno_counters.find_one_and_update(
        {"_id": "fno_alerts"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


def _doc_to_alert(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["id"],
        "nse_symbol": doc["nse_symbol"],
        "index_name": doc.get("index_name") or doc["nse_symbol"],
        "strategy": doc["strategy"],
        "side": doc["side"],
        "strike": doc.get("strike"),
        "entry_premium": doc.get("entry_premium"),
        "sl_premium": doc.get("sl_premium"),
        "s5_premium": doc.get("s5_premium"),
        "s10_premium": doc.get("s10_premium"),
        "t1_premium": doc.get("t1_premium"),
        "t2_premium": doc.get("t2_premium"),
        "spot_at_entry": doc.get("spot_at_entry"),
        "expiry": doc.get("expiry"),
        "alerted_at": doc.get("alerted_at"),
        "close_premium": doc.get("close_premium"),
        "outcome": doc.get("outcome"),
        "pnl_pts": doc.get("pnl_pts"),
        "exit_status": doc.get("exit_status", "OPEN"),
        "summarized": int(doc.get("summarized") or 0),
        "alert_date": doc.get("alert_date"),
        "entry_conditions": doc.get("entry_conditions") or {},
        "pick_type": doc.get("pick_type") or "safe",
    }


def ensure_fno_storage() -> None:
    if use_mongodb():
        db = _get_mongo_db()
        db.fno_alerts.create_index([("alert_date", 1)])
        db.fno_alerts.create_index(
            [("alert_date", 1), ("nse_symbol", 1), ("strategy", 1), ("side", 1), ("strike", 1)]
        )
        db.fno_alerts.create_index([("alert_date", 1), ("summarized", 1)])
        db.fno_scan_stats.create_index([("alert_date", 1)], unique=True)
        db.fno_eod_sent.create_index([("alert_date", 1)], unique=True)
        db.fno_alert_prefs.create_index([("chat_id", 1)])
        db.fno_alert_prefs.create_index([("chat_id", 1), ("nse_symbol", 1)], unique=True)
        db.fno_alert_tg.create_index([("alert_id", 1), ("chat_id", 1)], unique=True)
        db.fno_alert_tg.create_index([("alert_date", 1)])
        cutoff = _retention_cutoff()
        r = db.fno_alerts.delete_many({"summarized": 1, "alert_date": {"$lt": cutoff}})
        if r.deleted_count:
            log.info("F&O MongoDB: pruned %d old summarized alerts (before %s)", r.deleted_count, cutoff)
        r2 = db.fno_alert_tg.delete_many({"alert_date": {"$lt": cutoff}})
        if r2.deleted_count:
            log.info("F&O MongoDB: pruned %d old telegram message refs", r2.deleted_count)
        return

    con = sqlite3.connect(DB_FILE)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS fno_alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_date      TEXT NOT NULL,
                nse_symbol      TEXT NOT NULL,
                index_name      TEXT,
                strategy        TEXT NOT NULL,
                side            TEXT NOT NULL,
                strike          INTEGER,
                entry_premium   REAL,
                sl_premium      REAL,
                t1_premium      REAL,
                t2_premium      REAL,
                spot_at_entry   REAL,
                expiry          TEXT,
                alerted_at      TEXT,
                close_premium   REAL,
                outcome         TEXT,
                pnl_pts         REAL,
                summarized      INTEGER DEFAULT 0
            )
        """)
        for col, typ in (
            ("index_name", "TEXT"), ("entry_premium", "REAL"), ("sl_premium", "REAL"),
            ("t1_premium", "REAL"), ("t2_premium", "REAL"), ("spot_at_entry", "REAL"),
            ("expiry", "TEXT"), ("close_premium", "REAL"), ("outcome", "TEXT"),
            ("pnl_pts", "REAL"), ("summarized", "INTEGER DEFAULT 0"),
        ):
            try:
                con.execute(f"ALTER TABLE fno_alerts ADD COLUMN {col} {typ}")
            except Exception:
                pass
        con.execute("""
            CREATE TABLE IF NOT EXISTS fno_eod_sent (
                alert_date  TEXT PRIMARY KEY,
                sent_at     TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS fno_alert_prefs (
                chat_id     INTEGER NOT NULL,
                nse_symbol  TEXT NOT NULL,
                PRIMARY KEY (chat_id, nse_symbol)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS fno_alert_tg (
                alert_id    INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                message_id  INTEGER NOT NULL,
                alert_date  TEXT NOT NULL,
                PRIMARY KEY (alert_id, chat_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS fno_scan_stats (
                alert_date      TEXT PRIMARY KEY,
                setups          INTEGER DEFAULT 0,
                sent            INTEGER DEFAULT 0,
                skip_quality    INTEGER DEFAULT 0,
                skip_dedupe     INTEGER DEFAULT 0,
                skip_premium    INTEGER DEFAULT 0,
                scan_cycles     INTEGER DEFAULT 0
            )
        """)
        cutoff = _retention_cutoff()
        con.execute(
            "DELETE FROM fno_alerts WHERE summarized = 1 AND alert_date < ?",
            (cutoff,),
        )
        con.commit()
    finally:
        con.close()


def eod_summary_sent_today() -> bool:
    today = _ist_today()
    if use_mongodb():
        return _get_mongo_db().fno_eod_sent.find_one({"alert_date": today}) is not None
    con = sqlite3.connect(DB_FILE)
    try:
        return con.execute(
            "SELECT 1 FROM fno_eod_sent WHERE alert_date = ?", (today,)
        ).fetchone() is not None
    finally:
        con.close()


def mark_eod_summary_sent() -> None:
    today = _ist_today()
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%dT%H:%M:%S")
    if use_mongodb():
        _get_mongo_db().fno_eod_sent.update_one(
            {"alert_date": today},
            {"$set": {"alert_date": today, "sent_at": now_ist}},
            upsert=True,
        )
        return
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            "INSERT OR REPLACE INTO fno_eod_sent (alert_date, sent_at) VALUES (?, ?)",
            (today, now_ist),
        )
        con.commit()
    finally:
        con.close()


def set_user_alert_indices(chat_id: int, nse_symbols: list[str]) -> None:
    if use_mongodb():
        db = _get_mongo_db()
        db.fno_alert_prefs.delete_many({"chat_id": chat_id})
        if nse_symbols:
            db.fno_alert_prefs.insert_many(
                [{"chat_id": chat_id, "nse_symbol": s} for s in nse_symbols]
            )
        return
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute("DELETE FROM fno_alert_prefs WHERE chat_id = ?", (chat_id,))
        for sym in nse_symbols:
            con.execute(
                "INSERT INTO fno_alert_prefs (chat_id, nse_symbol) VALUES (?, ?)",
                (chat_id, sym),
            )
        con.commit()
    finally:
        con.close()


def clear_user_alert_indices(chat_id: int) -> None:
    if use_mongodb():
        _get_mongo_db().fno_alert_prefs.delete_many({"chat_id": chat_id})
        return
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute("DELETE FROM fno_alert_prefs WHERE chat_id = ?", (chat_id,))
        con.commit()
    finally:
        con.close()


def get_user_alert_indices(chat_id: int) -> set[str] | None:
    if use_mongodb():
        rows = _get_mongo_db().fno_alert_prefs.find({"chat_id": chat_id}, {"nse_symbol": 1})
        syms = {r["nse_symbol"] for r in rows}
        return None if not syms else syms
    con = sqlite3.connect(DB_FILE)
    try:
        rows = con.execute(
            "SELECT nse_symbol FROM fno_alert_prefs WHERE chat_id = ?",
            (chat_id,),
        ).fetchall()
        if not rows:
            return None
        return {r[0] for r in rows}
    finally:
        con.close()


def already_alerted(nse_symbol: str, strategy: str, side: str, strike: int) -> bool:
    """Block if this index+side was alerted today (any strategy, any outcome).

    High-quality mode: once an index+side combo fires, no more entries for
    the rest of the day.  Prevents re-entry spam after quick exits.
    """
    today = _ist_today()
    if use_mongodb():
        return _get_mongo_db().fno_alerts.find_one({
            "alert_date": today,
            "nse_symbol": nse_symbol,
            "side": side,
            "pick_type": "safe",
            "exit_status": {"$nin": ["LEGACY"]},
        }) is not None
    con = sqlite3.connect(DB_FILE)
    try:
        return con.execute(
            """SELECT 1 FROM fno_alerts
               WHERE alert_date=? AND nse_symbol=? AND side=?
               AND outcome IS NOT NULL AND outcome != 'NO DATA'""",
            (today, nse_symbol, side),
        ).fetchone() is not None
    finally:
        con.close()


def record_alert(
    signal: dict[str, Any],
    *,
    pick_type: str = "safe",
    agg_pick: dict[str, Any] | None = None,
) -> int:
    """Persist one trade alert (safe or aggressive near-ATM pick). Returns alert id."""
    today = _ist_today()
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%dT%H:%M:%S")
    scalp_t5 = float(os.getenv("FNO_SCALP_T5_PCT", "5"))
    scalp_t10 = float(os.getenv("FNO_SCALP_T10_PCT", "10"))

    if pick_type == "aggressive" and agg_pick:
        strike = int(agg_pick["strike"])
        premium = float(agg_pick.get("premium") or 0)
        ex = agg_pick.get("exits") or {}
    else:
        strike = int(signal["strike"])
        premium = float(signal.get("premium") or 0)
        ex = signal.get("exits") or {}

    s5 = round(float(ex.get("s5") or premium * (1 + scalp_t5 / 100)), 2)
    s10 = round(float(ex.get("s10") or premium * (1 + scalp_t10 / 100)), 2)

    if use_mongodb():
        db = _get_mongo_db()
        aid = _next_alert_id(db)
        tech = signal.get("tech") or {}
        oi_data = signal.get("oi") or {}
        db.fno_alerts.insert_one({
            "id": aid,
            "alert_date": today,
            "nse_symbol": signal["nse"],
            "index_name": signal.get("name"),
            "strategy": signal["strategy"],
            "side": signal["side"],
            "strike": strike,
            "entry_premium": round(premium, 2),
            "sl_premium": ex.get("sl"),
            "s5_premium": s5,
            "s10_premium": s10,
            "t1_premium": ex.get("t1"),
            "t2_premium": ex.get("t2"),
            "spot_at_entry": signal.get("spot"),
            "expiry": signal.get("expiry"),
            "alerted_at": now_ist,
            "close_premium": None,
            "outcome": None,
            "pnl_pts": None,
            "exit_status": "OPEN",
            "pick_type": pick_type,
            "summarized": 0,
            "entry_conditions": {
                "rsi": tech.get("rsi"),
                "vwap": tech.get("vwap"),
                "ema9": tech.get("ema9"),
                "ema21": tech.get("ema21"),
                "adx": tech.get("adx"),
                "pcr": oi_data.get("pcr"),
                "bb_upper": tech.get("bb_upper"),
                "bb_middle": tech.get("bb_middle"),
                "bb_lower": tech.get("bb_lower"),
            },
        })
        return aid
    con = sqlite3.connect(DB_FILE)
    try:
        cur = con.execute(
            """INSERT INTO fno_alerts (
                alert_date, nse_symbol, index_name, strategy, side, strike,
                entry_premium, sl_premium, t1_premium, t2_premium,
                spot_at_entry, expiry, alerted_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                today, signal["nse"], signal.get("name"), signal["strategy"],
                signal["side"], strike, round(premium, 2),
                ex.get("sl"), ex.get("t1"), ex.get("t2"),
                signal.get("spot"), signal.get("expiry"), now_ist,
            ),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def record_alerts_for_signal(signal: dict[str, Any]) -> list[dict[str, Any]]:
    """Record safe + optional aggressive picks. Returns refs for trace IDs in Telegram."""
    refs: list[dict[str, Any]] = []
    safe_id = record_alert(signal, pick_type="safe")
    refs.append({
        "id": safe_id,
        "pick_type": "safe",
        "strike": signal["strike"],
        "side": signal["side"],
        "premium": signal.get("premium"),
    })

    agg = signal.get("aggressive")
    if not agg:
        return refs

    strike = int(agg.get("strike") or 0)
    premium = float(agg.get("premium") or 0)
    if strike <= 0 or premium <= 0:
        return refs

    if already_alerted(signal["nse"], signal["strategy"], signal["side"], strike):
        log.info(
            "Aggressive pick already open: %s %s %s %s",
            signal["nse"], signal["strategy"], signal["side"], strike,
        )
        return refs

    agg_id = record_alert(signal, pick_type="aggressive", agg_pick=agg)
    refs.append({
        "id": agg_id,
        "pick_type": "aggressive",
        "strike": strike,
        "side": signal["side"],
        "premium": round(premium, 2),
    })
    return refs


def record_scan_stats(delta: dict[str, int]) -> None:
    today = _ist_today()
    if use_mongodb():
        _get_mongo_db().fno_scan_stats.update_one(
            {"alert_date": today},
            {
                "$inc": {
                    "setups": delta.get("setups", 0),
                    "sent": delta.get("sent", 0),
                    "skip_quality": delta.get("skip_quality", 0),
                    "skip_dedupe": delta.get("skip_dedupe", 0),
                    "skip_premium": delta.get("skip_premium", 0),
                    "scan_cycles": 1,
                },
                "$setOnInsert": {"alert_date": today},
            },
            upsert=True,
        )
        return
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            """
            INSERT INTO fno_scan_stats (
                alert_date, setups, sent, skip_quality, skip_dedupe, skip_premium, scan_cycles
            ) VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(alert_date) DO UPDATE SET
                setups = setups + excluded.setups,
                sent = sent + excluded.sent,
                skip_quality = skip_quality + excluded.skip_quality,
                skip_dedupe = skip_dedupe + excluded.skip_dedupe,
                skip_premium = skip_premium + excluded.skip_premium,
                scan_cycles = scan_cycles + 1
            """,
            (
                today,
                delta.get("setups", 0),
                delta.get("sent", 0),
                delta.get("skip_quality", 0),
                delta.get("skip_dedupe", 0),
                delta.get("skip_premium", 0),
            ),
        )
        con.commit()
    finally:
        con.close()


def get_scan_stats_for_date(alert_date: str) -> dict[str, int] | None:
    if use_mongodb():
        doc = _get_mongo_db().fno_scan_stats.find_one({"alert_date": alert_date})
        if not doc:
            return None
        return {
            "setups": int(doc.get("setups") or 0),
            "sent": int(doc.get("sent") or 0),
            "skip_quality": int(doc.get("skip_quality") or 0),
            "skip_dedupe": int(doc.get("skip_dedupe") or 0),
            "skip_premium": int(doc.get("skip_premium") or 0),
            "scan_cycles": int(doc.get("scan_cycles") or 0),
        }
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            """SELECT setups, sent, skip_quality, skip_dedupe, skip_premium, scan_cycles
               FROM fno_scan_stats WHERE alert_date = ?""",
            (alert_date,),
        ).fetchone()
        if not row:
            return None
        return {
            "setups": row[0], "sent": row[1], "skip_quality": row[2],
            "skip_dedupe": row[3], "skip_premium": row[4], "scan_cycles": row[5],
        }
    finally:
        con.close()


def get_scan_stats_range(start_date: str, end_date: str) -> dict[str, int]:
    if use_mongodb():
        pipeline = [
            {"$match": {"alert_date": {"$gte": start_date, "$lte": end_date}}},
            {"$group": {
                "_id": None,
                "setups": {"$sum": "$setups"},
                "sent": {"$sum": "$sent"},
                "skip_quality": {"$sum": "$skip_quality"},
                "skip_dedupe": {"$sum": "$skip_dedupe"},
                "skip_premium": {"$sum": "$skip_premium"},
                "scan_cycles": {"$sum": "$scan_cycles"},
            }},
        ]
        rows = list(_get_mongo_db().fno_scan_stats.aggregate(pipeline))
        if not rows:
            return {"setups": 0, "sent": 0, "skip_quality": 0, "skip_dedupe": 0,
                    "skip_premium": 0, "scan_cycles": 0}
        r = rows[0]
        return {
            "setups": int(r.get("setups") or 0),
            "sent": int(r.get("sent") or 0),
            "skip_quality": int(r.get("skip_quality") or 0),
            "skip_dedupe": int(r.get("skip_dedupe") or 0),
            "skip_premium": int(r.get("skip_premium") or 0),
            "scan_cycles": int(r.get("scan_cycles") or 0),
        }
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            """
            SELECT COALESCE(SUM(setups),0), COALESCE(SUM(sent),0),
                   COALESCE(SUM(skip_quality),0), COALESCE(SUM(skip_dedupe),0),
                   COALESCE(SUM(skip_premium),0), COALESCE(SUM(scan_cycles),0)
            FROM fno_scan_stats
            WHERE alert_date >= ? AND alert_date <= ?
            """,
            (start_date, end_date),
        ).fetchone()
        return {
            "setups": row[0], "sent": row[1], "skip_quality": row[2],
            "skip_dedupe": row[3], "skip_premium": row[4], "scan_cycles": row[5],
        }
    finally:
        con.close()


def get_alerts_between(start_date: str, end_date: str) -> list[dict[str, Any]]:
    if use_mongodb():
        cursor = _get_mongo_db().fno_alerts.find(
            {"alert_date": {"$gte": start_date, "$lte": end_date}},
        ).sort([("alert_date", 1), ("alerted_at", 1)])
        return [_doc_to_alert(d) for d in cursor]
    con = sqlite3.connect(DB_FILE)
    try:
        rows = con.execute(
            """SELECT id, nse_symbol, index_name, strategy, side, strike,
                      entry_premium, sl_premium, t1_premium, t2_premium,
                      spot_at_entry, expiry, alerted_at,
                      close_premium, outcome, pnl_pts, summarized, alert_date
               FROM fno_alerts
               WHERE alert_date >= ? AND alert_date <= ?
               ORDER BY alert_date, alerted_at""",
            (start_date, end_date),
        ).fetchall()
        return [
            {
                "id": r[0], "nse_symbol": r[1], "index_name": r[2] or r[1],
                "strategy": r[3], "side": r[4], "strike": r[5],
                "entry_premium": r[6], "sl_premium": r[7],
                "t1_premium": r[8], "t2_premium": r[9],
                "spot_at_entry": r[10], "expiry": r[11], "alerted_at": r[12],
                "close_premium": r[13], "outcome": r[14], "pnl_pts": r[15],
                "summarized": r[16], "alert_date": r[17],
            }
            for r in rows
        ]
    finally:
        con.close()


def get_today_alerts(unsummarized_only: bool = True) -> list[dict[str, Any]]:
    today = _ist_today()
    if use_mongodb():
        q: dict[str, Any] = {"alert_date": today}
        if unsummarized_only:
            q["summarized"] = 0
        cursor = _get_mongo_db().fno_alerts.find(q).sort("alerted_at", 1)
        return [_doc_to_alert(d) for d in cursor]
    con = sqlite3.connect(DB_FILE)
    try:
        q = """SELECT id, nse_symbol, index_name, strategy, side, strike,
                      entry_premium, sl_premium, t1_premium, t2_premium,
                      spot_at_entry, expiry, alerted_at,
                      close_premium, outcome, pnl_pts, summarized
               FROM fno_alerts WHERE alert_date = ?"""
        params: tuple[Any, ...] = (today,)
        if unsummarized_only:
            q += " AND summarized = 0"
        q += " ORDER BY alerted_at ASC"
        rows = con.execute(q, params).fetchall()
        return [
            {
                "id": r[0], "nse_symbol": r[1], "index_name": r[2] or r[1],
                "strategy": r[3], "side": r[4], "strike": r[5],
                "entry_premium": r[6], "sl_premium": r[7],
                "t1_premium": r[8], "t2_premium": r[9],
                "spot_at_entry": r[10], "expiry": r[11], "alerted_at": r[12],
                "close_premium": r[13], "outcome": r[14], "pnl_pts": r[15],
                "summarized": r[16], "alert_date": today,
            }
            for r in rows
        ]
    finally:
        con.close()


def update_alert_result(alert_id: int, close_ltp: float, outcome: str, pnl: float) -> None:
    if use_mongodb():
        _get_mongo_db().fno_alerts.update_one(
            {"id": alert_id},
            {"$set": {
                "close_premium": close_ltp,
                "outcome": outcome,
                "pnl_pts": pnl,
                "summarized": 1,
            }},
        )
        return
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            """UPDATE fno_alerts SET close_premium=?, outcome=?, pnl_pts=?, summarized=1
               WHERE id=?""",
            (close_ltp, outcome, pnl, alert_id),
        )
        con.commit()
    finally:
        con.close()


def get_active_alerts() -> list[dict[str, Any]]:
    """Get today's alerts that are still OPEN (no exit triggered yet)."""
    today = _ist_today()
    if use_mongodb():
        cursor = _get_mongo_db().fno_alerts.find(
            {"alert_date": today, "exit_status": "OPEN"}
        ).sort("alerted_at", 1)
        return [_doc_to_alert(d) for d in cursor]
    con = sqlite3.connect(DB_FILE)
    try:
        rows = con.execute(
            """SELECT id, nse_symbol, index_name, strategy, side, strike,
                      entry_premium, sl_premium, t1_premium, t2_premium,
                      spot_at_entry, expiry, alerted_at,
                      close_premium, outcome, pnl_pts, summarized
               FROM fno_alerts WHERE alert_date = ? AND (outcome IS NULL OR outcome = '')
               ORDER BY alerted_at ASC""",
            (today,),
        ).fetchall()
        return [
            {
                "id": r[0], "nse_symbol": r[1], "index_name": r[2] or r[1],
                "strategy": r[3], "side": r[4], "strike": r[5],
                "entry_premium": r[6], "sl_premium": r[7],
                "t1_premium": r[8], "t2_premium": r[9],
                "spot_at_entry": r[10], "expiry": r[11], "alerted_at": r[12],
                "close_premium": r[13], "outcome": r[14], "pnl_pts": r[15],
                "summarized": r[16], "alert_date": today,
            }
            for r in rows
        ]
    finally:
        con.close()


def update_exit_status(alert_id: int, exit_status: str, live_premium: float) -> None:
    """Mark an alert with its exit status (S5, S10, T1, T2, SL)."""
    if use_mongodb():
        _get_mongo_db().fno_alerts.update_one(
            {"id": alert_id},
            {"$set": {"exit_status": exit_status, "close_premium": live_premium}},
        )
        return
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            "UPDATE fno_alerts SET close_premium=?, outcome=? WHERE id=?",
            (live_premium, exit_status, alert_id),
        )
        con.commit()
    finally:
        con.close()


def save_alert_telegram_msg(alert_id: int, chat_id: int, message_id: int) -> None:
    """Store entry alert Telegram message id for reply threading on exit."""
    today = _ist_today()
    if use_mongodb():
        _get_mongo_db().fno_alert_tg.update_one(
            {"alert_id": alert_id, "chat_id": chat_id},
            {"$set": {
                "alert_id": alert_id,
                "chat_id": chat_id,
                "message_id": message_id,
                "alert_date": today,
            }},
            upsert=True,
        )
        return
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            """INSERT INTO fno_alert_tg (alert_id, chat_id, message_id, alert_date)
               VALUES (?,?,?,?)
               ON CONFLICT(alert_id, chat_id) DO UPDATE SET message_id=excluded.message_id""",
            (alert_id, chat_id, message_id, today),
        )
        con.commit()
    finally:
        con.close()


def get_alert_telegram_msg(alert_id: int, chat_id: int) -> int | None:
    if use_mongodb():
        doc = _get_mongo_db().fno_alert_tg.find_one({"alert_id": alert_id, "chat_id": chat_id})
        return int(doc["message_id"]) if doc else None
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            "SELECT message_id FROM fno_alert_tg WHERE alert_id=? AND chat_id=?",
            (alert_id, chat_id),
        ).fetchone()
        return int(row[0]) if row else None
    finally:
        con.close()


def get_alert_by_id(alert_id: int) -> dict[str, Any] | None:
    if use_mongodb():
        doc = _get_mongo_db().fno_alerts.find_one({"id": alert_id})
        return _doc_to_alert(doc) if doc else None
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            """SELECT id, nse_symbol, index_name, strategy, side, strike,
                      entry_premium, sl_premium, t1_premium, t2_premium,
                      spot_at_entry, expiry, alerted_at,
                      close_premium, outcome, pnl_pts, summarized, alert_date
               FROM fno_alerts WHERE id=?""",
            (alert_id,),
        ).fetchone()
        if not row:
            return None
        return _doc_to_alert({
            "id": row[0], "nse_symbol": row[1], "index_name": row[2],
            "strategy": row[3], "side": row[4], "strike": row[5],
            "entry_premium": row[6], "sl_premium": row[7],
            "t1_premium": row[8], "t2_premium": row[9],
            "spot_at_entry": row[10], "expiry": row[11], "alerted_at": row[12],
            "close_premium": row[13], "outcome": row[14], "pnl_pts": row[15],
            "summarized": row[16], "alert_date": row[17],
            "exit_status": "OPEN",
            "pick_type": "safe",
            "entry_conditions": {},
        })
    finally:
        con.close()
