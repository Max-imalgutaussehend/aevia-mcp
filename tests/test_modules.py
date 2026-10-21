import asyncio
import json

import httpx

from aevia_mcp import gateway as gw
from aevia_mcp.gateway import Gateway, Notify
from aevia_mcp.k8s import Kube, _cpu, _mem
from aevia_mcp.observability import Observability


def client(handler, **kw):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=kw.get("base_url", "http://k"))


def test_quantities():
    assert _mem("131072Ki") == 128 and _mem("2Gi") == 2048 and _mem("268435456") == 256
    assert _cpu("250m") == 0.25 and _cpu("1500000n") == 0.0015 and _cpu("2") == 2.0


def test_k8s_pods_nodes_and_problems():
    def h(req: httpx.Request):
        p = req.url.path
        if p == "/api/v1/pods":
            return httpx.Response(200, json={"items": [
                {"metadata": {"namespace": "apps", "name": "ok", "creationTimestamp": "2026-10-20T10:00:00Z"}, "spec": {"containers": [{}]},
                 "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0, "state": {"running": {}}}]}},
                {"metadata": {"namespace": "apps", "name": "bad", "creationTimestamp": "2026-10-20T10:00:00Z"}, "spec": {"containers": [{}]},
                 "status": {"phase": "Running", "containerStatuses": [{"ready": False, "restartCount": 4, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}]})
        if p == "/apis/metrics.k8s.io/v1beta1/pods":
            return httpx.Response(200, json={"items": [{"metadata": {"namespace": "apps", "name": "ok"}, "containers": [{"usage": {"memory": "64Mi"}}]}]})
        if p == "/api/v1/nodes":
            return httpx.Response(200, json={"items": [{"metadata": {"name": "n1"}, "status": {
                "conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"kubeletVersion": "v1"},
                "allocatable": {"cpu": "4", "memory": "8Gi"}}}]})
        if p == "/apis/metrics.k8s.io/v1beta1/nodes":
            return httpx.Response(200, json={"items": [{"metadata": {"name": "n1"}, "usage": {"cpu": "500m", "memory": "4Gi"}}]})
        return httpx.Response(404)

    k = Kube(client(h))
    pods = asyncio.run(k.pods())
    assert [(p["name"], p["status"], p["memory_mib"]) for p in pods] == [("ok", "Running", 64), ("bad", "CrashLoopBackOff", None)]
    assert [p["name"] for p in asyncio.run(k.pods(only_problems=True))] == ["bad"]
    n = asyncio.run(k.nodes())[0]
    assert n["ready"] and n["memory_mib"] == 8192 and n["memory_used_mib"] == 4096 and n["cpu_used"] == 0.5


def test_k8s_logs_and_argocd():
    def h(req: httpx.Request):
        if req.url.path.endswith("/log"):
            assert req.url.params["tailLines"] == "50"
            return httpx.Response(200, text="x" * 30000)
        return httpx.Response(200, json={"items": [{"metadata": {"name": "data"}, "spec": {"source": {"targetRevision": "main"}},
            "status": {"sync": {"status": "Synced", "revision": "abcdef1234"}, "health": {"status": "Healthy"}}}]})

    k = Kube(client(h))
    assert len(asyncio.run(k.logs("a", "b", tail=50))) == 20000
    app = asyncio.run(k.argocd_apps())[0]
    assert app == {"name": "data", "sync": "Synced", "health": "Healthy", "revision": "abcdef12", "target": "main", "last_sync": None}


def test_prometheus_and_loki():
    def h(req: httpx.Request):
        if req.url.path.startswith("/loki/"):
            return httpx.Response(200, json={"data": {"result": [{"stream": {"k8s_pod_name": "p"}, "values": [["2", "second"], ["1", "first"]]}]}})
        if "/api/v1/query_range" in req.url.path:
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": [
                {"metric": {"ns": "a"}, "values": [[1, "1"], [2, "3"]]}]}})
        if req.url.path.startswith("/api/v1/query"):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {"a": "b"}, "value": [1, "42"]}]}})
        return httpx.Response(404)

    o = Observability(client(h))
    assert asyncio.run(o.prometheus_query("up"))["series"][0]["value"] == "42"
    r = asyncio.run(o.prometheus_range("up", 10))["series"][0]
    assert (r["first"], r["last"], r["min"], r["max"]) == ("1", "3", 1.0, 3.0)
    lq = asyncio.run(o.loki_query('{a="b"}'))["lines"]
    assert [x["line"] for x in lq] == ["second", "first"] and lq[0]["pod"] == "p"


def test_gateway_and_notify(monkeypatch):
    seen = {}

    def h(req: httpx.Request):
        seen[req.url.path] = (req.headers.get("authorization"), req.content)
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]}, headers={"x-routed-via": "groq/x"})
        return httpx.Response(200, json={"data": [{"id": "auto"}]})

    g = Gateway(client(h))
    assert "not configured" in asyncio.run(g.ask("q"))["error"]  # no key: refuses instead of calling out
    monkeypatch.setattr(gw, "GATEWAY_KEY", "k123")
    assert asyncio.run(g.ask("q")) == {"answer": "hi", "routed_via": "groq/x"}
    assert seen["/v1/chat/completions"][0] == "Bearer k123"
    assert asyncio.run(g.models()) == {"models": ["auto"]}

    n = Notify(client(h))
    monkeypatch.setattr(gw, "NTFY_USER", "u")
    monkeypatch.setattr(gw, "NTFY_PASSWORD", "p")
    assert asyncio.run(n.send("done", priority=9)) == {"sent": True, "status": 200}
    assert json.loads(json.dumps(list(seen))) and seen["/aevia"][1] == b"done"
