"""
CP-4B focused tests — Open/Returned live read-only calculation preview.

GET /payroll/periods/{id}/calculation-preview

Covers:
  - lifecycle guard (Open/Returned allowed, every other status denied);
  - permission contract (payroll.view/payroll.entry allowed, payroll.finalize-only
    denied, ODA/driver-role denied, wrong-company/inaccessible-branch denied);
  - daily PerUnit parity with the existing production calculation path;
  - canonical live Status-derived pay, and exclusion of the stale persisted
    STATUS_PAYMENT/STATUS_PAY compatibility projection;
  - missing-Status-rate blocker behavior;
  - non-BONUS period pay inclusion, legacy BONUS DraftLine exclusion;
  - canonical Active-only bonus, CP-3C minimum/maximum-then-bonus ordering;
  - financial-source-driven driver union/deduplication;
  - structural-blocker propagation from the shared read-only validator;
  - absolute read-only (before/after DB-state) proof, including the
    missing-rate-blocker path;
  - non-regression of the existing Approved-only finalization-preview route.

Test-infrastructure safety (this pass):
  - every persistent-write fixture helper begins its ownership scope BEFORE
    the write is sent, using a predeclared recovery marker/composite key
    known before the write -- never only after response parsing succeeds;
  - cleanup recovers the exact persisted ID via that marker when the normal
    code path never reached the assignment, deletes FK-dependent rows,
    deletes exact AuditLog rows by EntityName+EntityID (never a broad
    company/branch/time-window delete), then asserts zero residue;
  - no shared database schema object (index, table) is ever created,
    dropped, or altered by this file -- structural-blocker coverage for
    duplicate Daily lines is proven via a test-local stub of
    `_validate_period_can_finalize` plus the existing, untouched
    `test_payroll_trust_p7.py` / `test_payroll_trust_p8.py` validator
    regression suites, which already exercise genuine duplicate rows inside
    their own pre-existing, isolated setup.

Isolation: periods use year 2199 dates, distinct from every other test module's
isolation year.
"""
import contextlib
import datetime
import pathlib
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text

_PERIOD_CODE_PREFIX = "P4CP4B-"
PERIOD_START = "2199-02-03"
PERIOD_END = "2199-02-09"
DATE_FEB04 = "2199-02-04"
DATE_FEB05 = "2199-02-05"


class _DeliberateSetupFailure(Exception):
    """Test-local-only exception used by every acquisition-failure regression
    test and `_inject_failure_after_*` fault-injection hook below -- never
    referenced by production code. Raised strictly AFTER a persistent write
    has committed but BEFORE the normal code path would extract its ID from
    the response/row, to reproduce the exact unsafe window Codex flagged."""


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Independent cleanup runner (Codex P1 fix).
#
# Every ownership helper's `finally` block previously ran its DELETE
# statements as bare sequential `await db.execute(...)` calls: the first
# exception (e.g. an unexpected FK violation) aborted every remaining,
# otherwise-independent cleanup step. `_CleanupRunner` attempts each
# registered operation, records (never silently swallows) any failure, and
# always continues with the rest. All collected failures are raised
# together at the end via the real, repository-compatible builtin
# `ExceptionGroup` (Python 3.11+) -- never only the first -- and raising
# only happens when at least one operation actually failed, so the normal
# (zero-error) path never manufactures a new exception that could mask an
# original exception already propagating through the same `finally`.
# ---------------------------------------------------------------------------

class _CleanupRunner:
    def __init__(self):
        self._errors: list[BaseException] = []

    async def execute(self, db, sql: str, params: dict, *, label: str) -> None:
        """Attempt one DELETE/UPDATE cleanup statement; record failure and continue."""
        try:
            await db.execute(_text(sql), params)
        except Exception as exc:  # noqa: BLE001 -- recorded, never dropped
            exc.add_note(f"cleanup step failed: {label}")
            self._errors.append(exc)

    async def check(self, fn, *, label: str) -> None:
        """Attempt one arbitrary async callable (typically a residue
        assertion); record failure and continue with remaining checks."""
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001 -- recorded, never dropped
            exc.add_note(f"cleanup assertion failed: {label}")
            self._errors.append(exc)

    def raise_if_any(self) -> None:
        if self._errors:
            raise ExceptionGroup(f"{len(self._errors)} cleanup step(s) failed", self._errors)


# ---------------------------------------------------------------------------
# PeriodCode generation -- P1-C/P3 fix.
#
# The full uuid4 token is reserved FIRST and is solely responsible for
# uniqueness; only the human-readable prefix/suffix portion is truncated to
# whatever space remains under VARCHAR(80). This mirrors the proven pattern
# in test_cp4a_perunit_core.py's `_generate_unique_period_code` (a separate
# copy is kept here, not imported, since this module owns its own prefix and
# isolation year).
# ---------------------------------------------------------------------------

_PERIOD_CODE_MAX_LEN = 80  # payroll.PayrollPeriods.PeriodCode is VARCHAR(80)
_PERIOD_CODE_SEPARATOR = "-"


def _generate_unique_period_code(branch_id: int, suffix: str = "") -> str:
    """
    Builds a PeriodCode that is unique on every call regardless of whether a
    caller passes `suffix` -- the random `uuid4().hex` token is always
    present, in FULL, and is solely responsible for uniqueness. The token is
    reserved first; the readable `prefix+branch_id+suffix` portion is
    truncated to whatever space remains, so an arbitrarily long suffix can
    never truncate the token itself and cause a collision.
    """
    token = uuid.uuid4().hex
    reserved = len(_PERIOD_CODE_SEPARATOR) + len(token)
    max_readable_len = max(0, _PERIOD_CODE_MAX_LEN - reserved)

    fixed_part = f"{_PERIOD_CODE_PREFIX}{branch_id}"
    if len(fixed_part) > max_readable_len:
        fixed_part = fixed_part[:max_readable_len]
        readable_suffix = ""
    else:
        available_for_suffix = max_readable_len - len(fixed_part)
        readable_suffix = suffix[:available_for_suffix]

    code = f"{fixed_part}{readable_suffix}{_PERIOD_CODE_SEPARATOR}{token}"
    assert len(code) <= _PERIOD_CODE_MAX_LEN, (
        f"test bug: generated PeriodCode {code!r} ({len(code)} chars) exceeds "
        f"VARCHAR({_PERIOD_CODE_MAX_LEN})"
    )
    assert code.endswith(token), (
        f"test bug: generated PeriodCode {code!r} does not end with the complete "
        f"UUID token {token!r} -- the token must never be truncated"
    )
    return code


async def _recover_period_id(db, company_id: int, branch_id: int, period_code: str) -> int | None:
    """Tenant-safe recovery lookup scoped by the exact (CompanyID, BranchID,
    PeriodCode) tuple -- never PeriodCode alone, never a broad fallback."""
    rows = (await db.execute(
        _text("""
            SELECT payrollperiodid FROM payroll.payrollperiods
            WHERE companyid = :cid AND branchid = :bid AND periodcode = :code
        """),
        {"cid": company_id, "bid": branch_id, "code": period_code},
    )).mappings().all()
    if len(rows) > 1:
        raise AssertionError(
            f"Recovery lookup for period {period_code!r} (company={company_id}, "
            f"branch={branch_id}) matched {len(rows)} rows; expected at most one -- "
            f"refusing to guess which to clean up"
        )
    return rows[0]["payrollperiodid"] if rows else None


async def _cancel_active_periods(client: httpx.AsyncClient, token: str, branch_id: int, db) -> None:
    await db.execute(
        _text(
            "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
            "WHERE branchid = :bid AND status IN ('InReview', 'Approved', 'Returned') "
            "AND periodcode LIKE :prefix"
        ),
        {"bid": branch_id, "prefix": f"{_PERIOD_CODE_PREFIX}%"},
    )
    headers = auth(token)
    for s in ("Draft", "Open"):
        resp = await client.get("/payroll/periods", params={"branch_id": branch_id, "status": s}, headers=headers)
        if resp.status_code != 200:
            continue
        for p in resp.json():
            if not p.get("period_code", "").startswith(_PERIOD_CODE_PREFIX):
                continue
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"}, headers=headers,
            )


