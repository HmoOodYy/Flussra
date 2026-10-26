from __future__ import annotations

from datetime import UTC, date, datetime
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.imports.errors import ImportFailure
from app.imports.template import generate_template
from app.payroll.definition_calculation import PeriodDefinition


def _definition(code="REGULAR", name="Regular work", definition_id=11):
    return PeriodDefinition(
        payroll_period_definition_id=definition_id,
        pay_definition_id=definition_id + 100,
        rate_definition_id=definition_id + 200,
        code=code,
        name=name,
        input_type="Decimal",
        unit="hours",
        calculation_method="PerUnit",
        calculation_method_version=1,
        rate_shape="Scalar",
        is_active=True,
    )


def _context(definitions=None, *, driver_number="0012", employee_key="EMP-12", driver_name="Driver One"):
    work_date = date(2026, 10, 20)
    assignment = {
        "driverassignmentid": 71,
        "driverid": 7,
        "drivernumber": driver_number,
        "employeekey": employee_key,
        "drivername": driver_name,
        "effectivefromat": datetime(2020, 1, 1, tzinfo=UTC),
        "effectivetoat": None,
        "endreason": None,
        "branchtimezoneidsnapshot": "America/New_York",
        "iseligibleforperiod": True,
        "eligibilityreasoncode": "Active",
        "eligibleworkdates": [work_date],
    }
    return {
        "definitions": [_definition()] if definitions is None else definitions,
        "days": {work_date: False},
        "roster": [assignment],
        "live_days": {(71, work_date)},
    }


def test_generated_template_keeps_identity_values_literal_and_roundtrips():
    context = _context(driver_number="=1+1", employee_key="+0012", driver_name="@SUM(A1:A2)")

    payload = generate_template(context)

    workbook = load_workbook(BytesIO(payload), read_only=False, data_only=False)
    try:
        sheet = workbook["Payroll work"]
        assert [cell.value for cell in sheet[1]] == [
            "Driver number", "Employee key", "Driver name", "WorkDate", "Pay Item: REGULAR",
        ]
        for cell, expected in zip(sheet[2][:3], ["=1+1", "+0012", "@SUM(A1:A2)"]):
            assert cell.value == expected
            assert cell.data_type == "s"
            assert cell.number_format == "@"
        assert sheet[2][3].value == "2026-10-20"
    finally:
        workbook.close()


@pytest.mark.parametrize("definitions", [
    [_definition("foo", "First", 11), _definition("FOO", "Second", 12)],
    [_definition("REGULAR", "Driver number", 11)],
    [_definition("A", "Pay Item: B", 11), _definition("B", "Second", 12)],
])
def test_template_refuses_ambiguous_generated_headers(definitions):
    with pytest.raises(ImportFailure) as caught:
        generate_template(_context(definitions))

    assert caught.value.code == "IMPORT_TEMPLATE_HEADERS_AMBIGUOUS"
