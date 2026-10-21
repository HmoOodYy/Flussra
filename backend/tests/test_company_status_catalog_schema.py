"""Database guarantees of the Company Status catalog (migration 0090).

Each test owns a Company in the disposable target database, so nothing is shared and nothing
needs cleanup. The application checks first for clear messages; these tests prove the database
refuses the same writes when the application is bypassed or loses a race.
"""
from __future__ import annotations

from uuid import uuid4

import psycopg2
import pytest

from tests.builders.status import (
    add_alias,
    default_status_column,
    make_status_column,
    make_status_key,
)
from tests.compensation_database_fixtures import (
    make_branch,
    make_company,
    make_driver_pair,
    p3b_cursor,  # noqa: F401 - registers the cur fixture
    p3b_database,  # noqa: F401 - registers the p3b_dsn fixture
    user_id,
)
from tests.db_state import replica_replication_role_sync


@pytest.fixture
def company(cur) -> int:
    return make_company(cur)


def _refused(cur, statement: str, params=(), *, contains: str) -> None:
    with pytest.raises(psycopg2.Error) as raised:
        cur.execute(statement, params)
    assert contains.lower() in str(raised.value).lower(), str(raised.value)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def test_active_primary_names_are_unique_per_company_by_normalized_form(cur, company):
    make_status_key(cur, company, name="Vacation")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        make_status_key(cur, company, name="  VACATION ")


def test_the_same_name_is_allowed_in_another_company(cur, company):
    make_status_key(cur, company, name="Vacation")
    make_status_key(cur, make_company(cur), name="Vacation")


def test_an_inactive_status_does_not_block_its_name(cur, company):
    make_status_key(cur, company, name="Vacation", active=False)
    make_status_key(cur, company, name="Vacation")
    make_status_key(cur, company, name="Vacation", active=False)


def test_reactivating_a_status_rechecks_its_name(cur, company):
    old = make_status_key(cur, company, name="Vacation", active=False)
    make_status_key(cur, company, name="Vacation")
    _refused(cur, "UPDATE payroll.payrollstatuskeys SET isactive = TRUE WHERE statuskeyid = %s",
             (old,), contains="STATUS_NAME_NOT_UNIQUE")


def test_renaming_to_an_existing_active_name_is_refused(cur, company):
    make_status_key(cur, company, name="Vacation")
    other = make_status_key(cur, company, name="Sick")
    _refused(cur, """
        UPDATE payroll.payrollstatuskeys SET keyname = 'Vacation', normalizedkeyname = 'vacation'
        WHERE statuskeyid = %s""", (other,), contains="STATUS_NAME_NOT_UNIQUE")


@pytest.mark.parametrize("name", ["", "   "])
def test_a_blank_name_is_refused(cur, company, name):
    with pytest.raises(psycopg2.errors.CheckViolation):
        make_status_key(cur, company, name=name)