async def _delete_period_and_children(db, period_id: int) -> None:
    """
    Hard-deletes exactly this test-owned period and everything under it.
    `add_draft_line`/`add_period_pay_line`/bonus-event creation each write a
    real AuditLog row (`_write_line_audit`, entity_name='PayrollDraftLines'
    or 'PayrollBonusEvents') immediately after insert -- this captures the
    exact DraftLineIDs/BonusEventIDs for this period BEFORE the rows
    themselves are deleted, deletes their matching audit rows by exact
    EntityID (never a broad company/branch/time-window delete), then
    asserts zero residue for every row and audit type this period could
    have produced.
    """
    draft_line_ids = [
        r["draftlineid"] for r in (await db.execute(
            _text("SELECT draftlineid FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )).mappings().all()
    ]
    bonus_event_ids = [
        r["payrollbonuseventid"] for r in (await db.execute(
            _text("SELECT payrollbonuseventid FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )).mappings().all()
    ]
    draft_line_id_strs = [str(i) for i in draft_line_ids] or [""]
    bonus_event_id_strs = [str(i) for i in bonus_event_ids] or [""]

    runner = _CleanupRunner()
    await runner.execute(db,
        "DELETE FROM audit.auditlog WHERE entityname = 'PayrollPeriods' AND entityid = :eid",
        {"eid": str(period_id)}, label="delete PayrollPeriods audit")
    await runner.execute(db,
        "DELETE FROM audit.auditlog WHERE entityname = 'PayrollDraftLines' AND entityid = ANY(:ids)",
        {"ids": draft_line_id_strs}, label="delete PayrollDraftLines audit")
    await runner.execute(db,
        "DELETE FROM audit.auditlog WHERE entityname = 'PayrollBonusEvents' AND entityid = ANY(:ids)",
        {"ids": bonus_event_id_strs}, label="delete PayrollBonusEvents audit")
    await runner.execute(db,
        "DELETE FROM payroll.payrollperioddriverdayentrystate WHERE payrollperiodid = :pid",
        {"pid": period_id}, label="delete PPDES rows")
    await runner.execute(db,
        "DELETE FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid",
        {"pid": period_id}, label="delete PayrollBonusEvents rows")
    await runner.execute(db,
        "DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = :pid",
        {"pid": period_id}, label="delete PayrollFinalLines rows")
    await runner.execute(db,
        "DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid",
        {"pid": period_id}, label="delete PayrollDraftLines rows")
    await runner.execute(db,
        "DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid",
        {"pid": period_id}, label="delete PayrollPeriods row")

    async def _residue():
        residue = (await db.execute(
            _text("""
                SELECT
                    (SELECT COUNT(*) FROM payroll.payrollperiods WHERE payrollperiodid = :pid) AS periods,
                    (SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid) AS draftlines,
                    (SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid) AS finallines,
                    (SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid) AS bonusevents,
                    (SELECT COUNT(*) FROM payroll.payrollperioddriverdayentrystate WHERE payrollperiodid = :pid) AS ppdes,
                    (SELECT COUNT(*) FROM audit.auditlog
                        WHERE entityname = 'PayrollPeriods' AND entityid = :eid) AS period_audit,
                    (SELECT COUNT(*) FROM audit.auditlog
                        WHERE entityname = 'PayrollDraftLines' AND entityid = ANY(:dlids)) AS draftline_audit,
                    (SELECT COUNT(*) FROM audit.auditlog
                        WHERE entityname = 'PayrollBonusEvents' AND entityid = ANY(:beids)) AS bonus_audit
            """),
            {"pid": period_id, "eid": str(period_id),
             "dlids": draft_line_id_strs, "beids": bonus_event_id_strs},
        )).mappings().first()
        assert (
            residue["periods"] == 0 and residue["draftlines"] == 0 and residue["finallines"] == 0
            and residue["bonusevents"] == 0 and residue["ppdes"] == 0
            and residue["period_audit"] == 0 and residue["draftline_audit"] == 0 and residue["bonus_audit"] == 0
        ), f"Residue check failed for period {period_id}: {dict(residue)}"

    await runner.check(_residue, label=f"period {period_id} residue assertion")
    runner.raise_if_any()


@contextlib.asynccontextmanager
async def _owned_period(
    session_client, auth_token, branch_id, direct_db, *, status="Open", suffix="",
    _inject_failure_after_write: bool = False,
    _inject_failure_after_returned_restoration: bool = False,
    _code_override: str | None = None,
):
    """
    Exception-safe period lifecycle. Ownership begins in this `try` BEFORE
    the INSERT is sent (P1-B fix: an earlier version assigned
    `pid = row["payrollperiodid"]` before entering any try/finally at all).

    `code` (the PeriodCode) is the pre-write recovery identity, known before
    the INSERT. `_inject_failure_after_write` is a test-local-only
    fault-injection hook (never referenced by production code) used by the
    acquisition-failure regression matrix to prove cleanup survives a
    failure strictly AFTER the write commits but BEFORE `pid` is extracted.
    """
    await _cancel_active_periods(session_client, auth_token, branch_id, direct_db)
    code = _code_override if _code_override is not None else _generate_unique_period_code(branch_id, suffix)
    insert_status = "Open" if status == "Returned" else status
    pid = None
    review_item_id = None
    try:
        row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, :status, :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """),
            {"bid": branch_id, "status": insert_status, "code": code, "name": f"CP4B {status}",
             "start": datetime.date.fromisoformat(PERIOD_START), "end": datetime.date.fromisoformat(PERIOD_END)},
        )).mappings().first()
        if _inject_failure_after_write:
            raise _DeliberateSetupFailure(
                "simulated failure after PayrollPeriod write committed, before payrollperiodid extracted"
            )
        assert row is not None, f"Could not create period {code!r}"
        pid = row["payrollperiodid"]

        if status == "Returned":
            # ck_PayrollPeriods_ReturnedPointerConsistency requires a non-null
            # CurrentReturnReviewItemID whenever Status='Returned' -- insert as
            # Open first (always constraint-safe), then flip status and set
            # the pointer together in one UPDATE.
            rev_row = (await direct_db.execute(
                _text("""
                    INSERT INTO review.managerreviewitems
                        (companyid, branchid, requesttype, entityschema, entityname, entityid, title, status)
                    VALUES (1, :bid, 'PeriodApproval', 'payroll', 'PayrollPeriods', :eid, 'CP4B test Returned pointer', 'Pending')
                    RETURNING reviewitemid
                """),
                {"bid": branch_id, "eid": str(pid)},
            )).mappings().first()
            review_item_id = rev_row["reviewitemid"]
            await direct_db.execute(
                _text("UPDATE payroll.payrollperiods SET status = 'Returned', currentreturnreviewitemid = :rid WHERE payrollperiodid = :pid"),
                {"rid": review_item_id, "pid": pid},
            )
        yield pid
    finally:
        # Returned teardown used to restore its review pointer with bare awaits.
        # A failed restoration then skipped every remaining period cleanup step.
        # Keep the pointer-clear before deleting the review item, but make every
        # independently useful operation run through one cleanup ledger.
        runner = _CleanupRunner()

        async def _recover_period():
            nonlocal pid
            recovered_pid = await _recover_period_id(direct_db, 1, branch_id, code)
            if recovered_pid is not None:
                pid = recovered_pid

        await runner.check(_recover_period, label="recover owned PayrollPeriod")

        if pid is not None:
            async def _recover_review_item():
                nonlocal review_item_id
                if review_item_id is not None:
                    return
                rrow = (await direct_db.execute(
                    _text("SELECT currentreturnreviewitemid FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                    {"pid": pid},
                )).mappings().first()
                if rrow is not None and rrow["currentreturnreviewitemid"] is not None:
                    review_item_id = rrow["currentreturnreviewitemid"]

            await runner.check(_recover_review_item, label="recover Returned review-item linkage")

            if review_item_id is not None:
                async def _restore_returned_pointer():
                    # ck_PayrollPeriods_ReturnedPointerConsistency is a
                    # biconditional: leave Returned and clear the pointer in
                    # the same UPDATE before removing the review item.
                    await direct_db.execute(
                        _text("UPDATE payroll.payrollperiods SET status = 'Open', currentreturnreviewitemid = NULL WHERE payrollperiodid = :pid"),
                        {"pid": pid},
                    )
                    if _inject_failure_after_returned_restoration:
                        raise _DeliberateSetupFailure(
                            "simulated failure after Returned pointer restoration during cleanup"
                        )

                await runner.check(_restore_returned_pointer, label="restore Returned review-item linkage")
                await runner.execute(
                    direct_db,
                    "DELETE FROM review.managerreviewitems WHERE reviewitemid = :id",
                    {"id": review_item_id},
                    label="delete Returned ManagerReviewItem",
                )

            # `_delete_period_and_children` has its own dependency-ordered
            # cleanup runner. Treat it as one outer independent operation so
            # a review-item cleanup failure cannot prevent its full sweep.
            await runner.check(
                lambda: _delete_period_and_children(direct_db, pid),
                label="delete owned PayrollPeriod and children",
            )

        runner.raise_if_any()


async def _returned_period_residue(direct_db, period_id: int, review_item_id: int) -> dict:
    """Exact-ID post-teardown check for the test-owned Returned linkage."""
    return (await direct_db.execute(
        _text("""
            SELECT
                (SELECT COUNT(*) FROM payroll.payrollperiods WHERE payrollperiodid = :pid) AS periods,
                (SELECT COUNT(*) FROM review.managerreviewitems WHERE reviewitemid = :rid) AS review_items,
                (SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid) AS draftlines,
                (SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid) AS finallines,
                (SELECT COUNT(*) FROM payroll.payrollperioddriverdayentrystate WHERE payrollperiodid = :pid) AS ppdes,
                (SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid) AS bonuses,
                (SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityname = 'PayrollPeriods' AND entityid = :period_entity_id) AS period_audit
        """),
        {"pid": period_id, "rid": review_item_id, "period_entity_id": str(period_id)},
    )).mappings().first()


# ---------------------------------------------------------------------------
# Driver / employee ownership
# ---------------------------------------------------------------------------

async def _delete_driver_and_residue(db, driver_id: int, employee_id: int | None) -> None:
    """
    Hard-deletes exactly the test-owned driver/employee pair and every
    dependent row scoped to this exact driverid -- idempotent, since each
    DELETE is a no-op when nothing matches. `core.drivers`/`core.employees`
    creation writes NO AuditLog row (confirmed by reading app/core/service.py
    -- no `entityname` literal for 'Drivers'/'Employees' exists anywhere in
    that module); the audit deletes below for those two entities are
    therefore defensive no-ops, kept so the residue assertion still covers
    the (currently always-zero) audit case explicitly rather than silently
    assuming it.

    This context unwinds BEFORE its enclosing `_owned_period` (LIFO nesting)
    and must delete this driver's DraftLines/BonusEvents here to satisfy
    fk_DraftLines_Driver/fk_BonusEvents_Driver before the driver row itself
    can be deleted -- which means `_delete_period_and_children`'s own later
    capture-then-delete audit sweep will find these rows already gone. This
    function therefore captures and deletes their exact PayrollDraftLines/
    PayrollBonusEvents audit rows itself, BEFORE removing the rows, so no
    audit entry is ever orphaned regardless of unwind order.
    """
    draft_line_ids = [
        r["draftlineid"] for r in (await db.execute(
            _text("SELECT draftlineid FROM payroll.payrolldraftlines WHERE driverid = :id"), {"id": driver_id},
        )).mappings().all()
    ]
    bonus_event_ids = [
        r["payrollbonuseventid"] for r in (await db.execute(
            _text("SELECT payrollbonuseventid FROM payroll.payrollbonusevents WHERE driverid = :id"), {"id": driver_id},
        )).mappings().all()
    ]
    pay_rule_ids = [
        r["driverpayruleid"] for r in (await db.execute(
            _text("SELECT driverpayruleid FROM payroll.driverpayrules WHERE driverid = :id"), {"id": driver_id},
        )).mappings().all()
    ]
    draft_line_id_strs = [str(i) for i in draft_line_ids] or [""]
    bonus_event_id_strs = [str(i) for i in bonus_event_ids] or [""]
    pay_rule_id_strs = [str(i) for i in pay_rule_ids] or [""]

    runner = _CleanupRunner()
    await runner.execute(db,
        "DELETE FROM audit.auditlog WHERE entityname = 'PayrollDraftLines' AND entityid = ANY(:ids)",
        {"ids": draft_line_id_strs}, label="delete driver-scoped PayrollDraftLines audit")
    await runner.execute(db,
        "DELETE FROM audit.auditlog WHERE entityname = 'PayrollBonusEvents' AND entityid = ANY(:ids)",
        {"ids": bonus_event_id_strs}, label="delete driver-scoped PayrollBonusEvents audit")
    await runner.execute(db,
        "DELETE FROM audit.auditlog WHERE entityname = 'DriverPayRules' AND entityid = ANY(:ids)",
        {"ids": pay_rule_id_strs}, label="delete driver-scoped DriverPayRules audit")
    await runner.execute(db, "DELETE FROM payroll.payrollfinallines WHERE driverid = :id", {"id": driver_id}, label="delete FinalLines")
    await runner.execute(db, "DELETE FROM payroll.payrolldraftlines WHERE driverid = :id", {"id": driver_id}, label="delete DraftLines")
    await runner.execute(db, "DELETE FROM payroll.payrollbonusevents WHERE driverid = :id", {"id": driver_id}, label="delete BonusEvents")
    await runner.execute(db, "DELETE FROM payroll.payrollperioddriverdayentrystate WHERE driverid = :id", {"id": driver_id}, label="delete PPDES")
    await runner.execute(db, "DELETE FROM payroll.driverrates WHERE driverid = :id", {"id": driver_id}, label="delete DriverRates")
    await runner.execute(db, "DELETE FROM payroll.driverpayrules WHERE driverid = :id", {"id": driver_id}, label="delete DriverPayRules")
    await runner.execute(db,
        "DELETE FROM audit.auditlog WHERE entityname = 'Drivers' AND entityid = :eid", {"eid": str(driver_id)},
        label="delete Drivers audit")
    await runner.execute(db, "DELETE FROM core.drivers WHERE driverid = :id", {"id": driver_id}, label="delete Driver row")
    if employee_id is not None:
        await runner.execute(db,
            "DELETE FROM audit.auditlog WHERE entityname = 'Employees' AND entityid = :eid", {"eid": str(employee_id)},
            label="delete Employees audit")
        await runner.execute(db, "DELETE FROM core.employees WHERE employeeid = :id", {"id": employee_id}, label="delete Employee row")
    runner.raise_if_any()


@contextlib.asynccontextmanager
async def _owned_driver_and_employee(
    session_client, auth_token, branch_id, direct_db, *, name,
    _inject_failure_after_write: bool = False,
):
    """
    Exception-safe driver lifecycle. `name` is combined with a `uuid4`
    suffix to form `marker` -- a globally-unique recovery key chosen BEFORE
    the write -- so the created driver/employee can be recovered even when
    the local `driver_id`/`employee_id` variables were never assigned (a
    201 response whose body then fails to parse still leaves the row
    committed server-side; recovery does not depend on reaching that
    assignment).
    """
    marker = f"{name} {uuid.uuid4().hex[:8]}"
    driver_id = None
    employee_id = None
    try:
        r = await session_client.post(
            "/core/drivers", json={"branch_id": branch_id, "full_name": marker}, headers=auth(auth_token),
        )
        assert r.status_code == 201, f"Create driver failed: {r.text}"
        if _inject_failure_after_write:
            raise _DeliberateSetupFailure(
                "simulated failure after Driver/Employee write committed, before driver_id extracted"
            )
        body = r.json()
        driver_id = body["driver_id"]
        employee_id = body.get("employee_id")
        yield driver_id
    finally:
        if driver_id is None or employee_id is None:
            row = (await direct_db.execute(
                _text("""
                    SELECT d.driverid, d.employeeid
                    FROM   core.drivers d
                    JOIN   core.employees e ON e.employeeid = d.employeeid
                    WHERE  e.fullname = :name AND d.branchid = :bid
                """),
                {"name": marker, "bid": branch_id},
            )).mappings().first()
            if row is not None:
                driver_id = row["driverid"]
                employee_id = row["employeeid"]
        if driver_id is not None:
            await _delete_driver_and_residue(direct_db, driver_id, employee_id)
            residue = (await direct_db.execute(
                _text("""
                    SELECT
                        (SELECT COUNT(*) FROM core.drivers WHERE driverid = :did) AS drivers,
                        (SELECT COUNT(*) FROM core.employees WHERE employeeid = :eid) AS employees,
                        (SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE driverid = :did) AS draftlines,
                        (SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE driverid = :did) AS bonusevents,
                        (SELECT COUNT(*) FROM payroll.driverpayrules WHERE driverid = :did) AS payrules,
                        (SELECT COUNT(*) FROM audit.auditlog
                            WHERE entityname = 'Drivers' AND entityid = :did_s) AS driver_audit,
                        (SELECT COUNT(*) FROM audit.auditlog
                            WHERE entityname = 'Employees' AND entityid = :eid_s) AS employee_audit
                """),
                {
                    "did": driver_id, "eid": employee_id if employee_id is not None else -1,
                    "did_s": str(driver_id), "eid_s": str(employee_id) if employee_id is not None else "-1",
                },
            )).mappings().first()
            assert residue["drivers"] == 0, f"Driver {driver_id} was not removed"
            assert residue["draftlines"] == 0, f"DraftLine rows for driver {driver_id} were not removed"
            assert residue["bonusevents"] == 0, f"BonusEvent rows for driver {driver_id} were not removed"
            assert residue["payrules"] == 0, f"DriverPayRule rows for driver {driver_id} were not removed"
            assert residue["driver_audit"] == 0, f"Driver {driver_id} audit residue was not removed"
            if employee_id is not None:
                assert residue["employees"] == 0, f"Employee {employee_id} was not removed"
                assert residue["employee_audit"] == 0, f"Employee {employee_id} audit residue was not removed"


async def _get_hourly_rate_type_id(client: httpx.AsyncClient, token: str) -> int:
    rv = await client.get("/payroll/rate-types", headers=auth(token))
    assert rv.status_code == 200
    for rt in rv.json():
        if rt["rate_code"] == "HOURLY":
            return rt["rate_type_id"]
    raise AssertionError("HOURLY rate type not found")


async def _get_status_pay_rate_type_id(client: httpx.AsyncClient, token: str) -> int:
    rv = await client.get("/payroll/rate-types", headers=auth(token))
    assert rv.status_code == 200
    for rt in rv.json():
        if rt["rate_code"] == "STATUS_PAY":
            return rt["rate_type_id"]
    raise AssertionError("STATUS_PAY rate type not found")


@contextlib.asynccontextmanager
async def _owned_driver_rate(
    direct_db, session_client, auth_token, driver_id, rate_type_id, amount, effective_from,
    *, _inject_failure_after_write: bool = False,
):
    """
    Exception-safe DriverRate lifecycle. Ownership begins BEFORE the
    create-rate POST is sent. Recovery key: the exact
    (driver_id, rate_type_id, effective_from) tuple, plus a unique `notes`
    marker (a real, schema-supported field on DriverRateCreate) as a second,
    independent recovery signal -- either recovers the created
    DriverRateID even if the HTTP response was never successfully parsed.
    """
    marker = f"CP4B-RATE-{uuid.uuid4().hex[:8]}"
    eff_date = datetime.date.fromisoformat(effective_from)
    rate_id = None
    try:
        rc = await session_client.post(
            "/payroll/rates",
            json={"driver_id": driver_id, "rate_type_id": rate_type_id, "amount": amount,
                  "effective_from": effective_from, "notes": marker},
            headers=auth(auth_token),
        )
        assert rc.status_code == 201, f"Create rate failed: {rc.text}"
        if _inject_failure_after_write:
            raise _DeliberateSetupFailure(
                "simulated failure after DriverRate write committed, before rate_id extracted"
            )
        rate_id = rc.json()["driver_rate_id"]
        ra = await session_client.post(f"/payroll/rates/{rate_id}/approve", headers=auth(auth_token))
        assert ra.status_code == 200, f"Approve rate failed: {ra.text}"
        yield rate_id
    finally:
        if rate_id is None:
            row = (await direct_db.execute(
                _text("""
                    SELECT driverrateid FROM payroll.driverrates
                    WHERE driverid = :did AND ratetypeid = :rtid
                      AND (effectivefrom = :eff OR notes = :marker)
                """),
                {"did": driver_id, "rtid": rate_type_id, "eff": eff_date, "marker": marker},
            )).mappings().first()
            if row is not None:
                rate_id = row["driverrateid"]
        if rate_id is not None:
            runner = _CleanupRunner()
            await runner.execute(direct_db,
                "DELETE FROM audit.auditlog WHERE entityname = 'DriverRates' AND entityid = :eid",
                {"eid": str(rate_id)}, label="delete DriverRates audit")
            await runner.execute(direct_db,
                "DELETE FROM payroll.driverrates WHERE driverrateid = :id", {"id": rate_id},
                label="delete DriverRates row")

            async def _residue():
                residue = (await direct_db.execute(
                    _text("""
                        SELECT
                            (SELECT COUNT(*) FROM payroll.driverrates WHERE driverrateid = :id) AS rates,
                            (SELECT COUNT(*) FROM audit.auditlog
                                WHERE entityname = 'DriverRates' AND entityid = :eid) AS rate_audit
                    """),
                    {"id": rate_id, "eid": str(rate_id)},
                )).mappings().first()
                assert residue["rates"] == 0, f"DriverRate {rate_id} was not removed"
                assert residue["rate_audit"] == 0, f"DriverRate {rate_id} audit residue was not removed"

            await runner.check(_residue, label=f"DriverRate {rate_id} residue assertion")
            runner.raise_if_any()


# ---------------------------------------------------------------------------
# Status configuration ownership (StatusRateColumns / PayrollStatusKeys) --
# neither table has any AuditLog writer anywhere in service.py (confirmed by
# source inspection: no `entityname` literal for either table exists), so
# no audit cleanup step applies; the residue proof below still explicitly
# checks the (always-zero) audit case rather than assuming it.
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def _status_rate_column(
    direct_db, company_id: int, branch_id: int,
    *, _inject_failure_after_write: bool = False,
):
    """Reuses an existing active StatusRateColumn if present; otherwise
    creates and hard-deletes a uniquely-marked test-owned fallback row.
    Ownership begins before the INSERT; `marker` (ColumnName) is the
    pre-write recovery identity."""
    existing = (await direct_db.execute(
        _text("""
            SELECT statusratecolumnid FROM payroll.statusratecolumns
            WHERE  branchid = :bid AND companyid = :cid AND isactive = TRUE
            ORDER BY isdefault DESC, statusratecolumnid LIMIT 1
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    if existing:
        yield existing["statusratecolumnid"]
        return

    rt_row = (await direct_db.execute(
        _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
    )).mappings().first()
    marker = f"CP4B-SRC-{uuid.uuid4().hex[:8]}"
    src_col_id = None
    try:
        row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.statusratecolumns
                    (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
                VALUES (:cid, :bid, :rtid, :marker, :norm, TRUE, TRUE)
                RETURNING statusratecolumnid
            """),
            {"cid": company_id, "bid": branch_id, "rtid": rt_row["ratetypeid"], "marker": marker, "norm": marker.upper()},
        )).mappings().first()
        if _inject_failure_after_write:
            raise _DeliberateSetupFailure(
                "simulated failure after StatusRateColumn write committed, before statusratecolumnid extracted"
            )
        src_col_id = row["statusratecolumnid"]
        yield src_col_id
    finally:
        if src_col_id is None:
            row = (await direct_db.execute(
                _text("SELECT statusratecolumnid FROM payroll.statusratecolumns WHERE columnname = :m"),
                {"m": marker},
            )).mappings().first()
            if row is not None:
                src_col_id = row["statusratecolumnid"]
        if src_col_id is not None:
            runner = _CleanupRunner()
            await runner.execute(direct_db,
                "DELETE FROM payroll.statusratecolumns WHERE statusratecolumnid = :id", {"id": src_col_id},
                label="delete StatusRateColumn row")

            async def _residue():
                residue = (await direct_db.execute(
                    _text("SELECT COUNT(*) AS c FROM payroll.statusratecolumns WHERE statusratecolumnid = :id"),
                    {"id": src_col_id},
                )).mappings().first()
                assert residue["c"] == 0, f"StatusRateColumn {src_col_id} was not removed"

            await runner.check(_residue, label=f"StatusRateColumn {src_col_id} residue assertion")
            runner.raise_if_any()


@contextlib.asynccontextmanager
async def _owned_status_key(
    direct_db, company_id, branch_id, code, hours, status_rate_column_id,
    *, _inject_failure_after_write: bool = False,
):
    """Ownership begins before the INSERT; `code` (StatusCode) is the
    pre-write recovery identity, unique per caller."""
    status_key_id = None
    try:
        row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollstatuskeys
                    (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                     isoffreason, hoursvalue, isactive, displayorder, statusratecolumnid)
                VALUES (:cid, :bid, :code, :norm, :name, FALSE, :hours, TRUE, 99, :src_col)
                RETURNING statuskeyid
            """),
            {"cid": company_id, "bid": branch_id, "code": code, "norm": code.upper(),
             "name": f"CP4B {code}", "hours": Decimal(hours), "src_col": status_rate_column_id},
        )).mappings().first()
        if _inject_failure_after_write:
            raise _DeliberateSetupFailure(
                "simulated failure after StatusKey write committed, before statuskeyid extracted"
            )
        status_key_id = row["statuskeyid"]
        yield status_key_id
    finally:
        if status_key_id is None:
            row = (await direct_db.execute(
                _text("SELECT statuskeyid FROM payroll.payrollstatuskeys WHERE statuscode = :code"),
                {"code": code},
            )).mappings().first()
            if row is not None:
                status_key_id = row["statuskeyid"]
        if status_key_id is not None:
            # This context may be nested inside a period context that has not
            # unwound yet -- any PPDES rows still referencing this StatusKeyID
            # must be cleared first, or the DELETE below violates the RESTRICT
            # FK fk_PPDES_StatusKey.
            runner = _CleanupRunner()
            await runner.execute(direct_db,
                "DELETE FROM payroll.payrollperioddriverdayentrystate WHERE statuskeyid = :id",
                {"id": status_key_id}, label="delete referencing PPDES rows")
            await runner.execute(direct_db,
                "DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :id", {"id": status_key_id},
                label="delete StatusKey row")

            async def _residue():
                residue = (await direct_db.execute(
                    _text("SELECT COUNT(*) AS c FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": status_key_id},
                )).mappings().first()
                assert residue["c"] == 0, f"StatusKey {status_key_id} was not removed"

            await runner.check(_residue, label=f"StatusKey {status_key_id} residue assertion")
            runner.raise_if_any()


async def _save_day_grid_status(session_client, auth_token, period_id, driver_id, work_date, status_key_code):
    r = await session_client.post(
        f"/payroll/periods/{period_id}/day-grid",
        json={"work_date": work_date, "rows": [{"driver_id": driver_id, "status_key": status_key_code, "values": {}}]},
        headers=auth(auth_token),
    )
    assert r.status_code == 200, f"Day grid save failed: {r.text}"


# ---------------------------------------------------------------------------
# DraftLine / BonusEvent ownership -- used by the acquisition-failure
# regression matrix. Rows created inline elsewhere in this file (e.g. the
# business-logic tests below) remain covered by `_owned_period`'s own
# comprehensive, exact-ID audit + row cleanup (see `_delete_period_and_children`
# above) -- one owner covering every DraftLine/BonusEvent a period's scenario
# produces, rather than a separate wrapper at every call site.
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def _owned_draft_line(
    session_client, auth_token, direct_db, *, period_id, driver_id, work_date, line_type, quantity="1.0000",
    _inject_failure_after_write: bool = False,
):
    """Recovery key: the exact (period_id, driver_id, work_date, line_type)
    tuple -- the real business key enforced by the active-Daily uniqueness
    index -- known before the write."""
    wdate = datetime.date.fromisoformat(work_date)
    draft_line_id = None
    try:
        r = await session_client.post(
            f"/payroll/periods/{period_id}/lines",
            json={"driver_id": driver_id, "work_date": work_date, "line_type": line_type, "quantity": quantity},
            headers=auth(auth_token),
        )
        assert r.status_code == 201, f"Add draft line failed: {r.text}"
        if _inject_failure_after_write:
            raise _DeliberateSetupFailure(
                "simulated failure after PayrollDraftLine write committed, before draft_line_id extracted"
            )
        draft_line_id = r.json()["draft_line_id"]
        yield draft_line_id
    finally:
        if draft_line_id is None:
            row = (await direct_db.execute(
                _text("""
                    SELECT draftlineid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverid = :did
                      AND workdate = :wd AND linetype = :lt AND status != 'Void'
                    ORDER BY draftlineid DESC LIMIT 1
                """),
                {"pid": period_id, "did": driver_id, "wd": wdate, "lt": line_type},
            )).mappings().first()
            if row is not None:
                draft_line_id = row["draftlineid"]
        if draft_line_id is not None:
            runner = _CleanupRunner()
            await runner.execute(direct_db,
                "DELETE FROM audit.auditlog WHERE entityname = 'PayrollDraftLines' AND entityid = :eid",
                {"eid": str(draft_line_id)}, label="delete PayrollDraftLines audit")
            await runner.execute(direct_db,
                "DELETE FROM payroll.payrolldraftlines WHERE draftlineid = :id", {"id": draft_line_id},
                label="delete PayrollDraftLines row")

            async def _residue():
                residue = (await direct_db.execute(
                    _text("""
                        SELECT
                            (SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE draftlineid = :id) AS lines,
                            (SELECT COUNT(*) FROM audit.auditlog
                                WHERE entityname = 'PayrollDraftLines' AND entityid = :eid) AS line_audit
                    """),
                    {"id": draft_line_id, "eid": str(draft_line_id)},
                )).mappings().first()
                assert residue["lines"] == 0, f"DraftLine {draft_line_id} was not removed"
                assert residue["line_audit"] == 0, f"DraftLine {draft_line_id} audit residue was not removed"

            await runner.check(_residue, label=f"DraftLine {draft_line_id} residue assertion")
            runner.raise_if_any()


@contextlib.asynccontextmanager
async def _owned_bonus_event(
    session_client, auth_token, direct_db, *, period_id, driver_id, amount="10.00",
    _inject_failure_after_write: bool = False,
):
    """Recovery key: a unique `notes` marker (a real, schema-supported field
    on BonusEventCreate) written before the write."""
    marker = f"CP4B-BONUS-{uuid.uuid4().hex[:8]}"
    bonus_id = None
    try:
        r = await session_client.post(
            f"/payroll/periods/{period_id}/bonuses",
            json={"driver_id": driver_id, "amount": amount, "reason": "acquisition regression", "notes": marker},
            headers=auth(auth_token),
        )
        assert r.status_code == 201, f"Add bonus event failed: {r.text}"
        if _inject_failure_after_write:
            raise _DeliberateSetupFailure(
                "simulated failure after PayrollBonusEvent write committed, before bonus_event_id extracted"
            )
        bonus_id = r.json()["bonus_event_id"]
        yield bonus_id
    finally:
        if bonus_id is None:
            row = (await direct_db.execute(
                _text("SELECT payrollbonuseventid FROM payroll.payrollbonusevents WHERE notes = :m"),
                {"m": marker},
            )).mappings().first()
            if row is not None:
                bonus_id = row["payrollbonuseventid"]
        if bonus_id is not None:
            runner = _CleanupRunner()
            await runner.execute(direct_db,
                "DELETE FROM audit.auditlog WHERE entityname = 'PayrollBonusEvents' AND entityid = :eid",
                {"eid": str(bonus_id)}, label="delete PayrollBonusEvents audit")
            await runner.execute(direct_db,
                "DELETE FROM payroll.payrollbonusevents WHERE payrollbonuseventid = :id", {"id": bonus_id},
                label="delete PayrollBonusEvents row")

            async def _residue():
                residue = (await direct_db.execute(
                    _text("""
                        SELECT
                            (SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE payrollbonuseventid = :id) AS events,
                            (SELECT COUNT(*) FROM audit.auditlog
                                WHERE entityname = 'PayrollBonusEvents' AND entityid = :eid) AS event_audit
                    """),
                    {"id": bonus_id, "eid": str(bonus_id)},
                )).mappings().first()
                assert residue["events"] == 0, f"BonusEvent {bonus_id} was not removed"
                assert residue["event_audit"] == 0, f"BonusEvent {bonus_id} audit residue was not removed"

            await runner.check(_residue, label=f"BonusEvent {bonus_id} residue assertion")
            runner.raise_if_any()


# ---------------------------------------------------------------------------
# DriverPayRule ownership (Codex P1 fix) -- replaces the previous inline
# create/void/delete sequence duplicated in both the minimum- and maximum-
# pay tests. `DriverPayRules` writes a real AuditLog row via
# `_write_pay_rule_audit` (entity_name='DriverPayRules', entity_id=
# str(rule_id)), confirmed by reading app/payroll/service.py.
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def _owned_driver_pay_rule(
    session_client, auth_token, direct_db, *, driver_id, branch_id, rule_type, amount,
    effective_from, effective_to=None,
    _inject_failure_after_write: bool = False,
):
    """Recovery key: a unique `notes` marker (a real, schema-supported field
    on DriverPayRuleCreate) written before the write. Never touches a
    pre-existing/shared DriverPayRule -- recovery is scoped to this exact
    driver_id + marker."""
    marker = f"CP4B-PAYRULE-{uuid.uuid4().hex[:8]}"
    rule_id = None
    try:
        body = {
            "driver_id": driver_id, "branch_id": branch_id, "rule_type": rule_type,
            "amount": amount, "effective_from": effective_from, "notes": marker,
        }
        if effective_to is not None:
            body["effective_to"] = effective_to
        r = await session_client.post("/payroll/driver-pay-rules", json=body, headers=auth(auth_token))
        assert r.status_code == 201, f"Create DriverPayRule failed: {r.text}"
        if _inject_failure_after_write:
            raise _DeliberateSetupFailure(
                "simulated failure after DriverPayRule write committed, before rule_id extracted"
            )
        rule_id = r.json()["driver_pay_rule_id"]
        yield rule_id
    finally:
        if rule_id is None:
            row = (await direct_db.execute(
                _text("SELECT driverpayruleid FROM payroll.driverpayrules WHERE driverid = :did AND notes = :m"),
                {"did": driver_id, "m": marker},
            )).mappings().first()
            if row is not None:
                rule_id = row["driverpayruleid"]
        if rule_id is not None:
            runner = _CleanupRunner()
            await runner.execute(direct_db,
                "DELETE FROM audit.auditlog WHERE entityname = 'DriverPayRules' AND entityid = :eid",
                {"eid": str(rule_id)}, label="delete DriverPayRules audit")
            await runner.execute(direct_db,
                "DELETE FROM payroll.driverpayrules WHERE driverpayruleid = :id", {"id": rule_id},
                label="delete DriverPayRules row")

            async def _residue():
                residue = (await direct_db.execute(
                    _text("""
                        SELECT
                            (SELECT COUNT(*) FROM payroll.driverpayrules WHERE driverpayruleid = :id) AS rules,
                            (SELECT COUNT(*) FROM audit.auditlog
                                WHERE entityname = 'DriverPayRules' AND entityid = :eid) AS rule_audit
                    """),
                    {"id": rule_id, "eid": str(rule_id)},
                )).mappings().first()
                assert residue["rules"] == 0, f"DriverPayRule {rule_id} was not removed"
                assert residue["rule_audit"] == 0, f"DriverPayRule {rule_id} audit residue was not removed"

            await runner.check(_residue, label=f"DriverPayRule {rule_id} residue assertion")
            runner.raise_if_any()


async def _get_full_db_snapshot(direct_db, period_id: int, driver_ids: list | None = None) -> dict:
    """
    Exact-ID snapshot of every table + entity CP-4B reads and must never
    mutate: PayrollPeriods, PayrollDraftLines, PayrollFinalLines, PPDES,
    PayrollBonusEvents, DriverRates (for the tracked drivers -- CP-4B reads
    these for both PerUnit and live-Status rate resolution), and full audit
    rows (EntityName/EntityID/CompanyID/BranchID/ActionCode/old+new value,
    not merely a count) for every entity type the preview touches, taken in
    a FRESH read after the HTTP request (never relying on transaction
    rollback to mask writes).
    """
    driver_ids = driver_ids or []
    period = dict((await direct_db.execute(
        _text("SELECT * FROM payroll.payrollperiods WHERE payrollperiodid = :pid"), {"pid": period_id},
    )).mappings().first())
    draftlines = [dict(r) for r in (await direct_db.execute(
        _text("SELECT * FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid ORDER BY draftlineid"),
        {"pid": period_id},
    )).mappings().all()]
    finallines = [dict(r) for r in (await direct_db.execute(
        _text("SELECT * FROM payroll.payrollfinallines WHERE payrollperiodid = :pid ORDER BY finallineid"),
        {"pid": period_id},
    )).mappings().all()]
    ppdes = [dict(r) for r in (await direct_db.execute(
        _text("SELECT * FROM payroll.payrollperioddriverdayentrystate WHERE payrollperiodid = :pid ORDER BY payrollperioddriverdayentrystateid"),
        {"pid": period_id},
    )).mappings().all()]
    bonus = [dict(r) for r in (await direct_db.execute(
        _text("SELECT * FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid ORDER BY payrollbonuseventid"),
        {"pid": period_id},
    )).mappings().all()]
    driverrates = []
    if driver_ids:
        driverrates = [dict(r) for r in (await direct_db.execute(
            _text("SELECT * FROM payroll.driverrates WHERE driverid = ANY(:dids) ORDER BY driverrateid"),
            {"dids": driver_ids},
        )).mappings().all()]
    draftline_ids = [str(r["draftlineid"]) for r in draftlines] or [""]
    bonus_ids = [str(r["payrollbonuseventid"]) for r in bonus] or [""]
    driverrate_ids = [str(r["driverrateid"]) for r in driverrates] or [""]
    audit_rows = [dict(r) for r in (await direct_db.execute(
        _text("""
            SELECT auditid, entityname, entityid, companyid, branchid,
                   actioncode, oldvaluejson, newvaluejson
            FROM   audit.auditlog
            WHERE  (entityname = 'PayrollPeriods'      AND entityid = :pid_str)
               OR  (entityname = 'PayrollDraftLines'    AND entityid = ANY(:draftline_ids))
               OR  (entityname = 'PayrollBonusEvents'   AND entityid = ANY(:bonus_ids))
               OR  (entityname = 'DriverRates'          AND entityid = ANY(:driverrate_ids))
            ORDER BY auditid
        """),
        {"pid_str": str(period_id), "draftline_ids": draftline_ids,
         "bonus_ids": bonus_ids, "driverrate_ids": driverrate_ids},
    )).mappings().all()]
    return {
        "period": period, "draftlines": draftlines, "finallines": finallines,
        "ppdes": ppdes, "bonus": bonus, "driverrates": driverrates, "audit_rows": audit_rows,
    }


# ---------------------------------------------------------------------------
# Permission-fixture ownership (Codex P1-A/P1-B fix, this pass)
#
# `_PermissionOwnership` is entered BEFORE any write. Every field is
# registered the moment its ID is known, or recovered by exact marker/
# composite key in `cleanup()` when the normal code path never reached that
# assignment (e.g. a 201 whose body then fails to parse). `cleanup()` itself
# performs the FK-safe delete + exact-EntityName/EntityID audit delete +
# residue assertion -- the authoritative, non-vacuous proof -- so every
# caller of `_owned_permission_principal` gets this guarantee automatically,
# not only the dedicated regression tests below.
#
# Real production audit-entity identities used here (confirmed by reading
# app/admin/service.py's `_write_admin_audit` call sites): entity_name
# 'Users' (entity_id=str(user_id)), 'CompanyRoles' (entity_id=str(role_id)),
# 'CompanyRolePermissions' (entity_id=str(role_id), written only by the
# `/permissions` PUT endpoint -- a direct-SQL INSERT bypasses this and
# writes no audit row), 'UserBranchRoles' (entity_id=str(assignment_id)).
# A pre-existing/shared role (e.g. the seeded 'DRIVER' CompanyRole) is
# passed in via `existing_company_role_id` and is NEVER tracked or deleted.
# ---------------------------------------------------------------------------

class _PermissionOwnership:
    def __init__(self, direct_db, marker: str, *, owns_role: bool, known_role_id: int | None = None):
        self.direct_db = direct_db
        self.marker = marker
        self.owns_role = owns_role          # False => role is shared/seeded, never deleted
        self.role_id: int | None = known_role_id
        self.user_id: int | None = None
        self.assignment_id: int | None = None

    async def cleanup(self) -> None:
        db = self.direct_db

        # --- Recovery: fill in any ID the normal code path never reached ---
        if self.owns_role and self.role_id is None:
            row = (await db.execute(
                _text("SELECT companyroleid FROM sec.companyroles WHERE rolename = :m"), {"m": self.marker},
            )).mappings().first()
            if row is not None:
                self.role_id = row["companyroleid"]
        if self.user_id is None:
            row = (await db.execute(
                _text("SELECT userid FROM sec.users WHERE username = :m"), {"m": self.marker},
            )).mappings().first()
            if row is not None:
                self.user_id = row["userid"]
        if self.assignment_id is None and self.user_id is not None:
            row = (await db.execute(
                _text("""
                    SELECT userbranchroleid FROM sec.userbranchroles
                    WHERE userid = :uid AND (companyroleid = :rid OR :rid IS NULL)
                    ORDER BY userbranchroleid DESC LIMIT 1
                """),
                {"uid": self.user_id, "rid": self.role_id},
            )).mappings().first()
            if row is not None:
                self.assignment_id = row["userbranchroleid"]

        runner = _CleanupRunner()

        # 0. Unlink any ODA driver-profile pointer first so a nested driver/
        #    employee ownership context (unwinds after this one, LIFO) is
        #    not blocked by fk_Users_Employee.
        if self.user_id is not None:
            await runner.execute(db, "UPDATE sec.users SET employeeid = NULL WHERE userid = :uid",
                                  {"uid": self.user_id}, label="unlink ODA employee pointer")

        # 1. Permission overrides (always test-owned/per-user; never shared).
        # `set_user_permission_overrides` (app/admin/service.py) writes exactly
        # one AuditLog row per call: entity_name='UserPermissionOverrides',
        # entity_id=str(target_user_id) -- NOT the override row's own ID, NOT
        # the permission code. Confirmed by reading the production writer.
        # The audit row is captured/deleted by exact EntityName+EntityID
        # BEFORE the override rows themselves, per the required ordering.
        if self.user_id is not None:
            await runner.execute(db,
                "DELETE FROM audit.auditlog WHERE entityname = 'UserPermissionOverrides' AND entityid = :eid",
                {"eid": str(self.user_id)}, label="delete UserPermissionOverrides audit")
            await runner.execute(db, "DELETE FROM sec.userpermissionoverrides WHERE userid = :uid",
                                  {"uid": self.user_id}, label="delete UserPermissionOverrides rows")

        # 2. UserBranchRoles (FK to Users + CompanyRoles).
        if self.user_id is not None:
            await runner.execute(db, "DELETE FROM sec.userbranchroles WHERE userid = :uid",
                                  {"uid": self.user_id}, label="delete UserBranchRoles by user")
        if self.owns_role and self.role_id is not None:
            await runner.execute(db, "DELETE FROM sec.userbranchroles WHERE companyroleid = :rid",
                                  {"rid": self.role_id}, label="delete UserBranchRoles by role")

        # 3. Exact audit residue for the tracked entities.
        if self.assignment_id is not None:
            await runner.execute(db,
                "DELETE FROM audit.auditlog WHERE entityname = 'UserBranchRoles' AND entityid = :eid",
                {"eid": str(self.assignment_id)}, label="delete UserBranchRoles audit")
        if self.user_id is not None:
            await runner.execute(db,
                "DELETE FROM audit.auditlog WHERE entityname = 'Users' AND entityid = :eid",
                {"eid": str(self.user_id)}, label="delete Users audit")
        if self.owns_role and self.role_id is not None:
            await runner.execute(db,
                "DELETE FROM audit.auditlog WHERE entityname IN ('CompanyRoles', 'CompanyRolePermissions') "
                "AND entityid = :eid",
                {"eid": str(self.role_id)}, label="delete CompanyRoles/CompanyRolePermissions audit")

        # 4. CompanyRolePermissions (direct-SQL or admin-set).
        if self.owns_role and self.role_id is not None:
            await runner.execute(db, "DELETE FROM sec.companyrolepermissions WHERE companyroleid = :rid",
                                  {"rid": self.role_id}, label="delete CompanyRolePermissions rows")

        # 5. Users.
        if self.user_id is not None:
            await runner.execute(db, "DELETE FROM sec.users WHERE userid = :uid",
                                  {"uid": self.user_id}, label="delete Users row")

        # 6. CompanyRoles -- ONLY ever a role this context itself created.
        if self.owns_role and self.role_id is not None:
            await runner.execute(db, "DELETE FROM sec.companyroles WHERE companyroleid = :rid",
                                  {"rid": self.role_id}, label="delete CompanyRoles row")

        # --- Authoritative, non-vacuous residue proof (real recovered IDs) ---
        # Run even if earlier deletes failed, so every independent resource
        # still gets its own pass/fail signal rather than being skipped.
        checks: dict = {}
        if self.user_id is not None:
            checks["user"] = (
                "SELECT COUNT(*) AS c FROM sec.users WHERE userid = :uid", {"uid": self.user_id},
            )
            checks["user_audit"] = (
                "SELECT COUNT(*) AS c FROM audit.auditlog WHERE entityname = 'Users' AND entityid = :eid",
                {"eid": str(self.user_id)},
            )
            checks["assignment"] = (
                "SELECT COUNT(*) AS c FROM sec.userbranchroles WHERE userid = :uid", {"uid": self.user_id},
            )
            checks["overrides"] = (
                "SELECT COUNT(*) AS c FROM sec.userpermissionoverrides WHERE userid = :uid", {"uid": self.user_id},
            )
            checks["override_audit"] = (
                "SELECT COUNT(*) AS c FROM audit.auditlog "
                "WHERE entityname = 'UserPermissionOverrides' AND entityid = :eid",
                {"eid": str(self.user_id)},
            )
        if self.owns_role and self.role_id is not None:
            checks["role"] = (
                "SELECT COUNT(*) AS c FROM sec.companyroles WHERE companyroleid = :rid", {"rid": self.role_id},
            )
            checks["role_perms"] = (
                "SELECT COUNT(*) AS c FROM sec.companyrolepermissions WHERE companyroleid = :rid",
                {"rid": self.role_id},
            )
            checks["role_audit"] = (
                "SELECT COUNT(*) AS c FROM audit.auditlog "
                "WHERE entityname IN ('CompanyRoles', 'CompanyRolePermissions') AND entityid = :eid",
                {"eid": str(self.role_id)},
            )
        for label, (sql, params) in checks.items():
            async def _check(sql=sql, params=params, label=label):
                count = (await db.execute(_text(sql), params)).mappings().first()["c"]
                assert count == 0, f"Permission residue check {label!r} failed for marker {self.marker!r}: {count} remaining"
            await runner.check(_check, label=f"residue check {label!r}")

        runner.raise_if_any()


@contextlib.asynccontextmanager
async def _owned_permission_principal(
    session_client, admin_token, direct_db, *,
    role_perms: list | None = None,
    direct_sql_perms: list | None = None,
    existing_company_role_id: int | None = None,
    scope_type: str = "AllCompanyBranches",
    branch_id=None,
    override_perms: list | None = None,
    employee_id: int | None = None,
    password: str = "TestPass123!",
    _capture_marker: dict | None = None,
    _inject_failure_after_role_write: bool = False,
    _inject_failure_after_user_write: bool = False,
    _inject_failure_after_assignment_write: bool = False,
    _inject_failure_after_direct_perm_write: bool = False,
    _inject_failure_after_override_write: bool = False,
):
    """
    Fully test-owned permission principal: role (or a caller-supplied
    shared/seeded role, never deleted) + user + assignment + optional
    direct-SQL permission row / permission override -- guaranteed to clean
    up on every exit path, including a failure strictly AFTER a write
    commits but BEFORE its ID would normally be extracted (the
    `_inject_failure_after_*` hooks, test-local only, never referenced by
    production code).

    Ownership begins via `marker`, generated before the first write.
    `_capture_marker`, when supplied, is populated with `{"marker": marker}`
    immediately (before any write) and later with the recovered
    role/user/assignment IDs after cleanup, so a caller whose failure fires
    before `yield` -- and therefore never receives a yielded value -- can
    still make its own independent post-teardown assertions.

    Yields (token, user_id).
    """
    marker = f"cp4bperm{uuid.uuid4().hex[:10]}"
    if _capture_marker is not None:
        _capture_marker["marker"] = marker
    owns_role = existing_company_role_id is None
    own = _PermissionOwnership(direct_db, marker, owns_role=owns_role, known_role_id=existing_company_role_id)
    try:
        if owns_role:
            cr = await session_client.post(
                "/admin/company-roles", json={"role_name": marker}, headers=auth(admin_token),
            )
            assert cr.status_code == 201, f"Create role failed: {cr.text}"
            if _inject_failure_after_role_write:
                raise _DeliberateSetupFailure(
                    "simulated failure after CompanyRole write committed, before role_id extracted"
                )
            role_id = cr.json()["company_role_id"]
            own.role_id = role_id
            if role_perms:
                pr = await session_client.put(
                    f"/admin/company-roles/{role_id}/permissions", json={"permission_codes": role_perms},
                    headers=auth(admin_token),
                )
                assert pr.status_code == 200, f"Set permissions failed: {pr.text}"
        else:
            role_id = existing_company_role_id

        if direct_sql_perms:
            for code in direct_sql_perms:
                await direct_db.execute(
                    _text(
                        "INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode) "
                        "VALUES (:rid, :code) ON CONFLICT DO NOTHING"
                    ),
                    {"rid": role_id, "code": code},
                )
                if _inject_failure_after_direct_perm_write:
                    raise _DeliberateSetupFailure(
                        "simulated failure after direct-SQL CompanyRolePermissions write committed"
                    )

        resp = await session_client.post(
            "/admin/users",
            json={"username": marker, "display_name": marker, "password": password,
                  "is_active": True, "can_login": True, "must_change_password": False},
            headers=auth(admin_token),
        )
        assert resp.status_code == 201, f"Create user failed: {resp.text}"
        if _inject_failure_after_user_write:
            raise _DeliberateSetupFailure(
                "simulated failure after User write committed, before user_id extracted"
            )
        user_id = resp.json()["user_id"]
        own.user_id = user_id  # registered immediately -- before any further write

        assign_body: dict = {"company_role_id": role_id, "scope_type": scope_type}
        if branch_id is not None:
            assign_body["branch_id"] = branch_id
        assign_resp = await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments", json=assign_body, headers=auth(admin_token),
        )
        assert assign_resp.status_code in (200, 201), f"Assign role failed: {assign_resp.text}"
        if _inject_failure_after_assignment_write:
            raise _DeliberateSetupFailure(
                "simulated failure after UserBranchRoles write committed, before assignment_id extracted"
            )
        assignment_id = assign_resp.json()["assignment_id"]
        own.assignment_id = assignment_id  # registered immediately

        if override_perms:
            pr = await session_client.put(
                f"/admin/users/{user_id}/permission-overrides",
                json={"permission_codes": override_perms}, headers=auth(admin_token),
            )
            assert pr.status_code == 200, f"Set permission overrides failed: {pr.text}"
            if _inject_failure_after_override_write:
                raise _DeliberateSetupFailure(
                    "simulated failure after UserPermissionOverrides write committed, before helper return"
                )

        if employee_id is not None:
            await direct_db.execute(
                _text("UPDATE sec.users SET employeeid = :eid WHERE userid = :uid"),
                {"eid": employee_id, "uid": user_id},
            )

        login = await session_client.post(
            "/auth/login", json={"username": marker, "password": password, "company_code": "DEMO"},
        )
        assert login.status_code == 200, f"Login failed: {login.text}"

        yield login.json()["access_token"], user_id
    finally:
        await own.cleanup()
        if _capture_marker is not None:
            _capture_marker["role_id"] = own.role_id
            _capture_marker["user_id"] = own.user_id
            _capture_marker["assignment_id"] = own.assignment_id


async def _permission_residue(
    direct_db,
    marker: str,
    *,
    owned_user_ids: list[int] | None = None,
) -> dict:
    """Secondary, marker-scoped residue check usable by callers that never
    received a yielded value (a pre-yield injection failure)."""
    users = (await direct_db.execute(
        _text("SELECT COUNT(*) AS c FROM sec.users WHERE username = :m"), {"m": marker},
    )).mappings().first()["c"]
    roles = (await direct_db.execute(
        _text("SELECT COUNT(*) AS c FROM sec.companyroles WHERE rolename = :m"), {"m": marker},
    )).mappings().first()["c"]
    assignments = (await direct_db.execute(
        _text("""
            SELECT COUNT(*) AS c FROM sec.userbranchroles ubr
            WHERE ubr.userid IN (SELECT userid FROM sec.users WHERE username = :m)
               OR ubr.companyroleid IN (SELECT companyroleid FROM sec.companyroles WHERE rolename = :m)
        """),
        {"m": marker},
    )).mappings().first()["c"]
    company_role_permissions = (await direct_db.execute(
        _text("""
            SELECT COUNT(*) AS c FROM sec.companyrolepermissions crp
            JOIN sec.companyroles cr ON cr.companyroleid = crp.companyroleid WHERE cr.rolename = :m
        """),
        {"m": marker},
    )).mappings().first()["c"]
    overrides = (await direct_db.execute(
        _text("""
            SELECT COUNT(*) AS c FROM sec.userpermissionoverrides upo
            WHERE upo.userid IN (SELECT userid FROM sec.users WHERE username = :m)
        """),
        {"m": marker},
    )).mappings().first()["c"]
    residue = {
        "users": users, "roles": roles, "assignments": assignments,
        "company_role_permissions": company_role_permissions, "overrides": overrides,
    }
    # The owner records the exact UserID before deletion. Never make a
    # post-teardown audit assertion by joining through the deleted user row.
    if owned_user_ids:
        override_audit = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) AS c FROM audit.auditlog
                WHERE entityname = 'UserPermissionOverrides'
                  AND entityid = ANY(:entity_ids)
            """),
            {"entity_ids": [str(user_id) for user_id in owned_user_ids]},
        )).mappings().first()["c"]
        residue["override_audit"] = override_audit
    return residue


# ---------------------------------------------------------------------------
# 1. Lifecycle guard
# ---------------------------------------------------------------------------

class TestLifecycleGuard:

    @pytest.mark.asyncio
    async def test_open_returns_200(self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "Open"
            assert body["provisional"] is True
            assert body["financials_available"] is True

    @pytest.mark.asyncio
    async def test_returned_returns_200(self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Returned") as pid:
            r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "Returned"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["Draft", "InReview", "Approved", "Locked", "Archived", "Cancelled"])
    async def test_every_other_status_denied_422(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db, status,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status=status, suffix=f"-{status}") as pid:
            r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
            assert r.status_code == 422, f"status={status}: expected 422, got {r.status_code}: {r.text}"

    @pytest.mark.asyncio
    async def test_finalization_preview_route_unchanged(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """The existing Approved-only finalization-preview route must be
        completely unaffected by the new calculation-preview route."""
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            r = await session_client.get(f"/payroll/periods/{pid}/finalization-preview", headers=auth(auth_token))
            assert r.status_code == 422, "finalization-preview must still require Approved"
            assert "Approved" in r.text


# ---------------------------------------------------------------------------
# 1b. Returned-period teardown ownership
# ---------------------------------------------------------------------------

class TestReturnedPeriodCleanup:

    @pytest.mark.asyncio
    async def test_returned_restoration_failure_still_runs_later_period_cleanup(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """The deliberate failure occurs after the real pointer-clear so
        later independent cleanup can delete the exact review item and period."""
        captured: dict[str, int] = {}
        with pytest.raises(ExceptionGroup) as exc_info:
            async with _owned_period(
                session_client,
                auth_token,
                paytest_branch_id,
                direct_db,
                status="Returned",
                _inject_failure_after_returned_restoration=True,
            ) as period_id:
                row = (await direct_db.execute(
                    _text("SELECT currentreturnreviewitemid FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                    {"pid": period_id},
                )).mappings().first()
                assert row is not None and row["currentreturnreviewitemid"] is not None
                captured["period_id"] = period_id
                captured["review_item_id"] = row["currentreturnreviewitemid"]

        assert any(isinstance(error, _DeliberateSetupFailure) for error in exc_info.value.exceptions)
        residue = await _returned_period_residue(direct_db, **captured)
        assert all(value == 0 for value in residue.values()), dict(residue)

    @pytest.mark.asyncio
    async def test_returned_body_and_cleanup_failures_both_remain_visible(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        class _DeliberateReturnedBodyFailure(Exception):
            pass

        captured: dict[str, int] = {}
        with pytest.raises(ExceptionGroup) as exc_info:
            async with _owned_period(
                session_client,
                auth_token,
                paytest_branch_id,
                direct_db,
                status="Returned",
                _inject_failure_after_returned_restoration=True,
            ) as period_id:
                row = (await direct_db.execute(
                    _text("SELECT currentreturnreviewitemid FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                    {"pid": period_id},
                )).mappings().first()
                assert row is not None and row["currentreturnreviewitemid"] is not None
                captured["period_id"] = period_id
                captured["review_item_id"] = row["currentreturnreviewitemid"]
                raise _DeliberateReturnedBodyFailure("simulated Returned-period body failure")

        assert any(isinstance(error, _DeliberateSetupFailure) for error in exc_info.value.exceptions)
        assert isinstance(exc_info.value.__context__, _DeliberateReturnedBodyFailure)
        residue = await _returned_period_residue(direct_db, **captured)
        assert all(value == 0 for value in residue.values()), dict(residue)

    @pytest.mark.asyncio
    async def test_returned_period_teardown_leaves_zero_review_and_period_residue(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        captured: dict[str, int] = {}
        async with _owned_period(
            session_client,
            auth_token,
            paytest_branch_id,
            direct_db,
            status="Returned",
        ) as period_id:
            row = (await direct_db.execute(
                _text("SELECT currentreturnreviewitemid FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": period_id},
            )).mappings().first()
            assert row is not None and row["currentreturnreviewitemid"] is not None
            captured["period_id"] = period_id
            captured["review_item_id"] = row["currentreturnreviewitemid"]

        residue = await _returned_period_residue(direct_db, **captured)
        assert all(value == 0 for value in residue.values()), dict(residue)


# ---------------------------------------------------------------------------
# 2. Permission and tenant-scope
# ---------------------------------------------------------------------------

class TestPermissionAndScope:

    @pytest.mark.asyncio
    async def test_payroll_view_allowed(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_permission_principal(
                session_client, auth_token, direct_db, role_perms=["payroll.view"],
            ) as (token, _uid):
                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(token))
                assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_payroll_entry_allowed(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_permission_principal(
                session_client, auth_token, direct_db, role_perms=["payroll.entry"],
            ) as (token, _uid):
                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(token))
                assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_payroll_finalize_only_denied(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """
        payroll.finalize must NOT be treated as implying payroll.view/entry.

        Note: the admin role-permission endpoint (`set_company_role_permissions`,
        app/admin/service.py `_PERM_DEPS`) has a PRE-EXISTING, deliberate
        dependency rule that auto-attaches payroll.view whenever
        payroll.finalize is granted through the normal admin API -- this is
        existing product behavior unrelated to CP-4B. To isolate and prove
        THIS endpoint's own permission gate (not the admin dependency-
        expansion policy), the role-permission row is inserted directly so
        the role holds EXACTLY {'payroll.finalize'}, nothing else. This
        direct-SQL row is fully owned and removed by
        `_PermissionOwnership.cleanup` along with the role.
        """
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_permission_principal(
                session_client, auth_token, direct_db, role_perms=[], direct_sql_perms=["payroll.finalize"],
            ) as (token, _uid):
                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(token))
                assert r.status_code == 403, r.text

    @pytest.mark.asyncio
    async def test_no_relevant_permission_denied(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_permission_principal(
                session_client, auth_token, direct_db, role_perms=[],
            ) as (token, _uid):
                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(token))
                assert r.status_code == 403, r.text

    @pytest.mark.asyncio
    async def test_wrong_company_denied(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            r = await session_client.get(
                f"/payroll/periods/{pid}/calculation-preview",
                headers={"Authorization": "Bearer invalid-cross-tenant-token"},
            )
            assert r.status_code in (401, 403), r.text

    @pytest.mark.asyncio
    async def test_inaccessible_branch_denied(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
    ):
        """A user scoped to a DIFFERENT branch must be denied access to this branch's period."""
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            other_branch_id = hq_branch_id if hq_branch_id != paytest_branch_id else None
            if other_branch_id is None:
                pytest.skip("No distinct second branch available in this test environment")
            async with _owned_permission_principal(
                session_client, auth_token, direct_db, role_perms=["payroll.view"],
                scope_type="SpecificBranch", branch_id=other_branch_id,
            ) as (token, _uid):
                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(token))
                assert r.status_code in (403, 404), r.text

    @pytest.mark.asyncio
    async def test_oda_denial_direct_scope(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """
        ODA (OwnDriverDataOnly) denial exercised through the ACTUAL route,
        not a unit test of the guard helper: a principal with an
        OwnDriverDataOnly-scoped assignment and otherwise-sufficient
        payroll.view is still denied, and no financial data is exposed.
        """
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(
                session_client, auth_token, paytest_branch_id, direct_db, name="CP4B ODA Principal",
            ) as driver_id:
                emp_row = (await direct_db.execute(
                    _text("SELECT employeeid FROM core.drivers WHERE driverid = :id"), {"id": driver_id},
                )).mappings().first()
                async with _owned_permission_principal(
                    session_client, auth_token, direct_db, role_perms=["payroll.view"],
                    scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
                    employee_id=emp_row["employeeid"],
                ) as (token, _uid):
                    r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(token))
                    assert r.status_code == 403, r.text
                    body = r.json()
                    assert "drivers" not in body, "no financial preview data may be exposed on ODA denial"
                    assert "expected_pay" not in r.text

    @pytest.mark.asyncio
    async def test_driver_role_denial_real_role_classification(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """
        Driver-role denial exercised through the real seeded 'DRIVER'
        CompanyRole (not a synthetic role), assigned with OwnDriverDataOnly
        scope -- the actual role-classification path a real driver-role user
        goes through. payroll.view is granted via a per-user permission
        override (never by editing the shared DRIVER role's own permission
        set, which would contaminate other tests/fixtures using that role).
        """
        driver_role_row = (await direct_db.execute(
            _text("SELECT companyroleid FROM sec.companyroles WHERE companyid = 1 AND rolecode = 'DRIVER'"),
        )).mappings().first()
        if driver_role_row is None:
            pytest.skip("No seeded 'DRIVER' company role present in this test environment")
        driver_role_id = driver_role_row["companyroleid"]

        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(
                session_client, auth_token, paytest_branch_id, direct_db, name="CP4B DriverRole Principal",
            ) as driver_id:
                emp_row = (await direct_db.execute(
                    _text("SELECT employeeid FROM core.drivers WHERE driverid = :id"), {"id": driver_id},
                )).mappings().first()
                async with _owned_permission_principal(
                    session_client, auth_token, direct_db,
                    existing_company_role_id=driver_role_id,
                    scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
                    override_perms=["payroll.view"],
                    employee_id=emp_row["employeeid"],
                ) as (token, _uid):
                    r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(token))
                    assert r.status_code == 403, r.text
                    body = r.json()
                    assert "drivers" not in body, "no preview data may be returned on driver-role denial"

                # The shared seeded 'DRIVER' role itself must be completely
                # unaffected -- still present, permission set untouched.
                still_present = (await direct_db.execute(
                    _text("SELECT companyroleid FROM sec.companyroles WHERE companyroleid = :id"),
                    {"id": driver_role_id},
                )).mappings().first()
                assert still_present is not None, "shared DRIVER role must never be deleted by test cleanup"


