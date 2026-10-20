"""
DB-level composite FK integrity tests (Phases 2, 2.1, 2.2).

Covers:
  - Migration 0025: PayrollDraftLines, PayrollFinalLines, DriverRates
    cannot reference a Period or Driver from a different branch.
  - Migration 0088: DriverPayRules belong to a Driver of their Company and record a
    calendar source (Branch and Payroll Setup version) of that Company.
  - Migration 0026: core.Drivers and payroll.PayrollPeriods cannot reference
    a branch that belongs to a different company (parent-root integrity).

Isolation principles:
  - DriverPayRules tests use Status='Voided' to bypass the
    excl_driverpayrules_no_date_overlap exclusion constraint, isolating the
    foreign key under test as the only failing guard.
  - Parent-root tests create a temp Company B + Branch B in direct_db so the
    composite FK (not the individual Company or Branch FK) is the specific
    constraint under test.  All temp rows are cleaned up in try/finally.
"""
import pytest
from sqlalchemy import text as _text
from sqlalchemy.exc import IntegrityError

from tests.builders.pay_rules import insert_pay_rule, publish_version
from tests.db_state import (
    DRIVER_ASSIGNMENT_HISTORY_TRIGGER,
    allow_final_line_insert,
    suspended_test_triggers,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_company_id(direct_db, branch_id: int) -> int:
    row = await direct_db.execute(
        _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
        {"bid": branch_id},
    )
    return row.scalar_one()


async def _get_any_rate_type_id(direct_db) -> int:
    row = await direct_db.execute(
        _text("SELECT ratetypeid FROM payroll.ratetypes LIMIT 1"),
    )
    return row.scalar_one()


def _is_fk_violation(exc: IntegrityError) -> bool:
    """True if the IntegrityError is a FK violation (pgcode 23503)."""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    return pgcode == "23503"


async def _create_temp_company_and_branch(direct_db, code_suffix: str) -> tuple[int, int]:
    """Create a temporary company and branch for cross-company FK tests.
    Returns (company_id, branch_id).  Caller must clean up via _drop_temp_company."""
    cmp = await direct_db.execute(
        _text("""
            INSERT INTO core.companies
                (companycode, companyname, legalname, status, issuspended)
            VALUES
                (:code, :name, :legal, 'Active', FALSE)
            RETURNING companyid
        """),
        {
            "code":  f"INTTEST{code_suffix}",
            "name":  f"Integrity Test Co {code_suffix}",
            "legal": f"IntTest {code_suffix} Ltd",
        },
    )
    cmp_id = cmp.scalar_one()

    br = await direct_db.execute(
        _text("""
            INSERT INTO core.branches (companyid, branchcode, branchname, status, timezoneid)
            VALUES (:cid, :code, :name, 'Active', 'UTC')
            RETURNING branchid
        """),
        {"cid": cmp_id, "code": f"IT{code_suffix}HQ", "name": f"IntTest {code_suffix} Branch"},
    )
    br_id = br.scalar_one()
    return cmp_id, br_id


async def _drop_temp_company_and_branch(direct_db, cmp_id: int, br_id: int) -> None:
    await direct_db.execute(
        _text("DELETE FROM core.branches  WHERE branchid  = :bid"), {"bid": br_id}
    )
    await direct_db.execute(
        _text("DELETE FROM core.companies WHERE companyid = :cid"), {"cid": cmp_id}
    )


# ---------------------------------------------------------------------------
# Migration 0026 — parent-root integrity (strengthened)
#
# Strategy: create a real Company B + Branch B so all individual FKs
# (fk_Drivers_Company, fk_Drivers_Branch, fk_Drivers_Employee) can be
# satisfied — leaving fk_Drivers_Branch_Company as the only failing guard.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestParentRootBranchIntegrity:
    """core.DriverAssignments and payroll.PayrollPeriods must have (BranchID, CompanyID)
    that matches an actual row in core.Branches (parent-root integrity)."""

    async def test_driver_cross_company_branch_rejected(
        self,
        direct_db,
        hq_branch_id: int,
    ):
        """Insert a DriverAssignment where BranchID belongs to Company B but CompanyID is A.
        The Driver FK passes (the permanent Driver is in Company A); only the composite
        FK fk_DriverAssignments_Branch_Company fires."""
        company_a_id = await _get_company_id(direct_db, hq_branch_id)
        cmp_b_id, branch_b_id = await _create_temp_company_and_branch(direct_db, "DRV")

        # Create a temp employee in Company A so fk_Drivers_Employee passes
        emp = await direct_db.execute(
            _text("""
                INSERT INTO core.employees
                    (companyid, branchid, fullname, employeetype, employmentstatus)
                VALUES
                    (:cid, :bid, 'Temp IntTest Employee', 'Driver', 'Active')
                RETURNING employeeid
            """),
            {"cid": company_a_id, "bid": hq_branch_id},
        )
        emp_id = emp.scalar_one()
        drv_id = (await direct_db.execute(
            _text("""
                INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
                VALUES (:cid, :eid, 'DRV-CROSS-TEST', 'Active')
                RETURNING driverid
            """),
            {"cid": company_a_id, "eid": emp_id},
        )).scalar_one()

        try:
            # companyid=company_a_id → driver FK              PASSES ✓
            # branchid=branch_b_id  → fk_DriverAssignments_Branch PASSES ✓ (branch_B exists)
            # (branch_b_id, company_a_id) not in Branches → fk_DriverAssignments_Branch_Company FAILS ✗
            with pytest.raises(IntegrityError) as exc_info:
                await direct_db.execute(
                    _text("""
                        INSERT INTO core.driverassignments
                            (companyid, branchid, employeeid, driverid, effectivefromat)
                        VALUES
                            (:cid_a, :bid_b, :eid, :did, NOW())
                    """),
                    {"cid_a": company_a_id, "bid_b": branch_b_id, "eid": emp_id, "did": drv_id},
                )
            assert _is_fk_violation(exc_info.value), (
                f"Expected FK violation (23503) from fk_DriverAssignments_Branch_Company, "
                f"got: {exc_info.value}"
            )
        finally:
            await direct_db.execute(
                _text("DELETE FROM core.drivers WHERE driverid = :did"), {"did": drv_id}
            )
            await direct_db.execute(
                _text("DELETE FROM core.employees WHERE employeeid = :eid"), {"eid": emp_id}
            )
            await _drop_temp_company_and_branch(direct_db, cmp_b_id, branch_b_id)

    async def test_payroll_period_cross_company_branch_rejected(
        self,
        direct_db,
        hq_branch_id: int,
    ):
        """Insert a PayrollPeriod where BranchID belongs to Company B but CompanyID is A.
        fk_PayrollPeriods_Company PASSES, fk_PayrollPeriods_Branch PASSES, only
        fk_PayrollPeriods_Branch_Company fires."""
        company_a_id = await _get_company_id(direct_db, hq_branch_id)
        cmp_b_id, branch_b_id = await _create_temp_company_and_branch(direct_db, "PER")

        try:
            # companyid=company_a_id → fk_PayrollPeriods_Company  PASSES ✓
            # branchid=branch_b_id  → fk_PayrollPeriods_Branch    PASSES ✓
            # (branch_b_id, company_a_id) not in Branches → fk_PayrollPeriods_Branch_Company FAILS ✗
            with pytest.raises(IntegrityError) as exc_info:
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrollperiods
                            (companyid, branchid, periodcode, periodname,
                             periodtype, startdate, enddate, status)
                        VALUES
                            (:cid_a, :bid_b, 'XC-PER-001', 'CrossCo Period',
                             'Week', '2099-01-01', '2099-01-07', 'Draft')
                    """),
                    {"cid_a": company_a_id, "bid_b": branch_b_id},
                )
            assert _is_fk_violation(exc_info.value), (
                f"Expected FK violation (23503) from fk_PayrollPeriods_Branch_Company, "
                f"got: {exc_info.value}"
            )
        finally:
            await _drop_temp_company_and_branch(direct_db, cmp_b_id, branch_b_id)

    async def test_driver_same_company_branch_accepted(
        self,
        direct_db,
        hq_branch_id: int,
    ):
        """Regression: an assignment where (BranchID, CompanyID) matches core.Branches is accepted."""
        company_a_id = await _get_company_id(direct_db, hq_branch_id)

        emp = await direct_db.execute(
            _text("""
                INSERT INTO core.employees
                    (companyid, branchid, fullname, employeetype, employmentstatus)
                VALUES
                    (:cid, :bid, 'Regression Employee', 'Driver', 'Active')
                RETURNING employeeid
            """),
            {"cid": company_a_id, "bid": hq_branch_id},
        )
        emp_id = emp.scalar_one()

        try:
            drv = await direct_db.execute(
                _text("""
                    INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
                    VALUES (:cid, :eid, 'DRV-REGR-TEST', 'Active')
                    RETURNING driverid
                """),
                {"cid": company_a_id, "eid": emp_id},
            )
            drv_id = drv.scalar_one()
            assert drv_id is not None
            assignment_id = (await direct_db.execute(
                _text("""
                    INSERT INTO core.driverassignments
                        (companyid, branchid, employeeid, driverid, effectivefromat)
                    VALUES (:cid, :bid, :eid, :did, NOW())
                    RETURNING driverassignmentid
                """),
                {"cid": company_a_id, "bid": hq_branch_id, "eid": emp_id, "did": drv_id},
            )).scalar_one()
            assert assignment_id is not None
            async with suspended_test_triggers(direct_db, (DRIVER_ASSIGNMENT_HISTORY_TRIGGER,)):
                await direct_db.execute(
                    _text("DELETE FROM core.driverassignments WHERE driverassignmentid = :id"),
                    {"id": assignment_id},
                )
            await direct_db.execute(
                _text("DELETE FROM core.drivers WHERE driverid = :id"), {"id": drv_id}
            )
        finally:
            await direct_db.execute(
                _text("DELETE FROM core.employees WHERE employeeid = :eid"), {"eid": emp_id}
            )

    async def test_payroll_period_same_company_branch_accepted(
        self,
        direct_db,
        hq_branch_id: int,
    ):
        """Regression: a PayrollPeriod where (BranchID, CompanyID) matches Branches is accepted."""
        company_a_id = await _get_company_id(direct_db, hq_branch_id)

        per = await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname,
                     periodtype, startdate, enddate, status)
                VALUES
                    (:cid, :bid, 'REGR-001', 'Regression Period',
                     'Week', '2099-06-01', '2099-06-07', 'Draft')
                RETURNING payrollperiodid
            """),
            {"cid": company_a_id, "bid": hq_branch_id},
        )
        per_id = per.scalar_one()
        assert per_id is not None
        await direct_db.execute(
            _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :id"),
            {"id": per_id},
        )


