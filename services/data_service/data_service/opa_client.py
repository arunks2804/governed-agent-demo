"""OPA decision client. Fails closed: any error, timeout, non-200 response or
malformed decision is treated as "policy unavailable" and the request is denied.
No decisions are cached, so a policy change applies to the very next request."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

DECISION_PATH = "/v1/data/governed/authz/decision"


class PolicyUnavailable(Exception):
    """OPA could not produce a usable decision."""


@dataclass(frozen=True)
class Decision:
    allow: bool
    reasons: list[str]
    db_role: str | None
    row_limit: int
    policy_version: str | None
    visible_datasets: list[str] = field(default_factory=list)


def parse_decision(result: Any) -> Decision:
    if not isinstance(result, dict):
        raise PolicyUnavailable("OPA returned no decision document")
    allow = result.get("allow")
    if not isinstance(allow, bool):
        raise PolicyUnavailable("OPA decision is missing a boolean 'allow'")
    reasons = result.get("reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise PolicyUnavailable("OPA decision has malformed 'reasons'")
    db_role = result.get("db_role")
    row_limit = result.get("row_limit", 0)
    if db_role is not None and not isinstance(db_role, str):
        raise PolicyUnavailable("OPA decision has malformed 'db_role'")
    if not isinstance(row_limit, int) or row_limit < 0:
        raise PolicyUnavailable("OPA decision has malformed 'row_limit'")
    visible = result.get("visible_datasets", [])
    if not isinstance(visible, list):
        raise PolicyUnavailable("OPA decision has malformed 'visible_datasets'")
    version = result.get("policy_version")
    return Decision(
        allow=allow,
        reasons=reasons,
        db_role=db_role if isinstance(db_role, str) else None,
        row_limit=row_limit,
        policy_version=version if isinstance(version, str) else None,
        visible_datasets=[str(v) for v in visible],
    )


class OpaClient:
    def __init__(self, base_url: str, token: str, timeout_s: float) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s),
            headers={"Authorization": f"Bearer {token}"},
        )

    async def decide(self, policy_input: dict[str, Any]) -> Decision:
        try:
            response = await self._client.post(DECISION_PATH, json={"input": policy_input})
        except httpx.HTTPError as exc:
            raise PolicyUnavailable(f"OPA unreachable ({type(exc).__name__})") from exc
        if response.status_code != 200:
            raise PolicyUnavailable(f"OPA returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise PolicyUnavailable("OPA returned invalid JSON") from exc
        return parse_decision(body.get("result") if isinstance(body, dict) else None)

    async def healthy(self) -> bool:
        try:
            response = await self._client.get("/health")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._client.aclose()
