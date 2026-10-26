from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.imports.errors import ImportFailure
from app.imports.mapping import (
    fingerprint,
    infer_date_order,
    parse_date,
    quantity,
    suggest_mapping,
    target_key,
    targets,
    validate_mapping,
)
from app.imports.preview_model import build_preview
from app.payroll.definition_calculation import PeriodDefinition


def _definition(*, definition_id: int = 17, input_type: str = "Number", active: bool = True,
                method: str = "PerUnit", version: int = 1, code: str = "REG",
                name: str = "Regular") -> PeriodDefinition:
    return PeriodDefinition(
        payroll_period_definition_id=definition_id,
        pay_definition_id=definition_id + 100,
        rate_definition_id=definition_id + 200,
        code=code,
        name=name,
        input_type=input_type,
        unit="hours",
        calculation_method=method,
        calculation_method_version=version,
        rate_shape="Single",
        is_active=active,
    )


def _cell(value, kind: str = "text", *, missing_cache: bool = False, formula: str | None = None):
    return {"value": value, "kind": kind, "missing_cache": missing_cache, "formula": formula}


def _preview_row(source_row: int, *, number="0012", name="Ada Driver", employee="E-12",
                 work_date="2026-10-20", quantity_value="4", quantity_kind="number"):
    return {
        "source_row": source_row,
        "cells": {
            "1": _cell(number),
            "2": _cell(employee),
            "3": _cell(name),
            "4": _cell(work_date, "text"),
            "5": _cell(quantity_value, quantity_kind),
        },
    }


def _context(definitions=None, *, days=None, roster=None, lines=None, entries=None, live_days=None):
    result = {
        "definitions": definitions or [_definition()],
        "period": {"id": 9, "company_id": 3, "branch_id": 5, "company_code": "CO", "branch_code": "BR",
                   "period_code": "BR-20261001", "period_type": "Monthly",
                   "start": date(2026, 10, 1), "end": date(2026, 10, 31)},
        "days": days or [{"workdate": date(2026, 10, 20), "isconfiguredoffday": False}],
        "roster": roster if roster is not None else [{
            "driverassignmentid": 31,
            "driverid": 21,
            "drivernumber": "0012",
            "employeekey": "E-12",
            "drivername": "Ada Driver",
            "iseligibleforperiod": True,
            "eligibilityreasoncode": "EligibleByWindow",
            "eligibleworkdates": [date(2026, 10, 20)],
        }],
        "live_days": live_days if live_days is not None else {(31, date(2026, 10, 20))},
        "lines": lines or [],
        "entries": [{"statuscode": "SICK", **item} for item in entries or []],
    }
    result["roster"] = [{"effectivefromat": datetime(2026, 1, 1, tzinfo=UTC), "effectivetoat": None,
                         "endreason": None, "branchtimezoneidsnapshot": "UTC", **item} for item in result["roster"]]
    result["drivers"] = list({item["driverid"]: item for item in [{"driverid": 21, "drivernumber": "0012",
                              "employeekey": "E-12", "drivername": "Ada Driver"}, *result["roster"]]}.values())
    return result


def _materialized(*rows, nonempty=(1, 2, 3, 4, 5), headers=None):
    return {
        "inspection": {
            "selected_sheet": "CSV",
            "header_row_number": 1,
            "encoding": "utf-8",
            "delimiter": ",",
            "header_cells": headers or [],
        },
        "nonempty_columns": list(nonempty),
        "rows": list(rows),
    }


def _mapping(definition=None):
    definition = definition or _definition()
    return {"1": "DriverNumber", "2": "EmployeeKey", "3": "DriverName",
            "4": "WorkDate", "5": target_key(definition)}


def test_suggestions_use_only_trimmed_casefolded_exact_labels_and_template_headers():
    definition = _definition()
    assert suggest_mapping([" driver NUMBER ", "employee key", "DRIVERname", "Date", "hours", "REGULARS"],
                          [definition]) == {
        "1": "DriverNumber", "2": "EmployeeKey", "3": "DriverName", "4": "WorkDate"}
    for header in ("REG", "regular", "Pay Item: REG"):
        assert suggest_mapping([header], [definition]) == {"1": target_key(definition)}


