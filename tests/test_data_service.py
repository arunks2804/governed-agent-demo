"""Trusted data service: identity, request schema, validation, authorization,
connection selection, RLS and result limits - end to end against real OPA and
PostgreSQL."""

from __future__ import annotations

import concurrent.futures
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from conftest import (
    DATA_SERVICE_URL,
    EXPECTED_ROWS,
    USERS,
    auth,
    mint,
    run_query,
    secret,
    token_for,
)

ALL_COLUMNS = ["order_id", "tenant_id", "customer_name", "amount", "status", "order_date"]


def points(body: dict) -> list[tuple[str, str]]:
    return [(e["enforcement_point"], e["outcome"]) for e in body["enforcement"]]


# ------------------------------------------------------------------ happy path


@pytest.mark.parametrize("tenant", ["acme", "northstar"])
def test_select_star_returns_only_own_tenant_via_rls(data_client: httpx.Client, tenant: str) -> None:
    response = run_query(data_client, "SELECT * FROM orders", token_for(tenant))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["columns"] == ALL_COLUMNS
    assert body["row_count"] == EXPECTED_ROWS[tenant]
    assert {row[1] for row in body["rows"]} == {tenant}
    assert body["executed_as"] == f"{tenant}_reader"
    assert body["rls_evidence"] and all(
        e["policy_predicate"] == f"tenant_id = '{tenant}'::text" for e in body["rls_evidence"]
    )
    assert points(body) == [
        ("identity", "allow"),
        ("request_schema", "allow"),
        ("sql_validation", "allow"),
        ("opa", "allow"),
        ("connection_selection", "allow"),
        ("postgres", "allow"),
        ("result_limits", "allow"),
    ]


def test_order_summary_aggregates_only_acme(data_client: httpx.Client) -> None:
    sql = "SELECT status, count(*) AS n, round(sum(amount), 2) AS total FROM orders GROUP BY status ORDER BY status"
    body = run_query(data_client, sql, token_for("acme")).json()
    assert body["ok"] is True
    assert sum(row[1] for row in body["rows"]) == EXPECTED_ROWS["acme"]


# ------------------------------------------------------- cross-tenant attempts


def test_explicit_cross_tenant_filter_denied_by_opa(data_client: httpx.Client) -> None:
    response = run_query(data_client, "SELECT * FROM orders WHERE tenant_id = 'northstar'", token_for("acme"))
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "policy_denied"
    assert ("opa", "deny") in points(body)
    assert "cross-tenant" in body["message"]


def test_obfuscated_cross_tenant_filter_returns_nothing_via_rls(data_client: httpx.Client) -> None:
    """Service cannot see a tenant literal here, so it allows; RLS still filters."""
    response = run_query(data_client, "SELECT * FROM orders WHERE tenant_id <> 'acme'", token_for("acme"))
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 0
    assert body["executed_as"] == "acme_reader"
    assert body["rls_evidence"][0]["policy_predicate"] == "tenant_id = 'acme'::text"


def test_upper_case_literal_trick_returns_nothing(data_client: httpx.Client) -> None:
    body = run_query(data_client, "SELECT * FROM orders WHERE upper(tenant_id) = 'NORTHSTAR'", token_for("acme")).json()
    assert body["ok"] is True and body["row_count"] == 0


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("sql", "category"),
    [
        ("UPDATE orders SET amount = 0", "write"),
        ("SET ROLE northstar_reader", "role_change"),
        ("SELECT * FROM orders; DELETE FROM orders", "multiple_statements"),
        ("SELECT * FROM billing_accounts", "unapproved_table"),
        ("SELECT pg_sleep(30) FROM orders", "unsupported_function"),
    ],
)
def test_validation_rejects_before_policy_and_db(data_client: httpx.Client, sql: str, category: str) -> None:
    response = run_query(data_client, sql, token_for("acme"))
    assert response.status_code == 400
    body = response.json()
    assert body["category"] == category
    assert points(body)[-1] == ("sql_validation", "deny")
    assert not any(p == "opa" for p, _ in points(body))


# ------------------------------------------------------------------ identity


@pytest.mark.parametrize("header", [None, "", "Bearer", "Basic dXNlcjpwYXNz", "Bearer not-a-jwt"])
def test_missing_or_garbage_credentials(data_client: httpx.Client, header: str | None) -> None:
    headers = {} if header is None else {"Authorization": header}
    response = data_client.post("/v1/query", json={"sql": "SELECT * FROM orders"}, headers=headers)
    assert response.status_code == 401
    assert points(response.json()) == [("identity", "deny")]


