"""ClimbX League Telegram bot.

Run with:
    python bot.py

The bot stores registration, routes, and video proof submissions in SQLite.
Telegram video file IDs are stored instead of downloading student videos.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import os
import re
import sqlite3
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    Update,
)
from telegram.error import RetryAfter
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

LOGGER = logging.getLogger(__name__)
(
    REGISTER_NAME,
    REGISTER_EMAIL,
    SUBMIT_ROUTE,
    SUBMIT_PROOF,
    ADDROUTE_CATEGORY,
    ADDROUTE_NAME,
    ADDROUTE_POINTS,
    ADDROUTE_PHOTOS,
    ADDROUTEPHOTO_WAIT,
) = range(9)

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
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+$")
_MSG_COMMAND_FILTER = filters.UpdateType.MESSAGE


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
    return bool(_EMAIL_RE.fullmatch(email.strip()))


def _sanitize_single_line(text: str) -> str:
    return " ".join(text.split())


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

    for chunk in chunks:
        await message.reply_text(chunk, **kwargs)


def normalize_route_category(raw: str) -> Optional[str]:
    """Normalizes a route category string to one of ROUTE_CATEGORIES, or None."""
    cleaned = _sanitize_single_line(raw).lower()
    return _CATEGORY_ALIASES.get(cleaned)


@dataclass(frozen=True)
class Route:
    id: int
    name: str
    points: int
    active: bool
    category: Optional[str] = None
    photo_count: int = 0


class LeagueStore:
    """SQLite repository containing the competition state."""

    def __init__(self, path: str | Path = "climbx_league.sqlite3") -> None:
        self.path = str(path)
        self._connection = sqlite3.connect(self.path, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
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
                active INTEGER NOT NULL DEFAULT 1,
                category TEXT
                    CHECK(category IS NULL OR category IN ('Arkose', 'ENSTA gym', 'Pan', 'Outdoors'))
            );
            CREATE TABLE IF NOT EXISTS route_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
                file_id TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'photo'
                    CHECK(media_type IN ('photo', 'document')),
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_route_photos_route_id
                ON route_photos(route_id, id);
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES students(telegram_id),
                route_id INTEGER NOT NULL REFERENCES routes(id),
                video_file_id TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'video',
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected')),
                rejection_reason TEXT,
                submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT,
                reviewer_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_submissions_student_route_status
                ON submissions(telegram_id, route_id, status);
            CREATE TABLE IF NOT EXISTS admin_review_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                is_caption INTEGER NOT NULL DEFAULT 1,
                UNIQUE(submission_id, chat_id, message_id)
            );
            """
        )
        try:
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_active_unique
                    ON submissions(telegram_id, route_id)
                    WHERE status IN ('pending', 'approved')
                """
            )
        except sqlite3.IntegrityError:
            LOGGER.warning("Existing duplicate active submissions prevented unique index creation")
        route_columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(routes)").fetchall()
        }
        if "category" not in route_columns:
            self._connection.execute("ALTER TABLE routes ADD COLUMN category TEXT")
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(submissions)").fetchall()
        }
        if "video_file_id" not in columns and "photo_file_id" in columns:
            self._connection.execute(
                "ALTER TABLE submissions RENAME COLUMN photo_file_id TO video_file_id"
            )
            columns.add("video_file_id")
        if "reviewer_id" not in columns:
            self._connection.execute("ALTER TABLE submissions ADD COLUMN reviewer_id INTEGER")
        if "media_type" not in columns:
            self._connection.execute(
                "ALTER TABLE submissions ADD COLUMN media_type TEXT NOT NULL DEFAULT 'video'"
            )
        if "rejection_reason" not in columns:
            self._connection.execute("ALTER TABLE submissions ADD COLUMN rejection_reason TEXT")
        self._connection.commit()

    def register_student(self, telegram_id: int, full_name: str, email: str) -> None:
        cleaned_name = _sanitize_single_line(full_name)
        if not cleaned_name:
            raise ValueError("full_name cannot be empty")
        self._connection.execute(
            """
            INSERT INTO students (telegram_id, full_name, email)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                full_name = excluded.full_name,
                email = excluded.email
            """,
            (telegram_id, cleaned_name, email.strip().lower()),
        )
        self._connection.commit()

    def get_student(self, telegram_id: int) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            "SELECT telegram_id, full_name, email, registered_at FROM students WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()

    def is_registered(self, telegram_id: int) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM students WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return row is not None

    def get_all_students(self) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT telegram_id, full_name, email, registered_at FROM students ORDER BY registered_at"
            )
        )

    def add_route(
        self,
        name: str,
        points: int,
        category: Optional[str] = None,
        photos: Optional[Sequence[tuple[str, str] | str]] = None,
    ) -> int:
        cleaned_name = _sanitize_single_line(name)
        if not cleaned_name:
            raise ValueError("route name cannot be empty")
        normalized_category: Optional[str] = None
        if category is not None:
            normalized_category = normalize_route_category(category)
            if normalized_category is None:
                raise ValueError("invalid route category")

        validated_photos: list[tuple[str, str]] = []
        if photos:
            for item in photos:
                if isinstance(item, str):
                    media_type, file_id = "photo", item
                else:
                    media_type, file_id = item
                if media_type not in {"photo", "document"}:
                    raise ValueError("invalid photo media_type")
                if not file_id or not file_id.strip():
                    raise ValueError("photo file_id cannot be empty")
                validated_photos.append((media_type, file_id.strip()))

        cursor = self._connection.execute(
            "INSERT INTO routes (name, points, category) VALUES (?, ?, ?)",
            (cleaned_name, points, normalized_category),
        )
        route_id = int(cursor.lastrowid)
        if validated_photos:
            self._connection.executemany(
                "INSERT INTO route_photos (route_id, file_id, media_type) VALUES (?, ?, ?)",
                [(route_id, fid, mtype) for mtype, fid in validated_photos],
            )
        self._connection.commit()
        return route_id

    def add_route_photo(
        self, route_id: int, file_id: str, media_type: str = "photo"
    ) -> int:
        if self.get_route(route_id) is None:
            raise ValueError("route not found")
        if media_type not in {"photo", "document"}:
            raise ValueError("invalid photo media_type")
        if not file_id or not file_id.strip():
            raise ValueError("photo file_id cannot be empty")
        cursor = self._connection.execute(
            "INSERT INTO route_photos (route_id, file_id, media_type) VALUES (?, ?, ?)",
            (route_id, file_id.strip(), media_type),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def get_route_photos(self, route_id: int) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                """
                SELECT id, route_id, file_id, media_type, added_at
                FROM route_photos
                WHERE route_id = ?
                ORDER BY id
                """,
                (route_id,),
            )
        )

    def set_route_category(self, route_id: int, category: str) -> bool:
        normalized = normalize_route_category(category)
        if normalized is None:
            raise ValueError("invalid route category")
        cursor = self._connection.execute(
            "UPDATE routes SET category = ? WHERE id = ?",
            (normalized, route_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def set_route_active(self, route_id: int, active: bool) -> bool:
        cursor = self._connection.execute(
            "UPDATE routes SET active = ? WHERE id = ?", (1 if active else 0, route_id)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def list_routes(
        self, active_only: bool = True, category: Optional[str] = None
    ) -> list[Route]:
        conditions: list[str] = []
        params: list[Any] = []
        if active_only:
            conditions.append("r.active = 1")
        if category is not None:
            normalized = normalize_route_category(category)
            if normalized is None:
                raise ValueError("invalid route category")
            conditions.append("r.category = ?")
            params.append(normalized)
        where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT r.id, r.name, r.points, r.active, r.category,
                   COUNT(rp.id) AS photo_count
            FROM routes r
            LEFT JOIN route_photos rp ON rp.route_id = r.id
            {where_clause}
            GROUP BY r.id
            ORDER BY r.id
        """
        return [
            Route(
                id=int(row["id"]),
                name=str(row["name"]),
                points=int(row["points"]),
                active=bool(row["active"]),
                category=str(row["category"]) if row["category"] is not None else None,
                photo_count=int(row["photo_count"]),
            )
            for row in self._connection.execute(query, params)
        ]

    def get_route(self, route_id: int) -> Optional[Route]:
        row = self._connection.execute(
            """
            SELECT r.id, r.name, r.points, r.active, r.category,
                   COUNT(rp.id) AS photo_count
            FROM routes r
            LEFT JOIN route_photos rp ON rp.route_id = r.id
            WHERE r.id = ?
            GROUP BY r.id
            """,
            (route_id,),
        ).fetchone()
        if row is None:
            return None
        return Route(
            id=int(row["id"]),
            name=str(row["name"]),
            points=int(row["points"]),
            active=bool(row["active"]),
            category=str(row["category"]) if row["category"] is not None else None,
            photo_count=int(row["photo_count"]),
        )

    def get_route_details(
        self, route_id: int, telegram_id: Optional[int] = None
    ) -> Optional[dict[str, Any]]:
        route = self.get_route(route_id)
        if route is None:
            return None
        photos = self.get_route_photos(route_id)
        counts_row = self._connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS completions_count,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count
            FROM submissions
            WHERE route_id = ?
            """,
            (route_id,),
        ).fetchone()
        completions_count = int(counts_row["completions_count"] or 0) if counts_row else 0
        pending_count = int(counts_row["pending_count"] or 0) if counts_row else 0
        user_status = (
            self.get_student_route_status(telegram_id, route_id)
            if telegram_id is not None
            else None
        )
        return {
            "route": route,
            "photos": [dict(p) for p in photos],
            "completions_count": completions_count,
            "pending_count": pending_count,
            "user_status": user_status,
        }

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

    def get_student_route_statuses(self, telegram_id: int) -> dict[int, str]:
        """Returns mapping of route_id -> 'approved' | 'pending' for a student."""
        rows = self._connection.execute(
            """
            SELECT route_id, status FROM submissions
            WHERE telegram_id = ? AND status IN ('approved', 'pending')
            ORDER BY CASE status WHEN 'pending' THEN 1 WHEN 'approved' THEN 2 END
            """,
            (telegram_id,),
        ).fetchall()
        statuses: dict[int, str] = {}
        for row in rows:
            statuses[int(row["route_id"])] = str(row["status"])
        return statuses

    def create_submission(
        self,
        telegram_id: int,
        route_id: int,
        video_file_id: str,
        media_type: str = "video",
    ) -> int:
        route = self.get_route(route_id)
        if route is None or not route.active:
            raise ValueError("route is not active")
        existing_status = self.get_student_route_status(telegram_id, route_id)
        if existing_status == "approved":
            raise ValueError("already_approved")
        if existing_status == "pending":
            raise ValueError("already_pending")
        if media_type not in {"video", "video_note", "document", "animation"}:
            raise ValueError("invalid media_type")
        if not video_file_id or not video_file_id.strip():
            raise ValueError("video_file_id cannot be empty")
        cursor = self._connection.execute(
            """
            INSERT INTO submissions (telegram_id, route_id, video_file_id, media_type)
            VALUES (?, ?, ?, ?)
            """,
            (telegram_id, route_id, video_file_id, media_type),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def withdraw_pending_submission(
        self, telegram_id: int, route_id: int
    ) -> Optional[dict[str, Any]]:
        """Deletes a student's pending submission for route_id and returns its metadata."""
        row = self._connection.execute(
            """
            SELECT s.id, s.route_id, r.name AS route_name, s.telegram_id
            FROM submissions s
            JOIN routes r ON r.id = s.route_id
            WHERE s.telegram_id = ? AND s.route_id = ? AND s.status = 'pending'
            """,
            (telegram_id, route_id),
        ).fetchone()
        if row is None:
            return None
        submission_id = int(row["id"])
        admin_msgs = self.get_admin_review_messages(submission_id)
        self._connection.execute(
            "DELETE FROM submissions WHERE id = ? AND status = 'pending'",
            (submission_id,),
        )
        self._connection.commit()
        return {
            "id": submission_id,
            "route_id": int(row["route_id"]),
            "route_name": str(row["route_name"]),
            "telegram_id": int(row["telegram_id"]),
            "admin_messages": admin_msgs,
        }

    def get_submission(self, submission_id: int) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT s.id, s.telegram_id, st.full_name, st.email, s.route_id,
                   r.name AS route_name, r.points, s.video_file_id, s.media_type,
                   s.status, s.rejection_reason, s.submitted_at, s.reviewed_at,
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
                SELECT s.id, s.telegram_id, st.full_name, st.email, s.route_id,
                       r.name, r.points, s.video_file_id, s.media_type, s.submitted_at
                FROM submissions s
                JOIN students st ON st.telegram_id = s.telegram_id
                JOIN routes r ON r.id = s.route_id
                WHERE s.status = 'pending'
                ORDER BY s.submitted_at
                """
            )
        )

    def review_submission(
        self,
        submission_id: int,
        status: str,
        reviewer_id: Optional[int] = None,
        rejection_reason: Optional[str] = None,
    ) -> Optional[sqlite3.Row]:
        if status not in {"approved", "rejected"}:
            raise ValueError("invalid review status")
        reason = rejection_reason.strip() if rejection_reason else None
        cursor = self._connection.execute(
            """
            UPDATE submissions
            SET status = ?, reviewed_at = CURRENT_TIMESTAMP, reviewer_id = ?,
                rejection_reason = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status, reviewer_id, reason, submission_id),
        )
        self._connection.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_submission(submission_id)

    def reopen_submission(self, submission_id: int) -> tuple[Optional[sqlite3.Row], Optional[str]]:
        """Reopens an approved/rejected submission back to pending if no active duplicate exists."""
        sub = self.get_submission(submission_id)
        if sub is None:
            return None, "not_found"
        if str(sub["status"]) == "pending":
            return None, "already_pending"
        conflict = self._connection.execute(
            """
            SELECT id FROM submissions
            WHERE telegram_id = ? AND route_id = ? AND id != ? AND status IN ('pending', 'approved')
            LIMIT 1
            """,
            (int(sub["telegram_id"]), int(sub["route_id"]), submission_id),
        ).fetchone()
        if conflict is not None:
            return None, "conflict_active_submission"
        self._connection.execute(
            """
            UPDATE submissions
            SET status = 'pending', reviewed_at = NULL, reviewer_id = NULL, rejection_reason = NULL
            WHERE id = ?
            """,
            (submission_id,),
        )
        self._connection.commit()
        return self.get_submission(submission_id), None

    def record_admin_review_message(
        self, submission_id: int, chat_id: int, message_id: int, is_caption: bool
    ) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO admin_review_messages
                (submission_id, chat_id, message_id, is_caption)
            VALUES (?, ?, ?, ?)
            """,
            (submission_id, chat_id, message_id, 1 if is_caption else 0),
        )
        self._connection.commit()

    def get_admin_review_messages(self, submission_id: int) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                """
                SELECT chat_id, message_id, is_caption
                FROM admin_review_messages
                WHERE submission_id = ?
                """,
                (submission_id,),
            )
        )

    def delete_admin_review_messages(self, submission_id: int) -> None:
        self._connection.execute(
            "DELETE FROM admin_review_messages WHERE submission_id = ?", (submission_id,)
        )
        self._connection.commit()

    def leaderboard_detailed(self) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                """
                SELECT st.telegram_id,
                       st.full_name,
                       COALESCE(SUM(r.points), 0) AS points,
                       COUNT(s.id) AS tops
                FROM students st
                LEFT JOIN submissions s
                    ON s.telegram_id = st.telegram_id AND s.status = 'approved'
                LEFT JOIN routes r ON r.id = s.route_id
                GROUP BY st.telegram_id
                ORDER BY points DESC, tops DESC, st.full_name COLLATE NOCASE ASC
                """
            )
        )

    def leaderboard(self) -> list[tuple[str, int]]:
        return [
            (str(row["full_name"]), int(row["points"]))
            for row in self.leaderboard_detailed()
        ]

    def get_student_profile(self, telegram_id: int) -> Optional[dict[str, Any]]:
        student = self.get_student(telegram_id)
        if student is None:
            return None

        rankings = _compute_competition_ranks(self.leaderboard_detailed())
        rank = 1
        points = 0
        tops = 0
        for comp_rank, row in rankings:
            if int(row["telegram_id"]) == telegram_id:
                rank = comp_rank
                points = int(row["points"])
                tops = int(row["tops"])
                break

        approved_rows = self._connection.execute(
            """
            SELECT r.id, r.name, r.points, s.reviewed_at
            FROM submissions s
            JOIN routes r ON r.id = s.route_id
            WHERE s.telegram_id = ? AND s.status = 'approved'
            ORDER BY r.id
            """,
            (telegram_id,),
        ).fetchall()

        pending_rows = self._connection.execute(
            """
            SELECT s.id AS submission_id, r.id AS route_id, r.name, r.points, s.submitted_at
            FROM submissions s
            JOIN routes r ON r.id = s.route_id
            WHERE s.telegram_id = ? AND s.status = 'pending'
            ORDER BY s.submitted_at
            """,
            (telegram_id,),
        ).fetchall()

        return {
            "telegram_id": int(student["telegram_id"]),
            "full_name": str(student["full_name"]),
            "email": str(student["email"]),
            "registered_at": str(student["registered_at"]),
            "rank": rank,
            "total_students": len(rankings),
            "points": points,
            "tops": tops,
            "approved_routes": [dict(r) for r in approved_rows],
            "pending_routes": [dict(r) for r in pending_rows],
        }

    def get_export_rows(self) -> list[dict[str, Any]]:
        """Returns ranked student standings with email and route summaries for CSV export."""
        rankings = _compute_competition_ranks(self.leaderboard_detailed())
        results: list[dict[str, Any]] = []
        for comp_rank, row in rankings:
            tid = int(row["telegram_id"])
            student = self.get_student(tid)
            approved = self._connection.execute(
                """
                SELECT r.id, r.name
                FROM submissions s
                JOIN routes r ON r.id = s.route_id
                WHERE s.telegram_id = ? AND s.status = 'approved'
                ORDER BY r.id
                """,
                (tid,),
            ).fetchall()
            results.append(
                {
                    "rank": comp_rank,
                    "telegram_id": tid,
                    "full_name": str(row["full_name"]),
                    "email": str(student["email"]) if student else "",
                    "points": int(row["points"]),
                    "tops": int(row["tops"]),
                    "completed_routes": "; ".join(f"#{r['id']} {r['name']}" for r in approved),
                }
            )
        return results


def _store(context: ContextTypes.DEFAULT_TYPE) -> LeagueStore:
    return context.application.bot_data["store"]


def _admin_ids() -> set[int]:
    ids: set[int] = set()
    for value in os.getenv("ADMIN_IDS", "").split(","):
        parsed = _parse_positive_int(value)
        if parsed is not None:
            ids.add(parsed)
    return ids


def _is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in _admin_ids())


def _clear_conversation_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        "route_id",
        "full_name",
        "addroute_category",
        "addroute_name",
        "addroute_points",
        "addroute_photos",
        "addroutephoto_id",
        "addroutephoto_count",
    ):
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    text = (
        "🧗 Welcome to the ClimbX League at École Polytechnique!\n\n"
        "• /register - Join the league or update your info\n"
        "• /routes - View climbing routes & categories\n"
        "• /route <route_id> - View full details & pictures of a route\n"
        "• /submit <route_id> - Send a video proof of your ascent\n"
        "• /withdraw <route_id> - Withdraw a pending submission\n"
        "• /profile - Check your rank, score, and completed tops\n"
        "• /leaderboard - View the live standings\n"
        "• /help - Rules & full command guide"
    )
    if _is_admin(update):
        text += "\n\n🛡️ You have admin rights! Use /admin or /help to see admin commands."
    await update.effective_message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    text = (
        "🧗 <b>ClimbX League Guide &amp; Rules</b>\n\n"
        "<b>How to Participate:</b>\n"
        "1. Register once with /register.\n"
        "2. Check active routes with /routes (or filter by location: Arkose, ENSTA gym, Pan, Outdoors).\n"
        "3. Inspect full route details &amp; pictures with <code>/route &lt;route_id&gt;</code>.\n"
        "4. Record a video of your climb and submit it with <code>/submit &lt;route_id&gt;</code>.\n"
        "5. Once an admin approves your video, points are added to /leaderboard!\n\n"
        "<b>Video Proof Requirements:</b>\n"
        "• <b>Start</b>: Establish a controlled start with both hands on the starting holds.\n"
        "• <b>Ascent</b>: Complete the route using only allowed holds (no off-route dabs).\n"
        "• <b>Finish</b>: Match both hands on the top hold with control for 2 seconds.\n"
        "• <b>Format</b>: Must be a video (photos are not accepted).\n\n"
        "<b>Student Commands:</b>\n"
        "• /register - Register or update your name &amp; email\n"
        "• <code>/routes [category]</code> - List active routes and your status (✅/⏳)\n"
        "• <code>/route [route_id]</code> - View full route info &amp; pictures\n"
        "• <code>/submit [route_id]</code> - Submit video proof for a route\n"
        "• <code>/withdraw &lt;route_id&gt;</code> - Cancel a pending submission\n"
        "• /profile - View your personal stats and completed routes\n"
        "• /leaderboard - View competition rankings\n"
        "• /cancel - Cancel an ongoing action"
    )
    if _is_admin(update):
        text += (
            "\n\n🛡️ <b>Admin Status:</b> You are an admin and have admin rights.\n"
            "<b>Admin Commands:</b>\n"
            "• /admin - View your admin status &amp; rights\n"
            "• <code>/addroute</code> - Guided route creation (category, name, points, pictures)\n"
            "• <code>/addroute &lt;name&gt; &lt;points&gt; [category]</code> - Quick publish (attach photo optionally)\n"
            "• <code>/addroutephoto &lt;route_id&gt;</code> - Add picture(s) to an existing route\n"
            "• <code>/setroutecategory &lt;route_id&gt; &lt;category&gt;</code> - Set route location category\n"
            "• <code>/deactivateroute &lt;id&gt;</code> - Retire/deactivate a route\n"
            "• <code>/activateroute &lt;id&gt;</code> - Reactivate a route\n"
            "• <code>/routes all [category]</code> - View all routes (including inactive)\n"
            "• /pending - List submissions awaiting review\n"
            "• <code>/submission &lt;id&gt;</code> - View video &amp; inline review buttons\n"
            "• <code>/approve &lt;id&gt;</code> - Approve a submission\n"
            "• <code>/reject &lt;id&gt; [reason]</code> - Reject a submission with optional feedback\n"
            "• <code>/reopen &lt;id&gt;</code> - Reopen a reviewed submission to pending\n"
            "• /export - Export standings as CSV\n"
            "• <code>/broadcast &lt;message&gt;</code> - Send an announcement to all registered climbers"
        )
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        "• Route management: /addroute (with category & pictures), /addroutephoto, "
        "/setroutecategory, /activateroute, /deactivateroute, /routes all\n"
        "• Submission moderation: /pending, /submission <id>, /approve <id>, "
        "/reject <id> [reason], /reopen <id>, and inline Approve/Reject buttons\n"
        "• League operations: /export (CSV standings) and /broadcast <message>\n\n"
        f"Route categories: {categories_str}"
    )


async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    _clear_conversation_data(context)
    if getattr(update.effective_chat, "type", None) in {"group", "supergroup"}:
        await update.effective_message.reply_text(
            "Please send /register in a private chat with me so your email stays private."
        )
        return ConversationHandler.END
    existing = _store(context).get_student(update.effective_user.id)
    if existing:
        await update.effective_message.reply_text(
            f"You are currently registered as {existing['full_name']} ({existing['email']}).\n"
            "What is your full name? (or send /cancel to keep your current registration)"
        )
    else:
        await update.effective_message.reply_text("What is your full name?")
    return REGISTER_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    name = _sanitize_single_line(update.effective_message.text or "")
    if not name:
        await update.effective_message.reply_text("Please enter a valid full name.")
        return REGISTER_NAME
    context.user_data["full_name"] = name
    await update.effective_message.reply_text("What is your Polytechnique / IP Paris email address?")
    return REGISTER_EMAIL


async def register_name_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text("Please send your full name as text, or /cancel.")
    return REGISTER_NAME


async def register_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    email = (update.effective_message.text or "").strip().lower()
    if not _is_valid_email(email):
        await update.effective_message.reply_text("Please send a valid email address.")
        return REGISTER_EMAIL
    full_name = context.user_data.pop("full_name", None)
    if not full_name:
        await update.effective_message.reply_text(
            "Registration session expired. Please run /register again."
        )
        return ConversationHandler.END
    _store(context).register_student(update.effective_user.id, full_name, email)
    await update.effective_message.reply_text("Registration saved. Good luck climbing!")
    return ConversationHandler.END


async def register_email_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Please send your email address as text, or /cancel."
        )
    return REGISTER_EMAIL


async def routes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    args = list(context.args or [])
    show_all = False
    if args and args[0].lower() == "all":
        if _is_admin(update):
            show_all = True
        args = args[1:]
    elif args and len(args) > 1 and args[-1].lower() == "all":
        if _is_admin(update):
            show_all = True
        args = args[:-1]

    category_filter: Optional[str] = None
    if args:
        raw_cat = " ".join(args)
        category_filter = normalize_route_category(raw_cat)
        if category_filter is None:
            await update.effective_message.reply_text(
                f"Unknown category '{raw_cat}'. Available categories: {', '.join(ROUTE_CATEGORIES)}."
            )
            return

    store = _store(context)
    available = store.list_routes(active_only=not show_all, category=category_filter)
    if not available:
        if category_filter:
            await update.effective_message.reply_text(
                f"No routes found in category '{category_filter}'."
            )
        else:
            await update.effective_message.reply_text("No routes have been published yet.")
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
            elif status == "pending":
                badge = "⏳ "
                status_suffix = " (Pending review)"
            else:
                badge = ""
                status_suffix = ""

            pic_suffix = f" [📸 {route.photo_count}]" if route.photo_count > 0 else ""
            inactive_suffix = "" if route.active else " [Inactive]"
            lines.append(
                f"{badge}#{route.id}: {route.name} - {route.points} points"
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
    await _reply_chunked(update.effective_message, "\n".join(lines))


def _format_route_details_text(details: dict[str, Any]) -> str:
    route: Route = details["route"]
    photos: list[dict[str, Any]] = details["photos"]
    completions_count: int = details["completions_count"]
    pending_count: int = details["pending_count"]
    user_status: Optional[str] = details["user_status"]

    climber_word = "climber" if completions_count == 1 else "climbers"
    lines = [
        f"🧗 Route #{route.id}: {route.name}",
        f"📍 Location: {route.category or 'Uncategorized'}",
        f"🏆 Points: {route.points} pts",
        f"📌 Status: {'Active' if route.active else 'Inactive (archived)'}",
        f"🏅 Completions: {completions_count} {climber_word}",
        f"📸 Pictures: {len(photos)}",
    ]
    if pending_count > 0:
        lines.append(f"⏳ Pending submissions: {pending_count}")
    if user_status == "approved":
        lines.append("✅ Your status: Completed")
    elif user_status == "pending":
        lines.append("⏳ Your status: Pending review")
    elif route.active:
        lines.append(f"📝 Your status: Not submitted yet (use /submit {route.id})")
    return "\n".join(lines)


async def _send_route_details(
    message: Any, store: LeagueStore, route_id: int, user_id: Optional[int]
) -> None:
    details = store.get_route_details(route_id, telegram_id=user_id)
    if details is None:
        await message.reply_text(f"Route #{route_id} not found.")
        return

    info_text = _format_route_details_text(details)
    photos = details["photos"]
    if not photos:
        await _reply_chunked(message, info_text)
        return

    caption_fits = len(info_text) <= MAX_TELEGRAM_CAPTION_LENGTH
    try:
        if len(photos) == 1:
            photo = photos[0]
            caption = info_text if caption_fits else None
            if photo["media_type"] == "document":
                await message.reply_document(document=photo["file_id"], caption=caption)
            else:
                await message.reply_photo(photo=photo["file_id"], caption=caption)
            if not caption_fits:
                await _reply_chunked(message, info_text)
            return

        media_types = {p["media_type"] for p in photos}
        if len(media_types) == 1:
            only_type = next(iter(media_types))
            for idx in range(0, len(photos), MAX_MEDIA_GROUP_SIZE):
                batch = photos[idx : idx + MAX_MEDIA_GROUP_SIZE]
                media_items = []
                for pos, item in enumerate(batch):
                    cap = info_text if (idx == 0 and pos == 0 and caption_fits) else None
                    if only_type == "document":
                        media_items.append(InputMediaDocument(media=item["file_id"], caption=cap))
                    else:
                        media_items.append(InputMediaPhoto(media=item["file_id"], caption=cap))
                await message.reply_media_group(media=media_items)
            if not caption_fits:
                await _reply_chunked(message, info_text)
            return

        # Mixed photo and document attachments: send details text then each picture.
        await _reply_chunked(message, info_text)
        for item in photos:
            if item["media_type"] == "document":
                await message.reply_document(document=item["file_id"])
            else:
                await message.reply_photo(photo=item["file_id"])
    except Exception:
        LOGGER.warning("Failed to send pictures for route %s", route_id)
        await _reply_chunked(
            message, f"{info_text}\n\n⚠️ Could not load route picture(s)."
        )


async def route_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    store = _store(context)
    if not context.args:
        active_routes = store.list_routes(active_only=True)
        if not active_routes:
            await update.effective_message.reply_text("No active routes have been published yet.")
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
        await update.effective_message.reply_text(
            "Select a route to view its full info & pictures (or use /route <route_id>):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    route_id = _parse_positive_int(context.args[0], strip_hash=True)
    if route_id is None or len(context.args) > 1:
        await update.effective_message.reply_text("Usage: /route <route_id>")
        return

    user_id = update.effective_user.id if update.effective_user else None
    await _send_route_details(update.effective_message, store, route_id, user_id)


async def route_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await update.effective_message.reply_text(_video_prompt_for_route(route))
    return SUBMIT_PROOF


async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    _clear_conversation_data(context)
    store = _store(context)
    user_id = update.effective_user.id
    if not store.is_registered(user_id):
        await update.effective_message.reply_text("Please register first with /register.")
        return ConversationHandler.END

    parts = (update.effective_message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        active_routes = store.list_routes(active_only=True)
        if not active_routes:
            await update.effective_message.reply_text("No active routes are available right now.")
            return ConversationHandler.END
        statuses = store.get_student_route_statuses(user_id)
        open_routes = [r for r in active_routes if statuses.get(r.id) not in {"approved", "pending"}]
        if not open_routes:
            await update.effective_message.reply_text(
                "You have already completed or submitted all active routes! 🎉"
            )
            return ConversationHandler.END
        route_lines = "\n".join(
            f"#{r.id}: {r.name} ({r.points} pts"
            + (f" · {r.category}" if r.category else "")
            + ")"
            for r in open_routes
        )
        await _reply_chunked(
            update.effective_message,
            f"Which route are you submitting for?\n\n{route_lines}\n\n"
            "Reply with the route number (e.g. 1), or /cancel.",
        )
        return SUBMIT_ROUTE

    route_id = _parse_positive_int(parts[1], strip_hash=True)
    if route_id is None:
        await update.effective_message.reply_text("Usage: /submit <route_id>")
        return ConversationHandler.END

    return await _validate_and_prepare_route_submission(update, context, route_id)


async def submit_select_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.effective_message:
        return ConversationHandler.END
    route_id = _parse_positive_int(update.effective_message.text or "", strip_hash=True)
    if route_id is None:
        await update.effective_message.reply_text(
            "Please reply with a valid numeric route ID (e.g. 1), or /cancel."
        )
        return SUBMIT_ROUTE
    return await _validate_and_prepare_route_submission(update, context, route_id)


async def submit_select_route_non_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Please reply with a valid numeric route ID (e.g. 1), or /cancel."
        )
    return SUBMIT_ROUTE


def _format_submission_caption(
    submission_id: int,
    student_name: str,
    student_id: int,
    route_id: int,
    route_name: str,
    points: int,
    status: str = "pending",
    rejection_reason: Optional[str] = None,
) -> str:
    caption = (
        f"🧗 New Submission #{submission_id}\n"
        f"Student: {student_name} (ID: {student_id})\n"
        f"Route: #{route_id} {route_name} ({points} pts)"
    )
    if status != "pending":
        caption += f"\nStatus: {status.upper()}"
        if rejection_reason:
            caption += f"\nReason: {rejection_reason}"
    return caption


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
    )

    keyboard = (
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Approve ✅", callback_data=f"review:approve:{submission_id}"
                    ),
                    InlineKeyboardButton(
                        "Reject ❌", callback_data=f"review:reject:{submission_id}"
                    ),
                ]
            ]
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


async def receive_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        await message.reply_text("Submission session expired. Please run /submit again.")
        return ConversationHandler.END
    route_id = int(raw_route_id)
    route = store.get_route(route_id)

    try:
        submission_id = store.create_submission(
            update.effective_user.id, route_id, video_file_id, media_type=media_type
        )
    except ValueError as e:
        if str(e) == "already_approved":
            await message.reply_text("You have already completed this route.")
        elif str(e) == "already_pending":
            await message.reply_text("You already have a pending submission for this route.")
        else:
            await message.reply_text("Unable to submit: route is no longer active.")
        return ConversationHandler.END

    await message.reply_text(
        f"Proof submitted (#{submission_id}). An admin will review your video."
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
            )
            if sent_info is not None:
                msg_id, is_caption = sent_info
                store.record_admin_review_message(submission_id, admin_id, msg_id, is_caption)
        except Exception:
            LOGGER.warning("Could not send proof preview to admin %s", admin_id)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_conversation_data(context)
    if update.effective_message:
        await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def cancel_idle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_conversation_data(context)
    if update.effective_message:
        await update.effective_message.reply_text("No active action to cancel.")


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return
    store = _store(context)
    user_id = update.effective_user.id
    if not store.is_registered(user_id):
        await update.effective_message.reply_text("Please register first with /register.")
        return
    if not context.args or len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /withdraw <route_id>")
        return
    route_id = _parse_positive_int(context.args[0], strip_hash=True)
    if route_id is None:
        await update.effective_message.reply_text("Usage: /withdraw <route_id>")
        return

    withdrawn = store.withdraw_pending_submission(user_id, route_id)
    if withdrawn is None:
        await update.effective_message.reply_text(
            f"You have no pending submission for route #{route_id}."
        )
        return

    await update.effective_message.reply_text(
        f"Withdrew pending submission #{withdrawn['id']} for '{withdrawn['route_name']}'."
    )
    for row in withdrawn["admin_messages"]:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=int(row["chat_id"]),
                message_id=int(row["message_id"]),
                reply_markup=None,
            )
        except Exception:
            pass


_ADDROUTE_USAGE = (
    "Usage:\n"
    "• Interactive (with category & pictures): /addroute\n"
    "• Quick publish: /addroute <name> <points> [category]\n"
    "• Pipe format: /addroute <category> | <name> | <points>\n"
    f"Categories: {', '.join(ROUTE_CATEGORIES)}"
)


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


def _addroute_photos_keyboard(photo_count: int) -> InlineKeyboardMarkup:
    done_label = "Publish Route ✅" if photo_count > 0 else "Skip Pictures & Publish ✅"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(done_label, callback_data="addroute:done"),
                InlineKeyboardButton("Cancel ❌", callback_data="addroute:cancel"),
            ]
        ]
    )


def _format_published_message(
    route_id: int, category: Optional[str], points: int, photo_count: int
) -> str:
    if category is None and photo_count == 0:
        return f"Route #{route_id} published."
    details: list[str] = []
    if category is not None:
        details.append(category)
    details.append(f"{points} pts")
    if photo_count > 0:
        pic_word = "picture" if photo_count == 1 else "pictures"
        details.append(f"{photo_count} {pic_word}")
    return f"Route #{route_id} published ({', '.join(details)})."


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
    if not update.effective_message:
        return ConversationHandler.END
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return ConversationHandler.END

    raw_args = " ".join(context.args) if context.args else ""
    if not raw_args:
        return await _start_interactive_addroute(update.effective_message, context, None)

    parsed = _parse_addroute_args(raw_args)
    if parsed is None:
        await update.effective_message.reply_text(_ADDROUTE_USAGE)
        return ConversationHandler.END

    _clear_conversation_data(context)
    name, points, category = parsed
    route_id = _store(context).add_route(name, points, category=category)
    await update.effective_message.reply_text(
        _format_published_message(route_id, category, points, 0)
    )
    return ConversationHandler.END


async def add_route_caption_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    message = update.effective_message
    bot_username = getattr(context.bot, "username", None)
    parsed_cmd = _extract_command_and_args(message.caption or "", bot_username)
    if parsed_cmd is None or parsed_cmd[0] != "addroute":
        return ConversationHandler.END

    if not _is_admin(update):
        await message.reply_text("Admin access required.")
        return ConversationHandler.END

    photo_info = _extract_photo_info(message)
    if photo_info is None:
        await message.reply_text("Please attach a valid image/photo.")
        return ConversationHandler.END
    file_id, media_type = photo_info

    raw_args = parsed_cmd[1].strip()
    if not raw_args:
        return await _start_interactive_addroute(message, context, (media_type, file_id))

    parsed = _parse_addroute_args(raw_args)
    if parsed is None:
        await message.reply_text(_ADDROUTE_USAGE)
        return ConversationHandler.END

    _clear_conversation_data(context)
    name, points, category = parsed
    route_id = _store(context).add_route(
        name, points, category=category, photos=[(media_type, file_id)]
    )
    await message.reply_text(_format_published_message(route_id, category, points, 1))
    return ConversationHandler.END


async def addroute_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    if data == "addroute:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if query.message:
            await query.message.reply_text("Cancelled.")
        return ConversationHandler.END

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "addroute" or parts[1] != "cat":
        await query.answer("Invalid category selection.", show_alert=True)
        return ADDROUTE_CATEGORY

    try:
        idx = int(parts[2])
        category = ROUTE_CATEGORIES[idx]
    except (ValueError, IndexError):
        await query.answer("Invalid category selection.", show_alert=True)
        return ADDROUTE_CATEGORY

    context.user_data["addroute_category"] = category
    await query.answer(f"Selected {category}")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if query.message:
        await query.message.reply_text(
            f"📍 Category: {category}\nWhat is the route name? (or /cancel)"
        )
    return ADDROUTE_NAME


async def addroute_category_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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

    context.user_data["addroute_category"] = category
    await update.effective_message.reply_text(
        f"📍 Category: {category}\nWhat is the route name? (or /cancel)"
    )
    return ADDROUTE_NAME


async def addroute_category_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Please choose a category ({', '.join(ROUTE_CATEGORIES)}), or /cancel."
        )
    return ADDROUTE_CATEGORY


async def addroute_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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


async def addroute_name_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text("Please send the route name as text, or /cancel.")
    return ADDROUTE_NAME


async def addroute_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    points = _parse_positive_int(update.effective_message.text or "")
    if points is None:
        await update.effective_message.reply_text(
            "Please enter a valid positive whole number for points (e.g. 20), or /cancel."
        )
        return ADDROUTE_POINTS

    context.user_data["addroute_points"] = points
    existing_photos = context.user_data.setdefault("addroute_photos", [])
    photo_count = len(existing_photos)
    if photo_count > 0:
        prompt = (
            f"📸 {photo_count} picture(s) already attached.\n"
            "Send more pictures if you'd like, or tap Publish Route ✅ (or send /done) to publish."
        )
    else:
        prompt = (
            "📸 Send one or more pictures of the route.\n"
            "When finished, tap Publish Route ✅ (or send /done, or /skip for no pictures)."
        )
    await update.effective_message.reply_text(
        prompt, reply_markup=_addroute_photos_keyboard(photo_count)
    )
    return ADDROUTE_PHOTOS


async def addroute_points_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Please enter the points as a positive number, or /cancel."
        )
    return ADDROUTE_POINTS


async def addroute_receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    count = len(photos)
    await update.effective_message.reply_text(
        f"📸 Picture #{count} added. Send another picture, or send /done (or tap Publish Route ✅) to publish.",
        reply_markup=_addroute_photos_keyboard(count),
    )
    return ADDROUTE_PHOTOS


async def _finalize_addroute(
    message: Any, context: ContextTypes.DEFAULT_TYPE
) -> int:
    category = context.user_data.get("addroute_category")
    name = context.user_data.get("addroute_name")
    points = context.user_data.get("addroute_points")
    photos: list[tuple[str, str]] = list(context.user_data.get("addroute_photos") or [])
    _clear_conversation_data(context)

    if not name or points is None:
        await message.reply_text("Route creation session expired. Please run /addroute again.")
        return ConversationHandler.END

    route_id = _store(context).add_route(
        name, int(points), category=category, photos=photos
    )
    await message.reply_text(
        _format_published_message(route_id, category, int(points), len(photos))
    )
    return ConversationHandler.END


async def addroute_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    return await _finalize_addroute(update.effective_message, context)


async def addroute_photos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    if data == "addroute:cancel":
        _clear_conversation_data(context)
        await query.answer("Cancelled.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if query.message:
            await query.message.reply_text("Cancelled.")
        return ConversationHandler.END

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


async def addroute_photos_invalid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Please send a picture (photo), or send /done (or /skip) to publish, or /cancel."
        )
    return ADDROUTE_PHOTOS


async def stale_addroute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.answer("This prompt has expired. Please run the command again.", show_alert=True)


async def add_route_photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return ConversationHandler.END
    if not context.args or len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /addroutephoto <route_id>")
        return ConversationHandler.END
    route_id = _parse_positive_int(context.args[0], strip_hash=True)
    if route_id is None:
        await update.effective_message.reply_text("Usage: /addroutephoto <route_id>")
        return ConversationHandler.END
    route = _store(context).get_route(route_id)
    if route is None:
        await update.effective_message.reply_text(f"Route #{route_id} not found.")
        return ConversationHandler.END

    _clear_conversation_data(context)
    context.user_data["addroutephoto_id"] = route_id
    context.user_data["addroutephoto_count"] = 0
    await update.effective_message.reply_text(
        f"📸 Send picture(s) to attach to Route #{route.id} ('{route.name}').\n"
        "Send /done (or tap Done ✅) when finished, or /cancel.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Done ✅", callback_data="addroutephoto:done")]]
        ),
    )
    return ADDROUTEPHOTO_WAIT


async def add_route_photo_caption_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    message = update.effective_message
    bot_username = getattr(context.bot, "username", None)
    parsed_cmd = _extract_command_and_args(message.caption or "", bot_username)
    if parsed_cmd is None or parsed_cmd[0] != "addroutephoto":
        return ConversationHandler.END
    if not _is_admin(update):
        await message.reply_text("Admin access required.")
        return ConversationHandler.END

    arg_tokens = parsed_cmd[1].split()
    if len(arg_tokens) != 1:
        await message.reply_text("Usage: /addroutephoto <route_id>")
        return ConversationHandler.END
    route_id = _parse_positive_int(arg_tokens[0], strip_hash=True)
    if route_id is None:
        await message.reply_text("Usage: /addroutephoto <route_id>")
        return ConversationHandler.END
    store = _store(context)
    route = store.get_route(route_id)
    if route is None:
        await message.reply_text(f"Route #{route_id} not found.")
        return ConversationHandler.END

    photo_info = _extract_photo_info(message)
    if photo_info is None:
        await message.reply_text("Please attach a valid image/photo.")
        return ConversationHandler.END
    file_id, media_type = photo_info
    store.add_route_photo(route_id, file_id, media_type=media_type)
    total = len(store.get_route_photos(route_id))
    await message.reply_text(
        f"📸 Picture added to Route #{route.id} ('{route.name}'). Total pictures: {total}."
    )
    return ConversationHandler.END


async def addroutephoto_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    route_id = context.user_data.get("addroutephoto_id")
    if route_id is None:
        await update.effective_message.reply_text("Session expired. Please run /addroutephoto again.")
        return ConversationHandler.END
    photo_info = _extract_photo_info(update.effective_message)
    if photo_info is None:
        await update.effective_message.reply_text(
            "Please send a picture (photo), or send /done when finished, or /cancel."
        )
        return ADDROUTEPHOTO_WAIT
    file_id, media_type = photo_info
    store = _store(context)
    store.add_route_photo(int(route_id), file_id, media_type=media_type)
    added = int(context.user_data.get("addroutephoto_count", 0)) + 1
    context.user_data["addroutephoto_count"] = added
    total = len(store.get_route_photos(int(route_id)))
    await update.effective_message.reply_text(
        f"📸 Picture added (#{added} in this batch, {total} total). Send more or /done.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Done ✅", callback_data="addroutephoto:done")]]
        ),
    )
    return ADDROUTEPHOTO_WAIT


async def addroutephoto_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    route_id = context.user_data.get("addroutephoto_id")
    added = int(context.user_data.get("addroutephoto_count", 0))
    _clear_conversation_data(context)
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Finished adding {added} picture(s) to Route #{route_id}."
            if route_id is not None
            else "Done."
        )
    return ConversationHandler.END


async def addroutephoto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    if not _is_admin(update):
        await query.answer("Admin access required.", show_alert=True)
        return ConversationHandler.END
    route_id = context.user_data.get("addroutephoto_id")
    added = int(context.user_data.get("addroutephoto_count", 0))
    _clear_conversation_data(context)
    await query.answer("Done.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if query.message:
        await query.message.reply_text(
            f"Finished adding {added} picture(s) to Route #{route_id}."
            if route_id is not None
            else "Done."
        )
    return ConversationHandler.END


async def addroutephoto_invalid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Please send a picture (photo), or send /done when finished, or /cancel."
        )
    return ADDROUTEPHOTO_WAIT


async def set_route_category_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            f"Usage: /setroutecategory <route_id> <category>\nCategories: {', '.join(ROUTE_CATEGORIES)}"
        )
        return
    route_id = _parse_positive_int(context.args[0], strip_hash=True)
    raw_cat = " ".join(context.args[1:])
    category = normalize_route_category(raw_cat)
    if route_id is None or category is None:
        await update.effective_message.reply_text(
            f"Usage: /setroutecategory <route_id> <category>\nCategories: {', '.join(ROUTE_CATEGORIES)}"
        )
        return
    store = _store(context)
    route = store.get_route(route_id)
    if route is None:
        await update.effective_message.reply_text(f"Route #{route_id} not found.")
        return
    store.set_route_category(route_id, category)
    await update.effective_message.reply_text(
        f"Route #{route_id} ('{route.name}') category set to {category}."
    )


async def toggle_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    cmd = (update.effective_message.text or "").split()[0].lstrip("/")
    deactivate = cmd.startswith("deactivate")
    usage_cmd = "deactivateroute" if deactivate else "activateroute"
    if not context.args:
        await update.effective_message.reply_text(f"Usage: /{usage_cmd} <route_id>")
        return
    route_id = _parse_positive_int(context.args[0], strip_hash=True)
    if route_id is None:
        await update.effective_message.reply_text(f"Usage: /{usage_cmd} <route_id>")
        return
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
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    rows = _store(context).pending_submissions()
    if not rows:
        await update.effective_message.reply_text("No pending submissions.")
        return
    lines = [
        f"#{row['id']} {row['full_name']} - {row['name']} ({row['points']} pts)"
        for row in rows
    ]
    lines.append("\nTip: Use /submission <id> to watch video proof.")
    await _reply_chunked(update.effective_message, "\n".join(lines))


async def view_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /submission <submission_id>")
        return
    submission_id = _parse_positive_int(context.args[0], strip_hash=True)
    if submission_id is None:
        await update.effective_message.reply_text("Usage: /submission <submission_id>")
        return
    sub = _store(context).get_submission(submission_id)
    if not sub:
        await update.effective_message.reply_text(f"Submission #{submission_id} not found.")
        return

    try:
        await _send_submission_to_admin(
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
        )
    except Exception:
        LOGGER.warning("Failed to send submission %s video to admin", submission_id)
        await update.effective_message.reply_text(
            f"Submission #{submission_id} by {sub['full_name']} for '{sub['route_name']}' "
            f"(Status: {sub['status']}) — could not load video preview."
        )


def _format_student_review_notification(
    status: str, route_name: str, points: int, rejection_reason: Optional[str] = None
) -> str:
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


async def review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    is_approve = (update.effective_message.text or "").split()[0].startswith("/approve")
    submission_id = (
        _parse_positive_int(context.args[0], strip_hash=True) if context.args else None
    )
    if submission_id is None:
        await update.effective_message.reply_text(
            "Usage: /approve <id> or /reject <id> [reason]"
        )
        return
    if is_approve and len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage: /approve <id> or /reject <id> [reason]"
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
        await update.effective_message.reply_text("Submission not found or already reviewed.")
        return

    await update.effective_message.reply_text(f"Submission #{submission_id} {status}.")

    admin_display = update.effective_user.first_name or "Admin"
    decision_text = "APPROVED ✅" if is_approve else "REJECTED ❌"
    await _sync_admin_review_cards(
        context.bot,
        store,
        submission,
        f"{decision_text} by {admin_display}",
    )

    student_id = int(submission["telegram_id"])
    route_name = str(submission["route_name"])
    points = int(submission["points"])
    msg = _format_student_review_notification(status, route_name, points, rejection_reason)

    try:
        await context.bot.send_message(chat_id=student_id, text=msg)
    except Exception:
        LOGGER.warning("Failed to notify student %s about submission review", student_id)


async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    if action not in {"approve", "reject"} or submission_id is None:
        await query.answer("Invalid review action.", show_alert=True)
        return

    status = "approved" if action == "approve" else "rejected"
    store = _store(context)
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

    await query.answer(f"Submission #{submission_id} {status}.")

    admin_display = update.effective_user.first_name or "Admin"
    decision_text = "APPROVED ✅" if action == "approve" else "REJECTED ❌"
    decision_label = f"{decision_text} by {admin_display}"
    if getattr(query.message, "caption", None) is not None:
        current_caption = query.message.caption or ""
        new_caption = f"{current_caption}\n\nDecision: {decision_label}"
        try:
            await query.edit_message_caption(caption=new_caption, reply_markup=None)
        except Exception:
            LOGGER.warning("Failed to edit review message caption for submission %s", submission_id)
    else:
        current_text = getattr(query.message, "text", "") or ""
        new_text = f"{current_text}\n\nDecision: {decision_label}"
        try:
            await query.edit_message_text(text=new_text, reply_markup=None)
        except Exception:
            LOGGER.warning("Failed to edit review message text for submission %s", submission_id)

    exclude_chat_id = getattr(getattr(query.message, "chat", None), "id", None)
    exclude_message_id = getattr(query.message, "message_id", None)
    await _sync_admin_review_cards(
        context.bot,
        store,
        submission,
        decision_label,
        exclude_chat_id=exclude_chat_id if isinstance(exclude_chat_id, int) else None,
        exclude_message_id=exclude_message_id if isinstance(exclude_message_id, int) else None,
    )

    student_id = int(submission["telegram_id"])
    route_name = str(submission["route_name"])
    points = int(submission["points"])
    msg = _format_student_review_notification(status, route_name, points)

    try:
        await context.bot.send_message(chat_id=student_id, text=msg)
    except Exception:
        LOGGER.warning("Failed to notify student %s about submission review", student_id)


async def reopen_submission_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    submission_id = (
        _parse_positive_int(context.args[0], strip_hash=True) if context.args else None
    )
    if submission_id is None:
        await update.effective_message.reply_text("Usage: /reopen <submission_id>")
        return

    sub, error = _store(context).reopen_submission(submission_id)
    if error == "not_found":
        await update.effective_message.reply_text(f"Submission #{submission_id} not found.")
        return
    if error == "already_pending":
        await update.effective_message.reply_text(
            f"Submission #{submission_id} is already pending review."
        )
        return
    if error == "conflict_active_submission":
        await update.effective_message.reply_text(
            f"Cannot reopen submission #{submission_id}: student already has another pending or approved submission for that route."
        )
        return

    await update.effective_message.reply_text(
        f"Submission #{submission_id} reopened to pending. Use /submission {submission_id} to review it."
    )
    try:
        await context.bot.send_message(
            chat_id=int(sub["telegram_id"]),
            text=(
                f"🔄 Your submission #{submission_id} for '{sub['route_name']}' "
                "has been reopened for re-evaluation by an admin."
            ),
        )
    except Exception:
        LOGGER.warning("Failed to notify student %s about reopened submission", sub["telegram_id"])


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    lines = [f"🧗 Climber Profile: {info['full_name']}"]
    if is_admin:
        lines.append("Role: 🛡️ Admin (full admin rights)")
    lines.extend(
        [
            f"Email: {info['email']}",
            f"Rank: #{info['rank']} of {info['total_students']}",
            f"Score: {info['points']} pts ({info['tops']} {top_word})",
        ]
    )

    if info["approved_routes"]:
        lines.append("\n✅ Completed Routes:")
        for r in info["approved_routes"]:
            lines.append(f"• #{r['id']} {r['name']} ({r['points']} pts)")
    else:
        lines.append("\nNo completed routes yet. Check /routes and /submit!")

    if info["pending_routes"]:
        lines.append("\n⏳ Pending Review:")
        for r in info["pending_routes"]:
            lines.append(f"• #{r['route_id']} {r['name']} ({r['points']} pts)")

    await _reply_chunked(update.effective_message, "\n".join(lines))


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        name = str(row["full_name"])
        points = int(row["points"])
        tops = int(row["tops"])
        top_word = "top" if tops == 1 else "tops"
        lines.append(f"{prefix}{rank}. {name} - {points} pts ({tops} {top_word})")
        if caller_id is not None and int(row["telegram_id"]) == caller_id:
            caller_summary = f"\nYour rank: #{rank} of {len(rows)} ({points} pts, {tops} {top_word})"

    if caller_summary:
        lines.append(caller_summary)

    await _reply_chunked(update.effective_message, "\n".join(lines))


async def export_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return

    rows = _store(context).get_export_rows()
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "rank",
            "telegram_id",
            "full_name",
            "email",
            "points",
            "tops",
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


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    if not _is_admin(update):
        await update.effective_message.reply_text("Admin access required.")
        return
    parts = (update.effective_message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.effective_message.reply_text("Usage: /broadcast <message>")
        return

    announcement = f"📢 ClimbX League Announcement\n\n{parts[1].strip()}"
    if len(announcement) > MAX_TELEGRAM_MESSAGE_LENGTH:
        await update.effective_message.reply_text(
            f"Announcement is too long ({len(announcement)}/{MAX_TELEGRAM_MESSAGE_LENGTH} chars)."
        )
        return

    students = _store(context).get_all_students()
    if not students:
        await update.effective_message.reply_text("No registered students to broadcast to.")
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

    await update.effective_message.reply_text(
        f"Announcement sent to {sent_count}/{len(students)} registered students."
    )


async def _post_init(application: Application) -> None:
    commands = [
        BotCommand("routes", "View climbing routes & categories"),
        BotCommand("route", "View full info & pictures of a route"),
        BotCommand("submit", "Submit video proof for a route"),
        BotCommand("withdraw", "Withdraw a pending submission"),
        BotCommand("leaderboard", "View league rankings"),
        BotCommand("profile", "View your personal score and completed routes"),
        BotCommand("register", "Register or update your profile"),
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
    LOGGER.exception("Unhandled exception while processing update %s", update, exc_info=context.error)
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
    store = LeagueStore(database_path)
    addroute_caption_filter = _MSG_COMMAND_FILTER & (
        filters.PHOTO | filters.Document.IMAGE
    ) & filters.CaptionRegex(r"^/addroute(@\w+)?(\s|$)")
    addroutephoto_caption_filter = _MSG_COMMAND_FILTER & (
        filters.PHOTO | filters.Document.IMAGE
    ) & filters.CaptionRegex(r"^/addroutephoto(@\w+)?(\s|$)")

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"If 'per_message=False'",
            category=PTBUserWarning,
        )
        conversation = ConversationHandler(
            entry_points=[
                CommandHandler("register", register_start, filters=_MSG_COMMAND_FILTER),
                CommandHandler("submit", submit_start, filters=_MSG_COMMAND_FILTER),
                CommandHandler("addroute", add_route_entry, filters=_MSG_COMMAND_FILTER),
                MessageHandler(addroute_caption_filter, add_route_caption_entry),
                CommandHandler("addroutephoto", add_route_photo_cmd, filters=_MSG_COMMAND_FILTER),
                MessageHandler(addroutephoto_caption_filter, add_route_photo_caption_entry),
            ],
            states={
                REGISTER_NAME: [
                    MessageHandler(
                        _MSG_COMMAND_FILTER & filters.TEXT & ~filters.COMMAND, register_name
                    ),
                    MessageHandler(
                        _MSG_COMMAND_FILTER & ~filters.TEXT & ~filters.COMMAND,
                        register_name_non_text,
                    ),
                ],
                REGISTER_EMAIL: [
                    MessageHandler(
                        _MSG_COMMAND_FILTER & filters.TEXT & ~filters.COMMAND, register_email
                    ),
                    MessageHandler(
                        _MSG_COMMAND_FILTER & ~filters.TEXT & ~filters.COMMAND,
                        register_email_non_text,
                    ),
                ],
                SUBMIT_ROUTE: [
                    MessageHandler(
                        _MSG_COMMAND_FILTER & filters.TEXT & ~filters.COMMAND, submit_select_route
                    ),
                    MessageHandler(
                        _MSG_COMMAND_FILTER & ~filters.TEXT & ~filters.COMMAND,
                        submit_select_route_non_text,
                    ),
                ],
                SUBMIT_PROOF: [
                    MessageHandler(
                        _MSG_COMMAND_FILTER & filters.ALL & ~filters.COMMAND, receive_proof
                    )
                ],
                ADDROUTE_CATEGORY: [
                    CallbackQueryHandler(addroute_category_callback, pattern=r"^addroute:(cat:[0-9]+|cancel)$"),
                    MessageHandler(
                        _MSG_COMMAND_FILTER & filters.TEXT & ~filters.COMMAND,
                        addroute_category_text,
                    ),
                    MessageHandler(
                        _MSG_COMMAND_FILTER & ~filters.TEXT & ~filters.COMMAND,
                        addroute_category_non_text,
                    ),
                ],
                ADDROUTE_NAME: [
                    MessageHandler(
                        _MSG_COMMAND_FILTER & filters.TEXT & ~filters.COMMAND, addroute_name
                    ),
                    MessageHandler(
                        _MSG_COMMAND_FILTER & ~filters.TEXT & ~filters.COMMAND,
                        addroute_name_non_text,
                    ),
                ],
                ADDROUTE_POINTS: [
                    MessageHandler(
                        _MSG_COMMAND_FILTER & filters.TEXT & ~filters.COMMAND, addroute_points
                    ),
                    MessageHandler(
                        _MSG_COMMAND_FILTER & ~filters.TEXT & ~filters.COMMAND,
                        addroute_points_non_text,
                    ),
                ],
                ADDROUTE_PHOTOS: [
                    CallbackQueryHandler(
                        addroute_photos_callback, pattern=r"^addroute:(done|cancel)$"
                    ),
                    CommandHandler(["done", "skip"], addroute_photos_done, filters=_MSG_COMMAND_FILTER),
                    MessageHandler(
                        _MSG_COMMAND_FILTER
                        & (filters.PHOTO | filters.Document.IMAGE)
                        & ~filters.COMMAND,
                        addroute_receive_photo,
                    ),
                    MessageHandler(
                        _MSG_COMMAND_FILTER & filters.ALL & ~filters.COMMAND,
                        addroute_photos_invalid,
                    ),
                ],
                ADDROUTEPHOTO_WAIT: [
                    CallbackQueryHandler(addroutephoto_callback, pattern=r"^addroutephoto:done$"),
                    CommandHandler("done", addroutephoto_done, filters=_MSG_COMMAND_FILTER),
                    MessageHandler(
                        _MSG_COMMAND_FILTER
                        & (filters.PHOTO | filters.Document.IMAGE)
                        & ~filters.COMMAND,
                        addroutephoto_receive,
                    ),
                    MessageHandler(
                        _MSG_COMMAND_FILTER & filters.ALL & ~filters.COMMAND,
                        addroutephoto_invalid,
                    ),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel, filters=_MSG_COMMAND_FILTER)],
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
            CommandHandler("cancel", cancel_idle, filters=_MSG_COMMAND_FILTER),
            CommandHandler("start", start, filters=_MSG_COMMAND_FILTER),
            CommandHandler("help", help_command, filters=_MSG_COMMAND_FILTER),
            CommandHandler("admin", admin_status, filters=_MSG_COMMAND_FILTER),
            CommandHandler("routes", routes, filters=_MSG_COMMAND_FILTER),
            CommandHandler(["route", "routeinfo"], route_info, filters=_MSG_COMMAND_FILTER),
            CommandHandler("withdraw", withdraw, filters=_MSG_COMMAND_FILTER),
            CommandHandler(["profile", "me"], profile, filters=_MSG_COMMAND_FILTER),
            CommandHandler(
                ["setroutecategory", "setcategory"],
                set_route_category_cmd,
                filters=_MSG_COMMAND_FILTER,
            ),
            CommandHandler(
                ["deactivateroute", "activateroute"], toggle_route, filters=_MSG_COMMAND_FILTER
            ),
            CommandHandler("pending", pending, filters=_MSG_COMMAND_FILTER),
            CommandHandler(["submission", "view"], view_submission, filters=_MSG_COMMAND_FILTER),
            CommandHandler(["approve", "reject"], review, filters=_MSG_COMMAND_FILTER),
            CommandHandler("reopen", reopen_submission_cmd, filters=_MSG_COMMAND_FILTER),
            CommandHandler("export", export_results, filters=_MSG_COMMAND_FILTER),
            CommandHandler("leaderboard", leaderboard, filters=_MSG_COMMAND_FILTER),
            CommandHandler(["broadcast", "announce"], broadcast, filters=_MSG_COMMAND_FILTER),
            CallbackQueryHandler(review_callback, pattern=r"^review:(approve|reject):[0-9]+$"),
            CallbackQueryHandler(route_view_callback, pattern=r"^route:view:[0-9]+$"),
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
