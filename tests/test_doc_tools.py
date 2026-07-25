from core import config
from features.ai.tools.doc_tools import read_doc_tool
from features.ai.tools.registry import AI_TOOL_HANDLERS
from features.ai.tools.schemas import OPENAI_TOOLS


def test_internal_docs_are_loaded_from_resources():
    assert config.INTERNAL_DOCS
    for topic, content in config.INTERNAL_DOCS.items():
        assert topic
        assert content.strip()
        assert content.lstrip().startswith("# "), topic


def test_read_doc_lists_every_document_without_a_topic():
    result = read_doc_tool()
    topics = {entry["topic"] for entry in result["documents"]}
    assert topics == set(config.INTERNAL_DOCS)
    for entry in result["documents"]:
        assert entry["title"]
        assert entry["summary"]


def test_read_doc_returns_full_content_for_a_known_topic():
    result = read_doc_tool(topic="spam")
    assert result["topic"] == "spam"
    assert "/spam add" in result["content"]
    assert "truncated" not in result


def test_read_doc_normalizes_surrounding_whitespace():
    assert read_doc_tool(topic="  spam  ")["topic"] == "spam"


def test_read_doc_treats_blank_topic_as_no_topic():
    assert "documents" in read_doc_tool(topic="   ")


def test_read_doc_rejects_unknown_topic_and_lists_alternatives():
    result = read_doc_tool(topic="does-not-exist")
    assert "error" in result
    assert {entry["topic"] for entry in result["documents"]} == set(config.INTERNAL_DOCS)


def test_read_doc_truncates_long_documents(monkeypatch):
    monkeypatch.setitem(config.INTERNAL_DOCS, "huge", "# Huge\nsummary\n" + "x" * 20000)
    result = read_doc_tool(topic="huge")
    assert result["truncated"] is True
    assert len(result["content"]) == 8000


def test_read_doc_is_registered_as_a_tool():
    assert AI_TOOL_HANDLERS["read_doc"] is read_doc_tool
    names = {tool["function"]["name"] for tool in OPENAI_TOOLS}
    assert "read_doc" in names
    assert names == set(AI_TOOL_HANDLERS)
