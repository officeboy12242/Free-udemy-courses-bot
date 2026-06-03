"""
Multi-Account Udemy Enroller - MongoDB Database Management
Supports multiple Udemy accounts per user with auto-enrollment tracking
Persists across deployments using MongoDB Atlas
"""

import os
import logging
from datetime import datetime, timedelta
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

log = logging.getLogger(__name__)

# Daily enrollment limit for free users
FREE_DAILY_LIMIT = 20
# Owner ID from environment (gets full access)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
# MongoDB connection string from environment
MONGODB_URI = os.getenv("MONGODB_URI", "")

# Global database connection
_client = None
_db = None


def _get_db():
    """Get MongoDB database connection (lazy initialization with auto-reconnect)"""
    global _client, _db
    
    if not MONGODB_URI:
        raise ValueError("MONGODB_URI environment variable not set")
    
    # Check if connection is healthy
    if _client is not None:
        try:
            _client.admin.command('ping')
            return _db
        except Exception:
            log.warning("MongoDB connection lost, reconnecting...")
            _client = None
            _db = None
    
    # Try connection with different TLS settings
    log.info("Connecting to MongoDB...")
    
    # First try: with certifi certificates
    try:
        import certifi
        _client = MongoClient(
            MONGODB_URI,
            tls=True,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
            retryWrites=True,
            retryReads=True,
        )
        _client.admin.command('ping')
        _db = _client.udemy_enroller
        log.info("Connected to MongoDB with certifi")
        return _db
    except Exception as e1:
        log.warning(f"Certifi connection failed: {e1}")
    
    # Second try: with tlsAllowInvalidCertificates
    try:
        _client = MongoClient(
            MONGODB_URI,
            tls=True,
            tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
            retryWrites=True,
            retryReads=True,
        )
        _client.admin.command('ping')
        _db = _client.udemy_enroller
        log.info("Connected to MongoDB with tlsAllowInvalidCertificates")
        return _db
    except Exception as e2:
        log.error(f"Both connection methods failed: {e2}")
        _client = None
        _db = None
        raise ConnectionError(f"Cannot connect to MongoDB: {e2}")


def init_enroller_db():
    """Initialize MongoDB collections and indexes"""
    try:
        db = _get_db()
        
        # Create indexes for better performance
        db.user_accounts.create_index([("user_id", 1)])
        db.user_accounts.create_index([("user_id", 1), ("is_active", 1), ("auto_enroll", 1)])
        db.enrolled_courses.create_index([("user_id", 1), ("course_url", 1)])
        db.enrolled_courses.create_index([("user_id", 1), ("enrolled_at", -1)])
        db.daily_usage.create_index([("user_id", 1), ("date", 1)], unique=True)
        db.premium_users.create_index([("user_id", 1)], unique=True)
        db.user_setup_state.create_index([("user_id", 1)], unique=True)
        db.auto_enroll_state.create_index([("user_id", 1)], unique=True)

        # Owner course archive / download queue
        db.owner_download_queue.create_index([("owner_id", 1)])
        db.owner_download_queue.create_index([("owner_id", 1), ("udemy_course_id", 1)], unique=True)
        db.owner_archive_jobs.create_index([("owner_id", 1), ("udemy_course_id", 1)], unique=True)
        db.owner_archive_jobs.create_index([("owner_id", 1), ("status", 1)])
        
        log.info("MongoDB indexes created successfully")
    except Exception as e:
        log.error(f"MongoDB init error: {e}")


# ─── Account Management ──────────────────────────────────────────────────────

DEFAULT_CLIENT_ID = "bd2565cb7b0c313f5e9bae44961e8db2"


