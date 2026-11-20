"""Route browsing, creation, and unified interactive route editing handlers."""

from __future__ import annotations

import logging
from typing import Any, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    Update,
)
from telegram.ext import ContextTypes, ConversationHandler

from common import (
    ADDROUTE_CATEGORY,
    ADDROUTE_NAME,
    ADDROUTE_PHOTOS,
    ADDROUTE_POINTS,
    ADDROUTE_ZONE,
    EDITROUTE_CATEGORY,
    EDITROUTE_MENU,
    EDITROUTE_NAME,
    EDITROUTE_PHOTOS,
    EDITROUTE_POINTS,
    EDITROUTE_SELECT,
    EDITROUTE_ZONE,
    MAX_MEDIA_GROUP_SIZE,
    ROUTE_CATEGORIES,
    _can_manage_routes,
    _clear_conversation_data,
    _extract_command_and_args,
    _extract_photo_info,
    _menu_button_markup,
    _parse_positive_int,
    _reply_chunked,
    _sanitize_single_line,
    _send_role_menus,
    _store,
    normalize_route_category,
)
from store import LeagueStore, Route

LOGGER = logging.getLogger(__name__)

_ADDROUTE_USAGE = (
    "Usage:\n"
    "• Interactive (with category & pictures): /addroute\n"
    "• Quick publish: /addroute <name> <points> [category]\n"
    "• Pipe format: /addroute <category> | <name> | <points>\n"
    f"Categories: {', '.join(ROUTE_CATEGORIES)}"
)


