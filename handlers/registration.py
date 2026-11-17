"""Registration, username, and admin-approved re-registration handlers."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from common import (
    REGISTER_EMAIL,
    REGISTER_NAME,
    REGISTER_USERNAME,
    _admin_ids,
    _clear_conversation_data,
    _is_admin,
    _is_valid_email,
    _parse_positive_int,
    _reply_chunked,
    _sanitize_single_line,
    _store,
)
from store import LeagueStore

LOGGER = logging.getLogger(__name__)


async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        pending_req = store.get_pending_registration_request(user_id)
        if pending_req is not None:
            await message.reply_text(
                f"⏳ You already have a pending re-registration request (#{pending_req['id']}) "
                "awaiting admin approval."
            )
            return ConversationHandler.END
        context.user_data["is_reregister"] = True
        await message.reply_text(
            f"You are currently registered as {existing['full_name']} "
            f"(@{existing['username']}, {existing['email']}).\n"
            "⚠️ Direct re-registration is disabled — profile changes require admin approval.\n\n"
            "What is the new full name you want to request? (or send /cancel)"
        )
    else:
        context.user_data["is_reregister"] = False
        await message.reply_text("What is your full name?")
    return REGISTER_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    name = _sanitize_single_line(update.effective_message.text or "")
    if not name:
        await update.effective_message.reply_text("Please enter a valid full name.")
        return REGISTER_NAME
    context.user_data["full_name"] = name
    await update.effective_message.reply_text(
        "What username/nickname would you like displayed on the leaderboard?"
    )
    return REGISTER_USERNAME


async def register_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    username = _sanitize_single_line((update.effective_message.text or "").strip().lstrip("@"))
    if not username:
        await update.effective_message.reply_text("Please enter a valid username, or /cancel.")
        return REGISTER_USERNAME
    context.user_data["username"] = username
    await update.effective_message.reply_text("What is your Polytechnique / IP Paris email address?")
    return REGISTER_EMAIL


def _format_reg_request_text(req: sqlite3.Row) -> str:
    return (
        f"🪪 Re-Registration Request #{req['id']}\n"
        f"Student ID: {req['telegram_id']}\n"
        f"Current: {req['old_full_name']} (@{req['old_username']}, {req['old_email']})\n"
        f"Requested: {req['full_name']} (@{req['username']}, {req['email']})"
    )


async def register_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    email = (update.effective_message.text or "").strip().lower()
    if not _is_valid_email(email):
        await update.effective_message.reply_text("Please send a valid email address.")
        return REGISTER_EMAIL
    full_name = context.user_data.pop("full_name", None)
    username = context.user_data.pop("username", None)
    is_reregister = bool(context.user_data.pop("is_reregister", False))
    if not full_name or not username:
        await update.effective_message.reply_text(
            "Registration session expired. Please run /register again."
        )
        return ConversationHandler.END

    store = _store(context)
    user_id = update.effective_user.id
    if is_reregister or store.is_registered(user_id):
        try:
            req_id = store.create_registration_request(user_id, full_name, username, email)
        except ValueError:
            await update.effective_message.reply_text(
                "You already have a pending re-registration request awaiting admin review."
            )
            return ConversationHandler.END

        await update.effective_message.reply_text(
            f"📨 Re-registration request (#{req_id}) submitted! An admin will review your request."
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
                    LOGGER.warning("Could not send re-registration request to admin %s", admin_id)
        return ConversationHandler.END

    store.register_student(user_id, full_name, email, username=username)
    await update.effective_message.reply_text("Registration saved. Good luck climbing!")
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
        notif = (
            f"✅ Your re-registration request (#{req_id}) was approved! "
            f"Your profile is now {reviewed['full_name']} (@{reviewed['username']}, {reviewed['email']})."
        )
    else:
        reason_line = f"\nReason: {reason}" if reason else ""
        notif = f"❌ Your re-registration request (#{req_id}) was rejected.{reason_line}"
    try:
        await bot.send_message(chat_id=student_id, text=notif)
    except Exception:
        LOGGER.warning("Failed to notify student %s about reg request review", student_id)


async def regrequests_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    rows = _store(context).list_registration_requests(status="pending")
    if not rows:
        await update.effective_message.reply_text("No pending re-registration requests.")
        return
    lines = [
        f"#{r['id']} (ID {r['telegram_id']}): {r['old_full_name']} (@{r['old_username']}, {r['old_email']}) "
        f"-> {r['full_name']} (@{r['username']}, {r['email']})"
        for r in rows
    ]
    lines.append("\nUse /approvereg <id> or /rejectreg <id> [reason] to review.")
    await _reply_chunked(update.effective_message, "\n".join(lines))


async def review_reg_request_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    is_approve = (update.effective_message.text or "").split()[0].startswith("/approvereg")
    req_id = _parse_positive_int(context.args[0], strip_hash=True) if context.args else None
    if req_id is None or (is_approve and len(context.args) != 1):
        await update.effective_message.reply_text(
            "Usage: /approvereg <id> or /rejectreg <id> [reason]"
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
            "Re-registration request not found or already reviewed."
        )
        return
    await update.effective_message.reply_text(f"Re-registration request #{req_id} {status}.")
    await _finalize_reg_request_review(
        context.bot,
        store,
        reviewed,
        status,
        update.effective_user.first_name or "Admin",
        reason=reason,
    )


async def regreq_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