def add_account(user_id: int, account_name: str, access_token: str, client_id: str = None, udemy_user_id: int = None) -> int:
    """Add a new Udemy account for user. Returns account ID. Uses default client_id if not provided."""
    db = _get_db()
    
    # Generate a simple incrementing ID
    counter = db.counters.find_one_and_update(
        {"_id": "account_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True
    )
    account_id = counter["seq"]
    
    db.user_accounts.insert_one({
        "_id": account_id,
        "user_id": user_id,
        "account_name": account_name,
        "access_token": access_token,
        "client_id": client_id or DEFAULT_CLIENT_ID,
        "udemy_user_id": udemy_user_id,
        "is_active": True,
        "auto_enroll": True,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    })
    
    return account_id


def find_existing_account(user_id: int, access_token: str, udemy_user_id: int = None) -> dict:
    """Find an existing account for the user by access token or Udemy user ID"""
    db = _get_db()
    or_conditions = [{"access_token": access_token}]
    if udemy_user_id is not None:
        or_conditions.append({"udemy_user_id": udemy_user_id})
    
    query = {
        "user_id": user_id,
        "$or": or_conditions
    }
    
    a = db.user_accounts.find_one(query)
    if a:
        return {
            "id": a["_id"],
            "user_id": a["user_id"],
            "name": a["account_name"],
            "access_token": a["access_token"],
            "client_id": a["client_id"],
            "udemy_user_id": a.get("udemy_user_id"),
            "is_active": a.get("is_active", True),
            "auto_enroll": a.get("auto_enroll", True)
        }
    return None


def update_account_token(account_id: int, access_token: str, account_name: str = None) -> None:
    """Update access token and name of an existing account"""
    db = _get_db()
    update_data = {
        "access_token": access_token,
        "updated_at": datetime.utcnow()
    }
    if account_name:
        update_data["account_name"] = account_name
        
    db.user_accounts.update_one(
        {"_id": account_id},
        {"$set": update_data}
    )


def get_user_accounts(user_id: int) -> list:
    """Get all accounts for a user. Returns list of dicts."""
    db = _get_db()
    accounts = db.user_accounts.find({"user_id": user_id}).sort("_id", 1)
    return [
        {
            "id": a["_id"],
            "name": a["account_name"],
            "access_token": a["access_token"],
            "client_id": a["client_id"],
            "udemy_user_id": a.get("udemy_user_id"),
            "is_active": a.get("is_active", True),
            "auto_enroll": a.get("auto_enroll", True)
        }
        for a in accounts
    ]


def get_account(account_id: int) -> dict:
    """Get a specific account by ID"""
    db = _get_db()
    a = db.user_accounts.find_one({"_id": account_id})
    if a:
        return {
            "id": a["_id"],
            "user_id": a["user_id"],
            "name": a["account_name"],
            "access_token": a["access_token"],
            "client_id": a["client_id"],
            "udemy_user_id": a.get("udemy_user_id"),
            "is_active": a.get("is_active", True),
            "auto_enroll": a.get("auto_enroll", True)
        }
    return None


def remove_account(account_id: int) -> bool:
    """Remove an account by ID"""
    db = _get_db()
    result = db.user_accounts.delete_one({"_id": account_id})
    return result.deleted_count > 0


def toggle_auto_enroll(account_id: int, enabled: bool) -> None:
    """Toggle auto-enroll for an account"""
    db = _get_db()
    db.user_accounts.update_one(
        {"_id": account_id},
        {"$set": {"auto_enroll": enabled, "updated_at": datetime.utcnow()}}
    )


def get_all_auto_enroll_accounts() -> list:
    """Get all accounts with auto_enroll enabled (across all users)"""
    db = _get_db()
    accounts = db.user_accounts.find({
        "is_active": True,
        "auto_enroll": True
    })
    return [
        {
            "id": a["_id"],
            "user_id": a["user_id"],
            "name": a["account_name"],
            "access_token": a["access_token"],
            "client_id": a["client_id"]
        }
        for a in accounts
    ]


# ─── Auto-Enroll State ───────────────────────────────────────────────────────

def get_auto_enroll_state(user_id: int) -> dict:
    """Get auto-enroll state for a user"""
    db = _get_db()
    state = db.auto_enroll_state.find_one({"user_id": user_id})
    if state:
        return {
            "enabled": state.get("enabled", False),
            "last_check": state.get("last_check"),
            "last_course_id": state.get("last_course_id"),
            "total": state.get("total_auto_enrolled", 0)
        }
    return {"enabled": False, "last_check": None, "last_course_id": None, "total": 0}


def set_auto_enroll_enabled(user_id: int, enabled: bool) -> None:
    """Set auto-enroll enabled status for a user"""
    db = _get_db()
    db.auto_enroll_state.update_one(
        {"user_id": user_id},
        {"$set": {"enabled": enabled, "updated_at": datetime.utcnow()}},
        upsert=True
    )


def update_auto_enroll_state(user_id: int, last_course_id: str = None, enrolled_count: int = 0) -> None:
    """Update auto-enroll state after a check"""
    db = _get_db()
    update = {
        "$set": {
            "enabled": True,
            "last_check": datetime.utcnow()
        }
    }
    if last_course_id:
        update["$set"]["last_course_id"] = last_course_id
    if enrolled_count > 0:
        update["$inc"] = {"total_auto_enrolled": enrolled_count}
    
    db.auto_enroll_state.update_one({"user_id": user_id}, update, upsert=True)


# ─── Enrolled Course Tracking ────────────────────────────────────────────────

def log_enrollment(user_id: int, account_id: int, course_url: str, course_title: str, slug: str = None) -> None:
    """Log a course enrollment. Uses upsert by slug to avoid duplicate notifications."""
    db = _get_db()
    # Extract slug from URL if not provided
    if not slug and course_url:
        parts = course_url.strip("/").split("/")
        # URL format: /course/slug/ or just slug
        slug = parts[-1] if parts else None
        if slug == "draft" and len(parts) > 1:
            slug = parts[-2]
    
    if slug:
        # Upsert by user_id + slug to prevent duplicate entries
        db.enrolled_courses.update_one(
            {"user_id": user_id, "slug": slug},
            {"$set": {
                "user_id": user_id,
                "account_id": account_id,
                "course_url": course_url,
                "course_title": course_title,
                "slug": slug,
                "enrolled_at": datetime.utcnow()
            }},
            upsert=True
        )
    else:
        # Fallback: insert without slug (legacy behavior)
        db.enrolled_courses.insert_one({
            "user_id": user_id,
            "account_id": account_id,
            "course_url": course_url,
            "course_title": course_title,
            "enrolled_at": datetime.utcnow()
        })


def get_recently_enrolled(user_id: int, limit: int = 20) -> list:
    """Get recently enrolled courses for a user"""
    db = _get_db()
    courses = db.enrolled_courses.find(
        {"user_id": user_id}
    ).sort("enrolled_at", -1).limit(limit)
    return [
        {
            "title": c.get("course_title", ""),
            "enrolled_at": c.get("enrolled_at"),
            "account_id": c.get("account_id")
        }
        for c in courses
    ]


def is_course_enrolled(user_id: int, course_url: str) -> bool:
    """Check if a course is already enrolled by user"""
    db = _get_db()
    return db.enrolled_courses.find_one({"user_id": user_id, "course_url": course_url}) is not None


def is_course_enrolled_by_slug(user_id: int, slug: str) -> bool:
    """Check if a course is already enrolled by user (by slug - more reliable)"""
    db = _get_db()
    return db.enrolled_courses.find_one({"user_id": user_id, "slug": slug}) is not None


def get_enrolled_slugs_for_user(user_id: int) -> set:
    """Get all enrolled course slugs for a user (for fast local dedup)"""
    db = _get_db()
    docs = db.enrolled_courses.find({"user_id": user_id, "slug": {"$exists": True}}, {"slug": 1})
    return {d["slug"] for d in docs if d.get("slug")}


# ─── Setup State ─────────────────────────────────────────────────────────────

def set_user_setup_state(user_id: int, step: str, extra: str = None) -> None:
    """Set user setup state"""
    db = _get_db()
    db.user_setup_state.update_one(
        {"user_id": user_id},
        {"$set": {"setup_step": step, "extra_data": extra, "updated_at": datetime.utcnow()}},
        upsert=True
    )


def get_user_setup_state(user_id: int) -> tuple:
    """Returns (step, extra_data)"""
    db = _get_db()
    state = db.user_setup_state.find_one({"user_id": user_id})
    if state:
        return (state.get("setup_step"), state.get("extra_data"))
    return (None, None)


def clear_user_setup_state(user_id: int) -> None:
    """Clear user setup state"""
    db = _get_db()
    db.user_setup_state.delete_one({"user_id": user_id})


# ─── Legacy Compatibility ────────────────────────────────────────────────────

def user_has_credentials(user_id: int) -> bool:
    """Check if user has any accounts"""
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
        db = _get_db()
        update = {"updated_at": datetime.utcnow()}
        if access_token:
            update["access_token"] = access_token
        if client_id:
            update["client_id"] = client_id
        db.user_accounts.update_one({"_id": accounts[0]["id"]}, {"$set": update})
    else:
        add_account(user_id, "Account 1", access_token or "", client_id or "")


def log_scrape_history(user_id: int, site_name: str, course_count: int) -> None:
    """Legacy: no-op"""
    pass


def get_user_stats(user_id: int) -> dict:
    """Get user statistics"""
    accounts = get_user_accounts(user_id)
    state = get_auto_enroll_state(user_id)
    return {
        "total_accounts": len(accounts),
        "auto_enroll_total": state["total"],
        "last_check": state["last_check"],
    }


def validate_token_format(token: str) -> bool:
    """Validate access token format"""
    return bool(token) and len(token) > 20


def validate_client_id_format(client_id: str) -> bool:
    """Validate client ID format"""
    return bool(client_id) and len(client_id) >= 2


def get_setup_instructions() -> str:
    """Return setup instructions"""
    return """
🔐 **How to get your Udemy access token:**

1. Open https://www.udemy.com in your browser
2. Log in with your Udemy account
3. Press **F12** → **Application** tab
4. Click **Cookies** → `www.udemy.com`
5. Find `access_token` and copy its value

That's it! Just send the `access_token` value.
"""


def delete_user_data(user_id: int) -> bool:
    """Delete all data for a user"""
    try:
        db = _get_db()
        db.user_accounts.delete_many({"user_id": user_id})
        db.user_setup_state.delete_one({"user_id": user_id})
        db.enrolled_courses.delete_many({"user_id": user_id})
        db.auto_enroll_state.delete_one({"user_id": user_id})
        db.daily_usage.delete_many({"user_id": user_id})
        db.premium_users.delete_one({"user_id": user_id})
        return True
    except Exception as e:
        log.error(f"Error deleting user data: {e}")
        return False


# ─── Premium & Access Control ─────────────────────────────────────────────────

def is_owner(user_id: int) -> bool:
    """Check if user is the bot owner"""
    return user_id == OWNER_ID and OWNER_ID != 0


def is_premium(user_id: int) -> bool:
    """Check if user has premium access"""
    if is_owner(user_id):
        return True
    db = _get_db()
    return db.premium_users.find_one({"user_id": user_id}) is not None


def grant_premium(user_id: int, granted_by: int) -> bool:
    """Grant premium access to a user"""
    try:
        db = _get_db()
        db.premium_users.update_one(
            {"user_id": user_id},
            {"$set": {"user_id": user_id, "granted_by": granted_by, "granted_at": datetime.utcnow()}},
            upsert=True
        )
        return True
    except Exception as e:
        log.error(f"Error granting premium: {e}")
        return False


def revoke_premium(user_id: int) -> bool:
    """Revoke premium access from a user"""
    try:
        db = _get_db()
        db.premium_users.delete_one({"user_id": user_id})
        return True
    except Exception as e:
        log.error(f"Error revoking premium: {e}")
        return False


def get_all_premium_users() -> list:
    """Get all premium users"""
    db = _get_db()
    users = db.premium_users.find()
    return [
        {
            "user_id": u["user_id"],
            "granted_by": u.get("granted_by"),
            "granted_at": str(u.get("granted_at", ""))[:10]
        }
        for u in users
    ]


# ─── Daily Usage Limits ───────────────────────────────────────────────────────

def get_today_str() -> str:
    """Get today's date as string YYYY-MM-DD"""
    return datetime.utcnow().strftime("%Y-%m-%d")


def get_daily_usage(user_id: int) -> int:
    """Get number of enrollments today for a user"""
    db = _get_db()
    today = get_today_str()
    doc = db.daily_usage.find_one({"user_id": user_id, "date": today})
    return doc["enroll_count"] if doc else 0


def increment_daily_usage(user_id: int, count: int = 1) -> int:
    """Increment daily usage count. Returns new total."""
    db = _get_db()
    today = get_today_str()
    
    result = db.daily_usage.find_one_and_update(
        {"user_id": user_id, "date": today},
        {"$inc": {"enroll_count": count}},
        upsert=True,
        return_document=True
    )
    
    return result["enroll_count"] if result else count


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
    db = _get_db()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    result = db.daily_usage.delete_many({"date": {"$lt": cutoff}})
    return result.deleted_count


def get_all_daily_stats() -> dict:
    """Get today's enrollment stats for all users. Returns dict with user stats."""
    db = _get_db()
    today = get_today_str()
    
    # Get all users with today's usage
    users = list(db.daily_usage.find({"date": today}).sort("enroll_count", -1))
    
    # Get total for today
    pipeline = [
        {"$match": {"date": today}},
        {"$group": {"_id": None, "total": {"$sum": "$enroll_count"}}}
    ]
    today_agg = list(db.daily_usage.aggregate(pipeline))
    total_today = today_agg[0]["total"] if today_agg else 0
    
    # Get all-time total
    all_agg = list(db.daily_usage.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$enroll_count"}}}
    ]))
    all_time = all_agg[0]["total"] if all_agg else 0
    
    return {
        "today_total": total_today,
        "all_time_total": all_time,
        "users": [{"user_id": u["user_id"], "count": u["enroll_count"]} for u in users],
        "date": today
    }


