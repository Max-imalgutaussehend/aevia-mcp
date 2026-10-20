"""Prometheus and Loki queries (read-only)."""
from __future__ import annotations

import os
import time

import httpx

PROM = os.environ.get("PROMETHEUS_URL", "http://kps-prometheus.observability.svc.cluster.local:9090")
LOKI = os.environ.get("LOKI_URL", "http://loki.observability.svc.cluster.local:3100")
MAX_SERIES = 40


class Observability:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.c = client or httpx.AsyncClient(timeout=20)

    @staticmethod
    def _series(result: list[dict]) -> list[dict]:
        out = []
        for s in result[:MAX_SERIES]:
            item = {"metric": s.get("metric", {})}
            if "value" in s:
                item["value"] = s["value"][1]
            if "values" in s:
                vals = s["values"]
                item["points"] = len(vals)
                item["first"], item["last"] = vals[0][1], vals[-1][1]
                nums = [float(v[1]) for v in vals if v[1] not in ("NaN", "+Inf", "-Inf")]
                if nums:
                    item["min"], item["max"] = min(nums), max(nums)
            out.append(item)
        return out

    async def prometheus_query(self, query: str) -> dict:
        r = await self.c.get(f"{PROM}/api/v1/query", params={"query": query})
        data = r.json()
        if data.get("status") != "success":
            return {"error": data.get("error", "query failed")}
        return {"result_type": data["data"]["resultType"], "series": self._series(data["data"]["result"]), "truncated": len(data["data"]["result"]) > MAX_SERIES}

    async def prometheus_range(self, query: str, minutes: int = 60, step_seconds: int = 60) -> dict:
        end = time.time()
        r = await self.c.get(f"{PROM}/api/v1/query_range", params={"query": query, "start": end - minutes * 60, "end": end,
                                                                  "step": max(15, step_seconds)})
        data = r.json()
        if data.get("status") != "success":
            return {"error": data.get("error", "query failed")}
        return {"series": self._series(data["data"]["result"]), "truncated": len(data["data"]["result"]) > MAX_SERIES}

    async def loki_query(self, logql: str, minutes: int = 30, limit: int = 100) -> dict:
        end = int(time.time() * 1e9)
        r = await self.c.get(f"{LOKI}/loki/api/v1/query_range", params={"query": logql, "start": end - minutes * 60 * 10**9, "end": end,
                                                                       "limit": max(1, min(limit, 500)), "direction": "backward"})
        if r.status_code != 200:
            return {"error": r.text[:300]}
        lines = []
        for stream in r.json()["data"]["result"]:
            for ts, line in stream["values"]:
                lines.append((int(ts), stream["stream"].get("pod") or stream["stream"].get("app") or "", line[:400]))
        lines.sort(reverse=True)
        return {"lines": [{"pod": p, "line": line} for _, p, line in lines[:limit]]}
