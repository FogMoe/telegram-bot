from core import config
from features.ai.tools.doc_tools import read_doc_tool
from features.ai.tools.registry import AI_TOOL_HANDLERS
from features.ai.tools.schemas import OPENAI_TOOLS


def test_read_doc_lists_every_document_without_a_topic(monkeypatch):
    monkeypatch.setattr(
        config,
        "INTERNAL_DOCS",
        {
            "alpha": "# Alpha\nFirst summary",
            "beta": "# Beta\nSecond summary",
        },
    )

    result = read_doc_tool()
    topics = {entry["topic"] for entry in result["documents"]}
    assert topics == {"alpha", "beta"}
    for entry in result["documents"]:
        assert entry["title"]
        assert entry["summary"]


def test_read_doc_returns_full_content_for_a_known_topic(monkeypatch):
    monkeypatch.setattr(
        config,
        "INTERNAL_DOCS",
        {"alpha": "# Alpha\nFixture content"},
    )

    result = read_doc_tool(topic="alpha")

    assert result["topic"] == "alpha"
    assert result["content"] == "# Alpha\nFixture content"
    assert "truncated" not in result


def test_read_doc_normalizes_surrounding_whitespace(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_DOCS", {"alpha": "fixture"})

    assert read_doc_tool(topic="  alpha  ")["topic"] == "alpha"


def test_read_doc_treats_blank_topic_as_no_topic(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_DOCS", {"alpha": "fixture"})

    assert "documents" in read_doc_tool(topic="   ")


def test_read_doc_rejects_unknown_topic_and_lists_alternatives(monkeypatch):
    monkeypatch.setattr(
        config,
        "INTERNAL_DOCS",
        {
            "alpha": "# Alpha\nFirst summary",
            "beta": "# Beta\nSecond summary",
        },
    )

    result = read_doc_tool(topic="does-not-exist")

    assert "error" in result
    assert {entry["topic"] for entry in result["documents"]} == {"alpha", "beta"}


def test_read_doc_truncates_long_documents(monkeypatch):
    monkeypatch.setattr(
        config,
        "INTERNAL_DOCS",
        {"huge": "# Huge\nsummary\n" + "x" * 20000},
    )
    result = read_doc_tool(topic="huge")
    assert result["truncated"] is True
    assert len(result["content"]) == 8000


def test_read_doc_is_registered_as_a_tool():
    assert AI_TOOL_HANDLERS["read_doc"] is read_doc_tool
    names = {tool["function"]["name"] for tool in OPENAI_TOOLS}
    assert "read_doc" in names
    assert names == set(AI_TOOL_HANDLERS)
