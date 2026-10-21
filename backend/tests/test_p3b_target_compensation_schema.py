"""P3b: dormant target compensation persistence model and its database invariants."""
from __future__ import annotations

import json

import pytest
from psycopg2 import errors

from tests.builders.status import make_status_column
from tests.compensation_database_fixtures import (
    Ordinal,
    Scalar,
    add_driver,
    add_scalar_component,
    add_tiers,
    approve,
    approved_scalar,
    connect,
    make_branch,
    make_company,
    make_driver_pair,
    make_pay_definition,
    make_pending,
    make_rate_definition,
    p3b_cursor,  # noqa: F401 - registers the cur fixture
    p3b_database,  # noqa: F401 - registers the p3b_dsn fixture
    set_value,
    status_of,
    structure_locked_at,
    supersede,
    user_id,
)

TARGET_TABLES = (
    "paydefinitions",
    "ratedefinitions",
    "ratecomponentdefinitions",
    "driverrateschedules",
    "driverrateschedulevalues",
)


def rejected(error, match: str | None = None):
    return pytest.raises(error, match=match)


# ---------------------------------------------------------------------------
# Schema shape and dormancy
# ---------------------------------------------------------------------------

def test_target_tables_are_separate_from_legacy_pay_items(cur):
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'payroll' AND table_name = ANY(%s)
    """, (list(TARGET_TABLES),))
    assert {row[0] for row in cur.fetchall()} == set(TARGET_TABLES)

    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'payroll' AND table_name = 'payitems'
          AND column_name IN ('calculationmethod', 'structurelockedatutc', 'ratedefinitionid')
    """)
    assert cur.fetchall() == []


def test_structure_lock_exists_only_on_rate_definitions(cur):
    cur.execute("""
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = 'payroll' AND column_name = 'structurelockedatutc'
    """)
    assert [row[0] for row in cur.fetchall()] == ["ratedefinitions"]


def test_child_tables_have_no_effective_date_or_lifecycle_columns(cur):
    cur.execute("""
        SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema = 'payroll'
          AND table_name IN ('driverrateschedulevalues', 'ratecomponentdefinitions')
          AND (column_name LIKE '%%effective%%' OR column_name LIKE '%%status%%'
               OR column_name LIKE '%%approved%%' OR column_name LIKE '%%valid%%')
    """)
    assert cur.fetchall() == []


def test_value_amount_uses_internal_precision_without_payable_rounding(cur):
    cur.execute("""
        SELECT numeric_precision, numeric_scale FROM information_schema.columns
        WHERE table_schema = 'payroll' AND table_name = 'driverrateschedulevalues' AND column_name = 'amount'
    """)
    assert cur.fetchone() == (18, 4)

    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "12.34567")
    cur.execute("SELECT amount FROM payroll.driverrateschedulevalues WHERE driverratescheduleid = %s",
                (pending,))
    assert str(cur.fetchone()[0]) == "12.3457"  # NUMERIC(18,4) storage, no payable rounding


def test_company_is_valid_with_zero_pay_definitions(cur):
    company_id = make_company(cur)
    cur.execute("SELECT count(*) FROM payroll.paydefinitions WHERE companyid = %s", (company_id,))
    assert cur.fetchone()[0] == 0
    # The Company's only rate definition is the one its default Status rate column owns.
    cur.execute("""
        SELECT count(*) FILTER (WHERE paydefinitionid IS NOT NULL),
               count(*) FILTER (WHERE statusratecolumnid IS NOT NULL)
        FROM payroll.ratedefinitions WHERE companyid = %s""", (company_id,))
    assert cur.fetchone() == (0, 1)


# ---------------------------------------------------------------------------
# PayDefinition: business names carry no meaning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["HOURS", "MILES", "LOADS", "WAIT_TIME", "ITEM_ALPHA", "q'z-9_%"])
@pytest.mark.parametrize("method", ["PerUnit", "OrdinalTier"])
def test_any_code_or_name_behaves_identically_for_either_method(cur, code, method):
    company_id = make_company(cur)
    branch_id = make_branch(cur, company_id)
    driver_id, _ = make_driver_pair(cur, company_id, branch_id)
    pay_definition_id = make_pay_definition(cur, company_id, code=code, name=code.lower(),
                                            method=method)
    rate_definition_id = make_rate_definition(cur, pay_definition_id)
    if method == "PerUnit":
        components = [add_scalar_component(cur, rate_definition_id)]
        amounts = ["7.5"]
    else:
        components = add_tiers(cur, rate_definition_id)
        amounts = ["3", "4", "5"]

    cur.execute("""
        INSERT INTO payroll.driverrateschedules
            (companyid, driverid, paydefinitionid, ratedefinitionid, effectivefrom)
        VALUES (%s, %s, %s, %s, '2026-01-01') RETURNING driverratescheduleid
    """, (company_id, driver_id, pay_definition_id, rate_definition_id))
    schedule_id = cur.fetchone()[0]
    for component_id, amount in zip(components, amounts, strict=True):
        set_value(cur, schedule_id, rate_definition_id, component_id, amount)
    approve(cur, schedule_id)
    assert status_of(cur, schedule_id) == "Approved"
    assert structure_locked_at(cur, rate_definition_id) is not None


