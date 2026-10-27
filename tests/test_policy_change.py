"""A runtime OPA policy change affects the very next request - no restart, no
agent prompt change, no decision caching in the data service."""

from __future__ import annotations

import httpx

from conftest import run_query, token_for


def test_revocation_applies_to_next_request(
    data_client: httpx.Client, opa_admin: httpx.Client, restore_policy: None
) -> None:
    token = token_for("acme")
    assert run_query(data_client, "SELECT count(*) FROM orders", token).status_code == 200

    revoke = {"revocations": {"acme": {"orders": ["query"]}}}
    assert opa_admin.put("/v1/data/governed_runtime", json=revoke).status_code == 204

    denied = run_query(data_client, "SELECT count(*) FROM orders", token)
    assert denied.status_code == 403
    assert "revoked" in denied.json()["message"]

    # Tenant-scoped: Northstar is unaffected.
    assert run_query(data_client, "SELECT count(*) FROM orders", token_for("northstar")).status_code == 200

    assert opa_admin.put("/v1/data/governed_runtime", json={"revocations": {}}).status_code == 204
    assert run_query(data_client, "SELECT count(*) FROM orders", token).status_code == 200


def test_wildcard_revocation_hides_dataset_listing(
    data_client: httpx.Client, opa_admin: httpx.Client, restore_policy: None
) -> None:
    from conftest import auth

    opa_admin.put("/v1/data/governed_runtime", json={"revocations": {"acme": {"orders": ["*"]}}})
    listing = data_client.get("/v1/datasets", headers=auth(token_for("acme"))).json()
    assert listing["datasets"] == []
