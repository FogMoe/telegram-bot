from features.ai.tools import http_tools


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeResponse(self.payload)


def test_google_search_tool_returns_model_focused_results(monkeypatch):
    payload = {
        "search_metadata": {
            "id": "search-id",
            "json_endpoint": "https://serpapi.test/result.json",
            "total_time_taken": 1.23,
        },
        "search_parameters": {
            "engine": "google_light",
            "q": "example query",
            "device": "desktop",
        },
        "organic_results": [
            {
                "position": 1,
                "title": " Example title ",
                "link": "https://example.test",
                "displayed_link": "example.test",
                "snippet": " Example snippet ",
                "source": "Example",
                "date": "Jul 8, 2026",
                "thumbnail": "https://example.test/thumb.png",
                "cached_page_link": "https://cache.test",
            },
            {
                "position": 2,
                "displayed_link": "noise-only.test",
                "thumbnail": "https://example.test/noise.png",
            },
            {
                "title": "Fallback rank result",
                "link": "https://fallback.test",
            },
        ],
    }
    fake_session = _FakeSession(payload)

    monkeypatch.setattr(http_tools, "SERPAPI_API_KEY", "test-key")
    monkeypatch.setattr(http_tools, "_get_session", lambda: fake_session)

    result = http_tools.google_search_tool("example query")

    assert result == {
        "query": "example query",
        "results": [
            {
                "rank": 1,
                "title": "Example title",
                "url": "https://example.test",
                "snippet": "Example snippet",
                "source": "Example",
                "date": "Jul 8, 2026",
            },
            {
                "rank": 3,
                "title": "Fallback rank result",
                "url": "https://fallback.test",
            },
        ],
    }
    assert fake_session.calls[0]["params"]["engine"] == "google_light"
    assert fake_session.calls[0]["params"]["q"] == "example query"
    assert "search_metadata" not in result
    assert "search_parameters" not in result


def test_google_search_tool_uses_detailed_engine(monkeypatch):
    fake_session = _FakeSession({"organic_results": []})

    monkeypatch.setattr(http_tools, "SERPAPI_API_KEY", "test-key")
    monkeypatch.setattr(http_tools, "_get_session", lambda: fake_session)

    assert http_tools.google_search_tool("example query", detailed=True) == {
        "query": "example query",
        "results": [],
    }
    assert fake_session.calls[0]["params"]["engine"] == "google"


def test_google_search_tool_can_return_full_json(monkeypatch):
    payload = {
        "search_metadata": {
            "id": "search-id",
            "json_endpoint": "https://serpapi.test/result.json",
        },
        "search_parameters": {
            "engine": "google_light",
            "q": "example query",
            "api_key": "should-not-leak",
        },
        "organic_results": [
            {
                "position": 1,
                "title": "Example title",
                "link": "https://example.test",
                "displayed_link": "example.test",
                "thumbnail": "https://example.test/thumb.png",
            }
        ],
        "related_questions": [
            {"question": "What is an example?"}
        ],
    }
    fake_session = _FakeSession(payload)

    monkeypatch.setattr(http_tools, "SERPAPI_API_KEY", "test-key")
    monkeypatch.setattr(http_tools, "_get_session", lambda: fake_session)

    result = http_tools.google_search_tool("example query", show_full_json=True)

    assert result == {
        "search_metadata": {
            "id": "search-id",
            "json_endpoint": "https://serpapi.test/result.json",
        },
        "search_parameters": {
            "engine": "google_light",
            "q": "example query",
        },
        "organic_results": [
            {
                "position": 1,
                "title": "Example title",
                "link": "https://example.test",
                "displayed_link": "example.test",
                "thumbnail": "https://example.test/thumb.png",
            }
        ],
        "related_questions": [
            {"question": "What is an example?"}
        ],
    }
    assert payload["search_parameters"]["api_key"] == "should-not-leak"
