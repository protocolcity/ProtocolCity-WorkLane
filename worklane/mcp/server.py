"""Stdio MCP server for WorkLane (wl-19).

JSON-RPC 2.0 over stdin/stdout, protocol version 2024-11-05. No external
MCP SDK — keeps WL installable on Python 3.9 with only fastapi/uvicorn.

Run:
  python -m worklane.mcp --author grok
  WL_AGENT_ID=grok python -m worklane.mcp

Claude Desktop / Cursor config example::

  {
    "mcpServers": {
      "worklane": {
        "command": "python",
        "args": ["-m", "worklane.mcp", "--author", "cursor"],
        "env": {
          "WL_PRODUCT": "tradeos",
          "WORKLANE_RUNTIME_DIR": "/path/to/worklane/local"
        }
      }
    }
  }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any, Dict, Optional, TextIO

from worklane.mcp.handlers import (
    TPHandlers,
    ToolError,
    build_tool_definitions,
    dispatch_tool,
)
from worklane.products import default_product_slug

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "worklane"
SERVER_VERSION = "0.1.0"


class MCPServer:
    """Minimal stdio MCP server implementing tools only."""

    def __init__(
        self,
        handlers: TPHandlers,
        stdin: Optional[TextIO] = None,
        stdout: Optional[TextIO] = None,
    ) -> None:
        self.handlers = handlers
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._initialized = False
        self._tools = build_tool_definitions()

    # ── I/O ──────────────────────────────────────────────────────────

    def _write(self, message: Dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        self.stdout.write(line + "\n")
        self.stdout.flush()

    def _reply(self, req_id: Any, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(
        self, req_id: Any, code: int, message: str, data: Any = None
    ) -> None:
        err: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._write({"jsonrpc": "2.0", "id": req_id, "error": err})

    # ── request handling ─────────────────────────────────────────────

    def handle_message(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        req_id = msg.get("id", None)
        params = msg.get("params") or {}

        # Notifications have no id — process silently.
        is_notification = "id" not in msg

        if method == "initialize":
            self._initialized = True
            if not is_notification:
                self._reply(
                    req_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": SERVER_VERSION,
                        },
                        "instructions": (
                            f"WorkLane MCP. Signed as author="
                            f"{self.handlers.author!r}, default product="
                            f"{self.handlers.default_product!r}. "
                            "Use wl_ready to find work, wl_claim (or "
                            "wl_reserve for soft-lock) to take it, "
                            "wl_close with structured §5 sections to finish. "
                            "Triage: wl_label/wl_update/wl_cancel/wl_reopen. "
                            "Session: wl_mine, wl_counts."
                        ),
                    },
                )
            return

        if method == "notifications/initialized":
            return

        if method == "ping":
            if not is_notification:
                self._reply(req_id, {})
            return

        if method == "tools/list":
            if not is_notification:
                self._reply(req_id, {"tools": self._tools})
            return

        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                result = dispatch_tool(self.handlers, name, arguments)
                payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
                if not is_notification:
                    self._reply(
                        req_id,
                        {
                            "content": [{"type": "text", "text": payload}],
                            "structuredContent": result
                            if isinstance(result, dict)
                            else {"result": result},
                            "isError": False,
                        },
                    )
            except ToolError as exc:
                if not is_notification:
                    self._reply(
                        req_id,
                        {
                            "content": [
                                {"type": "text", "text": f"Error: {exc.message}"}
                            ],
                            "isError": True,
                        },
                    )
            except TypeError as exc:
                # bad/missing args
                if not is_notification:
                    self._reply(
                        req_id,
                        {
                            "content": [
                                {"type": "text", "text": f"Error: invalid arguments — {exc}"}
                            ],
                            "isError": True,
                        },
                    )
            except Exception as exc:  # noqa: BLE001 — surface unexpected errors to client
                if not is_notification:
                    self._reply(
                        req_id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Error: {type(exc).__name__}: {exc}",
                                }
                            ],
                            "isError": True,
                        },
                    )
            return

        # Unknown method
        if is_notification:
            return
        self._error(req_id, -32601, f"Method not found: {method}")

    def serve_forever(self) -> int:
        """Read newline-delimited JSON-RPC from stdin until EOF."""
        for raw in self.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                self._error(None, -32700, f"Parse error: {exc}")
                continue
            if not isinstance(msg, dict):
                self._error(None, -32600, "Invalid Request: expected object")
                continue
            try:
                self.handle_message(msg)
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
                req_id = msg.get("id")
                if req_id is not None:
                    self._error(req_id, -32603, "Internal error")
        return 0


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="worklane.mcp",
        description=(
            "WorkLane stdio MCP server. "
            "Author identity is required (PROTOCOL.md §3.8)."
        ),
    )
    p.add_argument(
        "--author",
        default=os.environ.get("WL_AGENT_ID", ""),
        help="Canonical agent id for signed writes (or set WL_AGENT_ID)",
    )
    p.add_argument(
        "--product",
        default=os.environ.get("WL_PRODUCT") or default_product_slug(),
        help="Default product store when tools omit product (or set WL_PRODUCT)",
    )
    return p.parse_args(argv)


def main(argv: Optional[list] = None) -> None:
    args = parse_args(argv)
    author = (args.author or "").strip()
    if not author:
        print(
            "Error: author identity required at connect time.\n"
            "  python -m worklane.mcp --author <agent-id>\n"
            "  or set WL_AGENT_ID=<agent-id>\n"
            "Canonical ids: work-pool, founder-terminal, cursor, grok, "
            "cowork, wl-pool (PROTOCOL.md §5.2).",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        handlers = TPHandlers(author=author, default_product=args.product)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    # Log identity to stderr only — stdout is reserved for MCP JSON-RPC.
    print(
        f"worklane MCP ready author={handlers.author!r} "
        f"product={handlers.default_product!r}",
        file=sys.stderr,
    )
    server = MCPServer(handlers)
    raise SystemExit(server.serve_forever())


if __name__ == "__main__":
    main()