@pytest.mark.parametrize(
    ("label", "kwargs", "reason"),
    [
        ("expired", {"iat_offset": -1000, "ttl": 300}, "expired"),
        ("not yet valid", {"iat_offset": 600}, "not yet valid"),
        ("wrong audience", {"aud": "governed-agent"}, "audience"),
        ("wrong issuer", {"iss": "evil-idp"}, "issuer"),
        ("missing jti", {"drop": ("jti",)}, "missing claim"),
        ("missing exp", {"drop": ("exp",)}, "missing claim"),
        ("unknown tenant", {"extra": {"tenant": "globex"}}, "tenant"),
        ("missing tenant", {"extra": {"tenant": None}}, "tenant"),
        ("bad context", {"ctx": "admin"}, "context"),
        ("too long lived", {"ttl": 86400}, "lifetime"),
    ],
)
def test_invalid_tokens_rejected(data_client: httpx.Client, label: str, kwargs: dict, reason: str) -> None:
    response = run_query(data_client, "SELECT * FROM orders", mint(**kwargs))
    assert response.status_code == 401, label
    assert reason in response.json()["message"], (label, response.json()["message"])


def test_token_signed_by_other_key_rejected(data_client: httpx.Client) -> None:
    token = mint(key=Ed25519PrivateKey.generate())
    assert run_query(data_client, "SELECT * FROM orders", token).status_code == 401


def test_alg_none_rejected(data_client: httpx.Client) -> None:
    now = int(time.time())
    claims = {
        "iss": "governed-demo-idp",
        "aud": "governed-data-service",
        "sub": USERS["northstar"],
        "tenant": "northstar",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "jti": "x",
    }
    token = jwt.encode(claims, key="", algorithm="none")
    assert run_query(data_client, "SELECT * FROM orders", token).status_code == 401


def test_hmac_key_confusion_rejected(data_client: httpx.Client) -> None:
    public_pem = secret("jwt_public.pem")
    now = int(time.time())
    claims = {
        "iss": "governed-demo-idp",
        "aud": "governed-data-service",
        "sub": USERS["northstar"],
        "tenant": "northstar",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "jti": "x",
    }
    header = {"alg": "HS256", "typ": "JWT"}
    import base64
    import hashlib
    import hmac
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
    sig = hmac.new(public_pem.encode(), signing_input.encode(), hashlib.sha256).digest()
    token = f"{signing_input}.{b64(sig)}"
    assert run_query(data_client, "SELECT * FROM orders", token).status_code == 401


def test_tampered_payload_rejected(data_client: httpx.Client) -> None:
    header, payload, sig = token_for("acme").split(".")
    import base64
    import json

    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["tenant"] = "northstar"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    assert run_query(data_client, "SELECT * FROM orders", f"{header}.{forged}.{sig}").status_code == 401


def test_signed_token_with_mismatched_tenant_denied_by_opa(data_client: httpx.Client) -> None:
    """Even a validly signed token cannot pair an Acme user with the Northstar tenant."""
    token = mint(USERS["acme"], "northstar")
    response = run_query(data_client, "SELECT * FROM orders", token)
    assert response.status_code == 403
    assert "tenant mismatch" in response.json()["message"]


def test_unknown_user_denied(data_client: httpx.Client) -> None:
    response = run_query(data_client, "SELECT * FROM orders", mint("mallory@acme.example", "acme"))
    assert response.status_code == 403
    assert "unknown identity" in response.json()["message"]


def test_viewer_without_query_grant_denied(data_client: httpx.Client) -> None:
    response = run_query(data_client, "SELECT * FROM orders", mint("victor@acme.example", "acme"))
    assert response.status_code == 403


@pytest.mark.parametrize("field", ["tenant", "tenant_id", "role", "db_role", "user"])
def test_caller_supplied_tenant_or_role_fields_rejected(data_client: httpx.Client, field: str) -> None:
    response = data_client.post(
        "/v1/query", headers=auth(token_for("acme")), json={"sql": "SELECT * FROM orders", field: "northstar"}
    )
    assert response.status_code == 422
    assert response.json()["enforcement"][0]["enforcement_point"] == "request_schema"


