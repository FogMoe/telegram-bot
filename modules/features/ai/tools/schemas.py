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
                    }
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
                "Fetch user's historical conversation summaries (newest on top, max 10 results per request)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "integer",
                        "description": "Start position (inclusive)",
                        "default": 1,
                    },
                    "end": {
                        "type": "integer",
                        "description": "End position (inclusive)",
                        "default": 2,
                    },
                },
            },
        },
    },
]

__all__ = ["OPENAI_TOOLS"]
