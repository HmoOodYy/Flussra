"""0020: DB-level uniqueness trigger for active Company Owner assignment.

Adds a BEFORE INSERT OR UPDATE trigger on sec.userbranchroles that rejects
any attempt to create a second active COMPANY_OWNER assignment for the same
company.  Service-level logic already prevents this, but the trigger provides
a hard DB-level guarantee.

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0020"
down_revision: str = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION sec.fn_check_company_owner_unique()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        DECLARE
            v_is_owner  BOOLEAN := FALSE;
            v_conflict  INT;
        BEGIN
            IF NEW.companyroleId IS NULL OR NOT NEW.isactive THEN
                RETURN NEW;
            END IF;

            SELECT (rolecode = 'COMPANY_OWNER')
            INTO   v_is_owner
            FROM   sec.companyroles
            WHERE  companyroleid = NEW.companyroleId;

            IF NOT FOUND OR NOT v_is_owner THEN
                RETURN NEW;
            END IF;

            SELECT COUNT(*)
            INTO   v_conflict
            FROM   sec.userbranchroles  ubr
            JOIN   sec.companyroles     cr ON cr.companyroleid = ubr.companyroleId
            WHERE  ubr.companyid  = NEW.companyid
              AND  cr.rolecode    = 'COMPANY_OWNER'
              AND  ubr.isactive   = TRUE
              AND  (TG_OP = 'INSERT' OR ubr.userbranchroleid <> NEW.userbranchroleid);

            IF v_conflict > 0 THEN
                RAISE EXCEPTION
                    'company_owner_duplicate: Only one active Company Owner assignment '
                    'is allowed per company (company_id=%).',
                    NEW.companyid
                    USING ERRCODE = 'unique_violation';
            END IF;

            RETURN NEW;
        END;
        $$
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_company_owner_unique
            BEFORE INSERT OR UPDATE ON sec.userbranchroles
            FOR EACH ROW EXECUTE FUNCTION sec.fn_check_company_owner_unique()
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_company_owner_unique ON sec.userbranchroles"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS sec.fn_check_company_owner_unique()"))
