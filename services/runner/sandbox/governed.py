"""Governed data access for sandboxed code - the only supported data path.

    from governed import list_datasets, describe_dataset, query_data
    result = query_data("SELECT status, count(*) AS n FROM orders GROUP BY status")
    for record in result["records"]:      # rows as dicts: {"status": ..., "n": ...}
        print(record["status"], record["n"])
    # result["columns"] / result["rows"] hold the same data as a header + list of lists

Each call is a real MCP tool call to the governed MCP server, authenticated
with the short-lived token the runner was given for this execution. Denials
raise GovernedAccessError with the structured reason.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

__all__ = ["GovernedAccessError", "describe_dataset", "list_datasets", "query_data"]


class GovernedAccessError(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("message") or payload.get("error") or "denied")
        self.payload = payload


async def _call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    headers = {"X-Trace-Id": os.environ.get("TRACE_ID", "sandbox")}
    token = os.environ.get("MCP_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        Client(streamable_http_client(os.environ["MCP_URL"], http_client=http)) as client,
    ):
        result = await client.call_tool(tool, args)
    text = getattr(result.content[0], "text", "{}") if result.content else "{}"
    payload = json.loads(text[text.index("{") :]) if "{" in text else {"error": text}
    if result.is_error:
        raise GovernedAccessError(payload)
    return payload


def list_datasets() -> dict[str, Any]:
    return asyncio.run(_call("list_datasets", {}))


def describe_dataset(name: str) -> dict[str, Any]:
    return asyncio.run(_call("describe_dataset", {"name": name}))


def query_data(sql: str) -> dict[str, Any]:
    result = asyncio.run(_call("query_data", {"sql": sql}))
    result["records"] = [dict(zip(result.get("columns", []), row, strict=False)) for row in result.get("rows", [])]
    return result