async def routes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List routes with optional routesetter/admin 'all' flag and category filtering."""
    if not update.effective_message:
        return
    args = list(context.args or [])
    show_all = False
    if args and args[0].lower() == "all":
        if _can_manage_routes(update):
            show_all = True
        args = args[1:]
    elif args and len(args) > 1 and args[-1].lower() == "all":
        if _can_manage_routes(update):
            show_all = True
        args = args[:-1]

    category_filter: Optional[str] = None
    if args:
        raw_cat = " ".join(args)
        category_filter = normalize_route_category(raw_cat)
        if category_filter is None:
            await update.effective_message.reply_text(
                f"Unknown category '{raw_cat}'. Available categories: {', '.join(ROUTE_CATEGORIES)}.",
                reply_markup=_menu_button_markup(),
            )
            return

    store = _store(context)
    available = store.list_routes(active_only=not show_all, category=category_filter)
    if not available:
        if category_filter:
            await update.effective_message.reply_text(
                f"No routes found in category '{category_filter}'.",
                reply_markup=_menu_button_markup(),
            )
        else:
            await update.effective_message.reply_text(
                "No routes have been published yet.",
                reply_markup=_menu_button_markup(),
            )
        return

    user_id = update.effective_user.id if update.effective_user else None
    statuses = store.get_student_route_statuses(user_id) if user_id else {}

    grouped: dict[Optional[str], list[Route]] = {}
    for route in available:
        grouped.setdefault(route.category, []).append(route)

    ordered_keys: list[Optional[str]] = [cat for cat in ROUTE_CATEGORIES if cat in grouped]
    if None in grouped:
        ordered_keys.append(None)

    lines: list[str] = []
    completed_count = 0
    active_count = 0
    earned_active_points = 0
    show_headers = category_filter is not None or any(r.category is not None for r in available)

    for key in ordered_keys:
        if show_headers:
            header = f"📍 {key}" if key is not None else "📍 Other"
            if lines:
                lines.append("")
            lines.append(header)

        for route in grouped[key]:
            if route.active:
                active_count += 1
            status = statuses.get(route.id)
            if status == "approved":
                badge = "✅ "
                status_suffix = " (Completed)"
                if route.active:
                    completed_count += 1
                    earned_active_points += route.points
            elif status == "zone":
                badge = "🔶 "
                z_pts = (
                    store.get_student_route_zone_points(user_id, route.id)
                    if user_id
                    else (route.zone_points or 0)
                )
                status_suffix = f" (Zone: {z_pts} pts)"
                if route.active:
                    earned_active_points += z_pts
            elif status == "pending":
                badge = "⏳ "
                z_pts = store.get_student_route_zone_points(user_id, route.id) if user_id else 0
                if z_pts > 0:
                    status_suffix = f" (Pending review; Zone: {z_pts} pts)"
                    if route.active:
                        earned_active_points += z_pts
                else:
                    status_suffix = " (Pending review)"
            else:
                badge = ""
                status_suffix = ""

            zone_info = f" (zone: {route.zone_points} pts)" if route.zone_points is not None else ""
            pic_suffix = f" [📸 {route.photo_count}]" if route.photo_count > 0 else ""
            inactive_suffix = "" if route.active else " [Inactive]"
            lines.append(
                f"{badge}#{route.id}: {route.name} - {route.points} points{zone_info}"
                f"{pic_suffix}{status_suffix}{inactive_suffix}"
            )

    if user_id and store.is_registered(user_id):
        profile_info = store.get_student_profile(user_id)
        total_points = int(profile_info["points"]) if profile_info else earned_active_points
        if category_filter is not None:
            lines.append(
                f"\n{category_filter} progress: {completed_count}/{active_count} "
                f"active routes completed ({earned_active_points} pts)"
            )
        elif total_points != earned_active_points:
            lines.append(
                f"\nProgress: {completed_count}/{active_count} active routes completed "
                f"({earned_active_points} pts active, {total_points} pts total)"
            )
        else:
            lines.append(
                f"\nProgress: {completed_count}/{active_count} active routes completed ({total_points} pts)"
            )

    lines.append("Tip: Use /route <id> for full details & pictures, or /routes <category> to filter.")
    await _reply_chunked(
        update.effective_message,
        "\n".join(lines),
        reply_markup=_menu_button_markup(),
    )


def _format_route_details_text(
    details: dict[str, Any], store: Optional[LeagueStore] = None, user_id: Optional[int] = None
) -> str:
    route: Route = details["route"]
    photos: list[dict[str, Any]] = details["photos"]
    completions_count: int = details["completions_count"]
    zones_count: int = details.get("zones_count", 0)
    pending_count: int = details["pending_count"]
    user_status: Optional[str] = details["user_status"]

    climber_word = "climber" if completions_count == 1 else "climbers"
    lines = [
        f"🧗 Route #{route.id}: {route.name}",
        f"📍 Location: {route.category or 'Uncategorized'}",
        f"🏆 Points: {route.points} pts",
    ]
    if route.zone_points is not None:
        lines.append(f"🔶 Zone Points: {route.zone_points} pts")
    lines.extend(
        [
            f"📌 Status: {'Active' if route.active else 'Inactive (archived)'}",
            f"🏅 Completions: {completions_count} {climber_word}"
            + (f" ({zones_count} zone)" if zones_count > 0 else ""),
            f"📸 Pictures: {len(photos)}",
        ]
    )
    if pending_count > 0:
        lines.append(f"⏳ Pending submissions: {pending_count}")
    if user_status == "approved":
        lines.append("✅ Your status: Completed (Top)")
    elif user_status == "zone":
        z_pts = (
            store.get_student_route_zone_points(user_id, route.id)
            if (store and user_id)
            else (route.zone_points or 0)
        )
        lines.append(
            f"🔶 Your status: Zone reached ({z_pts} pts) — use /submit {route.id} if you top it!"
        )
    elif user_status == "pending":
        lines.append("⏳ Your status: Pending review")
    elif route.active:
        is_reg = bool(store and user_id is not None and store.is_registered(user_id))
        if is_reg:
            lines.append(f"📝 Your status: Not submitted yet (use /submit {route.id})")
        else:
            lines.append("📝 Register with /register to submit climbs for this route.")
    return "\n".join(lines)


async def _send_route_details(
    message: Any, store: LeagueStore, route_id: int, user_id: Optional[int]
) -> None:
    details = store.get_route_details(route_id, telegram_id=user_id)
    if details is None:
        await message.reply_text(
            f"Route #{route_id} not found.",
            reply_markup=_menu_button_markup(),
        )
        return

    info_text = _format_route_details_text(details, store=store, user_id=user_id)
    photos = details["photos"]
    if not photos:
        await _reply_chunked(
            message, info_text, reply_markup=_menu_button_markup()
        )
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
        await _reply_chunked(
            message, info_text, reply_markup=_menu_button_markup()
        )
    except Exception:
        LOGGER.warning("Failed to send pictures for route %s", route_id)
        await _reply_chunked(
            message,
            f"{info_text}\n\n⚠️ Could not load route picture(s).",
            reply_markup=_menu_button_markup(),
        )


async def route_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display an inline route picker or full route details + pictures for /route <id>."""
    if not update.effective_message:
        return
    store = _store(context)
    if not context.args:
        active_routes = store.list_routes(active_only=True)
        if not active_routes:
            await update.effective_message.reply_text(
                "No active routes have been published yet.",
                reply_markup=_menu_button_markup(),
            )
            return
        keyboard = [
            [
                InlineKeyboardButton(
                    f"#{r.id} {r.name} ({r.points} pts"
                    + (f" · {r.category}" if r.category else "")
                    + ")",
                    callback_data=f"route:view:{r.id}",
                )
            ]
            for r in active_routes[:25]
        ]
        keyboard.append([InlineKeyboardButton("📋 Open Menu", callback_data="gui:menu")])
        await update.effective_message.reply_text(
            "Select a route to view its full info & pictures (or use /route <route_id>):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    route_id = _parse_positive_int(context.args[0], strip_hash=True)
    if route_id is None or len(context.args) > 1:
        await update.effective_message.reply_text(
            "Usage: /route <route_id>",
            reply_markup=_menu_button_markup(),
        )
        return

    user_id = update.effective_user.id if update.effective_user else None
    await _send_route_details(update.effective_message, store, route_id, user_id)


async def route_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline route:view:<route_id> button clicks."""
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "route" or parts[1] != "view":
        await query.answer("Invalid route selection.", show_alert=True)
        return
    route_id = _parse_positive_int(parts[2])
    if route_id is None:
        await query.answer("Invalid route ID.", show_alert=True)
        return
    await query.answer()
    if query.message:
        user_id = update.effective_user.id if update.effective_user else None
        await _send_route_details(query.message, _store(context), route_id, user_id)


def _parse_addroute_args(raw_args: str) -> Optional[tuple[str, int, Optional[str]]]:
    """Parses direct /addroute arguments into (name, points, category) or None if invalid."""
    cleaned = raw_args.strip()
    if not cleaned:
        return None

    if "|" in cleaned:
        parts = [_sanitize_single_line(part) for part in cleaned.split("|")]
        if len(parts) == 2:
            name, points_str = parts
            points = _parse_positive_int(points_str)
            if name and points is not None:
                return name, points, None
            return None
        if len(parts) == 3:
            first, middle, last = parts
            cat_first = normalize_route_category(first)
            pts_last = _parse_positive_int(last)
            if cat_first is not None and middle and pts_last is not None:
                return middle, pts_last, cat_first
            cat_last = normalize_route_category(last)
            pts_mid = _parse_positive_int(middle)
            if first and pts_mid is not None and cat_last is not None:
                return first, pts_mid, cat_last
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

    for cat_len in (2, 1):
        if len(tokens) >= cat_len + 2:
            cat = normalize_route_category(" ".join(tokens[-cat_len:]))
            pts = _parse_positive_int(tokens[-cat_len - 1])
            name = _sanitize_single_line(" ".join(tokens[: -cat_len - 1]))
            if cat is not None and pts is not None and name:
                return name, pts, cat

    return None


def _addroute_category_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(ROUTE_CATEGORIES[0], callback_data="addroute:cat:0"),
                InlineKeyboardButton(ROUTE_CATEGORIES[1], callback_data="addroute:cat:1"),
            ],
            [
                InlineKeyboardButton(ROUTE_CATEGORIES[2], callback_data="addroute:cat:2"),
                InlineKeyboardButton(ROUTE_CATEGORIES[3], callback_data="addroute:cat:3"),
            ],
            [InlineKeyboardButton("Cancel ❌", callback_data="addroute:cancel")],
        ]
    )


def _addroute_photos_keyboard(
    photo_count: int, zone_points: Optional[int] = None, points: int = 0
) -> InlineKeyboardMarkup:
    done_label = "Publish Route ✅" if photo_count > 0 else "Skip Pictures & Publish ✅"
    rows: list[list[InlineKeyboardButton]] = []
    if points > 1:
        zone_label = (
            f"🔶 Zone: {zone_points} pts (Change)"
            if zone_points is not None
            else "🔶 Set Zone Points"
        )
        rows.append([InlineKeyboardButton(zone_label, callback_data="addroute:setzone")])
    rows.append(
        [
            InlineKeyboardButton(done_label, callback_data="addroute:done"),
            InlineKeyboardButton("Cancel ❌", callback_data="addroute:cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _format_published_message(
    route_id: int,
    category: Optional[str],
    points: int,
    photo_count: int,
    zone_points: Optional[int] = None,
) -> str:
    if category is None and photo_count == 0 and zone_points is None:
        return f"Route #{route_id} published."
    details: list[str] = []
    if category is not None:
        details.append(category)
    details.append(f"{points} pts")
    if zone_points is not None:
        details.append(f"zone {zone_points} pts")
    if photo_count > 0:
        pic_word = "picture" if photo_count == 1 else "pictures"
        details.append(f"{photo_count} {pic_word}")
    return f"Route #{route_id} published ({', '.join(details)})."


async def _start_interactive_addroute(
    message: Any, context: ContextTypes.DEFAULT_TYPE, initial_photo: Optional[tuple[str, str]]
) -> int:
    _clear_conversation_data(context)
    photos = [initial_photo] if initial_photo is not None else []
    context.user_data["addroute_photos"] = photos
    prefix = "📸 Initial picture saved.\n\n" if photos else ""
    await message.reply_text(
        f"{prefix}📍 Where is this route located?\n"
        "Choose a category below (or type one: Arkose, ENSTA gym, Pan, Outdoors), or /cancel:",
        reply_markup=_addroute_category_keyboard(),
    )
    return ADDROUTE_CATEGORY


async def add_route_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start /addroute interactively or publish immediately when arguments are provided."""
    if not update.effective_message:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        await update.effective_message.reply_text(
            "Routesetter or admin access required.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    raw_args = " ".join(context.args) if context.args else ""
    if not raw_args:
        return await _start_interactive_addroute(update.effective_message, context, None)

    parsed = _parse_addroute_args(raw_args)
    if parsed is None:
        await update.effective_message.reply_text(
            _ADDROUTE_USAGE, reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END

    _clear_conversation_data(context)
    name, points, category = parsed
    route_id = _store(context).add_route(name, points, category=category)
    await update.effective_message.reply_text(
        _format_published_message(route_id, category, points, 0),
        reply_markup=_menu_button_markup(),
    )
    return ConversationHandler.END


async def add_route_caption_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /addroute sent directly as a photo or image document caption."""
    if not update.effective_message:
        return ConversationHandler.END
    message = update.effective_message
    bot_username = getattr(context.bot, "username", None)
    parsed_cmd = _extract_command_and_args(message.caption or "", bot_username)
    if parsed_cmd is None or parsed_cmd[0] != "addroute":
        return ConversationHandler.END

    if not _can_manage_routes(update):
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
        return await _start_interactive_addroute(message, context, (media_type, file_id))

    parsed = _parse_addroute_args(raw_args)
    if parsed is None:
        await message.reply_text(
            _ADDROUTE_USAGE, reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END

    _clear_conversation_data(context)
    name, points, category = parsed
    route_id = _store(context).add_route(
        name, points, category=category, photos=[(media_type, file_id)]
    )
    await message.reply_text(
        _format_published_message(route_id, category, points, 1),
        reply_markup=_menu_button_markup(),
    )
    return ConversationHandler.END


async def _cancel_addroute_callback(
    query: Any, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    _clear_conversation_data(context)
    await query.answer("Cancelled.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if query.message:
        await _send_role_menus(query.message, update, context, prefix="Cancelled.")
    return ConversationHandler.END


async def _select_addroute_category(
    message: Any, context: ContextTypes.DEFAULT_TYPE, category: str
) -> int:
    context.user_data["addroute_category"] = category
    if message:
        await message.reply_text(
            f"📍 Category: {category}\nWhat is the route name? (or /cancel)"
        )
    return ADDROUTE_NAME


async def _enter_addroute_photos_step(
    message: Any, context: ContextTypes.DEFAULT_TYPE, prompt: str
) -> int:
    photos = context.user_data.get("addroute_photos") or []
    z_val = context.user_data.get("addroute_zone_points")
    points = int(context.user_data.get("addroute_points") or 0)
    if message:
        await message.reply_text(
            prompt,
            reply_markup=_addroute_photos_keyboard(len(photos), z_val, points),
        )
    return ADDROUTE_PHOTOS


async def addroute_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle category selection or cancel buttons in /addroute."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    if data == "addroute:cancel":
        return await _cancel_addroute_callback(query, update, context)

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "addroute" or parts[1] != "cat":
        await query.answer("Invalid category selection.", show_alert=True)
        return ADDROUTE_CATEGORY

    try:
        category = ROUTE_CATEGORIES[int(parts[2])]
    except (ValueError, IndexError):
        await query.answer("Invalid category selection.", show_alert=True)
        return ADDROUTE_CATEGORY

    await query.answer(f"Selected {category}")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    return await _select_addroute_category(query.message, context, category)


async def addroute_category_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle typed category name or index during /addroute."""
    if not update.effective_message:
        return ConversationHandler.END
    raw = (update.effective_message.text or "").strip()
    category: Optional[str] = None
    if raw.isascii() and raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(ROUTE_CATEGORIES):
            category = ROUTE_CATEGORIES[idx]
    else:
        category = normalize_route_category(raw)

    if category is None:
        await update.effective_message.reply_text(
            f"Please choose one of: {', '.join(ROUTE_CATEGORIES)} (or tap a button above), or /cancel."
        )
        return ADDROUTE_CATEGORY

    return await _select_addroute_category(update.effective_message, context, category)


async def addroute_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store route name and ask for top points."""
    if not update.effective_message:
        return ConversationHandler.END
    name = _sanitize_single_line(update.effective_message.text or "")
    if not name:
        await update.effective_message.reply_text("Please enter a valid route name, or /cancel.")
        return ADDROUTE_NAME
    context.user_data["addroute_name"] = name
    await update.effective_message.reply_text(
        f"How many points is '{name}' worth? (positive integer, or /cancel)"
    )
    return ADDROUTE_POINTS


async def addroute_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store top points and transition to optional zone/picture upload step."""
    if not update.effective_message:
        return ConversationHandler.END
    points = _parse_positive_int(update.effective_message.text or "")
    if points is None:
        await update.effective_message.reply_text(
            "Please enter a valid positive whole number for points (e.g. 20), or /cancel."
        )
        return ADDROUTE_POINTS

    context.user_data["addroute_points"] = points
    if points <= 1:
        context.user_data["addroute_zone_points"] = None
    existing_photos = context.user_data.setdefault("addroute_photos", [])
    photo_count = len(existing_photos)
    if points <= 1:
        if photo_count > 0:
            prompt = (
                f"📸 {photo_count} picture(s) already attached (1-point routes cannot have partial zone points).\n"
                "Send more pictures if you'd like, or tap Publish Route ✅ (or send /done) to publish."
            )
        else:
            prompt = (
                "📸 Send one or more pictures of the route (1-point routes cannot have partial zone points).\n"
                "When finished, tap Publish Route ✅ (or send /done, or /skip for no pictures)."
            )
    elif photo_count > 0:
        prompt = (
            f"📸 {photo_count} picture(s) already attached.\n"
            "Send more pictures if you'd like, tap 🔶 Set Zone Points for partial points, "
            "or tap Publish Route ✅ (or send /done) to publish."
        )
    else:
        prompt = (
            "📸 Send one or more pictures of the route (or tap 🔶 Set Zone Points to configure partial points).\n"
            "When finished, tap Publish Route ✅ (or send /done, or /skip for no pictures)."
        )
    return await _enter_addroute_photos_step(update.effective_message, context, prompt)


async def addroute_zone_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and save optional partial zone points during /addroute."""
    if not update.effective_message:
        return ConversationHandler.END
    points = int(context.user_data.get("addroute_points") or 0)
    if points <= 1:
        context.user_data["addroute_zone_points"] = None
        return await _enter_addroute_photos_step(
            update.effective_message,
            context,
            "Routes worth 1 point cannot have partial zone points.\n"
            "Send route picture(s), or tap Publish Route ✅ (or send /done):",
        )

    raw = (update.effective_message.text or "").strip().lower()
    if raw in {"none", "skip", "0", "off"}:
        context.user_data["addroute_zone_points"] = None
        zone_msg = "Zone points cleared."
    else:
        zone_pts = _parse_positive_int(raw)
        if zone_pts is None or zone_pts >= points:
            await update.effective_message.reply_text(
                f"Please enter a positive integer strictly less than {points} (1 to {points - 1}, or 'none' to skip), or /cancel."
            )
            return ADDROUTE_ZONE
        context.user_data["addroute_zone_points"] = zone_pts
        zone_msg = f"🔶 Zone points set to {zone_pts} pts."

    return await _enter_addroute_photos_step(
        update.effective_message,
        context,
        f"{zone_msg}\nSend route picture(s), or tap Publish Route ✅ (or send /done):",
    )


async def addroute_zone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skip zone configuration via inline button and return to /addroute photos step."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    return await _enter_addroute_photos_step(
        query.message,
        context,
        "📸 Send route picture(s), or tap Publish Route ✅ (or send /done):",
    )


async def addroute_zone_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skip zone configuration via /skip during /addroute."""
    if not update.effective_message:
        return ConversationHandler.END
    return await _enter_addroute_photos_step(
        update.effective_message,
        context,
        "Skipped zone points.\n📸 Send route picture(s), or tap Publish Route ✅ (or send /done):",
    )


async def addroute_zone_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt again for numeric zone points with dynamic upper bound."""
    if update.effective_message:
        points = int(context.user_data.get("addroute_points") or 0)
        if points <= 1:
            return await _enter_addroute_photos_step(
                update.effective_message,
                context,
                "Routes worth 1 point cannot have partial zone points.\n"
                "Send route picture(s), or tap Publish Route ✅ (or send /done):",
            )
        await update.effective_message.reply_text(
            f"Please enter a whole number between 1 and {points - 1} (or 'none' / /skip), or /cancel."
        )
    return ADDROUTE_ZONE


async def addroute_receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Append an uploaded route picture during /addroute."""
    if not update.effective_message:
        return ConversationHandler.END
    photo_info = _extract_photo_info(update.effective_message)
    if photo_info is None:
        await update.effective_message.reply_text(
            "Please send a picture (photo), or send /done (or /skip) to publish, or /cancel."
        )
        return ADDROUTE_PHOTOS
    file_id, media_type = photo_info
    photos: list[tuple[str, str]] = context.user_data.setdefault("addroute_photos", [])
    photos.append((media_type, file_id))
    return await _enter_addroute_photos_step(
        update.effective_message,
        context,
        f"📸 Picture #{len(photos)} added. Send another picture, or send /done (or tap Publish Route ✅) to publish.",
    )


async def _finalize_addroute(message: Any, context: ContextTypes.DEFAULT_TYPE) -> int:
    category = context.user_data.get("addroute_category")
    name = context.user_data.get("addroute_name")
    points = context.user_data.get("addroute_points")
    zone_points = context.user_data.get("addroute_zone_points")
    photos: list[tuple[str, str]] = list(context.user_data.get("addroute_photos") or [])
    _clear_conversation_data(context)

    if not name or points is None:
        await message.reply_text(
            "Route creation session expired. Please run /addroute again.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    pts_int = int(points)
    z_int = int(zone_points) if (zone_points is not None and pts_int > 1) else None
    route_id = _store(context).add_route(
        name,
        pts_int,
        category=category,
        photos=photos,
        zone_points=z_int,
    )
    await message.reply_text(
        _format_published_message(
            route_id,
            category,
            pts_int,
            len(photos),
            z_int,
        ),
        reply_markup=_menu_button_markup(),
    )
    return ConversationHandler.END


async def addroute_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Publish route when /done or /skip is sent."""
    if not update.effective_message:
        return ConversationHandler.END
    return await _finalize_addroute(update.effective_message, context)


async def addroute_photos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Set Zone Points, Publish Route, or Cancel buttons in /addroute."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    if data == "addroute:cancel":
        return await _cancel_addroute_callback(query, update, context)

    if data == "addroute:setzone":
        points = int(context.user_data.get("addroute_points") or 0)
        if points <= 1:
            await query.answer(
                "Routes worth 1 point cannot have partial zone points.",
                show_alert=True,
            )
            return ADDROUTE_PHOTOS
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if query.message:
            await query.message.reply_text(
                f"🔶 How many partial (Zone) points should this route award? "
                f"(Enter 1 to {points - 1}, or 'none' to skip):",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Skip Zone ➡️", callback_data="addroute:skipzone")]]
                ),
            )
        return ADDROUTE_ZONE

    if data == "addroute:done":
        await query.answer("Publishing route...")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if query.message:
            return await _finalize_addroute(query.message, context)
        _clear_conversation_data(context)
        return ConversationHandler.END

    await query.answer("Invalid action.", show_alert=True)
    return ADDROUTE_PHOTOS


# ---------------------------------------------------------------------------
# Unified Route Editing (/editroute) for Routesetters and Admins
# ---------------------------------------------------------------------------


def _format_editroute_menu_text(route: Route) -> str:
    if route.points <= 1:
        zone_str = "Not available (1-point route)"
    elif route.zone_points is not None:
        zone_str = f"{route.zone_points} pts"
    else:
        zone_str = "None"

    return (
        f"✏️ Editing Route #{route.id}\n"
        f"• Name: {route.name}\n"
        f"• Category: {route.category or 'Uncategorized'}\n"
        f"• Points (Top): {route.points} pts\n"
        f"• Zone Points: {zone_str}\n"
        f"• Pictures: {route.photo_count}\n"
        f"• Status: {'Active' if route.active else 'Inactive (archived)'}\n\n"
        "Select a field below to add or modify, or tap Done Editing ✅:"
    )


def _editroute_menu_keyboard(route: Route) -> InlineKeyboardMarkup:
    row2 = [InlineKeyboardButton("🏆 Points", callback_data="editroute:field:points")]
    if route.points > 1:
        row2.append(
            InlineKeyboardButton("🔶 Zone Points", callback_data="editroute:field:zone")
        )

    row3 = [InlineKeyboardButton("📸 Add Pictures", callback_data="editroute:field:photos")]
    if route.photo_count > 0:
        row3.append(
            InlineKeyboardButton("🗑️ Clear Pictures", callback_data="editroute:clearphotos")
        )

    toggle_label = "🔄 Deactivate Route" if route.active else "🔄 Activate Route"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Name", callback_data="editroute:field:name"),
                InlineKeyboardButton("📍 Category", callback_data="editroute:field:category"),
            ],
            row2,
            row3,
            [InlineKeyboardButton(toggle_label, callback_data="editroute:toggle")],
            [InlineKeyboardButton("✅ Done Editing", callback_data="editroute:done")],
        ]
    )


async def _send_editroute_menu(
    message: Any, context: ContextTypes.DEFAULT_TYPE, route_id: int
) -> int:
    route = _store(context).get_route(route_id)
    if route is None:
        _clear_conversation_data(context)
        await message.reply_text(
            f"Route #{route_id} not found.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    context.user_data["editroute_id"] = route.id
    await message.reply_text(
        _format_editroute_menu_text(route),
        reply_markup=_editroute_menu_keyboard(route),
    )
    return EDITROUTE_MENU


async def start_editroute_picker(
    message: Any, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Display an inline route picker for /editroute when no route_id is given."""
    _clear_conversation_data(context)
    all_routes = _store(context).list_routes(active_only=False)
    if not all_routes:
        await message.reply_text(
            "No routes have been published yet.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    kb = [
        [
            InlineKeyboardButton(
                f"#{r.id} {r.name} ({r.points} pts · {'Active' if r.active else 'Inactive'})",
                callback_data=f"editroute:pick:{r.id}",
            )
        ]
        for r in all_routes[:25]
    ]
    kb.append([InlineKeyboardButton("Cancel ❌", callback_data="editroute:cancel")])
    await message.reply_text(
        "✏️ Select a route to edit (or reply with the route ID, or /cancel):",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return EDITROUTE_SELECT


async def edit_route_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start /editroute [route_id] for routesetters and admins."""
    if not update.effective_message:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        await update.effective_message.reply_text(
            "Routesetter or admin access required.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    if not context.args:
        return await start_editroute_picker(update.effective_message, context)

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage: /editroute [route_id]",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    route_id = _parse_positive_int(context.args[0], strip_hash=True)
    if route_id is None:
        await update.effective_message.reply_text(
            "Usage: /editroute [route_id]",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    _clear_conversation_data(context)
    return await _send_editroute_menu(update.effective_message, context, route_id)


async def editroute_select_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle route selection button in EDITROUTE_SELECT."""
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if data == "editroute:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        await _send_role_menus(query.message, update, context, prefix="Cancelled.")
        return ConversationHandler.END

    parts = data.split(":")
    route_id = _parse_positive_int(parts[2]) if len(parts) == 3 else None
    if route_id is None:
        await query.answer("Invalid route ID.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    return await _send_editroute_menu(query.message, context, route_id)


async def editroute_select_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle typed route number in EDITROUTE_SELECT."""
    if not update.effective_message:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            "Routesetter or admin access required.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    route_id = _parse_positive_int(update.effective_message.text or "", strip_hash=True)
    if route_id is None:
        await update.effective_message.reply_text(
            "Please reply with a valid numeric route ID (e.g. 1), or /cancel."
        )
        return EDITROUTE_SELECT
    return await _send_editroute_menu(update.effective_message, context, route_id)


async def editroute_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle field and action buttons on the Edit Route Dashboard."""
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END

    route_id = int(context.user_data.get("editroute_id") or 0)
    store = _store(context)
    route = store.get_route(route_id)
    if route is None:
        _clear_conversation_data(context)
        await query.answer("Route not found.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    if data == "editroute:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _send_role_menus(query.message, update, context, prefix="Cancelled.")
        return ConversationHandler.END

    if data == "editroute:done":
        _clear_conversation_data(context)
        await query.answer("Finished editing.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"✅ Finished editing Route #{route.id} ('{route.name}').",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END

    if data == "editroute:toggle":
        updated = store.update_route(route.id, active=not route.active)
        state_str = "reactivated" if (updated and updated.active) else "deactivated"
        await query.answer(f"Route #{route.id} {state_str}.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"Route #{route.id} ('{route.name}') has been {state_str}."
        )
        return await _send_editroute_menu(query.message, context, route.id)

    if data == "editroute:clearphotos":
        removed = store.clear_route_photos(route.id)
        await query.answer("Pictures cleared.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"🗑️ Removed {removed} picture(s) from Route #{route.id} ('{route.name}')."
        )
        return await _send_editroute_menu(query.message, context, route.id)

    if data == "editroute:field:name":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"✏️ Send the new name for Route #{route.id} (current: '{route.name}'), or /cancel:"
        )
        return EDITROUTE_NAME

    if data == "editroute:field:category":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        kb = [
            [
                InlineKeyboardButton(ROUTE_CATEGORIES[0], callback_data="editroute:cat:0"),
                InlineKeyboardButton(ROUTE_CATEGORIES[1], callback_data="editroute:cat:1"),
            ],
            [
                InlineKeyboardButton(ROUTE_CATEGORIES[2], callback_data="editroute:cat:2"),
                InlineKeyboardButton(ROUTE_CATEGORIES[3], callback_data="editroute:cat:3"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="editroute:back")],
        ]
        await query.message.reply_text(
            f"📍 Choose or type the category for Route #{route.id} ('{route.name}'):",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return EDITROUTE_CATEGORY

    if data == "editroute:field:points":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"🏆 Send the new top points for Route #{route.id} (positive integer, current: {route.points}), or /cancel:"
        )
        return EDITROUTE_POINTS

    if data == "editroute:field:zone":
        if route.points <= 1:
            await query.answer(
                "Routes worth 1 point cannot have partial zone points.",
                show_alert=True,
            )
            return EDITROUTE_MENU
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        kb_rows: list[list[InlineKeyboardButton]] = []
        if route.zone_points is not None:
            kb_rows.append(
                [InlineKeyboardButton("🗑️ Clear Zone Points", callback_data="editroute:clearzone")]
            )
        kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="editroute:back")])
        await query.message.reply_text(
            f"🔶 Enter partial (Zone) points for Route #{route.id} ('{route.name}') "
            f"(1 to {route.points - 1}, or 'none' to clear), or /cancel:",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return EDITROUTE_ZONE

    if data == "editroute:field:photos":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        context.user_data["editroute_photo_count"] = 0
        await query.message.reply_text(
            f"📸 Send picture(s) to attach to Route #{route.id} ('{route.name}').\n"
            "Send /done (or tap Done Adding Pictures ✅) when finished, or /cancel.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Done Adding Pictures ✅", callback_data="editroute:photos_done")]]
            ),
        )
        return EDITROUTE_PHOTOS

    await query.answer("Invalid action.", show_alert=True)
    return EDITROUTE_MENU


async def editroute_done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finish /editroute via /done when in EDITROUTE_MENU."""
    route_id = context.user_data.get("editroute_id")
    route = _store(context).get_route(int(route_id)) if route_id else None
    _clear_conversation_data(context)
    if update.effective_message:
        label = f"Route #{route.id} ('{route.name}')" if route else "route"
        await update.effective_message.reply_text(
            f"✅ Finished editing {label}.",
            reply_markup=_menu_button_markup(),
        )
    return ConversationHandler.END


async def editroute_name_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Update route name and return to the Edit Route Dashboard."""
    if not update.effective_message:
        return ConversationHandler.END
    route_id = int(context.user_data.get("editroute_id") or 0)
    name = _sanitize_single_line(update.effective_message.text or "")
    if not name:
        await update.effective_message.reply_text(
            "Please enter a valid non-empty route name, or /cancel."
        )
        return EDITROUTE_NAME
    updated = _store(context).update_route(route_id, name=name)
    if updated is None:
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            f"Route #{route_id} not found.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(
        f"✅ Route #{route_id} name updated to '{updated.name}'."
    )
    return await _send_editroute_menu(update.effective_message, context, route_id)


