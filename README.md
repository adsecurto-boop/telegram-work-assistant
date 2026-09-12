# Personal Work Assistant

A private, context-aware, persistent assistant for Windows that understands your work, remembers relevant dialogue, safely performs structured operations, and helps you throughout your working day.

Telegram is one interface to the assistant. The localhost dashboard is another. Voice, scheduler, connectors, and AI reasoning all connect to the same unified assistant core.

```text
                     PERSONAL WORK ASSISTANT
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
      Telegram            Dashboard             Voice
          │                   │                   │
          └───────────────────┼───────────────────┘
                              │
                    Assistant Core Engine
                              │
       ┌──────────────┬───────┼────────┬──────────────┐
       │              │       │        │              │
     Tasks          Cases  Testing  Follow-ups    Reports
       │              │       │        │              │
       └──────────────┴───────┼────────┴──────────────┘
                              │
                     Memory & Intelligence
        (Conversation Turns · Rolling Summary · Gemini Planning)
                              │
                        SQLite + Audit
                              │
                  Durable Personal Work Memory
```

---

## Primary Product Motto

> **This is my Personal Work Assistant at work.**

- **Truthfulness**: The assistant never falsely claims an action succeeded, a report is fresh, or an operation is undoable when reality differs.
- **Data Integrity**: Every state mutation is deterministic, validated, atomic, audited, and recoverable where applicable.
- **Durable Dialogue Memory**: Conversation turns (`conversation_turns`) persist across restarts and provide contextual pronoun resolution across dialogue turns.
- **AI as Brain, Python as Hands**: Gemini proposes structured multi-action plans; Python validates policies, verifies context, and executes transactions in SQLite.

---

## Feature Matrix

