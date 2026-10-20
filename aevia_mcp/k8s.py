"""Read-only view of the cluster and Argo CD through the in-cluster API. The service account may list pods, nodes, events,
workloads, metrics and Argo CD applications and read pod logs. It has no access to secrets and cannot exec or change anything."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MAX_LOG = 20_000


def _age(ts: str | None) -> str:
    if not ts:
        return ""
    secs = int((datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit}"
    return f"{secs}s"


def _mem(q: str | None) -> int:
    """Kubernetes quantity (Ki, Mi, Gi, n, u, m or plain) in MiB; cpu quantities are handled by _cpu."""
    if not q:
        return 0
    for suf, mul in (("Ki", 1 / 1024), ("Mi", 1), ("Gi", 1024), ("Ti", 1024 * 1024)):
        if q.endswith(suf):
            return int(float(q[: -len(suf)]) * mul)
    return int(float(q) / (1024 * 1024))


def _cpu(q: str | None) -> float:
    if not q:
        return 0.0
    if q.endswith("n"):
        return float(q[:-1]) / 1e9
    if q.endswith("u"):
        return float(q[:-1]) / 1e6
    if q.endswith("m"):
        return float(q[:-1]) / 1e3
    return float(q)


class Kube:
    def __init__(self, client: httpx.AsyncClient | None = None):
        if client is not None:
            self.c = client
            return
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        token = (SA / "token").read_text().strip() if (SA / "token").exists() else ""
        verify = str(SA / "ca.crt") if (SA / "ca.crt").exists() else True
        self.c = httpx.AsyncClient(base_url=f"https://{host}:{port}", headers={"Authorization": f"Bearer {token}"}, verify=verify, timeout=15)

    async def _get(self, path: str, **params):
        r = await self.c.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r

    async def nodes(self) -> list[dict]:
        nodes = (await self._get("/api/v1/nodes")).json()["items"]
        try:
            usage = {m["metadata"]["name"]: m["usage"] for m in (await self._get("/apis/metrics.k8s.io/v1beta1/nodes")).json()["items"]}
        except httpx.HTTPError:
            usage = {}
        out = []
        for n in nodes:
            name = n["metadata"]["name"]
            ready = next((c["status"] for c in n["status"]["conditions"] if c["type"] == "Ready"), "Unknown")
            alloc = n["status"]["allocatable"]
            u = usage.get(name, {})
            out.append({
                "name": name, "ready": ready == "True", "kubelet": n["status"]["nodeInfo"]["kubeletVersion"],
                "cpu_cores": _cpu(alloc["cpu"]), "cpu_used": round(_cpu(u.get("cpu")), 2),
                "memory_mib": _mem(alloc["memory"]), "memory_used_mib": _mem(u.get("memory")),
            })
        return out

    async def pods(self, namespace: str | None = None, only_problems: bool = False) -> list[dict]:
        path = f"/api/v1/namespaces/{namespace}/pods" if namespace else "/api/v1/pods"
        items = (await self._get(path)).json()["items"]
        try:
            mpath = f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods" if namespace else "/apis/metrics.k8s.io/v1beta1/pods"
            usage = {(m["metadata"]["namespace"], m["metadata"]["name"]): sum(_mem(c["usage"]["memory"]) for c in m["containers"])
                     for m in (await self._get(mpath)).json()["items"]}
        except httpx.HTTPError:
            usage = {}
        out = []
        for p in items:
            st = p["status"]
            cs = st.get("containerStatuses", [])
            ready = f'{sum(1 for c in cs if c.get("ready"))}/{len(p["spec"]["containers"])}'
            restarts = sum(c.get("restartCount", 0) for c in cs)
            waiting = next((c["state"]["waiting"]["reason"] for c in cs if "waiting" in c.get("state", {})), None)
            phase = waiting or st.get("phase", "")
            problem = phase not in ("Running", "Succeeded") or (phase == "Running" and ready.split("/")[0] != ready.split("/")[1])
            if only_problems and not problem:
                continue
            key = (p["metadata"]["namespace"], p["metadata"]["name"])
            out.append({"namespace": key[0], "name": key[1], "status": phase, "ready": ready, "restarts": restarts,
                        "age": _age(p["metadata"].get("creationTimestamp")), "memory_mib": usage.get(key)})
        return out

    async def logs(self, namespace: str, pod: str, container: str | None = None, tail: int = 200, since_minutes: int | None = None) -> str:
        r = await self._get(f"/api/v1/namespaces/{namespace}/pods/{pod}/log", container=container, tailLines=max(1, min(tail, 2000)),
                            sinceSeconds=since_minutes * 60 if since_minutes else None)
        text = r.text
        return text[-MAX_LOG:] if len(text) > MAX_LOG else text

    async def events(self, namespace: str | None = None, warnings_only: bool = True, limit: int = 40) -> list[dict]:
        path = f"/api/v1/namespaces/{namespace}/events" if namespace else "/api/v1/events"
        items = (await self._get(path)).json()["items"]
        items = [e for e in items if not warnings_only or e.get("type") == "Warning"]
        items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
        return [{"namespace": e["metadata"]["namespace"], "object": f'{e["involvedObject"]["kind"]}/{e["involvedObject"]["name"]}',
                 "reason": e.get("reason"), "message": (e.get("message") or "")[:300], "count": e.get("count", 1),
                 "last": e.get("lastTimestamp") or e.get("eventTime")} for e in items[:limit]]

    async def workloads(self, namespace: str | None = None) -> list[dict]:
        out = []
        for kind, path in (("Deployment", "deployments"), ("StatefulSet", "statefulsets"), ("DaemonSet", "daemonsets")):
            base = f"/apis/apps/v1/namespaces/{namespace}/{path}" if namespace else f"/apis/apps/v1/{path}"
            for w in (await self._get(base)).json()["items"]:
                s = w["status"]
                want = w["spec"].get("replicas", s.get("desiredNumberScheduled", 1))
                ready = s.get("readyReplicas", s.get("numberReady", 0))
                out.append({"kind": kind, "namespace": w["metadata"]["namespace"], "name": w["metadata"]["name"], "ready": f"{ready}/{want}"})
        return out

    async def argocd_apps(self) -> list[dict]:
        items = (await self._get("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications")).json()["items"]
        out = []
        for a in items:
            st = a.get("status", {})
            src = a["spec"].get("source") or (a["spec"].get("sources") or [{}])[0]
            out.append({"name": a["metadata"]["name"], "sync": st.get("sync", {}).get("status"), "health": st.get("health", {}).get("status"),
                        "revision": (st.get("sync", {}).get("revision") or "")[:8], "target": src.get("targetRevision"),
                        "last_sync": (st.get("operationState") or {}).get("finishedAt")})
        return sorted(out, key=lambda x: x["name"])

    async def argocd_app(self, name: str) -> dict:
        a = (await self._get(f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/{name}")).json()
        st = a.get("status", {})
        return {
            "name": name, "sync": st.get("sync", {}).get("status"), "health": st.get("health", {}).get("status"),
            "conditions": [{"type": c.get("type"), "message": (c.get("message") or "")[:300]} for c in st.get("conditions", [])],
            "operation": {k: (st.get("operationState") or {}).get(k) for k in ("phase", "message", "finishedAt")},
            "not_synced_or_unhealthy": [f'{r.get("kind")}/{r.get("name")} ({r.get("status")}, {(r.get("health") or {}).get("status")})'
                                        for r in st.get("resources", []) if r.get("status") != "Synced" or (r.get("health") or {}).get("status") not in (None, "Healthy")],
        }
