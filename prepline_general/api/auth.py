import logging
import os

import jwt
from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

# Issuers minted by the orq platform. Mirrors the issuer handling in
# libs/go/fiber/middlewares/jwt.go (VerifyOrqToken) and the python-runner
# validate_jwt_token helper: service tokens use `orq.internal`, external API
# keys use `orq`/`orq.ai`, and user sessions use `kratos`.
_ALLOWED_ISSUERS = frozenset({"orq.internal", "orq.ai", "orq", "kratos"})

# RFC 6750: 401 responses to bearer-auth endpoints should carry this header.
_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}

# Small clock-skew allowance so tokens minted by another host aren't
# rejected at the exp/nbf boundary.
_LEEWAY_SECONDS = 30


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers=_BEARER_CHALLENGE,
    )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise _unauthorized("Missing or malformed Authorization header")
    return parts[1]


def require_orq_workspace(request: Request) -> str:
    """Verify an orq-platform JWT and return its workspace id.

    Replaces the shared static `UNSTRUCTURED_API_KEY` with the same token
    contract the rest of the platform uses: an HS256 signature over
    `JWT_SECRET` plus an allow-listed issuer. The verified `workspace_id`
    claim is returned so callers can scope storage lookups to the
    authenticated tenant (closes the /extract IDOR). There is no static-key
    fallback — every caller must present a workspace-scoped bearer token.

    Hardening notes:
      - `exp` and `iss` are *required* claims. Without `require=["exp"]`,
        PyJWT happily accepts a token that simply omits `exp`, i.e. a
        token that never expires.
      - If `JWT_AUDIENCE` is set, the `aud` claim is verified. Because the
        whole platform shares one HS256 secret, any token minted by any
        service (session tokens, runner tokens, API keys) is otherwise
        interchangeable here; an audience claim is the standard way to stop
        that cross-service token confusion. When unset, PyJWT's default
        still fails closed on tokens that carry an unexpected `aud`.
    """
    secret = os.environ.get("JWT_SECRET") or ""
    if not secret:
        # Log server-side; don't tell callers which env var is missing.
        logger.error("JWT_SECRET is not configured; refusing all requests")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Service authentication is not configured",
        )
    if len(secret) < 32:
        # HS256 with a short secret is brute-forceable offline from any
        # captured token. Warn loudly but keep serving so a rotation to a
        # short secret doesn't cause a platform-wide outage.
        logger.warning("JWT_SECRET is shorter than 32 bytes; rotate to a longer secret")

    audience = os.environ.get("JWT_AUDIENCE") or None

    token = _bearer_token(request)
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],  # pinned: never trust the token's own header
            audience=audience,
            leeway=_LEEWAY_SECONDS,
            # Note: verify_aud is deliberately left at its default. If a
            # token carries an `aud` claim we didn't expect, PyJWT rejects
            # it even when JWT_AUDIENCE is unset (fail closed).
            options={"require": ["exp", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token has expired") from None
    except jwt.InvalidTokenError:
        # Covers bad signature, missing required claims, bad aud/nbf/iat,
        # malformed token, alg=none attempts, etc. One generic message so
        # the response doesn't act as a validation oracle.
        raise _unauthorized("Invalid token") from None

    if claims.get("iss") not in _ALLOWED_ISSUERS:
        raise _unauthorized("Untrusted token issuer")

    workspace_id = claims.get("workspace_id") or claims.get("workspaceId")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token is missing workspace scope",
        )

    return workspace_id