# ---------------------------------------------------------------------------
# Migration 0025 — PayrollDraftLines: period composite FK
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftLinePeriodIntegrity:
    """DraftLine.BranchID must match its Period's BranchID."""

    async def test_draft_line_wrong_branch_for_period_rejected(
        self,
        direct_db,
        hq_branch_id: int,
        paytest_branch_id: int,
        created_driver_assignment_id: int,
        created_period_id: int,
    ):
        """Insert a DraftLine whose BranchID disagrees with its Period's BranchID."""
        company_id = await _get_company_id(direct_db, hq_branch_id)
        with pytest.raises(IntegrityError) as exc_info:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverassignmentid,
                         workdate, linetype, quantity, sourcetype)
                    VALUES
                        (:cid, :bad_branch, :period_id, :assignment_id,
                         DATE '2090-01-01', 'REGULAR', 1, 'Test')
                """),
                {
                    "cid": company_id,
                    "bad_branch": paytest_branch_id,
                    "period_id": created_period_id,
                    "assignment_id": created_driver_assignment_id,
                },
            )
        assert _is_fk_violation(exc_info.value), (
            f"Expected FK violation (23503), got: {exc_info.value}"
        )

    async def test_draft_line_wrong_company_for_period_rejected(
        self,
        direct_db,
        hq_branch_id: int,
        created_driver_assignment_id: int,
        created_period_id: int,
    ):
        """Insert a DraftLine whose CompanyID disagrees with its Period's CompanyID."""
        with pytest.raises(IntegrityError) as exc_info:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverassignmentid,
                         workdate, linetype, quantity, sourcetype)
                    VALUES
                        (:bad_cid, :branch_id, :period_id, :assignment_id,
                         DATE '2090-01-01', 'REGULAR', 1, 'Test')
                """),
                {
                    "bad_cid": 99999,
                    "branch_id": hq_branch_id,
                    "period_id": created_period_id,
                    "assignment_id": created_driver_assignment_id,
                },
            )
        assert _is_fk_violation(exc_info.value), (
            f"Expected FK violation (23503), got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# Migration 0025 — PayrollDraftLines: driver composite FK
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftLineDriverIntegrity:
    """DraftLine.BranchID must match its Driver's BranchID."""

    async def test_draft_line_wrong_branch_for_driver_rejected(
        self,
        direct_db,
        hq_branch_id: int,
        paytest_driver_assignment_id: int,
        created_period_id: int,
    ):
        """Insert a DraftLine whose BranchID disagrees with its Driver's BranchID."""
        company_id = await _get_company_id(direct_db, hq_branch_id)
        with pytest.raises(IntegrityError) as exc_info:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverassignmentid,
                         workdate, linetype, quantity, sourcetype)
                    VALUES
                        (:cid, :hq_branch, :period_id, :assignment_id,
                         DATE '2090-01-01', 'REGULAR', 1, 'Test')
                """),
                {
                    "cid": company_id,
                    "hq_branch": hq_branch_id,
                    "period_id": created_period_id,
                    "assignment_id": paytest_driver_assignment_id,
                },
            )
        assert _is_fk_violation(exc_info.value), (
            f"Expected FK violation (23503), got: {exc_info.value}"
        )

    async def test_draft_line_matching_driver_and_period_accepted(
        self,
        direct_db,
        hq_branch_id: int,
        created_driver_assignment_id: int,
        created_period_id: int,
    ):
        """Regression: a consistent DraftLine (all same branch) must be accepted."""
        company_id = await _get_company_id(direct_db, hq_branch_id)
        result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverassignmentid,
                     workdate, linetype, quantity, sourcetype)
                VALUES
                    (:cid, :bid, :period_id, :assignment_id,
                     DATE '2090-01-01', 'REGULAR', 1, 'IntegrityTest')
                RETURNING draftlineid
            """),
            {
                "cid": company_id,
                "bid": hq_branch_id,
                "period_id": created_period_id,
                "assignment_id": created_driver_assignment_id,
            },
        )
        inserted_id = result.scalar_one()
        assert inserted_id is not None
        await direct_db.execute(
            _text("DELETE FROM payroll.payrolldraftlines WHERE draftlineid = :id"),
            {"id": inserted_id},
        )


