# Local Support Copilot Architecture

## 1. Architectural decision

Build the Support Copilot as a separate local application boundary. Reuse selected concepts and services from Telegram Work Assistant and selected UI patterns from PAIOS, but do not merge both monoliths or let the desktop UI access either database directly.

Initial deployment:

```text
Windows desktop
├── Electron + React overlay
├── Python local Core API (loopback only)
├── SQLite operational database
├── SQLite FTS5 approved-knowledge index
└── Optional remote AI provider through a provider gateway
```

The first release is local-first and single-owner. Cloud synchronization, organization tenancy, meetings, screen vision, and external write automation are later phases.

## 2. System context

```text
 Support API     Browser extension     Manual capture
     │                  │                    │
     └────────────── Capture Gateway ────────┘
                            │
                     Core Application API
                            │
       ┌────────────────────┼────────────────────┐
       │                    │                    │
 Context/Case          Knowledge           Activity Ledger
 Resolution            Retrieval           and Reporting
       │                    │                    │
       └──────────── Suggestion Orchestrator ────┘
                            │
                  AI Provider Gateway
               (Gemini / other / local model)
                            │
                    Desktop Overlay
                            │
                       Human operator

 n8n connects only through authenticated integration endpoints.
```

## 3. Component responsibilities

### Desktop overlay

- Displays the active conversation and resolved case.
- Shows proposed replies, sources, assumptions, and missing facts.
- Supports edit, copy, reject, regenerate, and explicit sent confirmation.
- Displays visible capture/provider/offline state.
- Never owns business rules or database credentials.

### Capture gateway

- Accepts manual input in Phase 1.
- Adds one channel adapter at a time beginning in Phase 3.
- Normalizes provider events into a canonical envelope.
- Verifies authentication and rejects replay before dispatch.
- Preserves provider message ID and raw-source hash without placing unrestricted raw content in logs.

Canonical event envelope:

```json
{
  "provider": "manual|freshdesk|browser_extension|other",
  "event_id": "provider-stable-id",
  "event_type": "message.received|message.sent",
  "occurred_at": "ISO-8601",
  "actor": {"external_id": "...", "role": "client|agent"},
  "conversation": {"external_id": "...", "case_hint": "..."},
  "payload": {"text": "..."},
  "schema_version": 1
}
```

### Context and case resolution

- Resolves explicit IDs first, then verified conversation links, then bounded candidates.
- Returns clarification when zero or multiple candidates remain.
- Never lets the model invent a durable entity identifier.

### Knowledge service

- Stores versioned, approved response articles and SOP fragments.
- Uses metadata filters plus SQLite FTS5 for Phase 1 retrieval.
- Adds embeddings only after a measured retrieval-quality baseline exists.
- Returns article ID, version, relevance evidence, required facts, and prohibited claims.
- Excludes drafts, retired articles, and incompatible product/client scopes.

### Suggestion orchestrator

- Builds a bounded, redacted prompt from the message, verified case facts, and retrieved knowledge.
- Requires schema-validated structured output.
- Applies deterministic post-validation for unsupported claims and missing citations.
- Stores the proposal, provider/model metadata, knowledge versions, and evaluation status.
- Does not send externally.

Suggested response contract:

```json
{
  "draft": "string",
  "source_refs": [{"article_id": 12, "version": 3}],
  "missing_facts": ["affected date range"],
  "assumptions": [],
  "prohibited_claims_detected": [],
  "confidence": 0.0,
  "recommended_action": "ask_clarification|reply|escalate"
}
```

### Activity ledger and reports

- Uses append-only events for observed and confirmed work.
- Distinguishes proposed, copied, sent, delivered, client-acknowledged, tested, escalated, and resolved.
- Derives TOD/EOD facts from structured records.
- Freezes a facts hash with each report preview and rejects stale finalization.

### AI provider gateway

- Presents one internal interface for remote and local models.
- Enforces timeouts, daily limits, redaction, schema validation, and failure classification.
- Does not silently fall back to a provider with weaker privacy settings.
- Records provider/model identifiers but not hidden chain-of-thought.

### n8n integration boundary

- Receives or polls external systems and normalizes events.
- Calls authenticated Core API endpoints.
- May distribute approved reports and send operational notifications.
- Never writes SQLite, grants permissions, or finalizes high-impact operations.

## 4. Trust boundaries

All of the following are untrusted:

- Client messages and attachments.
- Browser DOM content.
- OCR and screen-derived text.
- Meeting transcripts.
- Retrieved external documents.
- AI output and tool output.
- n8n payload content, even from an authenticated workflow.

Authentication proves who delivered a request. Authorization determines what that identity may do. Payload text never changes authorization.

Required controls:

- Loopback binding for local APIs by default.
- Short-lived scoped tokens between overlay and Core API.
- HMAC or asymmetric request signing for external integration calls.
- Timestamp, nonce, and idempotency validation.
- Capability allowlists per integration.
- Encryption for credentials and sensitive local fields.
- Configurable redaction and retention.
- Audit records for mutations and external calls.

## 5. Initial data model

```text
clients
support_cases
conversations
conversation_messages
knowledge_articles
knowledge_article_versions
response_suggestions
sent_responses
activity_events
followups
report_snapshots
integration_idempotency
audit_events
```

Key invariants:

- `(provider, event_id)` is unique.
- A `sent_response` requires an explicit confirmation or verified provider receipt.
- A suggestion references immutable knowledge versions.
- Case resolution stores the method and confidence, not only the resulting ID.
- Reports reference a frozen facts hash.
- Raw captures have explicit retention and are not permanent by default.

## 6. Initial Core API

```text
POST /v1/captures/manual-message
POST /v1/suggestions
GET  /v1/suggestions/{id}
POST /v1/suggestions/{id}/copy
POST /v1/suggestions/{id}/confirm-sent
POST /v1/suggestions/{id}/reject
GET  /v1/knowledge/search
POST /v1/knowledge/articles
POST /v1/knowledge/articles/{id}/approve
GET  /v1/activity/today
POST /v1/reports/eod/preview
POST /v1/reports/eod/finalize
POST /v1/integrations/events
```

All mutation endpoints accept a correlation ID. Integration endpoints additionally require provider identity and an idempotency key.

## 7. Observability

- Structured secret-free logs with correlation IDs.
- Metrics for capture failures, retrieval quality, suggestion latency, provider failure, human edit distance, acceptance/rejection rate, and duplicate suppression.
- Failure categories distinguish validation, authorization, dependency, timeout, and unexpected internal errors.
- User-facing status never claims a provider action succeeded without its receipt.

## 8. Explicitly deferred

- Automatic external sending.
- Continuous background screen recording.
- Autonomous mouse or keyboard control.
- Unattended medical or financial actions.
- Multi-tenant cloud deployment.
- Learning directly from every sent reply without approval.
- Vector database adoption before FTS retrieval is evaluated.
