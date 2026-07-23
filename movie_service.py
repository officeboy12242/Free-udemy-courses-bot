"""
Movie scraper — supports multiple sources:
  - HDHub4u         (https://new2.hdhub4u.cl/)          ← primary
  - 4KHDHub         (https://4khdhub.link/category/hindi-movies/)
  - MoviesDrive     (https://new2.moviesdrives.my/)
  - HDMovie2        (https://newhdmovie2.pro/)
  - Vegamovies      (https://vegamovies.global/)
  - SDMoviesPoint   (https://sd1.sdmoviespoint.trade/)
  - BollyFlix       (https://new.bollyflix.gd/)
  - MoviesMod       (https://moviesmod.farm/)
  - AtoZ Cinemas    (https://atoz.cinemaz.workers.dev/)
  - ZeeFliz         (https://zeefliz.beer/)
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import warnings
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    _CFFI_AVAILABLE = True
except ImportError:
    _CFFI_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

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

# Some sites have SSL chain issues on Windows / cloud deployments.
_NO_VERIFY_HOSTS = {
    "4khdhub.link",
    "cryptoinsights.site",
    "hblinks.org",
    "new2.hdhub4u.cl",    # HDHub4u - SSL cert issues
    "new1.hdhub4u.limo",  # HDHub4u legacy mirror
    "hdhub4u.limo",
    "linksmod.top",  # MoviesMod intermediary - SSL issues
    "episodes.modpro.blog",  # MoviesMod redirector - SSL issues
}

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

    Priority: curl_cffi (fast, free) → ScraperAPI fallback (if key set and cffi fails) → plain requests.
    """
    host = urllib.parse.urlparse(url).hostname or ""
    verify = host not in _NO_VERIFY_HOSTS

    # ── Try curl_cffi first (best anti-bot bypass, free) ─────────────────────
    use_cffi = _CFFI_AVAILABLE
    if use_cffi:
        cffi_exc = None
        for attempt in range(retries + 1):
            try:
                timeout = kwargs.get("timeout", 20)
                params = kwargs.get("params", None)
                r = cffi_requests.get(
                    url, headers=HEADERS, verify=verify,
                    timeout=timeout, params=params,
                    impersonate="chrome"
                )
                if r.status_code in (403, 429, 503) and attempt < retries:
                    log.warning("_get(cffi) %s → HTTP %s (attempt %d), retrying…",
                                url, r.status_code, attempt + 1)
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code == 200:
                    return r
                # Non-200: let it fall through to ScraperAPI/plain
                cffi_exc = None
                log.info("_get(cffi) %s → HTTP %s, trying fallback", url, r.status_code)
                break
            except Exception as exc:
                cffi_exc = exc
                log.warning("_get(cffi) %s → %s: %s (attempt %d)",
                            url, type(exc).__name__, exc, attempt + 1)
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))

    # ── ScraperAPI fallback (only if key is set and cffi failed/returned non-200) ─
    if SCRAPER_API_KEY:
        caller_params = kwargs.pop("params", None)
        target_url = url
        if caller_params:
            encoded = urllib.parse.urlencode(caller_params)
            sep = "&" if "?" in target_url else "?"
            target_url = target_url + sep + encoded

        api_params = {"api_key": SCRAPER_API_KEY, "url": target_url, "render": "false"}
        kwargs.setdefault("timeout", 30)
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = requests.get(SCRAPER_API_URL, params=api_params, **kwargs)
                if resp.status_code in (403, 429, 500, 503) and attempt < retries:
                    log.warning("ScraperAPI %s → HTTP %s (attempt %d), retrying…",
                                target_url, resp.status_code, attempt + 1)
                    time.sleep(2 * (attempt + 1))
                    continue
                if resp.status_code not in (200, 301, 302):
                    log.warning("ScraperAPI %s → HTTP %s", target_url, resp.status_code)
                return resp
            except Exception as exc:
                last_exc = exc
                log.warning("ScraperAPI %s → %s: %s (attempt %d)",
                            target_url, type(exc).__name__, exc, attempt + 1)
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
        if last_exc:
            raise last_exc

    # ── Plain requests (for SSL-broken hosts or if nothing else worked) ──────
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


def _get_rendered_html(url: str, timeout: int = 30, wait_ms: int = 8000) -> str | None:
    """Fetch a page using Playwright headless Chromium for full JS rendering.

    Uses 'domcontentloaded' instead of 'networkidle' because ad-heavy sites
    never reach network-idle state. The wait_ms pause lets JS inject dynamic content.
    Returns the rendered HTML string, or None if Playwright is unavailable or fails.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        log.debug("Playwright not installed — skipping JS render for %s", url)
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(wait_ms)
            html = page.content()
            browser.close()
        return html
    except Exception as exc:
        exc_str = str(exc)
        if "Executable doesn't exist" in exc_str or "not found" in exc_str.lower():
            log.warning("Playwright browser binaries not installed — skipping JS render")
        else:
            log.error("Playwright render failed for %s: %s", url, exc)
        return None


# ─── 4KHDHub ────────────────────────────────────────────────────────────────

HDH_BASE     = os.getenv("HDH_BASE_URL", "https://4khdhub.one")
HDH_CATEGORY = f"{HDH_BASE}/category/hindi-movies/"


HDH_PAGE_SIZE = 10


def hdh_latest_movies(page: int = 1) -> list[dict[str, str]]:
    """Scrape page N of the 4KHDHub Hindi category (10 per page)."""
    try:
        url = HDH_CATEGORY if page == 1 else f"{HDH_BASE}/category/hindi-movies/page/{page}/"
        # Prime the session with the homepage first (gets cookies, sets Referer)
        session = _session_for(urlparse(HDH_BASE).netloc or "4khdhub.link")
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


def hdh_movie_links(movie_url: str, *, fast: bool = False) -> dict[str, Any]:
    """Return poster + list of quality/link blocks for a 4KHDHub movie page."""
    try:
        session = _session_for(urlparse(HDH_BASE).netloc or "4khdhub.link")
        session.headers["Referer"] = HDH_CATEGORY
        page_timeout = 10 if fast else 20
        page_retries = 0 if fast else 2
        resp = _get(movie_url, timeout=page_timeout, retries=page_retries)
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

MD_BASE = os.getenv("MD_BASE_URL", "https://new2.moviesdrives.my")


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
    # Never treat the current MoviesDrive host as a download provider
    try:
        md_host = urlparse(MD_BASE).netloc.lower()
        if md_host and md_host in href_lower:
            return False
    except Exception:
        pass
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


def md_movie_links(movie_url: str, *, fast: bool = False) -> dict[str, Any]:
    """Return poster + list of quality/link rows for a MoviesDrive movie page.

    Handles two page layouts:
    1. <h5><a href="...">label</a></h5>  — link inside the heading
    2. <h5>label</h5> … <p><a href="...">…</a></p> — link in next sibling
    Also resolves mdrive.lol intermediary pages to real HubCloud/GDFlix links.
    """
    try:
        page_timeout = 10 if fast else 15
        page_retries = 0 if fast else 2
        resp = _get(movie_url, timeout=page_timeout, retries=page_retries)
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
        current_quality = ""

        def _md_append(href: str, label: str, link_text: str, quality: str) -> None:
            if "mdrive.lol" in href:
                for row in _expand_mdrive(href, label, link_text):
                    row["quality"] = quality or _api_extract_quality(
                        f"{row.get('label', '')} {row.get('name', '')}"
                    )
                    links.append(row)
            else:
                links.append({
                    "label": label,
                    "name": f"{link_text} ({_provider_name(href)})",
                    "url": href,
                    "quality": quality,
                })

        for elem in content.descendants:
            # Skip non-tag nodes and deeply nested elements we handle via parent
            if not hasattr(elem, "name"):
                continue

            # h5 heading — update label and capture ALL inline <a> links
            if elem.name == "h5":
                current_label = re.sub(r"\s+", " ", elem.get_text(" ", strip=True))
                # Keep last quality when heading is only "Zip [2.14GB]" etc.
                heading_quality = _api_extract_quality(current_label)
                if heading_quality:
                    current_quality = heading_quality
                for a_tag in elem.find_all("a", href=True):
                    href = a_tag.get("href", "")
                    link_text = re.sub(r"\s+", " ", a_tag.get_text(strip=True)) or current_label
                    link_quality = (
                        _api_extract_quality(f"{current_label} {link_text}")
                        or current_quality
                    )
                    if link_quality:
                        current_quality = link_quality
                    if href and _is_download_link(href):
                        _md_append(href, current_label, link_text, link_quality)
                continue

            # <a> tags that are NOT inside an h5 (handled above already)
            if elem.name == "a":
                # Skip if this <a> is a child of an h5 (already processed)
                if elem.find_parent("h5"):
                    continue
                href = elem.get("href", "")
                if href and _is_download_link(href):
                    link_text = re.sub(r"\s+", " ", elem.get_text(strip=True)) or current_label
                    # Zip rows often sit under a bare "Zip [size]" heading — inherit quality
                    link_quality = (
                        _api_extract_quality(f"{current_label} {link_text}")
                        or current_quality
                    )
                    if link_quality:
                        current_quality = link_quality
                    _md_append(href, current_label, link_text, link_quality)

        # Remove duplicates preserving order
        seen: set[str] = set()
        unique_links = [l for l in links if not (l["url"] in seen or seen.add(l["url"]))]  # type: ignore[func-returns-value]

        info = _scrape_md_info(content)
        return {"poster": poster, "links": unique_links, "info": info}
    except Exception as e:
        log.error("MoviesDrive movie page failed (%s): %s", movie_url, e)
        return {"poster": "", "links": [], "info": {}}


# ─── Search ──────────────────────────────────────────────────────────────────

def hdh_search(query: str, limit: int = 10, *, timeout: int = 20, retries: int = 2) -> list[dict[str, str]]:
    """Search 4KHDHub using the ?s= query parameter."""
    try:
        session = _session_for(urlparse(HDH_BASE).netloc or "4khdhub.link")
        session.headers["Referer"] = HDH_BASE + "/"
        resp = _get(f"{HDH_BASE}/", params={"s": query}, timeout=timeout, retries=retries)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        movies = []
        for card in soup.select(".movie-card"):
            title_el = card.select_one(".movie-card-title")
            title = title_el.text.strip() if title_el else "Unknown"
            if not _title_matches_query(title, query):
                continue
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


def md_search(
    query: str,
    limit: int = 10,
    *,
    timeout: int = 15,
    retries: int = 2,
) -> list[dict[str, str]]:
    """Search MoviesDrive via the /search.php JSON API (GET ?q=<query>&page=<page>)."""
    try:
        resp = _get(
            f"{MD_BASE.rstrip('/')}/search.php",
            params={"q": query, "page": 1},
            timeout=timeout,
            retries=retries,
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
            if not _title_matches_query(title, query):
                continue
            url = (
                permalink if permalink.startswith("http")
                else MD_BASE.rstrip("/") + (
                    permalink if permalink.startswith("/") else f"/{permalink}"
                )
            )
            movies.append({"title": title, "url": url, "poster": poster, "source": "md"})
            if len(movies) >= limit:
                break
        return movies
    except Exception as e:
        log.error("MoviesDrive search failed for '%s': %s", query, e)
        return []


# ─── HDMovie2 (newhdmovie2.pro) ─────────────────────────────────────────────

HDMOVIE2_BASE = os.getenv("HDMOVIE2_BASE_URL", "https://newhdmovie2.pro")

# Recognised final download providers on hdm.im redirect pages
_HDMOVIE2_DL_HOSTS = {
    "gdflix.dev", "gdflix.pro", "gdflix.", "gdtotv2.site", "gdtot.",
    "filebee.xyz", "gofile.io", "vikingfile.com", "megaup.net",
    "pixeldrain.com", "krakenfiles.com", "uploadrar.com",
    "hubcloud.foo", "hubdrive.space", "1fichier.com", "mediafire.com",
    "fastdl.zip", "vcloud.zip", "fileapi.com",
}


def _hdmovie2_provider(href: str, text: str = "") -> str:
    """Friendly provider name extracted from URL host or button text."""
    # Prefer text label like "[Gdflix]" / "[Filebee]" if present
    if text:
        m = re.search(r"\[([^\]]+)\]", text)
        if m:
            label = m.group(1).strip()
            # Normalise common labels
            normalise = {
                "Gdflix": "GDFlix",
                "Gdtotv2 Direct": "GDToT",
                "Gdtot": "GDToT",
                "Filebee": "Filepress",
            }
            return normalise.get(label, label)

    host = (urllib.parse.urlparse(href).hostname or "").lstrip("www.")
    _MAP = {
        "gdflix": "GDFlix",
        "gdtotv2": "GDToT",
        "gdtot": "GDToT",
        "filebee": "Filepress",
        "gofile": "GoFile",
        "vikingfile": "VikingFile",
        "megaup": "MegaUp",
        "pixeldrain": "PixelDrain",
        "hubcloud": "HubCloud",
        "hubdrive": "HubDrive",
        "krakenfiles": "Kraken",
        "1fichier": "1Fichier",
        "mediafire": "MediaFire",
        "fastdl": "G-Direct",
        "vcloud": "V-Cloud",
    }
    for needle, name in _MAP.items():
        if needle in host:
            return name
    return host.split(".")[0].title() if host else "Download"


def _is_hdmovie2_dl(href: str) -> bool:
    """Check if URL points to a known final-download host."""
    if not href or not href.startswith("http"):
        return False
    return any(h in href for h in _HDMOVIE2_DL_HOSTS)


def _scrape_hdmim(hdm_url: str) -> list[dict[str, str]]:
    """Scrape an hdm.im redirect page → list of final download links.

    Each link dict: {"label": "1080P [Gdflix] 5.83 GB", "url": "https://gdflix.dev/..."}
    """
    try:
        resp = _get(hdm_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        links: list[dict[str, str]] = []
        seen_urls: set = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            text = a.get_text(strip=True)

            if not _is_hdmovie2_dl(href):
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            label = text if text else _hdmovie2_provider(href)
            links.append({"label": label, "url": href})
        return links
    except Exception as e:
        log.error("hdm.im scrape failed (%s): %s", hdm_url, e)
        return []


def _parse_hdmovie2_articles(soup: BeautifulSoup, limit: int = 10) -> list[dict[str, str]]:
    """Extract movie list (title/url/poster) from a homepage / search-results page."""
    movies: list[dict[str, str]] = []
    for art in soup.select("article"):
        title_a = art.select_one(
            ".entry-title a, h2 a, h3 a, a[rel='bookmark'], a[title]"
        )
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        link = title_a.get("href", "")
        # Some themes nest the link inside .entry-title with the title in attribute
        if not title and title_a.get("title"):
            title = title_a["title"]

        # Poster
        poster = ""
        img = art.select_one("img")
        if img:
            poster = (
                img.get("src", "")
                or img.get("data-src", "")
                or img.get("data-lazy-src", "")
            )

        if title and link:
            movies.append(
                {"title": title, "url": link, "poster": poster, "source": "hdmovie2"}
            )
        if len(movies) >= limit:
            break
    return movies


def hdmovie2_latest_movies(page: int = 1) -> list[dict[str, str]]:
    """Recently-added movies from HDMovie2.

    Page 1 uses the homepage (newest trending titles). Subsequent pages use
    the /movie/page/N/ archive listing for proper pagination.
    """
    try:
        if page == 1:
            url = HDMOVIE2_BASE + "/"
        else:
            # Archive starts at /movie/page/1/ → bot page 2; offset by one
            url = f"{HDMOVIE2_BASE}/movie/page/{page - 1}/"
        resp = _get(url, timeout=15)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return _parse_hdmovie2_articles(soup, limit=10)
    except Exception as e:
        log.error("HDMovie2 listing failed (page %d): %s", page, e)
        return []


def hdmovie2_search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Search HDMovie2 via WordPress ?s= parameter."""
    try:
        url = f"{HDMOVIE2_BASE}/?s={urllib.parse.quote_plus(query)}"
        resp = _get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return _parse_hdmovie2_articles(soup, limit=limit)
    except Exception as e:
        log.error("HDMovie2 search failed for '%s': %s", query, e)
        return []


def _scrape_hdmovie2_info(soup: BeautifulSoup) -> dict[str, str]:
    """Parse genre/year/plot from a movie page."""
    info: dict[str, str] = {}

    # Genre tags
    genre_links = soup.select("a[href*='/genre/']")
    if genre_links:
        genres = list(
            dict.fromkeys(
                g.get_text(strip=True) for g in genre_links if g.get_text(strip=True)
            )
        )[:5]
        if genres:
            info["genre"] = ", ".join(genres)

    # Year
    year_link = soup.select_one("a[href*='/year/']")
    if year_link:
        year = year_link.get_text(strip=True)
        if year:
            info["year"] = year

    return info


