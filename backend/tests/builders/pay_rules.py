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


async def freeze_period_days(db, period_id: int) -> int:
    """Give a directly inserted period the frozen day calendar the canonical creator writes.

    The period takes the authority of the Branch's Payroll Setup in force at its start date
    (assignment and latest published version), and one PayrollPeriodDays row per date is
    written from that version's days-off mask. Returns the number of day rows.
    """
    period = (await db.execute(text("""
        SELECT companyid, branchid, startdate, enddate FROM payroll.payrollperiods
        WHERE payrollperiodid = :p"""), {"p": period_id})).mappings().one()
    authority = (await db.execute(text("""
        SELECT a.branchpayrollsetupassignmentid AS assignment_id, a.payrollsetupid AS setup_id,
               s.setupcode, v.payrollsetupversionid AS version_id, v.versionnumber,
               v.payrollfrequency, v.anchorstartdate, v.customintervaldays,
               v.normaldaysoffmask, v.confighash
        FROM payroll.branchpayrollsetupassignments a
        JOIN payroll.payrollsetups s ON s.payrollsetupid = a.payrollsetupid
        JOIN payroll.payrollsetupversions v
          ON v.payrollsetupid = a.payrollsetupid AND v.lifecyclestate = 'Published'
         AND v.effectivefromdate <= :start
        WHERE a.companyid = :c AND a.branchid = :b AND a.withdrawnatutc IS NULL
          AND a.effectivefromdate <= :start
          AND (a.effectivetodate IS NULL OR a.effectivetodate > :end)
        ORDER BY v.effectivefromdate DESC, v.versionnumber DESC LIMIT 1
    """), {"c": period["companyid"], "b": period["branchid"], "start": period["startdate"],
           "end": period["enddate"]})).mappings().one()
    await db.execute(text("""
        UPDATE payroll.payrollperiods SET
            branchpayrollsetupassignmentid = :a, payrollsetupversionid = :v,
            frozenpayrollsetupid = :s, frozenpayrollsetupcode = :code,
            frozenpayrollsetupversionnumber = :n, frozenpayrollfrequency = :f,
            frozenanchorstartdate = :anchor, frozencustomintervaldays = :i,
            frozennormaldaysoffmask = :m, scheduleconfighash = :h
        WHERE payrollperiodid = :p
    """), {"a": authority["assignment_id"], "v": authority["version_id"],
           "s": authority["setup_id"], "code": authority["setupcode"],
           "n": authority["versionnumber"], "f": authority["payrollfrequency"],
           "anchor": authority["anchorstartdate"], "i": authority["customintervaldays"],
           "m": authority["normaldaysoffmask"], "h": authority["confighash"], "p": period_id})
    result = await db.execute(text("""
        INSERT INTO payroll.payrollperioddays
            (payrollperiodid, companyid, branchid, branchpayrollsetupassignmentid,
             payrollsetupversionid, workdate, dayofweek, isdefaultworkday, isconfiguredoffday)
        SELECT :p, :c, :b, :a, :v, d::date, extract(dow FROM d)::int,
               ((:m >> extract(dow FROM d)::int) & 1) = 0,
               ((:m >> extract(dow FROM d)::int) & 1) = 1
        FROM generate_series(CAST(:start AS date), CAST(:end AS date), interval '1 day') AS d
    """), {"p": period_id, "c": period["companyid"], "b": period["branchid"],
           "a": authority["assignment_id"], "v": authority["version_id"],
           "m": authority["normaldaysoffmask"] or 0, "start": period["startdate"],
           "end": period["enddate"]})
    return result.rowcount
