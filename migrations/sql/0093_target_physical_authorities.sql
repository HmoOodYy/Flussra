-- 0093: Target physical authorities after the runtime cutover.
-- Historical migrations are immutable. Retired development catalogs are disposable;
-- no data is converted into target definitions, schedules or calculation evidence.
--
-- Dependency classification at 0092:
-- * Global Roles: RolePermissions and UserBranchRoles.RoleID only; 0092 already
--   replaced the Access view and functions with CompanyRole consumers.
-- * PayItems/RateTypes: maps, slots, CDPI storage, DriverRates/Tiers, and the
--   permanently unused FinalLines columns only. Status columns and immutable
--   used-rate evidence already reference target RateDefinitions/Schedules.
-- * Currency authority: replace its legacy rate/stored-draft-money branches.
-- * DraftLine LineType/SourceID/SourceSnapshot: predecessor Status projection;
--   ordinary source evidence is period-definition keyed and separately immutable.
-- * FinalLines: snapshot components, not source/rate lines, own financial identity.
-- All external dependencies are released explicitly. Every DROP uses RESTRICT.

DO $preflight$
BEGIN
    IF EXISTS (SELECT 1 FROM sec.UserBranchRoles WHERE CompanyRoleID IS NULL) THEN
        RAISE EXCEPTION 'TARGET_ROLE_ASSIGNMENTS_REQUIRE_REBUILD: legacy-only role assignments cannot be converted. Reset and reseed this preproduction database.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM payroll.PayrollDraftLines
               WHERE PayrollPeriodDefinitionID IS NULL) THEN
        RAISE EXCEPTION 'TARGET_SOURCE_FACTS_REQUIRE_REBUILD: predecessor draft rows cannot be converted. Reset and reseed this preproduction database.'
            USING ERRCODE = 'check_violation';
    END IF;
END
$preflight$;

-- Currency is locked only by durable target monetary history. Source quantities
-- and Pending schedules do not establish a monetary denomination.
CREATE OR REPLACE FUNCTION core.fn_company_has_durable_monetary_state(p_company_id INTEGER)
RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
    SELECT
        EXISTS (SELECT 1 FROM payroll.DriverRateSchedules s
                WHERE s.CompanyID = p_company_id
                  AND s.Status IN ('Approved', 'Superseded', 'Voided'))
        OR EXISTS (SELECT 1 FROM payroll.PayrollBonusEvents b WHERE b.CompanyID = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.DriverPayRules p WHERE p.CompanyID = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.PayrollCalculationSnapshots s WHERE s.CompanyID = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.PayrollFinalLines f WHERE f.CompanyID = p_company_id);
$$;

-- Role assignment identity is CompanyRoleID, including active-scope uniqueness.
DROP INDEX sec.ix_UserBranchRoles_User_Active RESTRICT;
DROP INDEX sec.ux_UserBranchRoles_Active_AllCompany RESTRICT;
DROP INDEX sec.ux_UserBranchRoles_Active_Branch RESTRICT;
ALTER TABLE sec.UserBranchRoles DROP COLUMN RoleID RESTRICT;
ALTER TABLE sec.UserBranchRoles ALTER COLUMN CompanyRoleID SET NOT NULL;
CREATE INDEX ix_UserBranchRoles_User_Active ON sec.UserBranchRoles
    (UserID, IsActive, CompanyID, BranchID, CompanyRoleID);
CREATE UNIQUE INDEX ux_UserBranchRoles_Active_AllCompany ON sec.UserBranchRoles
    (UserID, CompanyID, CompanyRoleID, ScopeType) WHERE IsActive AND BranchID IS NULL;
CREATE UNIQUE INDEX ux_UserBranchRoles_Active_Branch ON sec.UserBranchRoles
    (UserID, CompanyID, CompanyRoleID, ScopeType, BranchID) WHERE IsActive AND BranchID IS NOT NULL;
