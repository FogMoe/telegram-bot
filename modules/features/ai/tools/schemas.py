from typing import Any

from .models import AI_TOOL_ARG_MODELS, parameters_schema


def _tool_definition(name: str, description: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters_schema(AI_TOOL_ARG_MODELS[name]),
        },
    }


OPENAI_TOOLS: list[dict[str, Any]] = [
    _tool_definition(
        "get_help_text",
        "Returns a list of available Telegram commands and features for users",
    ),
    _tool_definition(
        "read_doc",
        (
            "Read an internal reference document about how this bot's own features "
            "work. Omit topic to list every available document with a one-line "
            "summary, then call again with a topic to read that one in full. "
            "Overly long documents are truncated."
        ),
    ),
    _tool_definition(
        "list_available_stickers",
        (
            "List configured Telegram sticker packs, their summaries, and currently "
            "available emoji choices. Use this before adding sticker directives to a reply."
        ),
    ),
    _tool_definition(
        "google_search",
        "Use Google search engine to obtain the latest information and answers",
    ),
    _tool_definition(
        "advisor",
        (
            "Submit one complete reasoning task to a read-only senior advisor. Put "
            "the question or decision in task. Use case_facts only for facts, evidence, "
            "options, and constraints specific to that task. Each call is a single-turn "
            "consultation and cannot receive follow-up messages. The advisor cannot "
            "use tools or take actions. Its response is advisory material for you to "
            "evaluate and synthesize."
        ),
    ),
    _tool_definition(
        "fetch_group_context",
        "Fetch message history from group chat (group chats only)",
    ),
    _tool_definition(
        "fetch_url",
        "Fetch and render webpage content for up-to-date browsing",
    ),
    _tool_definition(
        "execute_python_code",
        (
            "Run Python code remotely and return its output. All results must be "
            "printed with print(), otherwise they will not appear in the output."
        ),
    ),
    _tool_definition(
        "linux_sandbox",
        (
            "Execute a non-interactive shell command in an isolated temporary "
            "Linux sandbox. The sandbox preserves filesystem state across "
            "linux_sandbox calls in the same user request, then closes "
            "automatically. It lives at most 5 minutes and accepts at most 10 "
            "calls per request. Once it closes, the same user cannot open "
            "another sandbox for 5 minutes."
        ),
    ),
    _tool_definition(
        "generate_image",
        (
            "Generate exactly one image from a text prompt. The image is sent to "
            "Telegram immediately after the call succeeds."
        ),
    ),
    _tool_definition(
        "generate_voice",
        (
            "Generate exactly one spoken audio clip from text. The audio is sent to "
            "Telegram immediately after the call succeeds."
        ),
    ),
    _tool_definition(
        "kindness_gift",
        (
            "Gift coins to the user. Each user can receive one gift per 24 hours; "
            "calling again inside that window gifts nothing and returns a cooldown "
            "status. Omit amount to let the system pick one at random."
        ),
    ),
    _tool_definition(
        "update_impression",
        (
            "Replace the stored impression of the user. This overwrites the previous "
            "impression entirely instead of appending to it, so carry over everything "
            "still worth keeping; the current text is visible as <impression> in the "
            "user profile."
        ),
    ),
    _tool_definition(
        "fetch_permanent_summaries",
        "Fetch user's historical conversation summaries (newest on top, max 5 results per request)",
    ),
    _tool_definition(
        "search_permanent_records",
        "Search user's permanent chat snapshots with a regex pattern",
    ),
    _tool_definition(
        "schedule_ai_message",
        (
            "Schedule, list, or cancel one-time or recurring private messages for the user. "
            "Use recurrence parameters when creating recurring schedules. "
            "UTC timestamps only. Max 3 pending tasks, max 12 total (older tasks are overwritten)."
        ),
    ),
    _tool_definition(
        "user_diary",
        (
            "Read or update the internal diary for the current user. "
            "Actions: read (optionally by line range), append, overwrite, patch. "
            "Use patch with start_line/end_line to replace those lines; append adds "
            "content at the end. When a page exceeds its size limit, the oldest "
            "content is truncated."
        ),
    ),
]

__all__ = ["OPENAI_TOOLS"]
