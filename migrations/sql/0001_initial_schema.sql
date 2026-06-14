-- =============================================================================
-- PostgreSQL Schema: PayrollSystemV2
-- Converted from SQL Server by extraction (Migration_Extraction, 2026-05-27)
--
-- TYPE MAPPING USED:
--   [int] IDENTITY(1,1)    -> SERIAL
--   [bigint] IDENTITY(1,1) -> BIGSERIAL
--   [int]                  -> INTEGER
--   [bigint]               -> BIGINT
--   [smallint]             -> SMALLINT
--   [tinyint]              -> SMALLINT
--   [bit]                  -> BOOLEAN
--   [nvarchar](n)          -> VARCHAR(n)
--   [nvarchar](max)        -> TEXT
--   [decimal](18,4)        -> NUMERIC(18,4)
--   [decimal](5,2)         -> NUMERIC(5,2)
--   [decimal](18,2)        -> NUMERIC(18,2)
--   [datetime2]            -> TIMESTAMPTZ
--   [datetime2](0)         -> TIMESTAMPTZ(0)
--   [date]                 -> DATE
--   [uniqueidentifier]     -> UUID
--   [timestamp] (rowversion) -> OMITTED (SQL Server-specific concurrency token)
--
-- NOTES:
--   - SYSUTCDATETIME() -> NOW() or CURRENT_TIMESTAMP
--   - All schemas are created with CREATE SCHEMA IF NOT EXISTS
--   - UUID columns that were UNIQUEIDENTIFIER defaults use gen_random_uuid()
--   - Enable pgcrypto extension: CREATE EXTENSION IF NOT EXISTS pgcrypto;
--   - SQL Server square-bracket identifiers are removed
--   - Constraints are preserved; names are cleaned of SET_ prefix etc.
--   - FKs are declared at end of each table definition
--   - RowVersion columns are omitted (handle concurrency in app layer or OCC)
--
-- VALIDATION: Executed against PostgreSQL 18 (testing.postgresql isolated cluster)
--   Tables:            42  (exact)
--   Views:              7  (exact)
--   Functions:          2  (sec.fn_UserCanAccessBranch, sec.fn_UserHasPermission)
--   Indexes:          108  (incl. PKs)
--   CHECK constraints: 328
--   FOREIGN KEYS:     128
--   Execution errors:   0
-- =============================================================================

-- Required extension for UUID generation
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- Required for integer/date equality operators inside EXCLUDE USING gist constraints
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- =============================================================================
-- SCHEMAS
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS sec;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS payroll;
CREATE SCHEMA IF NOT EXISTS import;
CREATE SCHEMA IF NOT EXISTS integration;
CREATE SCHEMA IF NOT EXISTS review;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS app;   -- used for views only


-- =============================================================================
-- SECTION 1: SECURITY / AUTH (sec schema)
-- =============================================================================

CREATE TABLE sec.Roles (
    RoleID       SERIAL       PRIMARY KEY,
    RoleCode     VARCHAR(50)  NOT NULL,
    RoleName     VARCHAR(100) NOT NULL,
    RoleLevel    INTEGER      NOT NULL DEFAULT 0,
    IsSystemRole BOOLEAN      NOT NULL DEFAULT FALSE,
    Notes        TEXT,

    CONSTRAINT uq_Roles_RoleCode UNIQUE (RoleCode)
);

CREATE TABLE sec.Permissions (
    PermissionID   SERIAL       PRIMARY KEY,
    PermissionCode VARCHAR(100) NOT NULL,
    PermissionName VARCHAR(150) NOT NULL,
    ModuleCode     VARCHAR(50)  NOT NULL,
    Notes          TEXT,

    CONSTRAINT uq_Permissions_Code UNIQUE (PermissionCode)
);

CREATE TABLE sec.RolePermissions (
    RolePermissionID SERIAL      PRIMARY KEY,
    RoleID           INTEGER     NOT NULL,
    PermissionID     INTEGER     NOT NULL,
    CreatedAtUtc     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_RolePermissions_Role_Permission UNIQUE (RoleID, PermissionID),
    CONSTRAINT fk_RolePermissions_Role       FOREIGN KEY (RoleID)       REFERENCES sec.Roles(RoleID),
    CONSTRAINT fk_RolePermissions_Permission FOREIGN KEY (PermissionID) REFERENCES sec.Permissions(PermissionID)
);

-- NOTE: sec.Users references core.Companies (CompanyID) and core.Employees (EmployeeID).
-- Both are nullable, so forward-reference FKs are declared DEFERRABLE or added after core tables.
-- sec.Users is created here; FK constraints to core are added after core tables.

CREATE TABLE sec.Users (
    UserID                    SERIAL       PRIMARY KEY,
    CompanyID                 INTEGER,                    -- FK to core.Companies (nullable; added after)
    EmployeeID                INTEGER,                    -- FK to core.Employees (nullable; added after)
    Username                  VARCHAR(150) NOT NULL,
    DisplayName               VARCHAR(200) NOT NULL,
    Email                     VARCHAR(255),
    Phone                     VARCHAR(40),
    PasswordHash              VARCHAR(500),
    PasswordHashVersion       VARCHAR(50),
    MustChangePassword        BOOLEAN      NOT NULL DEFAULT FALSE,
    TempCredentialExpiresAtUtc TIMESTAMPTZ,
    GmailSubject              TEXT,
    GmailEmail                VARCHAR(255),
    IsActive                  BOOLEAN      NOT NULL DEFAULT TRUE,
    CanLogin                  BOOLEAN      NOT NULL DEFAULT TRUE,
    FailedLoginCount          INTEGER      NOT NULL DEFAULT 0,
    LockedUntilUtc            TIMESTAMPTZ,
    LastLoginAtUtc            TIMESTAMPTZ,
    CreatedAtUtc              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UpdatedAtUtc              TIMESTAMPTZ
);

-- UserBranchRoles: declared after core.Branches
-- (defined below in its section)


-- =============================================================================
-- SECTION 2: CORE MASTER DATA (core schema)
-- =============================================================================

CREATE TABLE core.SystemSettings (
    SettingKey   VARCHAR(100) PRIMARY KEY,
    SettingValue TEXT,
    Notes        TEXT,
    CreatedAtUtc TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UpdatedAtUtc TIMESTAMPTZ
);

CREATE TABLE core.Companies (
    CompanyID     SERIAL       PRIMARY KEY,
    CompanyCode   VARCHAR(50)  NOT NULL,
    CompanyName   VARCHAR(200) NOT NULL,
    LegalName     VARCHAR(250),
    Status        VARCHAR(30)  NOT NULL DEFAULT 'Active',
    IsSuspended   BOOLEAN      NOT NULL DEFAULT FALSE,
    OwnerUserID   INTEGER,                                -- FK to sec.Users (nullable; circular)
    TimeZoneName  VARCHAR(100) NOT NULL DEFAULT 'Africa/Cairo',
    Notes         TEXT,
    CreatedAtUtc  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UpdatedAtUtc  TIMESTAMPTZ,

    CONSTRAINT uq_Companies_CompanyCode UNIQUE (CompanyCode)
);

-- Deferred FK: sec.Users.CompanyID -> core.Companies
ALTER TABLE sec.Users
    ADD CONSTRAINT fk_Users_Company
    FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID);

CREATE TABLE core.Branches (
    BranchID      SERIAL       PRIMARY KEY,
    CompanyID     INTEGER      NOT NULL,
    BranchCode    VARCHAR(50)  NOT NULL,
    BranchName    VARCHAR(200) NOT NULL,
    Status        VARCHAR(30)  NOT NULL DEFAULT 'Active',
    IsDefault     BOOLEAN      NOT NULL DEFAULT FALSE,
    AddressLine1  VARCHAR(250),
    City          VARCHAR(100),
    StateProvince VARCHAR(100),
    PostalCode    VARCHAR(20),
    Country       VARCHAR(100),
    Notes         TEXT,
    CreatedAtUtc  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UpdatedAtUtc  TIMESTAMPTZ,

    CONSTRAINT uq_Branches_Company_Code UNIQUE (CompanyID, BranchCode),
    CONSTRAINT uq_Branches_Company_Name UNIQUE (CompanyID, BranchName),
    CONSTRAINT fk_Branches_Company FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID)
);

CREATE TABLE core.Employees (
    EmployeeID        SERIAL       PRIMARY KEY,
    CompanyID         INTEGER      NOT NULL,
    BranchID          INTEGER      NOT NULL,
    EmployeeKey       VARCHAR(100),
    FullName          VARCHAR(200) NOT NULL,
    PreferredName     VARCHAR(150),
    EmployeeType      VARCHAR(30)  NOT NULL,  -- Driver | OfficeStaff | Manager | PayrollUser
    EmploymentStatus  VARCHAR(30)  NOT NULL DEFAULT 'Active',  -- Active | Inactive | Terminated
    Email             VARCHAR(255),
    PrimaryPhone      VARCHAR(40),
    HireDate          DATE,
    TerminationDate   DATE,
    Notes             TEXT,
    CreatedByUserID   INTEGER,
    CreatedAtUtc      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UpdatedAtUtc      TIMESTAMPTZ,

    CONSTRAINT fk_Employees_Company  FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_Employees_Branch   FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_Employees_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID)
);

