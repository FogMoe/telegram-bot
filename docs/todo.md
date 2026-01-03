# TODO

- Handle Telegram API timeouts (e.g., "Update ... Timed out") with retry/backoff and tuned timeouts so updates/messages are not dropped.
- Add fuzzy search support for `search_permanent_records` (mode + min_score, optional dependency like rapidfuzz).
- Add AI document database tool (action: search | list | read):
  1) Confirm DB schema and access rules (table name, fields, ownership scope, redaction).
  2) Add Alembic migration for documents table and indexes (consider FULLTEXT on title/content).
  3) Implement handler in `modules/features/ai/tools/doc_tools.py` with action dispatch, input validation, limits, and safe error handling.
  4) Wire tool schema in `modules/features/ai/tools/schemas.py` and register handler in `modules/features/ai/tools/registry.py`.
  5) Optionally extend `SYSTEM_PROMPT` guidance in `modules/core/config.py` for when/how to call the tool.
  6) Add minimal logging/metrics and verify manual flows for search/list/read and permission boundaries.
