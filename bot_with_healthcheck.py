"""
Telegram Course Bot — SQLite Edition (Render.com Compatible)
Fetches courses from real.discount API and posts new ones to a Telegram channel.
Duplicate prevention via local SQLite database (posted_courses.db).
Includes health check endpoint to prevent Render from sleeping.

Market module: Nifty / Sensex / Nifty BeES tracking, dip alerts to Telegram,
minimal dashboard, and JSON backtest API (see README).
"""

import os
import asyncio
import logging
import sqlite3
import html
import re
import requests
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
from aiohttp import web
from datetime import datetime, timedelta
from pathlib import Path

from market_service import (
    MARKET_FEATURES_ENABLED,
    MARKET_ALERT_CHAT_ID,
    DIP_THRESHOLD_PERCENT,
    ensure_market_tables,
    fetch_all_snapshots_async,
    format_test_dip_alert,
    build_dip_status_async,
    format_dip_status_telegram,
    run_market_monitor,
)
from market_backtest import run_backtest
from fno_entry_service import (
    build_all_entries_async,
    filter_entry_payload_for_user,
    format_entry_telegram_html,
    run_fno_monitor,
    run_fno_exit_monitor,
    run_fno_eod_summary,
    ensure_fno_tables,
    build_eod_summary_async,
    build_period_summary_async,
    format_eod_summary_html,
    format_period_summary_html,
    filter_eod_summary_for_user,
    filter_period_summary_for_user,
    parse_index_tokens,
    set_user_alert_indices,
    clear_user_alert_indices,
    format_user_alert_prefs_html,
    format_alert_prefs_set_html,
    format_alert_usage_html,
    build_trade_status_async,
    ALL_NSE_SYMBOLS,
)
from news_service import (
    ensure_news_table,
    scrape_inshorts,
    scrape_and_queue,
    get_fresh_articles_for_posting,
    format_news_post,
    format_news_posts,
    mark_news_posted,
    cleanup_old_news,
    clear_queue,
)
from multiuser_enroller_bot import (
    cmd_enroll_setup,
    cmd_set_token,
    cmd_enroll,
    cmd_enroll_status,
    cmd_myprofile,
    cmd_accounts,
    cmd_autoenroll,
    enroll_callback,
    setup_callback,
    profile_callback,
    handle_setup_message,
    auto_enroll_job,
    # Premium management (owner only)
    cmd_grant_premium,
    cmd_revoke_premium,
    cmd_list_premium,
    cmd_stats,
    cmd_channel_post,
    cmd_search_courses,
    cmd_download_queue,
    cmd_downloads,
    init_bot_pool,
)
from user_enroller import is_owner, is_premium, FREE_DAILY_LIMIT, get_remaining_today
from movie_service import (
    hdhub_latest_movies, hdhub_movie_links, format_hdhub_message,
    hdh_latest_movies, hdh_movie_links, format_hdh_message,
    md_latest_movies, md_movie_links, format_md_message,
    hdmovie2_latest_movies, hdmovie2_movie_links, format_hdmovie2_message,
    vega_latest_movies, vega_movie_links, format_vega_message,
    sdmp_latest_movies, sdmp_movie_links, format_sdmp_message,
    bollyflix_latest_movies, bollyflix_movie_links, format_bollyflix_message,
    moviesmod_latest_movies, moviesmod_movie_links, format_moviesmod_message,
    atoz_latest_movies, atoz_movie_links, format_atoz_message,
    zeefliz_search, zeefliz_movie_links, zeefliz_latest_movies, format_zeefliz_message,
    hdhub_search, hdh_search, md_search, hdmovie2_search, vega_search, sdmp_search,
    bollyflix_search, moviesmod_search, atoz_search,
    movies_search_combined, movies_latest_combined, movies_aggregate_links,
    movie_page_download_links,
)

# ─── Load env ────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN         = os.getenv("BOT_TOKEN")
CHANNEL_ID        = os.getenv("CHANNEL_ID")
MOVIES_CHANNEL_ID = os.getenv("MOVIES_CHANNEL_ID", CHANNEL_ID)
PORT = int(os.getenv("PORT", 10000))  # Render uses PORT env variable
# Set in .env to enable GET /api/test-alert?secret=... (sends sample dip text to MARKET_ALERT_CHAT_ID)
TEST_ALERT_SECRET = os.getenv("TEST_ALERT_SECRET", "").strip()
# News auto-post times (IST, 24h). Default: 10:00 and 22:00
NEWS_POST_HOURS = [int(h) for h in os.getenv("NEWS_POST_HOURS", "10,22").split(",")]

# ─── Obfuscated API Configuration ────────────────────────────────────────────
# Encoded endpoint for security - decodes at runtime
_X = [104,116,116,112,115,58,47,47,99,100,110,46,114,101,97,108,46,100,105,115,99,111,117,110,116,47,97,112,105,47,99,111,117,114,115,101,115]
def _get_endpoint(): return ''.join(chr(c) for c in _X)

DB_FILE     = "posted_courses.db"
CHECK_EVERY = 180   # seconds between polls (3 min)


# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# Telegram hard limit for sendMessage text (HTML entities count toward limit).
TG_HTML_CHUNK_SAFE = 4050


def _split_long_html_line(line: str, limit: int) -> list[str]:
    """Break one logical line that exceeds limit (e.g. many <a> joined by ' · ')."""
    if len(line) <= limit:
        return [line]
    sep = " · "
    if sep in line:
        raw_parts = line.split(sep)
        out: list[str] = []
        buf = raw_parts[0]
        if len(buf) > limit:
            while len(buf) > limit:
                out.append(buf[:limit])
                buf = buf[limit:]
        for p in raw_parts[1:]:
            extra = sep + p
            if len(buf) + len(extra) <= limit:
                buf += extra
            else:
                if buf:
                    out.append(buf)
                buf = p
                while len(buf) > limit:
                    out.append(buf[:limit])
                    buf = buf[limit:]
        if buf:
            out.append(buf)
        return out if out else [line[:limit]]
    out: list[str] = []
    rest = line
    while len(rest) > limit:
        out.append(rest[:limit])
        rest = rest[limit:]
    if rest:
        out.append(rest)
    return out


def split_html_for_telegram(text: str, limit: int = TG_HTML_CHUNK_SAFE) -> list[str]:
    """Split HTML into segments each under Telegram's 4096-char message limit."""
    if not text:
        return [""]
    lines: list[str] = []
    for line in text.split("\n"):
        lines.extend(_split_long_html_line(line, limit))
    chunks: list[str] = []
    buf: list[str] = []
    for sl in lines:
        if len(sl) > limit:
            if buf:
                chunks.append("\n".join(buf))
                buf = []
            chunks.extend(_split_long_html_line(sl, limit))
            continue
        cand = "\n".join(buf + [sl])
        if len(cand) <= limit:
            buf.append(sl)
        else:
            if buf:
                chunks.append("\n".join(buf))
            buf = [sl]
    if buf:
        chunks.append("\n".join(buf))
    return chunks


async def reply_html_chunked(
    message,
    text: str,
    *,
    reply_markup=None,
    disable_web_page_preview: bool = True,
) -> None:
    """Like reply_html; splits long bodies. Inline keyboard attaches to the first chunk."""
    chunks = split_html_for_telegram(text or "")
    for i, chunk in enumerate(chunks):
        kw: dict = {
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_web_page_preview,
        }
        if i == 0 and reply_markup is not None:
            kw["reply_markup"] = reply_markup
        await message.reply_text(**kw)


# ─── /start welcome ───────────────────────────────────────────────────────────

# Welcome for regular users (courses only)
WELCOME_USER_HTML = """<b>🎓 Udemy Auto-Enroller Bot</b>

Auto-enroll in FREE Udemy courses — automatically!

<b>🚀 Get Started:</b>
/enroll_setup — add your Udemy account (one-time)

<b>📚 Other Commands:</b>
/enroll — manual enroll now
/enroll_status — view your stats
/accounts — manage accounts
/myid — your Telegram ID

<b>📊 Free Plan:</b> {remaining}/{limit} enrollments today
<b>💎 Premium:</b> Unlimited enrollments

<b>ℹ️ How it works:</b>
1. Run /enroll_setup and add your Udemy cookies
2. Done! Bot auto-enrolls you every 2 minutes
3. You'll get notifications when courses are enrolled

⚡ <a href="https://t.me/CoursesDrivee">@CoursesDrivee</a>"""

# Welcome for premium users
WELCOME_PREMIUM_HTML = """<b>💎 Udemy Auto-Enroller Bot</b>

Premium user — <b>Unlimited enrollments!</b>

<b>🚀 Get Started:</b>
/enroll_setup — add your Udemy account (one-time)

<b>📚 Other Commands:</b>
/enroll — manual enroll now
/enroll_status — view your stats
/accounts — manage accounts
/myid — your Telegram ID

<b>ℹ️ How it works:</b>
1. Run /enroll_setup and add your Udemy cookies
2. Done! Bot auto-enrolls you every 2 minutes
3. You'll get notifications when courses are enrolled

⚡ <a href="https://t.me/CoursesDrivee">@CoursesDrivee</a>"""

