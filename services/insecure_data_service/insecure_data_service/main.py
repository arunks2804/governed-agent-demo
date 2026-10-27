"""INSECURE data service - the UNPROTECTED comparison path. SYNTHETIC DATA ONLY.

This service deliberately does the opposite of the trusted data service:
  - no identity check, no SQL validation, no OPA, no tenant isolation
  - connects as a single over-privileged role (demo_broad, BYPASSRLS)
  - runs whatever SQL it is handed

It exists so the demo can run the SAME agent and prompts against a path with no
guardrails, and compare. It only runs under the prompt-only / unprotected
overlay, and never shares a network or credential with the protected services.

Reads target the app schema (real synthetic data, both tenants). Writes target a
throwaway `sandbox` schema that is reset after each run, so the destructive
write demo never damages the protected demo data in app.orders.
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

SECRETS = Path("/run/secrets")
MAX_ROWS = 2000


def _jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    return value if isinstance(value, str | int | float | bool) or value is None else str(value)


class _State:
    pool: AsyncConnectionPool


state = _State()


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    import os

    conninfo = make_conninfo(
        host=os.environ.get("DB_HOST", "postgres"),
        port=5432,
        dbname="governed",
        user="demo_broad",
        password=(SECRETS / "promptonly_broad_password").read_text().strip(),
        application_name="insecure-data-service/demo_broad",
        connect_timeout=5,
    )
    state.pool = AsyncConnectionPool(conninfo, min_size=1, max_size=4, open=False, timeout=5)
    await state.pool.open(wait=True)
    yield
    await state.pool.close()


app = FastAPI(title="INSECURE data service (unprotected demo path)", lifespan=lifespan, docs_url=None, redoc_url=None)


class Sql(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=20000)


async def _run(sql: str, *, search_path: str, read_only: bool) -> dict[str, Any]:
    async with state.pool.connection() as conn:
        await conn.set_autocommit(False)
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(f"SET LOCAL search_path = {search_path}")
            await cur.execute("SET LOCAL statement_timeout = 5000")
            if read_only:
                await cur.execute("SET TRANSACTION READ ONLY")
            await cur.execute(sql)
            columns = [d.name for d in (cur.description or [])]
            rows = [] if cur.description is None else await cur.fetchmany(MAX_ROWS)
            rowcount = cur.rowcount
    return {
        "columns": columns,
        "rows": [[_jsonable(v) for v in r] for r in rows],
        "row_count": len(rows),
        "affected_rows": rowcount if not columns else None,
        "executed_as": "demo_broad",
        "bypassrls": True,
    }


def _err(exc: psycopg.Error) -> JSONResponse:
    diag = getattr(exc, "diag", None)
    msg = (getattr(diag, "message_primary", None) or str(exc)).splitlines()[0][:300]
    return JSONResponse(status_code=400, content={"ok": False, "error": exc.sqlstate or "db_error", "message": msg})


@app.post("/v1/query")
async def query(body: Sql) -> Any:
    """Read anything, any tenant: demo_broad bypasses row-level security."""
    try:
        result = await _run(body.sql, search_path="app, public", read_only=True)
    except psycopg.Error as exc:
        return _err(exc)
    tenants = (
        sorted({r[result["columns"].index("tenant_id")] for r in result["rows"]})
        if "tenant_id" in result["columns"]
        else []
    )
    return {
        "ok": True,
        "path": "UNPROTECTED (no identity, no validation, no policy, BYPASSRLS)",
        "tenants_visible": tenants,
        **result,
    }


@app.post("/v1/execute")
async def execute(body: Sql) -> Any:
    """Run a write (UPDATE/DELETE/...) against the throwaway sandbox schema."""
    try:
        async with state.pool.connection() as conn:
            await conn.set_autocommit(False)
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute("SET LOCAL search_path = sandbox, public")
                await cur.execute("SET LOCAL statement_timeout = 5000")
                before = await (await cur.execute("SELECT count(*) FROM sandbox.orders")).fetchone()
                await cur.execute(body.sql)
                affected = cur.rowcount
                after = await (await cur.execute("SELECT count(*) FROM sandbox.orders")).fetchone()
    except psycopg.Error as exc:
        return _err(exc)
    return {
        "ok": True,
        "path": "UNPROTECTED (write-enabled demo_broad on throwaway sandbox schema)",
        "executed_as": "demo_broad",
        "affected_rows": affected,
        "rows_before": before[0] if before else None,
        "rows_after": after[0] if after else None,
    }


_WRITE = re.compile(r"^\s*(update|delete|insert|truncate|drop|alter|create|grant|revoke)\b", re.I)


@app.post("/v1/run")
async def run(body: Sql) -> Any:
    """Single entry used by the agent in UNPROTECTED mode: reads hit app (all
    tenants, BYPASSRLS); writes are auto-routed to the throwaway sandbox."""
    if _WRITE.match(body.sql):
        return await execute(body)
    return await query(body)


@app.post("/v1/reset")
async def reset() -> Any:
    async with state.pool.connection() as conn:
        await conn.set_autocommit(True)
        async with conn.cursor() as cur:
            await cur.execute("TRUNCATE sandbox.orders")
            await cur.execute("INSERT INTO sandbox.orders SELECT * FROM sandbox.orders_seed")
            n = await (await cur.execute("SELECT count(*) FROM sandbox.orders")).fetchone()
    return {"ok": True, "sandbox_rows": n[0] if n else None}


@app.get("/v1/check-advisory")
async def check_advisory(
    sub: str = "",
    tenant: str = "",
    target_tenant: str = "",
    dataset: str = "orders",
    action: str = "query",
) -> Any:
    """Advisory-only access check for the L2 demo level.

    Returns a recommendation but DOES NOT enforce anything. The caller
    (or the agent) can ignore this and query anyway — that is the point:
    advisory ≠ enforcement.
    """
    cross_tenant = bool(target_tenant) and target_tenant != tenant
    blocked_action = action not in ("query", "read", "list")
    allowed = not cross_tenant and not blocked_action
    if cross_tenant:
        reason = f"cross-tenant: {tenant!r} requesting {target_tenant!r} data — not recommended"
    elif blocked_action:
        reason = f"action {action!r} on {dataset!r} is not recommended"
    else:
        reason = f"within own tenant {tenant!r}, action {action!r} is expected"
    return {
        "advisory": "ALLOWED" if allowed else "NOT_RECOMMENDED",
        "recommended": allowed,
        "sub": sub,
        "tenant": tenant,
        "target_tenant": target_tenant or tenant,
        "dataset": dataset,
        "action": action,
        "reason": reason,
        "enforced": False,
        "warning": "ADVISORY ONLY — this response is not enforced; the backend will execute regardless.",
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "insecure-data-service", "warning": "UNPROTECTED demo path"}
