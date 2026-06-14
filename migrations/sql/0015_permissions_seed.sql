-- =============================================================================
-- 0015_permissions_seed.sql
--
-- Expands sec.Permissions with the full permission set for company-role-based
-- access control.
--
-- All inserts use ON CONFLICT DO NOTHING so this file is idempotent and
-- safe to run after existing seeds that already inserted a subset of these
-- codes (e.g. setup.manage, drivers.manage from conftest.py).
--
-- Permission code conventions:
--   <module>.<action>   e.g.  payroll.view,  roles.edit
-- =============================================================================

INSERT INTO sec.Permissions (permissioncode, permissionname, modulecode) VALUES
    -- Company / Branches
    ('company.view',      'View Company Profile',       'company'),
    ('company.edit',      'Edit Company Profile',       'company'),
    ('branches.view',     'View Branches',              'company'),
    ('branches.create',   'Create Branches',            'company'),
    ('branches.edit',     'Edit Branches',              'company'),

    -- Roles
    ('roles.view',        'View Roles & Permissions',   'roles'),
    ('roles.create',      'Create Roles',               'roles'),
    ('roles.edit',        'Edit Role Permissions',      'roles'),
    ('roles.delete',      'Delete Custom Roles',        'roles'),

    -- Users / Members
    ('users.view',        'View Users',                 'users'),
    ('users.create',      'Create Users',               'users'),
    ('users.edit',        'Edit Users',                 'users'),
    ('users.deactivate',  'Deactivate Users',           'users'),

    -- Payroll
    ('payroll.view',      'View Payroll',               'payroll'),
    ('payroll.edit',      'Edit Payroll Data',          'payroll'),
    ('payroll.approve',   'Approve Payroll',            'payroll'),
    ('payroll.finalize',  'Finalize Payroll',           'payroll'),

    -- Pay Items
    ('payitems.view',     'View Pay Items',             'payitems'),
    ('payitems.edit',     'Edit Pay Items',             'payitems'),

    -- Pay Rates
    ('payrates.view',     'View Pay Rates',             'payrates'),
    ('payrates.edit',     'Edit Pay Rates',             'payrates'),

    -- Drivers
    ('drivers.view',      'View Drivers',               'drivers'),
    ('drivers.create',    'Create Drivers',             'drivers'),
    ('drivers.edit',      'Edit Drivers',               'drivers'),

    -- Dispatch / Operations
    ('dispatch.view',     'View Dispatch',              'dispatch'),
    ('dispatch.edit',     'Edit Dispatch',              'dispatch'),

    -- Reports
    ('reports.view',      'View Reports',               'reports'),

    -- Settings (broad admin access)
    ('settings.view',     'View Settings',              'settings'),
    ('settings.manage',   'Manage Settings',            'settings')

ON CONFLICT (permissioncode) DO NOTHING;

-- Note: the legacy codes already seeded by conftest.py / ensure_dev_admin.py
-- are preserved unchanged via ON CONFLICT DO NOTHING:
--   payroll.entry, payroll.approve_rate, payroll.finalize,
--   review.decide, setup.manage, drivers.manage
