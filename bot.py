"""
Telegram Course Bot — SQLite Edition
Fetches courses from real.discount API and posts new ones to a Telegram channel.
Duplicate prevention via local SQLite database (posted_courses.db).
"""

import os
import asyncio
import logging
import sqlite3
import html
import re
import requests
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

# ─── Load env ────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN  = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")


_X = [104,116,116,112,115,58,47,47,99,100,110,46,114,101,97,108,46,100,105,115,99,111,117,110,116,47,97,112,105,47,99,111,117,114,115,101,115]
def _get_endpoint(): return ''.join(chr(c) for c in _X)

DB_FILE     = "posted_courses.db"
CHECK_EVERY = 180   # seconds between polls (3 min)

# ─── Logging ──────────────────────────────────────────────────────────━━━──────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ─── Database ────────────────────────────────────────────────────────────────

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
    con.commit()
    con.close()
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


# ─── Message formatter ────────────────────━━━───────────────────────────────────

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


# ─── Poster ───────────────────────────────────────────────────────────────────

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


# ─── Main loop ────────────────────────────────────────────────────────────────

async def run():
    init_db()

    bot = Bot(token=BOT_TOKEN)
    me  = await bot.get_me()
    log.info("🤖 Bot started: @%s", me.username)

    while True:
        log.info("─── 🔍 Checking for new courses ───")
        new_courses = fetch_new_courses()

        if new_courses:
            log.info("📬 %d new course(s) to post.", len(new_courses))
            for course in new_courses:
                await post_course(bot, course)
                await asyncio.sleep(2)      # small gap between posts
        else:
            log.info("💤 No new courses.")

        log.info("⏱  Sleeping %ds until next check.\n", CHECK_EVERY)
        await asyncio.sleep(CHECK_EVERY)


if __name__ == "__main__":
    asyncio.run(run())
