"""ClimbX League Telegram bot.

Run with:
    TELEGRAM_BOT_TOKEN=... python bot.py

The bot stores registration, routes, and proof submissions in SQLite. Telegram
photo file IDs are stored instead of downloading student images.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

LOGGER = logging.getLogger(__name__)
REGISTER_NAME, REGISTER_EMAIL, SUBMIT_PROOF = range(3)


@dataclass(frozen=True)
class Route:
    id: int
    name: str
    points: int
    active: bool


class LeagueStore:
    """Small SQLite repository containing the competition state."""

    def __init__(self, path: str | Path = "climbx_league.sqlite3") -> None:
        self.path = str(path)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL,
                registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                points INTEGER NOT NULL CHECK(points > 0),
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES students(telegram_id),
                route_id INTEGER NOT NULL REFERENCES routes(id),
                photo_file_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected')),
                submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT,
                reviewer_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_submissions_student_route_status
                ON submissions(telegram_id, route_id, status);
            """
        )
        columns = [
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(submissions)").fetchall()
        ]
        if "reviewer_id" not in columns:
            self._connection.execute("ALTER TABLE submissions ADD COLUMN reviewer_id INTEGER")
        self._connection.commit()

    def register_student(self, telegram_id: int, full_name: str, email: str) -> None:
        self._connection.execute(
            """
            INSERT INTO students (telegram_id, full_name, email)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                full_name = excluded.full_name,
                email = excluded.email
            """,
            (telegram_id, full_name.strip(), email.strip().lower()),
        )
        self._connection.commit()

    def is_registered(self, telegram_id: int) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM students WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return row is not None

    def add_route(self, name: str, points: int) -> int:
        cursor = self._connection.execute(
            "INSERT INTO routes (name, points) VALUES (?, ?)", (name.strip(), points)
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def set_route_active(self, route_id: int, active: bool) -> bool:
        cursor = self._connection.execute(
            "UPDATE routes SET active = ? WHERE id = ?", (1 if active else 0, route_id)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def list_routes(self, active_only: bool = True) -> list[Route]:
        query = "SELECT id, name, points, active FROM routes"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY id"
        return [
            Route(
                id=int(row["id"]),
                name=str(row["name"]),
                points=int(row["points"]),
                active=bool(row["active"]),
            )
            for row in self._connection.execute(query)
        ]

    def get_route(self, route_id: int) -> Optional[Route]:
        row = self._connection.execute(
            "SELECT id, name, points, active FROM routes WHERE id = ?", (route_id,)
        ).fetchone()
        if row is None:
            return None
        return Route(int(row["id"]), str(row["name"]), int(row["points"]), bool(row["active"]))

    def get_student_route_status(self, telegram_id: int, route_id: int) -> Optional[str]:
        """Returns 'approved' or 'pending' if the student has an active/completed submission, or None."""
        row = self._connection.execute(
            """
            SELECT status FROM submissions
            WHERE telegram_id = ? AND route_id = ? AND status IN ('approved', 'pending')
            ORDER BY CASE status WHEN 'approved' THEN 1 ELSE 2 END
            LIMIT 1
            """,
            (telegram_id, route_id),
        ).fetchone()
        return str(row["status"]) if row else None

    def create_submission(self, telegram_id: int, route_id: int, photo_file_id: str) -> int:
        route = self.get_route(route_id)
        if route is None or not route.active:
            raise ValueError("route is not active")
        existing_status = self.get_student_route_status(telegram_id, route_id)
        if existing_status == "approved":
            raise ValueError("already_approved")
        if existing_status == "pending":
            raise ValueError("already_pending")
        cursor = self._connection.execute(
            """
            INSERT INTO submissions (telegram_id, route_id, photo_file_id)
            VALUES (?, ?, ?)
            """,
            (telegram_id, route_id, photo_file_id),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def get_submission(self, submission_id: int) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT s.id, s.telegram_id, st.full_name, s.route_id, r.name AS route_name,
                   r.points, s.photo_file_id, s.status, s.submitted_at, s.reviewed_at,
                   s.reviewer_id
            FROM submissions s
            JOIN students st ON st.telegram_id = s.telegram_id
            JOIN routes r ON r.id = s.route_id
            WHERE s.id = ?
            """,
            (submission_id,),
        ).fetchone()

    def pending_submissions(self) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                """
                SELECT s.id, s.telegram_id, st.full_name, r.name, r.points,
                       s.submitted_at
                FROM submissions s
                JOIN students st ON st.telegram_id = s.telegram_id
                JOIN routes r ON r.id = s.route_id
                WHERE s.status = 'pending'
                ORDER BY s.submitted_at
                """
            )
        )

    def review_submission(
        self, submission_id: int, status: str, reviewer_id: Optional[int] = None
    ) -> Optional[sqlite3.Row]:
        if status not in {"approved", "rejected"}:
            raise ValueError("invalid review status")
        cursor = self._connection.execute(
            """
            UPDATE submissions
            SET status = ?, reviewed_at = CURRENT_TIMESTAMP, reviewer_id = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status, reviewer_id, submission_id),
        )
        self._connection.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_submission(submission_id)

    def leaderboard(self) -> list[tuple[str, int]]:
        return [
            (str(row["full_name"]), int(row["points"]))
            for row in self._connection.execute(
                """
                SELECT st.full_name, COALESCE(SUM(r.points), 0) AS points
                FROM students st
                LEFT JOIN submissions s
                    ON s.telegram_id = st.telegram_id AND s.status = 'approved'
                LEFT JOIN routes r ON r.id = s.route_id
                GROUP BY st.telegram_id
                ORDER BY points DESC, st.full_name
                """
            )
        ]


def _store(context: ContextTypes.DEFAULT_TYPE) -> LeagueStore:
    return context.application.bot_data["store"]


def _admin_ids() -> set[int]:
    return {
        int(value.strip())
        for value in os.getenv("ADMIN_IDS", "").split(",")
        if value.strip().isdigit()
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Welcome to the ClimbX League at Ecole Polytechnique!\n"
        "Use /register to join, /routes to see routes, and /submit to send proof.\n"
        "Use /leaderboard to see the rankings."
    )


async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("What is your full name?")
    return REGISTER_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["full_name"] = update.effective_message.text.strip()
    await update.effective_message.reply_text("What is your Polytechnique email address?")
    return REGISTER_EMAIL


async def register_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = update.effective_message.text.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        await update.effective_message.reply_text("Please send a valid email address.")
        return REGISTER_EMAIL
    _store(context).register_student(
        update.effective_user.id, context.user_data.pop("full_name"), email
    )
    await update.effective_message.reply_text("Registration saved. Good luck climbing!")
    return ConversationHandler.END


async def routes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    show_all = False
    if context.args and context.args[0].lower() == "all" and _is_admin(update):
        show_all = True
    available = _store(context).list_routes(active_only=not show_all)
    if not available:
        await update.effective_message.reply_text("No routes have been published yet.")
        return
    lines = [
        f"#{route.id}: {route.name} - {route.points} points" + ("" if route.active else " [Inactive]")
        for route in available
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _store(context).is_registered(update.effective_user.id):
        await update.effective_message.reply_text("Please register first with /register.")
        return ConversationHandler.END
    parts = update.effective_message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await update.effective_message.reply_text("Usage: /submit <route_id>")
        return ConversationHandler.END
    route_id = int(parts[1])
    store = _store(context)
    route = store.get_route(route_id)
    if route is None or not route.active:
        await update.effective_message.reply_text("That route is not active. Check /routes.")
        return ConversationHandler.END

    status = store.get_student_route_status(update.effective_user.id, route_id)
    if status == "approved":
        await update.effective_message.reply_text(
            f"You have already completed '{route.name}' and received points for it! 🎉"
        )
        return ConversationHandler.END
    if status == "pending":
        await update.effective_message.reply_text(
            f"You already have a pending submission for '{route.name}'. Please wait for an admin to review it."
        )
        return ConversationHandler.END

    context.user_data["route_id"] = route.id
    await update.effective_message.reply_text(
        f"Send one photo proving you climbed '{route.name}' ({route.points} points)."
    )
    return SUBMIT_PROOF


async def receive_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo = update.effective_message.photo
    if not photo:
        await update.effective_message.reply_text("Please send a photo, or /cancel.")
        return SUBMIT_PROOF
    store = _store(context)
    route_id = int(context.user_data.pop("route_id"))
    route = store.get_route(route_id)
    photo_file_id = photo[-1].file_id

    try:
        submission_id = store.create_submission(
            update.effective_user.id, route_id, photo_file_id
        )
    except ValueError as e:
        if str(e) == "already_approved":
            await update.effective_message.reply_text("You have already completed this route.")
        elif str(e) == "already_pending":
            await update.effective_message.reply_text(
                "You already have a pending submission for this route."
            )
        else:
            await update.effective_message.reply_text("Unable to submit: route is no longer active.")
        return ConversationHandler.END

    await update.effective_message.reply_text(
        f"Proof submitted (#{submission_id}). An admin will review it."
    )

    # Proactively notify admins with photo preview and inline action buttons
    student_name = update.effective_user.full_name or "Student"
    caption = (
        f"🧗 New Submission #{submission_id}\n"
        f"Student: {student_name} (ID: {update.effective_user.id})\n"
        f"Route: #{route.id} {route.name} ({route.points} pts)"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve ✅", callback_data=f"review:approve:{submission_id}"),
                InlineKeyboardButton("Reject ❌", callback_data=f"review:reject:{submission_id}"),
            ]
        ]
    )
    for admin_id in _admin_ids():
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=caption,
                reply_markup=keyboard,
            )
        except Exception:
            LOGGER.warning("Could not send proof preview to admin %s", admin_id)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("route_id", None)
    context.user_data.pop("full_name", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


def _is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in _admin_ids())


