"""Current PeriodDefinition and draft-source integrity constraints."""

from __future__ import annotations

from datetime import date

import psycopg2
import pytest
from psycopg2 import errors

from tests.compensation_database_fixtures import (
    make_branch,
    make_company,
    make_driver_pair,
    make_pay_definition,
    make_rate_definition,
)


def _period(
    cursor,
    company: int,
    branch: int,
    suffix: str,
    *,
    status: str = "Open",
    start: str = "2030-01-07",
    end: str = "2030-01-13",
) -> int:
    cursor.execute(
        """
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (%s, %s, %s, %s, %s, 'Week', %s, %s)
        RETURNING payrollperiodid
    """,
        (company, branch, status, suffix, suffix, start, end),
    )
    return cursor.fetchone()[0]


def _config(cursor, company: int, branch: int, definition: int) -> int:
    cursor.execute(
        """
        INSERT INTO payroll.branchpaydefinitionconfig
            (companyid, branchid, paydefinitionid, isactive, effectivefrom)
        VALUES (%s, %s, %s, TRUE, DATE '2020-01-01') RETURNING configid
    """,
        (company, branch, definition),
    )
    return cursor.fetchone()[0]


def _period_definition(
    cursor,
    company: int,
    branch: int,
    period: int,
    definition: int,
    rate: int,
    config: int,
    *,
    rate_shape: str = "Scalar",
) -> int:
    cursor.execute(
        """
        INSERT INTO payroll.payrollperioddefinitions
            (payrollperiodid, companyid, branchid, paydefinitionid, ratedefinitionid,
             definitioncodesnapshot, definitionnamesnapshot, inputtypesnapshot, unitsnapshot,
             calculationmethodsnapshot, calculationmethodversionsnapshot, rateshapesnapshot,
             definitionstatusatsnapshot, isactiveinperiod, sortorder, sourcebranchconfigid,
             sourcebranchconfigeffectivefrom)
        VALUES (%s, %s, %s, %s, %s, 'MEASURE', 'Measured work', 'Decimal', 'unit',
                'PerUnit', 1, %s, 'Active', TRUE, 0, %s, DATE '2020-01-01')
        RETURNING payrollperioddefinitionid
    """,
        (period, company, branch, definition, rate, rate_shape, config),
    )
    return cursor.fetchone()[0]


def _line(
    cursor,
    company: int,
    branch: int,
    period: int,
    assignment: int,
    period_definition: int,
    *,
    workdate="2030-01-08",
) -> int:
    cursor.execute(
        """
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverassignmentid, workdate,
             payrollperioddefinitionid, quantity, sourcetype, status)
        VALUES (%s, %s, %s, %s, %s, %s, 5, 'Manual', 'Active')
        RETURNING draftlineid
    """,
        (company, branch, period, assignment, workdate, period_definition),
    )
    return cursor.fetchone()[0]


def _world(cursor):
    company = make_company(cursor)
    branch = make_branch(cursor, company, is_default=True)
    driver, assignment = make_driver_pair(cursor, company, branch)
    definition = make_pay_definition(cursor, company)
    rate = make_rate_definition(cursor, definition)
    config = _config(cursor, company, branch, definition)
    cursor.execute(
        """
        INSERT INTO sec.users (companyid, username, displayname)
        VALUES (%s, %s, 'schema test') RETURNING userid
    """,
        (company, "schema_" + str(company)),
    )
    return company, branch, driver, assignment, definition, rate, config


def _rejected(cursor, error, action) -> None:
    cursor.execute("SAVEPOINT expected_rejection")
    with pytest.raises(error):
        action()
    cursor.execute("ROLLBACK TO SAVEPOINT expected_rejection")
    cursor.execute("RELEASE SAVEPOINT expected_rejection")


