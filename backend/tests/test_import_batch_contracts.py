from __future__ import annotations

from datetime import date

import pytest

from app.imports.batch_service import (
    FILE_SOURCE_CONTRACT,
    _normalize_date_source,
    _require_file_source_contract,
)
from app.imports.errors import ImportFailure
from app.imports.mapping import encoded
from app.imports.readmodels import project_source_record


def test_supported_file_contract_is_accepted():
    _require_file_source_contract([{"contractversion": FILE_SOURCE_CONTRACT}])


def test_unsupported_file_contract_fails_closed():
    with pytest.raises(ImportFailure) as caught:
        _require_file_source_contract([{"contractversion": FILE_SOURCE_CONTRACT + 1}])

    assert caught.value.code == "IMPORT_CONTRACT_UNSUPPORTED"
    assert "re-upload" in caught.value.message


def test_import_fixed_provider_is_durable_reference_to_import_default():
    accepted = _normalize_date_source({"kind": "import_fixed"}, {}, date(2026, 10, 20),
                                      expand_import_fixed=False)
    effective = _normalize_date_source(accepted, {}, date(2026, 10, 21), expand_import_fixed=True)

    assert accepted == {"kind": "import_fixed"}
    assert effective == {"kind": "import_fixed", "value": "2026-10-21"}
    with pytest.raises(ImportFailure) as caught:
        _normalize_date_source(accepted, {}, None, expand_import_fixed=True)
    assert caught.value.code == "IMPORT_DATE_SOURCE_REQUIRED"


def test_source_cell_pages_keep_values_and_issues_reachable_under_response_bound():
    text = "x" * 8_000
    item = {
        "source_record_id": 1,
        "payload": {str(column): {"value": text if column <= 126 else "identifier/date",
                                  "kind": "text", "formula": False, "missing_cache": False}
                    for column in range(1, 129)},
        "values_json": {str(column): {"source_column": column, "raw_value": text}
                        for column in range(1, 127)},
        "issues": ([{"column": column, "code": f"ISSUE_{column}", "value": text}
                    for column in range(1, 127)]
                   + [{"column": None, "code": "ROW_ISSUE", "message": "row-level"}]),
    }
    assert len(encoded(item).encode("utf-8")) > 2_000_000

    for limit in (1, 50):
        cells, values, issues = {}, {}, {}
        for offset in range(0, 128, limit):
            page = project_source_record(item, offset, limit)
            assert len(encoded(page).encode("utf-8")) < 2_000_000
            assert page["cell_count"] == 128
            assert page["values_count"] == 126
            assert page["issue_count"] == 127
            cells.update(page["cells"])
            values.update(page["values_json"])
            issues.update({entry["code"]: entry for entry in page["issues"]})
        assert len(cells) == 128
        assert len(values) == 126
        assert len(issues) == 127


@pytest.mark.parametrize("source", [
    {"kind": "column", "value": "2026-10-20"},
    {"kind": "import_fixed", "value": "2026-10-20"},
    {"kind": "fixed_unit", "value": "10/20/2026"},
    {"kind": "fixed_unit", "value": "2026-10-20", "comment": "untrusted metadata"},
    {"kind": "filename", "value": "2026-10-20"},
])
def test_date_provider_rejects_noncanonical_or_unselected_values(source):
    with pytest.raises(ImportFailure):
        _normalize_date_source(source, {"filename_date_candidates": [], "unit_date_candidates": []},
                               date(2026, 10, 20), expand_import_fixed=False)


