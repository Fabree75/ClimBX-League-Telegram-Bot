"""Role-separated GUI menus, /rules, /commands, /role, profile, leaderboard, routesetter management, logs, export, and broadcast."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import sqlite3
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import ContextTypes, ConversationHandler

from common import (
    ADDROUTESETTER_INPUT,
    GUI_PROMPT_INPUT,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    ROUTE_CATEGORIES,
    _admin_ids,
    _admin_menu_keyboard,
    _can_manage_routes,
    _clear_conversation_data,
    _climber_menu_keyboard,
    _compute_competition_ranks,
    _is_admin,
    _is_registered,
    _is_routesetter,
    _log_action,
    _menu_button_markup,
    _parse_positive_int,
    _reply_chunked,
    _routesetter_ids,
    _routesetter_menu_keyboard,
    _send_role_menus,
    _store,
    _unregistered_menu_keyboard,
    _with_menu_button,
)
from handlers.challenges import (
    _start_interactive_addchallenge,
    all_challenge_submissions_cmd,
    challenge_info_cmd,
    challenges_cmd,
    perform_withdraw_challenge,
    prompt_open_challenges_for_submit,
    start_editchallenge_picker,
    view_challenge_submission_cmd,
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

LOGS_PAGE_SIZE = 10


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome overview and role-separated GUI menus."""
    if not update.effective_message:
        return
    is_admin = _is_admin(update)
    is_setter = _is_routesetter(update, context)
    is_registered = _is_registered(update, context)

    if is_registered:
        text = (
            "🧗 Welcome to the ClimbX League at École Polytechnique!\n\n"
            "You can interact using either the GUI buttons below or slash commands at any time:\n"
            "• /profile & /editprofile - View or update your climber profile\n"
            "• /routes & /route <id> - Browse climbing routes & pictures\n"
            "• /challenges & /challenge <id> - Browse climbing challenges\n"
            "• /submit <id> & /submitchallenge <id> - Submit proof for routes or challenges\n"
            "• /withdraw <id> & /withdrawchallenge <id> - Withdraw a pending submission\n"
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
            "• /challenges & /challenge <id> - Browse climbing challenges\n"
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
    if _can_manage_routes(update, context):
        await update.effective_message.reply_text(
            "🔧 Routesetter Menu — manage climbing routes & challenges:",
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
        "2. Check active routes with /routes (or filter by location: Arkose, ENSTA gym, Pan, Outdoors) and bonus challenges with /challenges.\n"
        "3. Inspect full details &amp; pictures with <code>/route &lt;route_id&gt;</code> or <code>/challenge &lt;challenge_id&gt;</code>.\n"
        "4. Submit proof with <code>/submit &lt;route_id&gt;</code> (video) or <code>/submitchallenge &lt;challenge_id&gt;</code> (video or photo).\n"
        "5. Once an admin approves your submission, points are added to /leaderboard!\n\n"
        "<b>Scoring, Partial (Zone) Points &amp; Challenges:</b>\n"
        "• <b>Top</b>: Awards the full route points.\n"
        "• <b>Zone</b>: Routes worth 2+ points may have an established Zone hold awarding partial points. "
        "If you reach the Zone and later top the route, your score upgrades to the full Top points.\n"
        "• <b>Challenges</b>: Special tasks published by routesetters/admins awarding bonus points once approved.\n\n"
        "<b>Video Proof Requirements (Routes):</b>\n"
        "• <b>Start</b>: Establish a controlled start with both hands on the starting holds.\n"
        "• <b>Ascent</b>: Complete the route using only allowed holds (no off-route dabs).\n"
        "• <b>Finish</b>: Match both hands on the top hold with control for 2 seconds (or control the Zone hold for partial points).\n"
        "• <b>Format</b>: Routes require a video (photos are not accepted for routes). Challenges accept both videos and photos.\n\n"
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
            "• /profile - View your personal stats, routes, and challenges\n"
            "• /editprofile - Edit your username or request a full name/email change\n"
            "• <code>/routes [category]</code> - List active routes and your status (✅/🔶/⏳)\n"
            "• <code>/route [route_id]</code> - View full route info &amp; pictures\n"
            "• /challenges - List active challenges and your status (✅/⏳)\n"
            "• <code>/challenge [challenge_id]</code> - View full challenge info &amp; pictures\n"
            "• <code>/submit [route_id]</code> - Submit video proof for a route\n"
            "• <code>/submitchallenge [challenge_id]</code> - Submit video or photo proof for a challenge\n"
            "• <code>/withdraw &lt;route_id&gt;</code> - Cancel a pending route submission\n"
            "• <code>/withdrawchallenge &lt;challenge_id&gt;</code> - Cancel a pending challenge submission\n"
            "• /leaderboard - View competition rankings\n"
            "• /role - View your current role &amp; permissions\n"
            "• /rules - View competition rules &amp; proof requirements\n"
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
            "• /challenges - Browse active climbing challenges\n"
            "• <code>/challenge [challenge_id]</code> - View full challenge info &amp; pictures\n"
            "• /leaderboard - View competition rankings\n"
            "• /role - View your current role &amp; permissions\n"
            "• /rules - View competition rules &amp; proof requirements\n"
            "• /commands - View this command reference\n"
            "• /cancel - Cancel an ongoing action"
        )
    if _can_manage_routes(update, context):
        text += (
            "\n\n<b>🔧 Routesetter Commands:</b>\n"
            "• <code>/addroute</code> - Guided route creation (category, name, points, zone, pictures)\n"
            "• <code>/addroute &lt;name&gt; &lt;points&gt; [category]</code> - Quick route publish (attach photo optionally)\n"
            "• <code>/editroute [route_id]</code> - Edit route fields, pictures, or active status\n"
            "• <code>/routes all [category]</code> - View all routes (including inactive)\n"
            "• <code>/addchallenge</code> - Guided challenge creation (name, description, points, pictures)\n"
            "• <code>/addchallenge &lt;name&gt; &lt;points&gt;</code> - Quick challenge publish\n"
            "• <code>/editchallenge [challenge_id]</code> - Edit challenge fields, pictures, or active status\n"
            "• <code>/challenges all</code> - View all challenges (including inactive)"
        )
    if _is_admin(update):
        text += (
            "\n\n<b>🛡️ Admin Commands:</b>\n"
            "• /pending - Interactive review GUI for pending route &amp; challenge submissions\n"
            "• <code>/submissions [all|pending|approved|zone|rejected]</code> - List route submissions\n"
            "• <code>/submission &lt;id&gt;</code> - View route submission &amp; inline review buttons\n"
            "• <code>/approve &lt;id&gt;</code> / <code>/zone &lt;id&gt;</code> / <code>/reject &lt;id&gt; [reason]</code> / <code>/reopen &lt;id&gt;</code>\n"
            "• <code>/challengesubmissions [all|pending|approved|rejected]</code> - List challenge submissions\n"
            "• <code>/challengesubmission &lt;id&gt;</code> - View challenge submission &amp; inline review buttons\n"
            "• <code>/approvechallenge &lt;id&gt;</code> / <code>/rejectchallenge &lt;id&gt; [reason]</code> / <code>/reopenchallenge &lt;id&gt;</code>\n"
            "• /routesetters, <code>/addroutesetter &lt;id&gt;</code>, <code>/removeroutesetter &lt;id&gt;</code> - Manage routesetter roles\n"
            "• <code>/logs [all|climber|routesetter|admin|user_id]</code> - View audit logs of past actions\n"
            "• /profilerequests, <code>/approvereq &lt;id&gt;</code>, <code>/rejectreq &lt;id&gt; [reason]</code>\n"
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
            "• Routesetter tools: /addroute, /editroute, /routes all, /addchallenge, /editchallenge (including delete), /challenges all\n"
            "• Submission moderation: /pending, /submissions, /submission <id>, /approve, /zone, /reject, /reopen, "
            "/challengesubmissions, /challengesubmission <id>, /approvechallenge, /rejectchallenge, /reopenchallenge\n"
            "• Role & audit management: /routesetters, /addroutesetter, /removeroutesetter, /logs\n"
            "• Profile moderation: /profilerequests, /approvereq <id>, /rejectreq <id> [reason]\n"
            "• League operations: /export (CSV standings) and /broadcast <message>\n\n"
            f"Route categories: {categories_str}",
            reply_markup=_menu_button_markup(),
        )
        return

    if _is_routesetter(update, context):
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
            "• Manage routes: /addroute, /editroute [route_id], /routes all [category]\n"
            "• Manage challenges: /addchallenge, /editchallenge [challenge_id] (edit & activate/deactivate), /challenges all\n\n"
            f"Route categories: {categories_str}",
            reply_markup=_menu_button_markup(),
        )
        return

    if is_registered:
        await update.effective_message.reply_text(
            f"🧗 Role: Climber{id_line}\n"
            "You are a registered climber. You can view your profile with /profile, edit it with /editprofile, "
            "browse routes (/routes, /route) and challenges (/challenges, /challenge), submit proofs (/submit, /submitchallenge), "
            "and view /leaderboard.\n"
            "Ask a league organizer if you need routesetter or admin rights.",
            reply_markup=_menu_button_markup(),
        )
        return

    await update.effective_message.reply_text(
        f"👤 Role: Unregistered{id_line}\n"
        "You are not registered in ClimbX League yet.\n\n"
        "You can currently:\n"
        "• Join the league with /register (unlocks Climber features: /profile, /editprofile, /submit, /submitchallenge, /withdraw)\n"
        "• Browse routes (/routes, /route) & challenges (/challenges, /challenge), view /leaderboard, read /rules, and check /commands",
        reply_markup=_with_menu_button(
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("📝 Register", callback_data="gui:cmd:register")]]
            )
        ),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel any active multi-step conversation."""
    _clear_conversation_data(context)
    if update.effective_message:
        await update.effective_message.reply_text(
            "Cancelled.", reply_markup=_menu_button_markup()
        )
    return ConversationHandler.END


async def cancel_idle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond cleanly when /cancel is used outside an active conversation."""
    _clear_conversation_data(context)
    if update.effective_message:
        await update.effective_message.reply_text(
            "No active action to cancel.", reply_markup=_menu_button_markup()
        )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the caller's climber profile, rank, completed routes, zones, challenges, and pending items."""
    if not update.effective_user or not update.effective_message:
        return
    store = _store(context)
    is_admin = _is_admin(update)
    is_setter = _is_routesetter(update, context)
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
    chal_count = int(info.get("challenges", 0))
    chal_word = "challenge" if chal_count == 1 else "challenges"
    chal_suffix = f", {chal_count} {chal_word}" if chal_count > 0 else ""
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
            f"Score: {info['points']} pts ({info['tops']} {top_word}{zone_suffix}{chal_suffix})",
        ]
    )

    if info["approved_routes"]:
        lines.append("\n✅ Completed Routes:")
        for r in info["approved_routes"]:
            lines.append(f"• #{r['id']} {r['name']} ({r['points']} pts)")
    elif not info.get("zone_routes") and not info.get("approved_challenges"):
        lines.append("\nNo completed routes yet. Check /routes and /submit!")

    if info.get("zone_routes"):
        lines.append("\n🔶 Zone Routes (Partial Points):")
        for r in info["zone_routes"]:
            lines.append(f"• #{r['id']} {r['name']} ({r['points']} pts)")

    if info.get("approved_challenges"):
        lines.append("\n🎯 Completed Challenges:")
        for c in info["approved_challenges"]:
            lines.append(f"• #{c['id']} {c['name']} ({c['points']} pts)")

    if info["pending_routes"]:
        lines.append("\n⏳ Pending Route Reviews:")
        for r in info["pending_routes"]:
            lines.append(f"• #{r['route_id']} {r['name']} ({r['points']} pts)")

    if info.get("pending_challenges"):
        lines.append("\n⏳ Pending Challenge Reviews:")
        for c in info["pending_challenges"]:
            lines.append(f"• #{c['challenge_id']} {c['name']} ({c['points']} pts)")

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
        challenges = int(row["challenges"]) if "challenges" in row.keys() else 0
        top_word = "top" if tops == 1 else "tops"
        zone_part = f", {zones} zone" if zones > 0 else ""
        chal_word = "challenge" if challenges == 1 else "challenges"
        chal_part = f", {challenges} {chal_word}" if challenges > 0 else ""
        lines.append(
            f"{prefix}{rank}. {display_name} - {points} pts ({tops} {top_word}{zone_part}{chal_part})"
        )
        if caller_id is not None and int(row["telegram_id"]) == caller_id:
            caller_summary = (
                f"\nYour rank: #{rank} of {len(rows)} ({points} pts, {tops} {top_word}{zone_part}{chal_part})"
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
            "challenges",
            "completed_routes",
            "completed_challenges",
        ],
    )
    writer.writeheader()
    writer.writerows(rows)
    payload = io.BytesIO(buf.getvalue().encode("utf-8"))
    payload.name = "climbx_standings.csv"
    _log_action(
        update,
        context,
        "export_csv",
        details=f"Exported standings CSV ({len(rows)} climbers)",
    )
    await update.effective_message.reply_document(
        document=payload,
        filename="climbx_standings.csv",
        caption=f"ClimbX League standings ({len(rows)} registered climbers).",
        reply_markup=_menu_button_markup(),
    )