def get_user_total_enrollments(user_id: int) -> int:
    """Get total enrollments for a user across all time"""
    db = _get_db()
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": None, "total": {"$sum": "$enroll_count"}}}
    ]
    result = list(db.daily_usage.aggregate(pipeline))
    return result[0]["total"] if result else 0


# ─── Bot Settings (Owner) ─────────────────────────────────────────────────────

def get_setting(key: str, default=None):
    """Get a bot setting value"""
    try:
        db = _get_db()
        doc = db.bot_settings.find_one({"key": key})
        return doc["value"] if doc else default
    except Exception:
        return default


def set_setting(key: str, value) -> bool:
    """Set a bot setting value"""
    try:
        db = _get_db()
        db.bot_settings.update_one(
            {"key": key},
            {"$set": {"key": key, "value": value, "updated_at": datetime.utcnow()}},
            upsert=True
        )
        return True
    except Exception as e:
        log.error(f"Failed to set setting {key}: {e}")
        return False


def is_channel_posting_enabled() -> bool:
    """Check if channel posting is enabled"""
    return get_setting("channel_posting", False)


def toggle_channel_posting() -> bool:
    """Toggle channel posting on/off. Returns new state."""
    current = is_channel_posting_enabled()
    new_state = not current
    set_setting("channel_posting", new_state)
    return new_state


