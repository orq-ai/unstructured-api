import os

import pytest

from prepline_general.api.app import app
from prepline_general.api.auth import require_orq_workspace


@pytest.fixture(autouse=True)
def _bypass_orq_workspace_auth():
    """Endpoint tests exercise document parsing, not auth, so override the
    workspace dependency to a fixed tenant and make sure JWT_SECRET is set.

    Auth itself is covered directly in test_extract_auth.py (the verifier) and
    test_general_requires_valid_jwt (end-to-end through the client).
    """
    os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
    app.dependency_overrides[require_orq_workspace] = lambda: "ws_test"
    yield
    app.dependency_overrides.pop(require_orq_workspace, None)
