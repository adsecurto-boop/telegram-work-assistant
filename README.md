# Telegram Work Assistant

A personal Windows Telegram bot for flexible shifts, task plans, support work,
testing, learning, and TOD / pre-lunch / EOD reports. SQLite is the source of
truth; Gemini drafts and categorization are optional.

## Setup on this PC

The project virtual environment and dependencies have been installed during development.
In PowerShell, from this project directory:

```powershell
.\.venv\Scripts\python.exe scripts\configure.py
.\scripts\run.ps1
```

The local wizard asks for your bot token, numeric Telegram **user** ID, Gemini
API key, and a model ID available to that key. Secret inputs are hidden. It
preserves existing environment values when you press Enter. Do not send API
keys to the Telegram bot. Your existing `.env` has not been modified by the build.
Use right-click to paste into hidden Windows prompts; Ctrl+V can insert a hidden
control character. Get your Telegram user ID from `@userinfobot` in Telegram.
It is an account ID such as `123456789`, not your phone number.

Send `/start` to your bot from the configured owner's private Telegram account.
Other users and groups are ignored. Access fails closed when OWNER_ID is absent.
`.env.example` lists settings. `SHIFT_TIMEZONE` defaults to Asia/Kolkata and
supersedes the old scheduler TIMEZONE setting. Existing DB_PATH is the legacy
JSON source; SQLITE_PATH configures the new database and must be a different path.

For a fresh installation, install Python 3.12, then:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

`requirements.lock.txt` records the dependency versions tested on Windows/Python 3.12.
`requirements.txt` expresses upgrade ranges. Re-test after upgrading.

## A typical shift

```text
/shift
/schedule 21:00 16:00
/task Test Linux web blocking | urgent | today | EmpMonitor | - | BUG-42 | Retest failures | linux,web
/task Follow up on deployment | high | tomorrow | EmpMonitor | Pace | REQ-7 | Request logs | deployment
/tod
/progress 1
/support Pace | Teams | investigated | Investigated installation | EmpMonitor | installation | Await logs | SUP-1 | 1 | pace-install
/testing Linux web blocking | failed | EmpMonitor | Rocky Linux | 4.2 | Two defects | BUG-42 | required
/learning Silent installation | KT | EmpMonitor | Use deployment flags | Practise tomorrow
/case Login issue | Pace | Teams | investigating | handled | EmpMonitor | Windows | SUP-8 | high | Check logs | client | tomorrow 10:00
/testsession 1 | Login retest | failed | Windows 11 | 4.3 | Login succeeds | Bad request | BUG-8 | yes | yes | Fresh install | Install and sign in
/followup 1 | tomorrow 10:00 | client | Request fresh logs
/pl
/done 1
/eod
/endshift
```

`/shift` records the actual start now and suggests nine hours later as the end.
`/shift 08:00 17:00`, `/shift 10:00 19:00`, and `/shift 12:00 21:00` are explicit
presets. `/default 12:00 21:00` sets the saved default; `/preset` starts it.
A default does not automatically start a shift or create reminders on days off.

Lunch is optional. `/schedule 19:00 14:00` changes end and lunch; use `none` to
remove lunch. Clock times after midnight belong to the same shift when appropriate.
Use an explicit start such as `/shift 2026-09-08T23:00 08:00` for earlier or overnight
work. Only one shift is active at once. `/endshift` drafts EOD and presents a close
button; scheduled end time alone does not close the shift.

Tasks carry forward without duplication. Every status change within a shift is
recorded. Old completions are not counted as current accomplishments. `/reopen ID`
clears the completion timestamp. `/delete ID` archives rather than erases a task.
Task priorities are low, normal, high, and urgent. Due dates accept `today`,
`tomorrow`, or `YYYY-MM-DD`. `/todo`, `/today`, and `/overdue` include inline actions.
`/taskdetails ID` shows metadata and `/edittask ID | field | value` changes one field.

## Logging and AI

Plain messages are saved verbatim as notes first. Tap **Organize with AI** or use
`/organize NOTE_ID` to preview proposed plans, support entries, testing, learning,
and notes. Only **Accept entries** applies the suggestion, transactionally. Original
notes remain in audit history. AI categorization never changes existing task status;
use `/done` or `/progress` explicitly. Edit a mistaken note before re-organizing it.
Suggestions display confidence and clarification questions. Use `/proposal ID`,
`/edititem PROPOSAL ITEM | field | value`, `/dropitem`, `/accept`, or `/dismiss` to
review them. Acceptance is transactional, preventing partially saved suggestions.