# ---------------------------------------------------------------------------
# 2b. Real pre-parse permission-acquisition failure proof (Codex P1-A fix)
#
# Each parametrized case injects failure strictly AFTER a persistent write
# commits but BEFORE the normal code path extracts its ID -- the exact
# unsafe window -- for role creation (A), user creation (B), user-role
# assignment (C), and direct-SQL permission insertion (D). The failure fires
# INSIDE `_owned_permission_principal`'s own setup, before `yield`, so the
# `with` body below is provably never reached.
# ---------------------------------------------------------------------------

class TestPermissionAcquisitionFailure:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("injection_point", ["role", "user", "assignment", "direct_perm"])
    async def test_permission_write_survives_pre_parse_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, direct_db, injection_point,
    ):
        inject_kwargs = {
            "role":         {"_inject_failure_after_role_write": True},
            "user":         {"_inject_failure_after_user_write": True},
            "assignment":   {"_inject_failure_after_assignment_write": True},
            "direct_perm":  {"_inject_failure_after_direct_perm_write": True},
        }[injection_point]

        marker_holder: dict = {}
        with pytest.raises(_DeliberateSetupFailure):
            async with _owned_permission_principal(
                session_client, auth_token, direct_db,
                role_perms=[], direct_sql_perms=["payroll.finalize"],
                _capture_marker=marker_holder,
                **inject_kwargs,
            ) as (_token, _uid):
                pytest.fail(f"[{injection_point}] must not reach yield -- injection raises first")

        marker = marker_holder["marker"]
        # `_owned_permission_principal`'s own `finally` already recovered
        # every reachable ID and asserted zero row/audit residue for each
        # (the authoritative, non-vacuous proof -- if that internal
        # assertion had failed, this test would see a different exception
        # type than _DeliberateSetupFailure and fail here). The marker-scoped
        # check below is a second, independent confirmation.
        residue = await _permission_residue(direct_db, marker)
        assert residue["users"] == 0, f"[{injection_point}] user residue: {residue}"
        assert residue["roles"] == 0, f"[{injection_point}] role residue: {residue}"
        assert residue["assignments"] == 0, f"[{injection_point}] assignment residue: {residue}"
        assert residue["company_role_permissions"] == 0, f"[{injection_point}] permission residue: {residue}"
        assert residue["overrides"] == 0, f"[{injection_point}] override residue: {residue}"

    @pytest.mark.asyncio
    async def test_normal_permission_setup_leaves_zero_residue(
        self, session_client: httpx.AsyncClient, auth_token: str, direct_db,
    ):
        """The normal, successful permission-test path leaves zero residue."""
        marker_holder: dict = {}
        async with _owned_permission_principal(
            session_client, auth_token, direct_db, role_perms=["payroll.view"],
            _capture_marker=marker_holder,
        ) as (_token, user_id):
            assert user_id is not None

        residue = await _permission_residue(
            direct_db,
            marker_holder["marker"],
            owned_user_ids=[marker_holder["user_id"]],
        )
        assert all(v == 0 for v in residue.values()), residue

    @pytest.mark.asyncio
    async def test_normal_override_use_leaves_zero_override_residue(
        self, session_client: httpx.AsyncClient, auth_token: str, direct_db,
    ):
        """UserPermissionOverrides P1 fix: a successful override write
        (entity_name='UserPermissionOverrides', entity_id=str(user_id) --
        confirmed by reading app/admin/service.py) leaves zero override rows
        AND zero exact override audit rows after normal teardown."""
        marker_holder: dict = {}
        async with _owned_permission_principal(
            session_client, auth_token, direct_db, role_perms=[], override_perms=["payroll.view"],
            _capture_marker=marker_holder,
        ) as (_token, user_id):
            row = (await direct_db.execute(
                _text("SELECT COUNT(*) AS c FROM sec.userpermissionoverrides WHERE userid = :uid"), {"uid": user_id},
            )).mappings().first()
            assert row["c"] == 1, "test bug: override row must be persisted before cleanup runs"
            audit_before = (await direct_db.execute(
                _text("SELECT COUNT(*) AS c FROM audit.auditlog WHERE entityname = 'UserPermissionOverrides' AND entityid = :eid"),
                {"eid": str(user_id)},
            )).mappings().first()
            assert audit_before["c"] == 1, "test bug: exactly one USER_PERMISSION_OVERRIDES_SET audit row expected"

        residue = await _permission_residue(
            direct_db,
            marker_holder["marker"],
            owned_user_ids=[marker_holder["user_id"]],
        )
        assert residue["overrides"] == 0, residue
        assert residue["override_audit"] == 0, residue

    @pytest.mark.asyncio
    async def test_override_write_survives_pre_parse_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, direct_db,
    ):
        """The override write commits (200 received), then a deliberate
        failure fires strictly AFTER that commit but BEFORE the helper
        returns -- cleanup must still find and remove the override row and
        its exact audit row via the recovered user_id, not via a
        never-reached local variable."""
        marker_holder: dict = {}
        with pytest.raises(_DeliberateSetupFailure):
            async with _owned_permission_principal(
                session_client, auth_token, direct_db, role_perms=[], override_perms=["payroll.view"],
                _capture_marker=marker_holder, _inject_failure_after_override_write=True,
            ) as (_token, _uid):
                pytest.fail("must not reach yield -- override injection raises first")

        residue = await _permission_residue(
            direct_db,
            marker_holder["marker"],
            owned_user_ids=[marker_holder["user_id"]],
        )
        assert residue["overrides"] == 0, residue
        assert residue["override_audit"] == 0, residue
        assert residue["users"] == 0, residue

    @pytest.mark.asyncio
    async def test_override_cleanup_survives_assertion_failure_after_yield(
        self, session_client: httpx.AsyncClient, auth_token: str, direct_db,
    ):
        """An override was persisted successfully and the context yielded
        normally, but the TEST BODY itself then raises (e.g. a failed
        assertion unrelated to setup) -- the context manager's `finally`
        must still run and remove the override row/audit regardless of the
        exception type propagating out of the `with` body."""
        marker_holder: dict = {}

        class _DeliberateBodyAssertionFailure(Exception):
            pass

        with pytest.raises(_DeliberateBodyAssertionFailure):
            async with _owned_permission_principal(
                session_client, auth_token, direct_db, role_perms=[], override_perms=["payroll.view"],
                _capture_marker=marker_holder,
            ) as (_token, user_id):
                row = (await direct_db.execute(
                    _text("SELECT COUNT(*) AS c FROM sec.userpermissionoverrides WHERE userid = :uid"), {"uid": user_id},
                )).mappings().first()
                assert row["c"] == 1, "test bug: override row must be persisted before the deliberate body failure"
                raise _DeliberateBodyAssertionFailure("simulated assertion failure in the test body, after yield")

        residue = await _permission_residue(
            direct_db,
            marker_holder["marker"],
            owned_user_ids=[marker_holder["user_id"]],
        )
        assert residue["overrides"] == 0, residue
        assert residue["override_audit"] == 0, residue
        assert residue["users"] == 0, residue


