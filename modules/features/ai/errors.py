from litellm.exceptions import Timeout as LiteLLMTimeout


class SafetyBlockError(RuntimeError):
    """Raised when a provider blocks a response for safety reasons."""


def is_timeout_error(error: BaseException) -> bool:
    """Return whether an exception or one of its causes is a timeout."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, LiteLLMTimeout)):
            return True
        current = current.__cause__ or current.__context__
    return False

