import asyncio
import csv
import io
import json
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from app.imports.canonical import extract_canonical_file
from app.imports.date_interpretation import interpret_date
from app.imports.errors import ImportFailure
from app.imports.headers import combined_column_count
from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY
from app.product_policy import DEFAULT_PRODUCT_POLICY
from scripts.benchmark_import_files import (
    ACCEPTANCE_SCENARIOS,
    BASE_CURRENT_SCENARIOS,
    CAPABILITY_SCENARIOS,
    CATEGORY_COUNT,
    CURRENT_DIMENSIONS,
    CURRENT_SCENARIOS,
    DATE_AUTHORITIES,
    IDENTITY_FINDINGS,
    MISSING_COVERAGE_DAY_INDEX,
    REPEATS,
    Phases,
    canonical_boundary_payloads,
    category_excluded,
    category_label,
    check_date_review,
    filename_date_selection,
    identity_kind,
    mapping_expected,
    mapping_requests,
    renamed_repeat_files,
    repeated_category,
    repeated_expected,
    repeated_totals,
    run_scenarios,
    scenario_coverage,
    scenario_covered_days,
    scenario_dimensions,
    source_files,
    target_mapping_plan,
    written_date,
)


def _fixture():
    start = date(2026, 1, 1)
    return {
        "definitions": [SimpleNamespace(code=f"INPUT_{index}") for index in range(14)],
        "drivers": [{"drivernumber": f"D{index:03d}", "fullname": f"Measured Driver {index}"}
                    for index in range(150)],
        "days": [start + timedelta(days=index) for index in range(31)],
    }


def _scenario_fixture(scenario):
    drivers, definitions, days, _outside = CURRENT_DIMENSIONS[scenario]
    start = date(2026, 1, 1)
    return {
        "definitions": [SimpleNamespace(code=f"Q{index + 1}") for index in range(definitions)],
        "drivers": [{"drivernumber": f"D{index:03d}", "fullname": f"Measured Driver {index}"}
                    for index in range(drivers)],
        "days": [start + timedelta(days=index) for index in range(days)],
    }


def _csv_rows(payload):
    return list(csv.reader(io.StringIO(payload.decode("utf-8"))))


def _xlsx_rows(payload):
    workbook = load_workbook(BytesIO(payload), read_only=True, data_only=True)
    try:
        assert len(workbook.sheetnames) == 1
        return workbook.sheetnames[0], [list(row) for row in workbook.active.iter_rows(values_only=True)]
    finally:
        workbook.close()


def test_current_daily_csv_shapes_use_repeated_headers_and_bounded_daily_files():
    fixture = _fixture()
    for scenario in ("current_daily_csv", "current_daily_issue_csv"):
        files = source_files(scenario, fixture)
        assert len(files) == 31
        for name, payload in files:
            assert name.endswith(".csv")
            rows = payload.decode("utf-8").splitlines()
            assert rows[0].startswith("DriverNumber,INPUT_0,INPUT_1")
            assert len(rows) == 151
            assert len(payload) < 1_000_000
        if scenario == "current_daily_issue_csv":
            assert ",invalid" in files[0][1].decode("utf-8")


def test_current_daily_xlsx_shapes_have_one_sheet_and_the_same_headers():
    files = source_files("current_daily_xlsx", _fixture())
    assert len(files) == 31
    for name, payload in files:
        assert name.endswith(".xlsx")
        workbook = load_workbook(BytesIO(payload), read_only=True, data_only=True)
        try:
            assert len(workbook.sheetnames) == 1
            sheet = workbook[workbook.sheetnames[0]]
            assert next(sheet.iter_rows(values_only=True)) == (
                "DriverNumber", *(f"INPUT_{index}" for index in range(14)))
        finally:
            workbook.close()