def test_repeated_or_cross_target_ambiguous_headers_get_no_suggestions():
    first = _definition(definition_id=17, code="REG", name="Regular")
    colliding = _definition(definition_id=18, code="OT", name="REG")

    assert suggest_mapping(["Driver number", "driver NUMBER", "REG"], [first, colliding]) == {}


def test_mapping_validation_accepts_manual_targets_and_explicit_ignore():
    definition = _definition()
    mapping = {"1": "DriverNumber", "2": "WorkDate", "3": target_key(definition), "4": "Ignore"}

    assert validate_mapping(mapping, [1, 2, 3, 4], [definition]) == mapping


@pytest.mark.parametrize(
    ("mapping", "columns", "code"),
    [
        ({"1": "DriverNumber", "2": "WorkDate"}, [1, 2, 3], "IMPORT_MAPPING_INCOMPLETE"),
        ({"1": "DriverNumber", "2": "WorkDate", "3": "Ignore"}, [1, 2, 3],
         "IMPORT_MAPPING_REQUIRED_TARGET"),
        ({"1": "DriverNumber", "2": "DriverNumber", "3": "WorkDate", "4": target_key(_definition())},
         [1, 2, 3, 4], "IMPORT_MAPPING_DUPLICATE_TARGET"),
        ({"1": "DriverNumber", "2": "WorkDate", "3": "Definition:999"}, [1, 2, 3],
         "IMPORT_MAPPING_INVALID"),
        ({"0": "DriverNumber", "2": "WorkDate", "3": target_key(_definition())}, [2, 3],
         "IMPORT_MAPPING_INVALID"),
        ({"51": "Ignore", "1": "DriverNumber", "2": "WorkDate", "3": target_key(_definition())},
         [1, 2, 3], "IMPORT_MAPPING_INVALID"),
    ],
)
def test_mapping_validation_rejects_incomplete_duplicate_or_foreign_targets(mapping, columns, code):
    with pytest.raises(ImportFailure) as raised:
        validate_mapping(mapping, columns, [_definition()])
    assert raised.value.code == code


@pytest.mark.parametrize("definition", [_definition(active=False), _definition(method="Unknown"),
                                          _definition(method="PerUnit", version=2)])
def test_targets_exclude_inactive_or_nonoperational_frozen_definitions(definition):
    assert target_key(definition) not in {target["target"] for target in targets([definition])}
    with pytest.raises(ImportFailure) as raised:
        validate_mapping({"1": "DriverNumber", "2": "WorkDate", "3": target_key(definition)},
                         [1, 2, 3], [definition])
    assert raised.value.code == "IMPORT_MAPPING_INVALID"


def test_quantity_preserves_blank_zero_and_decimal_meaning_and_enforces_limits():
    definition = _definition()
    assert quantity(_cell(None, "blank"), definition) == (None, None)
    assert quantity(_cell("  ", "text"), definition) == (None, None)
    assert quantity(_cell("0.0000", "text"), definition) == (Decimal("0.0000"), None)
    assert quantity(_cell("1.2345", "number"), definition) == (Decimal("1.2345"), None)
    assert quantity(_cell("1.23456", "text"), definition)[1] == "At most four decimal places are supported."
    assert quantity(_cell("-1", "text"), definition)[1] == "Quantity must not be negative."
    assert quantity(_cell("NaN", "text"), definition)[1] is not None
    assert quantity(_cell("1.5", "text"), _definition(input_type="WholeNumber"))[1] == (
        "This item requires a whole number quantity.")
    assert quantity(_cell("2", "text"), _definition(input_type="WholeNumber")) == (Decimal("2"), None)


def test_formula_without_cached_value_is_not_treated_as_blank_or_zero():
    result, error = quantity(_cell(None, "blank", missing_cache=True, formula="=1+1"), _definition())
    assert result is None
    assert error == "Formula without a calculated value."


