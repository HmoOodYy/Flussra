"""Authority, visibility and lock primitives for Driver-owned compensation.

Ordinary rate schedules and Min/Max rules belong to the permanent Driver, not to a Branch.
Who may change them, and who may read them, is decided from the Driver's home tenures
(``core.DriverAssignments``), never from a Branch stored on the compensation record.

Authority (write)
    A change has a *start date*: the first date whose resolved compensation changes. The
    *context assignment* of that date is the latest-starting assignment of the Driver whose
    instant window overlaps the date's Branch-local day in the assignment's own Branch
    timezone (``core.fn_DriverAssignmentForBusinessDate``). The caller needs the rate-edit
    permission on the context assignment's Branch, or company-wide. The context must exist
    and must not have ended at the decision instant: ordinary historical mutation is
    refused for everyone, company-wide roles included. A Scheduled transfer's destination
    is a context from its start day; a transfer that is not yet Scheduled grants nothing.
    Discarding a Pending row has no payroll effect, so it needs only authority over the
    Driver now: a current or pending tenure's Branch, or company-wide.

Visibility (read)
    The Driver's current or pending authority Branches and company-wide readers see the
    whole compensation history, Pending rows included. A Branch whose tenure has ended
    sees, read-only, only rows whose windows overlap that tenure's Branch-local days, and
    never a Pending row.

Lock order for every Driver-owned writer
    1. authorize from an unlocked read (so an unauthorized caller never reaches a lock);
    2. Company currency, FOR SHARE (monetary writers only);
    3. the Driver's ``core.DriverAssignments`` rows, FOR SHARE, in ascending id;
    4. Branch workflow advisory locks, ascending BranchID, for every Branch whose tenure
       overlaps ``[start date, infinity)`` (writers with payroll effect only);
    5. the RateDefinition structure lock;
    6. the schedule / rule rows, FOR UPDATE;
    7. one decision instant (``clock_timestamp()``), then the finalized-payroll guard and the
       context check at that instant, then the DML.

    Transfers and termination take Employee, request, assignments (FOR NO KEY UPDATE), the
    Branch workflow lock and the period rows in that order; period creation and
    submit/finalize take the Company currency and then the Branch workflow lock; payroll
    source writers take the Company currency and then the period row. Steps 2-4 above are a
    subsequence of those orders, so no cycle exists. The RateDefinition lock (5) comes after
    the workflow lock, as in period creation (workflow, compensation config, RateDefinition).
    The database trigger ``core.trg_DriverCompensationContextWritable`` re-decides the
    context at the DML statement's own instant, so the application decision is never the
    last word.
"""

from dataclasses import dataclass
from datetime import date, datetime

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import has_any_permission
from app.access.policy import require_non_driver_subject
from app.company_currency import lock_and_get_company_currency_for_monetary_write
from app.compensation.errors import compensation_error
from app.compensation.guards import RATE_EDIT, RATE_READ
from app.payroll.workflow_lock import acquire_branch_workflow_lock
from app.rate_definition_concurrency import lock_rate_definition_structure
from app.workforce.effective import driver_compensation_historical_detail
from app.workforce.ops import decision_instant


def _historical() -> HTTPException:
    return HTTPException(status_code=409, detail=driver_compensation_historical_detail())


@dataclass(frozen=True)
class Tenure:
    """One non-empty home tenure of a Driver, with its Branch-local first and last day."""

    driver_assignment_id: int
    branch_id: int
    effective_from_at: datetime
    effective_to_at: datetime | None
    first_day: date
    last_day: date | None  # None while the tenure is open

    def is_authority(self, instant: datetime) -> bool:
        """Current or pending: not yet ended at the instant."""
        return self.effective_to_at is None or self.effective_to_at > instant


@dataclass(frozen=True)
class ContextAssignment:
    driver_assignment_id: int
    branch_id: int
    effective_from_at: datetime
    effective_to_at: datetime | None
    ended: bool


async def require_driver(company_id: int, driver_id: int, db: AsyncConnection) -> None:
    """404 when the Driver is not in the Company."""
    found = (await db.execute(
        text("SELECT 1 FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": driver_id, "cid": company_id},
    )).scalar_one_or_none()
    if found is None:
        raise compensation_error("DRIVER_NOT_FOUND", "Driver not found.", 404)