async def _run_broadcast_text(
    message: Any, update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str
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

    preview = raw_text.strip()
    if len(preview) > 80:
        preview = preview[:79] + "…"
    _log_action(
        update,
        context,
        "broadcast",
        details=f"Sent to {sent_count}/{len(students)} climbers: {preview}",
    )
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
    await _run_broadcast_text(update.effective_message, update, context, parts[1])


# ---------------------------------------------------------------------------
# Admin Routesetter Role Management (DB primary + .env ROUTESETTER_IDS fallback)
# ---------------------------------------------------------------------------


def _format_routesetters_panel(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str, InlineKeyboardMarkup]:
    store = _store(context)
    db_rows = store.list_routesetters()
    env_ids = _routesetter_ids()
    db_id_set = {int(r["telegram_id"]) for r in db_rows}
    env_only_ids = sorted(env_ids - db_id_set)

    lines = ["🔧 Routesetters Management", ""]
    kb: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Add Routesetter", callback_data="gui:cmd:addroutesetter")]
    ]

    if not db_rows and not env_only_ids:
        lines.append("No routesetters configured yet (admins always have routesetter access).")
    else:
        for r in db_rows:
            tid = int(r["telegram_id"])
            if r["full_name"]:
                u_part = f" (@{r['username']})" if r["username"] else ""
                label = f"{r['full_name']}{u_part} — ID {tid}"
                btn_label = f"🗑️ Remove {r['full_name']} ({tid})"
            else:
                label = f"ID {tid} (unregistered)"
                btn_label = f"🗑️ Remove ID {tid}"
            env_note = " [also in .env fallback]" if tid in env_ids else ""
            lines.append(f"• {label}{env_note}")
            kb.append(
                [InlineKeyboardButton(btn_label[:56], callback_data=f"rs:remove:{tid}")]
            )
        for tid in env_only_ids:
            st = store.get_student(tid)
            if st:
                label = f"{st['full_name']} — ID {tid}"
            else:
                label = f"ID {tid}"
            lines.append(f"• {label} [.env fallback — edit .env to remove]")

    lines.append(
        "\nUse /addroutesetter <telegram_id> or /removeroutesetter <telegram_id>, or the buttons below."
    )
    kb.append([InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def routesetters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List and manage routesetters (/routesetters) for admins."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    text, markup = _format_routesetters_panel(context)
    await _reply_chunked(update.effective_message, text, reply_markup=markup)


async def _grant_routesetter_role(
    message: Any, update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int
) -> None:
    store = _store(context)
    if target_id in _admin_ids():
        await message.reply_text(
            f"User {target_id} is already an Admin (admins already have full routesetter permissions).",
            reply_markup=_menu_button_markup(),
        )
        return
    added = store.add_routesetter(
        target_id, added_by=update.effective_user.id if update.effective_user else None
    )
    if not added:
        await message.reply_text(
            f"User {target_id} is already a Routesetter.",
            reply_markup=_menu_button_markup(),
        )
        return
    student = store.get_student(target_id)
    target_label = f"{student['full_name']} (ID {target_id})" if student else f"ID {target_id}"
    _log_action(
        update,
        context,
        "add_routesetter",
        target_type="user",
        target_id=target_id,
        details=f"Granted routesetter role to {target_label}",
    )
    await message.reply_text(
        f"✅ Granted Routesetter role to {target_label}.",
        reply_markup=_menu_button_markup(),
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "🔧 You have been granted the Routesetter role in ClimbX League!\n"
                "Open /menu or run /role to see your routesetter tools."
            ),
            reply_markup=_menu_button_markup(),
        )
    except Exception:
        LOGGER.debug("Could not notify user %s about new routesetter role", target_id)


async def _revoke_routesetter_role(
    message: Any, update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int
) -> None:
    store = _store(context)
    removed = store.remove_routesetter(target_id)
    in_env = target_id in _routesetter_ids()
    if not removed:
        if in_env:
            await message.reply_text(
                f"User {target_id} is configured via the .env ROUTESETTER_IDS fallback (not in the bot database). "
                "Remove them from .env to revoke their role.",
                reply_markup=_menu_button_markup(),
            )
        else:
            await message.reply_text(
                f"User {target_id} is not a Routesetter.",
                reply_markup=_menu_button_markup(),
            )
        return

    student = store.get_student(target_id)
    target_label = f"{student['full_name']} (ID {target_id})" if student else f"ID {target_id}"
    _log_action(
        update,
        context,
        "remove_routesetter",
        target_type="user",
        target_id=target_id,
        details=f"Revoked routesetter role from {target_label}",
    )
    env_warning = (
        "\n⚠️ Note: This user is still listed in .env ROUTESETTER_IDS fallback."
        if in_env
        else ""
    )
    await message.reply_text(
        f"🗑️ Removed Routesetter role from {target_label}.{env_warning}",
        reply_markup=_menu_button_markup(),
    )
    if not in_env:
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="ℹ️ Your Routesetter role in ClimbX League has been removed by an admin.",
                reply_markup=_menu_button_markup(),
            )
        except Exception:
            LOGGER.debug("Could not notify user %s about removed routesetter role", target_id)


