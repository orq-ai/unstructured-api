import io
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import jwt
import pytest
from fastapi.testclient import TestClient

from prepline_general.api import general, parse_markdown as parse_markdown_module, pdf_extractor
from prepline_general.api.app import app
from prepline_general.api.auth import require_orq_workspace
from prepline_general.api.pdf_extractor import get_database, get_storage_client

MAIN_API_ROUTE = "general/v0/general"


@pytest.mark.parametrize(
    ("filename", "content_type", "body"),
    [
        ("prod_lfi.rst", "text/x-rst", b".. include:: /etc/hosts\n\nPad\n"),
        ("prod_ssrf.org", "text/org", b"#+INCLUDE: http://127.0.0.1/latest\n"),
        ("prod_epub.epub", "application/epub", b"unsafe epub payload"),
        ("prod_odt.odt", "application/vnd.oasis.opendocument.text", b"unsafe odt payload"),
        ("prod_rtf.rtf", "application/rtf", b"{\\rtf1 unsafe rtf payload}"),
    ],
)
def test_general_rejects_unsafe_parser_filetypes(monkeypatch, filename, content_type, body):
    mock_partition = Mock()
    monkeypatch.setattr(general, "partition", mock_partition)

    client = TestClient(app)
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (filename, io.BytesIO(body), content_type))],
    )

    assert response.status_code == 400
    assert response.json()["detail"].endswith("is not supported.")
    mock_partition.assert_not_called()


def test_extract_rejects_unsafe_stored_filetype_before_partition(monkeypatch):
    mock_partition = Mock()
    monkeypatch.setattr(pdf_extractor, "partition", mock_partition)

    mock_db = Mock()
    mock_db.find_one.return_value = {
        "_id": "file_1",
        "workspace_id": "ws_test",
        "object_name": "uploads/prod_lfi.rst",
        "file_name": "prod_lfi.rst",
    }

    mock_storage_client = Mock()
    mock_storage_client.download_file.return_value = True

    app.dependency_overrides[get_database] = lambda: mock_db
    app.dependency_overrides[get_storage_client] = lambda: mock_storage_client

    try:
        client = TestClient(app)
        response = client.post("/extract", json={"file_id": "file_1"})
    finally:
        app.dependency_overrides.pop(get_database, None)
        app.dependency_overrides.pop(get_storage_client, None)

    assert response.status_code == 400
    assert response.json() == {"detail": "File type text/x-rst is not supported."}
    mock_storage_client.download_file.assert_not_called()
    mock_partition.assert_not_called()


def test_parse_markdown_requires_valid_jwt(monkeypatch):
    async def mock_process_markdown_message(markdown: str, **partition_params):
        return [{"content": "Body", "metadata": {"type": "Text"}}]

    monkeypatch.setattr(
        parse_markdown_module,
        "process_markdown_message",
        mock_process_markdown_message,
    )
    app.dependency_overrides.pop(require_orq_workspace, None)

    client = TestClient(app)
    response = client.post("/parse-markdown", json={"markdown": "# Title\n\nBody"})

    assert response.status_code == 401

    # exp is now a required claim: a token without one never expires
    token = jwt.encode(
        {
            "iss": "orq.internal",
            "workspace_id": "ws_test",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        os.environ["JWT_SECRET"],
        algorithm="HS256",
    )
    response = client.post(
        "/parse-markdown",
        json={"markdown": "# Title\n\nBody"},
        headers={"authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200


def test_cors_rejects_untrusted_origins():
    client = TestClient(app)

    response = client.options(
        f"/{MAIN_API_ROUTE}",
        headers={
            "origin": "https://evil.example",
            "access-control-request-method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers

    response = client.options(
        f"/{MAIN_API_ROUTE}",
        headers={
            "origin": "https://my.orq.ai",
            "access-control-request-method": "POST",
        },
    )
    assert response.headers["access-control-allow-origin"] == "https://my.orq.ai"
