# Daily Workflow, Conversational Planning & Task Tracking

The daily workflow unifies flexible shifts, conversational planning, task-to-request relationships, factual corrections, work-time precision, and verifiable TOD/lunch/EOD reporting. All states persist in SQLite (Schema Version 9) and survive process restarts.

---

## 1. Conversational Daily Planning (Stage A)

Start your day either in a single message or conversationally across multiple turns:

```text
Start my day. Shift is 12 to 9, lunch at 4. Need to test attendance and follow up on the NVIZION request.
```

### Conversational Wizard
If any parameters are omitted, the assistant prompts step-by-step:
1. **Shift hours:** "What are your shift hours today? (e.g. 12 to 9 or 10am to 7pm)"
2. **Lunch break:** "When is your lunch break scheduled? (e.g. at 4pm) or say 'no lunch'"
3. **Plan preview:** Presents shift times, lunch time, existing/similar task matches, and open carried tasks with inline **Confirm Plan** and **Cancel** buttons.

### Persistence & Concurrency Rules
- Survives application and Windows restarts; state is stored in `planning_conversations`.
- Unrelated slash commands (such as `/status`, `/todo`) remain fully usable during active planning.
- `/cancel` or "cancel planning" exits the session cleanly.
- Replayed Telegram updates and double-clicks on confirmation buttons are idempotent (`Planning session is already completed`).
- Stale confirmation buttons on cancelled sessions cannot overwrite or start a shift.
- Normalized exact titles are reused without duplication; similar titles are highlighted as `[Review similar #ID: 'Title']` for explicit review.
- **Atomic Confirmation:** Shift creation/revision, task creation, association of existing tasks with the active shift, baseline snapshot generation, settings, and conversation completion execute in a single atomic SQLite transaction (`confirm_daily_plan_atomic`). Interruptions cannot leave partial or duplicate state.
- Existing tasks included in the plan have their `planned_shift_id` updated to the active shift, guaranteeing they appear in shift task queries and TOD outputs.
- Confirmation locks in a versioned snapshot in `plan_snapshots` containing both task IDs and frozen titles, preserving the immutable baseline for the day.

---

## 2. Linking Tasks to Work Requests (Stage B)

Explicitly connect tasks to client work drafts or cases without false conflations:

```text
/linktask 4 draft=2
/unlinktask 4 draft=2
link task #4 to draft #2
connect task #5 to case #12
unlink task #4 from draft #2
```

### Linking Rules
- **Explicit relationships:** Links are stored in `record_links` between `(task, task_id)` and `(work_draft, draft_id)` or `(work_case, case_id)`.
- **Independent lifecycles:** Completing a linked task does **not** close or mark the request shared or completed.
- **Provenance display:** Linked work is visible in `/todo`, `/tomorrow`, `/checkpoint`, and work draft summaries.
- **Historical retention:** Task deletions retain historical audit logs and detach links safely; Undo restores them transactionally.

---

## 3. Natural Task Updates & Factual Corrections (Stage C)

Update task progress using natural phrasing:

```text
started attendance testing
finished attendance testing
attendance testing is blocked because the staging build is failing
move the retest to tomorrow
```

### Reference Resolution & Disambiguation Hierarchy
1. Explicit record ID (e.g. `task #4` or `#4`)
2. Reply to a specific task message or prompt
3. Currently selected active task in conversation context
4. Unique match among active tasks
5. **Interactive Disambiguation:** If multiple candidates match (e.g. two active tasks matching "move the retest to tomorrow"), the bot prompts with interactive buttons to select the exact task. It **never** falls back to an arbitrary recent task.
6. **No Invented Clients:** Phrases like "that was for the other client" check known clients across the shift and database. If ambiguous or unknown, the bot presents choices or asks for the client name; it never defaults to hardcoded client names.

### Factual Corrections with Stale-State Protection
Factual corrections (such as test failures or client reassignments) propose explicit before/after diffs:
```text
Actually, it failed on Windows 11
That was for client Acme
```
- **Proposal generation:** Stored in `nl_proposals` with diff display.
- **Explicit confirmation:** Requires pressing **Confirm Correction** before any database mutation occurs.
- **Stale-State Protection:** Confirmation compares the proposal's stored `before` state against the task's current live state. If the task was modified after the proposal was generated, the proposal is rejected with an explanatory conflict notice.
- **Atomic Undo:** Full audit trail recorded; `/undo` reverts back to previous status and blocker.

---

## 4. Work Time vs. Logged Time (Stage D)

Support late-logged work without misrepresenting historical accuracy:

```text
Yesterday at 11 pm I tested the new build.
I handled Acme Corp before lunch.
This happened during my previous shift: Emergency firewall patch.
```

