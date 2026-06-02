"""
Multi-Account Udemy Auto-Enroller Bot
- Multiple accounts per user
- Auto-enroll background job (checks API every 10 min)
- Notifications when new courses are enrolled
"""

import asyncio
import logging
import os
import shutil
import tempfile
import zipfile
import requests
from datetime import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ContextTypes, Application

from udemy_enroller import Course, UdemyAutoEnroller
from user_enroller import (
    init_enroller_db,
    add_account, get_user_accounts, get_account, remove_account, toggle_auto_enroll,
    get_all_auto_enroll_accounts, find_existing_account, update_account_token,
    add_to_download_queue, get_owner_download_queue, remove_from_download_queue, clear_owner_download_queue,
    set_user_setup_state, get_user_setup_state, clear_user_setup_state,
    DEFAULT_CLIENT_ID,
    get_auto_enroll_state, set_auto_enroll_enabled, update_auto_enroll_state,
    log_enrollment, is_course_enrolled, get_recently_enrolled,
    user_has_credentials, get_user_stats,
    validate_token_format, validate_client_id_format, get_setup_instructions,
    delete_user_data,
    # Premium & Access Control
    is_owner, is_premium, grant_premium, revoke_premium, get_all_premium_users,
    can_enroll, get_remaining_today, increment_daily_usage, FREE_DAILY_LIMIT,
    get_all_daily_stats, get_daily_usage, get_user_total_enrollments,
    # Settings
    is_channel_posting_enabled, toggle_channel_posting,
)

CHANNEL_ID = os.getenv("CHANNEL_ID", "")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

log = logging.getLogger(__name__)

COURSES_API = "https://cdn.real.discount/api/courses"
AUTO_ENROLL_INTERVAL = 120  # 2 minutes


# ─── Premium Management Commands (Owner Only) ─────────────────────────────────

async def cmd_grant_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grant premium access to a user (owner only)"""
    if not update.effective_user or not update.effective_message:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return
    
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/grant_premium <user_id>`\nExample: `/grant_premium 123456789`",
            parse_mode="Markdown"
        )
        return
    
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid user ID. Must be a number.")
        return
    
    if grant_premium(target_id, update.effective_user.id):
        await update.effective_message.reply_text(
            f"✅ Premium access granted to user `{target_id}`",
            parse_mode="Markdown"
        )
    else:
        await update.effective_message.reply_text("❌ Failed to grant premium.")


async def cmd_revoke_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke premium access from a user (owner only)"""
    if not update.effective_user or not update.effective_message:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return
    
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/revoke_premium <user_id>`\nExample: `/revoke_premium 123456789`",
            parse_mode="Markdown"
        )
        return
    
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid user ID.")
        return
    
    if revoke_premium(target_id):
        await update.effective_message.reply_text(
            f"✅ Premium revoked from user `{target_id}`",
            parse_mode="Markdown"
        )
    else:
        await update.effective_message.reply_text("❌ Failed to revoke premium.")


async def cmd_list_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all premium users (owner only)"""
    if not update.effective_user or not update.effective_message:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return
    
    users = get_all_premium_users()
    if not users:
        await update.effective_message.reply_text("No premium users.")
        return
    
    lines = ["👑 **Premium Users:**\n"]
    for u in users:
        lines.append(f"• `{u['user_id']}` (granted: {u['granted_at'][:10]})")
    
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle channel posting for free courses (owner only)"""
    if not update.effective_user or not update.effective_message:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return
    
    current = is_channel_posting_enabled()
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔴 Turn OFF" if current else "🟢 Turn ON",
            callback_data="toggle_channel_post"
        ),
    ]])
    
    status = "🟢 **ON**" if current else "🔴 **OFF**"
    channel_info = f"Channel: `{CHANNEL_ID}`" if CHANNEL_ID else "⚠️ CHANNEL_ID not set in .env"
    
    await update.effective_message.reply_text(
        f"📢 **Channel Course Posting**\n\n"
        f"Status: {status}\n"
        f"{channel_info}\n\n"
        f"When ON, free Udemy courses are automatically posted to the channel.\n"
        f"When OFF, course posting to channel is paused.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ─── Owner: Search enrolled courses across all linked accounts & archive ─────

async def cmd_search_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: Search courses the linked Udemy accounts are enrolled in."""
    if not update.effective_user or not update.effective_message:
        return

    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/search_courses <keywords>`\n\n"
            "Searches within the courses your linked Udemy accounts are already enrolled in.\n"
            "Example: `/search_courses machine learning`",
            parse_mode="Markdown"
        )
        return

    query = " ".join(context.args).strip()
    if len(query) < 2:
        await update.effective_message.reply_text("Please provide at least 2 characters to search.")
        return

    msg = await update.effective_message.reply_text(f"🔍 Searching enrolled courses for “{query}” across your accounts...")

    accounts = get_user_accounts(user_id)
    if not accounts:
        await msg.edit_text("No Udemy accounts linked. Use `/enroll_setup` first.")
        return

    all_results = []
    seen_ids = set()
    # Search each account's library (page 1 is usually the most relevant)
    for acc in accounts:
        try:
            enroller = UdemyAutoEnroller(acc["access_token"], acc.get("client_id"))
            res = await asyncio.to_thread(enroller.search_enrolled_courses, query, 1, 20)
            for item in res.get("results", []):
                uid = item.get("id")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    item["source_account_id"] = acc["id"]  # mongo account id for preferring cookies later
                    item["source_account_name"] = acc.get("name", "Account")
                    all_results.append(item)
        except Exception as e:
            log.warning(f"Search failed on account {acc.get('name')}: {e}")

    if not all_results:
        await msg.edit_text(f"😕 No enrolled courses matched “{query}” in any of your accounts.")
        return

    # Store for pagination / selection in this chat
    context.user_data["dl_search"] = {
        "query": query,
        "page": 1,
        "results": all_results[:30]  # cap
    }

    await _send_search_results(msg, context, all_results[:10], query, 1)


