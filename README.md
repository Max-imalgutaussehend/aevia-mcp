# aevia-mcp

The MCP server of the Aevia platform: one private endpoint through which Claude (claude.ai, Claude Code) can look at the
platform and at the Obsidian vault. Streamable HTTP, OAuth through the identity provider (Authentik), read-only except for
one notification tool.

| Group | Tools |
|---|---|
| Platform | `aevia_overview`, `cluster_nodes`, `list_pods`, `pod_logs`, `cluster_events`, `workloads`, `argocd_apps`, `argocd_app` |
| Metrics and logs | `prometheus_query`, `prometheus_range`, `loki_query` |
| Vault (Obsidian, from a git clone) | `vault_overview`, `list_notes`, `read_note`, `search_notes`, `backlinks`, `list_tags`, `notes_by_tag`, `recent_notes` |
| AI gateway | `llm_models`, `llm_ask` (OpenAI-compatible FreeLLMAPI) |
| Notifications | `ntfy_send` (the only tool that sends anything) |

## Safety
- **Private:** in the Aevia platform the host sits behind Cloudflare Access; only the operator's login and Anthropic's published outbound range reach it, and every `/mcp` call still needs an OAuth token (RFC 7662 introspection).
- **Read-only cluster access:** a dedicated service account that can list pods, nodes, events, workloads, metrics and Argo CD applications and read pod logs. No secrets, no exec, no writes.
- **Vault privacy:** folders in `EXCLUDE_DIRS` (default `Privat`) and hidden folders are never listed, searched or read.

## Configuration (environment)
`PUBLIC_URL`, `OIDC_ISSUER`, `OIDC_INTROSPECT_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` (sign-in); `VAULT_GIT_URL`, `EXCLUDE_DIRS` (vault);
`PROMETHEUS_URL`, `LOKI_URL` (observability); `GATEWAY_URL`, `GATEWAY_API_KEY` (AI gateway); `NTFY_URL`, `NTFY_TOPIC`, `NTFY_USER`, `NTFY_PASSWORD` (notifications).
Tools whose backend is not configured answer with an error instead of failing the server.

## Develop
```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q tests      # unit tests, mocked backends, and an end-to-end test against a stub identity provider
```
