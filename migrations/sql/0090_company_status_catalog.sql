-- 0090: Company Status catalog and direct Status compensation.
--
-- Historical migrations remain immutable. A Status key is now owned by the Company, not by a
-- Branch: one catalog (names, aliases, Category, hours, off semantic, rate column binding) is
-- shared by every Branch. Usage limits move to a Branch x Status key table. A Status rate
-- column is owned by the Company and owns one scalar RateDefinition, so a Driver's Status rate
-- values are Driver-owned rate schedules like any other earning, and a permanent transfer needs
-- no copy.
--
-- This pre-production cutover keeps no Branch-owned Status model: the migration refuses to run
-- while a Status key, a Status selection, a calculation snapshot, a final line or a
-- STATUS_PAYMENT draft line exists, because none of them can be reinterpreted or backfilled.
-- Reset and reseed the development database, then apply the migrations again. The check runs
-- before any structural statement and the migration is one transaction, so a refusal leaves
-- the database exactly as it was. The default Status rate column each Branch used to have is
-- replaced by one default column per Company.

DO $preflight$
DECLARE
    v_Count BIGINT;
BEGIN
    SELECT (SELECT COUNT(*) FROM payroll.PayrollStatusKeys)
         + (SELECT COUNT(*) FROM payroll.PayrollPeriodDriverDayEntryState WHERE StatusKeyID IS NOT NULL)
         + (SELECT COUNT(*) FROM payroll.PayrollCalculationSnapshots)
         + (SELECT COUNT(*) FROM payroll.PayrollFinalLines)
         + (SELECT COUNT(*) FROM payroll.PayrollDraftLines WHERE SourceID LIKE 'STATUS_PAYMENT:%')
    INTO v_Count;

    IF v_Count > 0 THEN
        RAISE EXCEPTION
            'STATUS_MODEL_REBUILD_REQUIRED: % Status key, Status selection, calculation snapshot, final line or STATUS_PAYMENT draft line row(s) exist from the Branch-owned Status model. '
            'They cannot be reinterpreted or backfilled. Reset and reseed this preproduction database, then apply migrations again.',
            v_Count
            USING ERRCODE = 'check_violation';
    END IF;
END
$preflight$;

-- ---------------------------------------------------------------------------
-- 1. Release everything that keyed a Status rate column or key by Branch
-- ---------------------------------------------------------------------------

-- Retired: a key was tied to the Branch of its rate column. Both are now Company-owned and
-- bound by a composite foreign key.
DROP TRIGGER trg_psk_src_branch ON payroll.PayrollStatusKeys;
DROP FUNCTION payroll.trg_fn_psk_src_branch();
DROP TRIGGER trg_src_ratetype_owner ON payroll.StatusRateColumns;
DROP FUNCTION payroll.trg_fn_src_ratetype_owner();

ALTER TABLE payroll.PayrollPeriodDriverDayEntryState
    DROP CONSTRAINT fk_ppdes_statuskey;
ALTER TABLE payroll.PayrollStatusKeys
    DROP CONSTRAINT fk_psk_statusratecolumn;
ALTER TABLE payroll.RateDefinitions
    DROP CONSTRAINT fk_RateDefinitions_StatusRateColumn_Company_Branch,
    DROP CONSTRAINT ck_RateDefinitions_OwnerBranch;
ALTER TABLE payroll.PayrollCalculationSnapshotComponents
    DROP CONSTRAINT fk_PayrollCalculationSnapshotComponents_StatusRateColumn;

-- The unreferenced Branch-owned columns are replaced, never migrated. Their Status-owned
-- RateDefinitions cannot hold a schedule (a schedule required a PayDefinition) and none can
-- be referenced by evidence (the preflight proved there is none).
DELETE FROM payroll.RateComponentDefinitions
WHERE RateDefinitionID IN (SELECT RateDefinitionID FROM payroll.RateDefinitions
                           WHERE StatusRateColumnID IS NOT NULL);
DELETE FROM payroll.RateDefinitions WHERE StatusRateColumnID IS NOT NULL;
DELETE FROM payroll.StatusRateColumns;

-- ---------------------------------------------------------------------------
-- 2. Status rate columns: Company-owned
-- ---------------------------------------------------------------------------

DROP INDEX payroll.ix_StatusRateColumns_Branch;
DROP INDEX payroll.ux_StatusRateColumns_BranchDefault;
DROP INDEX payroll.ux_SRC_NormalizedName;
DROP INDEX payroll.ux_SRC_RateType_Branch;
DROP INDEX payroll.ux_StatusRateColumns_ID_Company_Branch;

ALTER TABLE payroll.StatusRateColumns
    DROP COLUMN BranchID,
    DROP COLUMN RateTypeID;

