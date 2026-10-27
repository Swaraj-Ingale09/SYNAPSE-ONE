"""JSON-RPC 2.0 + MCP protocol handling for the SYNAPSE stdio server (ADR 0011).

The wire format is newline-delimited JSON-RPC 2.0 over stdio — the MCP stdio
transport. This module owns envelope validation, error codes, method routing,
and version negotiation; tool execution lives in :mod:`synapse.mcp.tools`.

Honesty rules (spec 2) hold on the wire too: every failure carries its real
reason and a stable code — parse errors, unknown methods, bad ids, and bad
parameters are four distinct situations with four distinct messages, and a
notification (no id) never receives a response, per JSON-RPC.
"""

from __future__ import annotations

import json
from typing import Any

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 standard error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP protocol revisions this server speaks. Per the MCP initialization
# handshake: echo the client's requested version when we support it, else
# answer with the newest we know and let the client decide whether to stay.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2024-11-05", "2025-03-26")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]

SERVER_NAME = "synapse-one"
SERVER_TITLE = "SYNAPSE ONE"

SERVER_INSTRUCTIONS = (
    "SYNAPSE ONE workstation. Tools run against the local core over its "
    "authenticated loopback API: ask (model answer), research (evidence-first "
    "answer with citations), search (free public APIs), connectors (search "
    "catalog), jobs/job/cancel_job (durable job control), status (core health). "
    "Every tool reports real state: failures carry their actual reason, counts "
    "the provider did not report are marked as such, and nothing is simulated."
)


class JsonRpcError(Exception):
    """A protocol-level failure with its stable JSON-RPC code."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def parse_message(raw: str) -> dict[str, Any]:
    """Parse one input line; a bad line is a PARSE_ERROR, never a crash."""
    try:
        message = json.loads(raw)
    except ValueError as exc:
        raise JsonRpcError(PARSE_ERROR, f"Parse error: {exc}") from None
    if not isinstance(message, dict):
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: expected a JSON-RPC object")
    return message


def encode_response(response: dict[str, Any]) -> str:
    """One response object -> one line (the stdio transport forbids newlines)."""
    return json.dumps(response, ensure_ascii=False, default=str)


def error_response(
    message_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "error": error}


def result_response(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "result": result}


def valid_id(message_id: Any) -> bool:
    """JSON-RPC ids are strings, numbers, or null — never bools or objects."""
    if message_id is None:
        return True
    if isinstance(message_id, bool):
        return False
    return isinstance(message_id, str | int)
