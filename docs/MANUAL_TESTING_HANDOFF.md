# Manual Testing Handoff

This is a guide for owner-led manual testing after the architecture freeze. It is not an automated-pass report.

## Local assistant

- Create, complete, and carry forward tasks.
- Start, change, and end shifts.
- Create/update cases, capture test notes and follow-ups, generate reports, and use undo.
- Restart the bot and confirm persistence.
- Try ambiguous references and corrections.

## Conversation intelligence and Gemini

- Try pronouns, follow-up messages, compound requests, unclear requests, conditional requests, and contextual case/task references.
- Try vague and compound wording with Gemini enabled.
- Temporarily use unavailable Gemini configuration and confirm the local fallback stays usable.

## MCP read and mixed external/local flow

- Look up a GitHub issue, then ask follow-up questions using “it”.
- Ask for labels, state, and creator; verify a current-state question triggers a fresh lookup.
- Try: “Check issue #61. If it is still open, create a retest task for tomorrow.” Confirm the local task is conditional on a verified fact.

## External-write UX

Initially use safe mock/test configuration only. Try “Add the bug label.” It must show a confirmation and make no immediate mutation. Cancel it and confirm nothing executes. Confirm once and verify the exact mock call executes once; repeat the callback and verify replay is rejected.

## Resilience and security observations

Try Gemini/MCP unavailability, reconnect, Telegram restart, malformed input, expired/cancelled confirmations, and ambiguous targets. Report any claim of an action that did not occur, mutation without confirmation, guessed target, stale external fact, visible secret, or unexpected tool execution.