# ---------------------------------------------------------------------------
# 3. Daily PerUnit parity
# ---------------------------------------------------------------------------

class TestDailyPerUnitParity:

    @pytest.mark.asyncio
    async def test_daily_perunit_matches_production_calculation(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B PerUnit") as driver_id:
                hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
                async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, hourly_rt_id, "12.3400", DATE_FEB04):
                    line = await session_client.post(
                        f"/payroll/periods/{pid}/lines",
                        json={"driver_id": driver_id, "work_date": DATE_FEB04, "line_type": "HOURS", "quantity": "6.5000"},
                        headers=auth(auth_token),
                    )
                    assert line.status_code == 201, line.text

                    r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                    assert r.status_code == 200, r.text
                    body = r.json()
                    drv = next(d for d in body["drivers"] if d["driver_id"] == driver_id)
                    assert Decimal(str(drv["daily_pay"])) == Decimal("80.2100"), "6.5 x 12.34 = 80.2100 (PerUnit, ROUND_HALF_EVEN)"
                    assert Decimal(str(drv["expected_pay"])) == Decimal("80.2100")

    @pytest.mark.asyncio
    async def test_missing_rate_produces_blocker_not_zero(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B NoRate") as driver_id:
                line = await session_client.post(
                    f"/payroll/periods/{pid}/lines",
                    json={"driver_id": driver_id, "work_date": DATE_FEB04, "line_type": "HOURS", "quantity": "5.0000"},
                    headers=auth(auth_token),
                )
                assert line.status_code == 201, line.text

                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["has_blockers"] is True
                drv = next(d for d in body["drivers"] if d["driver_id"] == driver_id)
                assert drv["needs_manager_review"] is True
                line_entries = [ln for ln in drv["lines"] if ln["line_type"] == "HOURS"]
                assert len(line_entries) == 1
                assert line_entries[0]["calculated_amount"] is None, "must not silently become zero"
                assert line_entries[0]["needs_manager_review"] is True


# ---------------------------------------------------------------------------
# 4. Canonical live Status, stale-projection exclusion
# ---------------------------------------------------------------------------

class TestCanonicalLiveStatus:

    @pytest.mark.asyncio
    async def test_live_status_pay_uses_current_rate_not_stale_projection(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """
        Section 11's mandatory test: create a stored STATUS_PAYMENT
        projection at rate A, then change the applicable rate to B, and
        prove the response uses the LIVE rate B, not the stale stored
        projection amount, and that the projection row itself was never
        updated by the read.
        """
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B Status") as driver_id:
                status_pay_rt_id = await _get_status_pay_rate_type_id(session_client, auth_token)
                async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
                    async with _owned_status_key(direct_db, 1, paytest_branch_id, f"CP4BSTAT{uuid.uuid4().hex[:6]}", "8.00", src_col_id) as status_key_id:
                        async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, status_pay_rt_id, "20.0000", DATE_FEB04) as rate_a_id:
                            code_row = (await direct_db.execute(
                                _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                                {"id": status_key_id},
                            )).mappings().first()
                            status_code = code_row["statuscode"]

                            await _save_day_grid_status(session_client, auth_token, pid, driver_id, DATE_FEB04, status_code)

                            # Confirm the stored STATUS_PAYMENT projection exists at rate A ($160).
                            proj_before = (await direct_db.execute(
                                _text("""
                                    SELECT draftlineid, calculatedamount FROM payroll.payrolldraftlines
                                    WHERE payrollperiodid = :pid AND driverid = :did AND sourceid LIKE 'STATUS_PAYMENT:%'
                                """),
                                {"pid": pid, "did": driver_id},
                            )).mappings().first()
                            assert proj_before is not None
                            assert Decimal(str(proj_before["calculatedamount"])) == Decimal("160.0000")
                            proj_line_id = proj_before["draftlineid"]

                            # Change the applicable rate: void A, approve B ($30).
                            await direct_db.execute(
                                _text("UPDATE payroll.driverrates SET status = 'Voided' WHERE driverrateid = :id"), {"id": rate_a_id},
                            )
                            async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, status_pay_rt_id, "30.0000", DATE_FEB04) as _rate_b_id:
                                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                                assert r.status_code == 200, r.text
                                body = r.json()
                                drv = next(d for d in body["drivers"] if d["driver_id"] == driver_id)
                                # 8.00 x 30.0000 = 240.0000 (live), NOT 160.0000 (stale stored projection)
                                assert Decimal(str(drv["status_pay"])) == Decimal("240.0000"), (
                                    f"expected live rate B result 240.0000, got {drv['status_pay']}"
                                )
                                assert Decimal(str(drv["expected_pay"])) == Decimal("240.0000")

                                # The stored projection line itself must be untouched (still $160, still same row).
                                proj_after = (await direct_db.execute(
                                    _text("SELECT draftlineid, calculatedamount FROM payroll.payrolldraftlines WHERE draftlineid = :id"),
                                    {"id": proj_line_id},
                                )).mappings().first()
                                assert proj_after is not None
                                assert Decimal(str(proj_after["calculatedamount"])) == Decimal("160.0000"), (
                                    "stored stale projection must not be updated by the read-only preview"
                                )

                            await direct_db.execute(
                                _text("DELETE FROM payroll.payrolldraftlines WHERE draftlineid = :id"), {"id": proj_line_id},
                            )

    @pytest.mark.asyncio
    async def test_missing_status_rate_is_blocker_not_zero(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B StatusNoRate") as driver_id:
                async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
                    async with _owned_status_key(direct_db, 1, paytest_branch_id, f"CP4BNORATE{uuid.uuid4().hex[:6]}", "8.00", src_col_id) as status_key_id:
                        code_row = (await direct_db.execute(
                            _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                            {"id": status_key_id},
                        )).mappings().first()
                        await _save_day_grid_status(session_client, auth_token, pid, driver_id, DATE_FEB04, code_row["statuscode"])

                        r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                        assert r.status_code == 200, r.text
                        body = r.json()
                        assert body["has_blockers"] is True
                        drv = next(d for d in body["drivers"] if d["driver_id"] == driver_id)
                        assert drv["needs_manager_review"] is True
                        status_lines = [ln for ln in drv["lines"] if ln["source_type"] == "StatusEntryState"]
                        assert len(status_lines) == 1
                        assert status_lines[0]["calculated_amount"] is None
                        assert status_lines[0]["needs_manager_review"] is True

    @pytest.mark.asyncio
    async def test_similar_prefix_line_not_incorrectly_excluded(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """
        Codex P2 fix: a line whose SourceID merely STARTS WITH the
        'STATUS_PAYMENT:' text but has a different SourceType (not 'System')
        and does not match the exact three-integer-segment identity format
        is NOT the persisted Status compatibility projection, and must not
        be excluded from the daily aggregation by an overbroad prefix match.
        """
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B PrefixLine") as driver_id:
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrolldraftlines
                            (companyid, branchid, payrollperiodid, driverid,
                             workdate, linetype, linescope, quantity,
                             calculatedamount, sourcetype, sourceid,
                             status, needsmanagerreview, addedbyuserid)
                        VALUES
                            (1, :bid, :pid, :did,
                             :wdate, 'CP4BFAKE', 'Daily', 1,
                             5.0000, 'Manual', 'STATUS_PAYMENT:not-the-real-format',
                             'Active', FALSE, 1)
                    """),
                    {"bid": paytest_branch_id, "pid": pid, "did": driver_id,
                     "wdate": datetime.date.fromisoformat(DATE_FEB04)},
                )

                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                assert r.status_code == 200, r.text
                body = r.json()
                drv = next(d for d in body["drivers"] if d["driver_id"] == driver_id)
                assert Decimal(str(drv["daily_pay"])) == Decimal("5.0000"), (
                    "a SourceType != 'System' line with a merely-similar SourceID prefix "
                    "must be included, not excluded as a Status compatibility projection"
                )
                matching = [ln for ln in drv["lines"] if ln["source_id"] == "STATUS_PAYMENT:not-the-real-format"]
                assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_genuine_status_projection_source_type_is_excluded(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """
        Complements the similar-prefix test above: a line that DOES match
        the exact identity (SourceType='System' AND the three-integer-
        segment SourceID format) is excluded from the stored-line
        aggregation, exactly as the genuine STATUS_PAYMENT projection
        created by `_save_day_grid_status` already proves elsewhere -- here
        constructed directly to isolate the predicate itself from the
        day-grid write path.
        """
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B GenuineProj") as driver_id:
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrolldraftlines
                            (companyid, branchid, payrollperiodid, driverid,
                             workdate, linetype, linescope, quantity,
                             calculatedamount, sourcetype, sourceid,
                             status, needsmanagerreview, addedbyuserid)
                        VALUES
                            (1, :bid, :pid, :did,
                             :wdate, 'STATUS_PAY', 'Daily', 1,
                             99.0000, 'System', 'STATUS_PAYMENT:1:2:3',
                             'Active', FALSE, 1)
                    """),
                    {"bid": paytest_branch_id, "pid": pid, "did": driver_id,
                     "wdate": datetime.date.fromisoformat(DATE_FEB05)},
                )

                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                assert r.status_code == 200, r.text
                body = r.json()
                matching = [d for d in body["drivers"] if d["driver_id"] == driver_id]
                assert matching == [], (
                    "a genuine System-sourced STATUS_PAYMENT:<int>:<int>:<int> projection line "
                    "must be excluded from the stored-line aggregation entirely"
                )

                await direct_db.execute(
                    _text("DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid AND linetype = 'STATUS_PAY'"),
                    {"pid": pid},
                )