async def addroutesetter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grant routesetter role via /addroutesetter <telegram_id>."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    target_id = (
        _parse_positive_int(context.args[0])
        if (context.args and len(context.args) == 1)
        else None
    )
    if target_id is None:
        await update.effective_message.reply_text(
            "Usage: /addroutesetter <telegram_id>", reply_markup=_menu_button_markup()
        )
        return
    await _grant_routesetter_role(update.effective_message, update, context, target_id)


async def removeroutesetter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke routesetter role via /removeroutesetter <telegram_id>."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    target_id = (
        _parse_positive_int(context.args[0])
        if (context.args and len(context.args) == 1)
        else None
    )
    if target_id is None:
        await update.effective_message.reply_text(
            "Usage: /removeroutesetter <telegram_id>", reply_markup=_menu_button_markup()
        )
        return
    await _revoke_routesetter_role(update.effective_message, update, context, target_id)


async def _start_addroutesetter_flow(
    message: Any, context: ContextTypes.DEFAULT_TYPE
) -> int:
    _clear_conversation_data(context)
    store = _store(context)
    admins = _admin_ids()
    existing_rs = {int(r["telegram_id"]) for r in store.list_routesetters()}
    candidates = [
        st
        for st in store.get_all_students()
        if int(st["telegram_id"]) not in admins and int(st["telegram_id"]) not in existing_rs
    ]
    kb: list[list[InlineKeyboardButton]] = []
    for st in candidates[:20]:
        tid = int(st["telegram_id"])
        u_part = f" (@{st['username']})" if st["username"] else ""
        kb.append(
            [
                InlineKeyboardButton(
                    f"➕ {st['full_name']}{u_part} ({tid})"[:56],
                    callback_data=f"rs:add:{tid}",
                )
            ]
        )
    kb.append([InlineKeyboardButton("Cancel ❌", callback_data="rs:cancel")])
    prompt = (
        "🔧 Select a registered climber below to make them a Routesetter, "
        "or send any user's numeric Telegram ID (or /cancel):"
        if candidates
        else "🔧 Send the numeric Telegram ID of the user to grant the Routesetter role (or /cancel):"
    )
    await message.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb))
    return ADDROUTESETTER_INPUT


