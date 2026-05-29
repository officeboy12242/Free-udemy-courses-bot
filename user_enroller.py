"""
Multi-User Udemy Enroller - Database and credential management
Stores per-user access tokens and client IDs for automatic enrollment
"""

import sqlite3
import os
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger(__name__)

# Database file
ENROLL_DB_FILE = "user_enroller.db"

# ─── Database Schema ─────────────────────────────────────────────────────────

def init_enroller_db():
    """Initialize enroller database with tables for user credentials"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    
    # User credentials table
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_credentials (
            user_id INTEGER PRIMARY KEY,
            access_token TEXT,
            client_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_verified INTEGER DEFAULT 0
        )
    """)
    
    # User setup state tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_setup_state (
            user_id INTEGER PRIMARY KEY,
            setup_step TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Scrape history (for reference)
    c.execute("""
        CREATE TABLE IF NOT EXISTS scrape_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            site_name TEXT,
            course_count INTEGER,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES user_credentials(user_id)
        )
    """)
    
    conn.commit()
    conn.close()
    log.info("✅ Enroller database initialized")


# ─── User Credential Management ──────────────────────────────────────────────

def set_user_setup_state(user_id: int, step: str) -> None:
    """Set user's current setup step (waiting_token, waiting_client_id, etc.)"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    
    c.execute("""
        INSERT OR REPLACE INTO user_setup_state (user_id, setup_step, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (user_id, step))
    
    conn.commit()
    conn.close()


def get_user_setup_state(user_id: int) -> str:
    """Get user's current setup step"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    
    c.execute("SELECT setup_step FROM user_setup_state WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    
    return result[0] if result else None


def clear_user_setup_state(user_id: int) -> None:
    """Clear user's setup state"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM user_setup_state WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def store_user_credentials(user_id: int, access_token: str = None, client_id: str = None) -> None:
    """Store or update user's Udemy credentials"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    
    # Get existing credentials
    c.execute("SELECT access_token, client_id FROM user_credentials WHERE user_id = ?", (user_id,))
    existing = c.fetchone()
    
    if existing:
        # Update existing
        token = access_token if access_token else existing[0]
        client = client_id if client_id else existing[1]
        verified = 1 if (token and client) else 0
        
        c.execute("""
            UPDATE user_credentials 
            SET access_token = ?, client_id = ?, is_verified = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (token, client, verified, user_id))
    else:
        # Create new
        c.execute("""
            INSERT INTO user_credentials (user_id, access_token, client_id, is_verified)
            VALUES (?, ?, ?, ?)
        """, (user_id, access_token, client_id, 1 if (access_token and client_id) else 0))
    
    conn.commit()
    conn.close()
    log.info(f"✅ Credentials stored for user {user_id}")


def get_user_credentials(user_id: int) -> dict:
    """Get user's Udemy credentials"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    
    c.execute("""
        SELECT access_token, client_id, is_verified 
        FROM user_credentials 
        WHERE user_id = ?
    """, (user_id,))
    
    result = c.fetchone()
    conn.close()
    
    if result:
        return {
            "access_token": result[0],
            "client_id": result[1],
            "is_verified": bool(result[2])
        }
    return None


def user_has_credentials(user_id: int) -> bool:
    """Check if user has stored credentials"""
    creds = get_user_credentials(user_id)
    return creds and creds.get("access_token") and creds.get("client_id")


def log_scrape_history(user_id: int, site_name: str, course_count: int) -> None:
    """Log scrape history for analytics"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    
    c.execute("""
        INSERT INTO scrape_history (user_id, site_name, course_count)
        VALUES (?, ?, ?)
    """, (user_id, site_name, course_count))
    
    conn.commit()
    conn.close()


def get_user_stats(user_id: int) -> dict:
    """Get user's scraping statistics"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    
    # Get total scrapes
    c.execute("""
        SELECT COUNT(*) FROM scrape_history WHERE user_id = ?
    """, (user_id,))
    total_scrapes = c.fetchone()[0]
    
    # Get total courses scraped
    c.execute("""
        SELECT SUM(course_count) FROM scrape_history WHERE user_id = ?
    """, (user_id,))
    total_courses = c.fetchone()[0] or 0
    
    # Get last scrape time
    c.execute("""
        SELECT MAX(scraped_at) FROM scrape_history WHERE user_id = ?
    """, (user_id,))
    last_scrape = c.fetchone()[0]
    
    conn.close()
    
    return {
        "total_scrapes": total_scrapes,
        "total_courses": total_courses,
        "last_scrape": last_scrape
    }


# ─── User Setup Flow ─────────────────────────────────────────────────────────

SETUP_STEPS = {
    "not_started": "Setup not started",
    "waiting_token": "Waiting for access_token",
    "waiting_client_id": "Waiting for client_id",
    "complete": "Setup complete",
}


def get_setup_instructions() -> str:
    """Get instructions for getting Udemy cookies"""
    return """
🔐 **Setup Instructions:**

To enable automatic course enrollment, you need your Udemy cookies:

**Steps:**
1. Open https://www.udemy.com in your browser
2. Log in with your Udemy account
3. Press **F12** to open Developer Tools
4. Go to **Application** tab
5. Select **Cookies** → `udemy.com`
6. Find these cookies:
   - `access_token` → Copy the value
   - `client_id` → Copy the value

**Example values:**
- access_token: `eyJhbGc...` (long string)
- client_id: `6U...` (short string)

Send them to me one by one:
1. `/set_token <your_access_token>`
2. `/set_client_id <your_client_id>`

Or type them directly when I ask!
"""


# ─── Validate Credentials ────────────────────────────────────────────────────

def validate_token_format(token: str) -> bool:
    """Basic validation for access token format"""
    if not token or len(token) < 20:
        return False
    # Access tokens usually start with certain patterns or are base64-like
    return len(token) > 50


def validate_client_id_format(client_id: str) -> bool:
    """Basic validation for client ID format"""
    if not client_id or len(client_id) < 2:
        return False
    return True


# ─── Database Utilities ──────────────────────────────────────────────────────

def cleanup_old_setup_states(days: int = 7) -> int:
    """Remove setup states older than specified days"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    
    c.execute("""
        DELETE FROM user_setup_state 
        WHERE updated_at < datetime('now', '-' || ? || ' days')
    """, (days,))
    
    deleted = c.rowcount
    conn.commit()
    conn.close()
    
    return deleted


def get_all_verified_users() -> list:
    """Get all users with complete setup"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    
    c.execute("""
        SELECT user_id, access_token, client_id 
        FROM user_credentials 
        WHERE is_verified = 1
    """)
    
    results = c.fetchall()
    conn.close()
    
    return results


def delete_user_data(user_id: int) -> bool:
    """Delete all data for a user (GDPR compliance)"""
    conn = sqlite3.connect(ENROLL_DB_FILE)
    c = conn.cursor()
    
    try:
        c.execute("DELETE FROM user_credentials WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM user_setup_state WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM scrape_history WHERE user_id = ?", (user_id,))
        conn.commit()
        log.info(f"🗑️ All data deleted for user {user_id}")
        return True
    except Exception as e:
        log.error(f"Error deleting user data: {e}")
        return False
    finally:
        conn.close()


# Initialize database on module load
if not Path(ENROLL_DB_FILE).exists():
    init_enroller_db()