# ---------------------------------------------------------------------------
# 5. Bonus / min-max
# ---------------------------------------------------------------------------

class TestBonusAndMinMax:

    @pytest.mark.asyncio
    async def test_active_bonus_included_voided_excluded(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B Bonus") as driver_id:
                active = await session_client.post(
                    f"/payroll/periods/{pid}/bonuses",
                    json={"driver_id": driver_id, "amount": "50.00", "reason": "Active bonus"},
                    headers=auth(auth_token),
                )
                assert active.status_code == 201, active.text
                voided = await session_client.post(
                    f"/payroll/periods/{pid}/bonuses",
                    json={"driver_id": driver_id, "amount": "999.00", "reason": "Will be voided"},
                    headers=auth(auth_token),
                )
                assert voided.status_code == 201, voided.text
                del_r = await session_client.delete(
                    f"/payroll/periods/{pid}/bonuses/{voided.json()['bonus_event_id']}", headers=auth(auth_token),
                )
                assert del_r.status_code in (200, 204), del_r.text

                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                assert r.status_code == 200, r.text
                drv = next(d for d in r.json()["drivers"] if d["driver_id"] == driver_id)
                assert Decimal(str(drv["bonus_total"])) == Decimal("50.00")

    @pytest.mark.asyncio
    async def test_minimum_topup_before_bonus(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B MinTopup") as driver_id:
                hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
                async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, hourly_rt_id, "10.0000", DATE_FEB04):
                    line = await session_client.post(
                        f"/payroll/periods/{pid}/lines",
                        json={"driver_id": driver_id, "work_date": DATE_FEB04, "line_type": "HOURS", "quantity": "5.0000"},
                        headers=auth(auth_token),
                    )
                    assert line.status_code == 201, line.text  # normal_base = 50.00

                    async with _owned_driver_pay_rule(
                        session_client, auth_token, direct_db, driver_id=driver_id, branch_id=paytest_branch_id,
                        rule_type="MinimumPay", amount="200.00",
                        effective_from=PERIOD_START, effective_to=PERIOD_END,
                    ):
                        bonus_r = await session_client.post(
                            f"/payroll/periods/{pid}/bonuses",
                            json={"driver_id": driver_id, "amount": "25.00", "reason": "Bonus after min"},
                            headers=auth(auth_token),
                        )
                        assert bonus_r.status_code == 201, bonus_r.text

                        r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                        assert r.status_code == 200, r.text
                        drv = next(d for d in r.json()["drivers"] if d["driver_id"] == driver_id)
                        assert Decimal(str(drv["normal_base"])) == Decimal("50.0000")
                        assert Decimal(str(drv["minimum_adjustment"])) == Decimal("150.0000"), "200 min - 50 base = 150 topup, bonus excluded from base"
                        assert Decimal(str(drv["bonus_total"])) == Decimal("25.00")
                        assert Decimal(str(drv["expected_pay"])) == Decimal("225.0000"), "50 + 150 + 25 = 225"

    @pytest.mark.asyncio
    async def test_minimum_pay_rule_cleanup_survives_assertion_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """P1 fix: an assertion failure INSIDE the test body (after the
        DriverPayRule owner has yielded) must still leave zero rule/audit
        residue -- the context manager's `finally` runs regardless of the
        exception type propagating out of the `with` body."""
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B MinAssertFail") as driver_id:
                with pytest.raises(AssertionError, match="deliberate min-rule body failure"):
                    async with _owned_driver_pay_rule(
                        session_client, auth_token, direct_db, driver_id=driver_id, branch_id=paytest_branch_id,
                        rule_type="MinimumPay", amount="200.00",
                        effective_from=PERIOD_START, effective_to=PERIOD_END,
                    ) as rule_id:
                        row = (await direct_db.execute(
                            _text("SELECT COUNT(*) AS c FROM payroll.driverpayrules WHERE driverpayruleid = :id"), {"id": rule_id},
                        )).mappings().first()
                        assert row["c"] == 1, "test bug: rule must be persisted before the deliberate failure"
                        assert False, "deliberate min-rule body failure"

                residue = (await direct_db.execute(
                    _text("SELECT COUNT(*) AS c FROM payroll.driverpayrules WHERE driverid = :did"), {"did": driver_id},
                )).mappings().first()
                assert residue["c"] == 0, "DriverPayRule leaked after an assertion failure in the test body"

    @pytest.mark.asyncio
    async def test_maximum_cap_applied_before_bonus(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """normal_base > maximum -> maximum_adjustment is negative and applied
        BEFORE bonus; bonus is excluded from the cap comparison and fully
        added back afterward (CP-3C ordering)."""
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B MaxCap") as driver_id:
                hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
                async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, hourly_rt_id, "50.0000", DATE_FEB04):
                    line = await session_client.post(
                        f"/payroll/periods/{pid}/lines",
                        json={"driver_id": driver_id, "work_date": DATE_FEB04, "line_type": "HOURS", "quantity": "10.0000"},
                        headers=auth(auth_token),
                    )
                    assert line.status_code == 201, line.text  # normal_base = 500.00

                    async with _owned_driver_pay_rule(
                        session_client, auth_token, direct_db, driver_id=driver_id, branch_id=paytest_branch_id,
                        rule_type="MaximumPay", amount="300.00",
                        effective_from=PERIOD_START, effective_to=PERIOD_END,
                    ):
                        bonus_r = await session_client.post(
                            f"/payroll/periods/{pid}/bonuses",
                            json={"driver_id": driver_id, "amount": "40.00", "reason": "Bonus after cap"},
                            headers=auth(auth_token),
                        )
                        assert bonus_r.status_code == 201, bonus_r.text

                        r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                        assert r.status_code == 200, r.text
                        drv = next(d for d in r.json()["drivers"] if d["driver_id"] == driver_id)
                        assert Decimal(str(drv["normal_base"])) == Decimal("500.0000")
                        assert Decimal(str(drv["maximum_adjustment"])) == Decimal("-200.0000"), "300 max - 500 base = -200 cap"
                        assert Decimal(str(drv["minimum_adjustment"])) == Decimal("0")
                        assert Decimal(str(drv["bonus_total"])) == Decimal("40.00")
                        assert Decimal(str(drv["expected_pay"])) == Decimal("340.0000"), "500 - 200 + 40 = 340 (bonus excluded from cap)"

    @pytest.mark.asyncio
    async def test_maximum_pay_rule_cleanup_survives_assertion_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """Same proof as the minimum-pay case, for MaximumPay."""
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B MaxAssertFail") as driver_id:
                with pytest.raises(AssertionError, match="deliberate max-rule body failure"):
                    async with _owned_driver_pay_rule(
                        session_client, auth_token, direct_db, driver_id=driver_id, branch_id=paytest_branch_id,
                        rule_type="MaximumPay", amount="300.00",
                        effective_from=PERIOD_START, effective_to=PERIOD_END,
                    ) as rule_id:
                        row = (await direct_db.execute(
                            _text("SELECT COUNT(*) AS c FROM payroll.driverpayrules WHERE driverpayruleid = :id"), {"id": rule_id},
                        )).mappings().first()
                        assert row["c"] == 1, "test bug: rule must be persisted before the deliberate failure"
                        assert False, "deliberate max-rule body failure"

                residue = (await direct_db.execute(
                    _text("SELECT COUNT(*) AS c FROM payroll.driverpayrules WHERE driverid = :did"), {"did": driver_id},
                )).mappings().first()
                assert residue["c"] == 0, "DriverPayRule leaked after an assertion failure in the test body"

    @pytest.mark.asyncio
    async def test_mid_period_rate_change_resolves_per_work_date(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """Two Daily PerUnit lines on different work-dates, straddling a
        mid-period effective-dated rate change, each resolve the rate
        effective as-of their own WorkDate (not a single period-wide rate)."""
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B MidPeriod") as driver_id:
                hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
                async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, hourly_rt_id, "10.0000", PERIOD_START):
                    async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, hourly_rt_id, "20.0000", DATE_FEB05):
                        line1 = await session_client.post(
                            f"/payroll/periods/{pid}/lines",
                            json={"driver_id": driver_id, "work_date": DATE_FEB04, "line_type": "HOURS", "quantity": "3.0000"},
                            headers=auth(auth_token),
                        )
                        assert line1.status_code == 201, line1.text
                        line2 = await session_client.post(
                            f"/payroll/periods/{pid}/lines",
                            json={"driver_id": driver_id, "work_date": DATE_FEB05, "line_type": "HOURS", "quantity": "3.0000"},
                            headers=auth(auth_token),
                        )
                        assert line2.status_code == 201, line2.text

                        r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                        assert r.status_code == 200, r.text
                        drv = next(d for d in r.json()["drivers"] if d["driver_id"] == driver_id)
                        by_date = {ln["work_date"]: ln for ln in drv["lines"] if ln["line_type"] == "HOURS"}
                        assert Decimal(str(by_date[DATE_FEB04]["calculated_amount"])) == Decimal("30.0000"), "3 x 10.00 (rate as-of Feb04)"
                        assert Decimal(str(by_date[DATE_FEB05]["calculated_amount"])) == Decimal("60.0000"), "3 x 20.00 (rate as-of Feb05)"
                        assert Decimal(str(drv["daily_pay"])) == Decimal("90.0000")


