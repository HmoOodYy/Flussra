-- Import envelope, File source, and PayrollWork target. Dormant predecessor data
-- is disposable only by an explicit development reset, never by this migration.
DO $retire$
DECLARE
    names TEXT[] := ARRAY['import.importbatches','import.importfiles','import.importrows',
        'import.importdetectedcolumns','import.importmappingtemplates',
        'import.importmappingtemplatelines','import.importvalidationissues',
        'integration.integrationconnections','integration.integrationsyncruns'];
    ids OID[];
    name TEXT;
    populated BOOLEAN;
BEGIN
    SELECT array_agg(x::regclass::oid) INTO ids FROM unnest(names) x;
    -- Lock before checking; no writer may populate a predecessor after preflight.
    FOREACH name IN ARRAY names LOOP
        EXECUTE format('LOCK TABLE %s IN ACCESS EXCLUSIVE MODE', name);
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %s)', name) INTO populated;
        IF populated THEN
            RAISE EXCEPTION 'IMPORT_PREDECESSOR_DATA: % contains data; reset/reseed the development database.', name;
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE contype = 'f'
               AND confrelid = ANY(ids) AND NOT conrelid = ANY(ids))
       OR EXISTS (SELECT 1 FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid
                  WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = ANY(ids)
                    AND r.ev_class <> 'app.vw_importbatchsummary'::regclass)
       OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                  WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                    AND p.prosrc ~* '(import[.]|integration[.])[[:space:]"]*(importbatches|importfiles|importrows|importdetectedcolumns|importmappingtemplates|importmappingtemplatelines|importvalidationissues|integrationconnections|integrationsyncruns)') THEN
        RAISE EXCEPTION 'IMPORT_PREDECESSOR_DEPENDENCY: unexpected retained dependency; investigate before retrying.';
    END IF;
    -- RESTRICT also checks unusual catalog dependencies. All retirement DDL is
    -- in this one atomic statement even for the direct-SQL autocommit authority.
    DROP VIEW app.vw_ImportBatchSummary RESTRICT;
    DROP TABLE integration.IntegrationSyncRuns RESTRICT;
    DROP TABLE integration.IntegrationConnections RESTRICT;
    DROP TABLE import.ImportValidationIssues RESTRICT;
    DROP TABLE import.ImportDetectedColumns RESTRICT;
    DROP TABLE import.ImportRows RESTRICT;
    DROP TABLE import.ImportFiles RESTRICT;
    DROP TABLE import.ImportMappingTemplateLines RESTRICT;
    DROP TABLE import.ImportMappingTemplates RESTRICT;
    DROP TABLE import.ImportBatches RESTRICT;
END
$retire$;

CREATE UNIQUE INDEX ux_Users_ImportCompany ON sec.Users (UserID, CompanyID);
CREATE UNIQUE INDEX ux_ImportAppliedEvidence_Scope ON payroll.PayrollPeriodAuditEvidenceEvents
    (PayrollPeriodAuditEvidenceEventID, CompanyID, BranchID, PayrollPeriodID);
CREATE UNIQUE INDEX ux_ImportAppliedLine_Scope ON payroll.PayrollDraftLines
    (DraftLineID, CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, WorkDate, PayrollPeriodDefinitionID);

