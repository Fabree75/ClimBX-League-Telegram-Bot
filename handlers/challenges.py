"""Challenge browsing, creation, editing, deletion, proof submission, and admin review handlers."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    Update,
)
from telegram.ext import ContextTypes, ConversationHandler

from common import (
    ADDCHALLENGE_DESCRIPTION,
    ADDCHALLENGE_NAME,
    ADDCHALLENGE_PHOTOS,
    ADDCHALLENGE_POINTS,
    CHALLENGE_REVIEW_REJECT_REASON,
    EDITCHALLENGE_DELETE_CONFIRM,
    EDITCHALLENGE_DESCRIPTION,
    EDITCHALLENGE_MENU,
    EDITCHALLENGE_NAME,
    EDITCHALLENGE_PHOTOS,
    EDITCHALLENGE_POINTS,
    EDITCHALLENGE_SELECT,
    MAX_MEDIA_GROUP_SIZE,
    SUBMIT_CHALLENGE,
    SUBMIT_CHALLENGE_PROOF,
    _admin_ids,
    _can_manage_routes,
    _clear_conversation_data,
    _extract_command_and_args,
    _extract_photo_info,
    _extract_video_info,
    _is_admin,
    _log_action,
    _menu_button_markup,
    _parse_positive_int,
    _post_review_nav_markup,
    _reply_chunked,
    _sanitize_single_line,
    _store,
    _with_menu_button,
)
from store import Challenge, LeagueStore

LOGGER = logging.getLogger(__name__)

_ADDCHALLENGE_USAGE = (
    "Usage:\n"
    "• Interactive (with description & pictures): /addchallenge\n"
    "• Quick publish: /addchallenge <name> <points>\n"
    "• Pipe format: /addchallenge <name> | <points> | [description]"
)


def _extract_challenge_proof_info(message: Any) -> Optional[tuple[str, str]]:
    """Extracts (file_id, media_type) for challenge proof: accepts videos AND photos."""
    vid = _extract_video_info(message)
    if vid is not None:
        return vid
    photo = _extract_photo_info(message)
    if photo is not None:
        return photo
    return None


# ---------------------------------------------------------------------------
# Browsing Challenges (/challenges, /challenge)
# ---------------------------------------------------------------------------


async def challenges_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List challenges with optional routesetter/admin 'all' flag."""
    if not update.effective_message:
        return
    args = list(context.args or [])
    show_all = False
    if args and args[0].lower() == "all":
        if _can_manage_routes(update, context):
            show_all = True

    store = _store(context)
    available = store.list_challenges(active_only=not show_all)
    if not available:
        await update.effective_message.reply_text(
            "No challenges have been published yet.",
            reply_markup=_menu_button_markup(),
        )
        return

    user_id = update.effective_user.id if update.effective_user else None
    statuses = store.get_student_challenge_statuses(user_id) if user_id else {}

    lines: list[str] = ["🎯 ClimbX League Challenges:"]
    completed_count = 0
    active_count = 0
    earned_active_points = 0

    for chal in available:
        if chal.active:
            active_count += 1
        status = statuses.get(chal.id)
        if status == "approved":
            badge = "✅ "
            status_suffix = " (Completed)"
            if chal.active:
                completed_count += 1
                earned_active_points += chal.points
        elif status == "pending":
            badge = "⏳ "
            status_suffix = " (Pending review)"
        else:
            badge = ""
            status_suffix = ""

        pic_suffix = f" [📸 {chal.photo_count}]" if chal.photo_count > 0 else ""
        inactive_suffix = "" if chal.active else " [Inactive]"
        desc_preview = ""
        if chal.description:
            short = _sanitize_single_line(chal.description)
            if len(short) > 65:
                short = short[:64] + "…"
            desc_preview = f"\n   ↳ {short}"
        lines.append(
            f"{badge}#{chal.id}: {chal.name} - {chal.points} points"
            f"{pic_suffix}{status_suffix}{inactive_suffix}{desc_preview}"
        )

    if user_id and store.is_registered(user_id):
        lines.append(
            f"\nChallenge progress: {completed_count}/{active_count} "
            f"active challenges completed ({earned_active_points} pts)"
        )

    lines.append(
        "Tip: Use /challenge <id> for full details & pictures, or /submitchallenge <id> to submit proof."
    )
    await _reply_chunked(
        update.effective_message,
        "\n".join(lines),
        reply_markup=_menu_button_markup(),
    )


def _format_challenge_details_text(
    details: dict[str, Any], store: Optional[LeagueStore] = None, user_id: Optional[int] = None
) -> str:
    chal: Challenge = details["challenge"]
    photos: list[dict[str, Any]] = details["photos"]
    completions_count: int = details["completions_count"]
    pending_count: int = details["pending_count"]
    user_status: Optional[str] = details["user_status"]

    climber_word = "climber" if completions_count == 1 else "climbers"
    lines = [
        f"🎯 Challenge #{chal.id}: {chal.name}",
        f"🏆 Points: {chal.points} pts",
    ]
    if chal.description:
        lines.append(f"📝 Description: {chal.description}")
    lines.extend(
        [
            f"📌 Status: {'Active' if chal.active else 'Inactive (archived)'}",
            f"🏅 Completions: {completions_count} {climber_word}",
            f"📸 Pictures: {len(photos)}",
        ]
    )
    if pending_count > 0:
        lines.append(f"⏳ Pending submissions: {pending_count}")
    if user_status == "approved":
        lines.append("✅ Your status: Completed")
    elif user_status == "pending":
        lines.append("⏳ Your status: Pending review")
    elif chal.active:
        is_reg = bool(store and user_id is not None and store.is_registered(user_id))
        if is_reg:
            lines.append(
                f"📝 Your status: Not submitted yet (use /submitchallenge {chal.id})"
            )
        else:
            lines.append("📝 Register with /register to submit proof for this challenge.")
    return "\n".join(lines)


async def _send_challenge_details(
    message: Any, store: LeagueStore, challenge_id: int, user_id: Optional[int]
) -> None:
    details = store.get_challenge_details(challenge_id, telegram_id=user_id)
    if details is None:
        await message.reply_text(
            f"Challenge #{challenge_id} not found.",
            reply_markup=_menu_button_markup(),
        )
        return

    chal: Challenge = details["challenge"]
    info_text = _format_challenge_details_text(details, store=store, user_id=user_id)
    is_reg = bool(user_id is not None and store.is_registered(user_id))
    can_submit = (
        is_reg
        and chal.active
        and details["user_status"] not in {"approved", "pending"}
    )
    markup = (
        _with_menu_button(
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🎬 Submit Proof", callback_data=f"subchal:start:{chal.id}"
                        )
                    ]
                ]
            )
        )
        if can_submit
        else _menu_button_markup()
    )

    photos = details["photos"]
    if not photos:
        await _reply_chunked(message, info_text, reply_markup=markup)
        return

    try:
        if len(photos) == 1:
            photo = photos[0]
            if photo["media_type"] == "document":
                await message.reply_document(document=photo["file_id"])
            else:
                await message.reply_photo(photo=photo["file_id"])
        else:
            media_types = {p["media_type"] for p in photos}
            if len(media_types) == 1:
                only_type = next(iter(media_types))
                for idx in range(0, len(photos), MAX_MEDIA_GROUP_SIZE):
                    batch = photos[idx : idx + MAX_MEDIA_GROUP_SIZE]
                    media_items = [
                        InputMediaDocument(media=item["file_id"])
                        if only_type == "document"
                        else InputMediaPhoto(media=item["file_id"])
                        for item in batch
                    ]
                    await message.reply_media_group(media=media_items)
            else:
                for item in photos:
                    if item["media_type"] == "document":
                        await message.reply_document(document=item["file_id"])
                    else:
                        await message.reply_photo(photo=item["file_id"])
        await _reply_chunked(message, info_text, reply_markup=markup)
    except Exception:
        LOGGER.warning("Failed to send pictures for challenge %s", challenge_id)
        await _reply_chunked(
            message,
            f"{info_text}\n\n⚠️ Could not load challenge picture(s).",
            reply_markup=markup,
        )