async def addroutesetter_pick_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle picking a candidate climber or cancelling during ADDROUTESETTER_INPUT."""
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return ConversationHandler.END
    data = query.data or ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if data == "rs:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        await query.message.reply_text("Cancelled.", reply_markup=_menu_button_markup())
        return ConversationHandler.END
    parts = data.split(":")
    target_id = _parse_positive_int(parts[2]) if len(parts) == 3 and parts[1] == "add" else None
    if target_id is None:
        await query.answer("Invalid user ID.", show_alert=True)
        return ADDROUTESETTER_INPUT
    _clear_conversation_data(context)
    await query.answer()
    await _grant_routesetter_role(query.message, update, context, target_id)
    return ConversationHandler.END


async def addroutesetter_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle typed Telegram ID during ADDROUTESETTER_INPUT."""
    if not update.effective_message:
        return ConversationHandler.END
    if not _is_admin(update):
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END
    target_id = _parse_positive_int(update.effective_message.text or "")
    if target_id is None:
        await update.effective_message.reply_text(
            "Please enter a valid numeric Telegram ID (e.g. 123456789), or /cancel."
        )
        return ADDROUTESETTER_INPUT
    _clear_conversation_data(context)
    await _grant_routesetter_role(update.effective_message, update, context, target_id)
    return ConversationHandler.END


