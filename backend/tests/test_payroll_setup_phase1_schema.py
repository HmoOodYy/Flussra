"""Phase 1 database invariants; the legacy runtime remains in use."""
from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import psycopg2
import pytest


@pytest.fixture
def cursor(apply_schema):
    connection = psycopg2.connect(client_encoding="utf-8", **apply_schema.dsn())
    try:
        yield connection.cursor()
    finally:
        connection.rollback()
        connection.close()


@contextmanager
def rejected(cursor):
    cursor.execute("SAVEPOINT expected_rejection")
    with pytest.raises(psycopg2.Error):
        yield
    cursor.execute("ROLLBACK TO SAVEPOINT expected_rejection")
    cursor.execute("RELEASE SAVEPOINT expected_rejection")


def company(cursor):
    code = "P1_" + uuid4().hex[:12]
    cursor.execute(
        "INSERT INTO core.Companies (CompanyCode, CompanyName) VALUES (%s, %s) RETURNING CompanyID",
        (code, code),
    )
    company_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO core.Branches (CompanyID, BranchCode, BranchName) "
        "VALUES (%s, 'MAIN', 'Main') RETURNING BranchID",
        (company_id,),
    )
    return company_id, cursor.fetchone()[0]


def setup(cursor, company_id, *, status="Active"):
    code = "S_" + uuid4().hex[:12]
    cursor.execute(
        "INSERT INTO payroll.PayrollSetups "
        "(CompanyID, SetupCode, SetupName, Status) VALUES (%s, %s, %s, %s) "
        "RETURNING PayrollSetupID",
        (company_id, code, code, status),
    )
    return cursor.fetchone()[0], code


def actor(cursor):
    cursor.execute("SELECT UserID FROM sec.Users ORDER BY UserID LIMIT 1")
    return cursor.fetchone()[0]


def version(cursor, company_id, setup_id, number=1, effective="2026-01-05", replaces=None,
            frequency="Week", custom_interval=None):
    cursor.execute(
        "INSERT INTO payroll.PayrollSetupVersions "
        "(CompanyID, PayrollSetupID, LifecycleState, VersionNumber, EffectiveFromDate, "
        "PayrollFrequency, AnchorStartDate, CustomIntervalDays, NormalDaysOffMask, "
        "ConfigHash, ReplacesVersionID, PublishedByUserID, PublishedAtUtc) "
        "VALUES (%s, %s, 'Published', %s, %s, %s, '2026-01-05', %s, 0, "
        "%s, %s, %s, NOW()) RETURNING PayrollSetupVersionID",
        (company_id, setup_id, number, effective, frequency, custom_interval,
         "a" * 64, replaces, actor(cursor)),
    )
    return cursor.fetchone()[0]


def draft_version(cursor, company_id, setup_id, frequency, custom_interval):
    cursor.execute(
        "INSERT INTO payroll.PayrollSetupVersions "
        "(CompanyID, PayrollSetupID, LifecycleState, PayrollFrequency, CustomIntervalDays) "
        "VALUES (%s, %s, 'Draft', %s, %s) RETURNING PayrollSetupVersionID",
        (company_id, setup_id, frequency, custom_interval),
    )
    return cursor.fetchone()[0]


def publish_draft(cursor, version_id):
    cursor.execute(
        "UPDATE payroll.PayrollSetupVersions SET LifecycleState = 'Published', "
        "VersionNumber = 1, EffectiveFromDate = '2026-01-05', "
        "AnchorStartDate = '2026-01-05', NormalDaysOffMask = 0, ConfigHash = %s, "
        "PublishedByUserID = %s, PublishedAtUtc = NOW() "
        "WHERE PayrollSetupVersionID = %s",
        ("a" * 64, actor(cursor), version_id),
    )


def assignment(cursor, company_id, branch_id, setup_id, start="2026-01-05", end=None):
    cursor.execute(
        "INSERT INTO payroll.BranchPayrollSetupAssignments "
        "(CompanyID, BranchID, PayrollSetupID, EffectiveFromDate, EffectiveToDate) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING BranchPayrollSetupAssignmentID",
        (company_id, branch_id, setup_id, start, end),
    )
    return cursor.fetchone()[0]


