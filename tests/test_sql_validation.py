"""Unit tests for the AST-based SQL validator (the real data-service module)."""

from __future__ import annotations

import pytest

from data_service.sql_validator import SqlRejected, validate_sql

SUPPORTED = [
    "SELECT * FROM orders",
    "SELECT * FROM app.orders",
    "select order_id, amount from orders where status = 'shipped' order by amount desc limit 5",
    "SELECT status, count(*), sum(amount) FROM orders GROUP BY status HAVING count(*) > 1",
    "SELECT date_trunc('month', order_date) AS m, round(avg(amount), 2) FROM orders GROUP BY 1",
    "SELECT EXTRACT(YEAR FROM order_date) AS y, count(*) FROM orders GROUP BY 1",
    "WITH big AS (SELECT * FROM orders WHERE amount > 100) SELECT count(*) FROM big",
    "SELECT o.order_id FROM orders o JOIN orders p ON o.customer_name = p.customer_name",
    "SELECT * FROM orders o LEFT JOIN orders p ON o.order_id = p.order_id",
    "SELECT * FROM (SELECT customer_name, sum(amount) AS total FROM orders GROUP BY 1) t WHERE total > 10",
    "SELECT * FROM orders WHERE amount > (SELECT avg(amount) FROM orders)",
    "SELECT * FROM orders WHERE customer_name IN (SELECT customer_name FROM orders WHERE status = 'refunded')",
    "SELECT customer_name FROM orders UNION SELECT customer_name FROM orders",
    "SELECT rank() OVER (PARTITION BY status ORDER BY amount DESC) AS r, order_id FROM orders",
    "SELECT CASE WHEN amount > 100 THEN 'big' ELSE 'small' END AS size FROM orders",
    "SELECT coalesce(status, 'n/a'), lower(customer_name), upper(status) FROM orders",
    "SELECT * FROM orders WHERE customer_name ILIKE '%bake%' AND amount BETWEEN 10 AND 500",
    "SELECT * FROM orders WHERE order_date >= DATE '2025-06-01'",
    "SELECT * FROM orders WHERE order_date > current_date - interval '90 days'",
    "SELECT amount::text FROM orders",
    "SELECT count(DISTINCT customer_name) FROM orders",
    "SELECT * FROM orders -- trailing comment",
    "SELECT * FROM orders;",
]


@pytest.mark.parametrize("sql", SUPPORTED)
def test_supported_queries_accepted(sql: str) -> None:
    result = validate_sql(sql)
    assert result.tables == ["orders"]
    assert "app.orders" in result.sql  # schema pinned
    assert "--" not in result.sql and "/*" not in result.sql  # comments stripped


@pytest.mark.parametrize(
    ("sql", "category"),
    [
        ("SELECT 1; SELECT 2", "multiple_statements"),
        ("SELECT * FROM orders; DROP TABLE orders", "multiple_statements"),
        ("SELECT * FROM orders; UPDATE orders SET amount = 0", "multiple_statements"),
        ("UPDATE orders SET amount = 0", "write"),
        ("DELETE FROM orders", "write"),
        ("INSERT INTO orders VALUES (1, 'acme', 'x', 1, 'pending', current_date)", "write"),
        ("TRUNCATE orders", "write"),
        ("DROP TABLE orders", "write"),
        ("CREATE TABLE x AS SELECT * FROM orders", "write"),
        ("ALTER TABLE orders DISABLE ROW LEVEL SECURITY", "write"),
        ("COPY orders TO '/tmp/x'", "write"),
        ("SELECT * INTO stolen FROM orders", "write"),
        ("SET ROLE northstar_reader", "role_change"),
        ("RESET ROLE", "role_change"),
        ("SET SESSION AUTHORIZATION postgres", "role_change"),
        ("SET row_security = off", "session_change"),
        ("SET search_path = public", "session_change"),
        ("GRANT SELECT ON orders TO acme_reader", "privilege_change"),
        ("REVOKE SELECT ON orders FROM northstar_reader", "privilege_change"),
        ("SELECT * FROM billing_accounts", "unapproved_table"),
        ("SELECT * FROM app.billing_accounts", "unapproved_table"),
        ("SELECT * FROM pg_catalog.pg_roles", "unapproved_table"),
        ("SELECT * FROM pg_shadow", "unapproved_table"),
        ("SELECT * FROM information_schema.tables", "unapproved_table"),
        ("SELECT (SELECT iban_synthetic FROM billing_accounts LIMIT 1) FROM orders", "unapproved_table"),
        ("SELECT 1", "unapproved_table"),
        ("SELECT pg_sleep(10) FROM orders", "unsupported_function"),
        ("SELECT set_config('app.tenant', 'northstar', false) FROM orders", "unsupported_function"),
        ("SELECT current_setting('app.tenant') FROM orders", "unsupported_function"),
        ("SELECT pg_read_file('/etc/passwd') FROM orders", "unsupported_function"),
        ("SELECT lo_import('/etc/passwd') FROM orders", "unsupported_function"),
        ("SELECT dblink('host=x', 'select 1') FROM orders", "unsupported_function"),
        ("SELECT current_user FROM orders", "unsupported_function"),
        ("SELECT version() FROM orders", "unsupported_function"),
        ("SELECT * FROM orders WHERE customer_name ~ 'a'", "unsupported_function"),
        ("SELECT * FROM generate_series(1, 10)", "unsupported_syntax"),
        ("SELECT * FROM orders FOR UPDATE", "unsupported_syntax"),
        ("SELECT * FROM orders TABLESAMPLE SYSTEM (10)", "unsupported_syntax"),
        ("SELECT amount::regclass FROM orders", "unsupported_syntax"),
        ("WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x", "unsupported_syntax"),
        ("SELECT * FROM orders INTERSECT SELECT * FROM orders", "unsupported_statement"),
        ("EXPLAIN SELECT * FROM orders", "unsupported_statement"),
        ("DO $$ BEGIN PERFORM 1; END $$", "unsupported_statement"),
        ("BEGIN", "session_change"),
        ("", "empty"),
        ("   ", "empty"),
        ("SELECT * FROM", "parse_error"),
        ("SELEC * FROM orders", "parse_error"),
    ],
)
def test_rejected_with_category(sql: str, category: str) -> None:
    with pytest.raises(SqlRejected) as info:
        validate_sql(sql)
    assert info.value.category == category, info.value.reason


