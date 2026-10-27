"""MCP server: expose the SYNAPSE core to any MCP client as a thin client.

The workstation's vision (spec 1) says every terminal, IDE, MCP client, and
dashboard connects to one persistent service. This package is that fourth
surface: ``synapse mcp`` speaks JSON-RPC 2.0 over stdio (the MCP stdio
transport) and translates tool calls into authenticated calls against the
core's /v1 API — the same API the CLI and dashboard use. No database access,
no filesystem exposure, no shell: explicit bounded tools only (spec rule 7).

See ADR 0011 for the design and the honesty guarantees on the wire.
"""

from synapse.mcp.protocol import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    JsonRpcError,
)
from synapse.mcp.server import McpServer, run_mcp_server, serve_stdio
from synapse.mcp.tools import TOOLS, Tool, ToolError, ToolResult, call_tool

__all__ = [
    "JsonRpcError",
    "LATEST_PROTOCOL_VERSION",
    "McpServer",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOOLS",
    "Tool",
    "ToolError",
    "ToolResult",
    "call_tool",
    "run_mcp_server",
    "serve_stdio",
]