async def add_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    if len(context.args) < 2 or not context.args[-1].isdigit():
        await update.effective_message.reply_text("Usage: /addroute <name> <points>")
        return
    route_id = _store(context).add_route(" ".join(context.args[:-1]), int(context.args[-1]))
    await update.effective_message.reply_text(f"Route #{route_id} published.")


async def toggle_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    cmd = update.effective_message.text.split()[0].lstrip("/")
    deactivate = cmd.startswith("deactivate")
    if not context.args or not context.args[0].isdigit():
        usage_cmd = "deactivateroute" if deactivate else "activateroute"
        await update.effective_message.reply_text(f"Usage: /{usage_cmd} <route_id>")
        return
    route_id = int(context.args[0])
    store = _store(context)
    route = store.get_route(route_id)
    if route is None:
        await update.effective_message.reply_text(f"Route #{route_id} not found.")
        return
    new_active = not deactivate
    if route.active == new_active:
        state_str = "already active" if new_active else "already inactive"
        await update.effective_message.reply_text(
            f"Route #{route_id} ('{route.name}') is {state_str}."
        )
        return
    store.set_route_active(route_id, new_active)
    action_str = "reactivated" if new_active else "deactivated"
    await update.effective_message.reply_text(
        f"Route #{route_id} ('{route.name}') has been {action_str}."
    )


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    rows = _store(context).pending_submissions()
    await update.effective_message.reply_text(
        "No pending submissions."
        if not rows
        else "\n".join(
            f"#{row['id']} {row['full_name']} - {row['name']} ({row['points']} pts)"
            for row in rows
        )
    )


