"""Provider adapters: Anthropic Messages API and OpenAI Responses API.

Request construction is dict-based (serialized by httpx's ``json=``), never
string-interpolated, so prompt content can never escape into headers or JSON
structure. Responses are size-capped and shape-validated before use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from synapse.gateway.errors import (
    ModelNotFoundError,
    ProviderAuthError,
    ProviderRateLimited,
    ProviderServerError,
)
from synapse.gateway.types import ChatMessage, ModelRef, assert_valid_model_id
from synapse.security import redact

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class ProviderReply:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


class Provider(Protocol):
    name: str

    def complete(
        self,
        model: ModelRef,
        messages: list[ChatMessage],
        *,
        system: str,
        max_tokens: int,
        client: httpx.Client,
    ) -> ProviderReply: ...


def _raise_for_status(response: httpx.Response, provider: str) -> None:
    if response.status_code < 400:
        return
    body_preview = ""
    try:
        body_preview = response.text[:500]
    except Exception:  # noqa: BLE001 - preview must never mask the status
        pass
    detail = redact(f"{provider} HTTP {response.status_code}: {body_preview}") or ""
    if response.status_code in (401, 403):
        raise ProviderAuthError(f"{detail} — check the API key in your environment (.env)")
    if response.status_code == 404:
        raise ModelNotFoundError(detail)
    if response.status_code == 429:
        retry_after: float | None = None
        header = response.headers.get("retry-after")
        if header:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
        raise ProviderRateLimited(detail, retry_after=retry_after)
    if response.status_code >= 500:
        raise ProviderServerError(detail)
    raise ProviderServerError(detail)


def _read_json(response: httpx.Response, provider: str) -> dict[str, Any]:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ProviderServerError(f"{provider} response exceeded size cap")
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderServerError(f"{provider} returned non-JSON response") from exc
    if not isinstance(data, dict):
        raise ProviderServerError(f"{provider} returned unexpected JSON shape")
    return data


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = (
            api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        ).strip()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def complete(
        self,
        model: ModelRef,
        messages: list[ChatMessage],
        *,
        system: str,
        max_tokens: int,
        client: httpx.Client,
    ) -> ProviderReply:
        assert_valid_model_id(model.model_id)
        payload: dict[str, Any] = {
            "model": model.model_id,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        response = client.post(
            "https://api.anthropic.com/v1/messages",
            json=payload,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )
        _raise_for_status(response, self.name)
        data = _read_json(response, self.name)
        blocks = data.get("content")
        text_parts = [
            block.get("text", "")
            for block in blocks or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not text_parts:
            raise ProviderServerError("anthropic returned no text content")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ProviderReply(
            text="\n".join(text_parts),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            stop_reason=data.get("stop_reason"),
        )


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = (
            api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        ).strip()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def complete(
        self,
        model: ModelRef,
        messages: list[ChatMessage],
        *,
        system: str,
        max_tokens: int,
        client: httpx.Client,
    ) -> ProviderReply:
        assert_valid_model_id(model.model_id)
        # Responses API (2026 current): instructions=system, input=messages.
        payload: dict[str, Any] = {
            "model": model.model_id,
            "instructions": system,
            "input": [
                {"role": m.role, "content": [{"type": "input_text", "text": m.content}]}
                if m.role == "user"
                else {"role": m.role, "content": [{"type": "output_text", "text": m.content}]}
                for m in messages
            ],
            "max_output_tokens": max_tokens,
        }
        response = client.post(
            "https://api.openai.com/v1/responses",
            json=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
        )
        _raise_for_status(response, self.name)
        data = _read_json(response, self.name)

        text_parts: list[str] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text_parts.append(content.get("text", ""))
        if not text_parts:
            # Some responses carry the flattened helper field.
            if isinstance(data.get("output_text"), str) and data["output_text"]:
                text_parts.append(data["output_text"])
            else:
                raise ProviderServerError("openai returned no text content")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ProviderReply(
            text="\n".join(text_parts),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            stop_reason=data.get("status"),
        )


PROVIDERS: dict[str, Provider] = {
    "anthropic": AnthropicProvider(),
    "openai": OpenAIProvider(),
}


def provider_is_configured(name: str) -> bool:
    provider = PROVIDERS.get(name)
    configured = getattr(provider, "configured", False)
    return bool(configured)
