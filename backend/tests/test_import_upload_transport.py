from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace

import pytest
from starlette.requests import Request

from app.imports import ImportFailure
from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY
from app.imports.transport import (
    receive_files,
    receive_mapping_json,
)


def _multipart(file_data: bytes, *, filename: str = "source.csv", field: str = "file", boundary: str = "b-a1") -> bytes:
    return (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
        + file_data
        + f"\r\n--{boundary}--\r\n".encode()
    )


def _request(body: bytes, boundary: str = "b-a1", chunk_sizes: tuple[int, ...] = (),
             content_length: str | None = None) -> Request:
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

    headers = [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())]
    if content_length is not None:
        headers.append((b"content-length", content_length.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "headers": headers,
    }
    return Request(scope, receive)


def _one_file_policy(*, file_bytes: int = 10_000_000, overhead: int = 256_000):
    return replace(DEFAULT_IMPORT_RESOURCE_POLICY, max_file_bytes=file_bytes,
                   max_total_file_bytes=file_bytes, max_files=1,
                   max_multipart_overhead_bytes=overhead)


def _receive_files(body: bytes, *, policy=DEFAULT_IMPORT_RESOURCE_POLICY,
                   chunk_sizes: tuple[int, ...] = (), content_length: str | None = None):
    return asyncio.run(receive_files(
        _request(body, chunk_sizes=chunk_sizes, content_length=content_length), policy
    ))


def test_receive_mapping_json_counts_streamed_bytes_not_declared_content_length() -> None:
    body = b'{"expected_revision":1}'
    request = _request(body, content_length="1")

    assert asyncio.run(receive_mapping_json(request, len(body))) == body


def test_receive_mapping_json_rejects_actual_oversize_even_when_content_length_is_small() -> None:
    body = b'{"targets":[],"padding":"0123456789"}'
    request = _request(body, chunk_sizes=(4, 5, 3), content_length="1")

    with pytest.raises(ImportFailure) as exc:
        asyncio.run(receive_mapping_json(request, 16))

    assert exc.value.code == "IMPORT_MAPPING_REQUEST_TOO_LARGE"
    assert exc.value.status_code == 413


def test_receive_file_streams_split_boundaries_and_calculates_sha256() -> None:
    data = b"Driver Number,Date\r\n0012,2026-10-20\r\n"
    body = _multipart(data)

    payload = _receive_files(body, policy=_one_file_policy(), chunk_sizes=(3, 7, 19, 2, 1))[0]

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
        _receive_files(body, policy=_one_file_policy(file_bytes=10))
    assert exc_info.value.code == code


def test_receive_files_rejects_extra_field_and_enforces_explicit_file_count() -> None:
    first = _multipart(b"a")
    second_part = (
        b"--b-a1\r\nContent-Disposition: form-data; name=\"extra\"\r\n\r\nvalue"
        b"\r\n--b-a1--\r\n"
    )
    body = first[: -len(b"\r\n--b-a1--\r\n")] + b"\r\n" + second_part
    with pytest.raises(ImportFailure) as extra_field:
        _receive_files(body, policy=replace(_one_file_policy(file_bytes=1), max_files=2))
    assert extra_field.value.code == "IMPORT_MULTIPART_PARTS_INVALID"

    body = first[: -len(b"\r\n--b-a1--\r\n")] + (
        b"\r\n--b-a1\r\nContent-Disposition: form-data; name=\"file\"; filename=\"two.csv\""
        b"\r\n\r\ny\r\n--b-a1--\r\n"
    )
    with pytest.raises(ImportFailure) as second_file:
        _receive_files(body, policy=_one_file_policy(file_bytes=1))
    assert second_file.value.code == "IMPORT_FILE_COUNT_EXCEEDED"


def test_receive_files_rejects_actual_file_size_over_limit() -> None:
    body = _multipart(b"x" * 11)

    with pytest.raises(ImportFailure) as exc_info:
        _receive_files(body, policy=_one_file_policy(file_bytes=10))

    assert exc_info.value.code == "IMPORT_FILE_TOO_LARGE"


def test_receive_files_accepts_exact_file_and_overhead_caps() -> None:
    data = b"x" * 10
    body = _multipart(data)
    overhead = 512
    policy = _one_file_policy(file_bytes=len(data), overhead=overhead)
    assert len(body) < policy.max_request_body_bytes
    body += b" " * (overhead - (len(body) - len(data)))
    assert len(body) == policy.max_request_body_bytes

    payload = _receive_files(body, policy=policy)[0]

    assert payload.file_size == len(data)
    assert payload.file_sha256 == hashlib.sha256(data).hexdigest()


def test_receive_files_rejects_actual_body_over_limit_even_with_false_length() -> None:
    data = b"x" * 10
    body = _multipart(data)
    overhead = 512
    policy = _one_file_policy(file_bytes=len(data), overhead=overhead)
    body += b" " * (overhead - (len(body) - len(data)) + 1)
    assert len(body) == policy.max_request_body_bytes + 1

    with pytest.raises(ImportFailure) as exc_info:
        _receive_files(body, policy=policy, content_length="1")

    assert exc_info.value.code == "IMPORT_REQUEST_TOO_LARGE"