async def load_driver_tenures(
    company_id: int, driver_id: int, db: AsyncConnection,
) -> list[Tenure]:
    """Every non-empty tenure of the Driver, earliest first. Empty never-effective windows
    (a terminated destination) cover no day and are omitted."""
    rows = (await db.execute(
        text("""
            SELECT a.driverassignmentid, a.branchid, a.effectivefromat, a.effectivetoat,
                   (a.effectivefromat AT TIME ZONE b.timezoneid)::date AS first_day,
                   CASE WHEN a.effectivetoat IS NULL THEN NULL
                        ELSE ((a.effectivetoat - INTERVAL '1 microsecond')
                              AT TIME ZONE b.timezoneid)::date END AS last_day
            FROM   core.driverassignments a
            JOIN   core.branches b ON b.branchid = a.branchid AND b.companyid = a.companyid
            WHERE  a.companyid = :cid AND a.driverid = :did
              AND  (a.effectivetoat IS NULL OR a.effectivetoat > a.effectivefromat)
            ORDER  BY a.effectivefromat, a.driverassignmentid
        """),
        {"cid": company_id, "did": driver_id},
    )).mappings().all()
    return [
        Tenure(
            driver_assignment_id=int(r["driverassignmentid"]), branch_id=int(r["branchid"]),
            effective_from_at=r["effectivefromat"], effective_to_at=r["effectivetoat"],
            first_day=r["first_day"], last_day=r["last_day"],
        )
        for r in rows
    ]


async def resolve_compensation_context(
    company_id: int, driver_id: int, start_date: date, db: AsyncConnection,
    *, at: datetime | None = None,
) -> ContextAssignment | None:
    """The context assignment of a start date, or None when the Driver has no tenure that
    day. ``ended`` is judged at ``at`` (the wall clock when omitted)."""
    instant = at if at is not None else await decision_instant(db)
    row = (await db.execute(
        text("""
            SELECT a.driverassignmentid, a.branchid, a.effectivefromat, a.effectivetoat
            FROM   core.driverassignments a
            WHERE  a.driverassignmentid =
                   core.fn_DriverAssignmentForBusinessDate(:cid, :did, CAST(:day AS date))
        """),
        {"cid": company_id, "did": driver_id, "day": start_date},
    )).mappings().first()
    if row is None:
        return None
    ended_at = row["effectivetoat"]
    return ContextAssignment(
        driver_assignment_id=int(row["driverassignmentid"]), branch_id=int(row["branchid"]),
        effective_from_at=row["effectivefromat"], effective_to_at=ended_at,
        ended=ended_at is not None and ended_at <= instant,
    )


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=("You do not have permission to perform this action "
                f"(required one of: {', '.join(RATE_EDIT)})."),
    )


async def require_compensation_authority(
    company_id: int, user_id: int, driver_id: int, start_date: date | None,
    db: AsyncConnection, *, mode: str = "write", at: datetime | None = None,
) -> ContextAssignment | None:
    """Require authority over a Driver-owned compensation change.

    ``mode="write"`` decides from the context assignment of ``start_date`` and returns it.
    ``mode="discard"`` (a Pending row, no payroll effect) needs authority over the Driver
    now and returns None. DRIVER/Self subjects are denied first, and permission is
    established before any tenure state is revealed.
    """
    await require_non_driver_subject(company_id, user_id, db)
    await require_driver(company_id, driver_id, db)
    instant = at if at is not None else await decision_instant(db)
    company_wide = await has_any_permission(company_id, user_id, None, RATE_EDIT, db)
    tenures = await load_driver_tenures(company_id, driver_id, db)

    if mode == "discard":
        if company_wide:
            return None
        for branch_id in sorted({t.branch_id for t in tenures if t.is_authority(instant)}):
            if await has_any_permission(company_id, user_id, branch_id, RATE_EDIT, db):
                return None
        raise _forbidden()
    if mode != "write":
        raise ValueError(f"Unknown authority mode: {mode}")
    if start_date is None:
        raise ValueError("start_date is required for a write")

    permitted: set[int] = set()
    if not company_wide:
        for branch_id in sorted({t.branch_id for t in tenures}):
            if await has_any_permission(company_id, user_id, branch_id, RATE_EDIT, db):
                permitted.add(branch_id)
        if not permitted:
            raise _forbidden()

    context = await resolve_compensation_context(
        company_id, driver_id, start_date, db, at=instant)
    if context is None:
        if company_wide:
            raise _historical()
        raise _forbidden()
    if not company_wide and context.branch_id not in permitted:
        raise _forbidden()
    if context.ended:
        raise _historical()
    return context


