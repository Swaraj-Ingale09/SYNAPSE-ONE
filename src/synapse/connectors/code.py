"""Coding connectors: GitHub repository search, Stack Overflow questions (ADR 0008).

Both verified live 2026-10-25 without a key: GitHub's *search* endpoint reports
``x-ratelimit-limit: 10`` (10 requests/minute keyless — the famous 60/hour is
the core API, not search), and Stack Exchange reports quota in the JSON envelope
(``quota_max``/``quota_remaining``; 300/day keyless).
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode

from synapse.connectors.base import (
    Connector,
    RateLimit,
    ResultItem,
    clip_snippet,
    float_of,
    int_of,
    na,
    text_of,
)


def _title_or_placeholder(value: object) -> str:
    return text_of(value) or "(no title reported)"


def _iso_from_epoch(value: object) -> str | None:
    seconds = float_of(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds")


class GitHubConnector(Connector):
    """GitHub repository search. Supports the documented query qualifiers
    (``language:python stars:>1000``) because the query passes through as-is."""

    name = "github"
    domain = "coding"
    description = (
        "GitHub repositories via the Search API (date = last update; 10 searches/min keyless)"
    )
    max_results = 100

    def build_url(self, query: str, page_size: int) -> str:
        return "https://api.github.com/search/repositories?" + urlencode(
            {"q": query, "per_page": page_size}
        )

    def parse(
        self, query: str, page_size: int, envelope: object
    ) -> tuple[list[ResultItem], int | None, RateLimit | None]:
        data = self._obj(envelope, "the response")
        rows = self._arr(data.get("items"), "items")
        total = int_of(data.get("total_count"))

        items: list[ResultItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            license_info = row.get("license")
            spdx = license_info.get("spdx_id") if isinstance(license_info, dict) else None
            snippet, note = clip_snippet(text_of(row.get("description")))
            facts: list[tuple[str, str]] = [
                ("stars", na(int_of(row.get("stargazers_count")))),
                ("language", na(text_of(row.get("language")))),
                ("license", na(text_of(spdx))),
            ]
            if note:
                facts.append(note)
            items.append(
                ResultItem(
                    title=_title_or_placeholder(row.get("full_name")),
                    url=text_of(row.get("html_url")),
                    date=text_of(row.get("updated_at")),
                    snippet=snippet,
                    facts=tuple(facts),
                )
            )
        return items, total, None


class StackOverflowConnector(Connector):
    """Stack Overflow questions, top-voted first, through the Stack Exchange API."""

    name = "stackoverflow"
    domain = "coding"
    description = "Stack Overflow questions by votes (date = created; no excerpts, 300/day keyless)"
    max_results = 100

    def build_url(self, query: str, page_size: int) -> str:
        return "https://api.stackexchange.com/2.3/search/advanced?" + urlencode(
            {
                "order": "desc",
                "sort": "votes",
                "q": query,
                "site": "stackoverflow",
                "pagesize": page_size,
            }
        )

    def parse(
        self, query: str, page_size: int, envelope: object
    ) -> tuple[list[ResultItem], int | None, RateLimit | None]:
        data = self._obj(envelope, "the response")
        rows = self._arr(data.get("items"), "items")
        # The API reports has_more, never a total count — total stays None (n/a).
        limit = int_of(data.get("quota_max"))
        remaining = int_of(data.get("quota_remaining"))
        rate = (
            RateLimit(limit=limit, remaining=remaining, source="body")
            if limit is not None or remaining is not None
            else None
        )

        items: list[ResultItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_tags = row.get("tags")
            tags = (
                [tag for tag in raw_tags if isinstance(tag, str)]
                if isinstance(raw_tags, list)
                else []
            )
            answered = row.get("is_answered")
            facts: list[tuple[str, str]] = [
                ("score", na(int_of(row.get("score")))),
                ("answers", na(int_of(row.get("answer_count")))),
                (
                    "answered",
                    "yes" if answered is True else "no" if answered is False else "n/a",
                ),
                ("tags", ", ".join(tags[:5]) if tags else "n/a"),
            ]
            items.append(
                ResultItem(
                    title=_title_or_placeholder(row.get("title")),
                    url=text_of(row.get("link")),
                    date=_iso_from_epoch(row.get("creation_date")),
                    snippet=None,  # default filter returns no excerpt; don't invent one
                    facts=tuple(facts),
                )
            )
        return items, None, rate