async def cmd_download_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: Show current archive/download queue."""
    if not update.effective_user or not update.effective_message:
        return
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only.")
        return
    queue = get_owner_download_queue(update.effective_user.id)
    await update.effective_message.reply_text(
        "Use the buttons below or search with /search_courses.",
        parse_mode="Markdown"
    )
    await _show_download_queue(update.effective_message, queue)


async def _send_search_results(msg, context, results, query, page):
    """Render a page of search results with Select buttons."""
    lines = [f"🔍 **Search results for “{query}”** (page {page})\n"]
    keyboard = []

    for idx, r in enumerate(results):
        title = (r.get("title") or "Untitled")[:55]
        headline = (r.get("headline") or "")[:70]
        paid = "💰 Paid" if r.get("is_paid") else "🆓 Free"
        rating = f"⭐ {r.get('avg_rating'):.1f}" if r.get("avg_rating") else ""
        src = r.get("source_account_name", "")
        lines.append(f"**{title}**\n{headline}\n{paid} {rating} · via {src}\n")

        udemy_id = r.get("id")
        keyboard.append([
            InlineKeyboardButton(f"📥 Select “{title[:30]}”", callback_data=f"dl_select_{udemy_id}"),
        ])

    # Pagination (simple, we have the full list in context for now)
    total = len(context.user_data.get("dl_search", {}).get("results", []))
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data="dl_prev"))
    if (page * 10) < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data="dl_next"))
    if nav:
        keyboard.append(nav)

    keyboard.append([
        InlineKeyboardButton("📋 View Queue", callback_data="dl_queue"),
        InlineKeyboardButton("🗑️ Clear Queue", callback_data="dl_clear"),
    ])

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3750] + "\n... (truncated)"

    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def _show_download_queue(query_or_msg, queue: list):
    """Display the current download queue with action buttons."""
    if not queue:
        text = "📋 **Download / Archive Queue**\n\nQueue is empty.\nUse `/search_courses <query>` to find courses from your accounts."
        keyboard = []
    else:
        lines = ["📋 **Download / Archive Queue**\n"]
        keyboard = []
        for item in queue[:12]:  # show up to 12
            title = item["title"][:50]
            lines.append(f"• **{title}**")
            uid = item["udemy_course_id"]
            keyboard.append([
                InlineKeyboardButton(f"🚀 Archive “{title[:25]}”", callback_data=f"dl_start_{uid}"),
                InlineKeyboardButton("🗑️", callback_data=f"dl_remove_{uid}"),
            ])
        if len(queue) > 12:
            lines.append(f"... +{len(queue)-12} more")
        keyboard.append([
            InlineKeyboardButton("🚀 Archive ALL", callback_data="dl_start_all"),
            InlineKeyboardButton("🗑️ Clear All", callback_data="dl_clear"),
        ])
        text = "\n".join(lines)

    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    try:
        if hasattr(query_or_msg, "edit_message_text"):
            await query_or_msg.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        else:
            await query_or_msg.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        pass


def _build_udemy_cookies_file(work_dir, access_token, client_id):
    """Write a Netscape cookies.txt that yt-dlp understands for Udemy auth."""
    exp = str(int(__import__("datetime").datetime.utcnow().timestamp()) + 86400 * 30)
    lines = [
        "# Netscape HTTP Cookie File",
        f".udemy.com\tTRUE\t/\tTRUE\t{exp}\taccess_token\t{access_token}",
        f".udemy.com\tTRUE\t/\tTRUE\t{exp}\tclient_id\t{client_id}",
    ]
    cookie_file = work_dir / "udemy_cookies.txt"
    cookie_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cookie_file


def _get_course_id_from_url(course_url: str) -> str | None:
    """Extract Udemy course slug or numeric ID from a course URL."""
    import re
    # /course/draft/1234567/ or /course/slug-name/
    m = re.search(r"/course/(?:draft/)?([^/]+)", course_url)
    return m.group(1) if m else None


def _get_best_m3u8(master_m3u8_content: str, prefer_height: int = 720) -> str | None:
    """Parse an HLS master playlist and pick the best quality variant URL."""
    import re
    lines = master_m3u8_content.strip().split("\n")
    variants = []  # (bandwidth, resolution_height, url)
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
            url = lines[i + 1].strip()
            bw = int(re.search(r"BANDWIDTH=(\d+)", line).group(1)) if re.search(r"BANDWIDTH=(\d+)", line) else 0
            h = int(re.search(r"RESOLUTION=\d+x(\d+)", line).group(1)) if re.search(r"RESOLUTION=\d+x(\d+)", line) else 0
            variants.append((bw, h, url))
    if not variants:
        return None
    # Prefer the target height; if not found, pick highest
    preferred = [v for v in variants if v[1] == prefer_height]
    chosen = max(preferred or variants, key=lambda v: v[0])
    return chosen[2]


def _download_course_via_api(course_url: str, out_dir: Path, access_token: str, client_id: str, progress_callback=None):
    """
    Download all lectures of a Udemy course by:
    1. Using the Udemy subscriber-curriculum-items API to enumerate all chapters/lectures
    2. For each lecture, fetching the HLS stream URL via the lecture detail API
    3. Downloading each HLS stream with yt-dlp's generic HLS downloader (bypassing the
       Udemy extractor which 403s on the course webpage)

    Returns (ok: bool, errors: list[str]).
    Output structure: Chapter folder / lecture-index - title.mp4
    """
    import yt_dlp, re

    MOBILE_UA = "okhttp/4.10.0 UdemyAndroid 9.7.0(515) (phone)"
    session = requests.Session()
    session.cookies.update({"access_token": access_token, "client_id": client_id})
    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Accept": "application/json",
        "Referer": "https://www.udemy.com/",
    })

    # Resolve course ID
    slug_or_id = _get_course_id_from_url(course_url)
    if not slug_or_id:
        return False, ["Cannot extract course identifier from URL"]

    # Try to get numeric course_id from search or by fetching the course info
    r_course = session.get(
        f"https://www.udemy.com/api-2.0/courses/{slug_or_id}/?fields[course]=id,title",
        timeout=20
    )
    if r_course.status_code != 200:
        return False, [f"Cannot fetch course info: HTTP {r_course.status_code}"]
    course_data = r_course.json()
    course_id = course_data.get("id")
    course_title = course_data.get("title", slug_or_id)
    if not course_id:
        return False, ["Cannot find course_id"]

    if progress_callback:
        progress_callback(2, f"📚 Found course: **{course_title}**\nFetching curriculum...")

    # Get full curriculum
    all_items = []
    next_page = (
        f"https://www.udemy.com/api-2.0/courses/{course_id}/subscriber-curriculum-items/"
        f"?page_size=100"
        f"&fields[lecture]=id,title,object_index,asset"
        f"&fields[chapter]=id,title,object_index"
        f"&fields[asset]=id,asset_type,filename,media_sources,is_downloadable"
    )
    while next_page:
        r = session.get(next_page, timeout=20)
        if r.status_code != 200:
            break
        data = r.json()
        all_items.extend(data.get("results", []))
        next_page = data.get("next")

    chapters = {i["id"]: i for i in all_items if i.get("_class") == "chapter"}
    lectures = [i for i in all_items if i.get("_class") == "lecture"]

    if not lectures:
        return False, ["No lectures found in curriculum"]

    if progress_callback:
        progress_callback(4, f"📋 {len(lectures)} lectures in {len(chapters)} chapters. Downloading...")

    errors = []
    lecture_counter = [0]
    total_lectures = len(lectures)

    # Find chapter for each lecture (by order in curriculum)
    chapter_idx = [0]
    chapter_num = [0]
    current_chapter = [{"object_index": 0, "title": "Uncategorized"}]

    for item in all_items:
        if item.get("_class") == "chapter":
            current_chapter[0] = item
            chapter_num[0] += 1

    # We'll iterate all_items in order to track chapter context
    chap_num_counter = [0]
    current_chap = [{"title": "Uncategorized", "object_index": 0}]

    for item in all_items:
        if item.get("_class") == "chapter":
            chap_num_counter[0] += 1
            current_chap[0] = item
            continue
        if item.get("_class") != "lecture":
            continue

        lec = item
        lid = lec["id"]
        ltitle = lec.get("title", f"Lecture {lid}")
        lindex = lec.get("object_index", lecture_counter[0] + 1)
        chap = current_chap[0]
        chap_num = chap_num_counter[0]
        chap_title = chap.get("title", "Uncategorized")

        lecture_counter[0] += 1
        safe_chap = f"{chap_num:02d} - " + "".join(c if c.isalnum() or c in " -_." else "_" for c in chap_title)[:50]
        safe_lec = f"{lindex:03d} - " + "".join(c if c.isalnum() or c in " -_." else "_" for c in ltitle)[:55]
        lec_dir = out_dir / safe_chap
        lec_dir.mkdir(parents=True, exist_ok=True)
        out_path = lec_dir / f"{safe_lec}.mp4"

        if out_path.exists():
            continue  # already downloaded

        # Get lecture detail for stream URL
        detail_url = (
            f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/lectures/{lid}/"
            f"?fields[lecture]=@all&fields[asset]=@all"
        )
        try:
            r_lec = session.get(detail_url, timeout=20)
            if r_lec.status_code != 200:
                errors.append(f"Lecture {lid} API error {r_lec.status_code}")
                continue

            asset = r_lec.json().get("asset") or {}
            atype = asset.get("asset_type", "")
            ms = asset.get("media_sources") or []

            if atype == "Article":
                # Save article text as .html
                body = asset.get("body") or lec.get("description") or ""
                html_path = lec_dir / f"{safe_lec}.html"
                if body:
                    html_path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
                continue

            if atype in ("SourceCode", "File"):
                # Save external URL as reference
                ext_url = asset.get("external_url") or asset.get("source_url") or ""
                if ext_url:
                    ref_path = lec_dir / f"{safe_lec}_url.txt"
                    ref_path.write_text(ext_url, encoding="utf-8")
                continue

            if not ms:
                errors.append(f"No media for: {ltitle}")
                continue

            if asset.get("asset_is_drmed") or asset.get("course_is_drmed"):
                errors.append(f"DRM protected (skipped): {ltitle}")
                continue

            # Get M3U8 master URL
            m3u8_master_url = next(
                (m["src"] for m in ms if "mpegURL" in m.get("type", "") or m.get("src","").endswith(".m3u8")),
                None
            )
            if not m3u8_master_url:
                errors.append(f"No HLS source for: {ltitle}")
                continue

            # Fetch master M3U8 to find quality variants
            r_m3u8 = session.get(m3u8_master_url, timeout=20)
            if r_m3u8.status_code != 200:
                errors.append(f"M3U8 fetch error {r_m3u8.status_code} for: {ltitle}")
                continue

            # Pick 720p variant (or best available)
            variant_url = _get_best_m3u8(r_m3u8.text, prefer_height=720)
            if not variant_url:
                # No variants - M3U8 might be a segment list directly
                variant_url = m3u8_master_url

            # Compute progress
            pct_base = int((lecture_counter[0] / total_lectures) * 85)

            last_pct_lec = [0]
            def _hook(d, _chap=safe_chap, _lec=safe_lec, _pb=pct_base):
                if progress_callback is None:
                    return
                status = d.get("status", "")
                if status == "downloading":
                    try:
                        p = float((d.get("_percent_str") or "0").replace("%","").strip())
                    except Exception:
                        p = last_pct_lec[0]
                    last_pct_lec[0] = p
                    speed = d.get("_speed_str") or ""
                    eta   = d.get("_eta_str") or ""
                    dl    = d.get("_downloaded_bytes_str") or ""
                    total = d.get("_total_bytes_str") or d.get("_total_bytes_estimate_str") or ""
                    stage = (
                        f"📚 Lecture {lecture_counter[0]}/{total_lectures}\n"
                        f"📄 `{_lec[:45]}`"
                    )
                    if dl and total:
                        stage += f"\n📦 {dl} / {total}"
                    if speed:
                        stage += f"\n⚡ {speed}"
                    if eta:
                        stage += f"\n⏱ ETA {eta}"
                    progress_callback(_pb + int(p * 0.12), stage)
                elif status == "finished":
                    progress_callback(_pb + 12, f"🔀 Merging: `{_lec[:45]}`")

            ydl_opts = {
                "outtmpl": str(out_path),
                "cookiefile": None,  # not needed - segments don't require auth
                "http_headers": {
                    "User-Agent": MOBILE_UA,
                    "Referer": "https://www.udemy.com/",
                    "Cookie": f"access_token={access_token}; client_id={client_id}",
                },
                "format": "best",
                "merge_output_format": "mp4",
                "force_generic_extractor": True,
                "ignoreerrors": True,
                "nooverwrites": True,
                "retries": 3,
                "fragment_retries": 5,
                "concurrent_fragment_downloads": 4,
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [_hook],
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([variant_url])

        except Exception as e:
            errors.append(f"Error on {ltitle}: {e}")

    return True, errors


async def _start_course_archive(update, context, item, silent_start=False):
    """
    Background task:
    1. Download entire Udemy course via yt-dlp (cookie auth, chapter/lecture folder structure)
    2. ZIP the downloaded tree
    3. Split if > 2 GB (very rare)
    4. Upload via Pyrogram (~2 GB support) or Bot API fallback
    5. Progress messages sent to owner throughout
    """
    from user_enroller import OWNER_ID as _OWNER_ID
    owner_id = update.effective_user.id if update.effective_user else _OWNER_ID
    udemy_id = item["udemy_course_id"]
    title = item.get("title", f"Course-{udemy_id}")
    course_url = item.get("course_url", "")
    source_acc_id = item.get("source_account_id")

    accounts = get_user_accounts(owner_id)
    chosen = None
    if source_acc_id:
        chosen = next((a for a in accounts if a["id"] == source_acc_id), None)
    if not chosen and accounts:
        chosen = accounts[0]

    if not chosen or not course_url:
        if not silent_start:
            try:
                await context.bot.send_message(owner_id, f"\u274c Cannot archive \u201c{title}\u201d: no linked account or missing URL.")
            except Exception:
                pass
        return

    safe_name = "".join(c if c.isalnum() or c in " -_." else "_" for c in title)[:60].strip("_").strip()
    work_dir = Path(tempfile.mkdtemp(prefix="udemy_"))
    out_dir = work_dir / safe_name
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = work_dir / f"{safe_name}.zip"

    progress_msg = None
    _last_edit = [0.0]

    async def _send_progress(pct, stage):
        nonlocal progress_msg
        import time
        now = time.time()
        if progress_msg and (now - _last_edit[0]) < 5:
            return
        _last_edit[0] = now
        bar = "\u2588" * (pct // 10) + "\u2591" * (10 - pct // 10)
        txt = f"\U0001f4e5 **Downloading course**\n\n**{title}**\n\n[{bar}] {pct}%\n\n{stage}"
        try:
            if progress_msg:
                await progress_msg.edit_text(txt, parse_mode="Markdown")
            else:
                progress_msg = await context.bot.send_message(owner_id, txt, parse_mode="Markdown")
        except Exception:
            pass

    def _sync_progress(pct, stage):
        asyncio.create_task(_send_progress(pct, stage))

    try:
        if not silent_start:
            await _send_progress(1, f"\U0001f510 Preparing cookies for: **{chosen.get('name')}**")

        await _send_progress(3, "\U0001f680 Fetching curriculum and downloading lectures...")

        ok, errs = await asyncio.to_thread(
            _download_course_via_api,
            course_url,
            out_dir,
            chosen["access_token"],
            chosen.get("client_id") or DEFAULT_CLIENT_ID,
            _sync_progress,
        )

        if errs:
            err_summary = "\n".join(errs[:5])
            if len(errs) > 5:
                err_summary += f"\n... +{len(errs)-5} more"
            try:
                await context.bot.send_message(
                    owner_id,
                    f"\u26a0\ufe0f Some lectures skipped ({len(errs)} issues):\n`{err_summary[:400]}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        all_files = [f for f in out_dir.rglob("*") if f.is_file()]
        video_files = [f for f in all_files if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".m4v", ".avi")]

        if not all_files:
            await context.bot.send_message(
                owner_id,
                f"\u274c **No files downloaded** for \u201c{title}\u201d.\n\n"
                "Possible reasons:\n"
                "\u2022 Course URL is /draft/ (Udemy DRM — cannot download)\n"
                "\u2022 Token expired — update via /enroll_setup\n"
                "\u2022 Course uses Widevine DRM (not extractable by yt-dlp)"
            )
            return

        await _send_progress(90, f"\u2705 {len(video_files)} video(s) downloaded. Creating ZIP...")

        def _zip_tree(src, dst):
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
                for root, _, files in os.walk(src):
                    for fname in sorted(files):
                        full = Path(root) / fname
                        zf.write(full, full.relative_to(src))

        await asyncio.to_thread(_zip_tree, out_dir, zip_path)
        size_mb = zip_path.stat().st_size // (1024 * 1024)
        await _send_progress(95, f"\U0001f4e6 ZIP ready: {size_mb} MB. Uploading...")

        MAX_PART = 2000 * 1024 * 1024
        if zip_path.stat().st_size <= MAX_PART:
            part_files = [zip_path]
        else:
            part_files = await asyncio.to_thread(_split_file_into_parts, zip_path, work_dir, MAX_PART)

        raw_target = CHANNEL_ID if CHANNEL_ID else None

        for i, p in enumerate(part_files, 1):
            part_mb = p.stat().st_size // (1024 * 1024)
            part_label = f"Part {i}/{len(part_files)} \u2022 " if len(part_files) > 1 else ""
            caption = (
                f"\U0001f4da **{title}**\n"
                f"{part_label}{part_mb} MB\n"
                f"\U0001f4c2 {len(video_files)} lecture(s)\n"
                f"\U0001f517 udemy.com{course_url.split('/learn')[0]}"
            )
            if len(part_files) > 1:
                caption += "\n\n\U0001f4a1 Join: `cat *.part*.zip > full.zip`"

            # Convert channel ID to int if possible (Pyrogram requires numeric peer)
            target = raw_target
            if target and isinstance(target, str):
                try:
                    target = int(target)
                except ValueError:
                    pass  # keep as @username string — Pyrogram resolves it
            if not target:
                target = owner_id

            upload_start = [datetime.utcnow()]
            upload_last_edit = [0.0]

            async def _upload_progress_cb(current, total):
                import time
                now = time.time()
                if now - upload_last_edit[0] < 5:
                    return
                upload_last_edit[0] = now
                elapsed = max((datetime.utcnow() - upload_start[0]).total_seconds(), 0.1)
                speed_bps = current / elapsed
                if speed_bps > 1_048_576:
                    speed_str = f"{speed_bps / 1_048_576:.1f} MB/s"
                elif speed_bps > 1024:
                    speed_str = f"{speed_bps / 1024:.0f} KB/s"
                else:
                    speed_str = f"{speed_bps:.0f} B/s"

                remaining = max(total - current, 0)
                eta_secs = int(remaining / speed_bps) if speed_bps > 0 else 0
                eta_str = f"{eta_secs // 60}m {eta_secs % 60}s" if eta_secs > 60 else f"{eta_secs}s"
                uploaded_mb = current / 1_048_576
                total_mb = total / 1_048_576
                pct_ul = int(current / total * 100) if total else 0
                bar = "█" * (pct_ul // 10) + "░" * (10 - pct_ul // 10)

                stage = (
                    f"⬆️ Uploading part {i}/{len(part_files)}\n"
                    f"[{bar}] {pct_ul}%\n"
                    f"📦 {uploaded_mb:.1f} / {total_mb:.1f} MB\n"
                    f"⚡ {speed_str}   ⏱ ETA {eta_str}"
                )
                asyncio.create_task(_send_progress(95 + int(i * 4 / len(part_files)), stage))

            upload_ok = await _upload_file_to_chat(target, p, caption, p.name, progress_cb=_upload_progress_cb)
            if not upload_ok:
                try:
                    with open(p, "rb") as f:
                        await context.bot.send_document(
                            chat_id=owner_id,
                            document=f,
                            filename=p.name,
                            caption=caption[:1020],
                            parse_mode="Markdown",
                        )
                except Exception as e2:
                    log.error(f"All upload methods failed for {p.name}: {e2}")

        remove_from_download_queue(owner_id, udemy_id)
        done_txt = (
            f"\u2705 **Archive complete!**\n\n"
            f"\U0001f4da **{title}**\n"
            f"\U0001f4c2 {len(video_files)} lecture(s) \u2022 {size_mb} MB\n"
            f"\U0001f4e6 {len(part_files)} ZIP part(s) uploaded\n\n"
            "Item removed from queue."
        )
        try:
            if progress_msg:
                await progress_msg.edit_text(done_txt, parse_mode="Markdown")
            else:
                await context.bot.send_message(owner_id, done_txt, parse_mode="Markdown")
        except Exception:
            pass

    except Exception as e:
        log.exception(f"Archive failed for {title}: {e}")
        try:
            await context.bot.send_message(owner_id, f"\u274c Archive failed for \u201c{title}\u201d:\n`{str(e)[:300]}`", parse_mode="Markdown")
        except Exception:
            pass
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

def _split_file_into_parts(src: Path, work_dir: Path, max_bytes: int) -> list:
    """Split a large file into sequentially named .partXX.zip files."""
    parts = []
    part_num = 1
    with open(src, "rb") as f:
        while True:
            chunk = f.read(max_bytes)
            if not chunk:
                break
            part_name = work_dir / f"{src.stem}.part{part_num:02d}{src.suffix}"
            with open(part_name, "wb") as pf:
                pf.write(chunk)
            parts.append(part_name)
            part_num += 1
    return parts


async def _upload_file_to_chat(chat_id, file_path: Path, caption: str, filename=None, progress_cb=None) -> bool:
    """
    Upload a (potentially large) file to a chat.
    - If API_ID + API_HASH configured → Pyrogram (MTProto, up to ~2GB, with upload progress).
    - Otherwise → Bot API fallback (limited to ~50MB).
    Returns True on success.
    progress_cb: async callable(current_bytes, total_bytes) for upload progress.
    """
    size_mb = file_path.stat().st_size // (1024 * 1024)

    # Normalise chat_id: Pyrogram requires int for numeric ids, or @username str
    if isinstance(chat_id, str):
        try:
            chat_id = int(chat_id)
        except ValueError:
            pass  # keep as @username

    if API_ID and API_HASH:
        try:
            from pyrogram import Client
            session_name = f"arc_{int(datetime.utcnow().timestamp())}"
            async with Client(
                session_name,
                api_id=int(API_ID),
                api_hash=API_HASH,
                bot_token=os.getenv("BOT_TOKEN"),
                in_memory=True,
            ) as client:
                await client.send_document(
                    chat_id=chat_id,
                    document=str(file_path),
                    caption=caption[:1020] if caption else None,
                    file_name=filename,
                    force_document=True,
                    progress=progress_cb,   # Pyrogram calls this with (current, total)
                )
            log.info(f"Pyrogram upload OK: {file_path.name} ({size_mb} MB)")
            return True
        except Exception as e:
            log.error(f"Pyrogram upload failed for {file_path.name}: {e}")
            # fall through

    # Fallback: regular python-telegram-bot (will fail gracefully if >~50MB)
    try:
        bot_token = os.getenv("BOT_TOKEN")
        if bot_token:
            ptb_bot = Bot(token=bot_token)
            with open(file_path, "rb") as f:
                await ptb_bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=filename or file_path.name,
                    caption=caption[:1020] if caption else None,
                    parse_mode="Markdown"
                )
            return True
    except Exception as e:
        log.error(f"Bot API fallback upload failed for {file_path.name}: {e}")

    return False


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show enrollment stats for all users (owner only)"""
    if not update.effective_user or not update.effective_message:
        return
    
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Owner only command.")
        return
    
    stats = get_all_daily_stats()
    
    lines = [
        "📊 **Enrollment Statistics**\n",
        f"📅 Date: {stats['date']}",
        f"✅ Today Total: **{stats['today_total']}** courses",
        f"📈 All-Time Total: **{stats['all_time_total']}** courses",
        "",
        "👥 **Today's Enrollments by User:**"
    ]
    
    if stats['users']:
        for i, u in enumerate(stats['users'][:20], 1):  # Top 20
            user_type = "👑" if is_owner(u['user_id']) else ("💎" if is_premium(u['user_id']) else "👤")
            lines.append(f"{i}. {user_type} `{u['user_id']}`: {u['count']} courses")
        
        if len(stats['users']) > 20:
            lines.append(f"... and {len(stats['users']) - 20} more users")
    else:
        lines.append("No enrollments today yet.")
    
    lines.append("\n_Legend: 👑=Owner 💎=Premium 👤=Free_")
    
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Setup Commands ──────────────────────────────────────────────────────────