ALTER TABLE payroll.StatusRateColumns
    ALTER COLUMN NormalizedColumnName DROP DEFAULT;

ALTER TABLE payroll.StatusRateColumns
    ADD CONSTRAINT uq_StatusRateColumns_ID_Company UNIQUE (StatusRateColumnID, CompanyID),
    ADD CONSTRAINT ck_StatusRateColumns_Name
        CHECK (btrim(ColumnName) <> ''
               AND NormalizedColumnName <> ''
               AND NormalizedColumnName = regexp_replace(btrim(NormalizedColumnName), '\s+', ' ', 'g')),
    -- A default column is always an active column.
    ADD CONSTRAINT ck_StatusRateColumns_DefaultActive
        CHECK (NOT IsDefault OR IsActive);

CREATE INDEX ix_StatusRateColumns_Company
    ON payroll.StatusRateColumns (CompanyID, IsActive);
-- At most one active default column per Company.
CREATE UNIQUE INDEX ux_StatusRateColumns_CompanyDefault
    ON payroll.StatusRateColumns (CompanyID)
    WHERE IsDefault AND IsActive;
-- No two active columns of a Company share a normalized name.
CREATE UNIQUE INDEX ux_StatusRateColumns_ActiveName
    ON payroll.StatusRateColumns (CompanyID, NormalizedColumnName)
    WHERE IsActive;

-- ---------------------------------------------------------------------------
-- 3. Status RateDefinitions follow their Company-owned column
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.RateDefinitions
    DROP COLUMN OwnerBranchID;

ALTER TABLE payroll.RateDefinitions
    ADD CONSTRAINT fk_RateDefinitions_StatusRateColumn_Company
        FOREIGN KEY (StatusRateColumnID, CompanyID)
        REFERENCES payroll.StatusRateColumns (StatusRateColumnID, CompanyID),
    -- The key a Driver rate schedule needs to bind itself to a Status-owned definition.
    ADD CONSTRAINT uq_RateDefinitions_ID_StatusRateColumn
        UNIQUE (RateDefinitionID, StatusRateColumnID);

CREATE OR REPLACE FUNCTION payroll.trg_ratedefinitions_beforeupdate()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.CompanyID IS DISTINCT FROM OLD.CompanyID
       OR NEW.PayDefinitionID IS DISTINCT FROM OLD.PayDefinitionID
       OR NEW.StatusRateColumnID IS DISTINCT FROM OLD.StatusRateColumnID THEN
        RAISE EXCEPTION 'RATE_DEFINITION_OWNER_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.StructureLockedAtUtc IS DISTINCT FROM OLD.StructureLockedAtUtc THEN
        IF OLD.StructureLockedAtUtc IS NOT NULL THEN
            RAISE EXCEPTION 'RATE_STRUCTURE_LOCK_IMMUTABLE'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.Shape IS DISTINCT FROM OLD.Shape THEN
            RAISE EXCEPTION 'RATE_STRUCTURE_LOCK_WITH_SHAPE_CHANGE'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.Shape IS DISTINCT FROM OLD.Shape THEN
        PERFORM payroll.fn_AssertRateStructureMutable(OLD.RateDefinitionID);
    END IF;
    RETURN NEW;
END
$function$;

-- ---------------------------------------------------------------------------
-- 4. Driver rate schedules: one owner, a PayDefinition or a Status rate column
-- ---------------------------------------------------------------------------
--
-- A schedule used to require a PayDefinition, so a Status-owned RateDefinition could never
-- have one. The owner is now exactly one of the two, each bound to the RateDefinition by its
-- own composite key (MATCH SIMPLE: the key of the owner the schedule does not have is NULL
-- and is skipped). The exclusion constraint and the pending/approved unique indexes are keyed
-- by Driver and RateDefinition and apply to either owner unchanged. The structure lock, the
-- assertion functions and the lifecycle triggers are keyed by RateDefinitionID and so already
-- serve either owner; only the identity guard below names the owner columns.

ALTER TABLE payroll.DriverRateSchedules
    ALTER COLUMN PayDefinitionID DROP NOT NULL,
    ADD COLUMN StatusRateColumnID INTEGER;

ALTER TABLE payroll.DriverRateSchedules
    ADD CONSTRAINT ck_DriverRateSchedules_SingleOwner
        CHECK (num_nonnulls(PayDefinitionID, StatusRateColumnID) = 1),
    ADD CONSTRAINT fk_DriverRateSchedules_RateDefinition_StatusRateColumn
        FOREIGN KEY (RateDefinitionID, StatusRateColumnID)
        REFERENCES payroll.RateDefinitions (RateDefinitionID, StatusRateColumnID);