| Feature Area | Status | Notes / Pre-requisites |
|---|---|---|
| **Durable Dialogue Memory** | **Implemented & Tested** | Multi-turn conversational memory (`conversation_turns`), rolling daily summaries, and restart-resilient pronoun resolution. |
| **Multi-Action Planning** | **Implemented & Tested** | `ConversationPlan` with atomic transactions, dependency propagation, confirmation gates, and audit-backed rollback. |
| **Shift Management & Overrides** | **Implemented & Tested** | Daily shifts (08:00–17:00, 10:00–19:00, 12:00–21:00, custom). Future shifts stored in `shift_calendar` without activating today. Strict time validation (`0am`/`0pm` and invalid bounds rejected). |
| **Conversational Daily Planning** | **Implemented & Tested** | Persistent multi-turn wizard (`planning_conversations`), similar-task duplicate review, confirmed versioned baseline snapshots (`plan_snapshots`), and TOD draft generation. |
| **Task-to-Request Linking** | **Implemented & Tested** | Explicit many-to-many relationships (`record_links`) linking tasks to work drafts and cases with independent lifecycles. |
| **Work Time vs. Logged Time** | **Implemented & Tested** | Late entries with `occurred_at` and `time_precision`, overnight shift interval matching, and finalized report immutability. |
| **Shift Templates & Rotational Schedule** | **Implemented & Tested** | Named templates (`Morning`, `General`, `Evening`). Date-range assignments enforce `active_weekdays`. Daily overrides take priority. |
| **Task Lifecycle & Tracking** | **Implemented & Tested** | Create, update, complete, reopen, carry forward. Scoped to shift/date. |
| **Case Management & Evidence** | **Implemented & Tested** | 11 canonical statuses (`new`, `triaged`, `investigating`, `waiting_client`, `waiting_internal`, `fix_ready`, `testing`, `retest_required`, `resolved`, `client_updated`, `closed`). Attached media with NULL-safe SHA-256 deduplication and relational verification. |
| **Test Sessions & Learning Records** | **Implemented & Tested** | Captures scenarios, builds, environments, defect IDs, retest states, and learning takeaways. Cross-case test linking strictly prevented. |
| **Report Generation & Finalization Guard** | **Implemented & Tested** | Shift-scoped TOD, Pre-Lunch (PL), and EOD reports. Live facts hash re-verified at finalization time; stale reports rejected until refreshed. |
| **Natural Language Interaction** | **Implemented & Tested** | 27+ intents supported via deterministic rules and shared domain services. Bounded context builder for Gemini reasoning. |
| **Confidence Policy & Proposals** | **Implemented & Tested** | High confidence: direct execution. Medium confidence: stored proposal in `nl_proposals` with 1-click confirmation or interactive choices. Low confidence: zero mutations. |
| **Atomic & Compound Undo** | **Implemented & Tested** | `/undo`, Telegram undo buttons, natural-language "undo", dashboard audit undo. Correlation-based multi-record atomic rollback with tamper detection. Undo button is never displayed without a verifiable audit entry. |
| **Historical Clustering** | **Implemented & Tested** | CLI & Dashboard. SHA-256 fingerprint (`cluster-v1:...`) prevents duplicate suggestions. Supports Accept, Reject, Split, Merge, Attach to case. |
| **Bulk Review Inbox** | **Implemented & Tested** | 12 filters in web dashboard. Two-phase preview and confirm with one-time tokens, atomic execution, and single-correlation undo. |
| **Localhost Web Dashboard** | **Implemented & Tested** | Bound strictly to `127.0.0.1`. Thread-safe session management, cookie auth with instant URL history cleaning, CSRF tokens, strict CSP (`default-src 'none'; sandbox`), safe inline media allowlist, and chunked streaming. |
| **Gemini Copilot Tools** | **Requires Credentials** | Optional `/casesummary`, `/nextaction`, `/draftclient`, `/draftescalation`, `/analyzetest`, and report polishing. Works with fallback deterministic text when offline. Bounded, field-allowlisted context payloads. |
| **Voice Transcription & Single-Logging** | **Requires Credentials** | Voice messages transcribed via Gemini and routed cleanly to semantic NLP execution without phantom duplicate notes. Captured locally when offline. |
| **Read-Only Connectors** | **Requires Credentials** | Freshdesk and Freshchat pollers with HTTPS enforcement, pagination, and untrusted payload tagging (`author_is_owner=False`, `trusted=False`). CSV sync via CLI. |
| **MCP External Tools** | **Optional / Credential Dependent** | Official MCP Python SDK v2 client, paginated discovery, long-lived stdio and Streamable HTTP sessions, live health, conservative policy classification, and exact persisted-call confirmation. GitHub is runtime-wired but disabled by default. |
| **Multi-User / Public Cloud** | **Planned or Unavailable** | Designed exclusively as a private, single-owner assistant on a local Windows PC. Access is restricted to `OWNER_ID` in private chats. |

---

## Setup & Runtime on Windows

### Prerequisites
- Windows 10/11 64-bit
- Python 3.12 (e.g. 3.12.9)
- Telegram account with a Bot token from `@BotFather`
- (Optional) Google Gemini API Key from Google AI Studio

### Configuration
Secrets stay in `.env` (or Windows Credential Manager). Copy `.env.example` to `.env`:

```powershell
Copy-Item .env.example .env
```

Key environment variables:
- `BOT_TOKEN`: Telegram bot token from `@BotFather`.
- `OWNER_ID`: Numeric Telegram user ID (from `@userinfobot`). Not your phone number.
- `SHIFT_TIMEZONE`: Timezone for shift scheduling (e.g. `Asia/Kolkata`).
- `SQLITE_PATH`: Path to SQLite database (default: `storage/work.sqlite3`).
- `GEMINI_API_KEY`: Google Gemini API key.
- `AI_MODEL`: Primary Gemini model identifier (the current installation uses `gemini-3.6-flash`).
- `AI_FALLBACK_MODEL`: Fallback Gemini model (default: `gemini-3.5-flash-lite`).
- `AI_DAILY_LIMIT`: Maximum daily Gemini calls (default: `30`).
- `DASHBOARD_PORT`: Localhost port for dashboard (default: `8765`).

