"""G0.5 migration safety for retiring the predecessor custom PayItem architecture."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql

_ROOT = Path(__file__).resolve().parents[2]


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def g0_5_database(pg_instance):
    dsn = pg_instance.dsn()
    name = "g05_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    )
    env["SECRET_KEY"] = "g0-5-disposable-migration-test"
    check_dsn = {**dsn, "database": name}
    try:
        result = _alembic(env, "upgrade", "0079")
        assert result.returncode == 0, result.stderr
        yield env, check_dsn
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


_PAYITEM_INSERT = """
    INSERT INTO payroll.payitems (
        companyid, payitemcode, payitemname, category, datatype, unit, status,
        sortorder, appearsinpayrollentry, appearsinledger, appearsinreports,
        requiresrate, issystemstandard, itemscope, ratebehavior,
        isdefaultbranchactive, requestingbranchid, createdbyuserid
    ) VALUES (%s, %s, %s, 'Custom', 'Decimal', 'Stop', 'Active', 100,
              TRUE, TRUE, TRUE, TRUE, FALSE, 'Daily', 'PerUnit', FALSE, %s, %s)
    RETURNING payitemid
"""


def _rate_structure(cur, company_id: int, item_id: int, code: str) -> int:
    """RateType + PayItemRateTypeMap + PayItemRateSlots for one PerUnit item."""
    cur.execute("""
        INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
        VALUES (%s, %s, 'Stop', TRUE, %s) RETURNING ratetypeid
    """, (code, code + " rate", company_id))
    rate_type_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO payroll.payitemratetypemap (payitemid, ratetypeid, isprimary, status)
        VALUES (%s, %s, TRUE, 'Active')
    """, (item_id, rate_type_id))
    cur.execute("""
        INSERT INTO payroll.payitemrateslots
            (payitemid, ratetypeid, slotkey, slotrole, sortorder, isrequired,
             issystemgenerated, sourcekind, status)
        VALUES (%s, %s, 'per_unit_rate', 'perunit', 1, TRUE, TRUE, 'CDPI', 'Active')
    """, (item_id, rate_type_id))
    return rate_type_id


def _seed_catalog(cur) -> dict[str, int]:
    """A CDPI-managed item and an ownerless predecessor item, each with its rate graph."""
    cur.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                    timezonename, currencycode)
        VALUES ('G05', 'G05', 'G05', 'Active', FALSE, 'UTC', 'USD') RETURNING companyid
    """)
    company_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (%s, 'G05', 'G05', 'Active', TRUE) RETURNING branchid
    """, (company_id,))
    branch_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
        VALUES (%s, 'g05', 'G05', 'x', TRUE, FALSE) RETURNING userid
    """, (company_id,))
    user_id = cur.fetchone()[0]

    cur.execute(_PAYITEM_INSERT, (company_id, "G05_CDPI", "CDPI Item", branch_id, user_id))
    cdpi_item_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO payroll.cdpidefinitions
            (payitemid, definitionschemaversion, lockedatutc, createdbyuserid)
        VALUES (%s, 1, NOW(), %s)
    """, (cdpi_item_id, user_id))
    cdpi_rate_type_id = _rate_structure(cur, company_id, cdpi_item_id, "CPI_G05_CDPI")
    cur.execute("""
        INSERT INTO payroll.branchpayitemconfig
            (companyid, branchid, payitemid, isactive, effectivefrom, createdbyuserid)
        VALUES (%s, %s, %s, TRUE, CURRENT_DATE, %s)
    """, (company_id, branch_id, cdpi_item_id, user_id))

    cur.execute(_PAYITEM_INSERT, (company_id, "G05_OLD", "Predecessor Item", branch_id, user_id))
    old_item_id = cur.fetchone()[0]
    old_rate_type_id = _rate_structure(cur, company_id, old_item_id, "CPI_G05_OLD")
    cur.execute("""
        INSERT INTO payroll.branchpayitemconfig
            (companyid, branchid, payitemid, isactive, effectivefrom, createdbyuserid)
        VALUES (%s, %s, %s, TRUE, CURRENT_DATE, %s)
    """, (company_id, branch_id, old_item_id, user_id))
    cur.execute("""
        INSERT INTO payroll.payitemsettings
            (payitemid, companyid, settingkey, settingdatatype, settingvaluetext)
        VALUES (%s, %s, 'rate_name_1', 'Text', 'Old Rate')
    """, (old_item_id, company_id))
    cur.execute("""
        INSERT INTO payroll.custompayitemrequests
            (companyid, requestingbranchid, requestedbyuserid, payitemcode, payitemname,
             itemscope, ratebehavior, category, unit, status, approvedpayitemid)
        VALUES (%s, %s, %s, 'G05_OLD', 'Predecessor Item', 'Daily', 'PerUnit',
                'Count', 'Stop', 'Approved', %s)
    """, (company_id, branch_id, user_id, old_item_id))
    return {
        "company_id": company_id, "branch_id": branch_id, "user_id": user_id,
        "cdpi_item_id": cdpi_item_id, "cdpi_rate_type_id": cdpi_rate_type_id,
        "old_item_id": old_item_id, "old_rate_type_id": old_rate_type_id,
    }


