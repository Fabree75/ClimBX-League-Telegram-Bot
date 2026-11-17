# ClimbX League bot

Telegram bot for the climbing league at École Polytechnique. Students can register, view admin-published routes with personal progress badges, submit **video proof** of ascents, check their personal profile, and view the live leaderboard. Admins review submissions with video previews and inline buttons before route points count.

## Features

- **Student Flow**:
  - `/menu` & `/mode [gui|text]`: Open the role-aware interactive button GUI dashboard or toggle between GUI buttons and raw slash commands.
  - `/register`: Register full name, leaderboard username, and Polytechnique / IP Paris email address (in private chat). Already-registered students submit a profile update request requiring admin approval.
  - `/routes [category]`: View currently active routes grouped by location category (`Arkose`, `ENSTA gym`, `Pan`, `Outdoors`), points, zone points, picture count, and your personal status (`✅ Completed`, `🔶 Zone`, `⏳ Pending review`) plus overall progress.
  - `/route [route_id]`: View full info and pictures for a specific route (pictures always appear above the route info text consistently whether there is 1 or multiple pictures).
  - `/submit [route_id]`: Submit a video proof for a route (either directly with `/submit <id>` or interactively via `/submit`). Supports regular videos, video notes, video documents, and muted videos (`animation`); rejects photo uploads with clear guidance on video proof rules. Prevents duplicate submissions if already topped or pending review, and allows re-submission if previously rejected or if upgrading from Zone to Top.
  - `/withdraw <route_id>`: Withdraw a pending submission before it is reviewed.
  - `/profile` (or `/me`): View personal rank, username, full name, total points, completed tops, zones, pending submissions, and admin role badge if applicable.
  - `/leaderboard`: View real-time rankings displaying climber usernames, podium medals (`🥇, 🥈, 🥉`), tops/zones tie-breaking, shared tie ranks, and personal rank summary.
  - `/help`: View competition video rules and available commands.
  - `/cancel`: Cancel active registration, submission, or route creation flow.

- **Admin Moderation & Lifecycle**:
  - **Admin Visibility & GUI**: `/admin` confirms admin status and lists all admin rights & commands; `/menu` and `/start` include dedicated admin action buttons when the user is an admin.
  - **Inline Video Review Cards**: Admins receive instant video previews with `[Approve ✅]`, `[Zone 🔶]`, and `[Reject ❌]` inline buttons whenever a student submits proof. Clicking `[Zone 🔶]` lets admins award preset or custom partial points; clicking `[Reject ❌]` asks whether to add a rejection reason or reject immediately. Reviewing on one admin's card automatically syncs and clears buttons across all other admins' cards.
  - **Student Feedback**: Students automatically receive Telegram notifications when their climb is approved (full Top or partial Zone) or rejected (including optional admin feedback).
  - `/addroute`: Guided interactive flow to publish a route with location category (`Arkose`, `ENSTA gym`, `Pan`, `Outdoors`), name, points, optional partial zone points, and one or more pictures. Also supports quick publish (`/addroute <name> <points> [category]`, `/addroute <category> | <name> | <points>`, or attaching a photo with the command as caption).
  - `/addroutephoto <route_id>`: Add picture(s) to an existing route (interactively or via photo caption).
  - `/setroutecategory <route_id> <category>`: Set or update a route's location category.
  - `/setzonepoints <route_id> <zone_points|none>` (or `/setzone`): Set or clear preset partial zone points on a route.
  - `/deactivateroute <route_id>`: Deactivate an expired or retired route (prevents new submissions).
  - `/activateroute <route_id>`: Reactivate a route.
  - `/routes all [category]`: View all routes including inactive ones.
  - `/pending`: List pending video submissions.
  - `/submissions [all|pending|approved|zone|rejected]` (or `/allsubmissions`): List all submissions, including closed/reviewed ones.
  - `/submission <submission_id>` (or `/view <submission_id>`): Re-send the video preview and inline review card for any submission on demand.
  - `/approve <submission_id>` / `/zone <submission_id> [points]` / `/reject <submission_id> [reason]`: Text commands to review submissions.
  - `/reopen <submission_id>`: Reopen an accidentally approved or rejected submission back to pending review.
  - `/regrequests`, `/approvereg <id>`, `/rejectreg <id> [reason]`: Review student re-registration / profile update requests.
  - `/export`: Sends an immediate progress notice and exports current competition standings as a CSV file (`climbx_standings.csv`).
  - `/broadcast <message>` (or `/announce <message>`): Broadcast an announcement to all registered climbers.

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

The SQLite database is initialized (and migrated) automatically. Video proofs use Telegram file IDs, so videos are hosted and streamed directly by Telegram without storing local video files on the server.