async def review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /approve <id> or /reject <id>")
        return
    status = "approved" if update.effective_message.text.startswith("/approve") else "rejected"
    submission_id = int(context.args[0])
    store = _store(context)
    submission = store.review_submission(
        submission_id, status, reviewer_id=update.effective_user.id
    )
    if not submission:
        await update.effective_message.reply_text("Submission not found or already reviewed.")
        return

    await update.effective_message.reply_text(f"Submission #{submission_id} {status}.")

    # Notify student
    student_id = int(submission["telegram_id"])
    route_name = submission["route_name"]
    points = int(submission["points"])
    if status == "approved":
        msg = f"🎉 Great job! Your submission for '{route_name}' was approved (+{points} points)!"
    else:
        msg = f"❌ Your submission for '{route_name}' was rejected. You can submit a new attempt with improved proof."

    try:
        await context.bot.send_message(chat_id=student_id, text=msg)
    except Exception:
        LOGGER.warning("Failed to notify student %s about submission review", student_id)


async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "review":
        return

    action, submission_id_str = parts[1], parts[2]
    if action not in {"approve", "reject"} or not submission_id_str.isdigit():
        return

    submission_id = int(submission_id_str)
    status = "approved" if action == "approve" else "rejected"
    store = _store(context)
    submission = store.review_submission(
        submission_id, status, reviewer_id=update.effective_user.id
    )

    admin_display = update.effective_user.first_name or "Admin"
    if not submission:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("This submission was already reviewed.", show_alert=True)
        return

    # Update admin message
    decision_text = "APPROVED ✅" if action == "approve" else "REJECTED ❌"
    current_caption = query.message.caption or ""
    new_caption = f"{current_caption}\n\nDecision: {decision_text} by {admin_display}"
    try:
        await query.edit_message_caption(caption=new_caption, reply_markup=None)
    except Exception:
        LOGGER.warning("Failed to edit review message caption for submission %s", submission_id)

    # Notify student
    student_id = int(submission["telegram_id"])
    route_name = submission["route_name"]
    points = int(submission["points"])
    if action == "approve":
        msg = f"🎉 Great job! Your submission for '{route_name}' was approved (+{points} points)!"
    else:
        msg = f"❌ Your submission for '{route_name}' was rejected. You can submit a new attempt with improved proof."

    try:
        await context.bot.send_message(chat_id=student_id, text=msg)
    except Exception:
        LOGGER.warning("Failed to notify student %s about submission review", student_id)


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = _store(context).leaderboard()
    await update.effective_message.reply_text(
        "No scores yet."
        if not rows
        else "\n".join(f"{index}. {name} - {points} pts" for index, (name, points) in enumerate(rows, 1))
    )


def build_application(token: str, database_path: str | Path = "climbx_league.sqlite3") -> Application:
    store = LeagueStore(database_path)
    registration = ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_email)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    submission = ConversationHandler(
        entry_points=[CommandHandler("submit", submit_start)],
        states={SUBMIT_PROOF: [MessageHandler(filters.PHOTO, receive_proof)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application = ApplicationBuilder().token(token).build()
    application.bot_data["store"] = store
    application.add_handlers(
        [
            registration,
            submission,
            CommandHandler("start", start),
            CommandHandler("routes", routes),
            CommandHandler("addroute", add_route),
            CommandHandler(["deactivateroute", "activateroute"], toggle_route),
            CommandHandler("pending", pending),
            CommandHandler(["approve", "reject"], review),
            CommandHandler("leaderboard", leaderboard),
            CallbackQueryHandler(review_callback, pattern=r"^review:(approve|reject):\d+$"),
        ]
    )
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN must be set")
    application = build_application(token, os.getenv("DATABASE_PATH", "climbx_league.sqlite3"))
    application.run_polling()


if __name__ == "__main__":
    main()