async def editroute_category_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle inline category button selection in EDITROUTE_CATEGORY."""
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END

    route_id = int(context.user_data.get("editroute_id") or 0)
    data = query.data or ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if data == "editroute:back":
        await query.answer()
        return await _send_editroute_menu(query.message, context, route_id)

    parts = data.split(":")
    try:
        cat = ROUTE_CATEGORIES[int(parts[2])]
    except (ValueError, IndexError):
        await query.answer("Invalid category.", show_alert=True)
        return EDITROUTE_CATEGORY

    updated = _store(context).update_route(route_id, category=cat)
    if updated is None:
        _clear_conversation_data(context)
        await query.answer("Route not found.", show_alert=True)
        return ConversationHandler.END
    await query.answer(f"Category set to {cat}.")
    await query.message.reply_text(
        f"Route #{route_id} ('{updated.name}') category set to {cat}."
    )
    return await _send_editroute_menu(query.message, context, route_id)


async def editroute_category_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle typed category in EDITROUTE_CATEGORY."""
    if not update.effective_message:
        return ConversationHandler.END
    route_id = int(context.user_data.get("editroute_id") or 0)
    raw = (update.effective_message.text or "").strip()
    category: Optional[str] = None
    if raw.isascii() and raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(ROUTE_CATEGORIES):
            category = ROUTE_CATEGORIES[idx]
    else:
        category = normalize_route_category(raw)

    if category is None:
        await update.effective_message.reply_text(
            f"Please choose one of: {', '.join(ROUTE_CATEGORIES)}, or /cancel."
        )
        return EDITROUTE_CATEGORY

    updated = _store(context).update_route(route_id, category=category)
    if updated is None:
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            f"Route #{route_id} not found.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(
        f"Route #{route_id} ('{updated.name}') category set to {category}."
    )
    return await _send_editroute_menu(update.effective_message, context, route_id)


