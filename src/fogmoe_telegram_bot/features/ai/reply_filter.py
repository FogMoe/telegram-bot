import re


NO_RESPONSE_SENTINELS = {
    "[no_response]",
}


def normalize_ai_reply_text(value: object) -> str:
    text = str(value or "")
    for sentinel in NO_RESPONSE_SENTINELS:
        text = re.sub(re.escape(sentinel), "", text, flags=re.IGNORECASE)
    if not text.strip():
        return ""
    return text