def period(cursor, company_id, branch_id, *, assignment_id=None, version_id=None,
           setup_id=None, setup_code=None, schedule_version_id=None):
    code = "P_" + uuid4().hex[:12]
    cursor.execute(
        "INSERT INTO payroll.PayrollPeriods "
        "(CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate, "
        "ScheduleVersionID, BranchPayrollSetupAssignmentID, PayrollSetupVersionID, "
        "FrozenPayrollSetupID, FrozenPayrollSetupCode, FrozenPayrollSetupVersionNumber, "
        "FrozenPayrollFrequency, FrozenAnchorStartDate, FrozenNormalDaysOffMask, ScheduleConfigHash) "
        "VALUES (%s, %s, %s, %s, 'Week', '2026-01-05', '2026-01-11', "
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING PayrollPeriodID",
        (company_id, branch_id, code, code, schedule_version_id,
         assignment_id, version_id, setup_id, setup_code,
         1 if version_id else None, "Week" if version_id else None,
         "2026-01-05" if version_id else None, 0 if version_id else None,
         "a" * 64 if version_id else None),
    )
    return cursor.fetchone()[0]


def day(cursor, period_id, company_id, branch_id, *, schedule_version_id=None,
        assignment_id=None, version_id=None):
    cursor.execute(
        "INSERT INTO payroll.PayrollPeriodDays "
        "(PayrollPeriodID, CompanyID, BranchID, ScheduleVersionID, "
        "BranchPayrollSetupAssignmentID, PayrollSetupVersionID, WorkDate, DayOfWeek, "
        "IsDefaultWorkDay, IsConfiguredOffDay) "
        "VALUES (%s, %s, %s, %s, %s, %s, '2026-01-05', 0, TRUE, FALSE)",
        (period_id, company_id, branch_id, schedule_version_id, assignment_id, version_id),
    )


def current_authority(cursor):
    company_id, branch_id = company(cursor)
    setup_id, code = setup(cursor, company_id)
    version_id = version(cursor, company_id, setup_id)
    assignment_id = assignment(cursor, company_id, branch_id, setup_id)
    return company_id, branch_id, setup_id, code, version_id, assignment_id


def test_company_owns_multiple_reusable_setups(cursor):
    company_id, branch_a = company(cursor)
    cursor.execute(
        "INSERT INTO core.Branches (CompanyID, BranchCode, BranchName) "
        "VALUES (%s, 'OTHER', 'Other') RETURNING BranchID", (company_id,),
    )
    branch_b = cursor.fetchone()[0]
    setup_a, _ = setup(cursor, company_id)
    setup_b, _ = setup(cursor, company_id)
    assert setup_a != setup_b
    assert assignment(cursor, company_id, branch_a, setup_a) != assignment(
        cursor, company_id, branch_b, setup_a)
    with rejected(cursor):
        cursor.execute(
            "UPDATE payroll.PayrollSetups SET SetupCode = 'RENUMBERED' "
            "WHERE PayrollSetupID = %s", (setup_a,),
        )
    cursor.execute(
        "UPDATE payroll.PayrollSetups SET SetupName = 'Renamed' "
        "WHERE PayrollSetupID = %s", (setup_a,),
    )


def test_version_scope_and_numbering(cursor):
    first, _ = company(cursor)
    second, _ = company(cursor)
    first_setup, _ = setup(cursor, first)
    other_setup, _ = setup(cursor, first)
    assert version(cursor, first, first_setup) != version(cursor, first, other_setup)
    with rejected(cursor):
        version(cursor, first, first_setup)
    with rejected(cursor):
        version(cursor, second, first_setup, number=2)


def test_published_version_requires_complete_effective_configuration(cursor):
    company_id, _ = company(cursor)
    setup_id, _ = setup(cursor, company_id)
    with rejected(cursor):
        version(cursor, company_id, setup_id, effective=None)
    with rejected(cursor):
        cursor.execute(
            "INSERT INTO payroll.PayrollSetupVersions "
            "(CompanyID, PayrollSetupID, LifecycleState, VersionNumber, EffectiveFromDate, "
            "PayrollFrequency, AnchorStartDate, NormalDaysOffMask, ConfigHash, "
            "PublishedByUserID, PublishedAtUtc) "
            "VALUES (%s, %s, 'Published', 1, '2026-01-05', 'Custom', "
            "'2026-01-05', 0, %s, %s, NOW())",
            (company_id, setup_id, "a" * 64, actor(cursor)),
        )


