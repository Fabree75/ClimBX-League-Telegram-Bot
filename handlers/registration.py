"""Registration, optional username, profile editing, and admin profile change review handlers."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from common import (
    EDITPROFILE_CONFIRM,
    EDITPROFILE_EMAIL,
    EDITPROFILE_MENU,
    EDITPROFILE_NAME,
    EDITPROFILE_USERNAME,
    REGISTER_CONFIRM,
    REGISTER_EMAIL,
    REGISTER_NAME,
    REGISTER_USERNAME,
    _admin_ids,
    _clear_conversation_data,
    _is_admin,
    _is_valid_email,
    _menu_button_markup,
    _parse_positive_int,
    _reply_chunked,
    _sanitize_single_line,
    _store,
)
from store import LeagueStore

LOGGER = logging.getLogger(__name__)


def _format_username_display(username: Optional[str]) -> str:
    return f"@{username}" if username else "Not set (full name will be shown)"


def _format_short_username(username: Optional[str]) -> str:
    return f"@{username}" if username else "no username"


def _format_reg_request_text(req: sqlite3.Row) -> str:
    old_u = _format_short_username(req["old_username"])
    new_u = _format_short_username(req["username"])
    return (
        f"🪪 Profile Modification Request #{req['id']}\n"
        f"Student ID: {req['telegram_id']}\n"
        f"Current: {req['old_full_name']} ({old_u}, {req['old_email']})\n"
        f"Requested: {req['full_name']} ({new_u}, {req['email']})"
    )


async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start new student registration (or direct existing students to /editprofile)."""
    message = update.effective_message
    if not update.effective_user or not message:
        return ConversationHandler.END
    _clear_conversation_data(context)
    if getattr(update.effective_chat, "type", None) in {"group", "supergroup"}:
        await message.reply_text(
            "Please send /register in a private chat with me so your email stays private."
        )
        return ConversationHandler.END

    store = _store(context)
    user_id = update.effective_user.id
    existing = store.get_student(user_id)
    if existing:
        await message.reply_text(
            f"You are already registered as {existing['full_name']}!\n"
            "Use /editprofile (or the Edit Profile button in the menu) to modify your profile.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    await message.reply_text("What is your full name?")
    return REGISTER_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save full name and prompt for optional leaderboard username."""
    if not update.effective_message:
        return ConversationHandler.END
    name = _sanitize_single_line(update.effective_message.text or "")
    if not name:
        await update.effective_message.reply_text("Please enter a valid full name.")
        return REGISTER_NAME
    context.user_data["full_name"] = name
    await update.effective_message.reply_text(
        "What username/nickname would you like displayed on the leaderboard?\n"
        "(Optional — send a username, tap Skip Username ➡️, or send /skip to use your full name)",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Skip Username ➡️", callback_data="reg:skip_username")]]
        ),
    )
    return REGISTER_USERNAME


async def register_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save optional username (or skip if 'none'/'skip') and ask for @polytechnique.edu email."""
    if not update.effective_message:
        return ConversationHandler.END
    raw = (update.effective_message.text or "").strip()
    if raw.lower() in {"skip", "none"}:
        context.user_data["username"] = None
    else:
        username = _sanitize_single_line(raw.lstrip("@"))
        if not username:
            await update.effective_message.reply_text(
                "Please enter a valid username, tap Skip Username ➡️, or /cancel."
            )
            return REGISTER_USERNAME
        context.user_data["username"] = username
    await update.effective_message.reply_text(
        "What is your École Polytechnique email address (name@polytechnique.edu)?"
    )
    return REGISTER_EMAIL


async def register_skip_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skip optional username via /skip command or inline button during /register."""
    context.user_data["username"] = None
    if update.callback_query:
        query = update.callback_query
        await query.answer("Username skipped.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if query.message:
            await query.message.reply_text(
                "Skipped username (your full name will be shown on the leaderboard).\n"
                "What is your École Polytechnique email address (name@polytechnique.edu)?"
            )
    elif update.effective_message:
        await update.effective_message.reply_text(
            "Skipped username (your full name will be shown on the leaderboard).\n"
            "What is your École Polytechnique email address (name@polytechnique.edu)?"
        )
    return REGISTER_EMAIL


async def register_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate @polytechnique.edu email and display confirmation summary with warning."""
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    email = (update.effective_message.text or "").strip().lower()
    if not _is_valid_email(email):
        await update.effective_message.reply_text(
            "Please send a valid @polytechnique.edu email address (e.g. firstname.lastname@polytechnique.edu)."
        )
        return REGISTER_EMAIL

    full_name = context.user_data.get("full_name")
    username = context.user_data.get("username")
    if not full_name:
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            "Registration session expired. Please run /register again.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    context.user_data["email"] = email
    summary_text = (
        "📋 Please confirm your registration details:\n"
        f"• Full Name: {full_name}\n"
        f"• Username: {_format_username_display(username)}\n"
        f"• Email: {email}\n\n"
        "⚠️ Warning: Please check your information carefully. Your full name and email address "
        "cannot be freely modified after registration (any future changes to them will require admin approval)."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm & Register ✅", callback_data="reg:confirm"),
                InlineKeyboardButton("Cancel ❌", callback_data="reg:cancel"),
            ]
        ]
    )
    await update.effective_message.reply_text(summary_text, reply_markup=kb)
    return REGISTER_CONFIRM


