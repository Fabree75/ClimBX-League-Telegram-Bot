# ClimbX League bot

Telegram bot for the climbing league at Ecole Polytechnique. Students can register, view admin-published routes, submit photo proof of ascents, and view the live leaderboard. Admins review submissions with photo previews and inline buttons before route points count.

## Features

- **Student Flow**:
  - `/register`: Register name and Polytechnique email address.
  - `/routes`: View currently active routes and points.
  - `/submit <route_id>`: Submit a photo proof for a route. Prevents duplicate submissions if already approved or pending review. Re-submission is permitted if previously rejected.
  - `/leaderboard`: View real-time student point rankings.
  - `/cancel`: Cancel active registration or submission flow.

- **Admin Moderation & Lifecycle**:
  - **Inline Review Cards**: Admins receive instant photo previews with `[Approve ✅]` and `[Reject ❌]` inline buttons whenever a student submits proof.
  - **Student Feedback**: Students automatically receive Telegram notifications when their climb is approved (with points awarded) or rejected.
  - `/addroute <name> <points>`: Publish a new climbing route.
  - `/deactivateroute <route_id>`: Deactivate an expired or retired route (prevents new submissions).
  - `/activateroute <route_id>`: Reactivate a route.
  - `/routes all`: View all routes including inactive ones.
  - `/pending`: List pending submissions.
  - `/approve <submission_id>` / `/reject <submission_id>`: Text commands to review submissions.

## Setup

1. Create a bot with [BotFather](https://t.me/BotFather).
2. Install dependencies:
   ```bash
   python -m pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN` and the comma-separated numeric Telegram IDs of competition admins in `ADMIN_IDS`:
   ```bash
   cp .env.example .env
   ```
4. Export the variables and start the bot:
   ```bash
   python bot.py
   ```

The SQLite database is initialized automatically. Photo proofs use Telegram file IDs, so images are hosted directly by Telegram without storing local image files on the server.
