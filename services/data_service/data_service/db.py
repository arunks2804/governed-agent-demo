"""PostgreSQL access through per-tenant reader roles.

One connection pool per login role. A pooled connection is only ever
authenticated as that role, so connections cannot carry one tenant's identity
into another tenant's request. Every query runs in a READ ONLY transaction with
a local statement timeout.
"""

from __future__ import annotations

import datetime as dt
import decimal
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool, PoolTimeout


class DbError(Exception):
    category = "db_error"


class DbUnavailable(DbError):
    category = "db_unavailable"


class QueryTimeout(DbError):
    category = "query_timeout"


class DbPermissionDenied(DbError):
    category = "postgres_permission_denied"


class QueryFailed(DbError):
    category = "query_failed"


@dataclass(frozen=True)
class QueryResult:
    executed_as: str
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    rls_evidence: list[dict[str, str]]


def _jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def extract_rls_evidence(plan: Any, policy_predicate: str) -> list[dict[str, str]]:
    """Find the RLS policy qual in the plan. With RLS active, PostgreSQL adds
    the policy predicate (e.g. ``tenant_id = 'acme'::text``) to every scan of
    the table, ANDed with the query's own filters."""
    evidence: list[dict[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            relation = node.get("Relation Name")
            for key in ("Filter", "Index Cond", "Recheck Cond"):
                cond = node.get(key)
                if isinstance(cond, str) and policy_predicate in cond and relation:
                    evidence.append(
                        {
                            "relation": str(relation),
                            "node": key,
                            "policy_predicate": policy_predicate,
                            "plan_condition": cond,
                        }
                    )
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(plan)
    return evidence


async def _configure(conn: psycopg.AsyncConnection[Any]) -> None:
    await conn.set_read_only(True)
    await conn.set_autocommit(False)


class Database:
    def __init__(self, host: str, port: int, dbname: str, role_passwords: dict[str, str]) -> None:
        self._pools: dict[str, AsyncConnectionPool] = {}
        for role, password in role_passwords.items():
            conninfo = make_conninfo(
                host=host,
                port=port,
                dbname=dbname,
                user=role,
                password=password,
                application_name=f"data-service/{role}",
                connect_timeout=3,
            )
            self._pools[role] = AsyncConnectionPool(
                conninfo,
                min_size=0,
                max_size=4,
                open=False,
                timeout=3.0,
                name=f"pool-{role}",
                configure=_configure,
                check=AsyncConnectionPool.check_connection,
            )

    @property
    def roles(self) -> set[str]:
        return set(self._pools)

    async def open(self) -> None:
        for pool in self._pools.values():
            await pool.open(wait=False)

    async def close(self) -> None:
        for pool in self._pools.values():
            await pool.close()

    async def ping(self, role: str) -> bool:
        try:
            async with self._pools[role].connection(timeout=2.0) as conn:
                await conn.execute("SELECT 1")
                await conn.rollback()
            return True
        except (psycopg.Error, PoolTimeout):
            return False

    async def run_select(
        self, role: str, sql: str, row_limit: int, timeout_ms: int, policy_predicate: str
    ) -> QueryResult:
        """Execute already-validated, canonical SQL as ``role``."""
        pool = self._pools.get(role)
        if pool is None:
            raise DbUnavailable(f"no connection configured for role {role}")
        try:
            async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
                await cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                await cur.execute("SELECT current_user")
                row = await cur.fetchone()
                executed_as = str(row[0]) if row else "unknown"
                if executed_as != role:
                    raise DbPermissionDenied("connection role mismatch")
                await cur.execute(f"EXPLAIN (FORMAT JSON) {sql}", prepare=False)
                plan_row = await cur.fetchone()
                evidence = extract_rls_evidence(plan_row[0] if plan_row else None, policy_predicate)
                await cur.execute(
                    f"SELECT * FROM ({sql}) AS governed_q LIMIT {int(row_limit) + 1}",
                    prepare=False,
                )
                columns = [d.name for d in (cur.description or [])]
                fetched = await cur.fetchall()
        except PoolTimeout as exc:
            raise DbUnavailable("database connection unavailable") from exc
        except pg_errors.QueryCanceled as exc:
            raise QueryTimeout(f"query exceeded {timeout_ms} ms statement timeout") from exc
        except pg_errors.InsufficientPrivilege as exc:
            raise DbPermissionDenied(_primary(exc)) from exc
        except psycopg.OperationalError as exc:
            raise DbUnavailable("database connection failed") from exc
        except psycopg.Error as exc:
            raise QueryFailed(f"{exc.sqlstate or 'error'}: {_primary(exc)}") from exc

        truncated = len(fetched) > row_limit
        rows = [[_jsonable(v) for v in r] for r in fetched[:row_limit]]
        return QueryResult(
            executed_as=executed_as,
            columns=columns,
            rows=rows,
            truncated=truncated,
            rls_evidence=evidence,
        )


def _primary(exc: psycopg.Error) -> str:
    diag = getattr(exc, "diag", None)
    message = getattr(diag, "message_primary", None) or str(exc)
    return message.splitlines()[0][:300]
