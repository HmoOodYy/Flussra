-- =============================================================================
-- Migration 0003: Standard system pay items seed data
-- =============================================================================
--
-- Inserts the 10 platform-level pay items that exist on every installation.
-- PTO_STATUS was removed in migration 0055 (product decision: status is a
-- Daily Grid column only, not a Pay Item).
-- CompanyID and BranchID are NULL for system-standard items - they belong to
-- no specific company and appear for every tenant.
--
-- IsSystemStandard = TRUE means the item is platform-managed and its
-- core behaviour (DataType, ItemScope, RateBehavior) cannot be edited
-- through the normal settings UI.
--
-- AppearsInPayrollEntry controls which items show in the daily payroll
-- entry grid. It is FALSE for period-level and summary items.
--
-- IsDefaultBranchActive controls the fallback when no BranchPayItemConfig
-- row exists for a branch: TRUE = item is on by default.

INSERT INTO payroll.PayItems (
    CompanyID, BranchID,
    PayItemCode, PayItemName, Category,
    DataType, Unit,
    Status, SortOrder,
    AppearsInPayrollEntry, AppearsInLedger, AppearsInReports,
    RequiresRate, IsSystemStandard,
    ItemScope, RateBehavior, IsDefaultBranchActive
) VALUES
    (NULL, NULL, 'HOURS',             'Hours Worked',       'Time',       'Decimal', 'Hour',   'Active',  1, TRUE,  TRUE, TRUE, TRUE,  TRUE, 'Daily',  'PerUnit',    TRUE),
    (NULL, NULL, 'MILES',             'Miles Driven',       'Distance',   'Decimal', 'Mile',   'Active',  2, TRUE,  TRUE, TRUE, TRUE,  TRUE, 'Daily',  'PerUnit',    TRUE),
    (NULL, NULL, 'LOADS',             'Loads Delivered',    'Count',      'Integer', 'Load',   'Active',  3, TRUE,  TRUE, TRUE, TRUE,  TRUE, 'Daily',  'PerUnit',    TRUE),
    (NULL, NULL, 'OVERNIGHT',         'Overnight Stay',     'Count',      'Integer', 'Night',  'Active',  4, TRUE,  TRUE, TRUE, TRUE,  TRUE, 'Daily',  'Fixed',      FALSE),
    (NULL, NULL, 'WAIT_TIME',         'Wait Time',          'Time',       'Decimal', 'Hour',   'Active',  5, TRUE,  TRUE, TRUE, TRUE,  TRUE, 'Daily',  'PerUnit',    FALSE),
    (NULL, NULL, 'PALLETS',           'Pallets Handled',    'Count',      'Integer', 'Pallet', 'Active',  6, TRUE,  TRUE, TRUE, TRUE,  TRUE, 'Daily',  'PerUnit',    FALSE),
    (NULL, NULL, 'SILOS',             'Silos Filled',       'Count',      'Integer', 'Silo',   'Active',  7, TRUE,  TRUE, TRUE, TRUE,  TRUE, 'Daily',  'PerUnit',    FALSE),
    (NULL, NULL, 'BONUS',             'Performance Bonus',  'Bonus',      'Decimal', NULL,     'Active',  8, FALSE, TRUE, TRUE, FALSE, TRUE, 'Period', 'Fixed',      FALSE),
    (NULL, NULL, 'ADJUSTMENT',        'Manual Adjustment',  'Adjustment', 'Decimal', NULL,     'Active',  9, FALSE, TRUE, TRUE, FALSE, TRUE, 'Period', 'Fixed',      FALSE),
    (NULL, NULL, 'GUARANTEED_MINIMUM','Guaranteed Minimum', 'Guarantee',  'Decimal', NULL,     'Active', 10, FALSE, TRUE, TRUE, FALSE, TRUE, 'Period', 'Calculated', FALSE)
ON CONFLICT DO NOTHING;