CREATE OR REPLACE FUNCTION payroll.trg_driverrateschedules_beforeupdate()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_CurrencyCode VARCHAR(3);
BEGIN
    IF NEW.CompanyID IS DISTINCT FROM OLD.CompanyID
       OR NEW.DriverID IS DISTINCT FROM OLD.DriverID
       OR NEW.RateDefinitionID IS DISTINCT FROM OLD.RateDefinitionID
       OR NEW.PayDefinitionID IS DISTINCT FROM OLD.PayDefinitionID
       OR NEW.StatusRateColumnID IS DISTINCT FROM OLD.StatusRateColumnID THEN
        RAISE EXCEPTION 'RATE_SCHEDULE_IDENTITY_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    PERFORM payroll.fn_LockRateDefinitionStructure(OLD.RateDefinitionID);

    IF OLD.Status = 'Pending' THEN
        IF NEW.Status = 'Pending' THEN
            RETURN NEW;
        END IF;
        IF NEW.Status <> 'Approved' THEN
            RAISE EXCEPTION 'RATE_SCHEDULE_INVALID_TRANSITION'
                USING ERRCODE = 'check_violation',
                      DETAIL = format('%s -> %s', OLD.Status, NEW.Status);
        END IF;

        -- Approval: Company currency gate, complete structure, complete value set.
        PERFORM 1 FROM core.Companies c
         WHERE c.CompanyID = NEW.CompanyID
           FOR SHARE;
        SELECT c.CurrencyCode INTO v_CurrencyCode
          FROM core.Companies c
         WHERE c.CompanyID = NEW.CompanyID;
        IF v_CurrencyCode IS NULL THEN
            RAISE EXCEPTION 'COMPANY_CURRENCY_REQUIRED'
                USING ERRCODE = 'check_violation';
        END IF;

        PERFORM payroll.fn_AssertRateStructureComplete(NEW.RateDefinitionID);

        IF EXISTS (
            SELECT 1
              FROM payroll.RateComponentDefinitions c
              LEFT JOIN payroll.DriverRateScheduleValues v
                ON v.RateComponentDefinitionID = c.RateComponentDefinitionID
               AND v.DriverRateScheduleID = OLD.DriverRateScheduleID
             WHERE c.RateDefinitionID = NEW.RateDefinitionID
               AND v.Amount IS NULL
        ) THEN
            RAISE EXCEPTION 'RATE_SCHEDULE_INCOMPLETE'
                USING ERRCODE = 'check_violation',
                      DETAIL = format('DriverRateScheduleID=%s', OLD.DriverRateScheduleID);
        END IF;

        UPDATE payroll.RateDefinitions
           SET StructureLockedAtUtc = NOW()
         WHERE RateDefinitionID = NEW.RateDefinitionID
           AND StructureLockedAtUtc IS NULL;
        RETURN NEW;
    END IF;

    IF NEW.EffectiveFrom IS DISTINCT FROM OLD.EffectiveFrom
       OR NEW.ApprovedByUserID IS DISTINCT FROM OLD.ApprovedByUserID
       OR NEW.ApprovedAtUtc IS DISTINCT FROM OLD.ApprovedAtUtc
       OR (NEW.EffectiveTo IS DISTINCT FROM OLD.EffectiveTo
           AND NOT (OLD.Status = 'Approved' AND NEW.Status = 'Superseded'))
       OR (OLD.Status = 'Voided'
           AND (NEW.Status <> 'Voided'
                OR NEW.VoidedByUserID IS DISTINCT FROM OLD.VoidedByUserID
                OR NEW.VoidedAtUtc IS DISTINCT FROM OLD.VoidedAtUtc
                OR NEW.VoidReason IS DISTINCT FROM OLD.VoidReason)) THEN
        RAISE EXCEPTION 'RATE_SCHEDULE_AUTHORITATIVE_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NOT (
        (OLD.Status = 'Approved' AND NEW.Status IN ('Approved', 'Superseded', 'Voided'))
        OR (OLD.Status = 'Superseded' AND NEW.Status IN ('Superseded', 'Voided'))
        OR (OLD.Status = 'Voided' AND NEW.Status = 'Voided')
    ) THEN
        RAISE EXCEPTION 'RATE_SCHEDULE_INVALID_TRANSITION'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('%s -> %s', OLD.Status, NEW.Status);
    END IF;
    RETURN NEW;
END
$function$;

