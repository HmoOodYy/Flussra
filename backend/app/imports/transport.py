"""Strict multipart receive boundary for one bounded Import file."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, MultipartState, parse_options_header
from starlette.requests import ClientDisconnect, Request

from app.imports.errors import ImportFailure

MAX_FILE_BYTES = 10_000_000
MAX_MULTIPART_OVERHEAD_BYTES = 64_000
MAX_REQUEST_BODY_BYTES = MAX_FILE_BYTES + MAX_MULTIPART_OVERHEAD_BYTES
MAX_MULTIPART_HEADER_BYTES = 8_192
MAX_MULTIPART_HEADER_COUNT = 8
_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class FilePayload:
    data: bytes
    filename: str
    file_sha256: str
    file_size: int


async def receive_file(request: Request) -> FilePayload:
    """Receive exactly one multipart ``file`` part with strict actual-byte caps."""
    content_type = request.headers.get("content-type", "")
    media_type, content_type_parameters = parse_options_header(content_type)
    boundary = content_type_parameters.get(b"boundary")
    if media_type != b"multipart/form-data" or not boundary or len(boundary) > 70:
        raise _failure("IMPORT_MULTIPART_INVALID", "A valid multipart/form-data boundary is required.")

    file_bytes = bytearray()
    digest = hashlib.sha256()
    request_bytes = 0
    part_count = 0
    part_open = False
    part_finished = False
    multipart_ended = False
    current_header_name = bytearray()
    current_header_value = bytearray()
    part_headers: dict[bytes, bytes] = {}
    filename: str | None = None

    def on_part_begin() -> None:
        nonlocal part_count, part_open, part_finished, filename
        part_count += 1
        if part_count > 1:
            raise _failure("IMPORT_MULTIPART_PARTS_INVALID", "Upload exactly one file and no other form fields.")
        part_open = True
        part_finished = False
        filename = None
        part_headers.clear()

    def on_header_field(data: bytes, start: int, end: int) -> None:
        current_header_name.extend(data[start:end])

    def on_header_value(data: bytes, start: int, end: int) -> None:
        current_header_value.extend(data[start:end])

    def on_header_end() -> None:
        name = bytes(current_header_name).strip().lower()
        if not name or name in part_headers:
            raise _failure("IMPORT_MULTIPART_INVALID", "Multipart headers are malformed or duplicated.")
        part_headers[name] = bytes(current_header_value).strip()
        current_header_name.clear()
        current_header_value.clear()

    def on_headers_finished() -> None:
        nonlocal filename
        disposition, options = parse_options_header(part_headers.get(b"content-disposition"))
        if disposition != b"form-data" or options.get(b"name") != b"file":
            raise _failure("IMPORT_MULTIPART_PARTS_INVALID", "Upload exactly one file in the 'file' field.")
        raw_filename = options.get(b"filename")
        if not raw_filename:
            raise _failure("IMPORT_FILE_NAME_REQUIRED", "The 'file' part must include a filename.")
        try:
            filename = raw_filename.decode("utf-8")
        except UnicodeDecodeError:
            filename = raw_filename.decode("latin-1")
        if len(filename) > 260 or len(filename.encode("utf-8")) > 1040:
            raise _failure("IMPORT_FILE_NAME_INVALID", "Filename must be at most 260 characters.")
        suffix = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
        if suffix not in {"csv", "xlsx"}:
            raise _failure("IMPORT_FILE_TYPE_UNSUPPORTED", "Only .xlsx and .csv files are supported.")
        if any(ord(character) < 32 or ord(character) == 127 for character in filename):
            raise _failure("IMPORT_FILE_NAME_INVALID", "Filename contains invalid control characters.")

    def on_part_data(data: bytes, start: int, end: int) -> None:
        if not part_open or filename is None:
            raise _failure("IMPORT_MULTIPART_INVALID", "Multipart file data appeared before valid headers.")
        piece = memoryview(data)[start:end]
        if len(file_bytes) + len(piece) > MAX_FILE_BYTES:
            raise _failure("IMPORT_FILE_TOO_LARGE", "File exceeds the 10 MB upload limit.")
        file_bytes.extend(piece)
        digest.update(piece)

    def on_part_end() -> None:
        nonlocal part_open, part_finished
        part_open = False
        part_finished = True

    def on_end() -> None:
        nonlocal multipart_ended
        multipart_ended = True

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_end": on_end,
        },
        max_size=float("inf"),  # Its max_size truncates, so actual bytes are guarded here.
        max_header_count=MAX_MULTIPART_HEADER_COUNT,
        max_header_size=MAX_MULTIPART_HEADER_BYTES,
    )

    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            request_bytes += len(chunk)
            if request_bytes > MAX_REQUEST_BODY_BYTES:
                raise _failure("IMPORT_REQUEST_TOO_LARGE", "Multipart request exceeds the 10.064 MB limit.")
            written = parser.write(chunk)
            if written != len(chunk):
                raise _failure("IMPORT_MULTIPART_INVALID", "Multipart parser did not consume the complete body.")
            if request_bytes - len(file_bytes) > MAX_MULTIPART_OVERHEAD_BYTES:
                raise _failure(
                    "IMPORT_MULTIPART_OVERHEAD_TOO_LARGE", "Multipart overhead exceeds 64 KB."
                )
        parser.finalize()
    except ImportFailure:
        raise
    except ClientDisconnect as exc:
        raise _failure("IMPORT_UPLOAD_INCOMPLETE", "Upload ended before the multipart body was complete.") from exc
    except (MultipartParseError, ValueError, UnicodeError) as exc:
        raise _failure("IMPORT_MULTIPART_INVALID", "Multipart body is malformed or incomplete.") from exc

    overhead_bytes = request_bytes - len(file_bytes)
    if overhead_bytes > MAX_MULTIPART_OVERHEAD_BYTES:
        raise _failure("IMPORT_MULTIPART_OVERHEAD_TOO_LARGE", "Multipart overhead exceeds 64 KB.")
    if part_count != 1 or not part_finished or part_open or not multipart_ended:
        raise _failure("IMPORT_UPLOAD_INCOMPLETE", "Upload must contain one complete file part and final boundary.")
    if parser.state != MultipartState.END:
        raise _failure("IMPORT_UPLOAD_INCOMPLETE", "Upload is missing its final multipart boundary.")
    if not file_bytes:
        raise _failure("IMPORT_FILE_EMPTY", "The uploaded file is empty.")
    assert filename is not None
    return FilePayload(
        data=bytes(file_bytes),
        filename=filename,
        file_sha256=digest.hexdigest(),
        file_size=len(file_bytes),
    )


def _failure(code: str, message: str) -> ImportFailure:
    return ImportFailure(code, message)
