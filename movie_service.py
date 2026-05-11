"""
Movie scraper — supports two sources:
  - 4KHDHub  (https://4khdhub.link/category/hindi-movies/)
  - MoviesDrive (https://new2.moviesdrives.my/)
"""
from __future__ import annotations

import logging
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ─── 4KHDHub ────────────────────────────────────────────────────────────────

HDH_BASE = "https://4khdhub.link"
HDH_CATEGORY = f"{HDH_BASE}/category/hindi-movies/"


def hdh_latest_movies(limit: int = 5) -> list[dict[str, str]]:
    """Scrape the latest movies from 4KHDHub Hindi category."""
    try:
        resp = requests.get(HDH_CATEGORY, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        movies = []
        for card in soup.select(".movie-card"):
            title_el = card.select_one(".movie-card-title")
            title = title_el.text.strip() if title_el else "Unknown"
            poster_el = card.select_one("img")
            poster = poster_el.get("src") if poster_el else None
            link = card.get("href") or ""
            if not link.startswith("http"):
                link = HDH_BASE + link
            if link:
                movies.append({"title": title, "url": link, "poster": poster or ""})
            if len(movies) >= limit:
                break
        return movies
    except Exception as e:
        log.error("4KHDHub listing failed: %s", e)
        return []


def hdh_movie_links(movie_url: str) -> dict[str, Any]:
    """Return poster + list of quality/link blocks for a 4KHDHub movie page."""
    try:
        resp = requests.get(movie_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Poster: og:image or first movie-card img on the detail page
        poster = ""
        og = soup.find("meta", property="og:image")
        if og:
            poster = og.get("content", "")

        qualities: list[dict[str, Any]] = []
        for content_div in soup.select("div[id^='content-file']"):
            parent = content_div.parent
            if not parent:
                continue
            title_el = parent.select_one(".flex-1.text-left.font-semibold")
            if not title_el:
                continue
            quality_title = " ".join(
                (title_el.contents[0] if title_el.contents else "").strip().split()
            )
            links = []
            for a in content_div.select("a.btn"):
                text = re.sub(r"\s+", " ", a.text.strip()).replace("Download ", "")
                href = a.get("href", "")
                if text and href:
                    links.append({"name": text, "url": href})
            if links:
                qualities.append({"quality": quality_title, "links": links})

        return {"poster": poster, "qualities": qualities}
    except Exception as e:
        log.error("4KHDHub movie page failed (%s): %s", movie_url, e)
        return {"poster": "", "qualities": []}


# ─── MoviesDrive ─────────────────────────────────────────────────────────────

MD_BASE = "https://new2.moviesdrives.my"


def md_latest_movies(limit: int = 5) -> list[dict[str, str]]:
    """Scrape the latest movies from MoviesDrive home page."""
    try:
        resp = requests.get(MD_BASE + "/", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        movies = []
        for a_tag in soup.select("a[href]"):
            card = a_tag.select_one(".poster-card")
            if not card:
                continue
            title_el = card.select_one(".poster-title")
            title = title_el.text.strip() if title_el else "Unknown"
            poster_el = card.select_one("img")
            poster = poster_el.get("src", "") if poster_el else ""
            link = a_tag.get("href", "")
            if not link.startswith("http"):
                link = MD_BASE + link
            if title and link:
                movies.append({"title": title, "url": link, "poster": poster})
            if len(movies) >= limit:
                break
        return movies
    except Exception as e:
        log.error("MoviesDrive listing failed: %s", e)
        return []


def md_movie_links(movie_url: str) -> dict[str, Any]:
    """Return poster + list of quality/link rows for a MoviesDrive movie page."""
    try:
        resp = requests.get(movie_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Poster: <img alt="Poster"> or og:image fallback
        poster = ""
        poster_img = soup.find("img", alt="Poster")
        if poster_img:
            poster = poster_img.get("src", "")
        if not poster:
            og = soup.find("meta", property="og:image")
            if og:
                poster = og.get("content", "")

        # Links are <h5> tags containing <a href="...">
        links: list[dict[str, str]] = []
        h5_tags = soup.select("main h5, article h5, .entry-content h5")
        i = 0
        while i < len(h5_tags):
            a_tag = h5_tags[i].find("a")
            if a_tag:
                label = re.sub(r"\s+", " ", h5_tags[i].text.strip())
                href = a_tag.get("href", "")
                link_text = re.sub(r"\s+", " ", a_tag.text.strip())
                if href and link_text:
                    links.append({"label": label, "name": link_text, "url": href})
            i += 1

        # Remove duplicates preserving order
        seen = set()
        unique_links = []
        for l in links:
            if l["url"] not in seen:
                seen.add(l["url"])
                unique_links.append(l)

        return {"poster": poster, "links": unique_links}
    except Exception as e:
        log.error("MoviesDrive movie page failed (%s): %s", movie_url, e)
        return {"poster": "", "links": []}


# ─── Formatting ──────────────────────────────────────────────────────────────

def format_hdh_message(movie_title: str, data: dict[str, Any]) -> str:
    qualities = data.get("qualities", [])
    if not qualities:
        return f"🎬 <b>{movie_title}</b>\n\n❌ No download links found."

    lines = [f"🎬 <b>{movie_title}</b>\n", "📥 <b>Download Links (4KHDHub)</b>\n"]
    for q in qualities[:6]:
        lines.append(f"📼 <b>{q['quality']}</b>")
        parts = [f"<a href='{l['url']}'>{l['name']}</a>" for l in q["links"]]
        lines.append("  " + " | ".join(parts))
        lines.append("")
    if len(qualities) > 6:
        lines.append(f"<i>…and {len(qualities) - 6} more on the website.</i>")
    lines.append("\n⚡ Powered by @CoursesDrivee")
    return "\n".join(lines)


def format_md_message(movie_title: str, data: dict[str, Any]) -> str:
    links = data.get("links", [])
    if not links:
        return f"🎬 <b>{movie_title}</b>\n\n❌ No download links found."

    lines = [f"🎬 <b>{movie_title}</b>\n", "📥 <b>Download Links (MoviesDrive)</b>\n"]
    for l in links:
        lines.append(f"<a href='{l['url']}'>{l['name']}</a>")
    lines.append("\n⚡ Powered by @CoursesDrivee")
    return "\n".join(lines)
