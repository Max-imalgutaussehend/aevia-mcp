"""Aevia MCP server: one private endpoint for the platform (streamable HTTP), protected by OAuth tokens from Authentik.

Tools: the Obsidian vault (git clone, read-only), the cluster and Argo CD (read-only), Prometheus and Loki, the AI gateway and ntfy.

claude.ai discovers the authorization server through /.well-known/oauth-protected-resource, signs the user in at Authentik and
then sends `Authorization: Bearer <token>` with every call. Each token is checked by RFC 7662 introspection at Authentik.
"""
from __future__ import annotations

import hashlib
import os
import time

import anyio

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .gateway import Gateway, Notify
from .k8s import Kube
from .observability import Observability
from .vault import Vault

PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://mcp.maxrommel.de").rstrip("/")
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "https://authentik.maxrommel.de/application/o/aevia-mcp/")
INTROSPECT_URL = os.environ.get("OIDC_INTROSPECT_URL", "https://authentik.maxrommel.de/application/o/introspect/")
CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "aevia-mcp")
CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
VAULT_DIR = os.environ.get("VAULT_DIR", "/data/vault")
VAULT_REMOTE = os.environ.get("VAULT_GIT_URL") or None
EXCLUDE = [d for d in os.environ.get("EXCLUDE_DIRS", "Privat").split(",") if d.strip()]

vault = Vault(VAULT_DIR, exclude=EXCLUDE, remote=VAULT_REMOTE)

mcp = FastMCP(
    "aevia",
    instructions=("Aevia platform tools. aevia_overview first for the state of the cluster. Vault tools read Max's Obsidian notes "
                  "(vault_overview, search_notes, read_note). Cluster tools are read-only (pods, events, logs, Argo CD apps). "
                  "prometheus_query and loki_query answer metric and log questions. llm_ask calls the AI gateway; ntfy_send pushes a "
                  "notification to Max's phone. Quote what the tools return; never invent results."),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),  # remote server; every call is authenticated
)
kube, obs, gateway, notify = Kube() if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token") else None, Observability(), Gateway(), Notify()
RO = {"readOnlyHint": True, "openWorldHint": False}


async def _run(fn, *a):
    return await anyio.to_thread.run_sync(fn, *a)


def _err(e: Exception) -> dict:
    return {"error": f"{type(e).__name__}: {str(e)[:300]}"}


# --- vault --------------------------------------------------------------------------------------------------------
@mcp.tool(annotations=RO)
async def vault_overview() -> dict:
    """Obsidian vault: notes per top-level folder, the git head and the last sync status."""
    return await _run(vault.overview)


@mcp.tool(annotations=RO)
async def list_notes(folder: str = "", limit: int = 200) -> list[str]:
    """List note paths of the vault, optionally inside one folder (for example "Uni")."""
    return [str(p) for p in (await _run(vault.notes, folder))][: max(1, min(limit, 1000))]


@mcp.tool(annotations=RO)
async def read_note(path: str) -> dict:
    """Read one vault note by path (from list_notes or search_notes): frontmatter, content, wiki links and tags."""
    try:
        return await _run(vault.read, path)
    except (FileNotFoundError, ValueError):
        return {"error": f"no such note: {path}"}


@mcp.tool(annotations=RO)
async def search_notes(query: str, folder: str = "", limit: int = 20) -> list[dict]:
    """Search the vault for the words of the query (every word must appear; title matches rank higher). Returns paths with snippets."""
    hits = await _run(vault.search, query, folder, max(1, min(limit, 50)))
    return [{"path": h.path, "score": h.score, "snippets": h.snippets} for h in hits]


@mcp.tool(annotations=RO)
async def backlinks(path: str) -> list[str]:
    """Vault notes that link to the given note with a [[wiki link]]."""
    return await _run(vault.backlinks, path)


@mcp.tool(annotations=RO)
async def list_tags() -> dict:
    """All #tags in the vault with the number of notes that use them."""
    return await _run(vault.tags)


@mcp.tool(annotations=RO)
async def notes_by_tag(tag: str) -> list[str]:
    """Paths of the vault notes that carry a tag (with or without the leading #)."""
    return await _run(vault.by_tag, tag)


@mcp.tool(annotations=RO)
async def recent_notes(limit: int = 20) -> list[dict]:
    """The most recently changed vault notes according to the git history."""
    return await _run(vault.recent, max(1, min(limit, 100)))


