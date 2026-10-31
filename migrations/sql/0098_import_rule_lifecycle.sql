-- Forward-only Import lifecycle support. Existing 0094/0095 history is untouched.
-- No unsupported existing Import payload or rule is backfilled by this migration.

CREATE TABLE import.FileCanonicalData (
    FileSourceID BIGINT PRIMARY KEY,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    FormatVersion INTEGER NOT NULL CHECK (FormatVersion > 0),
    Payload BYTEA,
    ByteCount BIGINT NOT NULL CHECK (ByteCount > 0),
    HashSha256 TEXT NOT NULL CHECK (HashSha256 ~ '^[0-9a-f]{64}$'),
    PurgedAtUtc TIMESTAMPTZ,
    FOREIGN KEY (FileSourceID, ImportID, CompanyID)
        REFERENCES import.FileSources(FileSourceID, ImportID, CompanyID),
    CHECK ((Payload IS NULL) = (PurgedAtUtc IS NOT NULL)),
    CHECK (Payload IS NULL OR octet_length(Payload) = ByteCount),
    CHECK (Payload IS NULL OR encode(sha256(Payload), 'hex') = HashSha256),
    CHECK (Payload IS NULL OR jsonb_typeof(convert_from(Payload, 'UTF8')::jsonb) = 'object')
);
CREATE INDEX ix_FileCanonicalData_Import ON import.FileCanonicalData(ImportID, CompanyID, FileSourceID);

CREATE TABLE import.PayrollWorkRuleVersions (
    RuleVersionID BIGSERIAL PRIMARY KEY,
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PayDefinitionID INTEGER NOT NULL,
    OriginImportID BIGINT NOT NULL,
    OriginPayrollPeriodID INTEGER NOT NULL,
    RuleContractVersion INTEGER NOT NULL DEFAULT 1 CHECK (RuleContractVersion > 0),
    SourceContractVersion INTEGER NOT NULL DEFAULT 2 CHECK (SourceContractVersion > 0),
    TargetContractVersion INTEGER NOT NULL DEFAULT 2 CHECK (TargetContractVersion > 0),
    CanonicalizationVersion INTEGER NOT NULL DEFAULT 3 CHECK (CanonicalizationVersion > 0),
    RuleFingerprint TEXT NOT NULL CHECK (RuleFingerprint ~ '^[0-9a-f]{64}$'),
    SourceSignature JSONB NOT NULL CHECK (jsonb_typeof(SourceSignature) = 'object'),
    TargetMeaning JSONB NOT NULL CHECK (jsonb_typeof(TargetMeaning) = 'object'),
    RulePayload JSONB NOT NULL CHECK (jsonb_typeof(RulePayload) = 'object' AND RulePayload <> '{}'::jsonb),
    CreatedByUserID INTEGER NOT NULL,
    CreatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (RuleVersionID, CompanyID, BranchID, PayDefinitionID),
    FOREIGN KEY (OriginImportID, CompanyID, BranchID, OriginPayrollPeriodID)
        REFERENCES import.PayrollWorkImports(ImportID, CompanyID, BranchID, PayrollPeriodID),
    FOREIGN KEY (PayDefinitionID, CompanyID)
        REFERENCES payroll.PayDefinitions(PayDefinitionID, CompanyID),
    FOREIGN KEY (CreatedByUserID, CompanyID) REFERENCES sec.Users(UserID, CompanyID)
);
CREATE INDEX ix_PayrollWorkRuleVersions_Origin
    ON import.PayrollWorkRuleVersions(OriginImportID, CompanyID, PayDefinitionID, RuleVersionID);

CREATE TABLE import.PayrollWorkPendingRules (
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PayrollPeriodID INTEGER NOT NULL,
    PayDefinitionID INTEGER NOT NULL,
    PayrollPeriodDefinitionID BIGINT NOT NULL,
    RateDefinitionID INTEGER NOT NULL,
    RuleVersionID BIGINT NOT NULL,
    Revision BIGINT NOT NULL DEFAULT 1 CHECK (Revision > 0),
    ExpectedHeadRevision BIGINT NOT NULL CHECK (ExpectedHeadRevision >= 0),
    UpdatedByUserID INTEGER NOT NULL,
    UpdatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ImportID, PayDefinitionID),
    CONSTRAINT uq_PayrollWorkPendingRules_ImportTargetRule
        UNIQUE (ImportID, PayDefinitionID, RuleVersionID),
    FOREIGN KEY (ImportID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES import.PayrollWorkImports(ImportID, CompanyID, BranchID, PayrollPeriodID),
    FOREIGN KEY (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID,
                 PayDefinitionID, RateDefinitionID)
        REFERENCES payroll.PayrollPeriodDefinitions
            (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID,
             PayDefinitionID, RateDefinitionID),
    FOREIGN KEY (RuleVersionID, CompanyID, BranchID, PayDefinitionID)
        REFERENCES import.PayrollWorkRuleVersions(RuleVersionID, CompanyID, BranchID, PayDefinitionID),
    FOREIGN KEY (UpdatedByUserID, CompanyID) REFERENCES sec.Users(UserID, CompanyID)
);
CREATE INDEX ix_PayrollWorkPendingRules_RuleVersion
    ON import.PayrollWorkPendingRules(RuleVersionID, CompanyID, BranchID, PayDefinitionID);