@pytest.mark.parametrize("frequency", ["Week", "Biweek", "Month"])
def test_published_non_custom_version_rejects_custom_interval(cursor, frequency):
    company_id, _ = company(cursor)
    setup_id, _ = setup(cursor, company_id)
    version_id = draft_version(cursor, company_id, setup_id, frequency, 7)
    with rejected(cursor):
        publish_draft(cursor, version_id)


def test_published_custom_version_requires_positive_interval(cursor):
    company_id, _ = company(cursor)
    setup_id, _ = setup(cursor, company_id)
    version_id = draft_version(cursor, company_id, setup_id, "Custom", None)
    with rejected(cursor):
        publish_draft(cursor, version_id)
    cursor.execute(
        "UPDATE payroll.PayrollSetupVersions SET CustomIntervalDays = 7 "
        "WHERE PayrollSetupVersionID = %s", (version_id,),
    )
    publish_draft(cursor, version_id)
    cursor.execute(
        "SELECT LifecycleState, PayrollFrequency, CustomIntervalDays "
        "FROM payroll.PayrollSetupVersions WHERE PayrollSetupVersionID = %s",
        (version_id,),
    )
    assert cursor.fetchone() == ("Published", "Custom", 7)


def test_draft_version_custom_interval_remains_editable(cursor):
    company_id, _ = company(cursor)
    setup_id, _ = setup(cursor, company_id)
    cursor.execute(
        "INSERT INTO payroll.PayrollSetupVersions "
        "(CompanyID, PayrollSetupID, LifecycleState, PayrollFrequency, CustomIntervalDays) "
        "VALUES (%s, %s, 'Draft', 'Week', 7) RETURNING PayrollSetupVersionID",
        (company_id, setup_id),
    )
    assert cursor.fetchone()[0]


def test_published_version_cannot_change_or_be_deleted(cursor):
    company_id, _ = company(cursor)
    setup_id, _ = setup(cursor, company_id)
    version_id = version(cursor, company_id, setup_id)
    with rejected(cursor):
        cursor.execute(
            "UPDATE payroll.PayrollSetupVersions SET NormalDaysOffMask = 1 "
            "WHERE PayrollSetupVersionID = %s", (version_id,),
        )
    with rejected(cursor):
        cursor.execute(
            "DELETE FROM payroll.PayrollSetupVersions WHERE PayrollSetupVersionID = %s",
            (version_id,),
        )


def test_discarded_draft_remains_auditable_and_cannot_be_reused(cursor):
    company_id, _ = company(cursor)
    setup_id, _ = setup(cursor, company_id)
    username = "P1_" + uuid4().hex[:12]
    cursor.execute(
        "INSERT INTO sec.Users (CompanyID, Username, DisplayName) "
        "VALUES (%s, %s, %s) RETURNING UserID",
        (company_id, username, username),
    )
    user_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO payroll.PayrollSetupVersions "
        "(CompanyID, PayrollSetupID, LifecycleState, PayrollFrequency, CreatedByUserID) "
        "VALUES (%s, %s, 'Draft', 'Week', %s) RETURNING PayrollSetupVersionID",
        (company_id, setup_id, user_id),
    )
    draft_id = cursor.fetchone()[0]
    cursor.execute(
        "UPDATE payroll.PayrollSetupVersions SET PayrollFrequency = 'Biweek' "
        "WHERE PayrollSetupVersionID = %s", (draft_id,),
    )
    cursor.execute(
        "UPDATE payroll.PayrollSetupVersions "
        "SET DiscardedAtUtc = NOW(), DiscardedByUserID = %s "
        "WHERE PayrollSetupVersionID = %s", (user_id, draft_id),
    )
    cursor.execute(
        "INSERT INTO payroll.PayrollSetupPolicyAuditEvents "
        "(CompanyID, ActorUserID, EventType, PayrollSetupID, PayrollSetupVersionID) "
        "VALUES (%s, %s, 'DraftDiscarded', %s, %s)",
        (company_id, user_id, setup_id, draft_id),
    )
    with rejected(cursor):
        cursor.execute(
            "UPDATE payroll.PayrollSetupVersions SET PayrollFrequency = 'Week' "
            "WHERE PayrollSetupVersionID = %s", (draft_id,),
        )
    with rejected(cursor):
        cursor.execute(
            "DELETE FROM payroll.PayrollSetupVersions WHERE PayrollSetupVersionID = %s",
            (draft_id,),
        )


