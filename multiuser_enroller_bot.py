"""
Multi-Account Udemy Auto-Enroller Bot
- Multiple accounts per user
- Auto-enroll background job (checks API every 10 min)
- Notifications when new courses are enrolled
"""

import asyncio
import logging
import os
import shutil
import tempfile
import zipfile
import requests
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ContextTypes, Application

# Try to import psutil for disk/bandwidth monitoring
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

from udemy_enroller import Course, UdemyAutoEnroller
from user_enroller import (
    init_enroller_db,
    add_account, get_user_accounts, get_account, remove_account, toggle_auto_enroll,
    get_all_auto_enroll_accounts, find_existing_account, update_account_token,
    add_to_download_queue, get_owner_download_queue, remove_from_download_queue, clear_owner_download_queue,
    get_archive_job, upsert_archive_job, mark_archive_job_heartbeat, mark_archive_job_posted,
    set_user_setup_state, get_user_setup_state, clear_user_setup_state,
    DEFAULT_CLIENT_ID,
    get_auto_enroll_state, set_auto_enroll_enabled, update_auto_enroll_state,
    log_enrollment, is_course_enrolled, get_recently_enrolled,
    user_has_credentials, get_user_stats,
    validate_token_format, validate_client_id_format, get_setup_instructions,
    delete_user_data,
    # Premium & Access Control
    is_owner, is_premium, grant_premium, revoke_premium, get_all_premium_users,
    can_enroll, get_remaining_today, increment_daily_usage, FREE_DAILY_LIMIT,
    get_all_daily_stats, get_daily_usage, get_user_total_enrollments,
    # Settings
    is_channel_posting_enabled, toggle_channel_posting,
)

CHANNEL_ID = os.getenv("CHANNEL_ID", "")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# Multiple bot tokens for parallel operations (comma-separated)
UPLOAD_BOT_TOKENS_STR = os.getenv("UPLOAD_BOT_TOKENS", "")
UPLOAD_BOT_TOKENS = [t.strip() for t in UPLOAD_BOT_TOKENS_STR.split(",") if t.strip()] if UPLOAD_BOT_TOKENS_STR else []
_default_archive_dir = Path("/var/data/udemy_archives") if Path("/var/data").exists() else Path(tempfile.gettempdir()) / "udemy_archives"
ARCHIVE_WORK_DIR = Path(os.getenv("ARCHIVE_WORK_DIR", str(_default_archive_dir)))
ARCHIVE_FALLBACK_WORK_DIR = Path(os.getenv("ARCHIVE_FALLBACK_WORK_DIR", str(Path.cwd() / "archive_work")))
ARCHIVE_STUCK_AFTER_SECONDS = int(os.getenv("ARCHIVE_STUCK_AFTER_SECONDS", "900"))
ARCHIVE_MIN_FREE_GB = float(os.getenv("ARCHIVE_MIN_FREE_GB", "1.5"))
ALLOW_TMP_ARCHIVES = os.getenv("ALLOW_TMP_ARCHIVES", "0").strip().lower() in ("1", "true", "yes", "on")

log = logging.getLogger(__name__)

COURSES_API = "https://cdn.real.discount/api/courses"
AUTO_ENROLL_INTERVAL = 120  # 2 minutes

# Global tracker for active download/upload tasks
active_tasks = {}
active_tasks_lock = Lock()

# Persistent /downloads status message per owner (so repeated calls reuse the
# same message instead of spamming new ones). Maps owner_id -> message_id.
status_msg_refs = {}
# Owners that currently have a live /downloads refresh loop running.
status_live_owners = set()

# Bot pool for parallel operations
bot_pool = []
bot_pool_lock = Lock()


def get_disk_usage(path="/"):
    """Get disk usage stats for the given path"""
    try:
        target = Path(path)
        # disk_usage needs an existing path on some platforms.
        while not target.exists() and target != target.parent:
            target = target.parent
        if PSUTIL_AVAILABLE:
            usage = psutil.disk_usage(str(target))
        else:
            usage = shutil.disk_usage(str(target))
            percent = (usage.used / usage.total * 100) if usage.total else 0
            usage = type("Usage", (), {
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": percent,
            })()
        return {
            "total_gb": usage.total / (1024**3),
            "used_gb": usage.used / (1024**3),
            "free_gb": usage.free / (1024**3),
            "percent": usage.percent
        }
    except Exception:
        return None


def _is_tmp_path(path: Path) -> bool:
    """Return True when path is under the OS temporary directory."""
    try:
        tmp = Path(tempfile.gettempdir()).resolve()
        return tmp == path.resolve() or tmp in path.resolve().parents
    except Exception:
        return False


def ensure_archive_storage_ready(path: Path) -> tuple[bool, str]:
    """Preflight archive storage to avoid Render /tmp eviction crashes."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"Cannot create archive work directory `{path}`: {e}"

    disk = get_disk_usage(str(path))
    if not disk:
        return True, ""

    if disk["free_gb"] < ARCHIVE_MIN_FREE_GB:
        return False, (
            f"Not enough free disk for archive work. Free: {disk['free_gb']:.1f} GB; "
            f"required: {ARCHIVE_MIN_FREE_GB:.1f} GB. Clear storage or increase disk."
        )

    # Render's /tmp is often 2GB and evicts the whole instance when exceeded.
    # Block by default unless the owner explicitly opts in for small-test archives.
    if _is_tmp_path(path) and disk["total_gb"] <= 3 and not ALLOW_TMP_ARCHIVES:
        return False, (
            "Archive work directory is on small temporary storage (`/tmp`, about "
            f"{disk['total_gb']:.1f} GB). Render can evict the instance when this fills.\n\n"
            "Fix: add a Render persistent disk and set:\n"
            "`ARCHIVE_WORK_DIR=/var/data/udemy_archives`\n\n"
            "For small testing only, set `ALLOW_TMP_ARCHIVES=1`."
        )

    return True, ""


def resolve_archive_work_dir(owner_id: int, udemy_id: int, saved_work_dir: str | None = None) -> tuple[Path, bool, str]:
    """Return a writable archive work dir, trying fallback if primary is unusable.

    This handles the common Render case where ARCHIVE_WORK_DIR=/var/data/... is
    configured before a persistent disk is actually mounted, causing EACCES.
    """
    candidates = []
    if saved_work_dir:
        candidates.append(Path(saved_work_dir))
    candidates.append(ARCHIVE_WORK_DIR / str(owner_id) / str(udemy_id))
    candidates.append(ARCHIVE_FALLBACK_WORK_DIR / str(owner_id) / str(udemy_id))

    seen = set()
    failures = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        ok, msg = ensure_archive_storage_ready(candidate)
        if ok:
            return candidate, True, ""
        failures.append(f"- `{candidate}`: {msg}")

    return candidates[-1], False, "No usable archive storage found:\n" + "\n".join(failures)


def get_cpu_usage():
    """Get CPU + memory usage stats. Non-blocking (interval=None uses cached value)."""
    if not PSUTIL_AVAILABLE:
        return None
    try:
        # interval=None is non-blocking (returns since-last-call value) → low overhead
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        return {
            "cpu_percent": cpu_pct,
            "cores": psutil.cpu_count(logical=True) or 1,
            "mem_percent": mem.percent,
            "mem_used_gb": mem.used / (1024**3),
            "mem_total_gb": mem.total / (1024**3),
        }
    except Exception:
        return None


def _session_get_retry(session, url, retries=3, timeout=20, **kwargs):
    """GET with simple retry/backoff for transient Udemy API failures.

    Returns the Response (possibly non-200) or None if all attempts errored.
    """
    import time as _t
    last_exc = None
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=timeout, **kwargs)
            # Retry only on transient server-side / rate-limit statuses
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                _t.sleep(1.5 * (attempt + 1))
                continue
            return resp
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                _t.sleep(1.5 * (attempt + 1))
    if last_exc:
        log.warning(f"GET failed after {retries} attempts: {url} ({last_exc})")
    return None


def format_bytes(bytes_val):
    """Format bytes to human-readable string"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PB"


async def init_bot_pool():
    """Initialize bot pool from UPLOAD_BOT_TOKENS"""
    global bot_pool
    # Prime CPU meter so the first get_cpu_usage() call returns a real value.
    if PSUTIL_AVAILABLE:
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
    with bot_pool_lock:
        bot_pool.clear()
        for token in UPLOAD_BOT_TOKENS:
            try:
                bot = Bot(token=token)
                # Test bot validity
                await bot.get_me()
                bot_pool.append(bot)
                log.info(f"Bot pool: Added bot (token ending ...{token[-8:]})")
            except Exception as e:
                log.error(f"Failed to add bot to pool: {e}")
        
        log.info(f"Bot pool initialized with {len(bot_pool)} bot(s)")
        return len(bot_pool)


def split_list_into_batches(items, num_batches):
    """Split a list into roughly equal batches"""
    if num_batches <= 0:
        return [items]
    if num_batches >= len(items):
        return [[item] for item in items]
    
    batch_size = len(items) // num_batches
    remainder = len(items) % num_batches
    
    batches = []
    start = 0
    for i in range(num_batches):
        # Add 1 extra item to first 'remainder' batches
        size = batch_size + (1 if i < remainder else 0)
        batches.append(items[start:start + size])
        start += size
    
    return batches


# ─── Premium Management Commands (Owner Only) ─────────────────────────────────

async def cmd_grant_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grant premium access to a user (owner only)"""
    if not update.effective_user or not update.effective_message:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return
    
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/grant_premium <user_id>`\nExample: `/grant_premium 123456789`",
            parse_mode="Markdown"
        )
        return
    
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid user ID. Must be a number.")
        return
    
    if grant_premium(target_id, update.effective_user.id):
        await update.effective_message.reply_text(
            f"✅ Premium access granted to user `{target_id}`",
            parse_mode="Markdown"
        )
    else:
        await update.effective_message.reply_text("❌ Failed to grant premium.")


async def cmd_revoke_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke premium access from a user (owner only)"""
    if not update.effective_user or not update.effective_message:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return
    
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/revoke_premium <user_id>`\nExample: `/revoke_premium 123456789`",
            parse_mode="Markdown"
        )
        return
    
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid user ID.")
        return
    
    if revoke_premium(target_id):
        await update.effective_message.reply_text(
            f"✅ Premium revoked from user `{target_id}`",
            parse_mode="Markdown"
        )
    else:
        await update.effective_message.reply_text("❌ Failed to revoke premium.")


async def cmd_list_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all premium users (owner only)"""
    if not update.effective_user or not update.effective_message:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return
    
    users = get_all_premium_users()
    if not users:
        await update.effective_message.reply_text("No premium users.")
        return
    
    lines = ["👑 **Premium Users:**\n"]
    for u in users:
        lines.append(f"• `{u['user_id']}` (granted: {u['granted_at'][:10]})")
    
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle channel posting for free courses (owner only)"""
    if not update.effective_user or not update.effective_message:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return
    
    current = is_channel_posting_enabled()
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔴 Turn OFF" if current else "🟢 Turn ON",
            callback_data="toggle_channel_post"
        ),
    ]])
    
    status = "🟢 **ON**" if current else "🔴 **OFF**"
    channel_info = f"Channel: `{CHANNEL_ID}`" if CHANNEL_ID else "⚠️ CHANNEL_ID not set in .env"
    
    await update.effective_message.reply_text(
        f"📢 **Channel Course Posting**\n\n"
        f"Status: {status}\n"
        f"{channel_info}\n\n"
        f"When ON, free Udemy courses are automatically posted to the channel.\n"
        f"When OFF, course posting to channel is paused.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ─── Owner: Search enrolled courses across all linked accounts & archive ─────

async def cmd_search_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: Search courses the linked Udemy accounts are enrolled in."""
    if not update.effective_user or not update.effective_message:
        return

    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/search_courses <keywords>`\n\n"
            "Searches within the courses your linked Udemy accounts are already enrolled in.\n"
            "Example: `/search_courses machine learning`",
            parse_mode="Markdown"
        )
        return

    query = " ".join(context.args).strip()
    if len(query) < 2:
        await update.effective_message.reply_text("Please provide at least 2 characters to search.")
        return

    msg = await update.effective_message.reply_text(f"🔍 Searching enrolled courses for “{query}” across your accounts...")

    accounts = get_user_accounts(user_id)
    if not accounts:
        await msg.edit_text("No Udemy accounts linked. Use `/enroll_setup` first.")
        return

    all_results = []
    seen_ids = set()
    # Search each account's library (page 1 is usually the most relevant)
    for acc in accounts:
        try:
            enroller = UdemyAutoEnroller(acc["access_token"], acc.get("client_id"))
            res = await asyncio.to_thread(enroller.search_enrolled_courses, query, 1, 20)
            for item in res.get("results", []):
                uid = item.get("id")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    item["source_account_id"] = acc["id"]  # mongo account id for preferring cookies later
                    item["source_account_name"] = acc.get("name", "Account")
                    all_results.append(item)
        except Exception as e:
            log.warning(f"Search failed on account {acc.get('name')}: {e}")

    if not all_results:
        await msg.edit_text(f"😕 No enrolled courses matched “{query}” in any of your accounts.")
        return

    # Store for pagination / selection in this chat
    context.user_data["dl_search"] = {
        "query": query,
        "page": 1,
        "results": all_results[:30]  # cap
    }

    await _send_search_results(msg, context, all_results[:10], query, 1)


