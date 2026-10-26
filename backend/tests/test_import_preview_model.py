from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal

from app.imports.batch_service import _enforce_metadata_response_bound
from app.imports.errors import ImportFailure
from app.imports.payroll_context import context_identity
from app.imports.preview_model import build_batch_preview
from app.imports.preview_store import prepare_preview
from app.payroll.definition_calculation import PeriodDefinition

WORK_DATE = date(2026, 10, 20)


def _definition(definition_id=100, code="REGULAR", name="Regular work"):
    return PeriodDefinition(
        payroll_period_definition_id=definition_id,
        pay_definition_id=definition_id + 1000,
        rate_definition_id=definition_id + 2000,
        code=code,
        name=name,
        input_type="Decimal",
        unit="hours",
        calculation_method="PerUnit",
        calculation_method_version=1,
        rate_shape="Scalar",
        is_active=True,
    )


def _driver(driver_id=7, number="001", employee_key="EMP-1", name="Driver One"):
    return {"driverid": driver_id, "drivernumber": number,
            "employeekey": employee_key, "drivername": name}


def _assignment(driver_id=7, assignment_id=71, *, eligible=True):
    return {"driverassignmentid": assignment_id, "driverid": driver_id,
            "effectivefromat": datetime(2020, 1, 1, tzinfo=UTC),
            "effectivetoat": None, "endreason": None,
            "branchtimezoneidsnapshot": "America/New_York",
            "iseligibleforperiod": eligible,
            "eligibilityreasoncode": "Active" if eligible else "IncludedByExistingData",
            "eligibleworkdates": [WORK_DATE] if eligible else []}


def _context(*, driver=None, definition=None, assignment=None, live=True,
             current_lines=None, existing_dates=None):
    driver = driver or _driver()
    definition = definition or _definition()
    assignment = assignment or _assignment(driver["driverid"])
    aid, did = assignment["driverassignmentid"], definition.payroll_period_definition_id
    live_days = {(aid, WORK_DATE)} if live else set()
    existing_dates = set(existing_dates or ())
    lines = dict(current_lines or {})
    return {
        "period": {"id": 3, "company_id": 4, "branch_id": 5,
                   "company_code": "ACME", "branch_code": "EAST",
                   "period_code": "WK-2026-42", "period_type": "Week",
                   "start": date(2026, 10, 19), "end": date(2026, 10, 25)},
        "definitions": [definition], "definition_by_id": {did: definition},
        "drivers": [driver], "drivers_by_id": {driver["driverid"]: driver},
        "identity_indexes": {
            "DriverNumber": {driver["drivernumber"]: {driver["driverid"]}},
            "EmployeeKey": {driver["employeekey"]: {driver["driverid"]}},
            "DriverName": {"driver one": {driver["driverid"]}},
        },
        "roster": [assignment], "assignments_by_driver": {driver["driverid"]: [assignment]},
        "live_days": live_days, "days": {WORK_DATE: False}, "current_lines": lines,
        "existing_source_dates": existing_dates, "entries": {},
    }


def _source(*, quantity_value="4.0000", driver_number="001", employee_key=None, driver_name=None):
    mapping = {"1": "DriverNumber", "2": "WorkDate", "3": "Definition:100"}
    cells = {
        "1": {"value": driver_number, "kind": "text", "formula": False, "missing_cache": False},
        "2": {"value": WORK_DATE.isoformat(), "kind": "text", "formula": False, "missing_cache": False},
        "3": {"value": quantity_value, "kind": "text", "formula": False, "missing_cache": False},
    }
    if employee_key is not None:
        mapping["4"] = "EmployeeKey"
        cells["4"] = {"value": employee_key, "kind": "text", "formula": False, "missing_cache": False}
    if driver_name is not None:
        column = str(max(map(int, cells)) + 1)
        mapping[column] = "DriverName"
        cells[column] = {"value": driver_name, "kind": "text", "formula": False, "missing_cache": False}
    return {"file_ordinal": 1, "unit_name": "CSV", "unit_kind": "CSV",
            "structure_fingerprint": "a" * 64, "mapping": mapping,
            "date_source": {"kind": "column"}, "date_order": None,
            "materialized": {"inspection": {"header_row_number": 1,
                "header_cells": ["DriverNumber", "WorkDate", "REGULAR"]},
                "rows": [{"source_row": 2, "cells": cells}],
                "nonempty_columns": sorted(map(int, cells))}}


