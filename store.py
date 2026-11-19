"""SQLite persistence layer for the ClimbX League bot."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from common import (
    _compute_competition_ranks,
    _sanitize_single_line,
    normalize_route_category,
)

LOGGER = logging.getLogger(__name__)

_UNSET = object()

_STUDENT_COLS = (
    "telegram_id, full_name, username, COALESCE(username, full_name) AS display_name, "
    "email, registered_at"
)
_REG_REQUEST_SELECT = """
    SELECT rr.id, rr.telegram_id,
           st.full_name AS old_full_name,
           st.username AS old_username,
           st.email AS old_email,
           rr.full_name, rr.username, rr.email,
           rr.status, rr.rejection_reason, rr.requested_at,
           rr.reviewed_at, rr.reviewer_id
    FROM registration_requests rr
    JOIN students st ON st.telegram_id = rr.telegram_id
"""
_ROUTE_SELECT = """
    SELECT r.id, r.name, r.points, r.active, r.category, r.zone_points,
           COUNT(rp.id) AS photo_count
    FROM routes r
    LEFT JOIN route_photos rp ON rp.route_id = r.id
"""
_SUBMISSION_SELECT = """
    SELECT s.id, s.telegram_id, st.full_name,
           COALESCE(st.username, st.full_name) AS username,
           st.email, s.route_id,
           r.name AS route_name, r.name, r.points, r.zone_points,
           s.video_file_id, s.media_type,
           s.status, COALESCE(s.is_zone, 0) AS is_zone,
           s.awarded_points, s.rejection_reason,
           s.submitted_at, s.reviewed_at, s.reviewer_id
    FROM submissions s
    JOIN students st ON st.telegram_id = s.telegram_id
    JOIN routes r ON r.id = s.route_id
