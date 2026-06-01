"""
Multi-Account Udemy Auto-Enroller Bot
- Multiple accounts per user
- Auto-enroll background job (checks API every 10 min)
- Notifications when new courses are enrolled
"""

import asyncio
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ContextTypes, Application

from udemy_enroller import Course, UdemyAutoEnroller
from user_enroller import (
    init_enroller_db,
    add_account, get_user_accounts, get_account, remove_account, toggle_auto_enroll,
    get_all_auto_enroll_accounts,
    set_user_setup_state, get_user_setup_state, clear_user_setup_state,
    get_auto_enroll_state, set_auto_enroll_enabled, update_auto_enroll_state,
    log_enrollment, is_course_enrolled, get_recently_enrolled,
    user_has_credentials, get_user_stats,
    validate_token_format, validate_client_id_format, get_setup_instructions,
    delete_user_data,
    # Premium & Access Control
    is_owner, is_premium, grant_premium, revoke_premium, get_all_premium_users,
    can_enroll, get_remaining_today, increment_daily_usage, FREE_DAILY_LIMIT,
    get_all_daily_stats, get_daily_usage, get_user_total_enrollments,
)

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
    """Set access token via command"""
    if not update.effective_user or not update.effective_message or not context.args:
        await update.effective_message.reply_text("Usage: `/set_token <your_token>`", parse_mode="Markdown")
        return
    
    user_id = update.effective_user.id
    token = " ".join(context.args)
    
    if not validate_token_format(token):
        await update.effective_message.reply_text("❌ Token too short (need 20+ chars)")
        return
    
    set_user_setup_state(user_id, "waiting_client_new", None)
    context.user_data["pending_token"] = token
    
    await update.effective_message.reply_text(
        "✅ Token received!\n\nNow send: `/set_client_id <your_client_id>`",
        parse_mode="Markdown"
    )


