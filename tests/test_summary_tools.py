from datetime import datetime

from features.ai.tools.context import (
    clear_tool_request_context,
    set_tool_request_context,
)
from features.ai.tools.schemas import (
    OPENAI_TOOLS,
    SUMMARY_SEARCH_PRIOR_CONTEXT_TOOL,
)
from features.ai.tools import summary_tools


def test_summary_search_schema_is_not_exposed_to_main_ai():
    schema = SUMMARY_SEARCH_PRIOR_CONTEXT_TOOL["function"]

    assert schema["name"] == "search_prior_context"
    assert set(schema["parameters"]["properties"]) == {"query", "limit"}
    assert "search_prior_context" not in {
        tool["function"]["name"] for tool in OPENAI_TOOLS
    }


def test_rank_prior_summaries_matches_chinese_without_extra_dependency():
    documents = [
        summary_tools.PriorSummaryDocument(
            created_at="2026-07-29 12:00:00",
            content="苍蓝计划采用第二套部署方案，仍需确认回滚步骤。",
            recency_rank=0,
        ),
        summary_tools.PriorSummaryDocument(
            created_at="2026-07-28 12:00:00",
            content="讨论了周末的天气和晚餐。",
            recency_rank=1,
        ),
    ]

    results = summary_tools.rank_prior_summaries(
        documents,
        "苍蓝计划 部署方案",
    )

    assert results
    assert "苍蓝计划" in results[0]["content"]


def test_rank_prior_summaries_uses_small_recency_boost_for_equal_matches():
    documents = [
        summary_tools.PriorSummaryDocument(
            created_at="newer",
            content="需要继续确认白鲸项目的发布窗口。",
            recency_rank=0,
        ),
        summary_tools.PriorSummaryDocument(
            created_at="older",
            content="需要继续确认白鲸项目的发布窗口。",
            recency_rank=8,
        ),
    ]

    results = summary_tools.rank_prior_summaries(documents, "白鲸项目")

    assert [result["created_at"] for result in results] == ["newer", "older"]


def test_search_prior_context_is_bounded_to_active_user_and_earlier_summaries(
    monkeypatch,
):
    captured = {}

    def fake_fetch_all(sql, params):
        captured.update(sql=sql, params=params)
        return [
            (
                datetime(2026, 7, 29, 12, 0, 0),
                "白鲸项目仍需确认发布窗口。",
            ),
            (datetime(2026, 7, 28, 12, 0, 0), "无关摘要。"),
        ]

    monkeypatch.setattr(summary_tools.mysql_connection, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        summary_tools.mysql_connection,
        "run_sync",
        lambda value: value,
    )
    set_tool_request_context({"user_id": 123, "summary_record_id": 456})
    try:
        result = summary_tools.search_prior_context_tool("白鲸项目", limit=3)
    finally:
        clear_tool_request_context()

    assert captured["params"] == (
        123,
        456,
        summary_tools.SUMMARY_BM25_MAX_SUMMARIES,
    )
    assert "SELECT created_at, summary" in captured["sql"]
    assert "conversation_snapshot" not in captured["sql"]
    assert "user_id = %s" in captured["sql"]
    assert "id < %s" in captured["sql"]
    assert result["query"] == "白鲸项目"
    assert len(result["results"]) == 1
    assert result["results"][0]["created_at"] == "2026-07-29 12:00:00"


def test_search_result_excerpt_is_bounded():
    content = "前" * 900 + "白鲸项目" + "后" * 900
    documents = [
        summary_tools.PriorSummaryDocument(
            created_at=None,
            content=content,
            recency_rank=0,
        )
    ]

    result = summary_tools.rank_prior_summaries(documents, "白鲸项目")[0]

    assert "白鲸项目" in result["content"]
    assert len(result["content"]) <= summary_tools.SUMMARY_BM25_EXCERPT_CHARS + 2