async def cmd_download_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: Show current archive/download queue."""
    if not update.effective_user or not update.effective_message:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only.")
        return
    queue = get_owner_download_queue(update.effective_user.id)
    await update.effective_message.reply_text(
        "Use the buttons below or search with /search_courses.",
        parse_mode="Markdown"
    )
    await _show_download_queue(update.effective_message, queue)


def _build_status_text(remaining: int | None = None) -> str:
    """Build the active-tasks status text (same layout as live archive progress)."""
    with active_tasks_lock:
        tasks = dict(active_tasks)  # Copy to avoid lock issues

    if not tasks:
        msg = "📊 **Active Tasks**\n\n✅ No active downloads or uploads."
    else:
        msg = f"📊 **Active Tasks** ({len(tasks)})\n\n"
        for task_id, task_info in tasks.items():
            course = task_info.get("course", "Unknown")
            progress = task_info.get("progress", 0)
            speed = task_info.get("speed", "N/A")
            eta = task_info.get("eta", "N/A")
            stage = task_info.get("stage", "")
            bots = task_info.get("bots", {})

            bar = "█" * (progress // 10) + "░" * (10 - progress // 10)

            msg += f"**{course[:35]}**\n"
            msg += f"[{bar}] {progress}%\n"

            if bots and len(bots) > 1:
                msg += "\n🤖 **Bot Activity:**\n"
                for bot_num in sorted(bots.keys()):
                    bot_info = bots[bot_num]
                    bot_status_emoji = "⏳" if bot_info.get("status") == "downloading" else "✅"
                    lecture = bot_info.get("current_lecture", "Idle")[:25]
                    bot_pct = bot_info.get("progress", 0)
                    msg += f"Bot {bot_num+1}: {bot_status_emoji} {bot_pct}% - `{lecture}`\n"

            if stage and not bots:
                msg += f"{stage[:60]}\n"
            if speed != "N/A":
                msg += f"⚡ {speed}"
            if eta != "N/A":
                msg += f" | ⏱ {eta}"
            msg += "\n\n"

    cpu = get_cpu_usage()
    if cpu:
        msg += f"🖥️ **CPU:** {cpu['cpu_percent']:.0f}% of {cpu['cores']} core(s)\n"
        msg += f"🧠 **RAM:** {cpu['mem_used_gb']:.1f}/{cpu['mem_total_gb']:.1f} GB ({cpu['mem_percent']:.0f}%)\n"

    disk = get_disk_usage()
    if disk:
        msg += f"💾 **Disk:** {disk['used_gb']:.1f}/{disk['total_gb']:.1f} GB ({disk['percent']:.0f}%) • Free {disk['free_gb']:.1f} GB\n"

    if remaining is not None and remaining > 0:
        msg += f"\n_Live view • Refreshing... ({remaining}s remaining)_"
    return msg[:4000]


async def cmd_downloads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: View active download/upload tasks with system stats - LIVE updates.

    Reuses ONE persistent status message per owner (edits it in place) instead of
    sending a new message every time, so repeated /downloads calls never spam.
    """
    if not update.effective_user or not update.effective_message:
        return
    owner_id = update.effective_user.id
    if not is_owner(owner_id):
        await update.effective_message.reply_text("⛔ Owner only.")
        return

    import time

    # Try to reuse an existing status message; otherwise create a fresh one.
    existing_id = status_msg_refs.get(owner_id)
    status_msg = None
    if existing_id:
        try:
            status_msg = await context.bot.edit_message_text(
                chat_id=owner_id,
                message_id=existing_id,
                text=_build_status_text(remaining=60),
                parse_mode="Markdown",
            )
        except Exception:
            status_msg = None  # Old message gone/edit failed → create a new one

    if status_msg is None:
        status_msg = await update.effective_message.reply_text(
            _build_status_text(remaining=60), parse_mode="Markdown"
        )
        status_msg_refs[owner_id] = status_msg.message_id

    # If a live refresh loop is already running for this owner, don't start a
    # second one (that would double-edit / spam). The existing loop already
    # reflects the latest state.
    if owner_id in status_live_owners:
        return
    status_live_owners.add(owner_id)

    try:
        start_time = time.time()
        while time.time() - start_time < 60:
            with active_tasks_lock:
                has_tasks = bool(active_tasks)

            remaining = 60 - int(time.time() - start_time)
            try:
                await context.bot.edit_message_text(
                    chat_id=owner_id,
                    message_id=status_msg_refs[owner_id],
                    text=_build_status_text(remaining=remaining if has_tasks else None),
                    parse_mode="Markdown",
                )
            except Exception:
                pass  # Not modified / rate limited

            if not has_tasks:
                break
            await asyncio.sleep(3)
    finally:
        status_live_owners.discard(owner_id)


async def _send_search_results(msg, context, results, query, page):
    """Render a page of search results with Select buttons."""
    lines = [f"🔍 **Search results for “{query}”** (page {page})\n"]
    keyboard = []

    for idx, r in enumerate(results):
        title = (r.get("title") or "Untitled")[:55]
        headline = (r.get("headline") or "")[:70]
        paid = "💰 Paid" if r.get("is_paid") else "🆓 Free"
        rating = f"⭐ {r.get('avg_rating'):.1f}" if r.get("avg_rating") else ""
        src = r.get("source_account_name", "")
        lines.append(f"**{title}**\n{headline}\n{paid} {rating} · via {src}\n")

        udemy_id = r.get("id")
        keyboard.append([
            InlineKeyboardButton(f"📥 Select “{title[:30]}”", callback_data=f"dl_select_{udemy_id}"),
        ])

    # Pagination (simple, we have the full list in context for now)
    total = len(context.user_data.get("dl_search", {}).get("results", []))
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data="dl_prev"))
    if (page * 10) < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data="dl_next"))
    if nav:
        keyboard.append(nav)

    keyboard.append([
        InlineKeyboardButton("📋 View Queue", callback_data="dl_queue"),
        InlineKeyboardButton("🗑️ Clear Queue", callback_data="dl_clear"),
    ])

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3750] + "\n... (truncated)"

    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def _show_download_queue(query_or_msg, queue: list):
    """Display the current download queue with action buttons."""
    if not queue:
        text = "📋 **Download / Archive Queue**\n\nQueue is empty.\nUse `/search_courses <query>` to find courses from your accounts."
        keyboard = []
    else:
        lines = ["📋 **Download / Archive Queue**\n"]
        keyboard = []
        for item in queue[:12]:  # show up to 12
            title = item["title"][:50]
            status = item.get("job_status")
            if status == "posted":
                badge = f"✅ Posted ({item.get('zip_size_mb', 0)} MB)"
            elif status == "zip_ready":
                badge = "📦 ZIP ready - resume upload"
            elif status == "upload_failed":
                badge = "⚠️ Upload failed - resume upload"
            elif status == "failed":
                badge = "🔁 Failed - resume download"
            elif status == "running":
                badge = f"⏳ Running {item.get('job_progress', 0)}%"
            else:
                badge = "🆕 New"
            lines.append(f"• **{title}**\n  `{badge}`")
            uid = item["udemy_course_id"]
            keyboard.append([
                InlineKeyboardButton(f"🚀 Archive/Resume “{title[:20]}”", callback_data=f"dl_start_{uid}"),
                InlineKeyboardButton("🗑️", callback_data=f"dl_remove_{uid}"),
            ])
        if len(queue) > 12:
            lines.append(f"... +{len(queue)-12} more")
        keyboard.append([
            InlineKeyboardButton("🚀 Archive ALL", callback_data="dl_start_all"),
            InlineKeyboardButton("🗑️ Clear All", callback_data="dl_clear"),
        ])
        text = "\n".join(lines)

    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    try:
        if hasattr(query_or_msg, "edit_message_text"):
            await query_or_msg.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        else:
            await query_or_msg.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        pass


def _build_udemy_cookies_file(work_dir, access_token, client_id):
    """Write a Netscape cookies.txt that yt-dlp understands for Udemy auth."""
    exp = str(int(__import__("datetime").datetime.utcnow().timestamp()) + 86400 * 30)
    lines = [
        "# Netscape HTTP Cookie File",
        f".udemy.com\tTRUE\t/\tTRUE\t{exp}\taccess_token\t{access_token}",
        f".udemy.com\tTRUE\t/\tTRUE\t{exp}\tclient_id\t{client_id}",
    ]
    cookie_file = work_dir / "udemy_cookies.txt"
    cookie_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cookie_file


def _get_course_id_from_url(course_url: str) -> str | None:
    """Extract Udemy course slug or numeric ID from a course URL."""
    import re
    # /course/draft/1234567/ or /course/slug-name/
    m = re.search(r"/course/(?:draft/)?([^/]+)", course_url)
    return m.group(1) if m else None


def _make_udemy_session(access_token, client_id):
    """Build a requests Session with Udemy mobile auth headers/cookies."""
    s = requests.Session()
    s.cookies.update({"access_token": access_token, "client_id": client_id or DEFAULT_CLIENT_ID})
    s.headers.update({
        "User-Agent": "okhttp/4.10.0 UdemyAndroid 9.7.0(515) (phone)",
        "Accept": "application/json",
        "Referer": "https://www.udemy.com/",
    })
    return s


def _find_enrolled_account(accounts, course_id, preferred_acc_id=None):
    """Return the account that can actually access (is enrolled in) course_id.

    Prevents per-lecture 404s caused by using a token for an account that isn't
    subscribed to the course. Prefers preferred_acc_id, then tries the rest.
    Returns (account_dict_or_None, token_expired_any: bool).
    """
    if not course_id:
        return None, False
    ordered = sorted(
        accounts,
        key=lambda a: 0 if (preferred_acc_id and a["id"] == preferred_acc_id) else 1,
    )
    saw_expired = False
    for acc in ordered:
        try:
            s = _make_udemy_session(acc["access_token"], acc.get("client_id"))
            r = _session_get_retry(
                s,
                f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/?fields[course]=id",
                retries=2, timeout=15,
            )
            if r is None:
                continue
            if r.status_code == 200:
                return acc, saw_expired
            if r.status_code in (401, 403):
                saw_expired = True  # token likely expired/invalid for this account
        except Exception:
            continue
    return None, saw_expired


def _resolve_course(session, course_url, course_id_hint=None):
    """Resolve (course_id, course_title) robustly.

    Prefers the known numeric course_id (course_id_hint) so we don't fail the
    whole course just because a slug lookup returns 404/403. Falls back to slug
    lookup, then to searching the user's subscribed courses.
    Returns (course_id, title) — course_id may be None only if everything fails.
    """
    # 1) Use the numeric hint directly if we have one (most reliable).
    if course_id_hint and str(course_id_hint).isdigit():
        title = None
        r = _session_get_retry(
            session,
            f"https://www.udemy.com/api-2.0/courses/{course_id_hint}/?fields[course]=title",
        )
        if r is not None and r.status_code == 200:
            try:
                title = r.json().get("title")
            except Exception:
                title = None
        return int(course_id_hint), (title or f"Course-{course_id_hint}")

    # 2) Try slug/numeric extracted from the URL.
    slug_or_id = _get_course_id_from_url(course_url)
    if slug_or_id:
        r = _session_get_retry(
            session,
            f"https://www.udemy.com/api-2.0/courses/{slug_or_id}/?fields[course]=id,title",
        )
        if r is not None and r.status_code == 200:
            try:
                data = r.json()
                if data.get("id"):
                    return data["id"], data.get("title", str(slug_or_id))
            except Exception:
                pass
        # 2b) If slug is purely numeric, trust it even if metadata fetch failed.
        if str(slug_or_id).isdigit():
            return int(slug_or_id), f"Course-{slug_or_id}"

        # 3) Last resort: search the user's subscribed courses by the slug words.
        try:
            query = str(slug_or_id).replace("-", " ")
            rs = _session_get_retry(
                session,
                f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/"
                f"?search={requests.utils.quote(query)}&page_size=1&fields[course]=id,title",
            )
            if rs is not None and rs.status_code == 200:
                results = rs.json().get("results") or []
                if results and results[0].get("id"):
                    return results[0]["id"], results[0].get("title", query)
        except Exception:
            pass

    return None, None


def _get_best_m3u8(master_m3u8_content: str, prefer_height: int = 720) -> str | None:
    """Parse an HLS master playlist and pick the best quality variant URL."""
    import re
    lines = master_m3u8_content.strip().split("\n")
    variants = []  # (bandwidth, resolution_height, url)
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
            url = lines[i + 1].strip()
            bw = int(re.search(r"BANDWIDTH=(\d+)", line).group(1)) if re.search(r"BANDWIDTH=(\d+)", line) else 0
            h = int(re.search(r"RESOLUTION=\d+x(\d+)", line).group(1)) if re.search(r"RESOLUTION=\d+x(\d+)", line) else 0
            variants.append((bw, h, url))
    if not variants:
        return None
    # Prefer the target height; if not found, pick highest
    preferred = [v for v in variants if v[1] == prefer_height]
    chosen = max(preferred or variants, key=lambda v: v[0])
    return chosen[2]