-- Audit of a discarded Pending schedule names the owner of whichever kind it was.
CREATE OR REPLACE FUNCTION payroll.trg_driverrateschedules_beforedelete()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_Actor TEXT := nullif(current_setting('flussra.actor_user_id', TRUE), '');
BEGIN
    PERFORM payroll.fn_LockRateDefinitionStructure(OLD.RateDefinitionID);

    IF OLD.Status <> 'Pending' THEN
        RAISE EXCEPTION 'RATE_SCHEDULE_DELETE_BLOCKED'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('Status=%s', OLD.Status);
    END IF;

    INSERT INTO audit.AuditLog
        (CompanyID, BranchID, ActorUserID, ActionCode, EntitySchema, EntityName,
         EntityID, OldValueJson, SourceType)
    SELECT OLD.CompanyID, NULL, v_Actor::INTEGER,
           'DRIVER_RATE_SCHEDULE_DISCARDED', 'payroll', 'DriverRateSchedules',
           OLD.DriverRateScheduleID::TEXT,
           jsonb_build_object(
               'DriverRateScheduleID', OLD.DriverRateScheduleID,
               'DriverID', OLD.DriverID,
               'RateDefinitionID', OLD.RateDefinitionID,
               'PayDefinitionID', OLD.PayDefinitionID,
               'StatusRateColumnID', OLD.StatusRateColumnID,
               'EffectiveFrom', OLD.EffectiveFrom,
               'EffectiveTo', OLD.EffectiveTo,
               'Notes', OLD.Notes,
               'Values', COALESCE(
                   (SELECT jsonb_agg(
                               jsonb_build_object(
                                   'RateComponentDefinitionID', v.RateComponentDefinitionID,
                                   'Amount', v.Amount)
                               ORDER BY v.RateComponentDefinitionID)
                      FROM payroll.DriverRateScheduleValues v
                     WHERE v.DriverRateScheduleID = OLD.DriverRateScheduleID),
                   '[]'::jsonb))::TEXT,
           'Database';
    RETURN OLD;
END
$function$;

-- ---------------------------------------------------------------------------
-- 5. Status keys: Company-owned catalog entries
-- ---------------------------------------------------------------------------

DROP INDEX payroll.ix_payrollstatuskeys_branchactiveorder;
DROP INDEX payroll.ix_payrollstatuskeys_keyname;
DROP INDEX payroll.ux_payrollstatuskeys_activecode;
DROP INDEX payroll.ix_psk_statusratecolumn;

-- The limits are Branch x Status key facts and move to their own table below.
ALTER TABLE payroll.PayrollStatusKeys
    DROP CONSTRAINT ck_statuskeys_limitperperiod,
    DROP CONSTRAINT ck_statuskeys_limitperdriver,
    DROP CONSTRAINT ck_statuskeys_limitacrossdrivers,
    DROP CONSTRAINT ck_statuskeys_limitperday,
    DROP CONSTRAINT fk_payrollstatuskeys_branch;

ALTER TABLE payroll.PayrollStatusKeys
    DROP COLUMN BranchID,
    DROP COLUMN LimitUsesPerPeriodEnabled,
    DROP COLUMN LimitUsesPerPeriod,
    DROP COLUMN LimitUsesPerDriverEnabled,
    DROP COLUMN LimitUsesPerDriver,
    DROP COLUMN LimitUsesAcrossDriversEnabled,
    DROP COLUMN LimitUsesAcrossDrivers,
    DROP COLUMN LimitUsesPerDayEnabled,
    DROP COLUMN LimitUsesPerDay;

-- The table is empty (the preflight), so the new required columns need no backfill.
ALTER TABLE payroll.PayrollStatusKeys
    ADD COLUMN NormalizedKeyName VARCHAR(200) NOT NULL;

-- AllowanceCategory is the Status Category: required, from a fixed set. The Company has no
-- customer-defined categories and no allowance tracking.
ALTER TABLE payroll.PayrollStatusKeys
    ALTER COLUMN AllowanceCategory SET NOT NULL;

