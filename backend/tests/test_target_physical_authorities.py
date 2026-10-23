"""Physical target authorities and append-only generic audit history."""
from __future__ import annotations

from datetime import date

import psycopg2
import pytest
from psycopg2 import errors
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.schema_guard import (
    _RETIRED_COLUMNS,
    _RETIRED_TABLES,
    _check_target_physical_authorities,
)
from tests.builders.final_evidence import seed_finalized_evidence
from tests.compensation_database_fixtures import make_pay_definition, make_rate_definition
from tests.test_company_status_catalog_migration import (
    _alembic,
    _disposable_database,
    _seed_world,
    _version,
)


@pytest.fixture
def previous_database(pg_instance):
    with _disposable_database(pg_instance, "physicalpre") as (env, dsn):
        result = _alembic(env, "upgrade", "0092")
        assert result.returncode == 0, result.stderr
        yield env, dsn


@pytest.fixture(scope="module")
def target_database(pg_instance):
    with _disposable_database(pg_instance, "physicalhead") as (env, dsn):
        result = _alembic(env, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        yield env, dsn


def _assert_target(cursor):
    assert _check_target_physical_authorities(cursor) == []
    for qualified in _RETIRED_TABLES:
        cursor.execute("SELECT to_regclass(%s)", (qualified,))
        assert cursor.fetchone() == (None,), qualified
    for schema, table, column in _RETIRED_COLUMNS:
        cursor.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = %s
        """, (schema, table, column))
        assert cursor.fetchone() is None, (schema, table, column)
    # SQL/plpgsql string bodies are not fully represented in pg_depend. Prove
    # that a stale function body cannot hide behind a successful RESTRICT drop.
    cursor.execute("""
        SELECT n.nspname, p.proname FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname IN ('core', 'sec', 'payroll', 'app')
          AND p.prosrc ~* '(payroll[.](payitems|ratetypes|driverrates([^c]|$)|driverratetiers|cdpi|payitemratetype|payitemrateslot)|sec[.](roles|rolepermissions)|employeetype|branchpayitemconfig)'
    """)
    assert cursor.fetchall() == []


def test_fresh_upgrade_exposes_only_target_physical_authorities(target_database):
    _, dsn = target_database
    assert _version(dsn) == "0093"
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        _assert_target(cursor)
        for table in (
            "payrollfinallines", "paydefinitions", "ratedefinitions", "ratecomponentdefinitions",
            "driverrateschedules", "driverrateschedulevalues",
            "payrollcalculationsnapshotusedratedefinitions", "payrollcalculationsnapshotusedratecomponents",
            "payrollcalculationsnapshotcomponents", "paydefinitionrequests",
            "paydefinitionrequestevents", "paydefinitionprovenance",
        ):
            cursor.execute("SELECT to_regclass(%s)", (f"payroll.{table}",))
            assert cursor.fetchone()[0] is not None, table


@pytest.mark.asyncio
async def test_upgrade_preserves_target_source_branch_provenance_and_frozen_components(previous_database):
    env, dsn = previous_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        world = _seed_world(cursor)
        company, branch, period = world["company"], world["branches"][0], world["period"]
        definition = make_pay_definition(cursor, company)
        rate = make_rate_definition(cursor, definition)
        cursor.execute("""
            INSERT INTO payroll.BranchPayItemConfig
                (CompanyID, BranchID, PayDefinitionID, EffectiveFrom, Notes)
            VALUES (%s, %s, %s, '2039-01-01', 'preserved version') RETURNING ConfigID
        """, (company, branch, definition))
        config = cursor.fetchone()[0]
        cursor.execute("""
            INSERT INTO payroll.PayrollPeriodDefinitions
                (CompanyID, BranchID, PayrollPeriodID, PayDefinitionID, RateDefinitionID,
                 DefinitionCodeSnapshot, DefinitionNameSnapshot, InputTypeSnapshot,
                 CalculationMethodSnapshot, CalculationMethodVersionSnapshot, RateShapeSnapshot,
                 DefinitionStatusAtSnapshot, IsActiveInPeriod, SortOrder, SourceBranchConfigID,
                 SourceBranchConfigEffectiveFrom)
            VALUES (%s, %s, %s, %s, %s, 'MEASURE', 'Measured work', 'Decimal', 'PerUnit', 1,
                    'Scalar', 'Active', TRUE, 0, %s, '2039-01-01')
            RETURNING PayrollPeriodDefinitionID
        """, (company, branch, period, definition, rate, config))
        ppd = cursor.fetchone()[0]
        cursor.execute("""
            INSERT INTO payroll.PayrollDraftLines
                (CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, WorkDate,
                 PayrollPeriodDefinitionID, Quantity, SourceType, Notes)
            VALUES (%s, %s, %s, %s, '2040-01-02', %s, 8, 'Manual', 'source note')
            RETURNING DraftLineID
        """, (company, branch, period, world["assignment"], ppd))
        source = cursor.fetchone()[0]
    engine = create_async_engine(env["DATABASE_URL"])
    try:
        async with engine.begin() as db:
            evidence = await seed_finalized_evidence(
                db, period_id=period, driver_assignment_id=world["assignment"])
    finally:
        await engine.dispose()
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT SourceSnapshot FROM payroll.PayrollFinalLines WHERE FinalLineID = %s",
                       (evidence.final_line_id,))
        frozen = cursor.fetchone()[0]
    result = _alembic(env, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert _version(dsn) == "0093"
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        _assert_target(cursor)
        cursor.execute("""
            SELECT d.SourceBranchConfigID, c.Notes FROM payroll.PayrollPeriodDefinitions d
            JOIN payroll.BranchPayDefinitionConfig c ON c.ConfigID = d.SourceBranchConfigID
            WHERE d.PayrollPeriodDefinitionID = %s
        """, (ppd,))
        assert cursor.fetchone() == (config, "preserved version")
        cursor.execute("""
            SELECT PayrollPeriodDefinitionID, DriverAssignmentID, WorkDate, Quantity, Notes,
                   NeedsManagerReview FROM payroll.PayrollDraftLines WHERE DraftLineID = %s
        """, (source,))
        assert cursor.fetchone() == (ppd, world["assignment"], date(2040, 1, 2), 8, "source note", False)
        # Consumers retain the column, but valid source facts cannot persist derived review state.
        for statement in (
            "UPDATE payroll.PayrollDraftLines SET NeedsManagerReview = TRUE WHERE DraftLineID = %s",
            """INSERT INTO payroll.PayrollDraftLines
                (CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, WorkDate,
                 PayrollPeriodDefinitionID, Quantity, SourceType, NeedsManagerReview)
               SELECT CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, WorkDate,
                      PayrollPeriodDefinitionID, Quantity, SourceType, TRUE
               FROM payroll.PayrollDraftLines WHERE DraftLineID = %s""",
        ):
            cursor.execute("SAVEPOINT rejected_review_flag")
            with pytest.raises(errors.CheckViolation) as rejected:
                cursor.execute(statement, (source,))
            assert rejected.value.diag.constraint_name == "ck_draftlines_nostoredreview"
            cursor.execute("ROLLBACK TO SAVEPOINT rejected_review_flag")
        cursor.execute("SELECT NeedsManagerReview FROM payroll.PayrollDraftLines WHERE DraftLineID = %s", (source,))
        assert cursor.fetchone() == (False,)
        cursor.execute("""
            SELECT PayrollCalculationSnapshotID, PayrollCalculationSnapshotComponentID,
                   FinalAmount, SourceSnapshot FROM payroll.PayrollFinalLines WHERE FinalLineID = %s
        """, (evidence.final_line_id,))
        assert cursor.fetchone() == (evidence.snapshot_id, evidence.component_id, evidence.amount, frozen)
        # SourceBranchConfigID still RESTRICTs removing a referenced version.
        with pytest.raises(errors.ForeignKeyViolation):
            cursor.execute("DELETE FROM payroll.BranchPayDefinitionConfig WHERE ConfigID = %s", (config,))


@pytest.mark.parametrize("predecessor", ["role_assignment", "draft_line"])
def test_upgrade_refuses_predecessor_rows_without_converting_them(previous_database, predecessor):
    env, dsn = previous_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        world = _seed_world(cursor)
        if predecessor == "role_assignment":
            cursor.execute("INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel) VALUES ('OLD', 'Old', 1) RETURNING RoleID")
            role = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.UserBranchRoles (CompanyID, UserID, RoleID, ScopeType)
                VALUES (%s, %s, %s, 'AllCompanyBranches')
            """, (world["company"], world["user"], role))
        else:
            cursor.execute("""
                INSERT INTO payroll.PayrollDraftLines
                    (CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, WorkDate,
                     LineType, SourceType)
                VALUES (%s, %s, %s, %s, '2040-01-02', 'STATUS_PAY', 'System')
            """, (world["company"], world["branches"][0], world["period"], world["assignment"]))
    result = _alembic(env, "upgrade", "head")
    assert result.returncode != 0
    assert "REQUIRE_REBUILD" in result.stderr + result.stdout
    assert _version(dsn) == "0092"
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('payroll.BranchPayItemConfig'), to_regclass('sec.Roles')")
        assert all(cursor.fetchone())


