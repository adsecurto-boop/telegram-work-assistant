# Phased Delivery and Quality Gates

Each phase delivers a usable vertical slice. Work on a later phase begins only after the current phase's acceptance tests and regression suite pass.

## Phase 0 — Contracts and safety foundation

Deliver:

- Separate project skeleton and local loopback Core API.
- Versioned canonical event and suggestion schemas.
- SQLite migration framework, audit events, and idempotency registry.
- AI provider interface with a deterministic fake provider for tests.
- Secret-free structured logging and configuration validation.

Exit gate:

- Duplicate event, unauthorized request, malformed schema, provider timeout, and migration rollback tests pass.
- No UI capture or external integration is enabled.

## Phase 1 — Manual message to grounded reply

Deliver:

- Manual client-message entry in the desktop overlay.
- Approved knowledge article CRUD and FTS5 search.
- Grounded draft containing source references, missing facts, and assumptions.
- Edit, copy, reject, and explicit confirm-sent actions.
- Append-only activity events.

Exit gate:

- Every scenario in `features/phase_1_grounded_reply.feature` passes.
- Suggestions with unavailable sources, prompt injection, missing facts, and provider failure fail safely.
- Nothing can send a message externally.

## Phase 2 — Daily memory and truthful reports

Deliver:

- Today timeline grouped by client/case.
- Structured support, testing, escalation, follow-up, and outcome events.
- TOD/EOD preview with source links and live facts hash.
- Stale report rejection and explicit finalization.

Exit gate:

- Suggested/copied content never appears as completed work.
- Late events make previews stale.
- Reports survive restart and remain reproducible.

## Phase 3 — First support-channel adapter

Deliver:

- One selected channel using its official API/webhook where available.
- Stable conversation/message identity and replay protection.
- Verified capture of inbound messages and replies actually sent by the human.
- Manual fallback when provider delivery receipts are unavailable.

Exit gate:

- Duplicate, delayed, out-of-order, edited, and deleted provider events are tested.
- Disconnecting the channel does not break manual mode or corrupt state.

## Phase 4 — n8n external orchestration

Deliver:

- Signed, capability-scoped n8n integration endpoint.
- Read-only EOD distribution workflow.
- Operational error workflow and retry policy.
- Exported workflow definitions and acceptance fixtures under version control.

Exit gate:

- Replay, signature failure, excessive capability, partial downstream failure, and retry idempotency tests pass.
- n8n has no database access.

## Phase 5 — Meeting copilot

Deliver:

- Explicit start/stop session UI and visible recording state.
- Consent acknowledgement, transcription adapter, speaker-aware notes where supported.
- Proposed actions and meeting summary requiring human approval.
- Configurable audio/transcript retention.

Exit gate:

- No session starts without an explicit user action.
- Transcript uncertainty is visible.
- Promises and resolutions are not recorded as facts without approval.

## Phase 6 — Screen assistance

Deliver:

- User-selected window capture and pause hotkey.
- On-demand screenshot preview, OCR, redaction, and visual-model adapter.
- Retrieved troubleshooting steps with evidence and uncertainty.
- Password/payment/sensitive-screen blocking rules.

Exit gate:

- Unapproved windows cannot be captured.
- Redaction and retention tests pass.
- The system proposes actions but cannot control mouse or keyboard.

## Phase 7 — Reliability and controlled expansion

Delivered:

- Human-approved response-learning workflow originating only from confirmed sent responses.
- Version-controlled synthetic retrieval evaluation dataset and knowledge corpus with an isolated CLI runner.
- Online SQLite backup API, manifest generation, integrity check, and safe atomic restore with pre-flight verification.
- Operational health dashboard endpoint (`/v1/health/detailed`) with secret redaction.
- Disaster recovery runbook (`DISASTER_RECOVERY.md`) with tested recovery procedures.

Exit gate:

- All learning candidate proposal, approval, rejection, and idempotency tests pass.
- Online backup, manifest verification, corrupt file detection, and isolated restore drill pass.
- Detailed health status exposes no tokens, keys, secrets, or file paths.
- Learning candidates require explicitly reviewed, generalized content and reject obvious client identifiers.

Explicitly deferred (awaiting real usage evidence):

- Additional support channels (Slack, Teams, WhatsApp, email) remain deferred.
- Autonomous outbound actions remain prohibited.

## Per-phase review workflow across AI tools

1. Human product owner approves the feature scenarios.
2. Gemini challenges missing conversational and client-support cases.
3. Google AI Studio explores UI states using the approved scenarios.
4. The approved UI behavior is written into the feature file or an architecture decision.
5. Codex implements one vertical slice and produces test evidence.
6. Antigravity performs an independent architecture/security review of the committed diff.
7. Codex addresses accepted findings and reruns the full relevant suite.
8. Human product owner performs the acceptance walkthrough and closes the phase.

Reviews should exchange commits, diffs, feature files, and test results—not informal claims that a feature is complete.
