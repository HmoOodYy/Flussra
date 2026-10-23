"""Migration 0091: OrdinalTier governance topology and calculation contract v4 evidence."""
from __future__ import annotations

import psycopg2
import pytest

from app.db.schema_guard import _check_calculation_evidence
from tests.test_company_status_catalog_migration import (
    _alembic,
    _columns,
    _disposable_database,
    _seed_world,
    _tables,
    _version,
)


@pytest.fixture
def pre_0091_database(pg_instance):
    """A disposable database at 0090: calculation contract v3 evidence."""
    with _disposable_database(pg_instance, "ord91pre") as (env, dsn):
        assert _alembic(env, "upgrade", "0090").returncode == 0
        yield env, dsn


@pytest.fixture(scope="module")
def head_database(pg_instance):
    with _disposable_database(pg_instance, "ord91head") as (env, dsn):
        result = _alembic(env, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        yield env, dsn


def _seed_evidence(cursor, world: dict, kind: str) -> None:
    if kind == "snapshot":
        cursor.execute("""
            INSERT INTO payroll.payrollcalculationsnapshots
                (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
                 sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
                 currencycode, currencyminorunitdigits, periodstartdate, periodenddate,
                 payableroundingversion, minmaxprorationversion, scheduledworkdates, warnings)
            VALUES (%s, %s, %s, 1, 'payroll-calculation-v3', %s, %s, %s, 0, 'USD', 2,
                    DATE '2040-01-01', DATE '2040-01-07', 'payable-rounding-v1',
                    'minmax-proration-v1', '{}', '{}')
        """, (world["company"], world["branches"][0], world["period"],
              "a" * 64, "b" * 64, world["user"]))
        return
    from tests.db_state import replica_replication_role_sync

    with replica_replication_role_sync(cursor):
        cursor.execute("""
            INSERT INTO payroll.payrollfinallines
                (companyid, branchid, payrollperiodid, driverassignmentid, linetype, quantity,
                 finalamount, sourcetype, sourceid, approvedatutc, linescope, sourcesnapshot,
                 currencycode, currencyminorunitdigits, driverid, payrollcalculationsnapshotid,
                 payrollcalculationsnapshotcomponentid, employeeid, componentkind)
            VALUES (%s, %s, %s, %s, 'BONUS', 1, 10, 'SnapshotComponent', '1', NOW(), 'Period',
                    '{}', 'USD', 2, 1, 1, 1, 1, 'BONUS')
        """, (world["company"], world["branches"][0], world["period"], world["assignment"]))


@pytest.mark.parametrize("kind", ["snapshot", "final_line"])
def test_0091_refuses_evidence_of_the_previous_contract_before_any_ddl(pre_0091_database, kind):
    env, dsn = pre_0091_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        _seed_evidence(cursor, _seed_world(cursor), kind)

    result = _alembic(env, "upgrade", "head")

    assert result.returncode != 0
    assert "CALCULATION_EVIDENCE_REBUILD_REQUIRED" in result.stderr + result.stdout
    assert _version(dsn) == "0090"
    # The refusal ran before any structural statement.
    assert "ordinaltiers" not in _columns(dsn, "paydefinitionrequests")
    assert "rateshapesnapshot" not in _columns(dsn, "payrollcalculationsnapshotusedratedefinitions")
    assert "payrollcalculationsnapshotusedratecomponents" not in _tables(dsn)


def test_0091_upgrades_a_database_without_calculation_evidence(pre_0091_database):
    env, dsn = pre_0091_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        _seed_world(cursor)          # periods, drivers and the like are untouched
    result = _alembic(env, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert _version(dsn) == "0092"


def test_0091_is_irreversible(head_database):
    env, _ = head_database
    result = _alembic(env, "downgrade", "0090")
    assert result.returncode != 0
    assert "irreversible" in (result.stderr + result.stdout)


def test_fresh_upgrade_reaches_the_ordinal_tier_evidence_shape(head_database):
    _, dsn = head_database
    assert _version(dsn) == "0092"
    assert "ordinaltiers" in _columns(dsn, "paydefinitionrequests")
    assert "rateshapesnapshot" in _columns(dsn, "payrollcalculationsnapshotusedratedefinitions")
    assert {"payrollcalculationsnapshotusedratedefinitionid", "payrollcalculationsnapshotid",
            "ratedefinitionid", "sequenceno", "ratecomponentdefinitionid", "ordinalfrom",
            "ordinalto", "amountsnapshot"} <= _columns(dsn, "payrollcalculationsnapshotusedratecomponents")
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        assert _check_calculation_evidence(cursor) == []
        cursor.execute("""
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conname = 'ck_payrollcalculationsnapshotlines_source'
              AND conrelid = 'payroll.payrollcalculationsnapshotlines'::regclass
        """)
        (definition,) = cursor.fetchone()
    # An ordinary line no longer has to name a single rate; a Status line still does.
    ordinary, status = definition.split("StatusEntryState")[0], definition.split("StatusEntryState")[1]
    assert "resolvedrateamount IS NOT NULL" not in ordinary
    assert "resolvedrateamount IS NOT NULL" in status
