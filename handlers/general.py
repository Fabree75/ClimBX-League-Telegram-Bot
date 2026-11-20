"""Role-separated GUI menus, /rules, /commands, /role, profile, leaderboard, export, and broadcast."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import ContextTypes, ConversationHandler

from common import (
    GUI_PROMPT_INPUT,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    ROUTE_CATEGORIES,
    _admin_menu_keyboard,
    _can_manage_routes,
    _clear_conversation_data,
    _climber_menu_keyboard,
    _compute_competition_ranks,
    _is_admin,
    _is_registered,
    _is_routesetter,
    _menu_button_markup,
    _parse_positive_int,
    _reply_chunked,
    _routesetter_menu_keyboard,
    _send_role_menus,
    _store,
    _unregistered_menu_keyboard,
    _with_menu_button,
)
from handlers.registration import editprofile_start, register_start, regrequests_cmd
from handlers.routes import (
    _start_interactive_addroute,
    route_info,
    routes,
    start_editroute_picker,
)
from handlers.submissions import (
    all_submissions_cmd,
    pending,
    perform_withdraw,
    prompt_open_routes_for_submit,
    view_submission,
)

LOGGER = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome overview and role-separated GUI menus."""
    if not update.effective_message:
        return
    is_admin = _is_admin(update)
    is_setter = _is_routesetter(update)
    is_registered = _is_registered(update, context)

    if is_registered:
        text = (
            "🧗 Welcome to the ClimbX League at École Polytechnique!\n\n"
            "You can interact using either the GUI buttons below or slash commands at any time:\n"
            "• /profile & /editprofile - View or update your climber profile\n"
            "• /routes & /route <id> - Browse climbing routes & pictures\n"
            "• /submit <id> & /withdraw <id> - Submit or withdraw video proof\n"
            "• /leaderboard - View the live standings\n"
            "• /role - Check your role & permissions\n"
            "• /rules & /commands - Competition rules & full command list\n"
            "• /menu - Open the interactive GUI menus"
        )
        if is_admin:
            text += "\n\n🛡️ You have Admin rights (Climber + Routesetter + Admin menus enabled)."
        elif is_setter:
            text += "\n\n🔧 You have Routesetter rights (Climber + Routesetter menus enabled)."
        primary_markup = _climber_menu_keyboard()
    else:
        text = (
            "🧗 Welcome to the ClimbX League at École Polytechnique!\n\n"
            "You are currently browsing as an Unregistered user. Register to unlock full Climber features!\n"
            "Available actions using the GUI buttons below or slash commands:\n"
            "• /register - Join the league (@polytechnique.edu email)\n"
            "• /routes & /route <id> - Browse climbing routes & pictures\n"
            "• /leaderboard - View the live standings\n"
            "• /role - Check your role & permissions\n"
            "• /rules & /commands - Competition rules & available command list\n"
            "• /menu - Open the interactive GUI menu"
        )
        if is_admin:
            text += (
                "\n\n🛡️ You have Admin rights (Unregistered + Routesetter + Admin menus enabled). "
                "Use /register if you also want to compete as a climber."
            )
        elif is_setter:
            text += (
                "\n\n🔧 You have Routesetter rights (Unregistered + Routesetter menus enabled). "
                "Use /register if you also want to compete as a climber."
            )
        primary_markup = _unregistered_menu_keyboard()

    await update.effective_message.reply_text(text, reply_markup=primary_markup)
    if _can_manage_routes(update):
        await update.effective_message.reply_text(
            "🔧 Routesetter Menu — manage climbing routes:",
            reply_markup=_routesetter_menu_keyboard(),
        )
    if is_admin:
        await update.effective_message.reply_text(
            "🛡️ Admin Menu — competition administration:",
            reply_markup=_admin_menu_keyboard(),
        )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the separated role-based GUI menus."""
    if not update.effective_message:
        return
    await _send_role_menus(update.effective_message, update, context)


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display league explanations, participation guide, scoring, and video proof rules."""
    if not update.effective_message:
        return
    text = (
        "🧗 <b>ClimbX League Rules &amp; Guide</b>\n\n"
        "<b>How to Participate:</b>\n"
        "1. Register once with /register using your <code>@polytechnique.edu</code> email (username is optional).\n"
        "2. Check active routes with /routes (or filter by location: Arkose, ENSTA gym, Pan, Outdoors).\n"
        "3. Inspect full route details &amp; pictures with <code>/route &lt;route_id&gt;</code>.\n"
        "4. Record a video of your climb and submit it with <code>/submit &lt;route_id&gt;</code>.\n"
        "5. Once an admin approves your video (full Top or established Zone), points are added to /leaderboard!\n\n"
        "<b>Scoring &amp; Partial (Zone) Points:</b>\n"
        "• <b>Top</b>: Awards the full route points.\n"
        "• <b>Zone</b>: Routes worth 2+ points may have an established Zone hold awarding partial points. "
        "If you reach the Zone and later top the route, your score upgrades to the full Top points.\n\n"
        "<b>Video Proof Requirements:</b>\n"
        "• <b>Start</b>: Establish a controlled start with both hands on the starting holds.\n"
        "• <b>Ascent</b>: Complete the route using only allowed holds (no off-route dabs).\n"
        "• <b>Finish</b>: Match both hands on the top hold with control for 2 seconds (or control the Zone hold for partial points).\n"
        "• <b>Format</b>: Must be a video (photos are not accepted).\n\n"
        "<b>Profile &amp; Modifications:</b>\n"
        "• Use /editprofile to update your profile. Username changes apply immediately, whereas modifying your "
        "full name or email address requires admin approval.\n\n"
        "💡 <i>Use /commands to see the list of all slash commands available for your role.</i>"
    )
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=_menu_button_markup()
    )