DROP TABLE sec.RolePermissions RESTRICT;
DROP TABLE sec.Roles RESTRICT;
DELETE FROM sec.CompanyRolePermissions WHERE PermissionCode = 'drivers.manage';
DELETE FROM sec.UserPermissionOverrides WHERE PermissionCode = 'drivers.manage';
DELETE FROM sec.Permissions WHERE PermissionCode = 'drivers.manage';
ALTER TABLE core.Employees DROP COLUMN EmployeeType RESTRICT;

-- Rename the existing aggregate in place. ConfigID, effective versions and the
-- incoming PayrollPeriodDefinitions.SourceBranchConfigID FK keep their identity.
ALTER TABLE payroll.BranchPayItemConfig DROP COLUMN PayItemID RESTRICT;
ALTER TABLE payroll.BranchPayItemConfig RENAME TO BranchPayDefinitionConfig;
ALTER SEQUENCE payroll.branchpayitemconfig_configid_seq RENAME TO branchpaydefinitionconfig_configid_seq;
DO $rename_config_constraints$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'payroll.BranchPayDefinitionConfig'::regclass
               AND conname LIKE '%branchpayitemconfig%'
    LOOP
        EXECUTE format('ALTER TABLE payroll.BranchPayDefinitionConfig RENAME CONSTRAINT %I TO %I',
                       r.conname, replace(r.conname, 'branchpayitemconfig', 'branchpaydefinitionconfig'));
    END LOOP;
END
$rename_config_constraints$;
ALTER INDEX payroll.ix_BranchPayItemConfig_Branch_Definition_Dates
    RENAME TO ix_BranchPayDefinitionConfig_Branch_Definition_Dates;
ALTER INDEX payroll.uix_BranchPayItemConfig_OpenDefinitionVersion
    RENAME TO uix_BranchPayDefinitionConfig_OpenDefinitionVersion;

-- Ordinary source rows contain facts only. NeedsManagerReview remains a live
-- workflow/dashboard contract; target source rows must keep its stored value false.
ALTER TABLE payroll.PayrollDraftLines DROP CONSTRAINT ck_DraftLines_RowIdentity;
DROP INDEX payroll.uix_PayrollDraftLines_Daily_Active_Business_Key RESTRICT;
DROP INDEX payroll.ix_PayrollDraftLines_Period_Assignment RESTRICT;
ALTER TABLE payroll.PayrollDraftLines
    DROP COLUMN RateAmount RESTRICT,
    DROP COLUMN CalculatedAmount RESTRICT,
    DROP COLUMN LineType RESTRICT,
    DROP COLUMN SourceID RESTRICT,
    DROP COLUMN SourceSnapshot RESTRICT,
    ALTER COLUMN PayrollPeriodDefinitionID SET NOT NULL,
    ADD CONSTRAINT ck_DraftLines_SourceType CHECK (SourceType IN ('Manual', 'Import')),
    ADD CONSTRAINT ck_DraftLines_NoStoredReview CHECK (NeedsManagerReview = FALSE);
-- The target unique business key already protects assignment/date/definition.
-- Keep the existing period/assignment/date lookup index and definition FK.

-- Whole-component finalization retains frozen provenance, scope, identities,
-- amount/currency and the existing ledger vocabulary. Source-level money and
-- provenance are not meaningful on a component projection.
ALTER TABLE payroll.PayrollFinalLines DROP CONSTRAINT ck_PayrollFinalLines_ComponentLine;
ALTER TABLE payroll.PayrollFinalLines
    DROP COLUMN PayItemID RESTRICT,
    DROP COLUMN RateTypeID RESTRICT,
    DROP COLUMN DriverRateID RESTRICT,
    DROP COLUMN ResolvedRateAmount RESTRICT,
    DROP COLUMN RateBehavior RESTRICT,
    DROP COLUMN DraftLineID RESTRICT,
    DROP COLUMN BonusEventID RESTRICT,
    DROP COLUMN WorkDate RESTRICT,
    DROP COLUMN Quantity RESTRICT,
    DROP COLUMN RateAmount RESTRICT,
    DROP COLUMN Notes RESTRICT;