async def challenge_info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display an inline challenge picker or full challenge details + pictures for /challenge <id>."""
    if not update.effective_message:
        return
    store = _store(context)
    if not context.args:
        active_challenges = store.list_challenges(active_only=True)
        if not active_challenges:
            await update.effective_message.reply_text(
                "No active challenges have been published yet.",
                reply_markup=_menu_button_markup(),
            )
            return
        keyboard = [
            [
                InlineKeyboardButton(
                    f"#{c.id} {c.name} ({c.points} pts)",
                    callback_data=f"challenge:view:{c.id}",
                )
            ]
            for c in active_challenges[:25]
        ]
        keyboard.append([InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu")])
        await update.effective_message.reply_text(
            "Select a challenge to view its full info & pictures (or use /challenge <id>):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    challenge_id = _parse_positive_int(context.args[0], strip_hash=True)
    if challenge_id is None or len(context.args) > 1:
        await update.effective_message.reply_text(
            "Usage: /challenge <challenge_id>",
            reply_markup=_menu_button_markup(),
        )
        return

    user_id = update.effective_user.id if update.effective_user else None
    await _send_challenge_details(update.effective_message, store, challenge_id, user_id)


async def challenge_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline challenge:view:<challenge_id> button clicks."""
    query = update.callback_query
    if not query:
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "challenge" or parts[1] != "view":
        await query.answer("Invalid challenge selection.", show_alert=True)
        return
    challenge_id = _parse_positive_int(parts[2])
    if challenge_id is None:
        await query.answer("Invalid challenge ID.", show_alert=True)
        return
    await query.answer()
    if query.message:
        user_id = update.effective_user.id if update.effective_user else None
        await _send_challenge_details(query.message, _store(context), challenge_id, user_id)


# ---------------------------------------------------------------------------
# Creating Challenges (/addchallenge) for Routesetters & Admins
# ---------------------------------------------------------------------------


def _parse_addchallenge_args(raw_args: str) -> Optional[tuple[str, int, Optional[str]]]:
    """Parses direct /addchallenge arguments into (name, points, description) or None."""
    cleaned = raw_args.strip()
    if not cleaned:
        return None

    if "|" in cleaned:
        parts = [part.strip() for part in cleaned.split("|")]
        if len(parts) == 2:
            name = _sanitize_single_line(parts[0])
            points = _parse_positive_int(parts[1])
            if name and points is not None:
                return name, points, None
            return None
        if len(parts) >= 3:
            name = _sanitize_single_line(parts[0])
            points = _parse_positive_int(parts[1])
            desc = " | ".join(parts[2:]).strip() or None
            if name and points is not None:
                return name, points, desc
            return None
        return None

    tokens = cleaned.split()
    if len(tokens) < 2:
        return None
    points = _parse_positive_int(tokens[-1])
    if points is not None:
        name = _sanitize_single_line(" ".join(tokens[:-1]))
        if name:
            return name, points, None
    return None


def _format_challenge_published_message(
    challenge_id: int, points: int, photo_count: int
) -> str:
    details = [f"{points} pts"]
    if photo_count > 0:
        pic_word = "picture" if photo_count == 1 else "pictures"
        details.append(f"{photo_count} {pic_word}")
    return f"Challenge #{challenge_id} published ({', '.join(details)})."


def _addchallenge_photos_keyboard(photo_count: int) -> InlineKeyboardMarkup:
    done_label = "Publish Challenge ✅" if photo_count > 0 else "Skip Pictures & Publish ✅"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(done_label, callback_data="addchal:done"),
                InlineKeyboardButton("Cancel ❌", callback_data="addchal:cancel"),
            ]
        ]
    )


async def _start_interactive_addchallenge(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    initial_photo: Optional[tuple[str, str]],
) -> int:
    _clear_conversation_data(context)
    photos = [initial_photo] if initial_photo is not None else []
    context.user_data["addchallenge_photos"] = photos
    prefix = "📸 Initial picture saved.\n\n" if photos else ""
    await message.reply_text(
        f"{prefix}🎯 Creating a new challenge.\nWhat is the challenge name? (or /cancel)"
    )
    return ADDCHALLENGE_NAME


