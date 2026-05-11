"""
Movie scraper — supports two sources:
  - 4KHDHub  (https://4khdhub.link/category/hindi-movies/)
  - MoviesDrive (https://new2.moviesdrives.my/)
"""
from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
import warnings
from typing import Any

import requests
import urllib3
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# If set, all movie requests are routed through ScraperAPI (bypasses Cloudflare on cloud hosts)
# Sign up free at https://www.scraperapi.com  — 5,000 req/month free
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "")
SCRAPER_API_URL = "http://api.scraperapi.com"

# Full browser-like headers to avoid 403 blocks
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# Some sites (e.g. 4khdhub.link) have SSL chain issues on Windows.
_NO_VERIFY_HOSTS = {"4khdhub.link"}

# Per-host persistent sessions so cookies are carried across requests
_sessions: dict[str, requests.Session] = {}


def _session_for(host: str) -> requests.Session:
    if host not in _sessions:
        s = requests.Session()
        s.headers.update(HEADERS)
        _sessions[host] = s
    return _sessions[host]


def _get(url: str, retries: int = 2, **kwargs) -> requests.Response:
    """GET with browser headers, persistent session (cookies), SSL bypass for known hosts.

    When SCRAPER_API_KEY is set (recommended for cloud deployments), all requests
    are routed through ScraperAPI which bypasses Cloudflare / geo-blocks automatically.
    """
    # ── ScraperAPI mode ──────────────────────────────────────────────────────
    if SCRAPER_API_KEY:
        # If the caller passed query params (e.g. search functions), bake them
        # into the URL first — ScraperAPI uses its own `params` dict so we
        # cannot pass two separate `params` kwargs to requests.get().
        caller_params = kwargs.pop("params", None)
        if caller_params:
            encoded = urllib.parse.urlencode(caller_params)
            sep = "&" if "?" in url else "?"
            url = url + sep + encoded

        api_params = {"api_key": SCRAPER_API_KEY, "url": url, "render": "false"}
        kwargs.setdefault("timeout", 30)
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = requests.get(SCRAPER_API_URL, params=api_params, **kwargs)
                if resp.status_code in (403, 429, 500, 503) and attempt < retries:
                    log.warning("ScraperAPI %s → HTTP %s (attempt %d), retrying…",
                                url, resp.status_code, attempt + 1)
                    time.sleep(2 * (attempt + 1))
                    continue
                if resp.status_code not in (200, 301, 302):
                    log.warning("ScraperAPI %s → HTTP %s", url, resp.status_code)
                return resp
            except Exception as exc:
                last_exc = exc
                log.warning("ScraperAPI %s → %s: %s (attempt %d)",
                            url, type(exc).__name__, exc, attempt + 1)
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    # ── Direct mode (local / non-blocked network) ────────────────────────────
    host = urllib.parse.urlparse(url).hostname or ""
    verify = host not in _NO_VERIFY_HOSTS
    session = _session_for(host)

    ctx = warnings.catch_warnings()
    ctx.__enter__()
    if not verify:
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)

    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, verify=verify, **kwargs)
            if resp.status_code in (403, 429, 503) and attempt < retries:
                log.warning("_get %s → HTTP %s (attempt %d), retrying…", url, resp.status_code, attempt + 1)
                time.sleep(1.5 * (attempt + 1))
                _sessions.pop(host, None)
                session = _session_for(host)
                continue
            if resp.status_code not in (200, 301, 302):
                log.warning("_get %s → HTTP %s", url, resp.status_code)
            ctx.__exit__(None, None, None)
            return resp
        except Exception as exc:
            last_exc = exc
            log.warning("_get %s → %s: %s (attempt %d)", url, type(exc).__name__, exc, attempt + 1)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))

    ctx.__exit__(None, None, None)
    raise last_exc  # type: ignore[misc]


# ─── 4KHDHub ────────────────────────────────────────────────────────────────

HDH_BASE = "https://4khdhub.link"
HDH_CATEGORY = f"{HDH_BASE}/category/hindi-movies/"


HDH_PAGE_SIZE = 10


