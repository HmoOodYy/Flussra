"""Stage 1: branch payroll provenance is keyed by DriverAssignmentID.

Covers the identity contract that spans payroll modules: evidence writers carry both
ids, the snapshot hashes include them deterministically, and the profile-addressed
payroll and compensation routes no longer exist.
"""

from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.main import create_app
from app.payroll.audit_evidence import capture_period_audit_evidence
from app.payroll.snapshot_hash import (
    calculate_report_evidence_hash,
    calculate_snapshot_hash,
)


def _content(**overrides):
    total = {
        "DriverAssignmentID": 11,
        "DriverID": 5,
        "EmployeeID": 3,
        "BranchID": 1,
        "DriverNumberSnapshot": "D-5",
        "DriverNameSnapshot": "Alex",
        "DailyPay": Decimal("100.00"),
        "StatusPay": Decimal("0"),
        "MinimumAdjustment": Decimal("0"),
        "MaximumAdjustment": Decimal("0"),
        "BonusTotal": Decimal("0"),
        "ExpectedPay": Decimal("100.00"),
        "Components": [],
        "MinMax": {"Status": "NO_RULE", "Rules": []},
        "Lines": [],
    }
    total.update(overrides)
    return {
        "CompanyID": 1, "BranchID": 1, "PayrollPeriodID": 1,
        "CalculationVersion": "payroll-calculation-v2", "SourceConfigHash": "0" * 64,
        "DriverTotals": [total],
    }


def test_snapshot_hash_covers_assignment_driver_employee_and_number():
    base = calculate_snapshot_hash(_content())
    assert base == calculate_snapshot_hash(deepcopy(_content()))
    assert base != calculate_snapshot_hash(_content(DriverAssignmentID=12))
    assert base != calculate_snapshot_hash(_content(DriverID=6))
    assert base != calculate_snapshot_hash(_content(EmployeeID=4))
    assert base != calculate_snapshot_hash(_content(DriverNumberSnapshot="D-6"))


def test_report_evidence_hash_covers_assignment_and_driver():
    status = {
        "DriverAssignmentID": 11, "DriverID": 5, "WorkDate": "2030-01-01",
        "PayrollPeriodDriverDayEntryStateID": 1, "StatusKeyID": 2,
        "StatusCodeSnapshot": "OFF", "StatusLabelSnapshot": "Off",
        "StatusIsOffReasonSnapshot": True,
    }
    bonus = {
        "PayrollBonusEventID": 3, "DriverAssignmentID": 11, "DriverID": 5,
        "Amount": Decimal("5.00"),
    }
    base = calculate_report_evidence_hash(status_entries=[status], bonus_events=[bonus])
    assert base != calculate_report_evidence_hash(
        status_entries=[{**status, "DriverID": 6}], bonus_events=[bonus])
    assert base != calculate_report_evidence_hash(
        status_entries=[status], bonus_events=[{**bonus, "DriverAssignmentID": 12}])


def _mounted_routes(router, prefix: str = "") -> set[str]:
    routes: set[str] = set()
    for route in router.routes:
        if isinstance(route, APIRoute):
            routes.update(f"{m.upper()} {prefix}{route.path}" for m in route.methods or ())
        elif hasattr(route, "original_router") and hasattr(route, "include_context"):
            routes.update(_mounted_routes(
                route.original_router, prefix + route.include_context.prefix))
    return routes


def test_profile_addressed_routes_were_replaced_by_assignment_routes():
    mounted = _mounted_routes(create_app().router)
    # Ordinary compensation belongs to the permanent Driver (Durable Driver Stage 2).
    assert "GET /compensation/drivers/{driver_id}/pay-rates" in mounted
    assert "GET /compensation/driver-assignments/{driver_assignment_id}/pay-rates" not in mounted
    assert "GET /payroll/driver-assignments/{driver_assignment_id}/rate-matrix" in mounted
    # Pay rules belong to the permanent Driver; only status rates stay assignment-addressed.
    assert "GET /payroll/drivers/{driver_id}/pay-rules" in mounted
    assert "GET /payroll/driver-assignments/{driver_assignment_id}/pay-rules" not in mounted
    assert "GET /payroll/driver-assignments/rates-summary" in mounted
    # No compensation route is addressed by an assignment id: those are the permanent
    # Driver's. Of the payroll routes only pay rules take a {driver_id}.
    payroll_like = [r for r in mounted if " /payroll" in r]
    assert [r for r in payroll_like if "{driver_id}" in r or "/drivers/{" in r] == [
        "GET /payroll/drivers/{driver_id}/pay-rules"]
    assert not [r for r in mounted if " /compensation" in r and "{driver_assignment_id}" in r]


@pytest.mark.asyncio
async def test_audit_evidence_carries_the_permanent_driver_with_the_assignment(
    test_database_url, hq_driver_id, hq_driver_assignment_id,
):
    # Immutable evidence is written inside a transaction that is always rolled back.
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await _assert_audit_evidence_ids(conn, hq_driver_id, hq_driver_assignment_id)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def _assert_audit_evidence_ids(direct_db, hq_driver_id, hq_driver_assignment_id):
    created_period_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, Status, PeriodCode, PeriodName, PeriodType, StartDate, EndDate)
        VALUES (1, 1, 'Cancelled', 'STAGE1-PROV', 'Stage 1 provenance', 'Week',
                DATE '2031-01-06', DATE '2031-01-12')
        RETURNING PayrollPeriodID
    """))).scalar_one()
    event_id = await capture_period_audit_evidence(
        company_id=1, branch_id=1, period_id=created_period_id, domain="SOURCE",
        action_code="SOURCE_CREATED", source_entity_type="PayrollDraftLines",
        source_entity_id=0, user_id=1, required_permission_code="payroll.entry",
        db=direct_db, driver_assignment_id=hq_driver_assignment_id,
    )
    row = (await direct_db.execute(text("""
        SELECT DriverAssignmentID, DriverID
        FROM payroll.PayrollPeriodAuditEvidenceEvents
        WHERE PayrollPeriodAuditEvidenceEventID = :id
    """), {"id": event_id})).one()
    assert tuple(row) == (hq_driver_assignment_id, hq_driver_id)

    # An event that is not about a Driver carries neither id.
    other_id = await capture_period_audit_evidence(
        company_id=1, branch_id=1, period_id=created_period_id, domain="SOURCE",
        action_code="SOURCE_UPDATED", source_entity_type="PayrollDraftLines",
        source_entity_id=0, user_id=1, required_permission_code="payroll.entry",
        db=direct_db,
    )
    none_row = (await direct_db.execute(text("""
        SELECT DriverAssignmentID, DriverID
        FROM payroll.PayrollPeriodAuditEvidenceEvents
        WHERE PayrollPeriodAuditEvidenceEventID = :id
    """), {"id": other_id})).one()
    assert tuple(none_row) == (None, None)