def test_context_only_existing_source_rescue_changes_plan_not_source_content():
    assignment = _assignment(eligible=False)
    existing_key = (assignment["driverassignmentid"], WORK_DATE)
    rescued = _context(assignment=assignment, live=False, existing_dates={existing_key})
    fresh_assignment = _assignment(eligible=True)
    fresh = _context(assignment=fresh_assignment, live=True)
    rescued_preview = build_batch_preview([_source()], rescued)
    fresh_preview = build_batch_preview([_source()], fresh)

    assert rescued_preview["rows"][0]["values"]["100"]["outcome"] == "Invalid"
    assert any(issue["code"] == "IMPORT_NEW_SOURCE_NOT_ELIGIBLE" for issue in rescued_preview["issues"])
    assert rescued_preview["rows"][0]["row_fingerprint"] == fresh_preview["rows"][0]["row_fingerprint"]
    assert rescued_preview["content_fingerprint"] == fresh_preview["content_fingerprint"]
    assert rescued_preview["plan_fingerprint"] != fresh_preview["plan_fingerprint"]


def test_all_blank_quantity_row_is_skipped_without_identity_or_date_errors_but_missing_formula_is_not_blank():
    source = _source(driver_number="unknown-driver", quantity_value="")
    source["materialized"]["rows"][0]["cells"]["2"]["value"] = "not-a-date"
    skipped = build_batch_preview([source], _context())

    assert skipped["rows"][0]["skipped"] is True
    assert skipped["issues"] == []
    assert skipped["counts"]["errors"] == 0

    source["materialized"]["rows"][0]["cells"]["3"] = {
        "value": None, "kind": "text", "formula": True, "missing_cache": True,
    }
    formula = build_batch_preview([source], _context())
    assert formula["rows"][0]["skipped"] is False
    assert any(issue["code"] == "IMPORT_QUANTITY_INVALID" for issue in formula["issues"])


def test_context_race_identity_tracks_current_quantities_and_rescue_presence():
    context = _context(current_lines={(71, WORK_DATE, 100): Decimal("2")})
    baseline = context_identity(context)
    changed_quantity = deepcopy(context)
    changed_quantity["current_lines"][(71, WORK_DATE, 100)] = Decimal("3")
    changed_rescue = deepcopy(context)
    changed_rescue["existing_source_dates"].add((71, date(2026, 10, 21)))

    assert baseline != context_identity(changed_quantity)
    assert baseline != context_identity(changed_rescue)


def test_semantic_fingerprints_ignore_surrogate_identifier_renumbering():
    baseline = _context(current_lines={(71, WORK_DATE, 100): Decimal("2.0000")})
    source = _source()
    changed_driver = _driver(driver_id=7007)
    changed_definition = _definition(definition_id=9100)
    changed_assignment = _assignment(driver_id=7007, assignment_id=7100)
    renumbered = _context(driver=changed_driver, definition=changed_definition,
                          assignment=changed_assignment,
                          current_lines={(7100, WORK_DATE, 9100): Decimal("2.0000")})
    renumbered["period"].update(id=3003, company_id=4004, branch_id=5005,
                                 created_at="2030-01-01T00:00:00Z")
    baseline["period"]["created_at"] = "2020-01-01T00:00:00Z"
    source["source_unit_id"] = 17
    changed_source = _source()
    changed_source["source_unit_id"] = 1700
    changed_source["mapping"]["3"] = "Definition:9100"

    original = build_batch_preview([deepcopy(source)], baseline)
    equivalent = build_batch_preview([deepcopy(changed_source)], renumbered)
    assert original["mapping_fingerprint"] == equivalent["mapping_fingerprint"]
    assert original["content_fingerprint"] == equivalent["content_fingerprint"]
    assert original["plan_fingerprint"] == equivalent["plan_fingerprint"]
    assert original["rows"][0]["row_fingerprint"] == equivalent["rows"][0]["row_fingerprint"]
    assert original["rows"][0]["driver_id"] == 7
    assert equivalent["rows"][0]["driver_id"] == 7007


