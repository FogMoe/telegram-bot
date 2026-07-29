# TODO

- Track provider-side prompt-cache usage from LiteLLM responses, including cached/input tokens, token hit rate, and request hit rate; keep it distinct from LiteLLM response-cache metrics.
- Add fuzzy search support for `search_permanent_records` (mode + min_score, optional dependency like rapidfuzz).
- Add bidirectional AI reaction support:
  1) Give the AI a tool to add a reaction to a target Telegram message authored by either the user or the AI, with validation for supported reactions and bot permissions.
  2) Subscribe to user reaction updates for messages authored by either the AI or the reacting user, and turn reaction additions/removals into explicit conversation events that can prompt the AI.
  3) Deduplicate reaction updates and make retries safe so the AI is not prompted twice and tool calls do not repeat side effects.
  4) Add focused tests for AI-added reactions to user and AI messages, user reactions to AI messages and their own messages, reaction removal, unrelated-message filtering, and duplicate updates.
- Add safe cross-provider continuation after post-tool reply generation fails:
  1) Carry the accumulated provider-neutral messages, tool logs, and visible-send state in an explicit continuation object.
  2) Resume the next provider from that continuation state instead of restarting from the original user messages.
  3) Preserve tool-call/result pairing and sanitize provider-specific fields before handoff.
  4) Prove with tests that already completed tools, especially side-effectful tools, are never executed twice.
- Add AI document database tool (action: search | list | read):
  1) Confirm DB schema and access rules (table name, fields, ownership scope, redaction).
  2) Add Alembic migration for documents table and indexes (consider FULLTEXT on title/content).
  3) Implement handler in `modules/features/ai/tools/doc_tools.py` with action dispatch, input validation, limits, and safe error handling.
  4) Wire tool schema in `modules/features/ai/tools/schemas.py` and register handler in `modules/features/ai/tools/registry.py`.
  5) Optionally extend `SYSTEM_PROMPT` guidance in `modules/core/config.py` for when/how to call the tool.
  6) Add minimal logging/metrics and verify manual flows for search/list/read and permission boundaries.
