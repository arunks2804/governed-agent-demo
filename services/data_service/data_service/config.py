"""Settings for the trusted data service.

Secrets are read from files mounted by Docker (``/run/secrets/*``), never from
request input. Only this service holds the reader-role database passwords.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))


def read_secret(name: str) -> str:
    value = (SECRETS_DIR / name).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"secret {name} is empty")
    return value


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    # tenant -> PostgreSQL login role. Static, operator-controlled mapping.
    tenant_roles: dict[str, str]
    role_passwords: dict[str, str] = field(repr=False)
    opa_url: str
    opa_token: str = field(repr=False)
    opa_timeout_s: float
    jwt_public_key_pem: bytes = field(repr=False)
    jwt_issuer: str
    jwt_audience: str
    jwt_max_lifetime_s: int
    audit_token: str = field(repr=False)
    query_timeout_ms: int
    max_rows_cap: int
    max_sql_chars: int
    rate_limit_burst: int
    rate_limit_per_s: float


def load_settings() -> Settings:
    tenant_roles: dict[str, str] = json.loads(
        os.environ.get("TENANT_ROLES", '{"acme": "acme_reader", "northstar": "northstar_reader"}')
    )
    return Settings(
        db_host=os.environ.get("DB_HOST", "postgres"),
        db_port=int(os.environ.get("DB_PORT", "5432")),
        db_name=os.environ.get("DB_NAME", "governed"),
        tenant_roles=tenant_roles,
        role_passwords={role: read_secret(f"{role}_password") for role in tenant_roles.values()},
        opa_url=os.environ.get("OPA_URL", "http://opa:8181"),
        opa_token=read_secret("opa_data_service_token"),
        opa_timeout_s=float(os.environ.get("OPA_TIMEOUT_S", "1.5")),
        jwt_public_key_pem=(SECRETS_DIR / "jwt_public.pem").read_bytes(),
        jwt_issuer=os.environ.get("JWT_ISSUER", "governed-demo-idp"),
        jwt_audience=os.environ.get("JWT_AUDIENCE", "governed-data-service"),
        jwt_max_lifetime_s=int(os.environ.get("JWT_MAX_LIFETIME_S", "900")),
        audit_token=read_secret("audit_token"),
        query_timeout_ms=int(os.environ.get("QUERY_TIMEOUT_MS", "3000")),
        max_rows_cap=int(os.environ.get("MAX_ROWS_CAP", "500")),
        max_sql_chars=int(os.environ.get("MAX_SQL_CHARS", "8000")),
        rate_limit_burst=int(os.environ.get("RATE_LIMIT_BURST", "150")),
        rate_limit_per_s=float(os.environ.get("RATE_LIMIT_PER_S", "25")),
    )
