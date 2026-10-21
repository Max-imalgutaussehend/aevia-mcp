"""End-to-end: real uvicorn process, a stub for Authentik's introspection endpoint, a temporary git vault."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from tests.test_vault import make_vault


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Introspect(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        good = "token=good" in body and self.headers.get("Authorization", "").startswith("Basic ")
        out = json.dumps({"active": good, "client_id": "aevia-mcp"} if good else {"active": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):  # silence
        pass


def rpc(client, token, method, params=None, id_=1):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def test_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        vault = make_vault(tmp)
        stub_port, port = free_port(), free_port()
        stub = HTTPServer(("127.0.0.1", stub_port), Introspect)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        env = {**os.environ, "VAULT_DIR": str(vault.root), "EXCLUDE_DIRS": "Privat", "PUBLIC_URL": "http://127.0.0.1",
               "OIDC_INTROSPECT_URL": f"http://127.0.0.1:{stub_port}/introspect", "OIDC_CLIENT_SECRET": "s3cret", "SYNC_SECONDS": "3600", "INTERNAL_TOKEN": "cluster-internal-token"}
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "aevia_mcp.server:app", "--port", str(port)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
            for _ in range(50):
                try:
                    if c.get("/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.2)
            assert c.get("/healthz").text == "ok"
            meta = c.get("/.well-known/oauth-protected-resource").json()
            assert meta["authorization_servers"][0].endswith("/application/o/aevia-mcp/")

            r = rpc(c, None, "initialize")
            assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
            assert rpc(c, "bad", "initialize").status_code == 401

            # in-cluster clients (Jarvis) use the static token; through Cloudflare it must not work
            init0 = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
            assert rpc(c, "cluster-internal-token", "initialize", init0).status_code == 200
            via_cf = c.post("/mcp", headers={"Authorization": "Bearer cluster-internal-token", "Cf-Connecting-Ip": "203.0.113.9",
                                             "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                            json={"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": init0})
            assert via_cf.status_code == 401

            init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
            assert rpc(c, "good", "initialize", init).status_code == 200
            tools = rpc(c, "good", "tools/list", id_=2).json()["result"]["tools"]
            assert {t["name"] for t in tools} >= {"search_notes", "read_note", "vault_overview", "aevia_overview", "pod_logs", "prometheus_query", "ntfy_send"}
            res = rpc(c, "good", "tools/call", {"name": "search_notes", "arguments": {"query": "bayes"}}, 3).json()["result"]
            text = json.dumps(res)
            assert "Uni/Stats.md" in text and "Secret" not in text
            res = rpc(c, "good", "tools/call", {"name": "read_note", "arguments": {"path": "Privat/Secret.md"}}, 4).json()["result"]
            assert "no such note" in json.dumps(res)
        finally:
            proc.terminate()
            stub.shutdown()
