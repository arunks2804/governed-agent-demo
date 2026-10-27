"""Shared fixtures for the enforcement test suite.

These tests run inside the `tests` container against the real services
(PostgreSQL, OPA, data service, MCP server, runner, agent). The harness acts
as the trusted identity provider: it holds the JWT signing key to mint test
tokens, the same way the web UI does.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import jwt
import psycopg
import pytest

SECRETS = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))
DATA_SERVICE_URL = os.environ.get("DATA_SERVICE_URL", "http://data-service:8080")
OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
MCP_URL = os.environ.get("MCP_URL", "http://mcp-server:8000/mcp")
RUNNER_URL = os.environ.get("RUNNER_URL", "http://runner:8000")
PROMPT_ONLY_RUNNER_URL = os.environ.get("PROMPT_ONLY_RUNNER_URL", "")
INSECURE_DATA_SERVICE_URL = os.environ.get("INSECURE_DATA_SERVICE_URL", "")
AGENT_URL = os.environ.get("AGENT_URL", "http://agent:8000")
DB_HOST = os.environ.get("DB_HOST", "postgres")
CHAOS = os.environ.get("CHAOS", "")

ISSUER = "governed-demo-idp"
DATA_AUD = "governed-data-service"
AGENT_AUD = "governed-agent"
USERS = {"acme": "alice@acme.example", "northstar": "nina@northstar.example"}
ROLES = {"acme": "acme_reader", "northstar": "northstar_reader"}
EXPECTED_ROWS = {"acme": 60, "northstar": 45}


def secret(name: str) -> str:
    return (SECRETS / name).read_text().strip()


def private_key() -> str:
    return (SECRETS / "jwt_private.pem").read_text()


def mint(
    sub: str = USERS["acme"],
    tenant: str | None = "acme",
    *,
    ctx: str = "direct",
    aud: str = DATA_AUD,
    iss: str = ISSUER,
    ttl: int = 300,
    iat_offset: int = 0,
    key: Any = None,
    algorithm: str = "EdDSA",
    extra: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    now = int(time.time()) + iat_offset
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "tenant": tenant,
        "ctx": ctx,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    claims.update(extra or {})
    for name in drop:
        claims.pop(name, None)
    if tenant is None:
        claims.pop("tenant", None)
    return jwt.encode(claims, key if key is not None else private_key(), algorithm=algorithm)


def token_for(tenant: str, **kw: Any) -> str:
    return mint(USERS[tenant], tenant, **kw)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def pg_connect(role: str, password: str | None = None) -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=DB_HOST,
        dbname="governed",
        user=role,
        password=password if password is not None else secret(f"{role}_password"),
        connect_timeout=5,
        autocommit=True,
    )


def chaos_active(name: str) -> bool:
    return name == CHAOS


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Chaos tests run only in their matching phase; normal tests are skipped during chaos."""
    for item in items:
        chaos_marks = [m.name for m in item.iter_markers() if m.name.startswith("chaos_")]
        if chaos_marks:
            wanted = chaos_marks[0].removeprefix("chaos_")
            if wanted != CHAOS:
                item.add_marker(pytest.mark.skip(reason=f"chaos test; runs only with CHAOS={wanted}"))
        elif CHAOS:
            item.add_marker(pytest.mark.skip(reason=f"normal test skipped during chaos phase {CHAOS}"))
        if item.get_closest_marker("prompt_only") and not PROMPT_ONLY_RUNNER_URL:
            item.add_marker(pytest.mark.skip(reason="prompt-only overlay not running"))


@pytest.fixture
def data_client() -> httpx.Client:
    with httpx.Client(base_url=DATA_SERVICE_URL, timeout=20) as client:
        yield client


@pytest.fixture
def opa_admin() -> httpx.Client:
    with httpx.Client(base_url=OPA_URL, timeout=5, headers=auth(secret("opa_admin_token"))) as client:
        yield client


@pytest.fixture
def restore_policy(opa_admin: httpx.Client):
    """Guarantee runtime policy data is reset after a test that changes it."""
    yield
    opa_admin.put("/v1/data/governed_runtime", json={"revocations": {}})


def run_query(client: httpx.Client, sql: str, token: str, **headers: str) -> httpx.Response:
    return client.post("/v1/query", json={"sql": sql}, headers={**auth(token), **headers})


def runner_execute(
    code: str, *, url: str = RUNNER_URL, token: str | None = None, timeout_s: float = 10
) -> dict[str, Any]:
    payload = {"code": code, "timeout_s": timeout_s, "mcp_token": token, "trace_id": f"t-{uuid.uuid4().hex[:8]}"}
    response = httpx.post(
        f"{url}/execute", json=payload, timeout=timeout_s + 20, headers=auth(secret("runner_control_token"))
    )
    response.raise_for_status()
    return response.json()
