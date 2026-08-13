"""
Twitter / X video downloader for the Telegram bot.

Uses yt-dlp (already a project dependency for the Udemy downloader) with its
native [twitter] extractor, so twitter.com and x.com post links both work.
Public tweets download without any login. Tweets flagged as sensitive/adult,
or datacenter-IP blocks (common on Render), may require a cookies file —
point TWITTER_COOKIES_FILE at a Netscape cookies.txt exported from a logged-in
browser session (e.g. with the "Get cookies.txt LOCALLY" extension).

Telegram's Bot API upload limit is 50 MB, so downloads are capped at 49 MB.
If a video is bigger, resolve_direct_url() hands back the raw video URL as a
fallback link.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

TWITTER_URL_RE = re.compile(
    r"^https?://(?:www\.|mobile\.|m\.)?(?:twitter\.com|x\.com)/[^/]+/status/\d+",
    re.IGNORECASE,
)

MAX_TELEGRAM_BYTES = 49 * 1024 * 1024  # Bot API cap is 50 MB; keep headroom


class TwitterDownloadError(Exception):
    """Raised with a user-friendly message when a download can't complete."""


def is_twitter_url(url: str) -> bool:
    """True for links like https://x.com/user/status/123456789 (twitter.com too)."""
    return bool(TWITTER_URL_RE.match((url or "").strip()))


def _friendly_error(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if any(k in low for k in ("sign in", "login", "auth", "sensitive", "age", "private")):
        return (
            "X blocked this tweet without a login (private or sensitive content). "
            "Set TWITTER_COOKIES_FILE to a cookies.txt from a logged-in browser and redeploy."
        )
    if any(k in low for k in ("too many", "rate limit", "429", "403", "blocked", "cloudflare")):
        return (
            "X is rate-limiting or blocking this server's IP. Retry in a few minutes, "
            "or set TWITTER_COOKIES_FILE to a logged-in cookies.txt."
        )
    if "max-filesize" in low or "larger than" in low:
        return "The video is larger than Telegram's 50 MB limit."
    if "unsupported url" in low or "not a valid url" in low:
        return "That doesn't look like a Twitter/X post URL."
    if any(k in low for k in ("no video", "no media", "no formats", "is not a video", "no download")):
        return "This tweet has no downloadable video."
    return msg[:300]


def _ydl_base_opts() -> dict:
    cookiefile = os.getenv("TWITTER_COOKIES_FILE", "").strip()
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 5,
        "socket_timeout": 25,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def download_twitter_video(
    url: str,
    work_dir: str | os.PathLike | None = None,
    *,
    max_bytes: int = MAX_TELEGRAM_BYTES,
) -> dict:
    """Download a Twitter/X video and return metadata about the produced file.

    Returns a dict: {path, title, uploader, duration, filesize, ext}.
    Raises TwitterDownloadError (user-friendly message) on any failure.
    The returned file lives in a per-call temp dir; the caller must delete it.
    """
    import yt_dlp

    url = (url or "").strip()
    if not is_twitter_url(url):
        raise TwitterDownloadError(
            "Please send a Twitter/X post link, e.g. https://x.com/user/status/123456789"
        )

    out_dir = Path(tempfile.mkdtemp(prefix="twdl_", dir=work_dir))
    opts = _ydl_base_opts()
    opts.update({
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "max_filesize": max_bytes,
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = sorted(out_dir.iterdir()) if out_dir.exists() else []
        if not files:
            raise TwitterDownloadError("Download finished but no file was produced.")
        path = files[0]
        return {
            "path": path,
            "title": (info or {}).get("title") or path.stem,
            "uploader": (info or {}).get("uploader") or (info or {}).get("channel") or "",
            "duration": (info or {}).get("duration"),
            "filesize": path.stat().st_size,
            "ext": path.suffix.lstrip(".").lower(),
        }
    except TwitterDownloadError:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise TwitterDownloadError(_friendly_error(e)) from e


def resolve_direct_url(url: str) -> str | None:
    """Best direct mp4 URL for a tweet without downloading. None if unavailable.

    Used as a fallback when a video is too big for Telegram or the upload fails.
    Returns None when the tweet has no direct (non-HLS) media URL.
    """
    import yt_dlp

    url = (url or "").strip()
    if not is_twitter_url(url):
        return None
    opts = _ydl_base_opts()
    opts.update({"skip_download": True, "simulate": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        formats = info.get("formats") or []
        if not formats:
            return None
        # Largest direct http(s) mp4 first; skip HLS manifests (not browser-playable).
        candidates = [
            f for f in formats
            if f.get("url")
            and (f.get("protocol") or "").startswith("http")
            and ".m3u8" not in (f.get("url") or "").lower()
            and (f.get("ext") or "mp4") == "mp4"
        ]
        candidates.sort(key=lambda f: (f.get("filesize") or 0) or (f.get("tbr") or 0) * 1024 * 8, reverse=True)
        return candidates[0]["url"] if candidates else None
    except Exception as e:
        log.debug("resolve_direct_url failed for %s: %s", url, e)
        return None