def hdh_latest_movies(page: int = 1) -> list[dict[str, str]]:
    """Scrape page N of the 4KHDHub Hindi category (10 per page)."""
    try:
        url = HDH_CATEGORY if page == 1 else f"{HDH_BASE}/category/hindi-movies/page/{page}/"
        # Prime the session with the homepage first (gets cookies, sets Referer)
        session = _session_for("4khdhub.link")
        session.headers["Referer"] = HDH_BASE + "/"
        resp = _get(url, timeout=20)
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
            if len(movies) >= HDH_PAGE_SIZE:
                break
        return movies
    except Exception as e:
        log.error("4KHDHub listing failed: %s", e)
        return []


def hdh_movie_links(movie_url: str) -> dict[str, Any]:
    """Return poster + list of quality/link blocks for a 4KHDHub movie page."""
    try:
        session = _session_for("4khdhub.link")
        session.headers["Referer"] = HDH_CATEGORY
        resp = _get(movie_url, timeout=20)
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

            # First text node = "Movie Name (quality spec)" — strip movie name
            raw_title = " ".join(
                (title_el.contents[0] if title_el.contents else "").strip().split()
            )
            # Extract just the quality part inside the last parentheses
            m = re.search(r"\(([^)]+)\)\s*$", raw_title)
            quality_title = m.group(1).strip() if m else raw_title

            # Extract badges: size (orange), audio/languages (teal), format (green)
            size_badge = ""
            audio_badge = ""
            format_badge = ""
            for badge in title_el.select("span.badge"):
                style = badge.get("style", "")
                text = badge.get_text(strip=True)
                if not text:
                    continue
                if "#ea580c" in style:        # orange → file size
                    size_badge = text
                elif "#0d9488" in style:      # teal  → languages / audio
                    audio_badge = text
                elif "#15803d" in style:      # green → format
                    format_badge = text

            links = []
            for a in content_div.select("a.btn"):
                text = re.sub(r"\s+", " ", a.text.strip()).replace("Download ", "")
                href = a.get("href", "")
                if text and href:
                    links.append({"name": text, "url": href})
            if links:
                qualities.append({
                    "quality": quality_title,
                    "size": size_badge,
                    "audio": audio_badge,
                    "format": format_badge,
                    "links": links,
                })

        return {"poster": poster, "qualities": qualities}
    except Exception as e:
        log.error("4KHDHub movie page failed (%s): %s", movie_url, e)
        return {"poster": "", "qualities": []}


# ─── MoviesDrive ─────────────────────────────────────────────────────────────

MD_BASE = "https://new2.moviesdrives.my"


MD_PAGE_SIZE = 10


def md_latest_movies(page: int = 1) -> list[dict[str, str]]:
    """Scrape page N of MoviesDrive (10 per page)."""
    try:
        url = MD_BASE + "/" if page == 1 else f"{MD_BASE}/page/{page}/"
        resp = _get(url, timeout=15)
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
            if len(movies) >= MD_PAGE_SIZE:
                break
        return movies
    except Exception as e:
        log.error("MoviesDrive listing failed: %s", e)
        return []


_INFO_KEYS = {
    "imdb":     ["imdb rating", "imdb"],
    "genre":    ["genre"],
    "director": ["director"],
    "stars":    ["stars", "cast"],
    "language": ["language"],
    "quality":  ["quality"],
    "format":   ["format"],
    "writer":   ["writer"],
}