async def add_challenge_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start /addchallenge interactively or publish immediately when arguments are provided."""
    if not update.effective_message:
        return ConversationHandler.END
    if not _can_manage_routes(update, context):
        await update.effective_message.reply_text(
            "Routesetter or admin access required.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    raw_args = " ".join(context.args) if context.args else ""
    if not raw_args:
        return await _start_interactive_addchallenge(update.effective_message, context, None)

    parsed = _parse_addchallenge_args(raw_args)
    if parsed is None:
        await update.effective_message.reply_text(
            _ADDCHALLENGE_USAGE, reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END

    _clear_conversation_data(context)
    name, points, description = parsed
    challenge_id = _store(context).add_challenge(name, points, description=description)
    _log_action(
        update,
        context,
        "add_challenge",
        target_type="challenge",
        target_id=challenge_id,
        details=f"Challenge #{challenge_id} '{name}' ({points} pts)",
    )
    await update.effective_message.reply_text(
        _format_challenge_published_message(challenge_id, points, 0),
        reply_markup=_menu_button_markup(),
    )
    return ConversationHandler.END


async def add_challenge_caption_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle /addchallenge sent directly as a photo or image document caption."""
    if not update.effective_message:
        return ConversationHandler.END
    message = update.effective_message
    bot_username = getattr(context.bot, "username", None)
    parsed_cmd = _extract_command_and_args(message.caption or "", bot_username)
    if parsed_cmd is None or parsed_cmd[0] != "addchallenge":
        return ConversationHandler.END

    if not _can_manage_routes(update, context):
        await message.reply_text(
            "Routesetter or admin access required.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    photo_info = _extract_photo_info(message)
    if photo_info is None:
        await message.reply_text(
            "Please attach a valid image/photo.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    file_id, media_type = photo_info

    raw_args = parsed_cmd[1].strip()
    if not raw_args:
        return await _start_interactive_addchallenge(message, context, (media_type, file_id))

    parsed = _parse_addchallenge_args(raw_args)
    if parsed is None:
        await message.reply_text(
            _ADDCHALLENGE_USAGE, reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END

    _clear_conversation_data(context)
    name, points, description = parsed
    challenge_id = _store(context).add_challenge(
        name, points, description=description, photos=[(media_type, file_id)]
    )
    _log_action(
        update,
        context,
        "add_challenge",
        target_type="challenge",
        target_id=challenge_id,
        details=f"Challenge #{challenge_id} '{name}' ({points} pts, 1 photo)",
    )
    await message.reply_text(
        _format_challenge_published_message(challenge_id, points, 1),
        reply_markup=_menu_button_markup(),
    )
    return ConversationHandler.END


async def addchallenge_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save challenge name and ask for optional description."""
    if not update.effective_message:
        return ConversationHandler.END
    name = _sanitize_single_line(update.effective_message.text or "")
    if not name:
        await update.effective_message.reply_text(
            "Please enter a valid challenge name, or /cancel."
        )
        return ADDCHALLENGE_NAME
    context.user_data["addchallenge_name"] = name
    await update.effective_message.reply_text(
        f"🎯 Challenge: {name}\n"
        "Send a short description/rules for this challenge (optional — tap Skip Description ➡️ or send /skip), or /cancel:",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Skip Description ➡️", callback_data="addchal:skipdesc"),
                    InlineKeyboardButton("Cancel ❌", callback_data="addchal:cancel"),
                ]
            ]
        ),
    )
    return ADDCHALLENGE_DESCRIPTION


async def addchallenge_description_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Save optional challenge description and prompt for points."""
    if not update.effective_message:
        return ConversationHandler.END
    raw = (update.effective_message.text or "").strip()
    if raw.lower() in {"skip", "none"}:
        context.user_data["addchallenge_description"] = None
    else:
        context.user_data["addchallenge_description"] = raw
    name = context.user_data.get("addchallenge_name", "this challenge")
    await update.effective_message.reply_text(
        f"How many points is '{name}' worth? (positive integer, or /cancel)"
    )
    return ADDCHALLENGE_POINTS


async def addchallenge_description_skip(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Skip challenge description via /skip or inline button."""
    context.user_data["addchallenge_description"] = None
    name = context.user_data.get("addchallenge_name", "this challenge")
    if update.callback_query:
        query = update.callback_query
        if not _can_manage_routes(update, context):
            await query.answer("Routesetter or admin access required.", show_alert=True)
            return ConversationHandler.END
        data = query.data or ""
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if data == "addchal:cancel":
            _clear_conversation_data(context)
            await query.answer("Cancelled.")
            if query.message:
                await query.message.reply_text(
                    "Cancelled.", reply_markup=_menu_button_markup()
                )
            return ConversationHandler.END
        await query.answer("Description skipped.")
        if query.message:
            await query.message.reply_text(
                f"Skipped description.\nHow many points is '{name}' worth? (positive integer, or /cancel)"
            )
    elif update.effective_message:
        await update.effective_message.reply_text(
            f"Skipped description.\nHow many points is '{name}' worth? (positive integer, or /cancel)"
        )
    return ADDCHALLENGE_POINTS


async def addchallenge_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save challenge points and enter optional pictures upload step."""
    if not update.effective_message:
        return ConversationHandler.END
    points = _parse_positive_int(update.effective_message.text or "")
    if points is None:
        await update.effective_message.reply_text(
            "Please enter a valid positive whole number for points (e.g. 15), or /cancel."
        )
        return ADDCHALLENGE_POINTS

    context.user_data["addchallenge_points"] = points
    photos = context.user_data.setdefault("addchallenge_photos", [])
    photo_count = len(photos)
    if photo_count > 0:
        prompt = (
            f"📸 {photo_count} picture(s) already attached.\n"
            "Send more pictures if you'd like, or tap Publish Challenge ✅ (or send /done) to publish."
        )
    else:
        prompt = (
            "📸 Send one or more pictures for this challenge (optional).\n"
            "When finished, tap Publish Challenge ✅ (or send /done, or /skip for no pictures)."
        )
    await update.effective_message.reply_text(
        prompt, reply_markup=_addchallenge_photos_keyboard(photo_count)
    )
    return ADDCHALLENGE_PHOTOS


async def addchallenge_receive_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Attach an uploaded picture during /addchallenge."""
    if not update.effective_message:
        return ConversationHandler.END
    photo_info = _extract_photo_info(update.effective_message)
    if photo_info is None:
        await update.effective_message.reply_text(
            "Please send a picture (photo), or send /done (or /skip) to publish, or /cancel."
        )
        return ADDCHALLENGE_PHOTOS
    file_id, media_type = photo_info
    photos: list[tuple[str, str]] = context.user_data.setdefault("addchallenge_photos", [])
    photos.append((media_type, file_id))
    await update.effective_message.reply_text(
        f"📸 Picture #{len(photos)} added. Send another picture, or send /done (or tap Publish Challenge ✅) to publish.",
        reply_markup=_addchallenge_photos_keyboard(len(photos)),
    )
    return ADDCHALLENGE_PHOTOS


async def _finalize_addchallenge(
    message: Any, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    name = context.user_data.get("addchallenge_name")
    description = context.user_data.get("addchallenge_description")
    points = context.user_data.get("addchallenge_points")
    photos: list[tuple[str, str]] = list(context.user_data.get("addchallenge_photos") or [])
    _clear_conversation_data(context)

    if not name or points is None:
        await message.reply_text(
            "Challenge creation session expired. Please run /addchallenge again.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    pts_int = int(points)
    challenge_id = _store(context).add_challenge(
        name, pts_int, description=description, photos=photos
    )
    _log_action(
        update,
        context,
        "add_challenge",
        target_type="challenge",
        target_id=challenge_id,
        details=f"Challenge #{challenge_id} '{name}' ({pts_int} pts, {len(photos)} photos)",
    )
    await message.reply_text(
        _format_challenge_published_message(challenge_id, pts_int, len(photos)),
        reply_markup=_menu_button_markup(),
    )
    return ConversationHandler.END


async def addchallenge_photos_done(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    return await _finalize_addchallenge(update.effective_message, update, context)


async def addchallenge_photos_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _can_manage_routes(update, context):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END
    data = query.data or ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if data == "addchal:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        if query.message:
            await query.message.reply_text("Cancelled.", reply_markup=_menu_button_markup())
        return ConversationHandler.END
    if data == "addchal:done":
        await query.answer("Publishing challenge...")
        if query.message:
            return await _finalize_addchallenge(query.message, update, context)
        _clear_conversation_data(context)
        return ConversationHandler.END
    await query.answer("Invalid action.", show_alert=True)
    return ADDCHALLENGE_PHOTOS


# ---------------------------------------------------------------------------
# Editing & Deleting Challenges (/editchallenge)
# Routesetters: edit fields & activate/deactivate; Admins: all + permanent delete
# ---------------------------------------------------------------------------


def _format_editchallenge_menu_text(challenge: Challenge) -> str:
    desc_str = challenge.description if challenge.description else "None"
    return (
        f"✏️ Editing Challenge #{challenge.id}\n"
        f"• Name: {challenge.name}\n"
        f"• Description: {desc_str}\n"
        f"• Points: {challenge.points} pts\n"
        f"• Pictures: {challenge.photo_count}\n"
        f"• Status: {'Active' if challenge.active else 'Inactive (archived)'}\n\n"
        "Select a field below to modify, or tap Done Editing ✅:"
    )


def _editchallenge_menu_keyboard(
    challenge: Challenge, *, is_admin: bool
) -> InlineKeyboardMarkup:
    row2 = [InlineKeyboardButton("📸 Add Pictures", callback_data="editchal:field:photos")]
    if challenge.photo_count > 0:
        row2.append(
            InlineKeyboardButton("🗑️ Clear Pictures", callback_data="editchal:clearphotos")
        )
    toggle_label = "🔄 Deactivate Challenge" if challenge.active else "🔄 Activate Challenge"
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("✏️ Name", callback_data="editchal:field:name"),
            InlineKeyboardButton("📝 Description", callback_data="editchal:field:description"),
        ],
        [
            InlineKeyboardButton("🏆 Points", callback_data="editchal:field:points"),
        ],
        row2,
        [InlineKeyboardButton(toggle_label, callback_data="editchal:toggle")],
    ]
    if is_admin:
        rows.append(
            [InlineKeyboardButton("🗑️ Delete Challenge", callback_data="editchal:delete")]
        )
    rows.append([InlineKeyboardButton("✅ Done Editing", callback_data="editchal:done")])
    return InlineKeyboardMarkup(rows)


async def _send_editchallenge_menu(
    message: Any, update: Update, context: ContextTypes.DEFAULT_TYPE, challenge_id: int
) -> int:
    challenge = _store(context).get_challenge(challenge_id)
    if challenge is None:
        _clear_conversation_data(context)
        await message.reply_text(
            f"Challenge #{challenge_id} not found.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    context.user_data["editchallenge_id"] = challenge.id
    await message.reply_text(
        _format_editchallenge_menu_text(challenge),
        reply_markup=_editchallenge_menu_keyboard(challenge, is_admin=_is_admin(update)),
    )
    return EDITCHALLENGE_MENU


async def start_editchallenge_picker(
    message: Any, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Display an inline challenge picker for /editchallenge when no ID is provided."""
    _clear_conversation_data(context)
    all_challenges = _store(context).list_challenges(active_only=False)
    if not all_challenges:
        await message.reply_text(
            "No challenges have been published yet.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    kb = [
        [
            InlineKeyboardButton(
                f"#{c.id} {c.name} ({c.points} pts · {'Active' if c.active else 'Inactive'})",
                callback_data=f"editchal:pick:{c.id}",
            )
        ]
        for c in all_challenges[:25]
    ]
    kb.append([InlineKeyboardButton("Cancel ❌", callback_data="editchal:cancel")])
    await message.reply_text(
        "✏️ Select a challenge to edit (or reply with the challenge ID, or /cancel):",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return EDITCHALLENGE_SELECT


async def edit_challenge_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start /editchallenge [challenge_id] for routesetters and admins."""
    if not update.effective_message:
        return ConversationHandler.END
    if not _can_manage_routes(update, context):
        await update.effective_message.reply_text(
            "Routesetter or admin access required.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    if not context.args:
        return await start_editchallenge_picker(update.effective_message, context)

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage: /editchallenge [challenge_id]",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    challenge_id = _parse_positive_int(context.args[0], strip_hash=True)
    if challenge_id is None:
        await update.effective_message.reply_text(
            "Usage: /editchallenge [challenge_id]",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    _clear_conversation_data(context)
    return await _send_editchallenge_menu(
        update.effective_message, update, context, challenge_id
    )


async def editchallenge_select_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _can_manage_routes(update, context):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if data == "editchal:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        await query.message.reply_text("Cancelled.", reply_markup=_menu_button_markup())
        return ConversationHandler.END

    parts = data.split(":")
    challenge_id = _parse_positive_int(parts[2]) if len(parts) == 3 else None
    if challenge_id is None:
        await query.answer("Invalid challenge ID.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    return await _send_editchallenge_menu(query.message, update, context, challenge_id)


async def editchallenge_select_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    if not _can_manage_routes(update, context):
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            "Routesetter or admin access required.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    challenge_id = _parse_positive_int(update.effective_message.text or "", strip_hash=True)
    if challenge_id is None:
        await update.effective_message.reply_text(
            "Please reply with a valid numeric challenge ID (e.g. 1), or /cancel."
        )
        return EDITCHALLENGE_SELECT
    return await _send_editchallenge_menu(
        update.effective_message, update, context, challenge_id
    )


async def editchallenge_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _can_manage_routes(update, context):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END

    challenge_id = int(context.user_data.get("editchallenge_id") or 0)
    store = _store(context)
    challenge = store.get_challenge(challenge_id)
    if challenge is None:
        _clear_conversation_data(context)
        await query.answer("Challenge not found.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    if data == "editchal:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("Cancelled.", reply_markup=_menu_button_markup())
        return ConversationHandler.END

    if data == "editchal:done":
        _clear_conversation_data(context)
        await query.answer("Finished editing.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"✅ Finished editing Challenge #{challenge.id} ('{challenge.name}').",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    if data == "editchal:toggle":
        updated = store.update_challenge(challenge.id, active=not challenge.active)
        state_str = "reactivated" if (updated and updated.active) else "deactivated"
        _log_action(
            update,
            context,
            "edit_challenge",
            target_type="challenge",
            target_id=challenge.id,
            details=f"Challenge #{challenge.id} ('{challenge.name}') {state_str}",
        )
        await query.answer(f"Challenge #{challenge.id} {state_str}.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"Challenge #{challenge.id} ('{challenge.name}') has been {state_str}."
        )
        return await _send_editchallenge_menu(query.message, update, context, challenge.id)

    if data == "editchal:delete":
        if not _is_admin(update):
            await query.answer(
                "Only admins can permanently delete challenges. Routesetters can deactivate them instead.",
                show_alert=True,
            )
            return EDITCHALLENGE_MENU
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"⚠️ Permanently delete Challenge #{challenge.id} ('{challenge.name}')?\n"
            "This will also remove all submissions and awarded points for this challenge. "
            "Use Deactivate if you want to archive it while keeping past points.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Confirm Delete 🗑️", callback_data="editchal:delete_confirm"
                        ),
                        InlineKeyboardButton("⬅️ Back", callback_data="editchal:delete_back"),
                    ]
                ]
            ),
        )
        return EDITCHALLENGE_DELETE_CONFIRM

    if data == "editchal:clearphotos":
        removed = store.clear_challenge_photos(challenge.id)
        _log_action(
            update,
            context,
            "edit_challenge",
            target_type="challenge",
            target_id=challenge.id,
            details=f"Cleared {removed} picture(s) from Challenge #{challenge.id}",
        )
        await query.answer("Pictures cleared.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"🗑️ Removed {removed} picture(s) from Challenge #{challenge.id} ('{challenge.name}')."
        )
        return await _send_editchallenge_menu(query.message, update, context, challenge.id)

    if data == "editchal:field:name":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"✏️ Send the new name for Challenge #{challenge.id} (current: '{challenge.name}'), or /cancel:"
        )
        return EDITCHALLENGE_NAME

    if data == "editchal:field:description":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        kb_rows: list[list[InlineKeyboardButton]] = []
        if challenge.description:
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        "🗑️ Clear Description", callback_data="editchal:cleardesc"
                    )
                ]
            )
        kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="editchal:back")])
        await query.message.reply_text(
            f"📝 Send the new description for Challenge #{challenge.id} ('{challenge.name}') (or 'none' to clear), or /cancel:",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return EDITCHALLENGE_DESCRIPTION

    if data == "editchal:field:points":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"🏆 Send the new points for Challenge #{challenge.id} (positive integer, current: {challenge.points}), or /cancel:"
        )
        return EDITCHALLENGE_POINTS

    if data == "editchal:field:photos":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        context.user_data["editchallenge_photo_count"] = 0
        await query.message.reply_text(
            f"📸 Send picture(s) to attach to Challenge #{challenge.id} ('{challenge.name}').\n"
            "Send /done (or tap Done Adding Pictures ✅) when finished, or /cancel.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Done Adding Pictures ✅", callback_data="editchal:photos_done"
                        )
                    ]
                ]
            ),
        )
        return EDITCHALLENGE_PHOTOS

    await query.answer("Invalid action.", show_alert=True)
    return EDITCHALLENGE_MENU


async def editchallenge_delete_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Confirm or abort permanent challenge deletion (Admins only)."""
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return ConversationHandler.END

    challenge_id = int(context.user_data.get("editchallenge_id") or 0)
    data = query.data or ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if data == "editchal:delete_back":
        await query.answer()
        return await _send_editchallenge_menu(query.message, update, context, challenge_id)

    if data == "editchal:delete_confirm":
        _clear_conversation_data(context)
        deleted = _store(context).delete_challenge(challenge_id)
        if deleted is None:
            await query.answer("Challenge not found.", show_alert=True)
            await query.message.reply_text(
                f"Challenge #{challenge_id} not found.",
                reply_markup=_menu_button_markup(),
            )
            return ConversationHandler.END
        _log_action(
            update,
            context,
            "delete_challenge",
            target_type="challenge",
            target_id=deleted.id,
            details=f"Deleted Challenge #{deleted.id} ('{deleted.name}', {deleted.points} pts)",
        )
        await query.answer("Challenge deleted.")
        await query.message.reply_text(
            f"🗑️ Permanently deleted Challenge #{deleted.id} ('{deleted.name}').",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    await query.answer("Invalid action.", show_alert=True)
    return EDITCHALLENGE_DELETE_CONFIRM


async def editchallenge_done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    challenge_id = context.user_data.get("editchallenge_id")
    chal = _store(context).get_challenge(int(challenge_id)) if challenge_id else None
    _clear_conversation_data(context)
    if update.effective_message:
        label = f"Challenge #{chal.id} ('{chal.name}')" if chal else "challenge"
        await update.effective_message.reply_text(
            f"✅ Finished editing {label}.",
            reply_markup=_menu_button_markup(),
        )
    return ConversationHandler.END


async def editchallenge_name_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    challenge_id = int(context.user_data.get("editchallenge_id") or 0)
    name = _sanitize_single_line(update.effective_message.text or "")
    if not name:
        await update.effective_message.reply_text(
            "Please enter a valid non-empty challenge name, or /cancel."
        )
        return EDITCHALLENGE_NAME
    updated = _store(context).update_challenge(challenge_id, name=name)
    if updated is None:
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            f"Challenge #{challenge_id} not found.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END
    _log_action(
        update,
        context,
        "edit_challenge",
        target_type="challenge",
        target_id=challenge_id,
        details=f"Challenge #{challenge_id} renamed to '{updated.name}'",
    )
    await update.effective_message.reply_text(
        f"✅ Challenge #{challenge_id} name updated to '{updated.name}'."
    )
    return await _send_editchallenge_menu(
        update.effective_message, update, context, challenge_id
    )


async def editchallenge_description_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _can_manage_routes(update, context):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END
    challenge_id = int(context.user_data.get("editchallenge_id") or 0)
    data = query.data or ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if data == "editchal:back":
        await query.answer()
        return await _send_editchallenge_menu(query.message, update, context, challenge_id)
    if data == "editchal:cleardesc":
        updated = _store(context).update_challenge(challenge_id, description=None)
        if updated:
            _log_action(
                update,
                context,
                "edit_challenge",
                target_type="challenge",
                target_id=challenge_id,
                details=f"Cleared description for Challenge #{challenge_id}",
            )
            await query.message.reply_text(
                f"Challenge #{challenge_id} ('{updated.name}') description cleared."
            )
        await query.answer("Description cleared.")
        return await _send_editchallenge_menu(query.message, update, context, challenge_id)
    await query.answer("Invalid option.", show_alert=True)
    return EDITCHALLENGE_DESCRIPTION


async def editchallenge_description_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    challenge_id = int(context.user_data.get("editchallenge_id") or 0)
    raw = (update.effective_message.text or "").strip()
    new_desc = None if raw.lower() in {"none", "clear", "skip"} else raw
    updated = _store(context).update_challenge(challenge_id, description=new_desc)
    if updated is None:
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            f"Challenge #{challenge_id} not found.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END
    _log_action(
        update,
        context,
        "edit_challenge",
        target_type="challenge",
        target_id=challenge_id,
        details=f"Updated description for Challenge #{challenge_id}",
    )
    msg = (
        f"Challenge #{challenge_id} ('{updated.name}') description cleared."
        if new_desc is None
        else f"✅ Challenge #{challenge_id} ('{updated.name}') description updated."
    )
    await update.effective_message.reply_text(msg)
    return await _send_editchallenge_menu(
        update.effective_message, update, context, challenge_id
    )


async def editchallenge_points_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    challenge_id = int(context.user_data.get("editchallenge_id") or 0)
    pts = _parse_positive_int(update.effective_message.text or "")
    if pts is None:
        await update.effective_message.reply_text(
            "Please enter a valid positive whole number for points (e.g. 15), or /cancel."
        )
        return EDITCHALLENGE_POINTS
    updated = _store(context).update_challenge(challenge_id, points=pts)
    if updated is None:
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            f"Challenge #{challenge_id} not found.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END
    _log_action(
        update,
        context,
        "edit_challenge",
        target_type="challenge",
        target_id=challenge_id,
        details=f"Challenge #{challenge_id} ('{updated.name}') points set to {pts}",
    )
    await update.effective_message.reply_text(
        f"✅ Challenge #{challenge_id} ('{updated.name}') points set to {pts} pts."
    )
    return await _send_editchallenge_menu(
        update.effective_message, update, context, challenge_id
    )


async def editchallenge_receive_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    challenge_id = context.user_data.get("editchallenge_id")
    if challenge_id is None:
        await update.effective_message.reply_text(
            "Edit session expired. Please run /editchallenge again.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    photo_info = _extract_photo_info(update.effective_message)
    if photo_info is None:
        await update.effective_message.reply_text(
            "Please send a picture (photo), or send /done when finished, or /cancel."
        )
        return EDITCHALLENGE_PHOTOS
    file_id, media_type = photo_info
    store = _store(context)
    store.add_challenge_photo(int(challenge_id), file_id, media_type=media_type)
    added = int(context.user_data.get("editchallenge_photo_count", 0)) + 1
    context.user_data["editchallenge_photo_count"] = added
    total = len(store.get_challenge_photos(int(challenge_id)))
    await update.effective_message.reply_text(
        f"📸 Picture added (#{added} in this batch, {total} total). Send more or /done.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Done Adding Pictures ✅", callback_data="editchal:photos_done"
                    )
                ]
            ]
        ),
    )
    return EDITCHALLENGE_PHOTOS


async def editchallenge_photos_done(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    challenge_id = int(context.user_data.get("editchallenge_id") or 0)
    added = int(context.user_data.pop("editchallenge_photo_count", 0))
    if added > 0:
        _log_action(
            update,
            context,
            "edit_challenge",
            target_type="challenge",
            target_id=challenge_id,
            details=f"Added {added} picture(s) to Challenge #{challenge_id}",
        )
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Finished adding {added} picture(s) to Challenge #{challenge_id}."
        )
        return await _send_editchallenge_menu(
            update.effective_message, update, context, challenge_id
        )
    return ConversationHandler.END


async def editchallenge_photos_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _can_manage_routes(update, context):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END
    challenge_id = int(context.user_data.get("editchallenge_id") or 0)
    added = int(context.user_data.pop("editchallenge_photo_count", 0))
    if added > 0:
        _log_action(
            update,
            context,
            "edit_challenge",
            target_type="challenge",
            target_id=challenge_id,
            details=f"Added {added} picture(s) to Challenge #{challenge_id}",
        )
    await query.answer("Done adding pictures.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(
        f"Finished adding {added} picture(s) to Challenge #{challenge_id}."
    )
    return await _send_editchallenge_menu(query.message, update, context, challenge_id)


# ---------------------------------------------------------------------------
# Submitting & Withdrawing Challenge Proof (/submitchallenge, /withdrawchallenge)
# ---------------------------------------------------------------------------


def _challenge_proof_prompt(chal: Challenge) -> str:
    desc_block = f"\n📝 {chal.description}\n" if chal.description else "\n"
    return (
        f"🎬 Send a video or photo proving you completed Challenge #{chal.id}: '{chal.name}' ({chal.points} pts)."
        f"{desc_block}\n"
        "Send your video or photo proof now, or /cancel."
    )


async def _validate_and_prepare_challenge_submission(
    message: Any, user_id: int, context: ContextTypes.DEFAULT_TYPE, challenge_id: int
) -> int:
    store = _store(context)
    chal = store.get_challenge(challenge_id)
    if chal is None or not chal.active:
        await message.reply_text(
            "That challenge is not active. Check /challenges.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    status = store.get_student_challenge_status(user_id, challenge_id)
    if status == "approved":
        await message.reply_text(
            f"You have already completed Challenge '{chal.name}' and received points for it! 🎉",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    if status == "pending":
        await message.reply_text(
            f"You already have a pending submission for Challenge '{chal.name}'. Please wait for an admin to review it.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    context.user_data["challenge_id"] = chal.id
    await message.reply_text(_challenge_proof_prompt(chal))
    return SUBMIT_CHALLENGE_PROOF


async def prompt_open_challenges_for_submit(
    message: Any, store: LeagueStore, user_id: int
) -> int:
    """List open challenges for a registered student and enter SUBMIT_CHALLENGE state."""
    if not store.is_registered(user_id):
        await message.reply_text(
            "Please register first with /register.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    active_challenges = store.list_challenges(active_only=True)
    if not active_challenges:
        await message.reply_text(
            "No active challenges are available right now.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    statuses = store.get_student_challenge_statuses(user_id)
    open_challenges = [
        c for c in active_challenges if statuses.get(c.id) not in {"approved", "pending"}
    ]
    if not open_challenges:
        await message.reply_text(
            "You have already completed or submitted all active challenges! 🎉",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    chal_lines = "\n".join(
        f"#{c.id}: {c.name} ({c.points} pts)" for c in open_challenges
    )
    kb = [
        [
            InlineKeyboardButton(
                f"🎯 #{c.id} {c.name} ({c.points} pts)",
                callback_data=f"subchal:pick:{c.id}",
            )
        ]
        for c in open_challenges[:20]
    ]
    await _reply_chunked(
        message,
        f"Which challenge are you submitting proof for?\n\n{chal_lines}\n\n"
        "Tap a button below or reply with the challenge number (e.g. 1), or /cancel.",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SUBMIT_CHALLENGE


async def submit_challenge_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle /submitchallenge [challenge_id] entry point."""
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
        return await prompt_open_challenges_for_submit(
            update.effective_message, store, user_id
        )

    challenge_id = _parse_positive_int(parts[1], strip_hash=True)
    if challenge_id is None:
        await update.effective_message.reply_text(
            "Usage: /submitchallenge <challenge_id>",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    return await _validate_and_prepare_challenge_submission(
        update.effective_message, user_id, context, challenge_id
    )


async def submit_challenge_entry_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Enter challenge submission flow directly from '🎬 Submit Proof' on /challenge <id>."""
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return ConversationHandler.END
    parts = (query.data or "").split(":")
    challenge_id = _parse_positive_int(parts[2]) if len(parts) == 3 else None
    if challenge_id is None:
        await query.answer("Invalid challenge.", show_alert=True)
        return ConversationHandler.END
    _clear_conversation_data(context)
    user_id = update.effective_user.id
    if not _store(context).is_registered(user_id):
        await query.answer("Please register first.", show_alert=True)
        await query.message.reply_text(
            "Please register first with /register.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    await query.answer()
    return await _validate_and_prepare_challenge_submission(
        query.message, user_id, context, challenge_id
    )


async def submit_challenge_select_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle inline challenge pick during SUBMIT_CHALLENGE."""
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return ConversationHandler.END
    parts = (query.data or "").split(":")
    challenge_id = _parse_positive_int(parts[2]) if len(parts) == 3 else None
    if challenge_id is None:
        await query.answer("Invalid challenge.", show_alert=True)
        return SUBMIT_CHALLENGE
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    return await _validate_and_prepare_challenge_submission(
        query.message, update.effective_user.id, context, challenge_id
    )


async def submit_challenge_select_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    challenge_id = _parse_positive_int(update.effective_message.text or "", strip_hash=True)
    if challenge_id is None:
        await update.effective_message.reply_text(
            "Please reply with a valid numeric challenge ID (e.g. 1), or /cancel."
        )
        return SUBMIT_CHALLENGE
    return await _validate_and_prepare_challenge_submission(
        update.effective_message, update.effective_user.id, context, challenge_id
    )


def _format_challenge_submission_caption(
    submission_id: int,
    student_name: str,
    student_id: int,
    challenge_id: int,
    challenge_name: str,
    points: int,
    status: str = "pending",
    rejection_reason: Optional[str] = None,
) -> str:
    caption = (
        f"🎯 New Challenge Submission #{submission_id}\n"
        f"Student: {student_name} (ID: {student_id})\n"
        f"Challenge: #{challenge_id} {challenge_name} ({points} pts)"
    )
    if status != "pending":
        caption += f"\nStatus: {status.upper()}"
        if rejection_reason:
            caption += f"\nReason: {rejection_reason}"
    return caption


def _challenge_review_keyboard(
    submission_id: int, *, include_pending_nav: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Approve ✅", callback_data=f"creview:approve:{submission_id}"),
            InlineKeyboardButton("Reject ❌", callback_data=f"creview:reject:{submission_id}"),
        ]
    ]
    if include_pending_nav:
        rows.append(
            [
                InlineKeyboardButton(
                    "⏭️ Next Pending",
                    callback_data=f"pending:next:challenge:{submission_id}",
                ),
                InlineKeyboardButton("📋 Pending List", callback_data="pending:page:0"),
            ]
        )
    return InlineKeyboardMarkup(rows)


async def _send_challenge_submission_to_admin(
    bot: Bot,
    admin_id: int,
    submission_id: int,
    student_name: str,
    student_id: int,
    challenge_id: int,
    challenge_name: str,
    points: int,
    proof_file_id: str,
    media_type: str = "video",
    status: str = "pending",
    rejection_reason: Optional[str] = None,
    *,
    include_pending_nav: bool = False,
) -> Optional[tuple[int, bool]]:
    caption = _format_challenge_submission_caption(
        submission_id=submission_id,
        student_name=student_name,
        student_id=student_id,
        challenge_id=challenge_id,
        challenge_name=challenge_name,
        points=points,
        status=status,
        rejection_reason=rejection_reason,
    )
    keyboard = (
        _challenge_review_keyboard(submission_id, include_pending_nav=include_pending_nav)
        if status == "pending"
        else None
    )
    is_caption = True
    if media_type == "photo":
        sent = await bot.send_photo(
            chat_id=admin_id,
            photo=proof_file_id,
            caption=caption,
            reply_markup=keyboard,
        )
    elif media_type == "video_note":
        await bot.send_video_note(chat_id=admin_id, video_note=proof_file_id)
        sent = await bot.send_message(chat_id=admin_id, text=caption, reply_markup=keyboard)
        is_caption = False
    elif media_type == "document":
        sent = await bot.send_document(
            chat_id=admin_id,
            document=proof_file_id,
            caption=caption,
            reply_markup=keyboard,
        )
    elif media_type == "animation":
        sent = await bot.send_animation(
            chat_id=admin_id,
            animation=proof_file_id,
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        sent = await bot.send_video(
            chat_id=admin_id,
            video=proof_file_id,
            caption=caption,
            reply_markup=keyboard,
        )
    msg_id = getattr(sent, "message_id", None)
    if isinstance(msg_id, int):
        return msg_id, is_caption
    return None


async def send_pending_challenge_review_card(
    bot: Bot,
    store: LeagueStore,
    chat_id: int,
    fallback_message: Any,
    sub: sqlite3.Row,
) -> None:
    """Send a pending challenge submission's proof & review card and track it for multi-admin sync."""
    sub_id = int(sub["id"])
    try:
        sent_info = await _send_challenge_submission_to_admin(
            bot=bot,
            admin_id=chat_id,
            submission_id=sub_id,
            student_name=str(sub["full_name"]),
            student_id=int(sub["telegram_id"]),
            challenge_id=int(sub["challenge_id"]),
            challenge_name=str(sub["challenge_name"]),
            points=int(sub["points"]),
            proof_file_id=str(sub["video_file_id"]),
            media_type=str(sub["media_type"] or "video"),
            status=str(sub["status"]),
            rejection_reason=sub["rejection_reason"],
            include_pending_nav=True,
        )
        if sent_info is not None and str(sub["status"]) == "pending":
            msg_id, is_caption = sent_info
            store.record_admin_challenge_review_message(sub_id, chat_id, msg_id, is_caption)
    except Exception:
        LOGGER.warning("Failed to send pending challenge submission %s proof to admin", sub_id)
        caption = _format_challenge_submission_caption(
            submission_id=sub_id,
            student_name=str(sub["full_name"]),
            student_id=int(sub["telegram_id"]),
            challenge_id=int(sub["challenge_id"]),
            challenge_name=str(sub["challenge_name"]),
            points=int(sub["points"]),
            status=str(sub["status"]),
        )
        sent = await fallback_message.reply_text(
            f"{caption}\n\n⚠️ Could not load proof preview.",
            reply_markup=_challenge_review_keyboard(sub_id, include_pending_nav=True),
        )
        msg_id = getattr(sent, "message_id", None)
        if isinstance(msg_id, int) and str(sub["status"]) == "pending":
            store.record_admin_challenge_review_message(sub_id, chat_id, msg_id, False)


async def receive_challenge_proof(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive uploaded video or photo proof for a challenge, store it, and notify admins."""
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    message = update.effective_message
    proof_info = _extract_challenge_proof_info(message)
    if not proof_info:
        await message.reply_text(
            "Please send a video or photo proving your challenge completion, or /cancel."
        )
        return SUBMIT_CHALLENGE_PROOF

    proof_file_id, media_type = proof_info
    store = _store(context)
    raw_challenge_id = context.user_data.pop("challenge_id", None)
    if raw_challenge_id is None:
        await message.reply_text(
            "Submission session expired. Please run /submitchallenge again.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    challenge_id = int(raw_challenge_id)
    chal = store.get_challenge(challenge_id)

    try:
        submission_id = store.create_challenge_submission(
            update.effective_user.id, challenge_id, proof_file_id, media_type=media_type
        )
    except ValueError as e:
        if str(e) == "already_approved":
            err_msg = "You have already completed this challenge."
        elif str(e) == "already_pending":
            err_msg = "You already have a pending submission for this challenge."
        else:
            err_msg = "Unable to submit: challenge is no longer active."
        await message.reply_text(err_msg, reply_markup=_menu_button_markup())
        return ConversationHandler.END

    _log_action(
        update,
        context,
        "submit_challenge",
        target_type="challenge_submission",
        target_id=submission_id,
        details=f"Challenge #{challenge_id} '{chal.name if chal else ''}' ({media_type})",
    )
    await message.reply_text(
        f"Challenge proof submitted (#{submission_id}). An admin will review your submission.",
        reply_markup=_menu_button_markup(),
    )

    student_row = store.get_student(update.effective_user.id)
    student_name = (
        str(student_row["full_name"])
        if student_row
        else (update.effective_user.full_name or "Student")
    )
    if chal is not None:
        for admin_id in sorted(_admin_ids()):
            try:
                sent_info = await _send_challenge_submission_to_admin(
                    bot=context.bot,
                    admin_id=admin_id,
                    submission_id=submission_id,
                    student_name=student_name,
                    student_id=update.effective_user.id,
                    challenge_id=chal.id,
                    challenge_name=chal.name,
                    points=chal.points,
                    proof_file_id=proof_file_id,
                    media_type=media_type,
                    status="pending",
                )
                if sent_info is not None:
                    msg_id, is_caption = sent_info
                    store.record_admin_challenge_review_message(
                        submission_id, admin_id, msg_id, is_caption
                    )
            except Exception:
                LOGGER.warning("Could not send challenge proof preview to admin %s", admin_id)

    return ConversationHandler.END


async def perform_withdraw_challenge(
    bot: Bot, store: LeagueStore, user_id: int, challenge_id: int
) -> tuple[bool, str]:
    withdrawn = store.withdraw_pending_challenge_submission(user_id, challenge_id)
    if withdrawn is None:
        return False, f"You have no pending submission for challenge #{challenge_id}."
    for row in withdrawn["admin_messages"]:
        try:
            await bot.edit_message_reply_markup(
                chat_id=int(row["chat_id"]),
                message_id=int(row["message_id"]),
                reply_markup=None,
            )
        except Exception:
            pass
    return (
        True,
        f"Withdrew pending challenge submission #{withdrawn['id']} for '{withdrawn['challenge_name']}'.",
    )


async def withdraw_challenge_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Withdraw a student's own pending challenge submission via /withdrawchallenge <challenge_id>."""
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
            "Usage: /withdrawchallenge <challenge_id>",
            reply_markup=_menu_button_markup(),
        )
        return
    challenge_id = _parse_positive_int(context.args[0], strip_hash=True)
    if challenge_id is None:
        await update.effective_message.reply_text(
            "Usage: /withdrawchallenge <challenge_id>",
            reply_markup=_menu_button_markup(),
        )
        return

    ok, reply_msg = await perform_withdraw_challenge(
        context.bot, store, user_id, challenge_id
    )
    if ok:
        _log_action(
            update,
            context,
            "withdraw_challenge",
            target_type="challenge",
            target_id=challenge_id,
            details=reply_msg,
        )
    await update.effective_message.reply_text(
        reply_msg, reply_markup=_menu_button_markup()
    )


# ---------------------------------------------------------------------------
# Admin Challenge Submission Review
# ---------------------------------------------------------------------------


async def _sync_admin_challenge_review_cards(
    bot: Bot,
    store: LeagueStore,
    submission: sqlite3.Row,
    decision_label: str,
    *,
    exclude_chat_id: Optional[int] = None,
    exclude_message_id: Optional[int] = None,
) -> None:
    sub_id = int(submission["id"])
    rows = store.get_admin_challenge_review_messages(sub_id)
    if not rows:
        return
    base_caption = _format_challenge_submission_caption(
        submission_id=sub_id,
        student_name=str(submission["full_name"]),
        student_id=int(submission["telegram_id"]),
        challenge_id=int(submission["challenge_id"]),
        challenge_name=str(submission["challenge_name"]),
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
            LOGGER.debug("Could not sync admin challenge card %s/%s", chat_id, message_id)
    store.delete_admin_challenge_review_messages(sub_id)


async def _finalize_challenge_review(
    bot: Bot,
    store: LeagueStore,
    submission: sqlite3.Row,
    *,
    status: str,
    decision_label: str,
    rejection_reason: Optional[str] = None,
    card_query: Any = None,
    card_info: Optional[tuple[Optional[int], Optional[int], bool, str]] = None,
    reviewer_reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    sub_id = int(submission["id"])
    exclude_chat_id: Optional[int] = None
    exclude_message_id: Optional[int] = None

    if card_query is not None:
        if getattr(card_query.message, "caption", None) is not None:
            current_caption = card_query.message.caption or ""
            try:
                await card_query.edit_message_caption(
                    caption=f"{current_caption}\n\nDecision: {decision_label}",
                    reply_markup=reviewer_reply_markup,
                )
            except Exception:
                pass
        else:
            current_text = getattr(card_query.message, "text", "") or ""
            try:
                await card_query.edit_message_text(
                    text=f"{current_text}\n\nDecision: {decision_label}",
                    reply_markup=reviewer_reply_markup,
                )
            except Exception:
                pass
        c_id = getattr(getattr(card_query.message, "chat", None), "id", None)
        m_id = getattr(card_query.message, "message_id", None)
        exclude_chat_id = c_id if isinstance(c_id, int) else None
        exclude_message_id = m_id if isinstance(m_id, int) else None
    elif card_info is not None:
        c_chat, c_msg, is_caption, base_text = card_info
        if isinstance(c_chat, int) and isinstance(c_msg, int) and base_text:
            updated = f"{base_text}\n\nDecision: {decision_label}"
            try:
                if is_caption:
                    await bot.edit_message_caption(
                        chat_id=c_chat, message_id=c_msg, caption=updated, reply_markup=None
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=c_chat, message_id=c_msg, text=updated, reply_markup=None
                    )
            except Exception:
                pass
            exclude_chat_id = c_chat
            exclude_message_id = c_msg

    await _sync_admin_challenge_review_cards(
        bot,
        store,
        submission,
        decision_label,
        exclude_chat_id=exclude_chat_id,
        exclude_message_id=exclude_message_id,
    )

    student_id = int(submission["telegram_id"])
    chal_name = str(submission["challenge_name"])
    points = int(submission["points"])
    if status == "approved":
        msg = f"🎉 Great job! Your submission for Challenge '{chal_name}' was approved (+{points} points)!"
    elif rejection_reason:
        msg = (
            f"❌ Your submission for Challenge '{chal_name}' was rejected.\n"
            f"Reason: {rejection_reason}\n"
            "You can submit a new attempt with improved proof."
        )
    else:
        msg = (
            f"❌ Your submission for Challenge '{chal_name}' was rejected. "
            "You can submit a new attempt with improved proof."
        )
    try:
        await bot.send_message(
            chat_id=student_id, text=msg, reply_markup=_menu_button_markup()
        )
    except Exception:
        LOGGER.warning("Failed to notify student %s about challenge submission review", student_id)


async def challenge_review_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle Approve/Reject inline buttons on challenge submission cards."""
    query = update.callback_query
    if not query:
        return
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "creview":
        await query.answer("Invalid review action.", show_alert=True)
        return
    action, sub_id_str = parts[1], parts[2]
    sub_id = _parse_positive_int(sub_id_str)
    if action not in {"approve", "reject", "reject_now", "back"} or sub_id is None:
        await query.answer("Invalid review action.", show_alert=True)
        return

    store = _store(context)
    sub = store.get_challenge_submission(sub_id)
    if not sub or str(sub["status"]) != "pending":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("This submission was already reviewed.", show_alert=True)
        return

    if action == "back":
        await query.answer()
        try:
            await query.edit_message_reply_markup(
                reply_markup=_challenge_review_keyboard(sub_id)
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
                        "Add Reason ✏️", callback_data=f"creview:reject_reason:{sub_id}"
                    ),
                    InlineKeyboardButton(
                        "Reject Without Reason ❌",
                        callback_data=f"creview:reject_now:{sub_id}",
                    ),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"creview:back:{sub_id}")],
            ]
        )
        try:
            await query.edit_message_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return

    status = "approved" if action == "approve" else "rejected"
    reviewed = store.review_challenge_submission(
        sub_id, status, reviewer_id=update.effective_user.id
    )
    if not reviewed:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("This submission was already reviewed.", show_alert=True)
        return

    _log_action(
        update,
        context,
        "approve_challenge_submission" if status == "approved" else "reject_challenge_submission",
        target_type="challenge_submission",
        target_id=sub_id,
        details=f"Challenge sub #{sub_id} ({reviewed['full_name']} — {reviewed['challenge_name']}) {status}",
    )
    await query.answer(f"Challenge submission #{sub_id} {status}.")
    admin_display = update.effective_user.first_name or "Admin"
    decision_text = "APPROVED ✅" if action == "approve" else "REJECTED ❌"
    await _finalize_challenge_review(
        context.bot,
        store,
        reviewed,
        status=status,
        decision_label=f"{decision_text} by {admin_display}",
        card_query=query,
        reviewer_reply_markup=_post_review_nav_markup(store),
    )


async def challenge_review_prompt_entry_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Enter conversation state to collect a rejection reason for a challenge submission."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return ConversationHandler.END
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "creview" or parts[1] != "reject_reason":
        await query.answer("Invalid review action.", show_alert=True)
        return ConversationHandler.END
    sub_id = _parse_positive_int(parts[2])
    if sub_id is None:
        await query.answer("Invalid submission ID.", show_alert=True)
        return ConversationHandler.END

    store = _store(context)
    sub = store.get_challenge_submission(sub_id)
    if not sub or str(sub["status"]) != "pending":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("This submission was already reviewed.", show_alert=True)
        return ConversationHandler.END

    _clear_conversation_data(context)
    context.user_data["challenge_review_submission_id"] = sub_id
    if query.message:
        chat_obj = getattr(query.message, "chat", None)
        context.user_data["challenge_review_card_chat_id"] = getattr(chat_obj, "id", None)
        context.user_data["challenge_review_card_message_id"] = getattr(
            query.message, "message_id", None
        )
        is_cap = getattr(query.message, "caption", None) is not None
        context.user_data["challenge_review_card_is_caption"] = is_cap
        context.user_data["challenge_review_card_base_text"] = (
            query.message.caption if is_cap else getattr(query.message, "text", "")
        ) or ""

    await query.answer()
    if query.message:
        await query.message.reply_text(
            f"✏️ Send the rejection reason for Challenge Submission #{sub_id} (or /cancel):"
        )
    return CHALLENGE_REVIEW_REJECT_REASON


async def challenge_review_reject_reason_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    reason = _sanitize_single_line(update.effective_message.text or "")
    if not reason:
        await update.effective_message.reply_text(
            "Please enter a non-empty rejection reason, or /cancel."
        )
        return CHALLENGE_REVIEW_REJECT_REASON

    sub_id = context.user_data.get("challenge_review_submission_id")
    card_info = (
        context.user_data.get("challenge_review_card_chat_id"),
        context.user_data.get("challenge_review_card_message_id"),
        bool(context.user_data.get("challenge_review_card_is_caption", True)),
        str(context.user_data.get("challenge_review_card_base_text") or ""),
    )
    _clear_conversation_data(context)
    if sub_id is None:
        await update.effective_message.reply_text(
            "Review session expired.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END

    store = _store(context)
    reviewed = store.review_challenge_submission(
        int(sub_id), "rejected", reviewer_id=update.effective_user.id, rejection_reason=reason
    )
    if not reviewed:
        await update.effective_message.reply_text(
            "Challenge submission not found or already reviewed.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    _log_action(
        update,
        context,
        "reject_challenge_submission",
        target_type="challenge_submission",
        target_id=int(sub_id),
        details=f"Challenge sub #{sub_id} ({reviewed['full_name']} — {reviewed['challenge_name']}) rejected: {reason}",
    )
    await update.effective_message.reply_text(
        f"Challenge submission #{sub_id} rejected (Reason: {reason}).",
        reply_markup=_post_review_nav_markup(store),
    )
    admin_display = update.effective_user.first_name or "Admin"
    await _finalize_challenge_review(
        context.bot,
        store,
        reviewed,
        status="rejected",
        decision_label=f"REJECTED ❌ ({reason}) by {admin_display}",
        rejection_reason=reason,
        card_info=card_info,
    )
    return ConversationHandler.END


async def all_challenge_submissions_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """List all challenge submissions for admins (/challengesubmissions [filter])."""
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
        if status_filter not in {"all", "pending", "approved", "rejected"}:
            await update.effective_message.reply_text(
                "Usage: /challengesubmissions [all|pending|approved|rejected]",
                reply_markup=_menu_button_markup(),
            )
            return
    rows = _store(context).all_challenge_submissions(status_filter=status_filter)
    if not rows:
        await update.effective_message.reply_text(
            "No challenge submissions found.", reply_markup=_menu_button_markup()
        )
        return

    lines: list[str] = ["🎯 Challenge Submissions:"]
    for row in rows:
        st = str(row["status"])
        if st == "approved":
            badge = f"✅ APPROVED ({row['points']} pts)"
        elif st == "rejected":
            reason_suffix = f" - {row['rejection_reason']}" if row["rejection_reason"] else ""
            badge = f"❌ REJECTED{reason_suffix}"
        else:
            badge = "⏳ PENDING"
        lines.append(
            f"#{row['id']} {row['full_name']} (@{row['username']}) — "
            f"#{row['challenge_id']} {row['challenge_name']} [{badge}]"
        )
    lines.append("\nTip: Use /challengesubmission <id> to view proof & review card.")
    await _reply_chunked(
        update.effective_message, "\n".join(lines), reply_markup=_menu_button_markup()
    )


async def view_challenge_submission_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send a specific challenge submission's proof and review card to an admin."""
    if not update.effective_message or not update.effective_chat:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /challengesubmission <submission_id>",
            reply_markup=_menu_button_markup(),
        )
        return
    sub_id = _parse_positive_int(context.args[0], strip_hash=True)
    if sub_id is None:
        await update.effective_message.reply_text(
            "Usage: /challengesubmission <submission_id>",
            reply_markup=_menu_button_markup(),
        )
        return
    store = _store(context)
    sub = store.get_challenge_submission(sub_id)
    if not sub:
        await update.effective_message.reply_text(
            f"Challenge submission #{sub_id} not found.",
            reply_markup=_menu_button_markup(),
        )
        return
    try:
        is_pending = str(sub["status"]) == "pending"
        sent_info = await _send_challenge_submission_to_admin(
            bot=context.bot,
            admin_id=update.effective_chat.id,
            submission_id=int(sub["id"]),
            student_name=str(sub["full_name"]),
            student_id=int(sub["telegram_id"]),
            challenge_id=int(sub["challenge_id"]),
            challenge_name=str(sub["challenge_name"]),
            points=int(sub["points"]),
            proof_file_id=str(sub["video_file_id"]),
            media_type=str(sub["media_type"] or "video"),
            status=str(sub["status"]),
            rejection_reason=sub["rejection_reason"],
            include_pending_nav=is_pending,
        )
        if sent_info is not None and is_pending:
            msg_id, is_caption = sent_info
            store.record_admin_challenge_review_message(
                int(sub["id"]), update.effective_chat.id, msg_id, is_caption
            )
    except Exception:
        LOGGER.warning("Failed to send challenge submission %s proof to admin", sub_id)
        await update.effective_message.reply_text(
            f"Challenge submission #{sub_id} by {sub['full_name']} for '{sub['challenge_name']}' "
            f"(Status: {sub['status']}) — could not load proof preview.",
            reply_markup=_menu_button_markup(),
        )


async def review_challenge_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Approve or reject a pending challenge submission via /approvechallenge or /rejectchallenge."""
    if not update.effective_user or not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    cmd_token = (update.effective_message.text or "").split()[0].lower()
    is_approve = cmd_token.startswith("/approve")
    sub_id = _parse_positive_int(context.args[0], strip_hash=True) if context.args else None
    if sub_id is None or (is_approve and len(context.args) != 1):
        await update.effective_message.reply_text(
            "Usage: /approvechallenge <id> or /rejectchallenge <id> [reason]",
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
    reviewed = store.review_challenge_submission(
        sub_id, status, reviewer_id=update.effective_user.id, rejection_reason=rejection_reason
    )
    if not reviewed:
        await update.effective_message.reply_text(
            "Challenge submission not found or already reviewed.",
            reply_markup=_menu_button_markup(),
        )
        return

    _log_action(
        update,
        context,
        "approve_challenge_submission" if is_approve else "reject_challenge_submission",
        target_type="challenge_submission",
        target_id=sub_id,
        details=f"Challenge sub #{sub_id} ({reviewed['full_name']} — {reviewed['challenge_name']}) {status}"
        + (f" ({rejection_reason})" if rejection_reason else ""),
    )
    await update.effective_message.reply_text(
        f"Challenge submission #{sub_id} {status}.",
        reply_markup=_menu_button_markup(),
    )
    admin_display = update.effective_user.first_name or "Admin"
    decision_text = "APPROVED ✅" if is_approve else "REJECTED ❌"
    if rejection_reason:
        decision_text += f" ({rejection_reason})"
    await _finalize_challenge_review(
        context.bot,
        store,
        reviewed,
        status=status,
        decision_label=f"{decision_text} by {admin_display}",
        rejection_reason=rejection_reason,
    )


async def reopen_challenge_submission_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reopen a closed challenge submission back to pending via /reopenchallenge <id>."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    sub_id = _parse_positive_int(context.args[0], strip_hash=True) if context.args else None
    if sub_id is None:
        await update.effective_message.reply_text(
            "Usage: /reopenchallenge <submission_id>",
            reply_markup=_menu_button_markup(),
        )
        return
    sub, error = _store(context).reopen_challenge_submission(sub_id)
    if error == "not_found":
        await update.effective_message.reply_text(
            f"Challenge submission #{sub_id} not found.",
            reply_markup=_menu_button_markup(),
        )
        return
    if error == "already_pending":
        await update.effective_message.reply_text(
            f"Challenge submission #{sub_id} is already pending review.",
            reply_markup=_menu_button_markup(),
        )
        return
    if error == "conflict_active_submission":
        await update.effective_message.reply_text(
            f"Cannot reopen challenge submission #{sub_id}: student already has another pending or approved submission for that challenge.",
            reply_markup=_menu_button_markup(),
        )
        return

    _log_action(
        update,
        context,
        "reopen_challenge_submission",
        target_type="challenge_submission",
        target_id=sub_id,
        details=f"Reopened challenge submission #{sub_id} ({sub['full_name']} — {sub['challenge_name']})",
    )
    await update.effective_message.reply_text(
        f"Challenge submission #{sub_id} reopened to pending. Use /challengesubmission {sub_id} to review it.",
        reply_markup=_menu_button_markup(),
    )
    try:
        await context.bot.send_message(
            chat_id=int(sub["telegram_id"]),
            text=(
                f"🔄 Your challenge submission #{sub_id} for '{sub['challenge_name']}' "
                "has been reopened for re-evaluation by an admin."
            ),
            reply_markup=_menu_button_markup(),
        )
    except Exception:
        LOGGER.warning(
            "Failed to notify student %s about reopened challenge submission",
            sub["telegram_id"],
        )