### MCP and GitHub

`mcp_config.json` selects each server's transport explicitly. Supported values are
`stdio` and `streamable_http`; unknown values stop configuration loading. Disabled
or unavailable integrations do not prevent the bot or local commands from starting.

The supplied GitHub entry uses GitHub's official hosted MCP endpoint in server-side
read-only mode and is disabled by default. To enable it, set `GITHUB_TOKEN` in the
environment and change only `enabled` to `true`. The token is expanded into an HTTP
authorization header at runtime and is never stored in JSON or sent to Gemini.
For a local installation, use GitHub's official `ghcr.io/github/github-mcp-server`
Docker image or official binary with `transport: "stdio"`; do not use the repository
name as an npm package. Keep `GITHUB_READ_ONLY=1` and limit toolsets for read-first use.

Tool annotations are recorded as hints and are trusted only when the operator opts in
and the configured endpoint exactly matches a pinned identity. Destructive and known
write semantics always take priority. Unknown external operations require Telegram
confirmation. Confirming atomically claims the persisted proposal, revalidates the
tool/schema/server/risk, and executes those exact stored arguments once.

Verification is deliberately separated: `scripts/check_live_gemini_mcp.py` performs
an opt-in live Gemini round trip against a local safe MCP server, while
`scripts/check_live_github_mcp.py` performs one harmless `get_me` read against the
official GitHub endpoint. Both skip with exit code 2 when their credential is absent.
The automated suite proves local mock MCP compatibility; it does not claim live GitHub
verification. Gmail, Google Calendar, and Google Drive MCP integrations are **not
implemented / future integrations** unless the operator adds and validates servers.

The adapter constructs the generic SDK function-result turn with `role="tool"` and
preserves every call ID. The Gemini 3.6 GenerateContent endpoint currently rejects
that generic role and requires the documented legacy-compatible `role="user"`; the
adapter retries only that explicit 400 compatibility response and reports the role
used in the live smoke diagnostics.

Run the interactive setup wizard:
```powershell
.\.venv\Scripts\python.exe scripts\configure.py
```

### Windows Scheduled Task Management
The bot runs automatically in the background on your Windows PC via Windows Task Scheduler.

- **Task Name**: `Telegram Work Assistant`
- **Target Executable**: `C:\Projects\TELEBOT\telegram-work-assistant\.venv\Scripts\pythonw.exe`
- **Start the Task**:
  ```powershell
  Start-ScheduledTask -TaskName 'Telegram Work Assistant'
  ```
- **Stop the Task**:
  ```powershell
  Stop-ScheduledTask -TaskName 'Telegram Work Assistant'
  ```
- **Check Task Status**:
  ```powershell
  Get-ScheduledTask -TaskName 'Telegram Work Assistant'
  ```

---

## Natural-Language Interface

You can interact with the bot in plain conversational English. The system uses a fast deterministic parser backed by a structured Gemini interpreter when ambiguous.

### Conversational Examples
- **Shifts**:
  - `"My shift today is 10 to 7 and I'll take lunch around 2"`
  - `"Tomorrow I'm working 8 to 5"` (schedules tomorrow in `shift_calendar` without activating today)
  - `"Friday is a day off"` (creates a day-off override)
  - `"Lunch today is at 3"`
- **Tasks**:
  - `"Today I need to test idle time and follow up with Rahul"` (creates two tasks)
  - `"Mark that task as done"`
  - `"Move the remaining task to tomorrow"`
- **Cases & Support**:
  - `"Create a case for Acme's attendance issue"`
  - `"Mark that case waiting for client"`
  - `"The client confirmed it is working"`
  - `"I shared the logs with the development team"`
- **Testing & Learning**:
  - `"Checked it on Windows 11 and reproduced the issue"`
  - `"I learned how idle-time calculation works"`