def test_current_generated_workloads_fit_default_import_policy():
    fixture = _fixture()
    assert DEFAULT_PRODUCT_POLICY.max_company_pay_items == 50
    assert DEFAULT_PRODUCT_POLICY.max_branch_input_concepts == 15
    assert DEFAULT_PRODUCT_POLICY.max_import_combined_columns == 40
    assert DEFAULT_PRODUCT_POLICY.max_import_canonical_file_bytes == 1_000_000
    assert DEFAULT_IMPORT_RESOURCE_POLICY.max_file_bytes == 10_000_000
    for scenario in BASE_CURRENT_SCENARIOS:
        files = source_files(scenario, fixture)
        assert len(files) == len(fixture["days"])
        for _, payload in files:
            assert len(payload) <= DEFAULT_IMPORT_RESOURCE_POLICY.max_file_bytes
            assert len(payload) <= DEFAULT_PRODUCT_POLICY.max_import_canonical_file_bytes
        if scenario == "current_daily_xlsx":
            workbook = load_workbook(BytesIO(files[0][1]), read_only=True, data_only=True)
            try:
                assert len(workbook.sheetnames) == 1
            finally:
                workbook.close()
            worksheets_per_file = 1
        else:
            worksheets_per_file = 0
        assert worksheets_per_file == (1 if scenario == "current_daily_xlsx" else 0)

    csv_headers = source_files("current_daily_csv", fixture)[0][1].decode("utf-8").splitlines()[0].split(",")
    xlsx = load_workbook(BytesIO(source_files("current_daily_xlsx", fixture)[0][1]), read_only=True)
    try:
        xlsx_headers = list(next(xlsx.active.iter_rows(values_only=True)))
    finally:
        xlsx.close()
    assert csv_headers == xlsx_headers
    assert len(csv_headers) == 15
    assert len(csv_headers) <= DEFAULT_PRODUCT_POLICY.max_import_combined_columns


def test_capability_workloads_fit_default_product_and_resource_policy():
    assert set(CAPABILITY_SCENARIOS) | set(ACCEPTANCE_SCENARIOS) == set(CURRENT_SCENARIOS) - set(BASE_CURRENT_SCENARIOS)
    for scenario in (*CAPABILITY_SCENARIOS, *ACCEPTANCE_SCENARIOS):
        fixture = _scenario_fixture(scenario)
        files = source_files(scenario, fixture)
        assert 0 < len(files) <= DEFAULT_IMPORT_RESOURCE_POLICY.max_files
        assert len({name for name, _ in files}) == len(files)
        assert len(fixture["definitions"]) <= DEFAULT_PRODUCT_POLICY.max_branch_input_concepts
        headers, data_rows = [], 0
        for name, payload in files:
            assert len(payload) <= DEFAULT_IMPORT_RESOURCE_POLICY.max_file_bytes
            rows = _csv_rows(payload) if name.endswith(".csv") else _xlsx_rows(payload)[1]
            headers.append(rows[0])
            data_rows += len(rows) - 1
            assert len(rows) - 1 <= DEFAULT_IMPORT_RESOURCE_POLICY.max_rows_per_unit
        if scenario == "current_partial_overlap_csv":
            assert len({tuple(header) for header in headers}) == 2
            assert set().union(*(set(header) for header in headers)) == {
                "DriverNumber", *(definition.code for definition in fixture["definitions"])}
        else:
            assert all(header == headers[0] for header in headers)
        assert combined_column_count(headers) <= DEFAULT_PRODUCT_POLICY.max_import_combined_columns
        assert data_rows <= DEFAULT_IMPORT_RESOURCE_POLICY.max_data_rows
        largest = max(files, key=lambda item: len(item[1]))
        canonical = extract_canonical_file(largest[1], largest[0])
        assert canonical["canonical_byte_count"] <= DEFAULT_PRODUCT_POLICY.max_import_canonical_file_bytes
        assert scenario_coverage(scenario)["semantic_repeat_recognition"] is (
            scenario == "current_completed_repeat_csv")
        assert scenario_coverage(scenario)["final_revised_contract_acceptance"] is False


