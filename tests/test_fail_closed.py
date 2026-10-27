"""Fail-closed behaviour with dependencies really stopped.

scripts/test.sh stops the OPA (or PostgreSQL) container, runs these with
CHAOS=opa_down (or db_down), then starts it again. No mocks.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import auth, run_query, token_for


@pytest.mark.chaos_opa_down
def test_query_denied_when_opa_unavailable(data_client: httpx.Client) -> None:
    response = run_query(data_client, "SELECT * FROM orders", token_for("acme"))
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "policy_unavailable"
    assert "rows" not in body
    assert body["enforcement"][-1]["enforcement_point"] == "opa"
    assert body["enforcement"][-1]["outcome"] == "deny"


@pytest.mark.chaos_opa_down
def test_dataset_listing_denied_when_opa_unavailable(data_client: httpx.Client) -> None:
    response = data_client.get("/v1/datasets", headers=auth(token_for("acme")))
    assert response.status_code == 503


@pytest.mark.chaos_opa_down
def test_validation_still_rejects_first_when_opa_unavailable(data_client: httpx.Client) -> None:
    response = run_query(data_client, "DROP TABLE orders", token_for("acme"))
    assert response.status_code == 400


@pytest.mark.chaos_opa_down
def test_readiness_reports_opa_down(data_client: httpx.Client) -> None:
    assert data_client.get("/readyz").json()["opa"] == "down"


@pytest.mark.chaos_db_down
def test_query_fails_cleanly_when_database_unavailable(data_client: httpx.Client) -> None:
    response = run_query(data_client, "SELECT * FROM orders", token_for("acme"))
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "db_unavailable"
    assert "rows" not in body
