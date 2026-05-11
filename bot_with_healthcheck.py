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
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
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
from news_service import (
    ensure_news_table,
    scrape_inshorts,
    get_fresh_articles_for_posting,
    format_news_post,
    format_news_posts,
    mark_news_posted,
)

# ─── Load env ────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN  = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
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


# ─── /start welcome ───────────────────────────────────────────────────────────

WELCOME_HTML = """<b>Welcome!</b> 👋

Here's what I can do:

• <b>Course alerts</b> — free-course picks posted to the channel on a timer.

• <b>Market dip heads-up</b> — alerts when Nifty 50, Sensex, or Nifty BeES fall by your dip threshold vs previous close.

• <b>Tech news</b> — latest tech headlines auto-posted daily at 10 AM &amp; 10 PM IST.

<b>Commands:</b>
/start — this menu
/news — preview latest tech news (Post / Skip)
/market — live market snapshot + dip status
/testdip — sample dip alert (not real data)
/testalert — check if dip alerts can reach their destination

<i>Market data is unofficial/delayed (Yahoo). Not financial advice.</i>

⚡ Powered by <a href="https://t.me/CoursesDrivee">@CoursesDrivee</a>"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat:
        return
        
    # Save user to DB so they receive market alerts
    chat_id = update.effective_chat.id
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute("INSERT OR IGNORE INTO bot_users (chat_id) VALUES (?)", (chat_id,))
        con.commit()
    except Exception as e:
        log.error("Failed to save user %s: %s", chat_id, e)
    finally:
        con.close()
        
    await update.effective_message.reply_html(WELCOME_HTML)


async def cmd_testdip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a sample dip alert to this chat (same template as live alerts)."""
    if not update.effective_message:
        return
    text = format_test_dip_alert(threshold=DIP_THRESHOLD_PERCENT)
    await update.effective_message.reply_text(text)


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch Yahoo snapshot now and explain dip rule vs your DIP_THRESHOLD_PERCENT."""
    if not update.effective_message:
        return
    status = await build_dip_status_async()
    await update.effective_message.reply_text(format_dip_status_telegram(status))


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
    """Diagnose dip alert delivery: tries to send a test message to MARKET_ALERT_CHAT_ID."""
    if not update.effective_message:
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


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Preview latest tech news; offer Post/Skip buttons."""
    if not update.effective_message:
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
    if not query:
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
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("testdip", cmd_testdip))
    app.add_handler(CommandHandler("testalert", cmd_testalert))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CallbackQueryHandler(news_callback, pattern=r"^news_"))
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


def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/api/market", api_market)
    app.router.add_get("/api/dip-status", api_dip_status)
    app.router.add_get("/api/backtest", api_backtest)
    app.router.add_get("/api/test-alert", api_test_alert)
    app.router.add_get("/", dashboard_page)
    return app


async def start_http_server(app: web.Application):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(
        "🌐 HTTP server on port %s (/, /health, /dashboard, /api/market, /api/dip-status, …)",
        PORT,
    )


# ─── Main loop ───────────────────────────────────────────────────────────────

async def run_news_autopost(bot: Bot):
    """Auto-post tech news to CHANNEL_ID at configured IST hours (default 10 AM, 10 PM)."""
    posted_today: set[int] = set()

    while True:
        try:
            from zoneinfo import ZoneInfo
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        except Exception:
            now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)

        current_hour = now_ist.hour
        current_day = now_ist.date()

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
                    log.info("📰 News posted: %d articles (%d msgs)", len(articles), len(parts))
                except TelegramError as e:
                    log.error("📰 News post failed: %s", e)
            else:
                log.info("📰 No new articles for auto-post")
            posted_today.add(current_hour)

        # Reset tracking at midnight
        check_day = now_ist.date() if hasattr(now_ist, "date") else current_day
        if check_day != current_day:
            posted_today.clear()

        await asyncio.sleep(120)


async def run_course_loop(bot: Bot):
    """Poll courses and post to CHANNEL_ID."""
    last_db_check = datetime.now()

    while True:
        if (datetime.now() - last_db_check) > timedelta(hours=6):
            if should_reset_database():
                log.info("🔄 Resetting database during runtime...")
                reset_database()
            last_db_check = datetime.now()

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

    app = create_web_app()
    await start_http_server(app)

    tg_app = build_telegram_application()
    await tg_app.initialize()
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
