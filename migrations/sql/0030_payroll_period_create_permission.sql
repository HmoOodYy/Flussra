-- 0030: Add payroll.period.create permission
--
-- Introduces a dedicated permission code for creating payroll periods.
-- Previously the backend checked payroll.entry (a legacy/test-only code not
-- present in production role seeds). This migration seeds the new code and
-- grants it to every company role that already holds payroll.edit or
-- payroll.entry so no production user loses access.
--
-- The backend service layer is updated (alongside this migration) to accept
-- either payroll.period.create OR payroll.entry at the create_period gate.

-- 1. Register the new permission code.
INSERT INTO sec.permissions (permissioncode, permissionname, modulecode)
VALUES ('payroll.period.create', 'Create Payroll Period', 'payroll')
ON CONFLICT (permissioncode) DO NOTHING;

-- 2. Grant to every COMPANY role that has payroll.edit (production seed code).
INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
SELECT crp.companyroleid, 'payroll.period.create'
FROM   sec.companyrolepermissions crp
WHERE  crp.permissioncode = 'payroll.edit'
ON CONFLICT DO NOTHING;

-- 3. Grant to every COMPANY role that has payroll.entry (legacy/test code),
--    in case any environment seeded via conftest or ensure_dev_admin.
INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
SELECT crp.companyroleid, 'payroll.period.create'
FROM   sec.companyrolepermissions crp
WHERE  crp.permissioncode = 'payroll.entry'
ON CONFLICT DO NOTHING;

-- 4. Grant to every GLOBAL role that has payroll.entry (rolepermissions table).
--    PAYROLL_ADMIN is the primary global role in tests.
INSERT INTO sec.rolepermissions (roleid, permissionid)
SELECT rp.roleid, p.permissionid
FROM   sec.rolepermissions rp
JOIN   sec.permissions p ON p.permissioncode = 'payroll.period.create'
WHERE  rp.permissionid = (
    SELECT permissionid FROM sec.permissions WHERE permissioncode = 'payroll.entry'
)
ON CONFLICT DO NOTHING;
