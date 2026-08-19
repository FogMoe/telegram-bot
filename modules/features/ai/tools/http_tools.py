import logging
import threading
from copy import deepcopy
from typing import Any
from urllib.parse import quote

import requests
import trafilatura

from core import config

SERPAPI_API_KEY = getattr(config, "SERPAPI_API_KEY", "")
FETCH_URL_MAX_CHARS = 12000
_JINA_READER_HEADERS = {"X-Return-Format": "html"}
_SESSION_LOCAL = threading.local()


def _get_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _SESSION_LOCAL.session = session
    return session


def _clean_search_result(result: dict[str, Any], fallback_rank: int) -> dict[str, Any]:
    cleaned: dict[str, Any] = {"rank": result.get("position") or fallback_rank}

    field_mapping = {
        "title": "title",
        "url": "link",
        "snippet": "snippet",
        "source": "source",
        "date": "date",
    }
    for output_key, source_key in field_mapping.items():
        value = result.get(source_key)
        if isinstance(value, str):
            value = value.strip()
        if value:
            cleaned[output_key] = value

    return cleaned


def _extract_page_text(raw: str, url: str) -> tuple[str, str | None]:
    """Extract main article text and an optional title from fetched page content."""
    title: str | None = None
    extracted: str | None = None

    try:
        extracted = trafilatura.extract(
            raw,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
    except Exception:
        logging.exception("Trafilatura extract failed for %s", url)
        extracted = None

    try:
        metadata = trafilatura.extract_metadata(raw, default_url=url)
    except Exception:
        logging.exception("Trafilatura metadata extract failed for %s", url)
        metadata = None
    if metadata is not None:
        metadata_title = getattr(metadata, "title", None)
        if isinstance(metadata_title, str) and metadata_title.strip():
            title = metadata_title.strip()

    if not extracted:
        try:
            extracted = trafilatura.html2txt(raw)
        except Exception:
            logging.exception("Trafilatura html2txt failed for %s", url)
            extracted = None

    content = (extracted or raw).strip()
    return content, title


def _truncate_text(content: str, max_chars: int) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    return content[:max_chars], True


def _full_search_response(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"raw_response": data}

    full_response = deepcopy(data)
    search_parameters = full_response.get("search_parameters")
    if isinstance(search_parameters, dict):
        search_parameters.pop("api_key", None)
    full_response.pop("api_key", None)
    return full_response


def google_search_tool(
    query: str,
    detailed: bool = False,
    show_full_json: bool = False,
    **kwargs,
) -> dict:
    """Perform a Google search via SerpApi."""
    if not SERPAPI_API_KEY:
        return {"error": "SerpApi key is not configured."}

    session = _get_session()
    engine = "google" if detailed else "google_light"
    params = {
        "engine": engine,
        "q": query,
        "api_key": SERPAPI_API_KEY,
    }

    try:
        response = session.get("https://serpapi.com/search", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logging.exception("SerpApi request failed: %s", exc)
        return {"error": f"SerpApi request failed: {exc}"}

    if show_full_json:
        return _full_search_response(data)

    organic_results = data.get("organic_results", []) or []
    cleaned_results = [
        cleaned
        for index, result in enumerate(organic_results, start=1)
        if isinstance(result, dict)
        for cleaned in [_clean_search_result(result, index)]
        if any(cleaned.get(key) for key in ("title", "url", "snippet"))
    ]

    return {
        "query": query,
        "results": cleaned_results,
    }


def fetch_url_tool(
    url: str,
    **kwargs,
) -> dict:
    """Fetch a webpage and return extracted main text."""
    if not isinstance(url, str) or not url.strip():
        return {"error": "Please provide a valid URL"}

    normalized_url = url.strip()
    if not normalized_url.startswith(("http://", "https://")):
        normalized_url = f"https://{normalized_url}"

    session = _get_session()

    try:
        if "#" in normalized_url:
            response = session.post(
                "https://r.jina.ai/",
                data={"url": normalized_url},
                headers=_JINA_READER_HEADERS,
                timeout=10,
            )
        else:
            encoded_url = quote(normalized_url, safe=":/?&=#[]@!$&'()*+,;")
            response = session.get(
                f"https://r.jina.ai/{encoded_url}",
                headers=_JINA_READER_HEADERS,
                timeout=10,
            )
    except requests.RequestException as exc:
        logging.exception("Failed to fetch URL : %s", exc)
        return {"error": f"Failed to fetch URL: {exc}"}

    if response.status_code >= 400:
        return {
            "error": "Upstream fetch failed",
            "status_code": response.status_code,
            "details": response.text[:500],
        }

    content, title = _extract_page_text(response.text, normalized_url)
    content, truncated = _truncate_text(content, FETCH_URL_MAX_CHARS)
    result: dict[str, Any] = {
        "url": normalized_url,
        "status_code": response.status_code,
        "content_type": response.headers.get("Content-Type"),
        "content": content,
        "truncated": truncated,
    }
    if title:
        result["title"] = title
    return result


__all__ = [
    "google_search_tool",
    "fetch_url_tool",
]
