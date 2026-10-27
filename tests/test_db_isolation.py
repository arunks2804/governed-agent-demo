"""Database-level isolation, tested with the real reader credentials and NO
data service in the path. Proves PostgreSQL enforces tenant isolation on its
own, independent of SQL validation and OPA."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors

from conftest import EXPECTED_ROWS, ROLES, pg_connect

TENANTS = list(ROLES)


@pytest.mark.parametrize("tenant", TENANTS)
def test_unfiltered_select_returns_only_own_tenant(tenant: str) -> None:
    with pg_connect(ROLES[tenant]) as conn:
        rows = conn.execute("SELECT tenant_id, count(*) FROM app.orders GROUP BY 1").fetchall()
    assert rows == [(tenant, EXPECTED_ROWS[tenant])]


@pytest.mark.parametrize("tenant", TENANTS)
def test_explicit_other_tenant_filter_returns_nothing(tenant: str) -> None:
    other = next(t for t in TENANTS if t != tenant)
    with pg_connect(ROLES[tenant]) as conn:
        count = conn.execute("SELECT count(*) FROM app.orders WHERE tenant_id = %s", (other,)).fetchone()
    assert count == (0,)


@pytest.mark.parametrize("tenant", TENANTS)
def test_rls_predicate_visible_in_plan(tenant: str) -> None:
    with pg_connect(ROLES[tenant]) as conn:
        plan = "\n".join(r[0] for r in conn.execute("EXPLAIN SELECT * FROM app.orders").fetchall())
    assert f"tenant_id = '{tenant}'::text" in plan


@pytest.mark.parametrize("tenant", TENANTS)
def test_cannot_switch_to_other_tenant_role(tenant: str) -> None:
    other = next(t for t in TENANTS if t != tenant)
    with pg_connect(ROLES[tenant]) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(f"SET ROLE {ROLES[other]}")


@pytest.mark.parametrize("target", ["postgres", "orders_owner"])
def test_cannot_assume_privileged_roles(target: str) -> None:
    with pg_connect("acme_reader") as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(f"SET ROLE {target}")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(f"SET SESSION AUTHORIZATION {target}")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE app.orders SET amount = 0",
        "DELETE FROM app.orders",
        "INSERT INTO app.orders VALUES (9999, 'acme', 'x', 1, 'pending', current_date)",
        "TRUNCATE app.orders",
    ],
)
def test_writes_fail_even_in_read_write_transaction(statement: str) -> None:
    with pg_connect("acme_reader") as conn:
        conn.autocommit = False
        conn.execute("SET TRANSACTION READ WRITE")  # override the role's read-only default
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)
        conn.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE app.orders DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE app.orders NO FORCE ROW LEVEL SECURITY",
        "DROP POLICY acme_reader_isolation ON app.orders",
        "ALTER POLICY acme_reader_isolation ON app.orders USING (true)",
        "CREATE POLICY sneaky ON app.orders FOR SELECT TO acme_reader USING (true)",
        "GRANT SELECT ON app.billing_accounts TO acme_reader",
    ],
)
def test_cannot_change_policies_or_table(statement: str) -> None:
    with pg_connect("acme_reader") as conn:
        conn.autocommit = False
        conn.execute("SET TRANSACTION READ WRITE")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)
        conn.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE ROLE sneaky LOGIN PASSWORD 'x'",
        "ALTER ROLE acme_reader BYPASSRLS",
        "ALTER ROLE acme_reader SUPERUSER",
        "GRANT northstar_reader TO acme_reader",
        "CREATE TABLE public.exfil (x text)",
        "CREATE TEMP TABLE exfil (x text)",
        "CREATE SCHEMA sneaky",
    ],
)
def test_cannot_create_roles_or_objects(statement: str) -> None:
    with pg_connect("acme_reader") as conn:
        conn.autocommit = False
        conn.execute("SET TRANSACTION READ WRITE")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)
        conn.rollback()


def test_regrant_without_grant_option_has_no_effect() -> None:
    """Holding SELECT without GRANT OPTION: PostgreSQL warns and grants nothing."""
    with pg_connect("acme_reader") as conn:
        conn.autocommit = False
        conn.execute("SET TRANSACTION READ WRITE")
        conn.execute("GRANT SELECT ON app.orders TO PUBLIC")
        conn.commit()
        public_grants = conn.execute(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE grantee = 'PUBLIC' AND table_schema = 'app'"
        ).fetchone()
    assert public_grants == (0,)


def test_disabling_row_security_errors_instead_of_bypassing() -> None:
    with pg_connect("acme_reader") as conn:
        conn.execute("SET row_security = off")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM app.orders")


def test_session_settings_do_not_affect_visibility() -> None:
    """Isolation is bound to the login role, not to any agent-editable setting."""
    with pg_connect("acme_reader") as conn:
        conn.execute("SELECT set_config('app.tenant', 'northstar', false)")
        conn.execute("SET search_path = public, app")
        rows = conn.execute("SELECT DISTINCT tenant_id FROM app.orders").fetchall()
    assert rows == [("acme",)]


def test_unapproved_table_denied_by_privileges() -> None:
    with pg_connect("acme_reader") as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("SELECT * FROM app.billing_accounts")


@pytest.mark.parametrize("role", list(ROLES.values()))
def test_reader_role_attributes_are_minimal(role: str) -> None:
    with pg_connect(role) as conn:
        attrs = conn.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication, rolinherit "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        owns = conn.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner WHERE r.rolname = current_user"
        ).fetchone()
        memberships = conn.execute(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = current_user"
        ).fetchone()
        privileges = conn.execute(
            "SELECT table_schema || '.' || table_name, privilege_type "
            "FROM information_schema.role_table_grants WHERE grantee = current_user ORDER BY 1, 2"
        ).fetchall()
    assert attrs == (False, False, False, False, False, False)
    assert owns == (0,)
    assert memberships == (0,)
    assert privileges == [("app.orders", "SELECT")]


def test_wrong_password_rejected() -> None:
    with pytest.raises(psycopg.OperationalError):
        pg_connect("acme_reader", password="not-the-password")


def test_reader_cannot_use_other_tenant_password() -> None:
    from conftest import secret

    with pytest.raises(psycopg.OperationalError):
        pg_connect("acme_reader", password=secret("northstar_reader_password"))
