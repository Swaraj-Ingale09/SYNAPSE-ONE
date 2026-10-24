"""HTML-to-text extraction using only the standard library.

We deliberately avoid regex-based "HTML stripping" (a classic injection
source) and third-party parsers: ``html.parser.HTMLParser`` tokenizes
correctly, never executes anything, and treats all input as inert data
(spec 15.3 — external content is data, never policy).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_SKIP_TAGS: frozenset[str] = frozenset(
    {"script", "style", "noscript", "template", "svg", "head", "iframe", "object"}
)
_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "table",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
        "pre",
        "hr",
    }
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001 - stdlib signature
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse whitespace per line, drop blank lines.
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def extract_text(html: str, max_chars: int = 200_000) -> tuple[str, str]:
    """Return ``(title, text)`` from an HTML document.

    Output is capped at ``max_chars`` so a hostile multi-megabyte page cannot
    bloat the database or a future prompt.
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML must not crash ingestion
        pass  # whatever was parsed before the error is still returned
    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()
    text = parser.text()
    if len(text) > max_chars:
        text = text[:max_chars]
    return title, text
