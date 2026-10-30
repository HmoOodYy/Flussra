"""Current immutable calculation evidence schema shape on a fresh Alembic schema."""

from __future__ import annotations

import psycopg2
import pytest

from app.db.schema_guard import _check_calculation_evidence
from tests.schema_helpers import _columns, _schema_database, _tables, _version


@pytest.fixture(scope="module")
def evidence_database(schema_databases):
    with _schema_database(schema_databases, "calculation_evidence") as database:
        yield database


def test_fresh_schema_has_the_current_immutable_evidence_shape(evidence_database):
    _, dsn = evidence_database
    assert _version(dsn) == "0096"
    assert {
        "payrollcalculationsnapshotcomponents",
        "payrollcalculationsnapshotminmaxevaluations",
        "payrollcalculationsnapshotminmaxrules",
    } <= _tables(dsn)
    assert {
        "periodstartdate",
        "periodenddate",
        "payableroundingversion",
        "minmaxprorationversion",
        "scheduledworkdates",
        "warnings",
    } <= _columns(dsn, "payrollcalculationsnapshots")
    assert "employeeid" in _columns(dsn, "payrollcalculationdrivertotals")
    assert {
        "driverratescheduleid",
        "payrollcalculationsnapshotcomponentid",
        "payrollperioddefinitionid",
    } <= _columns(dsn, "payrollcalculationsnapshotlines")
    assert "payitemid" not in _columns(dsn, "payrollcalculationsnapshotlines")
    assert {"driverpayruleid", "payitemid", "ruletypesnapshot"}.isdisjoint(
        _columns(dsn, "payrollcalculationsnapshotusedratedefinitions")
    )
    assert {
        "payrollcalculationsnapshotid",
        "payrollcalculationsnapshotcomponentid",
        "employeeid",
        "componentkind",
    } <= _columns(dsn, "payrollfinallines")
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        assert _check_calculation_evidence(cursor) == []
        cursor.execute("""
            SELECT t.tgname FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE t.tgname LIKE 'trg_payrollcalculation%immutable' AND NOT t.tgisinternal
        """)
        immutable_triggers = {row[0].lower() for row in cursor.fetchall()}
        assert immutable_triggers == {
            "trg_payrollcalculationsnapshots_immutable",
            "trg_payrollcalculationdrivertotals_immutable",
            "trg_payrollcalculationsnapshotlines_immutable",
            "trg_payrollcalculationsnapshotstatusentries_immutable",
            "trg_payrollcalculationsnapshotbonusevents_immutable",
            "trg_payrollcalculationsnapshotusedratedefinitions_immutable",
            "trg_payrollcalculationsnapshotcomponents_immutable",
            "trg_payrollcalculationsnapshotminmaxevaluations_immutable",
            "trg_payrollcalculationsnapshotminmaxrules_immutable",
            "trg_payrollcalculationsnapshotusedratecomponents_immutable",
        }