CREATE TABLE import.Imports (
    ImportID BIGSERIAL PRIMARY KEY,
    CompanyID INTEGER NOT NULL REFERENCES core.Companies(CompanyID),
    BranchID INTEGER,
    ImportKind VARCHAR(50) NOT NULL,
    SourceKind VARCHAR(50) NOT NULL,
    Status VARCHAR(20) NOT NULL DEFAULT 'NeedsMapping'
        CHECK (Status IN ('NeedsMapping','NeedsReview','Completed','Cancelled')),
    CorrelationID UUID NOT NULL UNIQUE,
    Revision BIGINT NOT NULL DEFAULT 1 CHECK (Revision > 0),
    ContractVersion INTEGER NOT NULL DEFAULT 1 CHECK (ContractVersion > 0),
    IdempotencyKey UUID NOT NULL,
    CreationFingerprint TEXT NOT NULL CHECK (CreationFingerprint ~ '^[0-9a-f]{64}$'),
    CreatedByUserID INTEGER NOT NULL,
    CreatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UpdatedByUserID INTEGER NOT NULL,
    UpdatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CompletedByUserID INTEGER,
    CompletedAtUtc TIMESTAMPTZ,
    CancelledByUserID INTEGER,
    CancelledAtUtc TIMESTAMPTZ,
    UNIQUE (CompanyID, IdempotencyKey),
    UNIQUE (ImportID, CompanyID),
    UNIQUE (ImportID, CompanyID, BranchID),
    FOREIGN KEY (BranchID, CompanyID) REFERENCES core.Branches(BranchID, CompanyID),
    FOREIGN KEY (CreatedByUserID, CompanyID) REFERENCES sec.Users(UserID, CompanyID),
    FOREIGN KEY (UpdatedByUserID, CompanyID) REFERENCES sec.Users(UserID, CompanyID),
    FOREIGN KEY (CompletedByUserID, CompanyID) REFERENCES sec.Users(UserID, CompanyID),
    FOREIGN KEY (CancelledByUserID, CompanyID) REFERENCES sec.Users(UserID, CompanyID),
    CHECK ((Status = 'Completed') = (CompletedAtUtc IS NOT NULL AND CompletedByUserID IS NOT NULL)),
    CHECK ((Status = 'Cancelled') = (CancelledAtUtc IS NOT NULL AND CancelledByUserID IS NOT NULL)),
    CHECK ((CompletedAtUtc IS NULL) = (CompletedByUserID IS NULL)),
    CHECK ((CancelledAtUtc IS NULL) = (CancelledByUserID IS NULL))
);

CREATE TABLE import.ImportSourceRecords (
    SourceRecordID BIGSERIAL PRIMARY KEY,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    Locator JSONB NOT NULL,
    Payload JSONB NOT NULL,
    UNIQUE (SourceRecordID, ImportID, CompanyID),
    FOREIGN KEY (ImportID, CompanyID) REFERENCES import.Imports(ImportID, CompanyID)
);

CREATE TABLE import.ImportIssues (
    ImportIssueID BIGSERIAL PRIMARY KEY,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    SourceRecordID BIGINT,
    Locator JSONB,
    Severity VARCHAR(10) NOT NULL CHECK (Severity IN ('Error','Warning','Conflict')),
    Code VARCHAR(100) NOT NULL,
    Message TEXT NOT NULL,
    FOREIGN KEY (ImportID, CompanyID) REFERENCES import.Imports(ImportID, CompanyID),
    FOREIGN KEY (SourceRecordID, ImportID, CompanyID)
        REFERENCES import.ImportSourceRecords(SourceRecordID, ImportID, CompanyID)
);

CREATE TABLE import.FileSources (
    ImportID BIGINT PRIMARY KEY,
    CompanyID INTEGER NOT NULL,
    ContractVersion INTEGER NOT NULL DEFAULT 1 CHECK (ContractVersion > 0),
    OriginalFileName VARCHAR(260) NOT NULL,
    FileType VARCHAR(4) NOT NULL CHECK (FileType IN ('csv','xlsx')),
    FileSizeBytes INTEGER NOT NULL CHECK (FileSizeBytes BETWEEN 1 AND 10000000),
    FileHashSha256 TEXT NOT NULL CHECK (FileHashSha256 ~ '^[0-9a-f]{64}$'),
    FileContent BYTEA,
    ContentPurgedAtUtc TIMESTAMPTZ,
    SheetName TEXT,
    HeaderRowNumber INTEGER CHECK (HeaderRowNumber BETWEEN 1 AND 20000),
    DateOrder VARCHAR(3) CHECK (DateOrder IN ('DMY','MDY')),
    InspectionMetadata JSONB,
    UNIQUE (ImportID, CompanyID),
    FOREIGN KEY (ImportID, CompanyID) REFERENCES import.Imports(ImportID, CompanyID),
    CHECK ((FileContent IS NULL) = (ContentPurgedAtUtc IS NOT NULL)),
    CHECK (FileContent IS NULL OR octet_length(FileContent) = FileSizeBytes),
    CHECK (InspectionMetadata IS NULL OR octet_length(InspectionMetadata::TEXT) <= 4000000),
    CHECK (ContentPurgedAtUtc IS NULL OR
           (InspectionMetadata IS NULL AND SheetName IS NULL AND HeaderRowNumber IS NULL AND DateOrder IS NULL))
);