def _scrape_md_info(content) -> dict[str, str]:
    """Extract movie metadata (IMDB, genre, etc.) from a MoviesDrive content block.

    Two layouts on the site:
      A) <strong>🌟iMDB Rating: 7.6/10</strong>   → value is INSIDE <strong>
      B) <strong>🗣Language:</strong> Hindi/English → value is TEXT AFTER <strong>
    """
    info: dict[str, str] = {}
    _EMOJI_RE = re.compile(r"^[\U00010000-\U0010ffff\u2600-\u26FF\u2700-\u27BF\u00A9\u00AE\u203C-\u2BFF]+")

    for strong in content.find_all("strong"):
        raw_label = strong.get_text(strip=True)
        key_text = raw_label.lower()
        # Strip leading emoji for cleaner comparison
        clean_key = _EMOJI_RE.sub("", key_text).strip()

        matched_field = None
        for field, keywords in _INFO_KEYS.items():
            if field in info:
                continue
            if any(kw in clean_key for kw in keywords):
                matched_field = field
                break
        if not matched_field:
            continue

        # Try to get value after ":" inside the strong text (layout A)
        if ":" in raw_label:
            after_colon = raw_label.split(":", 1)[1].strip()
            if after_colon:
                info[matched_field] = after_colon
                continue

        # Fallback: value is in sibling text nodes after the <strong> (layout B)
        parent = strong.parent
        if parent:
            parent_text = parent.get_text(" ", strip=True)
            label_clean = raw_label.strip()
            # Remove the label from the beginning
            if parent_text.startswith(label_clean):
                value = parent_text[len(label_clean):].strip().lstrip(":").strip()
            else:
                value = parent_text.replace(label_clean, "").strip().lstrip(":").strip()
            value = _EMOJI_RE.sub("", value).strip()
            if value:
                info[matched_field] = value

    return info


_DOWNLOAD_PATTERNS = (
    "hubcloud", "gdflix", "mdrive.lol", "gofile", "gdtot",
    "driveseed", "filedrive", "hub.foo",
    "workers.dev",   # moviesdrives-com.workers.dev CDN proxy links
)

# Non-download hrefs that should be excluded even if they match _DOWNLOAD_PATTERNS
_EXCLUDE_PATTERNS = (
    "new2.moviesdrives.my",
    "moviesdrives.my/tag/", "moviesdrives.my/category/", "moviesdrives.my/page/",
    "moviesdrive.one", "t.me/",
    "/tag/", "/category/",
)


def _is_download_link(href: str) -> bool:
    href_lower = href.lower()
    if any(x in href_lower for x in _EXCLUDE_PATTERNS):
        return False
    return any(p in href_lower for p in _DOWNLOAD_PATTERNS)


def _provider_name(href: str) -> str:
    href_lower = href.lower()
    if "hubcloud" in href_lower:
        return "HubCloud"
    if "gdflix" in href_lower:
        return "GDFlix"
    if "gofile" in href_lower:
        return "GoFile"
    if "workers.dev" in href_lower:
        return "MoviesDrive CDN"
    return "Download"


def _expand_mdrive(href: str, label: str, link_text: str) -> list[dict[str, str]]:
    """Fetch a mdrive.lol archive page and return its inner download links."""
    try:
        inner_resp = _get(href, timeout=10)
        inner_soup = BeautifulSoup(inner_resp.text, "html.parser")
        found = []
        for inner_a in inner_soup.select(".entry-content a[href], article a[href], main a[href]"):
            inner_href = inner_a.get("href", "")
            if inner_href and _is_download_link(inner_href) and "mdrive.lol" not in inner_href:
                provider = _provider_name(inner_href)
                found.append({
                    "label": label,
                    "name": f"{link_text} ({provider})",
                    "url": inner_href,
                })
        return found
    except Exception as e:
        log.error("Failed to fetch inner mdrive link %s: %s", href, e)
        return [{"label": label, "name": link_text, "url": href}]


