"""Current branch PayDefinition configuration ownership and versioning constraints."""
from __future__ import annotations

import psycopg2
import pytest
from psycopg2 import errors

from tests.compensation_database_fixtures import make_branch, make_company, make_pay_definition


def test_branch_definition_versions_enforce_scope_and_nonoverlap(schema_databases):
    with schema_databases.database("branch_definition_config", authority="sql") as dsn:
        with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
            company = make_company(cursor)
            other_company = make_company(cursor)
            branch = make_branch(cursor, company, is_default=True)
            other_branch = make_branch(cursor, other_company, is_default=True)
            definition = make_pay_definition(cursor, company)
            other_definition = make_pay_definition(cursor, other_company)
            conn.commit()

            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'payroll' AND table_name = 'branchpaydefinitionconfig'
            """)
            columns = {row[0] for row in cursor.fetchall()}
            assert {"companyid", "branchid", "paydefinitionid", "isactive",
                    "effectivefrom", "effectiveto"} <= columns
            assert "payitemid" not in columns

            insert = """
                INSERT INTO payroll.branchpaydefinitionconfig
                    (companyid, branchid, paydefinitionid, isactive, effectivefrom, effectiveto)
                VALUES (%s, %s, %s, TRUE, %s, %s)
            """
            with pytest.raises(errors.NotNullViolation):
                cursor.execute(insert, (company, branch, None, "2026-01-01", None))
            conn.rollback()
            with pytest.raises(errors.ForeignKeyViolation):
                cursor.execute(insert, (company, other_branch, definition, "2026-01-01", None))
            conn.rollback()
            with pytest.raises(errors.ForeignKeyViolation):
                cursor.execute(insert, (company, branch, other_definition, "2026-01-01", None))
            conn.rollback()

            cursor.execute(insert, (company, branch, definition, "2026-01-01", "2026-01-31"))
            cursor.execute(insert, (company, branch, definition, "2026-02-01", None))
            conn.commit()
            with pytest.raises(errors.ExclusionViolation):
                cursor.execute(insert, (company, branch, definition, "2026-01-15", "2026-02-15"))
            conn.rollback()
            with pytest.raises(errors.UniqueViolation):
                cursor.execute(insert, (company, branch, definition, "2027-01-01", None))
            conn.rollback()
            with pytest.raises(errors.CheckViolation):
                cursor.execute(insert, (company, branch, definition, "2026-05-01", "2026-04-01"))
            conn.rollback()