def test_0080_removes_ownerless_predecessor_items_and_keeps_current_authority(g0_5_database):
    env, dsn = g0_5_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        ids = _seed_catalog(cur)

    upgraded = _alembic(env, "upgrade", "0080")
    assert upgraded.returncode == 0, upgraded.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        # Ownerless predecessor item and its disposable chain are gone.
        cur.execute("SELECT COUNT(*) FROM payroll.payitems WHERE payitemcode = 'G05_OLD'")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT COUNT(*) FROM payroll.ratetypes WHERE ratecode = 'CPI_G05_OLD'")
        assert cur.fetchone() == (0,)
        cur.execute("""
            SELECT (SELECT COUNT(*) FROM payroll.branchpayitemconfig WHERE payitemid = %(i)s)
                 + (SELECT COUNT(*) FROM payroll.payitemratetypemap WHERE payitemid = %(i)s)
                 + (SELECT COUNT(*) FROM payroll.payitemrateslots WHERE payitemid = %(i)s)
        """, {"i": ids["old_item_id"]})
        assert cur.fetchone() == (0,)

        # System catalog and the CDPI-managed item with its governance and rate graph survive.
        cur.execute("SELECT COUNT(*) FROM payroll.payitems WHERE companyid IS NULL AND issystemstandard")
        assert cur.fetchone()[0] > 0
        cur.execute("""
            SELECT pi.payitemcode, pi.requestingbranchid, pi.status
            FROM payroll.payitems pi
            JOIN payroll.cdpidefinitions d ON d.payitemid = pi.payitemid
            WHERE pi.payitemid = %s
        """, (ids["cdpi_item_id"],))
        assert cur.fetchone() == ("G05_CDPI", ids["branch_id"], "Active")
        cur.execute("""
            SELECT (SELECT COUNT(*) FROM payroll.payitemratetypemap
                    WHERE payitemid = %(i)s AND ratetypeid = %(r)s)
                 + (SELECT COUNT(*) FROM payroll.payitemrateslots
                    WHERE payitemid = %(i)s AND ratetypeid = %(r)s)
                 + (SELECT COUNT(*) FROM payroll.branchpayitemconfig WHERE payitemid = %(i)s)
        """, {"i": ids["cdpi_item_id"], "r": ids["cdpi_rate_type_id"]})
        assert cur.fetchone() == (3,)

        # The current schema keeps accepting new CDPI-owned structure.
        cur.execute(_PAYITEM_INSERT, (ids["company_id"], "G05_CDPI_2", "CDPI Two",
                                      ids["branch_id"], ids["user_id"]))
        new_item_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.cdpidefinitions
                (payitemid, definitionschemaversion, lockedatutc, createdbyuserid)
            VALUES (%s, 1, NOW(), %s)
        """, (new_item_id, ids["user_id"]))
        _rate_structure(cur, ids["company_id"], new_item_id, "CPI_G05_CDPI_2")

        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0080",)


def test_0080_refuses_when_final_ledger_depends_on_predecessor_item(g0_5_database):
    env, dsn = g0_5_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        ids = _seed_catalog(cur)
        cur.execute("""
            INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
            VALUES (%s, %s, 'G05 Driver', 'Driver', 'Active') RETURNING employeeid
        """, (ids["company_id"], ids["branch_id"]))
        employee_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
            VALUES (%s, %s, %s, 'G05-D', 'Active') RETURNING driverid
        """, (ids["company_id"], ids["branch_id"], employee_id))
        driver_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (%s, %s, 'Open', 'G05-P', 'G05', 'Week', '2098-06-01', '2098-06-07')
            RETURNING payrollperiodid
        """, (ids["company_id"], ids["branch_id"]))
        period_id = cur.fetchone()[0]
        cur.execute("SELECT set_config('app.allow_payroll_final_line_insert', 'true', true)")
        cur.execute("""
            INSERT INTO payroll.payrollfinallines
                (companyid, branchid, payrollperiodid, driverid, linetype, quantity,
                 finalamount, sourcetype, payitemid, approvedatutc,
                 currencycode, currencyminorunitdigits)
            VALUES (%s, %s, %s, %s, 'G05_OLD', 1, 20, 'DraftLine', %s, NOW(), 'USD', 2)
        """, (ids["company_id"], ids["branch_id"], period_id, driver_id, ids["old_item_id"]))

    refused = _alembic(env, "upgrade", "0080")
    assert refused.returncode != 0
    assert "G05_BLOCKED_FINAL_LEDGER_DEPENDENCY" in refused.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0079",)
        cur.execute("SELECT COUNT(*) FROM payroll.payitems WHERE payitemcode = 'G05_OLD'")
        assert cur.fetchone() == (1,)


@pytest.mark.parametrize("period_status", ["InReview", "Approved", "Locked", "Archived"])
def test_0080_refuses_to_delete_frozen_period_layout_of_predecessor_item(
    g0_5_database, period_status
):
    env, dsn = g0_5_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        ids = _seed_catalog(cur)
        cur.execute("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (%s, %s, %s, 'G05-L', 'G05-L', 'Week', '2098-07-01', '2098-07-07')
            RETURNING payrollperiodid
        """, (ids["company_id"], ids["branch_id"], period_status))
        period_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.payrollperiodpayitems
                (payrollperiodid, companyid, branchid, payitemid, payitemcode, payitemname,
                 category, datatype, unit, itemscope, ratebehavior, appearsinpayrollentry,
                 appearsinledger, appearsinreports, requiresrate, issystemstandard, iscustom,
                 payitemstatusatsnapshot, isactiveinperiod)
            VALUES (%s, %s, %s, %s, 'G05_OLD', 'Predecessor Item', 'Custom', 'Decimal', 'Stop',
                    'Daily', 'PerUnit', TRUE, TRUE, TRUE, TRUE, FALSE, TRUE, 'Active', TRUE)
        """, (period_id, ids["company_id"], ids["branch_id"], ids["old_item_id"]))

    refused = _alembic(env, "upgrade", "0080")
    assert refused.returncode != 0
    assert "G05_BLOCKED_FROZEN_PERIOD_LAYOUT_DEPENDENCY" in refused.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0079",)
        cur.execute("""
            SELECT COUNT(*) FROM payroll.payrollperiodpayitems WHERE payitemid = %s
        """, (ids["old_item_id"],))
        assert cur.fetchone() == (1,)
