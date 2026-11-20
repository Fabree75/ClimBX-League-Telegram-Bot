# ClimbX League bot

Telegram bot for the climbing league at École Polytechnique. Students can register with their `@polytechnique.edu` email, view routes with personal progress badges, submit **video proof** of ascents, manage their profile, and view the live leaderboard. Routesetters manage routes and admins review submissions and profile modification requests.

## Features

- **Coexisting Role-Separated GUI & Slash Commands**:
  - Interactive GUI buttons and slash commands are both active at all times (GUI shown by default on `/start` and `/menu`).
  - Terminal bot messages (including `/cancel`) include a `📋 Open Menu` button so users can reopen their role menus with one tap.
  - Role-separated menus:
    - **👤 Unregistered Menu** (shown to unregistered users: register, browse routes & challenges, leaderboard, role, rules, commands)
    - **🧗 Climber Menu** (shown to registered climbers, without the Register option)
    - **🔧 Routesetter Menu** (shown to routesetters and admins)
    - **🛡️ Admin Menu** (shown to admins)

- **Unregistered & Climber Flow**:
  - `/register`: Register full name, optional leaderboard username (can be skipped to display full name), and `@polytechnique.edu` email address, with a confirmation summary warning that full name and email cannot be freely modified afterwards.
  - `/profile` (or `/me`): View personal rank, username, full name, total points, completed tops, zones, completed challenges, pending submissions, and role badge, with an inline `✏️ Edit Profile` button.
  - `/editprofile`: Edit profile fields with a confirmation summary before saving. Username changes apply immediately; modifying full name or email submits an approval request to admins.
  - `/routes [category]` & `/route [route_id]`: Browse active routes grouped by location category (`Arkose`, `ENSTA gym`, `Pan`, `Outdoors`), full route details, pictures, and personal status (`✅ Completed`, `🔶 Zone`, `⏳ Pending review`).
  - `/challenges` & `/challenge [challenge_id]`: Browse active challenges, descriptions, points, pictures, and personal status (`✅ Completed`, `⏳ Pending review`).
  - `/submit [route_id]` & `/withdraw <route_id>`: Submit video proof for a route or withdraw a pending submission.
  - `/submitchallenge [challenge_id]` & `/withdrawchallenge <challenge_id>`: Submit **video or photo** proof for a challenge or withdraw a pending submission.
  - `/leaderboard`: View live standings (route + challenge points) with podium medals (`🥇, 🥈, 🥉`), tie-breaking, and personal rank summary.
  - `/role`, `/rules`, `/commands`, `/cancel`: Check active role, competition rules, role-filtered commands, or cancel an active action.

- **Routesetter Flow (In-Bot `/routesetters` + optional `.env` `ROUTESETTER_IDS` fallback, & `ADMIN_IDS`)**:
  - `/addroute` & `/editroute [route_id]`: Create and edit routes (category, name, points, optional zone points, pictures, active/inactive status).
  - `/addchallenge` & `/editchallenge [challenge_id]`: Create and edit challenges (name, optional description, points, optional pictures, activate/deactivate; admins can also permanently delete challenges).
  - `/routes all [category]` & `/challenges all`: View all routes or challenges including inactive ones.

- **Admin Moderation, Role Management & Audit Logs (`ADMIN_IDS`)**:
  - **Unified Pending Submission Review GUI**: `/pending` (and `⏳ Pending` in Admin Menu) provides a paginated interactive review queue across both route and challenge submissions with clickable buttons, `▶️ Review Next Pending`, `🔄 Refresh`, and post-review `⏭️ Next Pending` navigation.
  - **Submission Commands**: `/submissions`, `/submission <id>`, `/approve`, `/zone`, `/reject`, `/reopen`, plus `/challengesubmissions`, `/challengesubmission <id>`, `/approvechallenge`, `/rejectchallenge`, `/reopenchallenge`.
  - **Routesetter Role Management**: `/routesetters` (interactive GUI), `/addroutesetter <telegram_id>`, and `/removeroutesetter <telegram_id>` (backed by SQLite; `ROUTESETTER_IDS` in `.env` remains supported as an optional fallback).
  - **Audit Logging**: `/logs [all|climber|routesetter|admin|<telegram_id>]` (and `📜 Logs` in Admin Menu) provides a filterable, paginated history of climber, routesetter, and admin actions with CSV export.
  - **Profile Requests, Export & Broadcast**: `/profilerequests`, `/approvereq`, `/rejectreq`, `/export` (standings CSV including challenges), and `/broadcast <message>`.

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
