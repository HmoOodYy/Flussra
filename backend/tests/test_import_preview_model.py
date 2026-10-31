from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.imports.batch_service import _enforce_metadata_response_bound
from app.imports.errors import ImportFailure
from app.imports.mapping import normalized_identifier
from app.imports.payroll_context import context_identity
from app.imports.policy import ImportResourcePolicy
from app.imports.preview_model import (
    _apply_row_handling,
    _effective_row_policy,
    build_batch_preview,
)
from app.imports.preview_store import prepare_preview
from app.payroll.definition_calculation import PeriodDefinition

WORK_DATE = date(2026, 10, 20)


def _definition(definition_id=100, code="REGULAR", name="Regular work", input_type="Decimal"):
    return PeriodDefinition(
        payroll_period_definition_id=definition_id,
        pay_definition_id=definition_id + 1000,
        rate_definition_id=definition_id + 2000,
        code=code,
        name=name,
        input_type=input_type,
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
    normalized_name = normalized_identifier("DriverName", driver["drivername"])
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
        "name_match_summaries": {normalized_name: {"count": 1, "driver_id": driver["driverid"],
            "driver": driver, "samples": [driver],
            "membership_sha256": hashlib.sha256(driver["driverid"].to_bytes(8, "big")).hexdigest()}},
        "roster": [assignment], "assignments_by_driver": {driver["driverid"]: [assignment]},
        "live_days": live_days, "days": {WORK_DATE: False}, "current_lines": lines,
        "current_line_provenance": {},
        "existing_source_dates": existing_dates, "entries": {},
    }


def _source(*, quantity_value="4.0000", driver_number="001", employee_key=None, driver_name=None):
    mapping = {"1": "DriverNumber", "2": "WorkDate"}
    quantity_rules = [{"target": "Definition:100", "transform": "Direct", "columns": ["3"],
                       "inputs": [{"header": "REGULAR"}],
                       "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                                          "WorkDate": {"header": "WORKDATE"}},
                       "source_signature": {"inputs": [{"header": "regular"}]}}]
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
    return {"source_unit_id": 1, "file_ordinal": 1, "unit_name": "CSV", "unit_kind": "CSV",
            "structure_fingerprint": "a" * 64, "mapping": mapping, "quantity_rules": quantity_rules,
            "date_source": {"kind": "column"}, "date_order": None,
            "materialized": {"inspection": {"header_row_number": 1,
                "header_cells": ["DriverNumber", "WorkDate", "REGULAR"]},
                "rows": [{"source_row": 2, "cells": cells}],
                "nonempty_columns": sorted(map(int, cells))}}


def _sum_source(*, first="0.5", second="0.5"):
    source = _source(quantity_value=first)
    source["quantity_rules"] = [{"target": "Definition:100", "transform": "Sum", "columns": ["3", "4"],
                                 "inputs": [{"header": "REGULAR A"}, {"header": "REGULAR B"}],
                                 "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                                                    "WorkDate": {"header": "WORKDATE"}},
                                 "source_signature": {"inputs": [{"header": "regular-a"},
                                                                    {"header": "regular-b"}]}}]
    source["materialized"]["inspection"]["header_cells"] = [
        "DriverNumber", "WorkDate", "Regular A", "Regular B"]
    source["materialized"]["rows"][0]["cells"]["4"] = {
        "value": second, "kind": "number", "formula": False, "missing_cache": False,
    }
    source["materialized"]["nonempty_columns"] = [1, 2, 3, 4]
    return source


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
    changed_source["quantity_rules"][0]["target"] = "Definition:9100"

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


def test_canonical_name_basis_changes_plan_but_not_source_content_identity():
    source = _source(driver_name="Imported Variation")
    first = build_batch_preview([deepcopy(source)], _context(
        driver=_driver(name="Current Canonical Name A")))
    second = build_batch_preview([deepcopy(source)], _context(
        driver=_driver(name="Current Canonical Name B")))

    assert first["content_fingerprint"] == second["content_fingerprint"]
    assert first["plan_fingerprint"] != second["plan_fingerprint"]
    assert first["rows"][0]["context"]["driver_identity"]["status"] == "NameDiscrepancy"
    assert second["rows"][0]["context"]["driver_identity"]["status"] == "NameDiscrepancy"


def test_formula_without_cached_stable_identifier_blocks_other_key_fallback():
    source = _source(driver_number="", employee_key="EMP-1")
    source["materialized"]["rows"][0]["cells"]["1"] = {
        "value": None, "kind": "text", "formula": True, "missing_cache": True,
    }

    preview = build_batch_preview([source], _context())

    assert preview["rows"][0]["driver_id"] is None
    assert any(issue["code"] == "IMPORT_DRIVER_UNRESOLVED" for issue in preview["issues"])


def test_formula_without_cached_name_blocks_a_known_driver_code():
    source = _source(driver_number="001", driver_name="")
    name_column = next(column for column, target in source["mapping"].items()
                       if target == "DriverName")
    source["materialized"]["rows"][0]["cells"][name_column] = {
        "value": None, "kind": "text", "formula": True, "missing_cache": True,
    }

    preview = build_batch_preview([source], _context())

    assert preview["rows"][0]["driver_id"] is None
    assert any(issue["code"] == "IMPORT_DRIVER_UNRESOLVED" and issue["severity"] == "Error"
               for issue in preview["issues"])


def test_invalid_target_contribution_does_not_block_other_target_aggregation():
    first_definition = _definition()
    second_definition = _definition(101, "BONUS", "Bonus", "Decimal")
    context = _context(definition=first_definition)
    context["definitions"].append(second_definition)
    context["definition_by_id"][101] = second_definition
    source = _source(quantity_value="not-a-number")
    source["mapping"]["4"] = "Definition:101"
    source["materialized"]["inspection"]["header_cells"].append("BONUS")
    source["quantity_rules"] = [
        {"target": "Definition:100", "transform": "Direct", "columns": ["3"],
         "inputs": [{"header": "REGULAR"}],
         "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                            "WorkDate": {"header": "WORKDATE"}},
         "row_handling": {"version": 1, "mode": "SumRows", "choices": []}},
        {"target": "Definition:101", "transform": "Direct", "columns": ["4"],
         "inputs": [{"header": "BONUS"}],
         "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                            "WorkDate": {"header": "WORKDATE"}},
         "row_handling": {"version": 1, "mode": "SumRows", "choices": []}},
    ]
    first = source["materialized"]["rows"][0]
    first["cells"]["4"] = {"value": "2", "kind": "number", "formula": False, "missing_cache": False}
    second = deepcopy(first)
    second["source_row"] = 3
    second["cells"]["3"] = {"value": "3", "kind": "number", "formula": False, "missing_cache": False}
    second["cells"]["4"] = {"value": "4", "kind": "number", "formula": False, "missing_cache": False}
    source["materialized"]["rows"].append(second)

    preview = build_batch_preview([source], context)

    assert preview["rows"][0]["values"]["100"]["outcome"] == "Invalid"
    assert preview["rows"][0]["values"]["101"]["quantity"] == "6"
    assert preview["rows"][0]["values"]["101"]["outcome"] == "New"
    assert any(issue["code"] == "IMPORT_QUANTITY_INVALID" and issue.get("column") == 3
               for issue in preview["issues"])


