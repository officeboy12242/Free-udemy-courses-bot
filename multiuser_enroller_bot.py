"""
Multi-User Udemy Enroller Bot Commands
Handles per-user credential setup and enrollment
"""

import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from udemy_enroller import UdemyScraper, Course
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

# Available coupon sites (same as before)
AVAILABLE_SITES = {
    "discudemy": "🎓 DiscUdemy",
    "udemyfreebies": "📚 Udemy Freebies",
    "tutorialbar": "📖 Tutorial Bar",
    "realdiscount": "💰 Real Discount",
    "coursevania": "🏫 CourseVania",
    "enext": "💼 E-Next",
    "coursejoiner": "🔗 CourseJoiner",
    "courson": "🎯 Courson",
}


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
    """Multi-user enroll command - check credentials first"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    
    # Check if credentials are set up
    if not user_has_credentials(user_id):
        keyboard = [[
            InlineKeyboardButton("🔐 Setup Now", callback_data="start_setup"),
        ]]
        
        await update.effective_message.reply_text(
            "🔒 You haven't set up your credentials yet.\n\n"
            "Run setup to enable auto-enrollment:\n"
            "`/enroll_setup`",
            reply_markup=InlineKeyboardMarkup(keyboard) if not keyboard[0][0].text.startswith("🔐") else None,
            parse_mode="Markdown"
        )
        return
    
    # User has credentials - show site selection
    if "enroll_sites" not in context.user_data:
        context.user_data["enroll_sites"] = set(AVAILABLE_SITES.keys())
    
    await _show_enroll_menu(update, context)


async def _show_enroll_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display site selection menu for multi-user"""
    selected = context.user_data.get("enroll_sites", set(AVAILABLE_SITES.keys()))
    
    keyboard = []
    
    for site_key, site_label in AVAILABLE_SITES.items():
        is_selected = site_key in selected
        emoji = "✅" if is_selected else "⭕"
        keyboard.append([
            InlineKeyboardButton(f"{emoji} {site_label}", callback_data=f"enroll_toggle_{site_key}")
        ])
    
    keyboard.append([
        InlineKeyboardButton("🚀 Start Scraping", callback_data="enroll_start"),
        InlineKeyboardButton("❌ Cancel", callback_data="enroll_cancel"),
    ])
    
    text = (
        "🎓 **Udemy Course Scraper**\n\n"
        "Select coupon sites to scrape:\n"
        f"(Selected: {len(selected)}/{len(AVAILABLE_SITES)})\n\n"
        "💡 Toggle sites with buttons"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        await update.effective_message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )


async def enroll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle enroll callbacks for multi-user"""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    data = query.data
    selected = context.user_data.get("enroll_sites", set(AVAILABLE_SITES.keys()))
    
    if data.startswith("enroll_toggle_"):
        site = data.replace("enroll_toggle_", "")
        if site in selected:
            selected.discard(site)
        else:
            selected.add(site)
        context.user_data["enroll_sites"] = selected
        await _show_enroll_menu(update, context)
        await query.answer()
    
    elif data == "enroll_cancel":
        await query.edit_message_text("❌ Cancelled.")
    
    elif data == "enroll_start":
        if not selected:
            await query.answer("⚠️ Select at least one site!", show_alert=True)
            return
        
        await query.answer("🔄 Starting scraper...")
        await query.edit_message_text(
            "🔄 Scraping courses...\n\n"
            "This takes 1-2 minutes.\n"
            "Please wait..."
        )
        
        asyncio.create_task(_run_scraper_multiuser(update, context, list(selected)))
    
    elif data == "start_setup":
        await cmd_enroll_setup(update, context)


async def _run_scraper_multiuser(update: Update, context: ContextTypes.DEFAULT_TYPE, sites: list) -> None:
    """Run scraper for multi-user"""
    try:
        user_id = update.effective_user.id
        scraper = UdemyScraper()
        
        results = await asyncio.to_thread(scraper.scrape_all, sites)
        
        total_courses = sum(len(courses) for courses in results.values())
        
        if total_courses == 0:
            message_text = (
                "❌ No courses found.\n"
                "Sites may be blocking or coupons expired."
            )
        else:
            lines = [f"✅ **Found {total_courses} Courses!**\n"]
            
            for site, courses in results.items():
                if courses:
                    site_label = AVAILABLE_SITES.get(site, site)
                    lines.append(f"• {site_label}: {len(courses)} courses")
                    log_scrape_history(user_id, site, len(courses))
            
            message_text = "\n".join(lines)
        
        if update.callback_query and update.callback_query.message:
            await update.callback_query.edit_message_text(message_text, parse_mode="Markdown")
        else:
            await update.effective_message.reply_text(message_text, parse_mode="Markdown")
        
        context.user_data["enroll_results"] = results
        
    except Exception as e:
        log.error(f"Scraper error: {e}")
        if update.callback_query and update.callback_query.message:
            await update.callback_query.edit_message_text(f"❌ Error: {str(e)[:100]}")


async def cmd_enroll_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's enrollment status and stats"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    
    # Check credentials
    if not user_has_credentials(user_id):
        await update.effective_message.reply_text(
            "🔒 Setup not complete. Run `/enroll_setup` first.",
            parse_mode="Markdown"
        )
        return
    
    # Get results
    results = context.user_data.get("enroll_results", {})
    stats = get_user_stats(user_id)
    
    if not results:
        message = (
            "📊 **Your Stats**\n\n"
            f"Total scrapes: {stats['total_scrapes']}\n"
            f"Total courses found: {stats['total_courses']}\n"
        )
        if stats['last_scrape']:
            message += f"Last scrape: {stats['last_scrape']}\n"
        
        message += "\nRun `/enroll` to start scraping!"
    else:
        total = sum(len(courses) for courses in results.values())
        message = f"📊 **Latest Results**\n\nTotal: {total} courses\n\n"
        
        for site, courses in results.items():
            if courses:
                site_label = AVAILABLE_SITES.get(site, site)
                message += f"✅ {site_label}: {len(courses)}\n"
    
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