# ─── Owner Course Download / Archive Queue ────────────────────────────────────
# Allows the owner to search enrolled courses across linked accounts,
# select interesting ones, and later download+zip+upload them to the channel.

def add_to_download_queue(owner_id: int, udemy_course_id: int, title: str, course_url: str, source_account_id: int | None = None) -> bool:
    """Add a Udemy course to the owner's download/zip queue. Idempotent (unique on owner+udemy id)."""
    try:
        db = _get_db()
        db.owner_download_queue.update_one(
            {"owner_id": owner_id, "udemy_course_id": udemy_course_id},
            {"$set": {
                "title": title,
                "course_url": course_url,
                "source_account_id": source_account_id,
                "added_at": datetime.utcnow()
            }},
            upsert=True
        )
        return True
    except Exception as e:
        log.error(f"add_to_download_queue error: {e}")
        return False


def get_owner_download_queue(owner_id: int) -> list:
    """Return list of selected courses for the owner, newest first."""
    db = _get_db()
    docs = db.owner_download_queue.find({"owner_id": owner_id}).sort("added_at", -1)
    items = []
    for d in docs:
        job = db.owner_archive_jobs.find_one(
            {"owner_id": owner_id, "udemy_course_id": d["udemy_course_id"]},
            {"status": 1, "progress": 1, "stage": 1, "posted_at": 1, "zip_size_mb": 1, "part_count": 1},
        ) or {}
        items.append({
            "udemy_course_id": d["udemy_course_id"],
            "title": d.get("title", "Untitled"),
            "course_url": d.get("course_url", ""),
            "source_account_id": d.get("source_account_id"),
            "added_at": d.get("added_at"),
            "job_status": job.get("status"),
            "job_progress": job.get("progress", 0),
            "job_stage": job.get("stage", ""),
            "posted_at": job.get("posted_at"),
            "zip_size_mb": job.get("zip_size_mb", 0),
            "part_count": job.get("part_count", 0),
        })
    return items


