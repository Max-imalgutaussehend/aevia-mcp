# Obsidian MCP server: read-only, streamable HTTP, OAuth via Authentik. Non-root, vault cloned into /data (emptyDir in the cluster).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --create-home --home-dir /home/mcp mcp
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mcp_obsidian ./mcp_obsidian
ENV HOME=/home/mcp PYTHONUNBUFFERED=1 VAULT_DIR=/data/vault
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "mcp_obsidian.server:app", "--host", "0.0.0.0", "--port", "8000"]