- **Follow-ups**:
  - `"Remind me tomorrow at 11 to ask Rahul for fresh logs"`
  - `"Complete the follow-up"`
  - `"Snooze the follow-up to 4pm"`
- **Reports & Queries**:
  - `"Generate my TOD."`
  - `"Prepare my lunch update."`
  - `"Generate my EOD in a professional format."`
  - `"What is still pending today?"`
  - `"Show my cases."`
- **Copilot Tools**:
  - `"Summarize that case."`
  - `"Draft a reply asking for fresh logs."`
  - `"Analyze the last test."`
- **Undo**:
  - `"Undo"` or `"Undo last action"`

### Confidence Policy & Proposals
Active-shift timing corrections always require **Confirm** or **Cancel**, even when the wording is clear. For example, "My shift started at 10 am and ends at 7 pm" shows the current and proposed times before changing the existing shift. Confirming keeps logged work attached to the same shift; `/undo` restores the previous times. If the shift changes or closes before confirmation, send a fresh request.

You can combine today's shift hours with an EOD reminder: "Today my shift is from 10 am to 8 pm, remind me for EOD at 7 pm." This sets a shift reminder without creating a case or case follow-up. Custom EOD reminders must fall within the shift; future-calendar EOD reminders are not yet supported. Reminders already delivered for that shift are not sent again after a timing correction.

1. **High Confidence (>= 0.80)**: Low-risk operations execute immediately with an audit record and a 1-click `/undo` button.
2. **Medium Confidence (0.60 – 0.79)**: A proposal is stored in `nl_proposals`. The bot explains the proposed action and presents **Confirm** / **Cancel** or choice buttons. The target record is **not mutated** until you tap Confirm. Proposals expire after 15 minutes, are bound to your user ID, and cannot be replayed.
3. **Low Confidence (< 0.60)**: No mutations occur. The bot asks for clarification or shows likely options.

---

## Shift Planning & Templates

The assistant accommodates rotational schedules with strict time validation:
- **Valid Times**: Hours 0–23, minutes 0–59. Invalid tokens like `99:80` or `25:00` are strictly rejected.
- **Cross-Midnight**: Shifts crossing midnight (e.g. 22:00 to 07:00) are recognized as a continuous workday.
- **Configured Templates**:
  - `Morning`: 08:00–17:00 (Lunch: 12:30)
  - `General`: 10:00–19:00 (Lunch: 14:00)
  - `Evening`: 12:00–21:00 (Lunch: 16:00)
- **Rotational Range Assignment**: Assign templates across date ranges (e.g. "Use 12 to 9 for the rest of September"). Requested hours override template hours while the template still controls eligible weekdays.
- **Explicit Overrides**: Explicit single-day schedules or day-off markers always override template assignments.
- **Future Shifts**: Scheduling a shift for tomorrow or a future date writes an entry into `shift_calendar` and does **not** open an active shift today.
- **Calendar Preview**: Use `/shiftcalendar` to view the next 7 days and see any unassigned dates.

---

## Safe Transactional Undo & Auditing

Every mutation that advertises undo produces an atomic audit correlation:
- **Reversible Operations**: Task creation, task completion, shift creation, lunch updates, case creation, case status changes, case events, test sessions, follow-ups, shift calendar overrides, bulk review actions, and cluster acceptance.
- **Compound Undo**: Actions that create multiple records (e.g. creating two tasks, creating a test session with a linked case event, or accepting messages as tasks) share a single `correlation_id`. Undoing via `/undo`, a Telegram button, or the dashboard reverts **all records in the correlation batch atomically**.
- **Tamper Protection**: Every audit record stores before-state and after-state JSON snapshots. If a record has been modified by a subsequent action, undo is blocked and rolled back entirely.
- **No False Undo**: The bot never displays `"Undo: /undo"` unless a reversible audit record was created.

---

## Historical Clustering & Bulk Review