def test_each_capability_workload_claims_only_the_capability_it_exercises():
    claimed = {scenario: {key for key, value in scenario_coverage(scenario).items()
                          if value is True and key in {
                              "per_target_mapping_direct_and_sum", "driver_identity_discovery",
                              "date_authority_inference_and_review", "repeated_rows_and_categories",
                              "completed_input_repeat_after_manual_edit", "partial_column_overlap",
                              "canonical_byte_boundary_admission"}}
               for scenario in CURRENT_SCENARIOS}
    assert claimed["current_target_mapping_csv"] == {"per_target_mapping_direct_and_sum"}
    assert claimed["current_identity_csv"] == {"driver_identity_discovery"}
    assert claimed["current_repeated_rows_csv"] == {"repeated_rows_and_categories"}
    for scenario in DATE_AUTHORITIES:
        assert claimed[scenario] == {"date_authority_inference_and_review"}
    for scenario in BASE_CURRENT_SCENARIOS:
        assert claimed[scenario] == set()
    assert claimed["current_completed_repeat_csv"] == {
        "completed_input_repeat_after_manual_edit"}
    assert claimed["current_partial_overlap_csv"] == {"partial_column_overlap"}
    assert claimed["current_canonical_byte_boundary_csv"] == {"canonical_byte_boundary_admission"}


def test_target_mapping_workload_mixes_direct_sum_duplicate_headers_and_blank_operands():
    fixture = _scenario_fixture("current_target_mapping_csv")
    plan = target_mapping_plan(fixture["definitions"])
    files = source_files("current_target_mapping_csv", fixture)
    assert len(files) == 31
    rows = _csv_rows(files[0][1])
    header = rows[0]
    assert header == plan["header"]
    assert plan["shared"] == ["DriverNumber"] and plan["ignored"] == ["Region", "Notes"]
    assert all(name in header for name in plan["ignored"])
    transforms = Counter(item["transform"] for item in plan["targets"])
    assert transforms["Direct"] > 0 and transforms["Sum"] == 3
    assert [item["code"] for item in plan["targets"]] == [definition.code for definition in fixture["definitions"]]
    sums = [item for item in plan["targets"] if item["transform"] == "Sum"]
    assert all(len(item["headers"]) >= 2 for item in sums)
    assert any(len(set(item["headers"])) < len(item["headers"]) for item in sums)
    duplicated = [item for item in plan["targets"] if len(set(item["headers"])) < len(item["headers"])]
    assert len(duplicated) == 1 and header.count(duplicated[0]["headers"][0]) == 2
    assert sum(len(item["headers"]) for item in plan["targets"]) + 3 == len(header)
    # The first Sum target occasionally leaves its last operand blank, so no complete quantity exists.
    blankable = next(item for item in plan["targets"] if item["blankable"])
    position = header.index(blankable["headers"][-1])
    blank_rows = [index for index, row in enumerate(rows[1:]) if row[position] == ""]
    assert blank_rows and len(blank_rows) < len(rows[1:]) // 10
    assert all(mapping_expected(blankable, index) is None for index in blank_rows)
    assert mapping_expected(blankable, 1) == 3
    assert mapping_expected(duplicated[0], 1) == Decimal("0.75")
    assert all(len(row) == len(header) for row in rows)


def test_identity_workload_carries_names_and_a_minority_of_discrepant_rows():
    fixture = _scenario_fixture("current_identity_csv")
    files = source_files("current_identity_csv", fixture)
    assert len(files) == 31
    rows = _csv_rows(files[0][1])
    assert rows[0][:2] == ["DriverNumber", "DriverName"]
    known = {driver["drivernumber"]: driver["fullname"] for driver in fixture["drivers"]}
    kinds = Counter()
    for index, (number, name, *quantities) in enumerate(rows[1:]):
        kind = identity_kind(index)
        kinds[kind] += 1
        assert all(quantities)
        if kind == "exact":
            assert known[number] == name
        elif kind == "name_variation":
            assert number in known and name.startswith(known[number]) and name != known[number]
        elif kind == "unknown_code":
            assert number not in known and name not in known.values()
        else:
            assert number == "" and name in known.values()
    assert set(kinds) == {"exact", *IDENTITY_FINDINGS}
    assert 0 < sum(kinds[kind] for kind in IDENTITY_FINDINGS) < len(rows) // 10
    assert CURRENT_DIMENSIONS["current_identity_csv"][3] >= 10 * len(fixture["drivers"])