def hdmovie2_movie_links(movie_url: str) -> dict[str, Any]:
    """Scrape an HDMovie2 movie page → poster, info, final download links.

    Workflow:
      1. Fetch the movie page
      2. Pick up the unique hdm.im redirect URL(s) (one per movie, several for series)
      3. Resolve each hdm.im URL → quality-labeled final download links
    """
    result: dict[str, Any] = {"poster": "", "info": {}, "links": []}
    try:
        resp = _get(movie_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Poster: og:image first, fall back to first article image
        og = soup.find("meta", property="og:image")
        if og:
            result["poster"] = og.get("content", "")
        if not result["poster"]:
            img = soup.select_one("article img, .post img")
            if img:
                result["poster"] = (
                    img.get("src", "")
                    or img.get("data-src", "")
                    or img.get("data-lazy-src", "")
                )

        result["info"] = _scrape_hdmovie2_info(soup)

        # Find unique hdm.im redirect URLs
        hdm_urls: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            host = (urllib.parse.urlparse(href).hostname or "").lower()
            if "hdm.im" in host or "hdmovie2.im" in host:
                if href not in hdm_urls:
                    hdm_urls.append(href)

        # Resolve each — keep dedupe across all of them
        seen_urls: set = set()
        for hdm_url in hdm_urls:
            for lk in _scrape_hdmim(hdm_url):
                if lk["url"] in seen_urls:
                    continue
                seen_urls.add(lk["url"])
                result["links"].append(lk)

        return result
    except Exception as e:
        log.error("HDMovie2 movie page failed (%s): %s", movie_url, e)
        return result


def format_hdmovie2_message(
    movie_title: str, data: dict[str, Any], footer: bool = True
) -> str:
    """Format an HDMovie2 result as HTML for Telegram, grouped by quality."""
    links = data.get("links", [])
    lines: list[str] = []
    if movie_title:
        lines.append(f"🎬 <b>{movie_title}</b>")

    if not links:
        if lines:
            lines.append("\n❌ No download links found.")
        return "\n".join(lines) if lines else ""

    lines.append("━" * 32)
    info_lines = _info_block(data.get("info", {}))
    if info_lines:
        lines.extend(info_lines)
        lines.append("━" * 32)

    lines.append("\n📥 <b>Download Links</b>  <i>(HDMovie2)</i>\n")

    # Group by quality (e.g. 480P / 720P / 1080P / 2160P)
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for lk in links:
        label = lk.get("label", "Download")
        url_ = lk.get("url", "")

        q_match = re.search(r"(\d{3,4}P|4K|2160P)", label, re.IGNORECASE)
        quality = q_match.group(1).upper() if q_match else "Download"
        if quality == "4K":
            quality = "2160P"

        provider = _hdmovie2_provider(url_, label)

        size_match = re.search(r"(\d+(?:\.\d+)?\s*(?:GB|MB))", label, re.IGNORECASE)
        size = size_match.group(1) if size_match else ""

        grouped.setdefault(quality, []).append((provider, url_, size))

    # Sort 480 < 720 < 1080 < 2160
    quality_order = {"480P": 0, "720P": 1, "1080P": 2, "2160P": 3}
    sorted_qs = sorted(grouped.keys(), key=lambda q: quality_order.get(q, 99))

    for q in sorted_qs:
        entries = grouped[q]
        size = next((s for _, _, s in entries if s), "")
        header = f"📦 <b>{q}</b>" + (f"  <i>{size}</i>" if size else "")
        lines.append(header)

        seen_provs: set = set()
        parts: list[str] = []
        for prov, url_, _ in entries:
            if prov in seen_provs:
                continue
            seen_provs.add(prov)
            parts.append(f"<a href='{url_}'>{prov}</a>")
        if parts:
            lines.append("   🔗 " + " · ".join(parts))

    if footer:
        lines.append("")
        lines.append("━" * 32)
        lines.append(
            "⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>"
        )

    return "\n".join(lines)


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


# ══════════════════════════════════════════════════════════════════════════════
#  Vegamovies  (https://vegamovies.global/)
# ══════════════════════════════════════════════════════════════════════════════

VEGA_BASE        = os.getenv("VEGA_BASE_URL", "https://vegamovies.global")
_VEGA_SEARCH_URL = VEGA_BASE + "/?do=search&subaction=search&story={}"


def _vega_abs(url: str) -> str:
    """Make relative Vegamovies URLs absolute."""
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return VEGA_BASE + url
    return url


def _scrape_vega_info(soup: BeautifulSoup) -> dict[str, str]:
    """Extract movie metadata from a vegamovies.global movie page."""
    info: dict[str, str] = {}
    content = soup.select_one(".entry-content, article") or soup
    text = content.get_text("\n", strip=True)

    _PATTERNS = [
        (r"IMDb\s+Rating\s*[:\-]+\s*([\d.]+)", "imdb"),
        (r"Genre[s]?\s*[:\-]+\s*([^\n]+)", "genre"),
        (r"Original\s+language\s*[:\-]+\s*([^\n]+)", "language"),
        (r"Quality\s*[:\-]+\s*([^\n]+)", "quality"),
        (r"Runtime\s*[:\-]+\s*([^\n]+)", "runtime"),
        (r"(?:Cast|Stars?)\s*[:\-]+\s*([^\n]+)", "stars"),
        (r"Release\s+Year\s*[:\-]+\s*([^\n]+)", "year"),
        (r"Format\s*[:\-]+\s*([^\n]+)", "format"),
    ]
    for pattern, field in _PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip(".")
            val = re.split(r"\s{2,}|\n", val)[0].strip()
            if val:
                info[field] = val
    return info


def vega_latest_movies(page: int = 1, limit: int = 10) -> list[dict]:
    """Fetch latest movies from vegamovies.global with pagination.

    Vegamovies uses WordPress-style ``/page/N/`` pagination.
    Each site page has ~25 posts; we take the first ``limit`` per call.
    """
    url = VEGA_BASE + "/" if page == 1 else f"{VEGA_BASE}/page/{page}/"
    try:
        resp = _get(url, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("vega_latest_movies page=%d failed: %s", page, exc)
        return []

    movies: list[dict] = []
    for art in soup.select("article.post-item"):
        if len(movies) >= limit:
            break
        a = art.select_one("a.blog-img, a[title]")
        img = art.select_one("img[src], img[data-src]")
        if not a:
            continue
        title = (a.get("title") or a.get_text(strip=True)).strip()
        href  = a.get("href", "")
        poster = ""
        if img:
            poster = _vega_abs(img.get("src") or img.get("data-src") or "")
        if title and href:
            movies.append({"title": title, "url": href, "poster": poster})
    return movies


def vega_search(query: str, limit: int = 10) -> list[dict]:
    """Search vegamovies.global using the DLE search endpoint."""
    url = _VEGA_SEARCH_URL.format(urllib.parse.quote(query))
    try:
        resp = _get(url, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("vega_search '%s' failed: %s", query, exc)
        return []

    movies: list[dict] = []
    for art in soup.select("article.post-item"):
        if len(movies) >= limit:
            break
        a = art.select_one("a.blog-img, a[title]")
        img = art.select_one("img[src], img[data-src]")
        if not a:
            continue
        title = (a.get("title") or a.get_text(strip=True)).strip()
        href  = a.get("href", "")
        poster = ""
        if img:
            poster = _vega_abs(img.get("src") or img.get("data-src") or "")
        if title and href:
            movies.append({"title": title, "url": href, "poster": poster})
    return movies


def vega_movie_links(movie_url: str) -> dict[str, Any]:
    """Scrape download links from a vegamovies.global movie page.

    Returns::
        {
          "poster": str,
          "links":  [{"quality": str, "url": str, "size": str}],
          "info":   {imdb, genre, language, quality, runtime, stars, year, format},
        }
    """
    try:
        resp = _get(movie_url, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("vega_movie_links %s failed: %s", movie_url, exc)
        return {"poster": "", "links": [], "info": {}}

    og = soup.find("meta", property="og:image")
    poster = og.get("content", "") if og else ""

    info = _scrape_vega_info(soup)

    # Download links — structure:
    #   <h5>—–== Download Links ==—–</h5>
    #   <div class="download-links-div">
    #     <h3><span>QUALITY_LABEL</span></h3>
    #     <h3><div><a href="URL">Click Here To Download [SIZE]</a></div></h3>
    #     ...
    #   </div>
    content = soup.select_one(".entry-content, article") or soup

    dl_marker = None
    for el in content.find_all(["h5", "h4", "h3", "strong"]):
        if "download links" in el.get_text(strip=True).lower():
            dl_marker = el
            break

    links: list[dict] = []
    if dl_marker:
        # The links live inside the next <div> sibling (div.download-links-div)
        dl_div = dl_marker.find_next_sibling("div")
        if dl_div is None:
            dl_div = dl_marker.parent  # fallback: walk parent's children

        current_quality = ""
        for child in dl_div.find_all(["h3", "h4", "h2"], recursive=True):
            a_el = child.find("a", href=True)
            txt  = child.get_text(strip=True)

            if a_el:
                href = a_el.get("href", "")
                if href.startswith("http") and "vegamovies" not in href:
                    size_m = re.search(r"\[([^\]]+(?:MB|GB)[^\]]*)\]", txt, re.I)
                    size   = size_m.group(1) if size_m else ""
                    if current_quality:
                        links.append({
                            "quality": current_quality,
                            "url":     href,
                            "size":    size,
                        })
                    current_quality = ""
                    continue

            # Plain heading without a link = quality label
            if txt and not any(kw in txt.lower() for kw in
                               ("click here", "download", "thank you", "screenshot",
                                "vegamovies", "winding")):
                current_quality = txt

    return {"poster": poster, "links": links, "info": info}


def format_vega_message(movie_title: str, data: dict, footer: bool = True) -> str:
    """Format a Vegamovies result as HTML for Telegram."""
    links = data.get("links", [])
    info  = data.get("info", {})

    def _info_block() -> str:
        parts: list[str] = []
        if info.get("imdb"):     parts.append(f"⭐ <b>IMDb:</b> {info['imdb']}")
        if info.get("genre"):    parts.append(f"🎭 <b>Genre:</b> {info['genre'][:70]}")
        if info.get("language"): parts.append(f"🗣 <b>Language:</b> {info['language'][:50]}")
        if info.get("quality"):  parts.append(f"🎞 <b>Quality:</b> {info['quality'][:60]}")
        if info.get("runtime"):  parts.append(f"⏱ <b>Runtime:</b> {info['runtime'][:30]}")
        stars = info.get("stars", "")
        if stars:
            cast_list = [x.strip() for x in stars.split(",")][:3]
            parts.append(f"🎬 <b>Cast:</b> {', '.join(cast_list)}")
        return "\n".join(parts)

    lines: list[str] = []
    if movie_title:
        lines.append(f"🌟 <b>{movie_title}</b>")

    ib = _info_block()
    if ib:
        lines.append("━" * 32)
        lines.append(ib)

    if links:
        lines.append("━" * 32)
        lines.append("📥 <b>Download Links (Vegamovies)</b>")
        for lk in links:
            quality = lk.get("quality", "")
            url_    = lk.get("url", "")
            size    = lk.get("size", "")
            label   = f"{quality}  [{size}]" if size else quality
            lines.append(f"\n📦 <b>{label}</b>")
            lines.append(f"   🔗 <a href='{url_}'>Download</a>")
        lines.append("")

    if footer:
        lines.append("━" * 32)
        lines.append("⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  SDMoviesPoint  (https://sd1.sdmoviespoint.trade/)
# ══════════════════════════════════════════════════════════════════════════════

SDMP_BASE = os.getenv("SDMP_BASE_URL", "https://sd1.sdmoviespoint.trade")


def _sdmp_build_url(id_val: str, filename: str) -> str:
    """Build download URL from form id + filename hidden inputs.

    id examples:
      "sdshare.cfd/s"       → https://sdshare.cfd/s/<filename>
      "new3.sdshare.cfd/s"  → https://new3.sdshare.cfd/s/<filename>
      "51.159.212.34"       → http://51.159.212.34/<filename>
    """
    if not id_val or not filename:
        return ""
    is_ip = bool(re.match(r"^\d+\.\d+\.\d+\.\d+", id_val))
    proto = "http" if is_ip else "https"
    return f"{proto}://{id_val}/{filename}"


def _scrape_sdmp_info(content: BeautifulSoup) -> dict[str, str]:
    """Extract metadata from an SDMoviesPoint movie page.

    The info block is a <p> with alternating <strong>Label:</strong> text <br/>.
    """
    info: dict[str, str] = {}
    info_p = None
    for p in content.find_all("p"):
        if p.find("strong") and "full name" in p.get_text().lower():
            info_p = p
            break
    if not info_p:
        return info

    current_label = ""
    for child in info_p.children:
        cname = getattr(child, "name", None)
        if cname is None:
            # NavigableString — value for the current label
            val = str(child).strip()
            if val and current_label:
                lk = current_label.lower()
                if "genre" in lk:
                    info["genre"] = val
                elif "language" in lk:
                    info["language"] = val
                elif "cast" in lk:
                    info["stars"] = val
                elif "imdb rating" in lk:
                    info["imdb"] = val.split("/")[0].strip()
                elif "release date" in lk:
                    info["year"] = val[:4]
        elif cname == "strong":
            current_label = child.get_text(strip=True).rstrip(":")
        elif cname == "br":
            current_label = ""
    return info


def _sdmp_poster_map(soup: BeautifulSoup) -> dict[str, str]:
    """Build alt-text → poster-URL map from TMDB img tags on the page."""
    m: dict[str, str] = {}
    for img in soup.find_all("img", src=True):
        src = img.get("src", "")
        alt = img.get("alt", "").strip()
        if "tmdb" in src and alt:
            m[alt] = src
    return m


def _sdmp_find_poster(title: str, poster_map: dict[str, str]) -> str:
    for alt, src in poster_map.items():
        if title.startswith(alt) or alt.startswith(title[:50]):
            return src
    return ""


def _sdmp_parse_listing(soup: BeautifulSoup, limit: int) -> list[dict]:
    """Extract movie entries from a parsed SDMoviesPoint listing page."""
    poster_map = _sdmp_poster_map(soup)
    base_host  = SDMP_BASE.split("//")[-1].split("/")[0]
    movies: list[dict] = []
    for h3 in soup.find_all("h3"):
        if len(movies) >= limit:
            break
        a = h3.find("a", href=True)
        if not a or base_host not in a.get("href", ""):
            continue
        title = a.get_text(strip=True)
        href  = a["href"]
        if title and href:
            movies.append({"title": title, "url": href,
                           "poster": _sdmp_find_poster(title, poster_map)})
    return movies


def sdmp_latest_movies(page: int = 1, limit: int = 10) -> list[dict]:
    """Fetch latest movies from SDMoviesPoint with WordPress /page/N/ pagination."""
    url = SDMP_BASE + "/" if page == 1 else f"{SDMP_BASE}/page/{page}/"
    try:
        resp = _get(url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("sdmp_latest_movies page=%d failed: %s", page, exc)
        return []
    return _sdmp_parse_listing(soup, limit)


def sdmp_search(query: str, limit: int = 10) -> list[dict]:
    """Search SDMoviesPoint using the WordPress ?s= parameter."""
    url = f"{SDMP_BASE}/?s={urllib.parse.quote(query)}"
    try:
        resp = _get(url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("sdmp_search '%s' failed: %s", query, exc)
        return []
    return _sdmp_parse_listing(soup, limit)


def sdmp_movie_links(movie_url: str) -> dict[str, Any]:
    """Scrape download links from an SDMoviesPoint movie or series page.

    Two layouts:
    - **Movie**:  .dlarea → .dlarea-card per quality → form per server.
    - **Series**: .dlarea → flat <form> elements, one per episode.

    Download URL: ``https://{form[id]}/{form[filename]}``

    Returns::
        {
          "poster": str,
          "links":  [{"label": str, "size": str, "url": str}],
          "info":   {imdb, genre, language, stars, year},
          "is_series": bool,
        }
    """
    try:
        resp = _get(movie_url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("sdmp_movie_links %s failed: %s", movie_url, exc)
        return {"poster": "", "links": [], "info": {}, "is_series": False}

    og = soup.find("meta", property="og:image")
    poster = og.get("content", "") if og else ""

    content   = soup.select_one(".entry-content, article") or soup
    info      = _scrape_sdmp_info(content)
    dlarea    = content.select_one(".dlarea")
    links:    list[dict] = []
    is_series = False

    if dlarea:
        cards = dlarea.select(".dlarea-card")
        if cards:
            # ── Movie layout ──────────────────────────────────────────────
            for card in cards:
                quality_el = card.select_one(".dlarea-card-title")
                quality    = quality_el.get_text(strip=True) if quality_el else ""
                for form in card.find_all("form"):
                    fid   = form.find("input", {"name": "id"})
                    fname = form.find("input", {"name": "filename"})
                    chip  = form.select_one(".dlarea-chip")
                    chip_txt = chip.get_text(" ", strip=True) if chip else ""
                    size_m   = re.search(r"(\d[\d.]*\s*(?:MB|GB))", chip_txt, re.I)
                    size     = size_m.group(1) if size_m else ""
                    dl_url   = _sdmp_build_url(
                        fid.get("value", "") if fid else "",
                        fname.get("value", "") if fname else "",
                    )
                    if dl_url:
                        links.append({"label": quality, "size": size, "url": dl_url})
        else:
            # ── Series layout: flat forms, one per episode ────────────────
            is_series = True
            for form in dlarea.find_all("form"):
                fid   = form.find("input", {"name": "id"})
                fname = form.find("input", {"name": "filename"})
                chip  = form.select_one(".dlarea-chip")
                if not chip:
                    continue
                chip_txt = chip.get_text(" ", strip=True)
                size_m   = re.search(r"(\d[\d.]*\s*(?:MB|GB))", chip_txt, re.I)
                size     = size_m.group(1) if size_m else ""
                label    = re.sub(r"\d[\d.]*\s*(?:MB|GB)", "", chip_txt, flags=re.I).strip()
                dl_url   = _sdmp_build_url(
                    fid.get("value", "") if fid else "",
                    fname.get("value", "") if fname else "",
                )
                if dl_url:
                    links.append({"label": label, "size": size, "url": dl_url})

    return {"poster": poster, "links": links, "info": info, "is_series": is_series}


def format_sdmp_message(movie_title: str, data: dict, footer: bool = True) -> str:
    """Format an SDMoviesPoint result as HTML for Telegram."""
    links     = data.get("links", [])
    info      = data.get("info", {})
    is_series = data.get("is_series", False)

    def _info_block() -> str:
        parts: list[str] = []
        if info.get("imdb"):     parts.append(f"⭐ <b>IMDb:</b> {info['imdb']}")
        if info.get("genre"):    parts.append(f"🎭 <b>Genre:</b> {info['genre'][:70]}")
        if info.get("language"): parts.append(f"🗣 <b>Language:</b> {info['language'][:50]}")
        if info.get("year"):     parts.append(f"📅 <b>Year:</b> {info['year']}")
        stars = info.get("stars", "")
        if stars:
            cast_list = [x.strip() for x in stars.split(",")][:3]
            parts.append(f"🎬 <b>Cast:</b> {', '.join(cast_list)}")
        return "\n".join(parts)

    lines: list[str] = []
    if movie_title:
        icon = "📺" if is_series else "🎞"
        lines.append(f"{icon} <b>{movie_title}</b>")

    ib = _info_block()
    if ib:
        lines.append("━" * 32)
        lines.append(ib)

    if links:
        lines.append("━" * 32)
        hdr = "📥 <b>Episode Download Links</b>" if is_series else "📥 <b>Download Links (SDMoviesPoint)</b>"
        lines.append(hdr)
        for lk in links:
            label = lk.get("label", "")
            size  = lk.get("size", "")
            url_  = lk.get("url", "")
            if not label:
                continue  # skip unlabelled fallback IP links
            badge = f"  [{size}]" if size else ""
            lines.append(f"\n📦 <b>{label}{badge}</b>")
            lines.append(f"   🔗 <a href='{url_}'>Download</a>")
        lines.append("")

    if footer:
        lines.append("━" * 32)
        lines.append("⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  BollyFlix  (https://new.bollyflix.gd/)
#  Post pages: <a class="dl"> → fastdlserver / linksmod; maxbutton → fxlinks (episodes).
#  We expand linksmod → direct host links; fxlinks → per-episode fastdlserver gates.
# ══════════════════════════════════════════════════════════════════════════════

BOLLY_BASE = os.getenv("BOLLYFLIX_BASE_URL", "https://new.bollyflix.gd").rstrip("/")
_BOLLY_LINKSMOD_CAP = 12   # max linksmod pages to expand per post (avoids burst I/O)
_BOLLY_FXLINKS_CAP = 10    # max fxlinks/elinks pages to expand per post


def _bolly_section_signal(text: str) -> bool:
    t = text.strip()
    if len(t) < 6 or len(t) > 220:
        return False
    if set(t) <= {".", "…", "-", " ", "|", "_"}:
        return False
    tl = t.lower()
    if re.search(r"episode\s*\d", tl):
        return True
    if re.search(r"season\s*\d", tl) and any(x in tl for x in ("480p", "720p", "1080p", "2160p", "web")):
        return True
    if any(x in tl for x in ("480p", "720p", "1080p", "2160p", "hevc", "web-dl", "webdl")):
        return True
    if "mb/e" in tl or "gb]" in tl:
        return True
    if tl.startswith("season ") and "[" in tl:
        return True
    return False


def _bolly_scrape_info(content: BeautifulSoup) -> dict[str, str]:
    info: dict[str, str] = {}
    for h2 in content.find_all("h2"):
        ht = h2.get_text(strip=True).lower()
        if "details" not in ht:
            continue
        ul = h2.find_next_sibling("ul")
        if not ul:
            continue
        for li in ul.find_all("li"):
            strong = li.find("strong")
            if not strong:
                continue
            label = strong.get_text(strip=True).rstrip(":").lower()
            full = li.get_text(" ", strip=True)
            val = full.replace(strong.get_text(strip=True), "", 1).strip(" :")
            if "full name" in label:
                info["title"] = val
            elif "language" in label:
                info["language"] = val
            elif "released year" in label or "year" == label:
                info["year"] = val[:4] if val else val
            elif "genre" in label:
                info["genre"] = val
            elif "cast" in label:
                info["stars"] = val
            elif "quality" in label:
                info["quality"] = val
            elif "size" in label:
                info["size"] = val
            elif "source" in label:
                info["source"] = val
    return info


def _bolly_expand_linksmod(page_url: str) -> list[tuple[str, str]]:
    """Fetch a linksmod unlock page and return (short_name, direct_url) pairs."""
    try:
        resp = _get(page_url, timeout=28)
        sp = BeautifulSoup(resp.text, "html.parser")
        well = sp.select_one(".view-well") or sp.select_one(".view-links")
        if not well:
            return []
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for a in well.find_all("a", href=True):
            u = a["href"].strip()
            if not u.startswith("http"):
                continue
            ul = u.lower()
            if "linksmod" in ul:
                continue
            if u in seen:
                continue
            seen.add(u)
            host = urlparse(u).netloc.lower().replace("www.", "")
            short = host.split(".")[0].title() if host else "Host"
            out.append((short, u))
        return out
    except Exception as exc:
        log.debug("bolly linksmod %s: %s", page_url, exc)
        return []


def _bolly_expand_fxlinks(page_url: str, section: str) -> list[dict[str, str]]:
    """fxlinks /elinks/ page lists episodes as h3 > a → fastdlserver."""
    try:
        resp = _get(page_url, timeout=30)
        sp = BeautifulSoup(resp.text, "html.parser")
        main = sp.select_one(".entry-content, article, main") or sp
        rows: list[dict[str, str]] = []
        for h3 in main.find_all("h3"):
            a = h3.find("a", href=True)
            if not a:
                continue
            href = a["href"].strip()
            if "fastdlserver" not in href.lower():
                continue
            ep = a.get_text(strip=True) or "Episode"
            label = f"{section} — {ep}" if section else ep
            rows.append({
                "label": label[:200],
                "name": "FastDL / G‑Drive gate",
                "url": href,
            })
        return rows
    except Exception as exc:
        log.debug("bolly fxlinks %s: %s", page_url, exc)
        return []


def _bolly_collect_candidates(content: BeautifulSoup) -> list[dict[str, str]]:
    """Scan post body in order: quality headings update `section`, dl/maxbutton → rows."""
    section = "Download"
    out: list[dict[str, str]] = []
    for el in content.find_all(["h2", "h3", "h4", "h5", "pre", "a"]):
        if el.name != "a":
            txt = el.get_text(" ", strip=True)
            if txt and _bolly_section_signal(txt):
                section = txt[:200]
            continue
        href = (el.get("href") or "").strip()
        if not href.startswith("http"):
            continue
        host = urlparse(href).netloc.lower()
        if "bollyflix" in host and "bollyflixcdn" not in href:
            continue
        classes = " ".join(el.get("class") or [])
        if "maxbutton" not in classes and "dl" not in classes:
            continue
        kind = "other"
        hlow = href.lower()
        if "linksmod." in hlow:
            kind = "linksmod"
        elif "fxlinks.rest" in hlow or "/elinks/" in hlow:
            kind = "fxlinks"
        elif "fastdlserver" in hlow:
            kind = "fastdl"
        out.append({
            "section": section,
            "href": href,
            "text": el.get_text(strip=True) or "Link",
            "kind": kind,
        })
    return out


def _bolly_parse_article_list(soup: BeautifulSoup, limit: int) -> list[dict[str, str]]:
    movies: list[dict[str, str]] = []
    for art in soup.select("article.latestPost, article.excerpt, article"):
        if len(movies) >= limit:
            break
        h2a = art.select_one("h2.title a, h2.front-view-title a, header h2 a")
        if not h2a or not h2a.get("href"):
            continue
        title = h2a.get_text(strip=True)
        link = h2a["href"].strip()
        img = art.select_one("img")
        poster = ""
        if img:
            poster = (img.get("src") or img.get("data-src") or "").strip()
        if poster.startswith("//"):
            poster = "https:" + poster
        if title and link:
            movies.append({"title": title, "url": link, "poster": poster, "source": "bolly"})
    return movies


def bollyflix_latest_movies(page: int = 1, limit: int = 10) -> list[dict[str, str]]:
    """Latest posts from BollyFlix homepage."""
    url = f"{BOLLY_BASE}/" if page == 1 else f"{BOLLY_BASE}/page/{page}/"
    try:
        resp = _get(url, timeout=28)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("bollyflix_latest_movies page=%s: %s", page, exc)
        return []
    return _bolly_parse_article_list(soup, limit)


def bollyflix_search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """WordPress search on BollyFlix."""
    q = urllib.parse.quote_plus(query)
    url = f"{BOLLY_BASE}/?s={q}"
    try:
        resp = _get(url, timeout=28)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("bollyflix_search %r: %s", query, exc)
        return []
    return _bolly_parse_article_list(soup, limit)


def bollyflix_movie_links(movie_url: str) -> dict[str, Any]:
    """Scrape BollyFlix post: expand linksmod to file-host URLs; fxlinks to per-episode fastdl."""
    empty: dict[str, Any] = {"poster": "", "links": [], "info": {}, "is_series": False}
    try:
        resp = _get(movie_url, timeout=35)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("bollyflix_movie_links %s: %s", movie_url, exc)
        return empty

    og = soup.find("meta", property="og:image")
    poster = og.get("content", "") if og else ""

    content = soup.select_one(".post-single-content .entry-content, .entry-content, article") or soup
    info = _bolly_scrape_info(content)
    raw = _bolly_collect_candidates(content)

    lm_jobs: list[str] = []
    fx_jobs: list[tuple[str, str]] = []
    seen_lm: set[str] = set()
    seen_fx: set[str] = set()
    for row in raw:
        if row["kind"] == "linksmod" and row["href"] not in seen_lm and len(lm_jobs) < _BOLLY_LINKSMOD_CAP:
            seen_lm.add(row["href"])
            lm_jobs.append(row["href"])
        elif row["kind"] == "fxlinks" and row["href"] not in seen_fx and len(fx_jobs) < _BOLLY_FXLINKS_CAP:
            seen_fx.add(row["href"])
            fx_jobs.append((row["href"], row["section"]))

    lm_map: dict[str, list[tuple[str, str]]] = {}
    fx_map: dict[str, list[dict[str, str]]] = {}
    if lm_jobs or fx_jobs:
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(lm_jobs) + len(fx_jobs)))) as pool:
            fmap: dict[Any, tuple[str, str, str]] = {}
            for h in lm_jobs:
                fut = pool.submit(_bolly_expand_linksmod, h)
                fmap[fut] = ("lm", h, "")
            for h, sec in fx_jobs:
                fut = pool.submit(_bolly_expand_fxlinks, h, sec)
                fmap[fut] = ("fx", h, sec)
            for fut in as_completed(fmap):
                kind, h, sec = fmap[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    log.debug("bolly expand %s %s: %s", kind, h[:60], exc)
                    res = [] if kind == "lm" else []
                if kind == "lm":
                    lm_map[h] = res
                else:
                    fx_map[h] = res

    seen_url: set[str] = set()
    links: list[dict[str, str]] = []

    def _add(label: str, name: str, url: str) -> None:
        if not url or url in seen_url:
            return
        seen_url.add(url)
        links.append({"label": label[:200], "name": name[:80], "url": url})

    expanded_fx = False
    for row in raw:
        sec = row["section"]
        href = row["href"]
        txt = row["text"]
        kind = row["kind"]
        if kind == "linksmod":
            pairs = lm_map.get(href)
            if pairs:
                for host_short, u in pairs:
                    _add(sec, host_short, u)
            else:
                _add(sec, txt or "LinksMod", href)
        elif kind == "fxlinks":
            ep_rows = fx_map.get(href, [])
            if ep_rows:
                expanded_fx = True
                for er in ep_rows:
                    _add(er["label"], er["name"], er["url"])
            else:
                _add(sec, "FXLinks (episodes)", href)
        else:
            _add(sec, txt, href)

    body_low = (content.get_text(" ", strip=True) or "").lower()
    is_series = expanded_fx or ("series details" in body_low) or any(
        "episode" in lk.get("label", "").lower() for lk in links
    )

    return {"poster": poster, "links": links, "info": info, "is_series": is_series}


def format_bollyflix_message(movie_title: str, data: dict[str, Any], footer: bool = True) -> str:
    """Format BollyFlix scrape result as Telegram HTML."""
    links = data.get("links", [])
    info = data.get("info", {})
    is_series = data.get("is_series", False)

    lines: list[str] = []
    disp = movie_title or info.get("title", "")
    if disp:
        icon = "📺" if is_series else "🎬"
        lines.append(f"{icon} <b>{disp}</b>")

    ibits: list[str] = []
    if info.get("quality"):
        ibits.append(f"📐 <b>Quality:</b> {info['quality'][:80]}")
    if info.get("language"):
        ibits.append(f"🗣 <b>Language:</b> {info['language'][:60]}")
    if info.get("year"):
        ibits.append(f"📅 <b>Year:</b> {info['year']}")
    if info.get("genre"):
        ibits.append(f"🎭 <b>Genre:</b> {info['genre'][:80]}")
    if ibits:
        lines.append("━" * 32)
        lines.extend(ibits)

    if not links:
        if lines:
            lines.append("\n❌ No download links parsed.")
        return "\n".join(lines) if lines else ""

    lines.append("━" * 32)
    hdr = "📥 <b>Episode / mirror links</b>" if is_series else "📥 <b>Download links (BollyFlix)</b>"
    lines.append(hdr)
    lines.append(
        "<i>Linksmod entries are expanded to direct hosts where possible. "
        "FastDL opens a browser gate (JS) for G‑Drive / GdFlix.</i>\n"
    )

    by_label: dict[str, list] = {}
    for lk in links:
        by_label.setdefault(lk.get("label", "Download"), []).append(lk)

    for label, group in by_label.items():
        lines.append(f"\n📦 <b>{label}</b>")
        parts = [f"<a href='{x['url']}'>{x.get('name', 'Link')}</a>" for x in group]
        lines.append("   🔗 " + " · ".join(parts))

    if footer:
        lines.append("\n" + "━" * 32)
        lines.append("⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  Gadgetsweb / Cryptoinsights resolver
#  Chain: gadgetsweb → cryptoinsights (extract 'o') → atob→atob→rot13→atob→JSON
#         → atob → hblinks.org → final HubCloud/HubDrive/GoFile links
# ══════════════════════════════════════════════════════════════════════════════

_GW_AD_DOMAINS = {"gadgetsweb.xyz", "gadgetsweb.com", "cryptoinsights.site", "greenmountmotors.com"}

_FINAL_HOST_KEYWORDS = {
    "hubcloud", "hubdrive", "gofile", "pixeldrain", "mediafire",
    "gdrive", "filepress", "gdflix", "fastdl", "megaup",
    "drive.google", "hblinks",
}


_MEDIATOR_BTN_SELECTORS = [
    "#verify_btn",
    "#verify_button",
    "#verify_button2",
    "#btn_download",
    "#downloadButton",
    "a.get-link",
    "a.btn-primary",
    "[class*='get-link']",
    "[class*='continue']",
    "[id*='verify']",
    "[id*='download']",
    "[id*='continue']",
]

_MEDIATOR_BTN_TEXTS = [
    "continue", "verify", "get link", "click here", "go to",
    "download", "generate", "get download", "click to continue",
]

def _find_mediator_button(page):
    """Find a clickable 'continue/verify/get-link' button on any mediator page.

    Tries known selectors first, then falls back to text-matching any
    visible anchor/button/span/div whose text matches common mediator patterns.
    """
    for sel in _MEDIATOR_BTN_SELECTORS:
        # Some selectors might match multiple elements, find the first visible one
        elements = page.query_selector_all(sel)
        for el in elements:
            if el.is_visible():
                return el

    # Fallback: search all visible elements by text content
    for tag in ("a", "button", "span", "div"):
        elements = page.query_selector_all(tag)
        for el in elements:
            if not el.is_visible():
                continue
            text = (el.text_content() or "").strip().lower()
            if any(pattern in text for pattern in _MEDIATOR_BTN_TEXTS):
                return el
    return None


def _check_for_final_link(page) -> str | None:
    """Scan the page for any anchor whose href points to a known final host.

    Returns the first matching URL or None.
    """
    all_anchors = page.query_selector_all("a[href]")
    for a in all_anchors:
        href = a.get_attribute("href") or ""
        if not href.startswith("http"):
            continue
        host = urlparse(href).netloc.lower()
        if any(kw in host for kw in _FINAL_HOST_KEYWORDS):
            return href
        # Also check if the button itself changed to point to a final link
        if "hblinks" in host:
            return href
    return None


def _resolve_gadgetsweb_playwright(gw_url: str) -> list[dict]:
    """Navigate gadgetsweb URL in a real browser and automatically bypass any
    intermediate mediator/redirect pages to reach the final download links.

    Returns list of {"label": str, "url": str} for file-host links found.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        log.debug("Playwright not available, skipping gadgetsweb browser fallback")
        return []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()

            page.goto(gw_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(3000)

            final_url = None

            # Adaptive loop: keep clicking through mediator pages (max 60s total)
            for overall_attempt in range(6):
                current_host = urlparse(page.url).netloc.lower()

                # Check if we already landed on a final destination
                if any(kw in current_host for kw in _FINAL_HOST_KEYWORDS):
                    final_url = page.url
                    break

                # Scan page for a link pointing to a final host
                found = _check_for_final_link(page)
                if found:
                    final_url = found
                    break

                # Find and click mediator button
                btn = _find_mediator_button(page)
                if not btn:
                    # No button found; wait a bit and try once more
                    page.wait_for_timeout(3000)
                    btn = _find_mediator_button(page)
                    if not btn:
                        break

                btn.click()
                page.wait_for_timeout(2000)

                # After click: check if a countdown/timer appeared
                # Generic detection: look for any visible element with timer-like content
                has_countdown = page.evaluate('''() => {
                    const selectors = ['#countdown', '[class*="countdown"]', '[class*="timer"]',
                                       '[id*="countdown"]', '[id*="timer"]', '.loader'];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) return true;
                    }
                    return false;
                }''')

                if has_countdown:
                    # Wait for countdown (up to 15s — covers most 5-10s timers)
                    page.wait_for_timeout(15000)

                    # After countdown, check if button now has a final link
                    found = _check_for_final_link(page)
                    if found:
                        final_url = found
                        break

                    # Check if the same button's href updated
                    btn = _find_mediator_button(page)
                    if btn:
                        href = btn.get_attribute("href") or ""
                        if href.startswith("http"):
                            host = urlparse(href).netloc.lower()
                            if any(kw in host for kw in _FINAL_HOST_KEYWORDS) or "hblinks" in host:
                                final_url = href
                                break
                else:
                    # No countdown — maybe ad popup opened; wait and retry
                    page.wait_for_timeout(2000)

            browser.close()

            if not final_url:
                return []

            # If it's an hblinks page, fetch and scrape it via HTTP
            if "hblinks" in urlparse(final_url).netloc.lower():
                try:
                    r = requests.get(
                        final_url, timeout=20, verify=False,
                        headers=HEADERS,
                    )
                    return _scrape_hblinks_page(r.text)
                except Exception as exc:
                    log.debug("_resolve_gadgetsweb_playwright hblinks fetch failed: %s", exc)
                    return [{"label": "HBLinks Page", "url": final_url}]

            # Direct file-host link
            domain_hint = urlparse(final_url).netloc.split(".")[0].capitalize()
            return [{"label": f"{domain_hint} Download", "url": final_url}]

    except Exception as exc:
        log.debug("_resolve_gadgetsweb_playwright %s failed: %s", gw_url, exc)
        return []


def _scrape_hblinks_page(html: str) -> list[dict]:
    """Extract file-host download links from an hblinks.org page."""
    soup = BeautifulSoup(html, "html.parser")

    page_title = soup.title.string if soup.title else ""
    q_hint = ""
    q_m = re.search(r"(4K|2160p|1080p|720p|480p|360p)", page_title, re.I)
    if q_m:
        q_hint = q_m.group(1)

    links: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http") or "hblinks" in href or href in seen:
            continue
        seen.add(href)
        txt = a.get_text(strip=True)
        if not txt:
            domain = urlparse(href).netloc
            if "hubcloud" in domain:
                txt = "HubCloud"
            elif "hubdrive" in domain:
                txt = "HubDrive"
            elif "gofile" in domain:
                txt = "GoFile"
            else:
                txt = domain.split(".")[0].capitalize()
        if q_hint:
            txt = f"{q_hint} - {txt}"
        links.append({"label": txt, "url": href})
    return links


def _rot13(s: str) -> str:
    out = []
    for c in s:
        if "a" <= c <= "z":
            out.append(chr((ord(c) - 97 + 13) % 26 + 97))
        elif "A" <= c <= "Z":
            out.append(chr((ord(c) - 65 + 13) % 26 + 65))
        else:
            out.append(c)
    return "".join(out)


def _sb64(s: str) -> bytes:
    """Base64 decode with auto-padding."""
    s = s.strip()
    pad = 4 - len(s) % 4
    if pad < 4:
        s += "=" * pad
    return base64.b64decode(s)


def _decode_gw_o(o_val: str) -> str | None:
    """
    Decode the 'o' localStorage value set by cryptoinsights.site:
    atob → atob → ROT13 → atob → JSON.parse → decode "o" field → final URL
    """
    try:
        d1 = _sb64(o_val).decode()
        d2 = _sb64(d1).decode("latin-1")
        d3 = _rot13(d2)
        d4 = _sb64(d3).decode()
        data = json.loads(d4)
        final_b64 = data.get("o", "")
        return _sb64(final_b64).decode() if final_b64 else None
    except Exception:
        return None


def _resolve_gadgetsweb(gw_url: str) -> list[dict]:
    """
    Resolve a gadgetsweb.xyz / greenmountmotors.com ad-gate URL to final links.
    Returns a list of {"label": str, "url": str} dicts.  On failure returns [].

    Strategy:
    1. Fast path: fetch greenmountmotors.com/?id= which embeds the 'o' value
       in its HTML (no JS execution needed). Decode it to get the hblinks URL.
    2. Fallback: Playwright navigates the full redirect chain like a real browser
    """
    # ── Fast path: HTTP decode via greenmountmotors ───────────────────────────
    try:
        parsed = urlparse(gw_url)
        qs = urllib.parse.parse_qs(parsed.query)
        gw_id = qs.get("id", [None])[0]
        if not gw_id:
            return _resolve_gadgetsweb_playwright(gw_url)

        # greenmountmotors.com embeds the 'o' value directly in its response HTML
        gm_url = f"https://greenmountmotors.com/?id={gw_id}"
        gm_text = None
        try:
            r2 = _get(gm_url, timeout=20)
            gm_text = r2.text
        except Exception:
            pass

        if gm_text:
            m = re.search(r"s\('o','([^']+)'", gm_text)
            if m:
                final_url = _decode_gw_o(m.group(1))
                if final_url:
                    if "hblinks.org" in final_url:
                        try:
                            r3 = _get(final_url, timeout=20)
                            links = _scrape_hblinks_page(r3.text)
                            if links:
                                return links
                        except Exception:
                            pass
                    else:
                        domain_hint = urlparse(final_url).netloc.split(".")[0].capitalize()
                        return [{"label": f"{domain_hint} Download", "url": final_url}]

        # Legacy fallback: try cryptoinsights directly (may be down)
        crypto_url = f"https://cryptoinsights.site/?id={gw_id}"
        try:
            r2 = _get(crypto_url, timeout=15)
            m = re.search(r"s\('o','([^']+)'", r2.text)
            if m:
                final_url = _decode_gw_o(m.group(1))
                if final_url:
                    if "hblinks.org" in final_url:
                        r3 = _get(final_url, timeout=20)
                        links = _scrape_hblinks_page(r3.text)
                        if links:
                            return links
                    else:
                        domain_hint = urlparse(final_url).netloc.split(".")[0].capitalize()
                        return [{"label": f"{domain_hint} Download", "url": final_url}]
        except Exception:
            pass

    except Exception as exc:
        log.debug("_resolve_gadgetsweb HTTP path %s failed: %s", gw_url, exc)

    # ── Fallback: Playwright full redirect chain ──────────────────────────────
    return _resolve_gadgetsweb_playwright(gw_url)


def _expand_gw_links(raw_links: list[dict], orig_label: str = "") -> list[dict]:
    """
    For a list of links, replace any gadgetsweb URLs with their resolved direct links.
    Non-gadgetsweb links are passed through unchanged.
    Runs multiple gadgetsweb resolves in parallel (up to 4 threads).
    """
    gw_indices = [i for i, lk in enumerate(raw_links)
                  if urlparse(lk["url"]).netloc in _GW_AD_DOMAINS]

    if not gw_indices:
        return raw_links

    resolved: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_resolve_gadgetsweb, raw_links[i]["url"]): i
                   for i in gw_indices}
        for fut in as_completed(futures, timeout=90):
            i = futures[fut]
            try:
                resolved[i] = fut.result()
            except Exception:
                resolved[i] = []

    result: list[dict] = []
    for i, lk in enumerate(raw_links):
        if i in resolved:
            direct = resolved[i]
            if direct:
                result.extend(direct)
            else:
                # Keep original if resolve failed, update label
                result.append({
                    "label": lk["label"] or orig_label,
                    "url":   lk["url"],
                })
        else:
            result.append(lk)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  HDHub4u  (https://new2.hdhub4u.cl/)  — primary source
# ══════════════════════════════════════════════════════════════════════════════

HDHUB_BASE        = os.getenv("HDHUB_BASE_URL", "https://new2.hdhub4u.cl")
_HDHUB_SEARCH_API = os.getenv(
    "HDHUB_SEARCH_API",
    "https://search.pingora.fyi/collections/post/documents/search",
)
_HDHUB_SKIP_LABELS = {"stream"}
_HDHUB_SEARCH_ALERT_ENABLED = os.getenv("HDHUB_SEARCH_ALERT_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
_HDHUB_SEARCH_ALERT_COOLDOWN = max(
    300, int(os.getenv("HDHUB_SEARCH_ALERT_COOLDOWN", "1800"))
)
_hdhub_search_alert_last_at = 0.0


_QUERY_STOPWORDS = frozenset({
    "a", "an", "and", "or", "the", "of", "in", "on", "to", "for", "with",
})


def _query_tokens(query: str) -> list[str]:
    return [
        t for t in re.split(r"\s+", (query or "").strip().lower())
        if len(t) >= 2 and t not in _QUERY_STOPWORDS
    ]


def _title_matches_query(title: str, query: str) -> bool:
    tokens = _query_tokens(query)
    if not tokens:
        return False
    title_l = (title or "").lower()
    title_words = re.findall(r"[a-z0-9]+", title_l)

    def _token_matches(tok: str) -> bool:
        if tok in title_l:
            return True
        if len(tok) < 4:
            return False
        for word in title_words:
            if len(word) < 3:
                continue
            if SequenceMatcher(None, tok, word).ratio() >= 0.85:
                return True
        return False

    return all(_token_matches(tok) for tok in tokens)


def _hdhub_page_url(permalink: str) -> str:
    if permalink.startswith("http"):
        return permalink
    return HDHUB_BASE.rstrip("/") + "/" + permalink.lstrip("/")


def _hdhub_search_alert_chat_id() -> int | None:
    for key in ("HDHUB_SEARCH_ALERT_CHAT_ID", "ADMIN_ID", "OWNER_ID", "MARKET_ALERT_CHAT_ID"):
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return None


def _notify_hdhub_search_broken(query: str, reason: str, *, severity: str = "error") -> None:
    """Telegram alert when HDHub4u search pipeline fails (cooldown to avoid spam)."""
    if not _HDHUB_SEARCH_ALERT_ENABLED:
        return
    chat_id = _hdhub_search_alert_chat_id()
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not chat_id or not bot_token:
        log.warning("HDHub search alert skipped (BOT_TOKEN or alert chat id missing)")
        return

    global _hdhub_search_alert_last_at
    now = time.time()
    if now - _hdhub_search_alert_last_at < _HDHUB_SEARCH_ALERT_COOLDOWN:
        return
    _hdhub_search_alert_last_at = now

    icon = "\u26a0\ufe0f" if severity == "warn" else "\U0001f6a8"
    text = (
        f"{icon} <b>HDHub4u search issue</b>\n\n"
        f"Query: <code>{html.escape(query)}</code>\n"
        f"Reason: {html.escape(reason)}\n"
        f"Base: <code>{html.escape(HDHUB_BASE)}</code>\n"
        f"API: <code>{html.escape(_HDHUB_SEARCH_API)}</code>\n\n"
        "<i>Search may need a fix — check Pingora/Typesense or site changes.</i>"
    )
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.error("HDHub search alert HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.error("HDHub search alert failed: %s", exc)


def _hdhub_search_typesense(
    query: str,
    limit: int = 10,
    *,
    timeout: int = 20,
) -> tuple[list[dict], str | None]:
    """Search via HDHub4u Typesense proxy (requires site Referer)."""
    headers = {
        **HEADERS,
        "Referer": HDHUB_BASE.rstrip("/") + "/",
        "Origin": HDHUB_BASE.rstrip("/"),
    }
    params = {
        "q": query,
        "query_by": "post_title,category,stars,director,imdb_id",
        "query_by_weights": "4,2,2,2,4",
        "sort_by": "sort_by_date:desc",
        "limit": limit,
        "highlight_fields": "none",
        "use_cache": "true",
    }
    try:
        if _CFFI_AVAILABLE:
            resp = cffi_requests.get(
                _HDHUB_SEARCH_API,
                params=params,
                headers=headers,
                impersonate="chrome",
                timeout=timeout,
            )
        else:
            sess = requests.Session()
            sess.headers.update(headers)
            resp = sess.get(_HDHUB_SEARCH_API, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        log.warning("hdhub_search typesense '%s' failed: %s", query, reason)
        return [], reason

    movies: list[dict] = []
    for hit in data.get("hits", []):
        doc = hit.get("document", {})
        title = doc.get("post_title", "")
        permalink = doc.get("permalink", "")
        thumbnail = doc.get("post_thumbnail", "")
        if not title or not permalink:
            continue
        if not _title_matches_query(title, query):
            continue
        movies.append({
            "title": title,
            "url": _hdhub_page_url(permalink),
            "poster": thumbnail,
        })
        if len(movies) >= limit:
            break
    return movies, None


def _hdhub_search_latest_scan(query: str, limit: int = 10, max_pages: int = 8) -> list[dict]:
    """Fallback: scan recent listing pages and match query tokens in titles."""
    tokens = _query_tokens(query)
    if not tokens:
        return []
    matches: list[dict] = []
    seen_urls: set[str] = set()
    for page in range(1, max_pages + 1):
        batch = hdhub_latest_movies(page, 50)
        if not batch:
            break
        for movie in batch:
            title = movie.get("title", "")
            url = movie.get("url", "")
            if not title or not url or url in seen_urls:
                continue
            if not _title_matches_query(title, query):
                continue
            seen_urls.add(url)
            matches.append({"title": title, "url": url, "poster": movie.get("poster", "")})
            if len(matches) >= limit:
                return matches
    return matches


def _hdhub_clean_poster(src: str) -> str:
    """Normalise i0.wp.com proxy URLs to the original TMDB URL."""
    m = re.search(r"i\d\.wp\.com/(.+?)(?:\?|$)", src)
    return "https://" + m.group(1) if m else src


def _scrape_hdhub_info(text: str) -> dict[str, str]:
    """Extract movie metadata from plain text of an HDHub4u page."""
    info: dict[str, str] = {}
    patterns = {
        "imdb":     r"iMDB Rating:\s*([^\n\[]+)",
        "genre":    r"Genre:\s*([^\n]+)",
        "stars":    r"Stars:\s*([^\n]+)",
        "director": r"Director:\s*([^\n]+)",
        "language": r"Language:\s*([^\n]+)",
        "quality":  r"Quality:\s*([^\n]+)",
        "rating":   r"Rating:\s*([^\n]+)",
        "genres":   r"Genres:\s*([^\n]+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text, re.I)
        if m:
            val = m.group(1).strip().rstrip("/").strip()
            dest = "genre" if key == "genres" else ("imdb" if key == "rating" and "imdb" not in info else key)
            if dest not in info:
                info[dest] = val
    return info


def hdhub_latest_movies(page: int = 1, limit: int = 10) -> list[dict]:
    """Fetch latest movies from HDHub4u homepage with WordPress /page/N/ pagination."""
    url = HDHUB_BASE + "/" if page == 1 else f"{HDHUB_BASE}/page/{page}/"
    try:
        resp = _get(url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("hdhub_latest_movies page=%d failed: %s", page, exc)
        return []

    movies: list[dict] = []
    for li in soup.select("ul.recent-movies li.thumb"):
        if len(movies) >= limit:
            break
        img = li.find("img", src=True)
        fig = li.find("figcaption")
        a   = li.find("a", href=True)
        if not (a and fig):
            continue
        title  = fig.get_text(strip=True)
        href   = a["href"]
        poster = _hdhub_clean_poster(img.get("src", "")) if img else ""
        if title and href:
            movies.append({"title": title, "url": href, "poster": poster})
    return movies


def hdhub_search(query: str, limit: int = 10, *, fast: bool = False) -> list[dict]:
    """Search HDHub4u via Typesense proxy, then recent-page scan fallback."""
    q = (query or "").strip()
    if not q:
        return []

    ts_timeout = 8 if fast else 20
    movies, ts_error = _hdhub_search_typesense(q, limit, timeout=ts_timeout)
    if movies:
        return movies

    scan_pages = 2 if fast else 8
    fallback = _hdhub_search_latest_scan(q, limit, max_pages=scan_pages)
    if fallback:
        if ts_error:
            _notify_hdhub_search_broken(
                q,
                f"Typesense failed ({ts_error}); using latest-page fallback only",
                severity="warn",
            )
        return fallback

    if ts_error:
        reason = f"Typesense failed ({ts_error}) and latest-page scan found nothing"
    else:
        reason = "Typesense returned no hits and latest-page scan found nothing"
    _notify_hdhub_search_broken(q, reason)
    return []


def hdhub_movie_links(movie_url: str, *, fast: bool = False) -> dict[str, Any]:
    """Scrape download links from an HDHub4u movie or series page.

    Returns::
        {
          "poster":    str,
          "info":      {imdb, genre, stars, director, language, quality},
          "links":     [{"label": str, "url": str}],
          "episodes":  [{"ep": str, "qualities": {"720p": [{"label","url"}], ...}}],
          "is_series": bool,
        }
    """
    soup = None
    page_timeout = 12 if fast else 25
    page_retries = 0 if fast else 2
    try:
        resp = _get(movie_url, timeout=page_timeout, retries=page_retries)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.warning("hdhub_movie_links initial fetch %s failed: %s", movie_url, exc)

    # Check if the static fetch found any episode/download links;
    # if not, try Playwright (JS rendering) as optional fallback.
    _needs_render = False
    if soup is not None:
        _test_content = soup.select_one("main.page-body, .entry-content, article") or soup
        _test_headings = _test_content.find_all(["h2", "h3", "h4"])
        _has_dl_section = any("download links" in h.get_text(strip=True).lower() for h in _test_headings)
        _found_links = False
        if _has_dl_section:
            for h in _test_headings:
                a_tag = h.find("a", href=True)
                if a_tag and a_tag["href"].startswith("http"):
                    _found_links = True
                    break
            if not _found_links:
                # Also check for links immediately after headings
                for h in _test_headings:
                    nxt = h.find_next_sibling()
                    if nxt and nxt.find("a", href=True):
                        _found_links = True
                        break
        if not _found_links:
            _needs_render = True
    else:
        _needs_render = True

    if _needs_render and not fast:
        log.info("hdhub_movie_links: no links in static HTML, trying Playwright for %s", movie_url)
        rendered_html = _get_rendered_html(movie_url, timeout=30, wait_ms=5000)
        if rendered_html:
            soup = BeautifulSoup(rendered_html, "html.parser")
        elif soup is None:
            return {"poster": "", "info": {}, "links": [], "episodes": [], "is_series": False}
    elif _needs_render and fast:
        log.info("hdhub_movie_links: fast mode — skipping Playwright for %s", movie_url)
        if soup is None:
            return {"poster": "", "info": {}, "links": [], "episodes": [], "is_series": False}

    og = soup.find("meta", property="og:image")
    poster = _hdhub_clean_poster(og.get("content", "")) if og else ""

    content   = soup.select_one("main.page-body, .entry-content, article") or soup
    full_text = content.get_text("\n", strip=True)
    info      = _scrape_hdhub_info(full_text)

    # ── Collect all headings in document order ────────────────────────────────
    all_h = content.find_all(["h2", "h3", "h4"])
    dl_idx   = -1
    ep_sec   = -1
    for i, h in enumerate(all_h):
        t = h.get_text(strip=True).lower()
        if dl_idx == -1 and "download links" in t:
            dl_idx = i
        elif dl_idx >= 0 and ep_sec == -1 and "single episode" in t:
            ep_sec = i

    links:    list[dict] = []
    episodes: list[dict] = []

    _EP_PAT = re.compile(r"EP(?:i?SODE)?\s*\d+", re.I)

    def _is_ad_url(href: str) -> bool:
        return urlparse(href).netloc in _GW_AD_DOMAINS

    # ── Flat pack/quality links (between DL and Single Episode markers) ────────
    end_pack = ep_sec if ep_sec > 0 else len(all_h)
    flat_has_episodes = False
    for h in all_h[dl_idx + 1: end_pack]:
        all_a = [a for a in h.find_all("a", href=True) if a["href"].startswith("http")]
        if not all_a:
            continue
        first_label = all_a[0].get_text(strip=True)
        if _EP_PAT.search(first_label):
            flat_has_episodes = True
            break

    if flat_has_episodes:
        # ── Flat-episode layout (e.g. Undekhi): each h3 = "EPiSODE N | WATCH" ──
        for h in all_h[dl_idx + 1: end_pack]:
            all_a = [a for a in h.find_all("a", href=True) if a["href"].startswith("http")]
            if not all_a:
                continue
            first_label = all_a[0].get_text(strip=True)
            if not _EP_PAT.search(first_label):
                continue
            ep_entry: dict[str, Any] = {"ep": first_label, "qualities": {}}
            # Collect download links and watch links separately
            dl_links  = []
            wt_links  = []
            for a in all_a:
                lbl  = a.get_text(strip=True)
                href = a["href"]
                if lbl.lower() in ("watch", "watch online", "player-2", "player 2"):
                    wt_links.append({"label": "\U0001f4fa Watch Now", "url": href})
                elif _is_ad_url(href):
                    # Keep gadgetsweb links — _expand_gw_links will resolve them later
                    dl_links.append({"label": lbl or "\U0001f4e5 Download", "url": href})
                else:
                    dl_links.append({"label": lbl, "url": href})
            # Combine: direct downloads first, then watch links
            combined = dl_links + wt_links
            if combined:
                ep_entry["qualities"]["Download"] = combined
                episodes.append(ep_entry)
    else:
        # ── Normal pack/quality layout (Lukkhe ZIP packs, single movies) ──────
        # Include gadgetsweb links here — _expand_gw_links will resolve them later
        for h in all_h[dl_idx + 1: end_pack]:
            for a in h.find_all("a", href=True):
                href  = a.get("href", "")
                label = a.get_text(strip=True)
                if href.startswith("http") and label.lower() not in _HDHUB_SKIP_LABELS:
                    if label.lower() in ("watch", "watch online", "player-2", "player 2"):
                        label = "\U0001f4fa Watch Now"
                    links.append({"label": label, "url": href})

    # ── Series: detect and parse episode sections (Lukkhe-style div.Z1hOCe) ───
    is_series = ep_sec >= 0 or flat_has_episodes

    # Find the "Single Episode" h2 element, then its next sibling div.Z1hOCe
    ep_h2 = all_h[ep_sec] if ep_sec >= 0 else None
    if ep_h2 is not None:
        z_div = None
        for candidate in ep_h2.find_next_siblings():
            if getattr(candidate, "name", None) == "div":
                z_div = candidate
                break

        if z_div:
            all_ep_h4 = z_div.find_all("h4")
            current_ep: dict[str, Any] | None = None
            for h4 in all_ep_h4:
                raw   = h4.get_text(" ", strip=True)
                ext_a = [a for a in h4.find_all("a", href=True) if a["href"].startswith("http")]
                if not ext_a and _EP_PAT.search(raw):
                    if current_ep and current_ep["qualities"]:
                        episodes.append(current_ep)
                    current_ep = {"ep": raw.strip(), "qualities": {}}
                elif ext_a and current_ep is not None:
                    q_match = re.match(r"(4K|2160p|1080p|720p|480p|360p)", raw, re.I)
                    quality = q_match.group(1) if q_match else "Link"
                    ql: list[dict] = []
                    for a in ext_a:
                        lbl = a.get_text(strip=True)
                        href = a["href"]
                        if lbl.lower() in ("watch", "watch online", "player-2", "player 2"):
                            ql.append({"label": "📺 Watch Now", "url": href})
                        elif lbl.lower() not in _HDHUB_SKIP_LABELS:
                            ql.append({"label": lbl, "url": href})
                    if ql:
                        existing = current_ep["qualities"].get(quality, [])
                        current_ep["qualities"][quality] = existing + ql
            if current_ep and current_ep["qualities"]:
                episodes.append(current_ep)

    # ── Resolve gadgetsweb ad-gate links to real HubCloud/HubDrive/GoFile URLs ─
    # For flat-episode series, expand gadgetsweb 📥 Download links per episode
    if flat_has_episodes and episodes:
        for ep_entry in episodes:
            for quality, ql in ep_entry["qualities"].items():
                ep_entry["qualities"][quality] = _expand_gw_links(ql, ep_entry["ep"])
    elif links:
        # For movies / Lukkhe-style packs: expand any gadgetsweb pack links
        links = _expand_gw_links(links)

    return {
        "poster":    poster,
        "info":      info,
        "links":     links,
        "episodes":  episodes,
        "is_series": is_series,
    }


def format_hdhub_message(movie_title: str, data: dict, footer: bool = True) -> str:
    """Format an HDHub4u result as HTML for Telegram."""
    links     = data.get("links", [])
    episodes  = data.get("episodes", [])
    info      = data.get("info", {})
    is_series = data.get("is_series", False)

    def _info_block() -> str:
        parts: list[str] = []
        if info.get("imdb"):     parts.append(f"\u2b50 <b>IMDb:</b> {str(info['imdb']).split('/')[0].strip()}")
        if info.get("genre"):    parts.append(f"\U0001f3ad <b>Genre:</b> {info['genre'][:70]}")
        if info.get("language"): parts.append(f"\U0001f5e3 <b>Language:</b> {info['language'][:50]}")
        if info.get("quality"):  parts.append(f"\U0001f4fa <b>Quality:</b> {info['quality'][:60]}")
        stars = info.get("stars", "")
        if stars:
            parts.append(f"\U0001f3ac <b>Cast:</b> {', '.join(x.strip() for x in stars.split(',')[:3])}")
        director = info.get("director", "")
        if director:
            parts.append(f"\U0001f3a5 <b>Director:</b> {director[:50]}")
        return "\n".join(parts)

    lines: list[str] = []
    icon = "\U0001f4fa" if is_series else "\U0001f39e"
    if movie_title:
        lines.append(f"{icon} <b>{movie_title}</b>")

    ib = _info_block()
    if ib:
        lines.append("\u2501" * 32)
        lines.append(ib)

    if links:
        lines.append("\u2501" * 32)
        hdr = "\U0001f4e5 <b>Full Pack / ZIP Download</b>" if is_series else "\U0001f4e5 <b>Download Links (HDHub4u)</b>"
        lines.append(hdr)
        for lk in links:
            lk_url   = lk["url"]
            lk_label = lk["label"]
            lines.append(f"\n\U0001f517 <a href='{lk_url}'>{lk_label}</a>")
        lines.append("")

    if episodes:
        lines.append("\u2501" * 32)
        lines.append("\U0001f5c2 <b>Episode-wise Download</b>")
        for ep in episodes:
            ep_label = ep["ep"]
            qualities = ep["qualities"]
            # Flat-episode layout: single "Download" key with mixed links
            if list(qualities.keys()) == ["Download"]:
                ql = qualities["Download"]
                dl_parts = " | ".join(
                    "<a href='" + lk["url"] + "'>" + lk["label"] + "</a>" for lk in ql
                )
                lines.append(f"\n\U0001f4c1 <b>{ep_label}</b>  \u2014  {dl_parts}")
            else:
                # Quality-keyed layout (e.g. Lukkhe 720p/1080p per episode)
                lines.append(f"\n<b>{ep_label}</b>")
                for quality, ql in qualities.items():
                    server_parts = " | ".join(
                        "<a href='" + lk["url"] + "'>" + lk["label"] + "</a>" for lk in ql
                    )
                    lines.append(f"  {quality} \u2014 {server_parts}")
        lines.append("")

    if footer:
        lines.append("\u2501" * 32)
        lines.append("\u26a1 <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  MoviesMod  (https://moviesmod.farm/)
# ══════════════════════════════════════════════════════════════════════════════

MOVIESMOD_BASE = os.getenv("MOVIESMOD_BASE_URL", "https://moviesmod.farm")

def _resolve_modpro_blog(url: str) -> str:
    """
    Resolves episodes.modpro.blog links to their final destination.
    Flow:
    1. GET modpro.blog -> extract sid_url (cloud.unblockedgames.world)
    2. GET sid_url -> extract form1
    3. POST form1 -> extract form2
    4. POST form2 -> extract s_343 cookie and ?go= URL
    5. GET ?go= URL -> extract refresh URL
    6. GET refresh URL -> extract window.location.replace URL
    7. GET replace URL -> extract final download links (video-seed.pro, tgseed.link)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    for attempt in range(3):
        session = requests.Session()
        session.headers.update(headers)
        
        try:
            # 1. Fetch modpro.blog
            r1 = session.get(url, verify=False, timeout=15)
            soup1 = BeautifulSoup(r1.text, 'html.parser')
            sid_url = None
            for a in soup1.find_all('a', href=True):
                if 'cloud.unblockedgames.world/?sid=' in a['href']:
                    sid_url = a['href']
                    break
                    
            if not sid_url:
                log.warning("No sid_url found on %s (attempt %d)", url, attempt + 1)
                import time
                time.sleep(1)
                continue
                
            # 2. Fetch sid_url (GET)
            r2 = session.get(sid_url, verify=False, timeout=15)
            soup2 = BeautifulSoup(r2.text, 'html.parser')
            form1 = soup2.find('form', id='landing')
            if not form1:
                log.warning("No form1 found on %s (attempt %d)", sid_url, attempt + 1)
                import time
                time.sleep(1)
                continue
                
            action1 = form1.get('action')
            data1 = {inp.get('name'): inp.get('value') for inp in form1.find_all('input')}
            
            # 3. POST form1
            r3 = session.post(action1, data=data1, verify=False, timeout=15)
            soup3 = BeautifulSoup(r3.text, 'html.parser')
            form2 = soup3.find('form', id='landing')
            if not form2:
                log.warning("No form2 found on %s (attempt %d)", action1, attempt + 1)
                import time
                time.sleep(1)
                continue
                
            action2 = form2.get('action')
            data2 = {inp.get('name'): inp.get('value') for inp in form2.find_all('input')}
            
            # 4. POST form2
            r4 = session.post(action2, data=data2, verify=False, timeout=15)
            
            # Extract cookie
            m_cookie = re.search(r"s_343\('([^']+)',\s*'([^']+)'", r4.text)
            if m_cookie:
                c_name = m_cookie.group(1)
                c_value = m_cookie.group(2)
                session.cookies.set(c_name, c_value, domain='cloud.unblockedgames.world')
                
            # 5. Extract ?go= link
            m = re.search(r'setAttribute\("href","([^"]+)"\)', r4.text)
            if not m:
                log.warning("No go_url found on %s (attempt %d)", action2, attempt + 1)
                import time
                time.sleep(1)
                continue
                
            go_url = m.group(1)
            
            # 6. Fetch go_url
            r5 = session.get(go_url, verify=False, timeout=15)
            
            # 7. Extract refresh URL
            m_refresh = re.search(r'url=([^"]+)', r5.text)
            if not m_refresh:
                log.warning("No refresh_url found on %s (attempt %d)", go_url, attempt + 1)
                import time
                time.sleep(1)
                continue
                
            refresh_url = m_refresh.group(1)
            
            # 8. Fetch refresh_url
            r6 = session.get(refresh_url, verify=False, timeout=15)
            
            # 9. Extract replace URL
            m_replace = re.search(r'window\.location\.replace\("([^"]+)"\)', r6.text)
            if not m_replace:
                log.warning("No replace_url found on %s (attempt %d)", refresh_url, attempt + 1)
                import time
                time.sleep(1)
                continue
                
            replace_url = m_replace.group(1)
            if replace_url.startswith('/'):
                from urllib.parse import urljoin
                replace_url = urljoin(r6.url, replace_url)
                
            # 10. Fetch final page
            r7 = session.get(replace_url, verify=False, timeout=15)
            soup7 = BeautifulSoup(r7.text, 'html.parser')
            
            final_links = []
            for a in soup7.find_all('a', href=True):
                text = a.get_text(strip=True).lower()
                href = a['href']
                if 'instant download' in text or 'telegram file' in text or 'direct' in text:
                    final_links.append(href)
                    
            if final_links:
                return "\n".join(final_links)
                
            return r7.url
            
        except Exception as e:
            log.error("Error resolving modpro blog %s (attempt %d): %s", url, attempt + 1, e)
            import time
            time.sleep(1)
            
    return url


def moviesmod_movie_links(movie_url: str) -> dict:
    """
    Scrapes moviesmod.farm for download links.
    Returns a dict:
      {
        "title": "Movie Title",
        "links": [
           {"label": "Season 1 [200MB] - Episode Links", "url": "final_url_1\nfinal_url_2"},
           ...
        ]
      }
    """
    try:
        resp = _get(movie_url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("moviesmod_movie_links %s failed: %s", movie_url, exc)
        return {"title": "Error", "links": []}

    # Title
    title_tag = soup.find('h1')
    title = title_tag.get_text(strip=True) if title_tag else "MoviesMod Content"
    
    content = soup.select_one("main, .entry-content, article") or soup
    
    links = []
    current_header = ""
    
    for tag in content.find_all(['h2', 'h3', 'a']):
        if tag.name in ['h2', 'h3']:
            text = tag.get_text(strip=True)
            # Ignore some common non-link headers
            if text.lower() not in ['series info:', 'storyline:', 'screenshots:', 'related posts', 'search movies', 'categories']:
                current_header = text
        elif tag.name == 'a':
            href = tag.get('href', '')
            text = tag.get_text(strip=True)
            
            if 'episodes.modpro.blog' in href or 'links.modpro.blog' in href:
                label = f"{current_header} - {text}" if current_header else text
                links.append({"label": label, "url": href})
            elif 'uhdmovies.foo' in href:
                label = f"{current_header} - {text}" if current_header else text
                links.append({"label": label, "url": href})
                
    # Now resolve the links in parallel
    resolved_links = []
    
    def _resolve_link(item):
        url = item['url']
        if 'episodes.modpro.blog' in url or 'links.modpro.blog' in url:
            resolved_url = _resolve_modpro_blog(url)
        else:
            resolved_url = url
        return {"label": item['label'], "url": resolved_url}
        
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_resolve_link, item) for item in links]
        for future in as_completed(futures, timeout=120):
            try:
                res = future.result()
                if res['url']:
                    resolved_links.append(res)
            except Exception as e:
                log.error("Error resolving moviesmod link: %s", e)
                
    # Sort to maintain some order (maybe by label)
    resolved_links.sort(key=lambda x: x['label'])
    
    return {
        "title": title,
        "links": resolved_links
    }


def format_moviesmod_message(movie_title: str, data: dict, footer: bool = True) -> str:
    """Format a MoviesMod result as HTML for Telegram."""
    links = data.get("links", [])
    
    lines: list[str] = []
    disp = movie_title or data.get("title", "")
    if disp:
        lines.append(f"🎬 <b>{disp}</b>")
        
    if not links:
        if lines:
            lines.append("\n❌ No download links parsed.")
        return "\n".join(lines) if lines else ""
        
    zip_links = []
    ep_links = []
    other_links = []
    
    for lk in links:
        lbl = lk.get("label", "").lower()
        if "batch" in lbl or "zip" in lbl:
            zip_links.append(lk)
        elif "episode" in lbl:
            ep_links.append(lk)
        else:
            other_links.append(lk)
            
    def _render_links(link_list):
        out = []
        for lk in link_list:
            label = lk.get("label", "Download")
            url_ = lk.get("url", "")
            
            out.append(f"📦 <b>{label}</b>")
            
            parts = []
            for u in url_.split('\n'):
                if 'tgseed.link' in u:
                    parts.append(f"<a href='{u}'>Telegram File</a>")
                elif 'video-seed.pro' in u or 'cdn.video-gen.xyz' in u:
                    parts.append(f"<a href='{u}'>Direct Download</a>")
                elif 'driveseed.org' in u:
                    parts.append(f"<a href='{u}'>DriveSeed</a>")
                elif 'uhdmovies.foo' in u:
                    parts.append(f"<a href='{u}'>UHDMovies</a>")
                else:
                    parts.append(f"<a href='{u}'>Download</a>")
                    
            out.append("   🔗 " + " · ".join(parts))
        return out

    if zip_links:
        lines.append("\n" + "━" * 32)
        lines.append("🗂 <b>Batch / ZIP Download</b>\n")
        lines.extend(_render_links(zip_links))
        
    if ep_links:
        lines.append("\n" + "━" * 32)
        lines.append("📺 <b>Episode-wise Download</b>\n")
        lines.extend(_render_links(ep_links))
        
    if other_links:
        lines.append("\n" + "━" * 32)
        lines.append("📥 <b>Download Links</b>\n")
        lines.extend(_render_links(other_links))
        
    if footer:
        lines.append("\n" + "━" * 32)
        lines.append("⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")
        
    return "\n".join(lines)


def _moviesmod_parse_listing(soup: BeautifulSoup, limit: int) -> list[dict]:
    movies: list[dict] = []
    for art in soup.select("article"):
        if len(movies) >= limit:
            break
        title_a = art.select_one(".entry-title a, h2 a, h3 a")
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        link  = title_a.get("href", "")
        img   = art.select_one("img")
        poster = img.get("src", "") or img.get("data-src", "") if img else ""
        if link:
            movies.append({"title": title, "url": link, "poster": poster, "source": "moviesmod"})
    return movies

def moviesmod_latest_movies(page: int = 1, limit: int = 10) -> list[dict]:
    """Fetch latest movies from moviesmod.farm with WordPress /page/N/ pagination."""
    url = MOVIESMOD_BASE + "/" if page == 1 else f"{MOVIESMOD_BASE}/page/{page}/"
    try:
        resp = _get(url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("moviesmod_latest_movies page=%d failed: %s", page, exc)
        return []
    return _moviesmod_parse_listing(soup, limit)

def moviesmod_search(query: str, limit: int = 10) -> list[dict]:
    """Search moviesmod.farm using the WordPress ?s= parameter."""
    url = f"{MOVIESMOD_BASE}/?s={urllib.parse.quote(query)}"
    try:
        resp = _get(url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("moviesmod_search '%s' failed: %s", query, exc)
        return []
    return _moviesmod_parse_listing(soup, limit)


# ── AtoZ Cinemas ──────────────────────────────────────────────────────────────

ATOZ_BASE = os.getenv("ATOZ_BASE_URL", "https://atoz.cinemaz.workers.dev")


def _atoz_abs_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    base = ATOZ_BASE.rstrip("/")
    return base + (href if href.startswith("/") else f"/{href}")


def _atoz_parse_listing(soup: BeautifulSoup, limit: int) -> list[dict]:
    movies: list[dict] = []
    for card in soup.select("a.movie-card"):
        if len(movies) >= limit:
            break
        h3 = card.select_one("h3")
        title_el = h3 or card.select_one(".card-title")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            img = card.select_one("img")
            title = (img.get("alt", "") if img else "").strip()
        href = card.get("href", "")
        url = _atoz_abs_url(href)
        img = card.select_one("img")
        poster = ""
        if img:
            poster = img.get("src", "") or img.get("data-src", "") or ""
        if url and title:
            movies.append({"title": title, "url": url, "poster": poster, "source": "atoz"})
    return movies


def atoz_latest_movies(page: int = 1, limit: int = 10) -> list[dict]:
    """Fetch latest movies from AtoZ Cinemas."""
    url = f"{ATOZ_BASE}/?page={page}"
    try:
        resp = _get(url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("atoz_latest_movies page=%d failed: %s", page, exc)
        return []
    return _atoz_parse_listing(soup, limit)


def atoz_search(query: str, limit: int = 10) -> list[dict]:
    """Search AtoZ Cinemas."""
    url = f"{ATOZ_BASE}/search?q={urllib.parse.quote(query)}"
    try:
        resp = _get(url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("atoz_search '%s' failed: %s", query, exc)
        return []
    return _atoz_parse_listing(soup, limit)


def _atoz_button_label(btn: Any) -> str:
    """Extract filename text from a download button's surrounding container."""
    container = btn.find_parent(class_="file-item")
    if not container:
        for anc in btn.parents:
            if anc.name in ("body", "html", "[document]"):
                break
            name_el = anc.select_one(".dl-btn-name")
            if name_el:
                return name_el.get_text(strip=True)
        return btn.get_text(strip=True)
    name_el = container.select_one(".dl-btn-name")
    return name_el.get_text(strip=True) if name_el else btn.get_text(strip=True)


def _format_size(size_bytes: int) -> str:
    """Format bytes to human readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


_ATOZ_SERVER_RE = re.compile(r'\\"([a-zA-Z]+Server)\\":\\"([^"\\]+)\\"')


def _atoz_clean_file_label(file_name: str, *, quality: str = "") -> str:
    name = file_name or quality
    name = re.sub(r"(?i)@AtoZ_Files", "", name)
    name = re.sub(r"(?i)\.(mkv|mp4|avi)$", "", name)
    return name.strip() or quality


def _atoz_extract_brace_object(text: str, start: int) -> str | None:
    """Return the `{...}` slice starting at start, with nested braces."""
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _atoz_parse_embedded_files(html_text: str) -> dict[str, Any] | None:
    """Parse escaped Next.js payload: files, kind, baseUrl, *Server fields.

    Movies: files = {quality: {file_id, nulldrop_id, ...}}
    Series: files = {single|pack: {quality: [{episode, file_id, nulldrop_id, ...}]}}
    """
    marker = '\\"files\\":'
    pos = html_text.find(marker)
    if pos < 0:
        return None
    brace_at = html_text.find("{", pos + len(marker))
    files_esc = _atoz_extract_brace_object(html_text, brace_at)
    if not files_esc:
        return None
    try:
        files_data = json.loads(files_esc.replace('\\"', '"'))
    except json.JSONDecodeError:
        return None

    tail = html_text[brace_at + len(files_esc) : brace_at + len(files_esc) + 500]
    kind_m = re.search(r'\\"kind\\":\\"([^"\\]+)\\"', tail)
    base_m = re.search(r'\\"baseUrl\\":\\"([^"\\]+)\\"', tail)
    servers = {
        key: val.replace("\\/", "/")
        for key, val in _ATOZ_SERVER_RE.findall(tail)
    }
    return {
        "files": files_data,
        "kind": kind_m.group(1) if kind_m else "movie",
        "base_url": (base_m.group(1).replace("\\/", "/") if base_m else ""),
        "nulldrop_server": servers.get("nulldropServer", ""),
    }


def _atoz_iter_file_entries(files_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten movie or series files payload into linkable entries."""
    entries: list[dict[str, Any]] = []

    def _add(quality: str, info: dict[str, Any], *, pack: str = "") -> None:
        if not isinstance(info, dict):
            return
        file_id = (info.get("file_id") or "").strip()
        nulldrop_id = (info.get("nulldrop_id") or "").strip()
        if not file_id and not nulldrop_id:
            return
        episode = str(info.get("episode") or "").strip()
        label_bits = [quality]
        if pack:
            label_bits.insert(0, pack)
        if episode and episode.lower() not in ("complete", "full"):
            label_bits.append(episode)
        quality_label = " ".join(x for x in label_bits if x)
        entries.append({
            "quality": quality_label or quality,
            "file_name": info.get("file_name") or quality_label or quality,
            "file_id": file_id,
            "nulldrop_id": nulldrop_id,
            "file_size": info.get("file_size") or 0,
            "episode": episode,
        })

    if not isinstance(files_data, dict):
        return entries

    # Series layout: {single: {...}, pack: {...}}
    if any(k in files_data for k in ("single", "pack", "season")):
        for section, section_data in files_data.items():
            if not isinstance(section_data, dict):
                continue
            pack_label = "" if section == "single" else section.capitalize()
            for quality, payload in section_data.items():
                if isinstance(payload, list):
                    for item in payload:
                        _add(str(quality), item, pack=pack_label)
                elif isinstance(payload, dict):
                    _add(str(quality), payload, pack=pack_label)
        return entries

    # Movie layout: {quality: {file_id, ...}}
    for quality, payload in files_data.items():
        if isinstance(payload, list):
            for item in payload:
                _add(str(quality), item)
        elif isinstance(payload, dict):
            _add(str(quality), payload)
    return entries


def _atoz_dl_watch_url(file_id: str) -> str:
    """AtoZ site 'DL/Watch Online' page — no extra HTTP roundtrip."""
    if not file_id:
        return ""
    return f"{ATOZ_BASE.rstrip('/')}/links/{file_id}"


def _atoz_link_label(clean_name: str, size_str: str, server: str) -> str:
    base = f"{clean_name} ({size_str})" if size_str else clean_name
    return f"{base} [{server}]"


def atoz_movie_links(movie_url: str, *, fast: bool = True) -> dict[str, Any]:
    """Fetch download links for an AtoZ movie page (Next.js React site)."""
    movie_url = _atoz_abs_url(movie_url)
    result: dict[str, Any] = {
        "poster": "",
        "info": {},
        "links": [],
        "episodes": [],
        "is_series": False,
    }
    page_timeout = 12 if fast else 25
    try:
        resp = _get(movie_url, timeout=page_timeout)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("atoz_movie_links %s failed: %s", movie_url, exc)
        return result

    # Get poster from TMDB image
    for img in soup.find_all("img"):
        src = img.get("src", "") or ""
        if "tmdb" in src or "image.tmdb.org" in src:
            result["poster"] = src
            break
    if not result["poster"]:
        img = soup.select_one("img")
        if img:
            result["poster"] = img.get("src", "") or img.get("data-src", "") or ""

    html_text = resp.text
    embedded = _atoz_parse_embedded_files(html_text)
    if embedded:
        if embedded.get("kind") == "series":
            result["is_series"] = True

        nulldrop_server = (embedded.get("nulldrop_server") or "").rstrip("/")
        base_url = embedded.get("base_url") or ""

        for entry in _atoz_iter_file_entries(embedded.get("files") or {}):
            file_id = entry["file_id"]
            nulldrop_id = entry["nulldrop_id"]
            clean_name = _atoz_clean_file_label(entry["file_name"], quality=entry["quality"])
            size_str = _format_size(entry["file_size"]) if entry["file_size"] else ""

            # Order: NullDrop → DL/Watch → Telegram (NullDrop even without file_id)
            if nulldrop_server and nulldrop_id:
                result["links"].append({
                    "label": _atoz_link_label(clean_name, size_str, "NullDrop"),
                    "url": f"{nulldrop_server}/file/{nulldrop_id}",
                })
            if file_id:
                result["links"].append({
                    "label": _atoz_link_label(clean_name, size_str, "DL/Watch"),
                    "url": _atoz_dl_watch_url(file_id),
                })
                result["links"].append({
                    "label": _atoz_link_label(clean_name, size_str, "Telegram"),
                    "url": f"{base_url}{file_id}",
                })

    # Fallback: legacy buttons → DL/Watch then Telegram
    if not result["links"]:
        buttons = soup.find_all("button", attrs={"data-id": True})
        tg_base = "https://t.me/AtoZ_Files_Bot?start=file_"
        for btn in buttons:
            data_id = btn.get("data-id", "").strip()
            if not data_id:
                continue
            file_name = _atoz_clean_file_label(_atoz_button_label(btn))
            result["links"].append({
                "label": _atoz_link_label(file_name, "", "DL/Watch"),
                "url": _atoz_dl_watch_url(data_id),
            })
            result["links"].append({
                "label": _atoz_link_label(file_name, "", "Telegram"),
                "url": f"{tg_base}{data_id}",
            })

    return result


def format_atoz_message(movie_title: str, data: dict, footer: bool = True) -> str:
    """Format an AtoZ Cinemas result as HTML for Telegram."""
    links = data.get("links", [])

    lines: list[str] = []
    disp = movie_title or data.get("title", "")
    if disp:
        lines.append(f"🅰️ <b>{disp}</b>")

    if not links:
        if lines:
            lines.append("\n❌ No download links parsed.")
        return "\n".join(lines) if lines else ""

    lines.append("\n" + "━" * 32)
    lines.append("📥 <b>Download Links (AtoZ Cinemas)</b>\n")
    for lk in links:
        label = lk.get("label", "Download")
        url_ = lk.get("url", "")
        lines.append(f"📦 <b>{label}</b>")
        lines.append(f"   🔗 <a href='{url_}'>Download</a>")

    if footer:
        lines.append("\n" + "━" * 32)
        lines.append("⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")

    return "\n".join(lines)


# ─── ZeeFliz ─────────────────────────────────────────────────────────────────

ZEEFLIZ_BASE = os.getenv("ZEEFLIZ_BASE_URL", "https://zeefliz.beer")


def zeefliz_search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Search ZeeFliz via WP REST API."""
    api_url = f"{ZEEFLIZ_BASE}/wp-json/wp/v2/posts?search={urllib.parse.quote(query)}&per_page={limit}&_embed"
    try:
        resp = _get(api_url, timeout=15)
        posts = resp.json()
    except Exception as exc:
        log.error("zeefliz_search '%s' failed: %s", query, exc)
        return []

    movies: list[dict[str, str]] = []
    for p in posts:
        title = BeautifulSoup(p.get("title", {}).get("rendered", ""), "html.parser").get_text()
        link = p.get("link", "")
        poster = ""
        embedded = p.get("_embedded", {})
        media = embedded.get("wp:featuredmedia", [])
        if media:
            poster = media[0].get("source_url", "")
        if title and link:
            movies.append({"title": title[:100], "url": link, "poster": poster, "source": "zeefliz"})
    return movies


def zeefliz_latest_movies(page: int = 1, limit: int = 10) -> list[dict[str, str]]:
    """Fetch latest movies from ZeeFliz via WP REST API with pagination."""
    api_url = f"{ZEEFLIZ_BASE}/wp-json/wp/v2/posts?per_page={limit}&page={page}&_embed"
    try:
        resp = _get(api_url, timeout=15)
        if resp.status_code == 400:
            return []
        posts = resp.json()
    except Exception as exc:
        log.error("zeefliz_latest_movies page %d failed: %s", page, exc)
        return []

    movies: list[dict[str, str]] = []
    for p in posts:
        title = BeautifulSoup(p.get("title", {}).get("rendered", ""), "html.parser").get_text()
        link = p.get("link", "")
        poster = ""
        embedded = p.get("_embedded", {})
        media = embedded.get("wp:featuredmedia", [])
        if media:
            poster = media[0].get("source_url", "")
        if title and link:
            movies.append({"title": title[:100], "url": link, "poster": poster, "source": "zeefliz"})
    return movies


def zeefliz_movie_links(movie_url: str) -> dict[str, Any]:
    """Extract download links from a ZeeFliz movie/series page.

    Returns quality options with nexdrive links, and resolves each nexdrive
    page to get the actual download sources (G-Direct, Filepress, gofile, etc.).
    """
    result: dict[str, Any] = {
        "poster": "", "info": {}, "links": [], "episodes": [], "is_series": False,
    }
    try:
        resp = _get(movie_url, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        log.error("zeefliz_movie_links failed for %s: %s", movie_url, exc)
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # Poster via og:image
    og_img = soup.find("meta", property="og:image")
    if og_img:
        result["poster"] = og_img.get("content", "")

    # Fallback: get poster from WP API using post slug
    if not result["poster"]:
        slug = movie_url.rstrip("/").split("/")[-1]
        try:
            api_resp = _get(f"{ZEEFLIZ_BASE}/wp-json/wp/v2/posts?slug={slug}&_embed", timeout=10)
            posts = api_resp.json()
            if posts:
                media = posts[0].get("_embedded", {}).get("wp:featuredmedia", [])
                if media:
                    result["poster"] = media[0].get("source_url", "")
        except Exception:
            pass

    # Collect nexdrive links with quality context from preceding h3
    quality_map: list[tuple[str, str]] = []
    current_quality = "Download"
    for el in soup.find_all(["h3", "a"]):
        if el.name == "h3":
            text = el.get_text(strip=True)
            if any(x in text for x in ["480p", "720p", "1080p", "2160p", "4K", "Season", "Episode"]):
                current_quality = text
        elif el.name == "a":
            href = el.get("href", "")
            if "nexdrive" in href:
                quality_map.append((current_quality, href))

    if not quality_map:
        return result

    # Detect series
    is_series = any(x in movie_url.lower() for x in ["season", "episode", "s0", "s1"])
    result["is_series"] = is_series

    # Resolve nexdrive pages to get actual download sources
    # Limit resolution: max 6 unique nexdrive pages to avoid timeout
    seen_nex: set = set()
    max_resolve = 6
    for quality_label, nex_url in quality_map:
        if nex_url in seen_nex:
            continue
        seen_nex.add(nex_url)

        if len(seen_nex) > max_resolve:
            result["links"].append({"label": quality_label, "url": nex_url})
            continue

        try:
            nex_resp = _get(nex_url, timeout=15)
            nex_soup = BeautifulSoup(nex_resp.text, "html.parser")
        except Exception:
            result["links"].append({"label": quality_label, "url": nex_url})
            continue

        # Get quality/file info from page title
        nex_title = nex_soup.title.get_text(strip=True) if nex_soup.title else ""
        file_label = nex_title.split("–")[0].strip() if "–" in nex_title else quality_label

        # Collect download sources from nexdrive page
        sources: list[dict[str, str]] = []
        for a in nex_soup.find_all("a", href=True):
            href = a.get("href", "")
            if not href or href == "#":
                continue
            if "zee-dl" in href:
                sources.append({"label": f"{file_label} [G-Direct]", "url": href})
            elif "filebee" in href or "filepress" in href:
                sources.append({"label": f"{file_label} [Filepress]", "url": href})
            elif any(x in href for x in ["gofile.io", "vikingfile", "megaup.net", "pixeldrain", "hubcloud"]):
                sources.append({"label": f"{file_label} [Mirror]", "url": href})

        if sources:
            result["links"].extend(sources)
        else:
            result["links"].append({"label": file_label or quality_label, "url": nex_url})

    return result


def format_zeefliz_message(movie_title: str, data: dict, footer: bool = True) -> str:
    """Format a ZeeFliz result as HTML for Telegram."""
    links = data.get("links", [])
    lines: list[str] = []
    disp = movie_title or data.get("title", "")
    if disp:
        lines.append(f"🎬 <b>{disp}</b>")

    if not links:
        if lines:
            lines.append("\n❌ No download links found.")
        return "\n".join(lines) if lines else ""

    lines.append("\n" + "━" * 32)
    lines.append("📥 <b>Download Links (ZeeFliz)</b>\n")

    for lk in links:
        label = lk.get("label", "Download")
        url_ = lk.get("url", "")
        lines.append(f"📦 <b>{label}</b>")
        lines.append(f"   🔗 <a href='{url_}'>Download</a>")

    if footer:
        lines.append("\n" + "━" * 32)
        lines.append("⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")

    return "\n".join(lines)


# ─── REST API helpers (download links with size / audio metadata) ─────────────

MOVIES_API_SOURCE_TIMEOUT = max(4, int(os.getenv("MOVIES_API_SOURCE_TIMEOUT", "14")))
MOVIES_API_PER_SOURCE_TIMEOUT = max(3, int(os.getenv("MOVIES_API_PER_SOURCE_TIMEOUT", "12")))
MOVIES_API_LINK_TIMEOUT = max(5, int(os.getenv("MOVIES_API_LINK_TIMEOUT", "12")))
MOVIES_API_CACHE_TTL = max(0, int(os.getenv("MOVIES_API_CACHE_TTL", "300")))
MOVIES_API_MAX_WORKERS = max(2, min(16, int(os.getenv("MOVIES_API_MAX_WORKERS", "12"))))
MOVIES_API_SKIP_UNHEALTHY_SECONDS = max(
    60, int(os.getenv("MOVIES_API_SKIP_UNHEALTHY_SECONDS", "1800")),
)
_api_cache: dict[str, tuple[float, Any]] = {}
_site_api_skip_until: dict[str, float] = {}


def _api_cache_get(key: str) -> Any | None:
    if MOVIES_API_CACHE_TTL <= 0:
        return None
    item = _api_cache.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > MOVIES_API_CACHE_TTL:
        _api_cache.pop(key, None)
        return None
    return value


def _api_cache_set(key: str, value: Any) -> None:
    if MOVIES_API_CACHE_TTL <= 0:
        return
    # Never cache empty search/latest payloads — a timeout miss would stick for TTL.
    if value == [] or value == {}:
        return
    _api_cache[key] = (time.time(), value)
    # ponytail: O(n) prune when cache grows; swap for Redis/TTL index if multi-instance
    if len(_api_cache) > 500:
        cutoff = time.time() - MOVIES_API_CACHE_TTL
        for k, (ts, _) in list(_api_cache.items()):
            if ts < cutoff:
                _api_cache.pop(k, None)


def mark_site_api_skip(source_key: str, *, seconds: int | None = None) -> None:
    """Temporarily skip a source in combined API calls (stale/broken domain)."""
    ttl = seconds if seconds is not None else MOVIES_API_SKIP_UNHEALTHY_SECONDS
    _site_api_skip_until[source_key] = time.time() + max(60, ttl)
    log.info("API will skip source %s for %ss", source_key, ttl)


def clear_site_api_skip(source_key: str) -> None:
    _site_api_skip_until.pop(source_key, None)


def _api_filter_sources(
    sources: tuple[tuple[str, Any, str], ...],
) -> tuple[tuple[str, Any, str], ...]:
    """Drop sources flagged unhealthy so they don't block working ones."""
    now = time.time()
    active: list[tuple[str, Any, str]] = []
    for row in sources:
        _, _, key = row
        until = _site_api_skip_until.get(key, 0.0)
        if until > now:
            log.debug("API skipping unhealthy source %s (%.0fs left)", key, until - now)
            continue
        active.append(row)
    return tuple(active)


def _call_timed(timeout_sec: float, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run fn with a hard wall-clock cap so one slow site cannot block the pool."""
    box: list[Any] = []
    err: list[BaseException] = []

    def _run() -> None:
        try:
            box.append(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 — surface any worker failure
            err.append(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        log.warning("API source hard-timeout after %.1fs", timeout_sec)
        return []
    if err:
        log.warning("API source failed: %s", err[0])
        return []
    return box[0] if box else []


def _parallel_collect_rows(
    futures_map: dict[Future, str],
    *,
    total_timeout: float,
    label: str,
) -> list[dict[str, str]]:
    """Collect list rows from parallel tasks; return partial results on timeout."""
    out: list[dict[str, str]] = []
    pending = set(futures_map.keys())
    deadline = time.time() + total_timeout
    while pending and time.time() < deadline:
        done, pending = wait(
            pending,
            timeout=max(0.05, deadline - time.time()),
            return_when=FIRST_COMPLETED,
        )
        for fut in done:
            key = futures_map.get(fut, "?")
            try:
                rows = fut.result()
                if rows:
                    out.extend(rows)
            except Exception as exc:
                log.warning("%s %s failed: %s", label, key, exc)
    if pending:
        skipped = [futures_map[f] for f in pending]
        log.warning(
            "%s skipped %d slow source(s) after %.1fs: %s",
            label, len(pending), total_timeout, ", ".join(skipped),
        )
        for fut in pending:
            fut.cancel()
    return out


def _parallel_collect_dicts(
    futures_map: dict[Future, str],
    *,
    total_timeout: float,
    label: str,
) -> list[dict[str, Any]]:
    """Collect dict results from parallel tasks; harvest completed on timeout."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    pending = set(futures_map.keys())
    deadline = time.time() + total_timeout

    def _append(row: dict[str, Any]) -> None:
        page_url = row.get("page_url") or ""
        if page_url and page_url in seen:
            return
        if page_url:
            seen.add(page_url)
        out.append(row)

    while pending and time.time() < deadline:
        done, pending = wait(
            pending,
            timeout=max(0.05, deadline - time.time()),
            return_when=FIRST_COMPLETED,
        )
        for fut in done:
            key = futures_map.get(fut, "?")
            try:
                _append(fut.result())
            except Exception as exc:
                log.warning("%s %s failed: %s", label, key, exc)

    if pending:
        log.warning(
            "%s link fetch timeout — keeping %d result(s), skipped %d",
            label, len(out), len(pending),
        )
        for fut in pending:
            if fut.done() and not fut.cancelled():
                try:
                    _append(fut.result())
                except Exception:
                    pass
            else:
                fut.cancel()
    return out


MOVIE_API_SOURCE_ALIASES: dict[str, str] = {
    "hdhub4u": "hdhub",
    "hdhub": "hdhub",
    "4khdhub": "hdh",
    "hdh": "hdh",
    "moviesdrive": "md",
    "md": "md",
    "hdmovie2": "hdmovie2",
    "newhdmovie2": "hdmovie2",
    "vegamovies": "vega",
    "vega": "vega",
    "sdmoviespoint": "sdmp",
    "sdmp": "sdmp",
    "bollyflix": "bolly",
    "bolly": "bolly",
    "moviesmod": "moviesmod",
    "atoz": "atoz",
    "atozcinemas": "atoz",
}

MOVIE_API_SOURCE_LABELS: dict[str, str] = {
    "hdhub": "hdhub4u",
    "hdh": "4khdhub",
    "md": "moviesdrive",
    "hdmovie2": "hdmovie2",
    "vega": "vegamovies",
    "sdmp": "sdmoviespoint",
    "bolly": "bollyflix",
    "moviesmod": "moviesmod",
    "atoz": "atoz",
}

# Combined REST API sources (all movie sites except ZeeFliz)
MOVIE_API_SEARCH_SOURCES: tuple[tuple[str, Any, str], ...] = (
    ("hdhub4u", hdhub_search, "hdhub"),
    ("4khdhub", hdh_search, "hdh"),
    ("moviesdrive", md_search, "md"),
    ("hdmovie2", hdmovie2_search, "hdmovie2"),
    ("vegamovies", vega_search, "vega"),
    ("sdmoviespoint", sdmp_search, "sdmp"),
    ("bollyflix", bollyflix_search, "bolly"),
    ("moviesmod", moviesmod_search, "moviesmod"),
    ("atoz", atoz_search, "atoz"),
)

MOVIE_API_LATEST_SOURCES: tuple[tuple[str, Any, str], ...] = (
    ("hdhub4u", hdhub_latest_movies, "hdhub"),
    ("4khdhub", hdh_latest_movies, "hdh"),
    ("moviesdrive", md_latest_movies, "md"),
    ("hdmovie2", hdmovie2_latest_movies, "hdmovie2"),
    ("vegamovies", vega_latest_movies, "vega"),
    ("sdmoviespoint", sdmp_latest_movies, "sdmp"),
    ("bollyflix", bollyflix_latest_movies, "bolly"),
    ("moviesmod", moviesmod_latest_movies, "moviesmod"),
    ("atoz", atoz_latest_movies, "atoz"),
)

# Latest fetchers that accept (page, limit)
_MOVIE_API_LATEST_WITH_LIMIT = frozenset({
    "hdhub", "vega", "sdmp", "bolly", "moviesmod", "atoz",
})


def _normalize_movie_source(source: str) -> str:
    key = (source or "").strip().lower()
    normalized = MOVIE_API_SOURCE_ALIASES.get(key)
    if not normalized:
        keys = ", ".join(sorted(MOVIE_API_SOURCE_LABELS))
        raise ValueError(f"unknown source '{source}' (use one of: {keys})")
    return normalized


def _api_clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _api_extract_size(text: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)?\s*(?:GB|MB|gb|mb))", text or "", re.I)
    return m.group(1).upper().replace(" ", "") if m else ""


def _api_extract_quality(text: str) -> str:
    m = re.search(r"(4K|2160p|1080p|720p|480p|360p)", text or "", re.I)
    return m.group(1).upper() if m else ""


def _api_extract_audio(text: str) -> str:
    if not text:
        return ""
    bracket = re.search(r"\[([^\]]+)\]", text)
    if bracket:
        inner = bracket.group(1)
        if re.search(r"hindi|english|tamil|telugu|malayalam|kannada|dual|multi|dd\d", inner, re.I):
            return _api_clean(inner)
    m = re.search(
        r"((?:Hindi|English|Tamil|Telugu|Malayalam|Kannada|Dual Audio|Multi Audio)"
        r"[^|\n\[]*)",
        text,
        re.I,
    )
    return _api_clean(m.group(1)) if m else ""


def _api_link_entry(
    url: str,
    *,
    label: str = "",
    quality: str = "",
    size: str = "",
    audio: str = "",
    episode: str = "",
) -> dict[str, str]:
    entry: dict[str, str] = {"url": url}
    if label:
        entry["label"] = label
    if quality:
        entry["quality"] = quality
    if size:
        entry["size"] = size
    if audio:
        entry["audio"] = audio
    if episode:
        entry["episode"] = episode
    return entry


def _dedupe_api_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in links:
        url = (item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(item)
    return out


def _flat_links_from_hdhub(data: dict[str, Any]) -> list[dict[str, str]]:
    info = data.get("info") or {}
    page_audio = _api_clean(info.get("language", ""))
    page_quality = _api_clean(info.get("quality", ""))
    out: list[dict[str, str]] = []

    for link in data.get("links", []):
        href = link.get("url", "")
        if not href.startswith("http"):
            continue
        label = _api_clean(link.get("label", ""))
        out.append(_api_link_entry(
            href,
            label=label,
            quality=_api_extract_quality(label) or _api_extract_quality(page_quality) or page_quality,
            size=_api_extract_size(label) or _api_extract_size(page_quality),
            audio=_api_extract_audio(label) or _api_extract_audio(page_quality) or page_audio,
        ))

    for ep in data.get("episodes", []):
        ep_name = _api_clean(ep.get("ep", ""))
        for quality, ql in ep.get("qualities", {}).items():
            q_label = _api_clean(quality)
            for link in ql:
                href = link.get("url", "")
                if not href.startswith("http"):
                    continue
                label = _api_clean(link.get("label", ""))
                merged = " ".join(x for x in (q_label, label, ep_name) if x)
                out.append(_api_link_entry(
                    href,
                    label=label or q_label,
                    quality=_api_extract_quality(merged) or q_label,
                    size=_api_extract_size(merged),
                    audio=_api_extract_audio(merged) or page_audio,
                    episode=ep_name,
                ))
    return _dedupe_api_links(out)


def _flat_links_from_hdh(data: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for block in data.get("qualities", []):
        quality = _api_clean(block.get("quality", ""))
        size = _api_clean(block.get("size", "")) or _api_extract_size(quality)
        audio = _api_clean(block.get("audio", "")) or _api_extract_audio(quality)
        fmt = _api_clean(block.get("format", ""))
        for link in block.get("links", []):
            href = link.get("url", "")
            if not href.startswith("http"):
                continue
            name = _api_clean(link.get("name", ""))
            merged = " ".join(x for x in (quality, name, fmt) if x)
            out.append(_api_link_entry(
                href,
                label=name,
                quality=_api_extract_quality(merged) or quality,
                size=size or _api_extract_size(merged),
                audio=audio or _api_extract_audio(merged),
            ))
    return _dedupe_api_links(out)


def _flat_links_from_md(data: dict[str, Any]) -> list[dict[str, str]]:
    info = data.get("info") or {}
    page_audio = _api_clean(info.get("language", ""))
    out: list[dict[str, str]] = []
    for link in data.get("links", []):
        href = link.get("url", "")
        if not href.startswith("http"):
            continue
        label = _api_clean(link.get("label", ""))
        name = _api_clean(link.get("name", ""))
        merged = " ".join(x for x in (label, name) if x)
        quality = (
            _api_clean(link.get("quality", ""))
            or _api_extract_quality(merged)
        )
        size = _api_extract_size(merged)
        display = name or label
        # Zip packs often omit quality in link text — surface it in the label
        if quality and quality.lower() not in display.lower():
            if re.search(r"\bzip\b", display, re.I) or size:
                display = f"{quality} {display}".strip()
        out.append(_api_link_entry(
            href,
            label=display,
            quality=quality,
            size=size,
            audio=_api_extract_audio(merged) or page_audio,
        ))
    return _dedupe_api_links(out)


def _flat_links_from_hdmovie2(data: dict[str, Any]) -> list[dict[str, str]]:
    info = data.get("info") or {}
    page_audio = _api_clean(info.get("language", ""))
    out: list[dict[str, str]] = []
    for link in data.get("links", []):
        href = link.get("url", "")
        if not href.startswith("http"):
            continue
        label = _api_clean(link.get("label", ""))
        out.append(_api_link_entry(
            href,
            label=label,
            quality=_api_extract_quality(label),
            size=_api_extract_size(label),
            audio=_api_extract_audio(label) or page_audio,
        ))
    return _dedupe_api_links(out)


def _flat_links_from_vega(data: dict[str, Any]) -> list[dict[str, str]]:
    info = data.get("info") or {}
    page_audio = _api_clean(info.get("language", ""))
    out: list[dict[str, str]] = []
    for link in data.get("links", []):
        href = link.get("url", "")
        if not href.startswith("http"):
            continue
        quality = _api_clean(link.get("quality", ""))
        size = _api_clean(link.get("size", "")) or _api_extract_size(quality)
        merged = " ".join(x for x in (quality, size) if x)
        out.append(_api_link_entry(
            href,
            label=quality or "Download",
            quality=_api_extract_quality(merged) or quality,
            size=size or _api_extract_size(merged),
            audio=_api_extract_audio(merged) or page_audio,
        ))
    return _dedupe_api_links(out)


def _flat_links_from_sdmp(data: dict[str, Any]) -> list[dict[str, str]]:
    info = data.get("info") or {}
    page_audio = _api_clean(info.get("language", ""))
    out: list[dict[str, str]] = []
    for link in data.get("links", []):
        href = link.get("url", "")
        if not href.startswith("http"):
            continue
        label = _api_clean(link.get("label", ""))
        size = _api_clean(link.get("size", "")) or _api_extract_size(label)
        out.append(_api_link_entry(
            href,
            label=label,
            quality=_api_extract_quality(label),
            size=size,
            audio=_api_extract_audio(label) or page_audio,
        ))
    return _dedupe_api_links(out)


def _flat_links_from_bolly(data: dict[str, Any]) -> list[dict[str, str]]:
    info = data.get("info") or {}
    page_audio = _api_clean(info.get("language", ""))
    out: list[dict[str, str]] = []
    for link in data.get("links", []):
        href = link.get("url", "")
        if not href.startswith("http"):
            continue
        label = _api_clean(link.get("label", ""))
        name = _api_clean(link.get("name", ""))
        merged = " ".join(x for x in (label, name) if x)
        out.append(_api_link_entry(
            href,
            label=name or label,
            quality=_api_extract_quality(merged),
            size=_api_extract_size(merged),
            audio=_api_extract_audio(merged) or page_audio,
            episode=label if data.get("is_series") and "episode" in label.lower() else "",
        ))
    return _dedupe_api_links(out)


def _flat_links_from_moviesmod(data: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for link in data.get("links", []):
        raw_url = (link.get("url") or "").strip()
        if not raw_url:
            continue
        label = _api_clean(link.get("label", ""))
        for href in re.split(r"[\r\n]+", raw_url):
            href = href.strip()
            if not href.startswith("http"):
                continue
            out.append(_api_link_entry(
                href,
                label=label,
                quality=_api_extract_quality(label),
                size=_api_extract_size(label),
                audio=_api_extract_audio(label),
            ))
    return _dedupe_api_links(out)


def _flat_links_from_atoz(data: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for link in data.get("links", []):
        href = link.get("url", "")
        if not href.startswith("http"):
            continue
        label = _api_clean(link.get("label", ""))
        out.append(_api_link_entry(
            href,
            label=label,
            quality=_api_extract_quality(label),
            size=_api_extract_size(label),
        ))
    return _dedupe_api_links(out)


def movie_page_download_links(
    source: str,
    page_url: str,
    *,
    fast: bool = True,
) -> dict[str, Any]:
    """Scrape one movie page and return download links with size/audio metadata."""
    cache_key = f"links:{source}:{page_url}:{'fast' if fast else 'full'}"
    cached = _api_cache_get(cache_key)
    if cached is not None:
        return cached

    key = _normalize_movie_source(source)
    if key == "hdhub":
        data = hdhub_movie_links(page_url, fast=fast)
        links = _flat_links_from_hdhub(data)
    elif key == "hdh":
        data = hdh_movie_links(page_url, fast=fast)
        links = _flat_links_from_hdh(data)
    elif key == "md":
        data = md_movie_links(page_url, fast=fast)
        links = _flat_links_from_md(data)
    elif key == "hdmovie2":
        data = hdmovie2_movie_links(page_url)
        links = _flat_links_from_hdmovie2(data)
    elif key == "vega":
        data = vega_movie_links(page_url)
        links = _flat_links_from_vega(data)
    elif key == "sdmp":
        data = sdmp_movie_links(page_url)
        links = _flat_links_from_sdmp(data)
    elif key == "bolly":
        data = bollyflix_movie_links(page_url)
        links = _flat_links_from_bolly(data)
    elif key == "moviesmod":
        data = moviesmod_movie_links(page_url)
        links = _flat_links_from_moviesmod(data)
    elif key == "atoz":
        data = atoz_movie_links(page_url, fast=fast)
        links = _flat_links_from_atoz(data)
    else:
        raise ValueError(f"unsupported source '{source}'")
    result = {
        "source": MOVIE_API_SOURCE_LABELS[key],
        "page_url": page_url,
        "links": links,
    }
    _api_cache_set(cache_key, result)
    return result


def _movies_search_one_source(
    source_label: str,
    search_fn: Any,
    source_key: str,
    query: str,
    limit: int,
    *,
    fast: bool,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        if search_fn is hdhub_search:
            movies = search_fn(query, limit, fast=fast)
        elif search_fn is md_search:
            # Match Telegram reliability: MD JSON search is flaky under 6s/0-retry.
            movies = search_fn(
                query,
                limit,
                timeout=12 if fast else 15,
                retries=1 if fast else 2,
            )
        elif search_fn is hdh_search:
            movies = search_fn(
                query,
                limit,
                timeout=12 if fast else 20,
                retries=1 if fast else 2,
            )
        elif fast:
            movies = search_fn(query, min(limit, 5))
        else:
            movies = search_fn(query, limit)
        for movie in movies:
            page_url = movie.get("url", "")
            if not page_url:
                continue
            title = movie.get("title", "Unknown")
            if search_fn not in (hdhub_search, md_search) and not _title_matches_query(title, query):
                continue
            rows.append({
                "source": source_label,
                "source_key": source_key,
                "title": movie.get("title", "Unknown"),
                "page_url": page_url,
            })
    except Exception as exc:
        log.error("movies_search_combined %s failed: %s", source_label, exc)
    return rows


def movies_search_combined(
    query: str,
    limit_per_source: int = 5,
    *,
    fast: bool = True,
) -> list[dict[str, str]]:
    """Search all movie API sources in parallel (except ZeeFliz)."""
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit_per_source), 20))
    cache_key = f"search:v4:{q}:{limit}:{'fast' if fast else 'full'}"
    cached = _api_cache_get(cache_key)
    if cached is not None:
        return cached

    sources = _api_filter_sources(MOVIE_API_SEARCH_SOURCES)
    if not sources:
        sources = MOVIE_API_SEARCH_SOURCES  # all skipped — try anyway
    out: list[dict[str, str]] = []
    per_src = MOVIES_API_PER_SOURCE_TIMEOUT if fast else 30
    workers = min(len(sources), MOVIES_API_MAX_WORKERS)
    futures_map: dict[Future, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for source_label, search_fn, source_key in sources:
            # MoviesDrive/4KHDHub need a bit more than the default fast budget.
            src_budget = per_src
            if fast and source_key in ("md", "hdh"):
                src_budget = max(per_src, 14)
            fut = pool.submit(
                _call_timed,
                src_budget,
                _movies_search_one_source,
                source_label,
                search_fn,
                source_key,
                q,
                limit,
                fast=fast,
            )
            futures_map[fut] = source_key
        timeout = MOVIES_API_SOURCE_TIMEOUT if fast else 45
        if fast:
            timeout = max(timeout, 14)
        out = _parallel_collect_rows(
            futures_map, total_timeout=timeout, label="movies_search_combined",
        )

    _api_cache_set(cache_key, out)
    return out


def movies_search_source(
    query: str,
    source: str,
    limit: int = 5,
    *,
    fast: bool = True,
) -> list[dict[str, str]]:
    """Search a single movie API source by key (e.g. hdh, hdhub, md)."""
    key = _normalize_movie_source(source)
    q = (query or "").strip()
    if not q:
        return []
    limit_n = max(1, min(int(limit), 20))
    for source_label, search_fn, source_key in MOVIE_API_SEARCH_SOURCES:
        if source_key != key:
            continue
        return _movies_search_one_source(
            source_label, search_fn, source_key, q, limit_n, fast=fast,
        )
    return []


def movies_search_with_links(
    query: str,
    limit_per_source: int = 5,
    *,
    source: str | None = None,
    fast: bool = True,
) -> list[dict[str, Any]]:
    """Search and attach download links to each hit."""
    if source:
        listings = movies_search_source(query, source, limit_per_source, fast=fast)
    else:
        listings = movies_search_combined(query, limit_per_source, fast=fast)

    if not listings:
        return []

    per_link = min(MOVIES_API_PER_SOURCE_TIMEOUT, MOVIES_API_LINK_TIMEOUT) if fast else MOVIES_API_LINK_TIMEOUT
    futures_map: dict[Future, str] = {}
    workers = min(len(listings), MOVIES_API_MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in listings:
            fut = pool.submit(
                _call_timed,
                per_link,
                _aggregate_one_item,
                item,
                fast=fast,
            )
            futures_map[fut] = item.get("source_key", item.get("source", "?"))

        raw = _parallel_collect_dicts(
            futures_map,
            total_timeout=MOVIES_API_LINK_TIMEOUT,
            label="movies_search_with_links",
        )

    results: list[dict[str, Any]] = []
    for row in raw:
        item = next(
            (x for x in listings if x.get("page_url") == row.get("page_url")),
            {},
        )
        results.append({
            "source": item.get("source", row.get("source", "")),
            "source_key": item.get("source_key", ""),
            "title": row.get("title", ""),
            "page_url": row.get("page_url", ""),
            "links": row.get("links", []),
            **({"error": row["error"]} if row.get("error") else {}),
        })
    return results


def _movies_latest_one_source(
    source_label: str,
    fetch_fn: Any,
    source_key: str,
    page: int,
    limit: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        if source_key in _MOVIE_API_LATEST_WITH_LIMIT:
            movies = fetch_fn(page, limit)
        else:
            movies = fetch_fn(page)
        for movie in movies[:limit]:
            page_url = movie.get("url", "")
            if not page_url:
                continue
            rows.append({
                "source": source_label,
                "source_key": source_key,
                "title": movie.get("title", "Unknown"),
                "page_url": page_url,
            })
    except Exception as exc:
        log.error("movies_latest_combined %s failed: %s", source_label, exc)
    return rows


def movies_latest_combined(
    page: int = 1,
    limit_per_source: int = 10,
    *,
    fast: bool = True,
) -> list[dict[str, str]]:
    """Latest listings from all movie API sources in parallel (except ZeeFlix)."""
    page_n = max(1, int(page))
    limit = max(1, min(int(limit_per_source), 20))
    cache_key = f"latest:v3:{page_n}:{limit}"
    cached = _api_cache_get(cache_key)
    if cached is not None:
        return cached

    sources = _api_filter_sources(MOVIE_API_LATEST_SOURCES)
    if not sources:
        sources = MOVIE_API_LATEST_SOURCES
    out: list[dict[str, str]] = []
    per_src = MOVIES_API_PER_SOURCE_TIMEOUT if fast else 30
    workers = min(len(sources), MOVIES_API_MAX_WORKERS)
    futures_map: dict[Future, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for label, fn, key in sources:
            fut = pool.submit(
                _call_timed,
                per_src,
                _movies_latest_one_source,
                label,
                fn,
                key,
                page_n,
                limit,
            )
            futures_map[fut] = key
        timeout = MOVIES_API_SOURCE_TIMEOUT if fast else 45
        out = _parallel_collect_rows(
            futures_map, total_timeout=timeout, label="movies_latest_combined",
        )

    _api_cache_set(cache_key, out)
    return out


def _aggregate_one_item(item: dict[str, str], *, fast: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "source": item["source"],
        "title": item["title"],
        "page_url": item["page_url"],
        "links": [],
    }
    try:
        detail = movie_page_download_links(item["source_key"], item["page_url"], fast=fast)
        entry["links"] = detail["links"]
    except Exception as exc:
        log.error("movies_aggregate_links %s failed: %s", item["page_url"], exc)
        entry["error"] = str(exc)
    return entry


def movies_aggregate_links(
    query: str,
    limit_per_source: int = 3,
    *,
    fast: bool = True,
) -> dict[str, Any]:
    """Search all three sites and fetch download links for each hit in parallel."""
    q = (query or "").strip()
    limit = max(1, min(int(limit_per_source), 10))
    cache_key = f"agg:v2:{q}:{limit}:{'fast' if fast else 'full'}"
    cached = _api_cache_get(cache_key)
    if cached is not None:
        return cached

    listings = movies_search_combined(q, limit, fast=fast)
    results: list[dict[str, Any]] = []
    if listings:
        per_link = min(MOVIES_API_PER_SOURCE_TIMEOUT, MOVIES_API_LINK_TIMEOUT) if fast else MOVIES_API_LINK_TIMEOUT
        workers = min(MOVIES_API_MAX_WORKERS, len(listings))
        futures_map: dict[Future, str] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for item in listings:
                fut = pool.submit(
                    _call_timed,
                    per_link,
                    _aggregate_one_item,
                    item,
                    fast=fast,
                )
                futures_map[fut] = item.get("source_key", "?")
            results = _parallel_collect_dicts(
                futures_map,
                total_timeout=MOVIES_API_LINK_TIMEOUT,
                label="movies_aggregate_links",
            )

    payload = {"query": q, "count": len(results), "results": results}
    _api_cache_set(cache_key, payload)
    return payload


# ─── Movie site URL registry (runtime update via bot) ─────────────────────────

MOVIE_SITE_REGISTRY: dict[str, dict[str, str]] = {
    "hdhub": {
        "label": "HDHub4u",
        "env": "HDHUB_BASE_URL",
        "default": "https://new2.hdhub4u.cl",
        "health_path": "/",
    },
    "hdh": {
        "label": "4KHDHub",
        "env": "HDH_BASE_URL",
        "default": "https://4khdhub.one",
        "health_path": "/category/hindi-movies/",
    },
    "md": {
        "label": "MoviesDrive",
        "env": "MD_BASE_URL",
        "default": "https://new2.moviesdrives.my",
        "health_path": "/",
    },
    "hdmovie2": {
        "label": "HDMovie2",
        "env": "HDMOVIE2_BASE_URL",
        "default": "https://newhdmovie2.pro",
        "health_path": "/",
    },
    "vega": {
        "label": "Vegamovies",
        "env": "VEGA_BASE_URL",
        "default": "https://vegamovies.global",
        "health_path": "/",
    },
    "sdmp": {
        "label": "SDMoviesPoint",
        "env": "SDMP_BASE_URL",
        "default": "https://sd1.sdmoviespoint.trade",
        "health_path": "/",
    },
    "bolly": {
        "label": "BollyFlix",
        "env": "BOLLYFLIX_BASE_URL",
        "default": "https://new.bollyflix.gd",
        "health_path": "/",
    },
    "moviesmod": {
        "label": "MoviesMod",
        "env": "MOVIESMOD_BASE_URL",
        "default": "https://moviesmod.farm",
        "health_path": "/",
    },
    "atoz": {
        "label": "AtoZ Cinemas",
        "env": "ATOZ_BASE_URL",
        "default": "https://atoz.cinemaz.workers.dev",
        "health_path": "/",
    },
    "zeefliz": {
        "label": "ZeeFliz",
        "env": "ZEEFLIZ_BASE_URL",
        "default": "https://zeefliz.beer",
        "health_path": "/",
    },
}

_SITE_URL_STORE_FILE = Path(
    os.getenv("MOVIE_SITE_URLS_FILE", "movie_site_urls.json")
)
_SITE_HEALTH_COOLDOWN = max(
    300, int(os.getenv("MOVIE_SITE_HEALTH_COOLDOWN", "21600"))
)
_SITE_HEALTH_INTERVAL = max(
    300, int(os.getenv("MOVIE_SITE_HEALTH_INTERVAL", "1800"))
)
_MOVIE_SITE_AUTO_UPDATE_REDIRECT = os.getenv(
    "MOVIE_SITE_AUTO_UPDATE_REDIRECT", "1",
).strip().lower() not in ("0", "false", "no", "off")
_site_health_alert_at: dict[str, float] = {}
_site_fail_streak: dict[str, int] = {}


def normalize_site_url(url: str) -> str:
    """Normalize a site base URL (https, no trailing slash)."""
    u = (url or "").strip()
    if not u:
        raise ValueError("URL is empty")
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    parsed = urlparse(u)
    if not parsed.netloc:
        raise ValueError(f"Invalid URL: {url}")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def get_site_url(key: str) -> str:
    meta = MOVIE_SITE_REGISTRY.get(key)
    if not meta:
        raise KeyError(f"Unknown site key: {key}")
    return _current_site_urls().get(key) or meta["default"]


def _current_site_urls() -> dict[str, str]:
    return {
        "hdhub": HDHUB_BASE.rstrip("/"),
        "hdh": HDH_BASE.rstrip("/"),
        "md": MD_BASE.rstrip("/"),
        "hdmovie2": HDMOVIE2_BASE.rstrip("/"),
        "vega": VEGA_BASE.rstrip("/"),
        "sdmp": SDMP_BASE.rstrip("/"),
        "bolly": BOLLY_BASE.rstrip("/"),
        "moviesmod": MOVIESMOD_BASE.rstrip("/"),
        "atoz": ATOZ_BASE.rstrip("/"),
        "zeefliz": ZEEFLIZ_BASE.rstrip("/"),
    }


def list_movie_sites() -> list[dict[str, str]]:
    """Return all managed movie sites with current URLs."""
    urls = _current_site_urls()
    out: list[dict[str, str]] = []
    for key, meta in MOVIE_SITE_REGISTRY.items():
        out.append({
            "key": key,
            "label": meta["label"],
            "env": meta["env"],
            "url": urls.get(key, meta["default"]),
            "default": meta["default"],
        })
    return out


def _apply_site_url(key: str, url: str) -> None:
    """Update live module globals for one site."""
    global HDHUB_BASE, HDH_BASE, HDH_CATEGORY, MD_BASE
    global HDMOVIE2_BASE, VEGA_BASE, _VEGA_SEARCH_URL, SDMP_BASE
    global BOLLY_BASE, MOVIESMOD_BASE, ATOZ_BASE, ZEEFLIZ_BASE

    url = normalize_site_url(url)
    host = urlparse(url).netloc.lower()
    if host:
        _NO_VERIFY_HOSTS.add(host)

    if key == "hdhub":
        HDHUB_BASE = url
        os.environ["HDHUB_BASE_URL"] = url
    elif key == "hdh":
        HDH_BASE = url
        HDH_CATEGORY = f"{HDH_BASE}/category/hindi-movies/"
        os.environ["HDH_BASE_URL"] = url
    elif key == "md":
        MD_BASE = url
        os.environ["MD_BASE_URL"] = url
    elif key == "hdmovie2":
        HDMOVIE2_BASE = url
        os.environ["HDMOVIE2_BASE_URL"] = url
    elif key == "vega":
        VEGA_BASE = url
        _VEGA_SEARCH_URL = VEGA_BASE + "/?do=search&subaction=search&story={}"
        os.environ["VEGA_BASE_URL"] = url
    elif key == "sdmp":
        SDMP_BASE = url
        os.environ["SDMP_BASE_URL"] = url
    elif key == "bolly":
        BOLLY_BASE = url
        os.environ["BOLLYFLIX_BASE_URL"] = url
    elif key == "moviesmod":
        MOVIESMOD_BASE = url
        os.environ["MOVIESMOD_BASE_URL"] = url
    elif key == "atoz":
        ATOZ_BASE = url
        os.environ["ATOZ_BASE_URL"] = url
    elif key == "zeefliz":
        ZEEFLIZ_BASE = url
        os.environ["ZEEFLIZ_BASE_URL"] = url
    else:
        raise KeyError(f"Unknown site key: {key}")

    # Drop cached API responses so new domain is used immediately
    _api_cache.clear()


def _load_site_url_overrides() -> dict[str, str]:
    overrides: dict[str, str] = {}
    # MongoDB first (survives Render redeploys)
    try:
        from fno_storage import use_mongodb, _get_mongo_db
        if use_mongodb():
            doc = _get_mongo_db().movie_site_urls.find_one({"_id": "urls"})
            if doc and isinstance(doc.get("sites"), dict):
                for k, v in doc["sites"].items():
                    if k in MOVIE_SITE_REGISTRY and v:
                        overrides[k] = normalize_site_url(str(v))
    except Exception as exc:
        log.debug("movie site urls mongo load skipped: %s", exc)

    # Local JSON fallback / merge (file wins over mongo for local dev)
    try:
        if _SITE_URL_STORE_FILE.exists():
            data = json.loads(_SITE_URL_STORE_FILE.read_text(encoding="utf-8"))
            sites = data.get("sites", data) if isinstance(data, dict) else {}
            if isinstance(sites, dict):
                for k, v in sites.items():
                    if k in MOVIE_SITE_REGISTRY and v:
                        overrides[k] = normalize_site_url(str(v))
    except Exception as exc:
        log.warning("movie site urls file load failed: %s", exc)
    return overrides


def _persist_site_urls(urls: dict[str, str]) -> list[str]:
    """Persist site URLs. Returns list of storage backends written."""
    saved: list[str] = []
    payload = {"sites": urls, "updated_at": time.time()}

    try:
        from fno_storage import use_mongodb, _get_mongo_db
        if use_mongodb():
            _get_mongo_db().movie_site_urls.update_one(
                {"_id": "urls"},
                {"$set": payload},
                upsert=True,
            )
            saved.append("mongodb")
    except Exception as exc:
        log.warning("movie site urls mongo save failed: %s", exc)

    try:
        _SITE_URL_STORE_FILE.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        saved.append(str(_SITE_URL_STORE_FILE))
    except Exception as exc:
        log.warning("movie site urls file save failed: %s", exc)

    # Best-effort .env update for local / Render disk
    try:
        env_path = Path(".env")
        if env_path.exists():
            text = env_path.read_text(encoding="utf-8")
            for key, url in urls.items():
                env_key = MOVIE_SITE_REGISTRY[key]["env"]
                line = f"{env_key}={url}"
                if re.search(rf"^{re.escape(env_key)}=.*$", text, flags=re.M):
                    text = re.sub(
                        rf"^{re.escape(env_key)}=.*$",
                        line,
                        text,
                        flags=re.M,
                    )
                else:
                    text = text.rstrip() + f"\n{line}\n"
            env_path.write_text(text, encoding="utf-8")
            saved.append(".env")
    except Exception as exc:
        log.debug("movie site urls .env save skipped: %s", exc)

    return saved


def set_site_url(key: str, url: str) -> dict[str, Any]:
    """Update one site base URL live and persist it."""
    key = (key or "").strip().lower()
    if key not in MOVIE_SITE_REGISTRY:
        known = ", ".join(MOVIE_SITE_REGISTRY)
        raise KeyError(f"Unknown site '{key}'. Use one of: {known}")
    old = get_site_url(key)
    new = normalize_site_url(url)
    _apply_site_url(key, new)
    urls = _current_site_urls()
    saved = _persist_site_urls(urls)
    log.info("Movie site %s URL updated: %s → %s (saved=%s)", key, old, new, saved)
    return {
        "key": key,
        "label": MOVIE_SITE_REGISTRY[key]["label"],
        "old_url": old,
        "url": new,
        "saved_to": saved,
    }


def init_movie_site_urls() -> None:
    """Load persisted overrides (Mongo/JSON) over env defaults."""
    overrides = _load_site_url_overrides()
    for key, url in overrides.items():
        try:
            _apply_site_url(key, url)
            log.info("Loaded movie site URL override %s=%s", key, url)
        except Exception as exc:
            log.warning("Skip site URL override %s: %s", key, exc)


def check_movie_site(key: str, *, timeout: int = 15) -> dict[str, Any]:
    """Probe one movie site. Detects hard failures and host redirects."""
    meta = MOVIE_SITE_REGISTRY[key]
    base = get_site_url(key)
    path = meta.get("health_path") or "/"
    probe = base.rstrip("/") + (path if path.startswith("/") else f"/{path}")
    result: dict[str, Any] = {
        "key": key,
        "label": meta["label"],
        "url": base,
        "probe_url": probe,
        "ok": False,
        "status_code": 0,
        "final_url": "",
        "redirected": False,
        "error": "",
    }
    try:
        host = urlparse(probe).netloc
        verify = host not in _NO_VERIFY_HOSTS
        session = requests.Session()
        session.headers.update(HEADERS)
        with warnings.catch_warnings():
            if not verify:
                warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            resp = session.get(
                probe,
                timeout=timeout,
                verify=verify,
                allow_redirects=True,
            )
        result["status_code"] = int(resp.status_code)
        result["final_url"] = str(resp.url)
        final_host = urlparse(str(resp.url)).netloc.lower()
        base_host = urlparse(base).netloc.lower()
        if final_host and base_host and final_host != base_host:
            result["redirected"] = True
            result["error"] = f"redirected to {final_host}"
            # Prompt admin to update to the new host
            result["ok"] = False
            result["suggested_url"] = f"{urlparse(str(resp.url)).scheme}://{final_host}"
        elif 200 <= resp.status_code < 400 and len(resp.content) > 200:
            result["ok"] = True
        else:
            result["error"] = f"HTTP {resp.status_code} ({len(resp.content)} bytes)"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def check_all_movie_sites(*, timeout: int = 15) -> list[dict[str, Any]]:
    return [check_movie_site(key, timeout=timeout) for key in MOVIE_SITE_REGISTRY]


def _site_health_chat_id() -> int | None:
    for key in ("ADMIN_ID", "OWNER_ID", "MARKET_ALERT_CHAT_ID", "HDHUB_SEARCH_ALERT_CHAT_ID"):
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return None


def format_site_health_alert(
    row: dict[str, Any],
    *,
    auto_fixed: dict[str, Any] | None = None,
) -> str:
    key = row["key"]
    label = row["label"]
    url = row["url"]
    err = html.escape(row.get("error") or "unknown error")
    if auto_fixed:
        lines = [
            "✅ <b>Movie site URL auto-updated</b>",
            "",
            f"<b>{html.escape(label)}</b> (<code>{html.escape(key)}</code>)",
            f"Old: <code>{html.escape(auto_fixed.get('old_url', url))}</code>",
            f"New: <code>{html.escape(auto_fixed.get('url', ''))}</code>",
            f"Reason: <code>{err}</code>",
        ]
        saved = auto_fixed.get("saved_to") or []
        if saved:
            lines.append(f"Saved to: <code>{html.escape(', '.join(str(s) for s in saved))}</code>")
        lines.append("")
        lines.append("No action needed unless the new URL is wrong.")
        lines.append(
            f"If wrong: <code>/setsite {html.escape(key)} https://correct-domain.example</code>"
        )
        lines.append("<code>/sites</code> · <code>/movietest</code>")
        return "\n".join(lines)

    suggested = row.get("suggested_url")
    lines = [
        "⚠️ <b>Movie site needs manual URL update</b>",
        "",
        f"<b>{html.escape(label)}</b> (<code>{html.escape(key)}</code>)",
        f"Current: <code>{html.escape(url)}</code>",
        f"Issue: <code>{err}</code>",
    ]
    if suggested:
        lines.append(f"Detected redirect: <code>{html.escape(suggested)}</code>")
        lines.append("")
        lines.append("Auto-update could not apply. Set manually:")
        lines.append(f"<code>/setsite {key} {html.escape(suggested)}</code>")
    else:
        lines.append("")
        lines.append("Could not detect the new domain automatically.")
        lines.append("Find the new site URL and run:")
        lines.append(f"<code>/setsite {key} https://new-domain.example</code>")
    lines.append("")
    lines.append("<code>/sites</code> — list all URLs")
    lines.append("<code>/movietest</code> — re-check after update")
    return "\n".join(lines)


def format_sites_list_html() -> str:
    lines = ["🌐 <b>Movie site URLs</b>", ""]
    for row in list_movie_sites():
        lines.append(
            f"• <b>{html.escape(row['label'])}</b> "
            f"(<code>{html.escape(row['key'])}</code>)\n"
            f"  <code>{html.escape(row['url'])}</code>"
        )
    lines.append("")
    lines.append("<b>Auto:</b> redirects are updated automatically + Telegram alert")
    lines.append("<b>Manual:</b> <code>/setsite &lt;key&gt; &lt;url&gt;</code> when auto-detect fails")
    lines.append("<b>Example:</b> <code>/setsite hdhub https://new2.hdhub4u.cl</code>")
    return "\n".join(lines)


async def _process_site_health_rows(bot: Any, chat_id: int, rows: list[dict[str, Any]]) -> None:
    """Evaluate health rows, auto-fix redirects, and Telegram-alert admin."""
    now = time.time()
    for row in rows:
        key = row["key"]
        if row.get("ok"):
            _site_fail_streak[key] = 0
            clear_site_api_skip(key)
            continue

        redirected = bool(row.get("redirected"))
        streak = _site_fail_streak.get(key, 0) + 1
        _site_fail_streak[key] = streak
        # Don't let broken/stale domains slow the movies API
        if streak >= 1:
            mark_site_api_skip(key)
        # Redirects are definitive — alert on first detection; hard failures need 2 in a row
        if streak < (1 if redirected else 2):
            continue
        if now - _site_health_alert_at.get(key, 0.0) < _SITE_HEALTH_COOLDOWN:
            continue

        auto_fixed: dict[str, Any] | None = None
        if redirected and _MOVIE_SITE_AUTO_UPDATE_REDIRECT and row.get("suggested_url"):
            try:
                auto_fixed = await asyncio.to_thread(
                    set_site_url, key, row["suggested_url"],
                )
                # Re-check — if still broken, clear auto_fixed so user gets /setsite prompt
                recheck = await asyncio.to_thread(check_movie_site, key, timeout=12)
                if not recheck.get("ok"):
                    log.warning(
                        "Movie site %s still unhealthy after auto-update to %s",
                        key,
                        auto_fixed.get("url"),
                    )
                    row = {**row, **recheck, "error": recheck.get("error") or row.get("error")}
                    auto_fixed = None
                else:
                    log.info(
                        "Movie site %s auto-updated via redirect: %s → %s",
                        key,
                        auto_fixed.get("old_url"),
                        auto_fixed.get("url"),
                    )
                    clear_site_api_skip(key)
            except Exception as exc:
                log.error("Movie site auto-update failed for %s: %s", key, exc)
                row = {**row, "error": f"auto-update failed: {exc}"}

        _site_health_alert_at[key] = now
        text = format_site_health_alert(row, auto_fixed=auto_fixed)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            log.warning(
                "Movie site alert sent for %s: %s (auto_fixed=%s)",
                key,
                row.get("error"),
                bool(auto_fixed),
            )
        except Exception as exc:
            log.error("Movie site alert send failed: %s", exc)


async def run_startup_site_health(bot: Any) -> None:
    """One-shot health check right after bot boot (catches stale Mongo/env URLs)."""
    chat_id = _site_health_chat_id()
    if not chat_id:
        return
    try:
        rows = await asyncio.to_thread(check_all_movie_sites, timeout=12)
        await _process_site_health_rows(bot, chat_id, rows)
    except Exception as exc:
        log.error("Startup site health check failed: %s", exc)


async def run_movie_site_monitor(bot: Any) -> None:
    """Periodically probe movie sites and Telegram-alert admin on failures."""
    chat_id = _site_health_chat_id()
    if not chat_id:
        log.warning("Movie site monitor disabled (no ADMIN_ID / OWNER_ID)")
        return
    log.info(
        "Movie site health monitor every %ss → chat %s",
        _SITE_HEALTH_INTERVAL,
        chat_id,
    )
    # First run after a short delay so startup isn't blocked
    await asyncio.sleep(45)
    while True:
        try:
            rows = await asyncio.to_thread(check_all_movie_sites, timeout=12)
            await _process_site_health_rows(bot, chat_id, rows)
        except Exception as exc:
            log.error("Movie site monitor loop error: %s", exc)
        await asyncio.sleep(_SITE_HEALTH_INTERVAL)


# Call init_movie_site_urls() from bot startup (not at import — avoids Mongo hang).
