import logging
import os
from io import BytesIO
from typing import Optional

from fastapi import HTTPException, UploadFile

from unstructured.file_utils.filetype import detect_filetype
from unstructured.file_utils.model import FileType

from .metrics import BLOCKED_TOTAL

logger = logging.getLogger("unstructured_api")

ALLOWED_PARTITIONABLE_FILETYPES = {
    FileType.CSV,
    FileType.DOCX,
    FileType.HTML,
    FileType.PDF,
    FileType.TXT,
}


def _remove_optional_info_from_mime_type(content_type: str | None) -> str | None:
    """removes charset information from mime types, e.g.,
    "application/json; charset=utf-8" -> "application/json"
    """
    if not content_type:
        return content_type
    return content_type.split(";")[0]


def _get_filetype_from_filename(filename: str | None) -> FileType | None:
    if not filename:
        return None

    return FileType.from_extension(os.path.splitext(filename)[1].lower())


def _raise_unsupported_filetype(filetype: FileType | None) -> None:
    mime_type = filetype.mime_type if filetype else "unknown"
    # Security signal: rejected types are how the RST LFD/SSRF probe (and similar
    # parser-abuse attempts) surface at the gate. Count by reason; log the mime so
    # spikes of a specific disallowed type (e.g. text/x-rst) are visible.
    BLOCKED_TOTAL.labels(endpoint="partition", reason="unsupported_filetype").inc()
    logger.warning("blocked unsupported filetype: mime=%s", mime_type)
    raise HTTPException(
        status_code=400,
        detail=(f"File type {mime_type} is not supported."),
    )


def _validate_filetype(filetype: FileType | None) -> FileType:
    if filetype not in ALLOWED_PARTITIONABLE_FILETYPES:
        _raise_unsupported_filetype(filetype)

    assert filetype is not None
    return filetype


def get_validated_mimetype_for_filename(
    filename: str | None,
    content_type_hint: str | None = None,
) -> str:
    """Validate a persisted file's type before handing it to unstructured."""
    filetype = _get_filetype_from_filename(filename)

    if filetype is None:
        content_type = _remove_optional_info_from_mime_type(content_type_hint)
        filetype = FileType.from_mime_type(content_type)

    return _validate_filetype(filetype).mime_type


def get_validated_mimetype(file: UploadFile, content_type_hint: str | None = None) -> str:
    """Given the incoming file, identify and return the correct mimetype.

    Order of operations:
    - Block unsupported filename extensions before trusting a caller-supplied MIME type.
    - If user passed content_type as a form param, take it as truth.
    - Otherwise, use file.content_type (as set by the Content-Type header)
    - If no content_type was passed and the header wasn't useful, call the library's detect_filetype

    Once we have a filetype, enforce the service allowlist and return 400 if we don't support it.
    """
    content_type: str | None = None

    filetype = _get_filetype_from_filename(file.filename)

    if filetype:
        _validate_filetype(filetype)

    if content_type_hint is not None:
        content_type = content_type_hint
    else:
        content_type = _remove_optional_info_from_mime_type(file.content_type)

    filetype = FileType.from_mime_type(content_type)

    # If content_type was not specified, use the library to identify the file
    # We inspect the bytes to do this, so we need to buffer the file
    if not filetype or filetype == FileType.UNK:
        file_buffer = BytesIO(file.file.read())
        file.file.seek(0)

        file_buffer.name = file.filename

        filetype = detect_filetype(file=file_buffer)

    _validate_filetype(filetype)

    return filetype.mime_type
