"""P3b: real PostgreSQL race coverage for target compensation structure invariants."""
from __future__ import annotations

import pytest
from psycopg2 import errors

from tests.p3b_fixtures import (
    Blocked,
    Ordinal,
    Scalar,
    add_driver,
    approve,
    approved_scalar,
    connect,
    make_pending,
    p3b_cursor,  # noqa: F401 - registers the cur fixture
    p3b_database,  # noqa: F401 - registers the p3b_dsn fixture
    set_value,
    status_of,
    structure_locked_at,
)


def _insert_pending(fx, driver_id, effective_from="2026-01-01"):
    def run(cursor):
        make_pending(cursor, fx, driver_id=driver_id, effective_from=effective_from)
    return run


def _insert_component(fx, sequence_no=4, ordinal_from=100):
    def run(cursor):
        cursor.execute("""
            INSERT INTO payroll.ratecomponentdefinitions
                (ratedefinitionid, shape, sequenceno, ordinalfrom, ordinalto)
            VALUES (%s, 'OrdinalTierSchedule', %s, %s, NULL)
        """, (fx.rate_definition_id, sequence_no, ordinal_from))
    return run


def test_concurrent_pending_creation_for_one_identity_admits_exactly_one(p3b_dsn, cur):
    fx = Scalar(cur)
    first = connect(p3b_dsn)
    try:
        with first.cursor() as txn:
            make_pending(txn, fx)
            second = Blocked(p3b_dsn, _insert_pending(fx, fx.driver_id, "2027-01-01"))
            second.wait_for_lock(cur)
        first.commit()
        assert isinstance(second.finish(), errors.UniqueViolation)
    finally:
        first.close()
    cur.execute("SELECT count(*) FROM payroll.driverrateassignments "
                "WHERE driverid = %s AND status = 'Pending'", (fx.driver_id,))
    assert cur.fetchone()[0] == 1


def test_pending_creation_then_structure_change_cannot_both_commit(p3b_dsn, cur):
    fx = Ordinal(cur)
    creator = connect(p3b_dsn)
    try:
        with creator.cursor() as txn:
            make_pending(txn, fx)
            mutation = Blocked(p3b_dsn, _insert_component(fx))
            mutation.wait_for_lock(cur)
        creator.commit()
        error = mutation.finish()
    finally:
        creator.close()
    assert isinstance(error, errors.CheckViolation)
    assert "RATE_STRUCTURE_PENDING_ASSIGNMENT" in str(error)
    cur.execute("SELECT count(*) FROM payroll.ratecomponentdefinitions WHERE ratedefinitionid = %s",
                (fx.rate_definition_id,))
    assert cur.fetchone()[0] == 3


def test_structure_change_then_pending_creation_serializes_in_that_order(p3b_dsn, cur):
    fx = Ordinal(cur)
    mutator = connect(p3b_dsn)
    try:
        with mutator.cursor() as txn:
            txn.execute("DELETE FROM payroll.ratecomponentdefinitions "
                        "WHERE ratecomponentdefinitionid = %s", (fx.component_ids[2],))
            creation = Blocked(p3b_dsn, _insert_pending(fx, fx.driver_id))
            creation.wait_for_lock(cur)
        mutator.commit()
        assert creation.finish() is None
    finally:
        mutator.close()
    cur.execute("SELECT count(*) FROM payroll.ratecomponentdefinitions WHERE ratedefinitionid = %s",
                (fx.rate_definition_id,))
    assert cur.fetchone()[0] == 2
    cur.execute("SELECT count(*) FROM payroll.driverrateassignments "
                "WHERE ratedefinitionid = %s AND status = 'Pending'", (fx.rate_definition_id,))
    assert cur.fetchone()[0] == 1


def test_approval_vs_component_mutation_fails_the_mutation_on_the_new_lock(p3b_dsn, cur):
    fx = Ordinal(cur)
    pending = make_pending(cur, fx)
    for component_id in fx.component_ids:
        set_value(cur, pending, fx.rate_definition_id, component_id, "1")
    approver = connect(p3b_dsn)
    try:
        with approver.cursor() as txn:
            approve(txn, pending)
            mutation = Blocked(p3b_dsn, lambda c: c.execute(
                "UPDATE payroll.ratecomponentdefinitions SET ordinalto = 1 "
                "WHERE ratecomponentdefinitionid = %s", (fx.component_ids[0],)))
            mutation.wait_for_lock(cur)
        approver.commit()
        error = mutation.finish()
    finally:
        approver.close()
    assert isinstance(error, errors.CheckViolation) and "RATE_STRUCTURE_LOCKED" in str(error)
    assert status_of(cur, pending) == "Approved"
    assert structure_locked_at(cur, fx.rate_definition_id) is not None