# ---------------------------------------------------------------------------
# 6. Driver-union / deduplication
# ---------------------------------------------------------------------------

class TestDriverUnion:

    @pytest.mark.asyncio
    async def test_driver_with_only_status_source_included_once(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B StatusOnly") as driver_id:
                status_pay_rt_id = await _get_status_pay_rate_type_id(session_client, auth_token)
                async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
                    async with _owned_status_key(direct_db, 1, paytest_branch_id, f"CP4BUNION{uuid.uuid4().hex[:6]}", "8.00", src_col_id) as status_key_id:
                        async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, status_pay_rt_id, "18.0000", DATE_FEB04):
                            code_row = (await direct_db.execute(
                                _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                                {"id": status_key_id},
                            )).mappings().first()
                            await _save_day_grid_status(session_client, auth_token, pid, driver_id, DATE_FEB04, code_row["statuscode"])

                            r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                            assert r.status_code == 200, r.text
                            matching = [d for d in r.json()["drivers"] if d["driver_id"] == driver_id]
                            assert len(matching) == 1, "driver must appear exactly once"
                            assert Decimal(str(matching[0]["status_pay"])) == Decimal("144.0000")

    @pytest.mark.asyncio
    async def test_eligible_driver_with_no_financial_source_omitted(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B NoSource") as driver_id:
                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                assert r.status_code == 200, r.text
                matching = [d for d in r.json()["drivers"] if d["driver_id"] == driver_id]
                assert matching == [], "a driver with no current financial source must not receive a synthetic zero row"

    @pytest.mark.asyncio
    async def test_daily_only_driver_included_once(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B DailyOnly") as driver_id:
                hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
                async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, hourly_rt_id, "9.0000", DATE_FEB04):
                    line = await session_client.post(
                        f"/payroll/periods/{pid}/lines",
                        json={"driver_id": driver_id, "work_date": DATE_FEB04, "line_type": "HOURS", "quantity": "4.0000"},
                        headers=auth(auth_token),
                    )
                    assert line.status_code == 201, line.text

                    r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                    assert r.status_code == 200, r.text
                    matching = [d for d in r.json()["drivers"] if d["driver_id"] == driver_id]
                    assert len(matching) == 1
                    drv = matching[0]
                    assert Decimal(str(drv["daily_pay"])) == Decimal("36.0000")
                    assert Decimal(str(drv["period_pay"])) == Decimal("0")
                    assert Decimal(str(drv["bonus_total"])) == Decimal("0")
                    assert Decimal(str(drv["status_pay"])) == Decimal("0")

    @pytest.mark.asyncio
    async def test_period_pay_only_driver_included_once(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """
        POST /periods/{id}/period-pay blocks BOTH the system BONUS item
        (redirects to /bonuses) and manual ADJUSTMENT entry in this version
        (confirmed by test_m14.py::test_add_canonical_adjustment_blocked) --
        there is currently no supported API path to CREATE a new non-BONUS
        Period-scope line. CP-4B's own contract is to include whatever
        non-BONUS Period-scope DraftLines already exist (e.g. legacy/
        imported data), so this line is constructed directly, matching that
        contract. This also exercises the Fixed/EnteredAmount period-pay
        passthrough: CalculatedAmount is stored/read verbatim -- no PerUnit
        4dp rule and no additional 2dp rounding is applied by the preview.
        """
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B PeriodPayOnly") as driver_id:
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrolldraftlines
                            (companyid, branchid, payrollperiodid, driverid,
                             workdate, linetype, linescope, quantity,
                             calculatedamount, sourcetype, status, needsmanagerreview, addedbyuserid)
                        VALUES
                            (1, :bid, :pid, :did, NULL, 'ADJUSTMENT', 'Period', 1,
                             75.00, 'Manual', 'Active', FALSE, 1)
                    """),
                    {"bid": paytest_branch_id, "pid": pid, "did": driver_id},
                )

                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                assert r.status_code == 200, r.text
                matching = [d for d in r.json()["drivers"] if d["driver_id"] == driver_id]
                assert len(matching) == 1
                drv = matching[0]
                assert Decimal(str(drv["period_pay"])) == Decimal("75.00"), "amount must pass through unchanged"
                assert Decimal(str(drv["daily_pay"])) == Decimal("0")
                assert Decimal(str(drv["bonus_total"])) == Decimal("0")
                assert Decimal(str(drv["expected_pay"])) == Decimal("75.00")

    @pytest.mark.asyncio
    async def test_bonus_only_driver_included_once(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B BonusOnly") as driver_id:
                bonus_r = await session_client.post(
                    f"/payroll/periods/{pid}/bonuses",
                    json={"driver_id": driver_id, "amount": "60.00", "reason": "Bonus only"},
                    headers=auth(auth_token),
                )
                assert bonus_r.status_code == 201, bonus_r.text

                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                assert r.status_code == 200, r.text
                matching = [d for d in r.json()["drivers"] if d["driver_id"] == driver_id]
                assert len(matching) == 1
                drv = matching[0]
                assert Decimal(str(drv["bonus_total"])) == Decimal("60.00")
                assert Decimal(str(drv["daily_pay"])) == Decimal("0")
                assert Decimal(str(drv["period_pay"])) == Decimal("0")
                assert Decimal(str(drv["expected_pay"])) == Decimal("60.00")

    @pytest.mark.asyncio
    async def test_multi_source_driver_single_row_no_duplication(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B MultiSource") as driver_id:
                hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
                status_pay_rt_id = await _get_status_pay_rate_type_id(session_client, auth_token)
                async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
                    async with _owned_status_key(direct_db, 1, paytest_branch_id, f"CP4BMULTI{uuid.uuid4().hex[:6]}", "4.00", src_col_id) as status_key_id:
                        async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, hourly_rt_id, "10.0000", DATE_FEB04):
                            async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, status_pay_rt_id, "15.0000", DATE_FEB05):
                                line = await session_client.post(
                                    f"/payroll/periods/{pid}/lines",
                                    json={"driver_id": driver_id, "work_date": DATE_FEB04, "line_type": "HOURS", "quantity": "2.0000"},
                                    headers=auth(auth_token),
                                )
                                assert line.status_code == 201, line.text  # 20.00

                                code_row = (await direct_db.execute(
                                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                                    {"id": status_key_id},
                                )).mappings().first()
                                await _save_day_grid_status(session_client, auth_token, pid, driver_id, DATE_FEB05, code_row["statuscode"])  # 60.00

                                # POST /period-pay blocks manual ADJUSTMENT entry in this
                                # version (see test_period_pay_only_driver_included_once's
                                # docstring) -- construct the legacy-style Period-scope
                                # line directly, matching CP-4B's own inclusion contract.
                                await direct_db.execute(
                                    _text("""
                                        INSERT INTO payroll.payrolldraftlines
                                            (companyid, branchid, payrollperiodid, driverid,
                                             workdate, linetype, linescope, quantity,
                                             calculatedamount, sourcetype, status, needsmanagerreview, addedbyuserid)
                                        VALUES
                                            (1, :bid, :pid, :did, NULL, 'ADJUSTMENT', 'Period', 1,
                                             5.00, 'Manual', 'Active', FALSE, 1)
                                    """),
                                    {"bid": paytest_branch_id, "pid": pid, "did": driver_id},
                                )

                                bonus_r = await session_client.post(
                                    f"/payroll/periods/{pid}/bonuses",
                                    json={"driver_id": driver_id, "amount": "3.00", "reason": "Multi-source bonus"},
                                    headers=auth(auth_token),
                                )
                                assert bonus_r.status_code == 201, bonus_r.text

                                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                                assert r.status_code == 200, r.text
                                matching = [d for d in r.json()["drivers"] if d["driver_id"] == driver_id]
                                assert len(matching) == 1, "driver must appear exactly once despite four distinct sources"
                                drv = matching[0]
                                assert Decimal(str(drv["daily_pay"])) == Decimal("20.0000")
                                assert Decimal(str(drv["status_pay"])) == Decimal("60.0000")
                                assert Decimal(str(drv["period_pay"])) == Decimal("5.00")
                                assert Decimal(str(drv["bonus_total"])) == Decimal("3.00")
                                assert Decimal(str(drv["normal_base"])) == Decimal("85.0000"), "20 + 60 + 5, bonus excluded from base"
                                assert Decimal(str(drv["expected_pay"])) == Decimal("88.0000"), "85 base + 3 bonus, no min/max rule active"


# ---------------------------------------------------------------------------
# 6b. Structural blockers (Codex P1-A/P1-C fix) -- CP-4B must surface the
# same structural findings `_validate_period_can_finalize` reports for
# finalization-preview: duplicate active Daily lines, driver eligibility
# violations (Daily and Period-pay), contaminated/foreign RateType
# references, and unresolvable rate-type mapping.
#
# This module never mutates shared schema objects to manufacture a
# duplicate-line condition (that would require temporarily disabling the
# real active-Daily uniqueness index, a shared object other tests depend
# on). Real DETECTION of genuine duplicate rows against that live index is
# already proven by test_payroll_trust_p7.py::test_p7_t1_preview_blocks_duplicate_daily_lines
# (untouched by this task, its own isolated setup/teardown). CP-4B only
# needs to prove it PROPAGATES whatever the validator reports, which the
# monkeypatch-based test below verifies against the actual HTTP endpoint.
# ---------------------------------------------------------------------------

class TestStructuralBlockers:

    @pytest.mark.asyncio
    async def test_duplicate_blocker_propagates_from_validator(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db, monkeypatch,
    ):
        """
        Integration-boundary test: stubs `_validate_period_can_finalize` to
        return the real duplicate-Daily-line wording that
        test_payroll_trust_p7.py's own (untouched, already-existing) test
        asserts detection of, then proves the actual calculation-preview
        endpoint surfaces it with HTTP 200 and has_blockers=True. This
        proves propagation without ever touching shared schema.
        """
        from app.payroll import period_calculation

        real_duplicate_text = (
            "Cannot finalize: duplicate active Daily draft lines detected "
            "(driver 1 2199-02-04 DailyNote ×2). Void the extra lines before finalizing."
        )

        async def _stub_validator(*, period_id, company_id, branch_id, period_start, period_end, db):
            return [real_duplicate_text]

        # Stage B4-17: _validate_period_can_finalize now lives in
        # app.payroll.period_calculation, and _build_live_calculation_packet
        # (also in period_calculation) resolves it as a bare name through
        # that module's own globals — patching app.payroll.service's
        # compatibility re-export no longer intercepts it.
        monkeypatch.setattr(period_calculation, "_validate_period_can_finalize", _stub_validator)

        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["has_blockers"] is True
            assert real_duplicate_text in body["blockers"]
            assert any("duplicate" in b.lower() for b in body["blockers"])

    @pytest.mark.asyncio
    async def test_ineligible_driver_line_surfaced(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """The period has no eligibility snapshot (never submitted), so
        `_validate_period_can_finalize` uses its live core.drivers/
        core.employees eligibility check -- terminating the employee before
        the line's work_date makes it ineligible immediately, without
        needing to advance the period through InReview/Approved."""
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B Inelig") as driver_id:
                emp_row = (await direct_db.execute(
                    _text("SELECT employeeid FROM core.drivers WHERE driverid = :id"), {"id": driver_id},
                )).mappings().first()
                emp_id = emp_row["employeeid"]

                line = await session_client.post(
                    f"/payroll/periods/{pid}/lines",
                    json={"driver_id": driver_id, "work_date": DATE_FEB04, "line_type": "DailyNote", "notes": "will be ineligible"},
                    headers=auth(auth_token),
                )
                assert line.status_code == 201, line.text

                term_date = datetime.date.fromisoformat(DATE_FEB04) - datetime.timedelta(days=1)
                try:
                    await direct_db.execute(
                        _text("UPDATE core.employees SET terminationdate = :td WHERE employeeid = :eid"),
                        {"td": term_date, "eid": emp_id},
                    )

                    r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                    assert r.status_code == 200, r.text
                    body = r.json()
                    assert body["has_blockers"] is True
                    blockers_text = " ".join(body["blockers"]).lower()
                    assert "eligible" in blockers_text or "ineligible" in blockers_text, (
                        f"expected an eligibility blocker; got: {body['blockers']}"
                    )
                finally:
                    await direct_db.execute(
                        _text("UPDATE core.employees SET terminationdate = NULL WHERE employeeid = :eid"),
                        {"eid": emp_id},
                    )

    @pytest.mark.asyncio
    async def test_period_pay_ineligible_driver_line_surfaced(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        """
        Distinct validator category from the Daily-eligibility test above:
        `_validate_period_can_finalize`'s Period-Pay eligibility check uses
        the period's overlap window (EffectiveFrom/EffectiveTo vs.
        period start/end), not a single WorkDate. Terminating the employee
        before the period's own start date makes a Period-scope line
        ineligible for the whole period. Constructed via the same safe
        direct-insert convention already used for period-pay lines
        elsewhere in this file -- no shared-schema mutation.
        """
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B PeriodPayInelig") as driver_id:
                emp_row = (await direct_db.execute(
                    _text("SELECT employeeid FROM core.drivers WHERE driverid = :id"), {"id": driver_id},
                )).mappings().first()
                emp_id = emp_row["employeeid"]

                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrolldraftlines
                            (companyid, branchid, payrollperiodid, driverid,
                             workdate, linetype, linescope, quantity,
                             calculatedamount, sourcetype, status, needsmanagerreview, addedbyuserid)
                        VALUES
                            (1, :bid, :pid, :did, NULL, 'ADJUSTMENT', 'Period', 1,
                             10.00, 'Manual', 'Active', FALSE, 1)
                    """),
                    {"bid": paytest_branch_id, "pid": pid, "did": driver_id},
                )

                term_date = datetime.date.fromisoformat(PERIOD_START) - datetime.timedelta(days=1)
                try:
                    await direct_db.execute(
                        _text("UPDATE core.employees SET terminationdate = :td WHERE employeeid = :eid"),
                        {"td": term_date, "eid": emp_id},
                    )

                    r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                    assert r.status_code == 200, r.text
                    body = r.json()
                    assert body["has_blockers"] is True
                    blockers_text = " ".join(body["blockers"]).lower()
                    assert "eligible" in blockers_text or "ineligible" in blockers_text, (
                        f"expected a Period-Pay eligibility blocker; got: {body['blockers']}"
                    )
                finally:
                    await direct_db.execute(
                        _text("UPDATE core.employees SET terminationdate = NULL WHERE employeeid = :eid"),
                        {"eid": emp_id},
                    )