def test_row_context_size_does_not_repeat_the_category_choice_domain():
    source = _source()
    source["mapping"]["4"] = "Filter"
    source["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "Include", "kind": "text", "formula": False, "missing_cache": False,
    }

    def preview_with_choices(count):
        source_copy = deepcopy(source)
        choices = [{"category": {"type": "text", "value": f"Category {index}"},
                    "decision": "Include"} for index in range(count)]
        choices[0] = {"category": {"type": "text", "value": "Include"}, "decision": "Include"}
        source_copy["quantity_rules"][0]["row_handling"] = {
            "version": 1, "mode": "FilterThenSum", "choices": choices,
        }
        result = build_batch_preview([source_copy], _context())
        return result["rows"][0]["context"]

    small = preview_with_choices(1)
    large = preview_with_choices(1000)

    assert small["row_handling"] == large["row_handling"] == {"mode": "FilterThenSum"}
    assert "choices" not in large["row_handling"]
    assert len(json.dumps(large, separators=(",", ":"))) == len(json.dumps(small, separators=(",", ":")))


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


def test_sum_validates_final_quantity_and_warns_for_blank_operands_on_driver_date_row():
    whole_number_context = _context(definition=_definition(input_type="WholeNumber"))
    combined = build_batch_preview([_sum_source()], whole_number_context)

    assert combined["rows"][0]["skipped"] is False
    assert combined["rows"][0]["values"]["100"]["quantity"] == "1"
    assert not any(issue["code"] == "IMPORT_QUANTITY_INVALID" for issue in combined["issues"])

    blanked = build_batch_preview([_sum_source(first="", second="")], _context())
    assert blanked["rows"][0]["skipped"] is False
    assert blanked["rows"][0]["values"]["100"]["is_blank"] is True
    assert any(issue["code"] == "IMPORT_SUM_INPUT_MISSING" for issue in blanked["issues"])


def test_sum_missing_operand_does_not_mask_invalid_nonblank_operand():
    preview = build_batch_preview([_sum_source(first="", second="bad")], _context())

    assert preview["rows"][0]["skipped"] is False
    assert any(issue["code"] == "IMPORT_QUANTITY_INVALID" for issue in preview["issues"])
    assert not any(issue["code"] == "IMPORT_SUM_INPUT_MISSING" for issue in preview["issues"])


def test_sum_with_no_physical_operands_warns_without_emitting_unprovenanced_value():
    source = _sum_source()
    source["quantity_rules"][0]["columns"] = [None, None]
    preview = build_batch_preview([source], _context())

    assert preview["rows"][0]["skipped"] is False
    assert preview["rows"][0]["values"] == {}
    assert any(issue["code"] == "IMPORT_SUM_INPUT_MISSING"
               and issue["column"] is None for issue in preview["issues"])
    assert preview["rows"][0]["semantic"]["values"]["REGULAR"]["missing_source"] is True


def test_direct_rule_with_absent_input_preserves_semantics_without_candidate_value():
    source = _source()
    source["quantity_rules"][0]["columns"] = [None]
    preview = build_batch_preview([source], _context())

    assert preview["rows"][0]["values"] == {}
    assert preview["rows"][0]["semantic"]["values"]["REGULAR"]["missing_source"] is True
    assert preview["issues"] == []


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
    employee_source["quantity_rules"][0]["shared_columns"]["EmployeeKey"] = {"header": "EMPLOYEEKEY"}
    employee_source["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "EMP-1", "kind": "text", "formula": False, "missing_cache": False,
    }
    employee_source["materialized"]["nonempty_columns"].append(4)
    employee = build_batch_preview([employee_source], _context())
    filename_date_source = _source()
    filename_date_source["mapping"].pop("2")
    filename_date_source["date_source"] = {"kind": "filename"}
    filename_date_source["file_name"] = "weekly-2026-10-20.csv"
    filename_date_source["materialized"]["inspection"]["header_cells"] = ["DriverNumber", "WorkDate", "REGULAR"]
    filename_date_source["materialized"]["rows"][0]["cells"].pop("2")
    filename_date_source["materialized"]["nonempty_columns"] = [1, 3]
    filename = build_batch_preview([filename_date_source], _context())

    for changed in (employee, filename):
        assert number["mapping_fingerprint"] != changed["mapping_fingerprint"]
        assert number["content_fingerprint"] != changed["content_fingerprint"]
        assert number["plan_fingerprint"] != changed["plan_fingerprint"]

    filename_with_extra = deepcopy(filename_date_source)
    filename_with_extra["date_source"]["accepted_at"] = "ignored transport metadata"
    try:
        build_batch_preview([filename_with_extra], _context())
    except ImportFailure as exc:
        assert exc.code == "IMPORT_DATE_SOURCE_REQUIRED"
    else:
        raise AssertionError("unsupported date source fields must fail closed")


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


def test_filename_date_authority_ignores_row_date_values_and_explains_resolution():
    first_source = _source()
    first_source["file_name"] = "weekly-2026-10-20.csv"
    first_source["date_source"] = {"kind": "filename"}
    first_source["materialized"]["rows"][0]["cells"]["2"]["value"] = "2026-10-21"
    second_source = deepcopy(first_source)
    second_source["materialized"]["rows"][0]["cells"]["2"]["value"] = "not a date"

    first = build_batch_preview([first_source], _context())
    second = build_batch_preview([second_source], _context())

    assert first["rows"][0]["work_date"] == WORK_DATE
    assert second["rows"][0]["work_date"] == WORK_DATE
    assert first["rows"][0]["row_fingerprint"] == second["rows"][0]["row_fingerprint"]
    assert first["content_fingerprint"] == second["content_fingerprint"]
    assert first["plan_fingerprint"] == second["plan_fingerprint"]
    assert first["rows"][0]["context"]["date_interpretation"]["reason"] == "explicit_date"
    assert first["date_rule_profiles"]["Definition:100"][0]["authority"] == "filename"
    assert first["date_review"]["recognized_days"] == [WORK_DATE.isoformat()]


def test_column_date_authority_ignores_date_looking_filename_text():
    first_source = _source()
    first_source["file_name"] = "work-2026-10-20.csv"
    second_source = deepcopy(first_source)
    second_source["file_name"] = "work-2025-02-30.csv"

    first = build_batch_preview([first_source], _context())
    second = build_batch_preview([second_source], _context())

    assert first["rows"][0]["work_date"] == second["rows"][0]["work_date"] == WORK_DATE
    assert first["rows"][0]["row_fingerprint"] == second["rows"][0]["row_fingerprint"]
    assert first["content_fingerprint"] == second["content_fingerprint"]
    assert first["plan_fingerprint"] == second["plan_fingerprint"]


