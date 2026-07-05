import os

from fastapi import HTTPException, UploadFile, status

from unstructured.file_utils.filetype import detect_filetype
from unstructured.file_utils.model import FileType

ALLOWED_PARTITIONABLE_FILETYPES = {
    FileType.CSV,
    FileType.DOCX,
    FileType.HTML,
    FileType.PDF,
    FileType.TXT,
}

# Text-family types that byte sniffing cannot reliably tell apart (a CSV is
# valid TXT; an HTML fragment sniffs as TXT). Within this group we honor the
# caller's declared type. Outside it — the binary formats — the sniffed type
# is authoritative and any mismatch is rejected.
_TEXT_COMPATIBLE = {FileType.CSV, FileType.TXT, FileType.HTML}

# Cap uploads before they reach the parsers. DOCX is a zip container and PDF
# streams are compressed, so unbounded input is a decompression-bomb /
# memory-exhaustion vector inside `unstructured`, not just on the wire.
MAX_UPLOAD_SIZE_BYTES = int(
    os.environ.get("MAX_UPLOAD_SIZE_BYTES", 50 * 1024 * 1024)
)


def _normalize_mime_type(content_type: str | None) -> str | None:
    """Strip parameters and normalize case, e.g.
    "Application/JSON; charset=utf-8" -> "application/json"
    """
    if not content_type:
        return None
    return content_type.split(";")[0].strip().lower() or None


def _get_filetype_from_filename(filename: str | None) -> FileType | None:
    if not filename:
        return None
    return FileType.from_extension(os.path.splitext(filename)[1].lower())


def _raise_unsupported_filetype(filetype: FileType | None) -> None:
    mime_type = filetype.mime_type if filetype else "unknown"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"File type {mime_type} is not supported.",
    )


def _validate_filetype(filetype: FileType | None) -> FileType:
    # No `assert` here: asserts are stripped under `python -O`, which would
    # silently disable the check.
    if filetype is None or filetype not in ALLOWED_PARTITIONABLE_FILETYPES:
        _raise_unsupported_filetype(filetype)
    return filetype


def _enforce_size_limit(file: UploadFile) -> None:
    """Bound the upload without copying it into memory."""
    f = file.file
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    if size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="File is empty."
        )
    if size > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,  # Content Too Large
            detail=f"File exceeds the maximum size of {MAX_UPLOAD_SIZE_BYTES} bytes.",
        )


def _detect_filetype_from_bytes(file: UploadFile) -> FileType | None:
    """Sniff the actual bytes. The filename is deliberately NOT attached to
    the buffer, so detection cannot fall back to trusting the attacker-chosen
    extension.
    """
    f = file.file
    f.seek(0)
    try:
        detected = detect_filetype(file=f)
    except Exception:
        detected = None
    finally:
        f.seek(0)
    return detected


def _reject_binary_masquerading_as_text(file: UploadFile) -> None:
    """`detect_filetype` can classify arbitrary binaries (e.g. ELF, unknown
    formats) as TXT when libmagic is unavailable. Before accepting anything
    in the text family, require the leading bytes to actually look like text:
    no NUL bytes and overwhelmingly printable content.
    """
    f = file.file
    f.seek(0)
    head = f.read(8192)
    f.seek(0)
    if b"\x00" in head:
        _raise_unsupported_filetype(None)
    printable = sum(
        1 for b in head if b in (0x09, 0x0A, 0x0D) or 0x20 <= b <= 0x7E or b >= 0x80
    )
    if head and printable / len(head) < 0.95:
        _raise_unsupported_filetype(None)


def get_validated_mimetype_for_filename(
    filename: str | None,
    content_type_hint: str | None = None,
) -> str:
    """Validate a persisted file's type from its metadata.

    NOTE: this is metadata-only validation (extension, then declared MIME
    type) and provides no guarantee about the file's actual contents. It is
    only appropriate for files whose bytes were already validated by
    `get_validated_mimetype` at upload time. Anywhere the bytes are
    available, validate the bytes.
    """
    filetype = _get_filetype_from_filename(filename)

    if filetype is None:
        content_type = _normalize_mime_type(content_type_hint)
        filetype = FileType.from_mime_type(content_type) if content_type else None

    return _validate_filetype(filetype).mime_type


def get_validated_mimetype(
    file: UploadFile, content_type_hint: str | None = None
) -> str:
    """Identify and return the validated mimetype for an incoming file.

    The sniffed content type is authoritative; the filename extension and the
    caller-declared MIME type are treated as claims to be checked against it,
    never as truth. Order of operations:

    1. Enforce the size limit (rejects empty and oversized files).
    2. If the filename has a recognized extension, it must be allow-listed
       (fail fast with a clear error before touching the bytes).
    3. If a MIME type was declared (form param, else Content-Type header)
       and it is recognized, it must be allow-listed.
    4. Sniff the actual bytes with `detect_filetype`; the result must be
       allow-listed. This runs unconditionally — a declared type never
       skips it.
    5. Reject if the extension or declared type contradicts the sniffed
       type, except within the mutually-ambiguous text family
       (TXT/CSV/HTML), where the declared type is honored because byte
       sniffing cannot distinguish those reliably.
    """
    _enforce_size_limit(file)

    # -- Claim 1: filename extension ----------------------------------------
    # If a filename carries an extension, that extension must map to an
    # allow-listed type. Unknown extensions (.exe, .foo) are rejected rather
    # than ignored — an extension the platform doesn't recognize is a signal,
    # not noise. Extensionless filenames fall through to declared type + sniff.
    ext_filetype: FileType | None = None
    if file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext:
            ext_filetype = FileType.from_extension(ext)
            if ext_filetype == FileType.UNK:
                ext_filetype = None
            _validate_filetype(ext_filetype)

    # -- Claim 2: declared MIME type -----------------------------------------
    declared = _normalize_mime_type(content_type_hint) or _normalize_mime_type(
        file.content_type
    )
    declared_filetype = FileType.from_mime_type(declared) if declared else None
    if declared_filetype is not None and declared_filetype != FileType.UNK:
        _validate_filetype(declared_filetype)

    # -- Ground truth: the bytes ----------------------------------------------
    detected = _detect_filetype_from_bytes(file)
    if detected == FileType.UNK:
        detected = None
    validated = _validate_filetype(detected)

    # -- Claims must agree with the bytes --------------------------------------
    for claimed in (ext_filetype, declared_filetype):
        if claimed is None or claimed == FileType.UNK or claimed == validated:
            continue
        if claimed in _TEXT_COMPATIBLE and validated in _TEXT_COMPATIBLE:
            continue
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "File content does not match its declared type "
                f"({validated.mime_type} vs {claimed.mime_type})."
            ),
        )

    # Within the text family, prefer the caller's (already allow-listed)
    # declaration over the sniffed subtype, since e.g. a CSV legitimately
    # sniffs as plain text. Guard against binaries the sniffer mislabels
    # as text first.
    if validated in _TEXT_COMPATIBLE:
        _reject_binary_masquerading_as_text(file)
        for claimed in (declared_filetype, ext_filetype):
            if claimed in _TEXT_COMPATIBLE:
                return claimed.mime_type

    return validated.mime_type
