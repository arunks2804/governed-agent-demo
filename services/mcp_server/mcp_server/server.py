"""MCP server exposing governed data tools.

The server holds no credentials and makes no authorization decisions: it
forwards the caller's bearer token and trace id to the trusted data service,
which verifies identity, validates SQL, asks OPA, and executes. Tool results
include the enforcement trail so agents (and the UI) can see what acted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import httpx
import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

DATA_SERVICE_URL = os.environ.get("DATA_SERVICE_URL", "http://data-service:8080")
TRACE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_SQL_CHARS = 20000

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("mcp_server")

mcp = MCPServer(
    "governed-data",
    instructions=(
        "Governed access to your organisation's data. Use list_datasets and describe_dataset "
        "to discover data, then query_data with a single read-only SELECT. Your identity and "
        "tenant come from your credentials; you cannot choose them."
    ),
)
_http = httpx.AsyncClient(base_url=DATA_SERVICE_URL, timeout=httpx.Timeout(30.0))


def _forward_headers(ctx: Context) -> dict[str, str]:
    incoming = ctx.headers or {}
    headers: dict[str, str] = {}
    authorization = incoming.get("authorization")
    if authorization:
        headers["Authorization"] = authorization
    trace = incoming.get("x-trace-id", "")
    if TRACE_RE.match(trace):
        headers["X-Trace-Id"] = trace
    return headers


async def _call(
    ctx: Context, tool: str, method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    headers = _forward_headers(ctx)
    started = time.monotonic()
    try:
        response = await _http.request(method, path, json=payload, headers=headers)
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info(
            json.dumps(
                {
                    "service": "mcp-server",
                    "tool": tool,
                    "outcome": "error",
                    "trace_id": headers.get("X-Trace-Id"),
                    "error": type(exc).__name__,
                }
            )
        )
        raise ToolError(
            json.dumps(
                {"ok": False, "error": "data_service_unavailable", "message": "governed data service unreachable"}
            )
        ) from exc
    logger.info(
        json.dumps(
            {
                "service": "mcp-server",
                "tool": tool,
                "status": response.status_code,
                "trace_id": body.get("trace_id") if isinstance(body, dict) else None,
                "had_credentials": "Authorization" in headers,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        )
    )
    if response.status_code >= 400:
        # Surface the structured denial (reason + enforcement trail) to the agent.
        raise ToolError(json.dumps(body))
    return body


@mcp.tool()
async def list_datasets(ctx: Context) -> str:
    """List the datasets you are permitted to see."""
    return json.dumps(await _call(ctx, "list_datasets", "GET", "/v1/datasets"))


@mcp.tool()
async def describe_dataset(name: str, ctx: Context) -> str:
    """Describe a dataset's columns and the supported SQL scope."""
    if not re.fullmatch(r"[a-z_]{1,64}", name):
        raise ToolError(json.dumps({"ok": False, "error": "invalid_dataset_name"}))
    return json.dumps(await _call(ctx, "describe_dataset", "GET", f"/v1/datasets/{name}"))


@mcp.tool()
async def query_data(sql: str, ctx: Context) -> str:
    """Run ONE read-only SQL SELECT against an approved dataset (e.g. orders).
    Results are filtered to your tenant and capped by policy."""
    if len(sql) > MAX_SQL_CHARS:
        raise ToolError(json.dumps({"ok": False, "error": "sql_too_large"}))
    return json.dumps(await _call(ctx, "query_data", "POST", "/v1/query", {"sql": sql}))


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_app() -> Any:
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["mcp-server:8000", "mcp-server", "127.0.0.1:8000", "localhost:8000"],
            allowed_origins=[],
        ),
    )
    app.add_route("/healthz", healthz, methods=["GET"])
    return app


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