CREATE TABLE import.FileSourceRecords (
    SourceRecordID BIGINT PRIMARY KEY,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    SheetName TEXT NOT NULL,
    SourceRowNumber INTEGER NOT NULL CHECK (SourceRowNumber BETWEEN 1 AND 20000),
    UNIQUE (ImportID, SheetName, SourceRowNumber),
    FOREIGN KEY (SourceRecordID, ImportID, CompanyID)
        REFERENCES import.ImportSourceRecords(SourceRecordID, ImportID, CompanyID),
    FOREIGN KEY (ImportID, CompanyID) REFERENCES import.FileSources(ImportID, CompanyID)
);

CREATE TABLE import.PayrollWorkImports (
    ImportID BIGINT PRIMARY KEY,
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PayrollPeriodID INTEGER NOT NULL,
    ContractVersion INTEGER NOT NULL DEFAULT 1 CHECK (ContractVersion > 0),
    CanonicalizationVersion INTEGER NOT NULL DEFAULT 1 CHECK (CanonicalizationVersion > 0),
    ColumnMapping JSONB,
    MappingFingerprint TEXT,
    ContentFingerprint TEXT,
    PlanFingerprint TEXT,
    PreviewCounts JSONB,
    ReviewSummary JSONB,
    UNIQUE (ImportID, CompanyID),
    UNIQUE (ImportID, CompanyID, BranchID, PayrollPeriodID),
    FOREIGN KEY (ImportID, CompanyID, BranchID) REFERENCES import.Imports(ImportID, CompanyID, BranchID),
    FOREIGN KEY (PayrollPeriodID, CompanyID, BranchID)
        REFERENCES payroll.PayrollPeriods(PayrollPeriodID, CompanyID, BranchID)
);
CREATE INDEX ix_PayrollWorkImports_Period ON import.PayrollWorkImports (CompanyID, PayrollPeriodID, ImportID);

CREATE TABLE import.PayrollWorkRecords (
    SourceRecordID BIGINT PRIMARY KEY,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    RowFingerprint TEXT,
    IsSkipped BOOLEAN NOT NULL DEFAULT FALSE,
    IsExcluded BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (SourceRecordID, ImportID, CompanyID),
    FOREIGN KEY (SourceRecordID, ImportID, CompanyID)
        REFERENCES import.ImportSourceRecords(SourceRecordID, ImportID, CompanyID),
    FOREIGN KEY (ImportID, CompanyID) REFERENCES import.PayrollWorkImports(ImportID, CompanyID)
);

CREATE TABLE import.PayrollWorkDriverMatches (
    DriverMatchID BIGSERIAL PRIMARY KEY,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    IdentifierKind VARCHAR(40) NOT NULL,
    NormalizedValue TEXT NOT NULL,
    DriverID INTEGER,
    Resolution VARCHAR(30) NOT NULL DEFAULT 'Unresolved',
    ConfirmedByUserID INTEGER,
    ConfirmedAtUtc TIMESTAMPTZ,
    UNIQUE (ImportID, IdentifierKind, NormalizedValue),
    UNIQUE (DriverMatchID, ImportID, CompanyID),
    FOREIGN KEY (ImportID, CompanyID) REFERENCES import.PayrollWorkImports(ImportID, CompanyID),
    FOREIGN KEY (DriverID, CompanyID) REFERENCES core.Drivers(DriverID, CompanyID),
    FOREIGN KEY (ConfirmedByUserID, CompanyID) REFERENCES sec.Users(UserID, CompanyID),
    CHECK ((ConfirmedByUserID IS NULL) = (ConfirmedAtUtc IS NULL))
);

