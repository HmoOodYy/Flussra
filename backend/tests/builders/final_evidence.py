"""Builders for finalized payroll evidence seeded directly in the database.

A FinalLine projects exactly one official component of a calculation snapshot, so a valid
FinalLine always has the chain snapshot -> Driver total -> component behind it. These helpers
write the smallest valid chain and the FinalLine that projects it, the way the controlled
finalization path writes them (through the insert-guard setting):

* by default one Bonus component of the given official amount (no dated work);
* with ``work_date`` one Status component with a single dated source line and its rate
  evidence, for tests that need dated finalized work.

``insert_final_line`` accepts overrides of the FinalLine columns (``branch``, ``company``,
``amount`` ...) for tests that probe the database guarantees around a FinalLine. Tests that
exercise the real freeze/finalize path do not use these helpers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from tests.db_state import (
    PERIOD_EVIDENCE_IMMUTABLE_TRIGGERS,
    allow_final_line_insert,
    suspended_test_triggers,
)

_AMOUNT = Decimal("100.00")
_HEX = "c" * 64
_LINE_TYPES = {"BONUS": "BONUS", "STATUS": "STATUS_PAY"}

_SCOPE = """
    SELECT p.companyid, p.branchid, p.startdate, p.enddate,
           a.driverid, a.employeeid,
           COALESCE((SELECT u.userid FROM sec.users u WHERE u.companyid = p.companyid
                     ORDER BY u.userid LIMIT 1), 1) AS userid,
           (SELECT c.currencycode FROM core.companies c WHERE c.companyid = p.companyid) AS currency,
           COALESCE((SELECT sc.minorunitdigits FROM core.supportedcurrencies sc
                     JOIN core.companies c ON c.currencycode = sc.currencycode
                     WHERE c.companyid = p.companyid), 2) AS digits,
           (SELECT COALESCE(MAX(s.revisionnumber), 0) + 1
            FROM payroll.payrollcalculationsnapshots s
            WHERE s.payrollperiodid = p.payrollperiodid) AS next_revision,
           (SELECT rt.ratetypeid FROM payroll.ratetypes rt
            WHERE rt.ratecode = 'STATUS_PAY' ORDER BY rt.ratetypeid LIMIT 1) AS status_rate_type
    FROM payroll.payrollperiods p
    JOIN core.driverassignments a ON a.driverassignmentid = {assignment}
    WHERE p.payrollperiodid = {period}
"""

_STATUS_COLUMN = """
    SELECT statusratecolumnid FROM payroll.statusratecolumns
    WHERE companyid = {company} AND branchid = {branch} AND ratetypeid = {rate_type} AND isactive
    ORDER BY statusratecolumnid LIMIT 1