def test_definition_input_meaning_is_part_of_row_source_contract_identity():
    source = _source()
    original = build_batch_preview([deepcopy(source)], _context())
    changed_definition = _definition(name="Regular hours")
    changed_context = _context(definition=changed_definition)
    changed = build_batch_preview([deepcopy(source)], changed_context)

    assert original["mapping_fingerprint"] != changed["mapping_fingerprint"]
    assert original["rows"][0]["row_fingerprint"] != changed["rows"][0]["row_fingerprint"]
    assert original["content_fingerprint"] != changed["content_fingerprint"]
    assert original["plan_fingerprint"] != changed["plan_fingerprint"]


def test_matching_and_disagreeing_company_identifiers_and_name_proposals():
    matching = build_batch_preview([_source(employee_key="EMP-1")], _context())
    matching_row = matching["rows"][0]
    assert matching_row["driver_id"] == 7
    assert not any(issue["code"] == "IMPORT_DRIVER_DISAGREEMENT" for issue in matching["issues"])

    other = _driver(driver_id=8, number="002", employee_key="EMP-2", name="Driver Two")
    disagreement_context = _context()
    disagreement_context["drivers"].append(other)
    disagreement_context["drivers_by_id"][8] = other
    disagreement_context["identity_indexes"]["EmployeeKey"]["EMP-2"] = {8}
    disagreement = build_batch_preview([_source(employee_key="EMP-2")], disagreement_context)
    assert disagreement["rows"][0]["driver_id"] is None
    assert any(issue["code"] == "IMPORT_DRIVER_DISAGREEMENT" for issue in disagreement["issues"])

    proposal = build_batch_preview([_source(driver_number="", driver_name="Driver One")], _context())
    assert proposal["rows"][0]["driver_id"] == 7
    assert any(issue["code"] == "IMPORT_DRIVER_CONFIRMATION_REQUIRED" for issue in proposal["issues"])


def test_import_metadata_response_uses_a_serialized_byte_bound():
    response = {"label": "é" * 16}
    assert _enforce_metadata_response_bound(response, max_bytes=100) is response
    try:
        _enforce_metadata_response_bound(response, max_bytes=10)
    except ImportFailure as exc:
        assert exc.code == "IMPORT_METADATA_TOO_LARGE"
    else:
        raise AssertionError("oversized metadata response was accepted")


def test_blank_zero_and_decimal_formatting_have_distinct_and_stable_semantics():
    blank = build_batch_preview([_source(quantity_value="")], _context())
    zero = build_batch_preview([_source(quantity_value="0")], _context())
    two = build_batch_preview([_source(quantity_value="2")], _context())
    formatted_two = build_batch_preview([_source(quantity_value="2.0000")], _context())

    assert blank["mapping_fingerprint"] == zero["mapping_fingerprint"]
    assert blank["content_fingerprint"] != zero["content_fingerprint"]
    assert blank["plan_fingerprint"] != zero["plan_fingerprint"]
    assert blank["rows"][0]["values"]["100"]["is_blank"] is True
    assert zero["rows"][0]["values"]["100"]["is_blank"] is False
    assert two["mapping_fingerprint"] == formatted_two["mapping_fingerprint"]
    assert two["content_fingerprint"] == formatted_two["content_fingerprint"]
    assert two["plan_fingerprint"] == formatted_two["plan_fingerprint"]
    assert two["rows"][0]["row_fingerprint"] == formatted_two["rows"][0]["row_fingerprint"]


