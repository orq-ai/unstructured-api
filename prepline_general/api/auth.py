import os
import secrets

from fastapi import HTTPException, Request, status


def require_api_key(request: Request) -> None:
    """Enforce the shared internal API key.

    Parity with the /general endpoint: when UNSTRUCTURED_API_KEY is unset the
    check is a no-op so local/dev deployments keep working. When set, requests
    must present a matching `unstructured-api-key` header.
    """
    api_key_env = os.environ.get("UNSTRUCTURED_API_KEY")
    if not api_key_env:
        return

    provided = request.headers.get("unstructured-api-key") or ""
    if not secrets.compare_digest(provided, api_key_env):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
