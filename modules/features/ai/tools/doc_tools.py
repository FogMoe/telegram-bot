from core import config

MAX_DOC_CHARS = 8000


def _doc_outline(text: str) -> tuple[str, str]:
    """Return the leading heading and the first line of prose beneath it."""
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith("# "):
            continue
        title = line[2:].strip()
        for following in lines[index + 1:]:
            if following:
                return title, following
        return title, ""
    return "", ""


def _doc_index() -> list[dict]:
    entries = []
    for topic, text in config.INTERNAL_DOCS.items():
        title, summary = _doc_outline(text)
        entries.append(
            {
                "topic": topic,
                "title": title or topic,
                "summary": summary,
            }
        )
    return entries


def read_doc_tool(topic: str | None = None, **kwargs) -> dict:
    if not config.INTERNAL_DOCS:
        return {"error": "No internal documents are available"}

    normalized = str(topic or "").strip()
    if not normalized:
        return {"documents": _doc_index()}

    content = config.INTERNAL_DOCS.get(normalized)
    if content is None:
        return {
            "error": f"Unknown topic: {normalized}",
            "documents": _doc_index(),
        }

    if len(content) > MAX_DOC_CHARS:
        return {
            "topic": normalized,
            "content": content[:MAX_DOC_CHARS],
            "truncated": True,
        }
    return {"topic": normalized, "content": content}


__all__ = ["read_doc_tool"]