@dataclass(frozen=True)
class ReadScope:
    """What compensation history of one Driver a caller may read.

    ``full`` readers (an authority Branch or company-wide) see everything, Pending rows
    included. Others see only rows overlapping ``windows`` (inclusive Branch-local day
    ranges of their past tenures) and never a Pending row.
    """

    full: bool
    windows: tuple[tuple[date, date], ...] = ()

    def covers(self, day: date) -> bool:
        return self.full or any(first <= day <= last for first, last in self.windows)

    def row_filter(self, alias: str, params: dict) -> str:
        """SQL predicate (with leading AND) limiting ``alias`` rows to what is readable.

        ``alias`` must expose ``status``, ``effectivefrom`` and ``effectiveto``.
        """
        if self.full:
            return ""
        params["scope_from"] = [first for first, _ in self.windows]
        params["scope_to"] = [last for _, last in self.windows]
        return f"""
            AND {alias}.status <> 'Pending'
            AND EXISTS (
                SELECT 1
                FROM   unnest(CAST(:scope_from AS date[]), CAST(:scope_to AS date[])) AS w(f, t)
                WHERE  daterange({alias}.effectivefrom, {alias}.effectiveto, '[]')
                       && daterange(w.f, w.t, '[]'))
        """

    def sees(self, row: dict) -> bool:
        if self.full:
            return True
        if row["status"] == "Pending":
            return False
        last = row["effective_to"]
        return any(
            row["effective_from"] <= window_last and (last is None or last >= window_first)
            for window_first, window_last in self.windows
        )


async def driver_compensation_read_scope(
    company_id: int, user_id: int, driver_id: int, db: AsyncConnection,
) -> ReadScope:
    """Resolve what the caller may read of the Driver's compensation (403 when nothing)."""
    await require_non_driver_subject(company_id, user_id, db)
    await require_driver(company_id, driver_id, db)
    if await has_any_permission(company_id, user_id, None, RATE_READ, db):
        return ReadScope(full=True)
    instant = await decision_instant(db)
    tenures = await load_driver_tenures(company_id, driver_id, db)
    readable: dict[int, bool] = {}
    for branch_id in sorted({t.branch_id for t in tenures}):
        readable[branch_id] = await has_any_permission(
            company_id, user_id, branch_id, RATE_READ, db)
    if any(readable[t.branch_id] and t.is_authority(instant) for t in tenures):
        return ReadScope(full=True)
    windows = tuple(
        (t.first_day, t.last_day) for t in tenures
        if readable[t.branch_id] and t.last_day is not None
    )
    if not windows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=("You do not have permission to perform this action "
                    f"(required one of: {', '.join(RATE_READ)})."),
        )
    return ReadScope(full=False, windows=windows)



async def lock_driver_compensation_write(
    company_id: int, driver_id: int, rate_definition_id: int, db: AsyncConnection,
    *, monetary: bool, lock_from: date | None,
) -> None:
    """Take steps 2-5 of the lock order (see the module docstring).

    ``monetary`` takes the Company currency guard; ``lock_from`` (the change's start date)
    additionally takes the Branch workflow locks of every tenure overlapping
    ``[lock_from, infinity)``. The caller then locks its rows FOR UPDATE, reads one decision
    instant and re-checks authority and the finalized guard before any DML.
    """
    if monetary:
        await lock_and_get_company_currency_for_monetary_write(company_id, db)
    await db.execute(
        text("""
            SELECT driverassignmentid FROM core.driverassignments
            WHERE  companyid = :cid AND driverid = :did
            ORDER  BY driverassignmentid
            FOR SHARE
        """),
        {"cid": company_id, "did": driver_id},
    )
    if lock_from is not None:
        tenures = await load_driver_tenures(company_id, driver_id, db)
        branches = sorted({
            t.branch_id for t in tenures if t.last_day is None or t.last_day >= lock_from
        })
        for branch_id in branches:
            await acquire_branch_workflow_lock(company_id, branch_id, db)
    await lock_rate_definition_structure(rate_definition_id, db)