def test_approval_vs_method_change_fails_the_method_change(p3b_dsn, cur):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "1")
    approver = connect(p3b_dsn)
    try:
        with approver.cursor() as txn:
            approve(txn, pending)
            change = Blocked(p3b_dsn, lambda c: c.execute(
                "UPDATE payroll.paydefinitions SET calculationmethod = 'OrdinalTier', "
                "inputtype = 'WholeNumber' WHERE paydefinitionid = %s", (fx.pay_definition_id,)))
            change.wait_for_lock(cur)
        approver.commit()
        error = change.finish()
    finally:
        approver.close()
    assert isinstance(error, errors.CheckViolation) and "RATE_STRUCTURE_LOCKED" in str(error)
    cur.execute("SELECT calculationmethod FROM payroll.paydefinitions WHERE paydefinitionid = %s",
                (fx.pay_definition_id,))
    assert cur.fetchone()[0] == "PerUnit"


def test_method_change_vs_pending_creation_cannot_both_commit(p3b_dsn, cur):
    fx = Scalar(cur)
    changer = connect(p3b_dsn)
    try:
        with changer.cursor() as txn:
            txn.execute("UPDATE payroll.paydefinitions SET calculationmethod = 'OrdinalTier', "
                        "inputtype = 'WholeNumber' WHERE paydefinitionid = %s",
                        (fx.pay_definition_id,))
            txn.execute("DELETE FROM payroll.ratecomponentdefinitions WHERE ratedefinitionid = %s",
                        (fx.rate_definition_id,))
            txn.execute("UPDATE payroll.ratedefinitions SET shape = 'OrdinalTierSchedule' "
                        "WHERE ratedefinitionid = %s", (fx.rate_definition_id,))
            txn.execute("""
                INSERT INTO payroll.ratecomponentdefinitions
                    (ratedefinitionid, shape, sequenceno, ordinalfrom, ordinalto)
                VALUES (%s, 'OrdinalTierSchedule', 1, 1, NULL)
            """, (fx.rate_definition_id,))
            creation = Blocked(p3b_dsn, _insert_pending(fx, fx.driver_id))
            creation.wait_for_lock(cur)
        changer.commit()
        assert creation.finish() is None  # Pending is created against the final structure
    finally:
        changer.close()
    cur.execute("SELECT shape FROM payroll.ratedefinitions WHERE ratedefinitionid = %s",
                (fx.rate_definition_id,))
    assert cur.fetchone()[0] == "OrdinalTierSchedule"


def test_pending_creation_then_method_change_fails_the_method_change(p3b_dsn, cur):
    fx = Scalar(cur)
    creator = connect(p3b_dsn)
    try:
        with creator.cursor() as txn:
            make_pending(txn, fx)
            change = Blocked(p3b_dsn, lambda c: c.execute(
                "UPDATE payroll.paydefinitions SET calculationmethod = 'OrdinalTier', "
                "inputtype = 'WholeNumber' WHERE paydefinitionid = %s", (fx.pay_definition_id,)))
            change.wait_for_lock(cur)
        creator.commit()
        error = change.finish()
    finally:
        creator.close()
    assert isinstance(error, errors.CheckViolation)
    assert "RATE_STRUCTURE_PENDING_ASSIGNMENT" in str(error)


def test_value_write_vs_approval_serializes_and_freezes_the_value_set(p3b_dsn, cur):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "1")
    approver = connect(p3b_dsn)
    try:
        with approver.cursor() as txn:
            approve(txn, pending)
            edit = Blocked(p3b_dsn, lambda c: c.execute(
                "UPDATE payroll.driverratevalues SET amount = 500 "
                "WHERE driverrateassignmentid = %s", (pending,)))
            edit.wait_for_lock(cur)
        approver.commit()
        error = edit.finish()
    finally:
        approver.close()
    assert isinstance(error, errors.CheckViolation)
    assert "RATE_VALUES_IMMUTABLE_AFTER_APPROVAL" in str(error)
    cur.execute("SELECT amount FROM payroll.driverratevalues WHERE driverrateassignmentid = %s",
                (pending,))
    assert cur.fetchone()[0] == 1


def test_authoritative_overlap_race_resolves_to_the_committed_window(p3b_dsn, cur):
    """Superseding the current assignment and approving its successor race on one identity."""
    fx = Scalar(cur)
    current = approved_scalar(cur, fx, effective_from="2026-01-01")
    successor = make_pending(cur, fx, effective_from="2026-04-01")
    set_value(cur, successor, fx.rate_definition_id, fx.component_id, "2")

    closer = connect(p3b_dsn)
    try:
        with closer.cursor() as txn:
            txn.execute("UPDATE payroll.driverrateassignments SET status = 'Superseded', "
                        "effectiveto = '2026-03-31' WHERE driverrateassignmentid = %s", (current,))
            approval = Blocked(p3b_dsn, lambda c: approve(c, successor))
            approval.wait_for_lock(cur)
        closer.commit()
        assert approval.finish() is None
    finally:
        closer.close()
    assert (status_of(cur, current), status_of(cur, successor)) == ("Superseded", "Approved")