async def editroute_points_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Update route top points (auto-clearing zone points if points <= zone_points)."""
    if not update.effective_message:
        return ConversationHandler.END
    route_id = int(context.user_data.get("editroute_id") or 0)
    pts = _parse_positive_int(update.effective_message.text or "")
    if pts is None:
        await update.effective_message.reply_text(
            "Please enter a valid positive whole number for points (e.g. 20), or /cancel."
        )
        return EDITROUTE_POINTS

    store = _store(context)
    old_route = store.get_route(route_id)
    if old_route is None:
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            f"Route #{route_id} not found.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END

    had_zone = old_route.zone_points is not None
    updated = store.update_route(route_id, points=pts)
    zone_cleared_note = (
        " (zone points cleared because top points are now <= previous zone points)"
        if (had_zone and updated and updated.zone_points is None)
        else ""
    )
    await update.effective_message.reply_text(
        f"✅ Route #{route_id} ('{old_route.name}') points set to {pts} pts{zone_cleared_note}."
    )
    return await _send_editroute_menu(update.effective_message, context, route_id)


async def editroute_zone_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle Clear Zone Points or Back buttons in EDITROUTE_ZONE."""
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END

    route_id = int(context.user_data.get("editroute_id") or 0)
    data = query.data or ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if data == "editroute:back":
        await query.answer()
        return await _send_editroute_menu(query.message, context, route_id)

    if data == "editroute:clearzone":
        updated = _store(context).update_route(route_id, zone_points=None)
        await query.answer("Zone points cleared.")
        if updated:
            await query.message.reply_text(
                f"Route #{route_id} ('{updated.name}') zone points cleared."
            )
        return await _send_editroute_menu(query.message, context, route_id)

    await query.answer("Invalid option.", show_alert=True)
    return EDITROUTE_ZONE


