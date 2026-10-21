"""A stand-in for n8n's MCP server: stateless streamable HTTP answering with SSE like n8n, bearer token required. Run: uvicorn tests.fake_n8n:app"""
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

mcp = FastMCP("fake-n8n", stateless_http=True)


@mcp.tool()
def search_workflows(query: str = "", limit: int = 10) -> str:
    """Search for workflows."""
    return f'{{"query": "{query}", "limit": {limit}}}'


@mcp.tool()
def create_workflow_from_code(code: str, name: str) -> str:
    """Create a workflow from SDK code."""
    if "boom" in code:
        raise ValueError("invalid SDK code")
    return f'{{"id": "wf1", "name": "{name}", "code_chars": {len(code)}}}'


class Auth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.headers.get("authorization") != "Bearer good":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(Auth)
