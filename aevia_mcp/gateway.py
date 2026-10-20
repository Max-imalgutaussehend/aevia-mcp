"""The AI gateway (FreeLLMAPI, OpenAI-compatible) and ntfy push notifications."""
from __future__ import annotations

import os

import httpx

GATEWAY = os.environ.get("GATEWAY_URL", "http://freellmapi.platform.svc.cluster.local:3001")
GATEWAY_KEY = os.environ.get("GATEWAY_API_KEY", "")
NTFY = os.environ.get("NTFY_URL", "http://ntfy.platform.svc.cluster.local:80")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "aevia")
NTFY_USER, NTFY_PASSWORD = os.environ.get("NTFY_USER", ""), os.environ.get("NTFY_PASSWORD", "")


class Gateway:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.c = client or httpx.AsyncClient(timeout=90)

    async def models(self) -> dict:
        if not GATEWAY_KEY:
            return {"error": "gateway key not configured (secret aevia-mcp-gateway)"}
        r = await self.c.get(f"{GATEWAY}/v1/models", headers={"Authorization": f"Bearer {GATEWAY_KEY}"})
        return {"models": [m["id"] for m in r.json().get("data", [])]} if r.status_code == 200 else {"error": f"{r.status_code} {r.text[:200]}"}

    async def ask(self, prompt: str, model: str = "auto", max_tokens: int = 800, system: str | None = None) -> dict:
        if not GATEWAY_KEY:
            return {"error": "gateway key not configured (secret aevia-mcp-gateway)"}
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        r = await self.c.post(f"{GATEWAY}/v1/chat/completions", headers={"Authorization": f"Bearer {GATEWAY_KEY}"},
                              json={"model": model, "messages": msgs, "max_tokens": max(1, min(max_tokens, 4000))})
        if r.status_code != 200:
            return {"error": f"{r.status_code} {r.text[:300]}"}
        return {"answer": r.json()["choices"][0]["message"]["content"], "routed_via": r.headers.get("x-routed-via")}


class Notify:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.c = client or httpx.AsyncClient(timeout=15)

    async def send(self, message: str, title: str = "Aevia", priority: int = 3) -> dict:
        auth = (NTFY_USER, NTFY_PASSWORD) if NTFY_USER else None
        r = await self.c.post(f"{NTFY}/{NTFY_TOPIC}", content=message[:4000].encode(), auth=auth,
                              headers={"Title": title[:100], "Priority": str(max(1, min(priority, 5)))})
        return {"sent": r.status_code == 200, "status": r.status_code}