# --- cluster and Argo CD -------------------------------------------------------------------------------------------
@mcp.tool(annotations=RO)
async def aevia_overview() -> dict:
    """State of the platform in one call: node load, Argo CD applications that are not synced and healthy, pods with problems, recent warnings."""
    if kube is None:
        return {"error": "not running inside the cluster"}
    try:
        apps = await kube.argocd_apps()
        bad = [a for a in apps if a["sync"] != "Synced" or a["health"] != "Healthy"]
        return {"nodes": await kube.nodes(), "argocd": {"apps": len(apps), "not_ok": bad},
                "pods_with_problems": await kube.pods(only_problems=True), "recent_warnings": await kube.events(limit=10)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
async def cluster_nodes() -> list[dict] | dict:
    """Nodes with readiness, CPU and memory (allocatable and in use)."""
    try:
        return await kube.nodes()
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
async def list_pods(namespace: str = "", only_problems: bool = False) -> list[dict] | dict:
    """Pods with status, readiness, restarts, age and memory. Leave namespace empty for all; only_problems hides healthy pods."""
    try:
        return await kube.pods(namespace or None, only_problems)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
async def pod_logs(namespace: str, pod: str, container: str = "", tail: int = 200, since_minutes: int = 0) -> str | dict:
    """Last lines of a pod's log (at most 20,000 characters)."""
    try:
        return await kube.logs(namespace, pod, container or None, tail, since_minutes or None)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
async def cluster_events(namespace: str = "", warnings_only: bool = True, limit: int = 40) -> list[dict] | dict:
    """Recent Kubernetes events, newest first (warnings only by default)."""
    try:
        return await kube.events(namespace or None, warnings_only, max(1, min(limit, 100)))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
async def workloads(namespace: str = "") -> list[dict] | dict:
    """Deployments, StatefulSets and DaemonSets with ready/desired replicas."""
    try:
        return await kube.workloads(namespace or None)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
async def argocd_apps() -> list[dict] | dict:
    """All Argo CD applications with sync status, health, revision and last sync."""
    try:
        return await kube.argocd_apps()
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
async def argocd_app(name: str) -> dict:
    """One Argo CD application: conditions, last operation and the resources that are not synced or not healthy."""
    try:
        return await kube.argocd_app(name)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --- metrics and logs ----------------------------------------------------------------------------------------------
@mcp.tool(annotations=RO)
async def prometheus_query(query: str) -> dict:
    """Run an instant PromQL query, for example `sum by (namespace) (container_memory_working_set_bytes)`."""
    try:
        return await obs.prometheus_query(query)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
async def prometheus_range(query: str, minutes: int = 60, step_seconds: int = 60) -> dict:
    """Run a PromQL range query over the last N minutes; returns first, last, min and max per series."""
    try:
        return await obs.prometheus_range(query, minutes, step_seconds)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
async def loki_query(logql: str, minutes: int = 30, limit: int = 100) -> dict:
    """Search logs with LogQL, for example `{namespace="platform"} |= "error"`; newest lines first."""
    try:
        return await obs.loki_query(logql, minutes, limit)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --- AI gateway and notifications ----------------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def llm_models() -> dict:
    """Models available through the AI gateway."""
    try:
        return await gateway.models()
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def llm_ask(prompt: str, model: str = "auto", max_tokens: int = 800, system: str = "") -> dict:
    """Ask a model through the AI gateway (free-tier models, quality varies). Use it for cheap side questions, not for important answers."""
    try:
        return await gateway.ask(prompt, model, max_tokens, system or None)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
async def ntfy_send(message: str, title: str = "Aevia", priority: int = 3) -> dict:
    """Send a push notification to Max's phone (priority 1 to 5). The only tool that sends anything."""
    try:
        return await notify.send(message, title, priority)
    except Exception as e:  # noqa: BLE001
        return _err(e)


class OAuthGate(BaseHTTPMiddleware):
    """Every /mcp call needs a live token that Authentik issued to this server's client."""

    _cache: dict[str, float] = {}

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token or not await self._valid(token):
            return JSONResponse(
                {"error": "invalid_token"}, status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource"'})
        return await call_next(request)

    async def _valid(self, token: str) -> bool:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        if self._cache.get(key, 0) > now:
            return True
        if not CLIENT_SECRET:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(INTROSPECT_URL, data={"token": token}, auth=(CLIENT_ID, CLIENT_SECRET))
            data = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return False
        ok = bool(data.get("active")) and data.get("client_id", CLIENT_ID) == CLIENT_ID
        if ok:
            self._cache[key] = min(now + 60, float(data.get("exp", now + 60)))
        return ok


async def resource_metadata(_: Request) -> JSONResponse:
    return JSONResponse({
        "resource": f"{PUBLIC_URL}/mcp",
        "authorization_servers": [OIDC_ISSUER],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid", "email", "profile"],
    })


async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def build_app():
    app = mcp.streamable_http_app()  # serves /mcp
    app.add_route("/.well-known/oauth-protected-resource", resource_metadata)
    app.add_route("/.well-known/oauth-protected-resource/mcp", resource_metadata)
    app.add_route("/healthz", healthz)
    app.add_middleware(OAuthGate)
    return app


app = build_app()
vault.start_background_sync(int(os.environ.get("SYNC_SECONDS", "120")))