CREATE TABLE import.PayrollWorkRuleHeads (
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PayDefinitionID INTEGER NOT NULL,
    RuleVersionID BIGINT NOT NULL,
    Revision BIGINT NOT NULL CHECK (Revision > 0),
    ActivatedByImportID BIGINT NOT NULL,
    ActivatedPayrollPeriodID INTEGER NOT NULL,
    ActivatedByUserID INTEGER NOT NULL,
    ActivatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (CompanyID, BranchID, PayDefinitionID),
    FOREIGN KEY (BranchID, CompanyID) REFERENCES core.Branches(BranchID, CompanyID),
    FOREIGN KEY (PayDefinitionID, CompanyID)
        REFERENCES payroll.PayDefinitions(PayDefinitionID, CompanyID),
    FOREIGN KEY (RuleVersionID, CompanyID, BranchID, PayDefinitionID)
        REFERENCES import.PayrollWorkRuleVersions(RuleVersionID, CompanyID, BranchID, PayDefinitionID),
    FOREIGN KEY (ActivatedByImportID, CompanyID, BranchID, ActivatedPayrollPeriodID)
        REFERENCES import.PayrollWorkImports(ImportID, CompanyID, BranchID, PayrollPeriodID),
    FOREIGN KEY (ActivatedByUserID, CompanyID) REFERENCES sec.Users(UserID, CompanyID)
);
CREATE INDEX ix_PayrollWorkRuleHeads_RuleVersion
    ON import.PayrollWorkRuleHeads(RuleVersionID, CompanyID, BranchID, PayDefinitionID);

CREATE TABLE import.PayrollWorkReviewDecisions (
    DecisionID BIGSERIAL PRIMARY KEY,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PayrollPeriodID INTEGER NOT NULL,
    DecisionKey TEXT NOT NULL CHECK (btrim(DecisionKey) <> ''),
    ContractVersion INTEGER NOT NULL DEFAULT 1 CHECK (ContractVersion > 0),
    ReviewedRevision BIGINT NOT NULL CHECK (ReviewedRevision > 0),
    PlanFingerprint TEXT NOT NULL CHECK (PlanFingerprint ~ '^[0-9a-f]{64}$'),
    DecisionKind TEXT NOT NULL CHECK (btrim(DecisionKind) <> ''),
    Choice TEXT NOT NULL CHECK (btrim(Choice) <> ''),
    BasisJSON JSONB NOT NULL CHECK (jsonb_typeof(BasisJSON) = 'object'),
    DriverID INTEGER,
    DriverAssignmentID INTEGER,
    WorkDate DATE,
    PayrollPeriodDefinitionID BIGINT,
    PayDefinitionID INTEGER,
    RateDefinitionID INTEGER,
    ActorUserID INTEGER NOT NULL,
    CreatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ImportID, DecisionKey),
    FOREIGN KEY (ImportID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES import.PayrollWorkImports(ImportID, CompanyID, BranchID, PayrollPeriodID),
    FOREIGN KEY (DriverID, CompanyID) REFERENCES core.Drivers(DriverID, CompanyID),
    FOREIGN KEY (DriverAssignmentID, CompanyID, BranchID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, CompanyID, BranchID),
    FOREIGN KEY (DriverAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID),
    FOREIGN KEY (PayrollPeriodID, WorkDate)
        REFERENCES payroll.PayrollPeriodDays(PayrollPeriodID, WorkDate),
    FOREIGN KEY (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID,
                 PayDefinitionID, RateDefinitionID)
        REFERENCES payroll.PayrollPeriodDefinitions
            (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID,
             PayDefinitionID, RateDefinitionID),
    FOREIGN KEY (ActorUserID, CompanyID) REFERENCES sec.Users(UserID, CompanyID),
    CHECK (DriverAssignmentID IS NULL OR DriverID IS NOT NULL),
    CHECK ((PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL AND RateDefinitionID IS NULL)
        OR (PayrollPeriodDefinitionID IS NOT NULL AND PayDefinitionID IS NOT NULL AND RateDefinitionID IS NOT NULL))
);
CREATE INDEX ix_PayrollWorkReviewDecisions_Import
    ON import.PayrollWorkReviewDecisions(ImportID, CompanyID, DecisionID);

