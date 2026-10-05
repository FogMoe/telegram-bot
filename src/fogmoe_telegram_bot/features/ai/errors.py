from litellm.exceptions import Timeout as LiteLLMTimeout


class SafetyBlockError(RuntimeError):
    """Raised when a provider blocks a response for safety reasons."""


def _exception_chain(error: BaseException):
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_timeout_error(error: BaseException) -> bool:
    """Return whether an exception or one of its causes is a timeout."""
    for current in _exception_chain(error):
        if isinstance(current, (TimeoutError, LiteLLMTimeout)):
            return True
    return False


def is_retryable_completion_error(error: BaseException) -> bool:
    """Return whether a completion failure is transient enough to retry."""
    if is_timeout_error(error):
        return True

    for current in _exception_chain(error):
        status_code = getattr(current, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(current, "response", None), "status_code", None)
        try:
            parsed_status = int(status_code)
        except (TypeError, ValueError):
            continue
        if parsed_status in {408, 409, 429} or parsed_status >= 500:
            return True
    return False