def test_date_interpretation_supports_period_fit_selection_ymd_and_native_dates():
    dmy = _cell("10/11/2026")
    assert infer_date_order([dmy], date(2026, 11, 1), date(2026, 11, 30), None) == ("DMY", "detected")
    mdy = _cell("11/10/2026")
    assert infer_date_order([mdy], date(2026, 11, 1), date(2026, 11, 30), None) == ("MDY", "detected")
    assert infer_date_order([dmy], date(2026, 1, 1), date(2026, 12, 31), None) == (None, "unresolved")
    assert infer_date_order([dmy], date(2026, 1, 1), date(2026, 12, 31), "DMY") == ("DMY", "selected")
    assert parse_date(_cell("2026/10/20"), "YMD") == date(2026, 10, 20)
    assert parse_date(_cell("2026-10-20"), None) == date(2026, 10, 20)
    assert parse_date(_cell("2026-10-20T08:30:00", "date"), None) == date(2026, 10, 20)
    assert parse_date(_cell("2026-10-20T08:30:00+02:00", "date"), None) is None
    assert parse_date(_cell(46200, "number"), "YMD") is None


def test_fingerprints_are_scale_stable_and_keep_blank_distinct_from_zero():
    assert fingerprint("row", {"quantity": Decimal("1.2300")}) == fingerprint(
        "row", {"quantity": Decimal("1.23")})
    assert fingerprint("row", {"blank": True, "quantity": None}) != fingerprint(
        "row", {"blank": False, "quantity": Decimal("0")})


def test_preview_hashes_track_semantic_rows_and_current_payroll_without_raw_or_ignored_values():
    definition = _definition()
    first_day = date(2026, 10, 20)
    second_day = date(2026, 10, 21)
    roster = [{**_context()["roster"][0], "eligibleworkdates": [first_day, second_day]}]
    context = _context(
        days=[{"workdate": first_day, "isconfiguredoffday": False},
              {"workdate": second_day, "isconfiguredoffday": False}],
        roster=roster,
        live_days={(31, first_day), (31, second_day)},
    )
    first = _preview_row(2, work_date=first_day.isoformat(), quantity_value="4")
    second = _preview_row(3, work_date=second_day.isoformat(), quantity_value="7")
    rows = _materialized(first, second)
    mapping = _mapping(definition)

    def hashes(preview):
        return (
            preview["mapping_fingerprint"], preview["content_fingerprint"],
            preview["plan_fingerprint"], tuple(sorted(row["fingerprint"] for row in preview["rows"])),
        )

    baseline = build_preview(rows, mapping, context)
    scaled_first = _preview_row(2, work_date=first_day.isoformat(), quantity_value="4.0000")
    scaled = build_preview(_materialized(scaled_first, second), mapping, context)
    assert hashes(scaled) == hashes(baseline)

    reordered = build_preview(_materialized(second, first), mapping, context)
    assert hashes(reordered)[:3] == hashes(baseline)[:3]

    current_changed = build_preview(
        rows, mapping, _context(
            days=context["days"], roster=roster, live_days=context["live_days"],
            lines=[{"driverassignmentid": 31, "workdate": first_day,
                    "payrollperioddefinitionid": definition.payroll_period_definition_id,
                    "quantity": Decimal("3")}],
        ),
    )
    assert current_changed["mapping_fingerprint"] == baseline["mapping_fingerprint"]
    assert current_changed["content_fingerprint"] == baseline["content_fingerprint"]
    assert current_changed["plan_fingerprint"] != baseline["plan_fingerprint"]
    assert [row["fingerprint"] for row in current_changed["rows"]] == [
        row["fingerprint"] for row in baseline["rows"]]

    unrelated_driver = {
        "driverassignmentid": 32,
        "driverid": 22,
        "drivernumber": "0013",
        "employeekey": "E-13",
        "drivername": "Bea Driver",
        "iseligibleforperiod": True,
        "eligibilityreasoncode": "EligibleByWindow",
        "eligibleworkdates": [first_day],
    }
    unrelated_context = _context(
        days=context["days"], roster=[*roster, unrelated_driver],
        live_days={(31, first_day), (31, second_day), (32, first_day)},
        lines=[{"driverassignmentid": 32, "workdate": first_day,
                "payrollperioddefinitionid": definition.payroll_period_definition_id,
                "quantity": Decimal("99")}],
    )
    unrelated_changed = build_preview(rows, mapping, unrelated_context)
    assert hashes(unrelated_changed) == hashes(baseline)

    blank_preview = build_preview(
        _materialized(_preview_row(2, work_date=first_day.isoformat(), quantity_value="")),
        mapping, context,
    )
    zero_preview = build_preview(
        _materialized(_preview_row(2, work_date=first_day.isoformat(), quantity_value="0")),
        mapping, context,
    )
    assert blank_preview["mapping_fingerprint"] == zero_preview["mapping_fingerprint"]
    assert blank_preview["rows"][0]["fingerprint"] != zero_preview["rows"][0]["fingerprint"]
    assert blank_preview["content_fingerprint"] != zero_preview["content_fingerprint"]

    ignored_mapping = {**mapping, "6": "Ignore"}
    ignored_a = _preview_row(2, work_date=first_day.isoformat())
    ignored_b = _preview_row(2, work_date=first_day.isoformat())
    ignored_a["cells"]["6"] = _cell("source note A")
    ignored_b["cells"]["6"] = _cell("source note B")
    ignored_a_hashes = hashes(build_preview(
        _materialized(ignored_a, nonempty=(1, 2, 3, 4, 5, 6)), ignored_mapping, context,
    ))
    ignored_b_hashes = hashes(build_preview(
        _materialized(ignored_b, nonempty=(1, 2, 3, 4, 5, 6)), ignored_mapping, context,
    ))
    assert ignored_a_hashes == ignored_b_hashes


