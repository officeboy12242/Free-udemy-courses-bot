"""
Multi-Account Udemy Enroller - Database and credential management
Supports multiple Udemy accounts per user with auto-enrollment tracking
"""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger(__name__)

ENROLL_DB_FILE = "user_enroller.db"


def _conn():
    return sqlite3.connect(ENROLL_DB_FILE)


import os

# Daily enrollment limit for free users
FREE_DAILY_LIMIT = 20
# Owner ID from environment (gets full access)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))


def init_enroller_db():
    """Initialize enroller database with multi-account support"""
    conn = _conn()
    c = conn.cursor()
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_name TEXT NOT NULL,
            access_token TEXT NOT NULL,
            client_id TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            auto_enroll INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_setup_state (
            user_id INTEGER PRIMARY KEY,
            setup_step TEXT,
            extra_data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS enrolled_courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            course_url TEXT NOT NULL,
            course_title TEXT,
            enrolled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS auto_enroll_state (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER DEFAULT 0,
            last_check TIMESTAMP,
            last_course_id TEXT,
            total_auto_enrolled INTEGER DEFAULT 0
        )
    """)
    
    # Premium users table
    c.execute("""
        CREATE TABLE IF NOT EXISTS premium_users (
            user_id INTEGER PRIMARY KEY,
            granted_by INTEGER,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Daily usage tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            enroll_count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date)
        )
    """)
    
    # Migrate from old schema if needed
    try:
        c.execute("SELECT user_id, access_token, client_id FROM user_credentials")
        rows = c.fetchall()
        for user_id, token, client in rows:
            if token and client:
                c.execute("""
                    INSERT OR IGNORE INTO user_accounts (user_id, account_name, access_token, client_id, is_active, auto_enroll)
                    VALUES (?, ?, ?, ?, 1, 0)
                """, (user_id, "Account 1", token, client))
        if rows:
            log.info(f"Migrated {len(rows)} accounts from old schema")
    except sqlite3.OperationalError:
        pass
    
    conn.commit()
    conn.close()
    log.info("Enroller database initialized")


# ─── Account Management ──────────────────────────────────────────────────────

def add_account(user_id: int, account_name: str, access_token: str, client_id: str) -> int:
    """Add a new Udemy account for user. Returns account ID."""
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO user_accounts (user_id, account_name, access_token, client_id, is_active, auto_enroll)
        VALUES (?, ?, ?, ?, 1, 1)
    """, (user_id, account_name, access_token, client_id))
    account_id = c.lastrowid
    conn.commit()
    conn.close()
    return account_id


def get_user_accounts(user_id: int) -> list:
    """Get all accounts for a user. Returns list of dicts."""
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        SELECT id, account_name, access_token, client_id, is_active, auto_enroll
        FROM user_accounts WHERE user_id = ? ORDER BY id
    """, (user_id,))
    rows = c.fetchall()
    conn.close()
    return [
        {"id": r[0], "name": r[1], "access_token": r[2], "client_id": r[3],
         "is_active": bool(r[4]), "auto_enroll": bool(r[5])}
        for r in rows
    ]


def get_account(account_id: int) -> dict:
    """Get a specific account by ID"""
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        SELECT id, user_id, account_name, access_token, client_id, is_active, auto_enroll
        FROM user_accounts WHERE id = ?
    """, (account_id,))
    r = c.fetchone()
    conn.close()
    if r:
        return {"id": r[0], "user_id": r[1], "name": r[2], "access_token": r[3],
                "client_id": r[4], "is_active": bool(r[5]), "auto_enroll": bool(r[6])}
    return None


