"""Caller identity verification.

The tenant used for authorization and connection selection comes only from a
verified, signed token. Request bodies and headers cannot override it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

MAX_TOKEN_CHARS = 4096
ALLOWED_CONTEXTS = frozenset({"agent", "runner", "direct"})


class IdentityError(Exception):
    """Token missing or invalid. The message is safe to return to callers."""


@dataclass(frozen=True)
class Identity:
    sub: str
    tenant: str
    ctx: str
    token_ref: str  # short hash of the jti, for logs; never the token itself


class TokenVerifier:
    def __init__(
        self,
        public_key_pem: bytes,
        issuer: str,
        audience: str,
        known_tenants: set[str],
        max_lifetime_s: int,
    ) -> None:
        key = load_pem_public_key(public_key_pem)
        if not isinstance(key, Ed25519PublicKey):
            raise RuntimeError("JWT public key must be Ed25519")
        self._key = key
        self._issuer = issuer
        self._audience = audience
        self._tenants = frozenset(known_tenants)
        self._max_lifetime_s = max_lifetime_s

    def verify_authorization_header(self, header: str | None) -> Identity:
        if not header:
            raise IdentityError("missing bearer token")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise IdentityError("authorization header must be 'Bearer <token>'")
        return self.verify(token.strip())

    def verify(self, token: str) -> Identity:
        if len(token) > MAX_TOKEN_CHARS:
            raise IdentityError("token too large")
        try:
            claims = jwt.decode(
                token,
                key=self._key,
                algorithms=["EdDSA"],  # pinned: no 'none', no HMAC key confusion
                audience=self._audience,
                issuer=self._issuer,
                leeway=5,
                options={"require": ["exp", "iat", "nbf", "sub", "aud", "iss", "jti"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise IdentityError("token expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise IdentityError("token not yet valid") from exc
        except jwt.InvalidAudienceError as exc:
            raise IdentityError("token audience not accepted") from exc
        except jwt.InvalidIssuerError as exc:
            raise IdentityError("token issuer not trusted") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise IdentityError(f"token missing claim: {exc.claim}") from exc
        except jwt.InvalidTokenError as exc:
            raise IdentityError("token signature or format invalid") from exc

        sub, tenant, ctx = claims.get("sub"), claims.get("tenant"), claims.get("ctx", "direct")
        if not isinstance(sub, str) or not sub:
            raise IdentityError("token subject invalid")
        if not isinstance(tenant, str) or tenant not in self._tenants:
            raise IdentityError("token tenant not recognised")
        if ctx not in ALLOWED_CONTEXTS:
            raise IdentityError("token context not recognised")
        lifetime = int(claims["exp"]) - int(claims["iat"])
        if lifetime > self._max_lifetime_s:
            raise IdentityError("token lifetime exceeds policy maximum")
        jti_hash = hashlib.sha256(str(claims["jti"]).encode()).hexdigest()[:12]
        return Identity(sub=sub, tenant=tenant, ctx=ctx, token_ref=jti_hash)