def test_two_companies_use_different_methods_independently(cur):
    alpha = Scalar(cur)
    beta = Ordinal(cur)
    cur.execute("UPDATE payroll.paydefinitions SET definitioncode = 'ITEM_ALPHA' "
                "WHERE paydefinitionid = %s", (alpha.pay_definition_id,))
    cur.execute("UPDATE payroll.paydefinitions SET definitioncode = 'ITEM_BETA' "
                "WHERE paydefinitionid = %s", (beta.pay_definition_id,))
    approved_scalar(cur, alpha, "25")
    pending = make_pending(cur, beta)
    for component_id, amount in zip(beta.component_ids, ["3", "4", "5"], strict=True):
        set_value(cur, pending, beta.rate_definition_id, component_id, amount)
    approve(cur, pending)
    assert structure_locked_at(cur, alpha.rate_definition_id) is not None
    assert structure_locked_at(cur, beta.rate_definition_id) is not None


def test_same_code_is_allowed_across_companies_but_not_within_one(cur):
    first, second = make_company(cur), make_company(cur)
    make_pay_definition(cur, first, code="SHARED")
    make_pay_definition(cur, second, code="SHARED")
    with rejected(errors.UniqueViolation):
        make_pay_definition(cur, first, code="SHARED")


def test_only_first_production_methods_and_whole_number_ordinal(cur):
    company_id = make_company(cur)
    for method in ("RangeBracket", "Fixed", "Block", "Calculated", "EnteredAmount", "None"):
        with rejected(errors.CheckViolation):
            make_pay_definition(cur, company_id, method=method, input_type="Decimal")
    with rejected(errors.CheckViolation):
        make_pay_definition(cur, company_id, method="OrdinalTier", input_type="Decimal")


# ---------------------------------------------------------------------------
# Company / Branch / Driver ownership
# ---------------------------------------------------------------------------

def test_rate_definition_must_share_its_pay_definition_company(cur):
    pd_company = make_company(cur)
    other_company = make_company(cur)
    pay_definition_id = make_pay_definition(cur, pd_company)
    with rejected(errors.ForeignKeyViolation):
        cur.execute("""
            INSERT INTO payroll.ratedefinitions (companyid, paydefinitionid, shape)
            VALUES (%s, %s, 'Scalar')
        """, (other_company, pay_definition_id))


def test_schedule_rejects_driver_company_and_pay_definition_mismatch(cur):
    fx = Scalar(cur)
    other_company = make_company(cur)
    other_driver_company = Scalar(cur)
    other_pay_definition = make_pay_definition(cur, fx.company_id)
    insert = """
        INSERT INTO payroll.driverrateschedules
            (companyid, driverid, paydefinitionid, ratedefinitionid, effectivefrom)
        VALUES (%s, %s, %s, %s, '2026-01-01')
    """
    with rejected(errors.ForeignKeyViolation):  # Driver belongs to another Company
        cur.execute(insert, (fx.company_id, other_driver_company.driver_id, fx.pay_definition_id,
                             fx.rate_definition_id))
    with rejected(errors.ForeignKeyViolation):  # Company differs from Driver and RateDefinition
        cur.execute(insert, (other_company, fx.driver_id, fx.pay_definition_id, fx.rate_definition_id))
    with rejected(errors.ForeignKeyViolation):  # PayDefinition is not the RateDefinition's own
        cur.execute(insert, (fx.company_id, fx.driver_id, other_pay_definition, fx.rate_definition_id))
    with rejected(errors.NotNullViolation):  # a schedule is always owned by a Driver
        cur.execute("""
            INSERT INTO payroll.driverrateschedules
                (companyid, paydefinitionid, ratedefinitionid, effectivefrom)
            VALUES (%s, %s, %s, '2026-01-01')
        """, (fx.company_id, fx.pay_definition_id, fx.rate_definition_id))


def test_schedule_rejects_rate_definition_from_another_company(cur):
    fx = Scalar(cur)
    other = Scalar(cur)
    with rejected(errors.ForeignKeyViolation):
        make_pending(cur, fx, rate_definition_id=other.rate_definition_id)


def test_schedule_identity_is_immutable(cur):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    other_driver = add_driver(cur, fx)
    with rejected(errors.CheckViolation, "RATE_SCHEDULE_IDENTITY_IMMUTABLE"):
        cur.execute("UPDATE payroll.driverrateschedules SET driverid = %s "
                    "WHERE driverratescheduleid = %s", (other_driver, pending))


def test_rate_definition_has_exactly_one_owner(cur):
    fx = Scalar(cur)
    with rejected(errors.CheckViolation):
        cur.execute("INSERT INTO payroll.ratedefinitions (companyid, shape) VALUES (%s, 'Scalar')",
                    (fx.company_id,))
    with rejected(errors.UniqueViolation):
        make_rate_definition(cur, fx.pay_definition_id)