### Historical Message Clustering
Clusters historical messages into coherent issue groups based on Telegram reply chains, ticket identifiers, and temporal proximity.
- **Dry-Run Analysis**:
  ```powershell
  .\.venv\Scripts\python.exe scripts\cluster_history.py --dry-run
  ```
- **Apply Suggestions**:
  ```powershell
  .\.venv\Scripts\python.exe scripts\cluster_history.py --apply-suggestions
  ```
- **Idempotency**: Suggestions use a SHA-256 fingerprint (`cluster-v1:<hash>`) based on sorted source message IDs and the algorithm version. Repeated execution against unchanged messages produces zero duplicate clusters and leaves existing clusters intact.
- **Cluster Management**: In Telegram via `/clusters` or on the dashboard **Clusters** tab, you can **Accept as Case**, **Reject**, **Split**, **Merge**, or **Attach to Existing Case**. Clusters are never accepted automatically.

### Bulk Inbox Review
The web dashboard Review tab supports high-throughput review with 12 filters:
- Filter by date range, import source, client, product, classification, confidence, cluster, review status, media presence, ticket presence, owner authored, and text query.
- **Two-Phase Preview & Confirm**: Selecting messages and clicking an action (e.g. Accept as Tasks, Attach as Case Events, Assign Client) displays a **Preview Screen** with the affected record count and requires an explicit confirmation click backed by a one-time token. Direct mutations without preview are disallowed.
- **Atomic Rollback**: Bulk operations run in a single transaction with a single correlation ID. Undoing a bulk action restores all source messages and removes all generated items.

---

## Automated Report Validation

When generating Beginning of Day (`/tod`), Pre-Lunch (`/pl`), or End of Day (`/eod`) reports, `ReportValidator` inspects the text against database records:
- **Shift Scoping**: Validates strictly against records within the report's shift window or date.
- **Factual Discrepancies Caught**:
  - Unsupported unique-client count vs structured support records.
  - Duplicate client entries in client lists.
  - Completed tasks reported as pending, or pending tasks reported as completed.
  - Omitted open high-priority cases.
  - Overdue follow-ups omitted.
  - Claims of testing or resolution without supporting records or case events.
  - Pass/Fail conflicts between report claims and test session outcomes.
  - Hallucinated ticket numbers or client names not found in reference data.
- **Validation Storage**: Validation results, verified metrics, and warnings are saved in `report_validations` and linked via `report_provenance`.

---

## AI Privacy, Reliability & Quota Management

- **Strict Redaction**: `AIPayloadBuilder` strips bearer tokens, API keys, passwords, email addresses, and phone numbers before text is sent to Gemini.
- **Allowlists & Outbound Views**: Case summaries and client reply drafts use an explicit field allowlist. Internal notes and private events are excluded from client draft payloads.
- **Client Name Masking**: When `mask_client_names` is enabled, client identifiers are masked as `Client {id}` across report and copilot requests. Task, test, follow-up, case, and event strings are redacted before external AI calls.
- **Unified Metering**: Natural-language interpretation, `/understand`, reports, transcription, organization, and copilot features share the configured daily quota and record success/failure events.
- **Untrusted Data Boundary**: All imported content and notes are wrapped in `<untrusted_data>` delimiters with strict system instructions that untrusted data must never be treated as system commands.
- **Quota Accounting**: Every AI call (NLP interpretation, case summaries, drafts, report polishes, transcription) logs metadata in `ai_usage` and `ai_events`. Prompts and client message contents are **never** logged.
- **Bounded Exponential Backoff**: Retries up to 2 times with exponential delay for transient errors (429 rate limit, 503 service unavailable, timeouts). Authentication (401) and permission (403) errors fail immediately without retry.
- **Offline Functionality**: The bot functions fully without Gemini. If AI is disabled or offline, deterministic templates and rules provide complete operational capability.

---

## Localhost Web Dashboard

