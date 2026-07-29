"""Lightweight read-only BM25 retrieval for the conversation summarizer."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from core import mysql_connection

from .context import get_tool_request_context

SUMMARY_BM25_MAX_SUMMARIES = 20
SUMMARY_BM25_MAX_RESULTS = 3
SUMMARY_BM25_EXCERPT_CHARS = 800
SUMMARY_BM25_MIN_SCORE = 0.5
SUMMARY_BM25_RECENCY_BOOST = 0.35

_CJK_SEQUENCE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD_RE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*")


@dataclass(frozen=True)
class PriorSummaryDocument:
    created_at: str | None
    content: str
    recency_rank: int


def _bm25_tokens(value: object) -> list[str]:
    """Tokenize mixed Chinese/Latin text without loading a segmenter."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    tokens = _WORD_RE.findall(normalized)
    for sequence in _CJK_SEQUENCE_RE.findall(normalized):
        if len(sequence) == 1:
            tokens.append(sequence)
            continue
        for size in (2, 3):
            tokens.extend(
                sequence[index : index + size]
                for index in range(len(sequence) - size + 1)
            )
    return tokens


def _timestamp_text(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat(sep=" ")
    return str(value)


def build_prior_summary_documents(rows: Iterable[tuple]) -> list[PriorSummaryDocument]:
    documents: list[PriorSummaryDocument] = []
    for recency_rank, row in enumerate(rows):
        if len(row) < 2:
            continue
        created_at, summary = row[:2]
        content = re.sub(r"\s+", " ", str(summary or "")).strip()
        if not content:
            continue
        documents.append(
            PriorSummaryDocument(
                created_at=_timestamp_text(created_at),
                content=content,
                recency_rank=recency_rank,
            )
        )
    return documents


def _matching_excerpt(content: str, query: str) -> str:
    if len(content) <= SUMMARY_BM25_EXCERPT_CHARS:
        return content

    normalized_query = unicodedata.normalize("NFKC", query).casefold()
    terms = _WORD_RE.findall(normalized_query)
    terms.extend(_CJK_SEQUENCE_RE.findall(normalized_query))
    terms.sort(key=len, reverse=True)

    folded_content = unicodedata.normalize("NFKC", content).casefold()
    position = next(
        (folded_content.find(term) for term in terms if folded_content.find(term) >= 0),
        0,
    )
    start = max(0, position - 200)
    end = min(len(content), start + SUMMARY_BM25_EXCERPT_CHARS)
    if end == len(content):
        start = max(0, end - SUMMARY_BM25_EXCERPT_CHARS)
    excerpt = content[start:end]
    if start > 0:
        excerpt = f"…{excerpt}"
    if end < len(content):
        excerpt = f"{excerpt}…"
    return excerpt


def rank_prior_summaries(
    documents: Iterable[PriorSummaryDocument],
    query: str,
    *,
    limit: int = SUMMARY_BM25_MAX_RESULTS,
) -> list[dict]:
    document_list = list(documents)
    query_tokens = set(_bm25_tokens(query))
    if not document_list or not query_tokens:
        return []

    tokenized = [_bm25_tokens(document.content) for document in document_list]
    lengths = [len(tokens) for tokens in tokenized if tokens]
    if not lengths:
        return []
    average_length = sum(lengths) / len(lengths)

    document_frequency: Counter[str] = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens) & query_tokens)

    total_documents = len(document_list)
    scored: list[tuple[float, PriorSummaryDocument]] = []
    k1 = 1.5
    b = 0.75
    for document, tokens in zip(document_list, tokenized):
        if not tokens:
            continue
        frequencies = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            frequency = frequencies.get(token, 0)
            if frequency <= 0:
                continue
            frequency_in_documents = document_frequency[token]
            inverse_document_frequency = math.log(
                1
                + (total_documents - frequency_in_documents + 0.5)
                / (frequency_in_documents + 0.5)
            )
            denominator = frequency + k1 * (
                1 - b + b * len(tokens) / average_length
            )
            score += inverse_document_frequency * (
                frequency * (k1 + 1) / denominator
            )

        if score <= 0:
            continue
        score += SUMMARY_BM25_RECENCY_BOOST / (document.recency_rank + 1)
        if score >= SUMMARY_BM25_MIN_SCORE:
            scored.append((score, document))

    result_limit = max(1, min(int(limit), SUMMARY_BM25_MAX_RESULTS))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "created_at": document.created_at,
            "content": _matching_excerpt(document.content, query),
            "score": round(score, 4),
        }
        for score, document in scored[:result_limit]
    ]


def search_prior_context_tool(query: str, limit: int | None = None) -> dict:
    """Search a bounded set of earlier summaries for the active summary task."""
    context = get_tool_request_context()
    user_id = context.get("user_id")
    record_id = context.get("summary_record_id")
    if not user_id or not record_id:
        return {"error": "Missing summary context, cannot search prior summaries"}

    query_value = str(query or "").strip()
    if not query_value:
        return {"error": "Search query cannot be empty"}
    try:
        limit_value = int(limit) if limit is not None else SUMMARY_BM25_MAX_RESULTS
    except (TypeError, ValueError):
        limit_value = SUMMARY_BM25_MAX_RESULTS
    limit_value = max(1, min(limit_value, SUMMARY_BM25_MAX_RESULTS))

    # Both boundaries are injected by the runner. The model cannot select a
    # different user or search the snapshot currently being summarized.
    rows = mysql_connection.run_sync(
        mysql_connection.fetch_all(
            "SELECT created_at, summary FROM permanent_chat_records "
            "WHERE user_id = %s AND id < %s "
            "AND summary IS NOT NULL AND summary <> '' "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (int(user_id), int(record_id), SUMMARY_BM25_MAX_SUMMARIES),
        )
    )
    documents = build_prior_summary_documents(rows)
    return {
        "query": query_value,
        "results": rank_prior_summaries(
            documents,
            query_value,
            limit=limit_value,
        ),
    }


__all__ = [
    "PriorSummaryDocument",
    "build_prior_summary_documents",
    "rank_prior_summaries",
    "search_prior_context_tool",
]