async def commands_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display role-filtered slash commands (/commands)."""
    if not update.effective_message:
        return
    if _is_registered(update, context):
        text = (
            "⌨️ <b>ClimbX League Commands</b>\n\n"
            "<b>🧗 Climber Commands:</b>\n"
            "• /menu - Open the interactive GUI menus\n"
            "• /profile - View your personal stats and completed routes\n"
            "• /editprofile - Edit your username or request a full name/email change\n"
            "• <code>/routes [category]</code> - List active routes and your status (✅/🔶/⏳)\n"
            "• <code>/route [route_id]</code> - View full route info &amp; pictures\n"
            "• <code>/submit [route_id]</code> - Submit video proof for a route\n"
            "• <code>/withdraw &lt;route_id&gt;</code> - Cancel a pending submission\n"
            "• /leaderboard - View competition rankings\n"
            "• /role - View your current role &amp; permissions\n"
            "• /rules - View competition rules &amp; video proof requirements\n"
            "• /commands - View this command reference\n"
            "• /cancel - Cancel an ongoing action"
        )
    else:
        text = (
            "⌨️ <b>ClimbX League Commands</b>\n\n"
            "<b>👤 Unregistered Commands:</b>\n"
            "• /menu - Open the interactive GUI menu\n"
            "• /register - Register with your @polytechnique.edu email to unlock Climber features\n"
            "• <code>/routes [category]</code> - Browse active climbing routes\n"
            "• <code>/route [route_id]</code> - View full route info &amp; pictures\n"
            "• /leaderboard - View competition rankings\n"
            "• /role - View your current role &amp; permissions\n"
            "• /rules - View competition rules &amp; video proof requirements\n"
            "• /commands - View this command reference\n"
            "• /cancel - Cancel an ongoing action"
        )
    if _can_manage_routes(update):
        text += (
            "\n\n<b>🔧 Routesetter Commands:</b>\n"
            "• <code>/addroute</code> - Guided route creation (category, name, points, zone, pictures)\n"
            "• <code>/addroute &lt;name&gt; &lt;points&gt; [category]</code> - Quick publish (attach photo optionally)\n"
            "• <code>/editroute [route_id]</code> - Edit route name, category, points, zone points, pictures, or active status\n"
            "• <code>/routes all [category]</code> - View all routes (including inactive)"
        )
    if _is_admin(update):
        text += (
            "\n\n<b>🛡️ Admin Commands:</b>\n"
            "• /pending - List submissions awaiting review (interactive review GUI)\n"
            "• <code>/submissions [all|pending|approved|zone|rejected]</code> - List all submissions\n"
            "• <code>/submission &lt;id&gt;</code> - View video &amp; inline review buttons\n"
            "• <code>/approve &lt;id&gt;</code> - Approve full Top points\n"
            "• <code>/zone &lt;id&gt;</code> - Award the route's established Zone points\n"
            "• <code>/reject &lt;id&gt; [reason]</code> - Reject a submission with optional feedback\n"
            "• <code>/reopen &lt;id&gt;</code> - Reopen a reviewed submission to pending\n"
            "• /profilerequests - List pending profile modification requests\n"
            "• <code>/approvereq &lt;id&gt;</code> / <code>/rejectreq &lt;id&gt; [reason]</code> - Review profile modification request\n"
            "• /export - Export standings as CSV\n"
            "• <code>/broadcast &lt;message&gt;</code> - Send an announcement to all registered climbers"
        )
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=_menu_button_markup()
    )


async def role_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the caller's role(s) (Unregistered, Climber, Routesetter, Admin) and permissions (/role)."""
    if not update.effective_message:
        return
    user_id = update.effective_user.id if update.effective_user else None
    id_line = f" (Telegram ID: {user_id})" if user_id is not None else ""
    categories_str = ", ".join(ROUTE_CATEGORIES)
    is_registered = _is_registered(update, context)

    if _is_admin(update):
        climber_note = (
            "You have full Admin, Routesetter, and Climber rights in ClimbX League."
            if is_registered
            else "You have Admin and Routesetter rights in ClimbX League. "
            "(You are not registered as a climber yet — use /register if you also want to compete.)"
        )
        await update.effective_message.reply_text(
            f"🛡️ Role: Admin{id_line}\n"
            f"{climber_note}\n\n"
            "Your capabilities include:\n"
            "• Routesetter tools: /addroute, /editroute (modify name, category, points, zone points, pictures, active/inactive), /routes all\n"
            "• Submission moderation: /pending, /submissions, /submission <id>, /approve <id>, "
            "/zone <id>, /reject <id> [reason], /reopen <id>, and inline review buttons\n"
            "• Profile moderation: /profilerequests, /approvereq <id>, /rejectreq <id> [reason]\n"
            "• League operations: /export (CSV standings) and /broadcast <message>\n\n"
            f"Route categories: {categories_str}",
            reply_markup=_menu_button_markup(),
        )
        return

    if _is_routesetter(update):
        climber_note = (
            "You have Routesetter and Climber rights in ClimbX League (no admin moderation rights)."
            if is_registered
            else "You have Routesetter rights in ClimbX League (no admin moderation rights). "
            "(You are not registered as a climber yet — use /register if you also want to compete.)"
        )
        await update.effective_message.reply_text(
            f"🔧 Role: Routesetter{id_line}\n"
            f"{climber_note}\n\n"
            "Your routesetter capabilities include:\n"
            "• Create routes: /addroute (with category, points, zone points & pictures)\n"
            "• Edit routes: /editroute [route_id] (modify name, category, points, zone points, pictures, or active/inactive status)\n"
            "• View all routes: /routes all [category]\n\n"
            f"Route categories: {categories_str}",
            reply_markup=_menu_button_markup(),
        )
        return

    if is_registered:
        await update.effective_message.reply_text(
            f"🧗 Role: Climber{id_line}\n"
            "You are a registered climber. You can view your profile with /profile, edit it with /editprofile, "
            "browse routes with /routes and /route, submit climb videos with /submit, and view /leaderboard.\n"
            "Ask a league organizer if you need routesetter or admin rights.",
            reply_markup=_menu_button_markup(),
        )
        return

    await update.effective_message.reply_text(
        f"👤 Role: Unregistered{id_line}\n"
        "You are not registered in ClimbX League yet.\n\n"
        "You can currently:\n"
        "• Join the league with /register (unlocks Climber features: /profile, /editprofile, /submit, /withdraw)\n"
        "• Browse routes with /routes & /route, view /leaderboard, read /rules, and check /commands",
        reply_markup=_with_menu_button(
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("📝 Register", callback_data="gui:cmd:register")]]
            )
        ),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel any active multi-step conversation and show the role menu directly."""
    _clear_conversation_data(context)
    if update.effective_message:
        await _send_role_menus(
            update.effective_message, update, context, prefix="Cancelled."
        )
    return ConversationHandler.END


async def cancel_idle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond cleanly when /cancel is used outside an active conversation and show the menu."""
    _clear_conversation_data(context)
    if update.effective_message:
        await _send_role_menus(
            update.effective_message,
            update,
            context,
            prefix="No active action to cancel.",
        )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the caller's climber profile, rank, completed tops, zones, and pending climbs."""
    if not update.effective_user or not update.effective_message:
        return
    store = _store(context)
    is_admin = _is_admin(update)
    is_setter = _is_routesetter(update)
    info = store.get_student_profile(update.effective_user.id)
    if not info:
        register_markup = _with_menu_button(
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("📝 Register", callback_data="gui:cmd:register")]]
            )
        )
        if is_admin:
            await update.effective_message.reply_text(
                "🛡️ Role: Admin (use /role to view admin & routesetter capabilities).\n\n"
                "You are not registered as a climber yet. Use /register if you also want to join the league!",
                reply_markup=register_markup,
            )
        elif is_setter:
            await update.effective_message.reply_text(
                "🔧 Role: Routesetter (use /role to view routesetter capabilities).\n\n"
                "You are not registered as a climber yet. Use /register if you also want to join the league!",
                reply_markup=register_markup,
            )
        else:
            await update.effective_message.reply_text(
                "You are not registered yet. Use /register to join the league!",
                reply_markup=register_markup,
            )
        return

    top_word = "top" if info["tops"] == 1 else "tops"
    zone_suffix = f", {info['zones']} zone" if info.get("zones", 0) > 0 else ""
    username_line = (
        f"Username: @{info['username']}"
        if info["username"]
        else "Username: Not set (showing full name on leaderboard)"
    )
    lines = [
        f"🧗 Climber Profile: {info['full_name']}",
        username_line,
    ]
    if is_admin:
        lines.append("Role: 🛡️ Admin")
    elif is_setter:
        lines.append("Role: 🔧 Routesetter")
    lines.extend(
        [
            f"Email: {info['email']}",
            f"Rank: #{info['rank']} of {info['total_students']}",
            f"Score: {info['points']} pts ({info['tops']} {top_word}{zone_suffix})",
        ]
    )

    if info["approved_routes"]:
        lines.append("\n✅ Completed Routes:")
        for r in info["approved_routes"]:
            lines.append(f"• #{r['id']} {r['name']} ({r['points']} pts)")
    elif not info.get("zone_routes"):
        lines.append("\nNo completed routes yet. Check /routes and /submit!")

    if info.get("zone_routes"):
        lines.append("\n🔶 Zone Routes (Partial Points):")
        for r in info["zone_routes"]:
            lines.append(f"• #{r['id']} {r['name']} ({r['points']} pts)")

    if info["pending_routes"]:
        lines.append("\n⏳ Pending Review:")
        for r in info["pending_routes"]:
            lines.append(f"• #{r['route_id']} {r['name']} ({r['points']} pts)")

    lines.append("\nTip: Use /editprofile or tap Edit Profile below to modify your profile.")
    profile_markup = _with_menu_button(
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("✏️ Edit Profile", callback_data="gui:cmd:editprofile")]]
        )
    )
    await _reply_chunked(
        update.effective_message,
        "\n".join(lines),
        reply_markup=profile_markup,
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the competition standings using climber usernames (or full names if unset)."""
    if not update.effective_message:
        return
    store = _store(context)
    rows = store.leaderboard_detailed()
    if not rows:
        await update.effective_message.reply_text(
            "No scores yet.", reply_markup=_menu_button_markup()
        )
        return

    medals = {1: "🥇 ", 2: "🥈 ", 3: "🥉 "}
    lines = []
    caller_id = update.effective_user.id if update.effective_user else None
    caller_summary = None
    ranked = _compute_competition_ranks(rows)

    for rank, row in ranked:
        prefix = medals.get(rank, "")
        display_name = str(row["username"])
        points = int(row["points"])
        tops = int(row["tops"])
        zones = int(row["zones"]) if "zones" in row.keys() else 0
        top_word = "top" if tops == 1 else "tops"
        zone_part = f", {zones} zone" if zones > 0 else ""
        lines.append(
            f"{prefix}{rank}. {display_name} - {points} pts ({tops} {top_word}{zone_part})"
        )
        if caller_id is not None and int(row["telegram_id"]) == caller_id:
            caller_summary = (
                f"\nYour rank: #{rank} of {len(rows)} ({points} pts, {tops} {top_word}{zone_part})"
            )

    if caller_summary:
        lines.append(caller_summary)

    await _reply_chunked(
        update.effective_message,
        "\n".join(lines),
        reply_markup=_menu_button_markup(),
    )


async def export_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and upload the CSV league standings for admins."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return

    await update.effective_message.reply_text(
        "⏳ Generating standings export, please wait..."
    )

    rows = _store(context).get_export_rows()
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "rank",
            "telegram_id",
            "username",
            "full_name",
            "email",
            "points",
            "tops",
            "zones",
            "completed_routes",
        ],
    )
    writer.writeheader()
    writer.writerows(rows)
    payload = io.BytesIO(buf.getvalue().encode("utf-8"))
    payload.name = "climbx_standings.csv"
    await update.effective_message.reply_document(
        document=payload,
        filename="climbx_standings.csv",
        caption=f"ClimbX League standings ({len(rows)} registered climbers).",
        reply_markup=_menu_button_markup(),
    )


async def _run_broadcast_text(
    message: Any, context: ContextTypes.DEFAULT_TYPE, raw_text: str
) -> None:
    announcement = f"📢 ClimbX League Announcement\n\n{raw_text.strip()}"
    if len(announcement) > MAX_TELEGRAM_MESSAGE_LENGTH:
        await message.reply_text(
            f"Announcement is too long ({len(announcement)}/{MAX_TELEGRAM_MESSAGE_LENGTH} chars).",
            reply_markup=_menu_button_markup(),
        )
        return

    students = _store(context).get_all_students()
    if not students:
        await message.reply_text(
            "No registered students to broadcast to.",
            reply_markup=_menu_button_markup(),
        )
        return

    sent_count = 0
    throttle = len(students) > 20
    for student in students:
        chat_id = int(student["telegram_id"])
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=announcement,
                reply_markup=_menu_button_markup(),
            )
            sent_count += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.1)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=announcement,
                    reply_markup=_menu_button_markup(),
                )
                sent_count += 1
            except Exception:
                LOGGER.warning("Failed to send broadcast to student %s after retry", chat_id)
        except Exception:
            LOGGER.warning("Failed to send broadcast to student %s", chat_id)
        if throttle:
            await asyncio.sleep(0.035)

    await message.reply_text(
        f"Announcement sent to {sent_count}/{len(students)} registered students.",
        reply_markup=_menu_button_markup(),
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast an admin announcement via /broadcast <message>."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    parts = (update.effective_message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.effective_message.reply_text(
            "Usage: /broadcast <message>", reply_markup=_menu_button_markup()
        )
        return
    await _run_broadcast_text(update.effective_message, context, parts[1])


async def gui_conversation_entry_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Enter multi-step conversations directly from GUI inline dashboard buttons."""
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return ConversationHandler.END
    data = query.data or ""

    if data == "gui:cmd:register":
        await query.answer()
        return await register_start(update, context)

    if data == "gui:cmd:editprofile":
        await query.answer()
        return await editprofile_start(update, context)

    if data == "gui:cmd:submit":
        await query.answer()
        _clear_conversation_data(context)
        return await prompt_open_routes_for_submit(
            query.message, _store(context), update.effective_user.id
        )

    if data == "gui:cmd:addroute":
        if not _can_manage_routes(update):
            await query.answer("Routesetter or admin access required.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        return await _start_interactive_addroute(query.message, context, None)

    if data == "gui:cmd:editroute":
        if not _can_manage_routes(update):
            await query.answer("Routesetter or admin access required.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        return await start_editroute_picker(query.message, context)

    if data == "gui:cmd:broadcast":
        if not _is_admin(update):
            await query.answer("Admin access required.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        _clear_conversation_data(context)
        context.user_data["gui_action"] = "broadcast"
        await query.message.reply_text(
            "📢 Type the announcement message to broadcast to all registered climbers (or /cancel):"
        )
        return GUI_PROMPT_INPUT

    return ConversationHandler.END


async def gui_prompt_input_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Process single-turn text prompts started from GUI buttons (broadcast)."""
    if not update.effective_message:
        return ConversationHandler.END
    action = context.user_data.get("gui_action")
    text = (update.effective_message.text or "").strip()

    if action == "broadcast":
        if not _is_admin(update):
            _clear_conversation_data(context)
            await update.effective_message.reply_text(
                "Admin access required.", reply_markup=_menu_button_markup()
            )
            return ConversationHandler.END
        if not text:
            await update.effective_message.reply_text(
                "Please enter a non-empty broadcast message, or /cancel."
            )
            return GUI_PROMPT_INPUT
        _clear_conversation_data(context)
        await _run_broadcast_text(update.effective_message, context, text)
        return ConversationHandler.END

    _clear_conversation_data(context)
    return ConversationHandler.END


async def gui_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle '📋 Open Menu' button clicks and GUI command/picker callbacks."""
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    data = query.data or ""
    store = _store(context)
    user_id = update.effective_user.id
    is_admin = _is_admin(update)

    if data == "gui:menu":
        await query.answer()
        await _send_role_menus(query.message, update, context)
        return

    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "gui":
        await query.answer()
        return

    sub_action = parts[1]
    if sub_action == "withdraw":
        route_id = _parse_positive_int(parts[2])
        if route_id is None:
            await query.answer("Invalid route.", show_alert=True)
            return
        ok, msg = await perform_withdraw(context.bot, store, user_id, route_id)
        if not ok:
            await query.answer("No pending submission for that route.", show_alert=True)
            return
        await query.answer("Submission withdrawn.")
        await query.message.reply_text(msg, reply_markup=_menu_button_markup())
        return

    if sub_action == "viewsub":
        if not is_admin:
            await query.answer("Admin access required.", show_alert=True)
            return
        sub_id = _parse_positive_int(parts[2])
        sub = store.get_submission(sub_id) if sub_id else None
        if sub is None:
            await query.answer("Submission not found.", show_alert=True)
            return
        await query.answer()
        context.args = [str(sub_id)]
        await view_submission(update, context)
        return

    if sub_action == "cmd":
        cmd = parts[2]
        context.args = []
        student_cmds = {
            "routes": routes,
            "route": route_info,
            "profile": profile,
            "leaderboard": leaderboard,
            "role": role_status,
            "admin": role_status,
            "rules": rules_command,
            "commands": commands_command,
            "help": rules_command,
        }
        if cmd in student_cmds:
            await query.answer()
            await student_cmds[cmd](update, context)
            return

        if cmd == "withdraw":
            await query.answer()
            info = store.get_student_profile(user_id)
            if not info:
                await query.message.reply_text(
                    "Please register first with /register.",
                    reply_markup=_menu_button_markup(),
                )
                return
            pend = info["pending_routes"]
            if not pend:
                await query.message.reply_text(
                    "You have no pending submissions to withdraw.",
                    reply_markup=_menu_button_markup(),
                )
                return
            kb = [
                [
                    InlineKeyboardButton(
                        f"Withdraw #{r['route_id']} {r['name']}",
                        callback_data=f"gui:withdraw:{r['route_id']}",
                    )
                ]
                for r in pend
            ]
            kb.append([InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu")])
            await query.message.reply_text(
                "Select a pending submission to withdraw:",
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return

        if cmd == "routes_all":
            if not _can_manage_routes(update):
                await query.answer("Routesetter or admin access required.", show_alert=True)
                return
            await query.answer()
            context.args = ["all"]
            await routes(update, context)
            return

        if not is_admin:
            await query.answer("Admin access required.", show_alert=True)
            return

        if cmd == "submission":
            await query.answer()
            subs = store.all_submissions(status_filter="all")
            if not subs:
                await query.message.reply_text(
                    "No submissions found.",
                    reply_markup=_menu_button_markup(),
                )
                return
            kb = [
                [
                    InlineKeyboardButton(
                        f"#{s['id']} {s['full_name']} - {s['route_name']} ({s['status']})",
                        callback_data=f"gui:viewsub:{s['id']}",
                    )
                ]
                for s in subs[:20]
            ]
            kb.append([InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu")])
            await query.message.reply_text(
                "🔎 Select a submission to view its video & review card:",
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return

        admin_cmds = {
            "pending": pending,
            "submissions": all_submissions_cmd,
            "profilerequests": regrequests_cmd,
            "regrequests": regrequests_cmd,
            "export": export_results,
        }
        if cmd in admin_cmds:
            await query.answer()
            await admin_cmds[cmd](update, context)
            return

    await query.answer()