def test_authoritative_overlap_race_rejects_the_overlapping_commit(p3b_dsn, cur):
    fx = Scalar(cur)
    current = approved_scalar(cur, fx, effective_from="2026-01-01")
    successor = make_pending(cur, fx, effective_from="2026-04-01")
    set_value(cur, successor, fx.rate_definition_id, fx.component_id, "2")

    closer = connect(p3b_dsn)
    try:
        with closer.cursor() as txn:
            txn.execute("UPDATE payroll.driverrateassignments SET status = 'Superseded', "
                        "effectiveto = '2026-06-30' WHERE driverrateassignmentid = %s", (current,))
            approval = Blocked(p3b_dsn, lambda c: approve(c, successor))
            approval.wait_for_lock(cur)
        closer.commit()
        assert isinstance(approval.finish(), errors.ExclusionViolation)
    finally:
        closer.close()
    assert status_of(cur, successor) == "Pending"


def test_discard_vs_value_write_never_leaves_orphaned_values(p3b_dsn, cur):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "1")
    discarder = connect(p3b_dsn)
    try:
        with discarder.cursor() as txn:
            txn.execute("DELETE FROM payroll.driverrateassignments WHERE driverrateassignmentid = %s",
                        (pending,))
            edit = Blocked(p3b_dsn, lambda c: c.execute(
                "UPDATE payroll.driverratevalues SET amount = 2 WHERE driverrateassignmentid = %s",
                (pending,)))
            edit.wait_for_lock(cur)
        discarder.commit()
        edit.finish()
    finally:
        discarder.close()
    cur.execute("SELECT count(*) FROM payroll.driverratevalues WHERE driverrateassignmentid = %s",
                (pending,))
    assert cur.fetchone()[0] == 0


def test_unrelated_rate_definitions_do_not_contend(p3b_dsn, cur):
    first, second = Scalar(cur), Scalar(cur)
    holder = connect(p3b_dsn)
    other = connect(p3b_dsn)
    try:
        with holder.cursor() as txn, other.cursor() as other_txn:
            make_pending(txn, first)
            other_txn.execute("SET lock_timeout = '1s'")
            make_pending(other_txn, second)  # would time out if serialized on a shared lock
            other.commit()
        holder.commit()
    finally:
        holder.close()
        other.close()


def test_same_rate_definition_different_drivers_serialize_but_both_commit(p3b_dsn, cur):
    fx = Scalar(cur, driver_count=2)
    holder = connect(p3b_dsn)
    try:
        with holder.cursor() as txn:
            make_pending(txn, fx, driver_id=fx.driver_ids[0])
            second = Blocked(p3b_dsn, _insert_pending(fx, fx.driver_ids[1]))
            second.wait_for_lock(cur)
        holder.commit()
        assert second.finish() is None
    finally:
        holder.close()


def test_approval_vs_company_currency_change_serializes_on_the_company_row(p3b_dsn, cur):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "1")
    approver = connect(p3b_dsn)
    try:
        with approver.cursor() as txn:
            approve(txn, pending)
            change = Blocked(p3b_dsn, lambda c: c.execute(
                "UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s",
                (fx.company_id,)))
            change.wait_for_lock(cur)
        approver.commit()
        error = change.finish()
    finally:
        approver.close()
    assert isinstance(error, errors.CheckViolation)
    assert "COMPANY_CURRENCY_CHANGE_BLOCKED" in str(error)
    cur.execute("SELECT currencycode FROM core.companies WHERE companyid = %s", (fx.company_id,))
    assert cur.fetchone()[0] == "USD"


def test_currency_change_first_lets_approval_proceed_under_the_new_currency(p3b_dsn, cur):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "1")
    changer = connect(p3b_dsn)
    try:
        with changer.cursor() as txn:
            txn.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s",
                        (fx.company_id,))
            approval = Blocked(p3b_dsn, lambda c: approve(c, pending))
            approval.wait_for_lock(cur)
        changer.commit()
        assert approval.finish() is None
    finally:
        changer.close()
    cur.execute("SELECT currencycode FROM core.companies WHERE companyid = %s", (fx.company_id,))
    assert cur.fetchone()[0] == "EUR"
    assert status_of(cur, pending) == "Approved"


@pytest.mark.asyncio
async def test_application_lock_helper_takes_the_same_row_lock(p3b_dsn, cur):
    """The helper and the database triggers contend on the same RateDefinitions row."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.rate_definition_concurrency import lock_rate_definition_structure

    fx = Scalar(cur)
    url = (f"postgresql+asyncpg://{p3b_dsn['user']}@{p3b_dsn['host']}:{p3b_dsn['port']}"
           f"/{p3b_dsn['database']}")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as db:
            assert await lock_rate_definition_structure(fx.rate_definition_id, db) is False
            blocked = Blocked(p3b_dsn, _insert_pending(fx, fx.driver_id))
            await asyncio.to_thread(blocked.wait_for_lock, cur)
        assert blocked.finish() is None
        approved_driver = add_driver(cur, fx)
        approved_scalar(cur, fx, driver_id=approved_driver)
        async with engine.begin() as db:
            assert await lock_rate_definition_structure(fx.rate_definition_id, db) is True
    finally:
        await engine.dispose()
