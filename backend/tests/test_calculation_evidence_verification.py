"""The canonical snapshot verification covers every fact a frozen reader can expose.

The used-rate evidence is part of the financial hash and is reconciled with the lines that link
to it. The report-only Status and Bonus evidence keeps its own hash, but it is verified inside
``verify_snapshot`` and reconciled with the financial evidence it overlaps, so no reader can
expose it unverified and no two facts can disagree unchecked.
"""
from __future__ import annotations

import copy
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.payroll.snapshot_hash import calculate_report_evidence_hash, used_rate_fingerprint
from app.payroll.snapshot_verification import (
    SnapshotEvidenceError,
    load_snapshot_content,
    reconcile_report_evidence,
    reconcile_used_rate_evidence,
    verify_snapshot,
)
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.db_state import suspended_test_triggers
from tests.payroll_runtime_fixtures import build_payroll_tenant
from tests.test_calculation_snapshot_integrity import IMMUTABILITY_TRIGGERS, _full_snapshot

pytestmark = pytest.mark.asyncio
D = Decimal


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


_USED_RATE_TAMPERING = [
    ("schedule rate amount",
     "UPDATE payroll.payrollcalculationsnapshotusedratedefinitions SET rateamountsnapshot = 999 "
     "WHERE payrollcalculationsnapshotid = {snapshot} AND evidencekind = 'DriverRateSchedule'"),
    ("Status rate amount",
     "UPDATE payroll.payrollcalculationsnapshotusedratedefinitions SET rateamountsnapshot = 999 "
     "WHERE payrollcalculationsnapshotid = {snapshot} AND evidencekind = 'StatusDriverRate'"),
    ("rate effective window",
     "UPDATE payroll.payrollcalculationsnapshotusedratedefinitions "
     "SET effectivefromsnapshot = effectivefromsnapshot - 30 "
     "WHERE payrollcalculationsnapshotid = {snapshot}"),
    ("rate status",
     "UPDATE payroll.payrollcalculationsnapshotusedratedefinitions "
     "SET ratestatussnapshot = 'Pending' WHERE payrollcalculationsnapshotid = {snapshot}"),
    ("Status rate type label",
     "UPDATE payroll.payrollcalculationsnapshotusedratedefinitions "
     "SET ratetypenamesnapshot = 'Other' "
     "WHERE payrollcalculationsnapshotid = {snapshot} AND evidencekind = 'StatusDriverRate'"),
    ("an extra used-rate row",
     "INSERT INTO payroll.payrollcalculationsnapshotusedratedefinitions "
     "(payrollcalculationsnapshotid, companyid, branchid, payrollperiodid, driverassignmentid, "
     "driverid, definitionfingerprint, evidencekind, sourcetypesnapshot, driverratescheduleid, "
     "ratedefinitionid, paydefinitionid, ratecomponentdefinitionid, rateamountsnapshot, "
     "effectivefromsnapshot, ratestatussnapshot, currencycodesnapshot, "
     "currencyminorunitdigitssnapshot) "
     "SELECT payrollcalculationsnapshotid, companyid, branchid, payrollperiodid, "
     "driverassignmentid, driverid, repeat('b', 64), evidencekind, sourcetypesnapshot, "
     "driverratescheduleid, ratedefinitionid, paydefinitionid, ratecomponentdefinitionid, "
     "rateamountsnapshot + 1, effectivefromsnapshot, ratestatussnapshot, currencycodesnapshot, "
     "currencyminorunitdigitssnapshot "
     "FROM payroll.payrollcalculationsnapshotusedratedefinitions "
     "WHERE payrollcalculationsnapshotid = {snapshot} AND evidencekind = 'DriverRateSchedule' "
     "LIMIT 1"),
]