def test_date_column_workload_has_many_dates_per_file_and_one_missing_day():
    fixture = _scenario_fixture("current_date_column_csv")
    files = source_files("current_date_column_csv", fixture)
    covered = scenario_covered_days("current_date_column_csv", fixture["days"])
    missing = fixture["days"][MISSING_COVERAGE_DAY_INDEX]
    assert len(covered) == len(fixture["days"]) - 1 and missing not in covered
    seen = []
    for position, (_name, payload) in enumerate(files, 1):
        rows = _csv_rows(payload)
        assert rows[0][:2] == ["DriverNumber", "WorkDate"]
        dates = []
        for row in rows[1:]:
            if row[1] not in dates:
                dates.append(row[1])
        assert len(dates) > 1
        if position == len(files):
            # Written dates have no year; the frozen period supplies it.
            results = [interpret_date({"value": text, "kind": "text", "formula": False, "missing_cache": False},
                                      period_days=fixture["days"]) for text in dates]
            assert all(item["reason"] == "period_year_inferred" for item in results)
            seen.extend(date.fromisoformat(item["resolved_date"]) for item in results)
        else:
            seen.extend(date.fromisoformat(text) for text in dates)
    assert seen == covered


def test_filename_and_sheet_workloads_resolve_written_dates_to_period_days():
    for scenario, authority in (("current_filename_dates_csv", "filename"), ("current_sheet_dates_xlsx", "sheet")):
        fixture = _scenario_fixture(scenario)
        files = source_files(scenario, fixture)
        covered = scenario_covered_days(scenario, fixture["days"])
        assert DATE_AUTHORITIES[scenario] == authority and len(files) == len(covered) == 30
        assert fixture["days"][MISSING_COVERAGE_DAY_INDEX] not in covered
        resolved = []
        for (name, payload), day in zip(files, covered, strict=True):
            if authority == "filename":
                label, rows = name.removesuffix(".csv"), _csv_rows(payload)
                assert name.endswith(".csv") and label == written_date(day)
            else:
                label, rows = _xlsx_rows(payload)
                assert name.endswith(".xlsx") and label == written_date(day)
                assert label not in name
            assert rows[0][0] == "DriverNumber" and "WorkDate" not in rows[0]
            assert len(rows) == len(fixture["drivers"]) + 1
            result = interpret_date(label, period_days=fixture["days"], source_kind=authority)
            assert result["reason"] == "period_year_inferred" and result["explicit_year"] is False
            resolved.append(date.fromisoformat(result["resolved_date"]))
        assert resolved == covered


def test_repeated_row_workload_has_repeated_driver_days_and_more_categories_than_one_page():
    fixture = _scenario_fixture("current_repeated_rows_csv")
    files = source_files("current_repeated_rows_csv", fixture)
    assert len(files) == len(fixture["days"])
    rows = _csv_rows(files[0][1])
    assert rows[0] == ["DriverNumber", "Category", "Q1", "Q2", "Q3"]
    assert len(rows) - 1 == len(fixture["drivers"]) * REPEATS
    assert Counter(row[0] for row in rows[1:]) == Counter({driver["drivernumber"]: REPEATS
                                                           for driver in fixture["drivers"]})
    assert [row[1] for row in rows[1:1 + REPEATS]] == [
        category_label(repeated_category(0, 0, repeat)) for repeat in range(REPEATS)]
    categories = {row[1] for _name, payload in files for row in _csv_rows(payload)[1:]}
    assert len(categories) == CATEGORY_COUNT > DEFAULT_IMPORT_RESOURCE_POLICY.category_page_default
    assert CATEGORY_COUNT <= 2 * DEFAULT_IMPORT_RESOURCE_POLICY.category_page_max
    excluded = [index for index in range(CATEGORY_COUNT) if category_excluded(index)]
    assert 0 < len(excluded) < CATEGORY_COUNT
    totals = repeated_totals(len(fixture["drivers"]), len(fixture["days"]))
    assert totals["rows"] == sum(len(_csv_rows(payload)) - 1 for _name, payload in files)
    assert 0 < totals["excluded_rows"] < totals["rows"] and totals["visible_groups"] <= totals["groups"]
    # Summing every row and filtering excluded categories give different, known quantities.
    assert repeated_expected(0, 0, 3, filtered=False) == [6, 6, 3]
    assert repeated_expected(0, 0, 3, filtered=True) == [6, 6, 3]
    assert repeated_expected(0, 1, 3, filtered=True) == [5, 4, 2]