def test_replacement_chain_same_setup_date_and_no_fork(cursor):
    company_id, _ = company(cursor)
    setup_a, _ = setup(cursor, company_id)
    setup_b, _ = setup(cursor, company_id)
    first = version(cursor, company_id, setup_a)
    replacement = version(cursor, company_id, setup_a, number=2, replaces=first)
    assert replacement != first
    with rejected(cursor):
        version(cursor, company_id, setup_a, number=3, replaces=first)
    with rejected(cursor):
        version(cursor, company_id, setup_b, number=1, replaces=first)
    with rejected(cursor):
        version(cursor, company_id, setup_a, number=3, effective="2026-01-12", replaces=replacement)
    with rejected(cursor):
        version(cursor, company_id, setup_a, number=3, effective="2026-01-05")


def test_assignments_are_tenant_scoped_nonoverlapping_and_adjacent(cursor):
    company_a, branch_a = company(cursor)
    company_b, branch_b = company(cursor)
    setup_a, _ = setup(cursor, company_a)
    setup_b, _ = setup(cursor, company_b)
    assignment(cursor, company_a, branch_a, setup_a, end="2026-02-01")
    assignment(cursor, company_a, branch_a, setup_a, start="2026-02-01")
    with rejected(cursor):
        assignment(cursor, company_a, branch_a, setup_a, start="2026-01-31")
    with rejected(cursor):
        assignment(cursor, company_a, branch_b, setup_a)
    with rejected(cursor):
        assignment(cursor, company_a, branch_a, setup_b)
    cursor.execute(
        "SELECT BranchPayrollSetupAssignmentID FROM payroll.BranchPayrollSetupAssignments "
        "WHERE BranchID = %s ORDER BY EffectiveFromDate LIMIT 1", (branch_a,),
    )
    first_assignment = cursor.fetchone()[0]
    with rejected(cursor):
        cursor.execute(
            "UPDATE payroll.BranchPayrollSetupAssignments "
            "SET EffectiveFromDate = '2026-01-06' "
            "WHERE BranchPayrollSetupAssignmentID = %s", (first_assignment,),
        )
    assert company_a != company_b


def test_company_default_nullable_same_company_and_active(cursor):
    company_a, _ = company(cursor)
    company_b, _ = company(cursor)
    cursor.execute("SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = %s", (company_a,))
    assert cursor.fetchone()[0] is None
    active, _ = setup(cursor, company_a)
    other, _ = setup(cursor, company_b)
    archived, _ = setup(cursor, company_a, status="Archived")
    with rejected(cursor):
        cursor.execute("UPDATE core.Companies SET DefaultPayrollSetupID = %s WHERE CompanyID = %s", (other, company_a))
    with rejected(cursor):
        cursor.execute("UPDATE core.Companies SET DefaultPayrollSetupID = %s WHERE CompanyID = %s", (archived, company_a))
    cursor.execute("UPDATE core.Companies SET DefaultPayrollSetupID = %s WHERE CompanyID = %s", (active, company_a))
    with rejected(cursor):
        cursor.execute("UPDATE payroll.PayrollSetups SET Status = 'Archived' WHERE PayrollSetupID = %s", (active,))


def test_period_and_day_bind_exact_new_authority_without_legacy_version(cursor):
    company_id, branch_id, setup_id, code, version_id, assignment_id = current_authority(cursor)
    period_id = period(cursor, company_id, branch_id, assignment_id=assignment_id,
                       version_id=version_id, setup_id=setup_id, setup_code=code)
    day(cursor, period_id, company_id, branch_id, assignment_id=assignment_id,
        version_id=version_id)
    cursor.execute(
        "SELECT p.ScheduleVersionID, d.ScheduleVersionID, d.PayrollSetupVersionID "
        "FROM payroll.PayrollPeriods p JOIN payroll.PayrollPeriodDays d "
        "USING (PayrollPeriodID) WHERE p.PayrollPeriodID = %s", (period_id,),
    )
    assert cursor.fetchone() == (None, None, version_id)
    with rejected(cursor):
        day(cursor, period_id, company_id, branch_id, assignment_id=assignment_id,
            version_id=version_id + 10000)