async def routesetter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline rs:remove:<id> clicks on the Routesetters panel."""
    query = update.callback_query
    if not query or not query.message:
        return
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) == 3 and parts[0] == "rs" and parts[1] == "remove":
        target_id = _parse_positive_int(parts[2])
        if target_id is None:
            await query.answer("Invalid user ID.", show_alert=True)
            return
        await query.answer()
        await _revoke_routesetter_role(query.message, update, context, target_id)
        try:
            text, markup = _format_routesetters_panel(context)
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
        return
    await query.answer()


# ---------------------------------------------------------------------------
# Admin Audit Logs Viewer (/logs)
# ---------------------------------------------------------------------------


_ROLE_BADGES = {
    "admin": "🛡️",
    "routesetter": "🔧",
    "climber": "🧗",
    "unregistered": "👤",
}


def _format_logs_page(
    store: Any,
    role_filter: str = "all",
    page: int = 0,
    actor_id: Optional[int] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    total = store.count_audit_logs(role_filter=role_filter, actor_id=actor_id)
    total_pages = max(1, (total + LOGS_PAGE_SIZE - 1) // LOGS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = store.list_audit_logs(
        role_filter=role_filter,
        actor_id=actor_id,
        limit=LOGS_PAGE_SIZE,
        offset=page * LOGS_PAGE_SIZE,
    )

    filter_label = (
        f"User {actor_id}"
        if actor_id is not None
        else role_filter.capitalize()
    )
    header = f"📜 Audit Logs — {filter_label} (Page {page + 1}/{total_pages} · {total} entries)"
    lines = [header, ""]
    if not rows:
        lines.append("No log entries found for this filter.")
    else:
        for r in rows:
            badge = _ROLE_BADGES.get(str(r["actor_role"]), "•")
            ts = str(r["created_at"])[:16]
            detail_part = f" — {r['details']}" if r["details"] else ""
            lines.append(
                f"[{ts}] {badge} {r['actor_name']} (ID {r['actor_id']}) · {r['action']}{detail_part}"
            )

    def _fbtn(label: str, key: str) -> InlineKeyboardButton:
        marker = "✓ " if (actor_id is None and role_filter == key) else ""
        return InlineKeyboardButton(f"{marker}{label}", callback_data=f"logs:filter:{key}:0")

    kb: list[list[InlineKeyboardButton]] = [
        [
            _fbtn("All", "all"),
            _fbtn("🧗 Climbers", "climber"),
        ],
        [
            _fbtn("🔧 Routesetters", "routesetter"),
            _fbtn("🛡️ Admins", "admin"),
        ],
    ]
    if actor_id is None and total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    "⬅️ Prev", callback_data=f"logs:filter:{role_filter}:{page - 1}"
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                f"Page {page + 1}/{total_pages}",
                callback_data=f"logs:filter:{role_filter}:{page}",
            )
        )
        if page + 1 < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    "Next ➡️", callback_data=f"logs:filter:{role_filter}:{page + 1}"
                )
            )
        kb.append(nav_row)

    export_scope = role_filter if actor_id is None else "all"
    kb.append(
        [
            InlineKeyboardButton(
                "🔄 Refresh", callback_data=f"logs:filter:{export_scope}:{page}"
            ),
            InlineKeyboardButton(
                "📥 Export CSV", callback_data=f"logs:export:{export_scope}"
            ),
        ]
    )
    kb.append([InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display filterable audit logs of past actions for admins (/logs [role|user_id])."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    role_filter = "all"
    actor_id: Optional[int] = None
    if context.args:
        arg = context.args[0].strip().lower()
        role_aliases = {
            "all": "all",
            "climber": "climber",
            "climbers": "climber",
            "routesetter": "routesetter",
            "routesetters": "routesetter",
            "setter": "routesetter",
            "admin": "admin",
            "admins": "admin",
            "unregistered": "unregistered",
        }
        if arg in role_aliases:
            role_filter = role_aliases[arg]
        else:
            parsed_uid = _parse_positive_int(arg)
            if parsed_uid is None:
                await update.effective_message.reply_text(
                    "Usage: /logs [all|climber|routesetter|admin|<telegram_id>]",
                    reply_markup=_menu_button_markup(),
                )
                return
            actor_id = parsed_uid

    text, markup = _format_logs_page(
        _store(context), role_filter=role_filter, page=0, actor_id=actor_id
    )
    await _reply_chunked(update.effective_message, text, reply_markup=markup)


async def logs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle role filtering, pagination, refresh, and CSV export in /logs GUI."""
    query = update.callback_query
    if not query or not query.message:
        return
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    store = _store(context)

    if len(parts) == 4 and parts[0] == "logs" and parts[1] == "filter":
        role_filter = parts[2]
        if role_filter not in {"all", "climber", "routesetter", "admin"}:
            await query.answer("Invalid filter.", show_alert=True)
            return
        if not parts[3].isascii() or not parts[3].isdigit():
            await query.answer("Invalid page.", show_alert=True)
            return
        page = int(parts[3])
        await query.answer()
        text, markup = _format_logs_page(store, role_filter=role_filter, page=page)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            await query.message.reply_text(text, reply_markup=markup)
        return

    if len(parts) == 3 and parts[0] == "logs" and parts[1] == "export":
        role_filter = parts[2]
        if role_filter not in {"all", "climber", "routesetter", "admin"}:
            await query.answer("Invalid export filter.", show_alert=True)
            return
        await query.answer("Generating logs CSV...")
        rows = store.get_all_audit_logs(role_filter=role_filter)
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[
                "id",
                "created_at",
                "actor_id",
                "actor_name",
                "actor_role",
                "action",
                "target_type",
                "target_id",
                "details",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))
        payload = io.BytesIO(buf.getvalue().encode("utf-8"))
        payload.name = "climbx_audit_logs.csv"
        await query.message.reply_document(
            document=payload,
            filename="climbx_audit_logs.csv",
            caption=f"ClimbX League audit logs ({len(rows)} entries, filter: {role_filter}).",
            reply_markup=_menu_button_markup(),
        )
        return

    await query.answer("Invalid log action.", show_alert=True)


