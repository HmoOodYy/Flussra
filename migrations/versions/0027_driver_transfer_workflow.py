"""0027: Driver Transfer Workflow.

Enables one Employee to have multiple Driver profiles (one per branch) so
that drivers can be transferred between branches while preserving all
payroll, rate, and rule history on the old profile.

Changes:
  - Drop uq_Drivers_EmployeeID (global unique constraint)
  - Add TransferredFromDriverID / TransferredToDriverID lineage columns
  - Add partial unique index ux_Drivers_Employee_Branch_Active
    (one ACTIVE driver per employee per branch; ignores Transferred/Terminated)
  - Create core.DriverTransferRequests workflow table

Revision ID: 0027
Revises: 0026
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0027"
down_revision: str = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop old global unique constraint
    op.execute(sa.text(
        "ALTER TABLE core.Drivers DROP CONSTRAINT uq_Drivers_EmployeeID"
    ))

    # 2. Add lineage columns
    op.execute(sa.text(
        "ALTER TABLE core.Drivers "
        "ADD COLUMN TransferredFromDriverID INTEGER, "
        "ADD COLUMN TransferredToDriverID   INTEGER"
    ))

    # 3. Add lineage FK constraints
    op.execute(sa.text(
        "ALTER TABLE core.Drivers "
        "ADD CONSTRAINT fk_Drivers_TransferredFrom "
        "FOREIGN KEY (TransferredFromDriverID) REFERENCES core.Drivers(DriverID)"
    ))
    op.execute(sa.text(
        "ALTER TABLE core.Drivers "
        "ADD CONSTRAINT fk_Drivers_TransferredTo "
        "FOREIGN KEY (TransferredToDriverID) REFERENCES core.Drivers(DriverID)"
    ))

    # 4. Partial unique index: one active driver per (employee, branch)
    op.execute(sa.text(
        "CREATE UNIQUE INDEX ux_Drivers_Employee_Branch_Active "
        "ON core.Drivers(EmployeeID, BranchID) "
        "WHERE DriverStatus NOT IN ('Transferred', 'Terminated')"
    ))

    # 5. Create DriverTransferRequests table
    op.execute(sa.text("""
        CREATE TABLE core.DriverTransferRequests (
            TransferRequestID       SERIAL       PRIMARY KEY,
            CompanyID               INTEGER      NOT NULL,
            DriverID                INTEGER      NOT NULL,
            SourceBranchID          INTEGER      NOT NULL,
            TargetBranchID          INTEGER      NOT NULL,
            RequestedByUserID       INTEGER      NOT NULL,
            InitiatedBy             VARCHAR(20)  NOT NULL,
            Status                  VARCHAR(30)  NOT NULL DEFAULT 'PendingSourceApproval',
            EffectiveDate           DATE         NOT NULL,
            Reason                  TEXT,
            Notes                   TEXT,
            SourceApprovedByUserID  INTEGER,
            SourceApprovedAtUtc     TIMESTAMPTZ,
            TargetDecidedByUserID   INTEGER,
            TargetDecidedAtUtc      TIMESTAMPTZ,
            TargetDecisionNotes     TEXT,
            NewDriverID             INTEGER,
            CompletedAtUtc          TIMESTAMPTZ,
            CompletedByUserID       INTEGER,
            CancelledAtUtc          TIMESTAMPTZ,
            CancelledByUserID       INTEGER,
            CancelReason            TEXT,
            CreatedAtUtc            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UpdatedAtUtc            TIMESTAMPTZ,
            CONSTRAINT ck_DTR_Status CHECK (Status IN (
                'PendingSourceApproval','PendingTargetApproval',
                'Returned','Rejected','Approved','Completed','Cancelled'
            )),
            CONSTRAINT ck_DTR_InitiatedBy CHECK (InitiatedBy IN ('Driver','SourceBranch')),
            CONSTRAINT ck_DTR_SourceTarget CHECK (SourceBranchID != TargetBranchID),
            CONSTRAINT fk_DTR_Company
                FOREIGN KEY (CompanyID)          REFERENCES core.Companies(CompanyID),
            CONSTRAINT fk_DTR_Driver
                FOREIGN KEY (DriverID)           REFERENCES core.Drivers(DriverID),
            CONSTRAINT fk_DTR_SourceBranch
                FOREIGN KEY (SourceBranchID)     REFERENCES core.Branches(BranchID),
            CONSTRAINT fk_DTR_TargetBranch
                FOREIGN KEY (TargetBranchID)     REFERENCES core.Branches(BranchID),
            CONSTRAINT fk_DTR_RequestedBy
                FOREIGN KEY (RequestedByUserID)  REFERENCES sec.Users(UserID),
            CONSTRAINT fk_DTR_SourceApprovedBy
                FOREIGN KEY (SourceApprovedByUserID) REFERENCES sec.Users(UserID),
            CONSTRAINT fk_DTR_TargetDecidedBy
                FOREIGN KEY (TargetDecidedByUserID)  REFERENCES sec.Users(UserID),
            CONSTRAINT fk_DTR_NewDriver
                FOREIGN KEY (NewDriverID)        REFERENCES core.Drivers(DriverID),
            CONSTRAINT fk_DTR_CompletedBy
                FOREIGN KEY (CompletedByUserID)  REFERENCES sec.Users(UserID),
            CONSTRAINT fk_DTR_CancelledBy
                FOREIGN KEY (CancelledByUserID)  REFERENCES sec.Users(UserID)
        )
    """))

    op.execute(sa.text(
        "CREATE INDEX ix_DTR_Company_Status "
        "ON core.DriverTransferRequests(CompanyID, Status)"
    ))
    op.execute(sa.text(
        "CREATE INDEX ix_DTR_SourceBranch "
        "ON core.DriverTransferRequests(SourceBranchID, Status)"
    ))
    op.execute(sa.text(
        "CREATE INDEX ix_DTR_TargetBranch "
        "ON core.DriverTransferRequests(TargetBranchID, Status)"
    ))
    op.execute(sa.text(
        "CREATE INDEX ix_DTR_Driver ON core.DriverTransferRequests(DriverID)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS core.DriverTransferRequests"))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS ux_Drivers_Employee_Branch_Active"
    ))
    op.execute(sa.text(
        "ALTER TABLE core.Drivers "
        "DROP CONSTRAINT IF EXISTS fk_Drivers_TransferredFrom, "
        "DROP CONSTRAINT IF EXISTS fk_Drivers_TransferredTo"
    ))
    op.execute(sa.text(
        "ALTER TABLE core.Drivers "
        "DROP COLUMN IF EXISTS TransferredFromDriverID, "
        "DROP COLUMN IF EXISTS TransferredToDriverID"
    ))
    op.execute(sa.text(
        "ALTER TABLE core.Drivers "
        "ADD CONSTRAINT uq_Drivers_EmployeeID UNIQUE (EmployeeID)"
    ))