def _download_course_via_api(course_url: str, out_dir: Path, access_token: str, client_id: str, progress_callback=None, lecture_subset=None, batch_num=0, course_id_hint=None):
    """
    Download all or a subset of lectures of a Udemy course by:
    1. Using the Udemy subscriber-curriculum-items API to enumerate all chapters/lectures
    2. For each lecture, fetching the HLS stream URL via the lecture detail API
    3. Downloading each HLS stream with yt-dlp's generic HLS downloader (bypassing the
       Udemy extractor which 403s on the course webpage)

    Returns (ok: bool, errors: list[str]).
    Output structure: Chapter folder / lecture-index - title.mp4
    """
    import yt_dlp, re

    MOBILE_UA = "okhttp/4.10.0 UdemyAndroid 9.7.0(515) (phone)"
    session = requests.Session()
    session.cookies.update({"access_token": access_token, "client_id": client_id})
    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Accept": "application/json",
        "Referer": "https://www.udemy.com/",
    })

    # Resolve course ID robustly (prefer the known numeric id so we don't fail
    # the whole course on a flaky slug lookup).
    course_id, course_title = _resolve_course(session, course_url, course_id_hint)
    if not course_id:
        return False, ["Cannot resolve course (check URL / token, or course may be removed)"]

    if progress_callback:
        progress_callback(2, f"📚 Found course: **{course_title}**\nFetching curriculum...")

    # Get full curriculum
    all_items = []
    next_page = (
        f"https://www.udemy.com/api-2.0/courses/{course_id}/subscriber-curriculum-items/"
        f"?page_size=100"
        f"&fields[lecture]=id,title,object_index,asset"
        f"&fields[chapter]=id,title,object_index"
        f"&fields[asset]=id,asset_type,filename,media_sources,is_downloadable"
    )
    while next_page:
        r = _session_get_retry(session, next_page)
        if r is None or r.status_code != 200:
            break
        data = r.json()
        all_items.extend(data.get("results", []))
        next_page = data.get("next")

    chapters = {i["id"]: i for i in all_items if i.get("_class") == "chapter"}
    lectures = [i for i in all_items if i.get("_class") == "lecture"]

    if not lectures:
        return False, ["No lectures found in curriculum"]

    # Filter lectures if subset provided (for parallel downloads)
    if lecture_subset is not None:
        lecture_ids_to_process = {lec["id"] for lec in lecture_subset}
    else:
        lecture_ids_to_process = None  # Process all lectures

    lectures_to_process = lecture_subset if lecture_subset is not None else lectures
    
    batch_prefix = f"[Batch {batch_num + 1}] " if batch_num > 0 else ""
    if progress_callback:
        progress_callback(4, f"{batch_prefix}📋 {len(lectures_to_process)} lectures assigned. Downloading...")

    errors = []
    lecture_counter = [0]
    total_lectures = len(lectures_to_process)

    # Find chapter for each lecture (by order in curriculum)
    chapter_idx = [0]
    chapter_num = [0]
    current_chapter = [{"object_index": 0, "title": "Uncategorized"}]

    for item in all_items:
        if item.get("_class") == "chapter":
            current_chapter[0] = item
            chapter_num[0] += 1

    # We'll iterate all_items in order to track chapter context
    chap_num_counter = [0]
    current_chap = [{"title": "Uncategorized", "object_index": 0}]

    for item in all_items:
        if item.get("_class") == "chapter":
            chap_num_counter[0] += 1
            current_chap[0] = item
            continue
        if item.get("_class") != "lecture":
            continue

        lec = item
        lid = lec["id"]
        
        # Skip if not in subset (for parallel downloads)
        if lecture_ids_to_process is not None and lid not in lecture_ids_to_process:
            continue
        
        ltitle = lec.get("title", f"Lecture {lid}")
        lindex = lec.get("object_index", lecture_counter[0] + 1)
        chap = current_chap[0]
        chap_num = chap_num_counter[0]
        chap_title = chap.get("title", "Uncategorized")

        lecture_counter[0] += 1
        safe_chap = f"{chap_num:02d} - " + "".join(c if c.isalnum() or c in " -_." else "_" for c in chap_title)[:50]
        safe_lec = f"{lindex:03d} - " + "".join(c if c.isalnum() or c in " -_." else "_" for c in ltitle)[:55]
        lec_dir = out_dir / safe_chap
        lec_dir.mkdir(parents=True, exist_ok=True)
        out_path = lec_dir / f"{safe_lec}.mp4"

        if out_path.exists():
            continue  # already downloaded

        # Get lecture detail including supplementary assets (attachments/resources)
        qs = "?fields[lecture]=asset,supplementary_assets&fields[asset]=@all"
        detail_url = (
            f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/lectures/{lid}/{qs}"
        )
        try:
            r_lec = _session_get_retry(session, detail_url, retries=3, timeout=20)
            # Fallback: course-scoped lecture endpoint (helps when the subscribed-courses
            # path 404s for some lectures/courses).
            if r_lec is None or r_lec.status_code == 404:
                alt_url = f"https://www.udemy.com/api-2.0/courses/{course_id}/lectures/{lid}/{qs}"
                alt = _session_get_retry(session, alt_url, retries=2, timeout=20)
                if alt is not None and alt.status_code == 200:
                    r_lec = alt

            if r_lec is None or r_lec.status_code != 200:
                code = r_lec.status_code if r_lec is not None else "timeout"
                if code in (401, 403, 404):
                    errors.append(f"Lecture not accessible ({code}): {ltitle}")
                else:
                    errors.append(f"Lecture {lid} API error {code}")
                continue

            lecture_json = r_lec.json()
            asset = lecture_json.get("asset") or {}
            atype = asset.get("asset_type", "")
            ms = asset.get("media_sources") or []
            download_urls = asset.get("download_urls") or {}
            is_drm = bool(asset.get("asset_is_drmed") or asset.get("course_is_drmed"))

            # Download ALL supplementary assets (PDFs, ZIPs, code files, docs, etc.)
            # for every lecture — including DRM-protected video lectures.
            # This is legal as these are separate attached files.
            for sidx, supp in enumerate(lecture_json.get("supplementary_assets") or [], 1):
                try:
                    supp_title = supp.get("title", f"resource_{sidx}")
                    supp_filename = supp.get("filename", supp_title)
                    s_urls = supp.get("download_urls") or {}
                    s_file_url = None
                    if isinstance(s_urls, dict):
                        for _k in ("File", "file", "SourceCode", "video", "Video"):
                            _v = s_urls.get(_k)
                            if isinstance(_v, list) and _v:
                                s_file_url = _v[0].get("file") if isinstance(_v[0], dict) else _v[0]
                            elif isinstance(_v, dict) and _v.get("file"):
                                s_file_url = _v.get("file")
                            if s_file_url:
                                break
                    if not s_file_url and isinstance(s_urls, list) and s_urls:
                        s_file_url = s_urls[0].get("file") if isinstance(s_urls[0], dict) else s_urls[0]
                    if not s_file_url:
                        continue
                    safe_s = "".join(c if c.isalnum() or c in " -_." else "_" for c in supp_filename)[:90]
                    if not any(safe_s.lower().endswith(ext) for ext in (".pdf", ".zip", ".rar", ".7z", ".txt", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".html", ".htm", ".epub", ".py", ".js", ".json", ".csv")):
                        guess = str(s_file_url).split("?")[0].rsplit(".", 1)[-1]
                        if 1 < len(guess) <= 6:
                            safe_s += f".{guess}"
                    s_path = lec_dir / f"{safe_lec}_res{sidx}_{safe_s}"
                    if s_path.exists():
                        continue
                    rs = session.get(s_file_url, stream=True, timeout=90)
                    if rs.status_code == 200:
                        with open(s_path, "wb") as ff:
                            for ch in rs.iter_content(8192):
                                ff.write(ch)
                except Exception as _se:
                    errors.append(f"Resource error for {ltitle} #{sidx}: {_se}")

            if atype == "Article":
                # Save article text as .html
                body = asset.get("body") or lec.get("description") or ""
                html_path = lec_dir / f"{safe_lec}.html"
                if body:
                    html_path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
                continue

            if atype in ("SourceCode", "File"):
                # Save external URL as reference
                ext_url = asset.get("external_url") or asset.get("source_url") or ""
                if ext_url:
                    ref_path = lec_dir / f"{safe_lec}_url.txt"
                    ref_path.write_text(ext_url, encoding="utf-8")
                continue

            # Prefer official "Download" files when Udemy provides them (authorized by the course settings)
            # This can succeed for some lectures even if the streaming asset is flagged DRM.
            direct_got = False
            if download_urls:
                d_url = None
                for key in ("Video", "video", "mp4", "File", "SourceCode"):
                    val = download_urls.get(key)
                    if isinstance(val, list) and val:
                        it = val[0]
                        d_url = it.get("file") if isinstance(it, dict) else it
                    elif isinstance(val, dict) and val.get("file"):
                        d_url = val["file"]
                    if d_url:
                        break
                if d_url:
                    try:
                        rd = session.get(d_url, stream=True, timeout=120)
                        if rd.status_code == 200:
                            dext = str(d_url).split("?")[0].rsplit(".", 1)[-1] or "mp4"
                            if len(dext) > 6 or "/" in dext:
                                dext = "mp4"
                            dp = lec_dir / f"{safe_lec}.{dext}"
                            if not dp.exists():
                                with open(dp, "wb") as ff:
                                    for ch in rd.iter_content(8192):
                                        ff.write(ch)
                            direct_got = True
                            log.info(f"Used official download link for: {ltitle}")
                    except Exception as _de:
                        log.warning(f"Direct download attempt failed for {ltitle}: {_de}")

            if not direct_got:
                if not ms:
                    errors.append(f"No media for: {ltitle}")
                    continue

                if is_drm:
                    errors.append(f"DRM protected (skipped): {ltitle}")
                    continue

            # Try to get M3U8 (HLS) source first
            m3u8_master_url = next(
                (m["src"] for m in ms if "mpegURL" in m.get("type", "") or m.get("src","").endswith(".m3u8")),
                None
            )
            
            download_url = None
            use_generic_extractor = True
            
            if m3u8_master_url:
                # Fetch master M3U8 to find quality variants
                try:
                    r_m3u8 = session.get(m3u8_master_url, timeout=20)
                    if r_m3u8.status_code == 200:
                        # Pick 720p variant (or best available)
                        variant_url = _get_best_m3u8(r_m3u8.text, prefer_height=720)
                        download_url = variant_url if variant_url else m3u8_master_url
                except Exception as e:
                    log.warning(f"M3U8 fetch failed for {ltitle}: {e}")
            
            # Fallback: Try direct MP4/video sources if HLS not available or failed
            if not download_url:
                # Look for direct video files (mp4, webm, etc.) - prefer higher quality
                video_sources = [
                    m for m in ms 
                    if m.get("type") in ("video/mp4", "video/webm") or m.get("src","").endswith((".mp4", ".webm"))
                ]
                if video_sources:
                    # Sort by quality/label if available, or just pick the first one
                    video_sources.sort(key=lambda x: x.get("label", "0"), reverse=True)
                    download_url = video_sources[0]["src"]
                    use_generic_extractor = True  # yt-dlp can handle direct video links
                    log.info(f"Using direct MP4 fallback for: {ltitle}")
            
            if not download_url:
                errors.append(f"No HLS or MP4 source for: {ltitle}")
                continue

            # Compute progress
            pct_base = int((lecture_counter[0] / total_lectures) * 85)

            last_pct_lec = [0]
            def _hook(d, _chap=safe_chap, _lec=safe_lec, _pb=pct_base):
                if progress_callback is None:
                    return
                status = d.get("status", "")
                if status == "downloading":
                    try:
                        p = float((d.get("_percent_str") or "0").replace("%","").strip())
                    except Exception:
                        p = last_pct_lec[0]
                    last_pct_lec[0] = p
                    speed = d.get("_speed_str") or ""
                    eta   = d.get("_eta_str") or ""
                    dl    = d.get("_downloaded_bytes_str") or ""
                    total = d.get("_total_bytes_str") or d.get("_total_bytes_estimate_str") or ""
                    stage = (
                        f"📚 Lecture {lecture_counter[0]}/{total_lectures}\n"
                        f"📄 `{_lec[:45]}`"
                    )
                    if dl and total:
                        stage += f"\n📦 {dl} / {total}"
                    if speed:
                        stage += f"\n⚡ {speed}"
                    if eta:
                        stage += f"\n⏱ ETA {eta}"
                    progress_callback(_pb + int(p * 0.12), stage)
                elif status == "finished":
                    progress_callback(_pb + 12, f"🔀 Merging: `{_lec[:45]}`")

            ydl_opts = {
                "outtmpl": str(out_path),
                "cookiefile": None,  # not needed - segments don't require auth
                "http_headers": {
                    "User-Agent": MOBILE_UA,
                    "Referer": "https://www.udemy.com/",
                    "Cookie": f"access_token={access_token}; client_id={client_id}",
                },
                "format": "best",
                "merge_output_format": "mp4",
                "force_generic_extractor": use_generic_extractor,
                "ignoreerrors": True,
                "nooverwrites": True,
                "retries": 5,
                "fragment_retries": 10,
                # Scale fragment concurrency DOWN by the number of parallel workers so
                # total connections (and CPU) stay bounded (~8) instead of workers*8.
                "concurrent_fragment_downloads": max(2, 8 // max(1, len(bot_pool))),
                "http_chunk_size": 10485760,  # 10MB chunks for better speed
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [_hook],
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([download_url])
            # (supplementary assets already downloaded earlier for all lectures, DRM or not)

        except Exception as e:
            errors.append(f"Error on {ltitle}: {e}")

    return True, errors


async def _download_course_parallel(course_url: str, out_dir: Path, access_token: str, client_id: str, num_workers: int, progress_callback=None, course_id_hint=None):
    """
    Download course using multiple parallel workers (bots).
    Each worker downloads a subset of lectures simultaneously.
    Returns (ok: bool, errors: list[str]).
    """
    import yt_dlp, re, requests
    
    MOBILE_UA = "okhttp/4.10.0 UdemyAndroid 9.7.0(515) (phone)"
    session = requests.Session()
    session.cookies.update({"access_token": access_token, "client_id": client_id})
    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Accept": "application/json",
        "Referer": "https://www.udemy.com/",
    })

    # Resolve course robustly (prefer the numeric hint).
    course_id, course_title = _resolve_course(session, course_url, course_id_hint)
    if not course_id:
        return False, ["Cannot resolve course (check URL / token, or course may be removed)"]

    # Get full curriculum
    all_items = []
    next_page = (
        f"https://www.udemy.com/api-2.0/courses/{course_id}/subscriber-curriculum-items/"
        f"?page_size=100"
        f"&fields[lecture]=id,title,object_index,asset"
        f"&fields[chapter]=id,title,object_index"
        f"&fields[asset]=id,asset_type,filename,media_sources,is_downloadable"
    )
    while next_page:
        r = _session_get_retry(session, next_page)
        if r is None or r.status_code != 200:
            break
        data = r.json()
        all_items.extend(data.get("results", []))
        next_page = data.get("next")

    lectures = [i for i in all_items if i.get("_class") == "lecture"]
    
    if not lectures:
        return False, ["No lectures found in curriculum"]
    
    if progress_callback:
        progress_callback(2, f"📚 Found {len(lectures)} lectures. Splitting into {num_workers} batches...")
    
    # Split lectures into batches
    lecture_batches = split_list_into_batches(lectures, num_workers)
    
    # Create separate temp folders for each worker
    batch_dirs = []
    for i in range(num_workers):
        batch_dir = out_dir / f"_batch_{i}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_dirs.append(batch_dir)
    
    # Shared bot status tracking
    bot_status = {}
    for i in range(num_workers):
        bot_status[i] = {"progress": 0, "current_lecture": "Starting...", "status": "initializing"}
    
    # Run parallel downloads
    if progress_callback:
        progress_callback(5, f"🚀 Starting {num_workers} parallel downloads...", bot_status.copy())
    
    async def download_batch(batch_idx, batch_lectures, batch_dir):
        """Download a batch of lectures"""
        def batch_progress(pct, stage):
            # Extract lecture name from stage if possible
            lecture_name = "Processing..."
            if "📄 `" in stage:
                try:
                    lecture_name = stage.split("📄 `")[1].split("`")[0]
                except:
                    pass
            
            # Update bot status
            bot_status[batch_idx] = {
                "progress": int(pct * 0.85 / num_workers),  # Normalized to batch portion
                "current_lecture": lecture_name,
                "status": "downloading" if pct < 100 else "merging"
            }
            
            # Calculate overall progress
            overall_pct = 5 + sum(b["progress"] for b in bot_status.values())
            
            if progress_callback:
                progress_callback(overall_pct, stage, bot_status.copy())
        
        return await asyncio.to_thread(
            _download_course_via_api,
            course_url,
            batch_dir,
            access_token,
            client_id,
            batch_progress,
            batch_lectures,
            batch_idx,
            course_id,  # pass resolved numeric id so batches don't re-resolve/fail
        )
    
    # Execute all downloads in parallel
    results = await asyncio.gather(
        *[download_batch(i, batch_lectures, batch_dirs[i]) for i, batch_lectures in enumerate(lecture_batches)],
        return_exceptions=True
    )
    
    # Collect all errors
    all_errors = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            all_errors.append(f"Batch {i+1} failed: {str(result)}")
        elif isinstance(result, tuple):
            ok, errors = result
            if errors:
                all_errors.extend(errors)
    
    if progress_callback:
        progress_callback(85, f"🔄 Merging {num_workers} batches...")
    
    # Merge all batch folders into main out_dir.
    # Per-file try/except so one bad move never aborts the whole merge — we want
    # to keep every successfully downloaded file for the final ZIP.
    for batch_dir in batch_dirs:
        if batch_dir.exists():
            for item in batch_dir.rglob("*"):
                if item.is_file():
                    try:
                        rel_path = item.relative_to(batch_dir)
                        dest = out_dir / rel_path
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if dest.exists():
                            continue
                        shutil.move(str(item), str(dest))
                    except Exception as mv_err:
                        all_errors.append(f"Merge error for {item.name}: {mv_err}")
            # Remove batch dir
            shutil.rmtree(batch_dir, ignore_errors=True)
    
    if progress_callback:
        progress_callback(90, "✅ All batches merged successfully!")
    
    return True, all_errors


