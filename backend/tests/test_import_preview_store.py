"""Prepared detail remains bounded and invocation-owned before publication."""
import pytest

from app.imports import preview_store
from app.imports.preview_model import build_preview
from app.imports.service import check_versions
from tests.test_import_mapping import _context, _mapping, _materialized, _preview_row


def test_preparation_splits_batches_and_closes_rolled_over_files(monkeypatch):
    monkeypatch.setattr(preview_store, "COPY_BATCH", 2)
    monkeypatch.setattr(preview_store, "SPOOL_MEMORY_BYTES", 1)
    model = build_preview(_materialized(*(_preview_row(index + 2) for index in range(5))), _mapping(), _context())
    prepared = preview_store.prepare_preview(model)
    streams = [stream for _, stream in prepared.streams.values()]
    try:
        for table, (_, stream) in prepared.streams.items():
            batches = []
            while batch := preview_store.read_batch(stream):
                assert 0 < len(batch) <= 2
                batches.extend(batch)
            if table == "payrollworkcells":
                assert len(batches) == 5
                assert {item[0] for item in batches} == {2, 3, 4, 5, 6}
        assert all(key not in prepared.metadata for key in ("rows", "issues", "matches"))
        assert prepared.metadata["content_fingerprint"] == model["content_fingerprint"]
    finally:
        prepared.close()
    assert all(stream.closed for stream in streams)


def test_preparation_serialization_failure_closes_every_created_file(monkeypatch):
    created = []
    factory = preview_store.SpooledTemporaryFile

    def observed(**kwargs):
        stream = factory(**kwargs)
        created.append(stream)
        return stream

    def fail(_):
        raise OSError("injected disk/serialization failure")

    monkeypatch.setattr(preview_store, "SpooledTemporaryFile", observed)
    monkeypatch.setattr(preview_store, "encoded", fail)
    model = build_preview(_materialized(_preview_row(2)), _mapping(), _context())
    with pytest.raises(OSError, match="injected disk/serialization failure"):
        preview_store.prepare_preview(model)
    assert created and all(stream.closed for stream in created)


def test_obsolete_staged_canonicalization_is_refused_instead_of_reinterpreted():
    from app.imports.errors import ImportFailure

    row = {"importkind": "PayrollWork", "sourcekind": "File", "contractversion": 1,
           "file_version": 1, "target_version": 1, "canonicalizationversion": 2}
    check_versions(row)
    with pytest.raises(ImportFailure) as rejected:
        check_versions({**row, "canonicalizationversion": 1})
    assert rejected.value.code == "IMPORT_CONTRACT_UNSUPPORTED"
