"""Both supported Phase 1 upgrade paths run on disposable PostgreSQL databases."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest


_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("starting_revision", ["base", "0065"])
def test_phase1_alembic_upgrade_paths(pg_instance, starting_revision):
    dsn = pg_instance.dsn()
    database = "phase1_" + uuid4().hex[:12]
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
        env["SECRET_KEY"] = "phase1-disposable-migration-test"
        if starting_revision == "0065":
            result = subprocess.run(
                [sys.executable, "-B", "-m", "alembic", "upgrade", "0065"],
                cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
            )
            assert result.returncode == 0, result.stderr
            check_dsn = dict(dsn)
            check_dsn["database"] = database
            with psycopg2.connect(**check_dsn) as legacy_db:
                with legacy_db.cursor() as cursor:
                    company_code = "LEGACY_" + uuid4().hex[:10]
                    cursor.execute(
                        "INSERT INTO core.Companies (CompanyCode, CompanyName) "
                        "VALUES (%s, %s) RETURNING CompanyID",
                        (company_code, company_code),
                    )
                    company_id = cursor.fetchone()[0]
                    cursor.execute(
                        "INSERT INTO core.Branches (CompanyID, BranchCode, BranchName) "
                        "VALUES (%s, 'LEGACY', 'Legacy Branch') RETURNING BranchID",
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
                        "VALUES (%s, %s, %s, 'Legacy Week', 'Week', '2026-01-05', "
                        "'2026-01-11', %s) RETURNING PayrollPeriodID",
                        (company_id, branch_id, "LEGACY_" + uuid4().hex[:10],
                         schedule_version_id),
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
        result = subprocess.run(
            [sys.executable, "-B", "-m", "alembic", "upgrade", "head"],
            cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        check_dsn = dict(dsn)
        check_dsn["database"] = database
        with psycopg2.connect(**check_dsn) as check:
            with check.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchone() == ("0068",)
                cursor.execute("SELECT COUNT(*) FROM payroll.PayrollSetups")
                assert cursor.fetchone() == (0,)
                if starting_revision == "0065":
                    cursor.execute(
                        "SELECT p.PayrollPeriodID, d.PayrollPeriodDayID, "
                        "p.ScheduleVersionID, d.ScheduleVersionID, "
                        "p.PayrollSetupVersionID, d.PayrollSetupVersionID, "
                        "sv.ScheduleVersionID "
                        "FROM payroll.PayrollPeriods p "
                        "JOIN payroll.PayrollPeriodDays d USING (PayrollPeriodID) "
                        "JOIN payroll.PayrollScheduleVersions sv "
                        "ON sv.ScheduleVersionID = p.ScheduleVersionID "
                        "WHERE p.PayrollPeriodID = %s AND d.PayrollPeriodDayID = %s",
                        (period_id, period_day_id),
                    )
                    assert cursor.fetchone() == (
                        period_id, period_day_id, schedule_version_id,
                        schedule_version_id, None, None, schedule_version_id,
                    )
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
        admin.close()