def test_period_new_authority_rejects_cross_tenant_and_wrong_setup(cursor):
    company_a, branch_a, setup_a, code_a, version_a, assignment_a = current_authority(cursor)
    company_b, branch_b, setup_b, code_b, version_b, assignment_b = current_authority(cursor)
    with rejected(cursor):
        period(cursor, company_a, branch_a, assignment_id=assignment_b,
               version_id=version_a, setup_id=setup_a, setup_code=code_a)
    with rejected(cursor):
        period(cursor, company_a, branch_a, assignment_id=assignment_a,
               version_id=version_b, setup_id=setup_a, setup_code=code_a)
    with rejected(cursor):
        period(cursor, company_a, branch_a, assignment_id=assignment_a,
               version_id=version_a, setup_id=setup_b, setup_code=code_b)


def test_legacy_period_and_day_remain_representable(cursor):
    company_id, branch_id = company(cursor)
    cursor.execute(
        "INSERT INTO payroll.PayrollScheduleVersions "
        "(CompanyID, BranchID, VersionNumber, PayrollFrequency, AnchorStartDate, "
        "SourceAction) VALUES (%s, %s, 1, 'Week', '2026-01-05', 'TEST') "
        "RETURNING ScheduleVersionID", (company_id, branch_id),
    )
    old_version = cursor.fetchone()[0]
    period_id = period(cursor, company_id, branch_id, schedule_version_id=old_version)
    day(cursor, period_id, company_id, branch_id, schedule_version_id=old_version)
    cursor.execute(
        "SELECT p.BranchPayrollSetupAssignmentID, d.PayrollSetupVersionID "
        "FROM payroll.PayrollPeriods p JOIN payroll.PayrollPeriodDays d "
        "USING (PayrollPeriodID) WHERE p.PayrollPeriodID = %s", (period_id,),
    )
    assert cursor.fetchone() == (None, None)


def test_new_period_snapshot_is_frozen_and_matches_published_version(cursor):
    company_id, branch_id, setup_id, code, version_id, assignment_id = current_authority(cursor)
    with rejected(cursor):
        period(cursor, company_id, branch_id, assignment_id=assignment_id,
               version_id=version_id, setup_id=setup_id, setup_code="WRONG")
    period_id = period(cursor, company_id, branch_id, assignment_id=assignment_id,
                       version_id=version_id, setup_id=setup_id, setup_code=code)
    with rejected(cursor):
        cursor.execute(
            "UPDATE payroll.PayrollPeriods SET FrozenNormalDaysOffMask = 1 "
            "WHERE PayrollPeriodID = %s", (period_id,),
        )
    with rejected(cursor):
        cursor.execute(
            "UPDATE payroll.PayrollPeriods SET EndDate = '2026-01-12' "
            "WHERE PayrollPeriodID = %s", (period_id,),
        )


def test_payroll_setup_permissions_are_seeded_without_operational_implication(cursor):
    expected = {
        "payroll_setup.view", "payroll_setup.manage",
        "payroll_setup.publish", "payroll_setup.assign",
    }
    cursor.execute(
        "SELECT PermissionCode FROM sec.Permissions WHERE PermissionCode LIKE 'payroll_setup.%'"
    )
    assert {row[0] for row in cursor.fetchall()} == expected

    company_id, _ = company(cursor)
    username = "P1_" + uuid4().hex[:12]
    cursor.execute(
        "INSERT INTO sec.Users (CompanyID, Username, DisplayName) "
        "VALUES (%s, %s, %s) RETURNING UserID",
        (company_id, username, username),
    )
    user_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO sec.CompanyRoles "
        "(CompanyID, RoleCode, RoleName, RoleLevel, IsCustom) "
        "VALUES (%s, 'P1_PAYROLL', 'Payroll Only', 20, TRUE) RETURNING CompanyRoleID",
        (company_id,),
    )
    role_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode) "
        "VALUES (%s, 'payroll.view')", (role_id,),
    )
    cursor.execute("SELECT RoleID FROM sec.Roles WHERE RoleCode = 'PAYROLL_VIEWER'")
    legacy_role_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO sec.UserBranchRoles "
        "(UserID, CompanyID, RoleID, CompanyRoleID, ScopeType) "
        "VALUES (%s, %s, %s, %s, 'AllCompanyBranches')",
        (user_id, company_id, legacy_role_id, role_id),
    )
    cursor.execute(
        "SELECT sec.fn_UserHasPermission(%s, %s, NULL, 'payroll.view')",
        (user_id, company_id),
    )
    assert cursor.fetchone()[0] is True
    for permission in expected:
        cursor.execute(
            "SELECT sec.fn_UserHasPermission(%s, %s, NULL, %s)",
            (user_id, company_id, permission),
        )
        assert cursor.fetchone()[0] is False


