"""Exact-ID teardown primitives shared by backend integration tests."""

from contextlib import asynccontextmanager

from sqlalchemy import text as _text

from tests.db_state import FINALIZED_HISTORY_TRIGGERS, suspended_test_triggers


class CleanupRunner:
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



MUTABLE_PERIOD_STATUSES = ("Draft", "Open", "InReview", "Returned", "Approved")


async def assert_no_mutable_period_state(db, branch_id: int) -> None:
    """Fail, naming IDs and statuses, if the branch retains any mutable workflow period.

    Locked/Archived/Cancelled periods are legitimate retained history; a period
    that can still be edited, reviewed, returned or approved is shared mutable
    state that later tests on the same root would inherit.
    """
    rows = (await db.execute(
        _text("""
            SELECT payrollperiodid, status FROM payroll.payrollperiods
            WHERE branchid = :bid AND status = ANY(:statuses)
            ORDER BY payrollperiodid
        """),
        {"bid": branch_id, "statuses": list(MUTABLE_PERIOD_STATUSES)},
    )).all()
    assert not rows, (
        f"branch {branch_id} retains mutable workflow periods: "
        f"{[(row[0], row[1]) for row in rows]}"
    )


@asynccontextmanager
async def preserved_company_profile(db, company_code: str = "DEMO"):
    """Capture the Company's exact mutable profile, and restore and verify it on exit.

    PATCH /settings/company rewrites the whole company-wide profile (name, legal name,
    notes, time zone, self-approval policy), which every other module reads. Restoration
    runs in ``finally`` -- whatever the body did, including raising -- and restores the
    values that were captured, never a hard-coded name. A restoration mismatch fails.
    """
    read = _text(
        "SELECT companyname, legalname, timezonename, notes, allowselfapproval, updatedatutc "
        "FROM core.companies WHERE companycode = :code"
    )
    original = dict((await db.execute(read, {"code": company_code})).mappings().one())
    try:
        yield original
    finally:
        await db.execute(
            _text("""
                UPDATE core.companies
                SET companyname = :name, legalname = :legal, timezonename = :tz,
                    notes = :notes, allowselfapproval = :allow_self, updatedatutc = :updated
                WHERE companycode = :code
            """),
            {"name": original["companyname"], "legal": original["legalname"],
             "tz": original["timezonename"], "notes": original["notes"],
             "allow_self": original["allowselfapproval"], "updated": original["updatedatutc"],
             "code": company_code},
        )
        restored = dict((await db.execute(read, {"code": company_code})).mappings().one())
        assert restored == original, f"company profile not restored: {original} -> {restored}"


async def cancel_active_branch_periods(client, token: str, branch_id: int) -> None:
    """Cancel a MODULE-OWNED branch's Draft/Open periods through the API.

    Only Draft and Open are legal PATCH->Cancelled transitions (CP-1A); other
    statuses are never requested here. A failed list or cancel fails the test --
    cleanup must not be silent. Never call this for a shared branch.
    """
    headers = {"Authorization": f"Bearer {token}"}
    for status in ("Draft", "Open"):
        listed = await client.get(
            "/payroll/periods", params={"branch_id": branch_id, "status": status}, headers=headers,
        )
        assert listed.status_code == 200, f"list {status} periods for cleanup failed: {listed.text}"
        for period in listed.json():
            cancelled = await client.patch(
                f"/payroll/periods/{period['payroll_period_id']}/status",
                json={"status": "Cancelled"}, headers=headers,
            )
            assert cancelled.status_code == 200, (
                f"cleanup could not cancel {status} period "
                f"{period['payroll_period_id']}: {cancelled.text}"
            )


