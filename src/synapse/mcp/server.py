"""MCP stdio server: the loop that speaks JSON-RPC 2.0 to one client.

Newline-delimited JSON-RPC over stdio is the MCP stdio transport: each
message is one line, stdout carries protocol messages only, and anything
diagnostic goes to stderr (an MCP client treats stdout as the wire — a stray
print would corrupt the session). The server is a thin client of the running
core: tools in :mod:`synapse.mcp.tools` call the same authenticated /v1 API
the CLI and dashboard use, so the core stays the single owner of state
(ADR 0009 adds this as the fourth surface).
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

from synapse import __version__
from synapse.cli.client import SynapseClient
from synapse.config import Settings
from synapse.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    SERVER_TITLE,
    SUPPORTED_PROTOCOL_VERSIONS,
    JsonRpcError,
    encode_response,
    error_response,
    parse_message,
    result_response,
    valid_id,
)
from synapse.mcp.tools import call_tool


class McpServer:
    """Dispatches MCP requests against a SynapseClient-backed tool registry."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.initialized = False

    # --- request handlers --------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            version = requested
        else:
            # Per the MCP handshake: answer with the version we speak and let
            # the client decide whether to continue.
            version = LATEST_PROTOCOL_VERSION
        self.initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": SERVER_TITLE,
                "version": __version__,
            },
            "instructions": SERVER_INSTRUCTIONS,
        }

    def _tools_list(self) -> dict[str, Any]:
        from synapse.mcp.tools import TOOLS

        return {"tools": [tool.as_mcp() for tool in TOOLS]}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise JsonRpcError(INVALID_PARAMS, "params.name must be a non-empty string")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(
                INVALID_PARAMS, "params.arguments must be an object when provided"
            )
        # One client per call: the token is read once, no cross-call state.
        with SynapseClient(self.settings) as client:
            result = call_tool(name.strip(), arguments, client)
        return result.as_mcp()

    # --- dispatch ----------------------------------------------------------

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one parsed message; ``None`` means no response (notification)."""
        message_id = message.get("id")
        is_notification = "id" not in message

        if message.get("jsonrpc") != "2.0":
            return error_response(
                None, INVALID_REQUEST, "Invalid Request: jsonrpc must be \"2.0\""
            )
        method = message.get("method")
        if not isinstance(method, str) or not method:
            if is_notification:
                return None
            return error_response(
                message_id if valid_id(message_id) else None,
                INVALID_REQUEST,
                "Invalid Request: method must be a non-empty string",
            )
        if not is_notification and not valid_id(message_id):
            return error_response(
                None, INVALID_REQUEST, "Invalid Request: invalid id type"
            )
        params = message.get("params") or {}
        if not isinstance(params, dict):
            if is_notification:
                return None
            return error_response(
                message_id, INVALID_PARAMS, "params must be an object"
            )

        try:
            result = self._dispatch(method, params)
        except JsonRpcError as exc:
            if is_notification:
                return None
            return error_response(message_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # never crash the session on one message
            if is_notification:
                return None
            return error_response(
                message_id,
                INTERNAL_ERROR,
                f"internal error: {type(exc).__name__}: {exc}",
            )
        if is_notification:
            return None
        return result_response(message_id, result)

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "notifications/initialized":
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return self._tools_list()
        if method == "tools/call":
            return self._tools_call(params)
        raise JsonRpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    # --- wire --------------------------------------------------------------

    def handle_line(self, raw: str) -> str | None:
        """One input line -> one output line (or None for notifications)."""
        try:
            message = parse_message(raw)
        except JsonRpcError as exc:
            return encode_response(error_response(None, exc.code, exc.message))
        response = self.handle(message)
        if response is None:
            return None
        return encode_response(response)


def serve_stdio(
    server: McpServer,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Read JSON-RPC lines from stdin, answer on stdout, until EOF.

    stdout carries protocol messages only; diagnostics go to stderr. Any
    single bad line is answered with its error and the session continues —
    an MCP client never loses the connection over one malformed message.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    print(
        f"synapse-one MCP server v{__version__} — waiting for MCP client on stdio",
        file=stderr,
        flush=True,
    )
    for line in stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            response = server.handle_line(stripped)
        except Exception as exc:  # belt and braces: one line never kills the loop
            response = encode_response(
                error_response(
                    None, INVALID_REQUEST, f"internal error: {type(exc).__name__}: {exc}"
                )
            )
        if response is not None:
            stdout.write(response + "\n")
            stdout.flush()
    return 0


def run_mcp_server(settings: Settings) -> int:
    """Entry point used by ``synapse mcp``: wire the real core and serve."""
    return serve_stdio(McpServer(settings))