# Full welcome for owner
WELCOME_OWNER_HTML = """<b>👑 Owner Dashboard</b>

<b>📽️ Movie Commands:</b>
/movies — browse latest movies
/search &lt;title&gt; — search all movie sites

<b>📰 News Commands:</b>
/news — preview latest tech news

<b>📈 Market Commands:</b>
/market — live market snapshot
/entry — F&amp;O scalp sheet (respects /alert indices, shows filter pass/fail)
/summary — today's trade win/loss summary
/summary week — last 7 days + filter stats
/summaryweek — same as /summary week
/alert — choose which index alerts (nifty, banknifty, …)
/clearalert — reset to all indices
/myalerts — show your alert filter
/trade — today's trades (live P&amp;L on open legs)
/trade 42 — detail for trade #T-42
/trade open — open trades only
/testdip — sample dip alert
/testalert — test alert delivery

<b>🎓 Udemy Enroller:</b>
/enroll_setup — add Udemy accounts
/enroll — enroll in courses
/accounts — manage accounts
/autoenroll — toggle auto-enrollment
/enroll_status — view stats

<b>📚 Course Archive (Owner):</b>
/search_courses &lt;query&gt; — search enrolled courses
/download_queue — view download queue
/downloads or /status — live download progress & system stats

<b>👑 Owner Commands:</b>
/grant_premium &lt;user_id&gt; — give premium access
/revoke_premium &lt;user_id&gt; — remove premium
/list_premium — show all premium users
/stats — enrollment stats for all users
/channel_post — toggle channel posting

<b>🔧 Utility:</b>
/start — this menu
/help — detailed help
/myid — your Telegram ID

⚡ <a href="https://t.me/CoursesDrivee">@CoursesDrivee</a>"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    # Check if new user (for notification)
    is_new_user = False
    con = sqlite3.connect(DB_FILE)
    try:
        cursor = con.execute("SELECT 1 FROM bot_users WHERE chat_id = ?", (chat_id,))
        if cursor.fetchone() is None:
            is_new_user = True
            con.execute("INSERT INTO bot_users (chat_id) VALUES (?)", (chat_id,))
            con.commit()
    except Exception as e:
        log.error("Failed to save user %s: %s", chat_id, e)
    finally:
        con.close()
    
    # Notify owner about new user (if not owner themselves)
    if is_new_user and not is_owner(user_id):
        from user_enroller import OWNER_ID
        if OWNER_ID and OWNER_ID != 0:
            try:
                username = f"@{user.username}" if user.username else "No username"
                name = user.full_name or "No name"
                notify_msg = (
                    f"🆕 <b>New User Started Bot!</b>\n\n"
                    f"👤 <b>Name:</b> {name}\n"
                    f"🔗 <b>Username:</b> {username}\n"
                    f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
                    f"<i>Grant premium: /grant_premium {user_id}</i>"
                )
                await context.bot.send_message(chat_id=OWNER_ID, text=notify_msg, parse_mode="HTML")
            except Exception as e:
                log.error(f"Failed to notify owner about new user: {e}")
    
    # Show appropriate welcome based on user type
    if is_owner(user_id):
        await update.effective_message.reply_html(WELCOME_OWNER_HTML)
    elif is_premium(user_id):
        await update.effective_message.reply_html(WELCOME_PREMIUM_HTML)
    else:
        remaining = get_remaining_today(user_id)
        msg = WELCOME_USER_HTML.format(remaining=remaining, limit=FREE_DAILY_LIMIT)
        await update.effective_message.reply_html(msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed help based on user type"""
    if not update.effective_message or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    # Base help for all users (Udemy enroller)
    base_help = """<b>📖 UDEMY ENROLLER HELP</b>

<b>/enroll_setup</b>
Add a new Udemy account. You can add multiple!
Bot asks for access_token and client_id from cookies.

<b>/set_token &lt;token&gt;</b>
Set access_token directly.

<b>/set_client_id &lt;id&gt;</b>
Set client_id directly.

<b>/enroll</b>
Enroll in latest 50 free courses.
Choose: All accounts or latest only.

<b>/accounts</b>
Manage your Udemy accounts:
• Toggle auto-enroll per account
• Add or remove accounts

<b>/autoenroll</b>
Toggle background auto-enrollment.
Bot checks every 2 min for new courses.

<b>/enroll_status</b>
View enrollment stats.

<b>/myprofile</b>
View profile and manage data.

<b>/myid</b>
Show your Telegram user ID.

<b>💡 TIPS</b>
• Add multiple Udemy accounts
• Enable /autoenroll to never miss free courses
• Your data is private

<b>❓ FAQ</b>

<b>Q: Token expired?</b>
A: /enroll_setup → Add new account with fresh cookies.
"""
    
    # Premium info for non-premium users
    if not is_premium(user_id):
        base_help += f"""
<b>📊 FREE PLAN LIMITS</b>
Daily limit: {FREE_DAILY_LIMIT} courses/day
Resets at midnight.
💎 Contact owner for unlimited (premium).
"""
    
    # Owner-only additions
    if is_owner(user_id):
        owner_help = """

<b>👑 OWNER COMMANDS</b>

<b>/grant_premium &lt;user_id&gt;</b>
Give a user unlimited enrollment access.

<b>/revoke_premium &lt;user_id&gt;</b>
Remove premium access from a user.

<b>/list_premium</b>
Show all premium users.

<b>/stats</b>
View enrollment stats: today's total, all-time total,
and breakdown by user (owner/premium/free).

<b>/channel_post</b>
Toggle automatic channel posting of enrolled courses.

<b>📚 COURSE ARCHIVE</b>

<b>/search_courses &lt;query&gt;</b>
Search your enrolled courses across all accounts.
Select results to add to download queue.

<b>/download_queue</b>
View and manage your course download queue.
Archive courses with all resources (videos, PDFs, etc.).

<b>/downloads</b> (or <b>/status</b>)
View active downloads/uploads with real-time stats.
Reuses the same live message (no spam):
• Progress and speed for each task
• System disk usage and free space
• Bandwidth monitoring

<b>📽️ MOVIES</b>
/movies — browse latest movies
/search &lt;title&gt; — search movies

<b>📰 NEWS</b>
/news — preview &amp; post tech news

<b>📈 MARKET</b>
/market — live market snapshot
/entry — F&amp;O scalp sheet (respects /alert indices, shows filter pass/fail)
/summary — today's trade win/loss summary
/summary week — last 7 days + filter stats
/summaryweek — same as /summary week
/alert — choose which index alerts (nifty, banknifty, …)
/clearalert — reset to all indices
/myalerts — show your alert filter
/trade — today's trades (live P&amp;L on open legs)
/trade 42 — detail for trade #T-42
/trade open — open trades only
/testdip — sample dip alert
/testalert — test alert delivery
"""
        base_help += owner_help
    
    await update.effective_message.reply_html(base_help)


async def cmd_testdip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a sample dip alert. Owner only."""
    if not update.effective_message or not update.effective_user:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only feature.")
        return
    text = format_test_dip_alert(threshold=DIP_THRESHOLD_PERCENT)
    await update.effective_message.reply_text(text)


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch Yahoo snapshot now and explain dip rule. Owner only."""
    if not update.effective_message or not update.effective_user:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only feature. Use /enroll for Udemy courses.")
        return
    status = await build_dip_status_async()
    await update.effective_message.reply_text(format_dip_status_telegram(status))


async def cmd_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scalp entry/exit premiums for index F&O. Owner only; respects /alert index prefs."""
    if not update.effective_message or not update.effective_user:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only feature. Use /enroll for Udemy courses.")
        return
    await update.effective_message.reply_text("⏳ Scanning indices (same filters as auto-alerts)...")
    payload = await build_all_entries_async()
    payload = filter_entry_payload_for_user(payload, update.effective_chat.id)
    if not payload:
        await update.effective_message.reply_text(
            "No indices match your /alert settings. Use /myalerts or /clearalert."
        )
        return
    text = format_entry_telegram_html(payload)
    await reply_html_chunked(update.effective_message, text)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Today's or weekly F&O alert win/loss summary. Owner only."""
    if not update.effective_message or not update.effective_user:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only feature.")
        return

    args = [a.lower() for a in (context.args or [])]
    if args and args[0] in ("week", "7d", "weekly", "7"):
        await cmd_summaryweek(update, context)
        return

    await update.effective_message.reply_text("⏳ Building today's trade summary...")
    summary = await build_eod_summary_async()
    if not summary:
        await update.effective_message.reply_text("No F&O alerts recorded today yet.")
        return
    user_summary = filter_eod_summary_for_user(summary, update.effective_chat.id)
    if not user_summary:
        await update.effective_message.reply_text(
            "No alerts today for your selected indices. Use /myalerts to check."
        )
        return
    await reply_html_chunked(update.effective_message, format_eod_summary_html(user_summary))