async def _complete_registration(
    message: Any, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> int:
    full_name = context.user_data.get("full_name")
    username = context.user_data.get("username")
    email = context.user_data.get("email")
    _clear_conversation_data(context)
    if not full_name or not email:
        await message.reply_text(
            "Registration session expired. Please run /register again.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    _store(context).register_student(user_id, full_name, email, username=username)
    await message.reply_text(
        "Registration saved. Good luck climbing!",
        reply_markup=_menu_button_markup(),
    )
    return ConversationHandler.END


async def register_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle Confirm or Cancel buttons on the registration confirmation step."""
    query = update.callback_query
    if not query or not update.effective_user:
        return ConversationHandler.END
    data = query.data or ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if data == "reg:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        if query.message:
            await query.message.reply_text("Cancelled.", reply_markup=_menu_button_markup())
        return ConversationHandler.END

    if data == "reg:confirm":
        await query.answer("Registration confirmed!")
        if query.message:
            return await _complete_registration(
                query.message, update.effective_user.id, context
            )
        _clear_conversation_data(context)
        return ConversationHandler.END

    await query.answer("Invalid action.", show_alert=True)
    return REGISTER_CONFIRM


async def register_confirm_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Allow typing yes/confirm or no/cancel during REGISTER_CONFIRM."""
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    raw = (update.effective_message.text or "").strip().lower()
    if raw in {"yes", "y", "confirm", "ok"}:
        return await _complete_registration(
            update.effective_message, update.effective_user.id, context
        )
    if raw in {"no", "n", "cancel"}:
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            "Cancelled.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "Please tap Confirm & Register ✅ or Cancel ❌ above (or reply 'confirm' or /cancel)."
    )
    return REGISTER_CONFIRM


def _format_editprofile_menu_text(
    existing: sqlite3.Row,
    context: ContextTypes.DEFAULT_TYPE,
    pending_req: Optional[sqlite3.Row],
) -> str:
    draft_name = str(context.user_data.get("editprof_full_name") or existing["full_name"])
    draft_user = context.user_data.get("editprof_username")
    draft_email = str(context.user_data.get("editprof_email") or existing["email"])

    name_tag = " (modified — requires admin approval)" if draft_name != existing["full_name"] else ""
    user_tag = " (modified)" if draft_user != existing["username"] else ""
    email_tag = " (modified — requires admin approval)" if draft_email != existing["email"] else ""

    lines = [
        "✏️ Edit Your Climber Profile",
        f"• Full Name: {draft_name}{name_tag}",
        f"• Username: {_format_username_display(draft_user)}{user_tag}",
        f"• Email: {draft_email}{email_tag}",
        "",
        "ℹ️ Username updates are applied immediately after confirmation. "
        "Modifying your Full Name or Email requires admin approval.",
    ]
    if pending_req is not None:
        lines.append(
            f"\n⏳ Pending Admin Request #{pending_req['id']}: "
            f"{pending_req['full_name']} ({_format_short_username(pending_req['username'])}, {pending_req['email']})"
        )
    lines.append("\nSelect a field below to edit, then tap Review & Confirm ✅:")
    return "\n".join(lines)


def _editprofile_menu_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
) -> InlineKeyboardMarkup:
    draft_user = context.user_data.get("editprof_username")
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("✏️ Full Name", callback_data="editprof:field:name"),
            InlineKeyboardButton("🏷️ Username", callback_data="editprof:field:username"),
        ],
        [
            InlineKeyboardButton("📧 Email", callback_data="editprof:field:email"),
        ],
    ]
    if draft_user is not None:
        rows[1].append(
            InlineKeyboardButton("🗑️ Clear Username", callback_data="editprof:clear_username")
        )
    rows.append(
        [
            InlineKeyboardButton("Review & Confirm ✅", callback_data="editprof:review"),
            InlineKeyboardButton("Cancel ❌", callback_data="editprof:cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _send_editprofile_menu(
    message: Any, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> int:
    store = _store(context)
    existing = store.get_student(user_id)
    if existing is None:
        _clear_conversation_data(context)
        await message.reply_text(
            "You are not registered yet. Use /register to join the league!",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    pending_req = store.get_pending_registration_request(user_id)
    await message.reply_text(
        _format_editprofile_menu_text(existing, context, pending_req),
        reply_markup=_editprofile_menu_keyboard(context),
    )
    return EDITPROFILE_MENU


async def editprofile_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open the interactive /editprofile dashboard for a registered student."""
    message = update.effective_message
    if not update.effective_user or not message:
        return ConversationHandler.END
    _clear_conversation_data(context)
    if getattr(update.effective_chat, "type", None) in {"group", "supergroup"}:
        await message.reply_text(
            "Please use /editprofile in a private chat with me so your email stays private."
        )
        return ConversationHandler.END

    store = _store(context)
    user_id = update.effective_user.id
    existing = store.get_student(user_id)
    if existing is None:
        await message.reply_text(
            "You are not registered yet. Use /register to join the league!",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    context.user_data["editprof_full_name"] = str(existing["full_name"])
    context.user_data["editprof_username"] = (
        str(existing["username"]) if existing["username"] is not None else None
    )
    context.user_data["editprof_email"] = str(existing["email"])
    return await _send_editprofile_menu(message, user_id, context)


async def editprofile_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle button selections in the /editprofile menu."""
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return ConversationHandler.END
    user_id = update.effective_user.id
    store = _store(context)
    existing = store.get_student(user_id)
    if existing is None:
        _clear_conversation_data(context)
        await query.answer("Not registered.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    if data == "editprof:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("Cancelled.", reply_markup=_menu_button_markup())
        return ConversationHandler.END

    if data == "editprof:clear_username":
        context.user_data["editprof_username"] = None
        await query.answer("Username cleared in draft.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await _send_editprofile_menu(query.message, user_id, context)

    if data == "editprof:field:username":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            "🏷️ Send your new leaderboard username (or tap Clear Username 🗑️ / send 'none' to remove it, or /cancel):",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Clear Username 🗑️", callback_data="editprof:clear_username")]]
            ),
        )
        return EDITPROFILE_USERNAME

    if data in {"editprof:field:name", "editprof:field:email"}:
        pending_req = store.get_pending_registration_request(user_id)
        if pending_req is not None:
            await query.answer(
                f"You already have a pending profile request (#{pending_req['id']}) awaiting admin review.",
                show_alert=True,
            )
            return EDITPROFILE_MENU
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if data == "editprof:field:name":
            await query.message.reply_text(
                "✏️ Send the new full name you want to request (or /cancel):"
            )
            return EDITPROFILE_NAME
        await query.message.reply_text(
            "📧 Send the new École Polytechnique email address (name@polytechnique.edu) you want to request (or /cancel):"
        )
        return EDITPROFILE_EMAIL

    if data == "editprof:review":
        draft_name = str(context.user_data.get("editprof_full_name") or existing["full_name"])
        draft_user = context.user_data.get("editprof_username")
        draft_email = str(context.user_data.get("editprof_email") or existing["email"])

        name_changed = draft_name != str(existing["full_name"])
        user_changed = draft_user != existing["username"]
        email_changed = draft_email != str(existing["email"])

        if not (name_changed or user_changed or email_changed):
            await query.answer("You haven't changed any fields yet.", show_alert=True)
            return EDITPROFILE_MENU

        if (name_changed or email_changed) and store.get_pending_registration_request(user_id) is not None:
            await query.answer(
                "You already have a pending Full Name / Email request awaiting admin approval.",
                show_alert=True,
            )
            return EDITPROFILE_MENU

        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        if name_changed or email_changed:
            action_note = (
                "⚠️ Warning: Your full name and email address cannot be freely modified. "
                "Confirming will submit these changes as a request to the admins for approval."
            )
        else:
            action_note = (
                "⚠️ Warning: Your full name and email address cannot be freely modified "
                "(only your username was changed, which will be saved immediately)."
            )

        confirm_text = (
            "📋 Please confirm your updated profile details:\n"
            f"• Full Name: {draft_name}\n"
            f"• Username: {_format_username_display(draft_user)}\n"
            f"• Email: {draft_email}\n\n"
            f"{action_note}"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Confirm Changes ✅", callback_data="editprof:confirm"),
                    InlineKeyboardButton("⬅️ Back", callback_data="editprof:back"),
                ],
                [InlineKeyboardButton("Cancel ❌", callback_data="editprof:cancel")],
            ]
        )
        await query.message.reply_text(confirm_text, reply_markup=kb)
        return EDITPROFILE_CONFIRM

    await query.answer("Invalid option.", show_alert=True)
    return EDITPROFILE_MENU


async def editprofile_name_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Save draft full name during /editprofile and return to the edit menu."""
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    name = _sanitize_single_line(update.effective_message.text or "")
    if not name:
        await update.effective_message.reply_text("Please enter a valid non-empty full name, or /cancel.")
        return EDITPROFILE_NAME
    context.user_data["editprof_full_name"] = name
    return await _send_editprofile_menu(
        update.effective_message, update.effective_user.id, context
    )


async def editprofile_username_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Save or clear draft username during /editprofile and return to the edit menu."""
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    raw = (update.effective_message.text or "").strip()
    if raw.lower() in {"none", "skip", "clear"}:
        context.user_data["editprof_username"] = None
    else:
        cleaned = _sanitize_single_line(raw.lstrip("@"))
        if not cleaned:
            await update.effective_message.reply_text(
                "Please enter a valid username (or 'none' to clear), or /cancel."
            )
            return EDITPROFILE_USERNAME
        context.user_data["editprof_username"] = cleaned
    return await _send_editprofile_menu(
        update.effective_message, update.effective_user.id, context
    )


async def editprofile_email_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Validate and save draft @polytechnique.edu email during /editprofile."""
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    email = (update.effective_message.text or "").strip().lower()
    if not _is_valid_email(email):
        await update.effective_message.reply_text(
            "Please send a valid @polytechnique.edu email address (e.g. firstname.lastname@polytechnique.edu), or /cancel."
        )
        return EDITPROFILE_EMAIL
    context.user_data["editprof_email"] = email
    return await _send_editprofile_menu(
        update.effective_message, update.effective_user.id, context
    )


async def editprofile_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Apply confirmed profile edits (instant for username-only, admin request for name/email)."""
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return ConversationHandler.END
    user_id = update.effective_user.id
    store = _store(context)
    existing = store.get_student(user_id)
    if existing is None:
        _clear_conversation_data(context)
        await query.answer("Not registered.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if data == "editprof:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        await query.message.reply_text("Cancelled.", reply_markup=_menu_button_markup())
        return ConversationHandler.END

    if data == "editprof:back":
        await query.answer()
        return await _send_editprofile_menu(query.message, user_id, context)

    if data != "editprof:confirm":
        await query.answer("Invalid option.", show_alert=True)
        return EDITPROFILE_CONFIRM

    draft_name = str(context.user_data.get("editprof_full_name") or existing["full_name"])
    draft_user = context.user_data.get("editprof_username")
    draft_email = str(context.user_data.get("editprof_email") or existing["email"])
    _clear_conversation_data(context)

    name_changed = draft_name != str(existing["full_name"])
    email_changed = draft_email != str(existing["email"])

    if not name_changed and not email_changed:
        store.update_student_username(user_id, draft_user)
        await query.answer("Profile updated!")
        u_label = f"@{draft_user}" if draft_user else "cleared (full name will be shown)"
        await query.message.reply_text(
            f"✅ Profile updated! Your leaderboard username is now {u_label}.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    try:
        req_id = store.create_registration_request(
            user_id, draft_name, draft_user, draft_email
        )
    except ValueError:
        await query.answer("Request already pending.", show_alert=True)
        await query.message.reply_text(
            "You already have a pending profile modification request awaiting admin review.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    await query.answer("Profile modification request submitted!")
    await query.message.reply_text(
        f"📨 Profile modification request (#{req_id}) submitted! An admin will review your request.",
        reply_markup=_menu_button_markup(),
    )
    req = store.get_registration_request(req_id)
    if req is not None:
        card_text = _format_reg_request_text(req)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve ✅", callback_data=f"regreq:approve:{req_id}"),
                    InlineKeyboardButton("Reject ❌", callback_data=f"regreq:reject:{req_id}"),
                ]
            ]
        )
        for admin_id in sorted(_admin_ids()):
            try:
                sent = await context.bot.send_message(
                    chat_id=admin_id, text=card_text, reply_markup=kb
                )
                if hasattr(sent, "message_id") and isinstance(sent.message_id, int):
                    store.record_admin_reg_request_message(req_id, admin_id, sent.message_id)
            except Exception:
                LOGGER.warning("Could not send profile modification request to admin %s", admin_id)
    return ConversationHandler.END


async def _sync_admin_reg_request_cards(
    bot: Bot,
    store: LeagueStore,
    req: sqlite3.Row,
    decision_label: str,
    *,
    exclude_chat_id: Optional[int] = None,
    exclude_message_id: Optional[int] = None,
) -> None:
    req_id = int(req["id"])
    rows = store.get_admin_reg_request_messages(req_id)
    if not rows:
        return
    updated_text = f"{_format_reg_request_text(req)}\n\nDecision: {decision_label}"
    for row in rows:
        chat_id = int(row["chat_id"])
        message_id = int(row["message_id"])
        if chat_id == exclude_chat_id and message_id == exclude_message_id:
            continue
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=updated_text, reply_markup=None
            )
        except Exception:
            LOGGER.debug("Could not sync admin reg request card %s/%s", chat_id, message_id)
    store.delete_admin_reg_request_messages(req_id)


async def _finalize_reg_request_review(
    bot: Bot,
    store: LeagueStore,
    reviewed: sqlite3.Row,
    status: str,
    admin_display: str,
    reason: Optional[str] = None,
    *,
    exclude_chat_id: Optional[int] = None,
    exclude_message_id: Optional[int] = None,
) -> None:
    req_id = int(reviewed["id"])
    decision_text = "APPROVED ✅" if status == "approved" else "REJECTED ❌"
    await _sync_admin_reg_request_cards(
        bot,
        store,
        reviewed,
        f"{decision_text} by {admin_display}",
        exclude_chat_id=exclude_chat_id,
        exclude_message_id=exclude_message_id,
    )
    student_id = int(reviewed["telegram_id"])
    if status == "approved":
        u_str = _format_short_username(reviewed["username"])
        notif = (
            f"✅ Your profile modification request (#{req_id}) was approved! "
            f"Your profile is now {reviewed['full_name']} ({u_str}, {reviewed['email']})."
        )
    else:
        reason_line = f"\nReason: {reason}" if reason else ""
        notif = f"❌ Your profile modification request (#{req_id}) was rejected.{reason_line}"
    try:
        await bot.send_message(
            chat_id=student_id, text=notif, reply_markup=_menu_button_markup()
        )
    except Exception:
        LOGGER.warning("Failed to notify student %s about profile request review", student_id)


async def regrequests_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List pending profile modification requests for admins."""
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    rows = _store(context).list_registration_requests(status="pending")
    if not rows:
        await update.effective_message.reply_text(
            "No pending profile modification requests.",
            reply_markup=_menu_button_markup(),
        )
        return
    lines = [
        f"#{r['id']} (ID {r['telegram_id']}): {r['old_full_name']} "
        f"({_format_short_username(r['old_username'])}, {r['old_email']}) "
        f"-> {r['full_name']} ({_format_short_username(r['username'])}, {r['email']})"
        for r in rows
    ]
    lines.append("\nUse /approvereq <id> or /rejectreq <id> [reason] to review.")
    await _reply_chunked(
        update.effective_message,
        "\n".join(lines),
        reply_markup=_menu_button_markup(),
    )


async def review_reg_request_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve or reject a profile modification request via /approvereq or /rejectreq."""
    if not update.effective_user or not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text(
            "Admin access required.", reply_markup=_menu_button_markup()
        )
        return
    cmd_token = (update.effective_message.text or "").split()[0].lower()
    is_approve = cmd_token.startswith("/approve")
    req_id = _parse_positive_int(context.args[0], strip_hash=True) if context.args else None
    if req_id is None or (is_approve and len(context.args) != 1):
        await update.effective_message.reply_text(
            "Usage: /approvereq <id> or /rejectreq <id> [reason]",
            reply_markup=_menu_button_markup(),
        )
        return
    status = "approved" if is_approve else "rejected"
    reason = (
        " ".join(context.args[1:]).strip()
        if (not is_approve and len(context.args) > 1)
        else None
    )
    store = _store(context)
    reviewed = store.review_registration_request(
        req_id, status, reviewer_id=update.effective_user.id, rejection_reason=reason
    )
    if reviewed is None:
        await update.effective_message.reply_text(
            "Profile modification request not found or already reviewed.",
            reply_markup=_menu_button_markup(),
        )
        return
    await update.effective_message.reply_text(
        f"Profile modification request #{req_id} {status}.",
        reply_markup=_menu_button_markup(),
    )
    await _finalize_reg_request_review(
        context.bot,
        store,
        reviewed,
        status,
        update.effective_user.first_name or "Admin",
        reason=reason,
    )


async def regreq_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline Approve/Reject buttons on admin profile modification request cards."""
    query = update.callback_query
    if not query:
        return
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "regreq" or parts[1] not in {"approve", "reject"}:
        await query.answer("Invalid action.", show_alert=True)
        return
    action, req_id_str = parts[1], parts[2]
    req_id = _parse_positive_int(req_id_str)
    if req_id is None:
        await query.answer("Invalid request ID.", show_alert=True)
        return

    status = "approved" if action == "approve" else "rejected"
    store = _store(context)
    reviewed = store.review_registration_request(
        req_id, status, reviewer_id=update.effective_user.id
    )
    if reviewed is None:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("This request was already reviewed.", show_alert=True)
        return

    await query.answer(f"Request #{req_id} {status}.")
    admin_display = update.effective_user.first_name or "Admin"
    decision_label = f"{'APPROVED ✅' if action == 'approve' else 'REJECTED ❌'} by {admin_display}"
    current_text = getattr(query.message, "text", "") or ""
    try:
        await query.edit_message_text(
            text=f"{current_text}\n\nDecision: {decision_label}", reply_markup=None
        )
    except Exception:
        pass

    exclude_chat = getattr(getattr(query.message, "chat", None), "id", None)
    exclude_msg = getattr(query.message, "message_id", None)
    await _finalize_reg_request_review(
        context.bot,
        store,
        reviewed,
        status,
        admin_display,
        exclude_chat_id=exclude_chat if isinstance(exclude_chat, int) else None,
        exclude_message_id=exclude_msg if isinstance(exclude_msg, int) else None,
    )
