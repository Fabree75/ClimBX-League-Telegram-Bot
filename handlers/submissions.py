"""Proof submission, withdrawal, admin listing, preset-zone review, and reopening handlers."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from common import (
    REVIEW_REJECT_REASON,
    SUBMIT_PROOF,
    SUBMIT_ROUTE,
    _admin_ids,
    _clear_conversation_data,
    _extract_video_info,
    _is_admin,
    _log_action,
    _menu_button_markup,
    _parse_positive_int,
    _post_review_nav_markup,
    _reply_chunked,
    _sanitize_single_line,
    _store,
)
from handlers.challenges import send_pending_challenge_review_card
from store import LeagueStore, Route

LOGGER = logging.getLogger(__name__)

PENDING_PAGE_SIZE = 8


def _truncate_button_label(text: str, max_len: int = 56) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _unified_pending_items(store: LeagueStore) -> list[dict[str, Any]]:
    """Return all pending route and challenge submissions merged chronologically."""
    items: list[dict[str, Any]] = []
    for row in store.pending_submissions():
        items.append(
            {
                "kind": "route",
                "id": int(row["id"]),
                "submitted_at": str(row["submitted_at"] or ""),
                "full_name": str(row["full_name"]),
                "title": str(row["name"]),
                "points": int(row["points"]),
                "row": row,
            }
        )
    for row in store.pending_challenge_submissions():
        items.append(
            {
                "kind": "challenge",
                "id": int(row["id"]),
                "submitted_at": str(row["submitted_at"] or ""),
                "full_name": str(row["full_name"]),
                "title": str(row["challenge_name"]),
                "points": int(row["points"]),
                "row": row,
            }
        )
    items.sort(key=lambda item: (item["submitted_at"], 0 if item["kind"] == "route" else 1, item["id"]))
    return items


def _format_pending_line(item: dict[str, Any]) -> str:
    if item["kind"] == "challenge":
        return f"🎯 Challenge #{item['id']} {item['full_name']} - {item['title']} ({item['points']} pts)"
    return f"#{item['id']} {item['full_name']} - {item['title']} ({item['points']} pts)"


def _pending_list_keyboard(
    items: list[dict[str, Any]], page: int = 0
) -> InlineKeyboardMarkup:
    """Build interactive inline keyboard for the unified pending submission review queue."""
    total = len(items)
    total_pages = max(1, (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PENDING_PAGE_SIZE
    page_items = items[start : start + PENDING_PAGE_SIZE]

    kb: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("▶️ Review Next Pending", callback_data="pending:next")]
    ]
    for item in page_items:
        if item["kind"] == "challenge":
            label = _truncate_button_label(
                f"🎯 #{item['id']} {item['full_name']} — {item['title']} ({item['points']} pts)"
            )
            cb = f"pending:review:challenge:{item['id']}"
        else:
            label = _truncate_button_label(
                f"🎥 #{item['id']} {item['full_name']} — {item['title']} ({item['points']} pts)"
            )
            cb = f"pending:review:{item['id']}"
        kb.append([InlineKeyboardButton(label, callback_data=cb)])

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton("⬅️ Prev", callback_data=f"pending:page:{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(
                f"Page {page + 1}/{total_pages}", callback_data=f"pending:page:{page}"
            )
        )
        if page + 1 < total_pages:
            nav_row.append(
                InlineKeyboardButton("Next ➡️", callback_data=f"pending:page:{page + 1}")
            )
        kb.append(nav_row)

    kb.append(
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"pending:page:{page}"),
            InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu"),
        ]
    )
    return InlineKeyboardMarkup(kb)


def _format_pending_page_text(items: list[dict[str, Any]], page: int = 0) -> str:
    """Format a paginated pending submissions view for inline message edits."""
    total = len(items)
    total_pages = max(1, (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PENDING_PAGE_SIZE
    page_items = items[start : start + PENDING_PAGE_SIZE]

    header = (
        f"⏳ Pending Submissions (Page {page + 1}/{total_pages} — {total} total):"
        if total_pages > 1
        else f"⏳ Pending Submissions ({total} total):"
    )
    lines = [header, ""]
    for item in page_items:
        lines.append(_format_pending_line(item))
    lines.append(
        "\nTap a submission below to watch proof & review, or tap ▶️ Review Next Pending."
    )
    return "\n".join(lines)


def _video_prompt_for_route(route: Route) -> str:
    cat_part = f" · {route.category}" if route.category else ""
    return (
        f"🎥 Send one video proving you climbed '{route.name}' ({route.points} points{cat_part}).\n\n"
        "Make sure your video clearly shows:\n"
        "• Controlled start on the starting holds\n"
        "• Full ascent without off-route dabs\n"
        "• Controlled finish matching the top hold\n\n"
        "Send the video now, or /cancel."
    )


async def _validate_and_prepare_route_submission(
    update: Update, context: ContextTypes.DEFAULT_TYPE, route_id: int
) -> int:
    store = _store(context)
    route = store.get_route(route_id)
    if route is None or not route.active:
        await update.effective_message.reply_text(
            "That route is not active. Check /routes.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    status = store.get_student_route_status(update.effective_user.id, route_id)
    if status == "approved":
        await update.effective_message.reply_text(
            f"You have already completed '{route.name}' and received points for it! 🎉",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    if status == "pending":
        await update.effective_message.reply_text(
            f"You already have a pending submission for '{route.name}'. Please wait for an admin to review it.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    context.user_data["route_id"] = route.id
    await update.effective_message.reply_text(_video_prompt_for_route(route))
    return SUBMIT_PROOF


async def prompt_open_routes_for_submit(
    message: Any, store: LeagueStore, user_id: int
) -> int:
    """List open routes for a registered student and enter SUBMIT_ROUTE state."""
    if not store.is_registered(user_id):
        await message.reply_text(
            "Please register first with /register.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    active_routes = store.list_routes(active_only=True)
    if not active_routes:
        await message.reply_text(
            "No active routes are available right now.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    statuses = store.get_student_route_statuses(user_id)
    open_routes = [r for r in active_routes if statuses.get(r.id) not in {"approved", "pending"}]
    if not open_routes:
        await message.reply_text(
            "You have already completed or submitted all active routes! 🎉",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    route_lines = "\n".join(
        f"#{r.id}: {r.name} ({r.points} pts"
        + (f" · {r.category}" if r.category else "")
        + ")"
        for r in open_routes
    )
    await _reply_chunked(
        message,
        f"Which route are you submitting for?\n\n{route_lines}\n\n"
        "Reply with the route number (e.g. 1), or /cancel.",
    )
    return SUBMIT_ROUTE


async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /submit [route_id] entry point."""
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    _clear_conversation_data(context)
    store = _store(context)
    user_id = update.effective_user.id
    if not store.is_registered(user_id):
        await update.effective_message.reply_text(
            "Please register first with /register.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    parts = (update.effective_message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        return await prompt_open_routes_for_submit(update.effective_message, store, user_id)

    route_id = _parse_positive_int(parts[1], strip_hash=True)
    if route_id is None:
        await update.effective_message.reply_text(
            "Usage: /submit <route_id>",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    return await _validate_and_prepare_route_submission(update, context, route_id)


async def submit_select_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate selected route number during /submit."""
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    route_id = _parse_positive_int(update.effective_message.text or "", strip_hash=True)
    if route_id is None:
        await update.effective_message.reply_text(
            "Please reply with a valid numeric route ID (e.g. 1), or /cancel."
        )
        return SUBMIT_ROUTE
    return await _validate_and_prepare_route_submission(update, context, route_id)


def _format_submission_caption(
    submission_id: int,
    student_name: str,
    student_id: int,
    route_id: int,
    route_name: str,
    points: int,
    status: str = "pending",
    rejection_reason: Optional[str] = None,
    *,
    is_zone: bool = False,
    awarded_points: Optional[int] = None,
) -> str:
    caption = (
        f"🧗 New Submission #{submission_id}\n"
        f"Student: {student_name} (ID: {student_id})\n"
        f"Route: #{route_id} {route_name} ({points} pts)"
    )
    if status != "pending":
        if status == "approved" and is_zone:
            pts_label = f" ({awarded_points} pts)" if awarded_points is not None else ""
            caption += f"\nStatus: ZONE{pts_label}"
        else:
            caption += f"\nStatus: {status.upper()}"
        if rejection_reason:
            caption += f"\nReason: {rejection_reason}"
    return caption


def _submission_review_keyboard(
    submission_id: int,
    zone_points: Optional[int] = None,
    *,
    include_pending_nav: bool = False,
) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton("Approve ✅", callback_data=f"review:approve:{submission_id}")
    ]
    if zone_points is not None:
        row.append(
            InlineKeyboardButton(
                f"Zone ({zone_points} pts) 🔶",
                callback_data=f"review:zone:{submission_id}",
            )
        )
    row.append(
        InlineKeyboardButton("Reject ❌", callback_data=f"review:reject:{submission_id}")
    )
    rows = [row]
    if include_pending_nav:
        rows.append(
            [
                InlineKeyboardButton(
                    "⏭️ Next Pending", callback_data=f"pending:next:{submission_id}"
                ),
                InlineKeyboardButton("📋 Pending List", callback_data="pending:page:0"),
            ]
        )
    return InlineKeyboardMarkup(rows)


async def _send_submission_to_admin(
    bot: Bot,
    admin_id: int,
    submission_id: int,
    student_name: str,
    student_id: int,
    route_id: int,
    route_name: str,
    points: int,
    video_file_id: str,
    media_type: str = "video",
    status: str = "pending",
    rejection_reason: Optional[str] = None,
    *,
    is_zone: bool = False,
    awarded_points: Optional[int] = None,
    zone_points: Optional[int] = None,
    include_pending_nav: bool = False,
) -> Optional[tuple[int, bool]]:
    caption = _format_submission_caption(
        submission_id=submission_id,
        student_name=student_name,
        student_id=student_id,
        route_id=route_id,
        route_name=route_name,
        points=points,
        status=status,
        rejection_reason=rejection_reason,
        is_zone=is_zone,
        awarded_points=awarded_points,
    )

    keyboard = (
        _submission_review_keyboard(
            submission_id, zone_points, include_pending_nav=include_pending_nav
        )
        if status == "pending"
        else None
    )

    is_caption = True
    if media_type == "video_note":
        await bot.send_video_note(chat_id=admin_id, video_note=video_file_id)
        sent = await bot.send_message(chat_id=admin_id, text=caption, reply_markup=keyboard)
        is_caption = False
    elif media_type == "document":
        sent = await bot.send_document(
            chat_id=admin_id,
            document=video_file_id,
            caption=caption,
            reply_markup=keyboard,
        )
    elif media_type == "animation":
        sent = await bot.send_animation(
            chat_id=admin_id,
            animation=video_file_id,
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        sent = await bot.send_video(
            chat_id=admin_id,
            video=video_file_id,
            caption=caption,
            reply_markup=keyboard,
        )
    msg_id = getattr(sent, "message_id", None)
    if isinstance(msg_id, int):
        return msg_id, is_caption
    return None


async def receive_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive uploaded video proof, store submission, and alert admins."""
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    message = update.effective_message
    video_info = _extract_video_info(message)
    if not video_info:
        if getattr(message, "photo", None):
            await message.reply_text(
                "⚠️ Photo proofs are not accepted. Please send a video proving your climb, or /cancel."
            )
        else:
            await message.reply_text("Please send a video proof of your climb, or /cancel.")
        return SUBMIT_PROOF

    video_file_id, media_type = video_info
    store = _store(context)
    raw_route_id = context.user_data.pop("route_id", None)
    if raw_route_id is None:
        await message.reply_text(
            "Submission session expired. Please run /submit again.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    route_id = int(raw_route_id)
    route = store.get_route(route_id)

    try:
        submission_id = store.create_submission(
            update.effective_user.id, route_id, video_file_id, media_type=media_type
        )
    except ValueError as e:
        if str(e) == "already_approved":
            err_msg = "You have already completed this route."
        elif str(e) == "already_pending":
            err_msg = "You already have a pending submission for this route."
        else:
            err_msg = "Unable to submit: route is no longer active."
        await message.reply_text(err_msg, reply_markup=_menu_button_markup())
        return ConversationHandler.END

    _log_action(
        update,
        context,
        "submit_route",
        target_type="submission",
        target_id=submission_id,
        details=f"Route #{route_id} '{route.name if route else ''}' ({media_type})",
    )
    await message.reply_text(
        f"Proof submitted (#{submission_id}). An admin will review your video.",
        reply_markup=_menu_button_markup(),
    )

    student_row = store.get_student(update.effective_user.id)
    student_name = (
        str(student_row["full_name"])
        if student_row
        else (update.effective_user.full_name or "Student")
    )
    for admin_id in sorted(_admin_ids()):
        try:
            sent_info = await _send_submission_to_admin(
                bot=context.bot,
                admin_id=admin_id,
                submission_id=submission_id,
                student_name=student_name,
                student_id=update.effective_user.id,
                route_id=route.id,
                route_name=route.name,
                points=route.points,
                video_file_id=video_file_id,
                media_type=media_type,
                status="pending",
                zone_points=route.zone_points,
            )
            if sent_info is not None:
                msg_id, is_caption = sent_info
                store.record_admin_review_message(submission_id, admin_id, msg_id, is_caption)
        except Exception:
            LOGGER.warning("Could not send proof preview to admin %s", admin_id)

    return ConversationHandler.END


async def perform_withdraw(
    bot: Bot, store: LeagueStore, user_id: int, route_id: int
) -> tuple[bool, str]:
    """Withdraw a user's pending submission and clear admin review buttons."""
    withdrawn = store.withdraw_pending_submission(user_id, route_id)
    if withdrawn is None:
        return False, f"You have no pending submission for route #{route_id}."

    for row in withdrawn["admin_messages"]:
        try:
            await bot.edit_message_reply_markup(
                chat_id=int(row["chat_id"]),
                message_id=int(row["message_id"]),
                reply_markup=None,
            )
        except Exception:
            pass
    return True, f"Withdrew pending submission #{withdrawn['id']} for '{withdrawn['route_name']}'."


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Withdraw a student's own pending submission via /withdraw <route_id>."""
    if not update.effective_user or not update.effective_message:
        return
    store = _store(context)
    user_id = update.effective_user.id
    if not store.is_registered(user_id):
        await update.effective_message.reply_text(
            "Please register first with /register.",
            reply_markup=_menu_button_markup(),
        )
        return
    if not context.args or len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage: /withdraw <route_id>",
            reply_markup=_menu_button_markup(),
        )
        return
    route_id = _parse_positive_int(context.args[0], strip_hash=True)
    if route_id is None:
        await update.effective_message.reply_text(
            "Usage: /withdraw <route_id>",
            reply_markup=_menu_button_markup(),
        )
        return

    ok, reply_msg = await perform_withdraw(context.bot, store, user_id, route_id)
    if ok:
        _log_action(
            update,
            context,
            "withdraw_route",
            target_type="route",
            target_id=route_id,
            details=reply_msg,
        )
    await update.effective_message.reply_text(
        reply_msg, reply_markup=_menu_button_markup()
    )


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List pending route and challenge submissions with interactive review GUI for admins."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    items = _unified_pending_items(_store(context))
    if not items:
        await update.effective_message.reply_text(
            "No pending submissions.", reply_markup=_menu_button_markup()
        )
        return
    lines = [_format_pending_line(item) for item in items]
    lines.append(
        "\nTip: Tap a submission below to review, tap ▶️ Review Next Pending, or use /submission <id> / /challengesubmission <id>."
    )
    await _reply_chunked(
        update.effective_message,
        "\n".join(lines),
        reply_markup=_pending_list_keyboard(items, page=0),
    )


async def _send_pending_review_card(
    bot: Bot,
    store: LeagueStore,
    chat_id: int,
    fallback_message: Any,
    sub: sqlite3.Row,
) -> None:
    """Send a pending submission's video & interactive review card and track it for multi-admin sync."""
    sub_id = int(sub["id"])
    zone_pts = int(sub["zone_points"]) if sub["zone_points"] is not None else None
    try:
        sent_info = await _send_submission_to_admin(
            bot=bot,
            admin_id=chat_id,
            submission_id=sub_id,
            student_name=str(sub["full_name"]),
            student_id=int(sub["telegram_id"]),
            route_id=int(sub["route_id"]),
            route_name=str(sub["route_name"]),
            points=int(sub["points"]),
            video_file_id=str(sub["video_file_id"]),
            media_type=str(sub["media_type"] or "video"),
            status=str(sub["status"]),
            rejection_reason=sub["rejection_reason"],
            is_zone=bool(sub["is_zone"]),
            awarded_points=int(sub["awarded_points"]) if sub["awarded_points"] is not None else None,
            zone_points=zone_pts,
            include_pending_nav=True,
        )
        if sent_info is not None and str(sub["status"]) == "pending":
            msg_id, is_caption = sent_info
            store.record_admin_review_message(sub_id, chat_id, msg_id, is_caption)
    except Exception:
        LOGGER.warning("Failed to send pending submission %s video to admin", sub_id)
        caption = _format_submission_caption(
            submission_id=sub_id,
            student_name=str(sub["full_name"]),
            student_id=int(sub["telegram_id"]),
            route_id=int(sub["route_id"]),
            route_name=str(sub["route_name"]),
            points=int(sub["points"]),
            status=str(sub["status"]),
        )
        sent = await fallback_message.reply_text(
            f"{caption}\n\n⚠️ Could not load video preview.",
            reply_markup=_submission_review_keyboard(
                sub_id, zone_pts, include_pending_nav=True
            ),
        )
        msg_id = getattr(sent, "message_id", None)
        if isinstance(msg_id, int) and str(sub["status"]) == "pending":
            store.record_admin_review_message(sub_id, chat_id, msg_id, False)


async def pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pending submission review GUI callbacks (pagination, pick, next) for routes & challenges."""
    query = update.callback_query
    if not query or not query.message or not update.effective_chat:
        return
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "pending":
        await query.answer("Invalid pending action.", show_alert=True)
        return

    action = parts[1]
    store = _store(context)

    if action == "page":
        if len(parts) != 3 or not parts[2].isascii() or not parts[2].isdigit():
            await query.answer("Invalid page.", show_alert=True)
            return
        page = int(parts[2])
        items = _unified_pending_items(store)
        if not items:
            await query.answer("No pending submissions.")
            if getattr(query.message, "caption", None) is not None:
                await query.message.reply_text(
                    "✅ No pending submissions.",
                    reply_markup=_menu_button_markup(),
                )
            else:
                try:
                    await query.edit_message_text(
                        "✅ No pending submissions.",
                        reply_markup=_menu_button_markup(),
                    )
                except Exception:
                    await query.message.reply_text(
                        "✅ No pending submissions.",
                        reply_markup=_menu_button_markup(),
                    )
            return

        await query.answer()
        page_text = _format_pending_page_text(items, page=page)
        page_kb = _pending_list_keyboard(items, page=page)
        if getattr(query.message, "caption", None) is not None:
            await query.message.reply_text(page_text, reply_markup=page_kb)
        else:
            try:
                await query.edit_message_text(page_text, reply_markup=page_kb)
            except Exception:
                await query.message.reply_text(page_text, reply_markup=page_kb)
        return

    if action == "review":
        if len(parts) == 4 and parts[2] in {"route", "challenge"}:
            kind = parts[2]
            sub_id = _parse_positive_int(parts[3])
        elif len(parts) == 3:
            kind = "route"
            sub_id = _parse_positive_int(parts[2])
        else:
            await query.answer("Invalid submission ID.", show_alert=True)
            return
        if sub_id is None:
            await query.answer("Invalid submission ID.", show_alert=True)
            return

        if kind == "challenge":
            sub = store.get_challenge_submission(sub_id)
            if sub is None or str(sub["status"]) != "pending":
                await query.answer("This submission was already reviewed.", show_alert=True)
                return
            await query.answer()
            await send_pending_challenge_review_card(
                context.bot, store, update.effective_chat.id, query.message, sub
            )
            return

        sub = store.get_submission(sub_id)
        if sub is None or str(sub["status"]) != "pending":
            await query.answer("This submission was already reviewed.", show_alert=True)
            return
        await query.answer()
        await _send_pending_review_card(
            context.bot, store, update.effective_chat.id, query.message, sub
        )
        return

    if action == "next":
        items = _unified_pending_items(store)
        if not items:
            await query.answer("No pending submissions left!", show_alert=True)
            await query.message.reply_text(
                "✅ All pending submissions have been reviewed!",
                reply_markup=_menu_button_markup(),
            )
            return

        after_key: Optional[tuple[str, int]] = None
        if len(parts) == 4 and parts[2] in {"route", "challenge"}:
            aid = _parse_positive_int(parts[3])
            if aid is not None:
                after_key = (parts[2], aid)
        elif len(parts) == 3:
            aid = _parse_positive_int(parts[2])
            if aid is not None:
                after_key = ("route", aid)

        target = items[0]
        if after_key is not None:
            keys = [(it["kind"], it["id"]) for it in items]
            if len(items) == 1 and keys[0] == after_key:
                await query.answer(
                    "No other pending submissions in the queue.", show_alert=True
                )
                return
            if after_key in keys:
                next_idx = (keys.index(after_key) + 1) % len(items)
                target = items[next_idx]

        await query.answer()
        if target["kind"] == "challenge":
            await send_pending_challenge_review_card(
                context.bot, store, update.effective_chat.id, query.message, target["row"]
            )
        else:
            await _send_pending_review_card(
                context.bot, store, update.effective_chat.id, query.message, target["row"]
            )
        return

    await query.answer("Invalid pending action.", show_alert=True)


async def all_submissions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all submissions (with optional status filter) for admins."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    status_filter = "all"
    if context.args:
        status_filter = context.args[0].strip().lower()
        if status_filter not in {"all", "pending", "approved", "top", "zone", "rejected"}:
            await update.effective_message.reply_text(
                "Usage: /submissions [all|pending|approved|zone|rejected]",
                reply_markup=_menu_button_markup(),
            )
            return
    rows = _store(context).all_submissions(status_filter=status_filter)
    if not rows:
        await update.effective_message.reply_text(
            "No submissions found.", reply_markup=_menu_button_markup()
        )
        return

    lines: list[str] = []
    for row in rows:
        st = str(row["status"])
        is_zone = bool(row["is_zone"])
        if st == "approved" and is_zone:
            pts = row["awarded_points"] if row["awarded_points"] is not None else row["zone_points"]
            badge = f"🔶 ZONE ({pts} pts)"
        elif st == "approved":
            badge = f"✅ APPROVED ({row['points']} pts)"
        elif st == "rejected":
            reason_suffix = f" - {row['rejection_reason']}" if row["rejection_reason"] else ""
            badge = f"❌ REJECTED{reason_suffix}"
        else:
            badge = "⏳ PENDING"
        lines.append(
            f"#{row['id']} {row['full_name']} (@{row['username']}) — "
            f"#{row['route_id']} {row['route_name']} [{badge}]"
        )
    lines.append("\nTip: Use /submission <id> to view video & review card.")
    await _reply_chunked(
        update.effective_message,
        "\n".join(lines),
        reply_markup=_menu_button_markup(),
    )


async def view_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a specific submission's video and review card to an admin."""
    if not update.effective_message or not update.effective_chat:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /submission <submission_id>",
            reply_markup=_menu_button_markup(),
        )
        return
    submission_id = _parse_positive_int(context.args[0], strip_hash=True)
    if submission_id is None:
        await update.effective_message.reply_text(
            "Usage: /submission <submission_id>",
            reply_markup=_menu_button_markup(),
        )
        return
    sub = _store(context).get_submission(submission_id)
    if not sub:
        await update.effective_message.reply_text(
            f"Submission #{submission_id} not found.",
            reply_markup=_menu_button_markup(),
        )
        return

    try:
        is_pending = str(sub["status"]) == "pending"
        sent_info = await _send_submission_to_admin(
            bot=context.bot,
            admin_id=update.effective_chat.id,
            submission_id=int(sub["id"]),
            student_name=str(sub["full_name"]),
            student_id=int(sub["telegram_id"]),
            route_id=int(sub["route_id"]),
            route_name=str(sub["route_name"]),
            points=int(sub["points"]),
            video_file_id=str(sub["video_file_id"]),
            media_type=str(sub["media_type"] or "video"),
            status=str(sub["status"]),
            rejection_reason=sub["rejection_reason"],
            is_zone=bool(sub["is_zone"]),
            awarded_points=int(sub["awarded_points"]) if sub["awarded_points"] is not None else None,
            zone_points=int(sub["zone_points"]) if sub["zone_points"] is not None else None,
            include_pending_nav=is_pending,
        )
        if sent_info is not None and is_pending:
            msg_id, is_caption = sent_info
            _store(context).record_admin_review_message(
                int(sub["id"]), update.effective_chat.id, msg_id, is_caption
            )
    except Exception:
        LOGGER.warning("Failed to send submission %s video to admin", submission_id)
        await update.effective_message.reply_text(
            f"Submission #{submission_id} by {sub['full_name']} for '{sub['route_name']}' "
            f"(Status: {sub['status']}) — could not load video preview.",
            reply_markup=_menu_button_markup(),
        )


def _format_student_review_notification(
    status: str,
    route_name: str,
    points: int,
    rejection_reason: Optional[str] = None,
    *,
    is_zone: bool = False,
) -> str:
    if status in {"approved", "zone"} and is_zone:
        return (
            f"🔶 Nice effort! Your submission for '{route_name}' was awarded Zone "
            f"(+{points} partial points)! You can still submit a new video if you top the route."
        )
    if status == "approved":
        return f"🎉 Great job! Your submission for '{route_name}' was approved (+{points} points)!"
    if rejection_reason:
        return (
            f"❌ Your submission for '{route_name}' was rejected.\n"
            f"Reason: {rejection_reason}\n"
            "You can submit a new attempt with improved video proof."
        )
    return (
        f"❌ Your submission for '{route_name}' was rejected. "
        "You can submit a new attempt with improved video proof."
    )


async def _sync_admin_review_cards(
    bot: Bot,
    store: LeagueStore,
    submission: sqlite3.Row,
    decision_label: str,
    *,
    exclude_chat_id: Optional[int] = None,
    exclude_message_id: Optional[int] = None,
) -> None:
    submission_id = int(submission["id"])
    rows = store.get_admin_review_messages(submission_id)
    if not rows:
        return
    base_caption = _format_submission_caption(
        submission_id=submission_id,
        student_name=str(submission["full_name"]),
        student_id=int(submission["telegram_id"]),
        route_id=int(submission["route_id"]),
        route_name=str(submission["route_name"]),
        points=int(submission["points"]),
        status="pending",
    )
    updated_text = f"{base_caption}\n\nDecision: {decision_label}"
    for row in rows:
        chat_id = int(row["chat_id"])
        message_id = int(row["message_id"])
        if chat_id == exclude_chat_id and message_id == exclude_message_id:
            continue
        try:
            if row["is_caption"]:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=updated_text,
                    reply_markup=None,
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=updated_text,
                    reply_markup=None,
                )
        except Exception:
            LOGGER.debug("Could not sync admin review card %s/%s", chat_id, message_id)
    store.delete_admin_review_messages(submission_id)