CREATE TABLE import.PayrollWorkRecordIdentifiers (
    SourceRecordID BIGINT NOT NULL,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    SourceColumnIndex INTEGER NOT NULL CHECK (SourceColumnIndex BETWEEN 1 AND 50),
    DriverMatchID BIGINT NOT NULL,
    PRIMARY KEY (SourceRecordID, SourceColumnIndex),
    FOREIGN KEY (SourceRecordID, ImportID, CompanyID)
        REFERENCES import.PayrollWorkRecords(SourceRecordID, ImportID, CompanyID),
    FOREIGN KEY (DriverMatchID, ImportID, CompanyID)
        REFERENCES import.PayrollWorkDriverMatches(DriverMatchID, ImportID, CompanyID)
);

CREATE TABLE import.PayrollWorkCells (
    PayrollWorkCellID BIGSERIAL PRIMARY KEY,
    SourceRecordID BIGINT NOT NULL,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PayrollPeriodID INTEGER NOT NULL,
    SourceColumnIndex INTEGER NOT NULL CHECK (SourceColumnIndex BETWEEN 1 AND 50),
    WorkDate DATE,
    DriverAssignmentID INTEGER,
    PayrollPeriodDefinitionID INTEGER,
    PayDefinitionID INTEGER,
    RateDefinitionID INTEGER,
    RawValue TEXT,
    Quantity NUMERIC(18,4),
    IsBlank BOOLEAN NOT NULL DEFAULT FALSE,
    Outcome VARCHAR(40),
    CurrentValue JSONB,
    HasStatus BOOLEAN NOT NULL DEFAULT FALSE,
    IsDayOff BOOLEAN NOT NULL DEFAULT FALSE,
    Decision VARCHAR(40),
    DecisionBasis JSONB,
    AppliedDraftLineID BIGINT,
    AppliedAction VARCHAR(40),
    AppliedEvidenceEventID BIGINT,
    UNIQUE (SourceRecordID, SourceColumnIndex),
    FOREIGN KEY (SourceRecordID, ImportID, CompanyID)
        REFERENCES import.PayrollWorkRecords(SourceRecordID, ImportID, CompanyID),
    FOREIGN KEY (ImportID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES import.PayrollWorkImports(ImportID, CompanyID, BranchID, PayrollPeriodID),
    FOREIGN KEY (DriverAssignmentID, CompanyID, BranchID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, CompanyID, BranchID),
    FOREIGN KEY (PayrollPeriodID, WorkDate) REFERENCES payroll.PayrollPeriodDays(PayrollPeriodID, WorkDate),
    FOREIGN KEY (AppliedEvidenceEventID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollPeriodAuditEvidenceEvents
        (PayrollPeriodAuditEvidenceEventID, CompanyID, BranchID, PayrollPeriodID),
    FOREIGN KEY (AppliedDraftLineID, CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, WorkDate, PayrollPeriodDefinitionID)
        REFERENCES payroll.PayrollDraftLines
        (DraftLineID, CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, WorkDate, PayrollPeriodDefinitionID),
    CHECK (AppliedDraftLineID IS NULL OR
        (DriverAssignmentID IS NOT NULL AND WorkDate IS NOT NULL AND PayrollPeriodDefinitionID IS NOT NULL)),
    FOREIGN KEY (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID, PayDefinitionID, RateDefinitionID)
        REFERENCES payroll.PayrollPeriodDefinitions
        (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID, PayDefinitionID, RateDefinitionID),
    CHECK ((PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL AND RateDefinitionID IS NULL)
        OR (PayrollPeriodDefinitionID IS NOT NULL AND PayDefinitionID IS NOT NULL AND RateDefinitionID IS NOT NULL))
);

CREATE TABLE import.PayrollWorkStatusRemovals (
    StatusRemovalID BIGSERIAL PRIMARY KEY,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PayrollPeriodID INTEGER NOT NULL,
    DriverAssignmentID INTEGER NOT NULL,
    WorkDate DATE NOT NULL,
    RemovedStatusKeyID INTEGER NOT NULL,
    AppliedEvidenceEventID BIGINT NOT NULL,
    FOREIGN KEY (ImportID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES import.PayrollWorkImports(ImportID, CompanyID, BranchID, PayrollPeriodID),
    FOREIGN KEY (DriverAssignmentID, CompanyID, BranchID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, CompanyID, BranchID),
    FOREIGN KEY (RemovedStatusKeyID, CompanyID) REFERENCES payroll.PayrollStatusKeys(StatusKeyID, CompanyID)
    , FOREIGN KEY (AppliedEvidenceEventID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollPeriodAuditEvidenceEvents
        (PayrollPeriodAuditEvidenceEventID, CompanyID, BranchID, PayrollPeriodID)
    , FOREIGN KEY (PayrollPeriodID, WorkDate) REFERENCES payroll.PayrollPeriodDays(PayrollPeriodID, WorkDate)
);

-- Generic lifecycle protection has no PayrollPeriod or File interpretation.
CREATE FUNCTION import.fn_guard_import_envelope() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('DELETE','TRUNCATE') THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: durable Import envelopes cannot be removed.';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.Revision <> 1 OR NEW.Status <> 'NeedsMapping' THEN
            RAISE EXCEPTION 'IMPORT_LIFECYCLE: creation starts at revision 1, NeedsMapping.';
        END IF;
        RETURN NEW;
    END IF;
    IF (to_jsonb(NEW) - ARRAY['revision','updatedbyuserid','updatedatutc','status',
            'completedbyuserid','completedatutc','cancelledbyuserid','cancelledatutc'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['revision','updatedbyuserid','updatedatutc','status',
            'completedbyuserid','completedatutc','cancelledbyuserid','cancelledatutc'])
       OR NEW.Revision <> OLD.Revision + 1 THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: creation identity is immutable and revision must advance once.';
    END IF;
    IF OLD.Status IN ('Completed','Cancelled') AND
       (current_setting('flussra.import_purge_id', TRUE) IS DISTINCT FROM OLD.ImportID::TEXT
        OR (to_jsonb(NEW) - ARRAY['revision','updatedbyuserid','updatedatutc'])
            IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['revision','updatedbyuserid','updatedatutc'])) THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: completed/cancelled Import cannot change.';
    END IF;
    IF OLD.Status = 'NeedsMapping' AND NEW.Status = 'Completed' THEN
        RAISE EXCEPTION 'IMPORT_LIFECYCLE: an Import must be reviewed before completion.';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_Imports_Envelope BEFORE INSERT OR UPDATE OR DELETE ON import.Imports
    FOR EACH ROW EXECUTE FUNCTION import.fn_guard_import_envelope();
CREATE TRIGGER trg_Imports_NoTruncate BEFORE TRUNCATE ON import.Imports
    FOR EACH STATEMENT EXECUTE FUNCTION import.fn_guard_import_envelope();

-- A target-specific guard, independent of the generic envelope lifecycle.
CREATE FUNCTION import.fn_guard_payroll_work_detail() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    iid BIGINT;
    envelope import.Imports%ROWTYPE;
    target import.PayrollWorkImports%ROWTYPE;
    period_status TEXT;
    purging BOOLEAN;
    old_json JSONB;
    new_json JSONB;
BEGIN
    IF TG_OP = 'TRUNCATE' OR (TG_TABLE_NAME = 'payrollworkstatusremovals' AND TG_OP <> 'INSERT') THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: TRUNCATE is not an Import retention operation.';
    END IF;
    iid := CASE WHEN TG_OP = 'DELETE' THEN OLD.ImportID ELSE NEW.ImportID END;
    IF TG_OP = 'UPDATE' AND (NEW.ImportID, NEW.CompanyID) IS DISTINCT FROM (OLD.ImportID, OLD.CompanyID) THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: detail ownership cannot change.';
    END IF;
    SELECT * INTO target FROM import.PayrollWorkImports WHERE ImportID = iid;
    IF TG_TABLE_NAME = 'payrollworkimports' THEN
        IF TG_OP <> 'INSERT' AND (NEW.PayrollPeriodID, NEW.BranchID)
            IS DISTINCT FROM (OLD.PayrollPeriodID, OLD.BranchID) THEN
            RAISE EXCEPTION 'IMPORT_IMMUTABLE: PayrollWork target cannot change.';
        END IF;
        IF TG_OP = 'INSERT' THEN target := NEW; END IF;
    END IF;
    IF target.ImportID IS NOT NULL THEN
        SELECT Status INTO period_status FROM payroll.PayrollPeriods
            WHERE PayrollPeriodID = target.PayrollPeriodID AND CompanyID = target.CompanyID FOR UPDATE;
    END IF;
    SELECT * INTO envelope FROM import.Imports WHERE ImportID = iid FOR UPDATE;
    purging := current_setting('flussra.import_purge_id', TRUE) = iid::TEXT
        AND target.ImportID IS NOT NULL AND period_status IN ('Locked','Archived');
    IF purging THEN
        IF TG_TABLE_NAME = 'payrollworkstatusremovals' OR TG_OP = 'INSERT' THEN
            RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: durable evidence cannot change and detail cannot be inserted.';
        END IF;
        IF TG_OP = 'DELETE' THEN
            IF TG_TABLE_NAME IN ('filesources','payrollworkimports') THEN
                RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: durable source/target metadata cannot be removed.';
            END IF;
            RETURN OLD;
        END IF;
        old_json := to_jsonb(OLD);
        new_json := to_jsonb(NEW);
        IF TG_TABLE_NAME = 'filesources' THEN
            IF (old_json - ARRAY['filecontent','contentpurgedatutc','sheetname','headerrownumber','dateorder','inspectionmetadata'])
                IS DISTINCT FROM (new_json - ARRAY['filecontent','contentpurgedatutc','sheetname','headerrownumber','dateorder','inspectionmetadata'])
                OR NEW.FileContent IS NOT NULL OR NEW.ContentPurgedAtUtc IS NULL
                OR NEW.SheetName IS NOT NULL OR NEW.HeaderRowNumber IS NOT NULL
                OR NEW.DateOrder IS NOT NULL OR NEW.InspectionMetadata IS NOT NULL THEN
                RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: only File detail can be purged.';
            END IF;
        ELSIF TG_TABLE_NAME = 'payrollworkimports' THEN
            IF (old_json - ARRAY['columnmapping','mappingfingerprint','contentfingerprint','planfingerprint','previewcounts'])
                IS DISTINCT FROM (new_json - ARRAY['columnmapping','mappingfingerprint','contentfingerprint','planfingerprint','previewcounts'])
                OR NEW.ColumnMapping IS NOT NULL OR NEW.MappingFingerprint IS NOT NULL
                OR NEW.ContentFingerprint IS NOT NULL OR NEW.PlanFingerprint IS NOT NULL
                OR NEW.PreviewCounts IS NOT NULL THEN
                RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: only transient PayrollWork plan can be purged.';
            END IF;
        ELSE
            RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: detail is deleted, never rewritten during purge.';
        END IF;
        RETURN NEW;
    END IF;
    IF envelope.Status IN ('Completed','Cancelled')
       OR (target.ImportID IS NOT NULL AND period_status NOT IN ('Draft','Open','Returned'))
       OR EXISTS (SELECT 1 FROM import.FileSources WHERE ImportID = iid AND ContentPurgedAtUtc IS NOT NULL) THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: Import detail is no longer editable.';
    END IF;
    IF TG_TABLE_NAME = 'filesources' AND envelope.SourceKind <> 'File' THEN
        RAISE EXCEPTION 'IMPORT_SOURCE_KIND: File source requires File envelope.';
    END IF;
    IF TG_TABLE_NAME = 'filesources' THEN
        IF TG_OP = 'INSERT' AND NEW.ContentPurgedAtUtc IS NOT NULL THEN
            RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: File creation requires original content; retention occurs later.';
        END IF;
    END IF;
    IF TG_TABLE_NAME = 'payrollworkimports' AND envelope.ImportKind <> 'PayrollWork' THEN
        RAISE EXCEPTION 'IMPORT_TARGET_KIND: PayrollWork target requires PayrollWork envelope.';
    END IF;
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME IN ('filesources','payrollworkimports') THEN
        old_json := to_jsonb(OLD);
        new_json := to_jsonb(NEW);
        IF TG_TABLE_NAME = 'filesources' AND
           (old_json - ARRAY['sheetname','headerrownumber','dateorder','inspectionmetadata'])
            IS DISTINCT FROM (new_json - ARRAY['sheetname','headerrownumber','dateorder','inspectionmetadata']) THEN
            RAISE EXCEPTION 'IMPORT_IMMUTABLE: original File identity/content/version cannot change.';
        ELSIF TG_TABLE_NAME = 'payrollworkimports' AND
           (old_json - ARRAY['columnmapping','mappingfingerprint','contentfingerprint','planfingerprint','previewcounts','reviewsummary'])
            IS DISTINCT FROM (new_json - ARRAY['columnmapping','mappingfingerprint','contentfingerprint','planfingerprint','previewcounts','reviewsummary']) THEN
            RAISE EXCEPTION 'IMPORT_IMMUTABLE: target identity/version cannot change.';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' AND TG_TABLE_NAME IN ('filesources','payrollworkimports') THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: source/target metadata is durable.';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

DO $guards$
DECLARE name TEXT;
BEGIN
    FOREACH name IN ARRAY ARRAY['importsourceRecords','importissues','filesources','filesourcerecords',
        'payrollworkimports','payrollworkrecords','payrollworkdrivermatches',
        'payrollworkrecordidentifiers','payrollworkcells','payrollworkstatusremovals'] LOOP
        EXECUTE format('CREATE TRIGGER trg_ImportDetail_Writable BEFORE INSERT OR UPDATE OR DELETE ON import.%I FOR EACH ROW EXECUTE FUNCTION import.fn_guard_payroll_work_detail()', lower(name));
        EXECUTE format('CREATE TRIGGER trg_ImportDetail_NoTruncate BEFORE TRUNCATE ON import.%I FOR EACH STATEMENT EXECUTE FUNCTION import.fn_guard_payroll_work_detail()', lower(name));
    END LOOP;
END
$guards$;

-- The PayrollWork specialization constrains envelope mutations separately;
-- future Import kinds do not inherit a mandatory Payroll lifecycle.
CREATE FUNCTION import.fn_guard_payroll_work_envelope() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE target import.PayrollWorkImports%ROWTYPE; period_status TEXT;
BEGIN
    SELECT * INTO target FROM import.PayrollWorkImports WHERE ImportID = OLD.ImportID;
    IF target.ImportID IS NULL THEN RETURN NEW; END IF;
    SELECT Status INTO period_status FROM payroll.PayrollPeriods
        WHERE PayrollPeriodID = target.PayrollPeriodID AND CompanyID = target.CompanyID FOR UPDATE;
    IF period_status NOT IN ('Draft','Open','Returned') THEN
        IF current_setting('flussra.import_purge_id', TRUE) IS DISTINCT FROM OLD.ImportID::TEXT
            OR period_status NOT IN ('Locked','Archived')
            OR (to_jsonb(NEW) - ARRAY['revision','updatedbyuserid','updatedatutc'])
                IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['revision','updatedbyuserid','updatedatutc']) THEN
            RAISE EXCEPTION 'IMPORT_IMMUTABLE: PayrollWork period is no longer editable.';
        END IF;
    ELSIF OLD.Status IN ('Completed','Cancelled') THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: completed/cancelled PayrollWork Import cannot change.';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_Imports_PayrollWork BEFORE UPDATE ON import.Imports
    FOR EACH ROW EXECUTE FUNCTION import.fn_guard_payroll_work_envelope();
