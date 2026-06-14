-- 0019: sec.UserPermissionOverrides — member-specific extra permissions
--
-- A user's effective permissions = role permissions UNION extra ALLOW overrides.
-- Only ALLOW effect is supported for now (no DENY).
-- Soft-delete: IsActive=FALSE + RevokedAtUtc is the revoke pattern (not hard delete).

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
);

-- At most one active override per user+company+permission+effect
CREATE UNIQUE INDEX ux_UPO_Active
    ON sec.UserPermissionOverrides (UserID, CompanyID, PermissionCode, Effect)
    WHERE IsActive = TRUE;

-- Fast lookup by user + company
CREATE INDEX ix_UPO_User_Company
    ON sec.UserPermissionOverrides (UserID, CompanyID)
    WHERE IsActive = TRUE;