async def cmd_summaryweek(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last 7 days F&O summary with filter stats. Owner only."""
    if not update.effective_message or not update.effective_user:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only feature.")
        return
    days = 7
    if context.args:
        try:
            days = max(1, min(30, int(context.args[0])))
        except ValueError:
            pass
    await update.effective_message.reply_text(f"⏳ Building last {days} days summary...")
    summary = await build_period_summary_async(days)
    if not summary:
        await update.effective_message.reply_text("No F&O alert data for this period yet.")
        return
    user_summary = filter_period_summary_for_user(summary, update.effective_chat.id)
    if not user_summary:
        await update.effective_message.reply_text(
            "No alerts in this period for your selected indices. Use /myalerts to check."
        )
        return
    await reply_html_chunked(update.effective_message, format_period_summary_html(user_summary))


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Choose which index F&O alerts to receive."""
    if not update.effective_message or not update.effective_chat:
        return
    ensure_fno_tables()
    symbols, err = parse_index_tokens(context.args or [])
    if err == "usage":
        await update.effective_message.reply_html(format_alert_usage_html())
        return
    if err:
        await update.effective_message.reply_html(f"❌ {err}")
        return
    if symbols is None:
        await update.effective_message.reply_html(format_alert_usage_html())
        return
    if set(symbols) == ALL_NSE_SYMBOLS:
        clear_user_alert_indices(update.effective_chat.id)
        await update.effective_message.reply_html(
            "<b>✅ F&amp;O alerts reset</b>\n\nYou will receive alerts for <b>all indices</b>."
        )
        return
    set_user_alert_indices(update.effective_chat.id, symbols)
    await update.effective_message.reply_html(format_alert_prefs_set_html(symbols))


async def cmd_clearalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset F&O alerts to all indices."""
    if not update.effective_message or not update.effective_chat:
        return
    ensure_fno_tables()
    clear_user_alert_indices(update.effective_chat.id)
    await update.effective_message.reply_html(
        "<b>✅ F&amp;O alerts cleared</b>\n\nYou will receive alerts for <b>all indices</b> again."
    )


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show today's F&O trades with live status. Respects /alert index prefs."""
    if not update.effective_message or not update.effective_chat:
        return
    ensure_fno_tables()
    arg = " ".join(context.args) if context.args else None
    await update.effective_message.reply_text("⏳ Fetching trade status...")
    text = await build_trade_status_async(update.effective_chat.id, arg)
    await reply_html_chunked(update.effective_message, text)


async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show which index F&O alerts you receive."""
    if not update.effective_message or not update.effective_chat:
        return
    ensure_fno_tables()
    await update.effective_message.reply_html(
        format_user_alert_prefs_html(update.effective_chat.id)
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the sender's numeric chat ID — use this as MARKET_ALERT_CHAT_ID."""
    if not update.effective_message or not update.effective_chat:
        return
    cid = update.effective_chat.id
    await update.effective_message.reply_html(
        f"Your numeric chat ID is:\n\n<code>{cid}</code>\n\n"
        "Copy that value and set it in your <code>.env</code>:\n"
        f"<code>MARKET_ALERT_CHAT_ID={cid}</code>"
    )


async def cmd_testalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnose dip alert delivery. Owner only."""
    if not update.effective_message or not update.effective_user:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only feature.")
        return
    chat = MARKET_ALERT_CHAT_ID
    await update.effective_message.reply_text(
        f"Trying to send a test dip alert to: <code>{chat}</code>\n"
        f"(This is the value of MARKET_ALERT_CHAT_ID in .env)",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(
            chat_id=chat,
            text=(
                "🧪 Connectivity test from your dip alert bot.\n\n"
                "If you see this, real dip alerts will reach you here. ✅"
            ),
        )
        await update.effective_message.reply_text(
            f"✅ Success! Message delivered to <code>{chat}</code>.\n"
            "Real dip alerts will work.",
            parse_mode="HTML",
        )
    except TelegramError as e:
        await update.effective_message.reply_text(
            f"❌ <b>Failed to send to <code>{chat}</code></b>\n\n"
            f"<b>Error:</b> <code>{e}</code>\n\n"
            "<b>Fix:</b>\n"
            "• If this is a username: that user must send /start to this bot first.\n"
            "• If this is a channel: bot must be an admin of that channel.\n"
            "• Or change MARKET_ALERT_CHAT_ID in .env to your numeric chat ID "
            "(get it from @userinfobot).",
            parse_mode="HTML",
        )


async def cmd_updateapi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Update ScraperAPI key directly from Telegram."""
    if not update.effective_message or not update.effective_user:
        return
    
    # Only allow the bot owner (admin)
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
    if ADMIN_ID == 0 or update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_html(
            "❌ <b>Unauthorized</b>\n\n"
            "You are not authorized to use this command.\n"
            "Only the bot admin can update the API key."
        )
        return
    
    # Check if user provided a new key
    if not context.args:
        await update.effective_message.reply_html(
            "📝 <b>Usage:</b>\n\n"
            "<code>/updateapi &lt;new_scraper_api_key&gt;</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/updateapi abc123def456xyz789</code>\n\n"
            "<b>Current key:</b>\n"
            f"<code>{os.getenv('SCRAPER_API_KEY', 'NOT SET')[:20]}...</code>"
        )
        return
    
    new_key = " ".join(context.args).strip()
    
    if len(new_key) < 10:
        await update.effective_message.reply_html(
            "❌ <b>Invalid key</b>\n\n"
            "The API key seems too short. Please verify and try again."
        )
        return
    
    try:
        # Update the live process environment (works on Render + local)
        os.environ["SCRAPER_API_KEY"] = new_key

        # Also persist to .env file if it exists (local dev convenience)
        env_path = Path(".env")
        persisted = False
        try:
            if env_path.exists():
                env_content = env_path.read_text(encoding="utf-8")
                if "SCRAPER_API_KEY=" in env_content:
                    env_content = re.sub(
                        r"SCRAPER_API_KEY=.*",
                        f"SCRAPER_API_KEY={new_key}",
                        env_content
                    )
                else:
                    env_content += f"\nSCRAPER_API_KEY={new_key}"
                env_path.write_text(env_content, encoding="utf-8")
                persisted = True
        except Exception:
            pass  # File write is optional; os.environ update is what matters

        persist_note = (
            "Saved to <code>.env</code> (persists across restarts)."
            if persisted
            else "Active for this session (will reset on next deploy)."
        )
        await update.effective_message.reply_html(
            "✅ <b>API Key Updated!</b>\n\n"
            f"<b>New key (masked):</b> <code>{new_key[:10]}...{new_key[-4:]}</code>\n\n"
            f"<i>{persist_note}</i>"
        )
        log.info(f"ScraperAPI key updated by user {update.effective_user.id}")
    except Exception as e:
        await update.effective_message.reply_html(
            f"❌ <b>Error updating key</b>\n\n"
            f"<code>{html.escape(str(e))}</code>"
        )
        log.error(f"Failed to update API key: {e}")


async def cmd_movies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show site picker: 4KHDHub or MoviesDrive. Owner only."""
    if not update.effective_message or not update.effective_user:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only feature. Use /enroll for Udemy courses.")
        return

    keyboard = [
        [InlineKeyboardButton("⭐ HDHub4u (4K/HDR – Best)",  callback_data="msite_hdhub")],
        [InlineKeyboardButton("🎬 4KHDHub (4K/HDR)",        callback_data="msite_hdh")],
        [InlineKeyboardButton("🎥 MoviesDrive (480p–4K)",    callback_data="msite_md")],
        [InlineKeyboardButton("🎞 HDMovie2 (480p–1080p)",    callback_data="msite_hdmovie2")],
        [InlineKeyboardButton("🌟 Vegamovies (480p–4K)",     callback_data="msite_vega")],
        [InlineKeyboardButton("📀 SDMoviesPoint (HD+4K)",    callback_data="msite_sdmp")],
        [InlineKeyboardButton("🎞 BollyFlix",               callback_data="msite_bolly")],
        [InlineKeyboardButton("🚜 MoviesMod",                callback_data="msite_moviesmod")],
        [InlineKeyboardButton("🅰️ AtoZ Cinemas",            callback_data="msite_atoz")],
        [InlineKeyboardButton("🎬 ZeeFliz (Multi Audio)",    callback_data="msite_zeefliz")],
    ]
    await update.effective_message.reply_text(
        "🍿 <b>Movie Downloader</b>\n\nChoose a source:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def cmd_movietest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Test connectivity to movie sites. Owner only."""
    if not update.effective_message or not update.effective_user:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only feature.")
        return

    msg = await update.effective_message.reply_text("🔍 Testing connectivity to movie sites…")

    from movie_service import SCRAPER_API_KEY, BOLLY_BASE, _get as movie_get
    tests = [
        ("HDHub4u",        "https://new2.hdhub4u.cl/"),
        ("4KHDHub",        "https://4khdhub.link/category/hindi-movies/"),
        ("MoviesDrive",    "https://new2.moviesdrives.my/"),
        ("HDMovie2",       "https://newhdmovie2.pro/"),
        ("Vegamovies",     "https://vegamovies.global/"),
        ("SDMoviesPoint",  "https://sd1.sdmoviespoint.trade/"),
        ("BollyFlix",      f"{BOLLY_BASE}/"),
    ]

    mode = f"ScraperAPI (key set ✅)" if SCRAPER_API_KEY else "Direct (no proxy ⚠️ — may be blocked on cloud)"
    lines = [f"<b>Movie Site Connectivity Test</b>", f"Mode: <i>{mode}</i>\n"]

    for label, url in tests:
        try:
            r = await asyncio.to_thread(movie_get, url, 1, timeout=20)
            icon = "✅" if r.status_code == 200 else "⚠️"
            lines.append(f"{icon} <b>{label}</b>: HTTP {r.status_code} ({len(r.content):,} bytes)")
        except Exception as e:
            lines.append(f"❌ <b>{label}</b>: {type(e).__name__}: {str(e)[:120]}")

    if not SCRAPER_API_KEY:
        lines.append("\n💡 Set <code>SCRAPER_API_KEY</code> in env to bypass Cloudflare on cloud hosts.\nFree at scraperapi.com (5,000 req/month)")

    await msg.edit_text("\n".join(lines), parse_mode="HTML")


async def movie_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all movie-related inline button presses."""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    if str(update.effective_user.id) != str(MARKET_ALERT_CHAT_ID):
        await query.answer("⛔ You do not have permission.", show_alert=True)
        return

    data = query.data

    async def _edit_or_reply(text: str, **kwargs) -> None:
        """Edit text messages in-place; send a new message for photo/sticker messages."""
        if query.message and query.message.photo:
            await query.message.reply_text(text, **kwargs)
        else:
            try:
                await query.edit_message_text(text, **kwargs)
            except Exception:
                await query.message.reply_text(text, **kwargs)

    # ── Site picker / page navigation ────────────────────────────────────────
    if data.startswith("msite_") or data.startswith("mpage_"):
        # msite_hdh → page 1 for hdh
        # mpage_hdh_3 → page 3 for hdh
        if data.startswith("msite_"):
            source = data[len("msite_"):]
            page = 1
        else:
            _, source, p = data.split("_", 2)
            page = int(p)

        await query.answer(f"Loading page {page}…")

        fetch_fn = (hdhub_latest_movies  if source == "hdhub"
                    else hdh_latest_movies   if source == "hdh"
                    else md_latest_movies    if source == "md"
                    else vega_latest_movies  if source == "vega"
                    else sdmp_latest_movies  if source == "sdmp"
                    else bollyflix_latest_movies if source == "bolly"
                    else moviesmod_latest_movies if source == "moviesmod"
                    else atoz_latest_movies    if source == "atoz"
                    else zeefliz_latest_movies if source == "zeefliz"
                    else hdmovie2_latest_movies)
        movies = await asyncio.to_thread(fetch_fn, page)

        if not movies:
            await _edit_or_reply(
                "❌ No more movies found." if page > 1 else "❌ Failed to fetch movies. Try again later."
            )
            return

        # Store this page's movies keyed by global index offset so mpick_ indices don't collide
        offset = (page - 1) * 10
        stored: dict = context.user_data.setdefault(f"movies_{source}", {})
        for i, m in enumerate(movies):
            stored[offset + i] = m

        keyboard = []
        for i, m in enumerate(movies):
            title = m["title"][:55] + "…" if len(m["title"]) > 55 else m["title"]
            keyboard.append([InlineKeyboardButton(title, callback_data=f"mpick_{source}_{offset + i}")])

        # Prev / Next row
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("« Prev", callback_data=f"mpage_{source}_{page - 1}"))
        nav.append(InlineKeyboardButton(f"Page {page}", callback_data="mnoop"))
        if len(movies) == 10:
            nav.append(InlineKeyboardButton("Next »", callback_data=f"mpage_{source}_{page + 1}"))
        keyboard.append(nav)
        keyboard.append([InlineKeyboardButton(
            "📢 Post to Channel", callback_data=f"mpost_list_{source}_{page}"
        )])
        keyboard.append([InlineKeyboardButton("« Back to sources", callback_data="mback_sites")])

        site_label = {"hdhub": "HDHub4u", "hdh": "4KHDHub", "md": "MoviesDrive",
                      "hdmovie2": "HDMovie2", "vega": "Vegamovies", "sdmp": "SDMoviesPoint",
                      "bolly": "BollyFlix", "moviesmod": "MoviesMod", "atoz": "AtoZ Cinemas",
                      "zeefliz": "ZeeFliz"}.get(source, source)
        await _edit_or_reply(
            f"🍿 <b>Latest Movies — {site_label}</b>  (page {page})\n\nTap a movie for download links:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    # ── noop for page-number button ──────────────────────────────────────────
    elif data == "mnoop":
        await query.answer()
        return

    # ── Back to site picker ──────────────────────────────────────────────────
    elif data == "mback_sites":
        await query.answer()
        keyboard = [
            [InlineKeyboardButton("⭐ HDHub4u (4K/HDR – Best)",  callback_data="msite_hdhub")],
            [InlineKeyboardButton("🎬 4KHDHub (4K/HDR)",        callback_data="msite_hdh")],
            [InlineKeyboardButton("🎥 MoviesDrive (480p–4K)",    callback_data="msite_md")],
            [InlineKeyboardButton("🎞 HDMovie2 (480p–1080p)",    callback_data="msite_hdmovie2")],
            [InlineKeyboardButton("🌟 Vegamovies (480p–4K)",     callback_data="msite_vega")],
            [InlineKeyboardButton("📀 SDMoviesPoint (HD+4K)",    callback_data="msite_sdmp")],
            [InlineKeyboardButton("🎞 BollyFlix",               callback_data="msite_bolly")],
            [InlineKeyboardButton("🚜 MoviesMod",               callback_data="msite_moviesmod")],
            [InlineKeyboardButton("🅰️ AtoZ Cinemas",            callback_data="msite_atoz")],
            [InlineKeyboardButton("🎬 ZeeFliz (Multi Audio)",    callback_data="msite_zeefliz")],
        ]
        await _edit_or_reply(
            "🍿 <b>Movie Downloader</b>\n\nChoose a source:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    # ── Movie selected ────────────────────────────────────────────────────────
    elif data.startswith("mpick_"):
        parts = data.split("_")          # mpick_hdh_0
        source = parts[1]
        idx = int(parts[2])
        movies = context.user_data.get(f"movies_{source}", {})
        movie = movies.get(idx) if isinstance(movies, dict) else (movies[idx] if idx < len(movies) else None)
        if not movie:
            await query.answer("Session expired — run /movies again.", show_alert=True)
            return

        await query.answer("Fetching links…")

        poster_url = movie.get("poster", "")
        page = idx // 10 + 1
        back_cb = f"msite_{source}" if page == 1 else f"mpage_{source}_{page}"

        # Send an immediate "processing" message that we'll edit later
        processing_msg = await query.message.reply_text(
            "⏳ Fetching download links... This may take a moment.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("« Cancel", callback_data=back_cb)
            ]])
        )

        # Define the scraping task
        async def _do_scrape():
            try:
                if source == "hdhub":
                    detail = await asyncio.to_thread(hdhub_movie_links, movie["url"])
                    text = format_hdhub_message(movie["title"], detail)
                elif source == "hdh":
                    detail = await asyncio.to_thread(hdh_movie_links, movie["url"])
                    text = format_hdh_message(movie["title"], detail)
                elif source == "md":
                    detail = await asyncio.to_thread(md_movie_links, movie["url"])
                    text = format_md_message(movie["title"], detail)
                elif source == "vega":
                    detail = await asyncio.to_thread(vega_movie_links, movie["url"])
                    text = format_vega_message(movie["title"], detail)
                elif source == "sdmp":
                    detail = await asyncio.to_thread(sdmp_movie_links, movie["url"])
                    text = format_sdmp_message(movie["title"], detail)
                elif source == "bolly":
                    detail = await asyncio.to_thread(bollyflix_movie_links, movie["url"])
                    text = format_bollyflix_message(movie["title"], detail)
                elif source == "moviesmod":
                    detail = await asyncio.to_thread(moviesmod_movie_links, movie["url"])
                    text = format_moviesmod_message(movie["title"], detail)
                elif source == "atoz":
                    detail = await asyncio.to_thread(atoz_movie_links, movie["url"])
                    text = format_atoz_message(movie["title"], detail)
                elif source == "zeefliz":
                    detail = await asyncio.to_thread(zeefliz_movie_links, movie["url"])
                    text = format_zeefliz_message(movie["title"], detail)
                else:
                    detail = await asyncio.to_thread(hdmovie2_movie_links, movie["url"])
                    text = format_hdmovie2_message(movie["title"], detail)

                poster_url = detail.get("poster") or movie.get("poster", "")
                action_row = [
                    InlineKeyboardButton("📢 Post to Channel", callback_data=f"mpost_single_{source}_{idx}"),
                    InlineKeyboardButton("« Back", callback_data=back_cb),
                ]

                # Edit the processing message with results
                try:
                    if poster_url and len(text) <= 1024:
                        await processing_msg.delete()
                        await query.message.reply_photo(
                            photo=poster_url,
                            caption=text,
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup([action_row]),
                        )
                    elif poster_url:
                        await processing_msg.delete()
                        await query.message.reply_photo(photo=poster_url)
                        await reply_html_chunked(
                            query.message,
                            text,
                            disable_web_page_preview=True,
                            reply_markup=InlineKeyboardMarkup([action_row]),
                        )
                    else:
                        await reply_html_chunked(
                            processing_msg,
                            text,
                            disable_web_page_preview=True,
                            reply_markup=InlineKeyboardMarkup([action_row]),
                        )
                except Exception as e:
                    log.error("Error sending results: %s", e)
                    await processing_msg.edit_text(
                        f"❌ Error displaying results. Please try again.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("« Back", callback_data=back_cb)
                        ]])
                    )
            except asyncio.TimeoutError:
                await processing_msg.edit_text(
                    "⏱ Scraping timed out. The site may be slow or blocking requests. Please try again.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("« Back", callback_data=back_cb)
                    ]])
                )
            except Exception as e:
                log.error("Error scraping movie %s: %s", movie["url"], e)
                await processing_msg.edit_text(
                    f"❌ Error fetching links: {str(e)[:100]}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("« Back", callback_data=back_cb)
                    ]])
                )

        # Run scraping as a background task so bot stays responsive
        asyncio.create_task(_do_scrape())


# ─── Udemy Enroller (handled by multiuser_enroller_bot.py) ───────────────


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search movie sites. Usage: /search <title>. Owner only."""
    if not update.effective_message or not update.effective_user:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only feature. Use /enroll for Udemy courses.")
        return

    search_query = " ".join(context.args or []).strip()
    if not search_query:
        await update.effective_message.reply_text(
            "Usage: <code>/search movie name</code>\nExample: <code>/search Project Hail Mary</code>",
            parse_mode="HTML",
        )
        return

    # Store query and show source picker
    context.user_data["pending_search"] = search_query
    keyboard = [
        [InlineKeyboardButton("⭐ HDHub4u",      callback_data="msrc_hdhub"),
         InlineKeyboardButton("🎬 4KHDHub",      callback_data="msrc_hdh")],
        [InlineKeyboardButton("🎥 MoviesDrive",  callback_data="msrc_md"),
         InlineKeyboardButton("🎞 HDMovie2",     callback_data="msrc_hdmovie2")],
        [InlineKeyboardButton("🌟 Vegamovies",   callback_data="msrc_vega"),
         InlineKeyboardButton("📀 SDMoviesPoint", callback_data="msrc_sdmp")],
        [InlineKeyboardButton("🎞 BollyFlix",    callback_data="msrc_bolly"),
         InlineKeyboardButton("🚜 MoviesMod",    callback_data="msrc_moviesmod")],
        [InlineKeyboardButton("🅰️ AtoZ Cinemas", callback_data="msrc_atoz"),
         InlineKeyboardButton("🎬 ZeeFliz",      callback_data="msrc_zeefliz")],
        [InlineKeyboardButton("🔍 All Sites",    callback_data="msrc_both")],
    ]
    await update.effective_message.reply_html(
        f"🔍 Search for <b>{search_query}</b>\n\nChoose where to search:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def search_source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle source picker for /search command."""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    if str(update.effective_user.id) != str(MARKET_ALERT_CHAT_ID):
        await query.answer("⛔ You do not have permission.", show_alert=True)
        return

    source = query.data[len("msrc_"):]    # "hdh", "md", "both", or "back"

    async def _src_edit(text: str, **kwargs) -> None:
        """Edit text messages in-place; send a new message for photo/sticker messages."""
        if query.message and query.message.photo:
            await query.message.reply_text(text, **kwargs)
        else:
            try:
                await query.edit_message_text(text, **kwargs)
            except Exception:
                await query.message.reply_text(text, **kwargs)

    # "« Change source" — redisplay the picker
    if source == "back":
        search_query = context.user_data.get("pending_search", "")
        if not search_query:
            await query.answer("Session expired — run /search again.", show_alert=True)
            return
        keyboard = [
            [InlineKeyboardButton("⭐ HDHub4u",      callback_data="msrc_hdhub"),
             InlineKeyboardButton("🎬 4KHDHub",      callback_data="msrc_hdh")],
            [InlineKeyboardButton("🎥 MoviesDrive",  callback_data="msrc_md"),
             InlineKeyboardButton("🎞 HDMovie2",     callback_data="msrc_hdmovie2")],
            [InlineKeyboardButton("🌟 Vegamovies",   callback_data="msrc_vega"),
             InlineKeyboardButton("📀 SDMoviesPoint", callback_data="msrc_sdmp")],
            [InlineKeyboardButton("🎞 BollyFlix",    callback_data="msrc_bolly"),
             InlineKeyboardButton("🚜 MoviesMod",    callback_data="msrc_moviesmod")],
            [InlineKeyboardButton("🅰️ AtoZ Cinemas", callback_data="msrc_atoz"),
             InlineKeyboardButton("🎬 ZeeFliz",      callback_data="msrc_zeefliz")],
            [InlineKeyboardButton("🔍 All Sites",    callback_data="msrc_both")],
        ]
        await _src_edit(
            f"🔍 Search for <b>{search_query}</b>\n\nChoose where to search:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return

    search_query = context.user_data.get("pending_search", "")
    if not search_query:
        await query.answer("Session expired — run /search again.", show_alert=True)
        return

    source_label = {
        "hdhub": "HDHub4u", "hdh": "4KHDHub", "md": "MoviesDrive",
        "hdmovie2": "HDMovie2", "vega": "Vegamovies", "sdmp": "SDMoviesPoint",
        "bolly": "BollyFlix", "moviesmod": "MoviesMod", "atoz": "AtoZ Cinemas",
        "zeefliz": "ZeeFliz", "both": "All Sites",
    }.get(source, source)
    await _src_edit(
        f"🔍 Searching <b>{source_label}</b> for <b>{search_query}</b>…",
        parse_mode="HTML",
    )

    # Fetch from selected source(s)
    hdhub_results: list = []
    hdh_results:   list = []
    md_results:    list = []
    hdmovie2_results: list = []
    vega_results:  list = []
    sdmp_results:  list = []
    bolly_results: list = []
    moviesmod_results: list = []
    atoz_results: list = []
    zeefliz_results: list = []

    if source == "hdhub":
        hdhub_results = await asyncio.to_thread(hdhub_search, search_query, 10)
    elif source == "hdh":
        hdh_results = await asyncio.to_thread(hdh_search, search_query, 10)
    elif source == "md":
        md_results = await asyncio.to_thread(md_search, search_query, 10)
    elif source == "hdmovie2":
        hdmovie2_results = await asyncio.to_thread(hdmovie2_search, search_query, 10)
    elif source == "vega":
        vega_results = await asyncio.to_thread(vega_search, search_query, 10)
    elif source == "sdmp":
        sdmp_results = await asyncio.to_thread(sdmp_search, search_query, 10)
    elif source == "bolly":
        bolly_results = await asyncio.to_thread(bollyflix_search, search_query, 10)
    elif source == "moviesmod":
        moviesmod_results = await asyncio.to_thread(moviesmod_search, search_query, 10)
    elif source == "atoz":
        atoz_results = await asyncio.to_thread(atoz_search, search_query, 10)
    elif source == "zeefliz":
        zeefliz_results = await asyncio.to_thread(zeefliz_search, search_query, 10)
    else:  # all sites
        (
            hdhub_results, hdh_results, md_results, hdmovie2_results,
            vega_results, sdmp_results, bolly_results, moviesmod_results,
            atoz_results, zeefliz_results
        ) = await asyncio.gather(
            asyncio.to_thread(hdhub_search, search_query, 4),
            asyncio.to_thread(hdh_search,   search_query, 3),
            asyncio.to_thread(md_search,    search_query, 3),
            asyncio.to_thread(hdmovie2_search, search_query, 3),
            asyncio.to_thread(vega_search,  search_query, 3),
            asyncio.to_thread(sdmp_search,  search_query, 3),
            asyncio.to_thread(bollyflix_search, search_query, 3),
            asyncio.to_thread(moviesmod_search, search_query, 3),
            asyncio.to_thread(atoz_search, search_query, 3),
            asyncio.to_thread(zeefliz_search, search_query, 3),
        )

    if (not hdhub_results and not hdh_results and not md_results and not hdmovie2_results
            and not vega_results and not sdmp_results and not bolly_results
            and not moviesmod_results and not atoz_results and not zeefliz_results):
        await _src_edit(
            f"❌ No results found for <b>{search_query}</b> on {source_label}.",
            parse_mode="HTML",
        )
        return

    # Store results in user_data
    all_results = {f"hdhub_{i}": m for i, m in enumerate(hdhub_results)}
    all_results.update({f"hdh_{i}": m for i, m in enumerate(hdh_results)})
    all_results.update({f"md_{i}": m for i, m in enumerate(md_results)})
    all_results.update({f"hdmovie2_{i}": m for i, m in enumerate(hdmovie2_results)})
    all_results.update({f"vega_{i}": m for i, m in enumerate(vega_results)})
    all_results.update({f"sdmp_{i}": m for i, m in enumerate(sdmp_results)})
    all_results.update({f"bolly_{i}": m for i, m in enumerate(bolly_results)})
    all_results.update({f"moviesmod_{i}": m for i, m in enumerate(moviesmod_results)})
    all_results.update({f"atoz_{i}": m for i, m in enumerate(atoz_results)})
    all_results.update({f"zeefliz_{i}": m for i, m in enumerate(zeefliz_results)})
    context.user_data["search_results"] = all_results

    keyboard = []
    if hdh_results:
        keyboard.append([InlineKeyboardButton("━━ 4KHDHub ━━", callback_data="msearch_noop")])
        for i, m in enumerate(hdh_results):
            title = m["title"][:50] + "…" if len(m["title"]) > 50 else m["title"]
            keyboard.append([InlineKeyboardButton(f"🎬 {title}", callback_data=f"msres_hdh_{i}")])

    if md_results:
        keyboard.append([InlineKeyboardButton("━━ MoviesDrive ━━", callback_data="msearch_noop")])
        for i, m in enumerate(md_results):
            title = m["title"][:50] + "…" if len(m["title"]) > 50 else m["title"]
            keyboard.append([InlineKeyboardButton(f"🎥 {title}", callback_data=f"msres_md_{i}")])

    if hdmovie2_results:
        keyboard.append([InlineKeyboardButton("━━ HDMovie2 ━━", callback_data="msearch_noop")])
        for i, m in enumerate(hdmovie2_results):
            title = m["title"][:50] + "…" if len(m["title"]) > 50 else m["title"]
            keyboard.append([InlineKeyboardButton(f"🎞 {title}", callback_data=f"msres_hdmovie2_{i}")])

    if hdhub_results:
        keyboard.append([InlineKeyboardButton("━━ HDHub4u ━━", callback_data="msearch_noop")])
        for i, m in enumerate(hdhub_results):
            title = m["title"][:50] + "…" if len(m["title"]) > 50 else m["title"]
            keyboard.append([InlineKeyboardButton(f"⭐ {title}", callback_data=f"msres_hdhub_{i}")])

    if vega_results:
        keyboard.append([InlineKeyboardButton("━━ Vegamovies ━━", callback_data="msearch_noop")])
        for i, m in enumerate(vega_results):
            title = m["title"][:50] + "…" if len(m["title"]) > 50 else m["title"]
            keyboard.append([InlineKeyboardButton(f"🌟 {title}", callback_data=f"msres_vega_{i}")])

    if sdmp_results:
        keyboard.append([InlineKeyboardButton("━━ SDMoviesPoint ━━", callback_data="msearch_noop")])
        for i, m in enumerate(sdmp_results):
            title = m["title"][:50] + "…" if len(m["title"]) > 50 else m["title"]
            keyboard.append([InlineKeyboardButton(f"📀 {title}", callback_data=f"msres_sdmp_{i}")])

    if bolly_results:
        keyboard.append([InlineKeyboardButton("━━ BollyFlix ━━", callback_data="msearch_noop")])
        for i, m in enumerate(bolly_results):
            title = m["title"][:50] + "…" if len(m["title"]) > 50 else m["title"]
            keyboard.append([InlineKeyboardButton(f"🎞 {title}", callback_data=f"msres_bolly_{i}")])

    if moviesmod_results:
        keyboard.append([InlineKeyboardButton("━━ MoviesMod ━━", callback_data="msearch_noop")])
        for i, m in enumerate(moviesmod_results):
            title = m["title"][:50] + "…" if len(m["title"]) > 50 else m["title"]
            keyboard.append([InlineKeyboardButton(f"🚜 {title}", callback_data=f"msres_moviesmod_{i}")])

    if atoz_results:
        keyboard.append([InlineKeyboardButton("━━ AtoZ Cinemas ━━", callback_data="msearch_noop")])
        for i, m in enumerate(atoz_results):
            title = m["title"][:50] + "…" if len(m["title"]) > 50 else m["title"]
            keyboard.append([InlineKeyboardButton(f"🅰️ {title}", callback_data=f"msres_atoz_{i}")])

    if zeefliz_results:
        keyboard.append([InlineKeyboardButton("━━ ZeeFliz ━━", callback_data="msearch_noop")])
        for i, m in enumerate(zeefliz_results):
            title = m["title"][:50] + "…" if len(m["title"]) > 50 else m["title"]
            keyboard.append([InlineKeyboardButton(f"🎬 {title}", callback_data=f"msres_zeefliz_{i}")])

    # Post to channel + back
    keyboard.append([InlineKeyboardButton("📢 Post to Channel", callback_data="mpost_search")])
    keyboard.append([InlineKeyboardButton("« Change source", callback_data="msrc_back")])

    total = (
        len(hdhub_results) + len(hdh_results) + len(md_results) + len(hdmovie2_results)
        + len(vega_results) + len(sdmp_results) + len(bolly_results)
        + len(moviesmod_results) + len(atoz_results) + len(zeefliz_results)
    )
    await _src_edit(
        f"🔍 <b>{total} result{'s' if total != 1 else ''} for \"{search_query}\"</b>"
        f"  <i>({source_label})</i>\n\nTap a movie for download links:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def search_result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle search result button presses."""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    if str(update.effective_user.id) != str(MARKET_ALERT_CHAT_ID):
        await query.answer("⛔ You do not have permission.", show_alert=True)
        return

    data = query.data
    if data == "msearch_noop":
        await query.answer()
        return

    # msres_hdh_0 or msres_md_2
    parts = data.split("_")   # ["msres", "hdh", "0"]
    source = parts[1]
    idx = int(parts[2])
    key = f"{source}_{idx}"
    results = context.user_data.get("search_results", {})
    movie = results.get(key)

    if not movie:
        await query.answer("Session expired — search again.", show_alert=True)
        return

    await query.answer("Fetching links…")

    # ── For hdmovie2 / vega, show own detail without cross-site matching ───
    import re as _re
    def _title_words(t: str) -> set:
        return set(_re.sub(r"[^a-z0-9 ]", "", t.lower()).split()[:5])

    if source in ("hdmovie2", "vega", "sdmp", "hdhub", "bolly", "moviesmod", "atoz", "zeefliz"):
        # Send processing message and run scraping in background
        processing_msg = await query.message.reply_text(
            "⏳ Fetching download links... This may take a moment.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("« Cancel", callback_data="msrc_back")
            ]])
        )

        async def _do_scrape_search():
            try:
                if source == "hdhub":
                    detail = await asyncio.to_thread(hdhub_movie_links, movie["url"])
                    text = format_hdhub_message(movie["title"], detail)
                elif source == "vega":
                    detail = await asyncio.to_thread(vega_movie_links, movie["url"])
                    text = format_vega_message(movie["title"], detail)
                elif source == "sdmp":
                    detail = await asyncio.to_thread(sdmp_movie_links, movie["url"])
                    text = format_sdmp_message(movie["title"], detail)
                elif source == "bolly":
                    detail = await asyncio.to_thread(bollyflix_movie_links, movie["url"])
                    text = format_bollyflix_message(movie["title"], detail)
                elif source == "moviesmod":
                    detail = await asyncio.to_thread(moviesmod_movie_links, movie["url"])
                    text = format_moviesmod_message(movie["title"], detail)
                elif source == "atoz":
                    detail = await asyncio.to_thread(atoz_movie_links, movie["url"])
                    text = format_atoz_message(movie["title"], detail)
                elif source == "zeefliz":
                    detail = await asyncio.to_thread(zeefliz_movie_links, movie["url"])
                    text = format_zeefliz_message(movie["title"], detail)
                else:
                    detail = await asyncio.to_thread(hdmovie2_movie_links, movie["url"])
                    text = format_hdmovie2_message(movie["title"], detail)

                poster_url = detail.get("poster") or movie.get("poster", "")
                context.user_data[f"search_pair_{source}_{idx}"] = {
                    "primary": movie, "primary_source": source,
                    "other": None, "other_source": None,
                }
                post_cb = f"mpost_combined_{source}_{idx}"
                action_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 Post to Channel", callback_data=post_cb)],
                    [InlineKeyboardButton("« Back to results", callback_data="msrc_back")],
                ])

                # Send results
                try:
                    await processing_msg.delete()
                    if poster_url:
                        try:
                            await query.message.reply_photo(photo=poster_url)
                        except Exception:
                            pass
                    await reply_html_chunked(
                        query.message,
                        text or "❌ No download links found.",
                        disable_web_page_preview=True,
                        reply_markup=action_kb,
                    )
                except Exception as e:
                    log.error("Error sending search results: %s", e)
                    await processing_msg.edit_text(
                        "❌ Error displaying results. Please try again.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("« Back to results", callback_data="msrc_back")
                        ]])
                    )
            except asyncio.TimeoutError:
                await processing_msg.edit_text(
                    "⏱ Scraping timed out. The site may be slow or blocking requests.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("« Back to results", callback_data="msrc_back")
                    ]])
                )
            except Exception as e:
                log.error("Error scraping search result %s: %s", movie["url"], e)
                await processing_msg.edit_text(
                    f"❌ Error fetching links: {str(e)[:100]}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("« Back to results", callback_data="msrc_back")
                    ]])
                )

        # Run scraping in background task so bot stays responsive
        asyncio.create_task(_do_scrape_search())
        return

    # ── For hdh / md: try to find matching movie on the other site ────────
    other_source = "md" if source == "hdh" else "hdh"
    movie_words = _title_words(movie["title"])
    other_movie = None
    best_overlap = 0
    for k, v in results.items():
        if not k.startswith(other_source + "_"):
            continue
        overlap = len(movie_words & _title_words(v["title"]))
        if overlap > best_overlap:
            best_overlap = overlap
            other_movie = v
    if best_overlap < 2:
        other_movie = None

    async def fetch_hdh(url):
        return await asyncio.to_thread(hdh_movie_links, url)

    async def fetch_md(url):
        return await asyncio.to_thread(md_movie_links, url)

    async def _empty_hdh():
        return {"poster": "", "qualities": []}

    async def _empty_md():
        return {"poster": "", "links": []}

    if source == "hdh":
        hdh_detail, md_detail = await asyncio.gather(
            fetch_hdh(movie["url"]),
            fetch_md(other_movie["url"]) if other_movie else _empty_md(),
        )
    else:
        md_detail, hdh_detail = await asyncio.gather(
            fetch_md(movie["url"]),
            fetch_hdh(other_movie["url"]) if other_movie else _empty_hdh(),
        )

    # Build combined message
    poster_url = (
        hdh_detail.get("poster") or md_detail.get("poster")
        or movie.get("poster", "")
        or (other_movie.get("poster", "") if other_movie else "")
    )

    parts = [f"🎬 <b>{movie['title']}</b>\n"]

    hdh_links_text = format_hdh_message("", hdh_detail, footer=False)
    if hdh_links_text:
        parts.append("━━ <b>4KHDHub (4K/HDR)</b> ━━")
        parts.append(hdh_links_text)

    md_links_text = format_md_message("", md_detail, footer=False)
    if md_links_text:
        parts.append("━━ <b>MoviesDrive (480p–4K)</b> ━━")
        parts.append(md_links_text)

    if not hdh_links_text and not md_links_text:
        parts.append("❌ No download links found on either site.")

    parts.append("\n⚡ Powered by @CoursesDrivee")
    combined_text = "\n".join(parts)

    # Store the pair so post_to_channel_callback can post from both sources
    context.user_data[f"search_pair_{source}_{idx}"] = {
        "primary": movie,
        "primary_source": source,
        "other": other_movie,
        "other_source": other_source,
    }

    post_cb = f"mpost_combined_{source}_{idx}"
    back_cb = f"msrc_back"
    action_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Post to Channel", callback_data=post_cb)],
        [InlineKeyboardButton("« Back to results", callback_data=back_cb)],
    ])

    if poster_url:
        try:
            await query.message.reply_photo(photo=poster_url)
        except Exception:
            pass

    await reply_html_chunked(
        query.message,
        combined_text,
        disable_web_page_preview=True,
        reply_markup=action_kb,
    )


