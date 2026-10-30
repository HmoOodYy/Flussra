-- settings.manage now means only "manage company settings" (company profile,
-- currency, branch settings, Company Statuses and Status rate columns), and the
-- legacy setup.manage code is retired.
--
-- Until now both codes were hidden master keys: the API accepted either of them in
-- place of the specific permission on the users, roles, permission-listing, employee
-- link, pay rate and pay item routes. To keep every existing holder's reach, each
-- grant of those codes is converted below into the specific permissions it used to
-- stand in for.
--
-- settings.manage stood in for
--   users.view, users.create, users.edit,
--   roles.view, roles.create, roles.edit, roles.delete,
--   payrates.view, payrates.edit, payitems.view
-- setup.manage stood in for all of the above, plus
--   payitems.edit (branch pay-definition configuration) and the company-settings
--   routes, which are now authorized by settings.manage (with its settings.view
--   dependency parent).
--
-- Dependency parents the application normalizes on save (users.view, roles.view,
-- payitems.view, payrates.view, settings.view) are included in the lists above.
-- settings.manage grants keep their settings.manage row, whose meaning narrows.
--
-- DRIVER roles are skipped because generic permissions are never effective for them.

CREATE TEMP TABLE PermissionSplitMap (
    SourceCode  VARCHAR(100) NOT NULL,
    GrantedCode VARCHAR(100) NOT NULL,
    PRIMARY KEY (SourceCode, GrantedCode)
);

INSERT INTO PermissionSplitMap (SourceCode, GrantedCode) VALUES
    ('settings.manage', 'users.view'),
    ('settings.manage', 'users.create'),
    ('settings.manage', 'users.edit'),
    ('settings.manage', 'roles.view'),
    ('settings.manage', 'roles.create'),
    ('settings.manage', 'roles.edit'),
    ('settings.manage', 'roles.delete'),
    ('settings.manage', 'payrates.view'),
    ('settings.manage', 'payrates.edit'),
    ('settings.manage', 'payitems.view'),
    ('setup.manage',    'users.view'),
    ('setup.manage',    'users.create'),
    ('setup.manage',    'users.edit'),
    ('setup.manage',    'roles.view'),
    ('setup.manage',    'roles.create'),
    ('setup.manage',    'roles.edit'),
    ('setup.manage',    'roles.delete'),
    ('setup.manage',    'payrates.view'),
    ('setup.manage',    'payrates.edit'),
    ('setup.manage',    'payitems.view'),
    ('setup.manage',    'payitems.edit'),
    ('setup.manage',    'settings.manage'),
    ('setup.manage',    'settings.view');

-- Company roles: add the specific permissions next to the legacy ones.
INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
SELECT DISTINCT crp.CompanyRoleID, m.GrantedCode
FROM sec.CompanyRolePermissions crp
JOIN PermissionSplitMap m ON m.SourceCode = crp.PermissionCode
JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = crp.CompanyRoleID
WHERE cr.RoleCode <> 'DRIVER'
ON CONFLICT (CompanyRoleID, PermissionCode) DO NOTHING;

-- Member-specific permissions: add active ALLOW overrides for the specific permissions,
-- keeping the original granter. An existing active override for the same permission wins.
INSERT INTO sec.UserPermissionOverrides
    (UserID, CompanyID, PermissionCode, Effect, IsActive, GrantedByUserID, Notes)
SELECT DISTINCT ON (upo.UserID, upo.CompanyID, m.GrantedCode)
       upo.UserID, upo.CompanyID, m.GrantedCode, 'ALLOW', TRUE, upo.GrantedByUserID,
       'Granted in place of ' || upo.PermissionCode || ', which no longer covers this permission.'
FROM sec.UserPermissionOverrides upo
JOIN PermissionSplitMap m ON m.SourceCode = upo.PermissionCode
WHERE upo.IsActive
  AND upo.Effect = 'ALLOW'
ORDER BY upo.UserID, upo.CompanyID, m.GrantedCode, upo.PermissionCode
ON CONFLICT DO NOTHING;

-- Retire setup.manage: revoke the overrides (history is kept) and remove the role grants
-- and any catalogue row.
UPDATE sec.UserPermissionOverrides
SET IsActive = FALSE,
    RevokedAtUtc = COALESCE(RevokedAtUtc, NOW())
WHERE PermissionCode = 'setup.manage'
  AND IsActive;

DELETE FROM sec.CompanyRolePermissions WHERE PermissionCode = 'setup.manage';

DELETE FROM sec.Permissions WHERE PermissionCode = 'setup.manage';

UPDATE sec.Permissions
SET PermissionName = 'Manage Company Settings',
    Notes = 'Company profile, currency, branches, Company Statuses and Status rate columns. Grants no access to users, roles, pay items or pay rates.'
WHERE PermissionCode = 'settings.manage';

DROP TABLE PermissionSplitMap;