@pytest.mark.parametrize(
    "sql",
    [
        "WITH RECURSIVE t(n) AS (SELECT 1 FROM orders UNION ALL SELECT n + 1 FROM t WHERE n < 100000000) "
        "SELECT count(*) FROM t",
        "WITH RECURSIVE t AS (SELECT order_id FROM orders UNION ALL SELECT order_id FROM t) SELECT * FROM t",
    ],
)
def test_recursive_cte_rejected(sql: str) -> None:
    with pytest.raises(SqlRejected) as info:
        validate_sql(sql)
    assert info.value.category == "unsupported_syntax"
    assert "RECURSIVE" in info.value.reason


def test_table_reference_cap() -> None:
    validate_sql("SELECT count(*) FROM orders a, orders b, orders c, orders d")
    with pytest.raises(SqlRejected) as info:
        validate_sql("SELECT count(*) FROM orders a, orders b, orders c, orders d, orders e")
    assert info.value.category == "too_complex"


def test_oversized_sql_rejected() -> None:
    with pytest.raises(SqlRejected) as info:
        validate_sql("SELECT * FROM orders WHERE " + " OR ".join(["amount = 1"] * 2000), max_chars=8000)
    assert info.value.category == "too_large"


def test_too_many_nodes_rejected() -> None:
    sql = "SELECT * FROM orders WHERE " + " OR ".join(f"amount = {i}" for i in range(300))
    with pytest.raises(SqlRejected) as info:
        validate_sql(sql, max_chars=100_000)
    assert info.value.category == "too_complex"


def test_deep_nesting_rejected() -> None:
    sql = "SELECT * FROM orders WHERE amount = " + "(" * 300 + "1" + ")" * 300
    with pytest.raises(SqlRejected) as info:
        validate_sql(sql, max_chars=100_000)
    assert info.value.category in ("too_complex", "parse_error")


def test_nul_byte_rejected() -> None:
    with pytest.raises(SqlRejected):
        validate_sql("SELECT * FROM orders\x00; DROP TABLE orders")


@pytest.mark.parametrize(
    ("sql", "tenants"),
    [
        ("SELECT * FROM orders WHERE tenant_id = 'northstar'", ["northstar"]),
        ("SELECT * FROM orders WHERE 'NorthStar' = tenant_id", ["northstar"]),
        ("SELECT * FROM orders o WHERE o.tenant_id IN ('acme', 'northstar')", ["acme", "northstar"]),
        ("SELECT * FROM orders WHERE tenant_id = 'acme'", ["acme"]),
        ("SELECT * FROM orders WHERE tenant_id <> 'acme'", []),
        ("SELECT * FROM orders", []),
    ],
)
def test_requested_tenants_extracted_from_ast(sql: str, tenants: list[str]) -> None:
    assert validate_sql(sql).requested_tenants == tenants


def test_canonical_sql_is_regenerated_not_passed_through() -> None:
    result = validate_sql("select /* x */ *   from   orders  where amount>1")
    assert result.sql == "SELECT * FROM app.orders WHERE amount > 1"


def test_cte_name_shadowing_cannot_reach_other_tables() -> None:
    with pytest.raises(SqlRejected):
        validate_sql("WITH orders AS (SELECT * FROM billing_accounts) SELECT * FROM orders")