def test_the_normalized_name_must_be_in_normal_form(cur, company):
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, keyname, normalizedkeyname, statuscode, normalizedstatuscode,
                 allowancecategory)
            VALUES (%s, 'A  B', 'a  b', 'SK_X', 'SK_X', 'Other')""", (company,))


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------

def test_an_alias_cannot_equal_the_name_of_another_active_status(cur, company):
    make_status_key(cur, company, name="Sick")
    vacation = make_status_key(cur, company, name="Vacation")
    _refused(cur, """
        INSERT INTO payroll.payrollstatuskeyaliases
            (companyid, statuskeyid, isactive, aliastext, normalizedalias)
        VALUES (%s, %s, TRUE, 'SICK', 'sick')""", (company, vacation),
             contains="STATUS_ALIAS_AMBIGUOUS")


def test_an_alias_cannot_equal_its_own_status_name(cur, company):
    vacation = make_status_key(cur, company, name="Vacation")
    _refused(cur, """
        INSERT INTO payroll.payrollstatuskeyaliases
            (companyid, statuskeyid, isactive, aliastext, normalizedalias)
        VALUES (%s, %s, TRUE, 'vacation', 'vacation')""", (company, vacation),
             contains="STATUS_ALIAS_AMBIGUOUS")


def test_an_alias_cannot_equal_the_alias_of_another_active_status(cur, company):
    make_status_key(cur, company, name="Vacation", aliases=("Vac",))
    sick = make_status_key(cur, company, name="Sick")
    _refused(cur, """
        INSERT INTO payroll.payrollstatuskeyaliases
            (companyid, statuskeyid, isactive, aliastext, normalizedalias)
        VALUES (%s, %s, TRUE, 'VAC', 'vac')""", (company, sick),
             contains="STATUS_ALIAS_AMBIGUOUS")


def test_a_status_cannot_have_the_same_alias_twice(cur, company):
    vacation = make_status_key(cur, company, name="Vacation", aliases=("Vac",))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        add_alias(cur, vacation, " VAC ")


def test_an_alias_may_repeat_in_another_company(cur, company):
    make_status_key(cur, company, name="Vacation", aliases=("Vac",))
    make_status_key(cur, make_company(cur), name="Vacation", aliases=("Vac",))


def test_a_name_cannot_equal_an_active_alias_of_another_status(cur, company):
    make_status_key(cur, company, name="Vacation", aliases=("Holiday",))
    with pytest.raises(psycopg2.errors.UniqueViolation) as raised:
        make_status_key(cur, company, name="HOLIDAY")
    assert "STATUS_ALIAS_AMBIGUOUS" in str(raised.value)


def test_renaming_a_status_to_one_of_its_own_aliases_is_refused(cur, company):
    vacation = make_status_key(cur, company, name="Vacation", aliases=("Vac",))
    _refused(cur, """
        UPDATE payroll.payrollstatuskeys SET keyname = 'Vac', normalizedkeyname = 'vac'
        WHERE statuskeyid = %s""", (vacation,), contains="STATUS_ALIAS_AMBIGUOUS")


def test_aliases_of_an_inactive_status_do_not_block(cur, company):
    old = make_status_key(cur, company, name="Vacation", active=False, aliases=("Vac",))
    # Another Status may take the inactive Status's alias and name.
    other = make_status_key(cur, company, name="Vacation", aliases=("Vac",))
    assert other != old


def test_the_alias_of_an_inactive_status_may_equal_an_active_name(cur, company):
    make_status_key(cur, company, name="Sick")
    make_status_key(cur, company, name="Vacation", active=False, aliases=("Sick",))


def test_reactivating_a_status_rechecks_its_aliases(cur, company):
    old = make_status_key(cur, company, name="Vacation", active=False, aliases=("Vac",))
    make_status_key(cur, company, name="Sick", aliases=("Vac",))
    _refused(cur, "UPDATE payroll.payrollstatuskeys SET isactive = TRUE WHERE statuskeyid = %s",
             (old,), contains="STATUS_ALIAS_AMBIGUOUS")


def test_reactivating_a_status_rechecks_its_alias_against_active_names(cur, company):
    old = make_status_key(cur, company, name="Vacation", active=False, aliases=("Sick",))
    make_status_key(cur, company, name="Sick")
    _refused(cur, "UPDATE payroll.payrollstatuskeys SET isactive = TRUE WHERE statuskeyid = %s",
             (old,), contains="STATUS_ALIAS_AMBIGUOUS")


def test_aliases_follow_the_activity_of_their_status(cur, company):
    vacation = make_status_key(cur, company, name="Vacation", aliases=("Vac", "Hol"))
    cur.execute("UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = %s",
                (vacation,))
    cur.execute("SELECT isactive FROM payroll.payrollstatuskeyaliases WHERE statuskeyid = %s",
                (vacation,))
    assert {row[0] for row in cur.fetchall()} == {False}
    cur.execute("UPDATE payroll.payrollstatuskeys SET isactive = TRUE WHERE statuskeyid = %s",
                (vacation,))
    cur.execute("SELECT isactive FROM payroll.payrollstatuskeyaliases WHERE statuskeyid = %s",
                (vacation,))
    assert {row[0] for row in cur.fetchall()} == {True}


def test_an_alias_cannot_name_a_status_of_another_company(cur, company):
    other = make_status_key(cur, make_company(cur), name="Vacation")
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute("""
            INSERT INTO payroll.payrollstatuskeyaliases
                (companyid, statuskeyid, isactive, aliastext, normalizedalias)
            VALUES (%s, %s, TRUE, 'x', 'x')""", (company, other))


# ---------------------------------------------------------------------------
# Category, code, binding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category", [
    "Vacation", "Sick", "Bereavement", "Jury Duty", "Personal", "Holiday", "Unpaid", "Other"])
def test_each_fixed_category_is_accepted(cur, company, category):
    make_status_key(cur, company, category=category)


@pytest.mark.parametrize("category", ["Weekend", "vacation", ""])
def test_a_category_outside_the_fixed_set_is_refused(cur, company, category):
    with pytest.raises(psycopg2.errors.CheckViolation):
        make_status_key(cur, company, category=category)


def test_a_category_is_required(cur, company):
    with pytest.raises(psycopg2.errors.NotNullViolation):
        cur.execute("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, keyname, normalizedkeyname, statuscode, normalizedstatuscode)
            VALUES (%s, 'A', 'a', 'SK_A', 'SK_A')""", (company,))


