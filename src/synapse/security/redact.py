"""Secret redaction for logs and error messages (spec 15.2)."""

from __future__ import annotations

import os
import re

_ENV_SECRET_KEYS = ("DB_PASSWORD", "SYNAPSE_TOKEN")

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer <credential>
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.\=]+"),
    # key/value style: password=..., token: ..., api-key ...
    re.compile(r"(?i)\b(token|password|secret|api[-_]?key)\s*[=:]\s*\S+"),
    # common provider key shapes
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    # credentials embedded in URLs: scheme://user:pass@host
    re.compile(r"(://)[^:/@\s]+:[^@\s]+(@)"),
)

_REDACTED = "<redacted>"


def redact(text: str | None) -> str | None:
    """Remove likely secrets from a string before logging or display."""
    if not text:
        return text
    out = text
    for key in _ENV_SECRET_KEYS:
        value = os.environ.get(key)
        if value and len(value) >= 4:
            out = out.replace(value, f"<redacted:{key}>")
    for pattern in _PATTERNS:
        if pattern.groups:
            out = pattern.sub(lambda m: "".join("<redacted>" for _ in m.groups()), out)
        else:
            out = pattern.sub(_REDACTED, out)
    return out
