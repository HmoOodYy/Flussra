"""Strict multipart receive boundary for bounded ordered batches of files."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, MultipartState, parse_options_header
from starlette.requests import ClientDisconnect, Request

from app.imports.errors import ImportFailure
from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY, ImportResourcePolicy

MAX_FILE_BYTES = DEFAULT_IMPORT_RESOURCE_POLICY.max_file_bytes
MAX_MULTIPART_OVERHEAD_BYTES = DEFAULT_IMPORT_RESOURCE_POLICY.max_multipart_overhead_bytes
MAX_REQUEST_BODY_BYTES = DEFAULT_IMPORT_RESOURCE_POLICY.max_request_body_bytes
MAX_MULTIPART_HEADER_BYTES = 8_192
MAX_MULTIPART_HEADER_COUNT = 8
_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class FilePayload:
    data: bytes
    filename: str
    file_sha256: str
    file_size: int


async def receive_files(
    request: Request, policy: ImportResourcePolicy
) -> list[FilePayload]:
    """Receive a bounded ordered multipart batch of repeated ``file`` parts."""
    content_type = request.headers.get("content-type", "")
    media_type, content_type_parameters = parse_options_header(content_type)
    boundary = content_type_parameters.get(b"boundary")
    if media_type != b"multipart/form-data" or not boundary or len(boundary) > 70:
        raise _failure("IMPORT_MULTIPART_INVALID", "A valid multipart/form-data boundary is required.")

    file_bytes = bytearray()
    digest = hashlib.sha256()
    files: list[FilePayload] = []
    total_file_bytes = 0
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
        if part_count > policy.max_files:
            raise _failure("IMPORT_FILE_COUNT_EXCEEDED", "Import batch contains too many files.")
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
        nonlocal total_file_bytes
        if not part_open or filename is None:
            raise _failure("IMPORT_MULTIPART_INVALID", "Multipart file data appeared before valid headers.")
        piece = memoryview(data)[start:end]
        if len(file_bytes) + len(piece) > policy.max_file_bytes:
            raise _failure("IMPORT_FILE_TOO_LARGE", "A file exceeds its configured upload limit.")
        if total_file_bytes + len(piece) > policy.max_total_file_bytes:
            raise _failure("IMPORT_BATCH_TOO_LARGE", "Import batch exceeds its aggregate file-byte limit.")
        file_bytes.extend(piece)
        digest.update(piece)

    def on_part_end() -> None:
        nonlocal part_open, part_finished, total_file_bytes, digest
        if filename is None:
            raise _failure("IMPORT_MULTIPART_INVALID", "Multipart file data appeared before valid headers.")
        if not file_bytes:
            raise _failure("IMPORT_FILE_EMPTY", "A file in the import batch is empty.")
        files.append(FilePayload(
            data=bytes(file_bytes), filename=filename, file_sha256=digest.hexdigest(), file_size=len(file_bytes)
        ))
        total_file_bytes += len(file_bytes)
        file_bytes.clear()
        digest = hashlib.sha256()
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
            if request_bytes > policy.max_request_body_bytes:
                raise _failure("IMPORT_REQUEST_TOO_LARGE", "Multipart request exceeds its configured batch limit.")
            written = parser.write(chunk)
            if written != len(chunk):
                raise _failure("IMPORT_MULTIPART_INVALID", "Multipart parser did not consume the complete body.")
            if request_bytes - total_file_bytes - len(file_bytes) > policy.max_multipart_overhead_bytes:
                raise _failure(
                    "IMPORT_MULTIPART_OVERHEAD_TOO_LARGE", "Multipart overhead exceeds its configured limit."
                )
        parser.finalize()
    except ImportFailure:
        raise
    except ClientDisconnect as exc:
        raise _failure("IMPORT_UPLOAD_INCOMPLETE", "Upload ended before the multipart body was complete.") from exc
    except (MultipartParseError, ValueError, UnicodeError) as exc:
        raise _failure("IMPORT_MULTIPART_INVALID", "Multipart body is malformed or incomplete.") from exc

    overhead_bytes = request_bytes - total_file_bytes
    if overhead_bytes > policy.max_multipart_overhead_bytes:
        raise _failure("IMPORT_MULTIPART_OVERHEAD_TOO_LARGE", "Multipart overhead exceeds its configured limit.")
    if not files or not part_finished or part_open or not multipart_ended:
        raise _failure("IMPORT_UPLOAD_INCOMPLETE", "Upload must contain complete file parts and a final boundary.")
    if parser.state != MultipartState.END:
        raise _failure("IMPORT_UPLOAD_INCOMPLETE", "Upload is missing its final multipart boundary.")
    return files


async def receive_mapping_json(request: Request, max_bytes: int) -> bytes:
    """Read one bounded mapping request without trusting Content-Length."""
    body = bytearray()
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            if len(body) + len(chunk) > max_bytes:
                raise ImportFailure("IMPORT_MAPPING_REQUEST_TOO_LARGE",
                                    "Mapping request exceeds its configured size limit.", 413)
            body.extend(chunk)
    except ClientDisconnect as exc:
        raise ImportFailure("IMPORT_MAPPING_REQUEST_INCOMPLETE",
                            "Mapping request ended before the body was complete.", 400) from exc
    return bytes(body)


def _failure(code: str, message: str) -> ImportFailure:
    return ImportFailure(code, message)
