import psycopg2

from app.db.schema_guard import _check_import_foundation


def _index_shape(dsn):
    with psycopg2.connect(**dsn) as connection, connection.cursor() as cursor:
        cursor.execute("""
            SELECT am.amname, i.indisunique, i.indisvalid, i.indisready,
                ARRAY(
                    SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY AS key(attnum, ordinality)
                    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = key.attnum
                    WHERE key.ordinality <= i.indnkeyatts
                    ORDER BY key.ordinality
                )
            FROM pg_index i
            JOIN pg_class x ON x.oid = i.indexrelid
            JOIN pg_am am ON am.oid = x.relam
            WHERE i.indexrelid = to_regclass('core.ix_drivers_company_driverid')
              AND i.indrelid = to_regclass('core.drivers')
        """)
        return cursor.fetchone()


def test_company_driver_lookup_index_matches_direct_sql_and_alembic(schema_databases):
    shapes = {}
    for authority in ("sql", "alembic"):
        with schema_databases.database(f"driver_identity_{authority}", authority=authority) as dsn:
            shapes[authority] = _index_shape(dsn)
            with psycopg2.connect(**dsn) as connection, connection.cursor() as cursor:
                assert _check_import_foundation(cursor) == []

    assert shapes["sql"] == ("btree", False, True, True, ["companyid", "driverid"])
    assert shapes["alembic"] == shapes["sql"]
