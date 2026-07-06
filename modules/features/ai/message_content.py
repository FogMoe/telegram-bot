from typing import Any


def content_has_image(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "image_url"
        for item in content
    )


def messages_have_images(messages) -> bool:
    return any(
        isinstance(message, dict) and content_has_image(message.get("content"))
        for message in messages
    )


def content_to_text(content: Any) -> str:
    if not isinstance(content, list):
        return content if isinstance(content, str) else str(content or "")

    text_parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if text:
                text_parts.append(str(text))
    return "\n".join(text_parts)


def strip_image_content(messages) -> list:
    stripped_messages = []
    for message in messages:
        if not isinstance(message, dict):
            stripped_messages.append(message)
            continue
        content = message.get("content")
        if not content_has_image(content):
            stripped_messages.append(message)
            continue
        stripped_message = dict(message)
        stripped_message["content"] = content_to_text(content)
        stripped_messages.append(stripped_message)
    return stripped_messages
