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
        "t1_premium": doc.get("t1_premium"),
        "t2_premium": doc.get("t2_premium"),
        "spot_at_entry": doc.get("spot_at_entry"),
        "expiry": doc.get("expiry"),
        "alerted_at": doc.get("alerted_at"),
        "close_premium": doc.get("close_premium"),
        "outcome": doc.get("outcome"),
        "pnl_pts": doc.get("pnl_pts"),
        "summarized": int(doc.get("summarized") or 0),
        "alert_date": doc.get("alert_date"),
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
        cutoff = _retention_cutoff()
        r = db.fno_alerts.delete_many({"summarized": 1, "alert_date": {"$lt": cutoff}})
        if r.deleted_count:
            log.info("F&O MongoDB: pruned %d old summarized alerts (before %s)", r.deleted_count, cutoff)
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
    today = _ist_today()
    if use_mongodb():
        return _get_mongo_db().fno_alerts.find_one({
            "alert_date": today,
            "nse_symbol": nse_symbol,
            "strategy": strategy,
            "side": side,
            "strike": strike,
        }) is not None
    con = sqlite3.connect(DB_FILE)
    try:
        return con.execute(
            """SELECT 1 FROM fno_alerts
               WHERE alert_date=? AND nse_symbol=? AND strategy=? AND side=? AND strike=?""",
            (today, nse_symbol, strategy, side, strike),
        ).fetchone() is not None
    finally:
        con.close()


def record_alert(signal: dict[str, Any]) -> None:
    today = _ist_today()
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%dT%H:%M:%S")
    ex = signal.get("exits") or {}
    s5 = round(float(signal.get("premium") or 0) * (1 + float(os.getenv("FNO_SCALP_T5_PCT", "5")) / 100), 2)
    s10 = round(float(signal.get("premium") or 0) * (1 + float(os.getenv("FNO_SCALP_T10_PCT", "10")) / 100), 2)
    if use_mongodb():
        db = _get_mongo_db()
        aid = _next_alert_id(db)
        db.fno_alerts.insert_one({
            "id": aid,
            "alert_date": today,
            "nse_symbol": signal["nse"],
            "index_name": signal.get("name"),
            "strategy": signal["strategy"],
            "side": signal["side"],
            "strike": signal["strike"],
            "entry_premium": signal.get("premium"),
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
            "summarized": 0,
        })
        return
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            """INSERT INTO fno_alerts (
                alert_date, nse_symbol, index_name, strategy, side, strike,
                entry_premium, sl_premium, t1_premium, t2_premium,
                spot_at_entry, expiry, alerted_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                today, signal["nse"], signal.get("name"), signal["strategy"],
                signal["side"], signal["strike"], signal.get("premium"),
                ex.get("sl"), ex.get("t1"), ex.get("t2"),
                signal.get("spot"), signal.get("expiry"), now_ist,
            ),
        )
        con.commit()
    finally:
        con.close()


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