async def _start_course_archive(update, context, item, silent_start=False):
    """
    Background task:
    1. Download entire Udemy course via yt-dlp (cookie auth, chapter/lecture folder structure)
    2. ZIP the downloaded tree
    3. Split if > 2 GB (very rare)
    4. Upload via Pyrogram (~2 GB support) or Bot API fallback
    5. Progress messages sent to owner throughout
    """
    from user_enroller import OWNER_ID as _OWNER_ID
    owner_id = update.effective_user.id if update.effective_user else _OWNER_ID
    udemy_id = item["udemy_course_id"]
    title = item.get("title", f"Course-{udemy_id}")
    course_url = item.get("course_url", "")
    source_acc_id = item.get("source_account_id")
    task_id = f"{owner_id}_{udemy_id}"

    with active_tasks_lock:
        already_active = task_id in active_tasks
    if already_active:
        if not silent_start:
            try:
                await context.bot.send_message(
                    owner_id,
                    f"⏳ Archive is already running for “{title}”. Use /status to watch the live progress.",
                )
            except Exception:
                pass
        return

    job = await asyncio.to_thread(get_archive_job, owner_id, udemy_id)
    if job and job.get("status") == "posted":
        if not silent_start:
            try:
                await context.bot.send_message(
                    owner_id,
                    f"✅ “{title}” was already completed and posted to the channel.\n"
                    f"ZIP size: {job.get('zip_size_mb', 0)} MB • Parts: {job.get('part_count', 0)}",
                )
            except Exception:
                pass
        remove_from_download_queue(owner_id, udemy_id)
        return

    accounts = get_user_accounts(owner_id)
    chosen = None
    if source_acc_id:
        chosen = next((a for a in accounts if a["id"] == source_acc_id), None)
    if not chosen and accounts:
        chosen = accounts[0]

    if not chosen or not course_url:
        if not silent_start:
            try:
                await context.bot.send_message(owner_id, f"\u274c Cannot archive \u201c{title}\u201d: no linked account or missing URL.")
            except Exception:
                pass
        return

    # Pick the account that is ACTUALLY enrolled in this course, to avoid
    # per-lecture 404s when the wrong/duplicate account token is used.
    if accounts and str(udemy_id).isdigit():
        try:
            enrolled_acc, saw_expired = await asyncio.to_thread(
                _find_enrolled_account, accounts, int(udemy_id), source_acc_id
            )
            if enrolled_acc:
                chosen = enrolled_acc
            elif saw_expired and not silent_start:
                try:
                    await context.bot.send_message(
                        owner_id,
                        "⚠️ Your saved Udemy token(s) may have expired for this course. "
                        "If the archive comes back empty or with many 404s, refresh via /enroll_setup.",
                    )
                except Exception:
                    pass
        except Exception as _e:
            log.warning(f"Enrolled-account check failed for {title}: {_e}")

    safe_name = "".join(c if c.isalnum() or c in " -_." else "_" for c in title)[:60].strip("_").strip()
    work_dir, storage_ok, storage_msg = resolve_archive_work_dir(
        owner_id, udemy_id, job.get("work_dir") if job else None
    )
    if not storage_ok:
        await asyncio.to_thread(
            upsert_archive_job,
            owner_id,
            udemy_id,
            title=title,
            course_url=course_url,
            source_account_id=source_acc_id,
            status="failed",
            work_dir=str(work_dir),
            progress=0,
            stage="Storage limit - configure persistent disk",
            last_error=storage_msg[:500],
            last_heartbeat=datetime.utcnow(),
        )
        try:
            await context.bot.send_message(
                owner_id,
                f"⚠️ **Archive paused before download**\n\n"
                f"**{title}**\n\n"
                f"{storage_msg}\n\n"
                "Fix: add a Render Persistent Disk mounted at `/var/data`, or set a writable "
                "`ARCHIVE_WORK_DIR` / `ARCHIVE_FALLBACK_WORK_DIR`, then click Archive/Resume again.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return
    out_dir = work_dir / safe_name
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = work_dir / f"{safe_name}.zip"
    existing_zip = zip_path.exists() and zip_path.stat().st_size > 0

    await asyncio.to_thread(
        upsert_archive_job,
        owner_id,
        udemy_id,
        title=title,
        course_url=course_url,
        source_account_id=source_acc_id,
        status="running",
        work_dir=str(work_dir),
        out_dir=str(out_dir),
        zip_path=str(zip_path),
        progress=0,
        stage="Initializing",
        last_heartbeat=datetime.utcnow(),
    )

    # Register task in global tracker
    with active_tasks_lock:
        active_tasks[task_id] = {
            "course": title,
            "status": "initializing",
            "progress": 0,
            "speed": "N/A",
            "eta": "N/A",
            "stage": "Initializing...",
            "bots": {}  # Track per-bot status: {bot_num: {progress, current_lecture, status}}
        }

    progress_msg = None
    _last_edit = [0.0]
    _last_job_write = [0.0]
    progress_queue = asyncio.Queue()

    async def _send_progress(pct, stage, bot_info=None):
        nonlocal progress_msg
        import time
        now = time.time()
        if progress_msg and (now - _last_edit[0]) < 5:
            return
        _last_edit[0] = now
        bar = "\u2588" * (pct // 10) + "\u2591" * (10 - pct // 10)
        
        # Update global task tracker
        with active_tasks_lock:
            if task_id in active_tasks:
                active_tasks[task_id]["progress"] = pct
                active_tasks[task_id]["stage"] = stage
                active_tasks[task_id]["status"] = "downloading" if pct < 90 else "uploading"
                if bot_info:
                    active_tasks[task_id]["bots"] = bot_info
        if (now - _last_job_write[0]) >= 15:
            _last_job_write[0] = now
            asyncio.create_task(asyncio.to_thread(
                mark_archive_job_heartbeat, owner_id, udemy_id, stage, pct
            ))
        
        # Add CPU + disk usage to stage info
        sys_info = ""
        cpu = get_cpu_usage()
        if cpu:
            sys_info += f"\n🖥️ CPU {cpu['cpu_percent']:.0f}% • 🧠 RAM {cpu['mem_percent']:.0f}%"
        disk = get_disk_usage(str(work_dir))
        if disk:
            sys_info += f"\n💾 Free: {disk['free_gb']:.1f} GB"
        
        txt = f"\U0001f4e5 **Downloading course**\n\n**{title}**\n\n[{bar}] {pct}%\n\n{stage}{sys_info}"
        
        # Add bot status if parallel downloads
        if bot_info and len(bot_info) > 1:
            txt += "\n\n**🤖 Bot Activity:**"
            for bot_num in sorted(bot_info.keys()):
                info = bot_info[bot_num]
                status_emoji = "⏳" if info.get("status") == "downloading" else "✅"
                lecture = info.get("current_lecture", "Idle")[:30]
                bot_pct = info.get("progress", 0)
                txt += f"\nBot {bot_num+1}: {status_emoji} {bot_pct}% - `{lecture}`"
        
        try:
            if progress_msg:
                await progress_msg.edit_text(txt, parse_mode="Markdown")
            else:
                progress_msg = await context.bot.send_message(owner_id, txt, parse_mode="Markdown")
        except Exception:
            pass

    async def _progress_monitor():
        """Background task that monitors the progress queue and sends updates"""
        while True:
            try:
                data = await progress_queue.get()
                if data is None or (isinstance(data, tuple) and len(data) >= 2 and data[0] is None):  # Sentinel value
                    break
                
                # Handle both (pct, stage) and (pct, stage, bot_info) formats
                if isinstance(data, tuple):
                    if len(data) == 3:
                        pct, stage, bot_info = data
                        await _send_progress(pct, stage, bot_info)
                    elif len(data) == 2:
                        pct, stage = data
                        await _send_progress(pct, stage)
            except Exception:
                pass

    def _sync_progress(pct, stage, bot_info=None):
        """Thread-safe progress callback - puts updates into queue"""
        try:
            if bot_info is not None:
                progress_queue.put_nowait((pct, stage, bot_info))
            else:
                progress_queue.put_nowait((pct, stage))
        except Exception:
            pass

    # Start the progress monitor task
    monitor_task = asyncio.create_task(_progress_monitor())
    archive_completed = False

    try:
        if not silent_start:
            await _send_progress(1, f"\U0001f510 Preparing cookies for: **{chosen.get('name')}**")

        if existing_zip:
            await _send_progress(80, "\U0001f501 Found existing ZIP from previous run. Resuming from upload...")
            ok, errs = True, []
        else:
            await _send_progress(3, "\U0001f680 Fetching curriculum and downloading lectures...")

            # Use parallel downloads if bot pool available, otherwise single-threaded.
            # IMPORTANT: even if the download phase raises (DRM, network, merge error, etc.),
            # we must NOT abort — we still want to ZIP and share whatever was downloaded.
            num_workers = len(bot_pool) if bot_pool else 0
            ok, errs = False, []

            try:
                if num_workers > 1:
                    log.info(f"Using parallel download with {num_workers} workers")
                    ok, errs = await _download_course_parallel(
                        course_url,
                        out_dir,
                        chosen["access_token"],
                        chosen.get("client_id") or DEFAULT_CLIENT_ID,
                        num_workers,
                        _sync_progress,
                        udemy_id,  # known numeric course id → robust resolution
                    )
                else:
                    log.info("Using single-threaded download (no bot pool configured)")
                    ok, errs = await asyncio.to_thread(
                        _download_course_via_api,
                        course_url,
                        out_dir,
                        chosen["access_token"],
                        chosen.get("client_id") or DEFAULT_CLIENT_ID,
                        _sync_progress,
                        None,        # lecture_subset
                        0,           # batch_num
                        udemy_id,    # course_id_hint
                    )
            except Exception as dl_err:
                # Download phase crashed partway — keep going and archive whatever we have.
                log.exception(f"Download phase error for {title}: {dl_err}")
                errs = (errs or []) + [f"Download interrupted: {dl_err}"]

        # Safety net: if parallel batch dirs were left behind (e.g. merge crashed),
        # flatten them into out_dir so nothing downloaded is lost.
        try:
            for leftover in list(out_dir.glob("_batch_*")):
                if leftover.is_dir():
                    for item in leftover.rglob("*"):
                        if item.is_file():
                            rel_path = item.relative_to(leftover)
                            dest = out_dir / rel_path
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if not dest.exists():
                                shutil.move(str(item), str(dest))
                    shutil.rmtree(leftover, ignore_errors=True)
        except Exception as merge_err:
            log.warning(f"Leftover batch merge cleanup failed for {title}: {merge_err}")

        if errs:
            drm_errs = [e for e in errs if "DRM protected" in e]
            other_errs = [e for e in errs if "DRM protected" not in e]

            if drm_errs:
                drm_list = "\n".join("• " + e.split(":", 1)[1].strip() for e in drm_errs if ":" in e)[:800]
                try:
                    await context.bot.send_message(
                        owner_id,
                        f"🔒 **DRM-protected lectures skipped** ({len(drm_errs)})\n\n"
                        f"These videos are protected by Udemy's DRM and cannot be downloaded by third-party tools.\n"
                        f"Use the official Udemy website or mobile app 'Download' feature for offline viewing of these lectures:\n\n"
                        f"{drm_list}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

            if other_errs:
                access_errs = [e for e in other_errs if "not accessible" in e or "API error 401" in e or "API error 403" in e or "API error 404" in e]
                err_summary = "\n".join(other_errs[:5])
                if len(other_errs) > 5:
                    err_summary += f"\n... +{len(other_errs)-5} more"
                hint = ""
                # If most failures are access/404 errors, it's almost always a token/account issue.
                if access_errs and len(access_errs) >= max(3, len(other_errs) // 2):
                    hint = (
                        "\n\n💡 Many lectures returned *not accessible / 404*. This usually means:\n"
                        "• The Udemy token for this account has **expired** → refresh via /enroll_setup\n"
                        "• Or the course is enrolled on a **different account** than the one used.\n"
                        "Re-run after updating the token and it should download fully."
                    )
                try:
                    await context.bot.send_message(
                        owner_id,
                        f"\u26a0\ufe0f Some issues while archiving ({len(other_errs)}):\n`{err_summary[:400]}`{hint}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

        all_files = [f for f in out_dir.rglob("*") if f.is_file()]
        video_files = [f for f in all_files if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".m4v", ".avi")]

        if not all_files and not (zip_path.exists() and zip_path.stat().st_size > 0):
            await asyncio.to_thread(
                upsert_archive_job,
                owner_id,
                udemy_id,
                status="failed",
                stage="No files downloaded - ready to retry",
                progress=0,
                last_error="No files downloaded",
                last_heartbeat=datetime.utcnow(),
            )
            await context.bot.send_message(
                owner_id,
                f"\u274c **No files downloaded** for \u201c{title}\u201d.\n\n"
                "Possible reasons:\n"
                "\u2022 Course URL is /draft/ (Udemy DRM — cannot download)\n"
                "\u2022 Token expired — update via /enroll_setup\n"
                "\u2022 Course uses Widevine DRM (not extractable by yt-dlp)"
            )
            return

        if all_files and not (zip_path.exists() and zip_path.stat().st_size > 0):
            downloaded_bytes = sum(f.stat().st_size for f in all_files if f.exists())
            disk = get_disk_usage(str(work_dir))
            free_bytes = (disk["free_gb"] * (1024**3)) if disk else None
            # ZIP_STORED still needs a second file roughly the same size as the
            # downloaded tree. Keep a small safety buffer to avoid platform eviction.
            zip_buffer_bytes = int(ARCHIVE_MIN_FREE_GB * (1024**3))
            if free_bytes is not None and free_bytes < (downloaded_bytes + zip_buffer_bytes):
                needed_gb = (downloaded_bytes + zip_buffer_bytes) / (1024**3)
                await asyncio.to_thread(
                    upsert_archive_job,
                    owner_id,
                    udemy_id,
                    status="failed",
                    stage="Downloaded files kept - need more disk to create ZIP",
                    progress=90,
                    last_error=f"Need {needed_gb:.1f} GB free to create ZIP; have {disk['free_gb']:.1f} GB",
                    last_heartbeat=datetime.utcnow(),
                )
                await context.bot.send_message(
                    owner_id,
                    f"⚠️ **Not enough disk to create ZIP**\n\n"
                    f"Downloaded files were kept for resume.\n"
                    f"Free: {disk['free_gb']:.1f} GB\n"
                    f"Needed: {needed_gb:.1f} GB\n\n"
                    f"Fix storage (`ARCHIVE_WORK_DIR=/var/data/udemy_archives`) and click Archive/Resume again.",
                    parse_mode="Markdown",
                )
                return

        if zip_path.exists() and zip_path.stat().st_size > 0:
            await _send_progress(90, f"\u2705 Existing ZIP found. Uploading without rebuilding...")
        else:
            await _send_progress(90, f"\u2705 {len(video_files)} video(s) downloaded. Creating ZIP...")

            def _zip_tree(src, dst):
                # Use ZIP_STORED (no compression). Course content is mostly already-
                # compressed video (mp4/m3u8) — deflating it burns lots of CPU for
                # almost zero size saving. Storing keeps CPU usage minimal.
                with zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                    for root, _, files in os.walk(src):
                        for fname in sorted(files):
                            full = Path(root) / fname
                            zf.write(full, full.relative_to(src))

            await asyncio.to_thread(_zip_tree, out_dir, zip_path)
        size_mb = zip_path.stat().st_size // (1024 * 1024)
        await asyncio.to_thread(
            upsert_archive_job,
            owner_id,
            udemy_id,
            status="zip_ready",
            zip_path=str(zip_path),
            zip_size_mb=size_mb,
            video_count=len(video_files),
            stage="ZIP ready",
            progress=95,
            last_heartbeat=datetime.utcnow(),
        )
        await _send_progress(95, f"\U0001f4e6 ZIP ready: {size_mb} MB. Uploading...")

        # Once the ZIP exists, delete the extracted course folder to avoid holding
        # both copies. If upload fails, the ZIP remains and the next run resumes
        # from upload without re-downloading.
        if out_dir.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, out_dir, True)
                all_files = []
            except Exception as cleanup_err:
                log.warning(f"Could not remove extracted folder after ZIP creation: {cleanup_err}")

        MAX_PART = 2000 * 1024 * 1024
        zip_size = zip_path.stat().st_size
        total_parts = max(1, (zip_size + MAX_PART - 1) // MAX_PART)

        def _write_zip_part(src: Path, part_num: int, max_bytes: int) -> Path:
            """Create a single split part, upload it, then caller deletes it.

            We intentionally do NOT create all parts at once because that doubles
            disk usage for large courses. Keep only full ZIP + one current part.
            """
            part_path = work_dir / f"{src.stem}.part{part_num:02d}{src.suffix}"
            remaining = max_bytes
            with open(src, "rb") as source:
                source.seek((part_num - 1) * max_bytes)
                with open(part_path, "wb") as part:
                    while remaining > 0:
                        chunk = source.read(min(8 * 1024 * 1024, remaining))
                        if not chunk:
                            break
                        part.write(chunk)
                        remaining -= len(chunk)
            return part_path

        raw_target = CHANNEL_ID if CHANNEL_ID else None
        uploaded_all = True

        video_count_for_caption = len(video_files) or (job or {}).get("video_count", 0)

        for i in range(1, total_parts + 1):
            if total_parts == 1:
                p = zip_path
                delete_part_after_upload = False
            else:
                await _send_progress(95, f"✂️ Preparing upload part {i}/{total_parts}...")
                p = await asyncio.to_thread(_write_zip_part, zip_path, i, MAX_PART)
                delete_part_after_upload = True

            part_mb = p.stat().st_size // (1024 * 1024)
            part_label = f"Part {i}/{total_parts} \u2022 " if total_parts > 1 else ""
            caption = (
                f"\U0001f4da **{title}**\n"
                f"{part_label}{part_mb} MB\n"
                f"\U0001f4c2 {video_count_for_caption} lecture(s)\n"
                f"\U0001f517 udemy.com{course_url.split('/learn')[0]}"
            )
            if total_parts > 1:
                caption += "\n\n\U0001f4a1 Join: `cat *.part*.zip > full.zip`"

            # Convert channel ID to int if possible (Pyrogram requires numeric peer)
            target = raw_target
            if target and isinstance(target, str):
                try:
                    target = int(target)
                except ValueError:
                    pass  # keep as @username string — Pyrogram resolves it
            if not target:
                target = owner_id

            upload_start = [datetime.utcnow()]
            upload_last_edit = [0.0]

            async def _upload_progress_cb(current, total):
                import time
                now = time.time()
                if now - upload_last_edit[0] < 5:
                    return
                upload_last_edit[0] = now
                elapsed = max((datetime.utcnow() - upload_start[0]).total_seconds(), 0.1)
                speed_bps = current / elapsed
                if speed_bps > 1_048_576:
                    speed_str = f"{speed_bps / 1_048_576:.1f} MB/s"
                elif speed_bps > 1024:
                    speed_str = f"{speed_bps / 1024:.0f} KB/s"
                else:
                    speed_str = f"{speed_bps:.0f} B/s"

                remaining = max(total - current, 0)
                eta_secs = int(remaining / speed_bps) if speed_bps > 0 else 0
                eta_str = f"{eta_secs // 60}m {eta_secs % 60}s" if eta_secs > 60 else f"{eta_secs}s"
                uploaded_mb = current / 1_048_576
                total_mb = total / 1_048_576
                pct_ul = int(current / total * 100) if total else 0
                bar = "█" * (pct_ul // 10) + "░" * (10 - pct_ul // 10)

                stage = (
                    f"⬆️ Uploading part {i}/{total_parts}\n"
                    f"[{bar}] {pct_ul}%\n"
                    f"📦 {uploaded_mb:.1f} / {total_mb:.1f} MB\n"
                    f"⚡ {speed_str}   ⏱ ETA {eta_str}"
                )
                asyncio.create_task(_send_progress(95 + int(i * 4 / total_parts), stage))

            upload_ok = await _upload_file_to_chat(target, p, caption, p.name, progress_cb=_upload_progress_cb)
            if not upload_ok:
                try:
                    with open(p, "rb") as f:
                        await context.bot.send_document(
                            chat_id=owner_id,
                            document=f,
                            filename=p.name,
                            caption=caption[:1020],
                            parse_mode="Markdown",
                        )
                    upload_ok = True
                except Exception as e2:
                    log.error(f"All upload methods failed for {p.name}: {e2}")
                    uploaded_all = False
                    await asyncio.to_thread(
                        upsert_archive_job,
                        owner_id,
                        udemy_id,
                        status="upload_failed",
                        stage=f"Upload failed for {p.name}",
                        progress=95,
                        last_error=str(e2)[:500],
                        last_heartbeat=datetime.utcnow(),
                    )
                    break
            if upload_ok and delete_part_after_upload:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

        if not uploaded_all:
            await context.bot.send_message(
                owner_id,
                f"⚠️ Upload failed for “{title}”. The ZIP was kept for resume.\n"
                f"Run Archive again from /download_queue to resume from upload instead of re-downloading.",
            )
            return

        await asyncio.to_thread(mark_archive_job_posted, owner_id, udemy_id, total_parts, size_mb)
        archive_completed = True
        remove_from_download_queue(owner_id, udemy_id)
        drm_skipped = len([e for e in (errs or []) if "DRM protected" in e])
        done_txt = (
            f"\u2705 **Archive complete!**\n\n"
            f"\U0001f4da **{title}**\n"
            f"\U0001f4c2 {video_count_for_caption} lecture(s) \u2022 {size_mb} MB\n"
            f"\U0001f4e6 {total_parts} ZIP part(s) uploaded\n"
        )
        if drm_skipped:
            done_txt += f"\U0001f512 {drm_skipped} DRM lecture(s) skipped (not downloadable)\n"
        done_txt += "\nItem removed from queue."
        try:
            if progress_msg:
                await progress_msg.edit_text(done_txt, parse_mode="Markdown")
            else:
                await context.bot.send_message(owner_id, done_txt, parse_mode="Markdown")
        except Exception:
            pass

    except Exception as e:
        log.exception(f"Archive failed for {title}: {e}")
        await asyncio.to_thread(
            upsert_archive_job,
            owner_id,
            udemy_id,
            status="failed",
            stage="Failed - ready to resume",
            last_error=str(e)[:500],
            last_heartbeat=datetime.utcnow(),
        )
        try:
            await context.bot.send_message(
                owner_id, 
                f"\u274c Archive failed for \u201c{title}\u201d:\n`{str(e)[:300]}`\n\n"
                f"Item kept in queue. Select Archive again to resume from downloaded files/ZIP.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    finally:
        # Stop the progress monitor task
        try:
            progress_queue.put_nowait((None, None))  # Sentinel value
            await monitor_task
        except Exception:
            pass
        # Remove task from global tracker
        with active_tasks_lock:
            active_tasks.pop(task_id, None)
        # Keep files on failure/stuck so the next Archive click can resume.
        # Only remove after confirmed posted.
        if archive_completed:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass

def _split_file_into_parts(src: Path, work_dir: Path, max_bytes: int) -> list:
    """Split a large file into sequentially named .partXX.zip files."""
    parts = []
    part_num = 1
    with open(src, "rb") as f:
        while True:
            chunk = f.read(max_bytes)
            if not chunk:
                break
            part_name = work_dir / f"{src.stem}.part{part_num:02d}{src.suffix}"
            with open(part_name, "wb") as pf:
                pf.write(chunk)
            parts.append(part_name)
            part_num += 1
    return parts


async def _upload_file_to_chat(chat_id, file_path: Path, caption: str, filename=None, progress_cb=None) -> bool:
    """
    Upload a (potentially large) file to a chat.
    - If API_ID + API_HASH configured → Pyrogram (MTProto, up to ~2GB, with upload progress).
    - Otherwise → Bot API fallback (limited to ~50MB).
    Returns True on success.
    progress_cb: async callable(current_bytes, total_bytes) for upload progress.
    """
    size_mb = file_path.stat().st_size // (1024 * 1024)

    # Normalise chat_id: Pyrogram requires int for numeric ids, or @username str
    if isinstance(chat_id, str):
        try:
            chat_id = int(chat_id)
        except ValueError:
            pass  # keep as @username

    if API_ID and API_HASH:
        try:
            from pyrogram import Client
            session_name = f"arc_{int(datetime.utcnow().timestamp())}"
            async with Client(
                session_name,
                api_id=int(API_ID),
                api_hash=API_HASH,
                bot_token=os.getenv("BOT_TOKEN"),
                in_memory=True,
            ) as client:
                await client.send_document(
                    chat_id=chat_id,
                    document=str(file_path),
                    caption=caption[:1020] if caption else None,
                    file_name=filename,
                    force_document=True,
                    progress=progress_cb,   # Pyrogram calls this with (current, total)
                )
            log.info(f"Pyrogram upload OK: {file_path.name} ({size_mb} MB)")
            return True
        except Exception as e:
            log.error(f"Pyrogram upload failed for {file_path.name}: {e}")
            # fall through

    # Fallback: regular python-telegram-bot (will fail gracefully if >~50MB)
    try:
        bot_token = os.getenv("BOT_TOKEN")
        if bot_token:
            ptb_bot = Bot(token=bot_token)
            with open(file_path, "rb") as f:
                await ptb_bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=filename or file_path.name,
                    caption=caption[:1020] if caption else None,
                    parse_mode="Markdown"
                )
            return True
    except Exception as e:
        log.error(f"Bot API fallback upload failed for {file_path.name}: {e}")

    return False


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show enrollment stats for all users (owner only)"""
    if not update.effective_user or not update.effective_message:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return
    
    stats = get_all_daily_stats()
    
    lines = [
        "📊 **Enrollment Statistics**\n",
        f"📅 Date: {stats['date']}",
        f"✅ Today Total: **{stats['today_total']}** courses",
        f"📈 All-Time Total: **{stats['all_time_total']}** courses",
        "",
        "👥 **Today's Enrollments by User:**"
    ]
    
    if stats['users']:
        for i, u in enumerate(stats['users'][:20], 1):  # Top 20
            user_type = "👑" if is_owner(u['user_id']) else ("💎" if is_premium(u['user_id']) else "👤")
            lines.append(f"{i}. {user_type} `{u['user_id']}`: {u['count']} courses")
        
        if len(stats['users']) > 20:
            lines.append(f"... and {len(stats['users']) - 20} more users")
    else:
        lines.append("No enrollments today yet.")
    
    lines.append("\n_Legend: 👑=Owner 💎=Premium 👤=Free_")
    
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Setup Commands ──────────────────────────────────────────────────────────

async def cmd_enroll_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start adding a new account"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    if accounts:
        account_list = "\n".join(
            f"  {'✅' if a['auto_enroll'] else '⭕'} {a['name']} (ID: {a['id']})"
            for a in accounts
        )
        keyboard = [[
            InlineKeyboardButton("➕ Add Account", callback_data="setup_add_new"),
        ], [
            InlineKeyboardButton("🗑️ Remove Account", callback_data="setup_remove"),
            InlineKeyboardButton("✅ Done", callback_data="setup_done"),
        ]]
        await update.effective_message.reply_text(
            f"🎓 **Your Udemy Accounts:**\n\n{account_list}\n\n"
            "What would you like to do?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        set_user_setup_state(user_id, "waiting_token_new", "Account 1")
        await update.effective_message.reply_text(
            f"🎓 **Udemy Auto-Enroller Setup**\n\n"
            f"{get_setup_instructions()}\n"
            "📝 Send me your `access_token` now:",
            parse_mode="Markdown"
        )


async def cmd_set_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set access token via command - now completes setup directly (no client_id needed)"""
    if not update.effective_user or not update.effective_message or not context.args:
        await update.effective_message.reply_text("Usage: `/set_token <your_token>`", parse_mode="Markdown")
        return
    
    user_id = update.effective_user.id
    token = " ".join(context.args)
    
    if not validate_token_format(token):
        await update.effective_message.reply_text("❌ Token too short (need 20+ chars)")
        return
    
    # Show verifying message
    verify_msg = await update.effective_message.reply_text("🔄 Verifying token...")
    
    # Try to get actual Udemy username and user ID
    udemy_user_id = None
    try:
        enroller = UdemyAutoEnroller(token)
        is_valid = await asyncio.to_thread(enroller.verify_login)
        if not is_valid:
            await verify_msg.edit_text("❌ Invalid token or Udemy login failed.")
            return
        
        udemy_info = await asyncio.to_thread(enroller.get_user_info)
        if udemy_info:
            udemy_user_id = udemy_info.get("id")
            name = udemy_info.get("name") or "Udemy Account"
        else:
            name = "Udemy Account"
    except Exception:
        name = "Udemy Account"
    
    # Check if the account already exists for this user
    existing_acc = find_existing_account(user_id, token, udemy_user_id)
    if existing_acc:
        # Update existing account's token and name
        update_account_token(existing_acc["id"], token, name)
        clear_user_setup_state(user_id)
        await verify_msg.edit_text(
            f"🔄 **Account Updated!**\n\n"
            f"Your Udemy account **{name}** was already registered. I have successfully updated its access token to the new one you provided! ✅\n\n"
            f"🚀 Auto-enrollment is active!\n\n"
            f"📊 `/enroll_status` — View your stats",
            parse_mode="Markdown"
        )
    else:
        # Add as new account
        acc_id = add_account(user_id, name, token, None, udemy_user_id)
        clear_user_setup_state(user_id)
        await verify_msg.edit_text(
            f"🎉 **Setup Complete!**\n\n"
            f"✅ **{name}** added successfully\n"
            f"🚀 Auto-enrollment STARTED!\n\n"
            f"The bot will now automatically enroll you in free courses every 2 minutes.\n"
            f"You'll receive notifications when courses are enrolled.\n\n"
            f"📊 `/enroll_status` — View your stats",
            parse_mode="Markdown"
        )


# ─── Message Handler for Interactive Setup ───────────────────────────────────

async def handle_setup_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle raw message input during setup"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    step, extra = get_user_setup_state(user_id)
    
    if not step:
        return
    
    text = update.effective_message.text.strip()
    
    if step == "waiting_token_new":
        if not validate_token_format(text):
            await update.effective_message.reply_text("❌ Token too short. Copy the full `access_token` cookie value.")
            return
        
        # Show verifying message
        verify_msg = await update.effective_message.reply_text("🔄 Verifying token...")
        
        # Try to get actual Udemy username and user ID
        udemy_user_id = None
        try:
            enroller = UdemyAutoEnroller(text)  # Uses default client_id
            
            # Verify login first
            is_valid = await asyncio.to_thread(enroller.verify_login)
            if not is_valid:
                await verify_msg.edit_text("❌ Invalid token or Udemy login failed. Please check and try again.")
                return
            
            # Get actual Udemy username and ID
            udemy_info = await asyncio.to_thread(enroller.get_user_info)
            if udemy_info:
                udemy_user_id = udemy_info.get("id")
                name = udemy_info.get("name") or extra or "Udemy Account"
            else:
                name = extra or "Udemy Account"
        except Exception as e:
            log.error(f"Token verification error: {e}")
            name = extra or "Udemy Account"
        
        # Check if the account already exists for this user
        existing_acc = find_existing_account(user_id, text, udemy_user_id)
        if existing_acc:
            # Update existing account's token and name
            update_account_token(existing_acc["id"], text, name)
            clear_user_setup_state(user_id)
            await verify_msg.edit_text(
                f"🔄 **Account Updated!**\n\n"
                f"Your Udemy account **{name}** was already registered. I have successfully updated its access token to the new one you provided! ✅\n\n"
                f"🚀 Auto-enrollment is active!\n\n"
                f"📊 `/enroll_status` — View your stats",
                parse_mode="Markdown"
            )
        else:
            # Add account with actual name and udemy_user_id
            acc_id = add_account(user_id, name, text, None, udemy_user_id)
            clear_user_setup_state(user_id)
            
            await verify_msg.edit_text(
                f"🎉 **Setup Complete!**\n\n"
                f"✅ **{name}** added successfully\n"
                f"🚀 Auto-enrollment STARTED!\n\n"
                f"The bot will now automatically enroll you in free courses every 2 minutes.\n"
                f"You'll receive notifications when courses are enrolled.\n\n"
                f"📊 `/enroll_status` — View your stats",
                parse_mode="Markdown"
            )


# ─── Account Management ──────────────────────────────────────────────────────

async def _refresh_accounts_view(query) -> None:
    """Refresh accounts view - runs as independent background task"""
    try:
        user_id = query.from_user.id
        
        # Get accounts from DB (with timeout)
        try:
            accounts = await asyncio.wait_for(
                asyncio.to_thread(get_user_accounts, user_id),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            await query.message.reply_text("⏱️ Database timeout. Try again.")
            return
        except Exception as e:
            await query.message.reply_text(f"❌ Error: {str(e)[:50]}")
            return
        
        if not accounts:
            await query.message.reply_text("No accounts. Run `/enroll_setup`.", parse_mode="Markdown")
            return
        
        # Send new message immediately with "loading" for course counts
        lines = ["🎓 **Your Udemy Accounts:**\n"]
        keyboard = []
        
        for a in accounts:
            auto = "🟢 Auto" if a["auto_enroll"] else "🔴 Manual"
            lines.append(f"**{a['name']}** — {auto}\n   ⏳ Loading...")
            keyboard.append([
                InlineKeyboardButton(
                    f"{'🔴 Disable' if a['auto_enroll'] else '🟢 Enable'} Auto - {a['name']}",
                    callback_data=f"acc_toggle_{a['id']}"
                ),
                InlineKeyboardButton(f"🗑️ Remove", callback_data=f"acc_remove_{a['id']}"),
            ])
        
        keyboard.append([InlineKeyboardButton("➕ Add Account", callback_data="setup_add_new")])
        keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="show_accounts")])
        
        # Send NEW message (doesn't interfere with enrollment)
        msg = await query.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        
        # Fetch course counts in PARALLEL for all accounts
        async def get_course_count(acc):
            try:
                enroller = UdemyAutoEnroller(acc["access_token"], acc["client_id"])
                count = await asyncio.to_thread(enroller.get_total_courses_count)
                return acc["id"], count
            except Exception:
                return acc["id"], -1
        
        tasks = [get_course_count(a) for a in accounts]
        results = await asyncio.gather(*tasks)
        counts = {acc_id: count for acc_id, count in results}
        
        # Update message with actual counts
        lines = ["🎓 **Your Udemy Accounts:**\n"]
        for a in accounts:
            auto = "🟢 Auto" if a["auto_enroll"] else "🔴 Manual"
            count = counts.get(a["id"], -1)
            count_str = f"📚 {count} courses" if count >= 0 else "⚠️ Token expired"
            lines.append(f"**{a['name']}** — {auto}\n   {count_str}")
        
        await msg.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"Refresh accounts error: {e}")


async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show and manage accounts with course counts"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    
    # Try to get accounts with timeout
    try:
        accounts = await asyncio.wait_for(
            asyncio.to_thread(get_user_accounts, user_id),
            timeout=15.0
        )
    except asyncio.TimeoutError:
        await update.effective_message.reply_text(
            "⏱️ Database connection timed out. Please try again.",
            parse_mode="Markdown"
        )
        return
    except Exception as e:
        log.error(f"Error getting accounts: {e}")
        await update.effective_message.reply_text(
            f"❌ Database error: {str(e)[:100]}\nPlease try again later.",
            parse_mode="Markdown"
        )
        return
    
    if not accounts:
        await update.effective_message.reply_text(
            "No accounts set up.\nRun `/enroll_setup` to add one.",
            parse_mode="Markdown"
        )
        return
    
    # Show loading message while fetching course counts
    msg = await update.effective_message.reply_text("🔄 Loading accounts...")
    
    # Fetch ALL course counts in PARALLEL
    async def get_course_count(acc):
        try:
            enroller = UdemyAutoEnroller(acc["access_token"], acc["client_id"])
            count = await asyncio.to_thread(enroller.get_total_courses_count)
            return acc["id"], count
        except Exception:
            return acc["id"], -1
    
    tasks = [get_course_count(a) for a in accounts]
    results = await asyncio.gather(*tasks)
    counts = {acc_id: count for acc_id, count in results}
    
    lines = ["🎓 **Your Udemy Accounts:**\n"]
    keyboard = []
    
    for a in accounts:
        auto = "🟢 Auto" if a["auto_enroll"] else "🔴 Manual"
        count = counts.get(a["id"], -1)
        count_str = f"📚 {count} courses" if count >= 0 else "⚠️ Token expired"
        
        lines.append(f"**{a['name']}** — {auto}\n   {count_str}")
        keyboard.append([
            InlineKeyboardButton(
                f"{'🔴 Disable' if a['auto_enroll'] else '🟢 Enable'} Auto - {a['name']}",
                callback_data=f"acc_toggle_{a['id']}"
            ),
            InlineKeyboardButton(f"🗑️ Remove", callback_data=f"acc_remove_{a['id']}"),
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Add Account", callback_data="setup_add_new")])
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="show_accounts")])
    
    await msg.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── Auto-Enroll Toggle ─────────────────────────────────────────────────────

async def cmd_autoenroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle auto-enrollment on/off (owner only, regular users have it always ON)"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    
    # Regular users don't need this - auto-enroll is always on for them
    if not is_owner(user_id):
        await update.effective_message.reply_text(
            "✅ Auto-enrollment is **always active** for you!\n\n"
            "The bot checks for new free courses every 2 minutes and enrolls you automatically.\n\n"
            "📊 Use `/enroll_status` to see your stats.",
            parse_mode="Markdown"
        )
        return
    
    accounts = get_user_accounts(user_id)
    
    if not accounts:
        await update.effective_message.reply_text("No accounts. Run `/enroll_setup` first.", parse_mode="Markdown")
        return
    
    state = get_auto_enroll_state(user_id)
    current = state["enabled"]
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔴 Turn OFF" if current else "🟢 Turn ON",
            callback_data="autoenroll_toggle"
        ),
    ]])
    
    status = "🟢 **ACTIVE**" if current else "🔴 **INACTIVE**"
    auto_accounts = [a for a in accounts if a["auto_enroll"]]
    
    await update.effective_message.reply_text(
        f"⚡ **Auto-Enrollment Status:** {status}\n\n"
        f"Accounts with auto-enroll: {len(auto_accounts)}/{len(accounts)}\n"
        f"Check interval: Every 2 minutes\n"
        f"Total auto-enrolled: {state['total']}\n"
        f"Last check: {state['last_check'] or 'Never'}\n\n"
        "When active, I check for new free courses every 10 minutes "
        "and auto-enroll your accounts. You get notified when new courses are enrolled.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ─── Enroll Command ──────────────────────────────────────────────────────────

async def cmd_enroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual enroll - fetch courses and enroll in selected accounts"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    if not accounts:
        await update.effective_message.reply_text(
            "🔒 No accounts set up.\nRun `/enroll_setup` to add your Udemy cookies.",
            parse_mode="Markdown"
        )
        return
    
    # Check daily limit for free users
    can_do, remaining, user_is_premium = can_enroll(user_id)
    if not can_do:
        from user_enroller import OWNER_ID
        owner_link = f"tg://user?id={OWNER_ID}" if OWNER_ID else ""
        await update.effective_message.reply_html(
            f"⚠️ <b>Daily limit reached!</b>\n\n"
            f"Free users can enroll in {FREE_DAILY_LIMIT} courses/day.\n"
            f"Your limit resets at midnight.\n\n"
            f"💎 <b>Want unlimited enrollments?</b>\n"
            f"Contact the owner for premium access:\n"
            f"👉 <a href=\"{owner_link}\">Click here to message owner</a>\n"
            f"🆔 Owner ID: <code>{OWNER_ID}</code>"
        )
        return
    
    # Build limit info
    if user_is_premium:
        limit_info = "💎 Premium: Unlimited"
    else:
        limit_info = f"📊 Today: {remaining}/{FREE_DAILY_LIMIT} remaining"
    
    if len(accounts) == 1:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🚀 Enroll — {accounts[0]['name']}", callback_data="enroll_start_all"),
            InlineKeyboardButton("❌ Cancel", callback_data="enroll_cancel"),
        ]])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🚀 Enroll ALL ({len(accounts)} accounts)", callback_data="enroll_start_all")],
            [InlineKeyboardButton(f"📌 Latest account only ({accounts[-1]['name']})", callback_data=f"enroll_start_{accounts[-1]['id']}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="enroll_cancel")],
        ])
    
    await update.effective_message.reply_text(
        "🎓 **Udemy Auto-Enroller**\n\n"
        f"Accounts: {len(accounts)}\n"
        f"{limit_info}\n"
        "Action: Fetch latest 50 free courses & enroll\n"
        "Source: Real.Discount API\n"
        "Time: ~2-4 min per account\n\n"
        "Choose enrollment target:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def cmd_enroll_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show enrollment stats"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    stats = get_user_stats(user_id)
    state = get_auto_enroll_state(user_id)
    recent = get_recently_enrolled(user_id, 5)
    
    # Get daily stats
    today_count = get_daily_usage(user_id)
    total_all_time = get_user_total_enrollments(user_id)
    user_is_premium = is_premium(user_id)
    
    lines = [
        "📊 **Enrollment Stats**\n",
        f"Accounts: {stats['total_accounts']}",
        f"Auto-enroll: {'🟢 ON' if state['enabled'] else '🔴 OFF'}",
    ]
    
    # Show daily limit info
    if user_is_premium:
        lines.append(f"✅ Today: {today_count} courses (💎 Unlimited)")
    else:
        remaining = max(0, FREE_DAILY_LIMIT - today_count)
        lines.append(f"✅ Today: {today_count}/{FREE_DAILY_LIMIT} ({remaining} remaining)")
    
    lines.append(f"📈 All-time: {total_all_time} courses")
    lines.append(f"Last check: {state['last_check'] or 'Never'}")
    
    if recent:
        lines.append("\n**Recent Enrollments:**")
        for r in recent:
            short = r['title'][:40] + "..." if len(r['title']) > 40 else r['title']
            lines.append(f"• {short}")
    
    lines.append("\n`/enroll` — Enroll now\n`/autoenroll` — Toggle auto-enroll")
    
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_myprofile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show profile"""
    if not update.effective_user or not update.effective_message:
        return
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    state = get_auto_enroll_state(user_id)
    
    lines = [
        f"👤 **Profile** — `{user_id}`\n",
        f"Accounts: {len(accounts)}",
        f"Auto-enroll: {'🟢' if state['enabled'] else '🔴'}",
        f"Total enrolled: {state['total']}",
    ]
    
    keyboard = [[
        InlineKeyboardButton("⚙️ Accounts", callback_data="show_accounts"),
        InlineKeyboardButton("🗑️ Delete All Data", callback_data="clear_my_data"),
    ]]
    
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── Callbacks ───────────────────────────────────────────────────────────────

async def enroll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle enroll_ callbacks"""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "enroll_cancel":
        await query.edit_message_text("❌ Cancelled.")
    
    elif data == "enroll_start_all":
        await query.answer("🚀 Starting...")
        await query.edit_message_text("🔄 **Fetching & enrolling...**\n\n⏳ Please wait...")
        asyncio.create_task(_run_enroll_accounts(update, context, account_filter=None))
    
    elif data.startswith("enroll_start_"):
        acc_id = int(data.replace("enroll_start_", ""))
        await query.answer("🚀 Starting...")
        await query.edit_message_text("🔄 **Fetching & enrolling...**\n\n⏳ Please wait...")
        asyncio.create_task(_run_enroll_accounts(update, context, account_filter=acc_id))
    
    elif data == "enroll_auto_start":
        await query.answer("🚀 Starting...")
        await query.edit_message_text("🔄 **Enrolling...**\n\n⏳ Please wait...")
        asyncio.create_task(_run_enroll_accounts(update, context, account_filter=None))
    
    elif data == "enroll_auto_skip":
        await query.edit_message_text("✅ Skipped. Run `/enroll` anytime.")


async def setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle setup_ callbacks"""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "setup_add_new":
        accounts = get_user_accounts(user_id)
        name = f"Account {len(accounts) + 1}"
        set_user_setup_state(user_id, "waiting_token_new", name)
        await query.edit_message_text(
            f"➕ **Adding {name}**\n\n"
            f"{get_setup_instructions()}\n"
            "📝 Send your `access_token` now:",
            parse_mode="Markdown"
        )
    
    elif data == "setup_remove":
        accounts = get_user_accounts(user_id)
        keyboard = [
            [InlineKeyboardButton(f"🗑️ {a['name']}", callback_data=f"acc_remove_{a['id']}")]
            for a in accounts
        ]
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="setup_done")])
        await query.edit_message_text(
            "Select account to remove:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "setup_done":
        await query.edit_message_text("✅ Done!")
    
    elif data == "setup_update_token":
        set_user_setup_state(user_id, "waiting_token_new")
        await query.edit_message_text("📝 Send your new `access_token`:")
    
    elif data == "setup_keep_current":
        await query.edit_message_text("✅ Keeping current setup.")


async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle profile/account callbacks"""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "start_setup":
        await cmd_enroll_setup(update, context)
    
    elif data.startswith("acc_toggle_"):
        acc_id = int(data.replace("acc_toggle_", ""))
        acc = get_account(acc_id)
        if acc and acc["user_id"] == user_id:
            new_state = not acc["auto_enroll"]
            toggle_auto_enroll(acc_id, new_state)
            status = "🟢 Enabled" if new_state else "🔴 Disabled"
            await query.answer(f"Auto-enroll {status} for {acc['name']}")
            # Refresh accounts view
            await query.edit_message_text(f"✅ {acc['name']}: Auto-enroll {status}\n\nUse `/accounts` to see all.", parse_mode="Markdown")
    
    elif data.startswith("acc_remove_"):
        acc_id = int(data.replace("acc_remove_", ""))
        acc = get_account(acc_id)
        if acc and acc["user_id"] == user_id:
            remove_account(acc_id)
            await query.edit_message_text(f"🗑️ {acc['name']} removed.\n\nUse `/accounts` to see remaining.")
    
    elif data == "autoenroll_toggle":
        state = get_auto_enroll_state(user_id)
        new_enabled = not state["enabled"]
        set_auto_enroll_enabled(user_id, new_enabled)
        status = "🟢 ACTIVATED" if new_enabled else "🔴 DEACTIVATED"
        await query.edit_message_text(
            f"⚡ Auto-enrollment: {status}\n\n"
            + ("I'll check for new courses every 10 min and enroll automatically." if new_enabled
               else "Auto-enrollment stopped. Use `/enroll` for manual enrollment."),
            parse_mode="Markdown"
        )
    
    elif data == "show_accounts":
        # Refresh accounts display - runs as independent task
        await query.answer("🔄 Refreshing...")
        asyncio.create_task(_refresh_accounts_view(query))
    
    elif data == "clear_my_data":
        keyboard = [[
            InlineKeyboardButton("✅ Yes, Delete All", callback_data="confirm_delete"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete"),
        ]]
        await query.edit_message_text(
            "⚠️ Delete ALL data (accounts, history)?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "confirm_delete":
        delete_user_data(user_id)
        await query.edit_message_text("✅ All data deleted. Run `/enroll_setup` to start fresh.")
    
    elif data == "cancel_delete":
        await query.edit_message_text("Cancelled.")
    
    elif data == "update_creds":
        await cmd_enroll_setup(update, context)
    
    elif data == "toggle_channel_post":
        # Owner only
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        
        new_state = toggle_channel_posting()
        status = "🟢 **ON**" if new_state else "🔴 **OFF**"
        channel_info = f"Channel: `{CHANNEL_ID}`" if CHANNEL_ID else "⚠️ CHANNEL_ID not set"
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔴 Turn OFF" if new_state else "🟢 Turn ON",
                callback_data="toggle_channel_post"
            ),
        ]])
        
        await query.edit_message_text(
            f"📢 **Channel Course Posting**\n\n"
            f"Status: {status}\n"
            f"{channel_info}\n\n"
            f"When ON, free Udemy courses are automatically posted to the channel.\n"
            f"When OFF, course posting to channel is paused.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await query.answer(f"Channel posting {'enabled' if new_state else 'disabled'}!")

    # ─── Owner Download / Archive callbacks ─────────────────────────────────
    elif data.startswith("dl_select_"):
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        try:
            udemy_id = int(data.replace("dl_select_", ""))
        except ValueError:
            await query.answer("Bad selection")
            return

        search_state = context.user_data.get("dl_search", {})
        results = search_state.get("results", [])
        match = next((r for r in results if r.get("id") == udemy_id), None)
        if not match:
            await query.answer("Selection expired. Search again with /search_courses")
            return

        title = match.get("title", "Course")
        url = match.get("url", "")
        src_acc = match.get("source_account_id")

        job = await asyncio.to_thread(get_archive_job, user_id, udemy_id)
        if job and job.get("status") == "posted":
            await query.answer("Already archived and posted.", show_alert=True)
            return

        added = add_to_download_queue(user_id, udemy_id, title, url, src_acc)
        if added:
            await query.answer(f"✅ Added: {title[:40]}")
            # Show the queue immediately
            queue = get_owner_download_queue(user_id)
            await _show_download_queue(query, queue)
        else:
            await query.answer("Could not add (maybe duplicate?)")

    elif data in ("dl_next", "dl_prev"):
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        state = context.user_data.get("dl_search")
        if not state:
            await query.answer("Search session expired. Use /search_courses again.")
            return
        all_res = state.get("results", [])
        current_page = state.get("page", 1)
        new_page = current_page + 1 if data == "dl_next" else max(1, current_page - 1)
        state["page"] = new_page
        context.user_data["dl_search"] = state
        start = (new_page - 1) * 10
        page_results = all_res[start : start + 10]
        await _send_search_results(query.message, context, page_results, state.get("query", ""), new_page)

    elif data == "dl_queue":
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        queue = get_owner_download_queue(user_id)
        await _show_download_queue(query, queue)

    elif data == "dl_clear":
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        count = clear_owner_download_queue(user_id)
        await query.edit_message_text(f"🗑️ Cleared {count} items from download queue.")
        context.user_data.pop("dl_search", None)

    elif data.startswith("dl_remove_"):
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        try:
            uid = int(data.replace("dl_remove_", ""))
        except ValueError:
            return
        remove_from_download_queue(user_id, uid)
        queue = get_owner_download_queue(user_id)
        await _show_download_queue(query, queue)

    elif data.startswith("dl_start_"):
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        try:
            uid = int(data.replace("dl_start_", ""))
        except ValueError:
            return
        queue = get_owner_download_queue(user_id)
        item = next((q for q in queue if q["udemy_course_id"] == uid), None)
        if not item:
            await query.answer("Item not in queue anymore.")
            return
        # Start background download+zip+upload
        await query.answer("🚀 Starting download in background...")
        asyncio.create_task(_start_course_archive(update, context, item))

    elif data == "dl_start_all":
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        queue = get_owner_download_queue(user_id)
        if not queue:
            await query.answer("Queue is empty.")
            return
        await query.answer(f"🚀 Starting archive for {len(queue)} courses...")
        
        # Send initial notification
        try:
            await context.bot.send_message(
                user_id,
                f"🚀 **Archive ALL started**\n\n"
                f"Processing {len(queue)} course(s) from the queue.\n"
                f"Each will be downloaded and uploaded sequentially.\n\n"
                f"You'll receive notifications for each course.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        
        # Process sequentially with error handling
        success_count = 0
        failed_courses = []
        
        for idx, item in enumerate(queue, 1):
            try:
                log.info(f"Archive ALL: Processing {idx}/{len(queue)}: {item.get('title')}")
                await _start_course_archive(update, context, item, silent_start=True)
                success_count += 1
            except Exception as e:
                log.exception(f"Archive ALL: Failed for {item.get('title')}: {e}")
                failed_courses.append(item.get('title', 'Unknown'))
                # Continue with next course even if this one failed
        
        # Send summary
        try:
            summary = f"✅ **Archive ALL completed**\n\n"
            summary += f"✅ Successful: {success_count}/{len(queue)}\n"
            if failed_courses:
                summary += f"❌ Failed: {len(failed_courses)}\n\n"
                summary += "Failed courses:\n" + "\n".join(f"• {c}" for c in failed_courses[:5])
                if len(failed_courses) > 5:
                    summary += f"\n... and {len(failed_courses) - 5} more"
            await context.bot.send_message(user_id, summary, parse_mode="Markdown")
        except Exception:
            pass


# ─── Core Enrollment Logic ───────────────────────────────────────────────────

def _fetch_courses_from_api(limit: int = 50) -> list:
    """Fetch latest free Udemy courses from real.discount API"""
    courses = []
    seen_slugs = set()
    page = 1
    
    while len(courses) < limit:
        try:
            resp = requests.get(
                COURSES_API,
                params={"page": page, "limit": 50, "sortBy": "sale_start"},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if not items:
                break
            
            for item in items:
                try:
                    sale_price = float(item.get("sale_price", 0) or 0)
                except (ValueError, TypeError):
                    sale_price = 0
                
                if sale_price != 0 or "udemy.com" not in item.get("url", ""):
                    continue
                
                url = item.get("url", "")
                slug = UdemyAutoEnroller._extract_slug(url)
                if slug and slug in seen_slugs:
                    continue
                if slug:
                    seen_slugs.add(slug)
                coupon = url.split("couponCode=")[1].split("&")[0] if "couponCode=" in url else None
                courses.append(Course(
                    title=item.get("name", "Untitled"),
                    url=url,
                    coupon_code=coupon,
                ))
                if len(courses) >= limit:
                    break
            
            page += 1
            if page > 5:
                break
        except Exception as e:
            log.error(f"API fetch error page {page}: {e}")
            break
    
    return courses


def _progress_bar(current: int, total: int, width: int = 15) -> str:
    if total == 0:
        return "░" * width
    pct = current / total
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {int(pct * 100)}%"


def _enroll_account_in_courses(account: dict, courses: list) -> dict:
    """Enroll a single account in the given courses. Returns results dict."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    enroller = UdemyAutoEnroller(
        access_token=account["access_token"],
        client_id=account["client_id"]
    )
    
    if not enroller.verify_login():
        return {"enrolled": [], "already": 0, "expired": 0, "failed": 0, "error": "Login failed"}
    
    enroller._get_enrolled_courses()
    log.info(f"Pre-fetched {len(enroller.enrolled_slugs)} enrolled courses")
    
    enrolled = []
    already = 0
    expired = 0
    failed = 0
    batch = []
    free_courses = []
    
    # Step 1: Filter out already enrolled courses (fast, no API call)
    courses_to_process = []
    for course in courses:
        slug = enroller._extract_slug(course.url)
        if not slug:
            failed += 1
            continue
        if slug in enroller.enrolled_slugs:
            already += 1
            continue
        courses_to_process.append((course, slug))
    
    # Step 2: Validate courses in PARALLEL (4 threads)
    def validate_course(course_slug_tuple):
        course, slug = course_slug_tuple
        coupon = course.coupon_code or enroller._extract_coupon(course.url)
        course_id, is_free = enroller._get_course_id_from_page(slug)
        if not course_id:
            return ("failed", course, None, None, None)
        if str(course_id) in enroller.enrolled_course_ids:
            return ("already", course, course_id, None, None)
        subscribed = enroller._is_subscribed(course_id)
        if subscribed is True:
            return ("already", course, course_id, None, None)
        if subscribed is None:
            return ("failed", course, None, None, None)
        if is_free:
            return ("free", course, course_id, None, None)
        if not coupon:
            return ("failed", course, None, None, None)
        if not enroller._check_coupon(course_id, coupon):
            return ("expired", course, None, None, None)
        return ("valid", course, course_id, coupon, course.title)
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(validate_course, cs): cs for cs in courses_to_process}
        for future in as_completed(futures):
            try:
                status, course, course_id, coupon, title = future.result()
                if status == "failed":
                    failed += 1
                elif status == "already":
                    already += 1
                elif status == "expired":
                    expired += 1
                elif status == "free":
                    free_courses.append((course, course_id))
                elif status == "valid":
                    batch.append((course_id, coupon, title))
            except Exception:
                failed += 1
    
    # Step 3: Enroll FREE courses in parallel
    def enroll_free(course_tuple):
        course, course_id = course_tuple
        return enroller._free_checkout(course_id), course.title
    
    if free_courses:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(enroll_free, free_courses))
            for result, title in results:
                if result == "enrolled":
                    enrolled.append(title)
                    log.info(f"Free enrolled: {title[:40]}")
                elif result == "already":
                    already += 1
                else:
                    failed += 1
    
    # Step 4: Bulk checkout coupon courses (batch of 10)
    if batch:
        log.info(f"Processing {len(batch)} coupon courses in batches of 10")
        while batch:
            chunk = batch[:10]
            batch = batch[10:]
            log.info(f"Processing chunk of {len(chunk)} courses")
            titles = enroller._bulk_checkout(chunk)
            enrolled.extend(titles)
            if titles:
                log.info(f"Chunk enrolled {len(titles)}: {titles}")
            failed += len(chunk) - len(titles)
    
    log.info(f"Result: enrolled={len(enrolled)}, already={already}, expired={expired}, failed={failed}")
    return {"enrolled": enrolled, "already": already, "expired": expired, "failed": failed, "error": None}


async def _run_enroll_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE, account_filter=None) -> None:
    """Fetch courses and enroll in selected accounts with progress"""
    try:
        user_id = update.effective_user.id
        msg = update.callback_query.message if update.callback_query else update.effective_message
        
        accounts = get_user_accounts(user_id)
        if account_filter:
            accounts = [a for a in accounts if a["id"] == account_filter]
        
        if not accounts:
            await msg.edit_text("❌ No accounts found.")
            return
        
        # Check daily limit for free users
        can_do, remaining, user_is_premium = can_enroll(user_id)
        if not can_do:
            from user_enroller import OWNER_ID
            owner_link = f"tg://user?id={OWNER_ID}" if OWNER_ID else ""
            await msg.edit_text(
                f"⚠️ <b>Daily limit reached!</b>\n\n"
                f"Free: {FREE_DAILY_LIMIT}/day\n\n"
                f"💎 <b>Want unlimited?</b>\n"
                f"<a href=\"{owner_link}\">Contact owner for premium</a>\n"
                f"🆔 <code>{OWNER_ID}</code>",
                parse_mode="HTML"
            )
            return
        
        # Determine how many courses to fetch
        if user_is_premium:
            courses_to_fetch = 50
            limit_info = "💎 Premium: Unlimited"
        else:
            courses_to_fetch = min(50, remaining)
            limit_info = f"📊 Limit: {remaining} remaining today"
        
        # Fetch courses
        courses = await asyncio.to_thread(_fetch_courses_from_api, courses_to_fetch)
        if not courses:
            await msg.edit_text("❌ No free courses found from API.")
            return
        
        total_enrolled = []
        total_already = 0
        total_expired = 0
        total_failed = 0
        limit_reached = False
        
        # For free users, limit courses
        if not user_is_premium:
            courses_for_enrollment = courses[:remaining]
        else:
            courses_for_enrollment = courses
        
        # Show progress - enrolling all accounts simultaneously
        try:
            await msg.edit_text(
                f"🚀 **Enrolling ALL {len(accounts)} accounts simultaneously...**\n\n"
                f"📚 Courses to process: {len(courses_for_enrollment)}\n"
                f"{limit_info}\n\n"
                f"⏳ Please wait...",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        
        # Enroll ALL accounts SIMULTANEOUSLY using asyncio.gather
        async def enroll_single_account(account, courses_list):
            """Enroll a single account - runs in parallel with others"""
            try:
                result = await asyncio.to_thread(_enroll_account_in_courses, account, courses_list)
                return {"account": account, "result": result, "error": None}
            except Exception as e:
                return {"account": account, "result": None, "error": str(e)}
        
        # Launch all accounts in parallel
        tasks = [enroll_single_account(acc, courses_for_enrollment) for acc in accounts]
        results = await asyncio.gather(*tasks)
        
        # Process results from all parallel enrollments
        failed_accounts = []
        for res in results:
            account = res["account"]
            
            if res["error"]:
                total_failed += len(courses_for_enrollment)
                failed_accounts.append(account["name"])
                continue
            
            result = res["result"]
            if result["error"]:
                total_failed += len(courses_for_enrollment)
                failed_accounts.append(account["name"])
                continue
            
            # Log enrollments
            for title in result["enrolled"]:
                log_enrollment(user_id, account["id"], "", title)
            
            total_enrolled.extend([(t, account["name"]) for t in result["enrolled"]])
            total_already += result["already"]
            total_expired += result["expired"]
            total_failed += result["failed"]
        
        # Track daily usage after all parallel enrollments complete
        unique_enrolled = len(set(t for t, _ in total_enrolled))
        if unique_enrolled > 0 and not user_is_premium:
            increment_daily_usage(user_id, unique_enrolled)
            new_remaining = get_remaining_today(user_id)
            limit_info = f"📊 Limit: {new_remaining} remaining today"
        
        # Show failed accounts if any
        if failed_accounts:
            try:
                await msg.edit_text(
                    f"⚠️ Some accounts failed: {', '.join(failed_accounts)}\n"
                    "Token may be expired. Update with `/enroll_setup`.\n\n"
                    "Processing other accounts...",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        
        # Update state
        update_auto_enroll_state(user_id, enrolled_count=len(total_enrolled))
        
        # Final summary
        bar = _progress_bar(1, 1)
        lines = [
            f"🎉 **Enrollment Complete!**\n",
            f"{bar}\n",
            f"📊 Accounts: {len(accounts)}",
            f"✅ Enrolled: {len(total_enrolled)}",
            f"📚 Already had: {total_already}",
            f"⏰ Expired: {total_expired}",
            f"❌ Failed: {total_failed}",
        ]
        
        # Show limit reached message for free users
        if limit_reached:
            lines.append(f"\n⚠️ Daily limit reached ({FREE_DAILY_LIMIT} courses)")
        elif not user_is_premium:
            new_remaining = get_remaining_today(user_id)
            lines.append(f"\n📊 Remaining today: {new_remaining}/{FREE_DAILY_LIMIT}")
        
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
        
        # Send full list of enrolled courses as separate message(s)
        if total_enrolled:
            course_lines = [f"**✅ Newly Enrolled ({len(total_enrolled)} courses):**\n"]
            for title, acc_name in total_enrolled:
                short = title[:50] + "..." if len(title) > 50 else title
                course_lines.append(f"✅ {short}")
            
            full_text = "\n".join(course_lines)
            
            try:
                if len(full_text) <= 4000:
                    await msg.reply_text(full_text, parse_mode="Markdown")
                else:
                    # Send in chunks
                    header = course_lines[0]
                    await msg.reply_text(header, parse_mode="Markdown")
                    
                    chunk = []
                    chunk_len = 0
                    for line in course_lines[1:]:
                        if chunk_len + len(line) + 1 > 3900:
                            await msg.reply_text("\n".join(chunk))
                            chunk = []
                            chunk_len = 0
                        chunk.append(line)
                        chunk_len += len(line) + 1
                    if chunk:
                        await msg.reply_text("\n".join(chunk))
            except Exception as e:
                log.debug(f"Failed to send full course list: {e}")
        
    except Exception as e:
        log.error(f"Enroll error: {e}")
        try:
            msg = update.callback_query.message if update.callback_query else update.effective_message
            await msg.edit_text(f"❌ Error: {str(e)[:100]}")
        except Exception:
            pass


# ─── Auto-Enroll Background Job ──────────────────────────────────────────────

async def auto_enroll_job(app: Application) -> None:
    """Background job: checks API every 2 min, enrolls all enabled accounts, notifies user"""
    bot: Bot = app.bot
    log.info("Auto-enroll background job started (every 2 min)")
    
    while True:
        await asyncio.sleep(AUTO_ENROLL_INTERVAL)
        
        try:
            # Get ALL accounts with auto_enroll flag on
            accounts = await asyncio.to_thread(get_all_auto_enroll_accounts)
            if not accounts:
                continue
            
            log.info(f"Auto-enroll: {len(accounts)} accounts active, fetching courses...")
            
            # Fetch latest courses from API
            courses = await asyncio.to_thread(_fetch_courses_from_api, 50)
            if not courses:
                log.info("Auto-enroll: No courses from API this cycle")
                continue
            
            # Group accounts by user
            user_accounts = {}
            for acc in accounts:
                user_accounts.setdefault(acc["user_id"], []).append(acc)
            
            for user_id, user_accs in user_accounts.items():
                all_enrolled = []
                failed_accounts = []
                
                # Check if user has reached daily limit (for free users)
                user_is_premium = is_premium(user_id)
                if not user_is_premium:
                    remaining = get_remaining_today(user_id)
                    if remaining <= 0:
                        log.info(f"Auto-enroll: user {user_id} hit daily limit, skipping")
                        continue
                    courses_for_user = courses[:remaining]
                else:
                    courses_for_user = courses
                
                # Enroll ALL accounts SIMULTANEOUSLY using asyncio.gather
                async def enroll_single_account(acc, courses_list):
                    """Enroll a single account - runs in parallel with others"""
                    try:
                        result = await asyncio.to_thread(
                            _enroll_account_in_courses, acc, courses_list
                        )
                        return {"acc": acc, "result": result, "error": None}
                    except Exception as e:
                        return {"acc": acc, "result": None, "error": str(e)}
                
                # Launch all accounts in parallel
                tasks = [enroll_single_account(acc, courses_for_user) for acc in user_accs]
                results = await asyncio.gather(*tasks)
                
                # Process results
                total_enrolled = 0
                for res in results:
                    acc = res["acc"]
                    if res["error"]:
                        log.error(f"Auto-enroll error acc {acc['id']}: {res['error']}")
                        failed_accounts.append(acc["name"])
                        continue
                    
                    result = res["result"]
                    if result["error"]:
                        log.warning(f"Auto-enroll login failed user {user_id} acc {acc['id']}: {result['error']}")
                        failed_accounts.append(acc["name"])
                        continue
                    
                    enrolled_count = len(result["enrolled"])
                    for title in result["enrolled"]:
                        log_enrollment(user_id, acc["id"], title, title)
                        all_enrolled.append((title, acc["name"]))
                    total_enrolled += enrolled_count
                
                # Track daily usage after all parallel enrollments complete
                if total_enrolled > 0 and not user_is_premium:
                    increment_daily_usage(user_id, total_enrolled)
                
                # Always update state (last_check timestamp)
                update_auto_enroll_state(
                    user_id,
                    last_course_id=courses[0].url if courses else None,
                    enrolled_count=len(all_enrolled)
                )
                
                # Notify user only if something new was enrolled
                if all_enrolled:
                    header = f"🔔 **Auto-Enrolled {len(all_enrolled)} Courses!**\n\n"
                    
                    # Build full list of courses
                    course_lines = []
                    for title, acc_name in all_enrolled:
                        short = title[:50] + "..." if len(title) > 50 else title
                        course_lines.append(f"✅ {short}")
                    
                    # Split into chunks if too long (Telegram limit ~4096 chars)
                    full_text = header + "\n".join(course_lines)
                    
                    try:
                        if len(full_text) <= 4000:
                            await bot.send_message(
                                chat_id=user_id,
                                text=full_text,
                                parse_mode="Markdown"
                            )
                        else:
                            # Send header first
                            await bot.send_message(
                                chat_id=user_id,
                                text=header,
                                parse_mode="Markdown"
                            )
                            # Send courses in chunks
                            chunk = []
                            chunk_len = 0
                            for line in course_lines:
                                if chunk_len + len(line) + 1 > 3900:
                                    await bot.send_message(chat_id=user_id, text="\n".join(chunk))
                                    chunk = []
                                    chunk_len = 0
                                chunk.append(line)
                                chunk_len += len(line) + 1
                            if chunk:
                                await bot.send_message(chat_id=user_id, text="\n".join(chunk))
                    except Exception as e:
                        log.debug(f"Notify user {user_id} failed: {e}")
                    
                    log.info(f"Auto-enroll: user {user_id} got {len(all_enrolled)} new courses")
                
                # Notify user if ALL accounts failed login (token expired)
                if failed_accounts and not all_enrolled:
                    try:
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔄 Update Token", callback_data="setup_add_new"),
                        ]])
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"⚠️ **Auto-Enroll: Token Expired**\n\n"
                                f"Failed: {', '.join(failed_accounts)}\n\n"
                                "Your Udemy cookies have expired.\n"
                                "Click below to update with fresh cookies:"
                            ),
                            parse_mode="Markdown",
                            reply_markup=keyboard,
                        )
                    except Exception:
                        pass
        
        except Exception as e:
            log.error(f"Auto-enroll job error: {e}")
            await asyncio.sleep(10)


# Initialize database
init_enroller_db()