def md_movie_links(movie_url: str) -> dict[str, Any]:
    """Return poster + list of quality/link rows for a MoviesDrive movie page.

    Handles two page layouts:
    1. <h5><a href="...">label</a></h5>  — link inside the heading
    2. <h5>label</h5> … <p><a href="...">…</a></p> — link in next sibling
    Also resolves mdrive.lol intermediary pages to real HubCloud/GDFlix links.
    """
    try:
        resp = _get(movie_url, timeout=15)
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

        content = (
            soup.select_one(".entry-content")
            or soup.select_one("article")
            or soup.select_one("main")
            or soup
        )

        links: list[dict[str, str]] = []
        current_label = "Download"

        for elem in content.descendants:
            # Skip non-tag nodes and deeply nested elements we handle via parent
            if not hasattr(elem, "name"):
                continue

            # h5 heading — update label and capture ALL inline <a> links
            if elem.name == "h5":
                current_label = re.sub(r"\s+", " ", elem.get_text(" ", strip=True))
                for a_tag in elem.find_all("a", href=True):
                    href = a_tag.get("href", "")
                    link_text = re.sub(r"\s+", " ", a_tag.get_text(strip=True)) or current_label
                    if href and _is_download_link(href):
                        if "mdrive.lol" in href:
                            links.extend(_expand_mdrive(href, current_label, link_text))
                        else:
                            links.append({
                                "label": current_label,
                                "name": f"{link_text} ({_provider_name(href)})",
                                "url": href,
                            })
                continue

            # <a> tags that are NOT inside an h5 (handled above already)
            if elem.name == "a":
                # Skip if this <a> is a child of an h5 (already processed)
                if elem.find_parent("h5"):
                    continue
                href = elem.get("href", "")
                if href and _is_download_link(href):
                    link_text = re.sub(r"\s+", " ", elem.get_text(strip=True)) or current_label
                    if "mdrive.lol" in href:
                        links.extend(_expand_mdrive(href, current_label, link_text))
                    else:
                        links.append({
                            "label": current_label,
                            "name": f"{link_text} ({_provider_name(href)})",
                            "url": href,
                        })

        # Remove duplicates preserving order
        seen: set[str] = set()
        unique_links = [l for l in links if not (l["url"] in seen or seen.add(l["url"]))]  # type: ignore[func-returns-value]

        info = _scrape_md_info(content)
        return {"poster": poster, "links": unique_links, "info": info}
    except Exception as e:
        log.error("MoviesDrive movie page failed (%s): %s", movie_url, e)
        return {"poster": "", "links": [], "info": {}}


# ─── Search ──────────────────────────────────────────────────────────────────

