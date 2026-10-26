-- Replace disposable A1 single-file/cell staging; canonical Payroll is unchanged.
-- One DO statement keeps direct-SQL cutover atomic as well as Alembic cutover.
DO $cutover$
DECLARE name TEXT;
BEGIN
    LOCK TABLE import.Imports IN ACCESS EXCLUSIVE MODE;
    FOREACH name IN ARRAY ARRAY['importissues','importsourcerecords','filesources','filesourcerecords',
        'payrollworkimports','payrollworkrecords','payrollworkdrivermatches',
        'payrollworkrecordidentifiers','payrollworkcells','payrollworkstatusremovals'] LOOP
        EXECUTE format('LOCK TABLE import.%I IN ACCESS EXCLUSIVE MODE', name);
    END LOOP;
    IF EXISTS (SELECT 1 FROM import.Imports WHERE ImportKind <> 'PayrollWork' OR SourceKind <> 'File'
               OR Status = 'Completed')
       OR EXISTS (SELECT 1 FROM import.PayrollWorkStatusRemovals)
       OR EXISTS (SELECT 1 FROM import.PayrollWorkCells WHERE AppliedDraftLineID IS NOT NULL
                   OR AppliedEvidenceEventID IS NOT NULL OR AppliedAction IS NOT NULL) THEN
        RAISE EXCEPTION 'IMPORT_CUTOVER_PROTECTED: unsupported/applied history exists; investigate or reset/reseed preproduction.';
    END IF;
    DROP TRIGGER trg_Imports_Envelope ON import.Imports;
    DROP TRIGGER trg_Imports_PayrollWork ON import.Imports;
    FOREACH name IN ARRAY ARRAY['importissues','importsourcerecords','filesources','filesourcerecords',
        'payrollworkimports','payrollworkrecords','payrollworkdrivermatches',
        'payrollworkrecordidentifiers','payrollworkcells','payrollworkstatusremovals'] LOOP
        EXECUTE format('DROP TRIGGER trg_ImportDetail_Writable ON import.%I', name);
        EXECUTE format('DROP TRIGGER trg_ImportDetail_NoTruncate ON import.%I', name);
    END LOOP;
    DROP FUNCTION import.fn_guard_payroll_work_detail();
    DROP FUNCTION import.fn_guard_payroll_work_envelope();
    DROP TABLE import.PayrollWorkStatusRemovals RESTRICT;
    DROP TABLE import.PayrollWorkCells RESTRICT;
    DROP TABLE import.PayrollWorkRecordIdentifiers RESTRICT;
    DROP TABLE import.PayrollWorkDriverMatches RESTRICT;
    DROP TABLE import.PayrollWorkRecords RESTRICT;
    DROP TABLE import.FileSourceRecords RESTRICT;
    DROP TABLE import.FileSources RESTRICT;
    DROP TABLE import.PayrollWorkImports RESTRICT;
    DELETE FROM import.ImportIssues;
    DELETE FROM import.ImportSourceRecords;
    DELETE FROM import.Imports;

    CREATE TABLE import.PayrollWorkImports (
        ImportID BIGINT PRIMARY KEY,
        CompanyID INTEGER NOT NULL,
        BranchID INTEGER NOT NULL,
        PayrollPeriodID INTEGER NOT NULL,
        ContractVersion INTEGER NOT NULL DEFAULT 2 CHECK (ContractVersion > 0),
        CanonicalizationVersion INTEGER NOT NULL DEFAULT 3 CHECK (CanonicalizationVersion > 0),
        DefaultWorkDate DATE,
        DetailPurgedAtUtc TIMESTAMPTZ,
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
    CREATE INDEX ix_PayrollWorkImports_Period ON import.PayrollWorkImports(CompanyID, PayrollPeriodID, ImportID);

    CREATE TABLE import.FileSourceBatches (
        ImportID BIGINT PRIMARY KEY,
        CompanyID INTEGER NOT NULL,
        ContractVersion INTEGER NOT NULL DEFAULT 2 CHECK (ContractVersion > 0),
        SourceManifest JSONB NOT NULL CHECK (jsonb_typeof(SourceManifest) = 'array' AND jsonb_array_length(SourceManifest) > 0),
        UNIQUE (ImportID, CompanyID),
        FOREIGN KEY (ImportID, CompanyID) REFERENCES import.Imports(ImportID, CompanyID)
    );

    CREATE TABLE import.FileSources (
        FileSourceID BIGSERIAL PRIMARY KEY,
        ImportID BIGINT NOT NULL,
        CompanyID INTEGER NOT NULL,
        FileOrdinal INTEGER NOT NULL CHECK (FileOrdinal > 0),
        ContractVersion INTEGER NOT NULL DEFAULT 2 CHECK (ContractVersion > 0),
        OriginalFileName VARCHAR(260) NOT NULL,
        FileType VARCHAR(4) NOT NULL CHECK (FileType IN ('csv','xlsx')),
        FileSizeBytes BIGINT NOT NULL CHECK (FileSizeBytes > 0),
        FileHashSha256 TEXT NOT NULL CHECK (FileHashSha256 ~ '^[0-9a-f]{64}$'),
        FileContent BYTEA,
        ContentPurgedAtUtc TIMESTAMPTZ,
        InspectionMetadata JSONB,
        UNIQUE (FileSourceID, ImportID, CompanyID),
        UNIQUE (ImportID, FileOrdinal),
        FOREIGN KEY (ImportID, CompanyID) REFERENCES import.FileSourceBatches(ImportID, CompanyID),
        CHECK ((FileContent IS NULL) = (ContentPurgedAtUtc IS NOT NULL)),
        CHECK (FileContent IS NULL OR octet_length(FileContent) = FileSizeBytes),
        CONSTRAINT ck_FileSources_InspectionPurge CHECK (ContentPurgedAtUtc IS NULL OR InspectionMetadata IS NULL)
    );
    CREATE INDEX ix_FileSources_Import ON import.FileSources(ImportID, CompanyID);

    CREATE TABLE import.SourceLayouts (
        SourceLayoutID BIGSERIAL PRIMARY KEY,
        ImportID BIGINT NOT NULL,
        CompanyID INTEGER NOT NULL,
        StructureFingerprint TEXT NOT NULL CHECK (StructureFingerprint ~ '^[0-9a-f]{64}$'),
        Label TEXT,
        ColumnMapping JSONB CHECK (ColumnMapping IS NULL OR jsonb_typeof(ColumnMapping) = 'object'),
        MappingFingerprint TEXT,
        InspectionMetadata JSONB,
        UNIQUE (SourceLayoutID, ImportID, CompanyID),
        FOREIGN KEY (ImportID, CompanyID) REFERENCES import.Imports(ImportID, CompanyID)
    );
    CREATE INDEX ix_SourceLayouts_Structure ON import.SourceLayouts(ImportID, CompanyID, StructureFingerprint);

    CREATE TABLE import.FileSourceUnits (
        SourceUnitID BIGSERIAL PRIMARY KEY,
        FileSourceID BIGINT NOT NULL,
        ImportID BIGINT NOT NULL,
        CompanyID INTEGER NOT NULL,
        UnitName TEXT NOT NULL,
        UnitOrdinal INTEGER NOT NULL CHECK (UnitOrdinal > 0),
        UnitKind VARCHAR(12) NOT NULL CHECK (UnitKind IN ('CSV','Worksheet')),
        IsSelected BOOLEAN NOT NULL DEFAULT FALSE,
        HeaderRowNumber INTEGER NOT NULL CHECK (HeaderRowNumber > 0),
        DateOrder VARCHAR(3) CONSTRAINT ck_FileSourceUnits_DateOrder CHECK (DateOrder IN ('DMY','MDY','YMD')),
        DateSource JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(DateSource) = 'object'),
        Encoding VARCHAR(20) CHECK (Encoding IN ('utf-8','utf-8-sig','cp1252')),
        Delimiter VARCHAR(1) CHECK (Delimiter IN (',',chr(59),chr(9))),
        SourceLayoutID BIGINT,
        InspectionMetadata JSONB,
        UNIQUE (SourceUnitID, ImportID, CompanyID),
        UNIQUE (FileSourceID, UnitName),
        UNIQUE (FileSourceID, UnitOrdinal),
        FOREIGN KEY (FileSourceID, ImportID, CompanyID) REFERENCES import.FileSources(FileSourceID, ImportID, CompanyID),
        FOREIGN KEY (SourceLayoutID, ImportID, CompanyID) REFERENCES import.SourceLayouts(SourceLayoutID, ImportID, CompanyID)
    );
    CREATE INDEX ix_FileSourceUnits_Import ON import.FileSourceUnits(ImportID, CompanyID, SourceUnitID);
    CREATE INDEX ix_FileSourceUnits_Layout ON import.FileSourceUnits(SourceLayoutID, ImportID, CompanyID);

    CREATE TABLE import.FileSourceRecords (
        SourceRecordID BIGINT PRIMARY KEY,
        ImportID BIGINT NOT NULL,
        CompanyID INTEGER NOT NULL,
        SourceUnitID BIGINT NOT NULL,
        SourceRowNumber INTEGER NOT NULL CHECK (SourceRowNumber > 0),
        UNIQUE (SourceRecordID, ImportID, CompanyID),
        UNIQUE (SourceUnitID, SourceRowNumber),
        FOREIGN KEY (SourceRecordID, ImportID, CompanyID)
            REFERENCES import.ImportSourceRecords(SourceRecordID, ImportID, CompanyID),
        FOREIGN KEY (SourceUnitID, ImportID, CompanyID)
            REFERENCES import.FileSourceUnits(SourceUnitID, ImportID, CompanyID)
    );

    CREATE TABLE import.PayrollWorkPreviewGroups (
        PreviewGroupID BIGSERIAL PRIMARY KEY,
        ImportID BIGINT NOT NULL,
        CompanyID INTEGER NOT NULL,
        BranchID INTEGER NOT NULL,
        PayrollPeriodID INTEGER NOT NULL,
        GroupFingerprint TEXT NOT NULL CHECK (GroupFingerprint ~ '^[0-9a-f]{64}$'),
        DriverID INTEGER,
        WorkDate DATE,
        DriverDisplay JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(DriverDisplay) = 'object'),
        ErrorCount INTEGER NOT NULL DEFAULT 0 CHECK (ErrorCount >= 0),
        WarningCount INTEGER NOT NULL DEFAULT 0 CHECK (WarningCount >= 0),
        ConflictCount INTEGER NOT NULL DEFAULT 0 CHECK (ConflictCount >= 0),
        UNIQUE (PreviewGroupID, ImportID, CompanyID),
        UNIQUE (ImportID, GroupFingerprint),
        FOREIGN KEY (ImportID, CompanyID, BranchID, PayrollPeriodID)
            REFERENCES import.PayrollWorkImports(ImportID, CompanyID, BranchID, PayrollPeriodID),
        FOREIGN KEY (DriverID, CompanyID) REFERENCES core.Drivers(DriverID, CompanyID),
        FOREIGN KEY (PayrollPeriodID, WorkDate) REFERENCES payroll.PayrollPeriodDays(PayrollPeriodID, WorkDate)
    );
    CREATE INDEX ix_PayrollWorkPreviewGroups_Import ON import.PayrollWorkPreviewGroups(ImportID, CompanyID, PreviewGroupID);

    CREATE TABLE import.PayrollWorkRecords (
        SourceRecordID BIGINT PRIMARY KEY,
        ImportID BIGINT NOT NULL,
        CompanyID INTEGER NOT NULL,
        BranchID INTEGER NOT NULL,
        PayrollPeriodID INTEGER NOT NULL,
        PreviewGroupID BIGINT NOT NULL,
        DriverID INTEGER,
        DriverAssignmentID INTEGER,
        WorkDate DATE,
        RowFingerprint TEXT NOT NULL CHECK (RowFingerprint ~ '^[0-9a-f]{64}$'),
        IsSkipped BOOLEAN NOT NULL DEFAULT FALSE,
        IsExcluded BOOLEAN NOT NULL DEFAULT FALSE,
        ValuesJSON JSONB NOT NULL CHECK (jsonb_typeof(ValuesJSON) = 'object'),
        IssuesJSON JSONB NOT NULL CHECK (jsonb_typeof(IssuesJSON) = 'array'),
        ContextJSON JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(ContextJSON) = 'object'),
        UNIQUE (SourceRecordID, ImportID, CompanyID),
        FOREIGN KEY (SourceRecordID, ImportID, CompanyID)
            REFERENCES import.ImportSourceRecords(SourceRecordID, ImportID, CompanyID),
        FOREIGN KEY (ImportID, CompanyID, BranchID, PayrollPeriodID)
            REFERENCES import.PayrollWorkImports(ImportID, CompanyID, BranchID, PayrollPeriodID),
        FOREIGN KEY (PreviewGroupID, ImportID, CompanyID)
            REFERENCES import.PayrollWorkPreviewGroups(PreviewGroupID, ImportID, CompanyID),
        FOREIGN KEY (DriverID, CompanyID) REFERENCES core.Drivers(DriverID, CompanyID),
        FOREIGN KEY (DriverAssignmentID, CompanyID, BranchID)
            REFERENCES core.DriverAssignments(DriverAssignmentID, CompanyID, BranchID),
        FOREIGN KEY (PayrollPeriodID, WorkDate) REFERENCES payroll.PayrollPeriodDays(PayrollPeriodID, WorkDate)
    );
    CREATE INDEX ix_PayrollWorkRecords_Group ON import.PayrollWorkRecords(PreviewGroupID, ImportID, CompanyID, SourceRecordID);
    CREATE INDEX ix_PayrollWorkRecords_Import ON import.PayrollWorkRecords(ImportID, CompanyID, SourceRecordID);
    CREATE INDEX ix_ImportSourceRecords_Import ON import.ImportSourceRecords(ImportID, CompanyID, SourceRecordID);

    CREATE TABLE import.PayrollWorkDriverMatches (
        DriverMatchID BIGSERIAL PRIMARY KEY,
        ImportID BIGINT NOT NULL,
        CompanyID INTEGER NOT NULL,
        IdentifierKind VARCHAR(40) NOT NULL CHECK (IdentifierKind IN ('DriverNumber','EmployeeKey','DriverName')),
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
        SourceColumnIndex INTEGER NOT NULL CHECK (SourceColumnIndex > 0),
        DriverMatchID BIGINT NOT NULL,
        PRIMARY KEY (SourceRecordID, SourceColumnIndex),
        FOREIGN KEY (SourceRecordID, ImportID, CompanyID)
            REFERENCES import.PayrollWorkRecords(SourceRecordID, ImportID, CompanyID),
        FOREIGN KEY (DriverMatchID, ImportID, CompanyID)
            REFERENCES import.PayrollWorkDriverMatches(DriverMatchID, ImportID, CompanyID)
    );
    CREATE INDEX ix_PayrollWorkRecordIdentifiers_MatchScope ON import.PayrollWorkRecordIdentifiers(DriverMatchID, ImportID, CompanyID);
    CREATE INDEX ix_PayrollWorkRecordIdentifiers_Import ON import.PayrollWorkRecordIdentifiers(ImportID, CompanyID, SourceRecordID);
    CREATE INDEX ix_ImportIssues_SourceRecordScope ON import.ImportIssues(SourceRecordID, ImportID, CompanyID);

    EXECUTE $fn$
    CREATE FUNCTION import.fn_guard_payroll_work_detail() RETURNS TRIGGER LANGUAGE plpgsql AS $body$
    DECLARE iid BIGINT; envelope import.Imports%ROWTYPE; target import.PayrollWorkImports%ROWTYPE;
        period_status TEXT; purging BOOLEAN; old_json JSONB; new_json JSONB; manifest_file JSONB;
        unit_file import.FileSources%ROWTYPE; source_batch import.FileSourceBatches%ROWTYPE;
    BEGIN
        IF TG_OP = 'TRUNCATE' THEN
            RAISE EXCEPTION 'IMPORT_IMMUTABLE: TRUNCATE is not retention.';
        END IF;
        iid := CASE WHEN TG_OP = 'DELETE' THEN OLD.ImportID ELSE NEW.ImportID END;
        IF TG_OP = 'UPDATE' AND (NEW.ImportID, NEW.CompanyID) IS DISTINCT FROM (OLD.ImportID, OLD.CompanyID) THEN
            RAISE EXCEPTION 'IMPORT_IMMUTABLE: detail ownership cannot change.';
        END IF;
        SELECT * INTO target FROM import.PayrollWorkImports WHERE ImportID = iid;
        IF TG_TABLE_NAME = 'payrollworkimports' THEN
            IF TG_OP <> 'INSERT' AND (NEW.PayrollPeriodID, NEW.BranchID) IS DISTINCT FROM (OLD.PayrollPeriodID, OLD.BranchID) THEN
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: target cannot change.';
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
            IF TG_OP = 'INSERT' THEN RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: no new detail during purge.'; END IF;
            IF TG_OP = 'DELETE' THEN
                IF TG_TABLE_NAME IN ('filesourcebatches','filesources','filesourceunits','sourcelayouts','payrollworkimports') THEN
                    RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: accepted source/target metadata is durable.';
                END IF;
                RETURN OLD;
            END IF;
            old_json := to_jsonb(OLD); new_json := to_jsonb(NEW);
            IF TG_TABLE_NAME = 'filesources' THEN
                IF (old_json - ARRAY['filecontent','contentpurgedatutc','inspectionmetadata'])
                    IS DISTINCT FROM (new_json - ARRAY['filecontent','contentpurgedatutc','inspectionmetadata'])
                    OR NEW.FileContent IS NOT NULL OR NEW.ContentPurgedAtUtc IS NULL OR NEW.InspectionMetadata IS NOT NULL
                    OR (OLD.ContentPurgedAtUtc IS NOT NULL AND NEW.ContentPurgedAtUtc IS DISTINCT FROM OLD.ContentPurgedAtUtc) THEN
                    RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: only File detail can clear.';
                END IF;
            ELSIF TG_TABLE_NAME IN ('filesourceunits','sourcelayouts') THEN
                IF (old_json - 'inspectionmetadata') IS DISTINCT FROM (new_json - 'inspectionmetadata')
                    OR NEW.InspectionMetadata IS NOT NULL THEN
                    RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: only interpretation samples can clear.';
                END IF;
            ELSIF TG_TABLE_NAME = 'payrollworkimports' THEN
                IF (old_json - ARRAY['planfingerprint','previewcounts','detailpurgedatutc'])
                    IS DISTINCT FROM (new_json - ARRAY['planfingerprint','previewcounts','detailpurgedatutc'])
                    OR NEW.PlanFingerprint IS NOT NULL OR NEW.PreviewCounts IS NOT NULL OR NEW.DetailPurgedAtUtc IS NULL
                    OR (OLD.DetailPurgedAtUtc IS NOT NULL AND NEW.DetailPurgedAtUtc IS DISTINCT FROM OLD.DetailPurgedAtUtc) THEN
                    RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: accepted identity survives purge.';
                END IF;
            ELSE RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: detailed records are deleted, not rewritten.';
            END IF;
            RETURN NEW;
        END IF;
        IF envelope.Status IN ('Completed','Cancelled')
           OR (target.ImportID IS NOT NULL AND (period_status NOT IN ('Draft','Open','Returned') OR target.DetailPurgedAtUtc IS NOT NULL)) THEN
            RAISE EXCEPTION 'IMPORT_IMMUTABLE: detail is no longer editable.';
        END IF;
        IF TG_OP = 'DELETE' AND TG_TABLE_NAME IN ('filesourcebatches','filesources','filesourceunits','sourcelayouts','payrollworkimports') THEN
            RAISE EXCEPTION 'IMPORT_IMMUTABLE: source/target metadata is durable.';
        END IF;
        IF TG_OP = 'INSERT' THEN
            IF TG_TABLE_NAME = 'payrollworkimports' AND envelope.ImportKind <> 'PayrollWork' THEN
                RAISE EXCEPTION 'IMPORT_TARGET_KIND: wrong target kind.';
            ELSIF TG_TABLE_NAME = 'filesourcebatches' THEN
                IF envelope.SourceKind <> 'File' THEN RAISE EXCEPTION 'IMPORT_SOURCE_KIND: File batch needs File envelope.'; END IF;
            ELSIF TG_TABLE_NAME = 'filesources' THEN
                IF envelope.SourceKind <> 'File' OR NEW.ContentPurgedAtUtc IS NOT NULL THEN
                    RAISE EXCEPTION 'IMPORT_SOURCE_KIND: File requires live File/PayrollWork owner.';
                END IF;
                SELECT * INTO source_batch FROM import.FileSourceBatches WHERE ImportID = iid AND CompanyID = NEW.CompanyID;
                manifest_file := source_batch.SourceManifest->(NEW.FileOrdinal - 1);
                IF manifest_file IS NULL OR manifest_file->>'name' IS DISTINCT FROM NEW.OriginalFileName
                    OR manifest_file->>'type' IS DISTINCT FROM NEW.FileType
                    OR manifest_file->>'sha256' IS DISTINCT FROM NEW.FileHashSha256
                    OR (manifest_file->>'size')::BIGINT IS DISTINCT FROM NEW.FileSizeBytes THEN
                    RAISE EXCEPTION 'IMPORT_MANIFEST_SCOPE: File is outside immutable creation manifest.';
                END IF;
            ELSIF TG_TABLE_NAME = 'filesourceunits' THEN
                SELECT * INTO unit_file FROM import.FileSources WHERE FileSourceID = NEW.FileSourceID
                    AND ImportID = iid AND CompanyID = NEW.CompanyID;
                SELECT * INTO source_batch FROM import.FileSourceBatches WHERE ImportID = iid AND CompanyID = NEW.CompanyID;
                manifest_file := source_batch.SourceManifest->(unit_file.FileOrdinal - 1);
                IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(manifest_file->'units') u
                               WHERE u->>'name' = NEW.UnitName AND (u->>'index')::INTEGER = NEW.UnitOrdinal)
                    OR (unit_file.FileType = 'csv') IS DISTINCT FROM (NEW.UnitKind = 'CSV') THEN
                    RAISE EXCEPTION 'IMPORT_MANIFEST_SCOPE: source unit is outside its owning File.';
                END IF;
            END IF;
        END IF;
        IF TG_OP = 'UPDATE' THEN
            old_json := to_jsonb(OLD); new_json := to_jsonb(NEW);
            IF TG_TABLE_NAME IN ('filesources','filesourcebatches') AND old_json IS DISTINCT FROM new_json THEN
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: original File is immutable.';
            ELSIF TG_TABLE_NAME = 'filesourceunits'
                AND (old_json - ARRAY['isselected','headerrownumber','dateorder','datesource','encoding','delimiter','sourcelayoutid','inspectionmetadata'])
                    IS DISTINCT FROM (new_json - ARRAY['isselected','headerrownumber','dateorder','datesource','encoding','delimiter','sourcelayoutid','inspectionmetadata']) THEN
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: source-unit identity is immutable.';
            ELSIF TG_TABLE_NAME = 'sourcelayouts'
                AND (old_json - ARRAY['label','columnmapping','mappingfingerprint'])
                    IS DISTINCT FROM (new_json - ARRAY['label','columnmapping','mappingfingerprint']) THEN
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: source-layout structure is immutable.';
            ELSIF TG_TABLE_NAME = 'filesourcerecords' AND old_json IS DISTINCT FROM new_json THEN
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: physical provenance cannot change.';
            ELSIF TG_TABLE_NAME = 'importsourcerecords' AND old_json->'sourcerecordid' IS DISTINCT FROM new_json->'sourcerecordid' THEN
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: source record identity cannot change.';
            ELSIF TG_TABLE_NAME = 'payrollworkpreviewgroups'
                AND (old_json - ARRAY['errorcount','warningcount','conflictcount'])
                    IS DISTINCT FROM (new_json - ARRAY['errorcount','warningcount','conflictcount']) THEN
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: grid group identity cannot change.';
            ELSIF TG_TABLE_NAME = 'payrollworkrecords'
                AND (old_json - 'isexcluded') IS DISTINCT FROM (new_json - 'isexcluded') THEN
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: normalized source detail is rebuilt, not reinterpreted in place.';
            ELSIF TG_TABLE_NAME = 'payrollworkrecordidentifiers' AND old_json IS DISTINCT FROM new_json THEN
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: source identifier association cannot change.';
            ELSIF TG_TABLE_NAME = 'payrollworkimports'
                AND (old_json - ARRAY['defaultworkdate','mappingfingerprint','contentfingerprint','planfingerprint','previewcounts','reviewsummary'])
                    IS DISTINCT FROM (new_json - ARRAY['defaultworkdate','mappingfingerprint','contentfingerprint','planfingerprint','previewcounts','reviewsummary']) THEN
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: target/manifest/version is immutable.';
            END IF;
        END IF;
        IF TG_OP <> 'DELETE' AND TG_TABLE_NAME = 'payrollworkrecords' THEN
            IF EXISTS (SELECT 1 FROM jsonb_object_keys(NEW.ValuesJSON) k WHERE k !~ '^[1-9][0-9]*$')
               OR EXISTS (SELECT 1 FROM jsonb_each(NEW.ValuesJSON) v
                   WHERE jsonb_typeof(v.value) <> 'object'
                       OR jsonb_typeof(v.value->'source_column') IS DISTINCT FROM 'number'
                       OR (v.value->>'source_column') !~ '^[1-9][0-9]*$') THEN
                RAISE EXCEPTION 'IMPORT_PAYLOAD_SHAPE: malformed supplied values.';
            END IF;
            IF EXISTS (SELECT 1 FROM jsonb_object_keys(NEW.ValuesJSON) k WHERE NOT EXISTS (
                SELECT 1 FROM payroll.PayrollPeriodDefinitions d WHERE d.PayrollPeriodDefinitionID = k::INTEGER
                    AND d.PayrollPeriodID = NEW.PayrollPeriodID AND d.CompanyID = NEW.CompanyID AND d.BranchID = NEW.BranchID)) THEN
                RAISE EXCEPTION 'IMPORT_PAYLOAD_SCOPE: supplied definition belongs to another period.';
            END IF;
            IF NEW.DriverAssignmentID IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM core.DriverAssignments a WHERE a.DriverAssignmentID = NEW.DriverAssignmentID
                    AND a.CompanyID = NEW.CompanyID AND a.BranchID = NEW.BranchID AND a.DriverID = NEW.DriverID) THEN
                RAISE EXCEPTION 'IMPORT_PAYLOAD_SCOPE: assignment and permanent Driver disagree.';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM import.PayrollWorkPreviewGroups g WHERE g.PreviewGroupID = NEW.PreviewGroupID
                AND g.ImportID = iid AND g.CompanyID = NEW.CompanyID
                AND g.DriverID IS NOT DISTINCT FROM NEW.DriverID AND g.WorkDate IS NOT DISTINCT FROM NEW.WorkDate) THEN
                RAISE EXCEPTION 'IMPORT_PAYLOAD_SCOPE: normalized row disagrees with its grid group.';
            END IF;
        END IF;
        IF TG_OP <> 'DELETE' AND TG_TABLE_NAME = 'sourcelayouts' THEN
            IF NEW.ColumnMapping IS NOT NULL THEN
            IF EXISTS (SELECT 1 FROM jsonb_each_text(NEW.ColumnMapping) v
                WHERE v.key !~ '^[1-9][0-9]*$' OR NOT (v.value IN ('DriverNumber','EmployeeKey','DriverName','WorkDate','Ignore')
                    OR v.value ~ '^Definition:[1-9][0-9]*$')) THEN
                RAISE EXCEPTION 'IMPORT_MAPPING_SHAPE: malformed mapping.';
            END IF;
            IF EXISTS (SELECT 1 FROM jsonb_each_text(NEW.ColumnMapping) v
                WHERE v.value LIKE 'Definition:%' AND NOT EXISTS (
                    SELECT 1 FROM payroll.PayrollPeriodDefinitions d
                    WHERE d.PayrollPeriodDefinitionID = split_part(v.value, ':', 2)::INTEGER
                        AND d.PayrollPeriodID = target.PayrollPeriodID AND d.CompanyID = target.CompanyID AND d.BranchID = target.BranchID)) THEN
                RAISE EXCEPTION 'IMPORT_MAPPING_SCOPE: mapping definition belongs to another period.';
            END IF;
            END IF;
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $body$;
    $fn$;

    EXECUTE $fn$
    CREATE FUNCTION import.fn_guard_payroll_work_envelope() RETURNS TRIGGER LANGUAGE plpgsql AS $body$
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
                RAISE EXCEPTION 'IMPORT_IMMUTABLE: period is not editable.';
            END IF;
        ELSIF OLD.Status IN ('Completed','Cancelled') OR target.DetailPurgedAtUtc IS NOT NULL THEN
            RAISE EXCEPTION 'IMPORT_IMMUTABLE: target is no longer editable.';
        END IF;
        RETURN NEW;
    END $body$;
    $fn$;

    CREATE TRIGGER trg_Imports_Envelope BEFORE INSERT OR UPDATE OR DELETE ON import.Imports
        FOR EACH ROW EXECUTE FUNCTION import.fn_guard_import_envelope();
    CREATE TRIGGER trg_Imports_PayrollWork BEFORE UPDATE ON import.Imports
        FOR EACH ROW EXECUTE FUNCTION import.fn_guard_payroll_work_envelope();
    FOREACH name IN ARRAY ARRAY['importissues','importsourcerecords','filesourcebatches','filesources','filesourceunits','sourcelayouts',
        'filesourcerecords','payrollworkimports','payrollworkrecords','payrollworkpreviewgroups',
        'payrollworkdrivermatches','payrollworkrecordidentifiers'] LOOP
        EXECUTE format('CREATE TRIGGER trg_ImportDetail_Writable BEFORE INSERT OR UPDATE OR DELETE ON import.%I FOR EACH ROW EXECUTE FUNCTION import.fn_guard_payroll_work_detail()', name);
        EXECUTE format('CREATE TRIGGER trg_ImportDetail_NoTruncate BEFORE TRUNCATE ON import.%I FOR EACH STATEMENT EXECUTE FUNCTION import.fn_guard_payroll_work_detail()', name);
    END LOOP;
END
$cutover$;