def test_period_definition_scope_ownership_and_snapshot_constraints(schema_databases):
    with schema_databases.database("period_definition", authority="sql") as dsn:
        with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
            company, branch, _, _, definition, rate, config = _world(cursor)
            period = _period(cursor, company, branch, "PDS-A")
            shape_test_period = _period(
                cursor,
                company,
                branch,
                "PDS-SHAPE",
                status="Draft",
                start="2030-01-14",
                end="2030-01-20",
            )
            other_branch = make_branch(cursor, company)
            same_branch_other_definition = make_pay_definition(cursor, company)
            same_branch_other_rate = make_rate_definition(cursor, same_branch_other_definition)
            same_branch_other_config = _config(
                cursor, company, branch, same_branch_other_definition
            )
            other_branch_config = _config(
                cursor, company, other_branch, same_branch_other_definition
            )
            other_company = make_company(cursor)
            foreign_branch = make_branch(cursor, other_company, is_default=True)
            foreign_definition = make_pay_definition(cursor, other_company)
            foreign_rate = make_rate_definition(cursor, foreign_definition)
            foreign_config = _config(cursor, other_company, foreign_branch, foreign_definition)
            ppd = _period_definition(cursor, company, branch, period, definition, rate, config)
            conn.commit()

            # One row per PayDefinition per period.
            _rejected(
                cursor,
                errors.UniqueViolation,
                lambda: _period_definition(
                    cursor, company, branch, period, definition, rate, config
                ),
            )
            # A PayDefinition from another Company cannot own this period's row.
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: _period_definition(
                    cursor,
                    company,
                    branch,
                    period,
                    foreign_definition,
                    foreign_rate,
                    foreign_config,
                ),
            )
            # CompanyID must match the Company that owns PayrollPeriodID.
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: _period_definition(
                    cursor,
                    other_company,
                    foreign_branch,
                    period,
                    foreign_definition,
                    foreign_rate,
                    foreign_config,
                ),
            )
            # BranchID must match the Branch that owns PayrollPeriodID.
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: _period_definition(
                    cursor,
                    company,
                    other_branch,
                    period,
                    same_branch_other_definition,
                    same_branch_other_rate,
                    other_branch_config,
                ),
            )
            # Source configuration must belong to this Branch and PayDefinition.
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: _period_definition(
                    cursor,
                    company,
                    branch,
                    period,
                    same_branch_other_definition,
                    same_branch_other_rate,
                    other_branch_config,
                ),
            )
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: _period_definition(
                    cursor,
                    company,
                    branch,
                    period,
                    same_branch_other_definition,
                    same_branch_other_rate,
                    config,
                ),
            )
            # The RateDefinition must belong to the same PayDefinition.
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: _period_definition(
                    cursor,
                    company,
                    branch,
                    period,
                    same_branch_other_definition,
                    rate,
                    same_branch_other_config,
                ),
            )
            # PerUnit requires the frozen scalar rate shape.
            _rejected(
                cursor,
                errors.CheckViolation,
                lambda: _period_definition(
                    cursor,
                    company,
                    branch,
                    shape_test_period,
                    same_branch_other_definition,
                    same_branch_other_rate,
                    same_branch_other_config,
                    rate_shape="OrdinalTierSchedule",
                ),
            )
            # A frozen snapshot cannot be changed after creation.
            _rejected(
                cursor,
                errors.CheckViolation,
                lambda: cursor.execute(
                    """
                UPDATE payroll.payrollperioddefinitions SET isactiveinperiod = FALSE
                WHERE payrollperioddefinitionid = %s
            """,
                    (ppd,),
                ),
            )

            cursor.execute(
                """
                SELECT companyid, branchid, payrollperiodid, paydefinitionid,
                       ratedefinitionid, sourcebranchconfigid, rateshapesnapshot,
                       isactiveinperiod
                FROM payroll.payrollperioddefinitions
                WHERE payrollperioddefinitionid = %s
            """,
                (ppd,),
            )
            assert cursor.fetchone() == (
                company,
                branch,
                period,
                definition,
                rate,
                config,
                "Scalar",
                True,
            )
            # A frozen period layout keeps the source configuration version alive.
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: cursor.execute(
                    "DELETE FROM payroll.branchpaydefinitionconfig WHERE configid = %s",
                    (config,),
                ),
            )
            cursor.execute(
                "SELECT configid FROM payroll.branchpaydefinitionconfig WHERE configid = %s",
                (config,),
            )
            assert cursor.fetchone() == (config,)


