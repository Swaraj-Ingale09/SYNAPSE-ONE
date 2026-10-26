"""Scholarly and medical connectors: OpenAlex, Crossref, Europe PMC (ADR 0008).

Each provider was verified live on 2026-10-25: reachable without a key,
`robots.txt` permitting our User-Agent on the API host, response shape
inspected field by field. NCBI eutils and export.arxiv.org were rejected as
providers because their robots.txt disallows every path.
"""

from __future__ import annotations

from urllib.parse import urlencode

from synapse.connectors.base import (
    Connector,
    ConnectorError,
    RateLimit,
    ResultItem,
    clip_snippet,
    int_of,
    na,
    text_of,
)
from synapse.net.html_text import extract_text

_ABSTRACT_EXTRACT_CHARS = 50_000  # bound hostile bodies before snippet clipping


def _abstract_from_inverted(index: object) -> str | None:
    """OpenAlex stores abstracts as word -> positions; rebuild them in order."""
    if not isinstance(index, dict) or not index:
        return None
    placed: list[tuple[int, str]] = []
    for word, offsets in index.items():
        if not isinstance(word, str) or not isinstance(offsets, list):
            continue
        for offset in offsets:
            if isinstance(offset, bool) or not isinstance(offset, int):
                continue
            placed.append((offset, word))
    if not placed:
        return None
    placed.sort(key=lambda pair: pair[0])
    return " ".join(word for _, word in placed)


def _first_text(value: object) -> str | None:
    """Crossref repeats fields as one-element lists (``title``, ``container-title``)."""
    if isinstance(value, list):
        for element in value:
            text = text_of(element)
            if text:
                return text
        return None
    return text_of(value)


def _date_from_parts(value: object) -> str | None:
    """Crossref ``date-parts``: include only the precision the provider reported."""
    if not isinstance(value, list) or not value:
        return None
    first = value[0] if isinstance(value[0], list) else None
    if not first:
        return None
    parts = [int_of(part) for part in first]
    if not parts or parts[0] is None:
        return None
    if len(parts) >= 3 and parts[1] is not None and parts[2] is not None:
        return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
    if len(parts) >= 2 and parts[1] is not None:
        return f"{parts[0]:04d}-{parts[1]:02d}"
    return f"{parts[0]:04d}"


def _title_or_placeholder(value: object) -> str:
    return text_of(value) or "(no title reported)"


