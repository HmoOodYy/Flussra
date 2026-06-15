-- Migration 0044: CDPI schema corrections.
--
-- Corrects the following issues in the 0043 foundation:
--   1. Trigger parity: trigger function and trigger now live in this SQL file
--      so the test bootstrap (conftest psycopg2 path) and Alembic upgrade
--      both create the same objects.  The Python wrapper uses a dollar-quote-
--      aware splitter (migrations/utils.py) to execute this file correctly.
--   2. Tenant integrity: composite FKs ensure branch and approved item belong
--      to the same company as the request.
--   3. Remove CdpiDefinitions.SourceRequestID (redundant bidirectional link).
--      The canonical link is CdpiRequests.ApprovedPayItemID (unique).
--   4. Lifecycle integrity: CHECK enforces Approved <-> non-null ApprovedPayItemID.
--   5. Positive numeric constraints on Revision, RequestRevision, SchemaVersion.
--   6. CreatedByUserID made NOT NULL on CdpiRequests and CdpiDefinitions.
--   7. CopiedFromRequestID self-referencing FK with company-integrity check.
--   8. Index cleanup: ix_CdpiDefinitions_SourceRequest removed with the column.
--   9. Supporting unique indexes for composite FK references.
--
-- All DDL targets tables that are new in 0043 and contain no live rows,
-- so NOT NULL additions and CHECK constraints are safe to apply as VALID.


-- ===========================================================================
-- 1. Trigger parity
--    CREATE OR REPLACE is idempotent: safe whether or not 0043 already
--    created the function/trigger via its inline Python path.
-- ===========================================================================

CREATE OR REPLACE FUNCTION payroll.fn_guard_cdpi_request_events_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'cdpi_events_immutable: Request events are append-only and cannot be % '
        'once written. (event_id=%)',
        TG_OP, OLD.eventid
        USING ERRCODE = 'restrict_violation';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_guard_cdpi_request_events_immutable
    ON payroll.CdpiRequestEvents;

CREATE TRIGGER trg_guard_cdpi_request_events_immutable
    BEFORE UPDATE OR DELETE ON payroll.CdpiRequestEvents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_cdpi_request_events_immutable();


-- ===========================================================================
-- 2. Supporting unique indexes required by composite FK constraints
-- ===========================================================================

-- Already added by migration 0025; IF NOT EXISTS makes this idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS ux_Branches_ID_Company
    ON core.Branches (BranchID, CompanyID);

-- Needed so FK can enforce (ApprovedPayItemID, CompanyID) -> PayItems.
CREATE UNIQUE INDEX IF NOT EXISTS ux_PayItems_ID_Company
    ON payroll.PayItems (PayItemID, CompanyID);

-- Needed so the self-referencing CopiedFromRequestID composite FK can
-- enforce that the source request belongs to the same company.
CREATE UNIQUE INDEX IF NOT EXISTS ux_CdpiRequests_ID_Company
    ON payroll.CdpiRequests (RequestID, CompanyID);


-- ===========================================================================
-- 3. CdpiRequests: new column, corrected constraints, NOT NULL audit
-- ===========================================================================

-- 3a. Self-referencing copy provenance column
ALTER TABLE payroll.CdpiRequests
    ADD COLUMN IF NOT EXISTS CopiedFromRequestID UUID;

-- 3b. Lifecycle integrity: Approved status requires a linked PayItem;
--     all other statuses require it to be absent.
ALTER TABLE payroll.CdpiRequests
    ADD CONSTRAINT ck_CdpiRequests_ApprovalLink
    CHECK (
        (Status = 'Approved' AND ApprovedPayItemID IS NOT NULL) OR
        (Status != 'Approved' AND ApprovedPayItemID IS NULL)
    );

-- 3c. Revision must be positive (starts at 1, incremented on every write)
ALTER TABLE payroll.CdpiRequests
    ADD CONSTRAINT ck_CdpiRequests_Revision
    CHECK (Revision >= 1);

-- 3d. Tenant integrity: branch must belong to the same company as the request.
--     References ux_Branches_ID_Company (BranchID, CompanyID).
ALTER TABLE payroll.CdpiRequests
    ADD CONSTRAINT fk_CdpiRequests_Branch_Company
    FOREIGN KEY (RequestingBranchID, CompanyID)
    REFERENCES core.Branches (BranchID, CompanyID)
    NOT VALID;

-- 3e. Tenant integrity: approved pay item must belong to the same company.
--     NULL when not yet approved; FK fires only when non-NULL.
--     References ux_PayItems_ID_Company (PayItemID, CompanyID).
ALTER TABLE payroll.CdpiRequests
    ADD CONSTRAINT fk_CdpiRequests_ApprovedItem_Company
    FOREIGN KEY (ApprovedPayItemID, CompanyID)
    REFERENCES payroll.PayItems (PayItemID, CompanyID)
    NOT VALID;

-- 3f. Copy provenance: self-referencing FK + same-company composite check.
--     Simple FK: CopiedFromRequestID must reference an existing request.
ALTER TABLE payroll.CdpiRequests
    ADD CONSTRAINT fk_CdpiRequests_CopiedFrom
    FOREIGN KEY (CopiedFromRequestID)
    REFERENCES payroll.CdpiRequests (RequestID);

-- Composite FK: source request must belong to the same company.
ALTER TABLE payroll.CdpiRequests
    ADD CONSTRAINT fk_CdpiRequests_CopiedFrom_Company
    FOREIGN KEY (CopiedFromRequestID, CompanyID)
    REFERENCES payroll.CdpiRequests (RequestID, CompanyID)
    NOT VALID;

-- 3g. Required creation audit actor
ALTER TABLE payroll.CdpiRequests
    ALTER COLUMN CreatedByUserID SET NOT NULL;


-- ===========================================================================
-- 4. CdpiRequestEvents: positive revision constraint
-- ===========================================================================

ALTER TABLE payroll.CdpiRequestEvents
    ADD CONSTRAINT ck_CdpiRequestEvents_Revision
    CHECK (RequestRevision >= 1);


-- ===========================================================================
-- 5. CdpiDefinitions: remove SourceRequestID, add positive schema version,
--    make audit actor required
-- ===========================================================================

-- Drop partial index first (independent of the unique constraint index)
DROP INDEX IF EXISTS payroll.ix_CdpiDefinitions_SourceRequest;

-- Drop the unique constraint (also drops its underlying index)
ALTER TABLE payroll.CdpiDefinitions
    DROP CONSTRAINT IF EXISTS ux_CdpiDefinitions_SourceRequestID;

-- Drop the FK constraint before dropping the column
ALTER TABLE payroll.CdpiDefinitions
    DROP CONSTRAINT IF EXISTS fk_CdpiDefinitions_SourceRequest;

-- Drop the column itself
ALTER TABLE payroll.CdpiDefinitions
    DROP COLUMN IF EXISTS SourceRequestID;

-- Schema version must be a positive integer (starts at 1)
ALTER TABLE payroll.CdpiDefinitions
    ADD CONSTRAINT ck_CdpiDefinitions_SchemaVersion
    CHECK (DefinitionSchemaVersion >= 1);

-- Required creation audit actor
ALTER TABLE payroll.CdpiDefinitions
    ALTER COLUMN CreatedByUserID SET NOT NULL;