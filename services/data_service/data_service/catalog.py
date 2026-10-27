"""Static dataset catalog: what agents may learn about each dataset."""

from __future__ import annotations

from typing import Any

DATASETS: dict[str, dict[str, Any]] = {
    "orders": {
        "name": "orders",
        "description": "Customer orders for your organisation (synthetic demo data).",
        "table": "orders",
        "columns": [
            {"name": "order_id", "type": "bigint", "description": "Unique order identifier"},
            {"name": "tenant_id", "type": "text", "description": "Owning tenant (always your own)"},
            {"name": "customer_name", "type": "text", "description": "Customer display name"},
            {"name": "amount", "type": "numeric(12,2)", "description": "Order amount"},
            {
                "name": "status",
                "type": "text",
                "description": "pending | shipped | delivered | cancelled | refunded",
            },
            {"name": "order_date", "type": "date", "description": "Date the order was placed"},
        ],
        "supported_sql": (
            "One read-only SELECT over 'orders' (WITH, joins, subqueries, UNION, GROUP BY, "
            "ORDER BY, window ranks). Approved functions: count, sum, avg, min, max, round, "
            "coalesce, lower, upper, abs, length, date_trunc, extract, current_date, now. "
            "Results are capped by policy."
        ),
    }
}

TABLE_TO_DATASET = {meta["table"]: name for name, meta in DATASETS.items()}