-- Deferred FK: sec.Users.EmployeeID -> core.Employees
ALTER TABLE sec.Users
    ADD CONSTRAINT fk_Users_Employee
    FOREIGN KEY (EmployeeID) REFERENCES core.Employees(EmployeeID);

CREATE TABLE core.Drivers (
    DriverID         SERIAL      PRIMARY KEY,
    CompanyID        INTEGER     NOT NULL,
    BranchID         INTEGER     NOT NULL,
    EmployeeID       INTEGER     NOT NULL,
    DriverCode       VARCHAR(100),
    CDLNumber        VARCHAR(100),
    ExternalDriverID VARCHAR(150),
    DriverStatus     VARCHAR(30) NOT NULL DEFAULT 'Active',  -- Active | Inactive | Terminated | OnLeave
    CreatedAtUtc     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UpdatedAtUtc     TIMESTAMPTZ,

    CONSTRAINT uq_Drivers_EmployeeID UNIQUE (EmployeeID),
    CONSTRAINT fk_Drivers_Company   FOREIGN KEY (CompanyID)  REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_Drivers_Branch    FOREIGN KEY (BranchID)   REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_Drivers_Employee  FOREIGN KEY (EmployeeID) REFERENCES core.Employees(EmployeeID)
);

CREATE TABLE core.DriverAliases (
    DriverAliasID    SERIAL       PRIMARY KEY,
    CompanyID        INTEGER      NOT NULL,
    BranchID         INTEGER      NOT NULL,
    DriverID         INTEGER      NOT NULL,
    AliasValue       VARCHAR(250) NOT NULL,
    AliasType        VARCHAR(50)  NOT NULL,
    SourceName       VARCHAR(200),
    ConfidenceNote   VARCHAR(500),
    IsActive         BOOLEAN      NOT NULL DEFAULT TRUE,
    CreatedByUserID  INTEGER,
    CreatedAtUtc     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_DriverAliases_Company FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_DriverAliases_Branch  FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_DriverAliases_Driver  FOREIGN KEY (DriverID)  REFERENCES core.Drivers(DriverID),
    CONSTRAINT fk_DriverAliases_Creator FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE core.EmployeePhones (
    EmployeePhoneID SERIAL      PRIMARY KEY,
    EmployeeID      INTEGER     NOT NULL,
    PhoneNumber     VARCHAR(40) NOT NULL,
    IsPrimary       BOOLEAN     NOT NULL DEFAULT FALSE,
    StartDate       DATE        NOT NULL,
    EndDate         DATE,
    Notes           TEXT,

    CONSTRAINT fk_EmployeePhones_Employee FOREIGN KEY (EmployeeID) REFERENCES core.Employees(EmployeeID)
);

-- Now create UserBranchRoles (was deferred, needs core.Companies + core.Branches)
CREATE TABLE sec.UserBranchRoles (
    UserBranchRoleID  SERIAL       PRIMARY KEY,
    UserID            INTEGER      NOT NULL,
    CompanyID         INTEGER      NOT NULL,
    BranchID          INTEGER,                   -- NULL when ScopeType = 'AllCompanyBranches'
    RoleID            INTEGER      NOT NULL,
    ScopeType         VARCHAR(30)  NOT NULL,     -- AllCompanyBranches | SpecificBranch | OwnDriverDataOnly
    IsActive          BOOLEAN      NOT NULL DEFAULT TRUE,
    GrantedByUserID   INTEGER,
    GrantedAtUtc      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    RevokedAtUtc      TIMESTAMPTZ,
    Notes             TEXT,

    CONSTRAINT ck_UserBranchRoles_ScopeType  CHECK (ScopeType IN ('AllCompanyBranches', 'SpecificBranch', 'OwnDriverDataOnly')),
    CONSTRAINT ck_UserBranchRoles_BranchScope CHECK (
        (ScopeType = 'AllCompanyBranches' AND BranchID IS NULL)
        OR (ScopeType IN ('OwnDriverDataOnly', 'SpecificBranch') AND BranchID IS NOT NULL)
    ),
    CONSTRAINT fk_UserBranchRoles_User      FOREIGN KEY (UserID)          REFERENCES sec.Users(UserID),
    CONSTRAINT fk_UserBranchRoles_Company   FOREIGN KEY (CompanyID)       REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_UserBranchRoles_Branch    FOREIGN KEY (BranchID)        REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_UserBranchRoles_Role      FOREIGN KEY (RoleID)          REFERENCES sec.Roles(RoleID),
    CONSTRAINT fk_UserBranchRoles_Grantor   FOREIGN KEY (GrantedByUserID) REFERENCES sec.Users(UserID)
);

-- Also add OwnerUserID FK on core.Companies now that sec.Users exists
ALTER TABLE core.Companies
    ADD CONSTRAINT fk_Companies_Owner FOREIGN KEY (OwnerUserID) REFERENCES sec.Users(UserID);


-- =============================================================================
-- SECTION 3: PAYROLL (payroll schema)
-- =============================================================================

CREATE TABLE payroll.RateTypes (
    RateTypeID  SERIAL       PRIMARY KEY,
    RateCode    VARCHAR(50)  NOT NULL,
    RateName    VARCHAR(100) NOT NULL,
    UnitName    VARCHAR(50)  NOT NULL,
    IsActive    BOOLEAN      NOT NULL DEFAULT TRUE,

    CONSTRAINT uq_RateTypes_RateCode UNIQUE (RateCode)
);

CREATE TABLE payroll.PayrollPeriods (
    PayrollPeriodID       SERIAL       PRIMARY KEY,
    CompanyID             INTEGER      NOT NULL,
    BranchID              INTEGER      NOT NULL,
    ParentPayrollPeriodID INTEGER,
    PeriodCode            VARCHAR(80)  NOT NULL,
    PeriodName            VARCHAR(150) NOT NULL,
    PeriodType            VARCHAR(30)  NOT NULL,   -- Week | Biweek | Month | Custom
    StartDate             DATE         NOT NULL,
    EndDate               DATE         NOT NULL,
    Status                VARCHAR(30)  NOT NULL DEFAULT 'Draft',
    -- Draft | Open | InReview | Approved | Locked | Cancelled | Archived
    CreatedByUserID       INTEGER,
    CreatedAtUtc          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    SubmittedAtUtc        TIMESTAMPTZ,
    ApprovedByUserID      INTEGER,
    ApprovedAtUtc         TIMESTAMPTZ,
    LockedByUserID        INTEGER,
    LockedAtUtc           TIMESTAMPTZ,
    Notes                 TEXT,
    PayDate               DATE,

    CONSTRAINT fk_PayrollPeriods_Company     FOREIGN KEY (CompanyID)        REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_PayrollPeriods_Branch      FOREIGN KEY (BranchID)         REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_PayrollPeriods_Parent      FOREIGN KEY (ParentPayrollPeriodID) REFERENCES payroll.PayrollPeriods(PayrollPeriodID),
    CONSTRAINT fk_PayrollPeriods_Creator     FOREIGN KEY (CreatedByUserID)  REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayrollPeriods_Approver    FOREIGN KEY (ApprovedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayrollPeriods_Locker      FOREIGN KEY (LockedByUserID)   REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.BranchPayrollSettings (
    BranchPayrollSettingsID  SERIAL      PRIMARY KEY,
    CompanyID                INTEGER     NOT NULL,
    BranchID                 INTEGER     NOT NULL,
    PayrollFrequency         VARCHAR(20) NOT NULL,   -- Week | Biweek | Month | Custom
    AnchorStartDate          DATE        NOT NULL,
    PayDateOffsetDays        INTEGER     NOT NULL DEFAULT 0,
    IsActive                 BOOLEAN     NOT NULL DEFAULT TRUE,
    CreatedByUserID          INTEGER,
    CreatedAtUtc             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UpdatedAtUtc             TIMESTAMPTZ,
    Notes                    TEXT,
    PayDayOfWeek             SMALLINT,              -- 1=Sun ... 7=Sat (SQL Server convention)
    FirstPayDate             DATE,
    IncludePayDayAsWorkDay   BOOLEAN     NOT NULL DEFAULT FALSE,
    NormalDaysOffMask        SMALLINT,              -- bitmask of days off

    CONSTRAINT uq_BranchPayrollSettings_Branch UNIQUE (CompanyID, BranchID),
    CONSTRAINT fk_BranchPayrollSettings_Company FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_BranchPayrollSettings_Branch  FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_BranchPayrollSettings_Creator FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PayrollStatusKeys (
    StatusKeyID                 SERIAL       PRIMARY KEY,
    CompanyID                   INTEGER      NOT NULL,
    BranchID                    INTEGER      NOT NULL,
    StatusCode                  VARCHAR(50)  NOT NULL,
    NormalizedStatusCode        VARCHAR(50)  NOT NULL,
    HoursValue                  NUMERIC(5,2) NOT NULL DEFAULT 0,
    IsOffReason                 BOOLEAN      NOT NULL DEFAULT TRUE,
    DeductsFromYearlyAllowance  BOOLEAN      NOT NULL DEFAULT FALSE,
    AllowanceCategory           VARCHAR(50),
    IsActive                    BOOLEAN      NOT NULL DEFAULT TRUE,
    DisplayOrder                INTEGER      NOT NULL DEFAULT 0,
    CreatedByUserID             INTEGER,
    CreatedAtUtc                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UpdatedByUserID             INTEGER,
    UpdatedAtUtc                TIMESTAMPTZ,

    CONSTRAINT fk_PayrollStatusKeys_Company  FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_PayrollStatusKeys_Branch   FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_PayrollStatusKeys_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayrollStatusKeys_Updater  FOREIGN KEY (UpdatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PayrollDraftLines (
    DraftLineID        BIGSERIAL     PRIMARY KEY,
    CompanyID          INTEGER       NOT NULL,
    BranchID           INTEGER       NOT NULL,
    PayrollPeriodID    INTEGER       NOT NULL,
    DriverID           INTEGER       NOT NULL,
    WorkDate           DATE,
    LineType           VARCHAR(50)   NOT NULL,
    Quantity           NUMERIC(18,4) NOT NULL DEFAULT 0,
    RateAmount         NUMERIC(18,4),
    CalculatedAmount   NUMERIC(18,4),
    SourceType         VARCHAR(30)   NOT NULL,
    SourceID           VARCHAR(100),
    Status             VARCHAR(30)   NOT NULL DEFAULT 'Active',
    NeedsManagerReview BOOLEAN       NOT NULL DEFAULT FALSE,
    AddedByUserID      INTEGER,
    AddedAtUtc         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    Notes              TEXT,

    CONSTRAINT fk_DraftLines_Company  FOREIGN KEY (CompanyID)      REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_DraftLines_Branch   FOREIGN KEY (BranchID)       REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_DraftLines_Period   FOREIGN KEY (PayrollPeriodID) REFERENCES payroll.PayrollPeriods(PayrollPeriodID),
    CONSTRAINT fk_DraftLines_Driver   FOREIGN KEY (DriverID)        REFERENCES core.Drivers(DriverID),
    CONSTRAINT fk_DraftLines_AddedBy  FOREIGN KEY (AddedByUserID)   REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PayrollFinalLines (
    FinalLineID      BIGSERIAL     PRIMARY KEY,
    CompanyID        INTEGER       NOT NULL,
    BranchID         INTEGER       NOT NULL,
    PayrollPeriodID  INTEGER       NOT NULL,
    DraftLineID      BIGINT,
    DriverID         INTEGER       NOT NULL,
    WorkDate         DATE,
    LineType         VARCHAR(50)   NOT NULL,
    Quantity         NUMERIC(18,4) NOT NULL DEFAULT 0,
    RateAmount       NUMERIC(18,4),
    FinalAmount      NUMERIC(18,4) NOT NULL DEFAULT 0,
    SourceType       VARCHAR(30)   NOT NULL,
    SourceID         VARCHAR(100),
    ApprovedByUserID INTEGER,
    ApprovedAtUtc    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    LockedAtUtc      TIMESTAMPTZ,
    Notes            TEXT,

    CONSTRAINT fk_FinalLines_Company  FOREIGN KEY (CompanyID)      REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_FinalLines_Branch   FOREIGN KEY (BranchID)       REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_FinalLines_Period   FOREIGN KEY (PayrollPeriodID) REFERENCES payroll.PayrollPeriods(PayrollPeriodID),
    CONSTRAINT fk_FinalLines_Draft    FOREIGN KEY (DraftLineID)     REFERENCES payroll.PayrollDraftLines(DraftLineID),
    CONSTRAINT fk_FinalLines_Driver   FOREIGN KEY (DriverID)        REFERENCES core.Drivers(DriverID),
    CONSTRAINT fk_FinalLines_Approver FOREIGN KEY (ApprovedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.DriverRates (
    DriverRateID     SERIAL        PRIMARY KEY,
    CompanyID        INTEGER       NOT NULL,
    BranchID         INTEGER       NOT NULL,
    DriverID         INTEGER       NOT NULL,
    RateTypeID       INTEGER       NOT NULL,
    Amount           NUMERIC(18,4) NOT NULL,
    EffectiveFrom    DATE          NOT NULL,
    EffectiveTo      DATE,
    Status           VARCHAR(30)   NOT NULL DEFAULT 'PendingApproval',
    -- PendingApproval | Approved | Superseded | Voided
    CreatedByUserID  INTEGER,
    CreatedAtUtc     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    ApprovedByUserID INTEGER,
    ApprovedAtUtc    TIMESTAMPTZ,
    Notes            TEXT,

    CONSTRAINT fk_DriverRates_Company  FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_DriverRates_Branch   FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_DriverRates_Driver   FOREIGN KEY (DriverID)  REFERENCES core.Drivers(DriverID),
    CONSTRAINT fk_DriverRates_RateType FOREIGN KEY (RateTypeID) REFERENCES payroll.RateTypes(RateTypeID),
    CONSTRAINT fk_DriverRates_Creator  FOREIGN KEY (CreatedByUserID)  REFERENCES sec.Users(UserID),
    CONSTRAINT fk_DriverRates_Approver FOREIGN KEY (ApprovedByUserID) REFERENCES sec.Users(UserID)
);

-- Covering index (migration 017 equivalent for PostgreSQL)
CREATE INDEX ix_DriverRates_Company_Branch_Status
    ON payroll.DriverRates (CompanyID, BranchID, Status)
    INCLUDE (DriverID, RateTypeID, Amount, EffectiveFrom, EffectiveTo,
             CreatedByUserID, ApprovedByUserID, CreatedAtUtc, ApprovedAtUtc, Notes);

CREATE INDEX ix_DriverRates_Driver_Type_Dates
    ON payroll.DriverRates (DriverID, RateTypeID, EffectiveFrom, EffectiveTo, Status);

-- Belt-and-suspenders: at most one Approved rate per driver x rate type.
-- The service-layer atomic claim prevents concurrent double-approvals, but this
-- unique index makes it impossible at the database level as well.
CREATE UNIQUE INDEX ux_DriverRates_Driver_Type_Approved
    ON payroll.DriverRates (DriverID, RateTypeID)
    WHERE Status = 'Approved';

-- Enforce non-overlapping date ranges for all Approved and Superseded rates.
-- Two rates for the same Company+Driver+RateType may not cover the same date.
-- PendingApproval and Voided rows are exempt from this constraint.
--
-- daterange(from, to, '[]') creates an inclusive date range.
-- When EffectiveTo IS NULL the range is open-ended (no upper bound) in PostgreSQL.
-- The && operator returns TRUE when two ranges share any dates.
--
-- This catches the case the service layer cannot prevent on its own: a pre-existing
-- Superseded rate that was not cleanly closed before a new rate was inserted.
ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT excl_DriverRates_no_date_overlap
    EXCLUDE USING gist (
        CompanyID  WITH =,
        DriverID   WITH =,
        RateTypeID WITH =,
        daterange(EffectiveFrom, EffectiveTo, '[]') WITH &&
    ) WHERE (Status IN ('Approved', 'Superseded'));

CREATE TABLE payroll.PayItems (
    PayItemID              SERIAL       PRIMARY KEY,
    CompanyID              INTEGER,
    BranchID               INTEGER,
    PayItemCode            VARCHAR(50)  NOT NULL,
    PayItemName            VARCHAR(200) NOT NULL,
    Category               VARCHAR(50)  NOT NULL,
    DataType               VARCHAR(50)  NOT NULL,
    Unit                   VARCHAR(50),
    Status                 VARCHAR(30)  NOT NULL DEFAULT 'Active',
    SortOrder              INTEGER      NOT NULL DEFAULT 0,
    AppearsInPayrollEntry  BOOLEAN      NOT NULL DEFAULT FALSE,
    AppearsInLedger        BOOLEAN      NOT NULL DEFAULT TRUE,
    AppearsInReports       BOOLEAN      NOT NULL DEFAULT TRUE,
    RequiresRate           BOOLEAN      NOT NULL DEFAULT FALSE,
    IsSystemStandard       BOOLEAN      NOT NULL DEFAULT FALSE,
    CreatedByUserID        INTEGER,
    CreatedAtUtc           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UpdatedByUserID        INTEGER,
    UpdatedAtUtc           TIMESTAMPTZ,
    ItemScope              VARCHAR(30)  NOT NULL DEFAULT 'Daily',
    RateBehavior           VARCHAR(30)  NOT NULL DEFAULT 'PerUnit',
    IsDefaultBranchActive  BOOLEAN      NOT NULL DEFAULT FALSE,

    CONSTRAINT fk_PayItems_Company  FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_PayItems_Branch   FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_PayItems_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayItems_Updater  FOREIGN KEY (UpdatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PayItemSettings (
    PayItemSettingID    SERIAL        PRIMARY KEY,
    PayItemID           INTEGER       NOT NULL,
    CompanyID           INTEGER,
    BranchID            INTEGER,
    SettingKey          VARCHAR(100)  NOT NULL,
    SettingDataType     VARCHAR(30)   NOT NULL,
    SettingValueBit     BOOLEAN,
    SettingValueDecimal NUMERIC(18,4),
    SettingValueText    TEXT,
    Status              VARCHAR(30)   NOT NULL DEFAULT 'Active',
    CreatedByUserID     INTEGER,
    CreatedAtUtc        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UpdatedByUserID     INTEGER,
    UpdatedAtUtc        TIMESTAMPTZ,

    CONSTRAINT fk_PayItemSettings_PayItem  FOREIGN KEY (PayItemID)  REFERENCES payroll.PayItems(PayItemID),
    CONSTRAINT fk_PayItemSettings_Company  FOREIGN KEY (CompanyID)  REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_PayItemSettings_Branch   FOREIGN KEY (BranchID)   REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_PayItemSettings_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayItemSettings_Updater  FOREIGN KEY (UpdatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PayItemLineTypeMap (
    PayItemLineTypeMapID SERIAL      PRIMARY KEY,
    PayItemID            INTEGER     NOT NULL,
    LineType             VARCHAR(50) NOT NULL,
    SourceContext        VARCHAR(50) NOT NULL,
    IsPrimary            BOOLEAN     NOT NULL DEFAULT FALSE,
    Status               VARCHAR(30) NOT NULL DEFAULT 'Active',
    CreatedAtUtc         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_PayItemLineTypeMap_Item_LineType_Context UNIQUE (PayItemID, LineType, SourceContext),
    CONSTRAINT fk_PayItemLineTypeMap_PayItem FOREIGN KEY (PayItemID) REFERENCES payroll.PayItems(PayItemID)
);

CREATE TABLE payroll.PayItemRateTypeMap (
    PayItemRateTypeMapID SERIAL      PRIMARY KEY,
    PayItemID            INTEGER     NOT NULL,
    RateTypeID           INTEGER     NOT NULL,
    IsPrimary            BOOLEAN     NOT NULL DEFAULT FALSE,
    Status               VARCHAR(30) NOT NULL DEFAULT 'Active',
    CreatedAtUtc         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_PayItemRateTypeMap_Item_RateType UNIQUE (PayItemID, RateTypeID),
    CONSTRAINT fk_PayItemRateTypeMap_PayItem   FOREIGN KEY (PayItemID)  REFERENCES payroll.PayItems(PayItemID),
    CONSTRAINT fk_PayItemRateTypeMap_RateType  FOREIGN KEY (RateTypeID) REFERENCES payroll.RateTypes(RateTypeID)
);

CREATE TABLE payroll.BranchPayItemConfig (
    ConfigID        SERIAL      PRIMARY KEY,
    CompanyID       INTEGER     NOT NULL,
    BranchID        INTEGER     NOT NULL,
    PayItemID       INTEGER     NOT NULL,
    IsActive        BOOLEAN     NOT NULL DEFAULT TRUE,
    EffectiveFrom   DATE        NOT NULL,
    EffectiveTo     DATE,
    Notes           TEXT,
    CreatedAtUtc    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CreatedByUserID INTEGER,

    CONSTRAINT fk_BranchPayItemConfig_Company  FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_BranchPayItemConfig_Branch   FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_BranchPayItemConfig_PayItem  FOREIGN KEY (PayItemID) REFERENCES payroll.PayItems(PayItemID),
    CONSTRAINT fk_BranchPayItemConfig_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PayProfiles (
    PayProfileID    SERIAL       PRIMARY KEY,
    CompanyID       INTEGER      NOT NULL,
    BranchID        INTEGER,
    ProfileCode     VARCHAR(50)  NOT NULL,
    ProfileName     VARCHAR(200) NOT NULL,
    Description     TEXT,
    Status          VARCHAR(30)  NOT NULL DEFAULT 'Draft',
    EffectiveFrom   DATE         NOT NULL,
    EffectiveTo     DATE,
    CreatedByUserID INTEGER,
    CreatedAtUtc    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UpdatedByUserID INTEGER,
    UpdatedAtUtc    TIMESTAMPTZ,

    CONSTRAINT fk_PayProfiles_Company  FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_PayProfiles_Branch   FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_PayProfiles_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayProfiles_Updater  FOREIGN KEY (UpdatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PayProfilePayItems (
    PayProfilePayItemID SERIAL      PRIMARY KEY,
    PayProfileID        INTEGER     NOT NULL,
    RateTypeID          INTEGER     NOT NULL,
    Enabled             BOOLEAN     NOT NULL DEFAULT TRUE,
    SortOrder           INTEGER     NOT NULL DEFAULT 0,
    RequiresRate        BOOLEAN     NOT NULL DEFAULT TRUE,
    Status              VARCHAR(30) NOT NULL DEFAULT 'Active',
    CreatedByUserID     INTEGER,
    CreatedAtUtc        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UpdatedByUserID     INTEGER,
    UpdatedAtUtc        TIMESTAMPTZ,

    CONSTRAINT uq_PayProfilePayItems_Profile_RateType UNIQUE (PayProfileID, RateTypeID),
    CONSTRAINT fk_PayProfilePayItems_Profile  FOREIGN KEY (PayProfileID) REFERENCES payroll.PayProfiles(PayProfileID),
    CONSTRAINT fk_PayProfilePayItems_RateType FOREIGN KEY (RateTypeID)   REFERENCES payroll.RateTypes(RateTypeID),
    CONSTRAINT fk_PayProfilePayItems_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayProfilePayItems_Updater  FOREIGN KEY (UpdatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PayProfileRates (
    PayProfileRateID SERIAL        PRIMARY KEY,
    PayProfileID     INTEGER       NOT NULL,
    RateTypeID       INTEGER       NOT NULL,
    RateAmount       NUMERIC(18,4) NOT NULL,
    RateUnit         VARCHAR(50),
    EffectiveFrom    DATE          NOT NULL,
    EffectiveTo      DATE,
    Status           VARCHAR(30)   NOT NULL DEFAULT 'Draft',
    CreatedByUserID  INTEGER,
    CreatedAtUtc     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UpdatedByUserID  INTEGER,
    UpdatedAtUtc     TIMESTAMPTZ,

    CONSTRAINT fk_PayProfileRates_Profile  FOREIGN KEY (PayProfileID) REFERENCES payroll.PayProfiles(PayProfileID),
    CONSTRAINT fk_PayProfileRates_RateType FOREIGN KEY (RateTypeID)   REFERENCES payroll.RateTypes(RateTypeID),
    CONSTRAINT fk_PayProfileRates_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayProfileRates_Updater  FOREIGN KEY (UpdatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PersonPayProfileAssignments (
    AssignmentID    SERIAL      PRIMARY KEY,
    CompanyID       INTEGER     NOT NULL,
    EmployeeID      INTEGER     NOT NULL,
    PayProfileID    INTEGER     NOT NULL,
    BranchID        INTEGER,
    EffectiveFrom   DATE        NOT NULL,
    EffectiveTo     DATE,
    Status          VARCHAR(30) NOT NULL DEFAULT 'Active',
    AssignedByUserID INTEGER,
    AssignedAtUtc   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UpdatedByUserID INTEGER,
    UpdatedAtUtc    TIMESTAMPTZ,

    CONSTRAINT fk_PersonPayProfileAssign_Company   FOREIGN KEY (CompanyID)   REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_PersonPayProfileAssign_Employee  FOREIGN KEY (EmployeeID)  REFERENCES core.Employees(EmployeeID),
    CONSTRAINT fk_PersonPayProfileAssign_Profile   FOREIGN KEY (PayProfileID) REFERENCES payroll.PayProfiles(PayProfileID),
    CONSTRAINT fk_PersonPayProfileAssign_Branch    FOREIGN KEY (BranchID)    REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_PersonPayProfileAssign_Assigner  FOREIGN KEY (AssignedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PersonPayProfileAssign_Updater   FOREIGN KEY (UpdatedByUserID)  REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PayrollRunBonuses (
    PayrollRunBonusID              SERIAL        PRIMARY KEY,
    CompanyID                      INTEGER       NOT NULL,
    BranchID                       INTEGER       NOT NULL,
    PayrollPeriodID                INTEGER       NOT NULL,
    EmployeeID                     INTEGER       NOT NULL,
    DriverID                       INTEGER       NOT NULL,
    PayItemID                      INTEGER       NOT NULL,
    Amount                         NUMERIC(18,2) NOT NULL,
    Reason                         VARCHAR(500),
    IncludeInMinimumPayComparison  BOOLEAN       NOT NULL DEFAULT FALSE,
    Status                         VARCHAR(30)   NOT NULL DEFAULT 'Approved',
    CreatedByUserID                INTEGER,
    CreatedAtUtc                   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UpdatedByUserID                INTEGER,
    UpdatedAtUtc                   TIMESTAMPTZ,
    VoidedByUserID                 INTEGER,
    VoidedAtUtc                    TIMESTAMPTZ,
    VoidReason                     VARCHAR(500),

    CONSTRAINT fk_PayrollRunBonuses_Company  FOREIGN KEY (CompanyID)      REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_PayrollRunBonuses_Branch   FOREIGN KEY (BranchID)       REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_PayrollRunBonuses_Period   FOREIGN KEY (PayrollPeriodID) REFERENCES payroll.PayrollPeriods(PayrollPeriodID),
    CONSTRAINT fk_PayrollRunBonuses_Employee FOREIGN KEY (EmployeeID)     REFERENCES core.Employees(EmployeeID),
    CONSTRAINT fk_PayrollRunBonuses_Driver   FOREIGN KEY (DriverID)       REFERENCES core.Drivers(DriverID),
    CONSTRAINT fk_PayrollRunBonuses_PayItem  FOREIGN KEY (PayItemID)      REFERENCES payroll.PayItems(PayItemID),
    CONSTRAINT fk_PayrollRunBonuses_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayrollRunBonuses_Updater  FOREIGN KEY (UpdatedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayrollRunBonuses_Voider   FOREIGN KEY (VoidedByUserID)  REFERENCES sec.Users(UserID)
);

CREATE TABLE payroll.PayrollReconciliationIssues (
    ReconciliationIssueID BIGSERIAL    PRIMARY KEY,
    CompanyID             INTEGER      NOT NULL,
    BranchID              INTEGER      NOT NULL,
    PayrollPeriodID       INTEGER      NOT NULL,
    Severity              VARCHAR(30)  NOT NULL,
    IssueCode             VARCHAR(100) NOT NULL,
    IssueMessage          VARCHAR(1000) NOT NULL,
    EntityName            VARCHAR(200),
    EntityID              VARCHAR(100),
    IsResolved            BOOLEAN      NOT NULL DEFAULT FALSE,
    ResolvedByUserID      INTEGER,
    ResolvedAtUtc         TIMESTAMPTZ,
    CreatedAtUtc          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_ReconciliationIssues_Company  FOREIGN KEY (CompanyID)      REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_ReconciliationIssues_Branch   FOREIGN KEY (BranchID)       REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_ReconciliationIssues_Period   FOREIGN KEY (PayrollPeriodID) REFERENCES payroll.PayrollPeriods(PayrollPeriodID),
    CONSTRAINT fk_ReconciliationIssues_Resolver FOREIGN KEY (ResolvedByUserID) REFERENCES sec.Users(UserID)
);


-- =============================================================================
-- SECTION 4: IMPORT (import schema)
-- =============================================================================

CREATE TABLE import.ImportBatches (
    ImportBatchID       BIGSERIAL    PRIMARY KEY,
    CompanyID           INTEGER      NOT NULL,
    BranchID            INTEGER      NOT NULL,
    PayrollPeriodID     INTEGER,
    InputMethod         VARCHAR(30)  NOT NULL,
    DataKind            VARCHAR(50)  NOT NULL,
    BatchName           VARCHAR(200) NOT NULL,
    Status              VARCHAR(30)  NOT NULL DEFAULT 'Draft',
    PayrollPeriodStart  DATE,
    PayrollPeriodEnd    DATE,
    DetectedStartDate   DATE,
    DetectedEndDate     DATE,
    TotalRows           INTEGER      NOT NULL DEFAULT 0,
    ReadyRows           INTEGER      NOT NULL DEFAULT 0,
    ErrorRows           INTEGER      NOT NULL DEFAULT 0,
    WarningRows         INTEGER      NOT NULL DEFAULT 0,
    UploadedByUserID    INTEGER,
    UploadedAtUtc       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ConfirmedByUserID   INTEGER,
    ConfirmedAtUtc      TIMESTAMPTZ,
    Notes               TEXT,

    CONSTRAINT fk_ImportBatches_Company  FOREIGN KEY (CompanyID)      REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_ImportBatches_Branch   FOREIGN KEY (BranchID)       REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_ImportBatches_Period   FOREIGN KEY (PayrollPeriodID) REFERENCES payroll.PayrollPeriods(PayrollPeriodID),
    CONSTRAINT fk_ImportBatches_Uploader  FOREIGN KEY (UploadedByUserID)  REFERENCES sec.Users(UserID),
    CONSTRAINT fk_ImportBatches_Confirmer FOREIGN KEY (ConfirmedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE import.ImportFiles (
    ImportFileID      BIGSERIAL    PRIMARY KEY,
    ImportBatchID     BIGINT       NOT NULL,
    OriginalFileName  VARCHAR(260) NOT NULL,
    StoredFilePath    VARCHAR(1000),
    FileExtension     VARCHAR(20),
    MimeType          VARCHAR(200),
    FileSizeBytes     BIGINT,
    FileHashSha256    VARCHAR(100),
    ReadStatus        VARCHAR(30)  NOT NULL DEFAULT 'Pending',
    ReadMessage       VARCHAR(1000),
    UploadedAtUtc     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_ImportFiles_Batch FOREIGN KEY (ImportBatchID) REFERENCES import.ImportBatches(ImportBatchID)
);

CREATE TABLE import.ImportRows (
    ImportRowID          BIGSERIAL    PRIMARY KEY,
    ImportBatchID        BIGINT       NOT NULL,
    ImportFileID         BIGINT,
    SourceRowNumber      INTEGER      NOT NULL,
    RawDataJson          TEXT         NOT NULL,
    MappedDataJson       TEXT,
    DriverIdentifierRaw  VARCHAR(250),
    MatchedDriverID      INTEGER,
    MatchStatus          VARCHAR(30)  NOT NULL DEFAULT 'Unmatched',
    ValidationStatus     VARCHAR(30)  NOT NULL DEFAULT 'Pending',
    IsOutsidePayrollPeriod BOOLEAN    NOT NULL DEFAULT FALSE,
    ReadyForDraft        BOOLEAN      NOT NULL DEFAULT FALSE,
    UserDecision         VARCHAR(30),
    DecisionByUserID     INTEGER,
    DecisionAtUtc        TIMESTAMPTZ,
    Notes                TEXT,

    CONSTRAINT fk_ImportRows_Batch  FOREIGN KEY (ImportBatchID) REFERENCES import.ImportBatches(ImportBatchID),
    CONSTRAINT fk_ImportRows_File   FOREIGN KEY (ImportFileID)  REFERENCES import.ImportFiles(ImportFileID),
    CONSTRAINT fk_ImportRows_Driver FOREIGN KEY (MatchedDriverID) REFERENCES core.Drivers(DriverID),
    CONSTRAINT fk_ImportRows_Decider FOREIGN KEY (DecisionByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE import.ImportDetectedColumns (
    ImportDetectedColumnID BIGSERIAL   PRIMARY KEY,
    ImportFileID           BIGINT      NOT NULL,
    ColumnIndex            INTEGER     NOT NULL,
    OriginalColumnName     VARCHAR(250),
    NormalizedColumnName   VARCHAR(250),
    DetectedDataType       VARCHAR(30) NOT NULL,
    IsEmptyHeader          BOOLEAN     NOT NULL DEFAULT FALSE,
    IsDuplicateHeader      BOOLEAN     NOT NULL DEFAULT FALSE,
    IsSelected             BOOLEAN     NOT NULL DEFAULT TRUE,
    IgnoreReason           VARCHAR(500),

    CONSTRAINT uq_ImportDetectedColumns_File_Index UNIQUE (ImportFileID, ColumnIndex),
    CONSTRAINT fk_ImportDetectedColumns_File FOREIGN KEY (ImportFileID) REFERENCES import.ImportFiles(ImportFileID)
);

CREATE TABLE import.ImportMappingTemplates (
    ImportMappingTemplateID SERIAL       PRIMARY KEY,
    CompanyID               INTEGER      NOT NULL,
    BranchID                INTEGER,
    TemplateName            VARCHAR(150) NOT NULL,
    InputMethod             VARCHAR(30)  NOT NULL,
    DataKind                VARCHAR(50)  NOT NULL,
    SourceSystemName        VARCHAR(150),
    IsActive                BOOLEAN      NOT NULL DEFAULT TRUE,
    CreatedByUserID         INTEGER,
    CreatedAtUtc            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_ImportMappingTemplates_Company_Name UNIQUE (CompanyID, TemplateName),
    CONSTRAINT fk_ImportMappingTemplates_Company FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_ImportMappingTemplates_Branch  FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_ImportMappingTemplates_Creator FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE import.ImportMappingTemplateLines (
    ImportMappingTemplateLineID SERIAL  PRIMARY KEY,
    ImportMappingTemplateID     INTEGER NOT NULL,
    TargetField                 VARCHAR(100) NOT NULL,
    SourceColumnsJson           TEXT    NOT NULL,
    TransformRuleJson           TEXT,
    IsRequired                  BOOLEAN NOT NULL DEFAULT FALSE,

    CONSTRAINT fk_ImportMappingTemplateLines_Template
        FOREIGN KEY (ImportMappingTemplateID) REFERENCES import.ImportMappingTemplates(ImportMappingTemplateID)
);

CREATE TABLE import.ImportValidationIssues (
    ImportValidationIssueID BIGSERIAL    PRIMARY KEY,
    ImportBatchID           BIGINT       NOT NULL,
    ImportRowID             BIGINT,
    Severity                VARCHAR(30)  NOT NULL,
    IssueCode               VARCHAR(100) NOT NULL,
    IssueMessage            VARCHAR(1000) NOT NULL,
    ColumnName              VARCHAR(250),
    CreatedAtUtc            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ResolvedAtUtc           TIMESTAMPTZ,
    ResolvedByUserID        INTEGER,
    ResolutionNote          TEXT,

    CONSTRAINT fk_ImportValidationIssues_Batch    FOREIGN KEY (ImportBatchID) REFERENCES import.ImportBatches(ImportBatchID),
    CONSTRAINT fk_ImportValidationIssues_Row      FOREIGN KEY (ImportRowID)   REFERENCES import.ImportRows(ImportRowID),
    CONSTRAINT fk_ImportValidationIssues_Resolver FOREIGN KEY (ResolvedByUserID) REFERENCES sec.Users(UserID)
);


-- =============================================================================
-- SECTION 5: INTEGRATION (integration schema)
-- =============================================================================

CREATE TABLE integration.IntegrationConnections (
    IntegrationConnectionID SERIAL       PRIMARY KEY,
    CompanyID               INTEGER      NOT NULL,
    BranchID                INTEGER,
    ProviderCode            VARCHAR(50)  NOT NULL,
    ConnectionName          VARCHAR(150) NOT NULL,
    Status                  VARCHAR(30)  NOT NULL DEFAULT 'Planned',
    ExternalAccountID       VARCHAR(200),
    CredentialRef           VARCHAR(500),
    LastSyncAtUtc           TIMESTAMPTZ,
    CreatedByUserID         INTEGER,
    CreatedAtUtc            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    Notes                   TEXT,

    CONSTRAINT fk_IntegrationConnections_Company  FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_IntegrationConnections_Branch   FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_IntegrationConnections_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE integration.IntegrationSyncRuns (
    IntegrationSyncRunID    BIGSERIAL    PRIMARY KEY,
    IntegrationConnectionID INTEGER      NOT NULL,
    ImportBatchID           BIGINT,
    SyncType                VARCHAR(50)  NOT NULL,
    Status                  VARCHAR(30)  NOT NULL DEFAULT 'Started',
    StartedAtUtc            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    FinishedAtUtc           TIMESTAMPTZ,
    RowsPulled              INTEGER      NOT NULL DEFAULT 0,
    ErrorMessage            TEXT,

    CONSTRAINT fk_IntegrationSyncRuns_Connection FOREIGN KEY (IntegrationConnectionID)
        REFERENCES integration.IntegrationConnections(IntegrationConnectionID),
    CONSTRAINT fk_IntegrationSyncRuns_Batch FOREIGN KEY (ImportBatchID)
        REFERENCES import.ImportBatches(ImportBatchID)
);


-- =============================================================================
-- SECTION 6: REVIEW (review schema)
-- =============================================================================

CREATE TABLE review.ManagerReviewItems (
    ReviewItemID            BIGSERIAL    PRIMARY KEY,
    CompanyID               INTEGER      NOT NULL,
    BranchID                INTEGER      NOT NULL,
    RequestedByUserID       INTEGER,
    RequestType             VARCHAR(50)  NOT NULL,
    EntitySchema            VARCHAR(50),
    EntityName              VARCHAR(100),
    EntityID                VARCHAR(100),
    Title                   VARCHAR(250) NOT NULL,
    Description             TEXT,
    OldValueJson            TEXT,
    NewValueJson            TEXT,
    Status                  VARCHAR(30)  NOT NULL DEFAULT 'Pending',
    Priority                VARCHAR(30)  NOT NULL DEFAULT 'Normal',
    CreatedAtUtc            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    DueAtUtc                TIMESTAMPTZ,
    FinalDecisionByUserID   INTEGER,
    FinalDecisionAtUtc      TIMESTAMPTZ,
    FinalDecisionReason     VARCHAR(1000),

    CONSTRAINT fk_ReviewItems_Company    FOREIGN KEY (CompanyID)  REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_ReviewItems_Branch     FOREIGN KEY (BranchID)   REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_ReviewItems_Requester  FOREIGN KEY (RequestedByUserID)    REFERENCES sec.Users(UserID),
    CONSTRAINT fk_ReviewItems_Decider    FOREIGN KEY (FinalDecisionByUserID) REFERENCES sec.Users(UserID)
);

CREATE TABLE review.ManagerReviewDecisions (
    ReviewDecisionID  BIGSERIAL   PRIMARY KEY,
    ReviewItemID      BIGINT      NOT NULL,
    DecidedByUserID   INTEGER     NOT NULL,
    Decision          VARCHAR(30) NOT NULL,
    DecisionReason    TEXT,
    CreatedAtUtc      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_ReviewDecisions_Item    FOREIGN KEY (ReviewItemID)    REFERENCES review.ManagerReviewItems(ReviewItemID),
    CONSTRAINT fk_ReviewDecisions_Decider FOREIGN KEY (DecidedByUserID) REFERENCES sec.Users(UserID)
);


-- =============================================================================
-- SECTION 7: AUDIT (audit schema)
-- =============================================================================

CREATE TABLE audit.AuditLog (
    AuditID         BIGSERIAL   PRIMARY KEY,
    CompanyID       INTEGER,
    BranchID        INTEGER,
    ActorUserID     INTEGER,
    ActionCode      VARCHAR(100) NOT NULL,
    EntitySchema    VARCHAR(50),
    EntityName      VARCHAR(100),
    EntityID        VARCHAR(100),
    OldValueJson    TEXT,
    NewValueJson    TEXT,
    Reason          TEXT,
    SourceType      VARCHAR(50)  NOT NULL DEFAULT 'Application',
    IPAddress       VARCHAR(50),
    UserAgent       TEXT,
    CreatedAtUtc    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CorrelationID   UUID         NOT NULL DEFAULT gen_random_uuid()
);

CREATE INDEX ix_AuditLog_Company_Branch_Date  ON audit.AuditLog (CompanyID, BranchID, CreatedAtUtc DESC);
CREATE INDEX ix_AuditLog_Actor_Created        ON audit.AuditLog (ActorUserID, CreatedAtUtc DESC);
CREATE INDEX ix_AuditLog_Entity               ON audit.AuditLog (EntitySchema, EntityName, EntityID);

-- core indexes
CREATE INDEX ix_Branches_Company_Status
    ON core.Branches (CompanyID, Status, IsDefault);

CREATE INDEX ix_DriverAliases_Company_Alias
    ON core.DriverAliases (CompanyID, AliasType, AliasValue, IsActive);

CREATE UNIQUE INDEX ux_Drivers_Company_DriverCode
    ON core.Drivers (CompanyID, DriverCode)
    WHERE DriverCode IS NOT NULL;

CREATE UNIQUE INDEX ux_Employees_Company_EmployeeKey
    ON core.Employees (CompanyID, EmployeeKey)
    WHERE EmployeeKey IS NOT NULL;

-- import indexes
CREATE INDEX ix_ImportRows_Batch_Status
    ON import.ImportRows (ImportBatchID, ValidationStatus, MatchStatus, ReadyForDraft);

-- payroll.BranchPayItemConfig indexes
CREATE INDEX ix_BranchPayItemConfig_Branch_Item_Dates
    ON payroll.BranchPayItemConfig (CompanyID, BranchID, PayItemID, EffectiveFrom)
    INCLUDE (IsActive, EffectiveTo, Notes);

CREATE UNIQUE INDEX uix_BranchPayItemConfig_OpenVersion
    ON payroll.BranchPayItemConfig (CompanyID, BranchID, PayItemID)
    WHERE EffectiveTo IS NULL;

-- payroll.PayItemLineTypeMap indexes
CREATE INDEX ix_PayItemLineTypeMap_LineType_Context_Status
    ON payroll.PayItemLineTypeMap (LineType, SourceContext, Status);

CREATE INDEX ix_PayItemLineTypeMap_PayItem_Status
    ON payroll.PayItemLineTypeMap (PayItemID, Status);

-- payroll.PayItemRateTypeMap indexes
CREATE INDEX ix_PayItemRateTypeMap_PayItem_Status
    ON payroll.PayItemRateTypeMap (PayItemID, Status);

CREATE INDEX ix_PayItemRateTypeMap_RateType_Status
    ON payroll.PayItemRateTypeMap (RateTypeID, Status);

-- payroll.PayItems indexes
CREATE INDEX ix_PayItems_Company_Branch_Status_Sort
    ON payroll.PayItems (CompanyID, BranchID, Status, SortOrder);

CREATE INDEX ix_PayItems_Entry_Status_Sort
    ON payroll.PayItems (AppearsInPayrollEntry, Status, SortOrder);

CREATE INDEX ix_PayItems_PayItemCode
    ON payroll.PayItems (PayItemCode);

CREATE INDEX ix_PayItems_Status_System_Sort
    ON payroll.PayItems (Status, IsSystemStandard, SortOrder);

CREATE UNIQUE INDEX ux_PayItems_Global_PayItemCode
    ON payroll.PayItems (PayItemCode)
    WHERE CompanyID IS NULL;

-- payroll.PayItemSettings indexes
CREATE INDEX ix_PayItemSettings_Company_Branch_Status
    ON payroll.PayItemSettings (CompanyID, BranchID, Status);

CREATE INDEX ix_PayItemSettings_PayItem_Status
    ON payroll.PayItemSettings (PayItemID, Status);

CREATE UNIQUE INDEX ux_PayItemSettings_Active_Item_Scope_Key
    ON payroll.PayItemSettings (PayItemID, CompanyID, BranchID, SettingKey)
    WHERE Status = 'Active';

-- payroll.PayProfilePayItems indexes
CREATE INDEX ix_PayProfilePayItems_Profile_Status_Sort
    ON payroll.PayProfilePayItems (PayProfileID, Status, SortOrder);

CREATE INDEX ix_PayProfilePayItems_RateType
    ON payroll.PayProfilePayItems (RateTypeID);

-- payroll.PayProfileRates indexes
CREATE INDEX ix_PayProfileRates_Profile_RateType_Dates_Status
    ON payroll.PayProfileRates (PayProfileID, RateTypeID, EffectiveFrom, EffectiveTo, Status);

CREATE INDEX ix_PayProfileRates_RateType_Status
    ON payroll.PayProfileRates (RateTypeID, Status);

-- payroll.PayProfiles indexes
CREATE INDEX ix_PayProfiles_Company_Branch_Status
    ON payroll.PayProfiles (CompanyID, BranchID, Status);

CREATE UNIQUE INDEX ux_PayProfiles_Company_Branch_ProfileCode
    ON payroll.PayProfiles (CompanyID, BranchID, ProfileCode)
    WHERE BranchID IS NOT NULL;

CREATE UNIQUE INDEX ux_PayProfiles_Company_ProfileCode_CompanyWide
    ON payroll.PayProfiles (CompanyID, ProfileCode)
    WHERE BranchID IS NULL;

-- payroll.PayrollDraftLines indexes
CREATE INDEX ix_PayrollDraftLines_Period_Driver
    ON payroll.PayrollDraftLines (PayrollPeriodID, DriverID, WorkDate, LineType, Status);

-- payroll.PayrollFinalLines indexes
CREATE INDEX ix_PayrollFinalLines_Period_Driver
    ON payroll.PayrollFinalLines (PayrollPeriodID, DriverID, WorkDate, LineType);

-- Prevent a draft line from being finalized into the same period twice.
-- Belt-and-suspenders on top of the service-level atomic claim.
CREATE UNIQUE INDEX ux_PayrollFinalLines_Period_DraftLine
    ON payroll.PayrollFinalLines (PayrollPeriodID, DraftLineID)
    WHERE DraftLineID IS NOT NULL;

-- payroll.PayrollPeriods partial/filtered indexes
CREATE UNIQUE INDEX ux_PayrollPeriods_OneDraftPerBranch
    ON payroll.PayrollPeriods (CompanyID, BranchID)
    WHERE Status = 'Draft';

CREATE UNIQUE INDEX ux_PayrollPeriods_OneOpenPerBranch
    ON payroll.PayrollPeriods (CompanyID, BranchID)
    WHERE Status = 'Open';

CREATE UNIQUE INDEX ux_PayrollPeriods_PeriodCode_Active
    ON payroll.PayrollPeriods (CompanyID, BranchID, PeriodCode)
    WHERE Status <> 'Cancelled';

-- payroll.PayrollRunBonuses indexes
CREATE INDEX ix_PayrollRunBonuses_Company_Branch_Status
    ON payroll.PayrollRunBonuses (CompanyID, BranchID, Status);

CREATE INDEX ix_PayrollRunBonuses_CreatedAtUtc
    ON payroll.PayrollRunBonuses (CreatedAtUtc);

CREATE INDEX ix_PayrollRunBonuses_Driver_Status
    ON payroll.PayrollRunBonuses (DriverID, Status);

CREATE INDEX ix_PayrollRunBonuses_Employee_Status
    ON payroll.PayrollRunBonuses (EmployeeID, Status);

CREATE INDEX ix_PayrollRunBonuses_MinimumPayComparison
    ON payroll.PayrollRunBonuses (IncludeInMinimumPayComparison, Status);

CREATE INDEX ix_PayrollRunBonuses_PayItem_Status
    ON payroll.PayrollRunBonuses (PayItemID, Status);

CREATE INDEX ix_PayrollRunBonuses_Period_Status
    ON payroll.PayrollRunBonuses (PayrollPeriodID, Status);

-- payroll.PayrollStatusKeys indexes
CREATE INDEX ix_PayrollStatusKeys_BranchActiveOrder
    ON payroll.PayrollStatusKeys (CompanyID, BranchID, IsActive, DisplayOrder, StatusCode);

CREATE UNIQUE INDEX ux_PayrollStatusKeys_ActiveCode
    ON payroll.PayrollStatusKeys (CompanyID, BranchID, NormalizedStatusCode)
    WHERE IsActive = TRUE;

-- payroll.PersonPayProfileAssignments indexes
CREATE INDEX ix_PersonPayProfileAssignments_Company_Branch_Status
    ON payroll.PersonPayProfileAssignments (CompanyID, BranchID, Status);

CREATE INDEX ix_PersonPayProfileAssignments_Employee_Dates_Status
    ON payroll.PersonPayProfileAssignments (EmployeeID, EffectiveFrom, EffectiveTo, Status);

CREATE INDEX ix_PersonPayProfileAssignments_Profile_Status
    ON payroll.PersonPayProfileAssignments (PayProfileID, Status);

-- review indexes
CREATE INDEX ix_ManagerReviewItems_Branch_Status
    ON review.ManagerReviewItems (CompanyID, BranchID, Status, CreatedAtUtc DESC);

-- sec indexes
CREATE INDEX ix_UserBranchRoles_Branch_Active
    ON sec.UserBranchRoles (CompanyID, BranchID, IsActive);

CREATE INDEX ix_UserBranchRoles_User_Active
    ON sec.UserBranchRoles (UserID, IsActive, CompanyID, BranchID, RoleID);

CREATE UNIQUE INDEX ux_Users_Company_Username
    ON sec.Users (CompanyID, Username);


-- =============================================================================
-- SECTION 9a: CHECK CONSTRAINTS
-- =============================================================================

ALTER TABLE sec.Users
    ADD CONSTRAINT ck_Users_CompanyRequired
    CHECK (CompanyID IS NOT NULL OR Username LIKE 'sysadmin%');

ALTER TABLE import.ImportBatches
    ADD CONSTRAINT ck_ImportBatches_Status
    CHECK (Status IN ('Draft','Pending','Matching','Ready','Confirmed','Committed','Cancelled','Error'));

ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT ck_DriverRates_Status
    CHECK (Status IN ('PendingApproval','Approved','Superseded','Voided'));

ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT ck_PayrollPeriods_Dates
    CHECK (EndDate >= StartDate);

ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT ck_PayrollPeriods_Status
    CHECK (Status IN ('Draft','Open','InReview','Approved','Locked','Cancelled','Archived'));

ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT ck_PayrollPeriods_Type
    CHECK (PeriodType IN ('Week','Biweek','Month','Custom'));

ALTER TABLE payroll.PayrollReconciliationIssues
    ADD CONSTRAINT ck_ReconIssues_Severity
    CHECK (Severity IN ('Blocker','Error','Warning','Info'));

ALTER TABLE payroll.PayrollRunBonuses
    ADD CONSTRAINT ck_PayrollRunBonuses_Amount
    CHECK (Amount > 0);

ALTER TABLE payroll.PayrollRunBonuses
    ADD CONSTRAINT ck_PayrollRunBonuses_Status
    CHECK (Status IN ('Pending','Approved','Voided','Finalized'));

ALTER TABLE payroll.PayrollRunBonuses
    ADD CONSTRAINT ck_PayrollRunBonuses_VoidConsistency
    CHECK (
        (Status = 'Voided' AND VoidedAtUtc IS NOT NULL)
        OR (Status <> 'Voided' AND VoidedAtUtc IS NULL)
    );

ALTER TABLE payroll.PayrollStatusKeys
    ADD CONSTRAINT ck_PayrollStatusKeys_HoursValue
    CHECK (HoursValue >= 0 AND HoursValue <= 24);

ALTER TABLE payroll.PayrollStatusKeys
    ADD CONSTRAINT ck_PayrollStatusKeys_NormalizedCode
    CHECK (NormalizedStatusCode = UPPER(TRIM(StatusCode)));

ALTER TABLE payroll.PayrollStatusKeys
    ADD CONSTRAINT ck_PayrollStatusKeys_AllowanceCategoryRequired
    CHECK (DeductsFromYearlyAllowance = FALSE OR NULLIF(TRIM(AllowanceCategory), '') IS NOT NULL);

ALTER TABLE payroll.PayrollStatusKeys
    ADD CONSTRAINT ck_PayrollStatusKeys_AllowanceRequiresOffReason
    CHECK (DeductsFromYearlyAllowance = FALSE OR IsOffReason = TRUE);

ALTER TABLE payroll.PersonPayProfileAssignments
    ADD CONSTRAINT ck_PersonPayProfileAssignments_Dates
    CHECK (EffectiveTo IS NULL OR EffectiveTo >= EffectiveFrom);

ALTER TABLE payroll.PersonPayProfileAssignments
    ADD CONSTRAINT ck_PersonPayProfileAssignments_Status
    CHECK (Status IN ('Active','Ended','Voided'));

ALTER TABLE review.ManagerReviewDecisions
    ADD CONSTRAINT ck_ManagerReviewDecisions_Decision
    CHECK (Decision IN ('Approved','Rejected','EditRequested','Comment'));

ALTER TABLE review.ManagerReviewItems
    ADD CONSTRAINT ck_ManagerReviewItems_Priority
    CHECK (Priority IN ('Low','Normal','High','Urgent'));

ALTER TABLE review.ManagerReviewItems
    ADD CONSTRAINT ck_ManagerReviewItems_Status
    CHECK (Status IN ('Pending','Approved','Rejected','EditRequested','Cancelled'));

ALTER TABLE review.ManagerReviewItems
    ADD CONSTRAINT ck_ManagerReviewItems_RequestType
    CHECK (RequestType IN (
        'DriverRateChange','PayrollDraftChange','PayrollAdjustment',
        'ImportConfirmation','Correction','PeriodApproval','Override','Other'
    ));


-- =============================================================================
-- SECTION 8: APPLICATION VIEWS (app schema)
-- =============================================================================

CREATE OR REPLACE VIEW app.vw_UserBranchAccess AS
SELECT
    u.UserID,
    u.Username,
    u.DisplayName,
    c.CompanyID,
    c.CompanyCode,
    c.CompanyName,
    ubr.ScopeType,
    b.BranchID,
    b.BranchCode,
    b.BranchName,
    r.RoleID,
    r.RoleCode,
    r.RoleName,
    u.IsActive             AS UserIsActive,
    u.CanLogin,
    c.Status               AS CompanyStatus,
    c.IsSuspended          AS CompanyIsSuspended,
    b.Status               AS BranchStatus,
    ubr.IsActive           AS AccessIsActive
FROM sec.UserBranchRoles AS ubr
INNER JOIN sec.Users AS u      ON u.UserID    = ubr.UserID
INNER JOIN core.Companies AS c ON c.CompanyID = ubr.CompanyID
LEFT  JOIN core.Branches AS b  ON b.BranchID  = ubr.BranchID
INNER JOIN sec.Roles AS r      ON r.RoleID    = ubr.RoleID;

CREATE OR REPLACE VIEW app.vw_ActiveDrivers AS
SELECT
    d.CompanyID,
    d.BranchID,
    b.BranchName,
    d.DriverID,
    e.EmployeeID,
    e.EmployeeKey,
    e.FullName,
    e.PrimaryPhone,
    d.DriverCode,
    d.CDLNumber,
    d.ExternalDriverID,
    d.DriverStatus
FROM core.Drivers AS d
INNER JOIN core.Employees AS e ON e.EmployeeID = d.EmployeeID
INNER JOIN core.Branches  AS b ON b.BranchID   = d.BranchID
WHERE d.DriverStatus      = 'Active'
  AND e.EmploymentStatus  = 'Active';

CREATE OR REPLACE VIEW app.vw_HomeSummary AS
SELECT
    c.CompanyID,
    b.BranchID,
    b.BranchName,
    (
        SELECT p.PeriodName
        FROM payroll.PayrollPeriods p
        WHERE p.BranchID = b.BranchID
          AND p.Status IN ('Open', 'InReview', 'Approved')
        ORDER BY p.StartDate DESC
        LIMIT 1
    ) AS OpenPayrollPeriod,
    (SELECT COUNT(*) FROM app.vw_ActiveDrivers d WHERE d.BranchID = b.BranchID) AS ActiveDrivers,
    (SELECT COUNT(*) FROM review.ManagerReviewItems r WHERE r.BranchID = b.BranchID AND r.Status = 'Pending') AS PendingManagerReviews,
    (
        SELECT ib.Status
        FROM import.ImportBatches ib
        WHERE ib.BranchID = b.BranchID
        ORDER BY ib.UploadedAtUtc DESC
        LIMIT 1
    ) AS LastImportStatus,
    CASE WHEN EXISTS (
        SELECT 1 FROM review.ManagerReviewItems r
        WHERE r.BranchID = b.BranchID AND r.Status = 'Pending'
    ) THEN 'Needs Manager Review' ELSE 'All Clear' END AS AuditStatus
FROM core.Companies AS c
JOIN core.Branches AS b ON b.CompanyID = c.CompanyID
WHERE c.Status      = 'Active'
  AND c.IsSuspended = FALSE
  AND b.Status      = 'Active';

CREATE OR REPLACE VIEW app.vw_PayrollPeriodList AS
SELECT
    p.CompanyID,
    p.BranchID,
    b.BranchName,
    p.PayrollPeriodID,
    p.ParentPayrollPeriodID,
    p.PeriodCode,
    p.PeriodName,
    p.PeriodType,
    p.StartDate,
    p.EndDate,
    p.Status,
    COALESCE(dl_agg.DraftDrivers,              0) AS DraftDrivers,
    COALESCE(dl_agg.DraftLines,                0) AS DraftLines,
    COALESCE(dl_agg.DraftLinesNeedingAttention,0) AS DraftLinesNeedingAttention,
    COALESCE(fl_agg.FinalLines,                0) AS FinalLines
FROM payroll.PayrollPeriods AS p
JOIN core.Branches AS b ON b.BranchID = p.BranchID
LEFT JOIN (
    SELECT
        PayrollPeriodID,
        COUNT(DISTINCT DriverID)                                                                           AS DraftDrivers,
        COUNT(DraftLineID)                                                                                 AS DraftLines,
        SUM(CASE WHEN Status IN ('NeedsReview','Rejected') OR NeedsManagerReview THEN 1 ELSE 0 END)       AS DraftLinesNeedingAttention
    FROM payroll.PayrollDraftLines
    GROUP BY PayrollPeriodID
) AS dl_agg ON dl_agg.PayrollPeriodID = p.PayrollPeriodID
LEFT JOIN (
    SELECT
        PayrollPeriodID,
        COUNT(FinalLineID) AS FinalLines
    FROM payroll.PayrollFinalLines
    GROUP BY PayrollPeriodID
) AS fl_agg ON fl_agg.PayrollPeriodID = p.PayrollPeriodID;

CREATE OR REPLACE VIEW app.vw_PayrollDraftSummary AS
SELECT
    dl.CompanyID,
    dl.BranchID,
    dl.PayrollPeriodID,
    p.PeriodName,
    dl.DriverID,
    e.FullName                                                                                         AS DriverName,
    dl.LineType,
    SUM(dl.Quantity)                                                                                   AS TotalQuantity,
    SUM(COALESCE(dl.CalculatedAmount, 0))                                                              AS TotalCalculatedAmount,
    COUNT(*)                                                                                           AS LineCount,
    SUM(CASE WHEN dl.Status IN ('NeedsReview', 'Rejected') OR dl.NeedsManagerReview THEN 1 ELSE 0 END) AS LinesNeedingAttention
FROM payroll.PayrollDraftLines AS dl
JOIN payroll.PayrollPeriods AS p ON p.PayrollPeriodID = dl.PayrollPeriodID
JOIN core.Drivers            AS d ON d.DriverID        = dl.DriverID
JOIN core.Employees          AS e ON e.EmployeeID      = d.EmployeeID
GROUP BY dl.CompanyID, dl.BranchID, dl.PayrollPeriodID, p.PeriodName,
         dl.DriverID, e.FullName, dl.LineType;

CREATE OR REPLACE VIEW app.vw_ImportBatchSummary AS
SELECT
    ib.CompanyID,
    ib.BranchID,
    b.BranchName,
    ib.ImportBatchID,
    ib.PayrollPeriodID,
    ib.InputMethod,
    ib.DataKind,
    ib.BatchName,
    ib.Status,
    ib.TotalRows,
    ib.ReadyRows,
    ib.ErrorRows,
    ib.WarningRows,
    ib.DetectedStartDate,
    ib.DetectedEndDate,
    ib.UploadedAtUtc,
    u.DisplayName AS UploadedBy
FROM import.ImportBatches AS ib
JOIN core.Branches AS b  ON b.BranchID = ib.BranchID
LEFT JOIN sec.Users AS u ON u.UserID   = ib.UploadedByUserID;

CREATE OR REPLACE VIEW app.vw_ManagerReviewQueue AS
SELECT
    m.CompanyID,
    m.BranchID,
    b.BranchName,
    m.ReviewItemID,
    m.RequestType,
    m.EntitySchema,
    m.EntityName,
    m.EntityID,
    m.Title,
    m.Status,
    m.Priority,
    m.CreatedAtUtc,
    ru.DisplayName AS RequestedBy,
    du.DisplayName AS FinalDecisionBy,
    m.FinalDecisionAtUtc,
    m.FinalDecisionReason
FROM review.ManagerReviewItems AS m
JOIN core.Branches AS b          ON b.BranchID  = m.BranchID
LEFT JOIN sec.Users AS ru        ON ru.UserID   = m.RequestedByUserID
LEFT JOIN sec.Users AS du        ON du.UserID   = m.FinalDecisionByUserID;


-- =============================================================================
-- SECTION 9: FUNCTIONS (PL/pgSQL equivalents)
-- =============================================================================

CREATE OR REPLACE FUNCTION sec.fn_UserCanAccessBranch(
    p_UserID    INTEGER,
    p_CompanyID INTEGER,
    p_BranchID  INTEGER
) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1
        FROM sec.UserBranchRoles AS ubr
        WHERE ubr.UserID    = p_UserID
          AND ubr.CompanyID = p_CompanyID
          AND ubr.IsActive  = TRUE
          AND (
              ubr.ScopeType = 'AllCompanyBranches'
           OR ubr.BranchID  = p_BranchID
          )
    );
END;
$$;

CREATE OR REPLACE FUNCTION sec.fn_UserHasPermission(
    p_UserID         INTEGER,
    p_CompanyID      INTEGER,
    p_BranchID       INTEGER,
    p_PermissionCode VARCHAR(100)
) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1
        FROM sec.UserBranchRoles AS ubr
        INNER JOIN sec.RolePermissions AS rp ON rp.RoleID = ubr.RoleID
        INNER JOIN sec.Permissions AS perm   ON perm.PermissionID = rp.PermissionID
        WHERE ubr.UserID          = p_UserID
          AND ubr.CompanyID       = p_CompanyID
          AND ubr.IsActive        = TRUE
          AND perm.PermissionCode = p_PermissionCode
          AND (
              ubr.ScopeType = 'AllCompanyBranches'
           OR ubr.BranchID  = p_BranchID
          )
    );
END;
$$;


-- =============================================================================
-- END OF SCHEMA
-- =============================================================================