"""

_STATUS_COLUMN_INSERT = """
    INSERT INTO payroll.statusratecolumns
        (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
    VALUES ({company}, {branch}, {rate_type}, 'Evidence status', {normalized}, FALSE, TRUE)
    RETURNING statusratecolumnid
"""

_DRIVER_RATE = """
    SELECT driverrateid FROM payroll.driverrates
    WHERE driverassignmentid = {assignment} AND ratetypeid = {rate_type} AND status = 'Approved'
      AND amount = {amount} AND effectiveto IS NULL
    ORDER BY driverrateid LIMIT 1
"""

_DRIVER_RATE_INSERT = """
    INSERT INTO payroll.driverrates
        (companyid, branchid, driverassignmentid, ratetypeid, amount, effectivefrom, status,
         createdbyuserid, approvedbyuserid, approvedatutc)
    VALUES ({company}, {branch}, {assignment}, {rate_type}, {amount}, DATE '2000-01-01', 'Approved',
            {user}, {user}, NOW())
    RETURNING driverrateid
"""

_SNAPSHOT = """
    INSERT INTO payroll.payrollcalculationsnapshots
        (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
         sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
         reportevidenceversion, reportevidencehash, currencycode, currencyminorunitdigits,
         periodstartdate, periodenddate, payableroundingversion, minmaxprorationversion,
         scheduledworkdates, warnings)
    VALUES ({company}, {branch}, {period}, {revision}, 'payroll-calculation-v2',
            {source_hash}, {snapshot_hash}, {user}, {amount},
            {evidence_version}, {evidence_hash}, {currency}, {digits},
            {start}, {end}, 'payable-rounding-v1', 'minmax-proration-v1', '{{}}', '{{}}')
    RETURNING payrollcalculationsnapshotid
"""

_TOTAL = """
    INSERT INTO payroll.payrollcalculationdrivertotals
        (payrollcalculationsnapshotid, companyid, branchid, driverassignmentid, driverid,
         employeeid, drivernumbersnapshot, drivernamesnapshot, dailypay, statuspay,
         minimumadjustment, maximumadjustment, bonustotal, expectedpay)
    VALUES ({snapshot}, {company}, {branch}, {assignment}, {driver}, {employee},
            'D-SEED', 'Seeded Driver', 0, {status_pay}, 0, 0, {bonus}, {amount})
    RETURNING payrollcalculationdrivertotalid
"""

_COMPONENT = """
    INSERT INTO payroll.payrollcalculationsnapshotcomponents
        (payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
         payrollcalculationdrivertotalid, componentkind, statusratecolumnid,
         statusratecolumnnamesnapshot, preroundingamount, officialamount, sourcecount,
         unresolvedlinecount)
    VALUES ({snapshot}, {company}, {branch}, {period}, {total}, {kind}, {status_column},
            {status_column_name}, {amount}, {amount}, 1, 0)
    RETURNING payrollcalculationsnapshotcomponentid
"""

_USED_RATE = """
    INSERT INTO payroll.payrollcalculationsnapshotusedratedefinitions
        (payrollcalculationsnapshotid, companyid, branchid, payrollperiodid, driverassignmentid,
         driverid, definitionfingerprint, evidencekind, sourcetypesnapshot, ratetypeid,
         driverrateid, rateamountsnapshot, effectivefromsnapshot, ratestatussnapshot,
         currencycodesnapshot, currencyminorunitdigitssnapshot)
    VALUES ({snapshot}, {company}, {branch}, {period}, {assignment}, {driver}, {fingerprint},
            'StatusDriverRate', 'StatusEntryState', {line_rate_type}, {driver_rate}, {rate_amount},
            DATE '2000-01-01', 'Approved', {currency}, {digits})
    RETURNING payrollcalculationsnapshotusedratedefinitionid
"""

_LINE = """
    INSERT INTO payroll.payrollcalculationsnapshotlines
        (payrollcalculationdrivertotalid, payrollcalculationsnapshotcomponentid, sourcetype,
         sourceid, linetype, linescope, workdate, statusratecolumnid, ratetypeid, driverrateid,
         quantity, resolvedrateamount, calculatedamount, sourceevidencejsonb,
         usedratedefinitionid)
    VALUES ({total}, {component}, 'StatusEntryState', {source_id}, 'STATUS', 'Daily', {work_date},
            {status_column}, {line_rate_type}, {driver_rate}, 1, {rate_amount}, {amount}, '{{}}'::jsonb,
            {used_rate})
"""

_FINAL_LINE = """
    INSERT INTO payroll.payrollfinallines
        (companyid, branchid, payrollperiodid, driverassignmentid, driverid, employeeid,
         linetype, linescope, finalamount, sourcetype, sourceid, approvedbyuserid, approvedatutc,
         lockedatutc, sourcesnapshot, currencycode, currencyminorunitdigits,
         payrollcalculationsnapshotid, payrollcalculationsnapshotcomponentid, componentkind)
    VALUES ({company}, {branch}, {period}, {assignment}, {driver}, {employee},
            {line_type}, 'Period', {amount}, 'SnapshotComponent', {source_id}, {user}, NOW(), NOW(),
            CAST({source_snapshot} AS JSONB), {currency}, {digits}, {snapshot}, {component}, {kind})
    RETURNING finallineid
"""

_NAMES = [
    "company", "branch", "period", "assignment", "revision", "snapshot_hash", "source_hash",
    "user", "amount", "evidence_version", "evidence_hash", "currency", "digits", "start", "end",
    "driver", "employee", "snapshot", "total", "component", "source_id", "source_snapshot",
    "kind", "line_type", "status_column", "status_column_name", "status_pay", "bonus",
    "rate_type", "driver_rate", "normalized", "fingerprint", "used_rate", "work_date",
    "line_rate_type", "rate_amount",
]


@dataclass(frozen=True)
class Chain:
    """A snapshot with one Driver total and one component, ready to be projected."""

    snapshot_id: int
    total_id: int
    component_id: int
    amount: Decimal
    snapshot_hash: str
    revision: int
    kind: str
    params: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class FinalizedEvidence:
    snapshot_id: int
    total_id: int
    component_id: int
    final_line_id: int
    amount: Decimal
    snapshot_hash: str


def _sql(template: str, style: str) -> str:
    marks = {n: (f"%({n})s" if style == "psycopg2" else f":{n}") for n in _NAMES}
    return template.format(**marks)


def _base_params(scope: dict[str, Any], *, period_id: int, assignment_id: int, revision: int,
                 snapshot_hash: str, amount: Decimal, evidence: tuple[int, str] | None,
                 kind: str) -> dict:
    return {
        "company": scope["companyid"], "branch": scope["branchid"], "period": period_id,
        "assignment": assignment_id, "revision": revision, "snapshot_hash": snapshot_hash,
        "source_hash": _HEX, "user": scope["userid"], "amount": amount,
        "evidence_version": None if evidence is None else evidence[0],
        "evidence_hash": None if evidence is None else evidence[1],
        "currency": scope["currency"], "digits": scope["digits"], "start": scope["startdate"],
        "end": scope["enddate"], "driver": scope["driverid"], "employee": scope["employeeid"],
        "kind": kind, "line_type": _LINE_TYPES[kind],
        "status_pay": amount if kind == "STATUS" else Decimal("0"),
        "bonus": amount if kind == "BONUS" else Decimal("0"),
        "status_column": None, "status_column_name": None,
    }


def _final_line_params(chain: Chain, overrides: dict[str, Any]) -> dict[str, Any]:
    source_snapshot = json.dumps({
        "payroll_calculation_snapshot_id": chain.snapshot_id, "revision_number": chain.revision,
        "snapshot_hash": chain.snapshot_hash, "snapshot_component_id": chain.component_id,
    })
    return {
        **chain.params, "snapshot": chain.snapshot_id, "total": chain.total_id,
        "component": chain.component_id, "source_id": str(chain.component_id),
        "source_snapshot": source_snapshot, **overrides,
    }


async def seed_snapshot_component(
    db, *, period_id: int, driver_assignment_id: int, amount: Decimal = _AMOUNT,
    snapshot_hash: str = _HEX, revision: int | None = None,
    report_evidence: tuple[int, str] | None = None, work_date: date | None = None,
    driver_rate_id: int | None = None,
) -> Chain:
    """Snapshot, Driver total and component for one DriverAssignment of a period.

    A ``work_date`` makes the component a Status component with one source line on that day,
    using the Approved Status rate of the assignment (or the given ``driver_rate_id``, whose
    amount the line records).
    """
    scope = (await db.execute(
        text(_SCOPE.format(assignment=":assignment", period=":period")),
        {"assignment": driver_assignment_id, "period": period_id})).mappings().one()
    kind = "BONUS" if work_date is None else "STATUS"
    params = _base_params(
        scope, period_id=period_id, assignment_id=driver_assignment_id,
        revision=scope["next_revision"] if revision is None else revision,
        snapshot_hash=snapshot_hash, amount=amount, evidence=report_evidence, kind=kind)
    if work_date is not None:
        params["rate_type"] = scope["status_rate_type"]
        params["normalized"] = "EVIDENCE " + uuid4().hex[:12].upper()
        column = (await db.execute(
            text(_sql(_STATUS_COLUMN, "sqlalchemy")), params)).scalar_one_or_none()
        if column is None:
            column = (await db.execute(
                text(_sql(_STATUS_COLUMN_INSERT, "sqlalchemy")), params)).scalar_one()
        if driver_rate_id is not None:
            rate = driver_rate_id
            line_rate_type, rate_amount = (await db.execute(
                text("SELECT ratetypeid, amount FROM payroll.driverrates WHERE driverrateid = :id"),
                {"id": rate})).one()
        else:
            rate = (await db.execute(
                text(_sql(_DRIVER_RATE, "sqlalchemy")), params)).scalar_one_or_none()
            if rate is None:
                rate = (await db.execute(
                    text(_sql(_DRIVER_RATE_INSERT, "sqlalchemy")), params)).scalar_one()
            line_rate_type, rate_amount = params["rate_type"], amount
        params.update(status_column=column, status_column_name="Evidence status",
                      driver_rate=rate, work_date=work_date, fingerprint=uuid4().hex * 2,
                      line_rate_type=line_rate_type, rate_amount=rate_amount)
    snapshot = int((await db.execute(text(_sql(_SNAPSHOT, "sqlalchemy")), params)).scalar_one())
    total = int((await db.execute(
        text(_sql(_TOTAL, "sqlalchemy")), {**params, "snapshot": snapshot})).scalar_one())
    component = int((await db.execute(
        text(_sql(_COMPONENT, "sqlalchemy")),
        {**params, "snapshot": snapshot, "total": total})).scalar_one())
    if work_date is not None:
        used_rate = (await db.execute(
            text(_sql(_USED_RATE, "sqlalchemy")), {**params, "snapshot": snapshot})).scalar_one()
        await db.execute(text(_sql(_LINE, "sqlalchemy")), {
            **params, "total": total, "component": component, "used_rate": used_rate,
            "source_id": f"seed-{component}"})
    return Chain(snapshot, total, component, amount, snapshot_hash, params["revision"], kind, params)


async def insert_final_line(db, chain: Chain, **overrides: Any) -> int:
    """The FinalLine projecting the chain's component, through the controlled insert path."""
    async with allow_final_line_insert(db):
        return int((await db.execute(
            text(_sql(_FINAL_LINE, "sqlalchemy")), _final_line_params(chain, overrides),
        )).scalar_one())


async def seed_finalized_evidence(db, **kwargs: Any) -> FinalizedEvidence:
    """A snapshot chain and the FinalLine that projects it."""
    chain = await seed_snapshot_component(db, **kwargs)
    line = await insert_final_line(db, chain)
    return FinalizedEvidence(
        chain.snapshot_id, chain.total_id, chain.component_id, line, chain.amount,
        chain.snapshot_hash)


async def discard_chain(db, chain: Chain) -> None:
    """Remove a seeded chain (and any FinalLine on it) from a database the test shares."""
    async with suspended_test_triggers(db, PERIOD_EVIDENCE_IMMUTABLE_TRIGGERS):
        for statement in (
            "DELETE FROM payroll.payrollfinallines WHERE payrollcalculationsnapshotid = :snapshot",
            "DELETE FROM payroll.payrollcalculationsnapshotlines WHERE payrollcalculationdrivertotalid "
            "IN (SELECT payrollcalculationdrivertotalid FROM payroll.payrollcalculationdrivertotals "
            "WHERE payrollcalculationsnapshotid = :snapshot)",
            "DELETE FROM payroll.payrollcalculationsnapshotcomponents "
            "WHERE payrollcalculationsnapshotid = :snapshot",
            "DELETE FROM payroll.payrollcalculationsnapshotusedratedefinitions "
            "WHERE payrollcalculationsnapshotid = :snapshot",
            "DELETE FROM payroll.payrollcalculationdrivertotals "
            "WHERE payrollcalculationsnapshotid = :snapshot",
            "DELETE FROM payroll.payrollcalculationsnapshots "
            "WHERE payrollcalculationsnapshotid = :snapshot",
        ):
            await db.execute(text(statement), {"snapshot": chain.snapshot_id})