def test_status_owner_is_company_owned_scalar_and_schedulable_only_by_its_column(cur):
    fx = Scalar(cur)
    cur.execute("SELECT payroll.fn_EnsureDefaultStatusRateColumn(%s)", (fx.company_id,))
    column_id = cur.fetchone()[0]
    cur.execute("SELECT ratedefinitionid FROM payroll.ratedefinitions WHERE statusratecolumnid = %s",
                (column_id,))
    status_rate_definition = cur.fetchone()[0]

    with rejected(errors.UniqueViolation):  # one definition per column
        cur.execute("""
            INSERT INTO payroll.ratedefinitions (companyid, statusratecolumnid, shape)
            VALUES (%s, %s, 'Scalar')
        """, (fx.company_id, column_id))
    with rejected(errors.CheckViolation):  # Status-owned definitions are scalar
        cur.execute("""
            INSERT INTO payroll.ratedefinitions (companyid, statusratecolumnid, shape)
            VALUES (%s, %s, 'OrdinalTierSchedule')
        """, (fx.company_id, make_status_column(cur, fx.company_id).status_rate_column_id))
    with rejected(errors.CheckViolation):  # exactly one owner
        cur.execute("""
            INSERT INTO payroll.ratedefinitions (companyid, paydefinitionid, statusratecolumnid, shape)
            VALUES (%s, %s, %s, 'Scalar')
        """, (fx.company_id, fx.pay_definition_id, column_id))

    # A PayDefinition cannot own a schedule of a Status-owned definition.
    with rejected(errors.ForeignKeyViolation):
        cur.execute("""
            INSERT INTO payroll.driverrateschedules
                (companyid, driverid, paydefinitionid, ratedefinitionid, effectivefrom)
            VALUES (%s, %s, %s, %s, '2026-01-01')
        """, (fx.company_id, fx.driver_id, fx.pay_definition_id, status_rate_definition))
    cur.execute("""
        INSERT INTO payroll.driverrateschedules
            (companyid, driverid, statusratecolumnid, ratedefinitionid, effectivefrom)
        VALUES (%s, %s, %s, %s, '2026-01-01')
    """, (fx.company_id, fx.driver_id, column_id, status_rate_definition))


# ---------------------------------------------------------------------------
# Component structure
# ---------------------------------------------------------------------------

def test_scalar_structure_allows_exactly_one_component(cur):
    fx = Scalar(cur)
    with rejected(errors.CheckViolation):
        cur.execute("""
            INSERT INTO payroll.ratecomponentdefinitions (ratedefinitionid, shape, sequenceno)
            VALUES (%s, 'Scalar', 2)
        """, (fx.rate_definition_id,))
    with rejected(errors.UniqueViolation):
        add_scalar_component(cur, fx.rate_definition_id)


def test_component_shape_must_match_its_rate_definition(cur):
    fx = Scalar(cur)
    with rejected(errors.ForeignKeyViolation):
        cur.execute("""
            INSERT INTO payroll.ratecomponentdefinitions
                (ratedefinitionid, shape, sequenceno, ordinalfrom, ordinalto)
            VALUES (%s, 'OrdinalTierSchedule', 2, 1, NULL)
        """, (fx.rate_definition_id,))


def test_ordinal_tiers_cannot_overlap_or_have_two_open_ended_tiers(cur):
    fx = Ordinal(cur, tiers=[(1, 2), (3, None)])
    with rejected(errors.ExclusionViolation):
        cur.execute("""
            INSERT INTO payroll.ratecomponentdefinitions
                (ratedefinitionid, shape, sequenceno, ordinalfrom, ordinalto)
            VALUES (%s, 'OrdinalTierSchedule', 3, 2, 4)
        """, (fx.rate_definition_id,))
    with rejected(errors.ExclusionViolation):
        cur.execute("""
            INSERT INTO payroll.ratecomponentdefinitions
                (ratedefinitionid, shape, sequenceno, ordinalfrom, ordinalto)
            VALUES (%s, 'OrdinalTierSchedule', 3, 9, NULL)
        """, (fx.rate_definition_id,))


@pytest.mark.parametrize("ordinal_from, ordinal_to", [(0, 1), (-1, 3), (None, 3), (5, 4)])
def test_ordinal_bounds_must_be_positive_and_ordered(cur, ordinal_from, ordinal_to):
    fx = Ordinal(cur, tiers=[])
    with rejected(errors.CheckViolation):
        cur.execute("""
            INSERT INTO payroll.ratecomponentdefinitions
                (ratedefinitionid, shape, sequenceno, ordinalfrom, ordinalto)
            VALUES (%s, 'OrdinalTierSchedule', 1, %s, %s)
        """, (fx.rate_definition_id, ordinal_from, ordinal_to))


@pytest.mark.parametrize("tiers", [
    [(2, 2), (3, None)],                 # first tier does not start at 1
    [(1, 1), (3, None)],                 # gap
    [(1, 1), (2, 3)],                    # final tier is not open-ended
    [],                                  # no components at all
])
def test_approval_rejects_invalid_ordinal_topology(cur, tiers):
    fx = Ordinal(cur, tiers=tiers)
    pending = make_pending(cur, fx)
    for component_id in fx.component_ids:
        set_value(cur, pending, fx.rate_definition_id, component_id, "1")
    with rejected(errors.CheckViolation, "RATE_STRUCTURE_(INVALID_TOPOLOGY|INCOMPLETE)"):
        approve(cur, pending)
    assert status_of(cur, pending) == "Pending"
    assert structure_locked_at(cur, fx.rate_definition_id) is None


def test_single_open_ended_ordinal_tier_is_a_valid_schedule(cur):
    fx = Ordinal(cur, tiers=[(1, None)])
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_ids[0], "4")
    approve(cur, pending)
    assert status_of(cur, pending) == "Approved"


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

