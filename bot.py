"""ClimbX League Telegram bot entrypoint, application wiring, and compatibility facade.

Run with:
    python bot.py

The bot stores registration, routes, and video proof submissions in SQLite.
Telegram video file IDs are stored instead of downloading student videos.
"""

from __future__ import annotations

import asyncio
import logging
import os
import warnings
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from common import (
    ADDROUTEPHOTO_WAIT,
    ADDROUTE_CATEGORY,
    ADDROUTE_NAME,
    ADDROUTE_PHOTOS,
    ADDROUTE_POINTS,
    ADDROUTE_ZONE,
    GUI_PROMPT_INPUT,
    MAX_MEDIA_GROUP_SIZE,
    MAX_TELEGRAM_CAPTION_LENGTH,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    MSG_COMMAND_FILTER,
    REGISTER_EMAIL,
    REGISTER_NAME,
    REGISTER_USERNAME,
    REVIEW_CUSTOM_ZONE,
    REVIEW_REJECT_REASON,
    ROUTE_CATEGORIES,
    SAFE_MESSAGE_CHUNK_LENGTH,
    SQLITE_MAX_INT,
    SUBMIT_PROOF,
    SUBMIT_ROUTE,
    _admin_ids,
    _clear_conversation_data,
    _compute_competition_ranks,
    _extract_command_and_args,
    _extract_photo_info,
    _extract_video_info,
    _is_admin,
    _is_valid_email,
    _non_text_prompt,
    _parse_positive_int,
    _reply_chunked,
    _sanitize_single_line,
    _store,
    normalize_route_category,
)
from handlers.general import (
    admin_status,
    broadcast,
    cancel,
    cancel_idle,
    export_results,
    gui_callback,
    gui_conversation_entry_callback,
    gui_prompt_input_text,
    help_command,
    leaderboard,
    menu_command,
    mode_command,
    profile,
    start,
)
from handlers.registration import (
    register_email,
    register_name,
    register_start,
    register_username,
    regreq_callback,
    regrequests_cmd,
    review_reg_request_cmd,
)
from handlers.routes import (
    add_route_caption_entry,
    add_route_entry,
    add_route_photo_caption_entry,
    add_route_photo_cmd,
    addroute_category_callback,
    addroute_category_text,
    addroute_name,
    addroute_photos_callback,
    addroute_photos_done,
    addroute_points,
    addroute_receive_photo,
    addroute_zone_callback,
    addroute_zone_non_text,
    addroute_zone_skip,
    addroute_zone_text,
    addroutephoto_callback,
    addroutephoto_done,
    addroutephoto_receive,
    route_info,
    route_view_callback,
    routes,
    set_route_category_cmd,
    set_route_zone_points_cmd,
    stale_addroute_callback,
    toggle_route,
)
from handlers.submissions import (
    all_submissions_cmd,
    pending,
    receive_proof,
    reopen_submission_cmd,
    review,
    review_callback,
    review_custom_zone_text,
    review_prompt_entry_callback,
    review_reject_reason_text,
    submit_select_route,
    submit_start,
    view_submission,
    withdraw,
    zone_review_cmd,
)
from store import LeagueStore, Route

LOGGER = logging.getLogger(__name__)

__all__ = [
    "ADDROUTEPHOTO_WAIT",
    "ADDROUTE_CATEGORY",
    "ADDROUTE_NAME",
    "ADDROUTE_PHOTOS",
    "ADDROUTE_POINTS",
    "ADDROUTE_ZONE",
    "GUI_PROMPT_INPUT",
    "LeagueStore",
    "MAX_MEDIA_GROUP_SIZE",
    "MAX_TELEGRAM_CAPTION_LENGTH",
    "MAX_TELEGRAM_MESSAGE_LENGTH",
    "MSG_COMMAND_FILTER",
    "REGISTER_EMAIL",
    "REGISTER_NAME",
    "REGISTER_USERNAME",
    "REVIEW_CUSTOM_ZONE",
    "REVIEW_REJECT_REASON",
    "ROUTE_CATEGORIES",
    "Route",
    "SAFE_MESSAGE_CHUNK_LENGTH",
    "SQLITE_MAX_INT",
    "SUBMIT_PROOF",
    "SUBMIT_ROUTE",
    "_admin_ids",
    "_clear_conversation_data",
    "_compute_competition_ranks",
    "_extract_command_and_args",
    "_extract_photo_info",
    "_extract_video_info",
    "_is_admin",
    "_is_valid_email",
    "_parse_positive_int",
    "_reply_chunked",
    "_sanitize_single_line",
    "_store",
    "build_application",
    "main",
    "normalize_route_category",
    "receive_proof",
    "routes",
    "toggle_route",
]


