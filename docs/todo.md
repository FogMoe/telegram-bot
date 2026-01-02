# TODO

- Handle Telegram API timeouts (e.g., "Update ... Timed out") with retry/backoff and tuned timeouts so updates/messages are not dropped.
- Add fuzzy search support for `search_permanent_records` (mode + min_score, optional dependency like rapidfuzz).
