"""Current ordinal tier and immutable calculation evidence schema shape."""
from __future__ import annotations

import psycopg2
import pytest

from app.db.schema_guard import _check_calculation_evidence
from tests.schema_helpers import _columns, _schema_database, _version


@pytest.fixture(scope="module")
def head_database(schema_databases):
    with _schema_database(schema_databases, "ordinal_schema") as database:
        yield database



def test_fresh_schema_has_ordinal_tier_evidence_shape(head_database):
    _, dsn = head_database
    assert _version(dsn) == "0097"
    assert "ordinaltiers" in _columns(dsn, "paydefinitionrequests")
    assert "rateshapesnapshot" in _columns(dsn, "payrollcalculationsnapshotusedratedefinitions")
    assert {"payrollcalculationsnapshotusedratedefinitionid", "payrollcalculationsnapshotid",
            "ratedefinitionid", "sequenceno", "ratecomponentdefinitionid", "ordinalfrom",
            "ordinalto", "amountsnapshot"} <= _columns(dsn, "payrollcalculationsnapshotusedratecomponents")
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        assert _check_calculation_evidence(cursor) == []
