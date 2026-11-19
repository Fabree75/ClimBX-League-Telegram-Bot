# ClimbX League bot

Telegram bot for the climbing league at École Polytechnique. Students can register with their `@polytechnique.edu` email, view routes with personal progress badges, submit **video proof** of ascents, manage their profile, and view the live leaderboard. Routesetters manage routes and admins review submissions and profile modification requests.

## Features

- **Coexisting Role-Separated GUI & Slash Commands**:
  - Interactive GUI buttons and slash commands are both active at all times (GUI shown by default on `/start` and `/menu`).
  - Terminal bot messages include a `📋 Open Menu` button so users can reopen their role menus with one tap.
  - Role-separated menus:
    - **🧗 Climber Menu** (shown to all users)
    - **🔧 Routesetter Menu** (shown to routesetters and admins)
    - **🛡️ Admin Menu** (shown to admins)

- **Climber Flow**:
  - `/register`: Register full name, optional leaderboard username (can be skipped to display full name), and `@polytechnique.edu` email address, with a confirmation summary warning that full name and email cannot be freely modified afterwards.
  - `/profile` (or `/me`): View personal rank, username, full name, total points, completed tops, zones, pending submissions, and role badge.
  - `/editprofile`: Edit profile fields with a confirmation summary before saving. Username changes apply immediately; modifying full name or email submits an approval request to admins.
  - `/routes [category]`: View active routes grouped by location category (`Arkose`, `ENSTA gym`, `Pan`, `Outdoors`), points, zone points, picture count, and personal status (`✅ Completed`, `🔶 Zone`, `⏳ Pending review`).
  - `/route [route_id]`: View full info and pictures for a route (pictures always appear above the text consistently).
  - `/submit [route_id]`: Submit video proof for a route.
  - `/withdraw <route_id>`: Withdraw a pending submission before review.
  - `/leaderboard`: View live standings with podium medals (`🥇, 🥈, 🥉`), tie-breaking, and personal rank summary.
  - `/role`: Check your active role (`Climber`, `Routesetter`, `Admin`) and capabilities.
  - `/rules`: View competition explanations, scoring rules, and video proof requirements.
  - `/commands`: View available slash commands for your role.
  - `/cancel`: Cancel any ongoing multi-step action.

- **Routesetter Flow (`ROUTESETTER_IDS` & `ADMIN_IDS`)**:
  - `/addroute`: Guided or quick route creation with category (`Arkose`, `ENSTA gym`, `Pan`, `Outdoors`), name, points, optional partial zone points (for routes worth 2+ points), and pictures.
  - `/editroute [route_id]`: Unified interactive route editor to add or modify a route's name, category, top points, partial zone points (with 1-point route protection), pictures (add or clear), and active/inactive status.
  - `/routes all [category]`: View all routes including inactive ones.

- **Admin Moderation & Lifecycle (`ADMIN_IDS`)**:
  - **Inline Video Review Cards**: Admins receive video previews with `[Approve ✅]`, `[Zone (<pts> pts) 🔶]` (only shown if the route has established zone points), and `[Reject ❌]` buttons.
  - `/pending` & `/submissions [all|pending|approved|zone|rejected]`: List pending or all submissions.
  - `/submission <submission_id>`: View video and inline review card for any submission.
  - `/approve <id>` / `/zone <id>` / `/reject <id> [reason]` / `/reopen <id>`: Review or reopen submissions.
  - `/profilerequests`, `/approvereq <id>`, `/rejectreq <id> [reason]`: Review student profile modification requests for full name or email changes.
  - `/export`: Export current competition standings as `climbx_standings.csv`.
  - `/broadcast <message>`: Broadcast an announcement to all registered climbers.

## Setup

1. Create a bot with [BotFather](https://t.me/BotFather).
2. Install dependencies:
   ```bash
   python -m pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and configure `TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`, and `ROUTESETTER_IDS`:
   ```bash
   cp .env.example .env
   ```
4. Start the bot:
   ```bash
   python bot.py
   ```
