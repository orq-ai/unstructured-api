import logging
import os

import jwt
from fastapi import HTTPException, Request, status

from .metrics import AUTH_TOTAL
from .request_context import request_context

logger = logging.getLogger("unstructured_api")

# Issuers minted by the orq platform. Mirrors the issuer handling in
# libs/go/fiber/middlewares/jwt.go (VerifyOrqToken) and the python-runner
# validate_jwt_token helper: service tokens use `orq.internal`, external API
# keys use `orq`/`orq.ai`, and user sessions use `kratos`.
_ALLOWED_ISSUERS = frozenset({"orq.internal", "orq.ai", "orq", "kratos"})


def _reject(request: Request, *, reason: str, status_code: int, detail: str) -> HTTPException:
    """Record an auth failure (metric + structured log) and build the exception.

    Logged context is deliberately non-sensitive: never the token, only the
    reason, request path, and best-effort client host so we can spot probing /
    brute-force without leaking credentials.
    """
    AUTH_TOTAL.labels(result="failure", reason=reason).inc()
    ctx = request_context(request)
    logger.warning(
        "auth rejected: reason=%s path=%s ip=%s country=%s cf_ray=%s",
        reason,
        request.url.path,
        ctx["ip"],
        ctx["country"],
        ctx["ray"],
    )
    return HTTPException(status_code=status_code, detail=detail)


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise _reject(
            request,
            reason="missing_header",
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    return parts[1]


def require_orq_workspace(request: Request) -> str:
    """Verify an orq-platform JWT and return its workspace id.

    Replaces the shared static `UNSTRUCTURED_API_KEY` with the same token
    contract the rest of the platform uses: an HS256 signature over
    `JWT_SECRET` plus an allow-listed issuer. The verified `workspace_id`
    claim is returned so callers can scope storage lookups to the
    authenticated tenant (closes the /extract IDOR). There is no static-key
    fallback — every caller must present a workspace-scoped bearer token.
    """
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise _reject(
            request,
            reason="not_configured",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Service authentication is not configured",
        )

    token = _bearer_token(request)
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise _reject(
            request,
            reason="expired",
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )
    except jwt.InvalidTokenError:
        raise _reject(
            request,
            reason="invalid_token",
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    if claims.get("iss") not in _ALLOWED_ISSUERS:
        # The issuer is safe to log (not a secret) and is the highest-signal
        # field for spotting forged / cross-environment tokens.
        logger.warning("auth rejected: untrusted issuer iss=%r", claims.get("iss"))
        raise _reject(
            request,
            reason="untrusted_issuer",
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Untrusted token issuer",
        )

    workspace_id = claims.get("workspace_id") or claims.get("workspaceId")
    if not workspace_id:
        raise _reject(
            request,
            reason="missing_workspace",
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token is missing workspace scope",
        )

    AUTH_TOTAL.labels(result="success", reason="ok").inc()
    return workspace_id
