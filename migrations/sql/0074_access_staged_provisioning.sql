-- P2a: make unprovisioned Access accounts explicit and enforce Employee links.

DO $$
BEGIN
    IF EXISTS (
        SELECT EmployeeID FROM sec.Users
        WHERE EmployeeID IS NOT NULL
        GROUP BY EmployeeID HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION '0074 refused: duplicate sec.Users EmployeeID links exist; reset disposable development data or reconcile identities explicitly';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM sec.Users u
        JOIN core.Employees e ON e.EmployeeID = u.EmployeeID
        WHERE u.EmployeeID IS NOT NULL
          AND u.CompanyID IS DISTINCT FROM e.CompanyID
    ) THEN
        RAISE EXCEPTION '0074 refused: sec.Users EmployeeID links cross company boundaries; reset disposable development data or reconcile identities explicitly';
    END IF;
END $$;

ALTER TABLE sec.Users
    ADD COLUMN IsStaged BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE sec.Users u
SET IsStaged = TRUE, CanLogin = FALSE
WHERE NOT EXISTS (
    SELECT 1 FROM sec.UserBranchRoles ubr
    WHERE ubr.UserID = u.UserID AND ubr.IsActive = TRUE
);

ALTER TABLE sec.Users
    ADD CONSTRAINT ck_Users_StagedCannotLogin CHECK (NOT IsStaged OR NOT CanLogin),
    ADD CONSTRAINT ck_Users_EmployeeNeedsCompany CHECK (EmployeeID IS NULL OR CompanyID IS NOT NULL),
    ADD CONSTRAINT fk_Users_Employee_Company
        FOREIGN KEY (EmployeeID, CompanyID)
        REFERENCES core.Employees (EmployeeID, CompanyID);

CREATE UNIQUE INDEX ux_Users_EmployeeID_NotNull
    ON sec.Users (EmployeeID)
    WHERE EmployeeID IS NOT NULL;
