"""Phase 8 legacy payroll schedule authority retirement migration (0071).

Covers the migration-specific acceptance items that Phase 1's upgrade-path
test does not: fail-closed behavior and transactional atomicity when a
retained business row still depends on the legacy ScheduleVersionID
authority, and the exact post-retirement schema shape.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql

_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def phase8_database(pg_instance):
    """A disposable database upgraded to 0070 (immediately before retirement)."""
    dsn = pg_instance.dsn()
    database = "phase8_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        url = (
            f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:"
            f"{dsn['port']}/{database}"
        )
        env = os.environ.copy()
        env["DATABASE_URL"] = url
        env["SECRET_KEY"] = "phase8-disposable-migration-test"
        result = _run_alembic(env, "upgrade", "0070")
        assert result.returncode == 0, result.stderr
        check_dsn = dict(dsn)
        check_dsn["database"] = database
        yield env, check_dsn
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
        admin.close()


def test_retirement_migration_succeeds_on_clean_database(phase8_database):
    env, check_dsn = phase8_database

    result = _run_alembic(env, "upgrade", "0071")
    assert result.returncode == 0, result.stderr

    with psycopg2.connect(**check_dsn) as check:
        with check.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("0071",)

            for table_name in ("branchpayrollsettings", "payrollscheduleversions"):
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'payroll' AND table_name = %s)",
                    (table_name,),
                )
                assert cursor.fetchone() == (False,), f"payroll.{table_name} still exists"

            for table_name in ("payrollperiods", "payrollperioddays"):
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'payroll' AND table_name = %s "
                    "AND column_name = 'scheduleversionid')",
                    (table_name,),
                )
                assert cursor.fetchone() == (False,), (
                    f"payroll.{table_name}.ScheduleVersionID still exists"
                )

            # PeriodDays new-authority columns are now mandatory (no legacy branch left).
            for column_name in ("branchpayrollsetupassignmentid", "payrollsetupversionid"):
                cursor.execute(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'payroll' AND table_name = 'payrollperioddays' "
                    "AND column_name = %s",
                    (column_name,),
                )
                assert cursor.fetchone() == ("NO",), f"PayrollPeriodDays.{column_name} still nullable"

            # No legacy FK/index/check remains active.
            cursor.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conname IN ("
                "'fk_pp_scheduleversion', 'fk_ppd_scheduleversion', "
                "'fk_payrollperioddays_exactlegacyauthority')"
            )
            assert cursor.fetchall() == []
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'payroll' "
                "AND indexname IN ("
                "'ix_payrollperiods_scheduleversion', 'ux_payrollperiods_legacydayauthority')"
            )
            assert cursor.fetchall() == []

            # Canonical provenance FKs/constraints/trigger remain.
            cursor.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conname IN ("
                "'fk_payrollperiods_setupassignment', 'fk_payrollperiods_setupversion', "
                "'fk_payrollperiods_exactnewauthority', "
                "'ck_payrollperiods_authorityrepresentation')"
                " ORDER BY conname"
            )
            found = {row[0] for row in cursor.fetchall()}
            assert found == {
                "fk_payrollperiods_setupassignment",
                "fk_payrollperiods_setupversion",
                "ck_payrollperiods_authorityrepresentation",
            }
            cursor.execute(
                "SELECT tgname FROM pg_trigger WHERE tgname = 'trg_payrollperiods_setupprovenance'"
            )
            assert cursor.fetchone() == ("trg_payrollperiods_setupprovenance",)


def test_retirement_migration_preserves_pre_authority_historical_period(phase8_database):
    env, check_dsn = phase8_database

    with psycopg2.connect(**check_dsn) as seed:
        with seed.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.Companies (CompanyCode, CompanyName) "
                "VALUES ('P8HISTORY', 'P8 History') RETURNING CompanyID"
            )
            company_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO core.Branches (CompanyID, BranchCode, BranchName) "
                "VALUES (%s, 'P8HISTORY', 'P8 History Branch') RETURNING BranchID",
                (company_id,),
            )
            branch_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.PayrollPeriods "
                "(CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate) "
                "VALUES (%s, %s, 'P8HISTORY-1', 'Pre-authority history', 'Week', "
                "'2020-01-06', '2020-01-12') RETURNING PayrollPeriodID",
                (company_id, branch_id),
            )
            period_id = cursor.fetchone()[0]
        seed.commit()

    result = _run_alembic(env, "upgrade", "0071")
    assert result.returncode == 0, result.stderr

    with psycopg2.connect(**check_dsn) as check:
        with check.cursor() as cursor:
            cursor.execute(
                "SELECT PeriodCode, CompanyID, BranchID, StartDate, EndDate, "
                "BranchPayrollSetupAssignmentID, PayrollSetupVersionID, "
                "FrozenPayrollSetupID, FrozenPayrollSetupCode "
                "FROM payroll.PayrollPeriods WHERE PayrollPeriodID = %s",
                (period_id,),
            )
            assert cursor.fetchone() == (
                "P8HISTORY-1", company_id, branch_id,
                date(2020, 1, 6),
                date(2020, 1, 12),
                None, None, None, None,
            )


def test_retirement_migration_fails_closed_on_unreferenced_branch_payroll_settings(
    phase8_database,
):
    env, check_dsn = phase8_database

    with psycopg2.connect(**check_dsn) as seed:
        with seed.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.Companies (CompanyCode, CompanyName) "
                "VALUES ('P8SETTINGS', 'P8 Settings') RETURNING CompanyID"
            )
            company_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO core.Branches (CompanyID, BranchCode, BranchName) "
                "VALUES (%s, 'P8SETTINGS', 'P8 Settings Branch') RETURNING BranchID",
                (company_id,),
            )
            branch_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.BranchPayrollSettings "
                "(CompanyID, BranchID, PayrollFrequency, AnchorStartDate) "
                "VALUES (%s, %s, 'Week', '2026-01-05') "
                "RETURNING BranchPayrollSettingsID",
                (company_id, branch_id),
            )
            settings_id = cursor.fetchone()[0]
        seed.commit()

    result = _run_alembic(env, "upgrade", "0071")
    assert result.returncode != 0
    assert "BranchPayrollSettings row(s)" in result.stderr
    assert f"BranchPayrollSettingsID={settings_id}" in result.stderr

    with psycopg2.connect(**check_dsn) as check:
        with check.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("0070",)
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'payroll' AND table_name = 'branchpayrollsettings')"
            )
            assert cursor.fetchone() == (True,)
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'payroll' AND table_name = 'payrollperiods' "
                "AND column_name = 'scheduleversionid')"
            )
            assert cursor.fetchone() == (True,)
            cursor.execute(
                "SELECT BranchPayrollSettingsID, CompanyID, BranchID, PayrollFrequency, "
                "AnchorStartDate, CurrentScheduleVersionID "
                "FROM payroll.BranchPayrollSettings "
                "WHERE BranchPayrollSettingsID = %s",
                (settings_id,),
            )
            assert cursor.fetchone() == (
                settings_id, company_id, branch_id, "Week",
                date(2026, 1, 5), None,
            )


def test_retirement_migration_fails_closed_on_unreferenced_schedule_version(
    phase8_database,
):
    env, check_dsn = phase8_database

    with psycopg2.connect(**check_dsn) as seed:
        with seed.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.Companies (CompanyCode, CompanyName) "
                "VALUES ('P8VERSION', 'P8 Version') RETURNING CompanyID"
            )
            company_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO core.Branches (CompanyID, BranchCode, BranchName) "
                "VALUES (%s, 'P8VERSION', 'P8 Version Branch') RETURNING BranchID",
                (company_id,),
            )
            branch_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.PayrollScheduleVersions "
                "(CompanyID, BranchID, VersionNumber, PayrollFrequency, "
                "AnchorStartDate, SourceAction) "
                "VALUES (%s, %s, 1, 'Week', '2026-01-05', 'TEST') "
                "RETURNING ScheduleVersionID",
                (company_id, branch_id),
            )
            schedule_version_id = cursor.fetchone()[0]
        seed.commit()

    result = _run_alembic(env, "upgrade", "0071")
    assert result.returncode != 0
    assert "PayrollScheduleVersions row(s)" in result.stderr
    assert f"ScheduleVersionID={schedule_version_id}" in result.stderr

    with psycopg2.connect(**check_dsn) as check:
        with check.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("0070",)
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'payroll' AND table_name = 'payrollscheduleversions')"
            )
            assert cursor.fetchone() == (True,)
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'payroll' AND table_name = 'payrollperioddays' "
                "AND column_name = 'scheduleversionid')"
            )
            assert cursor.fetchone() == (True,)
            cursor.execute(
                "SELECT ScheduleVersionID, CompanyID, BranchID, VersionNumber, "
                "PayrollFrequency, AnchorStartDate, SourceAction "
                "FROM payroll.PayrollScheduleVersions "
                "WHERE ScheduleVersionID = %s",
                (schedule_version_id,),
            )
            assert cursor.fetchone() == (
                schedule_version_id, company_id, branch_id, 1, "Week",
                date(2026, 1, 5), "TEST",
            )


def test_retirement_migration_fails_closed_on_retained_legacy_period(phase8_database):
    env, check_dsn = phase8_database

    with psycopg2.connect(**check_dsn) as seed:
        with seed.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.Companies (CompanyCode, CompanyName) "
                "VALUES ('P8BLOCK', 'P8Block') RETURNING CompanyID"
            )
            company_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO core.Branches (CompanyID, BranchCode, BranchName) "
                "VALUES (%s, 'P8BLOCK', 'P8 Block Branch') RETURNING BranchID",
                (company_id,),
            )
            branch_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.PayrollScheduleVersions "
                "(CompanyID, BranchID, VersionNumber, PayrollFrequency, "
                "AnchorStartDate, SourceAction) "
                "VALUES (%s, %s, 1, 'Week', '2026-01-05', 'TEST') "
                "RETURNING ScheduleVersionID",
                (company_id, branch_id),
            )
            schedule_version_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.PayrollPeriods "
                "(CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, "
                "StartDate, EndDate, ScheduleVersionID) "
                "VALUES (%s, %s, 'P8BLOCK-1', 'Retained legacy period', 'Week', "
                "'2026-01-05', '2026-01-11', %s) RETURNING PayrollPeriodID",
                (company_id, branch_id, schedule_version_id),
            )
            period_id = cursor.fetchone()[0]
        seed.commit()

    result = _run_alembic(env, "upgrade", "0071")
    assert result.returncode != 0, "expected migration 0071 to abort"
    assert "payroll_legacy_schedule_authority_retirement_blocked" in result.stderr
    assert f"PayrollPeriodID={period_id}" in result.stderr

    with psycopg2.connect(**check_dsn) as check:
        with check.cursor() as cursor:
            # Atomicity: schema is untouched -- still at 0070, legacy tables intact.
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("0070",)
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'payroll' AND table_name = 'branchpayrollsettings')"
            )
            assert cursor.fetchone() == (True,)

            # Atomicity: the blocking business row is unchanged, not deleted or nulled.
            cursor.execute(
                "SELECT PeriodCode, ScheduleVersionID FROM payroll.PayrollPeriods "
                "WHERE PayrollPeriodID = %s",
                (period_id,),
            )
            assert cursor.fetchone() == ("P8BLOCK-1", schedule_version_id)
            cursor.execute(
                "SELECT COUNT(*) FROM payroll.PayrollScheduleVersions "
                "WHERE ScheduleVersionID = %s",
                (schedule_version_id,),
            )
            assert cursor.fetchone() == (1,)


def test_retirement_migration_fails_closed_on_retained_legacy_period_day(phase8_database):
    env, check_dsn = phase8_database

    with psycopg2.connect(**check_dsn) as seed:
        with seed.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.Companies (CompanyCode, CompanyName) "
                "VALUES ('P8BLOCKD', 'P8BlockDay') RETURNING CompanyID"
            )
            company_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO core.Branches (CompanyID, BranchCode, BranchName) "
                "VALUES (%s, 'P8BLOCKD', 'P8 Block Day Branch') RETURNING BranchID",
                (company_id,),
            )
            branch_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.PayrollScheduleVersions "
                "(CompanyID, BranchID, VersionNumber, PayrollFrequency, "
                "AnchorStartDate, SourceAction) "
                "VALUES (%s, %s, 1, 'Week', '2026-01-05', 'TEST') "
                "RETURNING ScheduleVersionID",
                (company_id, branch_id),
            )
            schedule_version_id = cursor.fetchone()[0]
            # 0067's exact legacy PeriodDay FK requires its parent Period to
            # carry the same legacy ScheduleVersionID. This scenario proves
            # the day-row preflight is independently reported as an offender.
            cursor.execute(
                "INSERT INTO payroll.PayrollPeriods "
                "(CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, "
                "StartDate, EndDate, ScheduleVersionID) "
                "VALUES (%s, %s, 'P8BLOCKD-1', 'Retained legacy day', 'Week', "
                "'2026-01-05', '2026-01-11', %s) RETURNING PayrollPeriodID",
                (company_id, branch_id, schedule_version_id),
            )
            period_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.PayrollPeriodDays "
                "(PayrollPeriodID, CompanyID, BranchID, ScheduleVersionID, "
                "WorkDate, DayOfWeek, IsDefaultWorkDay, IsConfiguredOffDay) "
                "VALUES (%s, %s, %s, %s, '2026-01-05', 0, TRUE, FALSE) "
                "RETURNING PayrollPeriodDayID",
                (period_id, company_id, branch_id, schedule_version_id),
            )
            period_day_id = cursor.fetchone()[0]
        seed.commit()

    result = _run_alembic(env, "upgrade", "0071")
    assert result.returncode != 0, "expected migration 0071 to abort"
    assert "payroll_legacy_schedule_authority_retirement_blocked" in result.stderr
    assert f"PayrollPeriodDayID={period_day_id}" in result.stderr

    with psycopg2.connect(**check_dsn) as check:
        with check.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("0070",)
            cursor.execute(
                "SELECT ScheduleVersionID FROM payroll.PayrollPeriodDays "
                "WHERE PayrollPeriodDayID = %s",
                (period_day_id,),
            )
            assert cursor.fetchone() == (schedule_version_id,)


def test_retirement_migration_fails_closed_on_incomplete_period_day_authority(phase8_database):
    """Malformed 0070 PeriodDay provenance is rejected before destructive DDL."""
    env, check_dsn = phase8_database

    with psycopg2.connect(**check_dsn) as seed:
        with seed.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.Companies (CompanyCode, CompanyName) "
                "VALUES ('P8INCOMPLETE', 'P8Incomplete') RETURNING CompanyID"
            )
            company_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO core.Branches (CompanyID, BranchCode, BranchName) "
                "VALUES (%s, 'P8INCOMPLETE', 'P8 Incomplete Branch') RETURNING BranchID",
                (company_id,),
            )
            branch_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.PayrollPeriods "
                "(CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate) "
                "VALUES (%s, %s, 'P8INCOMPLETE-1', 'Malformed day authority', 'Week', "
                "'2026-01-05', '2026-01-11') RETURNING PayrollPeriodID",
                (company_id, branch_id),
            )
            period_id = cursor.fetchone()[0]
            # This simulates a database that reached 0070 with its transitional
            # check disabled or corrupted. 0071 must still fail closed before
            # setting canonical PeriodDay columns NOT NULL.
            cursor.execute(
                "ALTER TABLE payroll.PayrollPeriodDays "
                "DROP CONSTRAINT ck_PayrollPeriodDays_AuthorityRepresentation"
            )
            cursor.execute(
                "INSERT INTO payroll.PayrollPeriodDays "
                "(PayrollPeriodID, CompanyID, BranchID, WorkDate, DayOfWeek, "
                "IsDefaultWorkDay, IsConfiguredOffDay) "
                "VALUES (%s, %s, %s, '2026-01-05', 0, TRUE, FALSE) "
                "RETURNING PayrollPeriodDayID",
                (period_id, company_id, branch_id),
            )
            period_day_id = cursor.fetchone()[0]
        seed.commit()

    result = _run_alembic(env, "upgrade", "0071")
    assert result.returncode != 0
    assert "incomplete canonical authority" in result.stderr

    with psycopg2.connect(**check_dsn) as check:
        with check.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("0070",)
            cursor.execute(
                "SELECT ScheduleVersionID, BranchPayrollSetupAssignmentID, "
                "PayrollSetupVersionID FROM payroll.PayrollPeriodDays "
                "WHERE PayrollPeriodDayID = %s",
                (period_day_id,),
            )
            assert cursor.fetchone() == (None, None, None)
            cursor.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'payroll' AND table_name = 'payrollperioddays' "
                "AND column_name = 'branchpayrollsetupassignmentid'"
            )
            assert cursor.fetchone() == ("YES",)


def test_retirement_migration_preserves_canonical_provenance_guard(phase8_database):
    env, check_dsn = phase8_database

    result = _run_alembic(env, "upgrade", "0071")
    assert result.returncode == 0, result.stderr

    with psycopg2.connect(**check_dsn) as seed:
        with seed.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.Companies (CompanyCode, CompanyName) "
                "VALUES ('P8GUARD', 'P8Guard') RETURNING CompanyID"
            )
            company_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO core.Branches (CompanyID, BranchCode, BranchName) "
                "VALUES (%s, 'P8GUARD', 'P8 Guard Branch') RETURNING BranchID",
                (company_id,),
            )
            branch_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.PayrollSetups "
                "(CompanyID, SetupCode, SetupName) VALUES (%s, 'P8GUARD', 'P8 Guard') "
                "RETURNING PayrollSetupID",
                (company_id,),
            )
            setup_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO sec.Users (CompanyID, Username, DisplayName) "
                "VALUES (%s, 'p8guard', 'P8 Guard User') RETURNING UserID",
                (company_id,),
            )
            user_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.PayrollSetupVersions "
                "(CompanyID, PayrollSetupID, LifecycleState, VersionNumber, "
                "EffectiveFromDate, PayrollFrequency, AnchorStartDate, "
                "NormalDaysOffMask, ConfigHash, PublishedByUserID, PublishedAtUtc) "
                "VALUES (%s, %s, 'Published', 1, '2026-01-05', 'Week', '2026-01-05', "
                "0, repeat('e', 64), %s, NOW()) RETURNING PayrollSetupVersionID",
                (company_id, setup_id, user_id),
            )
            version_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.BranchPayrollSetupAssignments "
                "(CompanyID, BranchID, PayrollSetupID, EffectiveFromDate) "
                "VALUES (%s, %s, %s, '2026-01-05') RETURNING BranchPayrollSetupAssignmentID",
                (company_id, branch_id, setup_id),
            )
            assignment_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO payroll.PayrollPeriods "
                "(CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate, "
                "BranchPayrollSetupAssignmentID, PayrollSetupVersionID, FrozenPayrollSetupID, "
                "FrozenPayrollSetupCode, FrozenPayrollSetupVersionNumber, FrozenPayrollFrequency, "
                "FrozenAnchorStartDate, FrozenNormalDaysOffMask, ScheduleConfigHash) "
                "VALUES (%s, %s, 'P8GUARD-1', 'Guard period', 'Week', '2026-01-05', '2026-01-11', "
                "%s, %s, %s, 'P8GUARD', 1, 'Week', '2026-01-05', 0, repeat('e', 64)) "
                "RETURNING PayrollPeriodID",
                (company_id, branch_id, assignment_id, version_id, setup_id),
            )
            period_id = cursor.fetchone()[0]
        seed.commit()

    with psycopg2.connect(**check_dsn) as check:
        with check.cursor() as cursor:
            with pytest.raises(psycopg2.Error):
                cursor.execute(
                    "UPDATE payroll.PayrollPeriods SET FrozenNormalDaysOffMask = 1 "
                    "WHERE PayrollPeriodID = %s",
                    (period_id,),
                )
            check.rollback()
            cursor.execute(
                "SELECT FrozenNormalDaysOffMask FROM payroll.PayrollPeriods "
                "WHERE PayrollPeriodID = %s",
                (period_id,),
            )
            assert cursor.fetchone() == (0,)