# ---------------------------------------------------------------------------
# Migration 0025 — PayrollFinalLines: period composite FK
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFinalLinePeriodIntegrity:
    """FinalLine.BranchID must match its Period's BranchID."""

    async def test_final_line_wrong_branch_for_period_rejected(
        self,
        direct_db,
        hq_branch_id: int,
        paytest_branch_id: int,
        created_driver_assignment_id: int,
        created_period_id: int,
    ):
        company_id = await _get_company_id(direct_db, hq_branch_id)
        # Phase 6: allow the INSERT so the FK violation (not the insert guard) is tested.
        async with allow_final_line_insert(direct_db):
            with pytest.raises(IntegrityError) as exc_info:
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrollfinallines
                            (companyid, branchid, payrollperiodid, driverassignmentid, driverid,
                             linetype, quantity, finalamount, sourcetype, CurrencyCode, CurrencyMinorUnitDigits)
                        VALUES
                            (:cid, :bad_branch, :period_id, :assignment_id,
                             (SELECT driverid FROM core.driverassignments WHERE driverassignmentid = :assignment_id),
                             'REGULAR', 1, 100, 'Test', 'USD', 2)
                    """),
                    {
                        "cid": company_id,
                        "bad_branch": paytest_branch_id,
                        "period_id": created_period_id,
                        "assignment_id": created_driver_assignment_id,
                    },
                )
        assert _is_fk_violation(exc_info.value), (
            f"Expected FK violation (23503), got: {exc_info.value}"
        )

    async def test_final_line_wrong_company_for_period_rejected(
        self,
        direct_db,
        hq_branch_id: int,
        created_driver_assignment_id: int,
        created_period_id: int,
    ):
        # Phase 6: allow the INSERT so the FK violation (not the insert guard) is tested.
        async with allow_final_line_insert(direct_db):
            with pytest.raises(IntegrityError) as exc_info:
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrollfinallines
                            (companyid, branchid, payrollperiodid, driverassignmentid, driverid,
                             linetype, quantity, finalamount, sourcetype, CurrencyCode, CurrencyMinorUnitDigits)
                        VALUES
                            (:bad_cid, :branch_id, :period_id, :assignment_id,
                             (SELECT driverid FROM core.driverassignments WHERE driverassignmentid = :assignment_id),
                             'REGULAR', 1, 100, 'Test', 'USD', 2)
                    """),
                    {
                        "bad_cid": 99999,
                        "branch_id": hq_branch_id,
                        "period_id": created_period_id,
                        "assignment_id": created_driver_assignment_id,
                    },
                )
        assert _is_fk_violation(exc_info.value), (
            f"Expected FK violation (23503), got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# Migration 0025 — PayrollFinalLines: driver composite FK
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFinalLineDriverIntegrity:
    """FinalLine.BranchID must match its Driver's BranchID."""

    async def test_final_line_wrong_branch_for_driver_rejected(
        self,
        direct_db,
        hq_branch_id: int,
        paytest_driver_assignment_id: int,
        created_period_id: int,
    ):
        company_id = await _get_company_id(direct_db, hq_branch_id)
        # Phase 6: allow the INSERT so the FK violation (not the insert guard) is tested.
        async with allow_final_line_insert(direct_db):
            with pytest.raises(IntegrityError) as exc_info:
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrollfinallines
                            (companyid, branchid, payrollperiodid, driverassignmentid, driverid,
                             linetype, quantity, finalamount, sourcetype, CurrencyCode, CurrencyMinorUnitDigits)
                        VALUES
                            (:cid, :hq_branch, :period_id, :assignment_id,
                             (SELECT driverid FROM core.driverassignments WHERE driverassignmentid = :assignment_id),
                             'REGULAR', 1, 100, 'Test', 'USD', 2)
                    """),
                    {
                        "cid": company_id,
                        "hq_branch": hq_branch_id,
                        "period_id": created_period_id,
                        "assignment_id": paytest_driver_assignment_id,
                    },
                )
        assert _is_fk_violation(exc_info.value), (
            f"Expected FK violation (23503), got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# Migration 0025 — DriverRates: composite FK
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDriverRateIntegrity:
    """DriverRate.BranchID must match its Driver's BranchID."""

    async def test_driver_rate_wrong_branch_for_driver_rejected(
        self,
        direct_db,
        hq_branch_id: int,
        paytest_driver_assignment_id: int,
    ):
        """Insert a DriverRate for a PAYTEST driver but claim HQ branch.
        Uses Status='PendingApproval' (exempt from excl_DriverRates_no_date_overlap)
        so only the composite FK can reject it."""
        company_id = await _get_company_id(direct_db, hq_branch_id)
        rate_type_id = await _get_any_rate_type_id(direct_db)
        with pytest.raises(IntegrityError) as exc_info:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.driverrates
                        (companyid, branchid, driverassignmentid, ratetypeid,
                         amount, effectivefrom, status)
                    VALUES
                        (:cid, :hq_branch, :assignment_id, :rt_id,
                         25.00, '2030-01-01', 'PendingApproval')
                """),
                {
                    "cid": company_id,
                    "hq_branch": hq_branch_id,
                    "assignment_id": paytest_driver_assignment_id,
                    "rt_id": rate_type_id,
                },
            )
        assert _is_fk_violation(exc_info.value), (
            f"Expected FK violation (23503), got: {exc_info.value}"
        )

    async def test_driver_rate_wrong_company_for_driver_rejected(
        self,
        direct_db,
        hq_branch_id: int,
        created_driver_assignment_id: int,
    ):
        """Insert a DriverRate with a non-existent company."""
        rate_type_id = await _get_any_rate_type_id(direct_db)
        with pytest.raises(IntegrityError) as exc_info:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.driverrates
                        (companyid, branchid, driverassignmentid, ratetypeid,
                         amount, effectivefrom, status)
                    VALUES
                        (:bad_cid, :branch_id, :assignment_id, :rt_id,
                         25.00, '2030-01-01', 'PendingApproval')
                """),
                {
                    "bad_cid": 99999,
                    "branch_id": hq_branch_id,
                    "assignment_id": created_driver_assignment_id,
                    "rt_id": rate_type_id,
                },
            )
        assert _is_fk_violation(exc_info.value), (
            f"Expected FK violation (23503), got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# Migration 0088 — DriverPayRules are owned by the permanent Driver
#
# Isolation: Status='Voided' bypasses excl_driverpayrules_no_date_overlap (which only covers
# Active/Ended). Each rejected insert supplies a valid calendar so that the foreign key under
# test is the only failing guard.
# ---------------------------------------------------------------------------

async def _draft_version(direct_db, company_id: int) -> int:
    """A Draft Payroll Setup version (deletable, unlike a Published one) of the Company."""
    setup_id = (await direct_db.execute(_text("""
        INSERT INTO payroll.payrollsetups (companyid, setupcode, setupname, status)
        VALUES (:c, 'DRAFTSRC', 'draft source', 'Active') RETURNING payrollsetupid
    """), {"c": company_id})).scalar_one()
    return int((await direct_db.execute(_text("""
        INSERT INTO payroll.payrollsetupversions (companyid, payrollsetupid, lifecyclestate)
        VALUES (:c, :s, 'Draft') RETURNING payrollsetupversionid
    """), {"c": company_id, "s": setup_id})).scalar_one())


async def _drop_draft_versions(direct_db, company_id: int) -> None:
    await direct_db.execute(
        _text("DELETE FROM payroll.payrollsetupversions WHERE companyid = :c"), {"c": company_id})
    await direct_db.execute(
        _text("DELETE FROM payroll.payrollsetups WHERE companyid = :c"), {"c": company_id})


async def _driver_of(direct_db, assignment_id: int) -> int:
    return int((await direct_db.execute(
        _text("SELECT driverid FROM core.driverassignments WHERE driverassignmentid = :a"),
        {"a": assignment_id})).scalar_one())


@pytest.mark.asyncio
class TestDriverPayRuleIntegrity:
    """A DriverPayRule belongs to a Driver of its Company and records a calendar source of
    that same Company."""

    async def test_driver_of_another_company_is_rejected(
        self, direct_db, hq_branch_id: int, paytest_driver_assignment_id: int,
    ):
        company_a = await _get_company_id(direct_db, hq_branch_id)
        driver_id = await _driver_of(direct_db, paytest_driver_assignment_id)
        cmp_b_id, branch_b_id = await _create_temp_company_and_branch(direct_db, "PR1")
        try:
            version_b = await _draft_version(direct_db, cmp_b_id)
            with pytest.raises(IntegrityError) as exc_info:
                # Company B with a Company A Driver: only fk_driverpayrules_driver_company fails.
                await insert_pay_rule(
                    direct_db, company_id=cmp_b_id, driver_id=driver_id, branch_id=branch_b_id,
                    start="2030-01-01", status="Voided", version_id=version_b)
            assert _is_fk_violation(exc_info.value), exc_info.value
            assert "fk_driverpayrules_driver_company" in str(exc_info.value)
            assert company_a != cmp_b_id
        finally:
            await _drop_draft_versions(direct_db, cmp_b_id)
            await _drop_temp_company_and_branch(direct_db, cmp_b_id, branch_b_id)

    async def test_calendar_source_of_another_company_is_rejected(
        self, direct_db, hq_branch_id: int, paytest_driver_assignment_id: int,
    ):
        company_a = await _get_company_id(direct_db, hq_branch_id)
        driver_id = await _driver_of(direct_db, paytest_driver_assignment_id)
        cmp_b_id, branch_b_id = await _create_temp_company_and_branch(direct_db, "PR2")
        try:
            _, version_a = await publish_version(direct_db, company_a)
            with pytest.raises(IntegrityError) as exc_info:
                # A Company A rule pointing at a Company B Branch: only the Branch FK fails.
                await insert_pay_rule(
                    direct_db, company_id=company_a, driver_id=driver_id, branch_id=branch_b_id,
                    start="2030-01-01", status="Voided", version_id=version_a)
            assert _is_fk_violation(exc_info.value), exc_info.value
            assert "fk_driverpayrules_calendarsourcebranch_company" in str(exc_info.value)
            version_b = await _draft_version(direct_db, cmp_b_id)
            with pytest.raises(IntegrityError) as exc_info:
                # ... or at a Company B Payroll Setup version: only the version FK fails.
                await insert_pay_rule(
                    direct_db, company_id=company_a, driver_id=driver_id, branch_id=hq_branch_id,
                    start="2030-01-01", status="Voided", version_id=version_b)
            assert _is_fk_violation(exc_info.value), exc_info.value
            assert "fk_driverpayrules_calendarsourceversion_company" in str(exc_info.value)
        finally:
            await _drop_draft_versions(direct_db, cmp_b_id)
            await _drop_temp_company_and_branch(direct_db, cmp_b_id, branch_b_id)

    async def test_driver_pay_rule_matching_driver_accepted(
        self, direct_db, hq_branch_id: int, created_driver_assignment_id: int,
    ):
        """Regression: a consistent DriverPayRule must be accepted."""
        company_id = await _get_company_id(direct_db, hq_branch_id)
        driver_id = await _driver_of(direct_db, created_driver_assignment_id)
        # The Driver's tenure and the rule's start date are not checked at the table level
        # (the context trigger decides that); a start inside the open tenure is accepted.
        inserted_id = await insert_pay_rule(
            direct_db, company_id=company_id, driver_id=driver_id, branch_id=hq_branch_id,
            amount="500.00", start="2099-06-01")
        assert inserted_id is not None
        await direct_db.execute(
            _text("DELETE FROM payroll.driverpayrules WHERE driverpayruleid = :id"),
            {"id": inserted_id},
        )