Access the private dashboard in your browser:
- Bound strictly to loopback: `http://127.0.0.1:8765/?token=...`
- **Authentication**: The dashboard access token is exchanged for an HTTP-only local session cookie (`dashboard_session`) that expires after eight hours.
- **CSRF Protection**: Every state-changing form includes a per-session CSRF token. Unauthenticated requests or missing CSRF tokens return HTTP 403.
- **Bulk Confirmation**: Preview tokens expire after ten minutes, are single-use, and are bound to the session that created them.
- **Secure Headers**: Responses include `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and `SameSite=Lax`.
- **Pages**:
  - **Overview**: Today's active shift status, open cases, inbox count, and follow-ups.
  - **Kanban Board**: Column-based workflow (`New`, `Investigating`, `Waiting Client`, `Waiting Internal`, `Testing`, `Resolved`, `Closed`) with status movement buttons.
  - **Review Inbox**: Filterable multi-select triage with two-phase preview and confirm.
  - **Clusters**: View historical cluster suggestions with confidence scores, message links, and 1-click case creation.
  - **Shifts & Calendar**: 7-day rotational schedule preview, template range assigner, and daily override editor.
  - **Reports**: View drafts, provenance links, and structured validation warnings.
  - **Audit Log**: Complete history of database mutations with 1-click safe undo.
  - **Health**: Real-time database integrity, backup count, AI quota usage, and connector health.

---

## Backup, Restore & Migration

- **Automated Backups**: Backups are created automatically every 6 hours and before any database schema migration using SQLite's online backup API in `storage/backups/`.
- **Manual Backup**:
  ```powershell
  .\.venv\Scripts\python.exe scripts\backup.py
  ```
- **Integrity Verification**:
  Every backup and migration step is verified using `PRAGMA integrity_check`.
- **Transactional Migration**:
  The migration runner executes schema updates statement-by-statement inside an explicit immediate transaction (`BEGIN IMMEDIATE`), updating `user_version` only after all tables, columns, indexes, and foreign keys pass validation. Injected failures automatically roll back to the clean previous state.
- **Restore from Backup**:
  ```powershell
  Stop-ScheduledTask -TaskName 'Telegram Work Assistant'
  .\.venv\Scripts\python.exe scripts\restore.py storage\backups\<backup_file>.sqlite3
  Start-ScheduledTask -TaskName 'Telegram Work Assistant'
  ```

---

## Virtual Environment Recovery Procedure

If the Python environment is damaged (e.g. `.venv\pyvenv.cfg` points to a missing runtime):
1. Locate a compatible Python 3.12 installation:
   ```powershell
   py -3.12 --version
   ```
2. Archive the broken environment to a timestamped backup:
   ```powershell
   Rename-Item .venv ".venv-broken-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
   ```
3. Create a fresh virtual environment:
   ```powershell
   py -3.12 -m venv .venv
   ```
4. Install locked dependencies:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
   ```
5. Verify package integrity:
   ```powershell
   .\.venv\Scripts\python.exe -m pip check
   ```
6. Verify Task Scheduler uses the repaired path (`.venv\Scripts\pythonw.exe`).

---

## Adaptive Natural-Language Recognition

The adaptive NLP pipeline keeps the original message and a conservatively normalized copy. It protects emails, URLs, ticket IDs, versions, and quoted technical text while normalizing common date, punctuation, and time-range variations. Deterministic rules run first; the structured Gemini interpreter is an optional fallback and is unavailable-safe.

Recognition confidence and permission to execute are separate. The action policy uses reason codes such as `uncertain_wording`, `invalid_time`, `ambiguous_reference`, and `missing_required_entity`. Read-only questions never mutate data, negated or invalid requests are rejected, uncertain changes require confirmation, and multiple task or case matches are shown as choices.

Useful owner-only commands:

```text
/understand TEXT
/unknowns [LIMIT]
/correct ID INTENT [field=value ...]
/nlstats [DAYS]
```

`/understand` is always read-only and shows the normal-message action policy. `/correct` records a parser correction for future examples and never executes the historical request.

Examples:

