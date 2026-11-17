"""Navigation, GUI dashboard, profile, leaderboard, export, broadcast, and cancel handlers."""

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
    _clear_conversation_data,
    _compute_competition_ranks,
    _is_admin,
    _parse_positive_int,
    _reply_chunked,
    _store,
)
from handlers.registration import register_start, regrequests_cmd
from handlers.routes import (
    _start_interactive_addroute,
    apply_zone_points_update,
    route_info,
    routes,
    start_addroutephoto_session,
)
from handlers.submissions import (
    all_submissions_cmd,
    pending,
    perform_withdraw,
    prompt_open_routes_for_submit,
    view_submission,
)

LOGGER = logging.getLogger(__name__)


def _gui_dashboard_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("🧗 Routes", callback_data="gui:cmd:routes"),
            InlineKeyboardButton("🔍 Route Info", callback_data="gui:cmd:route"),
        ],
        [
            InlineKeyboardButton("🎥 Submit Proof", callback_data="gui:cmd:submit"),
            InlineKeyboardButton("↩️ Withdraw", callback_data="gui:cmd:withdraw"),
        ],
        [
            InlineKeyboardButton("👤 My Profile", callback_data="gui:cmd:profile"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="gui:cmd:leaderboard"),
        ],
        [
            InlineKeyboardButton("📝 Register", callback_data="gui:cmd:register"),
            InlineKeyboardButton("🛡️ Admin Status", callback_data="gui:cmd:admin"),
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="gui:cmd:help"),
        ],
    ]
    if is_admin:
        rows.extend(
            [
                [
                    InlineKeyboardButton("➕ Add Route", callback_data="gui:cmd:addroute"),
                    InlineKeyboardButton("📸 Add Route Photo", callback_data="gui:cmd:addroutephoto"),
                ],
                [
                    InlineKeyboardButton("📍 Set Category", callback_data="gui:cmd:setcategory"),
                    InlineKeyboardButton("🔶 Set Zone Points", callback_data="gui:cmd:setzone"),
                ],
                [
                    InlineKeyboardButton("🔄 Toggle Route", callback_data="gui:cmd:toggleroute"),
                    InlineKeyboardButton("⏳ Pending", callback_data="gui:cmd:pending"),
                ],
                [
                    InlineKeyboardButton("📋 All Submissions", callback_data="gui:cmd:submissions"),
                    InlineKeyboardButton("🔎 View Submission", callback_data="gui:cmd:submission"),
                ],
                [
                    InlineKeyboardButton("🪪 Reg. Requests", callback_data="gui:cmd:regrequests"),
                    InlineKeyboardButton("📊 Export CSV", callback_data="gui:cmd:export"),
                ],
                [
                    InlineKeyboardButton("📢 Broadcast", callback_data="gui:cmd:broadcast"),
                ],
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "⌨️ Switch to Raw Text Mode", callback_data="gui:toggle_mode"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _is_gui_enabled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user:
        return False
    return _store(context).get_user_gui_mode(update.effective_user.id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome overview and optional GUI keyboard."""
    if not update.effective_message:
        return
    is_admin = _is_admin(update)
    gui_on = _is_gui_enabled(update, context)
    text = (
        "🧗 Welcome to the ClimbX League at École Polytechnique!\n\n"
        "• /register - Join the league or request a profile update\n"
        "• /routes - View climbing routes & categories\n"
        "• /route <route_id> - View full details & pictures of a route\n"
        "• /submit <route_id> - Send a video proof of your ascent\n"
        "• /withdraw <route_id> - Withdraw a pending submission\n"
        "• /profile - Check your rank, score, and completed tops\n"
        "• /leaderboard - View the live standings\n"
        "• /menu - Open the interactive button GUI\n"
        "• /mode - Toggle between GUI buttons and raw slash commands\n"
        "• /help - Rules & full command guide"
    )
    if is_admin:
        text += "\n\n🛡️ You have admin rights! Use /admin or /help to see admin commands."
    markup = _gui_dashboard_keyboard(is_admin) if gui_on else None
    await update.effective_message.reply_text(text, reply_markup=markup)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the interactive button GUI or offer a toggle if in raw text mode."""
    if not update.effective_message:
        return
    is_admin = _is_admin(update)
    if not _is_gui_enabled(update, context):
        await update.effective_message.reply_text(
            "⌨️ You are currently in Raw Text (Slash Command) Mode.\n"
            "Use /mode (or tap below) to enable the interactive GUI menu, or use slash commands directly.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📱 Switch to GUI Mode", callback_data="gui:toggle_mode")]]
            ),
        )
        return
    role_label = "🛡️ Admin & Climber Menu" if is_admin else "🧗 Climber Menu"
    await update.effective_message.reply_text(
        f"{role_label} — select an action below (or use /mode to switch to raw slash commands):",
        reply_markup=_gui_dashboard_keyboard(is_admin),
    )


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle or explicitly set GUI vs raw slash command mode."""
    if not update.effective_user or not update.effective_message:
        return
    store = _store(context)
    user_id = update.effective_user.id
    current = store.get_user_gui_mode(user_id)
    if context.args:
        arg = context.args[0].strip().lower()
        if arg in {"gui", "buttons", "on"}:
            new_mode = True
        elif arg in {"text", "raw", "slash", "off"}:
            new_mode = False
        else:
            await update.effective_message.reply_text("Usage: /mode [gui|text]")
            return
    else:
        new_mode = not current

    store.set_user_gui_mode(user_id, new_mode)
    if new_mode:
        await update.effective_message.reply_text(
            "📱 Switched to GUI Mode! Use the buttons below (and raw slash commands remain available too):",
            reply_markup=_gui_dashboard_keyboard(_is_admin(update)),
        )
    else:
        await update.effective_message.reply_text(
            "⌨️ Switched to Raw Text (Slash Command) Mode. GUI buttons are hidden; use slash commands directly or /mode to switch back.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📱 Switch to GUI Mode", callback_data="gui:toggle_mode")]]
            ),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display league rules, video proof rules, and available commands."""
    if not update.effective_message:
        return
    text = (
        "🧗 <b>ClimbX League Guide &amp; Rules</b>\n\n"
        "<b>How to Participate:</b>\n"
        "1. Register once with /register (choose your leaderboard username).\n"
        "2. Check active routes with /routes (or filter by location: Arkose, ENSTA gym, Pan, Outdoors).\n"
        "3. Inspect full route details &amp; pictures with <code>/route &lt;route_id&gt;</code>.\n"
        "4. Record a video of your climb and submit it with <code>/submit &lt;route_id&gt;</code>.\n"
        "5. Once an admin approves your video (full Top or partial Zone), points are added to /leaderboard!\n\n"
        "<b>Video Proof Requirements:</b>\n"
        "• <b>Start</b>: Establish a controlled start with both hands on the starting holds.\n"
        "• <b>Ascent</b>: Complete the route using only allowed holds (no off-route dabs).\n"
        "• <b>Finish</b>: Match both hands on the top hold with control for 2 seconds (or reach the Zone hold for partial points).\n"
        "• <b>Format</b>: Must be a video (photos are not accepted).\n\n"
        "<b>Student Commands:</b>\n"
        "• /register - Register (or request a profile update from admins)\n"
        "• <code>/routes [category]</code> - List active routes and your status (✅/🔶/⏳)\n"
        "• <code>/route [route_id]</code> - View full route info &amp; pictures\n"
        "• <code>/submit [route_id]</code> - Submit video proof for a route\n"
        "• <code>/withdraw &lt;route_id&gt;</code> - Cancel a pending submission\n"
        "• /profile - View your personal stats and completed routes\n"
        "• /leaderboard - View competition rankings\n"
        "• /menu - Open the interactive button GUI\n"
        "• <code>/mode [gui|text]</code> - Toggle between GUI buttons and raw slash commands\n"
        "• /admin - View your admin status &amp; rights\n"
        "• /cancel - Cancel an ongoing action"
    )
    if _is_admin(update):
        text += (
            "\n\n🛡️ <b>Admin Status:</b> You are an admin and have admin rights.\n"
            "<b>Admin Commands:</b>\n"
            "• <code>/addroute</code> - Guided route creation (category, name, points, zone, pictures)\n"
            "• <code>/addroute &lt;name&gt; &lt;points&gt; [category]</code> - Quick publish (attach photo optionally)\n"
            "• <code>/addroutephoto &lt;route_id&gt;</code> - Add picture(s) to an existing route\n"
            "• <code>/setroutecategory &lt;route_id&gt; &lt;category&gt;</code> - Set route location category\n"
            "• <code>/setzonepoints &lt;route_id&gt; &lt;points|none&gt;</code> - Set or clear route partial zone points\n"
            "• <code>/deactivateroute &lt;id&gt;</code> - Retire/deactivate a route\n"
            "• <code>/activateroute &lt;id&gt;</code> - Reactivate a route\n"
            "• <code>/routes all [category]</code> - View all routes (including inactive)\n"
            "• /pending - List submissions awaiting review\n"
            "• <code>/submissions [all|pending|approved|zone|rejected]</code> - List all submissions\n"
            "• <code>/submission &lt;id&gt;</code> - View video &amp; inline review buttons\n"
            "• <code>/approve &lt;id&gt;</code> - Approve full Top points\n"
            "• <code>/zone &lt;id&gt; [points]</code> - Concede partial (zone) points\n"
            "• <code>/reject &lt;id&gt; [reason]</code> - Reject a submission with optional feedback\n"
            "• <code>/reopen &lt;id&gt;</code> - Reopen a reviewed submission to pending\n"
            "• /regrequests - List pending re-registration requests\n"
            "• <code>/approvereg &lt;id&gt;</code> / <code>/rejectreg &lt;id&gt; [reason]</code> - Review re-registration request\n"
            "• /export - Export standings as CSV\n"
            "• <code>/broadcast &lt;message&gt;</code> - Send an announcement to all registered climbers"
        )
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report whether the caller holds admin privileges and summarize admin tools."""
    if not update.effective_message:
        return
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(update):
        id_line = f" (Telegram ID: {user_id})" if user_id is not None else ""
        await update.effective_message.reply_text(
            f"Admin Status: Not an admin{id_line}.\n"
            "You have standard climber access. Ask a league organizer if you need admin rights."
        )
        return

    categories_str = ", ".join(ROUTE_CATEGORIES)
    await update.effective_message.reply_text(
        f"🛡️ Admin Status: Active (Telegram ID: {user_id})\n"
        "You are an admin and have full admin rights in ClimbX League.\n\n"
        "Your admin rights include:\n"
        "• Route management: /addroute (with category, zone points & pictures), /addroutephoto, "
        "/setroutecategory, /setzonepoints, /activateroute, /deactivateroute, /routes all\n"
        "• Submission moderation: /pending, /submissions, /submission <id>, /approve <id>, "
        "/zone <id> [points], /reject <id> [reason], /reopen <id>, and inline review buttons\n"
        "• Registration moderation: /regrequests, /approvereg <id>, /rejectreg <id>\n"
        "• League operations: /export (CSV standings) and /broadcast <message>\n\n"
        f"Route categories: {categories_str}"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel any active multi-step conversation."""
    _clear_conversation_data(context)
    if update.effective_message:
        await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def cancel_idle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond cleanly when /cancel is used outside an active conversation."""
    _clear_conversation_data(context)
    if update.effective_message:
        await update.effective_message.reply_text("No active action to cancel.")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the caller's climber profile, rank, completed tops, zones, and pending climbs."""
    if not update.effective_user or not update.effective_message:
        return
    store = _store(context)
    is_admin = _is_admin(update)
    info = store.get_student_profile(update.effective_user.id)
    if not info:
        if is_admin:
            await update.effective_message.reply_text(
                "🛡️ Role: Admin (you have full admin rights — use /admin to view admin commands).\n\n"
                "You are not registered as a climber yet. Use /register if you also want to join the league!"
            )
        else:
            await update.effective_message.reply_text(
                "You are not registered yet. Use /register to join the league!"
            )
        return

    top_word = "top" if info["tops"] == 1 else "tops"
    zone_suffix = f", {info['zones']} zone" if info.get("zones", 0) > 0 else ""
    lines = [
        f"🧗 Climber Profile: {info['full_name']}",
        f"Username: @{info['username']}",
    ]
    if is_admin:
        lines.append("Role: 🛡️ Admin (full admin rights)")
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

    await _reply_chunked(update.effective_message, "\n".join(lines))


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the competition standings using climber usernames."""
    if not update.effective_message:
        return
    store = _store(context)
    rows = store.leaderboard_detailed()
    if not rows:
        await update.effective_message.reply_text("No scores yet.")
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

    await _reply_chunked(update.effective_message, "\n".join(lines))


async def export_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and upload the CSV league standings for admins."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
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
    )


async def _run_broadcast_text(
    message: Any, context: ContextTypes.DEFAULT_TYPE, raw_text: str
) -> None:
    announcement = f"📢 ClimbX League Announcement\n\n{raw_text.strip()}"
    if len(announcement) > MAX_TELEGRAM_MESSAGE_LENGTH:
        await message.reply_text(
            f"Announcement is too long ({len(announcement)}/{MAX_TELEGRAM_MESSAGE_LENGTH} chars)."
        )
        return

    students = _store(context).get_all_students()
    if not students:
        await message.reply_text("No registered students to broadcast to.")
        return

    sent_count = 0
    throttle = len(students) > 20
    for student in students:
        chat_id = int(student["telegram_id"])
        try:
            await context.bot.send_message(chat_id=chat_id, text=announcement)
            sent_count += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.1)
            try:
                await context.bot.send_message(chat_id=chat_id, text=announcement)
                sent_count += 1
            except Exception:
                LOGGER.warning("Failed to send broadcast to student %s after retry", chat_id)
        except Exception:
            LOGGER.warning("Failed to send broadcast to student %s", chat_id)
        if throttle:
            await asyncio.sleep(0.035)

    await message.reply_text(
        f"Announcement sent to {sent_count}/{len(students)} registered students."
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast an admin announcement via /broadcast <message>."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    parts = (update.effective_message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.effective_message.reply_text("Usage: /broadcast <message>")
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
    parts = data.split(":")

    if data == "gui:cmd:register":
        await query.answer()
        return await register_start(update, context)

    if data == "gui:cmd:submit":
        await query.answer()
        _clear_conversation_data(context)
        return await prompt_open_routes_for_submit(
            query.message, _store(context), update.effective_user.id
        )

    if data == "gui:cmd:addroute":
        if not _is_admin(update):
            await query.answer("Admin access required.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        return await _start_interactive_addroute(query.message, context, None)

    if len(parts) == 3 and parts[0] == "gui" and parts[1] == "addphoto":
        if not _is_admin(update):
            await query.answer("Admin access required.", show_alert=True)
            return ConversationHandler.END
        route_id = _parse_positive_int(parts[2])
        route = _store(context).get_route(route_id) if route_id else None
        if route is None:
            await query.answer("Route not found.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        return await start_addroutephoto_session(query.message, context, route)

    if len(parts) == 3 and parts[0] == "gui" and parts[1] == "setzone_route":
        if not _is_admin(update):
            await query.answer("Admin access required.", show_alert=True)
            return ConversationHandler.END
        route_id = _parse_positive_int(parts[2])
        route = _store(context).get_route(route_id) if route_id else None
        if route is None:
            await query.answer("Route not found.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        _clear_conversation_data(context)
        context.user_data["gui_action"] = "setzone"
        context.user_data["gui_route_id"] = route.id
        await query.message.reply_text(
            f"🔶 Enter zone points for Route #{route.id} '{route.name}' "
            f"(1 to {route.points - 1}, or 'none' to clear), or /cancel:"
        )
        return GUI_PROMPT_INPUT

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
    """Process single-turn text prompts started from GUI buttons (setzone, broadcast)."""
    if not update.effective_message:
        return ConversationHandler.END
    action = context.user_data.get("gui_action")
    text = (update.effective_message.text or "").strip()

    if action == "setzone":
        if not _is_admin(update):
            _clear_conversation_data(context)
            await update.effective_message.reply_text("Admin access required.")
            return ConversationHandler.END
        route_id = int(context.user_data.get("gui_route_id") or 0)
        store = _store(context)
        route = store.get_route(route_id)
        if route is None:
            _clear_conversation_data(context)
            await update.effective_message.reply_text("Route not found.")
            return ConversationHandler.END
        ok, msg = apply_zone_points_update(store, route_id, text)
        if not ok:
            await update.effective_message.reply_text(
                f"Please enter a whole number between 1 and {route.points - 1} (or 'none'), or /cancel."
            )
            return GUI_PROMPT_INPUT
        _clear_conversation_data(context)
        await update.effective_message.reply_text(msg)
        return ConversationHandler.END

    if action == "broadcast":
        if not _is_admin(update):
            _clear_conversation_data(context)
            await update.effective_message.reply_text("Admin access required.")
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
    """Handle GUI mode switches and button-driven command/picker callbacks."""
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    data = query.data or ""
    store = _store(context)
    user_id = update.effective_user.id
    is_admin = _is_admin(update)

    if data == "gui:toggle_mode":
        new_mode = not store.get_user_gui_mode(user_id)
        store.set_user_gui_mode(user_id, new_mode)
        if new_mode:
            await query.answer("Switched to GUI Mode.")
            await query.message.reply_text(
                "📱 Switched to GUI Mode! Use the buttons below (slash commands also work):",
                reply_markup=_gui_dashboard_keyboard(is_admin),
            )
        else:
            await query.answer("Switched to Raw Text Mode.")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.message.reply_text(
                "⌨️ Switched to Raw Text (Slash Command) Mode. GUI buttons are hidden; use slash commands directly or /mode to switch back.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📱 Switch to GUI Mode", callback_data="gui:toggle_mode")]]
                ),
            )
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
        await query.message.reply_text(msg)
        return

    if sub_action == "setcat_route":
        if not is_admin:
            await query.answer("Admin access required.", show_alert=True)
            return
        route_id = _parse_positive_int(parts[2])
        route = store.get_route(route_id) if route_id else None
        if route is None:
            await query.answer("Route not found.", show_alert=True)
            return
        await query.answer()
        kb = [
            [InlineKeyboardButton(cat, callback_data=f"gui:setcat_val:{route.id}:{idx}")]
            for idx, cat in enumerate(ROUTE_CATEGORIES)
        ]
        await query.message.reply_text(
            f"📍 Select category for Route #{route.id} ('{route.name}'):",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if sub_action == "setcat_val" and len(parts) == 4:
        if not is_admin:
            await query.answer("Admin access required.", show_alert=True)
            return
        route_id = _parse_positive_int(parts[2])
        try:
            cat = ROUTE_CATEGORIES[int(parts[3])]
        except (ValueError, IndexError):
            await query.answer("Invalid category.", show_alert=True)
            return
        route = store.get_route(route_id) if route_id else None
        if route is None:
            await query.answer("Route not found.", show_alert=True)
            return
        store.set_route_category(route.id, cat)
        await query.answer(f"Category set to {cat}.")
        await query.message.reply_text(
            f"Route #{route.id} ('{route.name}') category set to {cat}."
        )
        return

    if sub_action == "toggle":
        if not is_admin:
            await query.answer("Admin access required.", show_alert=True)
            return
        route_id = _parse_positive_int(parts[2])
        route = store.get_route(route_id) if route_id else None
        if route is None:
            await query.answer("Route not found.", show_alert=True)
            return
        new_active = not route.active
        store.set_route_active(route.id, new_active)
        state_label = "reactivated" if new_active else "deactivated"
        await query.answer(f"Route #{route.id} {state_label}.")
        await query.message.reply_text(
            f"Route #{route.id} ('{route.name}') has been {state_label}."
        )
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
            "admin": admin_status,
            "help": help_command,
        }
        if cmd in student_cmds:
            await query.answer()
            await student_cmds[cmd](update, context)
            return

        if cmd == "withdraw":
            await query.answer()
            info = store.get_student_profile(user_id)
            if not info:
                await query.message.reply_text("Please register first with /register.")
                return
            pend = info["pending_routes"]
            if not pend:
                await query.message.reply_text("You have no pending submissions to withdraw.")
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
            await query.message.reply_text(
                "Select a pending submission to withdraw:",
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return

        if not is_admin:
            await query.answer("Admin access required.", show_alert=True)
            return

        if cmd in {"addroutephoto", "setcategory", "setzone", "toggleroute"}:
            await query.answer()
            all_routes = store.list_routes(active_only=False)
            if not all_routes:
                await query.message.reply_text("No routes have been published yet.")
                return
            prefix_map = {
                "addroutephoto": ("📸 Select a route to add picture(s):", "gui:addphoto"),
                "setcategory": ("📍 Select a route to set its category:", "gui:setcat_route"),
                "setzone": ("🔶 Select a route to configure zone points:", "gui:setzone_route"),
                "toggleroute": ("🔄 Select a route to activate/deactivate:", "gui:toggle"),
            }
            prompt_text, cb_prefix = prefix_map[cmd]
            kb = [
                [
                    InlineKeyboardButton(
                        f"#{r.id} {r.name} ({'Active' if r.active else 'Inactive'})",
                        callback_data=f"{cb_prefix}:{r.id}",
                    )
                ]
                for r in all_routes[:25]
            ]
            await query.message.reply_text(prompt_text, reply_markup=InlineKeyboardMarkup(kb))
            return

        if cmd == "submission":
            await query.answer()
            subs = store.all_submissions(status_filter="all")
            if not subs:
                await query.message.reply_text("No submissions found.")
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
            await query.message.reply_text(
                "🔎 Select a submission to view its video & review card:",
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return

        admin_cmds = {
            "pending": pending,
            "submissions": all_submissions_cmd,
            "regrequests": regrequests_cmd,
            "export": export_results,
        }
        if cmd in admin_cmds:
            await query.answer()
            await admin_cmds[cmd](update, context)
            return

    await query.answer()