async def retire_branch_periods_directly(
    db, branch_id: int, *, retain_finalized_history: bool = False,
) -> None:
    """Retire a MODULE-OWNED branch's non-API-cancellable periods, then require a terminal branch.

    InReview/Approved/Returned cannot be cancelled through PATCH. They can own review
    state: an InReview period has a Pending PeriodApproval ManagerReviewItem and a
    Returned period points at one through CurrentReturnReviewItemID. Retiring only the
    period status would leave that company-wide review residue behind after a failed
    test, so, using EXACT ownership only:

      1. collect the exact IDs of the branch's InReview/Approved/Returned periods;
      2. collect the exact ManagerReviewItems whose canonical entity reference
         (payroll / PayrollPeriods / <period id>) is one of those periods;
      3. cancel exactly those periods, clearing CurrentReturnReviewItemID (the
         pointer-consistency CHECK and the item FK both require it first);
      4. delete exactly those items with their Decisions and audit rows, and verify
         zero review residue for those IDs (``delete_review_items_and_children``).
         Items that immutable P6D evidence references (workflow-action or audit-
         evidence rows RESTRICT their deletion) are retained history by product
         rule; for exactly those, a Pending item is cancelled instead of deleted so
         no Pending review state survives either way.

    Locked/Archived additionally need the finalized-history guards suspended for
    exactly their UPDATE; their review history is never touched. The result must hold
    no mutable workflow period. With ``retain_finalized_history`` the Locked/Archived
    periods are left in place as legitimate immutable history. Never call this for a
    shared branch.
    """
    period_ids = [row[0] for row in (await db.execute(
        _text("""
            SELECT payrollperiodid FROM payroll.payrollperiods
            WHERE branchid = :bid AND status IN ('InReview', 'Approved', 'Returned')
            ORDER BY payrollperiodid
        """), {"bid": branch_id},
    )).all()]
    review_item_ids: list[int] = []
    if period_ids:
        review_item_ids = [row[0] for row in (await db.execute(
            _text("""
                SELECT reviewitemid FROM review.managerreviewitems
                WHERE branchid = :bid
                  AND entityschema = 'payroll' AND entityname = 'PayrollPeriods'
                  AND entityid = ANY(:entity_ids)
                ORDER BY reviewitemid
            """),
            {"bid": branch_id, "entity_ids": [str(i) for i in period_ids]},
        )).all()]
        await db.execute(
            _text("UPDATE payroll.payrollperiods "
                  "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
                  "WHERE payrollperiodid = ANY(:ids)"),
            {"ids": period_ids},
        )
        evidenced_ids = [row[0] for row in (await db.execute(
            _text("""
                SELECT reviewitemid FROM payroll.payrollperiodworkflowactionevidence
                WHERE reviewitemid = ANY(:ids)
                UNION
                SELECT reviewitemid FROM payroll.payrollperiodauditevidenceevents
                WHERE reviewitemid = ANY(:ids)
            """), {"ids": review_item_ids},
        )).all()]
        if evidenced_ids:
            await db.execute(
                _text("UPDATE review.managerreviewitems SET status = 'Cancelled' "
                      "WHERE reviewitemid = ANY(:ids) AND status = 'Pending'"),
                {"ids": evidenced_ids},
            )
        await delete_review_items_and_children(
            db, [item_id for item_id in review_item_ids if item_id not in set(evidenced_ids)],
        )
        pending = (await db.execute(
            _text("SELECT reviewitemid FROM review.managerreviewitems "
                  "WHERE reviewitemid = ANY(:ids) AND status = 'Pending'"),
            {"ids": review_item_ids},
        )).scalars().all()
        assert not pending, f"retired periods still own Pending review items: {pending}"
    if not retain_finalized_history:
        async with suspended_test_triggers(db, FINALIZED_HISTORY_TRIGGERS):
            await db.execute(
                _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                      "WHERE branchid = :bid AND status IN ('Locked', 'Archived')"),
                {"bid": branch_id},
            )
    await assert_no_mutable_period_state(db, branch_id)


async def delete_period_and_children(db, period_id: int) -> None:
    """
    Deletes an un-evidenced test period, or cancels it when P6D evidence exists.

    The current product deliberately retains periods after source mutations have
    created immutable audit evidence.  Cancellation releases the branch's
    mutable workflow slot without weakening that retention boundary.

    `add_draft_line`/bonus-event creation each write a
    real AuditLog row (`_write_line_audit`, entity_name='PayrollDraftLines'
    or 'PayrollBonusEvents') immediately after insert -- this captures the
    exact DraftLineIDs/BonusEventIDs for this period BEFORE the rows
    themselves are deleted, deletes their matching audit rows by exact
    EntityID (never a broad company/branch/time-window delete), then
    asserts zero residue for every row and audit type this period could
    have produced.
    """
    has_p6d_evidence = (await db.execute(
        _text("""
            SELECT EXISTS (
                SELECT 1 FROM payroll.payrollperiodauditevidencecoverage
                WHERE payrollperiodid = :pid
            ) OR EXISTS (
                SELECT 1 FROM payroll.payrollcalculationsnapshots
                WHERE payrollperiodid = :pid
            ) OR EXISTS (
                SELECT 1 FROM payroll.payrollperiodworkflowactionevidence
                WHERE payrollperiodid = :pid
            ) AS present
        """), {"pid": period_id},
    )).scalar_one()
    if has_p6d_evidence:
        await db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )
        await db.commit()
        return

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

    runner = CleanupRunner()
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