async def editroute_zone_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Validate and update route partial zone points in EDITROUTE_ZONE."""
    if not update.effective_message:
        return ConversationHandler.END
    route_id = int(context.user_data.get("editroute_id") or 0)
    store = _store(context)
    route = store.get_route(route_id)
    if route is None:
        _clear_conversation_data(context)
        await update.effective_message.reply_text(
            f"Route #{route_id} not found.", reply_markup=_menu_button_markup()
        )
        return ConversationHandler.END

    if route.points <= 1:
        await update.effective_message.reply_text(
            "Routes worth 1 point cannot have partial zone points."
        )
        return await _send_editroute_menu(update.effective_message, context, route_id)

    raw = (update.effective_message.text or "").strip().lower()
    if raw in {"none", "off", "0", "clear", "skip"}:
        store.update_route(route_id, zone_points=None)
        await update.effective_message.reply_text(
            f"Route #{route_id} ('{route.name}') zone points cleared."
        )
        return await _send_editroute_menu(update.effective_message, context, route_id)

    z_pts = _parse_positive_int(raw)
    if z_pts is None or z_pts >= route.points:
        await update.effective_message.reply_text(
            f"Zone points for Route #{route_id} must be between 1 and {route.points - 1} (or 'none'), or /cancel."
        )
        return EDITROUTE_ZONE

    store.update_route(route_id, zone_points=z_pts)
    await update.effective_message.reply_text(
        f"Route #{route_id} ('{route.name}') zone points set to {z_pts} pts."
    )
    return await _send_editroute_menu(update.effective_message, context, route_id)


async def editroute_zone_non_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Prompt for valid numeric zone points in EDITROUTE_ZONE."""
    if update.effective_message:
        route_id = int(context.user_data.get("editroute_id") or 0)
        route = _store(context).get_route(route_id)
        if route is None or route.points <= 1:
            await update.effective_message.reply_text(
                "Routes worth 1 point cannot have partial zone points."
            )
            return await _send_editroute_menu(update.effective_message, context, route_id)
        await update.effective_message.reply_text(
            f"Please enter a whole number between 1 and {route.points - 1} (or 'none'), or /cancel."
        )
    return EDITROUTE_ZONE