@pytest.mark.parametrize("header", ["X-Tenant", "X-Tenant-Id", "X-Role", "X-Db-Role", "X-Forwarded-User"])
def test_caller_supplied_headers_ignored(data_client: httpx.Client, header: str) -> None:
    response = run_query(
        data_client,
        "SELECT * FROM orders",
        token_for("acme"),
        **{header: "northstar_reader" if "Role" in header else "northstar"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["executed_as"] == "acme_reader"
    assert {row[1] for row in body["rows"]} == {"acme"}


def test_concurrent_mixed_tenant_requests_do_not_leak() -> None:
    tokens = {t: token_for(t) for t in ("acme", "northstar")}

    def one(i: int) -> tuple[str, set[str], str, int]:
        tenant = "acme" if i % 2 == 0 else "northstar"
        with httpx.Client(base_url=DATA_SERVICE_URL, timeout=30) as client:
            body = run_query(client, "SELECT * FROM orders", tokens[tenant]).json()
        return tenant, {row[1] for row in body["rows"]}, body["executed_as"], body["row_count"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(one, range(80)))
    for tenant, seen, executed_as, count in results:
        assert seen == {tenant}
        assert executed_as == f"{tenant}_reader"
        assert count == EXPECTED_ROWS[tenant]


# -------------------------------------------------------------- limits


def test_statement_timeout_enforced(data_client: httpx.Client) -> None:
    # Within the validator's scope (4 table refs) but expensive: ~13M joined rows of string work.
    sql = (
        "SELECT count(*) FROM orders a, orders b, orders c, orders d "
        "WHERE lower(a.customer_name || b.customer_name || c.customer_name || d.customer_name) LIKE '%zzzz%'"
    )
    started = time.monotonic()
    response = run_query(data_client, sql, token_for("acme"))
    elapsed = time.monotonic() - started
    assert response.status_code == 504, response.text
    assert response.json()["error"] == "query_timeout"
    assert elapsed < 10
    assert ("postgres", "deny") in points(response.json())


def test_row_limit_enforced_and_reported(data_client: httpx.Client) -> None:
    sql = "SELECT a.order_id, b.order_id FROM orders a, orders b"
    body = run_query(data_client, sql, token_for("acme")).json()
    assert body["row_count"] == 200
    assert body["truncated"] is True
    assert ("result_limits", "truncated") in points(body)


def test_datasets_endpoints_authorized(data_client: httpx.Client) -> None:
    listing = data_client.get("/v1/datasets", headers=auth(token_for("acme"))).json()
    assert [d["name"] for d in listing["datasets"]] == ["orders"]
    described = data_client.get("/v1/datasets/orders", headers=auth(token_for("acme"))).json()
    assert [c["name"] for c in described["dataset"]["columns"]] == ALL_COLUMNS
    assert data_client.get("/v1/datasets/payroll", headers=auth(token_for("acme"))).status_code == 403
    assert data_client.get("/v1/datasets").status_code == 401


def test_per_identity_rate_limit(data_client: httpx.Client) -> None:
    """A dedicated (unknown-to-OPA) identity so the bucket doesn't affect other tests."""
    token = mint("ratelimit-probe@acme.example", "acme")
    statuses = [run_query(data_client, "SELECT 1 FROM orders", token).status_code for _ in range(300)]
    assert 429 in statuses
    assert statuses[0] == 403  # within budget: reaches OPA, which denies the unknown user
    limited = run_query(data_client, "SELECT 1 FROM orders", token).json()
    assert limited["enforcement"][-1]["enforcement_point"] == "rate_limit"
    # Other identities are unaffected.
    assert run_query(data_client, "SELECT count(*) FROM orders", token_for("acme")).status_code == 200


# ---------------------------------------------------------------- audit log


def test_audit_requires_token(data_client: httpx.Client) -> None:
    assert data_client.get("/v1/audit").status_code == 401
    assert data_client.get("/v1/audit", headers=auth("wrong")).status_code == 401


def test_audit_names_enforcement_points_without_secrets(data_client: httpx.Client) -> None:
    token = token_for("acme")
    trace = "audit-hygiene-1"
    run_query(data_client, "SELECT * FROM orders", token, **{"X-Trace-Id": trace})
    run_query(data_client, "SET ROLE northstar_reader", token, **{"X-Trace-Id": trace})
    entries = data_client.get("/v1/audit", params={"trace_id": trace}, headers=auth(secret("audit_token"))).json()[
        "entries"
    ]
    assert {e["enforcement_point"] for e in entries} >= {"identity", "sql_validation", "opa", "postgres"}
    blob = repr(entries)
    for sensitive in (
        token,
        secret("acme_reader_password"),
        secret("northstar_reader_password"),
        secret("opa_data_service_token"),
        secret("audit_token"),
    ):
        assert sensitive not in blob
    assert "Bluebell Bakery" not in blob  # result rows are never logged