async def delete_driver_and_residue(db, driver_id: int, employee_id: int | None) -> None:
    """
    Hard-deletes exactly the test-owned driver/employee pair and every
    dependent row scoped to this exact driverid, then verifies every exact
    Driver, Employee, dependent-row, and related audit residue query. `core.drivers`/`core.employees`
    creation writes NO AuditLog row (confirmed by reading app/core/service.py
    -- no `entityname` literal for 'Drivers'/'Employees' exists anywhere in
    that module); the audit deletes below for those two entities are
    therefore defensive no-ops, kept so the residue assertion still covers
    the (currently always-zero) audit case explicitly rather than silently
    assuming it.

    This context unwinds BEFORE its enclosing `_owned_period` (LIFO nesting)
    and must delete this driver's DraftLines/BonusEvents here to satisfy
    fk_DraftLines_Driver/fk_BonusEvents_Driver before the driver row itself
    can be deleted -- which means `delete_period_and_children`'s own later
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

    runner = CleanupRunner()
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
    async def _residue():
        residue = (await db.execute(
            _text("""
                SELECT
                    (SELECT COUNT(*) FROM core.drivers WHERE driverid = :did) AS drivers,
                    (SELECT COUNT(*) FROM core.employees WHERE employeeid = :eid) AS employees,
                    (SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverid = :did) AS final_lines,
                    (SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE driverid = :did) AS draft_lines,
                    (SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE driverid = :did) AS bonuses,
                    (SELECT COUNT(*) FROM payroll.payrollperioddriverdayentrystate WHERE driverid = :did) AS day_state,
                    (SELECT COUNT(*) FROM payroll.driverrates WHERE driverid = :did) AS rates,
                    (SELECT COUNT(*) FROM payroll.driverpayrules WHERE driverid = :did) AS pay_rules,
                    (SELECT COUNT(*) FROM audit.auditlog
                     WHERE entityname = 'PayrollDraftLines' AND entityid = ANY(:dlids)) AS draft_line_audit,
                    (SELECT COUNT(*) FROM audit.auditlog
                     WHERE entityname = 'PayrollBonusEvents' AND entityid = ANY(:beids)) AS bonus_audit,
                    (SELECT COUNT(*) FROM audit.auditlog
                     WHERE entityname = 'DriverPayRules' AND entityid = ANY(:ruleids)) AS rule_audit,
                    (SELECT COUNT(*) FROM audit.auditlog
                     WHERE entityname = 'Drivers' AND entityid = :did_text) AS driver_audit,
                    (SELECT COUNT(*) FROM audit.auditlog
                     WHERE entityname = 'Employees' AND entityid = :eid_text) AS employee_audit
            """), {
                "did": driver_id, "eid": employee_id,
                "dlids": draft_line_id_strs, "beids": bonus_event_id_strs,
                "ruleids": pay_rule_id_strs, "did_text": str(driver_id),
                "eid_text": str(employee_id) if employee_id is not None else "",
            },
        )).mappings().one()
        assert all(value == 0 for value in residue.values()), (
            f"Owned Driver cleanup left residue for {driver_id}: {dict(residue)}"
        )

    await runner.check(_residue, label=f"driver {driver_id} residue assertion")
    runner.raise_if_any()



async def delete_review_items_and_children(db, item_ids: list[int]) -> None:
    """Delete exactly the given ad-hoc ManagerReviewItems with their Decisions and audit.

    Review-domain writes record one audit entity ('ManagerReviewItems', EntityID =
    the item id). Every statement is keyed by the exact IDs, and the residue
    assertion covers every table and audit type those items could have produced.
    """
    if not item_ids:
        return
    id_strs = [str(i) for i in item_ids]
    runner = CleanupRunner()
    await runner.execute(db,
        "DELETE FROM audit.auditlog WHERE entityname = 'ManagerReviewItems' AND entityid = ANY(:ids)",
        {"ids": id_strs}, label="delete ManagerReviewItems audit")
    await runner.execute(db,
        "DELETE FROM review.managerreviewdecisions WHERE reviewitemid = ANY(:ids)",
        {"ids": item_ids}, label="delete ManagerReviewDecisions rows")
    await runner.execute(db,
        "DELETE FROM review.managerreviewitems WHERE reviewitemid = ANY(:ids)",
        {"ids": item_ids}, label="delete ManagerReviewItems rows")

    async def _residue():
        residue = (await db.execute(
            _text("""
                SELECT
                    (SELECT COUNT(*) FROM review.managerreviewitems WHERE reviewitemid = ANY(:ids)) AS items,
                    (SELECT COUNT(*) FROM review.managerreviewdecisions WHERE reviewitemid = ANY(:ids)) AS decisions,
                    (SELECT COUNT(*) FROM audit.auditlog
                     WHERE entityname = 'ManagerReviewItems' AND entityid = ANY(:id_strs)) AS item_audit
            """), {"ids": item_ids, "id_strs": id_strs},
        )).mappings().one()
        assert all(value == 0 for value in residue.values()), (
            f"Owned review item cleanup left residue for {item_ids}: {dict(residue)}"
        )

    await runner.check(_residue, label=f"review items {item_ids} residue assertion")
    runner.raise_if_any()


async def delete_user_access_state(db, user_id: int) -> None:
    """Delete one provisioned test User and its exact Access rows and audit."""
    user = (await db.execute(
        _text("SELECT employeeid FROM sec.users WHERE userid = :uid"), {"uid": user_id},
    )).mappings().first()
    if user is not None:
        assert user["employeeid"] is None, (
            f"Owned test User {user_id} unexpectedly has an Employee link"
        )

    assignment_ids = [row[0] for row in (await db.execute(
        _text("SELECT userbranchroleid FROM sec.userbranchroles WHERE userid = :uid"),
        {"uid": user_id},
    )).all()]
    assignment_entity_ids = [str(value) for value in assignment_ids] or [""]

    runner = CleanupRunner()
    await runner.execute(
        db,
        "DELETE FROM audit.auditlog WHERE entityname = 'UserPermissionOverrides' AND entityid = :eid",
        {"eid": str(user_id)}, label="delete exact UserPermissionOverrides audit",
    )
    await runner.execute(
        db, "DELETE FROM sec.userpermissionoverrides WHERE userid = :uid", {"uid": user_id},
        label="delete exact UserPermissionOverrides rows",
    )
    await runner.execute(
        db, "DELETE FROM sec.userbranchroles WHERE userid = :uid", {"uid": user_id},
        label="delete exact UserBranchRoles rows",
    )
    await runner.execute(
        db,
        "DELETE FROM audit.auditlog WHERE entityname = 'UserBranchRoles' AND entityid = ANY(:ids)",
        {"ids": assignment_entity_ids}, label="delete exact UserBranchRoles audit",
    )
    await runner.execute(
        db, "DELETE FROM audit.auditlog WHERE entityname = 'Users' AND entityid = :eid",
        {"eid": str(user_id)}, label="delete exact Users audit",
    )
    await runner.execute(
        db, "DELETE FROM sec.users WHERE userid = :uid", {"uid": user_id},
        label="delete exact User row",
    )

    async def _residue():
        residue = (await db.execute(
            _text("""
                SELECT
                    (SELECT COUNT(*) FROM sec.users WHERE userid = :uid) AS users,
                    (SELECT COUNT(*) FROM audit.auditlog
                     WHERE entityname = 'Users' AND entityid = :uid_text) AS user_audit,
                    (SELECT COUNT(*) FROM sec.userbranchroles WHERE userid = :uid) AS assignments,
                    (SELECT COUNT(*) FROM audit.auditlog
                     WHERE entityname = 'UserBranchRoles' AND entityid = ANY(:assignment_ids)) AS assignment_audit,
                    (SELECT COUNT(*) FROM sec.userpermissionoverrides WHERE userid = :uid) AS overrides,
                    (SELECT COUNT(*) FROM audit.auditlog
                     WHERE entityname = 'UserPermissionOverrides' AND entityid = :uid_text) AS override_audit
            """), {
                "uid": user_id, "uid_text": str(user_id),
                "assignment_ids": assignment_entity_ids,
            },
        )).mappings().one()
        assert all(value == 0 for value in residue.values()), (
            f"Owned User cleanup left residue for {user_id}: {dict(residue)}"
        )

    await runner.check(_residue, label=f"user {user_id} residue assertion")
    runner.raise_if_any()
