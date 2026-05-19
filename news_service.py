"""
Tech news scraper from Inshorts.
Fetches articles, filters ads/sponsored, deduplicates, formats for Telegram.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    _CFFI_AVAILABLE = True
except ImportError:
    _CFFI_AVAILABLE = False

log = logging.getLogger(__name__)

DB_FILE = os.getenv("DB_FILE", "posted_courses.db")
INSHORTS_URL = "https://inshorts.com/en/read/technology"

SPAM_KEYWORDS = [
    "flipkart",
    "amazon sale",
    "best deals",
    "oneblade",
    "brand ambassador",
    "sponsored",
    "buy now",
    "discount",
    "offer",
    "coupon",
    "sale starting",
    "checkout now",
]


def ensure_news_table():
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS posted_news (
                hash TEXT PRIMARY KEY,
                title TEXT,
                posted_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Queued articles scraped between post cycles
        con.execute("""
            CREATE TABLE IF NOT EXISTS news_queue (
                hash TEXT PRIMARY KEY,
                title TEXT,
                summary TEXT,
                scraped_at TEXT DEFAULT (datetime('now'))
            )
        """)
        con.commit()
    finally:
        con.close()


def cleanup_old_news(days: int = 7):
    """Remove posted_news entries older than N days to keep DB lean."""
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute(
            "DELETE FROM posted_news WHERE posted_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        con.commit()
    finally:
        con.close()


def _hash_title(title: str) -> str:
    return hashlib.md5(title.strip().lower().encode()).hexdigest()


def is_news_posted(title: str) -> bool:
    con = sqlite3.connect(DB_FILE)
    try:
        row = con.execute(
            "SELECT 1 FROM posted_news WHERE hash = ?", (_hash_title(title),)
        ).fetchone()
        return row is not None
    finally:
        con.close()


def mark_news_posted(titles: list[str]):
    con = sqlite3.connect(DB_FILE)
    try:
        for t in titles:
            h = _hash_title(t)
            con.execute(
                "INSERT OR IGNORE INTO posted_news (hash, title) VALUES (?, ?)",
                (h, t[:200]),
            )
            # Remove from queue if present
            con.execute("DELETE FROM news_queue WHERE hash = ?", (h,))
        con.commit()
    finally:
        con.close()


def queue_articles(articles: list[dict[str, str]]):
    """Store freshly scraped articles in queue for the next post cycle."""
    con = sqlite3.connect(DB_FILE)
    try:
        for a in articles:
            h = _hash_title(a["title"])
            con.execute(
                "INSERT OR IGNORE INTO news_queue (hash, title, summary) VALUES (?, ?, ?)",
                (h, a["title"][:200], a.get("summary", "")[:500]),
            )
        con.commit()
    finally:
        con.close()


def get_queued_articles() -> list[dict[str, str]]:
    """Get all queued articles that haven't been posted yet."""
    con = sqlite3.connect(DB_FILE)
    try:
        rows = con.execute(
            """SELECT q.title, q.summary FROM news_queue q
               WHERE q.hash NOT IN (SELECT hash FROM posted_news)
               ORDER BY q.scraped_at DESC"""
        ).fetchall()
        return [{"title": r[0], "summary": r[1]} for r in rows]
    finally:
        con.close()


def clear_queue():
    """Clear the news queue after posting."""
    con = sqlite3.connect(DB_FILE)
    try:
        con.execute("DELETE FROM news_queue")
        con.commit()
    finally:
        con.close()


