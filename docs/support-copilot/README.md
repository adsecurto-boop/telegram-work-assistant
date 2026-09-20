# Support Copilot Design Pack

Status: proposed architecture; no production integration is authorized by these documents.

This directory is the source of truth for planning the local Support Copilot. The product observes only user-approved support contexts, retrieves approved knowledge, proposes grounded replies, records what was actually sent, and builds fact-checked daily work reports.

## Documents

- `MASTER_ATDD_PROMPT.md` — reusable implementation prompt for AI coding tools.
- `ARCHITECTURE.md` — target architecture, trust boundaries, data model, and API contracts.
- `PHASED_DELIVERY.md` — incremental delivery plan and quality gates.
- `features/phase_1_grounded_reply.feature` — initial executable acceptance specification.
- `features/phase_2_truthful_reports.feature` — daily memory and stale-safe report specification.
- `features/phase_3_telegram_capture.feature` — read-only Telegram adapter specification.
- `features/phase_4_n8n_export.feature` — signed n8n export boundary specification.

## Product rules

1. The human operator remains responsible for every client-facing reply.
2. Suggested text is never treated as sent text.
3. The assistant must show the approved sources used for a reply.
4. Missing facts must be surfaced, not invented.
5. External content is untrusted data and cannot authorize tools or mutations.
6. Screen and meeting capture are explicit, visible, pausable, and scoped.
7. SQLite is accessed through the core application service, never directly by n8n or a UI adapter.
8. Every external event has an idempotency key.
9. Reports are derived from structured, verified events rather than transcript-only summarization.
10. Each phase must pass its acceptance suite before the next phase begins.

## Multi-AI collaboration rule

AI tools may propose, implement, or review work, but they do not redefine approved acceptance criteria. A phase has one active implementation branch and one designated implementer at a time. Other tools review the committed diff or produce design artifacts; they do not concurrently edit the same files.
