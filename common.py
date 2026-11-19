"""Shared constants, state identifiers, input parsers, role checks, and Telegram helpers."""

from __future__ import annotations

import os
import re
import sqlite3
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, filters

if TYPE_CHECKING:
    from store import LeagueStore

(
    REGISTER_NAME,
    REGISTER_USERNAME,
    REGISTER_EMAIL,
    REGISTER_CONFIRM,
    EDITPROFILE_MENU,
    EDITPROFILE_NAME,
    EDITPROFILE_USERNAME,
    EDITPROFILE_EMAIL,
    EDITPROFILE_CONFIRM,
    SUBMIT_ROUTE,
    SUBMIT_PROOF,
    ADDROUTE_CATEGORY,
    ADDROUTE_NAME,
    ADDROUTE_POINTS,
    ADDROUTE_ZONE,
    ADDROUTE_PHOTOS,
    EDITROUTE_SELECT,
    EDITROUTE_MENU,
    EDITROUTE_NAME,
    EDITROUTE_CATEGORY,
    EDITROUTE_POINTS,
    EDITROUTE_ZONE,
    EDITROUTE_PHOTOS,
    REVIEW_REJECT_REASON,
    GUI_PROMPT_INPUT,
) = range(25)

ROUTE_CATEGORIES: tuple[str, ...] = ("Arkose", "ENSTA gym", "Pan", "Outdoors")
_CATEGORY_ALIASES: dict[str, str] = {
    "arkose": "Arkose",
    "ensta gym": "ENSTA gym",
    "ensta": "ENSTA gym",
    "ensta_gym": "ENSTA gym",
    "enstagym": "ENSTA gym",
    "pan": "Pan",
    "outdoors": "Outdoors",
    "outdoor": "Outdoors",
}

MAX_TELEGRAM_CAPTION_LENGTH = 1024
MAX_MEDIA_GROUP_SIZE = 10
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
SAFE_MESSAGE_CHUNK_LENGTH = 4000
SQLITE_MAX_INT = 9_223_372_036_854_775_807

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*@polytechnique\.edu$",
    re.IGNORECASE,
)
MSG_COMMAND_FILTER = filters.UpdateType.MESSAGE

_CONVERSATION_DATA_KEYS = (
    "route_id",
    "full_name",
    "username",
    "email",
    "editprof_full_name",
    "editprof_username",
    "editprof_email",
    "addroute_category",
    "addroute_name",
    "addroute_points",
    "addroute_zone_points",
    "addroute_photos",
    "editroute_id",
    "editroute_photo_count",
    "review_submission_id",
    "review_card_chat_id",
    "review_card_message_id",
    "review_card_is_caption",
    "review_card_base_text",
    "gui_action",
)


def _parse_positive_int(raw: str, *, strip_hash: bool = False) -> Optional[int]:
    """Parses a positive ASCII integer fitting within SQLite's 64-bit signed range."""
    cleaned = raw.strip()
    if strip_hash:
        cleaned = cleaned.lstrip("#")
    if not cleaned or not cleaned.isascii() or not cleaned.isdigit():
        return None
    value = int(cleaned)
    if value <= 0 or value > SQLITE_MAX_INT:
        return None
    return value


def _is_valid_email(email: str) -> bool:
    """Validates that the email is a well-formed @polytechnique.edu address."""
    return bool(_EMAIL_RE.fullmatch(email.strip()))


def _sanitize_single_line(text: str) -> str:
    return " ".join(text.split())


def normalize_route_category(raw: str) -> Optional[str]:
    """Normalizes a route category string to one of ROUTE_CATEGORIES, or None."""
    return _CATEGORY_ALIASES.get(_sanitize_single_line(raw).lower())


def _compute_competition_ranks(rows: Sequence[sqlite3.Row]) -> list[tuple[int, sqlite3.Row]]:
    """Assigns standard competition ranks (1, 1, 3) for identical (points, tops)."""
    ranked: list[tuple[int, sqlite3.Row]] = []
    prev_score: Optional[tuple[int, int]] = None
    current_rank = 0
    for idx, row in enumerate(rows, 1):
        score = (int(row["points"]), int(row["tops"]))
        if score != prev_score:
            current_rank = idx
            prev_score = score
        ranked.append((current_rank, row))
    return ranked


