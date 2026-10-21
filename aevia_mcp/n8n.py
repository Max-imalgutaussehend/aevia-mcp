"""Bridge to n8n's own MCP server (`/mcp-server/http`): its tools show up here as `n8n_<name>` with n8n's original input schemas.

n8n (2.13 and later) ships a workflow builder as MCP tools: search nodes, read their types, validate SDK code, create and update workflows, run and
test them. This server does not reimplement any of that; it forwards. Configured by N8N_MCP_URL and N8N_MCP_TOKEN (a token of n8n's MCP server,
see scripts/n8n-mcp-token.py in the platform repository). The tool list is re-read every few minutes, so tools of a newer n8n appear by themselves.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Awaitable, Callable

from mcp.server.fastmcp.tools.base import Tool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.types import ToolAnnotations
from pydantic import ConfigDict

log = logging.getLogger("aevia.n8n")
PREFIX = "n8n_"
READ_ONLY = ("search_", "get_", "list_", "validate_", "explore_", "prepare_")
DESTRUCTIVE = ("archive_", "delete_", "unpublish_", "restore_")


class PassThroughArgs(ArgModelBase):
    """Arguments of a bridged tool: no fields of our own, whatever the caller sends goes to n8n unchanged (n8n validates against its schema)."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def model_dump_one_level(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


def annotations_for(name: str) -> ToolAnnotations:
    if name.startswith(READ_ONLY):
        return ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    return ToolAnnotations(readOnlyHint=False, destructiveHint=name.startswith(DESTRUCTIVE), idempotentHint=False, openWorldHint=False)


async def _list_remote(url: str, token: str) -> list[dict]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}, timeout=30) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.list_tools()
            return [{"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema} for t in res.tools]


async def _call_remote(url: str, token: str, name: str, arguments: dict) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}, timeout=120) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(name, arguments)
            text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            failed = bool(res.isError)
    if failed:  # raised after the connection is closed: inside it the message would vanish in a task group
        raise RuntimeError(text or "n8n reported an error")
    return text


class N8nBridge:
    def __init__(self, server, url: str, token: str, list_fn: Callable[[], Awaitable[list[dict]]] | None = None,
                 call_fn: Callable[[str, dict], Awaitable[str]] | None = None, interval: int = 300):
        self.server, self.url, self.token, self.interval = server, url, token, interval
        self.list_fn = list_fn or (lambda: _list_remote(url, token))
        self.call_fn = call_fn or (lambda name, args: _call_remote(url, token, name, args))
        self.names: set[str] = set()
        self.last_error: str | None = None
        self.last_sync: float | None = None

    def _make_tool(self, spec: dict) -> Tool:
        remote = spec["name"]

        async def forward(**arguments: Any) -> str:
            return await self.call_fn(remote, arguments)

        desc = (spec.get("description") or "").strip()
        return Tool(fn=forward, name=PREFIX + remote, description=f"[n8n] {desc}", parameters=spec.get("inputSchema") or {"type": "object", "properties": {}},
                    fn_metadata=FuncMetadata(arg_model=PassThroughArgs), is_async=True, context_kwarg=None, annotations=annotations_for(remote))

    async def sync(self) -> int:
        """Re-read n8n's tool list: add new tools, update changed ones, drop the ones n8n no longer has. Returns the number of bridged tools."""
        try:
            specs = await self.list_fn()
        except Exception as e:  # noqa: BLE001  n8n down or token wrong: keep what we have, report it in the overview
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            return len(self.names)
        tools = self.server._tool_manager._tools
        wanted = {PREFIX + s["name"]: s for s in specs}
        for name in list(self.names - wanted.keys()):
            tools.pop(name, None)
        for name, spec in wanted.items():
            tools[name] = self._make_tool(spec)
        self.names = set(wanted)
        self.last_error, self.last_sync = None, time.time()
        return len(self.names)

    def start_background_sync(self) -> None:
        def loop() -> None:
            while True:
                try:
                    asyncio.run(self.sync())
                except Exception:  # noqa: BLE001
                    log.exception("n8n sync failed")
                time.sleep(self.interval if self.names else 30)
        threading.Thread(target=loop, daemon=True, name="n8n-sync").start()

    def status(self) -> dict:
        return {"tools": len(self.names), "last_sync_epoch": self.last_sync, "last_error": self.last_error}
