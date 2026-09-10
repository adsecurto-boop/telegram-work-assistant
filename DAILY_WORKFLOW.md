# Daily workflow and task deletion

The daily workflow uses the existing shifts, tasks, activity logs, reports, report validation, AI wording and finalization paths. No second task list is created.

```text
/startday my shift today is 12 to 9 | Test attendance | Share client requirement
/schedule 21:00 16:00
/startday Test attendance | Follow up with development
/tod
started Test attendance
Test attendance is blocked because the updated build is unavailable
/checkpoint
/timeline
/resume
/eod
/tomorrow
/carrytask 3
```

Existing exact task titles are not duplicated by `/startday`. The first planning snapshot is retained for checkpoint comparison. Existing tasks from other shifts remain visible but are not silently reassigned. Priorities and next actions can be edited using `/edittask`. Checkpoints use the existing lunch report generator. EOD remains a draft until explicitly finalized through the existing report buttons; new work after a draft is subject to existing freshness checks.

Carry-forward preserves identity, status, blockers and history while moving the due date to tomorrow. It is undoable. The timeline displays recorded timestamps, not inferred work times.

## Deletion

```text
delete all tasks
clear all tasks
remove every task
delete all pending tasks
delete completed tasks
delete today's tasks
delete task 5
```

Explicit owner commands execute immediately. All includes every status. Today means tasks assigned to the active shift. Ambiguous “delete everything” asks for scope. Historical activities and stored reports remain intact; task links on activities are detached and restored by Undo. Undo runs transactionally and refuses conflicting reused task IDs rather than overwriting new tasks. Empty deletion does not offer an unrelated Undo.

## Remaining limitations

The guided multi-turn planning wizard, late-work event timestamps, task-to-request relationships, and conversational factual report corrections are not implemented by this increment. Existing report-edit commands and structured task commands remain available. No new claim is made about report quality or client-count accuracy beyond the existing tests.