```text
/correct 42 set_shift date=2026-09-11 shift_start=12:00 shift_end=21:00
/correct 57 complete_task reference=#3 notes="owner correction"
```

Gemini receives redacted structured context and a maximum of five redacted owner-approved corrections. Its JSON is Pydantic-validated, and its reported confidence never bypasses local entity validation or the action policy.

Run the offline evaluation corpus without Telegram or Gemini calls:

```powershell
.venv\Scripts\python.exe scripts\evaluate_nlp.py
```

---

## Verification & Automated Testing

The complete test suite runs against temporary databases using mocked Telegram and Gemini interfaces:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite includes more than 353 unit and integration tests covering:
- **Durable Dialogue Memory & Pronoun Resolution**: `conversation_turns` persistence across restarts, context building, rolling summaries, and unambiguous pronoun resolution (`tests/test_assistant_correctness.py`).
- **Multi-Action Planning**: `ConversationPlan` with atomic transactions, dependency propagation, confirmation requirements for high-risk actions, and full rollback on partial failure (`tests/test_mission_hardening.py`).
- **Stale Report Finalization Guard**: Live facts hash recomputation at finalization time, rejection of stale reports, and shift close validation (`tests/test_assistant_correctness.py`).
- **Undo Integrity & Verification**: Real audit verification; Undo buttons derived solely from verifiable mutation results (`tests/test_mission_hardening.py`).
- **Dashboard & Evidence Security**: XSS protection (forced download of HTML, SVG, XML, executables; safe inline allowlist for images/audio/video), CSP sandbox, thread-safe sessions, and NULL-safe evidence deduplication (`tests/test_mission_hardening.py`).
- **Connector Security**: HTTPS enforcement, pagination, and untrusted payload tagging (`tests/test_mission_hardening.py`).
- **Telegram Handlers**: Plain natural language, voice routing without phantom notes, `/understand`, `/undo`, and callbacks.
- **Undo & Cascades**: Multi-task correlation undo, compound test sessions, standalone follow-ups, cluster acceptance undo, and atomic rollback on tamper.
- **Shift Engine**: Schedule validation, invalid time rejection (`99:80`, `25:00`, `10:99`, `0am`, `0pm`, `10am to 10am`), future shifts, day-off overrides, template range weekday enforcement, and cross-midnight shifts.
- **Natural Language Dispatch**: Conversational TOD, PL, EOD generation, Copilot dispatch, medium-confidence proposals, and low-confidence no-mutation safety.
- **Dashboard Security**: Authenticated routes, unauthenticated 403 rejection, CSRF rejection, cluster acceptance POST, and bulk preview/confirm.
- **Clustering**: SHA-256 fingerprint idempotency, duplicate prevention, and repeated suggestion replay.
- **Report Validation**: Shift scoping, duplicate client warnings, hallucinated ticket/client detection, and task status consistency.
- **AI Privacy & Reliability**: Sensitive data redaction, client masking, untrusted data boundaries, quota tracking, exponential backoff, and AI-disabled fallbacks.
- **Database Migrations**: Transactional v4-to-v5 migration and automatic rollback on injected failure.
- **Shift Corrections**: Telegram confirmation/cancellation, stale and repeated confirmations, preserved work, undo, overnight times, custom EOD reminders, and backup-first v5-to-v6 migration.

---

## Known Limitations

1. **Local Host Only**: The bot and dashboard are designed for a private Windows PC without an always-on public server. If the PC is in sleep mode or powered off, processing is paused until wake.
2. **Owner-Only Security**: Only messages from `OWNER_ID` in private chats are processed. Group chats and non-owner interactions are intentionally ignored.
3. **Read-Only External Connectors**: Freshdesk and Freshchat connectors are strictly read-only and import data into the review inbox. The bot never sends outbound messages to external clients.
4. **Voice Transcription Requires Gemini**: When offline or without API credentials, voice messages are captured and preserved in storage as reviewable audio items rather than transcribed.
