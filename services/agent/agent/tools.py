"""Tool execution for the agent: MCP data tools and sandboxed Python.

The agent process holds the user's forwarded tokens; the LLM never sees them.
"""

from __future__ import annotations

import json
import time
from contextlib import AsyncExitStack
from typing import Any

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

MCP_TOOLS = ("list_datasets", "describe_dataset", "query_data")
MAX_TOOL_OUTPUT_CHARS = 20000


def _stopped_at(payload: dict[str, Any]) -> str | None:
    for entry in reversed(payload.get("enforcement") or []):
        if entry.get("outcome") in ("deny", "error"):
            return str(entry.get("enforcement_point"))
    return None


class ToolBox:
    def __init__(
        self,
        *,
        mcp_url: str,
        runner_urls: dict[str, str],
        runner_control_token: str,
        data_token: str,
        runner_token: str,
        trace_id: str,
        environment: str = "protected",
        insecure_url: str | None = None,
    ) -> None:
        self.environment = environment
        self._insecure_url = insecure_url
        self._mcp_url = mcp_url
        self._runner_urls = runner_urls
        self._runner_control_token = runner_control_token
        self._data_token = data_token
        self._runner_token = runner_token
        self.trace_id = trace_id
        self._stack = AsyncExitStack()
        self._client: Client | None = None

    async def __aenter__(self) -> ToolBox:
        http = await self._stack.enter_async_context(
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {self._data_token}", "X-Trace-Id": self.trace_id},
                timeout=40,
            )
        )
        self._client = await self._stack.enter_async_context(
            Client(streamable_http_client(self._mcp_url, http_client=http))
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def call_mcp(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None
        started = time.monotonic()
        try:
            result = await self._client.call_tool(tool, args)
            text = getattr(result.content[0], "text", "{}") if result.content else "{}"
            payload = json.loads(text[text.index("{") :]) if "{" in text else {"message": text}
            ok = not result.is_error
        except Exception as exc:  # noqa: BLE001 - surface any transport failure as a tool error
            payload, ok = {"ok": False, "error": "mcp_unavailable", "message": type(exc).__name__}, False
        return {
            "kind": "mcp",
            "tool": tool,
            "input": args,
            "ok": ok,
            "stopped_at": None if ok else (_stopped_at(payload) or "mcp"),
            "executed_as": payload.get("executed_as"),
            "route": ["agent", "mcp-server", "data-service", "opa", "postgres"],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "output": payload,
        }

    async def call_query_data(self, sql: str) -> dict[str, Any]:
        """Route a data query by environment: protected → governed MCP path;
        unprotected → the insecure service (demo_broad, BYPASSRLS, no checks)."""
        if self.environment != "unprotected":
            return await self.call_mcp("query_data", {"sql": sql})
        if not self._insecure_url:
            return {
                "kind": "data",
                "tool": "query_data",
                "input": {"sql": sql},
                "ok": False,
                "stopped_at": "agent",
                "route": ["agent"],
                "output": {
                    "ok": False,
                    "error": "insecure_path_unavailable",
                    "message": "unprotected environment not configured",
                },
            }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(f"{self._insecure_url}/v1/run", json={"sql": sql})
            payload = response.json()
            ok = response.status_code == 200 and payload.get("ok", False)
        except (httpx.HTTPError, ValueError) as exc:
            payload, ok = {"ok": False, "error": "insecure_unavailable", "message": type(exc).__name__}, False
        return {
            "kind": "data",
            "tool": "query_data",
            "environment": "unprotected",
            "input": {"sql": sql},
            "ok": ok,
            "stopped_at": None,  # nothing stops the unprotected path; that is the point
            "executed_as": payload.get("executed_as"),
            "route": ["agent", "insecure-data-service", "postgres (demo_broad, BYPASSRLS)"],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "output": payload,
        }

    async def run_python(self, code: str, runner: str = "protected", timeout_s: float = 15) -> dict[str, Any]:
        url = self._runner_urls.get(runner)
        if not url:
            return {
                "kind": "python",
                "tool": "run_python",
                "runner": runner,
                "input": {"code": code},
                "ok": False,
                "stopped_at": "agent",
                "output": {"error": f"runner '{runner}' is not configured"},
            }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s + 30) as http:
                response = await http.post(
                    f"{url}/execute",
                    json={
                        "code": code,
                        "timeout_s": timeout_s,
                        "mcp_token": self._runner_token,
                        "trace_id": self.trace_id,
                    },
                    headers={"Authorization": f"Bearer {self._runner_control_token}"},
                )
            payload = response.json()
            ok = response.status_code == 200 and payload.get("exit_code") == 0 and not payload.get("timed_out")
        except (httpx.HTTPError, ValueError) as exc:
            payload, ok = {"error": "runner_unavailable", "message": type(exc).__name__}, False
        return {
            "kind": "python",
            "tool": "run_python",
            "runner": runner,
            "input": {"code": code},
            "ok": ok,
            "stopped_at": None if ok else "runner",
            "route": ["agent", f"runner ({runner})"],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "output": payload,
        }


def compact_for_llm(step: dict[str, Any]) -> str:
    """What the LLM sees from a tool call: results and denials, never tokens."""
    out = step["output"]
    if step["kind"] == "data":
        view = {
            k: out.get(k)
            for k in (
                "ok",
                "error",
                "message",
                "columns",
                "rows",
                "row_count",
                "affected_rows",
                "rows_before",
                "rows_after",
                "tenants_visible",
            )
            if k in out
        }
        text = json.dumps(view, default=str)
        return text if len(text) <= MAX_TOOL_OUTPUT_CHARS else text[:MAX_TOOL_OUTPUT_CHARS] + "...[truncated]"
    if step["kind"] == "python":
        view = {k: out.get(k) for k in ("exit_code", "timed_out", "stdout", "stderr") if k in out}
    else:
        view = {
            k: out.get(k)
            for k in (
                "ok",
                "error",
                "message",
                "category",
                "columns",
                "rows",
                "row_count",
                "truncated",
                "datasets",
                "dataset",
            )
            if k in out
        }
    text = json.dumps(view, default=str)
    return text if len(text) <= MAX_TOOL_OUTPUT_CHARS else text[:MAX_TOOL_OUTPUT_CHARS] + "...[truncated]"