"""


@dataclass(frozen=True)
class Route:
    id: int
    name: str
    points: int
    active: bool
    category: Optional[str] = None
    photo_count: int = 0
    zone_points: Optional[int] = None


def _row_to_route(row: sqlite3.Row) -> Route:
    return Route(
        id=int(row["id"]),
        name=str(row["name"]),
        points=int(row["points"]),
        active=bool(row["active"]),
        category=str(row["category"]) if row["category"] is not None else None,
        photo_count=int(row["photo_count"]),
        zone_points=int(row["zone_points"]) if row["zone_points"] is not None else None,
    )


def _clean_optional_username(username: Optional[str]) -> Optional[str]:
    if username is None:
        return None
    cleaned = _sanitize_single_line(username.strip().lstrip("@"))
    return cleaned if cleaned else None


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

    def _table_columns(self, table: str) -> set[str]:
        return {
            row["name"]
            for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                email TEXT NOT NULL,
                registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS registration_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES students(telegram_id),
                full_name TEXT NOT NULL,
                username TEXT,
                email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected')),
                rejection_reason TEXT,
                requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT,
                reviewer_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS admin_reg_request_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL REFERENCES registration_requests(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                UNIQUE(request_id, chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                points INTEGER NOT NULL CHECK(points > 0),
                active INTEGER NOT NULL DEFAULT 1,
                category TEXT
                    CHECK(category IS NULL OR category IN ('Arkose', 'ENSTA gym', 'Pan', 'Outdoors')),
                zone_points INTEGER
                    CHECK(zone_points IS NULL OR (zone_points > 0 AND zone_points < points))
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
                is_zone INTEGER NOT NULL DEFAULT 0,
                awarded_points INTEGER,
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

        student_cols = self._table_columns("students")
        if "username" not in student_cols:
            self._connection.execute("ALTER TABLE students ADD COLUMN username TEXT")

        # Migrate registration_requests if an older schema had username NOT NULL
        reg_req_info = self._connection.execute(
            "PRAGMA table_info(registration_requests)"
        ).fetchall()
        if any(row["name"] == "username" and int(row["notnull"]) == 1 for row in reg_req_info):
            self._connection.executescript(
                """
                PRAGMA foreign_keys = OFF;
                ALTER TABLE registration_requests RENAME TO _old_registration_requests;
                CREATE TABLE registration_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES students(telegram_id),
                    full_name TEXT NOT NULL,
                    username TEXT,
                    email TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'approved', 'rejected')),
                    rejection_reason TEXT,
                    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at TEXT,
                    reviewer_id INTEGER
                );
                INSERT INTO registration_requests
                    (id, telegram_id, full_name, username, email, status,
                     rejection_reason, requested_at, reviewed_at, reviewer_id)
                SELECT id, telegram_id, full_name, username, email, status,
                       rejection_reason, requested_at, reviewed_at, reviewer_id
                FROM _old_registration_requests;
                DROP TABLE _old_registration_requests;
                PRAGMA foreign_keys = ON;
                """
            )

        route_cols = self._table_columns("routes")
        if "category" not in route_cols:
            self._connection.execute("ALTER TABLE routes ADD COLUMN category TEXT")
        if "zone_points" not in route_cols:
            self._connection.execute("ALTER TABLE routes ADD COLUMN zone_points INTEGER")

        sub_cols = self._table_columns("submissions")
        if "video_file_id" not in sub_cols and "photo_file_id" in sub_cols:
            self._connection.execute(
                "ALTER TABLE submissions RENAME COLUMN photo_file_id TO video_file_id"
            )
        for col_name, col_ddl in (
            ("reviewer_id", "INTEGER"),
            ("media_type", "TEXT NOT NULL DEFAULT 'video'"),
            ("rejection_reason", "TEXT"),
            ("is_zone", "INTEGER NOT NULL DEFAULT 0"),
            ("awarded_points", "INTEGER"),
        ):
            if col_name not in sub_cols:
                self._connection.execute(
                    f"ALTER TABLE submissions ADD COLUMN {col_name} {col_ddl}"
                )

        self._connection.execute("DROP INDEX IF EXISTS idx_submissions_active_unique")
        try:
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_pending_unique
                    ON submissions(telegram_id, route_id)
                    WHERE status = 'pending'
                """
            )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_top_unique
                    ON submissions(telegram_id, route_id)
                    WHERE status = 'approved' AND is_zone = 0
                """
            )
        except sqlite3.IntegrityError:
            LOGGER.warning("Existing duplicate active submissions prevented unique index creation")
        self._connection.commit()

    def register_student(
        self,
        telegram_id: int,
        full_name: str,
        email: str,
        username: Optional[str] = None,
    ) -> None:
        cleaned_name = _sanitize_single_line(full_name)
        if not cleaned_name:
            raise ValueError("full_name cannot be empty")
        cleaned_username = _clean_optional_username(username)
        self._connection.execute(
            """
            INSERT INTO students (telegram_id, full_name, username, email)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                full_name = excluded.full_name,
                username = excluded.username,
                email = excluded.email
            """,
            (telegram_id, cleaned_name, cleaned_username, email.strip().lower()),
        )
        self._connection.commit()

    def update_student_username(
        self, telegram_id: int, username: Optional[str]
    ) -> bool:
        """Directly updates a student's optional username without requiring admin review."""
        if not self.is_registered(telegram_id):
            return False
        cleaned_username = _clean_optional_username(username)
        cursor = self._connection.execute(
            "UPDATE students SET username = ? WHERE telegram_id = ?",
            (cleaned_username, telegram_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def get_student(self, telegram_id: int) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            f"SELECT {_STUDENT_COLS} FROM students WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()

    def is_registered(self, telegram_id: int) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM students WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            is not None
        )

    def get_all_students(self) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                f"SELECT {_STUDENT_COLS} FROM students ORDER BY registered_at"
            )
        )

    def get_pending_registration_request(self, telegram_id: int) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT id, telegram_id, full_name, username, email, status, requested_at
            FROM registration_requests
            WHERE telegram_id = ? AND status = 'pending'
            ORDER BY id DESC
            LIMIT 1
            """,
            (telegram_id,),
        ).fetchone()

    def create_registration_request(
        self,
        telegram_id: int,
        full_name: str,
        username: Optional[str],
        email: str,
    ) -> int:
        if not self.is_registered(telegram_id):
            raise ValueError("student is not registered yet")
        cleaned_name = _sanitize_single_line(full_name)
        if not cleaned_name:
            raise ValueError("name cannot be empty")
        cleaned_username = _clean_optional_username(username)
        if self.get_pending_registration_request(telegram_id) is not None:
            raise ValueError("already_pending")
        cursor = self._connection.execute(
            """
            INSERT INTO registration_requests (telegram_id, full_name, username, email)
            VALUES (?, ?, ?, ?)
            """,
            (telegram_id, cleaned_name, cleaned_username, email.strip().lower()),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def get_registration_request(self, request_id: int) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            f"{_REG_REQUEST_SELECT} WHERE rr.id = ?",
            (request_id,),
        ).fetchone()

    def list_registration_requests(
        self, status: Optional[str] = "pending"
    ) -> list[sqlite3.Row]:
        query = _REG_REQUEST_SELECT
        params: list[Any] = []
        if status and status != "all":
            query += " WHERE rr.status = ?"
            params.append(status)
        query += " ORDER BY rr.id"
        return list(self._connection.execute(query, params))

    def review_registration_request(
        self,
        request_id: int,
        status: str,
        reviewer_id: Optional[int] = None,
        rejection_reason: Optional[str] = None,
    ) -> Optional[sqlite3.Row]:
        if status not in {"approved", "rejected"}:
            raise ValueError("invalid review status")
        reason = rejection_reason.strip() if rejection_reason else None
        cursor = self._connection.execute(
            """
            UPDATE registration_requests
            SET status = ?, reviewed_at = CURRENT_TIMESTAMP, reviewer_id = ?,
                rejection_reason = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status, reviewer_id, reason, request_id),
        )
        if cursor.rowcount == 0:
            self._connection.commit()
            return None
        req = self.get_registration_request(request_id)
        if req is not None and status == "approved":
            self._connection.execute(
                """
                UPDATE students
                SET full_name = ?, username = ?, email = ?
                WHERE telegram_id = ?
                """,
                (
                    str(req["full_name"]),
                    req["username"],
                    str(req["email"]),
                    int(req["telegram_id"]),
                ),
            )
        self._connection.commit()
        return self.get_registration_request(request_id)

    def record_admin_reg_request_message(
        self, request_id: int, chat_id: int, message_id: int
    ) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO admin_reg_request_messages (request_id, chat_id, message_id)
            VALUES (?, ?, ?)
            """,
            (request_id, chat_id, message_id),
        )
        self._connection.commit()

    def get_admin_reg_request_messages(self, request_id: int) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT chat_id, message_id FROM admin_reg_request_messages WHERE request_id = ?",
                (request_id,),
            )
        )

    def delete_admin_reg_request_messages(self, request_id: int) -> None:
        self._connection.execute(
            "DELETE FROM admin_reg_request_messages WHERE request_id = ?", (request_id,)
        )
        self._connection.commit()

    def add_route(
        self,
        name: str,
        points: int,
        category: Optional[str] = None,
        photos: Optional[Sequence[tuple[str, str] | str]] = None,
        zone_points: Optional[int] = None,
    ) -> int:
        cleaned_name = _sanitize_single_line(name)
        if not cleaned_name:
            raise ValueError("route name cannot be empty")
        normalized_category: Optional[str] = None
        if category is not None:
            normalized_category = normalize_route_category(category)
            if normalized_category is None:
                raise ValueError("invalid route category")
        if zone_points is not None and (zone_points <= 0 or zone_points >= points):
            raise ValueError("zone_points must be between 1 and points - 1")

        validated_photos: list[tuple[str, str]] = []
        for item in photos or ():
            media_type, file_id = ("photo", item) if isinstance(item, str) else item
            if media_type not in {"photo", "document"}:
                raise ValueError("invalid photo media_type")
            if not file_id or not file_id.strip():
                raise ValueError("photo file_id cannot be empty")
            validated_photos.append((media_type, file_id.strip()))

        cursor = self._connection.execute(
            "INSERT INTO routes (name, points, category, zone_points) VALUES (?, ?, ?, ?)",
            (cleaned_name, points, normalized_category, zone_points),
        )
        route_id = int(cursor.lastrowid)
        if validated_photos:
            self._connection.executemany(
                "INSERT INTO route_photos (route_id, file_id, media_type) VALUES (?, ?, ?)",
                [(route_id, fid, mtype) for mtype, fid in validated_photos],
            )
        self._connection.commit()
        return route_id

    def update_route(
        self,
        route_id: int,
        *,
        name: Optional[str] = None,
        points: Optional[int] = None,
        category: Any = _UNSET,
        zone_points: Any = _UNSET,
        active: Optional[bool] = None,
    ) -> Optional[Route]:
        """Updates any combination of route fields and returns the updated Route."""
        route = self.get_route(route_id)
        if route is None:
            return None

        new_name = route.name if name is None else _sanitize_single_line(name)
        if not new_name:
            raise ValueError("route name cannot be empty")

        new_points = route.points if points is None else int(points)
        if new_points <= 0:
            raise ValueError("points must be positive")

        if category is _UNSET:
            new_category = route.category
        elif category is None:
            new_category = None
        else:
            new_category = normalize_route_category(str(category))
            if new_category is None:
                raise ValueError("invalid route category")

        if zone_points is _UNSET:
            new_zone = route.zone_points
            if new_zone is not None and new_zone >= new_points:
                new_zone = None
        else:
            new_zone = zone_points
            if new_zone is not None and (new_zone <= 0 or new_zone >= new_points):
                raise ValueError("zone_points must be between 1 and points - 1")

        new_active = route.active if active is None else bool(active)

        self._connection.execute(
            """
            UPDATE routes
            SET name = ?, points = ?, category = ?, zone_points = ?, active = ?
            WHERE id = ?
            """,
            (
                new_name,
                new_points,
                new_category,
                new_zone,
                1 if new_active else 0,
                route_id,
            ),
        )
        self._connection.commit()
        return self.get_route(route_id)

    def set_route_zone_points(self, route_id: int, zone_points: Optional[int]) -> bool:
        return self.update_route(route_id, zone_points=zone_points) is not None

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

    def clear_route_photos(self, route_id: int) -> int:
        """Removes all pictures attached to route_id and returns the count removed."""
        cursor = self._connection.execute(
            "DELETE FROM route_photos WHERE route_id = ?",
            (route_id,),
        )
        self._connection.commit()
        return int(cursor.rowcount)

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
        return self.update_route(route_id, category=category) is not None

    def set_route_active(self, route_id: int, active: bool) -> bool:
        return self.update_route(route_id, active=active) is not None

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
        query = f"{_ROUTE_SELECT} {where_clause} GROUP BY r.id ORDER BY r.id"
        return [_row_to_route(row) for row in self._connection.execute(query, params)]

    def get_route(self, route_id: int) -> Optional[Route]:
        row = self._connection.execute(
            f"{_ROUTE_SELECT} WHERE r.id = ? GROUP BY r.id",
            (route_id,),
        ).fetchone()
        return _row_to_route(row) if row is not None else None

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
                COUNT(DISTINCT CASE WHEN status = 'approved' AND COALESCE(is_zone, 0) = 0 THEN telegram_id END) AS completions_count,
                COUNT(DISTINCT CASE WHEN status = 'approved' AND COALESCE(is_zone, 0) = 1 THEN telegram_id END) AS zones_count,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count
            FROM submissions
            WHERE route_id = ?
            """,
            (route_id,),
        ).fetchone()
        return {
            "route": route,
            "photos": [dict(p) for p in photos],
            "completions_count": int(counts_row["completions_count"] or 0) if counts_row else 0,
            "zones_count": int(counts_row["zones_count"] or 0) if counts_row else 0,
            "pending_count": int(counts_row["pending_count"] or 0) if counts_row else 0,
            "user_status": (
                self.get_student_route_status(telegram_id, route_id)
                if telegram_id is not None
                else None
            ),
        }

    def get_student_route_status(self, telegram_id: int, route_id: int) -> Optional[str]:
        """Returns 'approved' (top), 'pending', or 'zone' for a student on route_id, or None."""
        row = self._connection.execute(
            """
            SELECT status, COALESCE(is_zone, 0) AS is_zone
            FROM submissions
            WHERE telegram_id = ? AND route_id = ? AND status IN ('approved', 'pending')
            ORDER BY CASE
                WHEN status = 'approved' AND COALESCE(is_zone, 0) = 0 THEN 1
                WHEN status = 'pending' THEN 2
                ELSE 3
            END
            LIMIT 1
            """,
            (telegram_id, route_id),
        ).fetchone()
        if row is None:
            return None
        if str(row["status"]) == "approved" and int(row["is_zone"]) == 1:
            return "zone"
        return str(row["status"])

    def get_student_route_statuses(self, telegram_id: int) -> dict[int, str]:
        """Returns mapping of route_id -> 'approved' | 'pending' | 'zone' for a student."""
        rows = self._connection.execute(
            """
            SELECT route_id, status, COALESCE(is_zone, 0) AS is_zone
            FROM submissions
            WHERE telegram_id = ? AND status IN ('approved', 'pending')
            ORDER BY CASE
                WHEN status = 'approved' AND COALESCE(is_zone, 0) = 1 THEN 1
                WHEN status = 'pending' THEN 2
                WHEN status = 'approved' AND COALESCE(is_zone, 0) = 0 THEN 3
            END
            """,
            (telegram_id,),
        ).fetchall()
        statuses: dict[int, str] = {}
        for row in rows:
            st = (
                "zone"
                if (str(row["status"]) == "approved" and int(row["is_zone"]) == 1)
                else str(row["status"])
            )
            statuses[int(row["route_id"])] = st
        return statuses

    def get_student_route_zone_points(self, telegram_id: int, route_id: int) -> int:
        row = self._connection.execute(
            """
            SELECT MAX(COALESCE(s.awarded_points, r.zone_points, 0)) AS pts
            FROM submissions s
            JOIN routes r ON r.id = s.route_id
            WHERE s.telegram_id = ? AND s.route_id = ?
              AND s.status = 'approved' AND COALESCE(s.is_zone, 0) = 1
            """,
            (telegram_id, route_id),
        ).fetchone()
        return int(row["pts"] or 0) if row else 0

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
            f"{_SUBMISSION_SELECT} WHERE s.id = ?",
            (submission_id,),
        ).fetchone()

    def pending_submissions(self) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                f"{_SUBMISSION_SELECT} WHERE s.status = 'pending' ORDER BY s.submitted_at, s.id"
            )
        )

    def all_submissions(self, status_filter: Optional[str] = None) -> list[sqlite3.Row]:
        flt = (status_filter or "all").strip().lower()
        where_map = {
            "pending": " WHERE s.status = 'pending'",
            "approved": " WHERE s.status = 'approved' AND COALESCE(s.is_zone, 0) = 0",
            "top": " WHERE s.status = 'approved' AND COALESCE(s.is_zone, 0) = 0",
            "zone": " WHERE s.status = 'approved' AND COALESCE(s.is_zone, 0) = 1",
            "rejected": " WHERE s.status = 'rejected'",
            "all": "",
        }
        if flt not in where_map:
            raise ValueError("invalid status filter")
        return list(
            self._connection.execute(f"{_SUBMISSION_SELECT}{where_map[flt]} ORDER BY s.id DESC")
        )

    def review_submission(
        self,
        submission_id: int,
        status: str,
        reviewer_id: Optional[int] = None,
        rejection_reason: Optional[str] = None,
        *,
        is_zone: bool = False,
        awarded_points: Optional[int] = None,
    ) -> Optional[sqlite3.Row]:
        if status not in {"approved", "zone", "rejected"}:
            raise ValueError("invalid review status")
        sub = self.get_submission(submission_id)
        if sub is None or str(sub["status"]) != "pending":
            return None

        route_points = int(sub["points"])
        preset_zone = int(sub["zone_points"]) if sub["zone_points"] is not None else None
        zone_mode = is_zone or status == "zone"
        reason = rejection_reason.strip() if rejection_reason else None

        if status == "rejected":
            db_status, db_is_zone, pts = "rejected", 0, None
        elif zone_mode:
            if preset_zone is None or preset_zone <= 0 or preset_zone >= route_points:
                raise ValueError("route has no zone points configured")
            if awarded_points is not None and awarded_points != preset_zone:
                raise ValueError("custom zone points are not allowed; use route zone_points")
            db_status, db_is_zone, pts = "approved", 1, preset_zone
        else:
            conflict = self._connection.execute(
                """
                SELECT id FROM submissions
                WHERE telegram_id = ? AND route_id = ? AND id != ?
                  AND status = 'approved' AND COALESCE(is_zone, 0) = 0
                LIMIT 1
                """,
                (int(sub["telegram_id"]), int(sub["route_id"]), submission_id),
            ).fetchone()
            if conflict is not None:
                return None
            db_status, db_is_zone = "approved", 0
            pts = route_points

        cursor = self._connection.execute(
            """
            UPDATE submissions
            SET status = ?, is_zone = ?, awarded_points = ?,
                reviewed_at = CURRENT_TIMESTAMP, reviewer_id = ?,
                rejection_reason = ?
            WHERE id = ? AND status = 'pending'
            """,
            (db_status, db_is_zone, pts, reviewer_id, reason, submission_id),
        )
        self._connection.commit()
        return self.get_submission(submission_id) if cursor.rowcount > 0 else None

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
            WHERE telegram_id = ? AND route_id = ? AND id != ?
              AND (status = 'pending' OR (status = 'approved' AND COALESCE(is_zone, 0) = 0))
            LIMIT 1
            """,
            (int(sub["telegram_id"]), int(sub["route_id"]), submission_id),
        ).fetchone()
        if conflict is not None:
            return None, "conflict_active_submission"
        self._connection.execute(
            """
            UPDATE submissions
            SET status = 'pending', is_zone = 0, awarded_points = NULL,
                reviewed_at = NULL, reviewer_id = NULL, rejection_reason = NULL
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
                       COALESCE(st.username, st.full_name) AS username,
                       COALESCE(SUM(best.earned_points), 0) AS points,
                       COALESCE(SUM(best.is_top), 0) AS tops,
                       COALESCE(SUM(best.is_zone_only), 0) AS zones
                FROM students st
                LEFT JOIN (
                    SELECT s.telegram_id,
                           s.route_id,
                           MAX(
                               CASE
                                   WHEN COALESCE(s.is_zone, 0) = 1
                                       THEN COALESCE(s.awarded_points, r.zone_points, 0)
                                   ELSE r.points
                               END
                           ) AS earned_points,
                           MAX(CASE WHEN COALESCE(s.is_zone, 0) = 0 THEN 1 ELSE 0 END) AS is_top,
                           CASE
                               WHEN MAX(CASE WHEN COALESCE(s.is_zone, 0) = 0 THEN 1 ELSE 0 END) = 0
                                AND MAX(CASE WHEN COALESCE(s.is_zone, 0) = 1 THEN 1 ELSE 0 END) = 1
                               THEN 1 ELSE 0
                           END AS is_zone_only
                    FROM submissions s
                    JOIN routes r ON r.id = s.route_id
                    WHERE s.status = 'approved'
                    GROUP BY s.telegram_id, s.route_id
                ) best ON best.telegram_id = st.telegram_id
                GROUP BY st.telegram_id
                ORDER BY points DESC, tops DESC, zones DESC,
                         COALESCE(st.username, st.full_name) COLLATE NOCASE ASC
                """
            )
        )

    def leaderboard(self) -> list[tuple[str, int]]:
        return [
            (str(row["username"]), int(row["points"]))
            for row in self.leaderboard_detailed()
        ]

    def get_student_profile(self, telegram_id: int) -> Optional[dict[str, Any]]:
        student = self.get_student(telegram_id)
        if student is None:
            return None

        rankings = _compute_competition_ranks(self.leaderboard_detailed())
        rank, points, tops, zones = 1, 0, 0, 0
        for comp_rank, row in rankings:
            if int(row["telegram_id"]) == telegram_id:
                rank = comp_rank
                points = int(row["points"])
                tops = int(row["tops"])
                zones = int(row["zones"])
                break

        approved_rows = self._connection.execute(
            """
            SELECT r.id, r.name, r.points, MAX(s.reviewed_at) AS reviewed_at
            FROM submissions s
            JOIN routes r ON r.id = s.route_id
            WHERE s.telegram_id = ? AND s.status = 'approved' AND COALESCE(s.is_zone, 0) = 0
            GROUP BY r.id
            ORDER BY r.id
            """,
            (telegram_id,),
        ).fetchall()

        zone_rows = self._connection.execute(
            """
            SELECT r.id, r.name, r.points AS route_points,
                   MAX(COALESCE(s.awarded_points, r.zone_points, 0)) AS points,
                   MAX(s.reviewed_at) AS reviewed_at
            FROM submissions s
            JOIN routes r ON r.id = s.route_id
            WHERE s.telegram_id = ? AND s.status = 'approved' AND COALESCE(s.is_zone, 0) = 1
              AND NOT EXISTS (
                  SELECT 1 FROM submissions s2
                  WHERE s2.telegram_id = s.telegram_id AND s2.route_id = s.route_id
                    AND s2.status = 'approved' AND COALESCE(s2.is_zone, 0) = 0
              )
            GROUP BY r.id
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

        raw_username = student["username"]
        return {
            "telegram_id": int(student["telegram_id"]),
            "full_name": str(student["full_name"]),
            "username": str(raw_username) if raw_username is not None else None,
            "display_name": str(student["display_name"]),
            "email": str(student["email"]),
            "registered_at": str(student["registered_at"]),
            "rank": rank,
            "total_students": len(rankings),
            "points": points,
            "tops": tops,
            "zones": zones,
            "approved_routes": [dict(r) for r in approved_rows],
            "zone_routes": [dict(r) for r in zone_rows],
            "pending_routes": [dict(r) for r in pending_rows],
        }

    def get_export_rows(self) -> list[dict[str, Any]]:
        """Returns ranked student standings with username, full_name, email and route summaries for CSV export."""
        rankings = _compute_competition_ranks(self.leaderboard_detailed())
        results: list[dict[str, Any]] = []
        for comp_rank, row in rankings:
            tid = int(row["telegram_id"])
            student = self.get_student(tid)
            profile_data = self.get_student_profile(tid)
            completed = list(profile_data["approved_routes"]) if profile_data else []
            zone_list = list(profile_data["zone_routes"]) if profile_data else []
            route_summaries = [f"#{r['id']} {r['name']}" for r in completed] + [
                f"#{r['id']} {r['name']} (Zone: {r['points']} pts)" for r in zone_list
            ]
            results.append(
                {
                    "rank": comp_rank,
                    "telegram_id": tid,
                    "username": str(row["username"]),
                    "full_name": str(row["full_name"]),
                    "email": str(student["email"]) if student else "",
                    "points": int(row["points"]),
                    "tops": int(row["tops"]),
                    "zones": int(row["zones"]),
                    "completed_routes": "; ".join(route_summaries),
                }
            )
        return results
