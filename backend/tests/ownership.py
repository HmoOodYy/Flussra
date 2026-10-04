"""Exact-ID teardown primitives shared by backend integration tests."""

from sqlalchemy import text as _text


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



async def delete_period_and_children(db, period_id: int) -> None:
    """
    Deletes an un-evidenced test period, or cancels it when P6D evidence exists.

    The current product deliberately retains periods after source mutations have
    created immutable audit evidence.  Cancellation releases the branch's
    mutable workflow slot without weakening that retention boundary.

    `add_draft_line`/`add_period_pay_line`/bonus-event creation each write a
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
