"""Shared gateway datatypes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Role = Literal["user", "assistant"]

# Model ids cross a JSON boundary from users; constrain strictly (defence in
# depth even though this layer also builds provider payloads from dataclass
# fields rather than interpolating strings).
MODEL_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-:/]{0,127}$")


def assert_valid_model_id(model_id: str) -> str:
    if not MODEL_ID_PATTERN.match(model_id):
        raise ValueError(f"invalid model id: {model_id!r}")
    return model_id


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant"):
            raise ValueError(f"invalid role: {self.role!r}")


@dataclass(frozen=True)
class ModelRef:
    """A resolved model reference: provider plus exact provider model id."""

    provider: str  # "anthropic" | "openai"
    model_id: str
    alias: str | None = None