async def _post_init(application: Application) -> None:
    commands = [
        BotCommand("menu", "Open interactive button GUI"),
        BotCommand("mode", "Toggle between GUI buttons and slash commands"),
        BotCommand("routes", "View climbing routes & categories"),
        BotCommand("route", "View full info & pictures of a route"),
        BotCommand("submit", "Submit video proof for a route"),
        BotCommand("withdraw", "Withdraw a pending submission"),
        BotCommand("leaderboard", "View league rankings"),
        BotCommand("profile", "View your personal score and completed routes"),
        BotCommand("register", "Register or request a profile update"),
        BotCommand("admin", "Check admin status & admin commands"),
        BotCommand("help", "View competition rules and commands"),
        BotCommand("cancel", "Cancel current action"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception:
        LOGGER.warning("Could not set bot commands menu")


async def _post_shutdown(application: Application) -> None:
    store = application.bot_data.get("store")
    if isinstance(store, LeagueStore):
        store.close()


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception(
        "Unhandled exception while processing update %s", update, exc_info=context.error
    )
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "An unexpected error occurred. Please try again in a moment."
            )
        except Exception:
            pass


def build_application(
    token: str, database_path: str | Path = "climbx_league.sqlite3"
) -> Application:
    """Build and configure the ClimbX League Telegram Application and all handlers."""
    store = LeagueStore(database_path)
    image_filter = MSG_COMMAND_FILTER & (filters.PHOTO | filters.Document.IMAGE)
    addroute_caption_filter = image_filter & filters.CaptionRegex(r"^/addroute(@\w+)?(\s|$)")
    addroutephoto_caption_filter = image_filter & filters.CaptionRegex(
        r"^/addroutephoto(@\w+)?(\s|$)"
    )

    text_msg = MSG_COMMAND_FILTER & filters.TEXT & ~filters.COMMAND
    non_text_msg = MSG_COMMAND_FILTER & ~filters.TEXT & ~filters.COMMAND

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"If 'per_message=False'",
            category=PTBUserWarning,
        )
        conversation = ConversationHandler(
            entry_points=[
                CommandHandler("register", register_start, filters=MSG_COMMAND_FILTER),
                CommandHandler("submit", submit_start, filters=MSG_COMMAND_FILTER),
                CommandHandler("addroute", add_route_entry, filters=MSG_COMMAND_FILTER),
                MessageHandler(addroute_caption_filter, add_route_caption_entry),
                CommandHandler("addroutephoto", add_route_photo_cmd, filters=MSG_COMMAND_FILTER),
                MessageHandler(addroutephoto_caption_filter, add_route_photo_caption_entry),
                CallbackQueryHandler(
                    review_prompt_entry_callback,
                    pattern=r"^review:(reject_reason|zone_custom):[0-9]+$",
                ),
                CallbackQueryHandler(
                    gui_conversation_entry_callback,
                    pattern=r"^gui:(cmd:(register|submit|addroute|broadcast)|(addphoto|setzone_route):[0-9]+)$",
                ),
            ],
            states={
                REGISTER_NAME: [
                    MessageHandler(text_msg, register_name),
                    MessageHandler(
                        non_text_msg,
                        _non_text_prompt(
                            "Please send your full name as text, or /cancel.", REGISTER_NAME
                        ),
                    ),
                ],
                REGISTER_USERNAME: [
                    MessageHandler(text_msg, register_username),
                    MessageHandler(
                        non_text_msg,
                        _non_text_prompt(
                            "Please send your username as text, or /cancel.", REGISTER_USERNAME
                        ),
                    ),
                ],
                REGISTER_EMAIL: [
                    MessageHandler(text_msg, register_email),
                    MessageHandler(
                        non_text_msg,
                        _non_text_prompt(
                            "Please send your email address as text, or /cancel.", REGISTER_EMAIL
                        ),
                    ),
                ],
                SUBMIT_ROUTE: [
                    MessageHandler(text_msg, submit_select_route),
                    MessageHandler(
                        non_text_msg,
                        _non_text_prompt(
                            "Please reply with a valid numeric route ID (e.g. 1), or /cancel.",
                            SUBMIT_ROUTE,
                        ),
                    ),
                ],
                SUBMIT_PROOF: [
                    MessageHandler(MSG_COMMAND_FILTER & filters.ALL & ~filters.COMMAND, receive_proof)
                ],
                ADDROUTE_CATEGORY: [
                    CallbackQueryHandler(
                        addroute_category_callback, pattern=r"^addroute:(cat:[0-9]+|cancel)$"
                    ),
                    MessageHandler(text_msg, addroute_category_text),
                    MessageHandler(
                        non_text_msg,
                        _non_text_prompt(
                            f"Please choose a category ({', '.join(ROUTE_CATEGORIES)}), or /cancel.",
                            ADDROUTE_CATEGORY,
                        ),
                    ),
                ],
                ADDROUTE_NAME: [
                    MessageHandler(text_msg, addroute_name),
                    MessageHandler(
                        non_text_msg,
                        _non_text_prompt(
                            "Please send the route name as text, or /cancel.", ADDROUTE_NAME
                        ),
                    ),
                ],
                ADDROUTE_POINTS: [
                    MessageHandler(text_msg, addroute_points),
                    MessageHandler(
                        non_text_msg,
                        _non_text_prompt(
                            "Please enter the points as a positive number, or /cancel.",
                            ADDROUTE_POINTS,
                        ),
                    ),
                ],
                ADDROUTE_ZONE: [
                    CallbackQueryHandler(addroute_zone_callback, pattern=r"^addroute:skipzone$"),
                    CommandHandler("skip", addroute_zone_skip, filters=MSG_COMMAND_FILTER),
                    MessageHandler(text_msg, addroute_zone_text),
                    MessageHandler(non_text_msg, addroute_zone_non_text),
                ],
                ADDROUTE_PHOTOS: [
                    CallbackQueryHandler(
                        addroute_photos_callback, pattern=r"^addroute:(done|cancel|setzone)$"
                    ),
                    CommandHandler(
                        ["done", "skip"], addroute_photos_done, filters=MSG_COMMAND_FILTER
                    ),
                    MessageHandler(image_filter & ~filters.COMMAND, addroute_receive_photo),
                    MessageHandler(
                        MSG_COMMAND_FILTER & filters.ALL & ~filters.COMMAND,
                        _non_text_prompt(
                            "Please send a picture (photo), or send /done (or /skip) to publish, or /cancel.",
                            ADDROUTE_PHOTOS,
                        ),
                    ),
                ],
                ADDROUTEPHOTO_WAIT: [
                    CallbackQueryHandler(addroutephoto_callback, pattern=r"^addroutephoto:done$"),
                    CommandHandler("done", addroutephoto_done, filters=MSG_COMMAND_FILTER),
                    MessageHandler(image_filter & ~filters.COMMAND, addroutephoto_receive),
                    MessageHandler(
                        MSG_COMMAND_FILTER & filters.ALL & ~filters.COMMAND,
                        _non_text_prompt(
                            "Please send a picture (photo), or send /done when finished, or /cancel.",
                            ADDROUTEPHOTO_WAIT,
                        ),
                    ),
                ],
                REVIEW_REJECT_REASON: [
                    MessageHandler(text_msg, review_reject_reason_text),
                    MessageHandler(
                        non_text_msg,
                        _non_text_prompt(
                            "Please send the rejection reason as text, or /cancel.",
                            REVIEW_REJECT_REASON,
                        ),
                    ),
                ],
                REVIEW_CUSTOM_ZONE: [
                    MessageHandler(text_msg, review_custom_zone_text),
                    MessageHandler(
                        non_text_msg,
                        _non_text_prompt(
                            "Please enter the partial zone points as a whole number, or /cancel.",
                            REVIEW_CUSTOM_ZONE,
                        ),
                    ),
                ],
                GUI_PROMPT_INPUT: [
                    MessageHandler(text_msg, gui_prompt_input_text),
                    MessageHandler(
                        non_text_msg,
                        _non_text_prompt(
                            "Please send your reply as text, or /cancel.", GUI_PROMPT_INPUT
                        ),
                    ),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel, filters=MSG_COMMAND_FILTER)],
            allow_reentry=True,
        )

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["store"] = store
    application.add_handlers(
        [
            conversation,
            CommandHandler("cancel", cancel_idle, filters=MSG_COMMAND_FILTER),
            CommandHandler("start", start, filters=MSG_COMMAND_FILTER),
            CommandHandler("menu", menu_command, filters=MSG_COMMAND_FILTER),
            CommandHandler(["mode", "gui"], mode_command, filters=MSG_COMMAND_FILTER),
            CommandHandler("help", help_command, filters=MSG_COMMAND_FILTER),
            CommandHandler("admin", admin_status, filters=MSG_COMMAND_FILTER),
            CommandHandler("routes", routes, filters=MSG_COMMAND_FILTER),
            CommandHandler(["route", "routeinfo"], route_info, filters=MSG_COMMAND_FILTER),
            CommandHandler("withdraw", withdraw, filters=MSG_COMMAND_FILTER),
            CommandHandler(["profile", "me"], profile, filters=MSG_COMMAND_FILTER),
            CommandHandler(
                ["setroutecategory", "setcategory"],
                set_route_category_cmd,
                filters=MSG_COMMAND_FILTER,
            ),
            CommandHandler(
                ["setzonepoints", "setzone"],
                set_route_zone_points_cmd,
                filters=MSG_COMMAND_FILTER,
            ),
            CommandHandler(
                ["deactivateroute", "activateroute"], toggle_route, filters=MSG_COMMAND_FILTER
            ),
            CommandHandler("pending", pending, filters=MSG_COMMAND_FILTER),
            CommandHandler(
                ["submissions", "allsubmissions"],
                all_submissions_cmd,
                filters=MSG_COMMAND_FILTER,
            ),
            CommandHandler(["submission", "view"], view_submission, filters=MSG_COMMAND_FILTER),
            CommandHandler(["approve", "reject"], review, filters=MSG_COMMAND_FILTER),
            CommandHandler(["zone", "partial"], zone_review_cmd, filters=MSG_COMMAND_FILTER),
            CommandHandler("reopen", reopen_submission_cmd, filters=MSG_COMMAND_FILTER),
            CommandHandler("regrequests", regrequests_cmd, filters=MSG_COMMAND_FILTER),
            CommandHandler(
                ["approvereg", "rejectreg"],
                review_reg_request_cmd,
                filters=MSG_COMMAND_FILTER,
            ),
            CommandHandler("export", export_results, filters=MSG_COMMAND_FILTER),
            CommandHandler("leaderboard", leaderboard, filters=MSG_COMMAND_FILTER),
            CommandHandler(["broadcast", "announce"], broadcast, filters=MSG_COMMAND_FILTER),
            CallbackQueryHandler(
                review_callback,
                pattern=r"^review:(approve|reject|reject_now|zone|zone_preset|back):[0-9]+$",
            ),
            CallbackQueryHandler(
                regreq_callback, pattern=r"^regreq:(approve|reject):[0-9]+$"
            ),
            CallbackQueryHandler(route_view_callback, pattern=r"^route:view:[0-9]+$"),
            CallbackQueryHandler(gui_callback, pattern=r"^gui:"),
            CallbackQueryHandler(stale_addroute_callback, pattern=r"^(addroute|addroutephoto):"),
        ]
    )
    application.add_error_handler(_error_handler)
    return application


def _load_env_file(path: str = ".env") -> None:
    """Loads key-value pairs from .env if present and not already set in os.environ."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _load_env_file()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN must be set")
    if not _admin_ids():
        LOGGER.warning("ADMIN_IDS is empty or invalid; no admin commands will be authorized.")
    application = build_application(token, os.getenv("DATABASE_PATH", "climbx_league.sqlite3"))
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling()


if __name__ == "__main__":
    main()