async def cmd_enroll_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start adding a new account"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    if accounts:
        account_list = "\n".join(
            f"  {'✅' if a['auto_enroll'] else '⭕'} {a['name']} (ID: {a['id']})"
            for a in accounts
        )
        keyboard = [[
            InlineKeyboardButton("➕ Add Account", callback_data="setup_add_new"),
        ], [
            InlineKeyboardButton("🗑️ Remove Account", callback_data="setup_remove"),
            InlineKeyboardButton("✅ Done", callback_data="setup_done"),
        ]]
        await update.effective_message.reply_text(
            f"🎓 **Your Udemy Accounts:**\n\n{account_list}\n\n"
            "What would you like to do?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        set_user_setup_state(user_id, "waiting_token_new", "Account 1")
        await update.effective_message.reply_text(
            f"🎓 **Udemy Auto-Enroller Setup**\n\n"
            f"{get_setup_instructions()}\n"
            "📝 Send me your `access_token` now:",
            parse_mode="Markdown"
        )


async def cmd_set_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set access token via command - now completes setup directly (no client_id needed)"""
    if not update.effective_user or not update.effective_message or not context.args:
        await update.effective_message.reply_text("Usage: `/set_token <your_token>`", parse_mode="Markdown")
        return
    
    user_id = update.effective_user.id
    token = " ".join(context.args)
    
    if not validate_token_format(token):
        await update.effective_message.reply_text("❌ Token too short (need 20+ chars)")
        return
    
    # Show verifying message
    verify_msg = await update.effective_message.reply_text("🔄 Verifying token...")
    
    # Try to get actual Udemy username and user ID
    udemy_user_id = None
    try:
        enroller = UdemyAutoEnroller(token)
        is_valid = await asyncio.to_thread(enroller.verify_login)
        if not is_valid:
            await verify_msg.edit_text("❌ Invalid token or Udemy login failed.")
            return
        
        udemy_info = await asyncio.to_thread(enroller.get_user_info)
        if udemy_info:
            udemy_user_id = udemy_info.get("id")
            name = udemy_info.get("name") or "Udemy Account"
        else:
            name = "Udemy Account"
    except Exception:
        name = "Udemy Account"
    
    # Check if the account already exists for this user
    existing_acc = find_existing_account(user_id, token, udemy_user_id)
    if existing_acc:
        # Update existing account's token and name
        update_account_token(existing_acc["id"], token, name)
        clear_user_setup_state(user_id)
        await verify_msg.edit_text(
            f"🔄 **Account Updated!**\n\n"
            f"Your Udemy account **{name}** was already registered. I have successfully updated its access token to the new one you provided! ✅\n\n"
            f"🚀 Auto-enrollment is active!\n\n"
            f"📊 `/enroll_status` — View your stats",
            parse_mode="Markdown"
        )
    else:
        # Add as new account
        acc_id = add_account(user_id, name, token, None, udemy_user_id)
        clear_user_setup_state(user_id)
        await verify_msg.edit_text(
            f"🎉 **Setup Complete!**\n\n"
            f"✅ **{name}** added successfully\n"
            f"🚀 Auto-enrollment STARTED!\n\n"
            f"The bot will now automatically enroll you in free courses every 2 minutes.\n"
            f"You'll receive notifications when courses are enrolled.\n\n"
            f"📊 `/enroll_status` — View your stats",
            parse_mode="Markdown"
        )


# ─── Message Handler for Interactive Setup ───────────────────────────────────

async def handle_setup_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle raw message input during setup"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    step, extra = get_user_setup_state(user_id)
    
    if not step:
        return
    
    text = update.effective_message.text.strip()
    
    if step == "waiting_token_new":
        if not validate_token_format(text):
            await update.effective_message.reply_text("❌ Token too short. Copy the full `access_token` cookie value.")
            return
        
        # Show verifying message
        verify_msg = await update.effective_message.reply_text("🔄 Verifying token...")
        
        # Try to get actual Udemy username and user ID
        udemy_user_id = None
        try:
            enroller = UdemyAutoEnroller(text)  # Uses default client_id
            
            # Verify login first
            is_valid = await asyncio.to_thread(enroller.verify_login)
            if not is_valid:
                await verify_msg.edit_text("❌ Invalid token or Udemy login failed. Please check and try again.")
                return
            
            # Get actual Udemy username and ID
            udemy_info = await asyncio.to_thread(enroller.get_user_info)
            if udemy_info:
                udemy_user_id = udemy_info.get("id")
                name = udemy_info.get("name") or extra or "Udemy Account"
            else:
                name = extra or "Udemy Account"
        except Exception as e:
            log.error(f"Token verification error: {e}")
            name = extra or "Udemy Account"
        
        # Check if the account already exists for this user
        existing_acc = find_existing_account(user_id, text, udemy_user_id)
        if existing_acc:
            # Update existing account's token and name
            update_account_token(existing_acc["id"], text, name)
            clear_user_setup_state(user_id)
            await verify_msg.edit_text(
                f"🔄 **Account Updated!**\n\n"
                f"Your Udemy account **{name}** was already registered. I have successfully updated its access token to the new one you provided! ✅\n\n"
                f"🚀 Auto-enrollment is active!\n\n"
                f"📊 `/enroll_status` — View your stats",
                parse_mode="Markdown"
            )
        else:
            # Add account with actual name and udemy_user_id
            acc_id = add_account(user_id, name, text, None, udemy_user_id)
            clear_user_setup_state(user_id)
            
            await verify_msg.edit_text(
                f"🎉 **Setup Complete!**\n\n"
                f"✅ **{name}** added successfully\n"
                f"🚀 Auto-enrollment STARTED!\n\n"
                f"The bot will now automatically enroll you in free courses every 2 minutes.\n"
                f"You'll receive notifications when courses are enrolled.\n\n"
                f"📊 `/enroll_status` — View your stats",
                parse_mode="Markdown"
            )


# ─── Account Management ──────────────────────────────────────────────────────

async def _refresh_accounts_view(query) -> None:
    """Refresh accounts view - runs as independent background task"""
    try:
        user_id = query.from_user.id
        
        # Get accounts from DB (with timeout)
        try:
            accounts = await asyncio.wait_for(
                asyncio.to_thread(get_user_accounts, user_id),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            await query.message.reply_text("⏱️ Database timeout. Try again.")
            return
        except Exception as e:
            await query.message.reply_text(f"❌ Error: {str(e)[:50]}")
            return
        
        if not accounts:
            await query.message.reply_text("No accounts. Run `/enroll_setup`.", parse_mode="Markdown")
            return
        
        # Send new message immediately with "loading" for course counts
        lines = ["🎓 **Your Udemy Accounts:**\n"]
        keyboard = []
        
        for a in accounts:
            auto = "🟢 Auto" if a["auto_enroll"] else "🔴 Manual"
            lines.append(f"**{a['name']}** — {auto}\n   ⏳ Loading...")
            keyboard.append([
                InlineKeyboardButton(
                    f"{'🔴 Disable' if a['auto_enroll'] else '🟢 Enable'} Auto - {a['name']}",
                    callback_data=f"acc_toggle_{a['id']}"
                ),
                InlineKeyboardButton(f"🗑️ Remove", callback_data=f"acc_remove_{a['id']}"),
            ])
        
        keyboard.append([InlineKeyboardButton("➕ Add Account", callback_data="setup_add_new")])
        keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="show_accounts")])
        
        # Send NEW message (doesn't interfere with enrollment)
        msg = await query.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        
        # Fetch course counts in PARALLEL for all accounts
        async def get_course_count(acc):
            try:
                enroller = UdemyAutoEnroller(acc["access_token"], acc["client_id"])
                count = await asyncio.to_thread(enroller.get_total_courses_count)
                return acc["id"], count
            except Exception:
                return acc["id"], -1
        
        tasks = [get_course_count(a) for a in accounts]
        results = await asyncio.gather(*tasks)
        counts = {acc_id: count for acc_id, count in results}
        
        # Update message with actual counts
        lines = ["🎓 **Your Udemy Accounts:**\n"]
        for a in accounts:
            auto = "🟢 Auto" if a["auto_enroll"] else "🔴 Manual"
            count = counts.get(a["id"], -1)
            count_str = f"📚 {count} courses" if count >= 0 else "⚠️ Token expired"
            lines.append(f"**{a['name']}** — {auto}\n   {count_str}")
        
        await msg.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"Refresh accounts error: {e}")


async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show and manage accounts with course counts"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    
    # Try to get accounts with timeout
    try:
        accounts = await asyncio.wait_for(
            asyncio.to_thread(get_user_accounts, user_id),
            timeout=15.0
        )
    except asyncio.TimeoutError:
        await update.effective_message.reply_text(
            "⏱️ Database connection timed out. Please try again.",
            parse_mode="Markdown"
        )
        return
    except Exception as e:
        log.error(f"Error getting accounts: {e}")
        await update.effective_message.reply_text(
            f"❌ Database error: {str(e)[:100]}\nPlease try again later.",
            parse_mode="Markdown"
        )
        return
    
    if not accounts:
        await update.effective_message.reply_text(
            "No accounts set up.\nRun `/enroll_setup` to add one.",
            parse_mode="Markdown"
        )
        return
    
    # Show loading message while fetching course counts
    msg = await update.effective_message.reply_text("🔄 Loading accounts...")
    
    # Fetch ALL course counts in PARALLEL
    async def get_course_count(acc):
        try:
            enroller = UdemyAutoEnroller(acc["access_token"], acc["client_id"])
            count = await asyncio.to_thread(enroller.get_total_courses_count)
            return acc["id"], count
        except Exception:
            return acc["id"], -1
    
    tasks = [get_course_count(a) for a in accounts]
    results = await asyncio.gather(*tasks)
    counts = {acc_id: count for acc_id, count in results}
    
    lines = ["🎓 **Your Udemy Accounts:**\n"]
    keyboard = []
    
    for a in accounts:
        auto = "🟢 Auto" if a["auto_enroll"] else "🔴 Manual"
        count = counts.get(a["id"], -1)
        count_str = f"📚 {count} courses" if count >= 0 else "⚠️ Token expired"
        
        lines.append(f"**{a['name']}** — {auto}\n   {count_str}")
        keyboard.append([
            InlineKeyboardButton(
                f"{'🔴 Disable' if a['auto_enroll'] else '🟢 Enable'} Auto - {a['name']}",
                callback_data=f"acc_toggle_{a['id']}"
            ),
            InlineKeyboardButton(f"🗑️ Remove", callback_data=f"acc_remove_{a['id']}"),
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Add Account", callback_data="setup_add_new")])
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="show_accounts")])
    
    await msg.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── Auto-Enroll Toggle ─────────────────────────────────────────────────────

async def cmd_autoenroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle auto-enrollment on/off (owner only, regular users have it always ON)"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    
    # Regular users don't need this - auto-enroll is always on for them
    if not is_owner(user_id):
        await update.effective_message.reply_text(
            "✅ Auto-enrollment is **always active** for you!\n\n"
            "The bot checks for new free courses every 2 minutes and enrolls you automatically.\n\n"
            "📊 Use `/enroll_status` to see your stats.",
            parse_mode="Markdown"
        )
        return
    
    accounts = get_user_accounts(user_id)
    
    if not accounts:
        await update.effective_message.reply_text("No accounts. Run `/enroll_setup` first.", parse_mode="Markdown")
        return
    
    state = get_auto_enroll_state(user_id)
    current = state["enabled"]
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔴 Turn OFF" if current else "🟢 Turn ON",
            callback_data="autoenroll_toggle"
        ),
    ]])
    
    status = "🟢 **ACTIVE**" if current else "🔴 **INACTIVE**"
    auto_accounts = [a for a in accounts if a["auto_enroll"]]
    
    await update.effective_message.reply_text(
        f"⚡ **Auto-Enrollment Status:** {status}\n\n"
        f"Accounts with auto-enroll: {len(auto_accounts)}/{len(accounts)}\n"
        f"Check interval: Every 2 minutes\n"
        f"Total auto-enrolled: {state['total']}\n"
        f"Last check: {state['last_check'] or 'Never'}\n\n"
        "When active, I check for new free courses every 10 minutes "
        "and auto-enroll your accounts. You get notified when new courses are enrolled.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ─── Enroll Command ──────────────────────────────────────────────────────────

async def cmd_enroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual enroll - fetch courses and enroll in selected accounts"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    if not accounts:
        await update.effective_message.reply_text(
            "🔒 No accounts set up.\nRun `/enroll_setup` to add your Udemy cookies.",
            parse_mode="Markdown"
        )
        return
    
    # Check daily limit for free users
    can_do, remaining, user_is_premium = can_enroll(user_id)
    if not can_do:
        from user_enroller import OWNER_ID
        owner_link = f"tg://user?id={OWNER_ID}" if OWNER_ID else ""
        await update.effective_message.reply_html(
            f"⚠️ <b>Daily limit reached!</b>\n\n"
            f"Free users can enroll in {FREE_DAILY_LIMIT} courses/day.\n"
            f"Your limit resets at midnight.\n\n"
            f"💎 <b>Want unlimited enrollments?</b>\n"
            f"Contact the owner for premium access:\n"
            f"👉 <a href=\"{owner_link}\">Click here to message owner</a>\n"
            f"🆔 Owner ID: <code>{OWNER_ID}</code>"
        )
        return
    
    # Build limit info
    if user_is_premium:
        limit_info = "💎 Premium: Unlimited"
    else:
        limit_info = f"📊 Today: {remaining}/{FREE_DAILY_LIMIT} remaining"
    
    if len(accounts) == 1:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🚀 Enroll — {accounts[0]['name']}", callback_data="enroll_start_all"),
            InlineKeyboardButton("❌ Cancel", callback_data="enroll_cancel"),
        ]])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🚀 Enroll ALL ({len(accounts)} accounts)", callback_data="enroll_start_all")],
            [InlineKeyboardButton(f"📌 Latest account only ({accounts[-1]['name']})", callback_data=f"enroll_start_{accounts[-1]['id']}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="enroll_cancel")],
        ])
    
    await update.effective_message.reply_text(
        "🎓 **Udemy Auto-Enroller**\n\n"
        f"Accounts: {len(accounts)}\n"
        f"{limit_info}\n"
        "Action: Fetch latest 50 free courses & enroll\n"
        "Source: Real.Discount API\n"
        "Time: ~2-4 min per account\n\n"
        "Choose enrollment target:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def cmd_enroll_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show enrollment stats"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    stats = get_user_stats(user_id)
    state = get_auto_enroll_state(user_id)
    recent = get_recently_enrolled(user_id, 5)
    
    # Get daily stats
    today_count = get_daily_usage(user_id)
    total_all_time = get_user_total_enrollments(user_id)
    user_is_premium = is_premium(user_id)
    
    lines = [
        "📊 **Enrollment Stats**\n",
        f"Accounts: {stats['total_accounts']}",
        f"Auto-enroll: {'🟢 ON' if state['enabled'] else '🔴 OFF'}",
    ]
    
    # Show daily limit info
    if user_is_premium:
        lines.append(f"✅ Today: {today_count} courses (💎 Unlimited)")
    else:
        remaining = max(0, FREE_DAILY_LIMIT - today_count)
        lines.append(f"✅ Today: {today_count}/{FREE_DAILY_LIMIT} ({remaining} remaining)")
    
    lines.append(f"📈 All-time: {total_all_time} courses")
    lines.append(f"Last check: {state['last_check'] or 'Never'}")
    
    if recent:
        lines.append("\n**Recent Enrollments:**")
        for r in recent:
            short = r['title'][:40] + "..." if len(r['title']) > 40 else r['title']
            lines.append(f"• {short}")
    
    lines.append("\n`/enroll` — Enroll now\n`/autoenroll` — Toggle auto-enroll")
    
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_myprofile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show profile"""
    if not update.effective_user or not update.effective_message:
        return
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    state = get_auto_enroll_state(user_id)
    
    lines = [
        f"👤 **Profile** — `{user_id}`\n",
        f"Accounts: {len(accounts)}",
        f"Auto-enroll: {'🟢' if state['enabled'] else '🔴'}",
        f"Total enrolled: {state['total']}",
    ]
    
    keyboard = [[
        InlineKeyboardButton("⚙️ Accounts", callback_data="show_accounts"),
        InlineKeyboardButton("🗑️ Delete All Data", callback_data="clear_my_data"),
    ]]
    
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── Callbacks ───────────────────────────────────────────────────────────────

async def enroll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle enroll_ callbacks"""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "enroll_cancel":
        await query.edit_message_text("❌ Cancelled.")
    
    elif data == "enroll_start_all":
        await query.answer("🚀 Starting...")
        await query.edit_message_text("🔄 **Fetching & enrolling...**\n\n⏳ Please wait...")
        asyncio.create_task(_run_enroll_accounts(update, context, account_filter=None))
    
    elif data.startswith("enroll_start_"):
        acc_id = int(data.replace("enroll_start_", ""))
        await query.answer("🚀 Starting...")
        await query.edit_message_text("🔄 **Fetching & enrolling...**\n\n⏳ Please wait...")
        asyncio.create_task(_run_enroll_accounts(update, context, account_filter=acc_id))
    
    elif data == "enroll_auto_start":
        await query.answer("🚀 Starting...")
        await query.edit_message_text("🔄 **Enrolling...**\n\n⏳ Please wait...")
        asyncio.create_task(_run_enroll_accounts(update, context, account_filter=None))
    
    elif data == "enroll_auto_skip":
        await query.edit_message_text("✅ Skipped. Run `/enroll` anytime.")


async def setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle setup_ callbacks"""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "setup_add_new":
        accounts = get_user_accounts(user_id)
        name = f"Account {len(accounts) + 1}"
        set_user_setup_state(user_id, "waiting_token_new", name)
        await query.edit_message_text(
            f"➕ **Adding {name}**\n\n"
            f"{get_setup_instructions()}\n"
            "📝 Send your `access_token` now:",
            parse_mode="Markdown"
        )
    
    elif data == "setup_remove":
        accounts = get_user_accounts(user_id)
        keyboard = [
            [InlineKeyboardButton(f"🗑️ {a['name']}", callback_data=f"acc_remove_{a['id']}")]
            for a in accounts
        ]
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="setup_done")])
        await query.edit_message_text(
            "Select account to remove:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "setup_done":
        await query.edit_message_text("✅ Done!")
    
    elif data == "setup_update_token":
        set_user_setup_state(user_id, "waiting_token_new")
        await query.edit_message_text("📝 Send your new `access_token`:")
    
    elif data == "setup_keep_current":
        await query.edit_message_text("✅ Keeping current setup.")


async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle profile/account callbacks"""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "start_setup":
        await cmd_enroll_setup(update, context)
    
    elif data.startswith("acc_toggle_"):
        acc_id = int(data.replace("acc_toggle_", ""))
        acc = get_account(acc_id)
        if acc and acc["user_id"] == user_id:
            new_state = not acc["auto_enroll"]
            toggle_auto_enroll(acc_id, new_state)
            status = "🟢 Enabled" if new_state else "🔴 Disabled"
            await query.answer(f"Auto-enroll {status} for {acc['name']}")
            # Refresh accounts view
            await query.edit_message_text(f"✅ {acc['name']}: Auto-enroll {status}\n\nUse `/accounts` to see all.", parse_mode="Markdown")
    
    elif data.startswith("acc_remove_"):
        acc_id = int(data.replace("acc_remove_", ""))
        acc = get_account(acc_id)
        if acc and acc["user_id"] == user_id:
            remove_account(acc_id)
            await query.edit_message_text(f"🗑️ {acc['name']} removed.\n\nUse `/accounts` to see remaining.")
    
    elif data == "autoenroll_toggle":
        state = get_auto_enroll_state(user_id)
        new_enabled = not state["enabled"]
        set_auto_enroll_enabled(user_id, new_enabled)
        status = "🟢 ACTIVATED" if new_enabled else "🔴 DEACTIVATED"
        await query.edit_message_text(
            f"⚡ Auto-enrollment: {status}\n\n"
            + ("I'll check for new courses every 10 min and enroll automatically." if new_enabled
               else "Auto-enrollment stopped. Use `/enroll` for manual enrollment."),
            parse_mode="Markdown"
        )
    
    elif data == "show_accounts":
        # Refresh accounts display - runs as independent task
        await query.answer("🔄 Refreshing...")
        asyncio.create_task(_refresh_accounts_view(query))
    
    elif data == "clear_my_data":
        keyboard = [[
            InlineKeyboardButton("✅ Yes, Delete All", callback_data="confirm_delete"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete"),
        ]]
        await query.edit_message_text(
            "⚠️ Delete ALL data (accounts, history)?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "confirm_delete":
        delete_user_data(user_id)
        await query.edit_message_text("✅ All data deleted. Run `/enroll_setup` to start fresh.")
    
    elif data == "cancel_delete":
        await query.edit_message_text("Cancelled.")
    
    elif data == "update_creds":
        await cmd_enroll_setup(update, context)
    
    elif data == "toggle_channel_post":
        # Owner only
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        
        new_state = toggle_channel_posting()
        status = "🟢 **ON**" if new_state else "🔴 **OFF**"
        channel_info = f"Channel: `{CHANNEL_ID}`" if CHANNEL_ID else "⚠️ CHANNEL_ID not set"
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔴 Turn OFF" if new_state else "🟢 Turn ON",
                callback_data="toggle_channel_post"
            ),
        ]])
        
        await query.edit_message_text(
            f"📢 **Channel Course Posting**\n\n"
            f"Status: {status}\n"
            f"{channel_info}\n\n"
            f"When ON, free Udemy courses are automatically posted to the channel.\n"
            f"When OFF, course posting to channel is paused.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await query.answer(f"Channel posting {'enabled' if new_state else 'disabled'}!")

    # ─── Owner Download / Archive callbacks ─────────────────────────────────
    elif data.startswith("dl_select_"):
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        try:
            udemy_id = int(data.replace("dl_select_", ""))
        except ValueError:
            await query.answer("Bad selection")
            return

        search_state = context.user_data.get("dl_search", {})
        results = search_state.get("results", [])
        match = next((r for r in results if r.get("id") == udemy_id), None)
        if not match:
            await query.answer("Selection expired. Search again with /search_courses")
            return

        title = match.get("title", "Course")
        url = match.get("url", "")
        src_acc = match.get("source_account_id")

        added = add_to_download_queue(user_id, udemy_id, title, url, src_acc)
        if added:
            await query.answer(f"✅ Added: {title[:40]}")
            # Show the queue immediately
            queue = get_owner_download_queue(user_id)
            await _show_download_queue(query, queue)
        else:
            await query.answer("Could not add (maybe duplicate?)")

    elif data in ("dl_next", "dl_prev"):
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        state = context.user_data.get("dl_search")
        if not state:
            await query.answer("Search session expired. Use /search_courses again.")
            return
        all_res = state.get("results", [])
        current_page = state.get("page", 1)
        new_page = current_page + 1 if data == "dl_next" else max(1, current_page - 1)
        state["page"] = new_page
        context.user_data["dl_search"] = state
        start = (new_page - 1) * 10
        page_results = all_res[start : start + 10]
        await _send_search_results(query.message, context, page_results, state.get("query", ""), new_page)

    elif data == "dl_queue":
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        queue = get_owner_download_queue(user_id)
        await _show_download_queue(query, queue)

    elif data == "dl_clear":
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        count = clear_owner_download_queue(user_id)
        await query.edit_message_text(f"🗑️ Cleared {count} items from download queue.")
        context.user_data.pop("dl_search", None)

    elif data.startswith("dl_remove_"):
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        try:
            uid = int(data.replace("dl_remove_", ""))
        except ValueError:
            return
        remove_from_download_queue(user_id, uid)
        queue = get_owner_download_queue(user_id)
        await _show_download_queue(query, queue)

    elif data.startswith("dl_start_"):
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        try:
            uid = int(data.replace("dl_start_", ""))
        except ValueError:
            return
        queue = get_owner_download_queue(user_id)
        item = next((q for q in queue if q["udemy_course_id"] == uid), None)
        if not item:
            await query.answer("Item not in queue anymore.")
            return
        # Start background download+zip+upload
        await query.answer("🚀 Starting download in background...")
        asyncio.create_task(_start_course_archive(update, context, item))

    elif data == "dl_start_all":
        if not is_owner(user_id):
            await query.answer("Owner only!", show_alert=True)
            return
        queue = get_owner_download_queue(user_id)
        if not queue:
            await query.answer("Queue is empty.")
            return
        await query.answer(f"🚀 Starting archive for {len(queue)} courses (one by one)...")
        # Process sequentially to avoid hammering disk/bandwidth
        for item in queue:
            await _start_course_archive(update, context, item, silent_start=True)


# ─── Core Enrollment Logic ───────────────────────────────────────────────────

def _fetch_courses_from_api(limit: int = 50) -> list:
    """Fetch latest free Udemy courses from real.discount API"""
    courses = []
    page = 1
    
    while len(courses) < limit:
        try:
            resp = requests.get(
                COURSES_API,
                params={"page": page, "limit": 50, "sortBy": "sale_start"},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if not items:
                break
            
            for item in items:
                try:
                    sale_price = float(item.get("sale_price", 0) or 0)
                except (ValueError, TypeError):
                    sale_price = 0
                
                if sale_price != 0 or "udemy.com" not in item.get("url", ""):
                    continue
                
                url = item.get("url", "")
                coupon = url.split("couponCode=")[1].split("&")[0] if "couponCode=" in url else None
                courses.append(Course(
                    title=item.get("name", "Untitled"),
                    url=url,
                    coupon_code=coupon,
                ))
                if len(courses) >= limit:
                    break
            
            page += 1
            if page > 5:
                break
        except Exception as e:
            log.error(f"API fetch error page {page}: {e}")
            break
    
    return courses


def _progress_bar(current: int, total: int, width: int = 15) -> str:
    if total == 0:
        return "░" * width
    pct = current / total
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {int(pct * 100)}%"


def _enroll_account_in_courses(account: dict, courses: list) -> dict:
    """Enroll a single account in the given courses. Returns results dict."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    enroller = UdemyAutoEnroller(
        access_token=account["access_token"],
        client_id=account["client_id"]
    )
    
    if not enroller.verify_login():
        return {"enrolled": [], "already": 0, "expired": 0, "failed": 0, "error": "Login failed"}
    
    enroller._get_enrolled_courses()
    log.info(f"Pre-fetched {len(enroller.enrolled_slugs)} enrolled courses")
    
    enrolled = []
    already = 0
    expired = 0
    failed = 0
    batch = []
    free_courses = []
    
    # Step 1: Filter out already enrolled courses (fast, no API call)
    courses_to_process = []
    for course in courses:
        slug = enroller._extract_slug(course.url)
        if not slug:
            failed += 1
            continue
        if slug in enroller.enrolled_slugs:
            already += 1
            continue
        courses_to_process.append((course, slug))
    
    # Step 2: Validate courses in PARALLEL (4 threads)
    def validate_course(course_slug_tuple):
        course, slug = course_slug_tuple
        coupon = course.coupon_code or enroller._extract_coupon(course.url)
        course_id, is_free = enroller._get_course_id_from_page(slug)
        if not course_id:
            return ("failed", course, None, None, None)
        if is_free:
            return ("free", course, course_id, None, None)
        if not coupon:
            return ("failed", course, None, None, None)
        if not enroller._check_coupon(course_id, coupon):
            return ("expired", course, None, None, None)
        return ("valid", course, course_id, coupon, course.title)
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(validate_course, cs): cs for cs in courses_to_process}
        for future in as_completed(futures):
            try:
                status, course, course_id, coupon, title = future.result()
                if status == "failed":
                    failed += 1
                elif status == "expired":
                    expired += 1
                elif status == "free":
                    free_courses.append((course, course_id))
                elif status == "valid":
                    batch.append((course_id, coupon, title))
            except Exception:
                failed += 1
    
    # Step 3: Enroll FREE courses in parallel
    def enroll_free(course_tuple):
        course, course_id = course_tuple
        return enroller._free_checkout(course_id), course.title
    
    if free_courses:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(enroll_free, free_courses))
            for result, title in results:
                if result == "enrolled":
                    enrolled.append(title)
                    log.info(f"Free enrolled: {title[:40]}")
                elif result == "already":
                    already += 1
                else:
                    failed += 1
    
    # Step 4: Bulk checkout coupon courses (batch of 10)
    if batch:
        log.info(f"Processing {len(batch)} coupon courses in batches of 10")
        while batch:
            chunk = batch[:10]
            batch = batch[10:]
            log.info(f"Processing chunk of {len(chunk)} courses")
            titles = enroller._bulk_checkout(chunk)
            enrolled.extend(titles)
            if titles:
                log.info(f"Chunk enrolled {len(titles)}: {titles}")
            failed += len(chunk) - len(titles)
    
    log.info(f"Result: enrolled={len(enrolled)}, already={already}, expired={expired}, failed={failed}")
    return {"enrolled": enrolled, "already": already, "expired": expired, "failed": failed, "error": None}