# ---------------------------------------------------------------------------
# 6c. Payroll fixture acquisition-failure regression matrix (Codex P1-B fix)
#
# Six required resources, each with its own focused failure-after-write
# test: driver, DriverRate, payroll period, DraftLine, bonus event, and
# Status configuration. Every case injects failure strictly AFTER the
# persistent write commits but BEFORE the normal code path would extract
# its ID -- production code is never touched, injection is entirely
# test-local (`_inject_failure_after_write` params on the fixture helpers
# above).
# ---------------------------------------------------------------------------

class TestPayrollAcquisitionFailure:

    @pytest.mark.asyncio
    async def test_driver_cleanup_survives_pre_parse_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        marker_name = f"CP4B-DRV-FAIL-{uuid.uuid4().hex[:8]}"
        driver_id = None
        with pytest.raises(_DeliberateSetupFailure):
            async with _owned_driver_and_employee(
                session_client, auth_token, paytest_branch_id, direct_db, name=marker_name,
                _inject_failure_after_write=True,
            ):
                pytest.fail("must not reach the driver context body -- injection raises first")

        row = (await direct_db.execute(
            _text("""
                SELECT d.driverid FROM core.drivers d
                JOIN core.employees e ON e.employeeid = d.employeeid
                WHERE e.fullname LIKE :m
            """),
            {"m": f"{marker_name}%"},
        )).mappings().first()
        # `_owned_driver_and_employee`'s own `finally` already recovered the
        # driver via the exact fullname marker and asserted zero row/audit
        # residue (the authoritative, non-vacuous proof). This re-confirms
        # via the same marker after the fact.
        assert row is None, f"Driver matching marker {marker_name!r} leaked after a pre-parse failure"

    @pytest.mark.asyncio
    async def test_driver_rate_cleanup_survives_pre_parse_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_driver_and_employee(
            session_client, auth_token, paytest_branch_id, direct_db, name="CP4B RateFail",
        ) as driver_id:
            hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
            with pytest.raises(_DeliberateSetupFailure):
                async with _owned_driver_rate(
                    direct_db, session_client, auth_token, driver_id, hourly_rt_id,
                    amount="15.0000", effective_from=DATE_FEB04, _inject_failure_after_write=True,
                ):
                    pytest.fail("must not reach the rate context body -- injection raises first")

            residue = (await direct_db.execute(
                _text("SELECT COUNT(*) AS c FROM payroll.driverrates WHERE driverid = :id"), {"id": driver_id},
            )).mappings().first()
            assert residue["c"] == 0, f"DriverRate for driver {driver_id} leaked after a pre-parse failure"

    @pytest.mark.asyncio
    async def test_period_cleanup_survives_pre_parse_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        code = _generate_unique_period_code(paytest_branch_id, suffix="-periodfail")
        with pytest.raises(_DeliberateSetupFailure):
            async with _owned_period(
                session_client, auth_token, paytest_branch_id, direct_db,
                status="Open", _inject_failure_after_write=True, _code_override=code,
            ):
                pytest.fail("must not reach the period context body -- injection raises first")

        residue = (await direct_db.execute(
            _text("SELECT COUNT(*) AS c FROM payroll.payrollperiods WHERE companyid = 1 AND branchid = :bid AND periodcode = :code"),
            {"bid": paytest_branch_id, "code": code},
        )).mappings().first()
        assert residue["c"] == 0, f"Period {code!r} leaked after a pre-parse failure"

    @pytest.mark.asyncio
    async def test_draft_line_cleanup_survives_pre_parse_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(
                session_client, auth_token, paytest_branch_id, direct_db, name="CP4B LineFail",
            ) as driver_id:
                with pytest.raises(_DeliberateSetupFailure):
                    async with _owned_draft_line(
                        session_client, auth_token, direct_db,
                        period_id=pid, driver_id=driver_id, work_date=DATE_FEB04, line_type="DailyNote",
                        _inject_failure_after_write=True,
                    ):
                        pytest.fail("must not reach the draft-line context body -- injection raises first")

                residue = (await direct_db.execute(
                    _text("""
                        SELECT COUNT(*) AS c FROM payroll.payrolldraftlines
                        WHERE payrollperiodid = :pid AND driverid = :did AND status != 'Void'
                    """),
                    {"pid": pid, "did": driver_id},
                )).mappings().first()
                assert residue["c"] == 0, f"DraftLine for driver {driver_id} leaked after a pre-parse failure"

    @pytest.mark.asyncio
    async def test_bonus_event_cleanup_survives_pre_parse_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(
                session_client, auth_token, paytest_branch_id, direct_db, name="CP4B BonusFail",
            ) as driver_id:
                with pytest.raises(_DeliberateSetupFailure):
                    async with _owned_bonus_event(
                        session_client, auth_token, direct_db,
                        period_id=pid, driver_id=driver_id, amount="12.00", _inject_failure_after_write=True,
                    ):
                        pytest.fail("must not reach the bonus-event context body -- injection raises first")

                residue = (await direct_db.execute(
                    _text("SELECT COUNT(*) AS c FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid AND driverid = :did"),
                    {"pid": pid, "did": driver_id},
                )).mappings().first()
                assert residue["c"] == 0, f"BonusEvent for driver {driver_id} leaked after a pre-parse failure"

    @pytest.mark.asyncio
    async def test_status_configuration_cleanup_survives_pre_parse_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        code = f"CP4BSTATFAIL{uuid.uuid4().hex[:6]}"
        async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
            with pytest.raises(_DeliberateSetupFailure):
                async with _owned_status_key(
                    direct_db, 1, paytest_branch_id, code, "8.00", src_col_id, _inject_failure_after_write=True,
                ):
                    pytest.fail("must not reach the status-key context body -- injection raises first")

            residue = (await direct_db.execute(
                _text("SELECT COUNT(*) AS c FROM payroll.payrollstatuskeys WHERE statuscode = :code"), {"code": code},
            )).mappings().first()
            assert residue["c"] == 0, f"StatusKey {code!r} leaked after a pre-parse failure"

    @pytest.mark.asyncio
    async def test_driver_pay_rule_cleanup_survives_pre_parse_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_driver_and_employee(
            session_client, auth_token, paytest_branch_id, direct_db, name="CP4B PayRuleFail",
        ) as driver_id:
            with pytest.raises(_DeliberateSetupFailure):
                async with _owned_driver_pay_rule(
                    session_client, auth_token, direct_db, driver_id=driver_id, branch_id=paytest_branch_id,
                    rule_type="MinimumPay", amount="100.00",
                    effective_from=PERIOD_START, effective_to=PERIOD_END,
                    _inject_failure_after_write=True,
                ):
                    pytest.fail("must not reach the pay-rule context body -- injection raises first")

            residue = (await direct_db.execute(
                _text("""
                    SELECT
                        (SELECT COUNT(*) FROM payroll.driverpayrules WHERE driverid = :did) AS rules,
                        (SELECT COUNT(*) FROM audit.auditlog WHERE entityname = 'DriverPayRules'
                            AND entityid IN (SELECT driverpayruleid::text FROM payroll.driverpayrules WHERE driverid = :did)) AS rule_audit
                """),
                {"did": driver_id},
            )).mappings().first()
            assert residue["rules"] == 0, f"DriverPayRule for driver {driver_id} leaked after a pre-parse failure"


