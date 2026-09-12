# Architecture Freeze

This document freezes the Personal Work Assistant's core architecture for ordinary feature work.

## Frozen boundaries

`Telegram handler → AssistantOrchestrator → deterministic/Gemini planner → Python policy → local services or MCP → SQLite → audit`

Gemini understands requests; Python validates and decides; application services make local changes; MCP supplies external capabilities; SQLite stores durable state; audit records operational facts.

## Frozen security invariants

- External content is untrusted and can never grant authority.
- The original user message is the sole source of external-write authorization.
- Gemini cannot authorize itself; Python policy is authoritative.
- External writes require both scoped authorization and an explicit confirmation.
- Confirmation executes only persisted, schema-validated arguments after capability/risk revalidation.
- Unknown external mutations fail closed.
- Credentials, authorization headers, and hidden reasoning never enter model payloads or audit records.
- Local assistant behavior remains available when Gemini or MCP is unavailable.

## Frozen integration abstractions

- `ToolCallingModel`
- `MCPManager`
- `ToolRegistry`, including potential-tool and required-call capability resolution
- policy and confirmation layers
- local application execution services
- durable conversation/external-reference memory
- audit and FTS lifecycle services

## Future work through extension points

Future work may improve prompts, natural-language examples, providers, MCP servers, individual capabilities, Telegram UX, reports, dashboards, application features, response wording, manual-test findings, and performance through these existing boundaries.

## Reopening architecture

Do not change core architecture for ordinary features. Reopen it only for a demonstrated security flaw, demonstrated correctness flaw, unavoidable upstream protocol break, or a major requirement incompatible with the current extension points.