def test_preview_resolves_exact_identity_and_only_proposes_name_match():
    definition = _definition()
    exact = build_preview(_materialized(_preview_row(2)), _mapping(definition), _context())
    row = exact["rows"][0]
    assert {(item["kind"], item["resolution"], item["driver_id"]) for item in row["identifiers"]} == {
        ("DriverNumber", "Exact", 21), ("EmployeeKey", "Exact", 21), ("DriverName", "Proposed", 21)}
    assert "IMPORT_DRIVER_CONFIRMATION_REQUIRED" not in {issue["code"] for issue in exact["issues"]}

    name_only_mapping = {"3": "DriverName", "4": "WorkDate", "5": target_key(definition)}
    name_only = build_preview(_materialized(_preview_row(2), nonempty=(3, 4, 5)),
                              name_only_mapping, _context())
    assert name_only["rows"][0]["identifiers"][0]["resolution"] == "Proposed"
    assert name_only["rows"][0]["work_cells"][0]["outcome"] == "Unresolved"
    assert "IMPORT_DRIVER_CONFIRMATION_REQUIRED" in {issue["code"] for issue in name_only["issues"]}


def test_preview_skips_blank_optional_identifier_but_requires_a_provided_identity():
    definition = _definition()
    mapping = _mapping(definition)
    row = _preview_row(2, employee=" ")
    preview = build_preview(_materialized(row), mapping, _context())

    identifiers = preview["rows"][0]["identifiers"]
    assert {(item["kind"], item["resolution"], item["driver_id"]) for item in identifiers} == {
        ("DriverNumber", "Exact", 21), ("DriverName", "Proposed", 21)}
    assert preview["rows"][0]["work_cells"][0]["outcome"] == "New"

    no_identity = build_preview(
        _materialized(_preview_row(2, number=" ", employee=" ", name=" ")),
        mapping,
        _context(),
    )
    assert "IMPORT_DRIVER_UNRESOLVED" in {issue["code"] for issue in no_identity["issues"]}
    assert no_identity["rows"][0]["identifiers"] == []
    assert no_identity["rows"][0]["work_cells"][0]["outcome"] == "Invalid"

    empty_roster = build_preview(
        _materialized(_preview_row(2)), mapping, _context(roster=[]),
    )
    assert "IMPORT_DRIVER_UNRESOLVED" in {issue["code"] for issue in empty_roster["issues"]}
    assert empty_roster["rows"][0]["work_cells"][0]["outcome"] == "Invalid"