def test_negative_values_are_rejected_and_zero_is_accepted(cur):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    with rejected(errors.CheckViolation):
        set_value(cur, pending, fx.rate_definition_id, fx.component_id, "-0.0001")
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "0")
    approve(cur, pending)
    assert status_of(cur, pending) == "Approved"
    cur.execute("SELECT amount FROM payroll.driverrateschedulevalues WHERE driverratescheduleid = %s",
                (pending,))
    assert cur.fetchone()[0] == 0


@pytest.mark.parametrize("missing_as", ["absent", "null"])
def test_missing_value_is_not_zero_and_blocks_approval(cur, missing_as):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    if missing_as == "null":
        set_value(cur, pending, fx.rate_definition_id, fx.component_id, None)
    with rejected(errors.CheckViolation, "RATE_SCHEDULE_INCOMPLETE"):
        approve(cur, pending)
    assert status_of(cur, pending) == "Pending"
    assert structure_locked_at(cur, fx.rate_definition_id) is None


def test_duplicate_component_value_is_rejected(cur):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "1")
    with rejected(errors.UniqueViolation):
        set_value(cur, pending, fx.rate_definition_id, fx.component_id, "2")


def test_value_from_another_rate_definition_is_impossible(cur):
    fx = Scalar(cur)
    other_definition = make_rate_definition(cur, make_pay_definition(cur, fx.company_id))
    foreign_component = add_scalar_component(cur, other_definition)
    pending = make_pending(cur, fx)
    # Foreign component paired with either RateDefinition identity.
    with rejected(errors.ForeignKeyViolation):
        set_value(cur, pending, fx.rate_definition_id, foreign_component, "1")
    with rejected(errors.ForeignKeyViolation):
        set_value(cur, pending, other_definition, foreign_component, "1")
    with rejected(errors.ForeignKeyViolation):
        set_value(cur, pending, other_definition, fx.component_id, "1")


def test_ordinal_approval_requires_every_tier_value(cur):
    fx = Ordinal(cur)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_ids[0], "3")
    set_value(cur, pending, fx.rate_definition_id, fx.component_ids[1], "4")
    with rejected(errors.CheckViolation, "RATE_SCHEDULE_INCOMPLETE"):
        approve(cur, pending)
    set_value(cur, pending, fx.rate_definition_id, fx.component_ids[2], "5")
    approve(cur, pending)
    assert status_of(cur, pending) == "Approved"


def test_one_schedule_owns_one_complete_value_set(cur):
    fx = Ordinal(cur)
    first = make_pending(cur, fx, effective_from="2026-01-01")
    for component_id, amount in zip(fx.component_ids, ["3", "4", "5"], strict=True):
        set_value(cur, first, fx.rate_definition_id, component_id, amount)
    approve(cur, first)
    supersede(cur, first, "2026-05-31")
    second = make_pending(cur, fx, effective_from="2026-06-01")
    for component_id, amount in zip(fx.component_ids, ["6", "7", "8"], strict=True):
        set_value(cur, second, fx.rate_definition_id, component_id, amount)
    approve(cur, second)
    cur.execute("""
        SELECT driverratescheduleid, array_agg(amount ORDER BY ratecomponentdefinitionid)
        FROM payroll.driverrateschedulevalues
        WHERE driverratescheduleid IN (%s, %s) GROUP BY 1 ORDER BY 1
    """, (first, second))
    assert [[int(v) for v in row[1]] for row in cur.fetchall()] == [[3, 4, 5], [6, 7, 8]]


# ---------------------------------------------------------------------------
# Approval, lifecycle and immutability
# ---------------------------------------------------------------------------

def test_schedule_must_be_created_pending(cur):
    fx = Scalar(cur)
    with rejected(errors.CheckViolation):
        cur.execute("""
            INSERT INTO payroll.driverrateschedules
                (companyid, driverid, paydefinitionid, ratedefinitionid, effectivefrom, status,
                 approvedbyuserid, approvedatutc)
            VALUES (%s, %s, %s, %s, '2026-01-01', 'Approved', %s, NOW())
        """, (fx.company_id, fx.driver_id, fx.pay_definition_id, fx.rate_definition_id, user_id(cur)))


def test_approval_requires_approver_metadata(cur):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "1")
    with rejected(errors.CheckViolation):
        cur.execute("UPDATE payroll.driverrateschedules SET status = 'Approved' "
                    "WHERE driverratescheduleid = %s", (pending,))
    assert status_of(cur, pending) == "Pending"


def test_first_approval_sets_the_single_structure_lock(cur):
    fx = Scalar(cur, driver_count=2)
    assert structure_locked_at(cur, fx.rate_definition_id) is None
    make_pending(cur, fx)
    assert structure_locked_at(cur, fx.rate_definition_id) is None  # Pending never locks

    approved_scalar(cur, fx, driver_id=fx.driver_ids[1])
    first_lock = structure_locked_at(cur, fx.rate_definition_id)
    assert first_lock is not None

    cur.execute("SELECT pg_sleep(0.05)")
    cur.execute("""
        SELECT driverratescheduleid FROM payroll.driverrateschedules
        WHERE driverid = %s AND status = 'Pending'
    """, (fx.driver_ids[0],))
    schedule = cur.fetchone()[0]
    set_value(cur, schedule, fx.rate_definition_id, fx.component_id, "2")
    approve(cur, schedule)
    assert structure_locked_at(cur, fx.rate_definition_id) == first_lock


