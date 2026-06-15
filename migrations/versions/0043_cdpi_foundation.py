"""0043: Custom Daily Pay Item (CDPI) schema foundation.

Adds the three-table schema foundation for the Custom Daily Pay Item
request workflow.  No existing tables are modified.

Tables added:
  payroll.CdpiRequests         — request header (Draft → Approved lifecycle)
  payroll.CdpiRequestEvents    — append-only audit/event log with immutability trigger
  payroll.CdpiDefinitions      — approved-definition marker (1-to-1 with PayItems)

UUID strategy: UUIDv4 via gen_random_uuid() — built into PostgreSQL 17,
no extension required.

The append-only trigger for CdpiRequestEvents is defined inline here (not in
the SQL file) because the SQL-file statement splitter cannot handle PL/pgSQL
dollar-quoted blocks that contain semicolons.

Revision ID: 0043
Revises: 0042
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0043_cdpi_foundation.sql"

# ---------------------------------------------------------------------------
# Append-only guard for CdpiRequestEvents
# Defined here (not in the SQL file) to avoid dollar-quote / semicolon issues
# with the _statements() splitter.  Pattern mirrors migration 0038.
# ---------------------------------------------------------------------------
_CREATE_FN_EVENTS_IMMUTABLE = """
CREATE OR REPLACE FUNCTION payroll.fn_guard_cdpi_request_events_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'cdpi_events_immutable: Request events are append-only and cannot be % '
        'once written. (event_id=%)',
        TG_OP, OLD.eventid
        USING ERRCODE = 'restrict_violation';
    RETURN NULL;
END;
$$
"""

_DROP_TRIGGER_EVENTS_IMMUTABLE = """
DROP TRIGGER IF EXISTS trg_guard_cdpi_request_events_immutable
    ON payroll.CdpiRequestEvents
"""

_CREATE_TRIGGER_EVENTS_IMMUTABLE = """
CREATE TRIGGER trg_guard_cdpi_request_events_immutable
    BEFORE UPDATE OR DELETE ON payroll.CdpiRequestEvents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_cdpi_request_events_immutable()
"""


def _statements(path: Path) -> list[str]:
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    # Step 1: apply all table/index DDL from the SQL file
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))

    # Step 2: append-only guard trigger (inline to avoid dollar-quote splitting)
    op.execute(sa.text(_CREATE_FN_EVENTS_IMMUTABLE))
    op.execute(sa.text(_DROP_TRIGGER_EVENTS_IMMUTABLE))
    op.execute(sa.text(_CREATE_TRIGGER_EVENTS_IMMUTABLE))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_guard_cdpi_request_events_immutable "
        "ON payroll.CdpiRequestEvents"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.fn_guard_cdpi_request_events_immutable()"
    ))
    op.execute(sa.text("DROP TABLE IF EXISTS payroll.CdpiDefinitions"))
    op.execute(sa.text("DROP TABLE IF EXISTS payroll.CdpiRequestEvents"))
    op.execute(sa.text("DROP TABLE IF EXISTS payroll.CdpiRequests"))