`/support client | channel | outcome | details` records one interaction. Outcomes
are resolved, investigated, escalated, pending, or assisted. Use `?` for unknown
clients. Keep the same client alias across channels for meaningful unique-client
counts. A resolved interaction is not necessarily one resolved query; exact query
counts cannot be reconstructed from vague or combined notes. The report deliberately
labels this measure **resolved interactions**. Unnamed clients make client totals incomplete.
Extended support fields record product, category, follow-up, ticket, query count, and
an issue key. Reusing an issue key prevents repeated updates about one issue from
being counted repeatedly in explicit resolved-query totals.

Testing records can store result, product, environment, build, defects, ticket, and
retest requirement. Learning records can store session type, product, takeaway, and
follow-up. Use `-` or `?` when optional information is unknown.

`/activity` lists IDs. `/editlog ID text` corrects details; `/undo ID` removes a mistaken
log from reports while retaining audit history. To correct a support client/channel/
outcome, undo that entry and log the corrected one. Task changes use task commands.

`/tod` (`/bos`), `/pl` (`/prelunch`), and `/eod` save template reports. `/ai REPORT_ID`
asks Gemini to polish one report, retaining the original. AI drafts require review:
model instructions cannot guarantee factual accuracy. `/editreport ID text` saves a
new version. `/history` and `/report ID` retrieve past reports. Finalized snapshots
are never overwritten by later task updates. Finalize does not send a report to HR
or other chats; copy it yourself.

Reports support `short`, `standard`, and `detailed`, for example `/eod detailed`.
`/reportstyle detailed` changes the default. `/privacy clients on` masks client names
in newly generated reports. `/section eod testing` rebuilds one current section.
`/summary` previews the current shift and `/week` summarizes the last seven days.
`/search`, `/client`, and `/findtesting` search locally. `/export csv` and
`/export markdown` send owner-only exports; spreadsheet-formula cells are escaped.

Only the chosen note or report is sent to Gemini, not the entire database or reference
file. Use client aliases if you prefer not to send names. The first provider adapter
is Gemini; other providers need an adapter implementing the writer interface. Set
AI_MODEL explicitly to avoid silently selecting an unavailable or undesired model.
`AI_FALLBACK_MODEL` defaults to `gemini-3.5-flash-lite` and is tried only when the
primary endpoint is retired, rate-limited, overloaded, or has a transient server
failure. Authentication, permission, and malformed-request errors fail immediately.
The application caps operations per local day with AI_DAILY_LIMIT (default 30),
including failed attempts. This is not a monetary budget. A fallback can produce two
provider calls for one operation when the primary fails. Timeouts preserve original
content and template reports remain usable offline.

## Cases, evidence, voice, and follow-ups

Cases connect fragmented support messages, calls, testing, handoffs, resolutions, and
client updates. `/case` creates one; `/cases` and `/caseinfo ID` show it. Use
`/caseevent`, `/casestatus`, and `/clientupdated` to build an evidence-backed timeline.
A fix is not treated as communicated until the client-update step is recorded.

`/testsession` captures environment, build, expected and actual behavior, defects,
retest state, and developer notification. Send a photo, document, or video with
`/attach CASE_ID TEST_ID | caption` as its Telegram caption. Evidence is stored under
`storage/evidence`; media files are pruned after `MEDIA_RETENTION_DAYS`, while their
audit metadata remains. Voice notes are transcribed with the configured Gemini model,
saved as reviewable notes, and deleted after transcription by default. Set
`DELETE_VOICE_AFTER_TRANSCRIPTION=false` to retain local audio.

`/followup CASE_ID | tomorrow 10:00 | client | Request fresh logs` schedules a case
follow-up. `/followups`, `/followupdone`, and `/snooze` manage it. Near shift end the
bot gives one combined reminder for open cases; reminder timing follows the current
day's flexible shift.

## Telegram history and review inbox

Analyze Telegram Desktop HTML exports without changing the database:

```powershell
.\scripts\import_telegram.ps1 'C:\path\to\ChatExport'
```

Add `-Apply` to import. The importer reconstructs joined messages and replies, masks
emails, phone numbers, links, mentions, tokens, and passwords, fingerprints messages,
and makes repeat imports idempotent. Owner-attributed work enters `/inbox`; other group
activity is retained as ignored context and never counts as personal work. Use
`/acceptmsg MESSAGE_ID [CASE_ID]` or `/ignoremsg MESSAGE_ID`. Inspect batches with
`/imports`; `/rollbackimport ID` removes an unaccepted batch without changing its source
files. Historical events keep their original timestamps and do not enter the
current shift unless they were forwarded live to the private bot.