async def _edit_review_card_decision(
    query: Any,
    submission_id: int,
    decision_label: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    if getattr(query.message, "caption", None) is not None:
        current_caption = query.message.caption or ""
        try:
            await query.edit_message_caption(
                caption=f"{current_caption}\n\nDecision: {decision_label}",
                reply_markup=reply_markup,
            )
        except Exception:
            LOGGER.warning("Failed to edit review message caption for submission %s", submission_id)
    else:
        current_text = getattr(query.message, "text", "") or ""
        try:
            await query.edit_message_text(
                text=f"{current_text}\n\nDecision: {decision_label}",
                reply_markup=reply_markup,
            )
        except Exception:
            LOGGER.warning("Failed to edit review message text for submission %s", submission_id)


async def _apply_card_update_from_user_data(
    bot: Bot,
    card_chat_id: Optional[int],
    card_msg_id: Optional[int],
    is_caption: bool,
    base_text: str,
    decision_label: str,
) -> None:
    if not isinstance(card_chat_id, int) or not isinstance(card_msg_id, int) or not base_text:
        return
    updated = f"{base_text}\n\nDecision: {decision_label}"
    try:
        if is_caption:
            await bot.edit_message_caption(
                chat_id=card_chat_id,
                message_id=card_msg_id,
                caption=updated,
                reply_markup=None,
            )
        else:
            await bot.edit_message_text(
                chat_id=card_chat_id,
                message_id=card_msg_id,
                text=updated,
                reply_markup=None,
            )
    except Exception:
        pass


async def _finalize_submission_review(
    bot: Bot,
    store: LeagueStore,
    submission: sqlite3.Row,
    *,
    status: str,
    decision_label: str,
    rejection_reason: Optional[str] = None,
    is_zone: bool = False,
    awarded_points: Optional[int] = None,
    card_query: Any = None,
    card_info: Optional[tuple[Optional[int], Optional[int], bool, str]] = None,
    reviewer_reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Update originating card, sync other admins' review cards, and notify the student."""
    submission_id = int(submission["id"])
    exclude_chat_id: Optional[int] = None
    exclude_message_id: Optional[int] = None

    if card_query is not None:
        await _edit_review_card_decision(
            card_query, submission_id, decision_label, reply_markup=reviewer_reply_markup
        )
        c_id = getattr(getattr(card_query.message, "chat", None), "id", None)
        m_id = getattr(card_query.message, "message_id", None)
        exclude_chat_id = c_id if isinstance(c_id, int) else None
        exclude_message_id = m_id if isinstance(m_id, int) else None
    elif card_info is not None:
        c_chat, c_msg, is_caption, base_text = card_info
        await _apply_card_update_from_user_data(
            bot, c_chat, c_msg, is_caption, base_text, decision_label
        )
        exclude_chat_id = c_chat if isinstance(c_chat, int) else None
        exclude_message_id = c_msg if isinstance(c_msg, int) else None

    await _sync_admin_review_cards(
        bot,
        store,
        submission,
        decision_label,
        exclude_chat_id=exclude_chat_id,
        exclude_message_id=exclude_message_id,
    )

    student_id = int(submission["telegram_id"])
    route_name = str(submission["route_name"])
    notify_points = (
        awarded_points
        if (is_zone and awarded_points is not None)
        else int(submission["points"])
    )
    msg = _format_student_review_notification(
        status,
        route_name,
        notify_points,
        rejection_reason,
        is_zone=is_zone,
    )
    try:
        await bot.send_message(
            chat_id=student_id, text=msg, reply_markup=_menu_button_markup()
        )
    except Exception:
        LOGGER.warning("Failed to notify student %s about submission review", student_id)


async def review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve or reject a pending submission via /approve or /reject."""
    if not update.effective_user or not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    is_approve = (update.effective_message.text or "").split()[0].startswith("/approve")
    submission_id = (
        _parse_positive_int(context.args[0], strip_hash=True) if context.args else None
    )
    if submission_id is None or (is_approve and len(context.args) != 1):
        await update.effective_message.reply_text(
            "Usage: /approve <id> or /reject <id> [reason]",
            reply_markup=_menu_button_markup(),
        )
        return

    status = "approved" if is_approve else "rejected"
    rejection_reason = (
        " ".join(context.args[1:]).strip()
        if (not is_approve and len(context.args) > 1)
        else None
    )

    store = _store(context)
    submission = store.review_submission(
        submission_id,
        status,
        reviewer_id=update.effective_user.id,
        rejection_reason=rejection_reason,
    )
    if not submission:
        await update.effective_message.reply_text(
            "Submission not found or already reviewed.",
            reply_markup=_menu_button_markup(),
        )
        return

    _log_action(
        update,
        context,
        "approve_submission" if is_approve else "reject_submission",
        target_type="submission",
        target_id=submission_id,
        details=f"Submission #{submission_id} ({submission['full_name']} — {submission['route_name']}) {status}"
        + (f" ({rejection_reason})" if rejection_reason else ""),
    )
    await update.effective_message.reply_text(
        f"Submission #{submission_id} {status}.",
        reply_markup=_menu_button_markup(),
    )

    admin_display = update.effective_user.first_name or "Admin"
    decision_text = "APPROVED ✅" if is_approve else "REJECTED ❌"
    if rejection_reason:
        decision_text += f" ({rejection_reason})"
    await _finalize_submission_review(
        context.bot,
        store,
        submission,
        status=status,
        decision_label=f"{decision_text} by {admin_display}",
        rejection_reason=rejection_reason,
    )


async def zone_review_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Award the route's established partial zone points via /zone <submission_id>."""
    if not update.effective_user or not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    if not context.args or len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage: /zone <submission_id>",
            reply_markup=_menu_button_markup(),
        )
        return
    submission_id = _parse_positive_int(context.args[0], strip_hash=True)
    if submission_id is None:
        await update.effective_message.reply_text(
            "Usage: /zone <submission_id>",
            reply_markup=_menu_button_markup(),
        )
        return

    store = _store(context)
    sub = store.get_submission(submission_id)
    if sub is None or str(sub["status"]) != "pending":
        await update.effective_message.reply_text(
            "Submission not found or already reviewed.",
            reply_markup=_menu_button_markup(),
        )
        return

    if sub["zone_points"] is None:
        await update.effective_message.reply_text(
            f"Route '{sub['route_name']}' does not have zone points configured. "
            "Zones can only be added or modified via /addroute or /editroute.",
            reply_markup=_menu_button_markup(),
        )
        return

    reviewed = store.review_submission(
        submission_id,
        "zone",
        reviewer_id=update.effective_user.id,
        is_zone=True,
    )
    if not reviewed:
        await update.effective_message.reply_text(
            "Submission not found or already reviewed.",
            reply_markup=_menu_button_markup(),
        )
        return

    awarded = int(reviewed["awarded_points"])
    _log_action(
        update,
        context,
        "zone_submission",
        target_type="submission",
        target_id=submission_id,
        details=f"Submission #{submission_id} ({reviewed['full_name']} — {reviewed['route_name']}) awarded Zone ({awarded} pts)",
    )
    await update.effective_message.reply_text(
        f"Submission #{submission_id} awarded Zone ({awarded} pts).",
        reply_markup=_menu_button_markup(),
    )
    admin_display = update.effective_user.first_name or "Admin"
    await _finalize_submission_review(
        context.bot,
        store,
        reviewed,
        status="zone",
        decision_label=f"ZONE 🔶 (+{awarded} pts) by {admin_display}",
        is_zone=True,
        awarded_points=awarded,
    )


async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle one-click approve, preset zone, and reject inline buttons on submission cards."""
    query = update.callback_query
    if not query:
        return

    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "review":
        await query.answer("Invalid review action.", show_alert=True)
        return

    action, submission_id_str = parts[1], parts[2]
    submission_id = _parse_positive_int(submission_id_str)
    if (
        action not in {"approve", "reject", "reject_now", "zone", "zone_preset", "back"}
        or submission_id is None
    ):
        await query.answer("Invalid review action.", show_alert=True)
        return

    store = _store(context)
    sub = store.get_submission(submission_id)
    if not sub or str(sub["status"]) != "pending":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("This submission was already reviewed.", show_alert=True)
        return

    route_zone_pts = int(sub["zone_points"]) if sub["zone_points"] is not None else None

    if action == "back":
        await query.answer()
        try:
            await query.edit_message_reply_markup(
                reply_markup=_submission_review_keyboard(submission_id, route_zone_pts)
            )
        except Exception:
            pass
        return

    if action == "reject":
        await query.answer("Choose whether to add a rejection reason.")
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Add Reason ✏️", callback_data=f"review:reject_reason:{submission_id}"
                    ),
                    InlineKeyboardButton(
                        "Reject Without Reason ❌",
                        callback_data=f"review:reject_now:{submission_id}",
                    ),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"review:back:{submission_id}")],
            ]
        )
        try:
            await query.edit_message_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return

    admin_display = update.effective_user.first_name or "Admin"
    if action in {"zone", "zone_preset"}:
        if route_zone_pts is None:
            await query.answer(
                "This route does not have zone points configured.",
                show_alert=True,
            )
            return
        submission = store.review_submission(
            submission_id,
            "zone",
            reviewer_id=update.effective_user.id,
            is_zone=True,
        )
        if not submission:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.answer("This submission was already reviewed.", show_alert=True)
            return
        awarded = int(submission["awarded_points"])
        _log_action(
            update,
            context,
            "zone_submission",
            target_type="submission",
            target_id=submission_id,
            details=f"Submission #{submission_id} ({submission['full_name']} — {submission['route_name']}) awarded Zone ({awarded} pts)",
        )
        await query.answer(f"Submission #{submission_id} awarded Zone ({awarded} pts).")
        await _finalize_submission_review(
            context.bot,
            store,
            submission,
            status="zone",
            decision_label=f"ZONE 🔶 (+{awarded} pts) by {admin_display}",
            is_zone=True,
            awarded_points=awarded,
            card_query=query,
            reviewer_reply_markup=_post_review_nav_markup(store),
        )
        return

    # action is "approve" or "reject_now"
    status = "approved" if action == "approve" else "rejected"
    submission = store.review_submission(
        submission_id, status, reviewer_id=update.effective_user.id
    )
    if not submission:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("This submission was already reviewed.", show_alert=True)
        return

    _log_action(
        update,
        context,
        "approve_submission" if action == "approve" else "reject_submission",
        target_type="submission",
        target_id=submission_id,
        details=f"Submission #{submission_id} ({submission['full_name']} — {submission['route_name']}) {status}",
    )
    await query.answer(f"Submission #{submission_id} {status}.")
    decision_text = "APPROVED ✅" if action == "approve" else "REJECTED ❌"
    await _finalize_submission_review(
        context.bot,
        store,
        submission,
        status=status,
        decision_label=f"{decision_text} by {admin_display}",
        card_query=query,
        reviewer_reply_markup=_post_review_nav_markup(store),
    )


async def review_prompt_entry_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Enter conversation state to collect a rejection reason for a submission."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return ConversationHandler.END

    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "review" or parts[1] != "reject_reason":
        await query.answer("Invalid review action.", show_alert=True)
        return ConversationHandler.END
    submission_id = _parse_positive_int(parts[2])
    if submission_id is None:
        await query.answer("Invalid submission ID.", show_alert=True)
        return ConversationHandler.END

    store = _store(context)
    sub = store.get_submission(submission_id)
    if not sub or str(sub["status"]) != "pending":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("This submission was already reviewed.", show_alert=True)
        return ConversationHandler.END

    _clear_conversation_data(context)
    context.user_data["review_submission_id"] = submission_id
    if query.message:
        chat_obj = getattr(query.message, "chat", None)
        context.user_data["review_card_chat_id"] = getattr(chat_obj, "id", None)
        context.user_data["review_card_message_id"] = getattr(query.message, "message_id", None)
        is_cap = getattr(query.message, "caption", None) is not None
        context.user_data["review_card_is_caption"] = is_cap
        context.user_data["review_card_base_text"] = (
            query.message.caption if is_cap else getattr(query.message, "text", "")
        ) or ""

    await query.answer()
    if query.message:
        await query.message.reply_text(
            f"✏️ Send the rejection reason for Submission #{submission_id} (or /cancel):"
        )
    return REVIEW_REJECT_REASON


async def review_reject_reason_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Reject the submission with the typed reason and sync all review cards."""
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    reason = _sanitize_single_line(update.effective_message.text or "")
    if not reason:
        await update.effective_message.reply_text(
            "Please enter a non-empty rejection reason, or /cancel."
        )
        return REVIEW_REJECT_REASON

    sub_id = context.user_data.get("review_submission_id")
    card_info = (
        context.user_data.get("review_card_chat_id"),
        context.user_data.get("review_card_message_id"),
        bool(context.user_data.get("review_card_is_caption", True)),
        str(context.user_data.get("review_card_base_text") or ""),
    )
    _clear_conversation_data(context)

    if sub_id is None:
        await update.effective_message.reply_text(
            "Review session expired.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END

    store = _store(context)
    submission = store.review_submission(
        int(sub_id),
        "rejected",
        reviewer_id=update.effective_user.id,
        rejection_reason=reason,
    )
    if not submission:
        await update.effective_message.reply_text(
            "Submission not found or already reviewed.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    _log_action(
        update,
        context,
        "reject_submission",
        target_type="submission",
        target_id=int(sub_id),
        details=f"Submission #{sub_id} ({submission['full_name']} — {submission['route_name']}) rejected: {reason}",
    )
    await update.effective_message.reply_text(
        f"Submission #{sub_id} rejected (Reason: {reason}).",
        reply_markup=_post_review_nav_markup(store),
    )
    admin_display = update.effective_user.first_name or "Admin"
    await _finalize_submission_review(
        context.bot,
        store,
        submission,
        status="rejected",
        decision_label=f"REJECTED ❌ ({reason}) by {admin_display}",
        rejection_reason=reason,
        card_info=card_info,
    )
    return ConversationHandler.END


async def reopen_submission_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reopen a closed submission back to pending status via /reopen <id>."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    submission_id = (
        _parse_positive_int(context.args[0], strip_hash=True) if context.args else None
    )
    if submission_id is None:
        await update.effective_message.reply_text(
            "Usage: /reopen <submission_id>",
            reply_markup=_menu_button_markup(),
        )
        return

    sub, error = _store(context).reopen_submission(submission_id)
    if error == "not_found":
        await update.effective_message.reply_text(
            f"Submission #{submission_id} not found.",
            reply_markup=_menu_button_markup(),
        )
        return
    if error == "already_pending":
        await update.effective_message.reply_text(
            f"Submission #{submission_id} is already pending review.",
            reply_markup=_menu_button_markup(),
        )
        return
    if error == "conflict_active_submission":
        await update.effective_message.reply_text(
            f"Cannot reopen submission #{submission_id}: student already has another pending or approved submission for that route.",
            reply_markup=_menu_button_markup(),
        )
        return

    _log_action(
        update,
        context,
        "reopen_submission",
        target_type="submission",
        target_id=submission_id,
        details=f"Reopened submission #{submission_id} ({sub['full_name']} — {sub['route_name']})",
    )
    await update.effective_message.reply_text(
        f"Submission #{submission_id} reopened to pending. Use /submission {submission_id} to review it.",
        reply_markup=_menu_button_markup(),
    )
    try:
        await context.bot.send_message(
            chat_id=int(sub["telegram_id"]),
            text=(
                f"🔄 Your submission #{submission_id} for '{sub['route_name']}' "
                "has been reopened for re-evaluation by an admin."
            ),
            reply_markup=_menu_button_markup(),
        )
    except Exception:
        LOGGER.warning("Failed to notify student %s about reopened submission", sub["telegram_id"])