# ---------------------------------------------------------------------------
# GUI Entry & Callback Routers
# ---------------------------------------------------------------------------


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

    if data == "gui:cmd:submitchallenge":
        await query.answer()
        _clear_conversation_data(context)
        return await prompt_open_challenges_for_submit(
            query.message, _store(context), update.effective_user.id
        )

    if data == "gui:cmd:addroute":
        if not _can_manage_routes(update, context):
            await query.answer("Routesetter or admin access required.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        return await _start_interactive_addroute(query.message, context, None)

    if data == "gui:cmd:editroute":
        if not _can_manage_routes(update, context):
            await query.answer("Routesetter or admin access required.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        return await start_editroute_picker(query.message, context)

    if data == "gui:cmd:addchallenge":
        if not _can_manage_routes(update, context):
            await query.answer("Routesetter or admin access required.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        return await _start_interactive_addchallenge(query.message, context, None)

    if data == "gui:cmd:editchallenge":
        if not _can_manage_routes(update, context):
            await query.answer("Routesetter or admin access required.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        return await start_editchallenge_picker(query.message, context)

    if data == "gui:cmd:addroutesetter":
        if not _is_admin(update):
            await query.answer("Admin access required.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        return await _start_addroutesetter_flow(query.message, context)

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
        await _run_broadcast_text(update.effective_message, update, context, text)
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
        _log_action(
            update,
            context,
            "withdraw_route",
            target_type="route",
            target_id=route_id,
            details=msg,
        )
        await query.answer("Submission withdrawn.")
        await query.message.reply_text(msg, reply_markup=_menu_button_markup())
        return

    if sub_action == "withdraw_challenge":
        chal_id = _parse_positive_int(parts[2])
        if chal_id is None:
            await query.answer("Invalid challenge.", show_alert=True)
            return
        ok, msg = await perform_withdraw_challenge(context.bot, store, user_id, chal_id)
        if not ok:
            await query.answer("No pending submission for that challenge.", show_alert=True)
            return
        _log_action(
            update,
            context,
            "withdraw_challenge",
            target_type="challenge",
            target_id=chal_id,
            details=msg,
        )
        await query.answer("Challenge submission withdrawn.")
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

    if sub_action == "viewcsub":
        if not is_admin:
            await query.answer("Admin access required.", show_alert=True)
            return
        sub_id = _parse_positive_int(parts[2])
        sub = store.get_challenge_submission(sub_id) if sub_id else None
        if sub is None:
            await query.answer("Challenge submission not found.", show_alert=True)
            return
        await query.answer()
        context.args = [str(sub_id)]
        await view_challenge_submission_cmd(update, context)
        return

    if sub_action == "cmd":
        cmd = parts[2]
        context.args = []
        student_cmds = {
            "routes": routes,
            "route": route_info,
            "challenges": challenges_cmd,
            "challenge": challenge_info_cmd,
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
            pend_routes = info["pending_routes"]
            pend_chals = info.get("pending_challenges", [])
            if not pend_routes and not pend_chals:
                await query.message.reply_text(
                    "You have no pending submissions to withdraw.",
                    reply_markup=_menu_button_markup(),
                )
                return
            kb = [
                [
                    InlineKeyboardButton(
                        f"🧗 Withdraw Route #{r['route_id']} {r['name']}",
                        callback_data=f"gui:withdraw:{r['route_id']}",
                    )
                ]
                for r in pend_routes
            ]
            kb.extend(
                [
                    [
                        InlineKeyboardButton(
                            f"🎯 Withdraw Challenge #{c['challenge_id']} {c['name']}",
                            callback_data=f"gui:withdraw_challenge:{c['challenge_id']}",
                        )
                    ]
                    for c in pend_chals
                ]
            )
            kb.append([InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu")])
            await query.message.reply_text(
                "Select a pending submission to withdraw:",
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return

        if cmd == "routes_all":
            if not _can_manage_routes(update, context):
                await query.answer("Routesetter or admin access required.", show_alert=True)
                return
            await query.answer()
            context.args = ["all"]
            await routes(update, context)
            return

        if cmd == "challenges_all":
            if not _can_manage_routes(update, context):
                await query.answer("Routesetter or admin access required.", show_alert=True)
                return
            await query.answer()
            context.args = ["all"]
            await challenges_cmd(update, context)
            return

        if not is_admin:
            await query.answer("Admin access required.", show_alert=True)
            return

        if cmd == "submission":
            await query.answer()
            subs = store.all_submissions(status_filter="all")
            csubs = store.all_challenge_submissions(status_filter="all")
            if not subs and not csubs:
                await query.message.reply_text(
                    "No submissions found.",
                    reply_markup=_menu_button_markup(),
                )
                return
            kb = [
                [
                    InlineKeyboardButton(
                        f"🧗 #{s['id']} {s['full_name']} - {s['route_name']} ({s['status']})"[:56],
                        callback_data=f"gui:viewsub:{s['id']}",
                    )
                ]
                for s in subs[:15]
            ]
            kb.extend(
                [
                    [
                        InlineKeyboardButton(
                            f"🎯 #{c['id']} {c['full_name']} - {c['challenge_name']} ({c['status']})"[:56],
                            callback_data=f"gui:viewcsub:{c['id']}",
                        )
                    ]
                    for c in csubs[:10]
                ]
            )
            kb.append([InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu")])
            await query.message.reply_text(
                "🔎 Select a route or challenge submission to view its proof & review card:",
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return

        admin_cmds = {
            "pending": pending,
            "submissions": all_submissions_cmd,
            "challengesubmissions": all_challenge_submissions_cmd,
            "profilerequests": regrequests_cmd,
            "regrequests": regrequests_cmd,
            "routesetters": routesetters_cmd,
            "logs": logs_cmd,
            "export": export_results,
        }
        if cmd in admin_cmds:
            await query.answer()
            await admin_cmds[cmd](update, context)
            return

    await query.answer()