def test_ignored_source_value_does_not_change_semantic_fingerprints():
    first_source = _source()
    first_source["mapping"]["4"] = "Ignore"
    first_source["materialized"]["inspection"]["header_cells"].append("Operator note")
    first_source["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "first note", "kind": "text", "formula": False, "missing_cache": False,
    }
    first_source["materialized"]["nonempty_columns"].append(4)
    second_source = deepcopy(first_source)
    second_source["materialized"]["rows"][0]["cells"]["4"]["value"] = "different note"

    first = build_batch_preview([first_source], _context())
    second = build_batch_preview([second_source], _context())

    assert first["mapping_fingerprint"] == second["mapping_fingerprint"]
    assert first["content_fingerprint"] == second["content_fingerprint"]
    assert first["plan_fingerprint"] == second["plan_fingerprint"]
    assert first["rows"][0]["raw_payload"]["4"]["value"] != second["rows"][0]["raw_payload"]["4"]["value"]


def test_current_payroll_quantity_changes_only_plan_fingerprint():
    first = build_batch_preview([_source(quantity_value="4")],
                                _context(current_lines={(71, WORK_DATE, 100): Decimal("2")}))
    changed_current = build_batch_preview([_source(quantity_value="4")],
                                          _context(current_lines={(71, WORK_DATE, 100): Decimal("3")}))

    assert first["mapping_fingerprint"] == changed_current["mapping_fingerprint"]
    assert first["content_fingerprint"] == changed_current["content_fingerprint"]
    assert first["rows"][0]["row_fingerprint"] == changed_current["rows"][0]["row_fingerprint"]
    assert first["plan_fingerprint"] != changed_current["plan_fingerprint"]


def test_mapping_and_date_provider_changes_affect_mapping_and_downstream_fingerprints():
    first_source = _source()
    number = build_batch_preview([first_source], _context())

    employee_source = _source(driver_number="")
    employee_source["mapping"] = {"1": "DriverNumber", "2": "WorkDate", "3": "Definition:100",
                                  "4": "EmployeeKey"}
    employee_source["materialized"]["inspection"]["header_cells"].append("EmployeeKey")
    employee_source["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "EMP-1", "kind": "text", "formula": False, "missing_cache": False,
    }
    employee_source["materialized"]["nonempty_columns"].append(4)
    employee = build_batch_preview([employee_source], _context())
    fixed_date_source = _source()
    fixed_date_source["mapping"].pop("2")
    fixed_date_source["date_source"] = {"kind": "import_fixed", "value": WORK_DATE.isoformat()}
    fixed_date_source["materialized"]["inspection"]["header_cells"] = ["DriverNumber", "REGULAR"]
    fixed_date_source["materialized"]["rows"][0]["cells"].pop("2")
    fixed_date_source["materialized"]["rows"][0]["cells"]["2"] = {
        "value": "", "kind": "text", "formula": False, "missing_cache": False,
    }
    fixed_date_source["materialized"]["nonempty_columns"] = [1, 3]
    fixed = build_batch_preview([fixed_date_source], _context())

    for changed in (employee, fixed):
        assert number["mapping_fingerprint"] != changed["mapping_fingerprint"]
        assert number["content_fingerprint"] != changed["content_fingerprint"]
        assert number["plan_fingerprint"] != changed["plan_fingerprint"]

    fixed_with_extra = deepcopy(fixed_date_source)
    fixed_with_extra["date_source"]["accepted_at"] = "ignored transport metadata"
    normalized_fixed = build_batch_preview([fixed_date_source], _context())
    normalized_with_extra = build_batch_preview([fixed_with_extra], _context())
    assert normalized_fixed["mapping_fingerprint"] == normalized_with_extra["mapping_fingerprint"]
    assert normalized_fixed["content_fingerprint"] == normalized_with_extra["content_fingerprint"]
    assert normalized_fixed["plan_fingerprint"] == normalized_with_extra["plan_fingerprint"]