def test_policy_audit_actor_scope_and_append_only_history(cursor):
    company_id, branch_id = company(cursor)
    setup_id, _ = setup(cursor, company_id)
    username = "P1_" + uuid4().hex[:12]
    cursor.execute(
        "INSERT INTO sec.Users (CompanyID, Username, DisplayName) "
        "VALUES (%s, %s, %s) RETURNING UserID",
        (company_id, username, username),
    )
    user_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO payroll.PayrollSetupPolicyAuditEvents "
        "(CompanyID, ActorUserID, EventType, PayrollSetupID) "
        "VALUES (%s, %s, 'SetupCreated', %s) "
        "RETURNING PayrollSetupPolicyAuditEventID",
        (company_id, user_id, setup_id),
    )
    event_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO payroll.PayrollSetupPolicyAuditEventBranches "
        "(PayrollSetupPolicyAuditEventID, CompanyID, BranchID) "
        "VALUES (%s, %s, %s)", (event_id, company_id, branch_id),
    )
    with rejected(cursor):
        cursor.execute(
            "UPDATE payroll.PayrollSetupPolicyAuditEvents SET EventType = 'SetupArchived' "
            "WHERE PayrollSetupPolicyAuditEventID = %s", (event_id,),
        )
    with rejected(cursor):
        cursor.execute(
            "DELETE FROM payroll.PayrollSetupPolicyAuditEvents "
            "WHERE PayrollSetupPolicyAuditEventID = %s", (event_id,),
        )
    with rejected(cursor):
        cursor.execute(
            "DELETE FROM payroll.PayrollSetupPolicyAuditEventBranches "
            "WHERE PayrollSetupPolicyAuditEventID = %s", (event_id,),
        )
    other_company, other_branch = company(cursor)
    with rejected(cursor):
        cursor.execute(
            "INSERT INTO payroll.PayrollSetupPolicyAuditEvents "
            "(CompanyID, ActorUserID, EventType, PayrollSetupID) "
            "VALUES (%s, %s, 'SetupCreated', %s)",
            (other_company, user_id, setup_id),
        )
    with rejected(cursor):
        cursor.execute(
            "INSERT INTO payroll.PayrollSetupPolicyAuditEventBranches "
            "(PayrollSetupPolicyAuditEventID, CompanyID, BranchID) "
            "VALUES (%s, %s, %s)", (event_id, company_id, other_branch),
        )


def test_period_created_audit_matches_frozen_authority(cursor):
    company_id, branch_id, setup_id, code, version_id, assignment_id = current_authority(cursor)
    period_id = period(cursor, company_id, branch_id, assignment_id=assignment_id,
                       version_id=version_id, setup_id=setup_id, setup_code=code)
    username = "P1_" + uuid4().hex[:12]
    cursor.execute(
        "INSERT INTO sec.Users (CompanyID, Username, DisplayName) "
        "VALUES (%s, %s, %s) RETURNING UserID",
        (company_id, username, username),
    )
    user_id = cursor.fetchone()[0]

    def write_event(config_hash):
        cursor.execute(
            "INSERT INTO payroll.PayrollSetupPolicyAuditEvents "
            "(CompanyID, ActorUserID, EventType, PayrollSetupID, "
            "PayrollSetupVersionID, BranchPayrollSetupAssignmentID, PayrollPeriodID, "
            "BranchID, NewConfigHash) "
            "VALUES (%s, %s, 'PeriodCreated', %s, %s, %s, %s, %s, %s) "
            "RETURNING PayrollSetupPolicyAuditEventID",
            (company_id, user_id, setup_id, version_id, assignment_id,
             period_id, branch_id, config_hash),
        )
        return cursor.fetchone()[0]

    with rejected(cursor):
        write_event("b" * 64)
    event_id = write_event("a" * 64)
    cursor.execute(
        "INSERT INTO payroll.PayrollSetupPolicyAuditEventBranches "
        "(PayrollSetupPolicyAuditEventID, CompanyID, BranchID) "
        "VALUES (%s, %s, %s)", (event_id, company_id, branch_id),
    )
