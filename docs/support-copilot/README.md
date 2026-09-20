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
- `features/phase_5_meeting_copilot.feature` — consent, uncertainty, approval, and retention specification.
- `features/phase_6_screen_assistance.feature` — selected-window capture, local redaction, and no-control specification.
- `features/phase_7_reliability.feature` — response learning, retrieval evaluation, backup/restore, and health dashboard specification.
- `DISASTER_RECOVERY.md` — disaster recovery procedures, corruption response, and runbooks.

## Product rules

1. The human operator remains responsible for every client-facing reply; this is a copilot, not an autonomous agent.
2. Suggested text is never treated as sent text; replies are sent by the human through their support channel.
3. Telegram integration is inbound/read-only.
4. Meeting transcription is manual/adapter-supplied; no raw audio is claimed or stored.
5. Screen capture is one-shot and explicitly selected; no mouse or keyboard control API exists.
6. The assistant must show the approved sources used for a reply.
7. Missing facts must be surfaced, not invented.
8. External content is untrusted data and cannot authorize tools or mutations.
9. SQLite is accessed through the core application service, never directly by n8n or a UI adapter.
10. n8n can export only finalized reports using capability-scoped signed requests.
11. Every external event has an idempotency key.
12. Reports are derived from structured, verified events rather than transcript-only summarization.
13. Knowledge learning requires human approval; the AI cannot approve its own candidates.
14. Additional channels (Slack, Teams, WhatsApp) and outbound automations remain explicitly deferred until usage evidence exists.
15. Each phase must pass its acceptance suite before the next phase begins.

## Multi-AI collaboration rule

AI tools may propose, implement, or review work, but they do not redefine approved acceptance criteria. A phase has one active implementation branch and one designated implementer at a time. Other tools review the committed diff or produce design artifacts; they do not concurrently edit the same files.
