from __future__ import annotations

import asyncio
import hashlib

import pytest
from starlette.requests import Request

from app.imports import ImportFailure, receive_file
from app.imports.transport import (
    MAX_FILE_BYTES,
    MAX_MULTIPART_OVERHEAD_BYTES,
    MAX_REQUEST_BODY_BYTES,
)


def _multipart(file_data: bytes, *, filename: str = "source.csv", field: str = "file", boundary: str = "b-a1") -> bytes:
    return (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
        + file_data
        + f"\r\n--{boundary}--\r\n".encode()
    )


def _request(body: bytes, boundary: str = "b-a1", chunk_sizes: tuple[int, ...] = ()) -> Request:
    messages: list[dict[str, object]] = []
    if chunk_sizes:
        offset = 0
        for size in chunk_sizes:
            messages.append({"type": "http.request", "body": body[offset : offset + size], "more_body": True})
            offset += size
        messages.append({"type": "http.request", "body": body[offset:], "more_body": False})
    else:
        messages.append({"type": "http.request", "body": body, "more_body": False})

    async def receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
    }
    return Request(scope, receive)


def test_receive_file_streams_split_boundaries_and_calculates_sha256() -> None:
    data = b"Driver Number,Date\r\n0012,2026-10-20\r\n"
    body = _multipart(data)

    payload = asyncio.run(receive_file(_request(body, chunk_sizes=(3, 7, 19, 2, 1))))

    assert payload.data == data
    assert payload.filename == "source.csv"
    assert payload.file_size == len(data)
    assert payload.file_sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    ("body", "filename", "field", "code"),
    [
        (_multipart(b"x", filename="source.xlsm"), "source.xlsm", "file", "IMPORT_FILE_TYPE_UNSUPPORTED"),
        (_multipart(b"x", field="other"), "source.csv", "other", "IMPORT_MULTIPART_PARTS_INVALID"),
        (
            _multipart(b"x").removesuffix(b"--\r\n"),
            "source.csv",
            "file",
            "IMPORT_UPLOAD_INCOMPLETE",
        ),
    ],
)
def test_receive_file_rejects_unsupported_or_incomplete_multipart(
    body: bytes, filename: str, field: str, code: str
) -> None:
    del filename, field
    with pytest.raises(ImportFailure) as exc_info:
        asyncio.run(receive_file(_request(body)))
    assert exc_info.value.code == code


def test_receive_file_rejects_extra_field_or_second_file() -> None:
    first = _multipart(b"a")
    second_part = (
        b"--b-a1\r\nContent-Disposition: form-data; name=\"extra\"\r\n\r\nvalue"
        b"\r\n--b-a1--\r\n"
    )
    body = first[: -len(b"\r\n--b-a1--\r\n")] + b"\r\n" + second_part
    with pytest.raises(ImportFailure) as extra_field:
        asyncio.run(receive_file(_request(body)))
    assert extra_field.value.code == "IMPORT_MULTIPART_PARTS_INVALID"

    body = first[: -len(b"\r\n--b-a1--\r\n")] + (
        b"\r\n--b-a1\r\nContent-Disposition: form-data; name=\"file\"; filename=\"two.csv\""
        b"\r\n\r\ny\r\n--b-a1--\r\n"
    )
    with pytest.raises(ImportFailure) as second_file:
        asyncio.run(receive_file(_request(body)))
    assert second_file.value.code == "IMPORT_MULTIPART_PARTS_INVALID"


def test_receive_file_rejects_actual_file_size_over_limit() -> None:
    body = _multipart(b"x" * (MAX_FILE_BYTES + 1))

    with pytest.raises(ImportFailure) as exc_info:
        asyncio.run(receive_file(_request(body)))

    assert exc_info.value.code == "IMPORT_FILE_TOO_LARGE"


def test_receive_file_accepts_exact_body_and_overhead_caps() -> None:
    data = b"x" * MAX_FILE_BYTES
    body = _multipart(data)
    overhead = MAX_MULTIPART_OVERHEAD_BYTES
    assert len(body) < MAX_REQUEST_BODY_BYTES
    body += b" " * (overhead - (len(body) - len(data)))
    assert len(body) == MAX_REQUEST_BODY_BYTES

    payload = asyncio.run(receive_file(_request(body)))

    assert payload.file_size == MAX_FILE_BYTES
    assert payload.file_sha256 == hashlib.sha256(data).hexdigest()


def test_receive_file_rejects_actual_body_over_limit() -> None:
    data = b"x" * MAX_FILE_BYTES
    body = _multipart(data)
    overhead = MAX_MULTIPART_OVERHEAD_BYTES
    body += b" " * (overhead - (len(body) - len(data)) + 1)
    assert len(body) == MAX_REQUEST_BODY_BYTES + 1

    with pytest.raises(ImportFailure) as exc_info:
        asyncio.run(receive_file(_request(body)))

    assert exc_info.value.code == "IMPORT_REQUEST_TOO_LARGE"
