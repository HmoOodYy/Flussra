from __future__ import annotations

import json
from datetime import date

import pytest

from app.imports.batch_service import (
    FILE_SOURCE_CONTRACT,
    _normalize_date_source,
    _orders_agree_on_source_evidence,
    _require_file_source_contract,
    _require_mapping_configuration_size,
    _stored_rule_payload,
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


def test_filename_date_source_supports_automatic_and_explicit_manual_link():
    days = [date(2026, 10, 20)]
    assert _normalize_date_source({"kind": "filename"}, label="payroll.csv",
                                  period_days=days, date_order=None) == {"kind": "filename"}
    assert _normalize_date_source({"kind": "filename", "value": "2026-10-20"},
                                  label="unknown.csv", period_days=days,
                                  date_order=None) == {"kind": "filename", "value": "2026-10-20"}

    with pytest.raises(ImportFailure) as caught:
        _normalize_date_source({"kind": "filename", "value": "2026-10-20"},
                              label="payroll-2025-10-20.csv", period_days=days,
                              date_order=None)
    assert caught.value.code == "IMPORT_DATE_SOURCE_INVALID"


def _stored_rule_payload_with_date_profile():
    return {"payload_version": 1, "transform": "Direct",
            "inputs": [{"header": "hours"}],
            "shared_columns": {"DriverNumber": {"header": "driver"}},
            "date_interpretation": {"version": 1, "profiles": [{
                "authority": "filename", "numeric_order": None,
                "grammars": ["written_month_day"],
            }]}}


@pytest.mark.parametrize("patch", [
    {"authority": []},
    {"authority": {}},
    {"numeric_order": []},
    {"numeric_order": {}},
    {"grammars": [{"bad": "value"}]},
    {"grammars": [["bad"]]},
])
def test_malformed_stored_date_profile_fails_closed(patch):
    payload = _stored_rule_payload_with_date_profile()
    payload["date_interpretation"]["profiles"][0].update(patch)

    assert _stored_rule_payload(payload) is None


def test_date_profile_addition_obeys_effective_mapping_metadata_limit():
    payload = _stored_rule_payload_with_date_profile()
    source = {"inputs": payload["inputs"], "ignored_columns": []}
    meaning = {"definition": {"code": "HOURS"}, "version": 1}
    without_profile = {key: value for key, value in payload.items() if key != "date_interpretation"}
    existing_size = len(json.dumps(
        {"source": source, "target": meaning, "payload": without_profile},
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    with pytest.raises(ImportFailure) as caught:
        _require_mapping_configuration_size(source, meaning, payload, existing_size)

    assert caught.value.code == "IMPORT_MAPPING_CONFIGURATION_TOO_LARGE"


def test_saved_order_alternatives_do_not_require_choice_for_iso_evidence():
    source = {"mapping": {"1": "WorkDate"},
              "quantity_rules": [{"transform": "Direct", "columns": ["2"]}],
              "materialized": {"rows": [{
                  "cells": {"1": {"value": "2026-10-16", "kind": "text", "formula": False,
                                   "missing_cache": False}, "2": {"value": "1", "missing_cache": False}}
              }]}}

    assert _orders_agree_on_source_evidence(
        source, "column", {"DMY", "MDY"}, [date(2026, 10, 16)]) is True


def test_saved_order_alternatives_require_choice_when_numeric_evidence_selects_different_days():
    source = {"mapping": {"1": "WorkDate"},
              "quantity_rules": [{"transform": "Direct", "columns": ["2"]}],
              "materialized": {"rows": [{
                  "cells": {"1": {"value": "10/11/2026", "kind": "text", "formula": False,
                                   "missing_cache": False}, "2": {"value": "1", "missing_cache": False}}
              }]}}

    assert _orders_agree_on_source_evidence(
        source, "column", {"DMY", "MDY"}, [date(2026, 10, 11), date(2026, 11, 10)]) is False


def test_saved_order_alternatives_do_not_mislabel_formula_errors_as_order_ambiguity():
    source = {"mapping": {"1": "WorkDate"},
              "quantity_rules": [{"transform": "Direct", "columns": ["2"]}],
              "materialized": {"rows": [{
                  "cells": {"1": {"value": None, "kind": "date", "formula": True,
                                   "missing_cache": True}, "2": {"value": "1", "missing_cache": False}}
              }]}}

    assert _orders_agree_on_source_evidence(
        source, "column", {"DMY", "MDY"}, [date(2026, 10, 16)]) is True


def test_skipped_blank_direct_rows_do_not_create_saved_date_order_conflict():
    def cell(value):
        return {"value": value, "kind": "text", "formula": False, "missing_cache": False}

    source = {
        "mapping": {"1": "WorkDate"},
        "quantity_rules": [{"transform": "Direct", "columns": ["2"]}],
        "materialized": {"rows": [
            {"cells": {"1": cell("2026-10-20"), "2": cell("1")}},
            {"cells": {"1": cell("10/11"), "2": cell("")}},
        ]},
    }

    assert _orders_agree_on_source_evidence(
        source, "column", {"DMY", "MDY"},
        [date(2026, 10, 11), date(2026, 10, 20), date(2026, 11, 10)]) is True


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


def test_sum_operand_provenance_is_sliced_to_each_source_cell_page():
    raw = "7" * 2_000
    operands = [
        {"column": 3, "value": raw, "kind": "number"},
        {"column": 4, "value": raw, "kind": "number"},
        None,
    ]
    item = {
        "source_record_id": 9,
        "payload": {str(column): {"value": raw, "kind": "number"} for column in (1, 2, 3, 4)},
        "values_json": {"81": {"source_column": 3, "source_columns": [3, 4],
                                 "transform": "Sum", "raw_value": operands}},
        "issues": [{"column": 3, "code": "IMPORT_SUM_INPUT_MISSING", "value": operands}],
    }

    first = project_source_record(item, 2, 1)
    second = project_source_record(item, 3, 1)

    assert first["values_json"]["81"]["source_columns"] == [3, 4]
    assert first["values_json"]["81"]["raw_value_operand_count"] == 3
    assert first["values_json"]["81"]["raw_value_missing_operand_count"] == 1
    assert [operand["column"] for operand in first["values_json"]["81"]["raw_value"]] == [3]
    assert [operand["column"] for operand in first["issues"][0]["value"]] == [3]
    assert second["values_json"]["81"]["raw_value_operand_count"] == 3
    assert [operand["column"] for operand in second["values_json"]["81"]["raw_value"]] == [4]
    assert second["issues"] == []
    assert len(encoded(first).encode("utf-8")) < 8_000
    assert len(encoded(second).encode("utf-8")) < 8_000


@pytest.mark.parametrize("source", [
    {"kind": "column", "value": "2026-10-20"},
    {"kind": "import_fixed", "value": "2026-10-20"},
    {"kind": "fixed_unit", "value": "10/20/2026"},
    {"kind": "fixed_unit", "value": "2026-10-20", "comment": "untrusted metadata"},
])
def test_date_provider_rejects_noncanonical_or_unselected_values(source):
    with pytest.raises(ImportFailure):
        _normalize_date_source(source, label="2026-10-20", period_days=[date(2026, 10, 20)],
                               date_order=None)
