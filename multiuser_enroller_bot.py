"""
Multi-User Udemy Enroller Bot Commands
Fetches latest 50 free courses from real.discount API and enrolls directly.
"""

import asyncio
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from udemy_enroller import Course, UdemyAutoEnroller
from user_enroller import (
    init_enroller_db,
    set_user_setup_state,
    get_user_setup_state,
    clear_user_setup_state,
    store_user_credentials,
    get_user_credentials,
    user_has_credentials,
    log_scrape_history,
    get_user_stats,
    validate_token_format,
    validate_client_id_format,
    get_setup_instructions,
    delete_user_data,
)

log = logging.getLogger(__name__)

COURSES_API = "https://cdn.real.discount/api/courses"


# ─── Setup Commands ──────────────────────────────────────────────────────────

async def cmd_enroll_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the credential setup process"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "User"
    
    # Check if already set up
    if user_has_credentials(user_id):
        keyboard = [[
            InlineKeyboardButton("🔄 Update Token", callback_data="setup_update_token"),
            InlineKeyboardButton("🔄 Update Client ID", callback_data="setup_update_client_id"),
        ], [
            InlineKeyboardButton("✅ Keep Current", callback_data="setup_keep_current"),
        ]]
        
        await update.effective_message.reply_text(
            f"👤 Hi {user_name}!\n\n"
            "You already have credentials set up. What would you like to do?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # Start setup process
    set_user_setup_state(user_id, "waiting_token")
    
    message = f"""
🎓 **Udemy Auto-Enroller - Setup**

Hello {user_name}! 👋

To use the auto-enrollment feature, I need your Udemy cookies.

{get_setup_instructions()}

**Ready?** Send me your `access_token` first:
"""
    
    await update.effective_message.reply_text(message, parse_mode="Markdown")


async def cmd_set_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set access token via command"""
    if not update.effective_user or not update.effective_message or not context.args:
        await update.effective_message.reply_text("❌ Usage: `/set_token <your_token>`", parse_mode="Markdown")
        return
    
    user_id = update.effective_user.id
    token = " ".join(context.args)
    
    # Validate format
    if not validate_token_format(token):
        await update.effective_message.reply_text(
            "❌ Invalid token format. Token should be a long string (50+ characters).\n\n"
            "Get it from: Browser DevTools → Application → Cookies → `access_token`"
        )
        return
    
    # Store token
    store_user_credentials(user_id, access_token=token)
    set_user_setup_state(user_id, "waiting_client_id")
    
    await update.effective_message.reply_text(
        "✅ **Access token saved!**\n\n"
        "Now send me your `client_id`:\n"
        "`/set_client_id <your_client_id>`"
    , parse_mode="Markdown")


async def cmd_set_client_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set client ID via command"""
    if not update.effective_user or not update.effective_message or not context.args:
        await update.effective_message.reply_text("❌ Usage: `/set_client_id <your_client_id>`", parse_mode="Markdown")
        return
    
    user_id = update.effective_user.id
    client_id = " ".join(context.args)
    
    # Validate format
    if not validate_client_id_format(client_id):
        await update.effective_message.reply_text(
            "❌ Invalid client ID format.\n\n"
            "Get it from: Browser DevTools → Application → Cookies → `client_id`"
        )
        return
    
    # Store client ID
    store_user_credentials(user_id, client_id=client_id)
    clear_user_setup_state(user_id)
    
    await update.effective_message.reply_text(
        "🎉 **Setup Complete!**\n\n"
        "✅ Access token: Saved\n"
        "✅ Client ID: Saved\n\n"
        "You can now use:\n"
        "• `/enroll` - Scrape courses\n"
        "• `/enroll_status` - View your stats"
    , parse_mode="Markdown")


# ─── Interactive Message Input ──────────────────────────────────────────────

async def handle_setup_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle raw message input for setup"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    current_step = get_user_setup_state(user_id)
    
    if not current_step or current_step == "complete":
        return  # Not in setup mode
    
    message_text = update.effective_message.text.strip()
    
    if current_step == "waiting_token":
        # Validate token
        if not validate_token_format(message_text):
            await update.effective_message.reply_text(
                "❌ Token too short. Make sure you copied the full value from cookies.\n"
                "It should be 50+ characters.",
                reply_to_message_id=update.effective_message.message_id
            )
            return
        
        # Store token
        store_user_credentials(user_id, access_token=message_text)
        set_user_setup_state(user_id, "waiting_client_id")
        
        await update.effective_message.reply_text(
            "✅ **Access token saved!**\n\n"
            "Now send me your `client_id` 👇",
            reply_to_message_id=update.effective_message.message_id
        )
    
    elif current_step == "waiting_client_id":
        # Validate client ID
        if not validate_client_id_format(message_text):
            await update.effective_message.reply_text(
                "❌ Invalid client ID format.",
                reply_to_message_id=update.effective_message.message_id
            )
            return
        
        # Store client ID and complete setup
        store_user_credentials(user_id, client_id=message_text)
        clear_user_setup_state(user_id)
        
        await update.effective_message.reply_text(
            "🎉 **Setup Complete!**\n\n"
            "✅ Access token: Saved\n"
            "✅ Client ID: Saved\n\n"
            "You can now use:\n"
            "• `/enroll` - Scrape and enroll in courses\n"
            "• `/enroll_status` - View your stats",
            reply_to_message_id=update.effective_message.message_id
        )


# ─── Setup Callback Handler ──────────────────────────────────────────────────

async def setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle setup menu callbacks"""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "setup_update_token":
        set_user_setup_state(user_id, "waiting_token")
        await query.edit_message_text(
            "📝 Send me your new `access_token`:\n\n"
            "Get it from: Browser DevTools → Application → Cookies → `access_token`"
        )
    
    elif data == "setup_update_client_id":
        set_user_setup_state(user_id, "waiting_client_id")
        await query.edit_message_text(
            "📝 Send me your new `client_id`:\n\n"
            "Get it from: Browser DevTools → Application → Cookies → `client_id`"
        )
    
    elif data == "setup_keep_current":
        await query.edit_message_text(
            "✅ Keeping your current setup.\n\n"
            "Use `/enroll` to start scraping courses!"
        )


# ─── Enroll Commands (Multi-User) ────────────────────────────────────────────

async def cmd_enroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Multi-user enroll command - fetch 50 latest free courses and enroll"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    
    if not user_has_credentials(user_id):
        keyboard = [[
            InlineKeyboardButton("🔐 Setup Now", callback_data="start_setup"),
        ]]
        await update.effective_message.reply_text(
            "🔒 You haven't set up your credentials yet.\n\n"
            "Run `/enroll_setup` first to add your Udemy cookies.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Fetch & Enroll (50 courses)", callback_data="enroll_start"),
        InlineKeyboardButton("❌ Cancel", callback_data="enroll_cancel"),
    ]])
    
    await update.effective_message.reply_text(
        "🎓 **Udemy Auto-Enroller**\n\n"
        "This will fetch the latest 50 free courses and auto-enroll you.\n\n"
        "⚡ Source: Real.Discount API\n"
        "📚 Courses: Latest 50 with 100% off coupons\n"
        "⏱️ Time: ~2-4 minutes\n\n"
        "Click below to start:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def enroll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle enroll callbacks"""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "enroll_cancel":
        await query.edit_message_text("❌ Cancelled.")
    
    elif data == "enroll_start":
        await query.answer("🔄 Fetching courses...")
        await query.edit_message_text(
            "🔄 **Fetching latest 50 free courses...**\n\n"
            "⏳ Please wait..."
        )
        asyncio.create_task(_run_fetch_and_enroll(update, context))
    
    elif data == "enroll_auto_start":
        await query.answer("🚀 Starting auto-enrollment...")
        await query.edit_message_text(
            "🚀 **Auto-Enrolling...**\n\n"
            "Enrolling in courses using your Udemy credentials.\n"
            "This may take 3-5 minutes depending on course count.\n\n"
            "⏳ Please wait..."
        )
        asyncio.create_task(_run_auto_enroll(update, context))
    
    elif data == "enroll_auto_skip":
        await query.edit_message_text(
            "✅ Done! Skipped auto-enrollment.\n\n"
            "Run `/enroll` again anytime."
        )
    
    elif data == "start_setup":
        await cmd_enroll_setup(update, context)


def _progress_bar(current: int, total: int, width: int = 15) -> str:
    """Generate a text progress bar using ░ ▒ ▓ █"""
    if total == 0:
        return "░" * width
    pct = current / total
    filled_full = int(width * pct)
    remainder = (width * pct) - filled_full
    
    bar = "█" * filled_full
    if remainder > 0.66:
        bar += "▓"
    elif remainder > 0.33:
        bar += "▒"
    elif filled_full < width:
        bar += "░"
    
    bar = bar.ljust(width, "░")[:width]
    return f"{bar} {int(pct * 100)}%"


def _fetch_courses_from_api(limit: int = 50) -> list:
    """Fetch latest free courses from real.discount API"""
    courses = []
    page = 1
    per_page = min(limit, 50)
    
    while len(courses) < limit:
        try:
            resp = requests.get(
                COURSES_API,
                params={"page": page, "limit": per_page, "sortBy": "sale_start"},
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
                
                if sale_price != 0:
                    continue
                
                url = item.get("url", "")
                if "udemy.com" not in url:
                    continue
                
                title = item.get("name", "Untitled")
                coupon = None
                if "couponCode=" in url:
                    coupon = url.split("couponCode=")[1].split("&")[0]
                
                courses.append(Course(
                    title=title,
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


async def _run_fetch_and_enroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch 50 latest free courses and enroll directly"""
    try:
        user_id = update.effective_user.id
        msg = update.callback_query.message if update.callback_query else update.effective_message
        
        # Fetch courses from API
        courses = await asyncio.to_thread(_fetch_courses_from_api, 50)
        
        if not courses:
            await msg.edit_text(
                "❌ No free courses found from API.\nTry again later.",
                parse_mode="Markdown"
            )
            return
        
        log_scrape_history(user_id, "real_discount_api", len(courses))
        
        # Show what we found and start enrolling
        bar = _progress_bar(1, 1)
        lines = [
            f"{bar}\n",
            f"✅ **Fetched {len(courses)} Free Courses!**\n",
            "🚀 Starting auto-enrollment...\n",
        ]
        try:
            await msg.edit_text("\n".join(lines), parse_mode="Markdown")
        except Exception:
            pass
        
        # Get user credentials
        creds = get_user_credentials(user_id)
        if not creds or not creds.get("access_token") or not creds.get("client_id"):
            await msg.edit_text("❌ Credentials not found. Run `/enroll_setup` first.")
            return
        
        # Start enrollment
        enroller = UdemyAutoEnroller(
            access_token=creds["access_token"],
            client_id=creds["client_id"]
        )
        
        total = len(courses)
        enrolled = []
        already_enrolled = []
        failed = []
        expired = []
        last_update = -5
        
        # Verify login
        login_ok = await asyncio.to_thread(enroller.verify_login)
        if not login_ok:
            await msg.edit_text(
                "❌ **Login Failed**\n\n"
                "Your access_token may have expired.\n"
                "Get a fresh token from browser cookies and run `/enroll_setup`.",
                parse_mode="Markdown"
            )
            return
        
        # Pre-fetch enrolled courses
        await asyncio.to_thread(enroller._get_enrolled_courses)
        
        batch = []
        
        for i, course in enumerate(courses):
            if i - last_update >= 5 or i == 0:
                last_update = i
                bar = _progress_bar(i, total)
                plines = [
                    f"🚀 **Auto-Enrolling...**\n",
                    f"{bar}  ({i}/{total})\n",
                    f"✅ Enrolled: {len(enrolled)}",
                    f"📚 Already had: {len(already_enrolled)}",
                    f"⏰ Expired: {len(expired)}",
                    f"❌ Failed: {len(failed)}",
                ]
                if enrolled:
                    short = enrolled[-1][:40] + "..." if len(enrolled[-1]) > 40 else enrolled[-1]
                    plines.append(f"\n🆕 Last: {short}")
                try:
                    await msg.edit_text("\n".join(plines), parse_mode="Markdown")
                except Exception:
                    pass
            
            # Process course
            result = await asyncio.to_thread(_enroll_single_course_v2, enroller, course)
            
            if result["status"] == "enrolled":
                enrolled.append(course.title)
            elif result["status"] == "batch":
                batch.append((result["course_id"], result["coupon"], course.title))
                if len(batch) >= 5:
                    titles = await asyncio.to_thread(enroller._bulk_checkout, batch)
                    enrolled.extend(titles)
                    not_enrolled = [t for _, _, t in batch if t not in titles]
                    for t in not_enrolled:
                        failed.append({"title": t, "reason": "Bulk checkout failed"})
                    batch.clear()
            elif result["status"] == "already":
                already_enrolled.append(course.title)
            elif result["status"] == "expired":
                expired.append({"title": course.title, "reason": result.get("reason", "")})
            else:
                failed.append({"title": course.title, "reason": result.get("reason", "Unknown")})
        
        # Final batch
        if batch:
            titles = await asyncio.to_thread(enroller._bulk_checkout, batch)
            enrolled.extend(titles)
            not_enrolled = [t for _, _, t in batch if t not in titles]
            for t in not_enrolled:
                failed.append({"title": t, "reason": "Bulk checkout failed"})
        
        # Final summary
        bar = _progress_bar(total, total)
        lines = [
            f"🎉 **Auto-Enrollment Complete!**\n",
            f"{bar}\n",
            f"📊 Total processed: {total}",
            f"✅ Enrolled: {len(enrolled)}",
            f"📚 Already had: {len(already_enrolled)}",
            f"⏰ Expired/Not free: {len(expired)}",
            f"❌ Failed: {len(failed)}",
        ]
        
        if enrolled:
            lines.append("\n**✅ Newly Enrolled:**")
            for title in enrolled[:10]:
                short = title[:45] + "..." if len(title) > 45 else title
                lines.append(f"• {short}")
            if len(enrolled) > 10:
                lines.append(f"  ...and {len(enrolled) - 10} more!")
        
        if expired:
            lines.append(f"\n⏰ Skipped {len(expired)} expired/paid courses")
        
        if failed and len(failed) <= 5:
            lines.append("\n**❌ Failed:**")
            for item in failed[:5]:
                short = item['title'][:40] + "..." if len(item['title']) > 40 else item['title']
                lines.append(f"• {short} — {item['reason']}")
        elif failed:
            lines.append(f"\n❌ {len(failed)} courses failed")
        
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
        
    except Exception as e:
        log.error(f"Fetch & enroll error: {e}")
        if update.callback_query and update.callback_query.message:
            await update.callback_query.edit_message_text(f"❌ Error: {str(e)[:100]}")


async def _run_auto_enroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback auto-enroll from stored results (legacy support)"""
    await _run_fetch_and_enroll(update, context)


def _enroll_single_course_v2(enroller: UdemyAutoEnroller, course) -> dict:
    """Process a single course for enrollment. Returns status + data for batching."""
    try:
        slug = enroller._extract_slug(course.url)
        if not slug:
            return {"status": "failed", "reason": "Invalid URL"}
        
        # Quick duplicate check from pre-fetched list
        if slug in enroller.enrolled_slugs:
            return {"status": "already"}
        
        coupon = course.coupon_code or enroller._extract_coupon(course.url)
        
        # Get course ID from page
        course_id, is_free = enroller._get_course_id_from_page(slug)
        if not course_id:
            return {"status": "failed", "reason": "Course not found"}
        
        # Naturally free course
        if is_free:
            if enroller._free_checkout(course_id):
                return {"status": "enrolled"}
            else:
                return {"status": "failed", "reason": "Free enrollment failed"}
        
        # No coupon for paid course
        if not coupon:
            return {"status": "failed", "reason": "No coupon code"}
        
        # Validate coupon
        if not enroller._check_coupon(course_id, coupon):
            return {"status": "expired", "reason": "Coupon expired/not 100% off"}
        
        # Return for batch enrollment
        return {"status": "batch", "course_id": course_id, "coupon": coupon}
            
    except Exception as e:
        return {"status": "failed", "reason": str(e)[:50]}


async def cmd_enroll_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's enrollment status and stats"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    
    if not user_has_credentials(user_id):
        await update.effective_message.reply_text(
            "🔒 Setup not complete. Run `/enroll_setup` first.",
            parse_mode="Markdown"
        )
        return
    
    stats = get_user_stats(user_id)
    
    message = (
        "📊 **Your Enrollment Stats**\n\n"
        f"Total runs: {stats['total_scrapes']}\n"
        f"Total courses processed: {stats['total_courses']}\n"
    )
    if stats['last_scrape']:
        message += f"Last run: {stats['last_scrape']}\n"
    
    message += "\nRun `/enroll` to enroll in latest 50 free courses!"
    
    await update.effective_message.reply_text(message, parse_mode="Markdown")


async def cmd_myprofile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's profile and setup status"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "User"
    
    creds = get_user_credentials(user_id)
    stats = get_user_stats(user_id)
    
    if not creds:
        status = "❌ Not set up"
    elif creds.get("is_verified"):
        status = "✅ Ready to enroll"
    else:
        status = "⚠️ Incomplete setup"
    
    message = f"""
👤 **Your Profile**

Name: {user_name}
ID: `{user_id}`
Status: {status}

📊 **Statistics**
Total scrapes: {stats['total_scrapes']}
Total courses: {stats['total_courses']}
"""
    
    keyboard = []
    if not creds or not creds.get("is_verified"):
        keyboard.append([InlineKeyboardButton("🔐 Setup", callback_data="start_setup")])
    else:
        keyboard.append([
            InlineKeyboardButton("🔄 Update Credentials", callback_data="update_creds"),
            InlineKeyboardButton("🗑️ Clear Data", callback_data="clear_my_data"),
        ])
    
    await update.effective_message.reply_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        parse_mode="Markdown"
    )


async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle profile callbacks"""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "start_setup":
        await cmd_enroll_setup(update, context)
    
    elif data == "update_creds":
        keyboard = [[
            InlineKeyboardButton("Update Token", callback_data="setup_update_token"),
            InlineKeyboardButton("Update Client ID", callback_data="setup_update_client_id"),
        ]]
        await query.edit_message_text(
            "What would you like to update?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "clear_my_data":
        keyboard = [[
            InlineKeyboardButton("✅ Yes, Delete", callback_data="confirm_delete"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete"),
        ]]
        await query.edit_message_text(
            "⚠️ This will delete ALL your data:\n"
            "• Credentials\n"
            "• Scrape history\n"
            "• Setup state\n\n"
            "Are you sure?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "confirm_delete":
        if delete_user_data(user_id):
            await query.edit_message_text(
                "✅ All your data has been deleted.\n\n"
                "Run `/enroll_setup` to set up again."
            )
        else:
            await query.edit_message_text("❌ Error deleting data.")
    
    elif data == "cancel_delete":
        await cmd_myprofile(update, context)


# Initialize database
init_enroller_db()
