# Telegram Work Assistant Bot

A non-AI Telegram bot that manages your daily work tasks and generates
three fixed-format status reports: **Beginning of Shift**, **Pre Lunch**,
and **End of Day**. Pure Python + SQLite — no LLM calls, no external AI
service.

## Features

- `/add`, `/list`, `/progress`, `/done`, `/block`, `/delete` — manage tasks
- `/bos`, `/prelunch`, `/eod` — generate the three report formats
- Automatic pending-task reminders every N hours (default 4) via APScheduler
- SQLite persistence, rotating file + console logging
- Clean module separation (config / models / database / reports / handlers / scheduler / bot)

## Design note: the extra `in_progress` status

The spec's `Task.status` field lists `pending/completed/blocked`, but the
required **Pre Lunch** report has three buckets: *Completed*, *In
Progress*, *Remaining*. Those can't both be true with only three
statuses, so a fourth status, `in_progress`, was added, along with a
`/progress <task_id>` command to set it. Everything else follows the
spec exactly. If you'd rather not have this extra status/command, you
can remove `TaskStatus.IN_PROGRESS` and the `/progress` handler — the
"In Progress" section of `/prelunch` will then just always be empty.

Similarly, the spec's example `Blocked` section shows only the reason
text (`• Waiting for Dev`) with no task name. This repo renders each
blocked line as `<task title> — <reason>` so multiple blocked tasks
stay distinguishable. If you need the literal reason-only format,
change `_blocked_lines()` in `reports.py`.

## Project structure

```
project/
    bot.py          # entry point: builds Application, registers handlers, runs polling
    handlers.py     # Telegram command handlers (thin, I/O-only)
    scheduler.py     # APScheduler job that sends periodic pending-task reminders
    database.py     # SQLite data-access layer (Database class)
    models.py       # Task dataclass + TaskStatus enum
    reports.py      # Pure functions: tasks -> formatted report strings
    config.py       # Loads and validates environment variables
    utils.py        # Logging setup + small arg-parsing helpers
    requirements.txt
    .env.example
    README.md
```

## Requirements

- Python 3.12+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

1. **Clone / copy the project**, then create a virtual environment:

   ```bash
   cd project
   python3.12 -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   ```

2. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

3. **Create your bot with @BotFather** on Telegram and copy the token it gives you.

4. **Configure environment variables:**

   ```bash
   cp .env.example .env
   ```

   Edit `.env`:

   ```
   BOT_TOKEN=your-real-token-here
   DB_PATH=tasks.db
   REMINDER_INTERVAL_HOURS=4
   REMINDERS_ENABLED=true
   TIMEZONE=Asia/Kolkata
   LOG_LEVEL=INFO
   LOG_FILE=bot.log
   ```

## Running locally

```bash
python bot.py
```

You should see log output like:

```
2026-07-20 10:00:00 | INFO | __main__ | Starting Telegram Work Assistant Bot...
2026-07-20 10:00:00 | INFO | database | Database initialised at tasks.db
2026-07-20 10:00:01 | INFO | scheduler | Scheduled pending-task reminders every 4 hour(s)
2026-07-20 10:00:01 | INFO | __main__ | Bot startup complete.
```

The bot uses long polling, so no public URL or webhook is required for
local/dev use.

### First-time use in Telegram

1. Open a chat with your bot and send `/start`.
   - This also registers your chat ID as the reminder destination
     (stored in the `settings` table), so make sure to run `/start`
     at least once before relying on automatic reminders.
2. Send `/help` to see the full command list.

## Usage examples

```
/add Fix login bug on staging
/add Prepare client demo deck
/list
/progress 1
/done 2
/block 3 Waiting for Dev
/delete 4
/bos
/prelunch
/eod
```

### Report output formats

**`/bos`**
```
Beginning of Shift
• Task A
• Task B
• Task C
```

**`/prelunch`**
```
Pre Lunch
Completed
• Task A
In Progress
• Task B
Remaining
• Task C
```

**`/eod`**
```
End of Day
Completed
• Task A
• Task B
Blocked
• Task C — Waiting for Dev
Carry Forward
• Task D
```

## Database

SQLite file created automatically at `DB_PATH` (default `tasks.db`) on
first run.

```sql
CREATE TABLE tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending/in_progress/completed/blocked
    blocked_reason  TEXT,
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    priority        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

`settings` currently stores a single key, `reminder_chat_id`, set by `/start`.

## Logging

Logs go to both stdout and a rotating file (`bot.log` by default, 2 MB
per file, 3 backups kept). Adjust verbosity with `LOG_LEVEL` in `.env`
(`DEBUG`, `INFO`, `WARNING`, `ERROR`).

## Running as a background service (optional)

**systemd (Linux)** — create `/etc/systemd/system/work-assistant-bot.service`:

```ini
[Unit]
Description=Telegram Work Assistant Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/project
ExecStart=/path/to/project/venv/bin/python bot.py
Restart=on-failure
EnvironmentFile=/path/to/project/.env

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now work-assistant-bot
```

## Testing the data/report layer without Telegram

`database.py` and `reports.py` have no Telegram or APScheduler
dependency, so you can exercise them directly, e.g.:

```python
from database import Database
from models import TaskStatus
import reports

db = Database("tasks.db")
db.add_task("Fix login bug")
print(reports.generate_bos(db.list_tasks()))
```

## Notes / limitations

- The bot is designed for **single-user** use (one chat receives
  reminders) — this matches "provide daily updates to my company"
  from one person. To support multiple users/chats, `settings` would
  need to become a per-chat table and `tasks` would need a `chat_id`
  column.
- Reports currently include *all* tasks currently in the DB, not
  scoped to "today" specifically. If you want EOD to only carry
  forward truly stale tasks, add a date filter in `reports.py` /
  `database.list_tasks`.