def test_changing_invalid_mapped_work_date_changes_source_identity():
    first_source = _source()
    first_source["materialized"]["rows"][0]["cells"]["2"]["value"] = "not-a-date"
    second_source = deepcopy(first_source)
    second_source["materialized"]["rows"][0]["cells"]["2"]["value"] = "also-not-a-date"

    first = build_batch_preview([first_source], _context())
    second = build_batch_preview([second_source], _context())

    assert first["mapping_fingerprint"] == second["mapping_fingerprint"]
    assert first["rows"][0]["row_fingerprint"] != second["rows"][0]["row_fingerprint"]
    assert first["content_fingerprint"] != second["content_fingerprint"]
    assert first["plan_fingerprint"] != second["plan_fingerprint"]


def test_distinct_layouts_merge_into_one_driver_date_group():
    definitions = [_definition(100 + index, f"ITEM-{index}") for index in range(6)]
    context = _context(definition=definitions[0])
    context["definitions"] = definitions
    context["definition_by_id"] = {item.payroll_period_definition_id: item for item in definitions}
    context["current_lines"] = {}
    sources = []
    for index, definition in enumerate(definitions):
        source = _source()
        source["file_ordinal"] = index + 1
        source["unit_name"] = f"Sheet {index + 1}"
        source["structure_fingerprint"] = f"{index + 1:064x}"
        source["mapping"]["3"] = f"Definition:{definition.payroll_period_definition_id}"
        source["materialized"]["inspection"]["header_cells"][2] = definition.code
        sources.append(source)

    preview = build_batch_preview(sources, context)

    assert len(preview["groups"]) == 1
    assert len(preview["rows"]) == 6
    assert {key for row in preview["rows"] for key in row["values"]} == {
        str(item.payroll_period_definition_id) for item in definitions
    }


def test_rescued_existing_target_remains_conflict_when_new_target_is_ineligible_in_either_column_order():
    existing_definition = _definition(100, "EXISTING")
    new_definition = _definition(101, "NEW")
    assignment = _assignment(eligible=False)
    context = _context(definition=existing_definition, assignment=assignment, live=False,
                       current_lines={(71, WORK_DATE, 100): Decimal("2")},
                       existing_dates={(71, WORK_DATE)})
    context["definitions"] = [existing_definition, new_definition]
    context["definition_by_id"] = {100: existing_definition, 101: new_definition}

    outcomes = []
    for mapping, cells in (
        ({"1": "DriverNumber", "2": "WorkDate", "3": "Definition:101", "4": "Definition:100"},
         {"3": "1", "4": "4"}),
        ({"1": "DriverNumber", "2": "WorkDate", "3": "Definition:100", "4": "Definition:101"},
         {"3": "4", "4": "1"}),
    ):
        source = _source()
        source["mapping"] = mapping
        source["materialized"]["inspection"]["header_cells"] = ["DriverNumber", "WorkDate", "EXISTING", "NEW"]
        source["materialized"]["nonempty_columns"] = [1, 2, 3, 4]
        source["materialized"]["rows"][0]["cells"].update({
            column: {"value": value, "kind": "text", "formula": False, "missing_cache": False}
            for column, value in cells.items()
        })
        preview = build_batch_preview([source], context)
        outcomes.append({definition_id: value["outcome"]
                         for definition_id, value in preview["rows"][0]["values"].items()})
        assert any(issue["code"] == "IMPORT_NEW_SOURCE_NOT_ELIGIBLE" for issue in preview["issues"])

    assert outcomes == [{"101": "Invalid", "100": "Conflict"}, {"100": "Conflict", "101": "Invalid"}]


def test_preview_preparation_serializes_rows_and_discards_duplicate_python_payloads():
    result = build_batch_preview([_source()], _context())
    original_group_counts = {
        key: result["groups"][0][key] for key in ("error_count", "warning_count", "conflict_count")
    }

    prepared = prepare_preview(result)

    row = prepared["rows"][0]
    assert prepared["_prepared"] is True
    assert "semantic" not in row and "raw_payload" not in row
    assert "values" not in row and "issues" not in row
    assert json.loads(row["_prepared_json"]["values"])["100"]["quantity"] == "4"
    assert json.loads(row["_prepared_json"]["payload"])["3"]["value"] == "4.0000"
    assert {key: prepared["groups"][0][key] for key in original_group_counts} == original_group_counts
    assert "issues" not in prepared