async def editroute_receive_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Attach an uploaded picture to the route during EDITROUTE_PHOTOS."""
    if not update.effective_message:
        return ConversationHandler.END
    route_id = context.user_data.get("editroute_id")
    if route_id is None:
        await update.effective_message.reply_text(
            "Edit session expired. Please run /editroute again.",
            reply_markup=_menu_button_markup(),
        )
        return ConversationHandler.END
    photo_info = _extract_photo_info(update.effective_message)
    if photo_info is None:
        await update.effective_message.reply_text(
            "Please send a picture (photo), or send /done when finished, or /cancel."
        )
        return EDITROUTE_PHOTOS
    file_id, media_type = photo_info
    store = _store(context)
    store.add_route_photo(int(route_id), file_id, media_type=media_type)
    added = int(context.user_data.get("editroute_photo_count", 0)) + 1
    context.user_data["editroute_photo_count"] = added
    total = len(store.get_route_photos(int(route_id)))
    await update.effective_message.reply_text(
        f"📸 Picture added (#{added} in this batch, {total} total). Send more or /done.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Done Adding Pictures ✅", callback_data="editroute:photos_done")]]
        ),
    )
    return EDITROUTE_PHOTOS


async def editroute_photos_done(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Finish adding pictures via /done and return to the Edit Route Dashboard."""
    route_id = int(context.user_data.get("editroute_id") or 0)
    added = int(context.user_data.pop("editroute_photo_count", 0))
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Finished adding {added} picture(s) to Route #{route_id}."
        )
        return await _send_editroute_menu(update.effective_message, context, route_id)
    return ConversationHandler.END


async def editroute_photos_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Finish adding pictures via inline button and return to the Edit Route Dashboard."""
    query = update.callback_query
    if not query or not query.message:
        return ConversationHandler.END
    if not _can_manage_routes(update):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return ConversationHandler.END
    route_id = int(context.user_data.get("editroute_id") or 0)
    added = int(context.user_data.pop("editroute_photo_count", 0))
    await query.answer("Done adding pictures.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(
        f"Finished adding {added} picture(s) to Route #{route_id}."
    )
    return await _send_editroute_menu(query.message, context, route_id)


async def stale_addroute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear expired conversation inline keyboards when pressed outside an active session."""
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if data.startswith(("addroute:", "editroute:")) and not _can_manage_routes(update):
        await query.answer("Routesetter or admin access required.", show_alert=True)
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.answer("This prompt has expired. Please run the command again.", show_alert=True)