def test_preview_reports_unknown_and_disagreeing_identifiers():
    definition = _definition()
    unknown = build_preview(
        _materialized(_preview_row(2, number="9999", employee="unknown", name="Nobody")),
        _mapping(definition), _context(),
    )
    assert {issue["code"] for issue in unknown["issues"]}.issuperset({"IMPORT_DRIVER_UNRESOLVED"})
    assert all(identifier["resolution"] == "Unresolved" for identifier in unknown["rows"][0]["identifiers"])

    second = {**_context()["roster"][0], "driverassignmentid": 32, "driverid": 22,
              "drivernumber": "0022", "employeekey": "E-22", "drivername": "Bea Driver",
              "eligibleworkdates": [date(2026, 10, 20)]}
    disagree = build_preview(
        _materialized(_preview_row(2, number="0012", employee="E-22")), _mapping(definition),
        _context(roster=[_context()["roster"][0], second], live_days={(31, date(2026, 10, 20)),
                                                                      (32, date(2026, 10, 20))}),
    )
    assert "IMPORT_DRIVER_DISAGREEMENT" in {issue["code"] for issue in disagree["issues"]}


def test_preview_marks_every_duplicate_fact_and_observes_current_value_diff_matrix():
    definition = _definition()
    duplicate = build_preview(_materialized(_preview_row(2), _preview_row(3)), _mapping(definition), _context())
    assert [row["work_cells"][0]["outcome"] for row in duplicate["rows"]] == ["Invalid", "Invalid"]
    duplicate_issues = [issue for issue in duplicate["issues"] if issue["code"] == "IMPORT_DUPLICATE_FACT"]
    assert [issue["source_row"] for issue in duplicate_issues] == [2, 3]

    days = [{"workdate": date(2026, 10, 20) + timedelta(days=index),
             "isconfiguredoffday": False} for index in range(4)]
    rows = [_preview_row(index + 2, work_date=(date(2026, 10, 20) + timedelta(days=index)).isoformat(),
                         quantity_value=value) for index, value in enumerate(("5", "5.000", "0", "3"))]
    existing = [{"driverassignmentid": 31, "workdate": date(2026, 10, 21),
                 "payrollperioddefinitionid": definition.payroll_period_definition_id, "quantity": Decimal("5")},
                {"driverassignmentid": 31, "workdate": date(2026, 10, 23),
                 "payrollperioddefinitionid": definition.payroll_period_definition_id, "quantity": Decimal("6")},
                {"driverassignmentid": 31, "workdate": date(2026, 10, 22),
                 "payrollperioddefinitionid": definition.payroll_period_definition_id, "quantity": Decimal("0")}]
    preview = build_preview(
        _materialized(*rows), _mapping(definition),
        _context(days=days, lines=[{**existing[0], "workdate": date(2026, 10, 21)}, *existing[1:]],
                 live_days={(31, day["workdate"]) for day in days}),
    )
    assert [row["work_cells"][0]["outcome"] for row in preview["rows"]] == [
        "New", "Unchanged", "Unchanged", "Conflict"]


def test_preview_surfaces_invalid_values_day_off_held_status_and_skipped_rows():
    definition = _definition()
    offday = date(2026, 10, 20)
    rows = [
        _preview_row(2, quantity_value="-1"),
        _preview_row(3, quantity_value="4", work_date=offday.isoformat()),
        _preview_row(4, quantity_value="4", work_date=offday.isoformat()),
        _preview_row(5, quantity_value="", quantity_kind="text"),
    ]
    context = _context(
        days=[{"workdate": offday, "isconfiguredoffday": True}],
        entries=[{"driverassignmentid": 31, "workdate": offday, "statuskeyid": 700, "isoffreason": True}],
    )
    preview = build_preview(_materialized(*rows), _mapping(definition), context)
    codes = [issue["code"] for issue in preview["issues"]]
    assert "IMPORT_QUANTITY_INVALID" in codes
    assert "IMPORT_DAY_OFF_DECISION_REQUIRED" in codes
    assert "IMPORT_STATUS_DECISION_REQUIRED" in codes
    assert preview["rows"][-1]["skipped"] is True
    assert preview["counts"]["skipped"] == 1
    assert preview["rows"][-1]["work_cells"][0]["outcome"] == "Blank"


@pytest.mark.parametrize("identifiers", [{"1": "DriverNumber"}, {"2": "EmployeeKey"},
                                        {"1": "DriverNumber", "2": "EmployeeKey"}])