def test_an_unexpected_foreign_key_stops_the_cutover_atomically(previous_database):
    env, dsn = previous_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("CREATE TABLE public.external_consumer (PayItemID INTEGER REFERENCES payroll.PayItems)")
    result = _alembic(env, "upgrade", "head")
    assert result.returncode != 0
    assert "external_consumer" in result.stderr + result.stdout
    assert _version(dsn) == "0092"
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('sec.Roles'), to_regclass('payroll.BranchPayItemConfig')")
        assert all(cursor.fetchone())


@pytest.mark.parametrize("statement", [
    "UPDATE audit.AuditLog SET Reason = 'rewrite' WHERE AuditID = %s",
    "DELETE FROM audit.AuditLog WHERE AuditID = %s",
    "TRUNCATE audit.AuditLog",
])
def test_generic_audit_accepts_inserts_and_rejects_mutation(target_database, statement):
    _, dsn = target_database
    conn = psycopg2.connect(**dsn)
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO audit.AuditLog (ActionCode, Reason) VALUES ('TEST_EVENT', 'original') RETURNING AuditID")
            audit_id = cursor.fetchone()[0]
            cursor.execute("SAVEPOINT rejected_mutation")
            with pytest.raises(errors.RestrictViolation, match="audit_log_immutable"):
                cursor.execute(statement, (audit_id,) if "%s" in statement else None)
            cursor.execute("ROLLBACK TO SAVEPOINT rejected_mutation")
            cursor.execute("SELECT Reason FROM audit.AuditLog WHERE AuditID = %s", (audit_id,))
            assert cursor.fetchone() == ("original",)
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("trigger", ["trg_AuditLog_Immutable", "trg_AuditLog_NoTruncate"])
def test_target_schema_guard_detects_missing_audit_protection(target_database, trigger):
    _, dsn = target_database
    conn = psycopg2.connect(**dsn)
    try:
        with conn.cursor() as cursor:
            assert _check_target_physical_authorities(cursor) == []
            cursor.execute(f"ALTER TRIGGER {trigger} ON audit.AuditLog RENAME TO audit_guard_probe")
            assert any("append-only trigger" in error for error in _check_target_physical_authorities(cursor))
    finally:
        conn.rollback()
        conn.close()