def test_date_review_samples_expose_counts_and_truncation_flags():
    days = [date(2026, 10, 20) + timedelta(days=offset) for offset in range(4)]
    context = _context()
    assignment = context["roster"][0]
    assignment["eligibleworkdates"] = days
    context["days"] = {day: False for day in days}
    context["live_days"] = {(assignment["driverassignmentid"], day) for day in days}
    sources = []
    for ordinal, day in enumerate(days[:2], start=1):
        source = _source()
        source["file_ordinal"] = ordinal
        source["file_name"] = f"payroll-{day.isoformat()}.csv"
        source["date_source"] = {"kind": "filename"}
        source["mapping"].pop("2")
        sources.append(source)

    result = build_batch_preview(sources, context, policy=ImportResourcePolicy(date_review_day_sample=1))
    review = result["date_review"]

    assert review["recognized_days"] == [days[0].isoformat()]
    assert review["recognized_day_count"] == 2
    assert review["recognized_days_truncated"] is True
    assert review["missing_coverage_days"] == [days[2].isoformat()]
    assert review["missing_coverage_count"] == 2
    assert review["missing_coverage_days_truncated"] is True


def test_date_order_state_uses_resolved_numeric_written_month_and_name_evidence():
    cases = [
        ("column", "03/04", date(2026, 3, 4), "MDY", "detected"),
        ("column", "O.c_t (Samsara) 16", date(2026, 10, 16), None, "not_required"),
        ("filename", "2026-10-16", date(2026, 10, 16), None, "not_required"),
    ]
    for authority, value, day, expected_order, expected_state in cases:
        context = _context()
        assignment = context["roster"][0]
        assignment["eligibleworkdates"] = [day]
        context["period"]["start"] = day
        context["period"]["end"] = day
        context["days"] = {day: False}
        context["live_days"] = {(assignment["driverassignmentid"], day)}
        source = _source()
        source["date_order"] = None
        if authority == "column":
            source["materialized"]["rows"][0]["cells"]["2"]["value"] = value
        else:
            source["mapping"].pop("2")
            source["date_source"] = {"kind": authority}
            source["file_name"] = f"payroll-{value}.csv"

        result = build_batch_preview([source], context)
        interpreted = result["interpretations"][0]

        assert result["rows"][0]["work_date"] == day
        assert interpreted["date_order"] == expected_order
        assert interpreted["date_order_state"] == expected_state
        assert result["date_rule_profiles"]["Definition:100"][0]["numeric_order"] == expected_order


def test_filename_date_does_not_activate_an_otherwise_blank_row_from_workdate():
    source = _source(quantity_value="", driver_number="")
    source["mapping"].pop("2")
    source["date_source"] = {"kind": "filename"}
    source["file_name"] = "weekly-2026-10-20.csv"
    source["materialized"]["rows"][0]["cells"]["2"]["value"] = "garbage"

    result = build_batch_preview([source], _context())

    assert result["rows"][0]["skipped"] is True
    assert result["rows"][0]["issues"] == []
    assert result["date_review"]["recognized_days"] == [WORK_DATE.isoformat()]


def test_equal_filename_date_meaning_has_same_content_identity_across_supported_spellings():
    variants = ["weekly-2026-10-20.csv", "weekly-Oct-20-2026.csv", "weekly-Oct-20.csv"]
    results = []
    for label in variants:
        source = _source()
        source["mapping"].pop("2")
        source["date_source"] = {"kind": "filename"}
        source["file_name"] = label
        results.append(build_batch_preview([source], _context()))

    assert {item["rows"][0]["work_date"] for item in results} == {WORK_DATE}
    assert len({item["mapping_fingerprint"] for item in results}) == 1
    assert len({item["content_fingerprint"] for item in results}) == 1
    assert len({item["plan_fingerprint"] for item in results}) == 1


