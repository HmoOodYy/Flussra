"""0032: Backfill missing RateTypes + PayItemRateTypeMap for custom pay items.

Root cause (Phase 3E diagnosis):
  create_custom_pay_item previously saved rate_names only to PayItemSettings
  (settingkey = 'rate_name_1', 'rate_name_2', ...) but never created RateTypes
  or PayItemRateTypeMap rows.  The comment in service.py labelled this a
  "Phase 2 gap".

  get_driver_rate_matrix uses INNER JOIN on PayItemRateTypeMap, so any custom
  pay item with no mapping rows was completely invisible in Pay Rates.

  Example: a custom item called "samya rate" (payitemid=15) would appear in
  the Pay Items list but silently disappear from Pay Rates.

What this migration does:
  For every custom (CompanyID IS NOT NULL), non-Retired, requires_rate=TRUE
  pay item that has NO active PayItemRateTypeMap row, it:
    1. Creates a RateTypes row with ratecode = 'CPI_<payitemid>_1'
    2. Creates a PayItemRateTypeMap row linking the two.
  ON CONFLICT / DO NOTHING makes this idempotent for repeated runs.

Revision ID: 0032
Revises: 0031
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0032"
down_revision: str = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
DO $$
DECLARE
    r RECORD;
    v_unit      VARCHAR(50);
    v_rate_name VARCHAR(200);
    v_rate_code VARCHAR(50);
    v_rt_id     INTEGER;
BEGIN
    FOR r IN
        SELECT pi.payitemid, pi.payitemname, pi.unit
        FROM   payroll.payitems pi
        WHERE  pi.companyid        IS NOT NULL
          AND  pi.issystemstandard = FALSE
          AND  pi.requiresrate     = TRUE
          AND  pi.status          != 'Retired'
          AND  NOT EXISTS (
                   SELECT 1
                   FROM   payroll.payitemratetypemap pirm
                   WHERE  pirm.payitemid = pi.payitemid
                     AND  pirm.status    = 'Active'
               )
    LOOP
        v_unit      := COALESCE(r.unit, 'Unit');
        v_rate_name := r.payitemname || ' Rate';
        v_rate_code := 'CPI_' || r.payitemid::TEXT || '_1';

        INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive)
        VALUES (v_rate_code, v_rate_name, v_unit, TRUE)
        ON CONFLICT (ratecode) DO UPDATE
            SET ratename = EXCLUDED.ratename;

        SELECT ratetypeid INTO v_rt_id
        FROM   payroll.ratetypes
        WHERE  ratecode = v_rate_code;

        INSERT INTO payroll.payitemratetypemap
            (payitemid, ratetypeid, isprimary, status)
        VALUES (r.payitemid, v_rt_id, TRUE, 'Active')
        ON CONFLICT (payitemid, ratetypeid) DO NOTHING;

        RAISE NOTICE 'Repaired pay item id=% name=% rate_code=%',
                     r.payitemid, r.payitemname, v_rate_code;
    END LOOP;
END $$;
    """))


def downgrade() -> None:
    # The backfill is additive and idempotent; no downgrade removes the rows
    # since they may have been legitimately extended by users post-migration.
    pass
