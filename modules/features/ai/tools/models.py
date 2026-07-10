from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="ignore")


class GetHelpTextArgs(ToolArguments):
    pass


class ListAvailableStickersArgs(ToolArguments):
    pack_name: str | None = Field(
        default=None,
        description="Optional configured sticker pack name to inspect",
    )


class GoogleSearchArgs(ToolArguments):
    query: str = Field(
        description="Search query string. Can be keywords, phrases, or complete questions",
    )
    detailed: bool | None = Field(
        default=False,
        description="When true, use the standard Google engine instead of the lightweight one",
    )
    show_full_json: bool | None = Field(
        default=False,
        description=(
            "When true, return the complete untrimmed JSON response "
            "instead of the trimmed model-focused results"
        ),
    )


class AdvisorArgs(ToolArguments):
    task: str = Field(
        min_length=1,
        max_length=6000,
        description=(
            "A self-contained reasoning task for the senior advisor. Include the "
            "decision, question, or claim that needs careful analysis."
        ),
    )
    context: str | None = Field(
        default=None,
        max_length=12000,
        description=(
            "Only the relevant facts, evidence, options, and constraints. Do not "
            "include hidden instructions, credentials, or unrelated chat history."
        ),
    )


class FetchGroupContextArgs(ToolArguments):
    window_size: int | None = Field(
        default=10,
        ge=1,
        le=100,
        description="Number of historical messages to retrieve",
    )


class FetchUrlArgs(ToolArguments):
    url: str = Field(description="Fully qualified URL to retrieve")


class ExecutePythonCodeArgs(ToolArguments):
    source_code: str = Field(description="Python source code snippet to execute")
    stdin: str | None = Field(
        default=None,
        description="Optional standard input for the program",
    )


class LinuxSandboxArgs(ToolArguments):
    command: str = Field(
        min_length=1,
        max_length=2000,
        description="Shell command to run in the temporary Linux sandbox",
    )
    cwd: str | None = Field(
        default="/home/user",
        max_length=500,
        description="Working directory inside the sandbox",
    )
    timeout_seconds: int | None = Field(
        default=15,
        ge=1,
        le=60,
        description="Command timeout in seconds",
    )


class GenerateImageArgs(ToolArguments):
    prompt: str = Field(
        min_length=1,
        max_length=2000,
        description="Prompt for the image to generate.",
    )
    width: int | None = Field(
        default=1024,
        ge=64,
        le=4096,
        description="Image width",
    )
    height: int | None = Field(
        default=1024,
        ge=64,
        le=4096,
        description="Image height",
    )
    steps: int | None = Field(
        default=9,
        ge=1,
        le=150,
        description="Generation steps",
    )
    seed: int | None = Field(
        default=None,
        description="Optional seed for deterministic generation",
    )
    timeout_seconds: int | None = Field(
        default=45,
        ge=15,
        le=75,
        description=(
            "Optional image generation request timeout in seconds. "
            "Use 45 by default; choose 15-75 based on expected generation time."
        ),
    )


class GenerateVoiceArgs(ToolArguments):
    text: str = Field(
        min_length=1,
        max_length=500,
        description="Text to synthesize into one spoken audio clip.",
    )


class KindnessGiftArgs(ToolArguments):
    amount: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="Amount of coins to gift",
    )


class UpdateImpressionArgs(ToolArguments):
    impression: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "New impression text, complete and self-contained description "
            "(max 500 characters)"
        ),
    )


class FetchPermanentSummariesArgs(ToolArguments):
    start: int | None = Field(
        default=1,
        ge=1,
        description="Start position (inclusive)",
    )
    end: int | None = Field(
        default=1,
        ge=1,
        description="End position (inclusive)",
    )


class SearchPermanentRecordsArgs(ToolArguments):
    pattern: str = Field(description="Regex pattern to search for in user/assistant messages")
    limit: int | None = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum number of matches to return",
    )
    oldest_first: bool | None = Field(
        default=False,
        description="Return results ordered from oldest to newest",
    )