def test_invalid_filename_date_evidence_changes_content_identity():
    sources = []
    for label in ("weekly-2026-02-30.csv", "weekly-2026-02-31.csv"):
        source = _source()
        source["mapping"].pop("2")
        source["date_source"] = {"kind": "filename"}
        source["file_name"] = label
        sources.append(source)

    first, second = (build_batch_preview([source], _context()) for source in sources)

    assert first["rows"][0]["work_date"] is None
    assert first["rows"][0]["row_fingerprint"] != second["rows"][0]["row_fingerprint"]
    assert first["content_fingerprint"] != second["content_fingerprint"]


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
        source["quantity_rules"][0]["target"] = f"Definition:{definition.payroll_period_definition_id}"
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
    for quantity_rules, cells in (
        ([{"target": "Definition:101", "transform": "Direct", "columns": ["3"],
          "inputs": [{"header": "EXISTING"}],
          "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                             "WorkDate": {"header": "WORKDATE"}}},
         {"target": "Definition:100", "transform": "Direct", "columns": ["4"],
          "inputs": [{"header": "NEW"}],
          "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                             "WorkDate": {"header": "WORKDATE"}}}],
         {"3": "1", "4": "4"}),
        ([{"target": "Definition:100", "transform": "Direct", "columns": ["3"],
          "inputs": [{"header": "EXISTING"}],
          "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                             "WorkDate": {"header": "WORKDATE"}}},
         {"target": "Definition:101", "transform": "Direct", "columns": ["4"],
          "inputs": [{"header": "NEW"}],
          "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                             "WorkDate": {"header": "WORKDATE"}}}],
         {"3": "4", "4": "1"}),
    ):
        source = _source()
        source["quantity_rules"] = quantity_rules
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


def test_sum_rows_prepares_one_final_quantity_candidate_and_preserves_contributions():
    source = _source(quantity_value="0.5")
    source["quantity_rules"][0]["row_handling"] = {
        "version": 1, "mode": "SumRows", "choices": []}
    second = deepcopy(source["materialized"]["rows"][0])
    second["source_row"] = 3
    second["cells"]["3"] = {"value": "0.5", "kind": "text", "formula": False,
                             "missing_cache": False}
    source["materialized"]["rows"].append(second)

    preview = build_batch_preview([source], _context())

    assert len(preview["rows"]) == 2
    assert preview["rows"][0]["values"]["100"]["quantity"] == "1"
    assert preview["rows"][0]["values"]["100"]["is_candidate"] is True
    assert preview["rows"][0]["values"]["100"]["contribution_quantity"] == "0.5"
    assert preview["rows"][1]["values"]["100"]["quantity"] is None
    assert preview["rows"][1]["values"]["100"]["is_candidate"] is False
    assert preview["rows"][1]["values"]["100"]["contribution_quantity"] == "0.5"
    assert not any(issue["code"] == "IMPORT_DUPLICATE_FACT" for issue in preview["issues"])


def test_sum_rows_applies_current_value_and_eligibility_checks_to_final_total():
    source = _source(quantity_value="0.5")
    source["quantity_rules"][0]["row_handling"] = {
        "version": 1, "mode": "SumRows", "choices": []}
    second = deepcopy(source["materialized"]["rows"][0])
    second["source_row"] = 3
    second["cells"]["3"] = {"value": "0.5", "kind": "number", "formula": False,
                             "missing_cache": False}
    source["materialized"]["rows"].append(second)

    unchanged = build_batch_preview([source], _context(current_lines={(71, WORK_DATE, 100): Decimal("1")}))
    assert unchanged["rows"][0]["values"]["100"]["quantity"] == "1"
    assert unchanged["rows"][0]["values"]["100"]["outcome"] == "Unchanged"
    assert not any(issue["code"] == "IMPORT_VALUE_CONFLICT" for issue in unchanged["issues"])

    ineligible = build_batch_preview([source], _context(live=False, existing_dates={(71, WORK_DATE)}))
    assert ineligible["rows"][0]["values"]["100"]["outcome"] == "Invalid"
    assert any(issue["code"] == "IMPORT_NEW_SOURCE_NOT_ELIGIBLE" for issue in ineligible["issues"])

    source["materialized"]["rows"][0]["cells"]["3"]["value"] = "1"
    second["cells"]["3"]["value"] = "-1"
    context = _context()
    context["days"][WORK_DATE] = True
    cancelled = build_batch_preview([source], context)
    assert cancelled["rows"][0]["values"]["100"]["quantity"] == "0"
    assert not any(issue["code"] == "IMPORT_DAY_OFF_DECISION_REQUIRED" for issue in cancelled["issues"])


def test_incomplete_row_aggregate_has_no_candidate_quantity_and_is_blank():
    source = _source(quantity_value="0.5")
    source["quantity_rules"][0]["row_handling"] = {
        "version": 1, "mode": "SumRows", "choices": []}
    second = deepcopy(source["materialized"]["rows"][0])
    second["source_row"] = 3
    second["cells"]["3"] = {"value": "", "kind": "text", "formula": False,
                             "missing_cache": False}
    source["materialized"]["rows"].append(second)

    preview = build_batch_preview([source], _context())

    value = preview["rows"][0]["values"]["100"]
    assert value["quantity"] is None
    assert value["is_blank"] is True
    assert value["aggregate_status"] == "withheld"
    assert value["aggregate_reason"] == "incomplete_contribution"
    assert value["outcome"] == "Blank"
    assert any(issue["code"] == "IMPORT_ROW_CONTRIBUTION_WITHHELD" for issue in preview["issues"])


def test_repeated_driver_day_with_multiple_assignments_is_not_split_or_aggregated():
    rows = []
    for source_row, assignment_id in ((2, 71), (3, 72)):
        rows.append({
            "file_ordinal": 1, "unit_name": "CSV", "source_row": source_row,
            "driver_id": 7, "driver_assignment_id": assignment_id, "work_date": WORK_DATE,
            "skipped": False, "issues": [],
            "context": {"identity_resolved": True, "row_handling": {"mode": "SumRows"}},
            "values": {"100": {"quantity": "1", "contribution_quantity": "1",
                                "is_blank": False, "outcome": "New"}},
        })
    issues = []

    _apply_row_handling(rows, {"Definition:100": _definition()}, {}, issues, max_work_digits=16000)

    assert [row["values"]["100"]["is_candidate"] for row in rows] == [True, False]
    assert all(row["values"]["100"]["quantity"] is None for row in rows)
    assert all(row["values"]["100"]["outcome"] == "Unresolved" for row in rows)
    assert all(row["values"]["100"]["aggregate_reason"] == "multiple_assignments" for row in rows)
    assert [issue["code"] for issue in issues] == [
        "IMPORT_ROW_ASSIGNMENT_CONFLICT", "IMPORT_ROW_ASSIGNMENT_CONFLICT"]


def test_name_proposal_cannot_become_an_accepted_repeated_row_contribution():
    source = _source(quantity_value="2")
    source["mapping"]["4"] = "DriverName"
    source["quantity_rules"][0]["row_handling"] = {
        "version": 1, "mode": "SumRows", "choices": []}
    source["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "Driver One", "kind": "text", "formula": False, "missing_cache": False,
    }
    proposed = deepcopy(source["materialized"]["rows"][0])
    proposed["source_row"] = 3
    proposed["cells"]["1"]["value"] = ""
    proposed["cells"]["3"]["value"] = "5"
    source["materialized"]["rows"].append(proposed)

    preview = build_batch_preview([source], _context())

    assert preview["rows"][0]["context"]["driver_identity"]["status"] == "Exact"
    assert preview["rows"][1]["context"]["driver_identity"]["status"] == "Proposed"
    assert preview["rows"][0]["values"]["100"]["quantity"] is None
    assert preview["rows"][0]["values"]["100"]["outcome"] == "Unresolved"
    assert preview["rows"][1]["values"]["100"]["is_candidate"] is False
    assert preview["rows"][1]["values"]["100"]["quantity"] is None
    assert not any(value["outcome"] == "New" for row in preview["rows"]
                   for value in row["values"].values())


def test_conflicting_target_row_policies_are_not_resolved_by_target_order():
    source = _source()
    source["mapping"]["4"] = "Definition:101"
    source["quantity_rules"] = [
        {"target": "Definition:100", "transform": "Direct", "columns": ["3"],
         "inputs": [{"header": "REGULAR"}],
         "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                            "WorkDate": {"header": "WORKDATE"}},
         "row_handling": {"version": 1, "mode": "SumRows", "choices": []}},
        {"target": "Definition:101", "transform": "Direct", "columns": ["4"],
         "inputs": [{"header": "BONUS"}],
         "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                            "WorkDate": {"header": "WORKDATE"}},
         "row_handling": {"version": 1, "mode": "RejectRepeated", "choices": []}},
    ]
    context = _context()
    second_definition = _definition(101, "BONUS", "Bonus", "Decimal")
    context["definitions"].append(second_definition)
    context["definition_by_id"][101] = second_definition

    try:
        build_batch_preview([source], context)
    except ImportFailure as exc:
        assert exc.code == "IMPORT_ROW_HANDLING_CONFLICT"
    else:
        raise AssertionError("Contradictory target policies must require an explicit common setup.")


def test_inactive_filter_metadata_does_not_change_nonfilter_row_policy_meaning():
    assert _effective_row_policy({"row_handling": None,
                                  "filter_selector": {"header": "Old category"}}) == {
        "mode": "RejectRepeated"}
    assert _effective_row_policy({"row_handling": {"mode": "SumRows", "choices": []},
                                  "filter_selector": {"header": "Old category"}}) == {
        "mode": "SumRows"}
    assert _effective_row_policy({"row_handling": {
        "mode": "FilterThenSum", "choices": [{"category": {"type": "text", "value": "X"},
                                                  "decision": "Include"}]},
        "filter_selector": {"header": "Activity"}}) == {
        "mode": "FilterThenSum", "filter": {"header": "Activity"},
        "choices": [{"category": {"type": "text", "value": "X"}, "decision": "Include"}]}


def test_filter_exclusion_precedes_identifier_date_and_quantity_validation():
    source = _source(quantity_value="4")
    source["mapping"]["4"] = "Filter"
    source["materialized"]["inspection"]["header_cells"].append("Category")
    source["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "Include", "kind": "text", "formula": False, "missing_cache": False}
    excluded = deepcopy(source["materialized"]["rows"][0])
    excluded["source_row"] = 3
    excluded["cells"]["1"] = {"value": "bad-driver", "kind": "text", "formula": False,
                               "missing_cache": False}
    excluded["cells"]["2"] = {"value": "2025-99-99", "kind": "text", "formula": False,
                               "missing_cache": False}
    excluded["cells"]["3"] = {"value": "not-a-quantity", "kind": "text", "formula": False,
                               "missing_cache": False}
    excluded["cells"]["4"] = {"value": "Ignore", "kind": "text", "formula": False,
                               "missing_cache": False}
    source["materialized"]["rows"].append(excluded)
    source["quantity_rules"][0]["row_handling"] = {
        "version": 1, "mode": "FilterThenSum", "choices": [
            {"category": {"type": "text", "value": "Ignore"}, "decision": "Exclude"},
            {"category": {"type": "text", "value": "Include"}, "decision": "Include"},
        ]}

    preview = build_batch_preview([source], _context())

    excluded_row = next(row for row in preview["rows"] if row["source_row"] == 3)
    assert excluded_row["skipped"] is True
    assert excluded_row["context"]["proposed_exclusion"]["status"] == "proposed_excluded"
    assert excluded_row["driver_id"] is None and excluded_row["work_date"] is None
    assert not any(issue["source_row"] == 3 for issue in preview["issues"])
    assert preview["counts"]["prepared_excluded_rows"] == 1


def test_repeated_driver_day_is_rejected_without_rule_even_for_complementary_targets():
    source = _source(quantity_value="4")
    source["mapping"]["4"] = "Definition:101"
    source["quantity_rules"].append({"target": "Definition:101", "transform": "Direct",
                                     "columns": ["4"], "inputs": [{"header": "OVERTIME"}],
                                     "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                                                        "WorkDate": {"header": "WORKDATE"}}})
    second = deepcopy(source["materialized"]["rows"][0])
    second["source_row"] = 3
    source["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "", "kind": "text", "formula": False, "missing_cache": False}
    second["cells"]["3"] = {"value": "", "kind": "text", "formula": False, "missing_cache": False}
    second["cells"]["4"] = {"value": "2", "kind": "text", "formula": False, "missing_cache": False}
    source["materialized"]["inspection"]["header_cells"].append("OVERTIME")
    source["materialized"]["rows"].append(second)
    context = _context()
    second_definition = _definition(101, "OVERTIME", "Overtime")
    context["definitions"].append(second_definition)
    context["definition_by_id"][101] = second_definition

    preview = build_batch_preview([source], context)

    assert len([issue for issue in preview["issues"] if issue["code"] == "IMPORT_DUPLICATE_FACT"]) == 2
    assert all(value["outcome"] == "Invalid"
               for row in preview["rows"] for value in row["values"].values())


def test_repeat_receipt_ignores_layout_display_and_manual_current_state_but_reports_noop():
    source = _source()
    baseline = build_batch_preview([source], _context())
    (receipt,) = baseline["semantic_receipts"]

    unrelated_layout = deepcopy(source)
    unrelated_layout["structure_fingerprint"] = "c" * 64
    unrelated_layout["materialized"]["inspection"]["header_cells"].append("Unused Notes")
    unrelated_layout["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "presentation only", "kind": "text", "formula": False, "missing_cache": False}
    renamed = _context()
    renamed["drivers_by_id"][7]["drivername"] = "A later display name"
    renamed["drivers_by_id"][7]["employeekey"] = "A later unmapped employee key"
    equivalent = build_batch_preview([unrelated_layout], renamed)
    assert equivalent["mapping_fingerprint"] == baseline["mapping_fingerprint"]
    assert equivalent["content_fingerprint"] == baseline["content_fingerprint"]
    assert equivalent["semantic_receipts"] == baseline["semantic_receipts"]

    manually_edited = _context(current_lines={(71, WORK_DATE, 100): Decimal("99")})
    manually_edited["current_line_provenance"][(71, WORK_DATE, 100)] = {
        "source_type": "Manual", "draft_line_id": 881, "added_by_user_id": 33}
    repeat = build_batch_preview([source], manually_edited, completed_imports=[{
        "import_id": 77, "completed_by_user_id": 12, "completed_at": datetime(2026, 10, 27, tzinfo=UTC),
        "targets": [receipt],
    }])
    value = repeat["rows"][0]["values"]["100"]
    assert repeat["repeat_notice"]["state"] == "Complete"
    assert repeat["candidates"] == 1
    assert value["outcome"] == "AlreadyImported"
    assert value["action"] == "NoOp"
    assert Decimal(str(value["quantity"])) == Decimal("4")
    assert Decimal(str(value["current_quantity"])) == Decimal("99")
    assert value["current_provenance"]["source_type"] == "Manual"
    assert repeat["content_fingerprint"] == baseline["content_fingerprint"]
    assert repeat["plan_fingerprint"] != baseline["plan_fingerprint"]


def test_repeated_target_matches_independently_when_another_supplied_target_is_invalid():
    source = _source()
    baseline = build_batch_preview([source], _context())
    (receipt,) = baseline["semantic_receipts"]

    second_definition = _definition(101, "OVERTIME", "Overtime")
    context = _context(definition=_definition())
    context["definitions"].append(second_definition)
    context["definition_by_id"][101] = second_definition
    batch = deepcopy(source)
    batch["quantity_rules"].append({"target": "Definition:101", "transform": "Direct", "columns": ["4"],
        "inputs": [{"header": "OVERTIME"}],
        "shared_columns": {"DriverNumber": {"header": "DRIVERNUMBER"},
                            "WorkDate": {"header": "WORKDATE"}},
        "source_signature": {"inputs": [{"header": "overtime"}]}})
    batch["materialized"]["inspection"]["header_cells"].append("OVERTIME")
    batch["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "not a quantity", "kind": "text", "formula": False, "missing_cache": False}

    preview = build_batch_preview([batch], context, completed_imports=[{
        "import_id": 77, "completed_by_user_id": 12, "completed_at": datetime(2026, 10, 27, tzinfo=UTC),
        "targets": [receipt],
    }])

    assert preview["repeat_notice"]["state"] == "Partial"
    assert preview["repeat_notice"]["matched_target_count"] == 1
    assert preview["repeat_notice"]["supplied_target_count"] == 2
    row = preview["rows"][0]
    assert row["values"]["100"]["outcome"] == "AlreadyImported"
    assert row["values"]["100"]["action"] == "NoOp"
    assert row["values"]["101"]["outcome"] == "Invalid"
    assert any(issue["code"] == "IMPORT_QUANTITY_INVALID" for issue in row["issues"])


def test_repeat_receipt_does_not_bypass_unresolved_source_identity():
    source = _source()
    baseline = build_batch_preview([source], _context())
    (receipt,) = baseline["semantic_receipts"]
    changed = _source(driver_number="UNKNOWN")

    preview = build_batch_preview([changed], _context(), completed_imports=[{
        "import_id": 77, "completed_by_user_id": 12, "completed_at": datetime(2026, 10, 27, tzinfo=UTC),
        "targets": [receipt],
    }])

    assert preview["repeat_notice"]["state"] == "None"
    assert preview["rows"][0]["values"]["100"].get("action") != "NoOp"
    assert any(issue["severity"] == "Error" and issue["code"] == "IMPORT_DRIVER_UNRESOLVED"
               for issue in preview["issues"])


def test_repeat_receipt_does_not_bypass_invalid_date_evidence():
    source = _source()
    baseline = build_batch_preview([source], _context())
    (receipt,) = baseline["semantic_receipts"]
    changed = deepcopy(source)
    changed["materialized"]["rows"][0]["cells"]["2"]["value"] = "not a date"

    preview = build_batch_preview([changed], _context(), completed_imports=[{
        "import_id": 77, "completed_by_user_id": 12, "completed_at": datetime(2026, 10, 27, tzinfo=UTC),
        "targets": [receipt],
    }])

    assert preview["repeat_notice"]["state"] == "None"
    assert any(issue["code"] == "IMPORT_WORK_DATE_INVALID" for issue in preview["issues"])
    assert preview["rows"][0]["values"]["100"].get("action") != "NoOp"


def test_repeat_receipt_preserves_incomplete_sum_without_partial_candidate_or_deletion():
    source = _sum_source(first="2", second="")
    baseline = build_batch_preview([source], _context())
    (receipt,) = baseline["semantic_receipts"]
    edited = _context(current_lines={(71, WORK_DATE, 100): Decimal("99")})
    preview = build_batch_preview([source], edited, completed_imports=[{
        "import_id": 77, "completed_by_user_id": 12, "completed_at": datetime(2026, 10, 27, tzinfo=UTC),
        "targets": [receipt],
    }])

    value = preview["rows"][0]["values"]["100"]
    assert preview["repeat_notice"]["state"] == "Complete"
    assert value["outcome"] == "AlreadyImported" and value["action"] == "NoOp"
    assert value["quantity"] is None and value["is_blank"] is True
    assert value["current_quantity"] == "99"
    assert value["is_candidate"] is False
    assert any(issue["code"] == "IMPORT_SUM_INPUT_MISSING" for issue in preview["issues"])


def test_repeat_receipt_does_not_bypass_unknown_category_choice():
    source = _source(quantity_value="4")
    source["mapping"]["4"] = "Filter"
    source["materialized"]["inspection"]["header_cells"].append("Category")
    source["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "Include", "kind": "text", "formula": False, "missing_cache": False}
    source["quantity_rules"][0]["row_handling"] = {"version": 1, "mode": "FilterThenSum", "choices": [
        {"category": {"type": "text", "value": "Exclude"}, "decision": "Exclude"},
        {"category": {"type": "text", "value": "Include"}, "decision": "Include"},
    ]}
    source["quantity_rules"][0]["shared_columns"]["Filter"] = {"header": "Category"}
    baseline = build_batch_preview([source], _context())
    (receipt,) = baseline["semantic_receipts"]
    changed = deepcopy(source)
    changed["materialized"]["rows"][0]["cells"]["4"]["value"] = "New category"

    preview = build_batch_preview([changed], _context(), completed_imports=[{
        "import_id": 77, "completed_by_user_id": 12, "completed_at": datetime(2026, 10, 27, tzinfo=UTC),
        "targets": [receipt],
    }])

    assert preview["repeat_notice"]["state"] == "None"
    assert any(issue["code"] == "IMPORT_CATEGORY_UNRESOLVED" for issue in preview["issues"])
    assert preview["rows"][0]["values"]["100"].get("action") != "NoOp"


def test_live_name_discrepancy_changes_review_plan_but_not_mapped_source_content():
    source = _source(driver_name="Driver One")
    source["materialized"]["inspection"]["header_cells"].append("DriverName")
    source["materialized"]["nonempty_columns"].append(4)
    exact = build_batch_preview([source], _context())

    renamed_context = _context()
    renamed_driver = renamed_context["drivers_by_id"][7]
    renamed_driver["drivername"] = "Driver One New"
    renamed_context["identity_indexes"]["DriverName"] = {"driver one new": {7}}
    renamed_context["name_match_summaries"] = {"driver one new": {
        "count": 1, "driver_id": 7, "driver": renamed_driver, "samples": [renamed_driver],
        "membership_sha256": hashlib.sha256((7).to_bytes(8, "big")).hexdigest(),
    }}
    discrepancy = build_batch_preview([source], renamed_context)

    assert any(issue["code"] == "IMPORT_DRIVER_NAME_DISCREPANCY" for issue in discrepancy["issues"])
    assert discrepancy["content_fingerprint"] == exact["content_fingerprint"]
    assert discrepancy["plan_fingerprint"] != exact["plan_fingerprint"]


def test_sum_rows_repeat_after_source_removal_keeps_one_combined_candidate():
    source = _source(quantity_value="2")
    source["quantity_rules"][0]["row_handling"] = {"version": 1, "mode": "SumRows", "choices": []}
    second = deepcopy(source["materialized"]["rows"][0])
    second["source_row"] = 3
    second["cells"]["3"]["value"] = "3"
    source["materialized"]["rows"].append(second)
    original = build_batch_preview([source], _context())
    (receipt,) = original["semantic_receipts"]

    no_longer_eligible = _context(live=False)
    no_longer_eligible["roster"][0]["iseligibleforperiod"] = False
    no_longer_eligible["roster"][0]["eligibleworkdates"] = []
    repeat = build_batch_preview([source], no_longer_eligible, completed_imports=[{
        "import_id": 77, "completed_by_user_id": 12, "completed_at": datetime(2026, 10, 27, tzinfo=UTC),
        "targets": [receipt],
    }])

    assert repeat["repeat_notice"]["state"] == "Complete"
    first, contributor = [row["values"]["100"] for row in repeat["rows"]]
    assert first["outcome"] == contributor["outcome"] == "AlreadyImported"
    assert first["action"] == contributor["action"] == "NoOp"
    assert first["quantity"] == "5" and first["is_candidate"] is True
    assert contributor["quantity"] is None and contributor["is_candidate"] is False
    assert all(row["driver_assignment_id"] is None for row in repeat["rows"])

    changed_operands = deepcopy(source)
    changed_operands["materialized"]["rows"][0]["cells"]["3"]["value"] = "1"
    changed_operands["materialized"]["rows"][1]["cells"]["3"]["value"] = "4"
    different_multiset = build_batch_preview([changed_operands], _context(), completed_imports=[{
        "import_id": 77, "completed_by_user_id": 12, "completed_at": datetime(2026, 10, 27, tzinfo=UTC),
        "targets": [receipt],
    }])
    assert different_multiset["rows"][0]["values"]["100"]["quantity"] == "5"
    assert different_multiset["repeat_notice"]["state"] == "None"


def test_preview_semantic_identity_matches_version_one_golden_fixtures():
    """Protect existing public and receipt hashes while optimizing preview preparation."""
    direct = _source()
    partial_sum = _sum_source(first="2", second="")
    invalid_sum = _sum_source(first="", second="bad")

    filtered = _source(quantity_value="4")
    filtered["mapping"]["4"] = "Filter"
    filtered["materialized"]["inspection"]["header_cells"].append("Category")
    filtered["materialized"]["rows"][0]["cells"]["4"] = {
        "value": "Include", "kind": "text", "formula": False, "missing_cache": False,
    }
    filtered["quantity_rules"][0]["row_handling"] = {
        "version": 1, "mode": "FilterThenSum", "choices": [
            {"category": {"type": "text", "value": "Include"}, "decision": "Include"},
            {"category": {"type": "text", "value": "Ignore"}, "decision": "Exclude"},
        ],
    }
    filtered["quantity_rules"][0]["shared_columns"]["Filter"] = {"header": "Category"}

    repeated = _source(quantity_value="2")
    repeated["quantity_rules"][0]["row_handling"] = {
        "version": 1, "mode": "SumRows", "choices": [],
    }
    second = deepcopy(repeated["materialized"]["rows"][0])
    second["source_row"] = 3
    second["cells"]["3"]["value"] = "3"
    repeated["materialized"]["rows"].append(second)

    duplicate_first = _sum_source(first="2", second="3")
    duplicate_first["quantity_rules"][0]["row_handling"] = {
        "version": 1, "mode": "SumRows", "choices": [],
    }
    duplicate_first["quantity_rules"][0]["inputs"] = [
        {"header": "Hours", "occurrences": [{"occurrence_ordinal": 1}]},
        {"header": "Hours", "occurrences": [{"occurrence_ordinal": 2}]},
    ]
    duplicate_first["source_unit_id"] = 1
    duplicate_first["unit_name"] = "First"
    duplicate_first["materialized"]["inspection"]["header_cells"] = [
        "DriverNumber", "WorkDate", "Hours", "Hours",
    ]
    duplicate_first["materialized"]["inspection"]["header_occurrences"] = [
        {"source_column": 3, "occurrence_count": 2, "occurrence_ordinal": 1},
        {"source_column": 4, "occurrence_count": 2, "occurrence_ordinal": 2},
    ]
    duplicate_second = deepcopy(duplicate_first)
    duplicate_second["source_unit_id"] = 2
    duplicate_second["file_ordinal"] = 2
    duplicate_second["unit_name"] = "Second"
    duplicate_second["mapping"] = {"3": "WorkDate", "4": "DriverNumber"}
    duplicate_second["quantity_rules"][0]["columns"] = ["1", "2"]
    duplicate_second["materialized"]["inspection"]["header_cells"] = [
        "Hours", "Hours", "WorkDate", "DriverNumber",
    ]
    duplicate_second["materialized"]["inspection"]["header_occurrences"] = [
        {"source_column": 1, "occurrence_count": 2, "occurrence_ordinal": 1},
        {"source_column": 2, "occurrence_count": 2, "occurrence_ordinal": 2},
    ]
    duplicate_second["materialized"]["rows"][0]["cells"] = {
        "1": {"value": "5", "kind": "number", "formula": False, "missing_cache": False},
        "2": {"value": "6", "kind": "number", "formula": False, "missing_cache": False},
        "3": {"value": WORK_DATE.isoformat(), "kind": "text", "formula": False, "missing_cache": False},
        "4": {"value": "001", "kind": "text", "formula": False, "missing_cache": False},
    }
    duplicate_second["materialized"]["nonempty_columns"] = [1, 2, 3, 4]
    duplicate_second["date_source"] = {"kind": "filename"}
    duplicate_second["file_name"] = "2026-10-20.csv"

    cases = {
        "direct": ([direct], "1206cacbfbf544d787ff0b7b7c221c3302d0e990f2f4e8ca582e857c5371f14a",
                   "98cc698a8d2056d6221be373bd3704292daaa22908911e1c7b7861dd7dfd9bf0",
                   "e3ebada9eebdc672be94eee240faeff024d8e4739e03bd2d0da386abfc0b4f8c",
                   ["de93616d440dd9a3e0d3482da69b131f6a855efb5ff12d7211f44c8b34a7a8cb"],
                   "a71c9441080dfb2d749242e7d8e52650381cee813ad4e54aad33a4152e5176de",
                   "c80bb11b66bcbe35bee45c01718b773886f727d34a33f82e91870eb0874f7878",
                   "cf124c6b37e54af62cc974c79163d4939de6641c22f619bea9f5bf7c97f84dea", 1, 1),
        "partial_sum": ([partial_sum], "33fd6978330c8f41e91f6ad9376b04643847a1b7f1c65eaec0f70a35ebcfffdf",
                        "4d989d3d134307ce4f020dceaf16653c338d8308949908c7af6e9f6f0a52c308",
                        "558224cc7cd145ed2f7ef2fed9933d41a0cce1138803ffb7db9230ef151dde7a",
                        ["26551513bdb6b8e8d8cc026f8197a71e998b94dfdbe6b48f4517d8e3d81b270d"],
                        "acf914e07ac93b71fac5a6746c94fa7dcbad9c029c43562fce392414eed39c05",
                        "ae2889f6fb62ea38969a250a72de62f516d2f59cf4db9981969352b43d54ff4d",
                        "cf124c6b37e54af62cc974c79163d4939de6641c22f619bea9f5bf7c97f84dea", 1, 1),
        "invalid_sum": ([invalid_sum], "33fd6978330c8f41e91f6ad9376b04643847a1b7f1c65eaec0f70a35ebcfffdf",
                        "87825e2e9337f01335aa0d9e2a007c59e63cda1ee9e5c2e9ef763453e766690d",
                        "e8d6db201e4be37741c6f9a631c43a0acfe8eb14641750c418aa9eba604058d7",
                        ["3c5d35bf8b3fc901dddbe5c652c1fc2e670fc2a885ea7b802f961b4da88a4338"],
                        None, None, None, 0, None),
        "filtered": ([filtered], "f357b4f97ae7f19ba74882693d32620ee8df80d9134debfbbcfa0d2764a14e5c",
                      "2303395dcb9e155a7e1ba30d05a660985724f218658326b46619168c5df8df02",
                      "19e0035e40aa6d193f9ca33f8064914639e56e115bb742236c2421aac865db76",
                      ["a81e217db076b08ae58c59cb9140f902d5d81a66af8084dff9d1d6c667f056e9"],
                      "5e49ba0487afb259f53aee8384776865e63cee9778cb9f2462211c82028c08f9",
                      "2c84f4725f7860a8cfa95f7ecc6d5e112317b4a83247ba8c6a67e5ef9848c129",
                      "cf124c6b37e54af62cc974c79163d4939de6641c22f619bea9f5bf7c97f84dea", 1, 1),
        "repeated": ([repeated], "a1fe930a2f9c106cfba9783eeec250aab5cd6f0539a670c1b97398f8ab0be37c",
                      "4f4b21c749d84d9f000fcfd4f6f36bfedd4fab5e36472ba7ff5a62034fd470c6",
                      "434289ee178853a598e65f0670a3fb5fda8025739a6524c5fdbf21c942421467",
                      ["0fe49b5eb8421181ed000a827b02b11bfe57b9bd7aeef4e521ccfdbfad2f1e16",
                       "a9052639899403b3c61cd9b6ed08a4e87ab52d50a7186240f96d2ed8e724e1ab"],
                      "2561b34563573c4cd1e5197fac5ce409d51b9d179cf02d55eec7fcfc63c4dcc2",
                      "25e2fb516721922665e2f9651da25c1ab0dfbfeb35d77e01426ac6061bb508dc",
                      "cf124c6b37e54af62cc974c79163d4939de6641c22f619bea9f5bf7c97f84dea",
                      1, 2),
        "duplicate_mixed": ([duplicate_first, duplicate_second],
                             "073d603d1acfd6a5979fda40ffd954a8c9e5dbca1f4cc2dafe00bf6c6503d17a",
                             "40c4f55ad16bf07b29fcb42b4d2c07fc147c56208acbf1babcad2fd64bb9e76f",
                             "8c5a107aee254cffee24132eadf742dffe235dd0e0d2b9053fc40401f86cc6da",
                             ["e79f2662cb7f0a9eb3578def47b0673e1290e9d1f84bbfb4953e5a6ffa591938",
                              "4cf96cfc36de07c09c2a660387e7ad5d1d093092348f51234e5efad3412b4904"],
                             "9430ffa2488a77b7dbd20451fe7499fcf66cf72505d5ad3d15feb0f614970c19",
                             "6f73f3079cded05d9b428975bbb54bb4bf6d7a5de72842b6348137df0bab1bdd",
                             "cf124c6b37e54af62cc974c79163d4939de6641c22f619bea9f5bf7c97f84dea", 1, 2),
    }

    for case, (sources, mapping_fp, content_fp, plan_fp, row_fps, receipt_fp,
               rule_fp, binding_fp, receipt_count, record_count) in cases.items():
        preview = build_batch_preview(sources, _context())
        assert preview["mapping_fingerprint"] == mapping_fp, case
        assert preview["content_fingerprint"] == content_fp, case
        assert preview["plan_fingerprint"] == plan_fp, case
        assert [row["row_fingerprint"] for row in preview["rows"]] == row_fps, case
        assert preview["candidates"] == 1, case
        assert len(preview["semantic_receipts"]) == receipt_count, case
        if receipt_fp is not None:
            receipt = preview["semantic_receipts"][0]
            assert receipt["input_sha256"] == receipt_fp, case
            assert receipt["rule_meaning_sha256"] == rule_fp, case
            assert receipt["binding_sha256"] == binding_fp, case
            assert receipt["version"] == 1, case
            assert receipt["record_count"] == record_count, case
        if case == "duplicate_mixed":
            assert preview["rows"][0]["values"]["100"]["quantity"] == "16"
            assert preview["rows"][0]["values"]["100"]["source_columns"] == [3, 4]
            assert preview["rows"][1]["values"]["100"]["source_columns"] == [1, 2]


def test_preview_reuses_each_physical_operand_parse_and_prepares_rules_once(monkeypatch):
    from collections import Counter

    from app.imports import preview_model, semantic_identity

    parsed = []
    original_parse = preview_model.parse_sum_operand

    def count_parse(cell):
        parsed.append(cell.get("value"))
        return original_parse(cell)

    validated = []
    original_validate = semantic_identity._validated_rule_meaning

    def count_validate(value):
        validated.append(value)
        return original_validate(value)

    monkeypatch.setattr(preview_model, "parse_sum_operand", count_parse)
    monkeypatch.setattr(semantic_identity, "_validated_rule_meaning", count_validate)

    direct = build_batch_preview([_source()], _context())
    assert direct["rows"][0]["values"]["100"]["quantity"] == "4"
    assert parsed == ["4.0000"]

    parsed.clear()
    validated.clear()
    summed = _sum_source(first="0.5", second="0.5")
    summed["quantity_rules"][0]["row_handling"] = {
        "version": 1, "mode": "SumRows", "choices": [],
    }
    for index, (first, second) in enumerate((("1", "2"), ("3", "4")), start=1):
        row = deepcopy(summed["materialized"]["rows"][0])
        row["source_row"] = index + 2
        row["cells"]["3"]["value"] = first
        row["cells"]["4"]["value"] = second
        summed["materialized"]["rows"].append(row)
    second_unit = _sum_source(first="5", second="6")
    second_unit["source_unit_id"] = 2
    second_unit["file_ordinal"] = 2
    second_unit["unit_name"] = "Second CSV"
    second_unit["quantity_rules"][0]["row_handling"] = {
        "version": 1, "mode": "SumRows", "choices": [],
    }

    result = build_batch_preview([summed, second_unit], _context())

    assert result["rows"][0]["values"]["100"]["quantity"] == "22"
    assert Counter(parsed) == Counter({"0.5": 2, "1": 1, "2": 1, "3": 1, "4": 1, "5": 1, "6": 1})
    assert len(validated) == 1


def test_absent_direct_target_in_aggregate_modes_does_not_block_other_target():
    for mode in ("SumRows", "FilterThenSum"):
        source = _source(quantity_value="unused")
        source["quantity_rules"][0]["columns"] = [None]
        row_rule = {"version": 1, "mode": mode, "choices": []}
        source["materialized"]["inspection"]["header_cells"].append("Overtime")
        source["materialized"]["rows"][0]["cells"]["4"] = {
            "value": "2", "kind": "text", "formula": False, "missing_cache": False,
        }
        source["materialized"]["nonempty_columns"].append(4)
        if mode == "FilterThenSum":
            source["mapping"]["5"] = "Filter"
            source["materialized"]["inspection"]["header_cells"].append("Category")
            source["materialized"]["rows"][0]["cells"]["5"] = {
                "value": "Work", "kind": "text", "formula": False, "missing_cache": False,
            }
            row_rule["choices"] = [{"category": {"type": "text", "value": "Work"},
                                    "decision": "Include"}]
            source["quantity_rules"][0]["shared_columns"]["Filter"] = {"header": "Category"}
        source["quantity_rules"][0]["row_handling"] = deepcopy(row_rule)
        second_rule = {
            "target": "Definition:101", "transform": "Direct", "columns": ["4"],
            "inputs": [{"header": "Overtime"}],
            "shared_columns": {"DriverNumber": {"header": "DriverNumber"},
                               "WorkDate": {"header": "WorkDate"}},
            "row_handling": deepcopy(row_rule),
        }
        if mode == "FilterThenSum":
            second_rule["shared_columns"]["Filter"] = {"header": "Category"}
        source["quantity_rules"].append(second_rule)
        context = _context()
        overtime = _definition(101, "OVERTIME", "Overtime")
        context["definitions"].append(overtime)
        context["definition_by_id"][101] = overtime

        preview = build_batch_preview([source], context)

        assert set(preview["rows"][0]["values"]) == {"101"}
        assert preview["rows"][0]["values"]["101"]["quantity"] == "2"
        assert preview["candidates"] == 1
