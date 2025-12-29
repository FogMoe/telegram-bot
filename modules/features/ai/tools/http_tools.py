import logging
from typing import Dict
from urllib.parse import quote

import requests

from core import config

SERPAPI_API_KEY = getattr(config, "SERPAPI_API_KEY", "")


def google_search_tool(query: str) -> dict:
    """Perform a Google search via SerpApi."""
    if not SERPAPI_API_KEY:
        return {"error": "SerpApi key is not configured."}

    params = {
        "engine": "google_light",
        "q": query,
        "api_key": SERPAPI_API_KEY,
    }

    try:
        response = requests.get("https://serpapi.com/search", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logging.exception("SerpApi request failed: %s", exc)
        return {"error": f"SerpApi request failed: {exc}"}

    return {
        "search_metadata": data.get("search_metadata", {}),
        "search_parameters": data.get("search_parameters", {}),
        "organic_results": data.get("organic_results", []) or [],
    }


def fetch_url_tool(
    url: str,
    **kwargs,
) -> dict:
    """Fetch and render web content via Jina AI Reader."""
    if not isinstance(url, str) or not url.strip():
        return {"error": "Please provide a valid URL"}

    normalized_url = url.strip()
    if not normalized_url.startswith(("http://", "https://")):
        normalized_url = f"https://{normalized_url}"

    headers: Dict[str, str] = {}

    try:
        if "#" in normalized_url:
            response = requests.post(
                "https://r.jina.ai/",
                data={"url": normalized_url},
                headers=headers,
                timeout=10,
            )
        else:
            encoded_url = quote(normalized_url, safe=":/?&=#[]@!$&'()*+,;")
            response = requests.get(
                f"https://r.jina.ai/{encoded_url}",
                headers=headers,
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

    return {
        "url": normalized_url,
        "status_code": response.status_code,
        "content_type": response.headers.get("Content-Type"),
        "content": response.text,
    }


__all__ = [
    "google_search_tool",
    "fetch_url_tool",
]
