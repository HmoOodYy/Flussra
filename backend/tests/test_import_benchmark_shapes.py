from datetime import date, timedelta
from io import BytesIO
from types import SimpleNamespace

from openpyxl import load_workbook

from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY
from app.product_policy import DEFAULT_PRODUCT_POLICY
from scripts.benchmark_import_files import CURRENT_SCENARIOS, source_files


def _fixture():
    start = date(2026, 1, 1)
    return {
        "definitions": [SimpleNamespace(code=f"INPUT_{index}") for index in range(14)],
        "drivers": [{"drivernumber": f"D{index:03d}"} for index in range(150)],
        "days": [start + timedelta(days=index) for index in range(31)],
    }


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
    for scenario in CURRENT_SCENARIOS:
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
