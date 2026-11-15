"""Authentication: verify Supabase-issued JWTs.

`verify_token` is plain Python (PyJWT only) so it can be unit-tested without FastAPI.
The FastAPI dependency lives in p2pilot.api.deps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import jwt

DEV_USER_ID = "00000000-0000-0000-0000-000000000001"


class AuthError(Exception):
    """Raised for any token problem. The message is safe to return to the client."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str | None = None
    token: str | None = None  # forwarded to PostgREST so RLS applies as this user
    claims: dict = field(default_factory=dict)


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(jwks_url, cache_keys=True)


def verify_token(
    token: str,
    *,
    audience: str,
    hs256_secret: str = "",
    jwks_url: str = "",
) -> Principal:
    """Verify signature, expiry and audience; return who the caller is.

    Supabase projects either sign with a shared HS256 secret (older / local stacks)
    or with asymmetric keys published at /auth/v1/.well-known/jwks.json.
    """
    try:
        if hs256_secret:
            claims = jwt.decode(token, hs256_secret, algorithms=["HS256"], audience=audience)
        elif jwks_url:
            key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["ES256", "RS256"], audience=audience)
        else:
            raise AuthError("Server has no JWT verification key configured")
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise AuthError("Token audience not accepted") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Invalid token") from exc

    sub = claims.get("sub")
    if not sub:
        raise AuthError("Token has no subject")
    return Principal(user_id=sub, email=claims.get("email"), token=token, claims=claims)


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AuthError("Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Authorization header must be 'Bearer <token>'")
    return token
