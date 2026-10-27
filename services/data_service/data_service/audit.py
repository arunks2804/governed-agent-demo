"""Structured enforcement log.

Every entry names the enforcement point that acted. Entries never contain
tokens, passwords, or result rows; SQL text is truncated.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger("data_service.audit")

ENFORCEMENT_POINTS = (
    "identity",
    "rate_limit",
    "request_schema",
    "sql_validation",
    "opa",
    "connection_selection",
    "postgres",
    "result_limits",
)
MAX_SQL_LOG_CHARS = 500
# Field names that must never reach the log, whatever a future caller passes.
FORBIDDEN_FIELDS = frozenset({"token", "authorization", "password", "secret", "jwt", "bearer", "rows"})


def truncate_sql(sql: str) -> str:
    return sql if len(sql) <= MAX_SQL_LOG_CHARS else sql[:MAX_SQL_LOG_CHARS] + "...[truncated]"


class AuditLog:
    def __init__(self, maxlen: int = 5000) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, trace_id: str, point: str, outcome: str, **detail: Any) -> dict[str, Any]:
        if point not in ENFORCEMENT_POINTS:
            raise ValueError(f"unknown enforcement point {point}")
        entry = {
            "ts": round(time.time(), 3),
            "service": "data-service",
            "trace_id": trace_id,
            "enforcement_point": point,
            "outcome": outcome,
            **{k: ("[redacted]" if k.lower() in FORBIDDEN_FIELDS else v) for k, v in detail.items() if v is not None},
        }
        with self._lock:
            self._entries.append(entry)
        logger.info(json.dumps(entry, default=str, sort_keys=True))
        return entry

    def query(self, trace_id: str | None, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            items = [e for e in self._entries if trace_id is None or e["trace_id"] == trace_id]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
