from __future__ import annotations

from datetime import date

import pytest

from app.imports.batch_service import (
    FILE_SOURCE_CONTRACT,
    _normalize_date_source,
    _require_file_source_contract,
)
from app.imports.errors import ImportFailure


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