def _is_spam(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(kw in combined for kw in SPAM_KEYWORDS)


def scrape_inshorts(min_articles: int = 10, skip_posted: bool = True) -> list[dict[str, str]]:
    """
    Scrape Inshorts tech page. Returns list of {title, summary}.
    Filters spam. If skip_posted=True, also filters already-posted articles.
    Uses curl_cffi for fresh content (bypasses caching/bot detection).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
    }
    articles: list[dict[str, str]] = []
    seen_titles: set[str] = set()

    # Add cache-buster to URL
    cache_bust_url = f"{INSHORTS_URL}?t={int(time.time())}"

    try:
        if _CFFI_AVAILABLE:
            resp = cffi_requests.get(cache_bust_url, headers=headers, timeout=15, impersonate="chrome")
        else:
            resp = requests.get(cache_bust_url, headers=headers, timeout=15)
        if resp.status_code != 200:
            log.warning("Inshorts returned HTTP %s", resp.status_code)
            return []
    except Exception as e:
        log.error("Inshorts fetch failed: %s", e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.find_all("div", {"itemtype": "http://schema.org/NewsArticle"})

    if not cards:
        log.warning("Inshorts: no NewsArticle cards found (page may have changed)")
        return []

    for card in cards:
        headline_tag = card.find(itemprop="headline")
        title = headline_tag.get_text(strip=True) if headline_tag else ""
        if not title:
            continue

        desc_tag = card.find(itemprop="articleBody")
        desc = desc_tag.get_text(strip=True) if desc_tag else ""

        if _is_spam(title, desc):
            continue
        if skip_posted and is_news_posted(title):
            continue
        if title.lower() in seen_titles:
            continue

        seen_titles.add(title.lower())
        articles.append({"title": title, "summary": desc})

        if len(articles) >= min_articles:
            break

    return articles


def scrape_and_queue() -> int:
    """Scrape Inshorts and add any new articles to the queue. Returns count of new articles."""
    articles = scrape_inshorts(min_articles=10, skip_posted=True)
    if articles:
        queue_articles(articles)
        log.info("Queued %d new articles from Inshorts", len(articles))
    return len(articles)


def get_fresh_articles_for_posting(min_articles: int = 10) -> list[dict[str, str]]:
    """Get unposted articles: live scrape + previously queued ones."""
    # First, do a fresh scrape
    live_articles = scrape_inshorts(min_articles, skip_posted=True)

    # Also get anything from the queue we captured between cycles
    queued = get_queued_articles()

    # Merge: live articles first, then queued (deduplicated by title hash)
    seen: set[str] = set()
    combined: list[dict[str, str]] = []
    for a in live_articles + queued:
        h = _hash_title(a["title"])
        if h not in seen:
            seen.add(h)
            combined.append(a)

    return combined[:min_articles]


HEADER = "\u26A1\uFE0F <b>\U0001d413\U0001d404\U0001d402\U0001d407 \U0001d40d\U0001d404\U0001d416\U0001d412 \U0001d405\U0001d40b\U0001d400\U0001d412\U0001d407</b> \u26A1\uFE0F"
FOOTER = (
    "\u2501" * 24
    + "\n"
    + '\u26A1 Powered by <a href="https://t.me/CoursesDrivee">@CoursesDrivee</a>'
    + "\n"
    + "\u2501" * 24
)
MAX_MSG_LEN = 4000  # leave margin below Telegram's 4096 limit


def format_news_posts(articles: list[dict[str, str]]) -> list[str]:
    """
    Format up to 10 articles into one or more Telegram HTML messages.
    Splits into multiple messages if content exceeds Telegram's limit.
    """
    blocks: list[str] = []
    for art in articles[:10]:
        block = (
            f"\U0001F4CC <b>{_escape_html(art['title'])}</b>\n\n"
            f"\U0001F4AC {_escape_html(art['summary'])}"
        )
        blocks.append(block)

    separator = "\n\n" + "\u2500" * 24 + "\n\n"
    messages: list[str] = []
    current = HEADER + "\n"
    for i, block in enumerate(blocks):
        addition = (separator if i > 0 else "\n") + block
        if len(current) + len(addition) + len(FOOTER) + 4 > MAX_MSG_LEN:
            current += "\n\n" + FOOTER
            messages.append(current)
            current = HEADER + "\n\n" + block
        else:
            current += addition

    current += "\n\n" + FOOTER
    messages.append(current)
    return messages


def format_news_post(articles: list[dict[str, str]]) -> str:
    """Single-string version (for /news preview). Returns first chunk only."""
    parts = format_news_posts(articles)
    return parts[0] if parts else ""


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