def test_permanent_identity_remains_known_without_period_assignment(identifiers):
    mapping = {**identifiers, "4": "WorkDate", "5": target_key(_definition())}
    preview = build_preview(_materialized(_preview_row(2), nonempty=tuple(map(int, mapping))),
                            mapping, _context(roster=[], live_days=set()))
    assert {(item["resolution"], item["driver_id"]) for item in preview["rows"][0]["identifiers"]} == {("Exact", 21)}
    assert {item["code"] for item in preview["issues"]} == {"IMPORT_ASSIGNMENT_UNRESOLVED"}
    assert preview["rows"][0]["work_cells"][0]["outcome"] == "Invalid"


def _identities(preview):
    return (preview["mapping_fingerprint"], tuple(row["fingerprint"] for row in preview["rows"]),
            preview["content_fingerprint"], preview["plan_fingerprint"])


def test_unresolved_name_does_not_erase_agreed_permanent_stable_identity():
    preview = build_preview(_materialized(_preview_row(2)), _mapping(), _context(roster=[], live_days=set()))
    assert {(item["kind"], item["resolution"]) for item in preview["rows"][0]["identifiers"]} == {
        ("DriverNumber", "Exact"), ("EmployeeKey", "Exact"), ("DriverName", "Unresolved")}
    assert {item["code"] for item in preview["issues"]} == {"IMPORT_DRIVER_UNRESOLVED", "IMPORT_ASSIGNMENT_UNRESOLVED"}
    assert preview["rows"][0]["work_cells"][0]["outcome"] == "Invalid"


def test_semantic_fingerprints_ignore_all_surrogate_ids_and_audit_times():
    day = date(2026, 10, 20)
    context = _context(lines=[{"driverassignmentid": 31, "workdate": day,
                               "payrollperioddefinitionid": 17, "quantity": Decimal("3")}],
                       entries=[{"driverassignmentid": 31, "workdate": day, "statuskeyid": 700,
                                 "statuscode": "SICK", "isoffreason": True}])
    source = _materialized(_preview_row(2))
    original = build_preview(source, _mapping(), context)
    changed = deepcopy(context)
    changed["period"].update(id=900, company_id=300, branch_id=500, created_at=datetime.now(UTC))
    changed["definitions"] = [replace(context["definitions"][0], payroll_period_definition_id=1700,
                                      pay_definition_id=11700, rate_definition_id=21700)]
    for item in changed["drivers"]:
        item["driverid"] = 2100
    for item in changed["roster"]:
        item.update(driverid=2100, driverassignmentid=3100, updated_at=datetime.now(UTC))
    changed["live_days"] = {(3100, day)}
    changed["lines"][0].update(driverassignmentid=3100, payrollperioddefinitionid=1700)
    changed["entries"][0].update(driverassignmentid=3100, statuskeyid=70000)
    renumbered = build_preview(source, _mapping(changed["definitions"][0]), changed)
    assert _identities(renumbered) == _identities(original)
    assert renumbered["rows"][0]["work_cells"][0]["assignment_id"] == 3100


@pytest.mark.parametrize("domain", ["period", "definition", "assignment", "status"])
def test_business_meaning_changes_the_correct_fingerprint_domains(domain):
    context = _context(entries=[{"driverassignmentid": 31, "workdate": date(2026, 10, 20),
                                 "statuskeyid": 700, "statuscode": "SICK", "isoffreason": True}])
    source = _materialized(_preview_row(2))
    original = build_preview(source, _mapping(), context)
    changed = deepcopy(context)
    if domain == "period":
        changed["period"]["branch_code"] = "OTHER"
    elif domain == "definition":
        changed["definitions"] = [replace(context["definitions"][0], unit="deliveries")]
    elif domain == "assignment":
        changed["roster"][0]["effectivefromat"] -= timedelta(days=1)
    else:
        changed["entries"][0]["statuscode"] = "ABSENT"
    revised = build_preview(source, _mapping(), changed)
    assert revised["plan_fingerprint"] != original["plan_fingerprint"]
    if domain in ("assignment", "status"):
        assert _identities(revised)[:3] == _identities(original)[:3]
    else:
        assert all(left != right for left, right in zip(_identities(revised), _identities(original), strict=True))