ALTER TABLE payroll.PayrollStatusKeys
    ADD CONSTRAINT ck_PayrollStatusKeys_Category
        CHECK (AllowanceCategory IN ('Vacation', 'Sick', 'Bereavement', 'Jury Duty',
                                     'Personal', 'Holiday', 'Unpaid', 'Other')),
    ADD CONSTRAINT ck_PayrollStatusKeys_Name
        CHECK (btrim(KeyName) <> ''
               AND NormalizedKeyName <> ''
               AND NormalizedKeyName = regexp_replace(btrim(NormalizedKeyName), '\s+', ' ', 'g')),
    -- A monetary Status is paid HoursValue x rate, so it needs hours.
    ADD CONSTRAINT ck_PayrollStatusKeys_RateColumnNeedsHours
        CHECK (StatusRateColumnID IS NULL OR HoursValue > 0),
    ADD CONSTRAINT uq_PayrollStatusKeys_ID_Company UNIQUE (StatusKeyID, CompanyID),
    -- The technical code is generated, immutable and unique per Company.
    ADD CONSTRAINT uq_PayrollStatusKeys_Company_Code UNIQUE (CompanyID, NormalizedStatusCode),
    -- Mirrored into the aliases by a cascading composite key, so an alias is "active" exactly
    -- when its Status is.
    ADD CONSTRAINT uq_PayrollStatusKeys_ID_Company_Active UNIQUE (StatusKeyID, CompanyID, IsActive),
    ADD CONSTRAINT fk_PayrollStatusKeys_StatusRateColumn_Company
        FOREIGN KEY (StatusRateColumnID, CompanyID)
        REFERENCES payroll.StatusRateColumns (StatusRateColumnID, CompanyID);

-- The canonical primary name is unique among a Company's active Statuses by normalized form.
CREATE UNIQUE INDEX ux_PayrollStatusKeys_ActiveName
    ON payroll.PayrollStatusKeys (CompanyID, NormalizedKeyName)
    WHERE IsActive;
CREATE INDEX ix_PayrollStatusKeys_CompanyActiveOrder
    ON payroll.PayrollStatusKeys (CompanyID, IsActive, DisplayOrder, KeyName);
CREATE INDEX ix_PayrollStatusKeys_StatusRateColumn
    ON payroll.PayrollStatusKeys (StatusRateColumnID)
    WHERE StatusRateColumnID IS NOT NULL;

-- Day-entry selections reference the Status of their own Company.
ALTER TABLE payroll.PayrollPeriodDriverDayEntryState
    ADD CONSTRAINT fk_ppdes_statuskey_company
        FOREIGN KEY (StatusKeyID, CompanyID)
        REFERENCES payroll.PayrollStatusKeys (StatusKeyID, CompanyID)
        ON DELETE RESTRICT;

-- Frozen Status evidence of a snapshot references the Status of its own Company.
ALTER TABLE payroll.PayrollCalculationSnapshotStatusEntries
    ADD CONSTRAINT fk_PayrollCalculationSnapshotStatusEntries_StatusKey_Company
        FOREIGN KEY (StatusKeyID, CompanyID)
        REFERENCES payroll.PayrollStatusKeys (StatusKeyID, CompanyID)
        ON DELETE RESTRICT;

-- A frozen Status component names the Status rate column of its own Company. The Branch of a
-- component is payroll provenance only: the column no longer has one.
ALTER TABLE payroll.PayrollCalculationSnapshotComponents
    ADD CONSTRAINT fk_PayrollCalculationSnapshotComponents_StatusRateColumn
        FOREIGN KEY (StatusRateColumnID, CompanyID)
        REFERENCES payroll.StatusRateColumns (StatusRateColumnID, CompanyID);

-- ---------------------------------------------------------------------------
-- 6. Aliases
-- ---------------------------------------------------------------------------
--
-- An alias is an input convenience only: a selection stores the canonical StatusKeyID, never
-- the alias, so aliases stay editable. IsActive is the Status's IsActive, kept by the
-- cascading composite key and never written by the application.

CREATE TABLE payroll.PayrollStatusKeyAliases (
    StatusKeyAliasID BIGSERIAL    PRIMARY KEY,
    CompanyID        INTEGER      NOT NULL,
    StatusKeyID      INTEGER      NOT NULL,
    IsActive         BOOLEAN      NOT NULL,
    AliasText        VARCHAR(200) NOT NULL,
    NormalizedAlias  VARCHAR(200) NOT NULL,
    CreatedByUserID  INTEGER,
    CreatedAtUtc     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_PayrollStatusKeyAliases_StatusKey
        FOREIGN KEY (StatusKeyID, CompanyID, IsActive)
        REFERENCES payroll.PayrollStatusKeys (StatusKeyID, CompanyID, IsActive)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_PayrollStatusKeyAliases_Creator
        FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users (UserID),
    CONSTRAINT ck_PayrollStatusKeyAliases_Alias
        CHECK (btrim(AliasText) <> ''
               AND NormalizedAlias <> ''
               AND NormalizedAlias = regexp_replace(btrim(NormalizedAlias), '\s+', ' ', 'g')),
    -- A Status never carries the same alias twice.
    CONSTRAINT uq_PayrollStatusKeyAliases_Key_Alias UNIQUE (StatusKeyID, NormalizedAlias)
);

-- Active normalized aliases are unambiguous within the Company.
CREATE UNIQUE INDEX ux_PayrollStatusKeyAliases_ActiveAlias
    ON payroll.PayrollStatusKeyAliases (CompanyID, NormalizedAlias)
    WHERE IsActive;