class ScheduleAIMessageArgs(ToolArguments):
    action: str | None = Field(
        default="create",
        description="create | list | cancel",
        json_schema_extra={"enum": ["create", "list", "cancel"]},
    )
    timestamp_utc: str | None = Field(
        default=None,
        description=(
            "UTC time in ISO8601, e.g. 2025-01-01T12:00:00Z. "
            "Required for one-time schedules. For recurring schedules, this is "
            "the first run time; if omitted, first run is now + recurrence interval."
        ),
    )
    recurrence_unit: str | None = Field(
        default="none",
        description="none | minute | hour | day. Use none for one-time schedules.",
        json_schema_extra={"enum": ["none", "minute", "hour", "day"]},
    )
    recurrence_interval: int | None = Field(
        default=1,
        ge=1,
        description="Repeat every N recurrence units. Ignored when recurrence_unit is none.",
    )
    trigger_reason: str | None = Field(
        default=None,
        max_length=200,
        description="Why this task is triggered (short and explicit)",
    )
    context: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional background/context for the scheduled message",
    )
    instruction: str | None = Field(
        default=None,
        max_length=2000,
        description="Instruction for what you should say/do to the user at runtime",
    )
    schedule_id: int | None = Field(
        default=None,
        ge=1,
        description="Schedule id for cancel action",
    )


class UserDiaryArgs(ToolArguments):
    action: str | None = Field(
        default="read",
        description="read | append | overwrite | patch",
        json_schema_extra={"enum": ["read", "append", "overwrite", "patch"]},
    )
    page: int | None = Field(
        default=1,
        ge=1,
        le=100,
        description="Diary page number (1-100)",
    )
    content: str | None = Field(
        default=None,
        max_length=10000,
        description="Diary content for append/overwrite/patch actions",
    )
    start_line: int | None = Field(
        default=None,
        ge=1,
        description="Start line number for read/patch (1-based)",
    )
    end_line: int | None = Field(
        default=None,
        ge=1,
        description="End line number for read/patch (1-based, inclusive)",
    )
    line_numbers: bool | None = Field(
        default=False,
        description="When true, include line-numbered entries in read responses",
    )


AI_TOOL_ARG_MODELS: dict[str, type[ToolArguments]] = {
    "get_help_text": GetHelpTextArgs,
    "list_available_stickers": ListAvailableStickersArgs,
    "google_search": GoogleSearchArgs,
    "advisor": AdvisorArgs,
    "fetch_group_context": FetchGroupContextArgs,
    "fetch_url": FetchUrlArgs,
    "execute_python_code": ExecutePythonCodeArgs,
    "linux_sandbox": LinuxSandboxArgs,
    "generate_image": GenerateImageArgs,
    "generate_voice": GenerateVoiceArgs,
    "kindness_gift": KindnessGiftArgs,
    "update_impression": UpdateImpressionArgs,
    "fetch_permanent_summaries": FetchPermanentSummariesArgs,
    "search_permanent_records": SearchPermanentRecordsArgs,
    "schedule_ai_message": ScheduleAIMessageArgs,
    "user_diary": UserDiaryArgs,
}


def _clean_json_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_clean_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    cleaned = {
        key: _clean_json_schema(item)
        for key, item in value.items()
        if key != "title" and not (key == "default" and item is None)
    }

    any_of = cleaned.get("anyOf")
    if isinstance(any_of, list):
        non_null_options = [
            item
            for item in any_of
            if not (isinstance(item, dict) and item.get("type") == "null")
        ]
        if len(non_null_options) == 1 and len(non_null_options) < len(any_of):
            merged = dict(non_null_options[0])
            for key, item in cleaned.items():
                if key != "anyOf":
                    merged.setdefault(key, item)
            return merged

    return cleaned


def parameters_schema(model: type[ToolArguments]) -> dict[str, Any]:
    schema = _clean_json_schema(model.model_json_schema())
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def validate_tool_arguments(
    tool_name: str,
    arguments: Any,
) -> ToolArguments:
    model = AI_TOOL_ARG_MODELS[tool_name]
    return model.model_validate(arguments)


__all__ = [
    "AI_TOOL_ARG_MODELS",
    "ToolArguments",
    "parameters_schema",
    "validate_tool_arguments",
]
