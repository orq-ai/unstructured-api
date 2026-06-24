"""Unit tests for require_orq_workspace (orq platform JWT verification)."""

import time

import jwt
import pytest
from fastapi import HTTPException

from prepline_general.api.auth import require_orq_workspace

SECRET = "unit-test-secret"


class _StubRequest:
    def __init__(self, headers):
        self.headers = headers


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)


def _token(secret=SECRET, **overrides):
    payload = {"iss": "orq.internal", "workspace_id": "ws_1"}
    payload.update(overrides)
    return jwt.encode(payload, secret, algorithm="HS256")


def _req(authorization=None):
    headers = {}
    if authorization is not None:
        headers["authorization"] = authorization
    return _StubRequest(headers)


def test_valid_token_returns_workspace_id():
    assert require_orq_workspace(_req(f"Bearer {_token()}")) == "ws_1"


def test_missing_header_rejected():
    with pytest.raises(HTTPException) as exc:
        require_orq_workspace(_req())
    assert exc.value.status_code == 401


def test_malformed_header_rejected():
    with pytest.raises(HTTPException) as exc:
        require_orq_workspace(_req("Token abc"))
    assert exc.value.status_code == 401


def test_bad_signature_rejected():
    with pytest.raises(HTTPException) as exc:
        require_orq_workspace(_req(f"Bearer {_token(secret='wrong-secret')}"))
    assert exc.value.status_code == 401


def test_untrusted_issuer_rejected():
    with pytest.raises(HTTPException) as exc:
        require_orq_workspace(_req(f"Bearer {_token(iss='evil')}"))
    assert exc.value.status_code == 401


def test_expired_token_rejected():
    token = _token(exp=int(time.time()) - 10)
    with pytest.raises(HTTPException) as exc:
        require_orq_workspace(_req(f"Bearer {token}"))
    assert exc.value.status_code == 401


def test_token_without_workspace_rejected():
    token = jwt.encode({"iss": "orq.internal"}, SECRET, algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        require_orq_workspace(_req(f"Bearer {token}"))
    assert exc.value.status_code == 403


def test_missing_secret_is_misconfiguration(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(HTTPException) as exc:
        require_orq_workspace(_req(f"Bearer {_token()}"))
    assert exc.value.status_code == 500
