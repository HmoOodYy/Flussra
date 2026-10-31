"""Physical target authorities and append-only generic audit history."""
from __future__ import annotations

import psycopg2
import pytest
from psycopg2 import errors

from app.db.schema_guard import (
    _RETIRED_COLUMNS,
    _RETIRED_TABLES,
    _check_target_physical_authorities,
    _get_script_head,
    run_schema_guard,
)
from tests.schema_helpers import _alembic, _schema_database, _version


@pytest.fixture(scope="module")
def target_database(schema_databases):
    with _schema_database(schema_databases, "target_authorities") as database:
        yield database



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
    assert _version(dsn) == "0099"
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


def test_the_development_schema_guard_requires_the_single_alembic_head(target_database):
    env, dsn = target_database
    heads = _alembic(env, "heads")
    assert heads.returncode == 0, heads.stderr
    assert [line.split()[0] for line in heads.stdout.splitlines() if "(head)" in line] == [
        _get_script_head()]
    run_schema_guard(env["DATABASE_URL"], is_dev=True)
    conn = psycopg2.connect(**dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE public.alembic_version SET version_num = '0092'")
            with pytest.raises(RuntimeError, match="Alembic version mismatch"):
                run_schema_guard(env["DATABASE_URL"], is_dev=True)
    finally:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE public.alembic_version SET version_num = %s", (_get_script_head(),))
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
