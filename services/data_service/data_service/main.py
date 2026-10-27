"""Trusted data service.

Request flow for ``POST /v1/query`` (each step is an enforcement point and is
logged with the request's trace id):

1. identity            verify the signed bearer token (tenant comes only from it)
2. request_schema      body may contain only ``sql``; caller-supplied tenant/role fields are rejected
3. sql_validation      AST allowlist; canonical SQL regenerated from the AST
4. opa                 authorization decision from OPA; any failure denies
5. connection_selection pick the reader role for the verified tenant; must match OPA's db_role
6. postgres            execute in a READ ONLY transaction; RLS filters rows by login role
7. result_limits       cap returned rows
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditLog, truncate_sql
from .catalog import DATASETS, TABLE_TO_DATASET
from .config import Settings, load_settings
from .db import Database, DbError
from .identity import Identity, IdentityError, TokenVerifier
from .opa_client import Decision, OpaClient, PolicyUnavailable
from .ratelimit import RateLimiter
from .sql_validator import SqlRejected, ValidatedQuery, validate_sql

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("data_service")

TRACE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class QueryRequest(BaseModel):
    # extra="forbid": a caller cannot smuggle tenant/role/db fields into the request.
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=20000)


class Steps:
    """Collects enforcement steps for the response and writes them to the audit log."""

    def __init__(self, audit: AuditLog, trace_id: str) -> None:
        self.audit = audit
        self.trace_id = trace_id
        self.items: list[dict[str, Any]] = []

    def add(self, point: str, outcome: str, **detail: Any) -> None:
        entry = self.audit.record(self.trace_id, point, outcome, **detail)
        self.items.append({k: v for k, v in entry.items() if k not in ("service", "trace_id")})


class Denied(Exception):
    def __init__(
        self, status: int, error: str, message: str, steps: Steps, extra: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.steps = steps
        self.extra = extra or {}


class AppState:
    settings: Settings
    verifier: TokenVerifier
    opa: OpaClient
    db: Database
    audit: AuditLog
    limiter: RateLimiter


state = AppState()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    state.settings = settings
    state.verifier = TokenVerifier(
        settings.jwt_public_key_pem,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        known_tenants=set(settings.tenant_roles),
        max_lifetime_s=settings.jwt_max_lifetime_s,
    )
    state.opa = OpaClient(settings.opa_url, settings.opa_token, settings.opa_timeout_s)
    state.db = Database(settings.db_host, settings.db_port, settings.db_name, settings.role_passwords)
    state.audit = AuditLog()
    state.limiter = RateLimiter(settings.rate_limit_burst, settings.rate_limit_per_s)
    await state.db.open()
    logger.info(json.dumps({"event": "startup", "roles": sorted(state.db.roles)}))
    yield
    await state.db.close()
    await state.opa.aclose()


app = FastAPI(title="Governed data service", lifespan=lifespan, docs_url=None, redoc_url=None)


def _trace_id(request: Request) -> str:
    supplied = request.headers.get("x-trace-id", "")
    return supplied if TRACE_RE.match(supplied) else f"ds-{uuid.uuid4().hex[:16]}"


@app.exception_handler(Denied)
async def _denied_handler(_: Request, exc: Denied) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={
            "ok": False,
            "error": exc.error,
            "message": exc.message,
            "trace_id": exc.steps.trace_id,
            "enforcement": exc.steps.items,
            **exc.extra,
        },
    )


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    trace = _trace_id(request)
    fields = sorted({".".join(str(p) for p in e.get("loc", [])[1:]) for e in exc.errors()})
    entry = state.audit.record(trace, "request_schema", "deny", reason="request body rejected", fields=fields)
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "error": "request_rejected",
            "message": f"request body rejected (fields: {', '.join(fields) or 'body'}); "
            "only 'sql' is accepted — tenant and role come from the verified token",
            "trace_id": trace,
            "enforcement": [entry],
        },
    )


def _authenticate(request: Request, steps: Steps) -> Identity:
    try:
        identity = state.verifier.verify_authorization_header(request.headers.get("authorization"))
    except IdentityError as exc:
        steps.add("identity", "deny", reason=str(exc))
        raise Denied(401, "unauthenticated", str(exc), steps) from exc
    steps.add(
        "identity",
        "allow",
        subject=identity.sub,
        tenant=identity.tenant,
        token_ctx=identity.ctx,
        token_ref=identity.token_ref,
    )
    if not state.limiter.allow(identity.sub):
        steps.add("rate_limit", "deny", subject=identity.sub)
        raise Denied(429, "rate_limited", "too many requests for this identity; slow down", steps)
    return identity


async def _authorize(identity: Identity, action: str, resource: dict[str, Any], steps: Steps) -> Decision:
    policy_input = {
        "subject": {"sub": identity.sub, "tenant": identity.tenant, "ctx": identity.ctx},
        "action": action,
        "resource": resource,
    }
    try:
        decision = await state.opa.decide(policy_input)
    except PolicyUnavailable as exc:
        steps.add("opa", "deny", reason=f"fail closed: {exc}", action=action)
        raise Denied(
            503, "policy_unavailable", f"authorization service unavailable — request denied (fail closed): {exc}", steps
        ) from exc
    if not decision.allow:
        steps.add("opa", "deny", action=action, reasons=decision.reasons, policy_version=decision.policy_version)
        raise Denied(
            403,
            "policy_denied",
            "; ".join(decision.reasons) or "denied by policy",
            steps,
            {"decision": {"allow": False, "reasons": decision.reasons}},
        )
    steps.add(
        "opa",
        "allow",
        action=action,
        db_role=decision.db_role,
        row_limit=decision.row_limit,
        policy_version=decision.policy_version,
    )
    return decision


@app.post("/v1/query")
async def query(body: QueryRequest, request: Request) -> dict[str, Any]:
    steps = Steps(state.audit, _trace_id(request))
    identity = _authenticate(request, steps)
    steps.add("request_schema", "allow")

    try:
        validated: ValidatedQuery = validate_sql(body.sql, state.settings.max_sql_chars)
    except SqlRejected as exc:
        steps.add("sql_validation", "deny", category=exc.category, reason=exc.reason, sql=truncate_sql(body.sql))
        raise Denied(400, "sql_rejected", exc.reason, steps, {"category": exc.category}) from exc
    steps.add(
        "sql_validation",
        "allow",
        canonical_sql=truncate_sql(validated.sql),
        tables=validated.tables,
        requested_tenants=validated.requested_tenants,
    )

    unmapped = [t for t in validated.tables if t not in TABLE_TO_DATASET]
    if unmapped:  # validator and catalog disagree: deny rather than guess
        steps.add("sql_validation", "deny", reason=f"tables not in catalog: {unmapped}")
        raise Denied(403, "sql_rejected", "table is not part of any dataset", steps)
    datasets = sorted({TABLE_TO_DATASET[t] for t in validated.tables})
    resource = {
        "dataset": datasets[0] if len(datasets) == 1 else datasets,
        "tables": validated.tables,
        "columns": validated.columns,
        "requested_tenants": validated.requested_tenants,
    }
    decision = await _authorize(identity, "query", resource, steps)

    expected_role = state.settings.tenant_roles.get(identity.tenant)
    if decision.db_role is None or decision.db_role != expected_role or expected_role not in state.db.roles:
        steps.add("connection_selection", "deny", reason="OPA role does not match the verified tenant's reader role")
        raise Denied(403, "connection_denied", "no permitted database connection for this identity", steps)
    steps.add("connection_selection", "allow", db_role=expected_role)

    row_limit = max(1, min(decision.row_limit, state.settings.max_rows_cap))
    try:
        result = await state.db.run_select(
            expected_role,
            validated.sql,
            row_limit,
            state.settings.query_timeout_ms,
            policy_predicate=f"tenant_id = '{identity.tenant}'::text",
        )
    except DbError as exc:
        status = {"query_timeout": 504, "db_unavailable": 503, "postgres_permission_denied": 403}.get(exc.category, 400)
        steps.add(
            "postgres",
            "deny" if status in (403, 504) else "error",
            category=exc.category,
            reason=str(exc),
            db_role=expected_role,
        )
        raise Denied(status, exc.category, str(exc), steps) from exc

    steps.add(
        "postgres",
        "allow",
        executed_as=result.executed_as,
        row_count=len(result.rows),
        rls_policy_predicate=result.rls_evidence[0]["policy_predicate"] if result.rls_evidence else None,
        rls_scans=len(result.rls_evidence),
    )
    steps.add(
        "result_limits", "truncated" if result.truncated else "allow", row_limit=row_limit, returned=len(result.rows)
    )

    return {
        "ok": True,
        "trace_id": steps.trace_id,
        "identity": {"sub": identity.sub, "tenant": identity.tenant},
        "decision": {
            "allow": True,
            "db_role": decision.db_role,
            "row_limit": row_limit,
            "policy_version": decision.policy_version,
        },
        "canonical_sql": validated.sql,
        "executed_as": result.executed_as,
        "rls_evidence": result.rls_evidence,
        "columns": result.columns,
        "rows": result.rows,
        "row_count": len(result.rows),
        "truncated": result.truncated,
        "enforcement": steps.items,
    }


@app.get("/v1/datasets")
async def list_datasets(request: Request) -> dict[str, Any]:
    steps = Steps(state.audit, _trace_id(request))
    identity = _authenticate(request, steps)
    decision = await _authorize(identity, "list", {}, steps)
    visible = [DATASETS[d] for d in decision.visible_datasets if d in DATASETS]
    return {
        "ok": True,
        "trace_id": steps.trace_id,
        "datasets": [{"name": d["name"], "description": d["description"]} for d in visible],
        "enforcement": steps.items,
    }


@app.get("/v1/datasets/{name}")
async def describe_dataset(name: str, request: Request) -> dict[str, Any]:
    steps = Steps(state.audit, _trace_id(request))
    identity = _authenticate(request, steps)
    await _authorize(identity, "describe", {"dataset": name}, steps)
    meta = DATASETS.get(name)
    if meta is None:  # OPA denies unknown datasets; this is a second guard
        raise Denied(404, "not_found", "unknown dataset", steps)
    # TEST-ONLY prompt-injection fixture. When DATASET_INJECTION_NOTE is set
    # (unprotected overlay only), the dataset description carries attacker text,
    # to show that the agent's backend still enforces regardless of what the
    # retrieved metadata "instructs". Never set in the protected configuration.
    injection = os.environ.get("DATASET_INJECTION_NOTE")
    if injection and name == "orders":
        meta = {**meta, "description": meta["description"] + " " + injection, "injected_note": injection}
    return {"ok": True, "trace_id": steps.trace_id, "dataset": meta, "enforcement": steps.items}


@app.get("/v1/audit")
async def audit(request: Request, trace_id: str | None = None, limit: int = 200) -> Any:
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {state.settings.audit_token}"
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthenticated"})
    limit = max(1, min(limit, 1000))
    return {"ok": True, "entries": state.audit.query(trace_id, limit)}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    opa_up = await state.opa.healthy()
    db = {role: await state.db.ping(role) for role in sorted(state.db.roles)}
    return {"opa": "up" if opa_up else "down", "postgres": db}