def test_draft_source_scope_period_definition_and_business_key(schema_databases):
    with schema_databases.database("period_definition_source", authority="sql") as dsn:
        with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
            company, branch, _, assignment, definition, rate, config = _world(cursor)
            period = _period(cursor, company, branch, "PDS-C")
            same_branch_other_period = _period(
                cursor,
                company,
                branch,
                "PDS-D",
                status="Draft",
                start="2030-01-14",
                end="2030-01-20",
            )
            other_branch = make_branch(cursor, company)
            other_branch_config = _config(cursor, company, other_branch, definition)
            other_branch_period = _period(cursor, company, other_branch, "PDS-E")
            other_company = make_company(cursor)
            foreign_branch = make_branch(cursor, other_company, is_default=True)
            foreign_period = _period(cursor, other_company, foreign_branch, "PDS-F")

            ppd = _period_definition(cursor, company, branch, period, definition, rate, config)
            same_branch_other_ppd = _period_definition(
                cursor, company, branch, same_branch_other_period, definition, rate, config
            )
            other_branch_ppd = _period_definition(
                cursor,
                company,
                other_branch,
                other_branch_period,
                definition,
                rate,
                other_branch_config,
            )
            foreign_definition = make_pay_definition(cursor, other_company)
            foreign_rate = make_rate_definition(cursor, foreign_definition)
            foreign_config = _config(cursor, other_company, foreign_branch, foreign_definition)
            foreign_ppd = _period_definition(
                cursor,
                other_company,
                foreign_branch,
                foreign_period,
                foreign_definition,
                foreign_rate,
                foreign_config,
            )
            conn.commit()

            line = _line(cursor, company, branch, period, assignment, ppd)
            conn.commit()
            # Valid source facts cannot retain derived manager-review state.
            for statement in (
                "UPDATE payroll.payrolldraftlines SET needsmanagerreview = TRUE "
                "WHERE draftlineid = %s",
                """
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverassignmentid, workdate,
                     payrollperioddefinitionid, quantity, sourcetype, status, needsmanagerreview)
                SELECT companyid, branchid, payrollperiodid, driverassignmentid, DATE '2030-01-09',
                       payrollperioddefinitionid, quantity, sourcetype, status, TRUE
                FROM payroll.payrolldraftlines WHERE draftlineid = %s
                """,
            ):
                cursor.execute("SAVEPOINT rejected_review_flag")
                with pytest.raises(errors.CheckViolation) as rejected:
                    cursor.execute(statement, (line,))
                assert rejected.value.diag.constraint_name == "ck_draftlines_nostoredreview"
                cursor.execute("ROLLBACK TO SAVEPOINT rejected_review_flag")
                cursor.execute("RELEASE SAVEPOINT rejected_review_flag")
            cursor.execute(
                "SELECT needsmanagerreview FROM payroll.payrolldraftlines WHERE draftlineid = %s",
                (line,),
            )
            assert cursor.fetchone() == (False,)
            # One active source per assignment/date/period definition.
            _rejected(
                cursor,
                errors.UniqueViolation,
                lambda: _line(cursor, company, branch, period, assignment, ppd),
            )
            # A same-Branch definition from another period is not interchangeable.
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: _line(
                    cursor,
                    company,
                    branch,
                    period,
                    assignment,
                    same_branch_other_ppd,
                    workdate="2030-01-09",
                ),
            )
            # A definition owned by another Branch cannot be used in this period.
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: _line(
                    cursor,
                    company,
                    branch,
                    period,
                    assignment,
                    other_branch_ppd,
                    workdate="2030-01-09",
                ),
            )
            # A PeriodDefinition owned by another Company cannot be used as source.
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: _line(
                    cursor, company, branch, period, assignment, foreign_ppd, workdate="2030-01-09"
                ),
            )
            # CompanyID must match the source Period, Assignment, and Definition.
            _rejected(
                cursor,
                errors.ForeignKeyViolation,
                lambda: _line(
                    cursor, other_company, branch, period, assignment, ppd, workdate="2030-01-09"
                ),
            )
            _rejected(
                cursor,
                errors.NotNullViolation,
                lambda: _line(cursor, company, branch, period, assignment, ppd, workdate=None),
            )

            cursor.execute(
                "SELECT draftlineid FROM payroll.payrolldraftlines WHERE payrollperiodid = %s",
                (period,),
            )
            assert cursor.fetchall() == [(line,)]
            cursor.execute(
                """
                SELECT companyid, branchid, payrollperiodid, driverassignmentid, workdate,
                       payrollperioddefinitionid, quantity, sourcetype, status
                FROM payroll.payrolldraftlines WHERE draftlineid = %s
            """,
                (line,),
            )
            assert cursor.fetchone() == (
                company,
                branch,
                period,
                assignment,
                date(2030, 1, 8),
                ppd,
                5,
                "Manual",
                "Active",
            )

            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'payroll' AND table_name = 'payrolldraftlines'
            """)
            columns = {row[0] for row in cursor.fetchall()}
            assert {
                "driverassignmentid",
                "payrollperioddefinitionid",
                "workdate",
                "quantity",
            } <= columns
            assert not columns & {"driverid", "linetype", "rateamount", "calculatedamount"}


def test_period_without_pay_definitions_has_no_sentinel_layout_row(schema_databases):
    with schema_databases.database("period_definition_empty", authority="sql") as dsn:
        with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
            company = make_company(cursor)
            branch = make_branch(cursor, company, is_default=True)
            period = _period(cursor, company, branch, "PDS-EMPTY")
            conn.commit()
            cursor.execute(
                "SELECT count(*) FROM payroll.payrollperioddefinitions WHERE payrollperiodid = %s",
                (period,),
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                "SELECT count(*) FROM payroll.paydefinitions WHERE companyid = %s", (company,)
            )
            assert cursor.fetchone() == (0,)
