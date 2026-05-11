"""
Tech news scraper from Inshorts.
Fetches articles, filters ads/sponsored, deduplicates, formats for Telegram.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from typing import Any

import requests
from bs4 import BeautifulSoup

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
            con.execute(
                "INSERT OR IGNORE INTO posted_news (hash, title) VALUES (?, ?)",
                (_hash_title(t), t[:200]),
            )
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
    Tries to get at least min_articles.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    articles: list[dict[str, str]] = []
    seen_titles: set[str] = set()

    try:
        resp = requests.get(INSHORTS_URL, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error("Inshorts fetch failed: %s", e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.find_all("div", {"itemtype": "http://schema.org/NewsArticle"})

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


def get_fresh_articles_for_posting(min_articles: int = 10) -> list[dict[str, str]]:
    """Get only unposted articles (for auto-post and /news Post button)."""
    return scrape_inshorts(min_articles, skip_posted=True)


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