def test_values_are_frozen_once_the_schedule_is_authoritative(cur):
    fx = Scalar(cur)
    schedule = approved_scalar(cur, fx)
    with rejected(errors.CheckViolation, "RATE_VALUES_IMMUTABLE_AFTER_APPROVAL"):
        cur.execute("UPDATE payroll.driverrateschedulevalues SET amount = 99 "
                    "WHERE driverratescheduleid = %s", (schedule,))
    with rejected(errors.CheckViolation, "RATE_VALUES_IMMUTABLE_AFTER_APPROVAL"):
        cur.execute("DELETE FROM payroll.driverrateschedulevalues WHERE driverratescheduleid = %s",
                    (schedule,))


def test_pending_values_are_editable_in_place(cur):
    fx = Scalar(cur)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "1")
    cur.execute("UPDATE payroll.driverrateschedulevalues SET amount = 2 WHERE driverratescheduleid = %s",
                (pending,))
    cur.execute("UPDATE payroll.driverrateschedules SET effectivefrom = '2026-02-01' "
                "WHERE driverratescheduleid = %s", (pending,))
    approve(cur, pending)
    cur.execute("SELECT effectivefrom, status FROM payroll.driverrateschedules "
                "WHERE driverratescheduleid = %s", (pending,))
    assert cur.fetchone()[1] == "Approved"


def test_authoritative_schedule_fields_are_immutable(cur):
    fx = Scalar(cur)
    schedule = approved_scalar(cur, fx)
    for column, value in (("effectivefrom", "2026-02-01"), ("effectiveto", "2026-12-31")):
        with rejected(errors.CheckViolation, "RATE_SCHEDULE_AUTHORITATIVE_IMMUTABLE"):
            cur.execute(f"UPDATE payroll.driverrateschedules SET {column} = %s "
                        "WHERE driverratescheduleid = %s", (value, schedule))


def test_lifecycle_transitions(cur):
    fx = Scalar(cur, driver_count=3)
    pending_to_void = make_pending(cur, fx)
    with rejected(errors.CheckViolation, "RATE_SCHEDULE_INVALID_TRANSITION"):
        cur.execute("""
            UPDATE payroll.driverrateschedules
               SET status = 'Voided', voidedatutc = NOW() WHERE driverratescheduleid = %s
        """, (pending_to_void,))
    with rejected(errors.CheckViolation, "RATE_SCHEDULE_INVALID_TRANSITION"):
        cur.execute("""
            UPDATE payroll.driverrateschedules
               SET status = 'Superseded', effectiveto = '2026-12-31'
             WHERE driverratescheduleid = %s
        """, (pending_to_void,))

    approved = approved_scalar(cur, fx, driver_id=fx.driver_ids[1])
    with rejected(errors.CheckViolation, "RATE_SCHEDULE_INVALID_TRANSITION"):
        cur.execute("UPDATE payroll.driverrateschedules SET status = 'Pending' "
                    "WHERE driverratescheduleid = %s", (approved,))
    with rejected(errors.CheckViolation, "RATE_SCHEDULE_DELETE_BLOCKED"):
        cur.execute("DELETE FROM payroll.driverrateschedules WHERE driverratescheduleid = %s",
                    (approved,))

    cur.execute("""
        UPDATE payroll.driverrateschedules
           SET status = 'Voided', voidedbyuserid = %s, voidedatutc = NOW(), voidreason = 'test'
         WHERE driverratescheduleid = %s
    """, (user_id(cur), approved))
    with rejected(errors.CheckViolation, "RATE_SCHEDULE_AUTHORITATIVE_IMMUTABLE"):
        cur.execute("UPDATE payroll.driverrateschedules SET status = 'Approved' "
                    "WHERE driverratescheduleid = %s", (approved,))


def test_superseded_schedule_requires_a_closed_window(cur):
    fx = Scalar(cur)
    schedule = approved_scalar(cur, fx)
    with rejected(errors.CheckViolation):
        cur.execute("UPDATE payroll.driverrateschedules SET status = 'Superseded' "
                    "WHERE driverratescheduleid = %s", (schedule,))