_REPORT_EVIDENCE_TAMPERING = [
    ("Bonus report amount",
     "UPDATE payroll.payrollcalculationsnapshotbonusevents SET amount = 999 "
     "WHERE payrollcalculationsnapshotid = {snapshot}",
     "REPORT_EVIDENCE_HASH_MISMATCH"),
    ("Status report key",
     "UPDATE payroll.payrollcalculationsnapshotstatusentries SET statuscodesnapshot = 'OTHER' "
     "WHERE payrollcalculationsnapshotid = {snapshot}",
     "REPORT_EVIDENCE_HASH_MISMATCH"),
    ("Status report off-reason flag",
     "UPDATE payroll.payrollcalculationsnapshotstatusentries "
     "SET statusisoffreasonsnapshot = NOT statusisoffreasonsnapshot "
     "WHERE payrollcalculationsnapshotid = {snapshot}",
     "REPORT_EVIDENCE_HASH_MISMATCH"),
    ("report evidence hash",
     "UPDATE payroll.payrollcalculationsnapshots SET reportevidencehash = repeat('a', 64) "
     "WHERE payrollcalculationsnapshotid = {snapshot}",
     "REPORT_EVIDENCE_HASH_MISMATCH"),
    ("report evidence version",
     "UPDATE payroll.payrollcalculationsnapshots SET reportevidenceversion = 1 "
     "WHERE payrollcalculationsnapshotid = {snapshot}",
     "UNSUPPORTED_REPORT_EVIDENCE_VERSION"),
]


async def test_tampering_with_the_used_rate_evidence_fails_the_hash_verification(
    p3c_client, p3c_engine, tenant,
):
    _, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    async with p3c_engine.connect() as conn:
        outer = await conn.begin()
        try:
            async with suspended_test_triggers(conn, IMMUTABILITY_TRIGGERS):
                for label, statement in _USED_RATE_TAMPERING:
                    savepoint = await conn.begin_nested()
                    result = await conn.execute(text(statement.format(snapshot=frozen.snapshot_id)))
                    assert result.rowcount >= 1, label
                    with pytest.raises(SnapshotEvidenceError) as failed:
                        await verify_snapshot(conn, snapshot_id=frozen.snapshot_id)
                    assert failed.value.code == "SNAPSHOT_HASH_MISMATCH", label
                    await savepoint.rollback()
                await verify_snapshot(conn, snapshot_id=frozen.snapshot_id)
        finally:
            await outer.rollback()


async def test_tampering_with_the_report_evidence_fails_its_own_verification(
    p3c_client, p3c_engine, tenant,
):
    _, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    async with p3c_engine.connect() as conn:
        outer = await conn.begin()
        try:
            async with suspended_test_triggers(conn, IMMUTABILITY_TRIGGERS):
                for label, statement, code in _REPORT_EVIDENCE_TAMPERING:
                    savepoint = await conn.begin_nested()
                    result = await conn.execute(text(statement.format(snapshot=frozen.snapshot_id)))
                    assert result.rowcount >= 1, label
                    with pytest.raises(SnapshotEvidenceError) as failed:
                        await verify_snapshot(conn, snapshot_id=frozen.snapshot_id)
                    assert failed.value.code == code, label
                    await savepoint.rollback()
                await verify_snapshot(conn, snapshot_id=frozen.snapshot_id)
        finally:
            await outer.rollback()


async def _loaded(engine, snapshot_id: int) -> dict:
    async with engine.connect() as conn:
        return await load_snapshot_content(conn, snapshot_id=snapshot_id)


def _first_line(content: dict, source_type: str) -> dict:
    return next(
        line for total in content["DriverTotals"] for line in total["Lines"]
        if line["SourceType"] == source_type)


def _reseal(content: dict, kind: str, **fields) -> None:
    """Change one rate row and keep its fingerprint and every link consistent with the change,
    so only the agreement with the lines can reject it."""
    rate = next(r for r in content["UsedRates"] if r["EvidenceKind"] == kind)
    old = rate["DefinitionFingerprint"]
    rate.update(fields)
    rate["DefinitionFingerprint"] = used_rate_fingerprint(rate)
    for total in content["DriverTotals"]:
        for line in total["Lines"]:
            if line["UsedRateFingerprint"] == old:
                line["UsedRateFingerprint"] = rate["DefinitionFingerprint"]


def _orphan(content: dict) -> None:
    extra = {**content["UsedRates"][0], "DriverRateScheduleID": 123456}
    extra["DefinitionFingerprint"] = used_rate_fingerprint(extra)
    content["UsedRates"].append(extra)