ALTER TABLE payroll.PayrollFinalLines
    ADD CONSTRAINT ck_PayrollFinalLines_ComponentLine CHECK (
        LineScope = 'Period' AND SourceType = 'SnapshotComponent' AND SourceID IS NOT NULL
        AND LineType = CASE ComponentKind
            WHEN 'ORDINARY' THEN 'ORDINARY'
            WHEN 'STATUS' THEN 'STATUS_PAY'
            WHEN 'BONUS' THEN 'BONUS'
            WHEN 'MINIMUM_ADJUSTMENT' THEN 'SYS_MIN_TOPUP'
            WHEN 'MAXIMUM_ADJUSTMENT' THEN 'SYS_MAX_CAP' END
        AND ((ComponentKind = 'ORDINARY')
            = (PayrollPeriodDefinitionID IS NOT NULL AND PayDefinitionID IS NOT NULL))
        AND (ComponentKind = 'ORDINARY'
            OR (PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL)));
CREATE INDEX ix_PayrollFinalLines_Period_Assignment
    ON payroll.PayrollFinalLines (PayrollPeriodID, DriverAssignmentID, LineType);
ALTER TABLE payroll.PayrollPeriodAuditEvidenceEvents DROP COLUMN PayItemID RESTRICT;

-- Generic governance covers origin, lifecycle, requesting Branch, creator,
-- submitter/approver, creation mode, versions, canonical structure lock and audit
-- timestamps (0082/0083/0091). No current consumer uses these CDPI tables.
DROP TABLE payroll.CdpiRequestEvents RESTRICT;
DROP TABLE payroll.CdpiDefinitions RESTRICT;
DROP TABLE payroll.CdpiRequests RESTRICT;
DROP TABLE payroll.PayItemLineTypeMap RESTRICT;
DROP TABLE payroll.PayItemRateSlots RESTRICT;
DROP TABLE payroll.PayItemRateTypeMap RESTRICT;
DROP TABLE payroll.DriverRateTiers RESTRICT;
DROP TABLE payroll.DriverRates RESTRICT;
DROP TABLE payroll.PayItems RESTRICT;
DROP TABLE payroll.RateTypes RESTRICT;

-- Table-owned indexes, FKs and triggers leave with their retired tables. These
-- functions have no remaining trigger or runtime consumer.
DROP FUNCTION payroll.fn_guard_cdpi_request_events_immutable() RESTRICT;
DROP FUNCTION payroll.fn_guard_payitemratetypemap_ownership() RESTRICT;
DROP FUNCTION payroll.fn_guard_driverratetier_used_mutation() RESTRICT;
DROP FUNCTION payroll.fn_guard_driverrate_used_mutation() RESTRICT;
DROP FUNCTION payroll.fn_guard_driverrate_void() RESTRICT;
DROP FUNCTION payroll.fn_DriverRateUsedInFinalizedPayroll(BIGINT) RESTRICT;
DROP FUNCTION core.trg_CompensationAssignmentWritable() RESTRICT;

-- Established immutable-evidence pattern: fail before every mutation, including
-- statement-level TRUNCATE (which does not execute row DELETE triggers).
CREATE FUNCTION audit.fn_guard_audit_log_immutable()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log_immutable: % on %.% is forbidden once written.',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END;
$$;
CREATE TRIGGER trg_AuditLog_Immutable
    BEFORE UPDATE OR DELETE ON audit.AuditLog
    FOR EACH ROW EXECUTE FUNCTION audit.fn_guard_audit_log_immutable();
CREATE TRIGGER trg_AuditLog_NoTruncate
    BEFORE TRUNCATE ON audit.AuditLog
    FOR EACH STATEMENT EXECUTE FUNCTION audit.fn_guard_audit_log_immutable();
