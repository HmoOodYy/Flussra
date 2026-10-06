-- 0082: Target Compensation schema and invariants (P3b).
--
-- Dormant persistence foundation:
--   PayDefinitions -> RateDefinitions -> RateComponentDefinitions
--   Drivers        -> DriverRateAssignments -> DriverRateValues
--
-- Legacy PayItems, RateTypes, DriverRates and every legacy compensation table
-- remain the only operational payroll authority. Nothing here is read or
-- written by payroll runtime code. PayDefinitions is a separate root and does
-- not repurpose payroll.PayItems.
--
-- Lock order (all target writers): the RateDefinitions row is locked
-- FOR NO KEY UPDATE first, then dependent rows. RateDefinitions.
-- StructureLockedAtUtc is the single structural lock authority.

-- ---------------------------------------------------------------------------
-- 1. Supporting key on the branch-owned StatusRateColumns table so a future
--    Status-owned RateDefinition can carry composite Company/Branch integrity.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX ux_StatusRateColumns_ID_Company_Branch
    ON payroll.StatusRateColumns (StatusRateColumnID, CompanyID, BranchID);

-- ---------------------------------------------------------------------------
-- 2. PayDefinitions (Company-owned; code and name carry no runtime meaning)
-- ---------------------------------------------------------------------------
CREATE TABLE payroll.PayDefinitions (
    PayDefinitionID    SERIAL        PRIMARY KEY,
    CompanyID          INTEGER       NOT NULL,
    DefinitionCode     VARCHAR(50)   NOT NULL,
    DefinitionName     VARCHAR(200)  NOT NULL,
    InputType          VARCHAR(30)   NOT NULL,
    Unit               VARCHAR(50),
    CalculationMethod  VARCHAR(30)   NOT NULL,
    Status             VARCHAR(30)   NOT NULL DEFAULT 'Active',
    CreatedByUserID    INTEGER,
    CreatedAtUtc       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UpdatedByUserID    INTEGER,
    UpdatedAtUtc       TIMESTAMPTZ,

    CONSTRAINT fk_PayDefinitions_Company FOREIGN KEY (CompanyID)
        REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_PayDefinitions_Creator FOREIGN KEY (CreatedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayDefinitions_Updater FOREIGN KEY (UpdatedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT uq_PayDefinitions_Company_Code UNIQUE (CompanyID, DefinitionCode),
    CONSTRAINT uq_PayDefinitions_ID_Company UNIQUE (PayDefinitionID, CompanyID),
    CONSTRAINT ck_PayDefinitions_Code CHECK (btrim(DefinitionCode) <> ''),
    CONSTRAINT ck_PayDefinitions_Name CHECK (btrim(DefinitionName) <> ''),
    CONSTRAINT ck_PayDefinitions_InputType
        CHECK (InputType IN ('Decimal', 'WholeNumber')),
    CONSTRAINT ck_PayDefinitions_Method
        CHECK (CalculationMethod IN ('PerUnit', 'OrdinalTier')),
    CONSTRAINT ck_PayDefinitions_OrdinalWholeNumber
        CHECK (CalculationMethod <> 'OrdinalTier' OR InputType = 'WholeNumber'),
    CONSTRAINT ck_PayDefinitions_Status
        CHECK (Status IN ('Active', 'Inactive', 'Retired'))
);

-- ---------------------------------------------------------------------------
-- 3. RateDefinitions (one semantic compensation requirement)
-- ---------------------------------------------------------------------------
CREATE TABLE payroll.RateDefinitions (
    RateDefinitionID      SERIAL       PRIMARY KEY,
    CompanyID             INTEGER      NOT NULL,
    PayDefinitionID       INTEGER,
    StatusRateColumnID    INTEGER,
    OwnerBranchID         INTEGER,
    Shape                 VARCHAR(30)  NOT NULL,
    StructureLockedAtUtc  TIMESTAMPTZ,
    CreatedAtUtc          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_RateDefinitions_Company FOREIGN KEY (CompanyID)
        REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_RateDefinitions_PayDefinition_Company
        FOREIGN KEY (PayDefinitionID, CompanyID)
        REFERENCES payroll.PayDefinitions (PayDefinitionID, CompanyID),
    CONSTRAINT fk_RateDefinitions_StatusRateColumn_Company_Branch
        FOREIGN KEY (StatusRateColumnID, CompanyID, OwnerBranchID)
        REFERENCES payroll.StatusRateColumns (StatusRateColumnID, CompanyID, BranchID),
    CONSTRAINT uq_RateDefinitions_ID_Company UNIQUE (RateDefinitionID, CompanyID),
    CONSTRAINT uq_RateDefinitions_ID_Shape UNIQUE (RateDefinitionID, Shape),
    CONSTRAINT ck_RateDefinitions_Shape
        CHECK (Shape IN ('Scalar', 'OrdinalTierSchedule')),
    CONSTRAINT ck_RateDefinitions_SingleOwner
        CHECK (num_nonnulls(PayDefinitionID, StatusRateColumnID) = 1),
    CONSTRAINT ck_RateDefinitions_OwnerBranch
        CHECK ((StatusRateColumnID IS NULL) = (OwnerBranchID IS NULL)),
    CONSTRAINT ck_RateDefinitions_StatusOwnerScalar
        CHECK (StatusRateColumnID IS NULL OR Shape = 'Scalar')
);

CREATE UNIQUE INDEX ux_RateDefinitions_PayDefinition
    ON payroll.RateDefinitions (PayDefinitionID)
    WHERE PayDefinitionID IS NOT NULL;

CREATE UNIQUE INDEX ux_RateDefinitions_StatusRateColumn
    ON payroll.RateDefinitions (StatusRateColumnID)
    WHERE StatusRateColumnID IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. RateComponentDefinitions (the one structural component abstraction)
--    Scalar: exactly one component at SequenceNo 1.
--    OrdinalTierSchedule: ordinal ranges owned by the definition, never by a
--    Driver assignment. Gap/first-tier/final-open-ended checks run when an
--    assignment is approved; overlap and a second open-ended tier are rejected
--    immediately by the exclusion constraint.
-- ---------------------------------------------------------------------------
CREATE TABLE payroll.RateComponentDefinitions (
    RateComponentDefinitionID  SERIAL       PRIMARY KEY,
    RateDefinitionID           INTEGER      NOT NULL,
    Shape                      VARCHAR(30)  NOT NULL,
    SequenceNo                 INTEGER      NOT NULL,
    OrdinalFrom                INTEGER,
    OrdinalTo                  INTEGER,
    CreatedAtUtc               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_RateComponents_RateDefinition_Shape
        FOREIGN KEY (RateDefinitionID, Shape)
        REFERENCES payroll.RateDefinitions (RateDefinitionID, Shape),
    CONSTRAINT uq_RateComponents_ID_RateDefinition
        UNIQUE (RateComponentDefinitionID, RateDefinitionID),
    CONSTRAINT uq_RateComponents_RateDefinition_Sequence
        UNIQUE (RateDefinitionID, SequenceNo),
    CONSTRAINT ck_RateComponents_Sequence CHECK (SequenceNo >= 1),
    CONSTRAINT ck_RateComponents_ShapeFields CHECK (
        (Shape = 'Scalar'
            AND SequenceNo = 1 AND OrdinalFrom IS NULL AND OrdinalTo IS NULL)
        OR
        (Shape = 'OrdinalTierSchedule'
            AND OrdinalFrom IS NOT NULL AND OrdinalFrom >= 1
            AND (OrdinalTo IS NULL OR OrdinalTo >= OrdinalFrom))
    ),
    CONSTRAINT excl_RateComponents_OrdinalOverlap
        EXCLUDE USING gist (
            RateDefinitionID WITH =,
            int4range(OrdinalFrom, OrdinalTo, '[]') WITH &&
        ) WHERE (Shape = 'OrdinalTierSchedule')
);

-- ---------------------------------------------------------------------------
-- 5. DriverRateAssignments (atomic effective-dated schedule version)
-- ---------------------------------------------------------------------------
CREATE TABLE payroll.DriverRateAssignments (
    DriverRateAssignmentID  SERIAL       PRIMARY KEY,
    CompanyID               INTEGER      NOT NULL,
    BranchID                INTEGER      NOT NULL,
    DriverID                INTEGER      NOT NULL,
    RateDefinitionID        INTEGER      NOT NULL,
    EffectiveFrom           DATE         NOT NULL,
    EffectiveTo             DATE,
    Status                  VARCHAR(30)  NOT NULL DEFAULT 'Pending',
    IsAuthoritative         BOOLEAN      GENERATED ALWAYS AS (Status <> 'Pending') STORED,
    CreatedByUserID         INTEGER,
    CreatedAtUtc            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UpdatedByUserID         INTEGER,
    UpdatedAtUtc            TIMESTAMPTZ,
    ApprovedByUserID        INTEGER,
    ApprovedAtUtc           TIMESTAMPTZ,
    VoidedByUserID          INTEGER,
    VoidedAtUtc             TIMESTAMPTZ,
    VoidReason              TEXT,
    Notes                   TEXT,

    CONSTRAINT fk_DriverRateAssignments_Driver_Company_Branch
        FOREIGN KEY (DriverID, CompanyID, BranchID)
        REFERENCES core.Drivers (DriverID, CompanyID, BranchID),
    CONSTRAINT fk_DriverRateAssignments_RateDefinition_Company
        FOREIGN KEY (RateDefinitionID, CompanyID)
        REFERENCES payroll.RateDefinitions (RateDefinitionID, CompanyID),
    CONSTRAINT fk_DriverRateAssignments_Creator FOREIGN KEY (CreatedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT fk_DriverRateAssignments_Updater FOREIGN KEY (UpdatedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT fk_DriverRateAssignments_Approver FOREIGN KEY (ApprovedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT fk_DriverRateAssignments_Voider FOREIGN KEY (VoidedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT uq_DriverRateAssignments_ID_RateDefinition
        UNIQUE (DriverRateAssignmentID, RateDefinitionID),
    CONSTRAINT uq_DriverRateAssignments_ID_Authoritative
        UNIQUE (DriverRateAssignmentID, IsAuthoritative),
    CONSTRAINT ck_DriverRateAssignments_Status
        CHECK (Status IN ('Pending', 'Approved', 'Superseded', 'Voided')),
    CONSTRAINT ck_DriverRateAssignments_EffectiveDates
        CHECK (EffectiveTo IS NULL OR EffectiveTo >= EffectiveFrom),
    CONSTRAINT ck_DriverRateAssignments_SupersededClosed
        CHECK (Status <> 'Superseded' OR EffectiveTo IS NOT NULL),
    CONSTRAINT ck_DriverRateAssignments_PendingMetadata CHECK (
        Status <> 'Pending'
        OR (ApprovedByUserID IS NULL AND ApprovedAtUtc IS NULL
            AND VoidedByUserID IS NULL AND VoidedAtUtc IS NULL AND VoidReason IS NULL)
    ),
    CONSTRAINT ck_DriverRateAssignments_ApprovalMetadata CHECK (
        Status = 'Pending'
        OR (ApprovedByUserID IS NOT NULL AND ApprovedAtUtc IS NOT NULL)
    ),
    CONSTRAINT ck_DriverRateAssignments_VoidMetadata CHECK (
        (Status = 'Voided' AND VoidedAtUtc IS NOT NULL)
        OR (Status <> 'Voided'
            AND VoidedByUserID IS NULL AND VoidedAtUtc IS NULL AND VoidReason IS NULL)
    ),
    CONSTRAINT excl_DriverRateAssignments_AuthoritativeOverlap
        EXCLUDE USING gist (
            DriverID WITH =,
            RateDefinitionID WITH =,
            daterange(EffectiveFrom, EffectiveTo, '[]') WITH &&
        ) WHERE (Status IN ('Approved', 'Superseded'))
);

CREATE UNIQUE INDEX ux_DriverRateAssignments_Pending
    ON payroll.DriverRateAssignments (DriverID, RateDefinitionID)
    WHERE Status = 'Pending';

CREATE UNIQUE INDEX ux_DriverRateAssignments_Approved
    ON payroll.DriverRateAssignments (DriverID, RateDefinitionID)
    WHERE Status = 'Approved';

CREATE INDEX ix_DriverRateAssignments_PendingByDefinition
    ON payroll.DriverRateAssignments (RateDefinitionID)
    WHERE Status = 'Pending';

CREATE INDEX ix_DriverRateAssignments_Company_Branch_Driver
    ON payroll.DriverRateAssignments (CompanyID, BranchID, DriverID);

-- ---------------------------------------------------------------------------
-- 6. DriverRateValues (no dates, no lifecycle; NULL Amount means missing)
-- ---------------------------------------------------------------------------
CREATE TABLE payroll.DriverRateValues (
    DriverRateValueID          SERIAL         PRIMARY KEY,
    DriverRateAssignmentID     INTEGER        NOT NULL,
    RateDefinitionID           INTEGER        NOT NULL,
    RateComponentDefinitionID  INTEGER        NOT NULL,
    Amount                     NUMERIC(18,4),

    CONSTRAINT fk_DriverRateValues_Assignment_RateDefinition
        FOREIGN KEY (DriverRateAssignmentID, RateDefinitionID)
        REFERENCES payroll.DriverRateAssignments (DriverRateAssignmentID, RateDefinitionID)
        ON DELETE CASCADE,
    CONSTRAINT fk_DriverRateValues_Component_RateDefinition
        FOREIGN KEY (RateComponentDefinitionID, RateDefinitionID)
        REFERENCES payroll.RateComponentDefinitions (RateComponentDefinitionID, RateDefinitionID),
    CONSTRAINT uq_DriverRateValues_Assignment_Component
        UNIQUE (DriverRateAssignmentID, RateComponentDefinitionID),
    CONSTRAINT ck_DriverRateValues_Amount CHECK (Amount IS NULL OR Amount >= 0)
);

CREATE INDEX ix_DriverRateValues_Component
    ON payroll.DriverRateValues (RateComponentDefinitionID);

-- ---------------------------------------------------------------------------
-- 7. Structural lock helpers
-- ---------------------------------------------------------------------------
CREATE FUNCTION payroll.fn_LockRateDefinitionStructure(p_RateDefinitionID INTEGER)
RETURNS TIMESTAMPTZ
LANGUAGE plpgsql
AS $$
DECLARE
    v_LockedAt TIMESTAMPTZ;
BEGIN
    SELECT rd.StructureLockedAtUtc
      INTO v_LockedAt
      FROM payroll.RateDefinitions rd
     WHERE rd.RateDefinitionID = p_RateDefinitionID
       FOR NO KEY UPDATE;
    RETURN v_LockedAt;
END
$$;

CREATE FUNCTION payroll.fn_AssertRateStructureMutable(p_RateDefinitionID INTEGER)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    IF payroll.fn_LockRateDefinitionStructure(p_RateDefinitionID) IS NOT NULL THEN
        RAISE EXCEPTION 'RATE_STRUCTURE_LOCKED'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('RateDefinitionID=%s', p_RateDefinitionID);
    END IF;
    IF EXISTS (
        SELECT 1
          FROM payroll.DriverRateAssignments a
         WHERE a.RateDefinitionID = p_RateDefinitionID
           AND a.Status = 'Pending'
    ) THEN
        RAISE EXCEPTION 'RATE_STRUCTURE_PENDING_ASSIGNMENT'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('RateDefinitionID=%s', p_RateDefinitionID);
    END IF;
END
$$;

CREATE FUNCTION payroll.fn_ShapeForCalculationMethod(p_Method VARCHAR)
RETURNS VARCHAR
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE p_Method
        WHEN 'PerUnit' THEN 'Scalar'
        WHEN 'OrdinalTier' THEN 'OrdinalTierSchedule'
    END
$$;

-- ---------------------------------------------------------------------------
-- 8. PayDefinitions guard: method/input changes respect the RateDefinition lock
-- ---------------------------------------------------------------------------
CREATE FUNCTION payroll.trg_PayDefinitions_StructureGuard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_RateDefinitionID INTEGER;
BEGIN
    IF NEW.CompanyID IS DISTINCT FROM OLD.CompanyID THEN
        RAISE EXCEPTION 'PAY_DEFINITION_COMPANY_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.CalculationMethod IS DISTINCT FROM OLD.CalculationMethod
       OR NEW.InputType IS DISTINCT FROM OLD.InputType THEN
        SELECT rd.RateDefinitionID
          INTO v_RateDefinitionID
          FROM payroll.RateDefinitions rd
         WHERE rd.PayDefinitionID = OLD.PayDefinitionID;
        IF FOUND THEN
            PERFORM payroll.fn_AssertRateStructureMutable(v_RateDefinitionID);
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_PayDefinitions_StructureGuard
BEFORE UPDATE ON payroll.PayDefinitions
FOR EACH ROW EXECUTE FUNCTION payroll.trg_PayDefinitions_StructureGuard();

-- ---------------------------------------------------------------------------
-- 9. RateDefinitions guard and shape/method consistency
-- ---------------------------------------------------------------------------
CREATE FUNCTION payroll.trg_RateDefinitions_BeforeInsert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.StructureLockedAtUtc IS NOT NULL THEN
        RAISE EXCEPTION 'RATE_STRUCTURE_LOCK_NOT_SETTABLE_ON_CREATE'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.PayDefinitionID IS NOT NULL THEN
        -- Serializes creation against a concurrent CalculationMethod change.
        PERFORM 1 FROM payroll.PayDefinitions pd
         WHERE pd.PayDefinitionID = NEW.PayDefinitionID
           FOR SHARE;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_RateDefinitions_BeforeInsert
BEFORE INSERT ON payroll.RateDefinitions
FOR EACH ROW EXECUTE FUNCTION payroll.trg_RateDefinitions_BeforeInsert();

CREATE FUNCTION payroll.trg_RateDefinitions_BeforeUpdate()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.CompanyID IS DISTINCT FROM OLD.CompanyID
       OR NEW.PayDefinitionID IS DISTINCT FROM OLD.PayDefinitionID
       OR NEW.StatusRateColumnID IS DISTINCT FROM OLD.StatusRateColumnID
       OR NEW.OwnerBranchID IS DISTINCT FROM OLD.OwnerBranchID THEN
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
$$;

CREATE TRIGGER trg_RateDefinitions_BeforeUpdate
BEFORE UPDATE ON payroll.RateDefinitions
FOR EACH ROW EXECUTE FUNCTION payroll.trg_RateDefinitions_BeforeUpdate();

CREATE FUNCTION payroll.trg_RateDefinitions_BeforeDelete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM payroll.fn_AssertRateStructureMutable(OLD.RateDefinitionID);
    RETURN OLD;
END
$$;

CREATE TRIGGER trg_RateDefinitions_BeforeDelete
BEFORE DELETE ON payroll.RateDefinitions
FOR EACH ROW EXECUTE FUNCTION payroll.trg_RateDefinitions_BeforeDelete();

CREATE FUNCTION payroll.trg_RateDefinitions_ShapeMatchesMethod()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_Shape VARCHAR(30);
    v_Method VARCHAR(30);
    v_PayDefinitionID INTEGER;
BEGIN
    IF TG_TABLE_NAME = 'paydefinitions' THEN
        SELECT rd.Shape INTO v_Shape
          FROM payroll.RateDefinitions rd
         WHERE rd.PayDefinitionID = NEW.PayDefinitionID;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        v_Method := NEW.CalculationMethod;
        v_PayDefinitionID := NEW.PayDefinitionID;
    ELSE
        IF NEW.PayDefinitionID IS NULL THEN
            RETURN NULL;
        END IF;
        SELECT pd.CalculationMethod INTO v_Method
          FROM payroll.PayDefinitions pd
         WHERE pd.PayDefinitionID = NEW.PayDefinitionID;
        v_Shape := NEW.Shape;
        v_PayDefinitionID := NEW.PayDefinitionID;
    END IF;

    IF payroll.fn_ShapeForCalculationMethod(v_Method) IS DISTINCT FROM v_Shape THEN
        RAISE EXCEPTION 'RATE_SHAPE_METHOD_MISMATCH'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('PayDefinitionID=%s Method=%s Shape=%s',
                                  v_PayDefinitionID, v_Method, v_Shape);
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER trg_RateDefinitions_ShapeMatchesMethod
AFTER INSERT OR UPDATE OF Shape, PayDefinitionID ON payroll.RateDefinitions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION payroll.trg_RateDefinitions_ShapeMatchesMethod();

CREATE CONSTRAINT TRIGGER trg_PayDefinitions_ShapeMatchesMethod
AFTER UPDATE OF CalculationMethod ON payroll.PayDefinitions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION payroll.trg_RateDefinitions_ShapeMatchesMethod();

-- ---------------------------------------------------------------------------
-- 10. RateComponentDefinitions guard
-- ---------------------------------------------------------------------------
CREATE FUNCTION payroll.trg_RateComponents_StructureGuard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM payroll.fn_AssertRateStructureMutable(OLD.RateDefinitionID);
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' AND NEW.RateDefinitionID IS DISTINCT FROM OLD.RateDefinitionID THEN
        RAISE EXCEPTION 'RATE_COMPONENT_DEFINITION_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;
    PERFORM payroll.fn_AssertRateStructureMutable(NEW.RateDefinitionID);
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_RateComponents_StructureGuard
BEFORE INSERT OR UPDATE OR DELETE ON payroll.RateComponentDefinitions
FOR EACH ROW EXECUTE FUNCTION payroll.trg_RateComponents_StructureGuard();

-- ---------------------------------------------------------------------------
-- 11. DriverRateAssignments lifecycle
-- ---------------------------------------------------------------------------
CREATE FUNCTION payroll.fn_AssertRateStructureComplete(p_RateDefinitionID INTEGER)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    v_Shape VARCHAR(30);
    v_Components INTEGER;
BEGIN
    SELECT rd.Shape INTO v_Shape
      FROM payroll.RateDefinitions rd
     WHERE rd.RateDefinitionID = p_RateDefinitionID;

    SELECT count(*) INTO v_Components
      FROM payroll.RateComponentDefinitions c
     WHERE c.RateDefinitionID = p_RateDefinitionID;

    IF v_Components = 0 OR (v_Shape = 'Scalar' AND v_Components <> 1) THEN
        RAISE EXCEPTION 'RATE_STRUCTURE_INCOMPLETE'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('RateDefinitionID=%s', p_RateDefinitionID);
    END IF;

    IF v_Shape = 'OrdinalTierSchedule' AND EXISTS (
        SELECT 1
          FROM (
              SELECT c.OrdinalFrom, c.OrdinalTo,
                     row_number() OVER w AS Pos,
                     lag(c.OrdinalTo) OVER w AS PrevTo,
                     lead(c.SequenceNo) OVER w AS NextSequenceNo
                FROM payroll.RateComponentDefinitions c
               WHERE c.RateDefinitionID = p_RateDefinitionID
              WINDOW w AS (ORDER BY c.SequenceNo)
          ) t
         WHERE (t.Pos = 1 AND t.OrdinalFrom <> 1)
            OR (t.Pos > 1 AND t.OrdinalFrom IS DISTINCT FROM t.PrevTo + 1)
            OR (t.NextSequenceNo IS NOT NULL AND t.OrdinalTo IS NULL)
            OR (t.NextSequenceNo IS NULL AND t.OrdinalTo IS NOT NULL)
    ) THEN
        RAISE EXCEPTION 'RATE_STRUCTURE_INVALID_TOPOLOGY'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('RateDefinitionID=%s', p_RateDefinitionID);
    END IF;
END
$$;

CREATE FUNCTION payroll.trg_DriverRateAssignments_BeforeInsert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_OwnerBranchID INTEGER;
BEGIN
    PERFORM payroll.fn_LockRateDefinitionStructure(NEW.RateDefinitionID);

    IF NEW.Status <> 'Pending' THEN
        RAISE EXCEPTION 'RATE_ASSIGNMENT_MUST_START_PENDING'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT rd.OwnerBranchID INTO v_OwnerBranchID
      FROM payroll.RateDefinitions rd
     WHERE rd.RateDefinitionID = NEW.RateDefinitionID;
    IF v_OwnerBranchID IS NOT NULL AND v_OwnerBranchID <> NEW.BranchID THEN
        RAISE EXCEPTION 'RATE_ASSIGNMENT_BRANCH_MISMATCH'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_DriverRateAssignments_BeforeInsert
BEFORE INSERT ON payroll.DriverRateAssignments
FOR EACH ROW EXECUTE FUNCTION payroll.trg_DriverRateAssignments_BeforeInsert();

CREATE FUNCTION payroll.trg_DriverRateAssignments_BeforeUpdate()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_CurrencyCode VARCHAR(3);
BEGIN
    IF NEW.CompanyID IS DISTINCT FROM OLD.CompanyID
       OR NEW.BranchID IS DISTINCT FROM OLD.BranchID
       OR NEW.DriverID IS DISTINCT FROM OLD.DriverID
       OR NEW.RateDefinitionID IS DISTINCT FROM OLD.RateDefinitionID THEN
        RAISE EXCEPTION 'RATE_ASSIGNMENT_IDENTITY_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    PERFORM payroll.fn_LockRateDefinitionStructure(OLD.RateDefinitionID);

    IF OLD.Status = 'Pending' THEN
        IF NEW.Status = 'Pending' THEN
            RETURN NEW;
        END IF;
        IF NEW.Status <> 'Approved' THEN
            RAISE EXCEPTION 'RATE_ASSIGNMENT_INVALID_TRANSITION'
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
              LEFT JOIN payroll.DriverRateValues v
                ON v.RateComponentDefinitionID = c.RateComponentDefinitionID
               AND v.DriverRateAssignmentID = OLD.DriverRateAssignmentID
             WHERE c.RateDefinitionID = NEW.RateDefinitionID
               AND v.Amount IS NULL
        ) THEN
            RAISE EXCEPTION 'RATE_ASSIGNMENT_INCOMPLETE'
                USING ERRCODE = 'check_violation',
                      DETAIL = format('DriverRateAssignmentID=%s', OLD.DriverRateAssignmentID);
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
        RAISE EXCEPTION 'RATE_ASSIGNMENT_AUTHORITATIVE_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NOT (
        (OLD.Status = 'Approved' AND NEW.Status IN ('Approved', 'Superseded', 'Voided'))
        OR (OLD.Status = 'Superseded' AND NEW.Status IN ('Superseded', 'Voided'))
        OR (OLD.Status = 'Voided' AND NEW.Status = 'Voided')
    ) THEN
        RAISE EXCEPTION 'RATE_ASSIGNMENT_INVALID_TRANSITION'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('%s -> %s', OLD.Status, NEW.Status);
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_DriverRateAssignments_BeforeUpdate
BEFORE UPDATE ON payroll.DriverRateAssignments
FOR EACH ROW EXECUTE FUNCTION payroll.trg_DriverRateAssignments_BeforeUpdate();

-- Discard: only a Pending assignment can be deleted, and its full value set
-- is written to audit.AuditLog in the same transaction.
CREATE FUNCTION payroll.trg_DriverRateAssignments_BeforeDelete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_Actor TEXT := nullif(current_setting('flussra.actor_user_id', TRUE), '');
BEGIN
    PERFORM payroll.fn_LockRateDefinitionStructure(OLD.RateDefinitionID);

    IF OLD.Status <> 'Pending' THEN
        RAISE EXCEPTION 'RATE_ASSIGNMENT_DELETE_BLOCKED'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('Status=%s', OLD.Status);
    END IF;

    INSERT INTO audit.AuditLog
        (CompanyID, BranchID, ActorUserID, ActionCode, EntitySchema, EntityName,
         EntityID, OldValueJson, SourceType)
    SELECT OLD.CompanyID, OLD.BranchID, v_Actor::INTEGER,
           'DRIVER_RATE_ASSIGNMENT_DISCARDED', 'payroll', 'DriverRateAssignments',
           OLD.DriverRateAssignmentID::TEXT,
           jsonb_build_object(
               'DriverRateAssignmentID', OLD.DriverRateAssignmentID,
               'DriverID', OLD.DriverID,
               'RateDefinitionID', OLD.RateDefinitionID,
               'EffectiveFrom', OLD.EffectiveFrom,
               'EffectiveTo', OLD.EffectiveTo,
               'Notes', OLD.Notes,
               'Values', COALESCE(
                   (SELECT jsonb_agg(
                               jsonb_build_object(
                                   'RateComponentDefinitionID', v.RateComponentDefinitionID,
                                   'Amount', v.Amount)
                               ORDER BY v.RateComponentDefinitionID)
                      FROM payroll.DriverRateValues v
                     WHERE v.DriverRateAssignmentID = OLD.DriverRateAssignmentID),
                   '[]'::jsonb))::TEXT,
           'Database';
    RETURN OLD;
END
$$;

CREATE TRIGGER trg_DriverRateAssignments_BeforeDelete
BEFORE DELETE ON payroll.DriverRateAssignments
FOR EACH ROW EXECUTE FUNCTION payroll.trg_DriverRateAssignments_BeforeDelete();

-- ---------------------------------------------------------------------------
-- 12. DriverRateValues: editable only while the parent assignment is Pending
-- ---------------------------------------------------------------------------
CREATE FUNCTION payroll.trg_DriverRateValues_PendingOnly()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_RateDefinitionID INTEGER;
    v_AssignmentID INTEGER;
    v_Status VARCHAR(30);
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_RateDefinitionID := OLD.RateDefinitionID;
        v_AssignmentID := OLD.DriverRateAssignmentID;
    ELSE
        v_RateDefinitionID := NEW.RateDefinitionID;
        v_AssignmentID := NEW.DriverRateAssignmentID;
    END IF;

    IF TG_OP = 'UPDATE'
       AND (NEW.DriverRateAssignmentID IS DISTINCT FROM OLD.DriverRateAssignmentID
            OR NEW.RateDefinitionID IS DISTINCT FROM OLD.RateDefinitionID
            OR NEW.RateComponentDefinitionID IS DISTINCT FROM OLD.RateComponentDefinitionID) THEN
        RAISE EXCEPTION 'RATE_VALUE_IDENTITY_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    PERFORM payroll.fn_LockRateDefinitionStructure(v_RateDefinitionID);

    SELECT a.Status INTO v_Status
      FROM payroll.DriverRateAssignments a
     WHERE a.DriverRateAssignmentID = v_AssignmentID;

    -- A missing parent is a foreign-key violation (insert) or a cascade from
    -- discarding a Pending assignment (delete); both are handled elsewhere.
    IF FOUND AND v_Status <> 'Pending' THEN
        RAISE EXCEPTION 'RATE_VALUES_IMMUTABLE_AFTER_APPROVAL'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('DriverRateAssignmentID=%s Status=%s', v_AssignmentID, v_Status);
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_DriverRateValues_PendingOnly
BEFORE INSERT OR UPDATE OR DELETE ON payroll.DriverRateValues
FOR EACH ROW EXECUTE FUNCTION payroll.trg_DriverRateValues_PendingOnly();

-- ---------------------------------------------------------------------------
-- 13. Company currency authority: authoritative target assignments are durable
--     monetary state. Pending assignments are non-authoritative and do not
--     count; Voided assignments kept their authoritative values and do.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION core.fn_company_has_durable_monetary_state(p_company_id INTEGER)
RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
    SELECT
        EXISTS (SELECT 1 FROM payroll.driverrates r
                WHERE r.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.driverratetiers t
                   JOIN payroll.driverrates r ON r.driverrateid = t.driverrateid
                   WHERE r.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.driverrateassignments a
                   WHERE a.companyid = p_company_id
                     AND a.isauthoritative)
        OR EXISTS (SELECT 1 FROM payroll.payrollbonusevents b
                   WHERE b.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.driverpayrules p
                   WHERE p.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payrolldraftlines d
                   WHERE d.companyid = p_company_id
                     AND (d.rateamount IS NOT NULL OR d.calculatedamount IS NOT NULL))
        OR EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshots s
                   WHERE s.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payrollfinallines f
                   WHERE f.companyid = p_company_id);
$$;