-- ---------------------------------------------------------------------------
-- 7. Name and alias uniqueness across both tables
-- ---------------------------------------------------------------------------
--
-- The partial unique indexes decide names among names and aliases among aliases. Whether an
-- alias equals another Status's name, and whether a name equals another Status's alias, spans
-- two tables, so a trigger on each decides it. Both take the Company's catalog lock first, so
-- two transactions that would together create an ambiguity cannot both pass the check.

CREATE FUNCTION payroll.fn_LockStatusCatalog(p_CompanyID INTEGER)
RETURNS VOID
LANGUAGE plpgsql
AS $function$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('flussra.status-catalog.' || p_CompanyID::TEXT, 0));
END
$function$;

CREATE FUNCTION payroll.fn_guard_status_key_name()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    PERFORM payroll.fn_LockStatusCatalog(NEW.CompanyID);

    -- An alias of a Status cannot equal its own primary name (active or not).
    IF EXISTS (
        SELECT 1 FROM payroll.PayrollStatusKeyAliases a
         WHERE a.StatusKeyID = NEW.StatusKeyID
           AND a.NormalizedAlias = NEW.NormalizedKeyName
    ) THEN
        RAISE EXCEPTION 'STATUS_ALIAS_AMBIGUOUS: the name equals an alias of the same Status.'
            USING ERRCODE = 'unique_violation';
    END IF;

    IF NEW.IsActive THEN
        IF EXISTS (
            SELECT 1 FROM payroll.PayrollStatusKeys k
             WHERE k.CompanyID = NEW.CompanyID
               AND k.IsActive
               AND k.StatusKeyID <> NEW.StatusKeyID
               AND k.NormalizedKeyName = NEW.NormalizedKeyName
        ) THEN
            RAISE EXCEPTION 'STATUS_NAME_NOT_UNIQUE: an active Status of this Company already has this name.'
                USING ERRCODE = 'unique_violation';
        END IF;
        IF EXISTS (
            SELECT 1 FROM payroll.PayrollStatusKeyAliases a
             WHERE a.CompanyID = NEW.CompanyID
               AND a.IsActive
               AND a.StatusKeyID <> NEW.StatusKeyID
               AND a.NormalizedAlias = NEW.NormalizedKeyName
        ) THEN
            RAISE EXCEPTION 'STATUS_ALIAS_AMBIGUOUS: an active alias of another Status of this Company equals this name.'
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER trg_PayrollStatusKeys_Name
    BEFORE INSERT OR UPDATE OF KeyName, NormalizedKeyName, IsActive ON payroll.PayrollStatusKeys
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_status_key_name();

CREATE FUNCTION payroll.fn_guard_status_key_alias()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_OwnName VARCHAR(200);
BEGIN
    PERFORM payroll.fn_LockStatusCatalog(NEW.CompanyID);

    SELECT k.NormalizedKeyName INTO v_OwnName
      FROM payroll.PayrollStatusKeys k
     WHERE k.StatusKeyID = NEW.StatusKeyID;
    IF v_OwnName = NEW.NormalizedAlias THEN
        RAISE EXCEPTION 'STATUS_ALIAS_AMBIGUOUS: an alias cannot equal the primary name of its own Status.'
            USING ERRCODE = 'unique_violation';
    END IF;

    IF NEW.IsActive THEN
        IF EXISTS (
            SELECT 1 FROM payroll.PayrollStatusKeys k
             WHERE k.CompanyID = NEW.CompanyID
               AND k.IsActive
               AND k.StatusKeyID <> NEW.StatusKeyID
               AND k.NormalizedKeyName = NEW.NormalizedAlias
        ) THEN
            RAISE EXCEPTION 'STATUS_ALIAS_AMBIGUOUS: the alias equals the name of another active Status of this Company.'
                USING ERRCODE = 'unique_violation';
        END IF;
        IF EXISTS (
            SELECT 1 FROM payroll.PayrollStatusKeyAliases a
             WHERE a.CompanyID = NEW.CompanyID
               AND a.IsActive
               AND a.StatusKeyAliasID IS DISTINCT FROM NEW.StatusKeyAliasID
               AND a.StatusKeyID <> NEW.StatusKeyID
               AND a.NormalizedAlias = NEW.NormalizedAlias
        ) THEN
            RAISE EXCEPTION 'STATUS_ALIAS_AMBIGUOUS: the alias is already an active alias of another Status of this Company.'
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER trg_PayrollStatusKeyAliases_Alias
    BEFORE INSERT OR UPDATE OF NormalizedAlias, IsActive ON payroll.PayrollStatusKeyAliases
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_status_key_alias();