def hdh_search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Search 4KHDHub using the ?s= query parameter."""
    try:
        session = _session_for("4khdhub.link")
        session.headers["Referer"] = HDH_BASE + "/"
        resp = _get(f"{HDH_BASE}/", params={"s": query}, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        movies = []
        for card in soup.select(".movie-card"):
            title_el = card.select_one(".movie-card-title")
            title = title_el.text.strip() if title_el else "Unknown"
            poster_el = card.select_one("img")
            poster = poster_el.get("src", "") if poster_el else ""
            link = card.get("href") or ""
            if not link.startswith("http"):
                link = HDH_BASE + link
            if link:
                movies.append({"title": title, "url": link, "poster": poster, "source": "hdh"})
            if len(movies) >= limit:
                break
        return movies
    except Exception as e:
        log.error("4KHDHub search failed for '%s': %s", query, e)
        return []


def md_search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Search MoviesDrive via the /search.php JSON API (GET ?q=<query>&page=<page>)."""
    try:
        # The frontend JS uses GET with ?q= and ?page= params
        resp = _get(
            "https://new2.moviesdrives.my/search.php",
            params={"q": query, "page": 1},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        movies = []
        for hit in data.get("hits", []):
            doc = hit.get("document", {})
            title = doc.get("post_title", "Unknown")
            permalink = doc.get("permalink", "")
            poster = doc.get("post_thumbnail", "")
            if not permalink:
                continue
            url = "https://new2.moviesdrives.my" + permalink if not permalink.startswith("http") else permalink
            movies.append({"title": title, "url": url, "poster": poster, "source": "md"})
            if len(movies) >= limit:
                break
        return movies
    except Exception as e:
        log.error("MoviesDrive search failed for '%s': %s", query, e)
        return []


# ─── Formatting helpers ──────────────────────────────────────────────────────

def _base_name(link: dict) -> str:
    n = link["name"]
    if " (" in n and n.endswith(")"):
        return n[:n.rfind(" (")].strip()
    return n.strip()


def _is_true_series(plinks: list) -> bool:
    if len(plinks) <= 1:
        return False
    return len({_base_name(l) for l in plinks}) == 1


def _info_block(info: dict[str, str]) -> list[str]:
    """Build a compact metadata block from scraped info dict."""
    lines = []
    if info.get("imdb"):
        raw = info["imdb"].replace("iMDB Rating:", "").replace("IMDB Rating:", "").strip().lstrip(":").strip()
        lines.append(f"⭐ <b>IMDb:</b> {raw}")
    if info.get("genre"):
        lines.append(f"🎭 <b>Genre:</b> {info['genre']}")
    if info.get("language"):
        lines.append(f"🗣 <b>Language:</b> {info['language']}")
    if info.get("quality"):
        lines.append(f"📺 <b>Quality:</b> {info['quality']}")
    if info.get("director"):
        lines.append(f"🎬 <b>Director:</b> {info['director']}")
    if info.get("stars"):
        stars = info["stars"]
        # Trim to first 3 cast members
        cast = [s.strip() for s in stars.split(",")][:3]
        lines.append(f"🌟 <b>Cast:</b> {', '.join(cast)}" + (" …" if len(stars.split(",")) > 3 else ""))
    return lines


# ─── Formatting ──────────────────────────────────────────────────────────────

def format_hdh_message(movie_title: str, data: dict[str, Any], footer: bool = True) -> str:
    qualities = data.get("qualities", [])
    if not qualities:
        return ""

    lines = []
    if movie_title:
        lines.append(f"🎬 <b>{movie_title}</b>")
    lines.append("━" * 32)

    # Info block from page metadata (if any)
    info = data.get("info", {})
    info_lines = _info_block(info)
    if info_lines:
        lines.extend(info_lines)
        lines.append("━" * 32)

    lines.append("\n📥 <b>Download Links</b>  <i>(4KHDHub)</i>\n")
    for q in qualities:
        # Quality header line
        lines.append(f"📦 <b>{q['quality']}</b>")
        # Inline badges: size | audio | format
        meta_parts = []
        if q.get("size"):
            meta_parts.append(f"📁 {q['size']}")
        if q.get("audio"):
            meta_parts.append(f"🗣 {q['audio']}")
        if q.get("format"):
            meta_parts.append(f"📺 {q['format']}")
        if meta_parts:
            lines.append("   " + "  |  ".join(meta_parts))
        # Download links
        parts = [f"<a href='{l['url']}'>{l['name']}</a>" for l in q["links"]]
        lines.append("   🔗 " + " · ".join(parts))
        lines.append("")
    if footer:
        lines.append("━" * 32)
        lines.append("⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")
    return "\n".join(lines)


def format_md_message(movie_title: str, data: dict[str, Any], footer: bool = True) -> str:
    links = data.get("links", [])
    if not links:
        return ""

    lines = []
    if movie_title:
        lines.append(f"🎬 <b>{movie_title}</b>")
    lines.append("━" * 32)

    # Info block
    info = data.get("info", {})
    info_lines = _info_block(info)
    if info_lines:
        lines.extend(info_lines)
        lines.append("━" * 32)

    lines.append("\n📥 <b>Download Links</b>  <i>(MoviesDrive)</i>\n")

    # Group by quality label
    by_label: dict[str, list] = {}
    for l in links:
        by_label.setdefault(l["label"], []).append(l)

    for label, group_links in by_label.items():
        lines.append(f"📦 <b>{label}</b>")

        by_provider: dict[str, list] = {}
        for l in group_links:
            prov = _provider_name(l["url"])
            by_provider.setdefault(prov, []).append(l)

        any_true_series = any(_is_true_series(v) for v in by_provider.values())

        if any_true_series:
            for prov, plinks in by_provider.items():
                if _is_true_series(plinks):
                    ep_parts = " · ".join(
                        f"<a href='{l['url']}'>Ep{i + 1}</a>"
                        for i, l in enumerate(plinks)
                    )
                    lines.append(f"   🔗 <b>{prov}</b>: {ep_parts}")
                else:
                    for l in plinks:
                        lines.append(f"   🔗 <a href='{l['url']}'>{l['name']}</a>")
        else:
            parts = [f"<a href='{l['url']}'>{l['name']}</a>" for l in group_links]
            lines.append("   🔗 " + " · ".join(parts))

        lines.append("")

    if footer:
        lines.append("━" * 32)
        lines.append("⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")
    return "\n".join(lines)
