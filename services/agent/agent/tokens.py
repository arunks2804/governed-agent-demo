"""Verify the tokens the web tier hands to the agent.

- agent token  (aud governed-agent):        proves the run request came from the web tier for a user
- data token   (aud governed-data-service): forwarded to MCP for the agent's own tool calls
- runner token (aud governed-data-service): short-lived, given to sandboxed code for MCP calls

All three must be valid and name the same user and tenant. The agent never
chooses or alters identity; it only forwards tokens it was given.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import jwt

ISSUER = "governed-demo-idp"


class TokenError(Exception):
    pass


@dataclass(frozen=True)
class RunIdentity:
    sub: str
    tenant: str
    name: str


class TokenChecker:
    def __init__(self, public_key_path: Path) -> None:
        self._key = public_key_path.read_text()

    def _decode(self, token: str, audience: str) -> dict:
        try:
            return jwt.decode(
                token,
                self._key,
                algorithms=["EdDSA"],
                audience=audience,
                issuer=ISSUER,
                options={"require": ["exp", "iat", "sub", "aud", "iss", "jti"]},
            )
        except jwt.InvalidTokenError as exc:
            raise TokenError(f"{audience} token invalid: {type(exc).__name__}") from exc

    def check(self, agent_token: str, data_token: str, runner_token: str) -> RunIdentity:
        agent = self._decode(agent_token, "governed-agent")
        data = self._decode(data_token, "governed-data-service")
        runner = self._decode(runner_token, "governed-data-service")
        for label, claims in (("data", data), ("runner", runner)):
            if claims.get("sub") != agent.get("sub") or claims.get("tenant") != agent.get("tenant"):
                raise TokenError(f"{label} token identity does not match the run's identity")
        if data.get("ctx") != "agent" or runner.get("ctx") != "runner":
            raise TokenError("token context mismatch")
        return RunIdentity(sub=str(agent["sub"]), tenant=str(agent["tenant"]), name=str(agent.get("name", "")))