CREATE TABLE import.PayrollWorkAppliedChanges (
    ChangeID BIGSERIAL PRIMARY KEY,
    ImportID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PayrollPeriodID INTEGER NOT NULL,
    PayDefinitionID INTEGER NOT NULL,
    PayrollPeriodDefinitionID BIGINT NOT NULL,
    RateDefinitionID INTEGER NOT NULL,
    RuleVersionID BIGINT NOT NULL,
    DriverID INTEGER NOT NULL,
    DriverAssignmentID INTEGER NOT NULL,
    WorkDate DATE NOT NULL,
    DraftLineID BIGINT NOT NULL,
    EvidenceEventID BIGINT NOT NULL,
    ReviewedRevision BIGINT NOT NULL CHECK (ReviewedRevision > 0),
    PlanFingerprint TEXT NOT NULL CHECK (PlanFingerprint ~ '^[0-9a-f]{64}$'),
    ContractVersion INTEGER NOT NULL DEFAULT 1 CHECK (ContractVersion > 0),
    Action VARCHAR(10) NOT NULL CHECK (Action IN ('Created','Updated','Voided')),
    CreatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ImportID, DriverAssignmentID, WorkDate, PayrollPeriodDefinitionID),
    FOREIGN KEY (ImportID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES import.PayrollWorkImports(ImportID, CompanyID, BranchID, PayrollPeriodID),
    FOREIGN KEY (PayDefinitionID, CompanyID)
        REFERENCES payroll.PayDefinitions(PayDefinitionID, CompanyID),
    FOREIGN KEY (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID,
                 PayDefinitionID, RateDefinitionID)
        REFERENCES payroll.PayrollPeriodDefinitions
            (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID,
             PayDefinitionID, RateDefinitionID),
    FOREIGN KEY (RuleVersionID, CompanyID, BranchID, PayDefinitionID)
        REFERENCES import.PayrollWorkRuleVersions(RuleVersionID, CompanyID, BranchID, PayDefinitionID),
    FOREIGN KEY (DriverID, CompanyID) REFERENCES core.Drivers(DriverID, CompanyID),
    FOREIGN KEY (DriverAssignmentID, CompanyID, BranchID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, CompanyID, BranchID),
    FOREIGN KEY (DriverAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID),
    FOREIGN KEY (PayrollPeriodID, WorkDate)
        REFERENCES payroll.PayrollPeriodDays(PayrollPeriodID, WorkDate),
    FOREIGN KEY (DraftLineID, CompanyID, BranchID, PayrollPeriodID,
                 DriverAssignmentID, WorkDate, PayrollPeriodDefinitionID)
        REFERENCES payroll.PayrollDraftLines
            (DraftLineID, CompanyID, BranchID, PayrollPeriodID,
             DriverAssignmentID, WorkDate, PayrollPeriodDefinitionID),
    FOREIGN KEY (EvidenceEventID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollPeriodAuditEvidenceEvents
            (PayrollPeriodAuditEvidenceEventID, CompanyID, BranchID, PayrollPeriodID),
    CONSTRAINT fk_PayrollWorkAppliedChanges_PendingRuleVersion
        FOREIGN KEY (ImportID, PayDefinitionID, RuleVersionID)
        REFERENCES import.PayrollWorkPendingRules(ImportID, PayDefinitionID, RuleVersionID)
);
CREATE INDEX ix_PayrollWorkAppliedChanges_Import
    ON import.PayrollWorkAppliedChanges(ImportID, CompanyID, ChangeID);

CREATE TABLE import.PayrollWorkPreviewRuleContexts (
    ImportID BIGINT PRIMARY KEY,
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PayrollPeriodID INTEGER NOT NULL,
    Fingerprint TEXT NOT NULL CHECK (Fingerprint ~ '^[0-9a-f]{64}$'),
    FOREIGN KEY (ImportID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES import.PayrollWorkImports(ImportID, CompanyID, BranchID, PayrollPeriodID)
);

-- Shared owner lock order: PayrollPeriod first, then Import. The optional purge
-- exception is limited to the transaction-local finalization scope.
CREATE FUNCTION import.fn_lock_payroll_work_owner(
    p_import_id BIGINT, p_company_id INTEGER, p_branch_id INTEGER, p_period_id INTEGER,
    p_allow_retention BOOLEAN DEFAULT FALSE
) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    period_status TEXT;
    import_status TEXT;
    detail_purged TIMESTAMPTZ;
    purge_scope BOOLEAN;
BEGIN
    SELECT Status INTO period_status FROM payroll.PayrollPeriods
        WHERE PayrollPeriodID = p_period_id AND CompanyID = p_company_id AND BranchID = p_branch_id
        FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'IMPORT_SCOPE: period is outside the requested Company/Branch.';
    END IF;

    SELECT i.Status, w.DetailPurgedAtUtc INTO import_status, detail_purged
    FROM import.Imports i JOIN import.PayrollWorkImports w
      ON w.ImportID = i.ImportID AND w.CompanyID = i.CompanyID
    WHERE i.ImportID = p_import_id AND i.CompanyID = p_company_id
      AND w.BranchID = p_branch_id AND w.PayrollPeriodID = p_period_id
    FOR UPDATE OF i;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'IMPORT_SCOPE: Import does not own the requested Company/Branch/period.';
    END IF;

    purge_scope := current_setting('flussra.import_purge_id', TRUE) = p_import_id::TEXT;
    IF p_allow_retention AND purge_scope AND period_status IN ('Locked','Archived') THEN
        RETURN;
    END IF;
    IF period_status NOT IN ('Draft','Open','Returned')
       OR import_status NOT IN ('NeedsMapping','NeedsReview') OR detail_purged IS NOT NULL
       OR purge_scope THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: Import detail is no longer editable.';
    END IF;
END $$;

CREATE FUNCTION import.fn_guard_file_canonical_data() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    source_row import.FileSources%ROWTYPE;
    target_row import.PayrollWorkImports%ROWTYPE;
    period_row payroll.PayrollPeriods%ROWTYPE;
BEGIN
    IF TG_OP = 'TRUNCATE' OR TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: canonical File identity is retained; only payload purge is allowed.';
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT * INTO source_row FROM import.FileSources
        WHERE FileSourceID = NEW.FileSourceID AND ImportID = NEW.ImportID AND CompanyID = NEW.CompanyID;
        IF NOT FOUND OR NEW.FormatVersion <> 1
           OR source_row.FileContent IS NULL OR source_row.ContentPurgedAtUtc IS NOT NULL
           OR NEW.Payload IS NULL OR NEW.PurgedAtUtc IS NOT NULL THEN
            RAISE EXCEPTION 'IMPORT_CANONICAL_SCOPE: live original File is required.';
        END IF;
        SELECT w.* INTO target_row FROM import.PayrollWorkImports w
        WHERE w.ImportID = NEW.ImportID AND w.CompanyID = NEW.CompanyID;
        IF NOT FOUND THEN RAISE EXCEPTION 'IMPORT_SCOPE: PayrollWork owner is required.'; END IF;
        PERFORM import.fn_lock_payroll_work_owner(
            NEW.ImportID, NEW.CompanyID, target_row.BranchID, target_row.PayrollPeriodID, FALSE);
        RETURN NEW;
    END IF;

    SELECT w.* INTO target_row FROM import.PayrollWorkImports w
    WHERE w.ImportID = OLD.ImportID AND w.CompanyID = OLD.CompanyID;
    IF NOT FOUND THEN RAISE EXCEPTION 'IMPORT_SCOPE: PayrollWork owner is required.'; END IF;
    PERFORM import.fn_lock_payroll_work_owner(
        OLD.ImportID, OLD.CompanyID, target_row.BranchID, target_row.PayrollPeriodID, TRUE);
    IF current_setting('flussra.import_purge_id', TRUE) IS DISTINCT FROM OLD.ImportID::TEXT
       OR OLD.Payload IS NULL OR NEW.Payload IS NOT NULL OR NEW.PurgedAtUtc IS NULL
       OR OLD.PurgedAtUtc IS NOT NULL OR NEW.PurgedAtUtc IS NULL
       OR (to_jsonb(NEW) - ARRAY['payload','purgedatutc'])
          IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['payload','purgedatutc']) THEN
        RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: only a one-way canonical payload purge is allowed.';
    END IF;
    SELECT * INTO period_row FROM payroll.PayrollPeriods
        WHERE PayrollPeriodID = target_row.PayrollPeriodID AND CompanyID = target_row.CompanyID
          AND BranchID = target_row.BranchID;
    IF period_row.Status NOT IN ('Locked','Archived') THEN
        RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: canonical payload purge requires a finalized period.';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_FileCanonicalData_Writable
    BEFORE INSERT OR UPDATE OR DELETE ON import.FileCanonicalData
    FOR EACH ROW EXECUTE FUNCTION import.fn_guard_file_canonical_data();
CREATE TRIGGER trg_FileCanonicalData_NoTruncate
    BEFORE TRUNCATE ON import.FileCanonicalData
    FOR EACH STATEMENT EXECUTE FUNCTION import.fn_guard_file_canonical_data();

-- An original File cannot be purged while its canonical payload remains live.
CREATE FUNCTION import.fn_guard_file_canonical_retention() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.FileContent IS NOT NULL AND NEW.FileContent IS NULL THEN
        IF current_setting('flussra.import_purge_id', TRUE) IS DISTINCT FROM OLD.ImportID::TEXT
           OR NEW.ContentPurgedAtUtc IS NULL
           OR EXISTS (SELECT 1 FROM import.FileCanonicalData c
                      WHERE c.FileSourceID = OLD.FileSourceID AND c.ImportID = OLD.ImportID
                        AND c.CompanyID = OLD.CompanyID AND c.Payload IS NOT NULL) THEN
            RAISE EXCEPTION 'IMPORT_PURGE_SCOPE: original File purge requires prior canonical payload purge.';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_FileSources_CanonicalRetention
    BEFORE UPDATE OF FileContent, ContentPurgedAtUtc ON import.FileSources
    FOR EACH ROW EXECUTE FUNCTION import.fn_guard_file_canonical_retention();

CREATE FUNCTION import.fn_guard_rule_version() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    target import.PayrollWorkImports%ROWTYPE;
BEGIN
    IF TG_OP = 'TRUNCATE' OR TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: saved rule versions are insert-only.';
    END IF;
    SELECT * INTO target FROM import.PayrollWorkImports
        WHERE ImportID = NEW.OriginImportID AND CompanyID = NEW.CompanyID
          AND BranchID = NEW.BranchID AND PayrollPeriodID = NEW.OriginPayrollPeriodID;
    IF NOT FOUND THEN RAISE EXCEPTION 'IMPORT_SCOPE: rule origin does not own this Company/Branch/period.'; END IF;
    IF NEW.RuleContractVersion <> 1 OR NEW.SourceContractVersion <> 2
       OR NEW.TargetContractVersion <> 2 OR NEW.CanonicalizationVersion <> 3 THEN
        RAISE EXCEPTION 'IMPORT_CONTRACT_UNSUPPORTED: saved rule protocol is not supported.';
    END IF;
    PERFORM import.fn_lock_payroll_work_owner(
        NEW.OriginImportID, NEW.CompanyID, NEW.BranchID, NEW.OriginPayrollPeriodID, FALSE);
    IF NOT EXISTS (
        SELECT 1 FROM payroll.PayrollPeriodDefinitions d
        WHERE d.PayrollPeriodID = NEW.OriginPayrollPeriodID AND d.CompanyID = NEW.CompanyID
          AND d.BranchID = NEW.BranchID AND d.PayDefinitionID = NEW.PayDefinitionID
    ) THEN
        RAISE EXCEPTION 'IMPORT_RULE_TARGET: rule definition is not in the origin period snapshot.';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_PayrollWorkRuleVersions_Writable
    BEFORE INSERT OR UPDATE OR DELETE ON import.PayrollWorkRuleVersions
    FOR EACH ROW EXECUTE FUNCTION import.fn_guard_rule_version();
CREATE TRIGGER trg_PayrollWorkRuleVersions_NoTruncate
    BEFORE TRUNCATE ON import.PayrollWorkRuleVersions
    FOR EACH STATEMENT EXECUTE FUNCTION import.fn_guard_rule_version();

CREATE FUNCTION import.fn_guard_pending_rule() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    iid BIGINT;
    cid INTEGER;
    bid INTEGER;
    pid INTEGER;
    owner import.Imports%ROWTYPE;
    target import.PayrollWorkImports%ROWTYPE;
    actual_head_revision BIGINT;
    origin_status TEXT;
    origin_purged TIMESTAMPTZ;
    rule_origin_import BIGINT;
    rule_contract INTEGER;
    source_contract INTEGER;
    target_contract INTEGER;
    canonicalization_version INTEGER;
BEGIN
    IF TG_OP = 'TRUNCATE' THEN RAISE EXCEPTION 'IMPORT_IMMUTABLE: TRUNCATE is not an Import retention operation.'; END IF;
    iid := CASE WHEN TG_OP = 'DELETE' THEN OLD.ImportID ELSE NEW.ImportID END;
    cid := CASE WHEN TG_OP = 'DELETE' THEN OLD.CompanyID ELSE NEW.CompanyID END;
    bid := CASE WHEN TG_OP = 'DELETE' THEN OLD.BranchID ELSE NEW.BranchID END;
    pid := CASE WHEN TG_OP = 'DELETE' THEN OLD.PayrollPeriodID ELSE NEW.PayrollPeriodID END;
    PERFORM import.fn_lock_payroll_work_owner(iid, cid, bid, pid, FALSE);
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    IF TG_OP = 'UPDATE' AND
       (NEW.ImportID, NEW.CompanyID, NEW.BranchID, NEW.PayrollPeriodID,
        NEW.PayDefinitionID, NEW.PayrollPeriodDefinitionID, NEW.RateDefinitionID)
       IS DISTINCT FROM
       (OLD.ImportID, OLD.CompanyID, OLD.BranchID, OLD.PayrollPeriodID,
        OLD.PayDefinitionID, OLD.PayrollPeriodDefinitionID, OLD.RateDefinitionID) THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: pending rule ownership and target are immutable.';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.Revision <> OLD.Revision + 1 THEN
        RAISE EXCEPTION 'IMPORT_REVISION: pending rule revision must advance exactly once.';
    ELSIF TG_OP = 'INSERT' AND NEW.Revision <> 1 THEN
        RAISE EXCEPTION 'IMPORT_REVISION: pending rule creation starts at revision one.';
    END IF;
    SELECT w.* INTO target FROM import.PayrollWorkImports w
    WHERE w.ImportID = iid AND w.CompanyID = cid AND w.BranchID = bid AND w.PayrollPeriodID = pid;
    SELECT Revision INTO actual_head_revision FROM import.PayrollWorkRuleHeads
      WHERE CompanyID = cid AND BranchID = bid AND PayDefinitionID = NEW.PayDefinitionID
      FOR UPDATE;
    actual_head_revision := COALESCE(actual_head_revision, 0);
    IF NEW.ExpectedHeadRevision <> actual_head_revision THEN
        RAISE EXCEPTION 'IMPORT_RULE_HEAD_STALE: expected head revision %, actual %.',
            NEW.ExpectedHeadRevision, actual_head_revision;
    END IF;
    SELECT OriginImportID, RuleContractVersion, SourceContractVersion,
           TargetContractVersion, CanonicalizationVersion
      INTO rule_origin_import, rule_contract, source_contract,
           target_contract, canonicalization_version
      FROM import.PayrollWorkRuleVersions r
        WHERE r.RuleVersionID = NEW.RuleVersionID AND r.CompanyID = cid
          AND r.BranchID = bid AND r.PayDefinitionID = NEW.PayDefinitionID;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'IMPORT_RULE_SCOPE: saved rule is outside the requested Company/Branch/definition.';
    END IF;
    IF rule_contract <> 1 OR source_contract <> 2 OR target_contract <> 2
       OR canonicalization_version <> 3 THEN
        RAISE EXCEPTION 'IMPORT_CONTRACT_UNSUPPORTED: saved rule protocol is not supported.';
    END IF;
    IF rule_origin_import <> iid THEN
        SELECT i.Status INTO origin_status FROM import.Imports i
        WHERE i.ImportID = rule_origin_import AND i.CompanyID = cid;
        IF origin_status IS DISTINCT FROM 'Completed' OR NOT EXISTS (
            SELECT 1 FROM import.PayrollWorkRuleHeads h
            WHERE h.CompanyID = cid AND h.BranchID = bid AND h.PayDefinitionID = NEW.PayDefinitionID
              AND h.RuleVersionID = NEW.RuleVersionID
        ) THEN
            RAISE EXCEPTION 'IMPORT_RULE_REUSE_UNCONFIRMED: only this Import proposal or the current completed rule may be reused.';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_PayrollWorkPendingRules_Writable
    BEFORE INSERT OR UPDATE OR DELETE ON import.PayrollWorkPendingRules
    FOR EACH ROW EXECUTE FUNCTION import.fn_guard_pending_rule();
CREATE TRIGGER trg_PayrollWorkPendingRules_NoTruncate
    BEFORE TRUNCATE ON import.PayrollWorkPendingRules
    FOR EACH STATEMENT EXECUTE FUNCTION import.fn_guard_pending_rule();

CREATE FUNCTION import.fn_guard_rule_head() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    expected_revision BIGINT;
    actual_import_status TEXT;
    period_status TEXT;
    rule_origin BIGINT;
    rule_contract INTEGER;
    source_contract INTEGER;
    target_contract INTEGER;
    canonical_version INTEGER;
    target_matches BOOLEAN;
    mapping_fingerprint TEXT;
    content_fingerprint TEXT;
    plan_fingerprint TEXT;
BEGIN
    IF TG_OP = 'TRUNCATE' OR TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: rule heads cannot be deleted or truncated.';
    END IF;
    SELECT Status INTO period_status FROM payroll.PayrollPeriods
    WHERE PayrollPeriodID = NEW.ActivatedPayrollPeriodID AND CompanyID = NEW.CompanyID
      AND BranchID = NEW.BranchID FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'IMPORT_SCOPE: activated period is outside Company/Branch.'; END IF;
    SELECT Status INTO actual_import_status FROM import.Imports
    WHERE ImportID = NEW.ActivatedByImportID AND CompanyID = NEW.CompanyID FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'IMPORT_SCOPE: activated Import is not owned by Company.'; END IF;
    SELECT MappingFingerprint, ContentFingerprint, PlanFingerprint
      INTO mapping_fingerprint, content_fingerprint, plan_fingerprint
      FROM import.PayrollWorkImports WHERE ImportID = NEW.ActivatedByImportID
        AND CompanyID = NEW.CompanyID AND BranchID = NEW.BranchID
        AND PayrollPeriodID = NEW.ActivatedPayrollPeriodID;
    IF NOT FOUND OR mapping_fingerprint IS NULL OR content_fingerprint IS NULL OR plan_fingerprint IS NULL
       OR period_status NOT IN ('Draft','Open','Returned')
       OR actual_import_status IS DISTINCT FROM 'Completed' THEN
        RAISE EXCEPTION 'IMPORT_RULE_HEAD_GATE: head activation requires completed Import in an editable period.';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.Revision <> 1 OR EXISTS (
            SELECT 1 FROM import.PayrollWorkRuleHeads h
            WHERE h.CompanyID = NEW.CompanyID AND h.BranchID = NEW.BranchID
              AND h.PayDefinitionID = NEW.PayDefinitionID
        ) THEN
            RAISE EXCEPTION 'IMPORT_RULE_HEAD_REVISION: first head revision must be one with no prior head.';
        END IF;
        expected_revision := 0;
    ELSE
        IF (NEW.CompanyID, NEW.BranchID, NEW.PayDefinitionID) IS DISTINCT FROM
           (OLD.CompanyID, OLD.BranchID, OLD.PayDefinitionID)
           OR NEW.Revision <> OLD.Revision + 1 THEN
            RAISE EXCEPTION 'IMPORT_IMMUTABLE: head target is immutable and revision advances once.';
        END IF;
        expected_revision := OLD.Revision;
    END IF;
    SELECT OriginImportID, RuleContractVersion, SourceContractVersion,
           TargetContractVersion, CanonicalizationVersion
      INTO rule_origin, rule_contract, source_contract, target_contract, canonical_version
      FROM import.PayrollWorkRuleVersions
      WHERE RuleVersionID = NEW.RuleVersionID AND CompanyID = NEW.CompanyID
        AND BranchID = NEW.BranchID AND PayDefinitionID = NEW.PayDefinitionID;
    IF NOT FOUND OR rule_origin <> NEW.ActivatedByImportID
       OR rule_contract <> 1 OR source_contract <> 2 OR target_contract <> 2 OR canonical_version <> 3 THEN
        RAISE EXCEPTION 'IMPORT_RULE_HEAD_SCOPE: head must point to a supported rule from its completing Import.';
    END IF;
    SELECT EXISTS (
        SELECT 1 FROM import.PayrollWorkPendingRules p
        WHERE p.ImportID = NEW.ActivatedByImportID AND p.CompanyID = NEW.CompanyID
          AND p.BranchID = NEW.BranchID AND p.PayrollPeriodID = NEW.ActivatedPayrollPeriodID
          AND p.PayDefinitionID = NEW.PayDefinitionID AND p.RuleVersionID = NEW.RuleVersionID
          AND p.ExpectedHeadRevision = expected_revision
    ) INTO target_matches;
    IF NOT target_matches THEN
        RAISE EXCEPTION 'IMPORT_RULE_HEAD_GATE: matching pending rule and expected head revision are required.';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_PayrollWorkRuleHeads_Writable
    BEFORE INSERT OR UPDATE OR DELETE ON import.PayrollWorkRuleHeads
    FOR EACH ROW EXECUTE FUNCTION import.fn_guard_rule_head();
CREATE TRIGGER trg_PayrollWorkRuleHeads_NoTruncate
    BEFORE TRUNCATE ON import.PayrollWorkRuleHeads
    FOR EACH STATEMENT EXECUTE FUNCTION import.fn_guard_rule_head();

CREATE FUNCTION import.fn_require_current_pending_head(
    p_import_id BIGINT, p_company_id INTEGER, p_branch_id INTEGER, p_period_id INTEGER,
    p_pay_definition_id INTEGER, p_period_definition_id BIGINT, p_rate_definition_id INTEGER
) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    expected_revision BIGINT;
    actual_revision BIGINT;
BEGIN
    SELECT ExpectedHeadRevision INTO expected_revision
      FROM import.PayrollWorkPendingRules
      WHERE ImportID = p_import_id AND CompanyID = p_company_id AND BranchID = p_branch_id
        AND PayrollPeriodID = p_period_id AND PayDefinitionID = p_pay_definition_id
        AND PayrollPeriodDefinitionID = p_period_definition_id AND RateDefinitionID = p_rate_definition_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'IMPORT_RULE_CONTEXT_STALE: target has no matching pending rule.';
    END IF;
    SELECT Revision INTO actual_revision FROM import.PayrollWorkRuleHeads
      WHERE CompanyID = p_company_id AND BranchID = p_branch_id
        AND PayDefinitionID = p_pay_definition_id FOR UPDATE;
    IF COALESCE(actual_revision, 0) IS DISTINCT FROM expected_revision THEN
        RAISE EXCEPTION 'IMPORT_RULE_CONTEXT_STALE: pending expected head revision is no longer current.';
    END IF;
END $$;

CREATE FUNCTION import.fn_guard_review_decision() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    iid BIGINT;
    cid INTEGER;
    bid INTEGER;
    pid INTEGER;
    owner_status TEXT;
    owner_revision BIGINT;
    plan_fingerprint TEXT;
    detail_purged TIMESTAMPTZ;
    period_status TEXT;
BEGIN
    IF TG_OP = 'TRUNCATE' THEN RAISE EXCEPTION 'IMPORT_IMMUTABLE: TRUNCATE is not an Import retention operation.'; END IF;
    iid := CASE WHEN TG_OP = 'DELETE' THEN OLD.ImportID ELSE NEW.ImportID END;
    cid := CASE WHEN TG_OP = 'DELETE' THEN OLD.CompanyID ELSE NEW.CompanyID END;
    bid := CASE WHEN TG_OP = 'DELETE' THEN OLD.BranchID ELSE NEW.BranchID END;
    pid := CASE WHEN TG_OP = 'DELETE' THEN OLD.PayrollPeriodID ELSE NEW.PayrollPeriodID END;
    PERFORM import.fn_lock_payroll_work_owner(iid, cid, bid, pid, TG_OP = 'DELETE');
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'IMPORT_IMMUTABLE: review decisions are immutable.'; END IF;
    SELECT i.Status, i.Revision, w.PlanFingerprint, w.DetailPurgedAtUtc, p.Status
      INTO owner_status, owner_revision, plan_fingerprint, detail_purged, period_status
      FROM import.Imports i JOIN import.PayrollWorkImports w
        ON w.ImportID = i.ImportID AND w.CompanyID = i.CompanyID
      JOIN payroll.PayrollPeriods p ON p.PayrollPeriodID = w.PayrollPeriodID
        AND p.CompanyID = w.CompanyID AND p.BranchID = w.BranchID
      WHERE i.ImportID = iid AND i.CompanyID = cid;
    IF owner_status <> 'NeedsReview' OR detail_purged IS NOT NULL
       OR period_status NOT IN ('Draft','Open','Returned')
       OR NEW.ContractVersion <> 1 OR NEW.ReviewedRevision <> owner_revision
       OR NEW.PlanFingerprint IS DISTINCT FROM plan_fingerprint OR plan_fingerprint IS NULL THEN
        RAISE EXCEPTION 'IMPORT_DECISION_STALE: decision must match current editable preview revision and plan.';
    END IF;
    IF NEW.PayDefinitionID IS NOT NULL THEN
        PERFORM import.fn_require_current_pending_head(
            iid, cid, bid, pid, NEW.PayDefinitionID, NEW.PayrollPeriodDefinitionID, NEW.RateDefinitionID);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_PayrollWorkReviewDecisions_Writable
    BEFORE INSERT OR UPDATE OR DELETE ON import.PayrollWorkReviewDecisions
    FOR EACH ROW EXECUTE FUNCTION import.fn_guard_review_decision();
CREATE TRIGGER trg_PayrollWorkReviewDecisions_NoTruncate
    BEFORE TRUNCATE ON import.PayrollWorkReviewDecisions
    FOR EACH STATEMENT EXECUTE FUNCTION import.fn_guard_review_decision();

CREATE FUNCTION import.fn_guard_applied_change() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    iid BIGINT;
    cid INTEGER;
    bid INTEGER;
    pid INTEGER;
    owner_status TEXT;
    owner_revision BIGINT;
    owner_correlation UUID;
    plan_fingerprint TEXT;
    expected_head_revision BIGINT;
    actual_head_revision BIGINT;
    event payroll.PayrollPeriodAuditEvidenceEvents%ROWTYPE;
    after_state JSONB;
    expected_action TEXT;
BEGIN
    IF TG_OP = 'TRUNCATE' OR TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'IMPORT_IMMUTABLE: applied Import changes are append-only and durable.';
    END IF;
    iid := NEW.ImportID; cid := NEW.CompanyID; bid := NEW.BranchID; pid := NEW.PayrollPeriodID;
    PERFORM import.fn_lock_payroll_work_owner(iid, cid, bid, pid, FALSE);
    SELECT i.Status, i.Revision, i.CorrelationID, w.PlanFingerprint
      INTO owner_status, owner_revision, owner_correlation, plan_fingerprint
      FROM import.Imports i JOIN import.PayrollWorkImports w
        ON w.ImportID = i.ImportID AND w.CompanyID = i.CompanyID
      WHERE i.ImportID = iid AND i.CompanyID = cid;
    IF owner_status IS DISTINCT FROM 'NeedsReview' OR NEW.ContractVersion <> 1
       OR NEW.ReviewedRevision IS DISTINCT FROM owner_revision OR NEW.PlanFingerprint IS DISTINCT FROM plan_fingerprint
       OR plan_fingerprint IS NULL THEN
        RAISE EXCEPTION 'IMPORT_APPLIED_CHANGE_STALE: link must match current reviewed Import revision and plan.';
    END IF;
    SELECT p.ExpectedHeadRevision INTO expected_head_revision
      FROM import.PayrollWorkPendingRules p
      WHERE p.ImportID = iid AND p.CompanyID = cid AND p.BranchID = bid AND p.PayrollPeriodID = pid
        AND p.PayDefinitionID = NEW.PayDefinitionID AND p.PayrollPeriodDefinitionID = NEW.PayrollPeriodDefinitionID
        AND p.RateDefinitionID = NEW.RateDefinitionID AND p.RuleVersionID = NEW.RuleVersionID;
    IF NOT FOUND THEN RAISE EXCEPTION 'IMPORT_APPLIED_CHANGE_RULE: matching pending target rule is required.'; END IF;
    SELECT Revision INTO actual_head_revision FROM import.PayrollWorkRuleHeads
      WHERE CompanyID = cid AND BranchID = bid AND PayDefinitionID = NEW.PayDefinitionID
      FOR UPDATE;
    IF COALESCE(actual_head_revision, 0) <> expected_head_revision THEN
        RAISE EXCEPTION 'IMPORT_RULE_CONTEXT_STALE: pending rule head revision changed.';
    END IF;
    SELECT * INTO event FROM payroll.PayrollPeriodAuditEvidenceEvents
      WHERE PayrollPeriodAuditEvidenceEventID = NEW.EvidenceEventID
        AND CompanyID = cid AND BranchID = bid AND PayrollPeriodID = pid;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'IMPORT_APPLIED_CHANGE_EVIDENCE: referenced evidence event is missing.';
    END IF;
    expected_action := CASE NEW.Action WHEN 'Created' THEN 'SOURCE_CREATED'
                         WHEN 'Updated' THEN 'SOURCE_UPDATED' WHEN 'Voided' THEN 'SOURCE_VOIDED' END;
    after_state := event.AfterStateJson;
    IF event.EvidenceDomain IS DISTINCT FROM 'SOURCE' OR event.SourceEntityType IS DISTINCT FROM 'PayrollDraftLines'
       OR event.SourceEntityID IS DISTINCT FROM NEW.DraftLineID::TEXT
       OR event.CorrelationID IS DISTINCT FROM owner_correlation
       OR event.SourceRevision IS DISTINCT FROM NEW.ReviewedRevision
       OR event.ActionCode IS DISTINCT FROM expected_action
       OR event.DriverID IS DISTINCT FROM NEW.DriverID
       OR event.DriverAssignmentID IS DISTINCT FROM NEW.DriverAssignmentID
       OR event.WorkDate IS DISTINCT FROM NEW.WorkDate
       OR after_state->>'source_type' IS DISTINCT FROM 'Import'
       OR after_state->>'payroll_period_definition_id' IS DISTINCT FROM NEW.PayrollPeriodDefinitionID::TEXT
       OR after_state->>'pay_definition_id' IS DISTINCT FROM NEW.PayDefinitionID::TEXT
       OR after_state->>'rate_definition_id' IS DISTINCT FROM NEW.RateDefinitionID::TEXT THEN
        RAISE EXCEPTION 'IMPORT_APPLIED_CHANGE_EVIDENCE: event does not describe this actual Import mutation.';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_PayrollWorkAppliedChanges_Writable
    BEFORE INSERT OR UPDATE OR DELETE ON import.PayrollWorkAppliedChanges
    FOR EACH ROW EXECUTE FUNCTION import.fn_guard_applied_change();
CREATE TRIGGER trg_PayrollWorkAppliedChanges_NoTruncate
    BEFORE TRUNCATE ON import.PayrollWorkAppliedChanges
    FOR EACH STATEMENT EXECUTE FUNCTION import.fn_guard_applied_change();

CREATE FUNCTION import.fn_require_applied_import_completed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    import_status TEXT;
BEGIN
    SELECT Status INTO import_status FROM import.Imports
    WHERE ImportID = NEW.ImportID AND CompanyID = NEW.CompanyID;
    IF import_status IS DISTINCT FROM 'Completed' THEN
        RAISE EXCEPTION 'IMPORT_APPLIED_CHANGE_PENDING: durable change links require a completed Import.';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_PayrollWorkAppliedChanges_CompletedImport
    AFTER INSERT ON import.PayrollWorkAppliedChanges
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION import.fn_require_applied_import_completed();

CREATE FUNCTION import.fn_guard_preview_rule_context() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN RAISE EXCEPTION 'IMPORT_IMMUTABLE: TRUNCATE is not an Import retention operation.'; END IF;
    IF TG_OP = 'UPDATE' THEN RAISE EXCEPTION 'IMPORT_IMMUTABLE: preview rule context is replaced, not updated.'; END IF;
    IF TG_OP = 'INSERT' THEN
        PERFORM import.fn_lock_payroll_work_owner(
            NEW.ImportID, NEW.CompanyID, NEW.BranchID, NEW.PayrollPeriodID, FALSE);
        RETURN NEW;
    END IF;
    PERFORM import.fn_lock_payroll_work_owner(
        OLD.ImportID, OLD.CompanyID, OLD.BranchID, OLD.PayrollPeriodID, TRUE);
    RETURN OLD;
END $$;
CREATE TRIGGER trg_PayrollWorkPreviewRuleContexts_Writable
    BEFORE INSERT OR UPDATE OR DELETE ON import.PayrollWorkPreviewRuleContexts
    FOR EACH ROW EXECUTE FUNCTION import.fn_guard_preview_rule_context();
CREATE TRIGGER trg_PayrollWorkPreviewRuleContexts_NoTruncate
    BEFORE TRUNCATE ON import.PayrollWorkPreviewRuleContexts
    FOR EACH STATEMENT EXECUTE FUNCTION import.fn_guard_preview_rule_context();
