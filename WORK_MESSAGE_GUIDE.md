# Work message assistant

Drafts are stored locally, survive restart, and use revision-bound action buttons. The requirement template preserves the owner's greeting, field order and punctuation. Other scenario headings are preliminary formats, editable with `scenario=`.

## Start

Send `/draft` followed by a rough note, or begin a message with `need to share`. Gemini extracts verbatim spans only. Its output is checked against the source before use. Without Gemini, the entire original note remains as the details, and explicitly recognizable email/channel values are filled.

For deterministic input:

```text
/draft scenario=requirement | recipients=@developer sir | platform=Teams | client=Example + TECH TEAM | requirements=Show download and upload speed at system startup. | admin_email=admin@example.com | cc=@sales
```

Missing fields are displayed. Use `/draftedit ID field=value | field=value` or reply to the latest draft with the same fields. Supported fields: scenario, recipients, platform, client, requirements, admin_email, cc. Reply `remove second requirement` to remove the second blank-line-separated requirement.

Replying with another unstructured note creates a linked child draft, preserving the parent and its original wording. This is deliberate: ambiguous follow-ups do not silently rewrite or close an existing request. Use field edits for replacements such as `requirements=43 licenses instead of 35`.

## Contacts and clients

```text
/workcontact akhil | @akhilpawar_globussoft | sir
/workclient client=Example + TECH TEAM | platform=Teams | admin_email=admin@example.com | cc=@sales
```

Only explicitly saved aliases are resolved. Ambiguous aliases remain visible for manual selection. Exact client names retrieve defaults; supplied draft values take precedence. Conflicting stored client profiles are not overwritten.

## Share and track

The copyable-text button emits a separate message. Send it to the official group manually, then press **Mark as shared**. Incomplete drafts cannot be marked shared. This records a single note on the active shift; drafting alone does not claim the work was shared, tested or resolved. These notes do not increase the resolved-client metric.

```text
/drafts
/draft 12
/drafthistory 12
/draftfollowup 12 2026-09-12T16:00
/workstatus 12 waiting_development Awaiting feasibility review
/workstatus 12 fix_reported Development reports a fix; retest pending
/workstatus 12 verified Retested on Windows 11; passed
/workstatus 12 client_confirmed Client confirmed the issue is resolved
/workhandover
```

Times without offsets use the configured timezone. Follow-ups catch up when the scheduled bot runs again. Handover lists recent non-cancelled request records; it is not a synthesized technical resolution report.

Statuses include investigating, fix_reported, deployed, verified, client_informed, client_confirmed, reopened, waiting_client, waiting_development, waiting_sales, waiting_access, waiting_availability. Status changes require an explicit note and are added to the current shift as owner-recorded facts.

## Scenario coverage

Requirement, modification, feasibility, issue, investigation, evidence, blocker, followup, fix, testing, resolution, reopened, license, renewal, trial, cancellation, custom_agent, deployment, meeting, handover, acknowledgment, alert, mixed, unknown.

Classification selects a draft format only. It never treats a developer's message, historical instructions, acknowledgment, or automated alert as authorization or completed work. Mixed requests are retained together for review; `/draftsplit ID` creates one linked child per blank-line-separated requirement for separate routing. Repeated splitting of the same revision does not duplicate children.

## Current boundaries

- No automatic group sending, contact scraping, commercial approval, or changes to client systems.
- No automatic import of the private HTML exports into this directory.
- Attachments continue through the existing evidence workflow; OCR and automatic attachment-to-draft linking are not added here.
- Meeting text is drafted as supplied; automatic timezone conversion and calendar invitations are not added.
- Free-form stylistic rewriting and automatic mixed-request splitting need a later reviewed-proposal workflow. Extractive drafting preserves facts but may require manual wording edits.

## Verification

```powershell
.venv\Scripts\python.exe -m unittest tests.test_work_messages
.venv\Scripts\python.exe -m unittest discover -s tests
```

Schema v8 adds drafts, revision events, explicit contacts and client profiles through the existing backup-first migration runner.
