# mcp-obsidian

A small, read-only [MCP](https://modelcontextprotocol.io) server that lets claude.ai (or any MCP client) ask questions about an
Obsidian vault that lives in a git repository. It clones the vault, keeps it fresh with `git pull`, and exposes search, read,
backlinks, tags and recent changes as tools. Nothing can be written.

- **Auth:** OAuth. The server publishes `/.well-known/oauth-protected-resource`, claude.ai signs you in at your identity provider
  (Authentik in the Aevia platform) and sends a bearer token; every call is checked by token introspection.
- **Privacy:** folders listed in `EXCLUDE_DIRS` (default `Privat`) and hidden folders (`.obsidian`, `.git`, `.trash`) are never listed, searched or read.
- **Tools:** `vault_overview`, `list_notes`, `read_note`, `search_notes`, `backlinks`, `list_tags`, `notes_by_tag`, `recent_notes`.

## Configuration (environment)
| Variable | Meaning |
|---|---|
| `VAULT_GIT_URL` | clone URL of the vault repo, including a read token |
| `PUBLIC_URL` | public base URL of this server (default `https://mcp.maxrommel.de`) |
| `OIDC_ISSUER` | issuer of the authorization server advertised to clients |
| `OIDC_INTROSPECT_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | token introspection |
| `EXCLUDE_DIRS` | comma-separated top-level folders to hide |

## Develop
```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q tests      # unit tests plus an end-to-end test against a stub identity provider
```