async def cmd_set_client_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set client ID via command"""
    if not update.effective_user or not update.effective_message or not context.args:
        await update.effective_message.reply_text("Usage: `/set_client_id <your_client_id>`", parse_mode="Markdown")
        return
    
    user_id = update.effective_user.id
    client_id = " ".join(context.args)
    
    if not validate_client_id_format(client_id):
        await update.effective_message.reply_text("❌ Invalid client_id format")
        return
    
    token = context.user_data.get("pending_token")
    if not token:
        await update.effective_message.reply_text(
            "❌ No token found. Send `/set_token <token>` first.",
            parse_mode="Markdown"
        )
        return
    
    accounts = get_user_accounts(user_id)
    name = f"Account {len(accounts) + 1}"
    acc_id = add_account(user_id, name, token, client_id)
    clear_user_setup_state(user_id)
    context.user_data.pop("pending_token", None)
    
    await update.effective_message.reply_text(
        f"🎉 **Setup Complete!**\n\n"
        f"✅ {name} added successfully\n"
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
        context.user_data["pending_token"] = text
        context.user_data["pending_account_name"] = extra or "Account 1"
        set_user_setup_state(user_id, "waiting_client_new")
        await update.effective_message.reply_text("✅ Token saved!\n\nNow send your `client_id`:")
    
    elif step == "waiting_client_new":
        if not validate_client_id_format(text):
            await update.effective_message.reply_text("❌ Invalid client_id.")
            return
        
        token = context.user_data.get("pending_token")
        if not token:
            await update.effective_message.reply_text("❌ Session expired. Start again with `/enroll_setup`")
            clear_user_setup_state(user_id)
            return
        
        name = context.user_data.get("pending_account_name", "Account")
        acc_id = add_account(user_id, name, token, text)
        clear_user_setup_state(user_id)
        context.user_data.pop("pending_token", None)
        context.user_data.pop("pending_account_name", None)
        
        await update.effective_message.reply_text(
            f"🎉 **Setup Complete!**\n\n"
            f"✅ {name} added successfully\n"
            f"🚀 Auto-enrollment STARTED!\n\n"
            f"The bot will now automatically enroll you in free courses every 2 minutes.\n"
            f"You'll receive notifications when courses are enrolled.\n\n"
            f"📊 `/enroll_status` — View your stats",
            parse_mode="Markdown"
        )


# ─── Account Management ──────────────────────────────────────────────────────

async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show and manage accounts"""
    if not update.effective_user or not update.effective_message:
        return
    
    user_id = update.effective_user.id
    accounts = get_user_accounts(user_id)
    
    if not accounts:
        await update.effective_message.reply_text(
            "No accounts set up.\nRun `/enroll_setup` to add one.",
            parse_mode="Markdown"
        )
        return
    
    lines = ["🎓 **Your Udemy Accounts:**\n"]
    keyboard = []
    for a in accounts:
        auto = "🟢 Auto" if a["auto_enroll"] else "🔴 Manual"
        lines.append(f"**{a['name']}** (ID: {a['id']}) — {auto}")
        keyboard.append([
            InlineKeyboardButton(
                f"{'🔴 Disable' if a['auto_enroll'] else '🟢 Enable'} Auto - {a['name']}",
                callback_data=f"acc_toggle_{a['id']}"
            ),
            InlineKeyboardButton(f"🗑️ Remove", callback_data=f"acc_remove_{a['id']}"),
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Add Account", callback_data="setup_add_new")])
    
    await update.effective_message.reply_text(
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
    
    elif data == "setup_update_client_id":
        set_user_setup_state(user_id, "waiting_client_new")
        await query.edit_message_text("📝 Send your new `client_id`:")
    
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
        await cmd_accounts(update, context)
    
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
    
    for course in courses:
        slug = enroller._extract_slug(course.url)
        if not slug:
            failed += 1
            continue
        if slug in enroller.enrolled_slugs:
            already += 1
            continue
        
        coupon = course.coupon_code or enroller._extract_coupon(course.url)
        course_id, is_free = enroller._get_course_id_from_page(slug)
        
        if not course_id:
            log.debug(f"No course_id for {slug}")
            failed += 1
            continue
        
        if is_free:
            free_result = enroller._free_checkout(course_id)
            if free_result == "enrolled":
                enrolled.append(course.title)
                log.info(f"Free enrolled: {course.title[:40]}")
            elif free_result == "already":
                already += 1
            else:
                failed += 1
            continue
        
        if not coupon:
            failed += 1
            continue
        
        if not enroller._check_coupon(course_id, coupon):
            expired += 1
            continue
        
        batch.append((course_id, coupon, course.title))
        log.debug(f"Added to batch: {course.title[:30]}")
        
        if len(batch) >= 5:
            log.info(f"Processing batch of {len(batch)} courses")
            titles = enroller._bulk_checkout(batch)
            enrolled.extend(titles)
            if titles:
                log.info(f"Batch enrolled {len(titles)}: {titles}")
            failed += len(batch) - len(titles)
            batch.clear()
    
    if batch:
        log.info(f"Processing final batch of {len(batch)} courses")
        titles = enroller._bulk_checkout(batch)
        enrolled.extend(titles)
        if titles:
            log.info(f"Final batch enrolled {len(titles)}: {titles}")
        failed += len(batch) - len(titles)
    
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
        
        for i, account in enumerate(accounts):
            # Check remaining limit for free users BEFORE each account
            if not user_is_premium:
                current_remaining = get_remaining_today(user_id)
                if current_remaining <= 0:
                    limit_reached = True
                    break
                # Limit courses for this account
                courses_for_account = courses[:current_remaining]
            else:
                courses_for_account = courses
            
            bar = _progress_bar(i, len(accounts))
            try:
                await msg.edit_text(
                    f"🚀 **Enrolling...**\n\n"
                    f"Account: {account['name']} ({i+1}/{len(accounts)})\n"
                    f"{bar}\n"
                    f"Courses to process: {len(courses_for_account)}\n"
                    f"{limit_info}\n\n"
                    f"✅ Enrolled so far: {len(total_enrolled)}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            
            result = await asyncio.to_thread(_enroll_account_in_courses, account, courses_for_account)
            
            if result["error"]:
                total_failed += len(courses_for_account)
                try:
                    await msg.edit_text(
                        f"⚠️ {account['name']}: {result['error']}\n"
                        "Token may be expired. Update with `/enroll_setup`.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                continue
            
            # Log enrollments and track usage IMMEDIATELY
            enrolled_count = len(result["enrolled"])
            for title in result["enrolled"]:
                log_enrollment(user_id, account["id"], "", title)
            
            # Track daily usage immediately after enrollment
            if enrolled_count > 0:
                increment_daily_usage(user_id, enrolled_count)
            
            total_enrolled.extend([(t, account["name"]) for t in result["enrolled"]])
            total_already += result["already"]
            total_expired += result["expired"]
            total_failed += result["failed"]
            
            # Update limit info for display
            if not user_is_premium:
                new_remaining = get_remaining_today(user_id)
                limit_info = f"📊 Limit: {new_remaining} remaining today"
        
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
                
                for acc in user_accs:
                    # Re-check limit before each account for free users
                    if not user_is_premium:
                        current_remaining = get_remaining_today(user_id)
                        if current_remaining <= 0:
                            log.info(f"Auto-enroll: user {user_id} hit limit mid-enrollment")
                            break
                        courses_for_acc = courses_for_user[:current_remaining]
                    else:
                        courses_for_acc = courses_for_user
                    
                    try:
                        result = await asyncio.to_thread(
                            _enroll_account_in_courses, acc, courses_for_acc
                        )
                    except Exception as e:
                        log.error(f"Auto-enroll error acc {acc['id']}: {e}")
                        failed_accounts.append(acc["name"])
                        continue
                    
                    if result["error"]:
                        log.warning(f"Auto-enroll login failed user {user_id} acc {acc['id']}: {result['error']}")
                        failed_accounts.append(acc["name"])
                        continue
                    
                    enrolled_count = len(result["enrolled"])
                    for title in result["enrolled"]:
                        log_enrollment(user_id, acc["id"], title, title)
                        all_enrolled.append((title, acc["name"]))
                    
                    # Track daily usage immediately after each account enrollment
                    if enrolled_count > 0:
                        increment_daily_usage(user_id, enrolled_count)
                
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