_REVIEW_INVARIANT = "ALTER TABLE payroll.PayrollDraftLines DROP CONSTRAINT ck_DraftLines_NoStoredReview"


@pytest.mark.parametrize("drift", [
    _REVIEW_INVARIANT,
    _REVIEW_INVARIANT + ", ADD CONSTRAINT ck_DraftLines_NoStoredReview "
                        "CHECK (NeedsManagerReview = FALSE) NOT VALID",
    _REVIEW_INVARIANT + ", ADD CONSTRAINT ck_DraftLines_NoStoredReview "
                        "CHECK (NeedsManagerReview IS NOT NULL)",
    "ALTER TABLE payroll.PayrollDraftLines ALTER COLUMN NeedsManagerReview DROP NOT NULL",
], ids=["dropped", "not-validated", "weakened", "nullable"])
def test_target_schema_guard_detects_a_missing_or_weakened_stored_review_invariant(target_database, drift):
    _, dsn = target_database
    conn = psycopg2.connect(**dsn)
    try:
        with conn.cursor() as cursor:
            assert _check_target_physical_authorities(cursor) == []
            cursor.execute(drift)
            assert any("ck_DraftLines_NoStoredReview" in error
                       for error in _check_target_physical_authorities(cursor))
    finally:
        conn.rollback()
        conn.close()


def test_role_assignment_identity_and_uniqueness_use_company_role_only(target_database):
    from uuid import uuid4

    _, dsn = target_database
    conn = psycopg2.connect(**dsn)
    try:
        with conn.cursor() as cursor:
            code = 'ACCESS_' + uuid4().hex[:8]
            cursor.execute("INSERT INTO core.Companies (CompanyCode, CompanyName) VALUES (%s, %s) RETURNING CompanyID", (code, code))
            company = cursor.fetchone()[0]
            cursor.execute("INSERT INTO sec.Users (CompanyID, Username, DisplayName) VALUES (%s, %s, 'Reviewer') RETURNING UserID", (company, code))
            user = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.CompanyRoles (CompanyID, RoleCode, RoleName, RoleLevel)
                VALUES (%s, 'REVIEWER', 'Reviewer', 20), (%s, 'OPERATOR', 'Operator', 30)
                RETURNING CompanyRoleID
            """, (company, company))
            roles = [row[0] for row in cursor.fetchall()]
            statement = """
                INSERT INTO sec.UserBranchRoles (CompanyID, UserID, CompanyRoleID, ScopeType)
                VALUES (%s, %s, %s, 'AllCompanyBranches')
            """
            for role in roles:
                cursor.execute(statement, (company, user, role))
            cursor.execute("SAVEPOINT duplicate_role")
            with pytest.raises(errors.UniqueViolation):
                cursor.execute(statement, (company, user, roles[0]))
            cursor.execute("ROLLBACK TO SAVEPOINT duplicate_role")
            with pytest.raises(errors.NotNullViolation):
                cursor.execute(statement, (company, user, None))
    finally:
        conn.rollback()
        conn.close()
