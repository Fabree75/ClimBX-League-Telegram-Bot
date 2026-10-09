import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import (
    LeagueStore,
    Route,
    _admin_ids,
    _is_admin,
    review,
    review_callback,
    toggle_route,
)


class LeagueStoreTests(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.NamedTemporaryFile(suffix=".sqlite3")
        self.store = LeagueStore(self.database.name)

    def tearDown(self):
        self.store.close()
        self.database.close()

    def test_approved_submission_counts_in_leaderboard(self):
        self.store.register_student(42, "Ada Lovelace", "ada@polytechnique.edu")
        route_id = self.store.add_route("Granite Wall", 25)
        submission_id = self.store.create_submission(42, route_id, "telegram-file-id")

        self.assertEqual(self.store.leaderboard(), [("Ada Lovelace", 0)])
        reviewed = self.store.review_submission(submission_id, "approved", reviewer_id=999)
        self.assertIsNotNone(reviewed)
        self.assertEqual(reviewed["route_name"], "Granite Wall")
        self.assertEqual(reviewed["reviewer_id"], 999)
        self.assertEqual(self.store.leaderboard(), [("Ada Lovelace", 25)])

    def test_rejected_submission_does_not_count(self):
        self.store.register_student(42, "Ada Lovelace", "ada@polytechnique.edu")
        route_id = self.store.add_route("Slab", 10)
        submission_id = self.store.create_submission(42, route_id, "telegram-file-id")

        reviewed = self.store.review_submission(submission_id, "rejected", reviewer_id=999)
        self.assertIsNotNone(reviewed)
        self.assertEqual(reviewed["status"], "rejected")
        self.assertEqual(self.store.leaderboard(), [("Ada Lovelace", 0)])

    def test_duplicate_submission_blocked_when_pending(self):
        self.store.register_student(42, "Ada Lovelace", "ada@polytechnique.edu")
        route_id = self.store.add_route("Overhang", 30)
        self.store.create_submission(42, route_id, "proof-1")

        self.assertEqual(self.store.get_student_route_status(42, route_id), "pending")
        with self.assertRaises(ValueError) as ctx:
            self.store.create_submission(42, route_id, "proof-2")
        self.assertEqual(str(ctx.exception), "already_pending")

    def test_duplicate_submission_blocked_when_approved(self):
        self.store.register_student(42, "Ada Lovelace", "ada@polytechnique.edu")
        route_id = self.store.add_route("Overhang", 30)
        submission_id = self.store.create_submission(42, route_id, "proof-1")
        self.store.review_submission(submission_id, "approved")

        self.assertEqual(self.store.get_student_route_status(42, route_id), "approved")
        with self.assertRaises(ValueError) as ctx:
            self.store.create_submission(42, route_id, "proof-2")
        self.assertEqual(str(ctx.exception), "already_approved")

    def test_resubmission_allowed_after_rejection(self):
        self.store.register_student(42, "Ada Lovelace", "ada@polytechnique.edu")
        route_id = self.store.add_route("Chimney", 15)
        sub1 = self.store.create_submission(42, route_id, "bad-proof")
        self.store.review_submission(sub1, "rejected")

        # After rejection, status is neither pending nor approved
        self.assertIsNone(self.store.get_student_route_status(42, route_id))

        # Can submit a second time
        sub2 = self.store.create_submission(42, route_id, "good-proof")
        self.assertNotEqual(sub1, sub2)
        self.store.review_submission(sub2, "approved")
        self.assertEqual(self.store.leaderboard(), [("Ada Lovelace", 15)])

    def test_route_deactivation_and_reactivation(self):
        self.store.register_student(42, "Ada Lovelace", "ada@polytechnique.edu")
        route_id = self.store.add_route("Roof Problem", 40)

        # Deactivate
        self.assertTrue(self.store.set_route_active(route_id, False))
        route = self.store.get_route(route_id)
        self.assertIsNotNone(route)
        self.assertFalse(route.active)

        # Should not show in default active-only list
        self.assertEqual(len(self.store.list_routes(active_only=True)), 0)
        # Should show when listing all
        all_routes = self.store.list_routes(active_only=False)
        self.assertEqual(len(all_routes), 1)
        self.assertEqual(all_routes[0].id, route_id)

        # Submission should fail on inactive route
        with self.assertRaises(ValueError) as ctx:
            self.store.create_submission(42, route_id, "roof-photo")
        self.assertEqual(str(ctx.exception), "route is not active")

        # Reactivate
        self.assertTrue(self.store.set_route_active(route_id, True))
        sub_id = self.store.create_submission(42, route_id, "roof-photo")
        self.assertIsInstance(sub_id, int)

    def test_double_review_returns_none(self):
        self.store.register_student(42, "Ada Lovelace", "ada@polytechnique.edu")
        route_id = self.store.add_route("Dyno", 20)
        sub_id = self.store.create_submission(42, route_id, "dyno-photo")

        first_review = self.store.review_submission(sub_id, "approved", reviewer_id=101)
        self.assertIsNotNone(first_review)

        # Subsequent review fails / returns None
        second_review = self.store.review_submission(sub_id, "rejected", reviewer_id=102)
        self.assertIsNone(second_review)

    def test_schema_migration_adds_missing_reviewer_id(self):
        # Create a database using old schema without reviewer_id
        old_db = tempfile.NamedTemporaryFile(suffix=".sqlite3")
        conn = sqlite3.connect(old_db.name)
        conn.executescript(
            """
            CREATE TABLE students (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL,
                registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                points INTEGER NOT NULL CHECK(points > 0),
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES students(telegram_id),
                route_id INTEGER NOT NULL REFERENCES routes(id),
                photo_file_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected')),
                submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT
            );
            """
        )
        conn.close()

        # LeagueStore should migrate the table and add reviewer_id
        migrated_store = LeagueStore(old_db.name)
        migrated_store.register_student(1, "Student A", "student@polytechnique.edu")
        r_id = migrated_store.add_route("Traverse", 10)
        s_id = migrated_store.create_submission(1, r_id, "photo")
        result = migrated_store.review_submission(s_id, "approved", reviewer_id=777)
        self.assertIsNotNone(result)
        self.assertEqual(result["reviewer_id"], 777)
        migrated_store.close()
        old_db.close()


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = tempfile.NamedTemporaryFile(suffix=".sqlite3")
        self.store = LeagueStore(self.database.name)

    async def asyncTearDown(self):
        self.store.close()
        self.database.close()

    def test_admin_ids_parsing(self):
        with patch.dict(os.environ, {"ADMIN_IDS": " 123, 456 , abc, 789 "}):
            self.assertEqual(_admin_ids(), {123, 456, 789})

    async def test_toggle_route_command(self):
        route_id = self.store.add_route("Campus Board", 15)

        update = MagicMock()
        update.effective_user.id = 999
        update.effective_message.text = "/deactivateroute 1"
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = [str(route_id)]
        context.application.bot_data = {"store": self.store}

        with patch.dict(os.environ, {"ADMIN_IDS": "999"}):
            await toggle_route(update, context)
            self.assertFalse(self.store.get_route(route_id).active)
            update.effective_message.reply_text.assert_called_with(
                f"Route #{route_id} ('Campus Board') has been deactivated."
            )

            # Reactivate
            update.effective_message.text = "/activateroute 1"
            await toggle_route(update, context)
            self.assertTrue(self.store.get_route(route_id).active)
            update.effective_message.reply_text.assert_called_with(
                f"Route #{route_id} ('Campus Board') has been reactivated."
            )

    async def test_review_command_single_argument(self):
        self.store.register_student(42, "Alan Turing", "alan@polytechnique.edu")
        route_id = self.store.add_route("Arête", 20)
        sub_id = self.store.create_submission(42, route_id, "arete-proof")

        update = MagicMock()
        update.effective_user.id = 999
        update.effective_message.text = f"/approve {sub_id}"
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = [str(sub_id)]
        context.application.bot_data = {"store": self.store}
        context.bot.send_message = AsyncMock()

        with patch.dict(os.environ, {"ADMIN_IDS": "999"}):
            await review(update, context)
            update.effective_message.reply_text.assert_called_with(
                f"Submission #{sub_id} approved."
            )
            context.bot.send_message.assert_called_once()
            self.assertIn("approved (+20 points)", context.bot.send_message.call_args[1]["text"])

    async def test_review_callback_flow(self):
        self.store.register_student(42, "Grace Hopper", "grace@polytechnique.edu")
        route_id = self.store.add_route("Roof Crack", 35)
        sub_id = self.store.create_submission(42, route_id, "proof-file")

        update = MagicMock()
        update.effective_user.id = 999
        update.effective_user.first_name = "SuperAdmin"
        query = MagicMock()
        query.data = f"review:approve:{sub_id}"
        query.message.caption = "Initial Submission Caption"
        query.answer = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        update.callback_query = query

        context = MagicMock()
        context.application.bot_data = {"store": self.store}
        context.bot.send_message = AsyncMock()

        with patch.dict(os.environ, {"ADMIN_IDS": "999"}):
            await review_callback(update, context)

            query.answer.assert_called()
            query.edit_message_caption.assert_called_once()
            caption_arg = query.edit_message_caption.call_args[1]["caption"]
            self.assertIn("APPROVED ✅ by SuperAdmin", caption_arg)

            context.bot.send_message.assert_called_once()
            self.assertEqual(context.bot.send_message.call_args[1]["chat_id"], 42)
            self.assertIn("approved (+35 points)", context.bot.send_message.call_args[1]["text"])


if __name__ == "__main__":
    unittest.main()
