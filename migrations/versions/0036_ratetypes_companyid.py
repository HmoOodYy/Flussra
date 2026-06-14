"""0036: RateTypes.CompanyID -- structural ownership column (Phase 4C).

Adds payroll.RateTypes.CompanyID (nullable FK to core.Companies):

  NULL     -> system / global RateType (usable by any company)
  NOT NULL -> company-owned custom RateType

Backfill rules applied in a DO $$ block:
  - System types (has a PayItem mapping with CompanyID IS NULL): stay NULL
  - Single-company custom types: set CompanyID = owning company
  - Ambiguous (>1 company, no system mapping): migration aborts with a clear message
  - Orphaned CPI_ (active, no mapping at all): deactivated (isactive=FALSE)
  - Other unmapped non-CPI types: left as NULL (treated as legacy system types)

Also adds:
  - ix_ratetypes_companyid index for ownership-filter queries
  - DB trigger payroll.trg_guard_payitemratetypemap_ownership that rejects
    INSERT/UPDATE on PayItemRateTypeMap where the RateType is company-owned
    and the PayItem belongs to a different company.

Revision ID: 0036
Revises: 0035
"""
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision: str = "0036"
down_revision: str = "0035"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0036_ratetypes_companyid.sql"


# ---------------------------------------------------------------------------
# Individual DDL statements (split from SQL file for asyncpg compatibility)
# ---------------------------------------------------------------------------

_ADD_COLUMN = """
ALTER TABLE payroll.ratetypes
    ADD COLUMN IF NOT EXISTS companyid INTEGER
"""

_ADD_FK = """
ALTER TABLE payroll.ratetypes
    ADD CONSTRAINT fk_ratetypes_company
        FOREIGN KEY (companyid) REFERENCES core.companies(companyid)
"""

_BACKFILL = """
DO $$
DECLARE
    r           RECORD;
    v_cid       INTEGER;
    v_cnt       INTEGER;
    v_ambiguous TEXT := '';
BEGIN
    -- 2a. System types already have companyid NULL -- no-op touch.
    UPDATE payroll.ratetypes rt
    SET    companyid = NULL
    WHERE  rt.companyid IS NULL
      AND  EXISTS (
               SELECT 1
               FROM   payroll.payitemratetypemap pirm
               JOIN   payroll.payitems pi ON pi.payitemid = pirm.payitemid
               WHERE  pirm.ratetypeid = rt.ratetypeid
                 AND  pi.companyid IS NULL
           );

    -- 2b+2c. Non-system types: determine ownership.
    FOR r IN
        SELECT rt.ratetypeid, rt.ratecode
        FROM   payroll.ratetypes rt
        WHERE  rt.companyid IS NULL
          AND  NOT EXISTS (
                   SELECT 1
                   FROM   payroll.payitemratetypemap pirm
                   JOIN   payroll.payitems pi ON pi.payitemid = pirm.payitemid
                   WHERE  pirm.ratetypeid = rt.ratetypeid
                     AND  pi.companyid IS NULL
               )
    LOOP
        SELECT COUNT(DISTINCT pi.companyid), MIN(pi.companyid)
        INTO   v_cnt, v_cid
        FROM   payroll.payitemratetypemap pirm
        JOIN   payroll.payitems pi ON pi.payitemid = pirm.payitemid
        WHERE  pirm.ratetypeid = r.ratetypeid
          AND  pi.companyid IS NOT NULL;

        IF v_cnt = 0 THEN
            IF r.ratecode LIKE 'CPI_%' THEN
                UPDATE payroll.ratetypes
                SET    isactive = FALSE
                WHERE  ratetypeid = r.ratetypeid;
                RAISE NOTICE 'Migration 0036: deactivated orphaned CPI_ id=% code=%',
                             r.ratetypeid, r.ratecode;
            ELSE
                RAISE NOTICE 'Migration 0036: unmapped non-CPI id=% code=% left as NULL',
                             r.ratetypeid, r.ratecode;
            END IF;
        ELSIF v_cnt = 1 THEN
            UPDATE payroll.ratetypes
            SET    companyid = v_cid
            WHERE  ratetypeid = r.ratetypeid;
            RAISE NOTICE 'Migration 0036: assigned companyid=% to id=% code=%',
                         v_cid, r.ratetypeid, r.ratecode;
        ELSE
            v_ambiguous := v_ambiguous || format(
                ' [ratetypeid=%s ratecode=%s companies=%s]',
                r.ratetypeid, r.ratecode, v_cnt
            );
        END IF;
    END LOOP;

    IF v_ambiguous <> '' THEN
        RAISE EXCEPTION
            'Migration 0036 ABORTED: ambiguous RateType ownership -- '
            'mapped to PayItems from multiple companies with no system anchor:%',
            v_ambiguous;
    END IF;
END $$
"""

_ADD_INDEX = """
CREATE INDEX IF NOT EXISTS ix_ratetypes_companyid
    ON payroll.ratetypes (companyid)
"""

_CREATE_GUARD_FN = """
CREATE OR REPLACE FUNCTION payroll.fn_guard_payitemratetypemap_ownership()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_rt_company  INTEGER;
    v_pi_company  INTEGER;
BEGIN
    SELECT companyid INTO v_rt_company
    FROM   payroll.ratetypes
    WHERE  ratetypeid = NEW.ratetypeid;

    SELECT companyid INTO v_pi_company
    FROM   payroll.payitems
    WHERE  payitemid = NEW.payitemid;

    IF v_rt_company IS NOT NULL
       AND v_pi_company IS NOT NULL
       AND v_rt_company <> v_pi_company
    THEN
        RAISE EXCEPTION
            'payitemratetypemap_ownership_violation: Cannot map PayItem (id=%, companyid=%) '
            'to RateType (id=%, companyid=%) -- RateType belongs to a different company.',
            NEW.payitemid, v_pi_company, NEW.ratetypeid, v_rt_company
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$
"""

_DROP_GUARD_TRIGGER = """
DROP TRIGGER IF EXISTS trg_guard_payitemratetypemap_ownership
    ON payroll.payitemratetypemap
"""

_CREATE_GUARD_TRIGGER = """
CREATE TRIGGER trg_guard_payitemratetypemap_ownership
    BEFORE INSERT OR UPDATE ON payroll.payitemratetypemap
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_payitemratetypemap_ownership()
"""


def upgrade() -> None:
    op.execute(sa.text(_ADD_COLUMN))
    op.execute(sa.text(_ADD_FK))
    op.execute(sa.text(_BACKFILL))
    op.execute(sa.text(_ADD_INDEX))
    op.execute(sa.text(_CREATE_GUARD_FN))
    op.execute(sa.text(_DROP_GUARD_TRIGGER))
    op.execute(sa.text(_CREATE_GUARD_TRIGGER))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_guard_payitemratetypemap_ownership "
        "ON payroll.payitemratetypemap"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.fn_guard_payitemratetypemap_ownership()"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS ix_ratetypes_companyid"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.ratetypes "
        "DROP CONSTRAINT IF EXISTS fk_ratetypes_company"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.ratetypes DROP COLUMN IF EXISTS companyid"
    ))
