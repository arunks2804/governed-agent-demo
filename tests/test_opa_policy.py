"""Authorization decisions from the real OPA server, plus OPA API lockdown."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import OPA_URL, auth, secret

DECISION = "/v1/data/governed/authz/decision"

IDENTITIES = {
    "alice@acme.example": ("acme", {"list", "describe", "query"}),
    "nina@northstar.example": ("northstar", {"list", "describe", "query"}),
    "victor@acme.example": ("acme", {"list", "describe"}),
}
DB_ROLE = {"acme": "acme_reader", "northstar": "northstar_reader"}


@pytest.fixture
def opa_ds() -> httpx.Client:
    with httpx.Client(base_url=OPA_URL, timeout=5, headers=auth(secret("opa_data_service_token"))) as client:
        yield client


def decide(client: httpx.Client, policy_input: Any) -> dict[str, Any]:
    response = client.post(DECISION, json={"input": policy_input})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def make_input(
    sub: str, tenant: str, action: str, dataset: str = "orders", requested: list[str] | None = None
) -> dict[str, Any]:
    resource: dict[str, Any] = {}
    if action in ("describe", "query"):
        resource["dataset"] = dataset
    if action == "query":
        resource.update(
            tables=["orders"] if dataset == "orders" else [dataset],
            columns=["amount"],
            requested_tenants=requested or [],
        )
    return {"subject": {"sub": sub, "tenant": tenant, "ctx": "agent"}, "action": action, "resource": resource}


@pytest.mark.parametrize("sub", list(IDENTITIES))
@pytest.mark.parametrize("action", ["list", "describe", "query"])
def test_identity_action_matrix(opa_ds: httpx.Client, sub: str, action: str) -> None:
    tenant, granted = IDENTITIES[sub]
    result = decide(opa_ds, make_input(sub, tenant, action))
    assert result["allow"] is (action in granted), result
    if result["allow"] and action == "query":
        assert result["db_role"] == DB_ROLE[tenant]
        assert result["row_limit"] == 200
    if not result["allow"]:
        assert result["reasons"], "denials must carry a reason"
        assert result["db_role"] is None


@pytest.mark.parametrize("sub", list(IDENTITIES))
@pytest.mark.parametrize("dataset", ["payroll", "billing_accounts", ""])
def test_unknown_datasets_denied(opa_ds: httpx.Client, sub: str, dataset: str) -> None:
    tenant, _ = IDENTITIES[sub]
    for action in ("describe", "query"):
        assert decide(opa_ds, make_input(sub, tenant, action, dataset))["allow"] is False


@pytest.mark.parametrize(
    ("sub", "forged_tenant"),
    [
        ("alice@acme.example", "northstar"),
        ("nina@northstar.example", "acme"),
    ],
)
def test_tenant_claim_must_match_directory(opa_ds: httpx.Client, sub: str, forged_tenant: str) -> None:
    result = decide(opa_ds, make_input(sub, forged_tenant, "query"))
    assert result["allow"] is False
    assert any("tenant mismatch" in r for r in result["reasons"])


@pytest.mark.parametrize(
    ("sub", "tenant", "other"),
    [
        ("alice@acme.example", "acme", "northstar"),
        ("nina@northstar.example", "northstar", "acme"),
    ],
)
def test_cross_tenant_predicate_denied(opa_ds: httpx.Client, sub: str, tenant: str, other: str) -> None:
    assert decide(opa_ds, make_input(sub, tenant, "query", requested=[tenant]))["allow"] is True
    result = decide(opa_ds, make_input(sub, tenant, "query", requested=[other]))
    assert result["allow"] is False
    assert any("cross-tenant" in r for r in result["reasons"])


@pytest.mark.parametrize(
    "bad_input",
    [
        {},
        {"action": "query"},
        {"subject": {}, "action": "query", "resource": {"dataset": "orders", "tables": ["orders"]}},
        {
            "subject": {"sub": "alice@acme.example"},
            "action": "query",
            "resource": {"dataset": "orders", "tables": ["orders"]},
        },
        {
            "subject": {"sub": 42, "tenant": "acme"},
            "action": "query",
            "resource": {"dataset": "orders", "tables": ["orders"]},
        },
        {
            "subject": {"sub": "alice@acme.example", "tenant": "acme"},
            "action": "query",
            "resource": {"dataset": "orders", "tables": "orders"},
        },
        {
            "subject": {"sub": "alice@acme.example", "tenant": "acme"},
            "action": "query",
            "resource": {"dataset": ["orders"], "tables": ["orders"]},
        },
        {
            "subject": {"sub": "alice@acme.example", "tenant": "acme"},
            "action": "drop",
            "resource": {"dataset": "orders", "tables": ["orders"]},
        },
        {
            "subject": {"sub": "mallory@evil.example", "tenant": "acme"},
            "action": "query",
            "resource": {"dataset": "orders", "tables": ["orders"]},
        },
        "SELECT * FROM orders",
        [1, 2, 3],
        None,
    ],
)
def test_missing_or_malformed_input_denied(opa_ds: httpx.Client, bad_input: Any) -> None:
    result = decide(opa_ds, bad_input)
    assert result["allow"] is False
    assert result["db_role"] is None


# ------------------------------------------------------------ OPA API lockdown


def test_decision_endpoint_requires_token() -> None:
    response = httpx.post(f"{OPA_URL}{DECISION}", json={"input": {}}, timeout=5)
    assert response.status_code == 401


def test_decision_endpoint_rejects_wrong_token() -> None:
    response = httpx.post(f"{OPA_URL}{DECISION}", json={"input": {}}, timeout=5, headers=auth("x" * 48))
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/policies"),
        ("PUT", "/v1/policies/evil"),
        ("PUT", "/v1/data/governed_runtime"),
        ("PUT", "/v1/data/governed/config"),
        ("GET", "/v1/data"),
        ("GET", "/v1/config"),
        ("POST", "/v1/data/system/authz/allow"),
    ],
)
def test_data_service_token_cannot_administer_opa(opa_ds: httpx.Client, method: str, path: str) -> None:
    response = opa_ds.request(method, path, json={"revocations": {}} if method == "PUT" else None)
    assert response.status_code in (401, 403), (method, path, response.status_code)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/v1/policies/evil"),
        ("DELETE", "/v1/policies/governed/authz.rego"),
        ("PUT", "/v1/data/governed/config"),
        ("GET", "/v1/data/governed/config"),
    ],
)
def test_admin_token_limited_to_runtime_data(opa_admin: httpx.Client, method: str, path: str) -> None:
    body = "package evil\nallow := true" if path.startswith("/v1/policies") else {"x": 1}
    kwargs: dict[str, Any] = {"content": body} if isinstance(body, str) else {"json": body}
    response = opa_admin.request(method, path, **kwargs)
    assert response.status_code in (401, 403), (method, path, response.status_code)


def test_admin_can_manage_runtime_data(opa_admin: httpx.Client, restore_policy: None) -> None:
    assert opa_admin.put("/v1/data/governed_runtime", json={"revocations": {}}).status_code == 204
    assert opa_admin.get("/v1/data/governed_runtime").json()["result"] == {"revocations": {}}


def test_health_is_public() -> None:
    assert httpx.get(f"{OPA_URL}/health", timeout=5).status_code == 200
