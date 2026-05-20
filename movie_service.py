"""
Movie scraper — supports multiple sources:
  - HDHub4u         (https://new1.hdhub4u.limo/)          ← primary
  - 4KHDHub         (https://4khdhub.link/category/hindi-movies/)
  - MoviesDrive     (https://new2.moviesdrives.my/)
  - Movies4U        (https://movies4u.ee/)
  - Vegamovies      (https://vegamovies.global/)
  - SDMoviesPoint   (https://sd1.sdmoviespoint.trade/)
  - BollyFlix       (https://new.bollyflix.gd/)
  - MoviesMod       (https://moviesmod.farm/)
  - AtoZ Cinemas    (https://atoz.cinemaz.workers.dev/)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.parse
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "new1.hdhub4u.limo",  # HDHub4u - SSL cert issues
    "hdhub4u.limo",
    "movies4u.ee",  # Movies4U - occasional SSL issues
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

HDH_BASE     = os.getenv("HDH_BASE_URL", "https://4khdhub.link")
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


# ─── Movies4U ────────────────────────────────────────────────────────────────

M4U_BASE   = os.getenv("M4U_BASE_URL", "https://movies4u.ee")
M4U_SEARCH = f"{M4U_BASE}/?s="

# Domains to treat as ads / skip
_M4U_AD_HOSTS = {"swagvio.com", "fuckmaza.com", "hianime.mx"}

# Recognised download providers on mdrive.ink
_M4U_DL_HOSTS = {
    "fastdl.zip", "vcloud.zip", "filebee.xyz", "dgdrive.site",
    "hubcloud.foo", "hubdrive.space", "gdflix.pro", "gofile.io",
    "mediafire.com", "pixeldrain.com", "1fichier.com",
}


def _is_m4u_dl(href: str) -> bool:
    host = urllib.parse.urlparse(href).hostname or ""
    return any(h in host for h in _M4U_DL_HOSTS)


def _m4u_provider(href: str) -> str:
    host = urllib.parse.urlparse(href).hostname or ""
    host = host.lstrip("www.")
    # Friendly names
    _MAP = {
        "fastdl.zip": "G-Direct",
        "vcloud.zip": "V-Cloud",
        "filebee.xyz": "Filepress",
        "dgdrive.site": "DropGalaxy",
    }
    for domain, name in _MAP.items():
        if domain in host:
            return name
    return host.split(".")[0].title()


def _scrape_mdrive_ink(mdrive_url: str) -> list[dict[str, Any]]:
    """Scrape an mdrive.ink/mdisk page — returns links grouped by quality label."""
    try:
        resp = _get(mdrive_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        links: list[dict[str, Any]] = []
        current_label = "Download"

        # Walk all children of <body> in order; h5 = quality label, a = link
        for tag in soup.find_all(["h5", "a"]):
            if tag.name == "h5":
                current_label = tag.get_text(strip=True)
            elif tag.name == "a":
                href = tag.get("href", "")
                host = urllib.parse.urlparse(href).hostname or ""
                # Skip ads and non-download links
                if not href.startswith("http"):
                    continue
                if any(ad in host for ad in _M4U_AD_HOSTS):
                    continue
                if not _is_m4u_dl(href):
                    continue
                prov = _m4u_provider(href)
                links.append({
                    "label": current_label,
                    "name": f"{current_label} ({prov})",
                    "url": href,
                })
        return links
    except Exception as e:
        log.error("mdrive.ink scrape failed (%s): %s", mdrive_url, e)
        return []


def _scrape_m4u_info(content) -> dict[str, str]:
    """Parse movies4u.ee info block (plain text 'Label:\\nValue' format)."""
    info: dict[str, str] = {}
    text = content.get_text("\n", strip=True)

    # Map of search patterns → field name
    _PATTERNS = [
        (r"IMDb\s+Rating\s*[:\-]+\s*([\d./]+)", "imdb"),
        (r"Language\s*:\s*\n?((?:(?!(?:Subtitle|Size|Quality|Format|Movie|Release|Genre|Director|Cast|Plot|Synopsis))[^\n])+)", "language"),
        (r"Genre\s*:\s*\n?([^\n]+)", "genre"),
        (r"Quality\s*:\s*\n?([^\n]+)", "quality"),
        (r"(?:Director|Directed by)\s*:\s*\n?([^\n]+)", "director"),
        (r"(?:Stars?|Cast)\s*:\s*\n?([^\n]+)", "stars"),
    ]
    for pattern, field in _PATTERNS:
        if field in info:
            continue
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip(".")
            if val:
                info[field] = val
    return info


def m4u_latest_movies(page: int = 1) -> list[dict[str, str]]:
    """Scrape page N of movies4u.ee homepage."""
    try:
        url = M4U_BASE + "/" if page == 1 else f"{M4U_BASE}/page/{page}/"
        resp = _get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        movies: list[dict[str, str]] = []
        for art in soup.select("article"):
            title_a = art.select_one(".entry-title a, h2 a, h3 a")
            if not title_a:
                continue
            title = title_a.get_text(strip=True)
            link  = title_a.get("href", "")
            img   = art.select_one("img")
            poster = img.get("src", "") or img.get("data-src", "") if img else ""
            if link:
                movies.append({"title": title, "url": link,
                               "poster": poster, "source": "m4u"})
            if len(movies) >= 10:
                break
        return movies
    except Exception as e:
        log.error("Movies4U listing failed: %s", e)
        return []


_M4U_SKIP_HEADINGS = {
    "series info:", "movie info:", "series-synopsis/plot:", "screenshots:",
    "synopsis/plot:", "plot:", "——", "—–", "download",
}

_M4U_QUALITY_KEYWORDS = ("480p", "720p", "1080p", "2160p", "4k", "season", "episode", "blu", "web")


def _is_gdirect(text: str) -> bool:
    t = text.lower()
    return "g-direct" in t or "instant" in t


def m4u_movie_links(movie_url: str) -> dict[str, Any]:
    """Scrape a movies4u.ee movie page.

    Two page layouts:
      A) Series  — h3/h4 quality labels, multiple mdrive.ink buttons per label
                   → parse labels from page, keep only G-Direct mdrive.ink link per quality
      B) Movie   — single mdrive.ink URL for all qualities
                   → follow it, parse quality sections inside, keep G-Direct links
    """
    try:
        resp = _get(movie_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        poster = ""
        og = soup.find("meta", property="og:image")
        if og:
            poster = og.get("content", "")

        content = soup.select_one(".entry-content") or soup.find("main") or soup
        info = _scrape_m4u_info(content)

        # ── Walk DOM in order: headings become labels, <a> → mdrive.ink ────────
        current_label = ""
        collected: list[dict] = []   # {"label", "url", "is_gdirect"}

        for tag in content.find_all(["h2", "h3", "h4", "h5", "strong", "a"]):
            if tag.name in ("h2", "h3", "h4", "h5"):
                txt = tag.get_text(strip=True)
                # Skip non-quality headings
                if txt.lower().rstrip(":").strip() in _M4U_SKIP_HEADINGS:
                    continue
                if any(kw in txt.lower() for kw in _M4U_QUALITY_KEYWORDS):
                    current_label = txt
            elif tag.name == "strong":
                txt = tag.get_text(strip=True)
                # Some pages use <strong> as quality label
                if any(kw in txt.lower() for kw in _M4U_QUALITY_KEYWORDS) and len(txt) > 8:
                    current_label = txt
            elif tag.name == "a":
                href = tag.get("href", "")
                if "mdrive.ink/mdisk" not in href:
                    continue
                btn_text = tag.get_text(strip=True)
                collected.append({
                    "label": current_label,
                    "url": href,
                    "is_gdirect": _is_gdirect(btn_text),
                    "btn_text": btn_text,
                })

        # ── Detect layout ────────────────────────────────────────────────────
        labeled = [c for c in collected if c["label"]]
        unique_urls = {c["url"] for c in collected}

        links: list[dict[str, Any]] = []

        if labeled and len(unique_urls) > 1:
            # ── Layout A: Series — multiple labeled mdrive.ink URLs ───────────
            # Resolve each mdrive URL to get final links (Hub-Cloud, GDFlix, G-Direct, etc.)
            by_label: dict[str, list] = {}
            for c in collected:
                label = c["label"] or "Download"
                by_label.setdefault(label, []).append(c)

            for label, entries in by_label.items():
                # Get all unique mdrive URLs for this quality
                seen_urls = set()
                for entry in entries:
                    mdrive_url = entry["url"]
                    if mdrive_url in seen_urls:
                        continue
                    seen_urls.add(mdrive_url)
                    
                    # Resolve mdrive URL to get final links
                    final_links = _resolve_mdrive_link(mdrive_url)
                    if final_links and final_links != mdrive_url:
                        # Parse the resolved links (they're newline-separated)
                        for line in final_links.split('\n'):
                            if ':' in line:
                                provider, url = line.split(':', 1)
                                provider = provider.strip()
                                url = url.strip()
                                links.append({
                                    "label": label,
                                    "name": provider,
                                    "url": url,
                                })
                    else:
                        # Fallback to original mdrive URL
                        links.append({
                            "label": label,
                            "name": _m4u_provider_from_btn(entry["btn_text"]),
                            "url": mdrive_url,
                        })

        elif collected:
            # ── Layout B: Movie — one mdrive.ink URL, follow it ───────────────
            mdrive_url = list(unique_urls)[0]
            final_links = _resolve_mdrive_link(mdrive_url)
            
            if final_links and final_links != mdrive_url:
                # Parse all resolved links
                current_label = "Download"
                for line in final_links.split('\n'):
                    if ':' in line:
                        provider, url = line.split(':', 1)
                        provider = provider.strip()
                        url = url.strip()
                        links.append({
                            "label": current_label,
                            "name": provider,
                            "url": url,
                        })
            else:
                # Fallback to original scraping method
                raw_links = _scrape_mdrive_ink(mdrive_url)
                by_lbl: dict[str, list] = {}
                for l in raw_links:
                    by_lbl.setdefault(l["label"], []).append(l)
                for lbl, entries in by_lbl.items():
                    gdirect = [l for l in entries if "fastdl.zip" in l.get("url", "")]
                    best = gdirect[0] if gdirect else entries[0]
                    links.append({
                        "label": best["label"],
                        "name": "G‑Direct" if gdirect else _m4u_provider(best["url"]),
                        "url": best["url"],
                    })

        return {"poster": poster, "links": links, "info": info}
    except Exception as e:
        log.error("Movies4U movie page failed (%s): %s", movie_url, e)
        return {"poster": "", "links": [], "info": {}}


def _m4u_provider_from_btn(btn_text: str) -> str:
    t = btn_text.lower()
    if "v-cloud" in t or "resumable" in t:
        return "V‑Cloud"
    if "batch" in t or "zip" in t:
        return "Batch/Zip"
    return btn_text.strip("⚡ ").split("[")[0].strip()


def m4u_search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Search movies4u.ee via WordPress ?s= parameter."""
    try:
        resp = _get(M4U_SEARCH + urllib.parse.quote_plus(query), timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        movies: list[dict[str, str]] = []
        for art in soup.select("article"):
            title_a = art.select_one(".entry-title a, h2 a, h3 a")
            if not title_a:
                continue
            title = title_a.get_text(strip=True)
            link  = title_a.get("href", "")
            img   = art.select_one("img")
            poster = img.get("src", "") or img.get("data-src", "") if img else ""
            if link:
                movies.append({"title": title, "url": link,
                               "poster": poster, "source": "m4u"})
            if len(movies) >= limit:
                break
        return movies
    except Exception as e:
        log.error("Movies4U search failed for '%s': %s", query, e)
        return []


def format_m4u_message(movie_title: str, data: dict[str, Any], footer: bool = True) -> str:
    """Format movies4u.ee result — grouped by title and quality."""
    links = data.get("links", [])
    if not links:
        return ""

    lines = []
    if movie_title:
        lines.append(f"🎬 <b>{movie_title}</b>")
    lines.append("━" * 32)

    info = data.get("info", {})
    info_lines = _info_block(info)
    if info_lines:
        lines.extend(info_lines)
        lines.append("━" * 32)

    lines.append("\n📥 <b>Download Links</b>  <i>(Movies4U)</i>\n")

    import re as _re
    
    def _extract_title_and_quality(label: str, provider_name: str) -> tuple[str, str]:
        """Extract title and quality from label and provider name."""
        # Look for quality pattern: 480p, 720p, 1080p with optional HEVC/HQ and size [XGB]
        quality_match = _re.search(r'(\d{3,4}p)\s*(?:HEVC|HQ|x264)?\s*\[([^\]]+)\]', label)
        
        if quality_match:
            quality = quality_match.group(1)
            size = quality_match.group(2)
            hevc = "HEVC" if "HEVC" in label else ""
            hq = "HQ" if "HQ" in label else ""
            
            # Build quality string
            qual_parts = [p for p in [quality, hevc, hq, f"[{size}]"] if p]
            quality_str = " ".join(qual_parts)
            
            # Extract title - everything before the quality
            title_part = label[:quality_match.start()].strip()
            # Remove movie name (usually at start) and year
            title_part = _re.sub(r'^[\w\s\-:]+\(\d{4}\)', '', title_part).strip()
            # Remove WEB-LEAK, HDTC, etc.
            title_part = _re.sub(r'WEB-LEAK|HDTC|HDCAM|DVDScr|WEB-DL|WEB-Rip|HDRip', '', title_part, flags=re.IGNORECASE).strip()
            # Clean up
            title_part = title_part.replace('  ', ' ').strip(' -[]')
            
            return (title_part if title_part else "Movie", quality_str)
        
        # If label is generic like "Download", try to extract from movie info
        if label in ['Download', '']:
            info_quality = info.get('quality', '')
            if info_quality:
                # Parse qualities like "480p || 720p || 1080p"
                qualities = [q.strip() for q in info_quality.split('||') if q.strip() and 'p' in q]
                if qualities:
                    return ("Movie", " | ".join(qualities))
            return ("Movie", "Download")
        
        # Fallback: just find resolution
        match = _re.search(r'(\d{3,4}p)', label)
        if match:
            return ("Movie", match.group(1))
        
        return (label[:40], "Download")

    def _clean_provider_name(name: str, url: str) -> str:
        """Clean up provider name for display."""
        if not name:
            name = _m4u_provider(url)
        
        # Remove emojis and clean up
        clean = name.replace('🚀', '').replace('⚡', '').replace('[DD]', '').replace('[Instant]', '').replace('[Resumable]', '').replace('[G-Drive]', '').strip()
        
        # Shorten common names
        shorten_map = {
            'Hub-Cloud': 'HubCloud',
            'G-Direct': 'GDirect',
            'V-Cloud': 'VCloud',
            'Filepress': 'Filepress',
            'GDFlix': 'GDFlix',
        }
        for full, short in shorten_map.items():
            if full in clean:
                clean = short
                break
        
        # Limit length
        if len(clean) > 12:
            clean = clean[:10] + ".."
        
        return clean

    # Group by (title, quality) tuple
    grouped: dict[tuple[str, str], list] = {}
    for lnk in links:
        title, quality = _extract_title_and_quality(lnk.get("label", ""), lnk.get("name", ""))
        key = (title, quality)
        grouped.setdefault(key, []).append(lnk)
    
    # Remove duplicate URLs within each group
    for key in grouped:
        seen_urls = set()
        unique_links = []
        for lnk in grouped[key]:
            url = lnk.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_links.append(lnk)
        grouped[key] = unique_links

    # Sort: by title first, then by quality (480p < 720p < 1080p)
    def _sort_key(item: tuple) -> tuple:
        (title, quality), _ = item
        # Quality sort order
        q_order = 3
        if "480p" in quality:
            q_order = 0
        elif "720p" in quality:
            q_order = 1
        elif "1080p" in quality:
            q_order = 2
        return (title, q_order)

    sorted_groups = sorted(grouped.items(), key=_sort_key)

    current_title = None
    for (title, quality), group in sorted_groups:
        # Print title header when it changes
        if title != current_title:
            if current_title is not None:
                lines.append("")  # Blank line between sections
            lines.append(f"📁 <b>{title}</b>")
            current_title = title
        
        # Print quality and links
        lines.append(f"  📦 {quality}")
        
        # Format provider links - group by provider type to avoid duplicates
        seen_providers = set()
        parts = []
        for l in group:
            name = _clean_provider_name(l.get('name', ''), l['url'])
            # Skip if we already have this provider
            if name in seen_providers:
                continue
            seen_providers.add(name)
            parts.append(f"<a href='{l['url']}'>{name}</a>")
        
        lines.append("     🔗 " + " · ".join(parts))

    if footer:
        lines.append("")
        lines.append("━" * 32)
        lines.append("⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")
    
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
#  HDHub4u  (https://new1.hdhub4u.limo/)  — primary source
# ══════════════════════════════════════════════════════════════════════════════

HDHUB_BASE        = os.getenv("HDHUB_BASE_URL", "https://new1.hdhub4u.limo")
_HDHUB_SEARCH_API = "https://search.hdhub4u.glass/collections/post/documents/search"
_HDHUB_SKIP_LABELS = {"stream"}


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


def hdhub_search(query: str, limit: int = 10) -> list[dict]:
    """Search HDHub4u via Typesense API (JS search backend)."""
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        resp = sess.get(
            _HDHUB_SEARCH_API,
            params={
                "q":                query,
                "query_by":         "post_title,category,stars,director,imdb_id",
                "sort_by":          "sort_by_date:desc",
                "limit":            limit,
                "highlight_fields": "none",
                "use_cache":        "true",
            },
            timeout=20,
        )
        data = resp.json()
    except Exception as exc:
        log.error("hdhub_search '%s' failed: %s", query, exc)
        return []

    movies: list[dict] = []
    for hit in data.get("hits", []):
        doc       = hit.get("document", {})
        title     = doc.get("post_title", "")
        permalink = doc.get("permalink", "")
        thumbnail = doc.get("post_thumbnail", "")
        if title and permalink:
            movies.append({
                "title":  title,
                "url":    HDHUB_BASE.rstrip("/") + "/" + permalink.lstrip("/"),
                "poster": thumbnail,
            })
    return movies


def hdhub_movie_links(movie_url: str) -> dict[str, Any]:
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
    try:
        resp = _get(movie_url, timeout=25)
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

    if _needs_render:
        log.info("hdhub_movie_links: no links in static HTML, trying Playwright for %s", movie_url)
        rendered_html = _get_rendered_html(movie_url, timeout=30, wait_ms=5000)
        if rendered_html:
            soup = BeautifulSoup(rendered_html, "html.parser")
        elif soup is None:
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

def _resolve_mdrive_link(url: str) -> str:
    """
    Resolves mdrive.ink/mdisk links to their final download links.
    The mdrive page contains multiple final links (Hub-Cloud, GDFlix, G-Direct, V-Cloud, etc.)
    Returns a string with all final links separated by newlines.
    Uses _get() for cloud deployment compatibility (ScraperAPI support).
    """
    try:
        # Use _get() which handles ScraperAPI for cloud deployments
        resp = _get(url, timeout=20)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        final_links = []
        seen_urls = set()
        
        # Find all links on the page
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            text = a.get_text(strip=True)
            text_lower = text.lower()
            
            # Skip navigation and irrelevant links
            if not href or any(x in href for x in ['mdrive.ink', 'javascript:', '#', 'wp-content', 'swagvio']):
                continue
            if any(x in text_lower for x in ['skip to content', 'home', 'nex drive', 'join now', 'adult', 'meet place', 'find perfect']):
                continue
            
            # Skip text that looks like a URL protocol
            if text_lower in ['https', 'http']:
                continue
                
            # These are the final download links we want
            is_provider = any(x in text_lower for x in ['hub-cloud', 'gdflix', 'g-direct', 'v-cloud', 'filepress', 
                                               'gofile', 'vikingfile', 'megaup', 'fastdl', 'filebee'])
            # Also include links to known file hosts even without specific button text
            is_filehost = any(x in href for x in ['gofile.io', 'vikingfile.com', 'megaup.net', 'hubcloud.foo', 
                                                    'gdflix.dev', 'fastdl.zip', 'vcloud.zip', 'filebee.xyz'])
            
            if is_provider or is_filehost:
                # Clean up the link
                if href.startswith('//'):
                    href = 'https:' + href
                elif href.startswith('/'):
                    from urllib.parse import urljoin
                    href = urljoin(resp.url, href)
                
                # Skip duplicates
                if href in seen_urls:
                    continue
                seen_urls.add(href)
                
                # Use appropriate label
                label = text if text else _m4u_provider(href)
                    
                final_links.append(f"{label}: {href}")
        
        if final_links:
            log.info("Resolved mdrive link %s to %d final links", url, len(final_links))
            return "\n".join(final_links)
        
        log.warning("No final links found for %s, returning original", url)
        return url
        
    except Exception as e:
        log.error("Error resolving mdrive link %s: %s", url, e)
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


def atoz_movie_links(movie_url: str) -> dict[str, Any]:
    """Fetch download links for an AtoZ movie page."""
    movie_url = _atoz_abs_url(movie_url)
    result: dict[str, Any] = {
        "poster": "",
        "info": {},
        "links": [],
        "episodes": [],
        "is_series": False,
    }
    try:
        resp = _get(movie_url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("atoz_movie_links %s failed: %s", movie_url, exc)
        return result

    for img in soup.find_all("img"):
        src = img.get("src", "") or ""
        if "tmdb" in src or (img.get("class") and "poster" in " ".join(img.get("class", []))):
            result["poster"] = src
            break
    if not result["poster"]:
        img = soup.select_one("img")
        if img:
            result["poster"] = img.get("src", "") or img.get("data-src", "") or ""

    buttons = soup.find_all("button", attrs={"data-id": True})
    if len(buttons) > 1:
        result["is_series"] = True

    seen_ids: set[str] = set()
    for btn in buttons:
        data_id = btn.get("data-id", "").strip()
        if not data_id or data_id in seen_ids:
            continue
        seen_ids.add(data_id)

        try:
            gen_resp = _get(f"{ATOZ_BASE}/generate_links?id={data_id}", timeout=25)
            data = gen_resp.json()
        except Exception as exc:
            log.warning("atoz generate_links id=%s failed: %s", data_id, exc)
            continue

        final_url = data.get("url", "")
        if not final_url:
            continue

        file_name = data.get("file_name", "") or _atoz_button_label(btn)
        file_size = data.get("file_size", "")
        label = f"{file_name} ({file_size})" if file_size else file_name
        result["links"].append({"label": label, "url": final_url})

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
