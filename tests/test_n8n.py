import asyncio
import socket
import subprocess
import sys
import time

import httpx
from mcp.server.fastmcp import FastMCP

from aevia_mcp import n8n

SPECS = [
    {"name": "search_workflows", "description": "Search for workflows.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "create_workflow_from_code", "description": "Create.", "inputSchema": {"type": "object", "required": ["code"], "properties": {"code": {"type": "string"}}}},
    {"name": "archive_workflow", "description": "Archive.", "inputSchema": {"type": "object", "properties": {"workflowId": {"type": "string"}}}},
]


def run(coro):
    return asyncio.run(coro)


def test_tools_are_bridged_with_their_schemas_and_arguments_pass_through():
    server, calls = FastMCP("t"), []

    async def lister():
        return SPECS

    async def caller(name, args):
        calls.append((name, args))
        return '{"ok": true}'

    bridge = n8n.N8nBridge(server, "http://n8n", "tok", list_fn=lister, call_fn=caller)

    async def go():
        assert await bridge.sync() == 3
        tools = {t.name: t for t in await server.list_tools()}
        assert set(tools) == {"n8n_search_workflows", "n8n_create_workflow_from_code", "n8n_archive_workflow"}
        assert tools["n8n_search_workflows"].inputSchema == SPECS[0]["inputSchema"]  # n8n's own schema, not ours
        assert tools["n8n_search_workflows"].annotations.readOnlyHint is True
        assert tools["n8n_create_workflow_from_code"].annotations.readOnlyHint is False
        assert tools["n8n_archive_workflow"].annotations.destructiveHint is True
        out = await server.call_tool("n8n_search_workflows", {"query": "mail", "limit": 3})
        return out

    out = run(go())
    assert calls == [("search_workflows", {"query": "mail", "limit": 3})]
    assert "ok" in str(out)


def test_sync_drops_removed_tools_and_survives_a_dead_n8n():
    server, state = FastMCP("t"), {"specs": SPECS, "down": False}

    async def lister():
        if state["down"]:
            raise ConnectionError("n8n is down")
        return state["specs"]

    bridge = n8n.N8nBridge(server, "u", "t", list_fn=lister, call_fn=lambda n, a: None)

    async def go():
        await bridge.sync()
        state["specs"] = SPECS[:1]
        await bridge.sync()
        assert [t.name for t in await server.list_tools()] == ["n8n_search_workflows"]
        state["down"] = True
        assert await bridge.sync() == 1  # keeps what it has
        assert "ConnectionError" in bridge.status()["last_error"]
        state["down"] = False
        await bridge.sync()
        assert bridge.status()["last_error"] is None

    run(go())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_transport_against_a_fake_n8n_with_sse_and_a_bearer_token():
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.fake_n8n:app", "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                if httpx.post(f"http://127.0.0.1:{port}/mcp", timeout=2).status_code == 401:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        url = f"http://127.0.0.1:{port}/mcp"
        server = FastMCP("bridge")
        bridge = n8n.N8nBridge(server, url, "good")

        async def go():
            assert await bridge.sync() == 2
            assert {t.name for t in await server.list_tools()} == {"n8n_search_workflows", "n8n_create_workflow_from_code"}
            out = await server.call_tool("n8n_search_workflows", {"query": "abc", "limit": 2})
            assert '"abc"' in str(out)
            try:
                await server.call_tool("n8n_create_workflow_from_code", {"code": "boom", "name": "x"})
                raise AssertionError("an n8n error must surface")
            except Exception as e:  # noqa: BLE001
                assert "invalid SDK code" in str(e)
            return await n8n.N8nBridge(FastMCP("x"), url, "wrong").sync()

        assert run(go()) == 0  # a wrong token registers nothing and does not raise
    finally:
        proc.terminate()
        proc.wait()