def test_schedule_has_no_generalized_authority_flag(cur):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'payroll' AND table_name = 'driverrateschedules'
          AND column_name LIKE '%authoritative%'
    """)
    assert cur.fetchall() == []
    cur.execute("""
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'payroll.driverrateschedules'::regclass
          AND conname = 'uq_driverrateschedules_id_authoritative'
    """)
    assert cur.fetchall() == []


# ---------------------------------------------------------------------------
# Effective windows
# ---------------------------------------------------------------------------

def test_overlapping_authoritative_windows_are_rejected(cur):
    fx = Scalar(cur)
    first = approved_scalar(cur, fx, effective_from="2026-01-01", effective_to="2026-06-30")
    supersede(cur, first, "2026-06-30")
    overlapping = make_pending(cur, fx, effective_from="2026-06-30")
    set_value(cur, overlapping, fx.rate_definition_id, fx.component_id, "2")
    with rejected(errors.ExclusionViolation):
        approve(cur, overlapping)
    assert status_of(cur, first) == "Superseded"

    cur.execute("UPDATE payroll.driverrateschedules SET effectivefrom = '2026-07-01' "
                "WHERE driverratescheduleid = %s", (overlapping,))
    approve(cur, overlapping)  # adjacent, not overlapping


def test_at_most_one_approved_schedule_per_logical_identity(cur):
    fx = Scalar(cur)
    approved_scalar(cur, fx, effective_from="2026-01-01", effective_to="2026-06-30")
    later = make_pending(cur, fx, effective_from="2026-07-01")
    set_value(cur, later, fx.rate_definition_id, fx.component_id, "2")
    with rejected(errors.UniqueViolation):
        approve(cur, later)


def test_open_ended_approved_schedule_blocks_a_later_overlapping_approval(cur):
    fx = Scalar(cur)
    approved_scalar(cur, fx, effective_from="2026-01-01")
    later = make_pending(cur, fx, effective_from="2030-01-01")
    set_value(cur, later, fx.rate_definition_id, fx.component_id, "2")
    with rejected((errors.ExclusionViolation, errors.UniqueViolation)):
        approve(cur, later)


def test_supersession_closes_the_previous_window_before_the_next_approval(cur):
    fx = Scalar(cur)
    first = approved_scalar(cur, fx, effective_from="2026-01-01")
    supersede(cur, first, "2026-05-31")
    second = make_pending(cur, fx, effective_from="2026-06-01")
    set_value(cur, second, fx.rate_definition_id, fx.component_id, "30")
    approve(cur, second)
    assert (status_of(cur, first), status_of(cur, second)) == ("Superseded", "Approved")


def test_windows_are_independent_per_driver_and_per_rate_definition(cur):
    fx = Scalar(cur, driver_count=2)
    approved_scalar(cur, fx, driver_id=fx.driver_ids[0])
    approved_scalar(cur, fx, driver_id=fx.driver_ids[1])  # same window, other Driver

    other_pay_definition = make_pay_definition(cur, fx.company_id)
    other_definition = make_rate_definition(cur, other_pay_definition)
    other_component = add_scalar_component(cur, other_definition)
    schedule = make_pending(cur, fx, rate_definition_id=other_definition)  # same Driver, other definition
    set_value(cur, schedule, other_definition, other_component, "1")
    approve(cur, schedule)


def test_voided_schedule_no_longer_occupies_its_window(cur):
    fx = Scalar(cur)
    first = approved_scalar(cur, fx, effective_from="2026-01-01")
    cur.execute("""
        UPDATE payroll.driverrateschedules
           SET status = 'Voided', voidedbyuserid = %s, voidedatutc = NOW()
         WHERE driverratescheduleid = %s
    """, (user_id(cur), first))
    replacement = make_pending(cur, fx, effective_from="2026-01-01")
    set_value(cur, replacement, fx.rate_definition_id, fx.component_id, "9")
    approve(cur, replacement)


def test_effective_window_must_be_ordered(cur):
    fx = Scalar(cur)
    with rejected(errors.CheckViolation):
        make_pending(cur, fx, effective_from="2026-02-01", effective_to="2026-01-31")


# ---------------------------------------------------------------------------
# Pending boundary
# ---------------------------------------------------------------------------

def test_one_pending_schedule_per_logical_identity(cur):
    fx = Scalar(cur, driver_count=2)
    make_pending(cur, fx, driver_id=fx.driver_ids[0])
    with rejected(errors.UniqueViolation):
        make_pending(cur, fx, driver_id=fx.driver_ids[0], effective_from="2027-01-01")
    make_pending(cur, fx, driver_id=fx.driver_ids[1])  # other Driver is a different identity


def test_discard_deletes_pending_with_values_and_audits_the_full_value_set(cur):
    fx = Ordinal(cur)
    pending = make_pending(cur, fx)
    for component_id, amount in zip(fx.component_ids, ["3", "4.5", "0"], strict=True):
        set_value(cur, pending, fx.rate_definition_id, component_id, amount)

    cur.execute("SELECT set_config('flussra.actor_user_id', %s, FALSE)", (str(user_id(cur)),))
    cur.execute("DELETE FROM payroll.driverrateschedules WHERE driverratescheduleid = %s",
                (pending,))
    cur.execute("SELECT count(*) FROM payroll.driverrateschedulevalues WHERE driverratescheduleid = %s",
                (pending,))
    assert cur.fetchone()[0] == 0

    cur.execute("""
        SELECT actoruserid, oldvaluejson, companyid, branchid FROM audit.auditlog
        WHERE actioncode = 'DRIVER_RATE_SCHEDULE_DISCARDED' AND entityid = %s
    """, (str(pending),))
    actor, payload, company_id, branch_id = cur.fetchone()
    assert (actor, company_id, branch_id) == (user_id(cur), fx.company_id, None)  # Driver-owned: no Branch
    discarded = json.loads(payload)
    assert discarded["DriverID"] == fx.driver_id and "DriverAssignmentID" not in discarded
    values = discarded["Values"]
    assert [float(item["Amount"]) for item in values] == [3.0, 4.5, 0.0]


def test_structure_is_editable_again_after_discarding_pending(cur):
    fx = Ordinal(cur)
    pending = make_pending(cur, fx)
    with rejected(errors.CheckViolation, "RATE_STRUCTURE_PENDING_SCHEDULE"):
        cur.execute("DELETE FROM payroll.ratecomponentdefinitions "
                    "WHERE ratecomponentdefinitionid = %s", (fx.component_ids[2],))
    cur.execute("DELETE FROM payroll.driverrateschedules WHERE driverratescheduleid = %s",
                (pending,))
    cur.execute("DELETE FROM payroll.ratecomponentdefinitions WHERE ratecomponentdefinitionid = %s",
                (fx.component_ids[2],))


# ---------------------------------------------------------------------------
# Structure mutation guard (Pending and the one canonical lock)
# ---------------------------------------------------------------------------

MUTATIONS = {
    "component_insert": lambda c, fx: c.execute("""
        INSERT INTO payroll.ratecomponentdefinitions
            (ratedefinitionid, shape, sequenceno, ordinalfrom, ordinalto)
        VALUES (%s, 'OrdinalTierSchedule', 4, 100, NULL)
    """, (fx.rate_definition_id,)),
    "component_update": lambda c, fx: c.execute("""
        UPDATE payroll.ratecomponentdefinitions SET ordinalto = 1
        WHERE ratecomponentdefinitionid = %s
    """, (fx.component_ids[0],)),
    "component_delete": lambda c, fx: c.execute("""
        DELETE FROM payroll.ratecomponentdefinitions WHERE ratecomponentdefinitionid = %s
    """, (fx.component_ids[2],)),
    "topology_change": lambda c, fx: c.execute("""
        UPDATE payroll.ratecomponentdefinitions SET ordinalfrom = 2, ordinalto = 2
        WHERE ratecomponentdefinitionid = %s
    """, (fx.component_ids[1],)),
    "shape_change": lambda c, fx: c.execute("""
        UPDATE payroll.ratedefinitions SET shape = 'Scalar' WHERE ratedefinitionid = %s
    """, (fx.rate_definition_id,)),
    "method_change": lambda c, fx: c.execute("""
        UPDATE payroll.paydefinitions SET calculationmethod = 'PerUnit'
        WHERE paydefinitionid = %s
    """, (fx.pay_definition_id,)),
    "input_type_change": lambda c, fx: c.execute("""
        UPDATE payroll.paydefinitions SET inputtype = 'Decimal'
        WHERE paydefinitionid = %s
    """, (fx.pay_definition_id,)),
    "rate_definition_delete": lambda c, fx: c.execute("""
        DELETE FROM payroll.ratedefinitions WHERE ratedefinitionid = %s
    """, (fx.rate_definition_id,)),
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_every_structural_mutation_fails_while_a_pending_schedule_exists(cur, mutation):
    fx = Ordinal(cur)
    make_pending(cur, fx)
    with rejected(errors.CheckViolation, "RATE_STRUCTURE_PENDING_SCHEDULE"):
        MUTATIONS[mutation](cur, fx)


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_every_structural_mutation_fails_once_the_structure_is_locked(cur, mutation):
    fx = Ordinal(cur)
    pending = make_pending(cur, fx)
    for component_id in fx.component_ids:
        set_value(cur, pending, fx.rate_definition_id, component_id, "1")
    approve(cur, pending)
    with rejected(errors.CheckViolation, "RATE_STRUCTURE_LOCKED"):
        MUTATIONS[mutation](cur, fx)


def test_structure_lock_is_monotonic_and_owner_is_immutable(cur):
    fx = Scalar(cur)
    approved_scalar(cur, fx)
    with rejected(errors.CheckViolation, "RATE_STRUCTURE_LOCK_IMMUTABLE"):
        cur.execute("UPDATE payroll.ratedefinitions SET structurelockedatutc = NULL "
                    "WHERE ratedefinitionid = %s", (fx.rate_definition_id,))
    with rejected(errors.CheckViolation, "RATE_STRUCTURE_LOCK_IMMUTABLE"):
        cur.execute("UPDATE payroll.ratedefinitions SET structurelockedatutc = NOW() + interval '1 day' "
                    "WHERE ratedefinitionid = %s", (fx.rate_definition_id,))
    other_pay_definition = make_pay_definition(cur, fx.company_id)
    with rejected(errors.CheckViolation, "RATE_DEFINITION_OWNER_IMMUTABLE"):
        cur.execute("UPDATE payroll.ratedefinitions SET paydefinitionid = %s "
                    "WHERE ratedefinitionid = %s", (other_pay_definition, fx.rate_definition_id))


def test_a_later_authoritative_event_can_set_the_lock_but_not_a_pending_one(cur):
    fx = Scalar(cur)
    cur.execute("UPDATE payroll.ratedefinitions SET structurelockedatutc = NOW() "
                "WHERE ratedefinitionid = %s", (fx.rate_definition_id,))
    with rejected(errors.CheckViolation, "RATE_STRUCTURE_LOCKED"):
        cur.execute("DELETE FROM payroll.ratecomponentdefinitions "
                    "WHERE ratecomponentdefinitionid = %s", (fx.component_id,))


def test_definition_without_rate_definition_changes_method_freely(cur):
    company_id = make_company(cur)
    pay_definition_id = make_pay_definition(cur, company_id, method="PerUnit")
    cur.execute("""
        UPDATE payroll.paydefinitions SET calculationmethod = 'OrdinalTier', inputtype = 'WholeNumber'
        WHERE paydefinitionid = %s
    """, (pay_definition_id,))


def test_method_and_shape_may_be_changed_together_while_unlocked_and_not_pending(dsn_conn):
    dsn, cur = dsn_conn
    fx = Scalar(cur)
    conn = connect(dsn)
    try:
        with conn.cursor() as txn:
            txn.execute("""
                UPDATE payroll.paydefinitions
                   SET calculationmethod = 'OrdinalTier', inputtype = 'WholeNumber'
                 WHERE paydefinitionid = %s
            """, (fx.pay_definition_id,))
            txn.execute("DELETE FROM payroll.ratecomponentdefinitions WHERE ratedefinitionid = %s",
                        (fx.rate_definition_id,))
            txn.execute("UPDATE payroll.ratedefinitions SET shape = 'OrdinalTierSchedule' "
                        "WHERE ratedefinitionid = %s", (fx.rate_definition_id,))
            add_tiers(txn, fx.rate_definition_id)
        conn.commit()
    finally:
        conn.close()


def test_method_without_matching_shape_cannot_commit(dsn_conn):
    dsn, cur = dsn_conn
    fx = Scalar(cur)
    conn = connect(dsn)
    try:
        with conn.cursor() as txn:
            txn.execute("""
                UPDATE payroll.paydefinitions
                   SET calculationmethod = 'OrdinalTier', inputtype = 'WholeNumber'
                 WHERE paydefinitionid = %s
            """, (fx.pay_definition_id,))
        with rejected(errors.CheckViolation, "RATE_SHAPE_METHOD_MISMATCH"):
            conn.commit()
    finally:
        conn.rollback()
        conn.close()
    cur.execute("SELECT calculationmethod FROM payroll.paydefinitions WHERE paydefinitionid = %s",
                (fx.pay_definition_id,))
    assert cur.fetchone()[0] == "PerUnit"


def test_rate_definition_shape_must_match_pay_definition_method_at_creation(dsn_conn):
    dsn, cur = dsn_conn
    company_id = make_company(cur)
    pay_definition_id = make_pay_definition(cur, company_id, method="PerUnit")
    conn = connect(dsn)
    try:
        with conn.cursor() as txn:
            txn.execute("""
                INSERT INTO payroll.ratedefinitions (companyid, paydefinitionid, shape)
                VALUES (%s, %s, 'OrdinalTierSchedule')
            """, (company_id, pay_definition_id))
        with rejected(errors.CheckViolation, "RATE_SHAPE_METHOD_MISMATCH"):
            conn.commit()
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def dsn_conn(p3b_dsn, cur):
    return p3b_dsn, cur


# ---------------------------------------------------------------------------
# Company currency authority
# ---------------------------------------------------------------------------

def _durable(cur, company_id: int) -> bool:
    cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (company_id,))
    return cur.fetchone()[0]


def test_unconfigured_company_cannot_approve_a_target_schedule(cur):
    fx = Scalar(cur, currency=None)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "1")
    with rejected(errors.CheckViolation, "COMPANY_CURRENCY_REQUIRED"):
        approve(cur, pending)
    assert status_of(cur, pending) == "Pending"
    assert structure_locked_at(cur, fx.rate_definition_id) is None

    cur.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s", (fx.company_id,))
    approve(cur, pending)
    assert status_of(cur, pending) == "Approved"


def test_pending_does_not_count_but_authoritative_schedules_do(cur):
    fx = Scalar(cur, driver_count=2)
    pending = make_pending(cur, fx)
    set_value(cur, pending, fx.rate_definition_id, fx.component_id, "1")
    assert _durable(cur, fx.company_id) is False
    cur.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s", (fx.company_id,))
    cur.execute("UPDATE core.companies SET currencycode = 'USD' WHERE companyid = %s", (fx.company_id,))

    approve(cur, pending)
    assert _durable(cur, fx.company_id) is True
    with rejected(errors.CheckViolation, "COMPANY_CURRENCY_CHANGE_BLOCKED"):
        cur.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s",
                    (fx.company_id,))


@pytest.mark.parametrize("final_state", ["Superseded", "Voided"])
def test_superseded_and_voided_schedules_remain_durable_monetary_state(cur, final_state):
    fx = Scalar(cur)
    schedule = approved_scalar(cur, fx)
    if final_state == "Superseded":
        supersede(cur, schedule, "2026-12-31")
    else:
        cur.execute("""
            UPDATE payroll.driverrateschedules
               SET status = 'Voided', voidedbyuserid = %s, voidedatutc = NOW()
             WHERE driverratescheduleid = %s
        """, (user_id(cur), schedule))
    assert _durable(cur, fx.company_id) is True
    with rejected(errors.CheckViolation, "COMPANY_CURRENCY_CHANGE_BLOCKED"):
        cur.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s",
                    (fx.company_id,))


def test_legacy_monetary_authority_is_unchanged_by_the_target_schema(cur):
    fx = Scalar(cur)
    assert _durable(cur, fx.company_id) is False
    cur.execute("SELECT ratetypeid FROM payroll.ratetypes ORDER BY ratetypeid LIMIT 1")
    rate_type_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO payroll.driverrates
            (companyid, branchid, driverassignmentid, ratetypeid, amount, effectivefrom, status)
        VALUES (%s, %s, %s, %s, 10, '2099-01-01', 'PendingApproval')
    """, (fx.company_id, fx.branch_id, fx.driver_assignment_id, rate_type_id))
    assert _durable(cur, fx.company_id) is True
    assert _durable(cur, make_company(cur)) is False