def test_partial_overlap_workload_alternates_two_compatible_daily_layouts():
    fixture = _scenario_fixture("current_partial_overlap_csv")
    files = source_files("current_partial_overlap_csv", fixture)
    assert len(files) == len(fixture["days"]) == 31
    headers = []
    for (name, payload), day in zip(files, fixture["days"], strict=True):
        rows = _csv_rows(payload)
        assert name == f"{day.isoformat()}.csv"
        assert len(rows) == len(fixture["drivers"]) + 1
        headers.append(tuple(rows[0]))
    assert len(set(headers)) == 2
    assert set(headers[0]) == {"DriverNumber", "Q1", "Q2", "Q3"}
    assert set(headers[1]) == {"DriverNumber", "Q1", "Q4", "Q5", "Q6"}
    assert {"DriverNumber", *(definition.code for definition in fixture["definitions"])} == set().union(
        *(set(header) for header in headers))
    assert scenario_coverage("current_partial_overlap_csv")["partial_column_overlap"] is True


def test_completed_repeat_workload_uses_same_input_with_written_date_filenames():
    fixture = _scenario_fixture("current_completed_repeat_csv")
    initial = source_files("current_completed_repeat_csv", fixture)
    repeat = renamed_repeat_files(initial, fixture["days"])
    assert len(initial) == len(repeat) == len(fixture["days"]) == 31
    for (initial_name, initial_payload), (repeat_name, repeat_payload), day in zip(
            initial, repeat, fixture["days"], strict=True):
        assert initial_name == f"{day.isoformat()}.csv"
        assert repeat_name == f"{written_date(day)}.csv"
        assert initial_payload == repeat_payload
    assert {len(_csv_rows(payload)[0]) for _name, payload in initial} == {15}
    assert scenario_coverage("current_completed_repeat_csv")["semantic_repeat_recognition"] is True
    assert scenario_coverage("current_completed_repeat_csv")["final_revised_contract_acceptance"] is False


def test_written_month_repeat_selects_filename_authority_without_legacy_candidates():
    # A08 interpretation handles written-month names; inspection's old candidate
    # list is not the authority and can legitimately be empty for these labels.
    assert filename_date_selection({"inspection": {"filename_date_candidates": []}}) == {"kind": "filename"}


def test_canonical_boundary_mapping_explicitly_ignores_padded_notes_column():
    shared, ignored, targets = mapping_requests(
        "current_canonical_byte_boundary_csv", {"layouts": []},
        [["DriverNumber", "Q1", "Notes"]], {"Q1": 101}, [SimpleNamespace(code="Q1")])
    assert shared == {"DriverNumber": {"header": "DriverNumber"}}
    assert ignored == [{"header": "Notes"}]
    assert targets == [{"target": "Definition:101", "transform": "Direct",
                        "inputs": [{"header": "Q1"}], "expected_head_revision": 0}]