async def _run_enroll_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE, account_filter=None) -> None:
    """Fetch courses and enroll in selected accounts with progress"""
    try:
        user_id = update.effective_user.id
        msg = update.callback_query.message if update.callback_query else update.effective_message
        
        accounts = get_user_accounts(user_id)
        if account_filter:
            accounts = [a for a in accounts if a["id"] == account_filter]
        
        if not accounts:
            await msg.edit_text("❌ No accounts found.")
            return
        
        # Check daily limit for free users
        can_do, remaining, user_is_premium = can_enroll(user_id)
        if not can_do:
            from user_enroller import OWNER_ID
            owner_link = f"tg://user?id={OWNER_ID}" if OWNER_ID else ""
            await msg.edit_text(
                f"⚠️ <b>Daily limit reached!</b>\n\n"
                f"Free: {FREE_DAILY_LIMIT}/day\n\n"
                f"💎 <b>Want unlimited?</b>\n"
                f"<a href=\"{owner_link}\">Contact owner for premium</a>\n"
                f"🆔 <code>{OWNER_ID}</code>",
                parse_mode="HTML"
            )
            return
        
        # Determine how many courses to fetch
        if user_is_premium:
            courses_to_fetch = 50
            limit_info = "💎 Premium: Unlimited"
        else:
            courses_to_fetch = min(50, remaining)
            limit_info = f"📊 Limit: {remaining} remaining today"
        
        # Fetch courses
        courses = await asyncio.to_thread(_fetch_courses_from_api, courses_to_fetch)
        if not courses:
            await msg.edit_text("❌ No free courses found from API.")
            return
        
        total_enrolled = []
        total_already = 0
        total_expired = 0
        total_failed = 0
        limit_reached = False
        
        # For free users, limit courses
        if not user_is_premium:
            courses_for_enrollment = courses[:remaining]
        else:
            courses_for_enrollment = courses
        
        # Show progress - enrolling all accounts simultaneously
        try:
            await msg.edit_text(
                f"🚀 **Enrolling ALL {len(accounts)} accounts simultaneously...**\n\n"
                f"📚 Courses to process: {len(courses_for_enrollment)}\n"
                f"{limit_info}\n\n"
                f"⏳ Please wait...",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        
        # Enroll ALL accounts SIMULTANEOUSLY using asyncio.gather
        async def enroll_single_account(account, courses_list):
            """Enroll a single account - runs in parallel with others"""
            try:
                result = await asyncio.to_thread(_enroll_account_in_courses, account, courses_list)
                return {"account": account, "result": result, "error": None}
            except Exception as e:
                return {"account": account, "result": None, "error": str(e)}
        
        # Launch all accounts in parallel
        tasks = [enroll_single_account(acc, courses_for_enrollment) for acc in accounts]
        results = await asyncio.gather(*tasks)
        
        # Process results from all parallel enrollments
        failed_accounts = []
        for res in results:
            account = res["account"]
            
            if res["error"]:
                total_failed += len(courses_for_enrollment)
                failed_accounts.append(account["name"])
                continue
            
            result = res["result"]
            if result["error"]:
                total_failed += len(courses_for_enrollment)
                failed_accounts.append(account["name"])
                continue
            
            # Log enrollments
            for title in result["enrolled"]:
                log_enrollment(user_id, account["id"], "", title)
            
            total_enrolled.extend([(t, account["name"]) for t in result["enrolled"]])
            total_already += result["already"]
            total_expired += result["expired"]
            total_failed += result["failed"]
        
        # Track daily usage after all parallel enrollments complete
        unique_enrolled = len(set(t for t, _ in total_enrolled))
        if unique_enrolled > 0 and not user_is_premium:
            increment_daily_usage(user_id, unique_enrolled)
            new_remaining = get_remaining_today(user_id)
            limit_info = f"📊 Limit: {new_remaining} remaining today"
        
        # Show failed accounts if any
        if failed_accounts:
            try:
                await msg.edit_text(
                    f"⚠️ Some accounts failed: {', '.join(failed_accounts)}\n"
                    "Token may be expired. Update with `/enroll_setup`.\n\n"
                    "Processing other accounts...",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        
        # Update state
        update_auto_enroll_state(user_id, enrolled_count=len(total_enrolled))
        
        # Final summary
        bar = _progress_bar(1, 1)
        lines = [
            f"🎉 **Enrollment Complete!**\n",
            f"{bar}\n",
            f"📊 Accounts: {len(accounts)}",
            f"✅ Enrolled: {len(total_enrolled)}",
            f"📚 Already had: {total_already}",
            f"⏰ Expired: {total_expired}",
            f"❌ Failed: {total_failed}",
        ]
        
        # Show limit reached message for free users
        if limit_reached:
            lines.append(f"\n⚠️ Daily limit reached ({FREE_DAILY_LIMIT} courses)")
        elif not user_is_premium:
            new_remaining = get_remaining_today(user_id)
            lines.append(f"\n📊 Remaining today: {new_remaining}/{FREE_DAILY_LIMIT}")
        
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
        
        # Send full list of enrolled courses as separate message(s)
        if total_enrolled:
            course_lines = [f"**✅ Newly Enrolled ({len(total_enrolled)} courses):**\n"]
            for title, acc_name in total_enrolled:
                short = title[:50] + "..." if len(title) > 50 else title
                course_lines.append(f"✅ {short}")
            
            full_text = "\n".join(course_lines)
            
            try:
                if len(full_text) <= 4000:
                    await msg.reply_text(full_text, parse_mode="Markdown")
                else:
                    # Send in chunks
                    header = course_lines[0]
                    await msg.reply_text(header, parse_mode="Markdown")
                    
                    chunk = []
                    chunk_len = 0
                    for line in course_lines[1:]:
                        if chunk_len + len(line) + 1 > 3900:
                            await msg.reply_text("\n".join(chunk))
                            chunk = []
                            chunk_len = 0
                        chunk.append(line)
                        chunk_len += len(line) + 1
                    if chunk:
                        await msg.reply_text("\n".join(chunk))
            except Exception as e:
                log.debug(f"Failed to send full course list: {e}")
        
    except Exception as e:
        log.error(f"Enroll error: {e}")
        try:
            msg = update.callback_query.message if update.callback_query else update.effective_message
            await msg.edit_text(f"❌ Error: {str(e)[:100]}")
        except Exception:
            pass


# ─── Auto-Enroll Background Job ──────────────────────────────────────────────

async def auto_enroll_job(app: Application) -> None:
    """Background job: checks API every 2 min, enrolls all enabled accounts, notifies user"""
    bot: Bot = app.bot
    log.info("Auto-enroll background job started (every 2 min)")
    
    while True:
        await asyncio.sleep(AUTO_ENROLL_INTERVAL)
        
        try:
            # Get ALL accounts with auto_enroll flag on
            accounts = await asyncio.to_thread(get_all_auto_enroll_accounts)
            if not accounts:
                continue
            
            log.info(f"Auto-enroll: {len(accounts)} accounts active, fetching courses...")
            
            # Fetch latest courses from API
            courses = await asyncio.to_thread(_fetch_courses_from_api, 50)
            if not courses:
                log.info("Auto-enroll: No courses from API this cycle")
                continue
            
            # Group accounts by user
            user_accounts = {}
            for acc in accounts:
                user_accounts.setdefault(acc["user_id"], []).append(acc)
            
            for user_id, user_accs in user_accounts.items():
                all_enrolled = []
                failed_accounts = []
                
                # Check if user has reached daily limit (for free users)
                user_is_premium = is_premium(user_id)
                if not user_is_premium:
                    remaining = get_remaining_today(user_id)
                    if remaining <= 0:
                        log.info(f"Auto-enroll: user {user_id} hit daily limit, skipping")
                        continue
                    courses_for_user = courses[:remaining]
                else:
                    courses_for_user = courses
                
                # Enroll ALL accounts SIMULTANEOUSLY using asyncio.gather
                async def enroll_single_account(acc, courses_list):
                    """Enroll a single account - runs in parallel with others"""
                    try:
                        result = await asyncio.to_thread(
                            _enroll_account_in_courses, acc, courses_list
                        )
                        return {"acc": acc, "result": result, "error": None}
                    except Exception as e:
                        return {"acc": acc, "result": None, "error": str(e)}
                
                # Launch all accounts in parallel
                tasks = [enroll_single_account(acc, courses_for_user) for acc in user_accs]
                results = await asyncio.gather(*tasks)
                
                # Process results
                total_enrolled = 0
                for res in results:
                    acc = res["acc"]
                    if res["error"]:
                        log.error(f"Auto-enroll error acc {acc['id']}: {res['error']}")
                        failed_accounts.append(acc["name"])
                        continue
                    
                    result = res["result"]
                    if result["error"]:
                        log.warning(f"Auto-enroll login failed user {user_id} acc {acc['id']}: {result['error']}")
                        failed_accounts.append(acc["name"])
                        continue
                    
                    enrolled_count = len(result["enrolled"])
                    for title in result["enrolled"]:
                        log_enrollment(user_id, acc["id"], title, title)
                        all_enrolled.append((title, acc["name"]))
                    total_enrolled += enrolled_count
                
                # Track daily usage after all parallel enrollments complete
                if total_enrolled > 0 and not user_is_premium:
                    increment_daily_usage(user_id, total_enrolled)
                
                # Always update state (last_check timestamp)
                update_auto_enroll_state(
                    user_id,
                    last_course_id=courses[0].url if courses else None,
                    enrolled_count=len(all_enrolled)
                )
                
                # Notify user only if something new was enrolled
                if all_enrolled:
                    header = f"🔔 **Auto-Enrolled {len(all_enrolled)} Courses!**\n\n"
                    
                    # Build full list of courses
                    course_lines = []
                    for title, acc_name in all_enrolled:
                        short = title[:50] + "..." if len(title) > 50 else title
                        course_lines.append(f"✅ {short}")
                    
                    # Split into chunks if too long (Telegram limit ~4096 chars)
                    full_text = header + "\n".join(course_lines)
                    
                    try:
                        if len(full_text) <= 4000:
                            await bot.send_message(
                                chat_id=user_id,
                                text=full_text,
                                parse_mode="Markdown"
                            )
                        else:
                            # Send header first
                            await bot.send_message(
                                chat_id=user_id,
                                text=header,
                                parse_mode="Markdown"
                            )
                            # Send courses in chunks
                            chunk = []
                            chunk_len = 0
                            for line in course_lines:
                                if chunk_len + len(line) + 1 > 3900:
                                    await bot.send_message(chat_id=user_id, text="\n".join(chunk))
                                    chunk = []
                                    chunk_len = 0
                                chunk.append(line)
                                chunk_len += len(line) + 1
                            if chunk:
                                await bot.send_message(chat_id=user_id, text="\n".join(chunk))
                    except Exception as e:
                        log.debug(f"Notify user {user_id} failed: {e}")
                    
                    log.info(f"Auto-enroll: user {user_id} got {len(all_enrolled)} new courses")
                
                # Notify user if ALL accounts failed login (token expired)
                if failed_accounts and not all_enrolled:
                    try:
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔄 Update Token", callback_data="setup_add_new"),
                        ]])
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"⚠️ **Auto-Enroll: Token Expired**\n\n"
                                f"Failed: {', '.join(failed_accounts)}\n\n"
                                "Your Udemy cookies have expired.\n"
                                "Click below to update with fresh cookies:"
                            ),
                            parse_mode="Markdown",
                            reply_markup=keyboard,
                        )
                    except Exception:
                        pass
        
        except Exception as e:
            log.error(f"Auto-enroll job error: {e}")
            await asyncio.sleep(10)


# Initialize database
init_enroller_db()
