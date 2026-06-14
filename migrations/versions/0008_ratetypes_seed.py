"""Seed payroll.RateTypes and repair PayItemRateTypeMap on fresh Alembic DBs.

Root cause:
    Migration 0007 joined payroll.RateTypes to build PayItemRateTypeMap rows, but
    RateTypes were only seeded by the test-session fixture -- never by Alembic.
    On a fresh 'alembic upgrade head' the 0007 JOIN found zero matching RateType
    rows and produced zero PayItemRateTypeMap inserts.

Fix:
    This migration seeds all 7 standard production RateTypes first (idempotent via
    ON CONFLICT DO NOTHING on RateCode), then re-runs the PayItemRateTypeMap seed
    from 0007 (also idempotent).  After this migration a fresh Alembic DB has all
    expected PayItemRateTypeMap rows.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-30
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0008_ratetypes_seed.sql"


def _statements(path: Path) -> list[str]:
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    # Remove the PayItemRateTypeMap rows seeded by 0007/0008.
    op.execute(sa.text("""
        DELETE FROM payroll.payitemratetypemap
        WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems
            WHERE payitemcode IN ('HOURS', 'MILES', 'LOADS', 'WAIT_TIME', 'PALLETS', 'SILOS')
              AND companyid IS NULL
        )
    """))
    # Remove the RateTypes seeded here.
    op.execute(sa.text("""
        DELETE FROM payroll.ratetypes
        WHERE ratecode IN ('HOURLY', 'MILEAGE', 'LOAD', 'OVERNIGHT', 'WAIT', 'PALLET', 'SILO')
    """))
