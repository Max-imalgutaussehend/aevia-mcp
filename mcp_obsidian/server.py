"""Read-only MCP server for an Obsidian vault (streamable HTTP), protected by OAuth tokens from Authentik.

claude.ai discovers the authorization server through /.well-known/oauth-protected-resource, signs the user in at Authentik and
then sends `Authorization: Bearer <token>` with every call. Each token is checked by RFC 7662 introspection at Authentik.
"""
from __future__ import annotations

import hashlib
import os
import time

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .vault import Vault

PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://mcp.maxrommel.de").rstrip("/")
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "https://authentik.maxrommel.de/application/o/obsidian-mcp/")
INTROSPECT_URL = os.environ.get("OIDC_INTROSPECT_URL", "https://authentik.maxrommel.de/application/o/introspect/")
CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "obsidian-mcp")
CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
VAULT_DIR = os.environ.get("VAULT_DIR", "/data/vault")
VAULT_REMOTE = os.environ.get("VAULT_GIT_URL") or None
EXCLUDE = [d for d in os.environ.get("EXCLUDE_DIRS", "Privat").split(",") if d.strip()]

vault = Vault(VAULT_DIR, exclude=EXCLUDE, remote=VAULT_REMOTE)

mcp = FastMCP(
    "obsidian",
    instructions=("Read-only access to Max's Obsidian vault. Start with vault_overview, then search_notes or list_notes; "
                  "read_note returns one note with its links and tags. Never invent note content: quote what the tools return."),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),  # remote server; every call is authenticated
)
RO = {"readOnlyHint": True, "openWorldHint": False}


@mcp.tool(annotations=RO)
def vault_overview() -> dict:
    """Counts of notes per top-level folder, the git head and the last sync status. Call this first."""
    return vault.overview()


@mcp.tool(annotations=RO)
def list_notes(folder: str = "", limit: int = 200) -> list[str]:
    """List note paths, optionally inside one folder (for example "Uni")."""
    return [str(p) for p in vault.notes(folder)][: max(1, min(limit, 1000))]


@mcp.tool(annotations=RO)
def read_note(path: str) -> dict:
    """Read one note by its path (as returned by list_notes or search_notes): frontmatter, content, wiki links and tags."""
    try:
        return vault.read(path)
    except (FileNotFoundError, ValueError):
        return {"error": f"no such note: {path}"}


@mcp.tool(annotations=RO)
def search_notes(query: str, folder: str = "", limit: int = 20) -> list[dict]:
    """Search all notes for the words of the query (every word must appear; title matches rank higher). Returns paths with snippets."""
    return [{"path": h.path, "score": h.score, "snippets": h.snippets} for h in vault.search(query, folder, max(1, min(limit, 50)))]


@mcp.tool(annotations=RO)
def backlinks(path: str) -> list[str]:
    """Notes that link to the given note with a [[wiki link]]."""
    return vault.backlinks(path)


@mcp.tool(annotations=RO)
def list_tags() -> dict:
    """All #tags in the vault with the number of notes that use them."""
    return vault.tags()


@mcp.tool(annotations=RO)
def notes_by_tag(tag: str) -> list[str]:
    """Paths of the notes that carry a tag (with or without the leading #)."""
    return vault.by_tag(tag)


@mcp.tool(annotations=RO)
def recent_notes(limit: int = 20) -> list[dict]:
    """The most recently changed notes according to the git history."""
    return vault.recent(max(1, min(limit, 100)))


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
