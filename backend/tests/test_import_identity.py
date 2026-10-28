from app.imports.batch_service import _stable_identifiers
from app.imports.files import FileCell
from app.imports.identity import (
    finish_name_match_state,
    fold_company_name_rows,
    new_name_match_state,
    resolve_row_identity,
)
from app.imports.mapping import normalized_identifier


def _driver(driver_id, number, key, name):
    return {"driverid": driver_id, "drivernumber": number, "employeekey": key,
            "drivername": name}


def _summary(*drivers):
    requested = {normalized_identifier("DriverName", driver["drivername"])
                 for driver in drivers}
    states = {name: new_name_match_state() for name in requested}
    fold_company_name_rows(states, [
        {"driverid": d["driverid"], "drivernumber": d["drivernumber"],
         "employeekey": d["employeekey"], "fullname": d["drivername"]}
        for d in drivers
    ])
    return finish_name_match_state(states)


def _provided(kind, value):
    return {"kind": kind, "value": normalized_identifier(kind, value),
            "source_value": value, "source_kind": "text", "column": 0,
            "missing_cache": False}


def _resolve(driver_list, values):
    summaries = _summary(*driver_list) if driver_list else {}
    by_id = {driver["driverid"]: driver for driver in driver_list}
    indexes = {"DriverNumber": {}, "EmployeeKey": {}}
    for driver in driver_list:
        indexes["DriverNumber"].setdefault(
            normalized_identifier("DriverNumber", driver["drivernumber"]), set()
        ).add(driver["driverid"])
        indexes["EmployeeKey"].setdefault(
            normalized_identifier("EmployeeKey", driver["employeekey"]), set()
        ).add(driver["driverid"])
    provided = []
    for item in values:
        value = _provided(*item[:2])
        if len(item) > 2:
            value["missing_cache"] = item[2]
        provided.append(value)
    return resolve_row_identity(provided, indexes,
                                summaries, by_id)


def test_company_name_matching_is_global_and_names_never_become_stable_matches():
    a = _driver(1, "001", "A", "Same Name")
    b = _driver(2, "002", "B", "Same Name")
    summary = _summary(a, b)["same name"]
    assert summary["count"] == 2
    assert summary["driver_id"] is None
    assert [item["driverid"] for item in summary["samples"]] == [1, 2]

    first = _resolve([a, b], [("DriverNumber", "001"), ("DriverName", "Same Name")])
    second = _resolve([a, b], [("DriverNumber", "002"), ("DriverName", "Same Name")])
    assert first["driver_id"] == 1 and first["status"] == "Exact"
    assert second["driver_id"] == 2 and second["status"] == "Exact"
    assert first["identifiers"][1]["driver_id"] is None
    assert second["identifiers"][1]["driver_id"] is None


def test_discrepancy_evidence_keeps_stable_code_candidate_before_name_samples():
    first = _driver(10, "010", "A", "Disputed Name")
    second = _driver(20, "020", "B", "Disputed Name")
    coded = _driver(30, "030", "C", "Canonical Name")

    result = _resolve([first, second, coded], [
        ("DriverNumber", "030"), ("DriverName", "Disputed Name")])

    assert result["driver_id"] is None
    assert result["review_candidate_ids"] == [30, 10]


def test_unique_name_is_proposal_and_known_code_name_variation_needs_confirmation():
    canonical = _driver(1, "001", "A", "Canonical Name")
    outsider = _driver(2, "002", "B", "Other Name")

    proposed = _resolve([canonical], [("DriverName", "Canonical Name")])
    assert (proposed["driver_id"], proposed["status"], proposed["requires_confirmation"]) == (
        1, "Proposed", True)

    variation = _resolve([canonical, outsider], [
        ("DriverNumber", "001"), ("DriverName", "Unlisted Variation")])
    assert variation["driver_id"] == 1
    assert variation["status"] == "NameDiscrepancy"
    assert variation["requires_confirmation"] is True
    assert variation["issue_severity"] == "Conflict"

    points_elsewhere = _resolve([canonical, outsider], [
        ("DriverNumber", "001"), ("DriverName", "Other Name")])
    assert points_elsewhere["driver_id"] is None
    assert points_elsewhere["issue_code"] == "IMPORT_DRIVER_DISAGREEMENT"


def test_provided_unknown_or_conflicting_stable_keys_block_fallback():
    canonical = _driver(1, "001", "A", "Canonical Name")
    other = _driver(2, "002", "B", "Other Name")

    unknown_code = _resolve([canonical], [
        ("DriverNumber", "999"), ("EmployeeKey", "A"), ("DriverName", "Canonical Name")])
    assert unknown_code["driver_id"] is None
    assert unknown_code["issue_code"] == "IMPORT_DRIVER_UNRESOLVED"

    conflict = _resolve([canonical, other], [
        ("DriverNumber", "001"), ("EmployeeKey", "B")])
    assert conflict["driver_id"] is None
    assert conflict["issue_code"] == "IMPORT_DRIVER_DISAGREEMENT"

    unknown_key = _resolve([canonical], [
        ("DriverNumber", "001"), ("EmployeeKey", "missing")])
    assert unknown_key["driver_id"] is None
    assert unknown_key["issue_code"] == "IMPORT_DRIVER_UNRESOLVED"

    unavailable_name = _resolve([canonical], [
        ("DriverNumber", "001"), ("DriverName", "Canonical Name", True)])
    assert unavailable_name["driver_id"] is None
    assert unavailable_name["issue_code"] == "IMPORT_DRIVER_UNRESOLVED"
    assert unavailable_name["issue_severity"] == "Error"


def test_numeric_driver_code_keeps_leading_zeroes_and_name_normalizer_authority():
    driver = _driver(1, "00127", "EMP-1", "Åda  Smith")
    resolved = _resolve([driver], [("DriverNumber", "00127")])
    assert resolved["driver_id"] == 1
    assert normalized_identifier("DriverName", "A\u030ada   Smith") == "åda smith"


def test_company_name_scan_refreshes_retained_drivers_even_when_name_was_not_requested():
    retained = {1: _driver(1, "001", "A", "Old Name")}
    states = {"requested name": new_name_match_state()}
    fold_company_name_rows(states, [{
        "driverid": 1, "drivernumber": "001", "employeekey": "A", "fullname": "Current Name",
    }], retained)

    assert retained[1]["drivername"] == "Current Name"
    assert finish_name_match_state(states)["requested name"]["count"] == 0


def test_name_lookup_inputs_skip_direct_blank_rows_and_unavailable_formula_values():
    source = {"mapping": {"1": "DriverName", "2": "WorkDate"},
        "quantity_rules": [{"transform": "Direct", "columns": ["3"]}],
        "materialized": {"rows": [
            {"cells": {"1": FileCell("", "text"), "2": FileCell(None, "text"),
                       "3": FileCell("", "text")}},
            {"cells": {"1": FileCell(None, "text", formula=True, missing_cache=True),
                       "2": FileCell("2026-10-20", "text"), "3": FileCell("1", "text")}},
        ]}}

    assert _stable_identifiers([source]) == {
        "DriverNumber": [], "EmployeeKey": [], "DriverName": [],
    }

    source["quantity_rules"] = [{"transform": "Sum", "columns": ["3", "4"]}]
    assert _stable_identifiers([source])["DriverName"] == []
    source["materialized"]["rows"][1]["cells"]["1"] = FileCell("  Requested Name ", "text")
    assert _stable_identifiers([source])["DriverName"] == ["requested name"]