def test_the_generated_code_is_unique_per_company(cur, company):
    first = make_status_key(cur, company)
    cur.execute("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = %s", (first,))
    code = cur.fetchone()[0]
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, keyname, normalizedkeyname, statuscode, normalizedstatuscode,
                 allowancecategory)
            VALUES (%s, 'Other name', 'other name', %s, %s, 'Other')""", (company, code, code))
    # The same code is fine in another Company.
    other = make_company(cur)
    cur.execute("""
        INSERT INTO payroll.payrollstatuskeys
            (companyid, keyname, normalizedkeyname, statuscode, normalizedstatuscode,
             allowancecategory)
        VALUES (%s, 'Other name', 'other name', %s, %s, 'Other')""", (other, code, code))


def test_the_identity_of_a_status_never_changes(cur, company):
    key = make_status_key(cur, company)
    _refused(cur, "UPDATE payroll.payrollstatuskeys SET statuscode = 'SK_NEW', "
                  "normalizedstatuscode = 'SK_NEW' WHERE statuskeyid = %s", (key,),
             contains="STATUS_KEY_IDENTITY_IMMUTABLE")
    _refused(cur, "UPDATE payroll.payrollstatuskeys SET companyid = %s WHERE statuskeyid = %s",
             (make_company(cur), key), contains="STATUS_KEY_IDENTITY_IMMUTABLE")


def test_a_monetary_status_needs_hours(cur, company):
    column = default_status_column(cur, company)
    with pytest.raises(psycopg2.errors.CheckViolation):
        make_status_key(cur, company, hours="0", column_id=column.status_rate_column_id)
    # Without a rate column zero hours is fine (a non-monetary Status).
    make_status_key(cur, company, hours="0")


def test_a_status_binds_only_a_rate_column_of_its_own_company(cur, company):
    foreign = default_status_column(cur, make_company(cur))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        make_status_key(cur, company, column_id=foreign.status_rate_column_id)


# ---------------------------------------------------------------------------
# The semantic lock of a used Status
# ---------------------------------------------------------------------------

def _use(cur, company_id: int, key_id: int, *, voided: bool = False) -> None:
    """A day-entry selection of the Status in a period of the Company."""
    branch_id = make_branch(cur, company_id)
    _, assignment_id = make_driver_pair(cur, company_id, branch_id)
    cur.execute("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (%s, %s, 'Open', %s, 'lock period', 'Week', DATE '2040-01-01', DATE '2040-01-07')
        RETURNING payrollperiodid
    """, (company_id, branch_id, "L" + uuid4().hex[:10]))
    period_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO payroll.payrollperioddriverdayentrystate
            (companyid, branchid, payrollperiodid, workdate, driverassignmentid,
             statuskeyid, isvoided, createdbyuserid)
        VALUES (%s, %s, %s, DATE '2040-01-02', %s, %s, %s, %s)
    """, (company_id, branch_id, period_id, assignment_id, key_id, voided, user_id(cur)))


LOCKED_CHANGES = {
    "hours": "hoursvalue = 4",
    "off_day_semantic": "isoffreason = NOT isoffreason",
    "category": "allowancecategory = 'Sick'",
}


@pytest.mark.parametrize("change", sorted(LOCKED_CHANGES))
@pytest.mark.parametrize("voided", [False, True])
def test_a_used_status_cannot_change_its_semantics(cur, company, change, voided):
    key = make_status_key(cur, company, category="Vacation", hours="8")
    _use(cur, company, key, voided=voided)
    _refused(cur, f"UPDATE payroll.payrollstatuskeys SET {LOCKED_CHANGES[change]} "
                  "WHERE statuskeyid = %s", (key,), contains="STATUS_KEY_IN_USE")


def test_a_used_status_cannot_change_its_rate_column(cur, company):
    column = default_status_column(cur, company)
    other = make_status_column(cur, company)
    key = make_status_key(cur, company, column_id=column.status_rate_column_id)
    _use(cur, company, key)
    _refused(cur, "UPDATE payroll.payrollstatuskeys SET statusratecolumnid = %s "
                  "WHERE statuskeyid = %s", (other.status_rate_column_id, key),
             contains="STATUS_KEY_IN_USE")
    _refused(cur, "UPDATE payroll.payrollstatuskeys SET statusratecolumnid = NULL "
                  "WHERE statuskeyid = %s", (key,), contains="STATUS_KEY_IN_USE")


def test_a_used_status_keeps_its_name_order_activity_and_aliases_editable(cur, company):
    key = make_status_key(cur, company, name="Vacation", aliases=("Vac",))
    _use(cur, company, key)
    cur.execute("""
        UPDATE payroll.payrollstatuskeys
        SET keyname = 'Annual leave', normalizedkeyname = 'annual leave', displayorder = 5
        WHERE statuskeyid = %s""", (key,))
    add_alias(cur, key, "Leave")
    cur.execute("DELETE FROM payroll.payrollstatuskeyaliases WHERE statuskeyid = %s AND "
                "normalizedalias = 'vac'", (key,))
    cur.execute("UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = %s", (key,))
    cur.execute("UPDATE payroll.payrollstatuskeys SET isactive = TRUE WHERE statuskeyid = %s", (key,))
    # Writing the same semantic values is not a change.
    cur.execute("UPDATE payroll.payrollstatuskeys SET hoursvalue = hoursvalue, "
                "allowancecategory = allowancecategory WHERE statuskeyid = %s", (key,))


def test_an_unused_status_can_still_change_its_semantics(cur, company):
    key = make_status_key(cur, company, category="Vacation", hours="8")
    # Another Status of the Company being used does not lock this one.
    _use(cur, company, make_status_key(cur, company))
    cur.execute("UPDATE payroll.payrollstatuskeys SET hoursvalue = 4, isoffreason = FALSE, "
                "allowancecategory = 'Sick' WHERE statuskeyid = %s", (key,))


def test_frozen_status_evidence_also_locks_the_semantics(cur, company):
    key = make_status_key(cur, company)
    with replica_replication_role_sync(cur):
        cur.execute("""
            INSERT INTO payroll.payrollcalculationsnapshotstatusentries
                (payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
                 driverassignmentid, workdate, payrollperioddriverdayentrystateid, statuskeyid,
                 statuscodesnapshot, statuslabelsnapshot, statusisoffreasonsnapshot, driverid)
            VALUES (1, %s, 1, 1, 1, DATE '2040-01-02', 1, %s, 'SK_X', 'X', TRUE, 1)
        """, (company, key))
    _refused(cur, "UPDATE payroll.payrollstatuskeys SET hoursvalue = 3 WHERE statuskeyid = %s",
             (key,), contains="STATUS_KEY_IN_USE")


def test_selections_reference_a_status_of_their_own_company(cur, company):
    foreign = make_status_key(cur, make_company(cur))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _use(cur, company, foreign)


# ---------------------------------------------------------------------------
# Branch usage limits
# ---------------------------------------------------------------------------

def _limits(cur, company_id, branch_id, key_id, **columns) -> None:
    columns = {"limitusesperperiodenabled": False, "limitusesperperiod": None,
               "limitusesperdriverenabled": False, "limitusesperdriver": None,
               "limitusesacrossdriversenabled": False, "limitusesacrossdrivers": None,
               "limitusesperdayenabled": False, "limitusesperday": None} | columns
    names = ", ".join(columns)
    cur.execute(
        f"INSERT INTO payroll.payrollstatuskeybranchlimits "
        f"(branchid, statuskeyid, companyid, {names}) "
        f"VALUES (%s, %s, %s, {', '.join(['%s'] * len(columns))})",
        (branch_id, key_id, company_id, *columns.values()))


@pytest.mark.parametrize("pair", ["perperiod", "perdriver", "acrossdrivers", "perday"])
def test_each_limit_pair_stores_a_positive_value(cur, company, pair):
    branch = make_branch(cur, company)
    key = make_status_key(cur, company)
    _limits(cur, company, branch, key, **{f"limituses{pair}enabled": True, f"limituses{pair}": 3})
    for bad in (0, -1, None):
        other = make_status_key(cur, company)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _limits(cur, company, branch, other,
                    **{f"limituses{pair}enabled": True, f"limituses{pair}": bad})


def test_a_limits_row_has_at_least_one_limit_enabled(cur, company):
    branch = make_branch(cur, company)
    key = make_status_key(cur, company)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _limits(cur, company, branch, key)


def test_limits_are_one_row_per_branch_and_status(cur, company):
    branch = make_branch(cur, company)
    key = make_status_key(cur, company)
    _limits(cur, company, branch, key, limitusesperdayenabled=True, limitusesperday=1)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _limits(cur, company, branch, key, limitusesperdayenabled=True, limitusesperday=2)
    # Another Branch of the same Company has its own row for the same Status.
    _limits(cur, company, make_branch(cur, company), key,
            limitusesperdayenabled=True, limitusesperday=5)


def test_limits_name_a_branch_and_a_status_of_their_own_company(cur, company):
    foreign_branch = make_branch(cur, make_company(cur))
    foreign_key = make_status_key(cur, make_company(cur))
    key = make_status_key(cur, company)
    branch = make_branch(cur, company)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _limits(cur, company, foreign_branch, key, limitusesperdayenabled=True, limitusesperday=1)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _limits(cur, company, branch, foreign_key, limitusesperdayenabled=True, limitusesperday=1)


# ---------------------------------------------------------------------------
# Company-owned Status rate columns and their rate definitions
# ---------------------------------------------------------------------------

def test_a_company_has_one_default_status_rate_column(cur, company):
    first = default_status_column(cur, company)
    assert default_status_column(cur, company) == first
    cur.execute("""
        SELECT count(*) FROM payroll.statusratecolumns
        WHERE companyid = %s AND isdefault AND isactive""", (company,))
    assert cur.fetchone()[0] == 1
    with pytest.raises(psycopg2.errors.UniqueViolation):
        make_status_column(cur, company, is_default=True)


def test_a_status_rate_column_owns_one_locked_scalar_rate_definition(cur, company):
    column = make_status_column(cur, company)
    cur.execute("""
        SELECT rd.companyid, rd.paydefinitionid, rd.shape, rd.structurelockedatutc IS NOT NULL,
               (SELECT count(*) FROM payroll.ratecomponentdefinitions c
                 WHERE c.ratedefinitionid = rd.ratedefinitionid)
        FROM payroll.ratedefinitions rd WHERE rd.statusratecolumnid = %s
    """, (column.status_rate_column_id,))
    assert cur.fetchall() == [(company, None, "Scalar", True, 1)]


def test_active_column_names_are_unique_per_company(cur, company):
    make_status_column(cur, company, name="Premium")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        make_status_column(cur, company, name="PREMIUM")
    make_status_column(cur, make_company(cur), name="Premium")
    cur.execute("""
        UPDATE payroll.statusratecolumns SET isactive = FALSE
        WHERE companyid = %s AND normalizedcolumnname = 'premium'""", (company,))
    make_status_column(cur, company, name="Premium")


def test_a_default_column_must_be_active(cur, company):
    column = default_status_column(cur, company)
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE payroll.statusratecolumns SET isactive = FALSE "
                    "WHERE statusratecolumnid = %s", (column.status_rate_column_id,))


def test_a_status_rate_definition_belongs_to_the_company_of_its_column(cur, company):
    cur.execute("""
        INSERT INTO payroll.statusratecolumns (companyid, columnname, normalizedcolumnname)
        VALUES (%s, 'Bare column', 'bare column') RETURNING statusratecolumnid""", (company,))
    column_id = cur.fetchone()[0]
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute("""
            INSERT INTO payroll.ratedefinitions (companyid, statusratecolumnid, shape)
            VALUES (%s, %s, 'Scalar')""", (make_company(cur), column_id))


def test_a_status_rate_definition_owner_never_changes(cur, company):
    column = make_status_column(cur, company)
    other = make_status_column(cur, company)
    _refused(cur, "UPDATE payroll.ratedefinitions SET statusratecolumnid = %s "
                  "WHERE ratedefinitionid = %s",
             (other.status_rate_column_id, column.rate_definition_id),
             contains="RATE_DEFINITION_OWNER_IMMUTABLE")


# ---------------------------------------------------------------------------
# Driver rate schedules owned by a Status rate column
# ---------------------------------------------------------------------------

class _World:
    def __init__(self, cur):
        self.company = make_company(cur)
        self.branch = make_branch(cur, self.company)
        self.driver, _ = make_driver_pair(cur, self.company, self.branch)
        self.column = default_status_column(cur, self.company)


@pytest.fixture
def world(cur) -> _World:
    return _World(cur)


def _schedule(cur, world: _World, *, start="2026-01-01", end=None, column=None,
              driver=None, pay_definition_id=None, status_column_id="same") -> int:
    column = column or world.column
    cur.execute("""
        INSERT INTO payroll.driverrateschedules
            (companyid, driverid, paydefinitionid, statusratecolumnid, ratedefinitionid,
             effectivefrom, effectiveto)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING driverratescheduleid
    """, (world.company, driver or world.driver, pay_definition_id,
          column.status_rate_column_id if status_column_id == "same" else status_column_id,
          column.rate_definition_id, start, end))
    return cur.fetchone()[0]


def _value(cur, schedule_id, column, amount) -> None:
    cur.execute("""
        INSERT INTO payroll.driverrateschedulevalues
            (driverratescheduleid, ratedefinitionid, ratecomponentdefinitionid, amount)
        VALUES (%s, %s, %s, %s)""", (schedule_id, column.rate_definition_id, column.component_id, amount))


def _approve(cur, schedule_id: int) -> None:
    cur.execute("""
        UPDATE payroll.driverrateschedules
           SET status = 'Approved', approvedbyuserid = %s, approvedatutc = NOW()
         WHERE driverratescheduleid = %s""", (user_id(cur), schedule_id))


def test_a_driver_can_hold_an_approved_schedule_of_a_status_rate_definition(cur, world):
    schedule = _schedule(cur, world)
    _value(cur, schedule, world.column, "25")
    _approve(cur, schedule)
    cur.execute("SELECT status, paydefinitionid, statusratecolumnid FROM payroll.driverrateschedules "
                "WHERE driverratescheduleid = %s", (schedule,))
    assert cur.fetchone() == ("Approved", None, world.column.status_rate_column_id)


def test_a_schedule_has_exactly_one_owner(cur, world):
    cur.execute("""
        INSERT INTO payroll.paydefinitions
            (companyid, definitioncode, definitionname, inputtype, calculationmethod)
        VALUES (%s, %s, 'x', 'Decimal', 'PerUnit') RETURNING paydefinitionid
    """, (world.company, "PD" + uuid4().hex[:8]))
    pay_definition_id = cur.fetchone()[0]
    with pytest.raises(psycopg2.errors.CheckViolation):
        _schedule(cur, world, status_column_id=None)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _schedule(cur, world, pay_definition_id=pay_definition_id)


def test_a_schedule_owner_must_match_its_rate_definition(cur, world):
    other = make_status_column(cur, world.company)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _schedule(cur, world, status_column_id=other.status_rate_column_id)


def test_a_status_schedule_must_be_complete_to_be_approved(cur, world):
    schedule = _schedule(cur, world)
    _refused(cur, "UPDATE payroll.driverrateschedules SET status = 'Approved', "
                  "approvedbyuserid = %s, approvedatutc = NOW() WHERE driverratescheduleid = %s",
             (user_id(cur), schedule), contains="RATE_SCHEDULE_INCOMPLETE")
    _value(cur, schedule, world.column, "0")  # an explicit zero is a complete value
    _approve(cur, schedule)


def test_authoritative_status_schedules_of_a_driver_cannot_overlap(cur, world):
    first = _schedule(cur, world, start="2026-01-01")
    _value(cur, first, world.column, "25")
    _approve(cur, first)
    second = _schedule(cur, world, start="2026-02-01")
    _value(cur, second, world.column, "30")
    with pytest.raises(psycopg2.Error):
        _approve(cur, second)


def test_a_driver_has_at_most_one_pending_schedule_per_status_rate_definition(cur, world):
    _schedule(cur, world, start="2026-01-01")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _schedule(cur, world, start="2026-03-01")
    other_driver, _ = make_driver_pair(cur, world.company, world.branch)
    _schedule(cur, world, start="2026-03-01", driver=other_driver)


def test_a_schedule_owner_never_changes(cur, world):
    other = make_status_column(cur, world.company)
    schedule = _schedule(cur, world)
    _refused(cur, "UPDATE payroll.driverrateschedules SET statusratecolumnid = %s, "
                  "ratedefinitionid = %s WHERE driverratescheduleid = %s",
             (other.status_rate_column_id, other.rate_definition_id, schedule),
             contains="RATE_SCHEDULE_IDENTITY_IMMUTABLE")


def test_a_pending_status_schedule_can_be_discarded_with_its_audit(cur, world):
    schedule = _schedule(cur, world)
    _value(cur, schedule, world.column, "25")
    cur.execute("DELETE FROM payroll.driverrateschedules WHERE driverratescheduleid = %s", (schedule,))
    cur.execute("""
        SELECT oldvaluejson::jsonb ->> 'StatusRateColumnID'
        FROM audit.auditlog
        WHERE actioncode = 'DRIVER_RATE_SCHEDULE_DISCARDED' AND entityid = %s""", (str(schedule),))
    assert cur.fetchone() == (str(world.column.status_rate_column_id),)


# ---------------------------------------------------------------------------
# Concurrent writers
# ---------------------------------------------------------------------------

def test_a_name_and_an_alias_that_would_collide_cannot_both_commit(p3b_dsn, cur, company):
    """Two transactions that together would make a name an alias of another Status serialize on
    the Company's catalog lock: the second one sees the first one's committed row and fails."""
    import threading
    import time

    other = make_status_key(cur, company, name="Anchor")
    first = psycopg2.connect(client_encoding="utf-8", **p3b_dsn)
    second = psycopg2.connect(client_encoding="utf-8", **p3b_dsn)
    outcome: dict = {}
    try:
        with first.cursor() as first_cur:
            first_cur.execute("""
                INSERT INTO payroll.payrollstatuskeys
                    (companyid, keyname, normalizedkeyname, statuscode, normalizedstatuscode,
                     allowancecategory)
                VALUES (%s, 'Shared word', 'shared word', 'SK_RACE1', 'SK_RACE1', 'Other')
            """, (company,))

        def second_writer() -> None:
            try:
                with second.cursor() as second_cur:
                    second_cur.execute("""
                        INSERT INTO payroll.payrollstatuskeyaliases
                            (companyid, statuskeyid, isactive, aliastext, normalizedalias)
                        VALUES (%s, %s, TRUE, 'Shared word', 'shared word')
                    """, (company, other))
                second.commit()
                outcome["result"] = "committed"
            except psycopg2.Error as exc:
                second.rollback()
                outcome["result"] = str(exc)

        writer = threading.Thread(target=second_writer)
        writer.start()
        time.sleep(0.5)
        assert writer.is_alive(), "the second writer must wait for the catalog lock"
        first.commit()
        writer.join(timeout=10)
        assert not writer.is_alive()
    finally:
        first.close()
        second.close()
    assert "STATUS_ALIAS_AMBIGUOUS" in outcome["result"]
