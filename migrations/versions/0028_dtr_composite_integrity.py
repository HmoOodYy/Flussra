"""0028: DriverTransferRequests composite FK integrity.

Adds company-composite FK constraints to core.DriverTransferRequests so that
cross-company source/target branches and cross-company drivers are blocked at
the DB level (in addition to service-layer enforcement).

Changes:
  - CREATE UNIQUE INDEX ux_Drivers_ID_Company on core.Drivers(DriverID, CompanyID)
    (needed as FK target — existing ux_Drivers_ID_Company_Branch has an extra
    column and cannot be used as a 2-column FK target)
  - ADD CONSTRAINT fk_DTR_SourceBranch_Company NOT VALID
  - ADD CONSTRAINT fk_DTR_TargetBranch_Company NOT VALID
  - ADD CONSTRAINT fk_DTR_Driver_Company NOT VALID

Revision ID: 0028
Revises: 0027
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0028"
down_revision: str = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Support index: (DriverID, CompanyID) unique — FK target
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_Drivers_ID_Company "
        "ON core.Drivers (DriverID, CompanyID)"
    ))

    # Composite FK: source branch must be in same company
    op.execute(sa.text(
        "ALTER TABLE core.DriverTransferRequests "
        "ADD CONSTRAINT fk_DTR_SourceBranch_Company "
        "FOREIGN KEY (SourceBranchID, CompanyID) "
        "REFERENCES core.Branches (BranchID, CompanyID) "
        "NOT VALID"
    ))

    # Composite FK: target branch must be in same company
    op.execute(sa.text(
        "ALTER TABLE core.DriverTransferRequests "
        "ADD CONSTRAINT fk_DTR_TargetBranch_Company "
        "FOREIGN KEY (TargetBranchID, CompanyID) "
        "REFERENCES core.Branches (BranchID, CompanyID) "
        "NOT VALID"
    ))

    # Composite FK: source driver must be in same company
    op.execute(sa.text(
        "ALTER TABLE core.DriverTransferRequests "
        "ADD CONSTRAINT fk_DTR_Driver_Company "
        "FOREIGN KEY (DriverID, CompanyID) "
        "REFERENCES core.Drivers (DriverID, CompanyID) "
        "NOT VALID"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE core.DriverTransferRequests "
        "DROP CONSTRAINT IF EXISTS fk_DTR_Driver_Company"
    ))
    op.execute(sa.text(
        "ALTER TABLE core.DriverTransferRequests "
        "DROP CONSTRAINT IF EXISTS fk_DTR_TargetBranch_Company"
    ))
    op.execute(sa.text(
        "ALTER TABLE core.DriverTransferRequests "
        "DROP CONSTRAINT IF EXISTS fk_DTR_SourceBranch_Company"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS ux_Drivers_ID_Company"
    ))