class OpenAlexConnector(Connector):
    """OpenAlex works search: the open catalog of ~250M scholarly works."""

    name = "openalex"
    domain = "knowledge"
    description = "OpenAlex scholarly works (date = publication date; abstracts when open)"
    max_results = 100

    def build_url(self, query: str, page_size: int) -> str:
        return "https://api.openalex.org/works?" + urlencode(
            {"search": query, "per_page": page_size}
        )

    def parse(
        self, query: str, page_size: int, envelope: object
    ) -> tuple[list[ResultItem], int | None, RateLimit | None]:
        data = self._obj(envelope, "the response")
        rows = self._arr(data.get("results"), "results")
        meta = data.get("meta")
        total = int_of(meta.get("count")) if isinstance(meta, dict) else None

        items: list[ResultItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            doi = text_of(row.get("doi"))
            primary = row.get("primary_location")
            landing = (
                text_of(primary.get("landing_page_url")) if isinstance(primary, dict) else None
            )
            if doi and not doi.startswith("http"):
                doi = f"https://doi.org/{doi}"
            snippet, note = clip_snippet(
                _abstract_from_inverted(row.get("abstract_inverted_index"))
            )
            facts: list[tuple[str, str]] = [
                ("cited by", na(int_of(row.get("cited_by_count")))),
                ("type", na(text_of(row.get("type")))),
            ]
            if note:
                facts.append(note)
            items.append(
                ResultItem(
                    title=_title_or_placeholder(row.get("title")),
                    url=doi or landing,
                    date=text_of(row.get("publication_date")),
                    snippet=snippet,
                    facts=tuple(facts),
                )
            )
        return items, total, None


class CrossrefConnector(Connector):
    """Crossref works search: DOI metadata for the scholarly record."""

    name = "crossref"
    domain = "knowledge"
    description = "Crossref DOI records (date = published date; JATS abstracts when present)"
    max_results = 100

    def build_url(self, query: str, page_size: int) -> str:
        return "https://api.crossref.org/works?" + urlencode(
            {"query": query, "rows": page_size}
        )

    def parse(
        self, query: str, page_size: int, envelope: object
    ) -> tuple[list[ResultItem], int | None, RateLimit | None]:
        data = self._obj(envelope, "the response")
        message = self._obj(data.get("message"), "message")
        rows = self._arr(message.get("items"), "items")
        total = int_of(message.get("total-results"))

        items: list[ResultItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            abstract_html = text_of(row.get("abstract"))
            abstract_text: str | None = None
            if abstract_html:
                _, abstract_text = extract_text(abstract_html, max_chars=_ABSTRACT_EXTRACT_CHARS)
            snippet, note = clip_snippet(abstract_text)
            published = row.get("published")
            date_parts = published.get("date-parts") if isinstance(published, dict) else None
            facts: list[tuple[str, str]] = [
                ("journal", na(_first_text(row.get("container-title")))),
                ("type", na(text_of(row.get("type")))),
                ("cited", na(int_of(row.get("is-referenced-by-count")))),
                ("doi", na(text_of(row.get("DOI")))),
            ]
            if note:
                facts.append(note)
            items.append(
                ResultItem(
                    title=_first_text(row.get("title")) or "(no title reported)",
                    url=text_of(row.get("URL")),
                    date=_date_from_parts(date_parts),
                    snippet=snippet,
                    facts=tuple(facts),
                )
            )
        return items, total, None


class EuropePmCConnector(Connector):
    """Europe PMC search: biomedical literature including PubMed and PMC records."""

    name = "europepmc"
    domain = "medical"
    description = "Europe PMC biomedical literature (date = first publication date)"
    max_results = 100

    def build_url(self, query: str, page_size: int) -> str:
        return "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode(
            {
                "query": query,
                "format": "json",
                "pageSize": page_size,
                "resultType": "core",
            }
        )

    def parse(
        self, query: str, page_size: int, envelope: object
    ) -> tuple[list[ResultItem], int | None, RateLimit | None]:
        data = self._obj(envelope, "the response")
        # Europe PMC reports query errors as HTTP 200 with an errCode envelope.
        if "errCode" in data:
            raise ConnectorError(
                "invalid_query",
                text_of(data.get("errMsg")) or "Europe PMC rejected the query",
            )
        listing = self._obj(data.get("resultList"), "resultList")
        rows = listing.get("result", [])
        if not isinstance(rows, list):
            raise ConnectorError("invalid_response", "europepmc expected 'result' to be an array")
        total = int_of(data.get("hitCount"))

        items: list[ResultItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            doi = text_of(row.get("doi"))
            pmid = text_of(row.get("pmid"))
            if doi:
                url = doi if doi.startswith("http") else f"https://doi.org/{doi}"
            elif pmid and pmid.isdigit():
                url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            else:
                url = None  # no verified deep link for this record — say nothing, not something
            journal_info = row.get("journalInfo")
            journal = journal_info.get("journal") if isinstance(journal_info, dict) else None
            journal_title = journal.get("title") if isinstance(journal, dict) else None
            authors = text_of(row.get("authorString"))
            if authors and len(authors) > 80:
                authors = authors[:80].rstrip() + "…"
            snippet, note = clip_snippet(text_of(row.get("abstractText")))
            facts: list[tuple[str, str]] = [
                ("authors", na(authors)),
                ("journal", na(text_of(journal_title))),
                ("cited", na(int_of(row.get("citedByCount")))),
                ("id", na(_record_id(row))),
            ]
            if note:
                facts.append(note)
            items.append(
                ResultItem(
                    title=_title_or_placeholder(row.get("title")),
                    url=url,
                    date=text_of(row.get("firstPublicationDate")),
                    snippet=snippet,
                    facts=tuple(facts),
                )
            )
        return items, total, None


def _record_id(row: dict[str, object]) -> str | None:
    source = text_of(row.get("source"))
    record = text_of(row.get("id"))
    if source and record:
        return f"{source}/{record}"
    return record or source
