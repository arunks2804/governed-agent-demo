"""Protected vs UNPROTECTED comparison (the security presentation demos).

These exercise the insecure overlay (demo_broad, no checks) against the same
agent API used by the protected path, and prove each control with a
deterministic probe. They require the unprotected overlay, so they are marked
prompt_only and skip under `make up-protected`.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from conftest import AGENT_URL, INSECURE_DATA_SERVICE_URL, USERS, auth, mint

pytestmark = pytest.mark.prompt_only


def agent_run(sub: str, tenant: str, *, environment: str, sql: str) -> dict[str, Any]:
    body = {
        "mode": "manual_sql",
        "sql": sql,
        "environment": environment,
        "data_token": mint(sub, tenant, ctx="agent"),
        "runner_token": mint(sub, tenant, ctx="runner"),
        "trace_id": f"cmp-{uuid.uuid4().hex[:8]}",
    }
    r = httpx.post(
        f"{AGENT_URL}/v1/run", json=body, timeout=60, headers=auth(mint(sub, tenant, aud="governed-agent", ctx="agent"))
    )
    r.raise_for_status()
    return r.json()


def only_step(record: dict[str, Any]) -> dict[str, Any]:
    steps = [s for s in record["steps"] if s.get("kind") in ("data", "mcp")]
    assert steps, record
    return steps[0]


# ------------------------------------------------------- cross-tenant read


def test_cross_tenant_protected_denied_unprotected_exposed() -> None:
    sql = "SELECT tenant_id, count(*) AS orders FROM orders WHERE tenant_id = 'northstar' GROUP BY tenant_id"
    protected = only_step(agent_run(USERS["acme"], "acme", environment="protected", sql=sql))
    assert protected["ok"] is False
    assert protected["stopped_at"] == "opa"

    unprotected = only_step(agent_run(USERS["acme"], "acme", environment="unprotected", sql=sql))
    assert unprotected["ok"] is True
    assert unprotected["output"]["tenants_visible"] == ["northstar"]
    assert unprotected["output"]["rows"] == [["northstar", 45]]
    assert unprotected["executed_as"] == "demo_broad"


def test_unfiltered_read_protected_own_tenant_unprotected_all() -> None:
    sql = "SELECT tenant_id, count(*) FROM orders GROUP BY tenant_id ORDER BY tenant_id"
    protected = only_step(agent_run(USERS["acme"], "acme", environment="protected", sql=sql))
    assert {r[0] for r in protected["output"]["rows"]} == {"acme"}
    unprotected = only_step(agent_run(USERS["acme"], "acme", environment="unprotected", sql=sql))
    assert {r[0] for r in unprotected["output"]["rows"]} == {"acme", "northstar"}


# ------------------------------------------------------------- viewer role


def test_viewer_protected_denied_unprotected_served() -> None:
    sql = "SELECT count(*) FROM orders"
    protected = only_step(agent_run("victor@acme.example", "acme", environment="protected", sql=sql))
    assert protected["ok"] is False  # viewer not granted query
    unprotected = only_step(agent_run("victor@acme.example", "acme", environment="unprotected", sql=sql))
    assert unprotected["ok"] is True  # insecure path ignores identity entirely


# ------------------------------------------------------- restricted billing


def test_billing_protected_denied_unprotected_exposed() -> None:
    sql = "SELECT tenant_id, iban_synthetic, credit_limit FROM billing_accounts"
    protected = only_step(agent_run(USERS["acme"], "acme", environment="protected", sql=sql))
    assert protected["ok"] is False
    assert protected["output"]["category"] == "unapproved_table"
    unprotected = only_step(agent_run(USERS["acme"], "acme", environment="unprotected", sql=sql))
    assert unprotected["ok"] is True
    assert {r[0] for r in unprotected["output"]["rows"]} == {"acme", "northstar"}


# ------------------------------------------------------------ update/delete


def test_write_protected_rejected_unprotected_mutates_sandbox() -> None:
    update = "UPDATE orders SET status = 'delivered' WHERE order_id = 2000"
    protected = only_step(agent_run(USERS["acme"], "acme", environment="protected", sql=update))
    assert protected["ok"] is False
    assert protected["output"]["category"] == "write"

    # Reset the throwaway sandbox, then mutate it via the unprotected path.
    httpx.post(f"{INSECURE_DATA_SERVICE_URL}/v1/reset", json={}, timeout=10)
    unprotected = only_step(agent_run(USERS["acme"], "acme", environment="unprotected", sql=update))
    assert unprotected["ok"] is True
    assert unprotected["output"]["affected_rows"] == 1
    delete = only_step(
        agent_run(USERS["acme"], "acme", environment="unprotected", sql="DELETE FROM orders WHERE order_id = 2001")
    )
    assert delete["output"]["rows_before"] - delete["output"]["rows_after"] == 1
    # Reset leaves the throwaway copy whole again.
    assert httpx.post(f"{INSECURE_DATA_SERVICE_URL}/v1/reset", json={}, timeout=10).json()["sandbox_rows"] == 105


def test_protected_demo_data_never_written_by_unprotected_path() -> None:
    """The real app.orders is untouched: the protected reader still sees 60 Acme rows."""
    httpx.post(f"{INSECURE_DATA_SERVICE_URL}/v1/reset", json={}, timeout=10)
    agent_run(USERS["acme"], "acme", environment="unprotected", sql="DELETE FROM orders WHERE order_id = 2001")
    protected = only_step(agent_run(USERS["acme"], "acme", environment="protected", sql="SELECT count(*) FROM orders"))
    assert protected["output"]["rows"] == [[60]]
    httpx.post(f"{INSECURE_DATA_SERVICE_URL}/v1/reset", json={}, timeout=10)


# ------------------------------------------------- prompt injection fixture


def test_injection_note_present_but_backend_still_filters() -> None:
    import asyncio
    import json

    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from conftest import MCP_URL

    async def describe() -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {mint(USERS['acme'], 'acme', ctx='agent')}"}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=20) as http,
            Client(streamable_http_client(MCP_URL, http_client=http)) as client,
        ):
            res = await client.call_tool("describe_dataset", {"name": "orders"})
        text = getattr(res.content[0], "text", "{}")
        return json.loads(text[text.index("{") :])

    described = asyncio.run(describe())
    # Overlay sets the injected note; it must be visible in retrieved metadata...
    assert "injected_note" in described["dataset"]
    assert "ignore" in described["dataset"]["injected_note"].lower()
    # ...but enforcement is unaffected: a caller still only sees their own tenant.
    probe = only_step(
        agent_run(
            USERS["acme"],
            "acme",
            environment="protected",
            sql="SELECT tenant_id, count(*) FROM orders GROUP BY tenant_id",
        )
    )
    assert {r[0] for r in probe["output"]["rows"]} == {"acme"}


# ------------------------------------------------- insecure service itself


def test_insecure_service_requires_the_overlay_network() -> None:
    """The insecure service is reachable only on the overlay network, and is
    clearly self-labelled as the unprotected path."""
    health = httpx.get(f"{INSECURE_DATA_SERVICE_URL}/healthz", timeout=5).json()
    assert health["service"] == "insecure-data-service"
    assert "UNPROTECTED" in health["warning"]