def _menu_button_markup() -> InlineKeyboardMarkup:
    """Returns a single-button inline keyboard that opens the role-based GUI menu."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu")]]
    )


def _with_menu_button(
    markup: Optional[InlineKeyboardMarkup] = None,
) -> InlineKeyboardMarkup:
    """Appends the '📋 Open Menu' button row to an existing inline keyboard (or creates one)."""
    if markup is None:
        return _menu_button_markup()
    rows = [list(row) for row in markup.inline_keyboard]
    rows.append([InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu")])
    return InlineKeyboardMarkup(rows)


async def _reply_chunked(message: Any, text: str, **kwargs: Any) -> None:
    """Sends a potentially long message split cleanly under Telegram's 4096-char limit."""
    if len(text) <= MAX_TELEGRAM_MESSAGE_LENGTH:
        await message.reply_text(text, **kwargs)
        return

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for line in text.splitlines():
        if len(line) > SAFE_MESSAGE_CHUNK_LENGTH:
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_len = 0
            for start_idx in range(0, len(line), SAFE_MESSAGE_CHUNK_LENGTH):
                chunks.append(line[start_idx : start_idx + SAFE_MESSAGE_CHUNK_LENGTH])
            continue

        added_len = len(line) + (1 if current_lines else 0)
        if current_len + added_len > SAFE_MESSAGE_CHUNK_LENGTH:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = len(line)
        else:
            current_lines.append(line)
            current_len += added_len

    if current_lines:
        chunks.append("\n".join(current_lines))

    non_final_kwargs = {k: v for k, v in kwargs.items() if k != "reply_markup"}
    for idx, chunk in enumerate(chunks):
        chunk_kwargs = kwargs if idx == len(chunks) - 1 else non_final_kwargs
        await message.reply_text(chunk, **chunk_kwargs)


def _store(context: ContextTypes.DEFAULT_TYPE) -> LeagueStore:
    return context.application.bot_data["store"]


def _parse_id_env(var_name: str) -> set[int]:
    ids: set[int] = set()
    for value in os.getenv(var_name, "").split(","):
        parsed = _parse_positive_int(value)
        if parsed is not None:
            ids.add(parsed)
    return ids


def _admin_ids() -> set[int]:
    return _parse_id_env("ADMIN_IDS")


def _routesetter_ids() -> set[int]:
    return _parse_id_env("ROUTESETTER_IDS")


def _is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in _admin_ids())


def _is_routesetter(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in _routesetter_ids())


def _can_manage_routes(update: Update) -> bool:
    """Returns True if the user is either a routesetter or an admin."""
    return _is_admin(update) or _is_routesetter(update)


def _clear_conversation_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in _CONVERSATION_DATA_KEYS:
        context.user_data.pop(key, None)


def _extract_photo_info(message: Any) -> Optional[tuple[str, str]]:
    """Extracts (file_id, media_type) if message contains a photo/image, else None."""
    photos = getattr(message, "photo", None)
    if photos:
        return photos[-1].file_id, "photo"
    doc = getattr(message, "document", None)
    if doc and getattr(doc, "mime_type", None) and str(doc.mime_type).startswith("image/"):
        return doc.file_id, "document"
    return None


def _extract_video_info(message: Any) -> Optional[tuple[str, str]]:
    """Extracts (file_id, media_type) if message contains a video, else None."""
    if getattr(message, "video", None):
        return message.video.file_id, "video"
    if getattr(message, "video_note", None):
        return message.video_note.file_id, "video_note"
    if getattr(message, "animation", None):
        return message.animation.file_id, "animation"
    doc = getattr(message, "document", None)
    if doc and getattr(doc, "mime_type", None) and str(doc.mime_type).startswith("video/"):
        return doc.file_id, "document"
    return None


def _extract_command_and_args(
    text: str, bot_username: Optional[str]
) -> Optional[tuple[str, str]]:
    """Extracts (base_command, rest_of_text) if text starts with /cmd[@bot]."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(maxsplit=1)
    cmd_token = parts[0][1:]
    rest = parts[1] if len(parts) > 1 else ""
    if "@" in cmd_token:
        cmd_name, target_bot = cmd_token.split("@", 1)
        if bot_username and target_bot.lower() != bot_username.lower():
            return None
    else:
        cmd_name = cmd_token
    return cmd_name.lower(), rest


def _non_text_prompt(
    prompt: str, next_state: int
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[int]]:
    """Creates a ConversationHandler state callback that prompts for valid input and stays in state."""

    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if update.effective_message:
            await update.effective_message.reply_text(prompt)
        return next_state

    return _handler
