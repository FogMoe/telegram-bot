from datetime import datetime

import pytest
from pydantic import ValidationError

from features.ai.tools import memory_tools
from features.ai.tools.context import clear_tool_request_context, set_tool_request_context
from features.ai.tools.models import UserDiaryArgs, parameters_schema


class _FakeDiaryDatabase:
    def __init__(self):
        self.rows = {}
        self.last_write_sql = ""

    def fetch_one(self, sql, params):
        if "SELECT MAX(page_no)" in sql:
            return (max(self.rows),) if self.rows else (None,)
        if "SELECT content, title, summary" in sql:
            page_no = params[1]
            row = self.rows.get(page_no)
            if row is None:
                return None
            return (
                row["content"],
                row["title"],
                row["summary"],
                row["created_at"],
                row["updated_at"],
            )
        raise AssertionError(f"Unexpected fetch_one query: {sql}")

    def fetch_all(self, sql, params):
        assert "ORDER BY page_no ASC" in sql
        preview_chars, _user_id = params
        return [
            (
                page_no,
                row["title"],
                row["summary"],
                len(row["content"]),
                row["created_at"],
                row["updated_at"],
                row["content"][:preview_chars],
            )
            for page_no, row in sorted(self.rows.items())
        ]

    def execute(self, sql, params):
        self.last_write_sql = sql
        _user_id, page_no, title, summary, content = params
        existing = self.rows.get(page_no)
        now = datetime(2026, 7, 25, 12, 0, 0)
        self.rows[page_no] = {
            "title": title,
            "summary": summary,
            "content": content,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        return 1

    def add_page(
        self,
        page_no,
        content,
        *,
        title=None,
        summary=None,
        updated_at=None,
    ):
        timestamp = updated_at or datetime(2026, 7, 24, 10, 30, 0)
        self.rows[page_no] = {
            "title": title,
            "summary": summary,
            "content": content,
            "created_at": timestamp,
            "updated_at": timestamp,
        }


@pytest.fixture
def diary_db(monkeypatch):
    fake = _FakeDiaryDatabase()
    monkeypatch.setattr(memory_tools.mysql_connection, "run_sync", lambda value: value)
    monkeypatch.setattr(memory_tools.mysql_connection, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(memory_tools.mysql_connection, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(memory_tools.mysql_connection, "execute", fake.execute)
    set_tool_request_context({"user_id": 123})
    try:
        yield fake
    finally:
        clear_tool_request_context()


def test_user_diary_schema_exposes_index_and_bounded_metadata():
    schema = parameters_schema(UserDiaryArgs)
    properties = schema["properties"]

    assert "index" in properties["action"]["enum"]
    assert properties["title"]["maxLength"] == 60
    assert properties["summary"]["maxLength"] == 120

    with pytest.raises(ValidationError):
        UserDiaryArgs(title="t" * 61)
    with pytest.raises(ValidationError):
        UserDiaryArgs(summary="s" * 121)


def test_user_diary_index_lists_pages_without_full_content(diary_db):
    diary_db.add_page(2, "Private details", title="Projects", summary="Current projects.")
    diary_db.add_page(1, "\n  First   observation\nSecond line")

    result = memory_tools.user_diary_tool(action="index")

    assert result["action"] == "index"
    assert result["total_pages"] == 2
    assert result["next_page"] == 3
    assert [page["page"] for page in result["pages"]] == [1, 2]
    assert result["pages"][0] == {
        "page": 1,
        "title": "Untitled page 1",
        "summary": "First observation Second line",
        "metadata_complete": False,
        "length": 34,
        "created_at": "2026-07-24 10:30:00",
        "updated_at": "2026-07-24 10:30:00",
    }
    assert result["pages"][1]["metadata_complete"] is True
    assert "content" not in result["pages"][1]


def test_user_diary_read_marks_legacy_metadata_as_incomplete(diary_db):
    diary_db.add_page(1, "First observation\nSecond line")

    result = memory_tools.user_diary_tool(action="read", page=1)

    assert result["title"] == "Untitled page 1"
    assert result["summary"] == "First observation Second line"
    assert result["metadata_complete"] is False
    assert result["content"] == "First observation\nSecond line"


def test_user_diary_create_requires_title_and_summary(diary_db):
    missing_summary = memory_tools.user_diary_tool(
        action="append",
        page=1,
        content="First entry",
        title="Relationship",
    )
    missing_title = memory_tools.user_diary_tool(
        action="append",
        page=1,
        content="First entry",
        summary="The first lasting observation.",
    )

    assert missing_summary["error"] == (
        "Missing summary for diary page 1; summarize the updated page"
    )
    assert missing_title["error"] == (
        "Missing title for diary page 1; add a stable topic title"
    )
    assert diary_db.rows == {}


def test_user_diary_write_updates_content_and_metadata_together(diary_db):
    diary_db.add_page(
        1,
        "Existing entry",
        title="Relationship",
        summary="Earlier observation.",
    )

    missing_summary = memory_tools.user_diary_tool(
        action="append",
        page=1,
        content="New entry",
    )
    result = memory_tools.user_diary_tool(
        action="append",
        page=1,
        content="New entry",
        summary="Earlier and new observations.",
    )

    assert "Missing summary" in missing_summary["error"]
    assert result["title"] == "Relationship"
    assert result["summary"] == "Earlier and new observations."
    assert result["metadata_complete"] is True
    assert diary_db.rows[1]["content"] == "Existing entry\nNew entry"
    assert diary_db.rows[1]["title"] == "Relationship"
    assert diary_db.rows[1]["summary"] == "Earlier and new observations."
    assert "title = VALUES(title)" in diary_db.last_write_sql
    assert "summary = VALUES(summary)" in diary_db.last_write_sql


def test_user_diary_index_reports_no_next_page_at_limit(diary_db):
    for page_no in range(1, memory_tools.MAX_USER_DIARY_PAGES + 1):
        diary_db.add_page(
            page_no,
            f"Entry {page_no}",
            title=f"Page {page_no}",
            summary=f"Summary {page_no}",
        )

    result = memory_tools.user_diary_tool(action="index")

    assert result["total_pages"] == memory_tools.MAX_USER_DIARY_PAGES
    assert result["next_page"] is None