async def test_used_rates_must_agree_with_the_lines_that_link_to_them(
    p3c_client, p3c_engine, tenant,
):
    _, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    content = await _loaded(p3c_engine, frozen.snapshot_id)
    reconcile_used_rate_evidence(content)
    assert {rate["EvidenceKind"] for rate in content["UsedRates"]} == {
        "DriverRateSchedule", "StatusDriverRate"}

    cases = {
        "an orphan used-rate row": _orphan,
        "a stale fingerprint": lambda c: c["UsedRates"][0].update({"RateAmount": D("999")}),
        "a schedule row for another schedule": lambda c: _reseal(
            c, "DriverRateSchedule", DriverRateScheduleID=987654),
        "a schedule row with another amount": lambda c: _reseal(
            c, "DriverRateSchedule", RateAmount=D("999")),
        "a schedule row for another pay definition": lambda c: _reseal(
            c, "DriverRateSchedule", PayDefinitionID=987654),
        "a Status row for another rate": lambda c: _reseal(
            c, "StatusDriverRate", DriverRateID=987654),
        "a Status row for another rate type": lambda c: _reseal(
            c, "StatusDriverRate", RateTypeID=987654),
        "a Status row with another amount": lambda c: _reseal(
            c, "StatusDriverRate", RateAmount=D("999")),
        "an ordinary line without a rate": lambda c: _first_line(c, "DraftLine").update(
            {"UsedRateFingerprint": None}),
        "a Bonus line with a rate": lambda c: _first_line(c, "BonusEvent").update(
            {"UsedRateFingerprint": c["UsedRates"][0]["DefinitionFingerprint"]}),
        "a row in another currency": lambda c: _reseal(
            c, "DriverRateSchedule", CurrencyCodeSnapshot="XXX"),
    }
    for label, change in cases.items():
        broken = copy.deepcopy(content)
        change(broken)
        with pytest.raises(SnapshotEvidenceError) as failed:
            reconcile_used_rate_evidence(broken)
        assert failed.value.code == "USED_RATE_EVIDENCE_INTEGRITY_ERROR", label


def _rehashed(content: dict) -> dict:
    """The content with its report evidence hash recomputed, so only the overlap checks with
    the financial evidence can reject it."""
    evidence = content["ReportEvidence"]
    return {**content, "ReportEvidenceHash": calculate_report_evidence_hash(
        status_entries=evidence["StatusEntries"], bonus_events=evidence["BonusEvents"])}


async def test_report_evidence_must_agree_with_the_financial_evidence(
    p3c_client, p3c_engine, tenant,
):
    _, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    content = await _loaded(p3c_engine, frozen.snapshot_id)
    reconcile_report_evidence(content)
    (total,) = content["DriverTotals"]
    counted = total["MinMax"]["CountedWorkDates"][0]

    def first_status(c):
        return c["ReportEvidence"]["StatusEntries"][0]

    cases = {
        "a Bonus event with another amount": lambda c: c["ReportEvidence"]["BonusEvents"][0].update(
            {"Amount": D("999")}),
        "a Bonus event of another assignment": lambda c: c["ReportEvidence"]["BonusEvents"][0].update(
            {"DriverAssignmentID": 987654}),
        "a Bonus event in another currency": lambda c: c["ReportEvidence"]["BonusEvents"][0].update(
            {"CurrencyCodeSnapshot": "XXX"}),
        "a missing Bonus event": lambda c: c["ReportEvidence"]["BonusEvents"].clear(),
        "a Status line without its entry": lambda c: c["ReportEvidence"]["StatusEntries"].pop(),
        "a Status entry of another day": lambda c: first_status(c).update(
            {"WorkDate": first_status(c)["WorkDate"] + timedelta(days=1)}),
        "a Status entry of another key": lambda c: first_status(c).update({"StatusKeyID": 987654}),
        "a Status entry naming another Driver": lambda c: first_status(c).update({"DriverID": 987654}),
        "an off-reason entry on a day Min/Max counted": lambda c: c["ReportEvidence"][
            "StatusEntries"].append({
                **first_status(c), "PayrollPeriodDriverDayEntryStateID": 987654,
                "WorkDate": counted, "StatusIsOffReasonSnapshot": True}),
    }
    for label, change in cases.items():
        broken = copy.deepcopy(content)
        change(broken)
        with pytest.raises(SnapshotEvidenceError) as failed:
            reconcile_report_evidence(_rehashed(broken))
        assert failed.value.code == "REPORT_EVIDENCE_INTEGRITY_ERROR", label