-- ---------------------------------------------------------------------------
-- 8. The semantic lock of a used Status
-- ---------------------------------------------------------------------------
--
-- Once a day-entry selection (voided or not) or a frozen Status evidence entry references a
-- Status, the fields that give it meaning cannot change: its hours, whether it counts as a
-- full day off, its Category and its rate column binding. The lifecycle is to deactivate the
-- Status and create a new one. The name, display order, active flag and aliases stay editable.
-- The identity of a Status (Company and generated code) never changes.

CREATE FUNCTION payroll.fn_guard_status_key_semantics()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.CompanyID IS DISTINCT FROM OLD.CompanyID
       OR NEW.StatusCode IS DISTINCT FROM OLD.StatusCode
       OR NEW.NormalizedStatusCode IS DISTINCT FROM OLD.NormalizedStatusCode THEN
        RAISE EXCEPTION 'STATUS_KEY_IDENTITY_IMMUTABLE: the Company and generated code of a Status never change.'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.HoursValue IS DISTINCT FROM OLD.HoursValue
       OR NEW.IsOffReason IS DISTINCT FROM OLD.IsOffReason
       OR NEW.AllowanceCategory IS DISTINCT FROM OLD.AllowanceCategory
       OR NEW.StatusRateColumnID IS DISTINCT FROM OLD.StatusRateColumnID THEN
        IF EXISTS (
            SELECT 1 FROM payroll.PayrollPeriodDriverDayEntryState e
             WHERE e.StatusKeyID = OLD.StatusKeyID
        ) OR EXISTS (
            SELECT 1 FROM payroll.PayrollCalculationSnapshotStatusEntries s
             WHERE s.StatusKeyID = OLD.StatusKeyID
        ) THEN
            RAISE EXCEPTION 'STATUS_KEY_IN_USE: a Status that has been selected cannot change its hours, off-day semantic, Category or rate column. Deactivate it and create a new Status.'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER trg_PayrollStatusKeys_Semantics
    BEFORE UPDATE ON payroll.PayrollStatusKeys
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_status_key_semantics();

-- ---------------------------------------------------------------------------
-- 9. Branch usage limits
-- ---------------------------------------------------------------------------
--
-- The four optional hard limits of a Status are a Branch x Status fact. A missing row means no
-- limits; a row always has at least one limit enabled.

CREATE TABLE payroll.PayrollStatusKeyBranchLimits (
    BranchID                       INTEGER     NOT NULL,
    StatusKeyID                    INTEGER     NOT NULL,
    CompanyID                      INTEGER     NOT NULL,
    LimitUsesPerPeriodEnabled      BOOLEAN     NOT NULL DEFAULT FALSE,
    LimitUsesPerPeriod             INTEGER,
    LimitUsesPerDriverEnabled      BOOLEAN     NOT NULL DEFAULT FALSE,
    LimitUsesPerDriver             INTEGER,
    LimitUsesAcrossDriversEnabled  BOOLEAN     NOT NULL DEFAULT FALSE,
    LimitUsesAcrossDrivers         INTEGER,
    LimitUsesPerDayEnabled         BOOLEAN     NOT NULL DEFAULT FALSE,
    LimitUsesPerDay                INTEGER,
    CreatedByUserID                INTEGER,
    CreatedAtUtc                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UpdatedByUserID                INTEGER,
    UpdatedAtUtc                   TIMESTAMPTZ,

    CONSTRAINT pk_PayrollStatusKeyBranchLimits PRIMARY KEY (BranchID, StatusKeyID),
    CONSTRAINT fk_PayrollStatusKeyBranchLimits_Branch
        FOREIGN KEY (BranchID, CompanyID) REFERENCES core.Branches (BranchID, CompanyID),
    CONSTRAINT fk_PayrollStatusKeyBranchLimits_StatusKey
        FOREIGN KEY (StatusKeyID, CompanyID)
        REFERENCES payroll.PayrollStatusKeys (StatusKeyID, CompanyID)
        ON DELETE CASCADE,
    CONSTRAINT fk_PayrollStatusKeyBranchLimits_Creator
        FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users (UserID),
    CONSTRAINT fk_PayrollStatusKeyBranchLimits_Updater
        FOREIGN KEY (UpdatedByUserID) REFERENCES sec.Users (UserID),
    CONSTRAINT ck_PayrollStatusKeyBranchLimits_PerPeriod
        CHECK (LimitUsesPerPeriodEnabled = FALSE
               OR (LimitUsesPerPeriod IS NOT NULL AND LimitUsesPerPeriod > 0)),
    CONSTRAINT ck_PayrollStatusKeyBranchLimits_PerDriver
        CHECK (LimitUsesPerDriverEnabled = FALSE
               OR (LimitUsesPerDriver IS NOT NULL AND LimitUsesPerDriver > 0)),
    CONSTRAINT ck_PayrollStatusKeyBranchLimits_AcrossDrivers
        CHECK (LimitUsesAcrossDriversEnabled = FALSE
               OR (LimitUsesAcrossDrivers IS NOT NULL AND LimitUsesAcrossDrivers > 0)),
    CONSTRAINT ck_PayrollStatusKeyBranchLimits_PerDay
        CHECK (LimitUsesPerDayEnabled = FALSE
               OR (LimitUsesPerDay IS NOT NULL AND LimitUsesPerDay > 0)),
    CONSTRAINT ck_PayrollStatusKeyBranchLimits_AnyEnabled
        CHECK (LimitUsesPerPeriodEnabled OR LimitUsesPerDriverEnabled
               OR LimitUsesAcrossDriversEnabled OR LimitUsesPerDayEnabled)
);