### Rules & Storage
- **Automatic Categorization:** Keywords in the detail automatically classify late work into `testing`, `learning`, `support`, or `task` (e.g. "I tested..." is categorized as `testing`, never misclassified as `support`).
- **Shift Boundary Matching:** The occurrence timestamp is matched against past shift boundaries (`start <= occurred_at <= end/closed_at`). Work is assigned to the shift that actually covered that time window, preserving accurate historical metrics.
- Stored with `occurred_at` (timezone-aware ISO datetime) and `time_precision` (`exact`, `approximate`, `unknown`).
- **Finalized shift immutability:** Late entries for a closed shift log the activity against that shift and prompt the user to generate an explicit new report revision (`/eod revision`). Finalized reports are never silently overwritten.

---

## 5. TOD & Checkpoint Workflow (Stage E)

### Beginning of Day (TOD)
- Generates the draft representing the confirmed baseline snapshot from morning planning.

### Mid-Day Checkpoint (`/checkpoint` or natural lunch report)
Compares current shift progress against the frozen start-of-day baseline:
- **Completed:** Baseline tasks completed so far
- **In Progress:** Baseline tasks currently underway
- **Blocked:** Tasks impeded with recorded blocker reasons
- **Remaining:** Unplanned / remaining tasks
- **Unplanned work added:** Tasks created after morning planning
- **Stale Button Protection:** Checkpoint interactive buttons ("Completed", "Blocked", "Still In Progress") encode expected task status. If a task's status was changed prior to clicking an older button, the button action is safely ignored to preserve newer changes.

### Windows Resume & Scheduler Resilience
- Periodic background ticks detect sleep/resume gaps (> 5 minutes).
- Overdue check-ins are consolidated into a single message rather than a burst of obsolete alerts.
- Obsolete TOD or Pre-lunch reminders for shifts that ended while asleep are automatically cancelled and marked delivered.

---

## 6. Traceable Report Facts & Conversational Review (Stages F & G)

### Shared Report Fact Layer & Automatic Staleness
All report paths (slash commands, natural language, scheduler) use `build_report_facts` and `ReportValidator`:
- **Unique Clients vs. Interaction Count:** 4 support queries for 1 client are counted as 1 distinct client and 4 interactions (no count inflation).
- **Verification separation:** Developer-reported fixes do not count as verified until retested.
- **Automatic Staleness Detection:** Reports retain `facts_hash`. Any new work, task edits, or activities automatically mark unfinalized reports stale (`is_stale = 1`) without requiring manual commands.

### Conversational Report Review
Request formatting adjustments without mutating underlying data or losing factual context:
```text
Make the EOD shorter
Use bullet points
Add the testing environment
```
- Creates a new versioned entry in `reports` with `revision = revision + 1` and `style = 'short' | 'detailed'`.
- Preserves the full factual snapshot (cases, testing sessions, baseline tasks, client masking) across wording revisions.
- Preserves historical versions immutably.
- Does not change or delete underlying tasks or activities.

---

## 7. Tomorrow Planning & Handover (Stage H)

At end-of-shift review, manage open work cleanly:

```text
/tomorrow
/carrytask <id>
/workhandover
```

- `/tomorrow`: Lists all unfinished tasks (pending, in progress, blocked) with next actions, blockers, and linked requests.
- `/carrytask ID`: Advances `due_date` to tomorrow while preserving task ID, status, blocker, and history. Undoable via `/undo`.
- `/workhandover`: Produces a clean handover summary of active requests, latest verified status, waiting-on dependencies, and next actions.
- Pending tasks are never automatically forced into tomorrow's TOD without explicit planning confirmation.

---

## 8. Deletion & Scoped Cleanups

```text
delete all tasks
delete completed tasks
delete pending tasks
delete today's tasks
delete task 5
```

- **Owner-only:** Immediate execution without blocking prompt for authorized owner.
- **Scope-aware:** "delete completed tasks" removes only completed items; "delete all tasks" removes all.
- **Ambiguity safety:** "delete everything" asks for clarification.
- **Historical preservation:** Historical reports and plan snapshots retain task details even after deletion.
- **Transactional Undo:** `/undo` restores deleted tasks and re-links associated activities.

---

## 9. Windows Verification & Health Checks

Verify system health and database integrity:

```powershell
# Verify live schema version (v9) and table integrity
.\.venv\Scripts\python.exe scripts\check_database.py

# Verify NLP intent and entity accuracy
.\.venv\Scripts\python.exe scripts\evaluate_nlp.py

# Run complete test suite (203 tests)
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# Check Scheduled Task status
Get-ScheduledTask -TaskName "Telegram Work Assistant"
```