# ---------------------------------------------------------------------------
# 6d. Shared-schema safety (Codex P1-C fix)
#
# `_shared_daily_index_lifetime_guard` is a module-scoped, autouse fixture:
# its setup runs once BEFORE the first test in this module and its teardown
# runs once AFTER the last -- so the before/after comparison spans this
# module's ENTIRE execution, not two immediate reads inside a single test.
# It captures catalog state (never bare name presence) via pg_class +
# pg_namespace + pg_index: the index's own OID (a stable physical identity
# -- unchanged across the module's run proves no DROP+CREATE-under-the-
# -same-name occurred), its full definition, and indisvalid/indisready/
# indislive/indisunique. It reuses the SAME test database this module's
# `direct_db`/`session_client` already talk to (via the session-scoped
# `test_database_url` fixture from conftest.py) -- never a separate,
# unrelated database.
# ---------------------------------------------------------------------------

_DAILY_UNIQUE_INDEX_NAME = "uix_payrolldraftlines_daily_active_business_key"

_INDEX_CATALOG_QUERY = """
    SELECT
        ic.oid                    AS index_oid,
        n.nspname                 AS schema_name,
        tc.relname                AS table_name,
        ic.relname                AS index_name,
        pg_get_indexdef(ic.oid)   AS index_def,
        idx.indisvalid,
        idx.indisready,
        idx.indislive,
        idx.indisunique
    FROM   pg_class      ic
    JOIN   pg_namespace  n   ON n.oid = ic.relnamespace
    JOIN   pg_index      idx ON idx.indexrelid = ic.oid
    JOIN   pg_class      tc  ON tc.oid = idx.indrelid
    WHERE  ic.relname = :name AND n.nspname = 'payroll'
"""


async def _capture_daily_index_state_via_connection(conn) -> dict:
    rows = (await conn.execute(_text(_INDEX_CATALOG_QUERY), {"name": _DAILY_UNIQUE_INDEX_NAME})).mappings().all()
    if len(rows) != 1:
        raise AssertionError(
            f"Expected exactly one catalog row for index {_DAILY_UNIQUE_INDEX_NAME!r} in "
            f"schema 'payroll'; found {len(rows)}"
        )
    return dict(rows[0])


def _assert_index_state_valid(state: dict) -> None:
    assert state["schema_name"] == "payroll"
    assert state["table_name"] == "payrolldraftlines"
    assert state["indisvalid"] is True, f"indisvalid must be true: {state}"
    assert state["indisready"] is True, f"indisready must be true: {state}"
    assert state["indislive"] is True, f"indislive must be true: {state}"
    assert state["indisunique"] is True, f"indisunique must be true: {state}"


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _shared_daily_index_lifetime_guard(test_database_url):
    """
    Module-lifetime guard (Codex P1 fix): captures the required shared
    Daily-line uniqueness index's exact catalog state before the first test
    in this module runs, and asserts exact equality (same OID, same
    definition, same flags) after the last test finishes. Never recreates
    or repairs the index -- a mismatch is a hard failure, not a self-heal.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(test_database_url, echo=False, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        before = await _capture_daily_index_state_via_connection(conn)
    _assert_index_state_valid(before)

    yield before

    async with engine.connect() as conn:
        after = await _capture_daily_index_state_via_connection(conn)
    await engine.dispose()
    assert after == before, (
        f"shared Daily-line uniqueness index catalog state changed across this "
        f"module's full test run:\nbefore={before}\nafter={after}"
    )
    _assert_index_state_valid(after)


class TestSharedSchemaSafety:

    @pytest.mark.asyncio
    async def test_index_state_mid_run_matches_module_start_snapshot(
        self, direct_db, _shared_daily_index_lifetime_guard,
    ):
        """A read taken mid-suite (via the same connection style every other
        test in this module uses) matches the module-start snapshot
        exactly, including OID identity -- proves no test in this run
        recreated the index under the same name."""
        current = await _capture_daily_index_state_via_connection(direct_db)
        assert current == _shared_daily_index_lifetime_guard
        _assert_index_state_valid(current)

    def test_this_test_file_never_touches_shared_schema_objects(self):
        """Source-scan self-check (defense-in-depth only -- the authoritative
        proof is the catalog-based module fixture above, which observes
        actual execution rather than source text). This file must never
        contain a schema-DDL statement. Search tokens are built via
        concatenation so this check itself never contains the literal
        phrases it searches for."""
        source = pathlib.Path(__file__).read_text(encoding="utf-8").upper()
        forbidden = (
            "DROP" + " INDEX",
            "CREATE" + " INDEX",
            "CREATE" + " UNIQUE INDEX",
            "ALTER" + " INDEX",
            "ALTER" + " TABLE",
            "DROP" + " CONSTRAINT",
            "CREATE" + " TABLE",
            "DROP" + " TABLE",
        )
        for token in forbidden:
            assert token not in source, f"forbidden schema-DDL token found in this test file: {token!r}"


# ---------------------------------------------------------------------------
# 6e. PeriodCode UUID-retention safety (Codex P3 fix)
# ---------------------------------------------------------------------------

class TestPeriodCodeSafety:

    @pytest.mark.parametrize("suffix_len", [0, 5, 79, 80, 200])
    def test_various_suffix_lengths_respect_varchar80_and_retain_full_token(self, monkeypatch, suffix_len):
        fixed_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        monkeypatch.setattr(uuid, "uuid4", lambda: fixed_uuid)
        suffix = "S" * suffix_len
        code = _generate_unique_period_code(branch_id=2, suffix=suffix)
        assert len(code) <= _PERIOD_CODE_MAX_LEN, f"suffix_len={suffix_len}: code {code!r} exceeds VARCHAR({_PERIOD_CODE_MAX_LEN})"
        assert code.endswith(fixed_uuid.hex), (
            f"suffix_len={suffix_len}: code {code!r} does not end with the complete UUID token"
        )

    def test_repeated_identical_long_suffix_still_produces_distinct_codes(self):
        long_suffix = "Q" * 150
        codes = [_generate_unique_period_code(branch_id=2, suffix=long_suffix) for _ in range(5)]
        assert len(set(codes)) == len(codes), f"Expected 5 distinct codes; got {codes}"

    def test_empty_suffix_codes_never_collide(self):
        first = _generate_unique_period_code(branch_id=2)
        second = _generate_unique_period_code(branch_id=2)
        assert first != second, "Two default-suffix PeriodCode generations must never collide"
        assert len(first) <= _PERIOD_CODE_MAX_LEN and len(second) <= _PERIOD_CODE_MAX_LEN


# ---------------------------------------------------------------------------
# 6f. Independent-cleanup regression proof (Codex P1 fix)
#
# Proves `_CleanupRunner` itself: one cleanup operation deliberately fails
# (via a test-local injected callback, never by corrupting shared schema or
# real SQL) while other, independent cleanup operations still run and
# succeed, the failure is surfaced (not swallowed), and -- separately --
# that an original exception already propagating through a `finally` is
# never silently replaced by a subsequent cleanup failure.
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def _owned_cleanup_runner_roles(
    session_client,
    auth_token,
    direct_db,
    *,
    _inject_failure_after_role_write: str | None = None,
):
    """Own the CompanyRoles used to exercise `_CleanupRunner` itself.

    The three role names are predeclared before the first POST. If an admin
    create request commits but its response is never parsed, teardown recovers
    its exact ID by (CompanyID, RoleName), removes its exact CompanyRoles audit
    entries, and then removes only that owned role.
    """
    marker = f"cp4bcleanup{uuid.uuid4().hex[:10]}"
    role_names = {key: f"{marker}{key}" for key in ("A", "B", "C")}
    role_ids: dict[str, int | None] = {key: None for key in role_names}
    try:
        for key, role_name in role_names.items():
            response = await session_client.post(
                "/admin/company-roles",
                json={"role_name": role_name},
                headers=auth(auth_token),
            )
            assert response.status_code == 201, f"Create cleanup-runner role {key} failed: {response.text}"
            if _inject_failure_after_role_write == key:
                raise _DeliberateSetupFailure(
                    f"simulated failure after cleanup-runner role {key} write committed, before ID extraction"
                )
            role_ids[key] = response.json()["company_role_id"]
        yield role_ids, role_names
    finally:
        for key, role_name in role_names.items():
            if role_ids[key] is None:
                row = (await direct_db.execute(
                    _text("""
                        SELECT companyroleid FROM sec.companyroles
                        WHERE companyid = 1 AND rolename = :role_name
                    """),
                    {"role_name": role_name},
                )).mappings().all()
                assert len(row) <= 1, f"Cleanup-runner role recovery matched multiple rows for {role_name!r}"
                if row:
                    role_ids[key] = row[0]["companyroleid"]

        runner = _CleanupRunner()
        for key, role_id in role_ids.items():
            if role_id is None:
                continue
            await runner.execute(
                direct_db,
                "DELETE FROM audit.auditlog WHERE entityname = 'CompanyRoles' AND entityid = :eid",
                {"eid": str(role_id)},
                label=f"delete cleanup-runner role {key} audit",
            )
            await runner.execute(
                direct_db,
                "DELETE FROM sec.companyroles WHERE companyroleid = :rid",
                {"rid": role_id},
                label=f"delete cleanup-runner role {key}",
            )

        async def _residue():
            for key, role_id in role_ids.items():
                if role_id is None:
                    continue
                row = (await direct_db.execute(
                    _text("""
                        SELECT
                            (SELECT COUNT(*) FROM sec.companyroles WHERE companyroleid = :rid) AS roles,
                            (SELECT COUNT(*) FROM audit.auditlog
                                WHERE entityname = 'CompanyRoles' AND entityid = :eid) AS audit_rows
                    """),
                    {"rid": role_id, "eid": str(role_id)},
                )).mappings().first()
                assert row["roles"] == 0 and row["audit_rows"] == 0, (
                    f"cleanup-runner role {key} residue: {dict(row)}"
                )

        await runner.check(_residue, label="cleanup-runner role residue assertion")
        runner.raise_if_any()

class TestIndependentCleanupRegression:

    @pytest.mark.asyncio
    async def test_later_independent_cleanup_operations_still_run_after_one_failure(
        self, session_client: httpx.AsyncClient, auth_token: str, direct_db,
    ):
        """
        Three independent test-owned rows are created. The SECOND row's
        cleanup operation is deliberately made to fail via a test-local
        callback. Proves: the first and third rows' cleanup operations still
        run and succeed independently, the injected failure is surfaced via
        ExceptionGroup, and no blanket exception handling occurs.
        """
        class _InjectedCleanupFailure(Exception):
            pass

        async with _owned_cleanup_runner_roles(session_client, auth_token, direct_db) as (role_ids, _role_names):
            runner = _CleanupRunner()

            async def _fail_step():
                raise _InjectedCleanupFailure("deliberate cleanup failure for owned role B")

            await runner.execute(
                direct_db,
                "DELETE FROM sec.companyroles WHERE companyroleid = :rid",
                {"rid": role_ids["A"]},
                label="delete A",
            )
            await runner.check(_fail_step, label="delete B (deliberately fails)")
            await runner.execute(
                direct_db,
                "DELETE FROM sec.companyroles WHERE companyroleid = :rid",
                {"rid": role_ids["C"]},
                label="delete C",
            )

            with pytest.raises(ExceptionGroup) as exc_info:
                runner.raise_if_any()
            assert len(exc_info.value.exceptions) == 1
            assert isinstance(exc_info.value.exceptions[0], _InjectedCleanupFailure)

            residue = (await direct_db.execute(
                _text("SELECT companyroleid FROM sec.companyroles WHERE companyroleid IN (:a, :b, :c)"),
                {"a": role_ids["A"], "b": role_ids["B"], "c": role_ids["C"]},
            )).mappings().all()
            remaining_ids = {r["companyroleid"] for r in residue}
            assert remaining_ids == {role_ids["B"]}, (
                "roles A and C must be independently removed despite B's cleanup failure; "
                f"remaining: {remaining_ids}"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("injection_point", ["A", "B", "C"])
    async def test_cleanup_runner_role_acquisition_is_owned_before_response_parsing(
        self, session_client: httpx.AsyncClient, auth_token: str, direct_db, injection_point,
    ):
        """Each persisted admin-created role remains recoverable by its
        predeclared unique name when failure fires before response parsing."""
        with pytest.raises(_DeliberateSetupFailure):
            async with _owned_cleanup_runner_roles(
                session_client,
                auth_token,
                direct_db,
                _inject_failure_after_role_write=injection_point,
            ):
                pytest.fail("must not reach the owned-role body after acquisition injection")

    @pytest.mark.asyncio
    async def test_original_and_cleanup_failures_both_remain_visible(self):
        """
        Proves Python's standard exception-chaining semantics, which every
        `_CleanupRunner`-based `finally` block in this file relies on:
        raising a new ExceptionGroup from a `finally` block while an
        original exception is already propagating attaches the original as
        `__context__` instead of discarding it -- the original failure is
        never silently replaced.
        """
        class _OriginalFailure(Exception):
            pass

        class _InjectedCleanupFailure(Exception):
            pass

        runner = _CleanupRunner()

        async def _fail_cleanup():
            raise _InjectedCleanupFailure("deliberate cleanup failure")

        caught: BaseException | None = None
        try:
            try:
                raise _OriginalFailure("deliberate original failure")
            finally:
                await runner.check(_fail_cleanup, label="poisoned step")
                runner.raise_if_any()
        except BaseException as exc:  # noqa: BLE001 -- inspected below, never swallowed
            caught = exc

        assert isinstance(caught, ExceptionGroup), f"expected ExceptionGroup, got {type(caught)}"
        assert isinstance(caught.exceptions[0], _InjectedCleanupFailure)
        assert isinstance(caught.__context__, _OriginalFailure), (
            "the original failure must remain attached via __context__, not silently discarded"
        )


# ---------------------------------------------------------------------------
# 7. Absolute read-only proof
# ---------------------------------------------------------------------------

class TestReadOnlyProof:

    @pytest.mark.asyncio
    async def test_successful_preview_causes_no_db_change(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B ReadOnly") as driver_id:
                hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
                async with _owned_driver_rate(direct_db, session_client, auth_token, driver_id, hourly_rt_id, "15.0000", DATE_FEB04):
                    line = await session_client.post(
                        f"/payroll/periods/{pid}/lines",
                        json={"driver_id": driver_id, "work_date": DATE_FEB04, "line_type": "HOURS", "quantity": "4.0000"},
                        headers=auth(auth_token),
                    )
                    assert line.status_code == 201, line.text

                    before = await _get_full_db_snapshot(direct_db, pid, driver_ids=[driver_id])
                    r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                    assert r.status_code == 200, r.text
                    after = await _get_full_db_snapshot(direct_db, pid, driver_ids=[driver_id])

                    assert before == after, "successful calculation-preview must not change any row"

    @pytest.mark.asyncio
    async def test_blocker_path_also_causes_no_db_change(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
    ):
        async with _owned_period(session_client, auth_token, paytest_branch_id, direct_db, status="Open") as pid:
            async with _owned_driver_and_employee(session_client, auth_token, paytest_branch_id, direct_db, name="CP4B ReadOnlyBlocker") as driver_id:
                line = await session_client.post(
                    f"/payroll/periods/{pid}/lines",
                    json={"driver_id": driver_id, "work_date": DATE_FEB04, "line_type": "HOURS", "quantity": "3.0000"},
                    headers=auth(auth_token),
                )
                assert line.status_code == 201, line.text  # no approved rate -> blocker path

                before = await _get_full_db_snapshot(direct_db, pid, driver_ids=[driver_id])
                r = await session_client.get(f"/payroll/periods/{pid}/calculation-preview", headers=auth(auth_token))
                assert r.status_code == 200, r.text
                assert r.json()["has_blockers"] is True
                after = await _get_full_db_snapshot(direct_db, pid, driver_ids=[driver_id])

                assert before == after, "the missing-rate blocker path must also cause no DB change"
