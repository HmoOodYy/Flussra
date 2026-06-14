"""0019: sec.UserPermissionOverrides — member-specific extra permissions.

Adds sec.UserPermissionOverrides for per-user ALLOW permission grants
that stack on top of the user's company role permissions.

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0019"
down_revision: str = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE sec.UserPermissionOverrides (
            UserPermissionOverrideID SERIAL       PRIMARY KEY,
            UserID                   INTEGER      NOT NULL,
            CompanyID                INTEGER      NOT NULL,
            PermissionCode           VARCHAR(100) NOT NULL,
            Effect                   VARCHAR(20)  NOT NULL DEFAULT 'ALLOW',
            IsActive                 BOOLEAN      NOT NULL DEFAULT TRUE,
            GrantedByUserID          INTEGER,
            GrantedAtUtc             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            RevokedAtUtc             TIMESTAMPTZ,
            Notes                    TEXT,

            CONSTRAINT fk_UPO_User    FOREIGN KEY (UserID)          REFERENCES sec.Users(UserID),
            CONSTRAINT fk_UPO_Company FOREIGN KEY (CompanyID)       REFERENCES core.Companies(CompanyID),
            CONSTRAINT fk_UPO_Granter FOREIGN KEY (GrantedByUserID) REFERENCES sec.Users(UserID),
            CONSTRAINT ck_UPO_Effect  CHECK (Effect IN ('ALLOW'))
        )
    """))
    op.execute(sa.text("""
        CREATE UNIQUE INDEX ux_UPO_Active
            ON sec.UserPermissionOverrides (UserID, CompanyID, PermissionCode, Effect)
            WHERE IsActive = TRUE
    """))
    op.execute(sa.text("""
        CREATE INDEX ix_UPO_User_Company
            ON sec.UserPermissionOverrides (UserID, CompanyID)
            WHERE IsActive = TRUE
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS sec.ix_UPO_User_Company"))
    op.execute(sa.text("DROP INDEX IF EXISTS sec.ux_UPO_Active"))
    op.execute(sa.text("DROP TABLE IF EXISTS sec.UserPermissionOverrides"))