CREATE INDEX ix_PayrollStatusKeyBranchLimits_StatusKey
    ON payroll.PayrollStatusKeyBranchLimits (StatusKeyID, CompanyID);

-- ---------------------------------------------------------------------------
-- 10. Status rate columns and their rate definitions are created together
-- ---------------------------------------------------------------------------
--
-- Each column owns one scalar RateDefinition with its one scalar component, so a Driver's
-- Status rate values are ordinary Driver-owned rate schedules. The structure of a Status
-- rate is fixed, so it is locked at creation. One function creates the three rows
-- (the application, the Company default below and any seeding all use it), and every Company
-- starts with one default active column.

CREATE FUNCTION payroll.fn_CreateStatusRateColumn(
    p_CompanyID INTEGER,
    p_ColumnName VARCHAR,
    p_NormalizedColumnName VARCHAR,
    p_IsDefault BOOLEAN,
    p_UserID INTEGER
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $function$
DECLARE
    v_ColumnID INTEGER;
    v_DefinitionID INTEGER;
BEGIN
    INSERT INTO payroll.StatusRateColumns
        (CompanyID, ColumnName, NormalizedColumnName, IsDefault, IsActive,
         CreatedByUserID, UpdatedByUserID)
    VALUES (p_CompanyID, p_ColumnName, p_NormalizedColumnName, p_IsDefault, TRUE,
            p_UserID, p_UserID)
    RETURNING StatusRateColumnID INTO v_ColumnID;

    INSERT INTO payroll.RateDefinitions (CompanyID, StatusRateColumnID, Shape)
    VALUES (p_CompanyID, v_ColumnID, 'Scalar')
    RETURNING RateDefinitionID INTO v_DefinitionID;

    INSERT INTO payroll.RateComponentDefinitions (RateDefinitionID, Shape, SequenceNo)
    VALUES (v_DefinitionID, 'Scalar', 1);

    UPDATE payroll.RateDefinitions
       SET StructureLockedAtUtc = NOW()
     WHERE RateDefinitionID = v_DefinitionID;

    RETURN v_ColumnID;
END
$function$;

CREATE FUNCTION payroll.fn_EnsureDefaultStatusRateColumn(p_CompanyID INTEGER)
RETURNS INTEGER
LANGUAGE plpgsql
AS $function$
DECLARE
    v_ColumnID INTEGER;
    v_Name VARCHAR := 'Status Pay';
BEGIN
    PERFORM payroll.fn_LockStatusCatalog(p_CompanyID);

    SELECT c.StatusRateColumnID INTO v_ColumnID
      FROM payroll.StatusRateColumns c
     WHERE c.CompanyID = p_CompanyID AND c.IsDefault AND c.IsActive;
    IF FOUND THEN
        RETURN v_ColumnID;
    END IF;

    IF EXISTS (SELECT 1 FROM payroll.StatusRateColumns c
                WHERE c.CompanyID = p_CompanyID AND c.IsActive
                  AND c.NormalizedColumnName = 'status pay') THEN
        v_Name := 'Status Pay (default)';
    END IF;
    RETURN payroll.fn_CreateStatusRateColumn(
        p_CompanyID, v_Name, lower(v_Name), TRUE, NULL);
END
$function$;

DO $company_defaults$
DECLARE
    v_CompanyID INTEGER;
BEGIN
    FOR v_CompanyID IN SELECT c.CompanyID FROM core.Companies c ORDER BY c.CompanyID LOOP
        PERFORM payroll.fn_EnsureDefaultStatusRateColumn(v_CompanyID);
    END LOOP;
END
$company_defaults$;