def remove_account(account_id: int) -> bool:
    conn = _conn()
    c = conn.cursor()
    c.execute("DELETE FROM user_accounts WHERE id = ?", (account_id,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def toggle_auto_enroll(account_id: int, enabled: bool) -> None:
    conn = _conn()
    c = conn.cursor()
    c.execute("UPDATE user_accounts SET auto_enroll = ? WHERE id = ?", (int(enabled), account_id))
    conn.commit()
    conn.close()


def get_all_auto_enroll_accounts() -> list:
    """Get all accounts with auto_enroll enabled (across all users)"""
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        SELECT id, user_id, account_name, access_token, client_id
        FROM user_accounts WHERE is_active = 1 AND auto_enroll = 1
    """)
    rows = c.fetchall()
    conn.close()
    return [
        {"id": r[0], "user_id": r[1], "name": r[2], "access_token": r[3], "client_id": r[4]}
        for r in rows
    ]


# ─── Auto-Enroll State ───────────────────────────────────────────────────────

def get_auto_enroll_state(user_id: int) -> dict:
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT enabled, last_check, last_course_id, total_auto_enrolled FROM auto_enroll_state WHERE user_id = ?", (user_id,))
    r = c.fetchone()
    conn.close()
    if r:
        return {"enabled": bool(r[0]), "last_check": r[1], "last_course_id": r[2], "total": r[3]}
    return {"enabled": False, "last_check": None, "last_course_id": None, "total": 0}


def set_auto_enroll_enabled(user_id: int, enabled: bool) -> None:
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO auto_enroll_state (user_id, enabled) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET enabled = ?
    """, (user_id, int(enabled), int(enabled)))
    conn.commit()
    conn.close()


def update_auto_enroll_state(user_id: int, last_course_id: str = None, enrolled_count: int = 0) -> None:
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO auto_enroll_state (user_id, enabled, last_check, last_course_id, total_auto_enrolled)
        VALUES (?, 1, CURRENT_TIMESTAMP, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET 
            last_check = CURRENT_TIMESTAMP,
            last_course_id = COALESCE(?, last_course_id),
            total_auto_enrolled = total_auto_enrolled + ?
    """, (user_id, last_course_id, enrolled_count, last_course_id, enrolled_count))
    conn.commit()
    conn.close()


# ─── Enrolled Course Tracking ────────────────────────────────────────────────

def log_enrollment(user_id: int, account_id: int, course_url: str, course_title: str) -> None:
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO enrolled_courses (user_id, account_id, course_url, course_title)
        VALUES (?, ?, ?, ?)
    """, (user_id, account_id, course_url, course_title))
    conn.commit()
    conn.close()


def get_recently_enrolled(user_id: int, limit: int = 20) -> list:
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        SELECT course_title, enrolled_at, account_id FROM enrolled_courses
        WHERE user_id = ? ORDER BY enrolled_at DESC LIMIT ?
    """, (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"title": r[0], "enrolled_at": r[1], "account_id": r[2]} for r in rows]


def is_course_enrolled(user_id: int, course_url: str) -> bool:
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM enrolled_courses WHERE user_id = ? AND course_url = ?", (user_id, course_url))
    exists = c.fetchone() is not None
    conn.close()
    return exists


# ─── Setup State ─────────────────────────────────────────────────────────────

def set_user_setup_state(user_id: int, step: str, extra: str = None) -> None:
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO user_setup_state (user_id, setup_step, extra_data, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    """, (user_id, step, extra))
    conn.commit()
    conn.close()


def get_user_setup_state(user_id: int) -> tuple:
    """Returns (step, extra_data)"""
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT setup_step, extra_data FROM user_setup_state WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return (result[0], result[1]) if result else (None, None)


