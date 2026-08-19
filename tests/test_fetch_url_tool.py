from features.ai.tools import http_tools


class _FakeResponse:
    def __init__(self, text, status_code=200, content_type="text/html"):
        self.text = text
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(
            {"method": "get", "url": url, "headers": headers, "timeout": timeout}
        )
        return self.response

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": "post",
                "url": url,
                "data": data,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return self.response


def _article_html(body: str, title: str = "Article Title") -> str:
    return (
        "<html><head>"
        f"<title>{title}</title>"
        "<meta name='description' content='A short summary'>"
        "</head><body>"
        "<nav>Home About Contact skip this nav clutter</nav>"
        f"<article><h1>{title}</h1><p>{body}</p></article>"
        "<footer>Copyright 2026 ignore me</footer>"
        "</body></html>"
    )


def test_fetch_url_extracts_main_text_and_marks_untruncated(monkeypatch):
    body = (
        "This is the main article body that should be extracted. "
        "More sentences to make it look like a real article for the extractor. "
        "Second paragraph with useful content about the topic."
    )
    fake_session = _FakeSession(_FakeResponse(_article_html(body)))
    monkeypatch.setattr(http_tools, "_get_session", lambda: fake_session)

    result = http_tools.fetch_url_tool("https://example.test/a")

    assert result["url"] == "https://example.test/a"
    assert result["truncated"] is False
    assert result["title"] == "Article Title"
    assert "main article body" in result["content"]
    assert "Home About Contact" not in result["content"]
    assert fake_session.calls[0]["headers"] == http_tools._JINA_READER_HEADERS


def test_fetch_url_truncates_extracted_text(monkeypatch):
    body = "Useful sentence about the topic. " * 800
    fake_session = _FakeSession(_FakeResponse(_article_html(body)))
    monkeypatch.setattr(http_tools, "_get_session", lambda: fake_session)
    monkeypatch.setattr(http_tools, "FETCH_URL_MAX_CHARS", 200)

    result = http_tools.fetch_url_tool("https://example.test/long")

    assert result["truncated"] is True
    assert len(result["content"]) == 200
    assert result["content"] == result["content"][:200]


def test_fetch_url_extracts_html_fragments_without_full_document(monkeypatch):
    fragment = (
        "<div><h1>Fragment Title</h1>"
        "<p>This is the main article body that should be extracted. "
        "More sentences to make it look like a real article for the extractor.</p>"
        "<p>Second paragraph with useful content about the topic.</p></div>"
    )
    fake_session = _FakeSession(_FakeResponse(fragment))
    monkeypatch.setattr(http_tools, "_get_session", lambda: fake_session)

    result = http_tools.fetch_url_tool("https://example.test/fragment")

    assert result["truncated"] is False
    assert "main article body" in result["content"]


def test_fetch_url_falls_back_to_plain_text_when_not_html(monkeypatch):
    fake_session = _FakeSession(
        _FakeResponse("Plain fetched notes", content_type="text/plain")
    )
    monkeypatch.setattr(http_tools, "_get_session", lambda: fake_session)

    result = http_tools.fetch_url_tool("https://example.test/notes.txt")

    assert result["truncated"] is False
    assert result["content"] == "Plain fetched notes"
    assert "title" not in result


def test_fetch_url_posts_fragment_urls_to_jina(monkeypatch):
    fake_session = _FakeSession(_FakeResponse("Plain fragment content"))
    monkeypatch.setattr(http_tools, "_get_session", lambda: fake_session)

    result = http_tools.fetch_url_tool("https://example.test/page#section")

    assert result["content"] == "Plain fragment content"
    assert fake_session.calls[0]["method"] == "post"
    assert fake_session.calls[0]["data"] == {"url": "https://example.test/page#section"}


def test_fetch_url_rejects_blank_url():
    assert http_tools.fetch_url_tool("   ") == {"error": "Please provide a valid URL"}


def test_fetch_url_returns_truncated_false_for_short_plain_text(monkeypatch):
    fake_session = _FakeSession(
        _FakeResponse("short note", content_type="text/markdown")
    )
    monkeypatch.setattr(http_tools, "_get_session", lambda: fake_session)
    monkeypatch.setattr(http_tools, "FETCH_URL_MAX_CHARS", 20)

    result = http_tools.fetch_url_tool("https://example.test/note.md")

    assert result["truncated"] is False
    assert result["content"] == "short note"


def test_fetch_url_truncates_plain_text_without_html(monkeypatch):
    fake_session = _FakeSession(
        _FakeResponse("abcdefghijklmnopqrstuvwxyz", content_type="text/plain")
    )
    monkeypatch.setattr(http_tools, "_get_session", lambda: fake_session)
    monkeypatch.setattr(http_tools, "FETCH_URL_MAX_CHARS", 10)

    result = http_tools.fetch_url_tool("https://example.test/plain.txt")

    assert result["truncated"] is True
    assert result["content"] == "abcdefghij"