def remove_from_download_queue(owner_id: int, udemy_course_id: int) -> bool:
    """Remove one item from the queue."""
    try:
        db = _get_db()
        res = db.owner_download_queue.delete_one({"owner_id": owner_id, "udemy_course_id": udemy_course_id})
        return res.deleted_count > 0
    except Exception as e:
        log.error(f"remove_from_download_queue error: {e}")
        return False


def clear_owner_download_queue(owner_id: int) -> int:
    """Clear the entire queue for the owner. Returns number deleted."""
    try:
        db = _get_db()
        res = db.owner_download_queue.delete_many({"owner_id": owner_id})
        return res.deleted_count
    except Exception as e:
        log.error(f"clear_owner_download_queue error: {e}")
        return 0


def get_archive_job(owner_id: int, udemy_course_id: int) -> dict | None:
    """Return persisted archive job state for a course, if any."""
    try:
        db = _get_db()
        return db.owner_archive_jobs.find_one({"owner_id": owner_id, "udemy_course_id": udemy_course_id})
    except Exception as e:
        log.error(f"get_archive_job error: {e}")
        return None


def upsert_archive_job(owner_id: int, udemy_course_id: int, **fields) -> bool:
    """Create/update archive job state. Used to resume stuck/partial archives."""
    try:
        db = _get_db()
        now = datetime.utcnow()
        update = {
            "$set": {**fields, "updated_at": now},
            "$setOnInsert": {
                "owner_id": owner_id,
                "udemy_course_id": udemy_course_id,
                "created_at": now,
            },
        }
        db.owner_archive_jobs.update_one(
            {"owner_id": owner_id, "udemy_course_id": udemy_course_id},
            update,
            upsert=True,
        )
        return True
    except Exception as e:
        log.error(f"upsert_archive_job error: {e}")
        return False


def mark_archive_job_heartbeat(owner_id: int, udemy_course_id: int, stage: str = None, progress: int = None) -> bool:
    """Update liveness/progress timestamp for an archive job."""
    fields = {"last_heartbeat": datetime.utcnow()}
    if stage is not None:
        fields["stage"] = stage
    if progress is not None:
        fields["progress"] = progress
    return upsert_archive_job(owner_id, udemy_course_id, **fields)


def mark_archive_job_posted(owner_id: int, udemy_course_id: int, part_count: int = 0, zip_size_mb: int = 0) -> bool:
    """Mark archive job complete and posted to Telegram."""
    return upsert_archive_job(
        owner_id,
        udemy_course_id,
        status="posted",
        posted_at=datetime.utcnow(),
        part_count=part_count,
        zip_size_mb=zip_size_mb,
        progress=100,
        stage="Posted to channel",
        last_heartbeat=datetime.utcnow(),
    )


# Initialize on import
try:
    if MONGODB_URI:
        init_enroller_db()
    else:
        log.warning("MONGODB_URI not set - database features disabled")
except Exception as e:
    log.error(f"Failed to initialize MongoDB: {e}")
