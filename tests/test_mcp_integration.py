"""Real MCP client -> MCP server -> data service -> OPA -> PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from conftest import EXPECTED_ROWS, MCP_URL, auth, secret, token_for


async def mcp_call(
    tool: str, args: dict[str, Any], token: str | None, trace: str = "mcp-test"
) -> tuple[bool, dict[str, Any]]:
    headers = {"X-Trace-Id": trace}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        Client(streamable_http_client(MCP_URL, http_client=http)) as client,
    ):
        result = await client.call_tool(tool, args)
    text = result.content[0].text if result.content else "{}"
    # Tool errors arrive as "Error executing tool <name>: {json}".
    return bool(result.is_error), json.loads(text[text.index("{") :])


async def test_tools_listed() -> None:
    async with (
        httpx2.AsyncClient(timeout=10) as http,
        Client(streamable_http_client(MCP_URL, http_client=http)) as client,
    ):
        tools = await client.list_tools()
    assert sorted(t.name for t in tools.tools) == ["describe_dataset", "list_datasets", "query_data"]


async def test_acme_order_summary_via_mcp() -> None:
    is_error, body = await mcp_call(
        "query_data",
        {"sql": "SELECT status, count(*) AS n FROM orders GROUP BY status ORDER BY status"},
        token_for("acme"),
    )
    assert not is_error, body
    assert sum(r[1] for r in body["rows"]) == EXPECTED_ROWS["acme"]
    assert body["executed_as"] == "acme_reader"


async def test_select_star_via_mcp_only_own_rows() -> None:
    is_error, body = await mcp_call("query_data", {"sql": "SELECT * FROM orders"}, token_for("northstar"))
    assert not is_error
    assert {r[1] for r in body["rows"]} == {"northstar"}


async def test_cross_tenant_denied_via_mcp() -> None:
    is_error, body = await mcp_call(
        "query_data", {"sql": "SELECT * FROM orders WHERE tenant_id = 'northstar'"}, token_for("acme")
    )
    assert is_error
    assert body["error"] == "policy_denied"


async def test_write_rejected_via_mcp() -> None:
    is_error, body = await mcp_call("query_data", {"sql": "UPDATE orders SET amount = 0"}, token_for("acme"))
    assert is_error
    assert body["category"] == "write"


async def test_no_credentials_via_mcp() -> None:
    is_error, body = await mcp_call("query_data", {"sql": "SELECT * FROM orders"}, None)
    assert is_error
    assert body["error"] == "unauthenticated"


async def test_list_and_describe_via_mcp() -> None:
    token = token_for("acme")
    _, listing = await mcp_call("list_datasets", {}, token)
    assert [d["name"] for d in listing["datasets"]] == ["orders"]
    _, described = await mcp_call("describe_dataset", {"name": "orders"}, token)
    assert described["dataset"]["table"] == "orders"
    is_error, _ = await mcp_call("describe_dataset", {"name": "../../v1/audit"}, token)
    assert is_error


async def test_trace_id_propagates_to_audit_log() -> None:
    trace = "mcp-trace-propagation"
    await mcp_call("query_data", {"sql": "SELECT count(*) FROM orders"}, token_for("acme"), trace)
    from conftest import DATA_SERVICE_URL

    entries = httpx.get(
        f"{DATA_SERVICE_URL}/v1/audit", params={"trace_id": trace}, headers=auth(secret("audit_token")), timeout=10
    ).json()["entries"]
    assert any(e["enforcement_point"] == "postgres" and e["outcome"] == "allow" for e in entries)


async def test_concurrent_mcp_sessions_do_not_cross_identities() -> None:
    async def one(i: int) -> tuple[str, set[str]]:
        tenant = "acme" if i % 2 else "northstar"
        _, body = await mcp_call(
            "query_data", {"sql": "SELECT tenant_id FROM orders"}, token_for(tenant), f"mcp-conc-{i}"
        )
        return tenant, {r[0] for r in body["rows"]}

    results = await asyncio.gather(*(one(i) for i in range(24)))
    for tenant, seen in results:
        assert seen == {tenant}


async def test_host_header_rebinding_rejected() -> None:
    async with httpx2.AsyncClient(timeout=10) as http:
        response = await http.post(
            MCP_URL,
            headers={
                "Host": "evil.example",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert response.status_code in (400, 403, 421)


@pytest.mark.chaos_opa_down
async def test_mcp_query_denied_when_opa_down() -> None:
    is_error, body = await mcp_call("query_data", {"sql": "SELECT * FROM orders"}, token_for("acme"))
    assert is_error
    assert body["error"] == "policy_unavailable"
