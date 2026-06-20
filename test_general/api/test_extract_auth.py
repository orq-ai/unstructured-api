"""Auth tests for the /extract endpoint dependency (require_api_key)."""

import pytest
from fastapi import HTTPException

from prepline_general.api.auth import require_api_key


class _StubRequest:
    def __init__(self, headers):
        self.headers = headers


def test_no_key_configured_is_noop(monkeypatch):
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    require_api_key(_StubRequest({}))


def test_missing_key_rejected(monkeypatch):
    monkeypatch.setenv("UNSTRUCTURED_API_KEY", "secret")
    with pytest.raises(HTTPException) as exc:
        require_api_key(_StubRequest({}))
    assert exc.value.status_code == 401


def test_wrong_key_rejected(monkeypatch):
    monkeypatch.setenv("UNSTRUCTURED_API_KEY", "secret")
    with pytest.raises(HTTPException) as exc:
        require_api_key(_StubRequest({"unstructured-api-key": "nope"}))
    assert exc.value.status_code == 401


def test_correct_key_accepted(monkeypatch):
    monkeypatch.setenv("UNSTRUCTURED_API_KEY", "secret")
    require_api_key(_StubRequest({"unstructured-api-key": "secret"}))
