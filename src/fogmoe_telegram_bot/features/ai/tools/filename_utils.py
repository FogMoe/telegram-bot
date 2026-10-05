import re

MAX_MEDIA_FILENAME_CHARS = 240
TRUNCATED_FILENAME_SUFFIX = "..."

_INVALID_FILENAME_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WHITESPACE_PATTERN = re.compile(r"\s+")
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def prompt_to_filename(
    prompt: object,
    extension: str,
    *,
    fallback_base: str,
    max_chars: int = MAX_MEDIA_FILENAME_CHARS,
) -> str:
    extension = extension if extension.startswith(".") else f".{extension}"
    base = _INVALID_FILENAME_PATTERN.sub(" ", str(prompt or ""))
    base = _WHITESPACE_PATTERN.sub(" ", base).strip(" .")
    if not base:
        base = fallback_base
    if base.upper() in _RESERVED_WINDOWS_NAMES:
        base = f"{base}_"

    max_base_chars = max_chars - len(extension)
    if max_base_chars <= 0:
        return extension[-max_chars:]

    if len(base) > max_base_chars:
        keep_chars = max(0, max_base_chars - len(TRUNCATED_FILENAME_SUFFIX))
        base = base[:keep_chars].rstrip(" .") + TRUNCATED_FILENAME_SUFFIX
        if not base.strip("."):
            base = fallback_base[:max_base_chars]

    return f"{base}{extension}"


__all__ = ["MAX_MEDIA_FILENAME_CHARS", "prompt_to_filename"]