def clear_user_setup_state(user_id: int) -> None:
    conn = _conn()
    c = conn.cursor()
    c.execute("DELETE FROM user_setup_state WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ─── Legacy Compatibility ────────────────────────────────────────────────────

def user_has_credentials(user_id: int) -> bool:
    accounts = get_user_accounts(user_id)
    return len(accounts) > 0


def get_user_credentials(user_id: int) -> dict:
    """Legacy: get first active account credentials"""
    accounts = get_user_accounts(user_id)
    if accounts:
        return {"access_token": accounts[0]["access_token"], "client_id": accounts[0]["client_id"]}
    return None


def store_user_credentials(user_id: int, access_token: str = None, client_id: str = None) -> None:
    """Legacy: store credentials as Account 1"""
    accounts = get_user_accounts(user_id)
    if accounts:
        conn = _conn()
        c = conn.cursor()
        if access_token:
            c.execute("UPDATE user_accounts SET access_token = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                      (access_token, accounts[0]["id"]))
        if client_id:
            c.execute("UPDATE user_accounts SET client_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                      (client_id, accounts[0]["id"]))
        conn.commit()
        conn.close()
    else:
        add_account(user_id, "Account 1", access_token or "", client_id or "")


def log_scrape_history(user_id: int, site_name: str, course_count: int) -> None:
    pass


def get_user_stats(user_id: int) -> dict:
    accounts = get_user_accounts(user_id)
    state = get_auto_enroll_state(user_id)
    return {
        "total_accounts": len(accounts),
        "auto_enroll_total": state["total"],
        "last_check": state["last_check"],
    }


def validate_token_format(token: str) -> bool:
    return bool(token) and len(token) > 20


def validate_client_id_format(client_id: str) -> bool:
    return bool(client_id) and len(client_id) >= 2


def get_setup_instructions() -> str:
    return """
🔐 **How to get Udemy cookies:**

1. Open https://www.udemy.com in your browser
2. Log in with your Udemy account
3. Press **F12** → **Application** tab
4. Select **Cookies** → `udemy.com`
5. Copy these values:
   - `access_token` (long string)
   - `client_id` (short hex string)

Send them when asked!
"""


def delete_user_data(user_id: int) -> bool:
    conn = _conn()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM user_accounts WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM user_setup_state WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM enrolled_courses WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM auto_enroll_state WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM daily_usage WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except Exception as e:
        log.error(f"Error deleting user data: {e}")
        return False
    finally:
        conn.close()


# ─── Premium & Access Control ─────────────────────────────────────────────────

def is_owner(user_id: int) -> bool:
    """Check if user is the bot owner"""
    return user_id == OWNER_ID and OWNER_ID != 0


def is_premium(user_id: int) -> bool:
    """Check if user has premium access"""
    if is_owner(user_id):
        return True
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM premium_users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None


def grant_premium(user_id: int, granted_by: int) -> bool:
    """Grant premium access to a user"""
    conn = _conn()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT OR REPLACE INTO premium_users (user_id, granted_by, granted_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (user_id, granted_by))
        conn.commit()
        return True
    except Exception as e:
        log.error(f"Error granting premium: {e}")
        return False
    finally:
        conn.close()


def revoke_premium(user_id: int) -> bool:
    """Revoke premium access from a user"""
    conn = _conn()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM premium_users WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except Exception as e:
        log.error(f"Error revoking premium: {e}")
        return False
    finally:
        conn.close()


def get_all_premium_users() -> list:
    """Get all premium users"""
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT user_id, granted_by, granted_at FROM premium_users")
    rows = c.fetchall()
    conn.close()
    return [{"user_id": r[0], "granted_by": r[1], "granted_at": r[2]} for r in rows]


# ─── Daily Usage Limits ───────────────────────────────────────────────────────

def get_today_str() -> str:
    """Get today's date as string YYYY-MM-DD"""
    return datetime.now().strftime("%Y-%m-%d")


def get_daily_usage(user_id: int) -> int:
    """Get number of enrollments today for a user"""
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT enroll_count FROM daily_usage WHERE user_id = ? AND date = ?",
              (user_id, get_today_str()))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0


def increment_daily_usage(user_id: int, count: int = 1) -> int:
    """Increment daily usage count. Returns new total."""
    today = get_today_str()
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO daily_usage (user_id, date, enroll_count)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, date) DO UPDATE SET enroll_count = enroll_count + ?
    """, (user_id, today, count, count))
    conn.commit()
    c.execute("SELECT enroll_count FROM daily_usage WHERE user_id = ? AND date = ?",
              (user_id, today))
    result = c.fetchone()
    conn.close()
    return result[0] if result else count


def get_remaining_today(user_id: int) -> int:
    """Get remaining enrollments for today. Returns -1 for unlimited (premium/owner)."""
    if is_premium(user_id):
        return -1  # Unlimited
    used = get_daily_usage(user_id)
    return max(0, FREE_DAILY_LIMIT - used)


def can_enroll(user_id: int, count: int = 1) -> tuple:
    """Check if user can enroll. Returns (can_enroll: bool, remaining: int, is_premium: bool)"""
    if is_premium(user_id):
        return True, -1, True
    remaining = get_remaining_today(user_id)
    return remaining >= count, remaining, False


def cleanup_old_usage(days: int = 30) -> int:
    """Clean up usage records older than X days"""
    conn = _conn()
    c = conn.cursor()
    c.execute("DELETE FROM daily_usage WHERE date < date('now', '-' || ? || ' days')", (days,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted


def get_all_daily_stats() -> dict:
    """Get today's enrollment stats for all users. Returns dict with user stats."""
    today = get_today_str()
    conn = _conn()
    c = conn.cursor()
    
    # Get all users with today's usage
    c.execute("""
        SELECT user_id, enroll_count FROM daily_usage WHERE date = ?
        ORDER BY enroll_count DESC
    """, (today,))
    rows = c.fetchall()
    
    # Get total for today
    c.execute("SELECT SUM(enroll_count) FROM daily_usage WHERE date = ?", (today,))
    total_result = c.fetchone()
    total_today = total_result[0] if total_result and total_result[0] else 0
    
    # Get all-time total (all dates)
    c.execute("SELECT SUM(enroll_count) FROM daily_usage")
    all_result = c.fetchone()
    all_time = all_result[0] if all_result and all_result[0] else 0
    
    conn.close()
    
    return {
        "today_total": total_today,
        "all_time_total": all_time,
        "users": [{"user_id": r[0], "count": r[1]} for r in rows],
        "date": today
    }


def get_user_total_enrollments(user_id: int) -> int:
    """Get total enrollments for a user across all time"""
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT SUM(enroll_count) FROM daily_usage WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result and result[0] else 0


# Initialize
init_enroller_db()
