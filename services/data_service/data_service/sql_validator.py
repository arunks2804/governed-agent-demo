"""AST-based validation for generated SQL.

Version-one scope: a single read-only SELECT (optionally WITH / UNION / joins /
subqueries) over the approved ``orders`` table. Validation is an *allowlist*
over sqlglot expression node types: anything not explicitly supported is
rejected with a named reason. The SQL that executes is regenerated from the
validated AST (comments stripped, tables schema-qualified), so what was checked
is what runs.

This is not the only boundary: PostgreSQL roles, grants and RLS enforce tenant
isolation independently, and OPA authorizes every request.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

APPROVED_SCHEMA = "app"
APPROVED_TABLES = frozenset({"orders"})
TENANT_COLUMN = "tenant_id"
MAX_AST_NODES = 400
MAX_TABLE_REFS = 4  # bounds join fan-out (cost), independent of the statement timeout
MAX_DEPTH = 40


class SqlRejected(Exception):
    def __init__(self, category: str, reason: str) -> None:
        super().__init__(reason)
        self.category = category
        self.reason = reason


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str  # canonical SQL regenerated from the AST
    tables: list[str]
    columns: list[str]
    requested_tenants: list[str]  # tenant literals compared with tenant_id via = / IN
    statement: str = "select"
    node_types: list[str] = field(default_factory=list)


def _node_types(*names: str) -> frozenset[type[exp.Expr]]:
    found = []
    for name in names:
        cls = getattr(exp, name, None)
        if isinstance(cls, type) and issubclass(cls, exp.Expr):
            found.append(cls)
    return frozenset(found)


# Structural / relational nodes.
_STRUCTURE = _node_types(
    "Select",
    "Union",
    "From",
    "Join",
    "Table",
    "TableAlias",
    "Alias",
    "Column",
    "Identifier",
    "Star",
    "Where",
    "Group",
    "Having",
    "Order",
    "Ordered",
    "Limit",
    "Offset",
    "Subquery",
    "With",
    "CTE",
    "Distinct",
    "Window",
    "Tuple",
)
# Scalar expressions and operators.
_SCALAR = _node_types(
    "Literal",
    "Boolean",
    "Null",
    "EQ",
    "NEQ",
    "GT",
    "GTE",
    "LT",
    "LTE",
    "And",
    "Or",
    "Not",
    "Paren",
    "In",
    "Between",
    "Like",
    "ILike",
    "Is",
    "Add",
    "Sub",
    "Mul",
    "Div",
    "Mod",
    "Neg",
    "Case",
    "If",
    "Cast",
    "DataType",
    "DPipe",
    "Interval",
)
# Approved functions only. Unknown functions parse as exp.Anonymous and are rejected.
_FUNCTIONS = _node_types(
    "Count",
    "Sum",
    "Avg",
    "Min",
    "Max",
    "Round",
    "Coalesce",
    "Lower",
    "Upper",
    "Abs",
    "Length",
    "TimestampTrunc",
    "DateTrunc",
    "Extract",
    "RowNumber",
    "Rank",
    "DenseRank",
    "Greatest",
    "Least",
    "CurrentDate",
    "CurrentTimestamp",
)
ALLOWED_NODES = _STRUCTURE | _SCALAR | _FUNCTIONS

# exp.Var (bare keywords) is only meaningful as a date-part / interval unit.
_VAR_PARENTS = _node_types("Extract", "TimestampTrunc", "DateTrunc", "Interval", "DataType")

ALLOWED_CAST_TYPES = frozenset(
    {
        "DATE",
        "TIMESTAMP",
        "TEXT",
        "VARCHAR",
        "CHAR",
        "INT",
        "BIGINT",
        "SMALLINT",
        "DECIMAL",
        "DOUBLE",
        "FLOAT",
        "BOOLEAN",
    }
)

_WRITE_TYPES = _node_types(
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "TruncateTable",
    "Copy",
)
_PRIVILEGE_TYPES = _node_types("Grant", "Revoke")
_WRITE_KEYWORDS = (
    "ALTER",
    "CREATE",
    "DROP",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "COPY",
    "VACUUM",
    "REINDEX",
    "CLUSTER",
    "COMMENT",
    "REFRESH",
    "LOCK",
    "CALL",
    "IMPORT",
)
_SESSION_TYPES = _node_types("Set", "SetItem", "Use", "Transaction", "Commit", "Rollback")


def _classify_statement(root: exp.Expr) -> SqlRejected | None:
    if isinstance(root, exp.Select | exp.Union):
        return None
    name = type(root).__name__
    if isinstance(root, tuple(_WRITE_TYPES)):
        return SqlRejected("write", f"write/DDL statements are not allowed ({name})")
    if isinstance(root, tuple(_PRIVILEGE_TYPES)):
        return SqlRejected("privilege_change", f"privilege changes are not allowed ({name})")
    try:
        text = root.sql(dialect="postgres").upper()
    except Exception:  # noqa: BLE001 - classification only; still rejected below
        text = ""
    if isinstance(root, exp.Command) and text.startswith(_WRITE_KEYWORDS):
        return SqlRejected("write", f"write/DDL statements are not allowed ({text.split()[0]})")
    if "ROLE" in text or "SESSION AUTHORIZATION" in text:
        return SqlRejected("role_change", "role changes are not allowed")
    if isinstance(root, tuple(_SESSION_TYPES)) or text.startswith(("SET", "RESET")):
        return SqlRejected("session_change", f"session/transaction control is not allowed ({name})")
    return SqlRejected("unsupported_statement", f"only SELECT queries are supported (got {name})")


def _depth(node: exp.Expr) -> int:
    d = 0
    parent = node.parent
    while parent is not None:
        d += 1
        parent = parent.parent
    return d


def _is_tenant_column(node: exp.Expr) -> bool:
    return isinstance(node, exp.Column) and node.name.lower() == TENANT_COLUMN


def _string_literal(node: exp.Expr) -> str | None:
    if isinstance(node, exp.Literal) and node.is_string:
        return str(node.this).strip().lower()
    return None


def _requested_tenants(root: exp.Expr) -> set[str]:
    tenants: set[str] = set()
    for eq in root.find_all(exp.EQ):
        left, right = eq.left, eq.right
        for col, lit in ((left, right), (right, left)):
            value = _string_literal(lit)
            if _is_tenant_column(col) and value is not None:
                tenants.add(value)
    for in_expr in root.find_all(exp.In):
        if _is_tenant_column(in_expr.this):
            for item in in_expr.expressions:
                value = _string_literal(item)
                if value is not None:
                    tenants.add(value)
    return tenants


def validate_sql(sql: str, max_chars: int = 8000) -> ValidatedQuery:
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected("empty", "SQL is empty")
    if len(sql) > max_chars:
        raise SqlRejected("too_large", f"SQL exceeds {max_chars} characters")
    if "\x00" in sql:
        raise SqlRejected("parse_error", "SQL contains a NUL byte")

    try:
        statements = [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    except (ParseError, TokenError) as exc:
        raise SqlRejected("parse_error", f"SQL could not be parsed: {str(exc).splitlines()[0][:200]}") from exc
    except RecursionError as exc:
        raise SqlRejected("too_complex", "SQL is nested too deeply") from exc

    if not statements:
        raise SqlRejected("empty", "SQL is empty")
    if len(statements) > 1:
        raise SqlRejected("multiple_statements", f"exactly one statement allowed, got {len(statements)}")
    root = statements[0]

    rejection = _classify_statement(root)
    if rejection is not None:
        raise rejection

    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    tables: set[str] = set()
    columns: set[str] = set()
    count = 0

    for node in root.walk():
        count += 1
        if count > MAX_AST_NODES:
            raise SqlRejected("too_complex", f"query exceeds {MAX_AST_NODES} syntax nodes")
        node_type = type(node)

        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise SqlRejected("unsupported_syntax", "WITH RECURSIVE is not supported")
        if node_type is exp.Anonymous:
            raise SqlRejected("unsupported_function", f"function '{node.name}' is not approved")
        if node_type is exp.Var:
            if not isinstance(node.parent, tuple(_VAR_PARENTS)):
                raise SqlRejected("unsupported_syntax", f"bare keyword '{node.name}' is not supported")
            continue
        if node_type not in ALLOWED_NODES:
            if isinstance(node, exp.Func):
                raise SqlRejected("unsupported_function", f"function {node_type.__name__} is not approved")
            raise SqlRejected("unsupported_syntax", f"unsupported SQL construct: {node_type.__name__}")
        if _depth(node) > MAX_DEPTH:
            raise SqlRejected("too_complex", "query is nested too deeply")

        if isinstance(node, exp.DataType):
            type_name = node.this.name if hasattr(node.this, "name") else str(node.this)
            if type_name.upper() not in ALLOWED_CAST_TYPES:
                raise SqlRejected("unsupported_syntax", f"cast to type {type_name} is not supported")
        elif isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier):
                raise SqlRejected("unsupported_syntax", "table expressions/functions are not supported")
            name = node.name.lower()
            schema = (node.db or "").lower()
            if node.catalog:
                raise SqlRejected("unapproved_table", "catalog-qualified tables are not allowed")
            if not schema and name in cte_names:
                continue
            if schema not in ("", APPROVED_SCHEMA) or name not in APPROVED_TABLES:
                shown = f"{schema}.{name}" if schema else name
                raise SqlRejected("unapproved_table", f"table '{shown}' is not an approved dataset table")
            tables.add(name)
        elif isinstance(node, exp.Column):
            columns.add(node.name.lower())
        elif isinstance(node, exp.Select) and node.args.get("into") is not None:
            raise SqlRejected("write", "SELECT INTO is not allowed")

    table_refs = sum(1 for _ in root.find_all(exp.Table))
    if table_refs > MAX_TABLE_REFS:
        raise SqlRejected("too_complex", f"query references {table_refs} tables; at most {MAX_TABLE_REFS} allowed")
    if not tables:
        raise SqlRejected("unapproved_table", "query must read from an approved table (orders)")

    # Pin unqualified approved tables to the approved schema so search_path cannot redirect them.
    for table in root.find_all(exp.Table):
        if not table.db and table.name.lower() in APPROVED_TABLES and table.name.lower() not in cte_names:
            table.set("db", exp.to_identifier(APPROVED_SCHEMA))

    canonical = root.sql(dialect="postgres", comments=False)
    return ValidatedQuery(
        sql=canonical,
        tables=sorted(tables),
        columns=sorted(columns),
        requested_tenants=sorted(_requested_tenants(root)),
        node_types=sorted({type(n).__name__ for n in root.walk()}),
    )
