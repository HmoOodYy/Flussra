"""Builders for Driver-owned pay rules (Min/Max) in backend tests.

A pay rule records the home pay calendar it was set for, captured from the Payroll Setup of
the Branch of its context tenure. API tests therefore need that Branch to have a published
Payroll Setup in force; direct-SQL tests need a calendar source (a published version) to
reference. Both are built here with plain SQL, so a test controls the exact calendar.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

# Every Monday-anchored weekly grid contains the same period starts, so one anchor serves any
# test that places periods on Mondays.
DEFAULT_ANCHOR = date(2000, 1, 3)


async def _user_id(db, company_id: int) -> int:
    row = (await db.execute(text(
        "SELECT userid FROM sec.users WHERE companyid = :c ORDER BY userid LIMIT 1"),
        {"c": company_id})).scalar_one_or_none()
    if row is not None:
        return int(row)
    return int((await db.execute(text("""
        INSERT INTO sec.users (companyid, username, displayname)
        VALUES (:c, :u, 'pay rule builder') RETURNING userid
    """), {"c": company_id, "u": "prb_" + uuid4().hex[:10]})).scalar_one())


async def publish_version(
    db, company_id: int, *, frequency: str = "Week", anchor: date = DEFAULT_ANCHOR,
    interval: int | None = None, mask: int = 0, effective_from: date | None = None,
    setup_id: int | None = None, version_number: int = 1,
) -> tuple[int, int]:
    """A published Payroll Setup version (and its setup unless given). Returns (setup, version)."""
    user = await _user_id(db, company_id)
    if setup_id is None:
        setup_id = int((await db.execute(text("""
            INSERT INTO payroll.payrollsetups (companyid, setupcode, setupname, status)
            VALUES (:c, :code, 'pay rule calendar', 'Active') RETURNING payrollsetupid
        """), {"c": company_id, "code": "PRC" + uuid4().hex[:10]})).scalar_one())
    version_id = int((await db.execute(text("""
        INSERT INTO payroll.payrollsetupversions
            (companyid, payrollsetupid, lifecyclestate, versionnumber, effectivefromdate,
             payrollfrequency, anchorstartdate, customintervaldays, normaldaysoffmask,
             confighash, publishedbyuserid, publishedatutc)
        VALUES (:c, :s, 'Published', :n, :eff, :f, :a, :i, :m, :h, :u, NOW())
        RETURNING payrollsetupversionid
    """), {"c": company_id, "s": setup_id, "n": version_number, "eff": effective_from or anchor,
           "f": frequency, "a": anchor, "i": interval, "m": mask,
           "h": uuid4().hex + uuid4().hex, "u": user})).scalar_one())
    return setup_id, version_id


async def ensure_branch_pay_calendar(
    db, company_id: int, branch_id: int, *, frequency: str = "Week",
    anchor: date = DEFAULT_ANCHOR, interval: int | None = None, mask: int = 0,
    from_date: date | None = None,
) -> dict:
    """Give the Branch a published Payroll Setup in force from ``from_date`` (default: anchor).

    If the Branch already has an effective assignment on that date nothing is created. Returns
    the setup, version and assignment ids in force.
    """
    start = from_date or anchor
    existing = (await db.execute(text("""
        SELECT a.branchpayrollsetupassignmentid, a.payrollsetupid
        FROM payroll.branchpayrollsetupassignments a
        WHERE a.companyid = :c AND a.branchid = :b AND a.withdrawnatutc IS NULL
          AND a.effectivefromdate <= :d AND (a.effectivetodate IS NULL OR a.effectivetodate > :d)
    """), {"c": company_id, "b": branch_id, "d": start})).mappings().first()
    if existing is not None:
        version = (await db.execute(text("""
            SELECT payrollsetupversionid FROM payroll.payrollsetupversions
            WHERE payrollsetupid = :s AND lifecyclestate = 'Published'
              AND effectivefromdate <= :d ORDER BY effectivefromdate DESC LIMIT 1
        """), {"s": existing["payrollsetupid"], "d": start})).scalar_one()
        return {"setup_id": int(existing["payrollsetupid"]), "version_id": int(version),
                "assignment_id": int(existing["branchpayrollsetupassignmentid"])}
    setup_id, version_id = await publish_version(
        db, company_id, frequency=frequency, anchor=anchor, interval=interval, mask=mask,
        effective_from=start)
    assignment_id = int((await db.execute(text("""
        INSERT INTO payroll.branchpayrollsetupassignments
            (companyid, branchid, payrollsetupid, effectivefromdate)
        VALUES (:c, :b, :s, :d) RETURNING branchpayrollsetupassignmentid
    """), {"c": company_id, "b": branch_id, "s": setup_id, "d": start})).scalar_one())
    return {"setup_id": setup_id, "version_id": version_id, "assignment_id": assignment_id}


async def insert_pay_rule(
    db, *, company_id: int, driver_id: int, branch_id: int, rule_type: str = "MinimumPay",
    amount: Decimal | str = "100", start: date | str = "2099-01-01", end: date | str | None = None,
    status: str = "Active", frequency: str = "Week", anchor: date = DEFAULT_ANCHOR,
    interval: int | None = None, version_id: int | None = None,
) -> int:
    """A pay rule straight into the table, with an explicit calendar (default weekly, Monday).

    The calendar source is a published version of ``branch_id``'s company, created on demand.
    """
    if version_id is None:
        _, version_id = await publish_version(
            db, company_id, frequency=frequency, anchor=anchor, interval=interval)
    return int((await db.execute(text("""
        INSERT INTO payroll.driverpayrules
            (companyid, driverid, ruletype, amount, effectivefrom, effectiveto, status,
             calendarfrequency, calendaranchorstartdate, calendarcustomintervaldays,
             calendarsourcebranchid, calendarsourcepayrollsetupversionid)
        VALUES (:c, :d, :t, :a, :f, :e, :s, :cf, :ca, :ci, :b, :v)
        RETURNING driverpayruleid
    """), {"c": company_id, "d": driver_id, "t": rule_type, "a": Decimal(str(amount)),
           "f": date.fromisoformat(str(start)),
           "e": None if end is None else date.fromisoformat(str(end)), "s": status,
           "cf": frequency, "ca": anchor, "ci": interval, "b": branch_id,
           "v": version_id})).scalar_one())
