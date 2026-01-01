from typing import Dict, List

OPENAI_TOOLS: List[Dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "get_help_text",
            "description": "Returns a list of available Telegram commands and features for users",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "google_search",
            "description": "Use Google search engine to obtain the latest information and answers",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query string. Can be keywords, phrases, or complete questions",
                    },
                    "detailed": {
                        "type": "boolean",
                        "description": "When true, use the standard Google engine instead of the lightweight one",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_group_context",
            "description": "Fetch message history from group chat (group chats only)",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_size": {
                        "type": "integer",
                        "description": "Number of historical messages to retrieve",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 100,
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch and render webpage content for up-to-date browsing",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Fully qualified URL to retrieve",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_python_code",
            "description": "Run Python code remotely and return its output",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_code": {
                        "type": "string",
                        "description": "Python source code snippet to execute",
                    },
                    "stdin": {
                        "type": "string",
                        "description": "Optional standard input for the program",
                    },
                },
                "required": ["source_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kindness_gift",
            "description": "Gift a certain amount of coins to the user based on your affection level towards them",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "integer",
                        "description": "Amount of coins to gift",
                        "minimum": 1,
                        "maximum": 10,
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_affection",
            "description": "Adjust your affection level towards the user (range: -100 to 100)",
            "parameters": {
                "type": "object",
                "properties": {
                    "delta": {
                        "type": "integer",
                        "description": (
                            "Affection level change value. Positive numbers indicate increase, "
                            "negative numbers indicate decrease"
                        ),
                        "minimum": -10,
                        "maximum": 10,
                    }
                },
                "required": ["delta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_impression",
            "description": "Update permanent impression of the user",
            "parameters": {
                "type": "object",
                "properties": {
                    "impression": {
                        "type": "string",
                        "description": (
                            "New impression text, complete and self-contained description "
                            "(max 500 characters)"
                        ),
                        "minLength": 1,
                        "maxLength": 500,
                    }
                },
                "required": ["impression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_permanent_summaries",
            "description": (
                "Fetch user's historical conversation summaries (newest on top, max 5 results per request)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "integer",
                        "description": "Start position (inclusive)",
                        "default": 1,
                        "minimum": 1,
                    },
                    "end": {
                        "type": "integer",
                        "description": "End position (inclusive)",
                        "default": 1,
                        "minimum": 1,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_permanent_records",
            "description": "Search user's permanent chat snapshots with a regex pattern",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for in user/assistant messages",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches to return",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 50,
                    },
                    "oldest_first": {
                        "type": "boolean",
                        "description": "Return results ordered from oldest to newest",
                        "default": False,
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_ai_message",
            "description": (
                "Schedule, list, or cancel one-time private messages for the user. "
                "UTC timestamps only. Max 3 pending tasks, max 12 total (older tasks are overwritten)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "create | list | cancel",
                        "default": "create",
                    },
                    "timestamp_utc": {
                        "type": "string",
                        "description": "UTC time in ISO8601, e.g. 2025-01-01T12:00:00Z",
                    },
                    "trigger_reason": {
                        "type": "string",
                        "description": "Why this task is triggered (short and explicit)",
                        "maxLength": 200,
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional background/context for the scheduled message",
                        "maxLength": 1000,
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Remind to yourself, what you should say/do to the user at runtime",
                        "maxLength": 2000,
                    },
                    "schedule_id": {
                        "type": "integer",
                        "description": "Schedule id for cancel action",
                        "minimum": 1,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "user_diary",
            "description": (
                "Read or update the internal diary for the current user. "
                "Actions: read (optionally by line range), append, overwrite, patch (replace line range). "
                "Use patch with start_line/end_line to replace lines; append adds content at the end. "
                "Up to 100 pages (1-based). Max 10,000 chars per page (older content truncated). Use the page parameter to select the page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "read | append | overwrite | patch",
                        "default": "read",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Diary page number (1-100)",
                        "default": 1,
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "content": {
                        "type": "string",
                        "description": "Diary content for append/overwrite/patch actions",
                        "maxLength": 10000,
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Start line number for read/patch (1-based)",
                        "minimum": 1,
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "End line number for read/patch (1-based, inclusive)",
                        "minimum": 1,
                    },
                    "line_numbers": {
                        "type": "boolean",
                        "description": "When true, include line-numbered entries in read responses",
                        "default": False,
                    },
                },
            },
        },
    },
]

__all__ = ["OPENAI_TOOLS"]
