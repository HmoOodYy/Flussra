-- =============================================================================
-- 0041: Surface enforced-but-uncatalogued permissions in sec.Permissions
--
-- Two permission codes are actively enforced by the backend but were never
-- added to the production permission catalogue via a migration:
--
--   payroll.entry       -- required for entering/editing payroll lines,
--                          transitioning periods (Draft->Open, Open->InReview),
--                          and submitting review items
--   review.decide       -- required for approving/rejecting review items
--
-- These codes existed only in conftest.py (test setup) and ensure_dev_admin.py
-- (dev seed script), which meant:
--   1. sec.fn_UserHasPermission Path A (Company Owner dynamic grant) returned
--      FALSE for these codes because it checks sec.permissions, causing Company
--      Owner to fail payroll.entry and review.decide checks in production.
--   2. Custom roles could not be granted these permissions via the UI.
--   3. The frontend /admin/permissions endpoint never listed them.
--
-- NOTE: payroll.period.create was already added by migration 0030.
--
-- Module codes:
--   payroll.entry   -- 'payroll' (appears in ui_only=true, visible in UI)
--   review.decide   -- 'review'  (excluded from ui_only=true filter until the
--                      UI step adds 'review' to the allowed list)
--
-- ON CONFLICT DO UPDATE: corrects any rows already inserted with the wrong
-- module code case (ensure_dev_admin.py used uppercase 'Payroll'/'Review')
-- so the ui_only filter (which uses lowercase) works correctly.
--
-- This migration is idempotent and safe to run multiple times.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Register payroll.entry in the permission catalogue
-- ---------------------------------------------------------------------------
INSERT INTO sec.permissions (permissioncode, permissionname, modulecode)
VALUES ('payroll.entry', 'Enter Payroll Data', 'payroll')
ON CONFLICT (permissioncode)
    DO UPDATE SET
        permissionname = EXCLUDED.permissionname,
        modulecode     = EXCLUDED.modulecode;

-- ---------------------------------------------------------------------------
-- 2. Register review.decide in the permission catalogue
-- ---------------------------------------------------------------------------
INSERT INTO sec.permissions (permissioncode, permissionname, modulecode)
VALUES ('review.decide', 'Approve / Reject Review Items', 'review')
ON CONFLICT (permissioncode)
    DO UPDATE SET
        permissionname = EXCLUDED.permissionname,
        modulecode     = EXCLUDED.modulecode;

-- ---------------------------------------------------------------------------
-- 3. Grant payroll.entry to every company role that already has payroll.edit
--    Rationale: a role that edits payroll data must also be able to enter it.
--    Mirrors the grant pattern used in migration 0030 for payroll.period.create.
-- ---------------------------------------------------------------------------
INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
SELECT crp.companyroleid, 'payroll.entry'
FROM   sec.companyrolepermissions crp
WHERE  crp.permissioncode = 'payroll.edit'
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. Grant payroll.entry to legacy global roles (sec.rolepermissions) that
--    already hold payroll.edit, so the backward-compat path (Path C) works.
-- ---------------------------------------------------------------------------
INSERT INTO sec.rolepermissions (roleid, permissionid)
SELECT rp.roleid, p.permissionid
FROM   sec.rolepermissions rp
JOIN   sec.permissions     p  ON p.permissioncode = 'payroll.entry'
WHERE  rp.permissionid = (
    SELECT permissionid FROM sec.permissions WHERE permissioncode = 'payroll.edit'
)
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- 5. Ensure COMPANY_OWNER company roles have explicit rows for both new codes.
--    Path A of fn_UserHasPermission handles Company Owner dynamically, but
--    maintaining explicit companyrolepermissions rows keeps the data consistent
--    and prevents surprises if Path A logic changes.
-- ---------------------------------------------------------------------------
INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
SELECT cr.companyroleid, perms.permissioncode
FROM   sec.companyroles cr
CROSS  JOIN (VALUES ('payroll.entry'), ('review.decide')) AS perms(permissioncode)
WHERE  cr.rolecode = 'COMPANY_OWNER'
ON CONFLICT DO NOTHING;