def test_canonical_boundary_workload_constructs_exact_and_one_byte_over_files():
    assert scenario_dimensions("current_canonical_byte_boundary_csv") == (150, 1, 31, 0)
    fixture = _scenario_fixture("current_canonical_byte_boundary_csv")
    assert len(fixture["days"]) == 31
    covered = scenario_covered_days("current_canonical_byte_boundary_csv", fixture["days"])
    assert covered == fixture["days"][:1]
    files = source_files("current_canonical_byte_boundary_csv", fixture)
    assert len(files) == 1 and files[0][0] == f"{covered[0].isoformat()}.csv"

    exact, over, evidence = canonical_boundary_payloads(fixture)
    assert files[0][1] == exact
    accepted = extract_canonical_file(exact, "2026-01-01.csv")
    assert accepted["canonical_byte_count"] == DEFAULT_PRODUCT_POLICY.max_import_canonical_file_bytes
    assert evidence["accepted_canonical_bytes"] == accepted["canonical_byte_count"]
    assert evidence["rejected_canonical_bytes"] == evidence["limit_bytes"] + 1
    assert len(exact) <= DEFAULT_IMPORT_RESOURCE_POLICY.max_file_bytes
    assert len(over) <= DEFAULT_IMPORT_RESOURCE_POLICY.max_file_bytes
    with pytest.raises(ImportFailure) as exc:
        extract_canonical_file(over, "2026-01-01.csv")
    assert exc.value.code == "IMPORT_CANONICAL_FILE_TOO_LARGE"
    assert scenario_coverage("current_canonical_byte_boundary_csv")["canonical_byte_boundary_admission"] is True

    days = [day.isoformat() for day in fixture["days"]]
    review = {
        "recognized_day_count": 1,
        "recognized_days": days[:1],
        "recognized_days_truncated": False,
        "missing_coverage_count": 30,
        "missing_coverage_days": days[1:],
        "missing_coverage_days_truncated": False,
        "unresolved_row_count": 0,
        "overlap_unit_day_count": 0,
        "overlap_unit_days": [],
        "overlap_unit_days_truncated": False,
        "units": [{"recognized_day_count": 1, "unresolved_row_count": 0}],
    }
    coverage = check_date_review("current_canonical_byte_boundary_csv", fixture, review, 1, 512)
    assert coverage["period_days"] == 31
    assert coverage["supplied_days"] == 1
    assert coverage["recognized_day_count"] == 1
    assert coverage["missing_coverage_count"] == 30


def test_async_benchmark_phase_timer_includes_awaited_snapshot_work():
    started = asyncio.Event()
    release = asyncio.Event()

    async def snapshot():
        started.set()
        await release.wait()
        return []

    fake_module = SimpleNamespace(_completed_receipt_snapshot=snapshot)
    phases = Phases()
    phases._wrap_async(fake_module, "_completed_receipt_snapshot", "completed_receipt_snapshot")

    async def invoke():
        task = asyncio.create_task(fake_module._completed_receipt_snapshot())
        await started.wait()
        await asyncio.sleep(0.02)
        release.set()
        await task

    asyncio.run(invoke())
    assert phases.calls["completed_receipt_snapshot"] == 1
    assert phases.values["completed_receipt_snapshot"] >= 0.015
    phases.restore()


def test_scenario_collection_reports_flushed_progress_and_keeps_json_artifacts(
    tmp_path, monkeypatch, capsys,
):
    calls = []

    def fake_run(command, *, check, env):
        assert check is True and isinstance(env, dict)
        scene = command[command.index("--scenario") + 1]
        destination = command[command.index("--output") + 1]
        calls.append(scene)
        Path(destination).write_text(json.dumps({"scenario": scene}), encoding="utf-8")

    monkeypatch.setattr("scripts.benchmark_import_files.subprocess.run", fake_run)
    output = tmp_path / "current.json"
    run_scenarios(("shape-one", "shape-two"), output)

    assert calls == ["shape-one", "shape-two"]
    assert capsys.readouterr().out.splitlines() == [
        "Starting shape-one (1/2)", "Completed shape-one (1/2)",
        "Starting shape-two (2/2)", "Completed shape-two (2/2)",
    ]
    assert json.loads(output.read_text(encoding="utf-8")) == [
        {"scenario": "shape-one"}, {"scenario": "shape-two"}]