async def _post_movie_to_channel(bot, channel: str, movie: dict, source: str) -> str:
    """Fetch full download links for one movie and post poster + links to channel.
    Returns a status string for logging."""
    try:
        if source == "hdhub":
            detail = await asyncio.to_thread(hdhub_movie_links, movie["url"])
            text = format_hdhub_message(movie["title"], detail)
        elif source == "hdh":
            detail = await asyncio.to_thread(hdh_movie_links, movie["url"])
            text = format_hdh_message(movie["title"], detail)
        elif source == "md":
            detail = await asyncio.to_thread(md_movie_links, movie["url"])
            text = format_md_message(movie["title"], detail)
        elif source == "vega":
            detail = await asyncio.to_thread(vega_movie_links, movie["url"])
            text = format_vega_message(movie["title"], detail)
        elif source == "sdmp":
            detail = await asyncio.to_thread(sdmp_movie_links, movie["url"])
            text = format_sdmp_message(movie["title"], detail)
        elif source == "bolly":
            detail = await asyncio.to_thread(bollyflix_movie_links, movie["url"])
            text = format_bollyflix_message(movie["title"], detail)
        elif source == "moviesmod":
            detail = await asyncio.to_thread(moviesmod_movie_links, movie["url"])
            text = format_moviesmod_message(movie["title"], detail)
        elif source == "atoz":
            detail = await asyncio.to_thread(atoz_movie_links, movie["url"])
            text = format_atoz_message(movie["title"], detail)
        elif source == "zeefliz":
            detail = await asyncio.to_thread(zeefliz_movie_links, movie["url"])
            text = format_zeefliz_message(movie["title"], detail)
        else:  # hdmovie2 (default fallback)
            detail = await asyncio.to_thread(hdmovie2_movie_links, movie["url"])
            text = format_hdmovie2_message(movie["title"], detail)

        if not text:
            text = f"🎬 <b>{movie['title']}</b>\n\n❌ No download links found."

        poster_url = detail.get("poster") or movie.get("poster", "")

        if poster_url:
            try:
                if len(text) <= 1024:
                    await bot.send_photo(
                        chat_id=channel,
                        photo=poster_url,
                        caption=text,
                        parse_mode="HTML",
                    )
                else:
                    await bot.send_photo(chat_id=channel, photo=poster_url)
                    for chunk in split_html_for_telegram(text):
                        await bot.send_message(
                            chat_id=channel,
                            text=chunk,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
            except Exception:
                for chunk in split_html_for_telegram(text):
                    await bot.send_message(
                        chat_id=channel,
                        text=chunk,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
        else:
            for chunk in split_html_for_telegram(text):
                await bot.send_message(
                    chat_id=channel,
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        return f"✅ {movie['title'][:50]}"
    except Exception as e:
        return f"❌ {movie['title'][:40]}: {e}"


async def post_to_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch full download links for each movie result and post them to the channel."""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    if str(update.effective_user.id) != str(MARKET_ALERT_CHAT_ID):
        await query.answer("⛔ You do not have permission.", show_alert=True)
        return

    await query.answer()

    data = query.data
    channel = MOVIES_CHANNEL_ID
    if not channel:
        await query.message.reply_text("❌ MOVIES_CHANNEL_ID not configured in .env")
        return

    # ── Collect movies to post ────────────────────────────────────────────
    movies_to_post: list[tuple[dict, str]] = []   # (movie_dict, source)

    if data.startswith("mpost_single_"):
        # mpost_single_hdh_5 — one movie from /movies listing
        _, _, source, idx_str = data.split("_", 3)
        idx = int(idx_str)
        stored: dict = context.user_data.get(f"movies_{source}", {})
        movie = stored.get(idx)
        if not movie:
            await query.message.reply_text("❌ Session expired — browse the movie first.")
            return
        movies_to_post = [(movie, source)]

    elif data.startswith("mpost_combined_"):
        # mpost_combined_hdh_0 — combined result from /search (post from both sites)
        _, _, source, idx_str = data.split("_", 3)
        idx = int(idx_str)
        pair: dict = context.user_data.get(f"search_pair_{source}_{idx}", {})
        if not pair:
            # fallback: post only the primary movie
            results: dict = context.user_data.get("search_results", {})
            movie = results.get(f"{source}_{idx}")
            if not movie:
                await query.message.reply_text("❌ Session expired — search again.")
                return
            movies_to_post = [(movie, source)]
        else:
            primary = pair.get("primary")
            other   = pair.get("other")
            if primary:
                movies_to_post.append((primary, pair["primary_source"]))
            if other:
                movies_to_post.append((other, pair["other_source"]))

    elif data.startswith("mpost_list_"):
        _, _, source, page_str = data.split("_", 3)
        page = int(page_str)
        offset = (page - 1) * 10
        stored: dict = context.user_data.get(f"movies_{source}", {})
        movies_to_post = [
            (stored[offset + i], source)
            for i in range(10) if (offset + i) in stored
        ]
        if not movies_to_post:
            await query.message.reply_text("❌ No movies in session — browse first.")
            return

    elif data == "mpost_search":
        results: dict = context.user_data.get("search_results", {})
        if not results:
            await query.message.reply_text("❌ No search results in session.")
            return
        movies_to_post = []
        for k, m in sorted(results.items(), key=lambda x: x[0]):
            if k.startswith("hdhub_"):
                src = "hdhub"
            elif k.startswith("hdh_"):
                src = "hdh"
            elif k.startswith("md_"):
                src = "md"
            elif k.startswith("hdmovie2_"):
                src = "hdmovie2"
            elif k.startswith("vega_"):
                src = "vega"
            elif k.startswith("sdmp_"):
                src = "sdmp"
            elif k.startswith("bolly_"):
                src = "bolly"
            elif k.startswith("moviesmod_"):
                src = "moviesmod"
            elif k.startswith("atoz_"):
                src = "atoz"
            elif k.startswith("zeefliz_"):
                src = "zeefliz"
            else:
                src = "hdmovie2"
            movies_to_post.append((m, src))

    if not movies_to_post:
        return

    total = len(movies_to_post)
    status_msg = await query.message.reply_text(
        f"⏳ Posting {total} movie{'s' if total > 1 else ''} to channel…"
    )

    # ── Post each movie sequentially with a small gap ─────────────────────
    results_log = []
    for movie, source in movies_to_post:
        status = await _post_movie_to_channel(context.bot, channel, movie, source)
        results_log.append(status)
        await asyncio.sleep(1)   # avoid Telegram flood limits

    done = sum(1 for s in results_log if s.startswith("✅"))
    fail = total - done
    summary = f"✅ Posted {done}/{total} to channel."
    if fail:
        summary += f"\n⚠️ {fail} failed — check logs."
    await status_msg.edit_text(summary)


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Preview latest tech news; offer Post/Skip buttons. Owner only."""
    if not update.effective_message or not update.effective_user:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only feature. Use /enroll for Udemy courses.")
        return
        
    await update.effective_message.reply_text("Fetching tech news...")

    articles = await asyncio.to_thread(scrape_inshorts, 10, False)
    if not articles:
        await update.effective_message.reply_text("No articles found on Inshorts right now.")
        return

    text = format_news_post(articles)
    keyboard = [
        [
            InlineKeyboardButton("\u2705 Post to channel", callback_data="news_post"),
            InlineKeyboardButton("\u274C Skip", callback_data="news_skip"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    context.user_data["pending_news_articles"] = articles
    await update.effective_message.reply_html(text, reply_markup=reply_markup)


async def news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Post/Skip button press from /news preview."""
    query = update.callback_query
    if not query or not update.effective_user:
        return
        
    # Only allow the admin (MARKET_ALERT_CHAT_ID) to click the buttons
    if str(update.effective_user.id) != str(MARKET_ALERT_CHAT_ID):
        await query.answer("⛔ You do not have permission to do this.", show_alert=True)
        return
        
    await query.answer()

    if query.data == "news_post":
        articles = context.user_data.get("pending_news_articles")
        if not articles:
            await query.edit_message_reply_markup(reply_markup=None)
            return
        # Filter to only post unposted articles
        from news_service import is_news_posted
        fresh = [a for a in articles if not is_news_posted(a["title"])]
        if not fresh:
            await query.message.reply_text("These articles were already posted. Skipping.")
            await query.edit_message_reply_markup(reply_markup=None)
            return
        parts = format_news_posts(fresh)
        try:
            for part in parts:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID, text=part, parse_mode="HTML"
                )
                await asyncio.sleep(1)
            mark_news_posted([a["title"] for a in fresh])
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"\u2705 Posted {len(fresh)} new article(s) to channel!")
        except TelegramError as e:
            await query.message.reply_text(f"Failed: {e}")
    elif query.data == "news_skip":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Skipped.")

    context.user_data.pop("pending_news_articles", None)


def build_telegram_application() -> Application:
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("testdip", cmd_testdip))
    app.add_handler(CommandHandler("testalert", cmd_testalert))
    app.add_handler(CommandHandler("updateapi", cmd_updateapi))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("entry", cmd_entry))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("summaryweek", cmd_summaryweek))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("clearalert", cmd_clearalert))
    app.add_handler(CommandHandler("myalerts", cmd_myalerts))
    app.add_handler(CommandHandler("movies", cmd_movies))
    app.add_handler(CommandHandler("movietest", cmd_movietest))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CallbackQueryHandler(news_callback, pattern=r"^news_"))
    app.add_handler(CallbackQueryHandler(movie_callback, pattern=r"^m(site|back|pick|page)_|^mnoop$"))
    app.add_handler(CallbackQueryHandler(search_source_callback,   pattern=r"^msrc_"))
    app.add_handler(CallbackQueryHandler(search_result_callback,   pattern=r"^msres_|^msearch_noop$"))
    app.add_handler(CallbackQueryHandler(post_to_channel_callback, pattern=r"^mpost_"))
    # Multi-user Udemy enroller handlers
    app.add_handler(CommandHandler("enroll_setup", cmd_enroll_setup))
    app.add_handler(CommandHandler("set_token", cmd_set_token))
    app.add_handler(CommandHandler("enroll", cmd_enroll))
    app.add_handler(CommandHandler("enroll_status", cmd_enroll_status))
    app.add_handler(CommandHandler("myprofile", cmd_myprofile))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("autoenroll", cmd_autoenroll))
    app.add_handler(CallbackQueryHandler(enroll_callback, pattern=r"^enroll_"))
    app.add_handler(CallbackQueryHandler(setup_callback, pattern=r"^setup_"))
    app.add_handler(CallbackQueryHandler(profile_callback, pattern=r"^(start_setup|update_creds|clear_my_data|confirm_delete|cancel_delete|acc_toggle_|acc_remove_|autoenroll_toggle|show_accounts|toggle_channel_post|dl_select_|dl_next|dl_prev|dl_queue|dl_clear|dl_remove_|dl_start_)"))
    
    # Premium management commands (owner only)
    app.add_handler(CommandHandler("grant_premium", cmd_grant_premium))
    app.add_handler(CommandHandler("revoke_premium", cmd_revoke_premium))
    app.add_handler(CommandHandler("list_premium", cmd_list_premium))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("channel_post", cmd_channel_post))
    app.add_handler(CommandHandler("search_courses", cmd_search_courses))
    app.add_handler(CommandHandler("download_queue", cmd_download_queue))
    app.add_handler(CommandHandler("downloads", cmd_downloads))
    app.add_handler(CommandHandler("status", cmd_downloads))
    
    # Message handler for setup input (must be last to not interfere with commands)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_setup_message))
    return app


# ─── Database ────────────────────────────────────────────────────────────────

def should_reset_database() -> bool:
    """Check if database should be reset (older than 3 days)"""
    db_path = Path(DB_FILE)
    
    if not db_path.exists():
        return False
    
    # Get database file creation/modification time
    db_modified_time = datetime.fromtimestamp(db_path.stat().st_mtime)
    current_time = datetime.now()
    
    # Check if database is older than 3 days
    age = current_time - db_modified_time
    if age > timedelta(days=3):
        log.info(f"🗑️ Database is {age.days} days old, resetting...")
        return True
    
    log.info(f"📊 Database age: {age.days} days, {age.seconds // 3600} hours")
    return False


def reset_database():
    """Delete and recreate the database"""
    try:
        db_path = Path(DB_FILE)
        if db_path.exists():
            db_path.unlink()
            log.info("🗑️ Old database deleted")
        
        init_db()
        log.info("✨ Fresh database created")
    except Exception as e:
        log.error(f"❌ Error resetting database: {e}")


def init_db():
    """Create the SQLite DB and table on first run."""
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS posted_courses (
            id        TEXT PRIMARY KEY,
            name      TEXT,
            url       TEXT,
            posted_at TEXT DEFAULT (datetime('now'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            chat_id INTEGER PRIMARY KEY
        )
    """)
    con.commit()
    con.close()
    ensure_market_tables()
    from news_service import ensure_news_table
    ensure_news_table()
    log.info("✅ SQLite DB ready: %s", DB_FILE)


def is_posted(course_id) -> bool:
    con = sqlite3.connect(DB_FILE)
    row = con.execute(
        "SELECT 1 FROM posted_courses WHERE id = ?", (str(course_id),)
    ).fetchone()
    con.close()
    return row is not None


def mark_posted(course_id, name: str, url: str):
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "INSERT OR IGNORE INTO posted_courses (id, name, url) VALUES (?, ?, ?)",
        (str(course_id), name, url),
    )
    con.commit()
    con.close()


# ─── API ─────────────────────────────────────────────────────────────────────

def fetch_page(page: int, limit: int = 14) -> list[dict]:
    try:
        resp = requests.get(
            _get_endpoint(),
            params={"page": page, "limit": limit, "sortBy": "sale_start"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("items", [])
    except Exception as e:
        log.error("API error (page %d): %s", page, e)
        return []


def fetch_new_courses() -> list[dict]:
    """Fetch only page 1 and return new free courses in allowed languages"""
    new = []
    allowed_languages = ["english", "hindi", "urdu"]
    
    # Only fetch page 1 for quick refresh
    items = fetch_page(1, limit=20)  # Get more items from page 1
    
    if not items:
        log.info("❌ No items from API")
        return new

    # Filter for free courses only and not already posted
    for c in items:
        if is_posted(c["id"]):
            continue
        
        # Check if course is free
        try:
            sale_price = float(c.get("sale_price", 0) or 0)
        except (ValueError, TypeError):
            sale_price = 0
        
        # Check language
        lang = c.get("language", "").lower()
        
        # Only add if sale price is 0 (free) AND language is allowed
        if sale_price == 0 and lang in allowed_languages:
            new.append(c)
    
    log.info("📊 Page 1: %d total | %d new (free + allowed lang)", len(items), len(new))

    return new


# ─── Message formatter ───────────────────────────────────────────────────────

def format_message(c: dict) -> str:
    title = c.get("name", "Untitled")
    description = c.get("description", "")
    
    # Clean description - remove HTML tags and decode entities
    if description:
        # Replace unicode escapes
        description = description.replace('\\u003c', '<').replace('\\u003e', '>')
        description = description.replace('\\u003cbr\\u003e', ' ')
        description = description.replace('\u003c', '<').replace('\u003e', '>')
        description = description.replace('\u003cbr\u003e', ' ')
        
        # Remove HTML tags
        description = re.sub(r'<[^>]+>', '', description)
        
        # Decode common HTML entities
        description = description.replace('&nbsp;', ' ')
        description = description.replace('&amp;', '&')
        description = description.replace('&lt;', '<')
        description = description.replace('&gt;', '>')
        description = description.replace('&quot;', '"')
        
        # Clean up whitespace
        description = ' '.join(description.split())
        
        # Truncate if too long
        if len(description) > 180:
            description = description[:180] + "..."
    
    # Convert to float, handle string values
    try:
        price = float(c.get("price", 0) or 0)
    except (ValueError, TypeError):
        price = 0
    
    try:
        sale = float(c.get("sale_price", 0) or 0)
    except (ValueError, TypeError):
        sale = 0
    
    try:
        rating = float(c.get("rating", 0) or 0)
    except (ValueError, TypeError):
        rating = 0
    
    try:
        views = int(c.get("views", 0) or 0)
    except (ValueError, TypeError):
        views = 0
    
    try:
        lectures = int(c.get("lectures", 0) or 0)
    except (ValueError, TypeError):
        lectures = 0
    
    lang = c.get("language", "")
    category = c.get("category", "")
    subcategory = c.get("subcategory", "")
    store = c.get("store", "")

    # Build professional message with attractive header
    message = "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    message += "🎓 <b>FREE COURSE ALERT!</b> 🎓\n"
    message += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Course Title - Make it engaging and attractive
    message += f"📚 <b><u>{html.escape(title)}</u></b>\n\n"
    
    # Description with spoiler effect (tap to reveal)
    if description:
        message += f"📄 <b>What You'll Learn:</b>\n<tg-spoiler>{html.escape(description)}</tg-spoiler>\n\n"
    
    # Divider
    message += "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
    
    # Price Section
    if price > 0 and sale == 0:
        message += f"💰 <b>Original Price:</b> <s>${price:.2f}</s>\n"
        message += f"✅ <b>Current Price:</b> <b>FREE</b>\n"
        message += f"🎁 <b>You Save:</b> ${price:.2f} (100% OFF)\n\n"
    elif sale > 0 and sale < price:
        discount = int(100 - (sale / price * 100))
        savings = price - sale
        message += f"💰 <b>Original Price:</b> <s>${price:.2f}</s>\n"
        message += f"✅ <b>Current Price:</b> ${sale:.2f}\n"
        message += f"🎁 <b>You Save:</b> ${savings:.2f} ({discount}% OFF)\n\n"
    else:
        message += f"✅ <b>Price:</b> FREE\n\n"
    
    # Course Information
    message += "📚 <b>Course Information:</b>\n"
    
    if rating:
        stars = "⭐" * int(rating)
        message += f"  • Rating: {rating:.1f}/5.0 {stars}\n"
    
    if lectures:
        message += f"  • Total Lectures: {lectures}\n"
    
    if views:
        message += f"  • Enrolled Students: {views:,}\n"
    
    if lang:
        message += f"  • Language: {lang}\n"
    
    if category:
        message += f"  • Category: {category}"
        if subcategory:
            message += f" → {subcategory}"
        message += "\n"
    
    if store:
        message += f"  • Platform: {store}\n"
    
    message += "\n▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
    
    # Call to action
    message += "⚡ <b>Limited Time Offer - Enroll Now!</b> ⚡"

    return message


# ─── Poster ──────────────────────────────────────────────────────────────────

async def post_course(bot: Bot, course: dict):
    cid   = course["id"]
    name  = course.get("name", "")
    url   = course.get("url", "")
    image = course.get("image", "")
    text  = format_message(course)

    # Create inline keyboard with enroll button
    keyboard = [
        [InlineKeyboardButton("🎓 ENROLL NOW - FREE! 🎓", url=url)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if image:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=False,
                reply_markup=reply_markup,
            )
        mark_posted(cid, name, url)
        log.info("✅ Posted: [%s] %s", cid, name[:70])

    except TelegramError as e:
        log.error("❌ Telegram error [%s]: %s", cid, e)
    except Exception as e:
        log.error("❌ Unexpected error [%s]: %s", cid, e)


# ─── Health Check Server ─────────────────────────────────────────────────────

async def health_check(_request):
    """Health check endpoint for Render"""
    return web.Response(text="Bot is running! ✅", status=200)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Market dip tracker</title>
  <style>
    :root { --bg:#0f1419; --card:#1a2332; --text:#e7ecf3; --muted:#8b9cb3; --down:#f85149; --up:#3fb950; }
    * { box-sizing: border-box; }
    body { font-family: system-ui, Segoe UI, sans-serif; background: var(--bg); color: var(--text);
      margin: 0; padding: 1.25rem; line-height: 1.45; }
    h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.25rem; }
    p.sub { color: var(--muted); font-size: 0.875rem; margin: 0 0 1rem; }
    .grid { display: grid; gap: 0.75rem; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); }
    .card { background: var(--card); border-radius: 10px; padding: 1rem; border: 1px solid #263041; }
    .name { font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
    .pct { font-size: 1.5rem; font-weight: 700; margin-top: 0.35rem; }
    .pct.down { color: var(--down); } .pct.up { color: var(--up); }
    .meta { font-size: 0.8rem; color: var(--muted); margin-top: 0.5rem; }
    footer { margin-top: 1.5rem; font-size: 0.75rem; color: var(--muted); max-width: 42rem; }
    a { color: #58a6ff; }
    h2 { font-size: 1.05rem; margin: 1.25rem 0 0.5rem; font-weight: 600; }
    .alert-yes { color: var(--down); font-weight: 600; }
    .alert-wait { color: var(--muted); }
  </style>
</head>
<body>
  <h1>Indian indices</h1>
  <p class="sub">Vs previous session close (delayed Yahoo data). Not financial advice.</p>
  <div id="grid" class="grid"></div>
  <p class="sub" id="status">Loading…</p>
  <h2>Dip alert logic (same as the bot)</h2>
  <p class="sub" id="dipRule"></p>
  <div id="dipPanel" class="grid"></div>
  <footer>
    Telegram: <code>/market</code> for this snapshot + would-it-fire. JSON: <code>/api/dip-status</code>.
    Backtest: <code>/api/backtest?ticker=^NSEI&amp;start=2015-01-01&amp;amount=5000&amp;dip=1</code>
  </footer>
  <script>
    async function load() {
      const st = document.getElementById('status');
      const grid = document.getElementById('grid');
      try {
        const r = await fetch('/api/market');
        const j = await r.json();
        st.textContent = 'Updated ' + (j.as_of || '');
        grid.innerHTML = '';
        (j.quotes || []).forEach(q => {
          const pct = q.pct_change;
          const div = document.createElement('div');
          div.className = 'card';
          const cls = pct < 0 ? 'down' : (pct > 0 ? 'up' : '');
          div.innerHTML = '<div class="name">' + escapeHtml(q.name) + '</div>' +
            '<div class="pct ' + cls + '">' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%</div>' +
            '<div class="meta">Last ' + q.last + ' · Prev close ' + q.previous_close + '</div>';
          grid.appendChild(div);
        });
      } catch (e) {
        st.textContent = 'Failed to load';
      }
    }
    async function loadDip() {
      const rule = document.getElementById('dipRule');
      const dipPanel = document.getElementById('dipPanel');
      try {
        const r = await fetch('/api/dip-status');
        const d = await r.json();
        rule.textContent = d.dip_rule_plain + ' · IST ' + d.calendar_day_ist;
        dipPanel.innerHTML = '';
        (d.instruments || []).forEach(i => {
          const div = document.createElement('div');
          div.className = 'card';
          let line = '';
          if (i.would_send_telegram_now) line = '<span class="alert-yes">Would ALERT on next poll tick</span>';
          else if (i.already_alerted_today_ist && i.condition_pct_vs_prev_close_lte_neg_threshold)
            line = '<span class="alert-wait">Dip met · already messaged today</span>';
          else line = '<span class="alert-wait">No alert · ~' + i.percent_points_more_decline_to_hit_threshold.toFixed(2) + ' pts more decline to threshold</span>';
          div.innerHTML = '<div class="name">' + escapeHtml(i.name) + '</div>' +
            '<div class="pct">' + (i.pct_change_vs_prev_close >= 0 ? '+' : '') + i.pct_change_vs_prev_close.toFixed(3) + '% vs prev close</div>' +
            '<div class="meta">' + line + '</div>';
          dipPanel.appendChild(div);
        });
      } catch (e) {
        rule.textContent = 'Could not load dip-status';
      }
    }
    function escapeHtml(s) {
      const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
    }
    load();
    loadDip();
    setInterval(load, 60000);
    setInterval(loadDip, 60000);
  </script>
</body>
</html>
"""


async def dashboard_page(_request):
    return web.Response(text=DASHBOARD_HTML, content_type="text/html", charset="utf-8")


async def api_market(_request):
    quotes = await fetch_all_snapshots_async()
    return web.json_response(
        {"as_of": datetime.utcnow().isoformat() + "Z", "quotes": quotes}
    )


async def api_dip_status(request: web.Request) -> web.Response:
    """Live-style snapshot + whether each symbol would trigger a dip alert now."""
    th: float | None = None
    raw_th = request.query.get("threshold")
    if raw_th is not None and raw_th.strip() != "":
        try:
            th = float(raw_th)
        except ValueError:
            return web.json_response({"error": "invalid threshold"}, status=400)
        if th <= 0 or th > 50:
            return web.json_response({"error": "threshold out of range"}, status=400)
    data = await build_dip_status_async(th)
    return web.json_response(data)


async def api_backtest(request):
    raw_ticker = request.query.get("ticker", "^NSEI")
    if not re.match(r"^[A-Za-z0-9^.\-]+$", raw_ticker):
        return web.json_response({"error": "invalid ticker"}, status=400)
    start = request.query.get("start", "2015-01-01")
    try:
        amount = float(request.query.get("amount", "5000"))
        dip = float(request.query.get("dip", "1"))
    except ValueError:
        return web.json_response({"error": "invalid amount or dip"}, status=400)
    if amount <= 0 or dip <= 0 or amount > 1e9:
        return web.json_response({"error": "amount/dip out of range"}, status=400)

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: run_backtest(raw_ticker, start, amount, dip)
    )
    if result is None:
        return web.json_response({"error": "no historical data"}, status=404)
    return web.json_response(result.to_dict())


async def api_test_alert(request: web.Request) -> web.Response:
    """Send sample dip alert to MARKET_ALERT_CHAT_ID. Requires TEST_ALERT_SECRET."""
    if not TEST_ALERT_SECRET:
        return web.json_response(
            {
                "error": "disabled",
                "hint": "Set TEST_ALERT_SECRET in the environment to enable this endpoint.",
            },
            status=403,
        )
    if request.query.get("secret") != TEST_ALERT_SECRET:
        return web.json_response({"error": "forbidden"}, status=403)

    bot = request.app.get("telegram_bot")
    if bot is None:
        return web.json_response({"error": "bot not attached yet"}, status=503)

    text = format_test_dip_alert(threshold=DIP_THRESHOLD_PERCENT)
    try:
        await bot.send_message(chat_id=MARKET_ALERT_CHAT_ID, text=text)
    except TelegramError as e:
        return web.json_response({"error": "telegram_failed", "detail": str(e)}, status=502)
    return web.json_response(
        {
            "ok": True,
            "sent_to": MARKET_ALERT_CHAT_ID,
            "preview": "Same template as a real dip alert, with a TEST banner.",
        }
    )


def _movies_api_limit(raw: str | None, *, default: int, maximum: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("limit must be an integer")
    if value < 1 or value > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


async def api_movies_search(request: web.Request) -> web.Response:
    """Search HDHub4u + 4KHDHub + MoviesDrive (titles and page URLs only)."""
    query = (request.query.get("q") or "").strip()
    if not query:
        return web.json_response({"error": "q required"}, status=400)
    try:
        limit = _movies_api_limit(request.query.get("limit"), default=5, maximum=20)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    results = await asyncio.to_thread(movies_search_combined, query, limit)
    public = [
        {"source": r["source"], "title": r["title"], "page_url": r["page_url"]}
        for r in results
    ]
    return web.json_response({"query": query, "count": len(public), "results": public})


async def api_movies_latest(request: web.Request) -> web.Response:
    """Latest movies from all three sources (no posters)."""
    try:
        page = max(1, int(request.query.get("page", "1")))
        limit = _movies_api_limit(request.query.get("limit"), default=10, maximum=20)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    results = await asyncio.to_thread(movies_latest_combined, page, limit)
    public = [
        {"source": r["source"], "title": r["title"], "page_url": r["page_url"]}
        for r in results
    ]
    return web.json_response({"page": page, "count": len(public), "results": public})


async def api_movies_links(request: web.Request) -> web.Response:
    """Download links only for one movie page (no poster / size / audio)."""
    page_url = (request.query.get("url") or "").strip()
    source = (request.query.get("source") or "").strip()
    if not page_url or not source:
        return web.json_response(
            {"error": "url and source required (hdhub, hdh, or md)"},
            status=400,
        )
    try:
        data = await asyncio.to_thread(movie_page_download_links, source, page_url)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        log.exception("api_movies_links failed")
        return web.json_response({"error": "scrape_failed", "detail": str(exc)}, status=502)
    return web.json_response(data)


async def api_movies(request: web.Request) -> web.Response:
    """Search all three sites and return download links for each result."""
    query = (request.query.get("q") or "").strip()
    if not query:
        return web.json_response({"error": "q required"}, status=400)
    try:
        limit = _movies_api_limit(request.query.get("limit"), default=3, maximum=10)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    data = await asyncio.to_thread(movies_aggregate_links, query, limit)
    return web.json_response(data)


def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/api/market", api_market)
    app.router.add_get("/api/dip-status", api_dip_status)
    app.router.add_get("/api/backtest", api_backtest)
    app.router.add_get("/api/test-alert", api_test_alert)
    app.router.add_get("/api/movies", api_movies)
    app.router.add_get("/api/movies/search", api_movies_search)
    app.router.add_get("/api/movies/latest", api_movies_latest)
    app.router.add_get("/api/movies/links", api_movies_links)
    app.router.add_get("/", dashboard_page)
    return app


async def start_http_server(app: web.Application):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(
        "🌐 HTTP server on port %s (/, /health, /dashboard, /api/market, /api/movies, …)",
        PORT,
    )


# ─── Main loop ───────────────────────────────────────────────────────────────

async def run_news_autopost(bot: Bot):
    """Auto-post tech news to CHANNEL_ID at configured IST hours (default 10 AM, 10 PM).
    Also scrapes every 2 hours to queue articles so nothing is missed."""
    posted_today: set[int] = set()
    last_post_day = None
    last_scrape_hour = -1

    while True:
        try:
            from zoneinfo import ZoneInfo
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        except Exception:
            now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)

        current_hour = now_ist.hour
        current_day = now_ist.date()

        # Reset tracking at midnight (new day)
        if last_post_day is not None and current_day != last_post_day:
            posted_today.clear()
            await asyncio.to_thread(cleanup_old_news, 7)
        last_post_day = current_day

        # Background scrape every 2 hours to capture new articles into queue
        scrape_due = (current_hour % 2 == 0) and (current_hour != last_scrape_hour)
        if scrape_due:
            try:
                count = await asyncio.to_thread(scrape_and_queue)
                if count:
                    log.info("📰 Background scrape: queued %d new articles", count)
                last_scrape_hour = current_hour
            except Exception as e:
                log.error("📰 Background scrape failed: %s", e)

        # Post at configured hours
        if current_hour in NEWS_POST_HOURS and current_hour not in posted_today:
            log.info("📰 News auto-post triggered (IST %02d:xx)", current_hour)
            articles = await asyncio.to_thread(get_fresh_articles_for_posting, 10)
            if articles:
                parts = format_news_posts(articles)
                try:
                    for part in parts:
                        await bot.send_message(
                            chat_id=CHANNEL_ID, text=part, parse_mode="HTML"
                        )
                        await asyncio.sleep(1)
                    mark_news_posted([a["title"] for a in articles])
                    clear_queue()
                    log.info("📰 News posted: %d articles (%d msgs)", len(articles), len(parts))
                except TelegramError as e:
                    log.error("📰 News post failed: %s", e)
            else:
                log.info("📰 No new articles for auto-post")
                posted_today.add(current_hour)

        await asyncio.sleep(120)


async def run_course_loop(bot: Bot):
    """Poll courses and post to CHANNEL_ID."""
    from user_enroller import is_channel_posting_enabled
    last_db_check = datetime.now()

    while True:
        if (datetime.now() - last_db_check) > timedelta(hours=6):
            if should_reset_database():
                log.info("🔄 Resetting database during runtime...")
                reset_database()
            last_db_check = datetime.now()
        
        # Check if channel posting is enabled
        if not is_channel_posting_enabled():
            log.info("📢 Channel posting disabled, skipping...")
            await asyncio.sleep(CHECK_EVERY)
            continue
        
        log.info("─── 🔍 Checking for new courses ───")
        new_courses = fetch_new_courses()

        if new_courses:
            log.info("📬 %d new course(s) to post.", len(new_courses))
            for course in new_courses:
                await post_course(bot, course)
                await asyncio.sleep(2)
        else:
            log.info("💤 No new courses.")

        log.info("⏱  Sleeping %ds until next check.\n", CHECK_EVERY)
        await asyncio.sleep(CHECK_EVERY)


async def main():
    """HTTP server + course bot + optional market dip alerts + /start welcome."""
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is missing; set it in the environment.")
        raise SystemExit(1)

    if should_reset_database():
        reset_database()
    else:
        init_db()
    ensure_news_table()
    
    # Initialize bot pool for parallel downloads/uploads
    num_bots = await init_bot_pool()
    if num_bots > 0:
        log.info(f"✅ Bot pool initialized with {num_bots} additional bot(s) for parallel operations")
    else:
        log.info("⚠️ No additional bots configured (UPLOAD_BOT_TOKENS not set). Using single-bot mode.")

    app = create_web_app()
    await start_http_server(app)

    tg_app = build_telegram_application()
    for attempt in range(1, 6):
        try:
            log.info("Connecting to Telegram API (attempt %d/5)...", attempt)
            await tg_app.initialize()
            break
        except TelegramError as e:
            log.warning("Telegram connect attempt %d failed: %s", attempt, e)
            if attempt == 5:
                raise
            await asyncio.sleep(3 * attempt)
    await tg_app.start()
    await tg_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    bot = tg_app.bot
    app["telegram_bot"] = bot
    me = await bot.get_me()
    log.info("🤖 Bot started: @%s (polling /start, /testdip, /testalert, /market, /news)", me.username)

    tasks = [
        asyncio.create_task(run_course_loop(bot)),
        asyncio.create_task(run_news_autopost(bot)),
        asyncio.create_task(auto_enroll_job(tg_app)),
    ]
    if MARKET_FEATURES_ENABLED:
        log.info(
            "📉 Market alerts enabled → %s (dip ≥ %s%%)",
            MARKET_ALERT_CHAT_ID,
            DIP_THRESHOLD_PERCENT,
        )
        tasks.append(
            asyncio.create_task(
                run_market_monitor(bot, MARKET_ALERT_CHAT_ID)
            )
        )
        ensure_fno_tables()
        from fno_storage import storage_backend_label
        log.info("📊 F&O scalp monitor enabled (Confluence + ORB + PCR + MACD MTF + EOD summary)")
        log.info("📊 F&O trade history storage: %s", storage_backend_label())
        tasks.append(
            asyncio.create_task(run_fno_monitor(bot))
        )
        tasks.append(
            asyncio.create_task(run_fno_exit_monitor(bot))
        )
        tasks.append(
            asyncio.create_task(run_fno_eod_summary(bot))
        )
    else:
        log.info("Market features off (MARKET_FEATURES_ENABLED).")

    try:
        await asyncio.gather(*tasks)
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