## Local dashboard and connectors

The bot starts a token-protected dashboard on `127.0.0.1:8765`. Send `/dashboard` to
receive the private local link. It shows cases, the review inbox, test sessions, and
follow-ups, and supports review and status changes. It is not exposed to the LAN or
internet. `scripts\launcher.ps1` opens a small Windows control panel for the dashboard,
scheduled task, and backups.

Freshdesk and Freshchat connectors are read-only and only import records assigned to
the configured agent ID. Run `scripts\configure_connectors.py`, then `/sync freshdesk`
or `/sync freshchat`. CSV is available through `scripts\sync_connector.py csv
--csv-path PATH`. Teams and WhatsApp messages can be forwarded to the private bot until
an organization-approved API connection is available. Set `ENABLED_CONNECTORS` to a
comma-separated list such as `freshdesk,freshchat` to sync configured connectors every
`CONNECTOR_SYNC_MINUTES`; all results still require review.

New secrets use Windows Credential Manager when the interactive Windows session permits
it and fall back to the local ignored `.env` file otherwise. Existing `.env` secrets can
be moved with `scripts\migrate_secrets.py`; non-secret settings remain in `.env`.

## Windows autostart and recovery

After local configuration and one successful manual run, stop that manual run and:

```powershell
.\scripts\run.ps1 -InstallAutostart
Start-ScheduledTask -TaskName 'Telegram Work Assistant'
```

The task runs as your signed-in Windows user, starts at login, allows battery power,
and attempts up to ten restarts at one-minute intervals. A database-specific OS lock
prevents running two copies against the same database. The task has not been
registered automatically by the build.

The PC must be awake, signed in, and online. Sleep/offline periods pause service.
On reconnect, one reminder combines due TOD/lunch/EOD prompts; delivered prompts
are remembered across restarts. Reminders do not require pending tasks. A successful
manual report suppresses its corresponding reminder. Missed Telegram updates are
subject to Telegram retention; the bot cannot promise recovery of arbitrarily old
messages. You can log missing work manually in an active shift.

Telegram delivery and local SQLite cannot be one atomic transaction: a crash just
after sending a reminder can cause one duplicate. Incoming update IDs are claimed
before processing to prevent repeated mutations; a crash in that narrow interval
can require checking `/activity` and resending the operation as a new message.

Stop the background bot with:

```powershell
Stop-ScheduledTask -TaskName 'Telegram Work Assistant'
```

## Storage, migration, backup, restore

On the first configured startup, legacy JSON tasks are validated and copied to a
separate timestamped `.bak` before importing into SQLite. The JSON stays unchanged;
legacy reminder chat IDs are not trusted or imported. Invalid JSON stops migration
instead of replacing data with an empty database. An already populated new database
is not silently merged. Legacy tasks have no fabricated shift history.

Startup backups, six-hour automatic backups, and `/backup` use SQLite's backup API
under `storage/backups`. `BACKUP_RETENTION` defaults to the newest 30 local backups.
Copy important backups to another drive; local backups do not protect against disk
failure. Backups are retained until you manage them; they may contain private work.

Stop the bot before restoring:

```powershell
.\scripts\restore.ps1 -BackupPath 'C:\path\to\backup.sqlite3'
```

Restore checks SQLite integrity and required tables, locks out the running bot,
and backs up the current database before replacement. Restart after restore.
Source control ignores secrets, virtual environments, new databases, logs, and backups.
The original `storage/tasks.json` was already tracked; migration leaves it alone.
Do not publish that legacy file if it contains private work data.

## Verification and current scope

```powershell
.\.venv\Scripts\python.exe smoke_test.py
.\.venv\Scripts\python.exe -m pip check
```

Tests use temporary storage and mocked Telegram/AI calls. They cover authorization,
concurrent writes, migration, backup/restore, overnight scheduling, correction audit,
report scope and counts, duplicate updates, reminder recovery, AI failure, proposal
acceptance/editing, rich records, search, exports, backup retention, and long Unicode
messages. `/health` reports schema, database integrity, active shift, background jobs,
backups, AI configuration, and AI event totals without exposing secrets.

Microsoft Teams and WhatsApp direct connectors, automated monthly rotations, OCR, and
additional AI provider adapters remain future extensions. Legacy `bot/` and
`utils/` placeholder folders are not imported; the supported entry point is `bot.py`
(also available through `main.py`).
