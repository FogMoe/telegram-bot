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
        "fetch_group_context",
        "Fetch message history from group chat (group chats only)",
    ),
    _tool_definition(
        "fetch_url",
        "Fetch and render webpage content for up-to-date browsing",
    ),
    _tool_definition(
        "execute_python_code",
        "Run Python code remotely and return its output",
    ),
    _tool_definition(
        "generate_image",
        "Generate exactly one image from a text prompt.",
    ),
    _tool_definition(
        "kindness_gift",
        "Gift a certain amount of coins to the user",
    ),
    _tool_definition(
        "update_impression",
        "Update permanent impression of the user",
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
            "Actions: read (optionally by line range), append, overwrite, patch (replace line range). "
            "Use patch with start_line/end_line to replace lines; append adds content at the end. "
            "Up to 100 pages (1-based). Max 10,000 chars per page (older content truncated). "
            "Use the page parameter to select the page."
        ),
    ),
]

__all__ = ["OPENAI_TOOLS"]
