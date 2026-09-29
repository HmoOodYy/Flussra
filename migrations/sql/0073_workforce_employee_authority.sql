-- P1b: establish Employee read/write permissions and make the legacy
-- EmployeeType column non-authoritative and nullable.

INSERT INTO sec.Permissions (PermissionCode, PermissionName, ModuleCode)
VALUES
    ('employees.view',   'View Employees',   'employees'),
    ('employees.manage', 'Manage Employees', 'employees')
ON CONFLICT (PermissionCode) DO NOTHING;

ALTER TABLE core.Employees
    ALTER COLUMN EmployeeType DROP NOT NULL;
