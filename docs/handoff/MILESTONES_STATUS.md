# Milestones Status

Last verified: 2026-05-30 (M18 — Frontend Handoff / Backend Readiness Docs)

## Completed Milestones

### M1 Auth
- POST /auth/login, GET /auth/me
- JWT, bcrypt, generic error responses, /auth/me DB revalidation
- Tests: test_auth.py

### M2 Core Domain
- GET /core/branches, /core/branches/{id}
- GET /core/people, /core/drivers, POST /core/drivers, PATCH /core/drivers/{id}
- Branch-scoped access, drivers.manage permission
- Tests: test_core.py, test_branch_access.py

### M3 Payroll Periods
- GET/POST /payroll/periods, PATCH /payroll/periods/{id}/status
- Status transition map, one-Draft-per-branch and one-Open-per-branch constraints
- Tests: test_payroll.py

### M4 Payroll Entry and Draft Lines
- GET/POST/PATCH/DELETE /payroll/periods/{id}/lines, GET .../lines/summary
- Blocked for frozen/locked periods, branch and permission gating
- Tests: test_entry.py

### M5 Finalization and Ledger
- POST /payroll/periods/{id}/finalize, GET .../final-lines
- Atomic claim, empty-period block, audit, duplicate constraint
- Tests: test_finalize.py

### M6 Pay Rates
- GET/POST/PATCH/DELETE /payroll/rates, POST .../approve, GET .../lookup
- PerUnit effective dating, approval flow, supersession
- Tests: test_rates.py

### M7 Review and Approvals Foundation
- GET/POST /review/items, POST /review/items/{id}/decide
- Foundation queue; AllowSelfApproval policy (core.Companies.AllowSelfApproval)
- Tests: test_review.py

### M8 Settings and Admin API Foundation
- GET/PATCH /settings/company, GET/POST/PATCH /settings/branches, POST .../set-default
- Tests: test_settings.py

### M9 Branch Payroll Setup and Status Keys
- GET/PUT /settings/branches/{id}/payroll-setup
- GET/POST/PATCH/DELETE /settings/branches/{id}/status-keys
- Tests: test_settings_payroll.py

### M10 Users and Permissions
- GET/POST/PATCH /admin/users, POST .../reset-password
- GET/POST/DELETE /admin/users/{id}/roles, GET /admin/roles, GET /admin/permissions
- Tests: test_admin.py

### M11 Pay Items and Rules API
- GET /settings/branches/{id}/pay-items (+ /{item_id}, /missing, /{item_id}/history)
- PATCH /settings/branches/{id}/pay-items/{item_id}
- 11 system pay items seeded. Effective-dated BranchPayItemConfig.
- Open period protection: changes scheduled after current period end (end_date+1).
- Tests: test_settings_payitems.py

### M12 Custom Pay Items Catalog and Request/Approval Flow
- Admin direct: GET/POST/PATCH/DELETE /settings/pay-items, GET .../usage
- Branch request flow: POST/GET /settings/pay-item-requests, GET /{id}, POST /{id}/decide
- M12 item types: Daily (ItemScope=Daily, RateBehavior=PerUnit), Period (EnteredAmount)
- Smart delete: never used = physical; only void/empty drafts = cleanup+physical;
  meaningful usage = Retired (code permanently locked).
- System items cannot be deleted. System codes blocked explicitly in service.
- On approval: PayItem (company-level) + BranchPayItemConfig (requesting branch) in one TX.
- Duplicate pending/approved request for same code → 422.
- Tests: test_settings_custom_pay_items.py

### M13 Rate Structures, Calculation Engine, Period Pay, Driver Pay Rules
- Migration 0009: DriverRateTiers table; BlockSize + RoundingRule on DriverRates;
  expanded ck_PayItems_RateBehavior CHECK for OrdinalTier/RangeBracket/RangeProgressive/Block.
- Migration 0010: LineScope column on PayrollDraftLines (Daily | Period).
- Rate structures: OrdinalTier, RangeBracket, RangeProgressive, Block.
- Calculation engine: reads PayItem.RateBehavior to compute CalculatedAmount per draft line.
- Tiered rates: create/update/approve with full tier history preservation.
- LineType validated against live active PayItemCodes (replaces M4 hardcoded list).
- PayItemRateTypeMap wiring for custom items.
- Period Pay lines: POST/PATCH/DELETE /payroll/periods/{id}/period-pay
- Driver Pay Rules foundation: MinimumPay / MaximumPay enforcement in finalization.
- Tests: test_m13a.py, test_m13b.py, test_m13c.py, test_m13d.py, test_m14.py

### M14 Finalization Engine Hardening
- Migration 0011: LineScope on PayrollFinalLines.
- Migration 0012: DriverPayRules table; SYS_MIN_TOPUP and SYS_MAX_CAP system pay items.
- Finalization: compute total earned, apply min/max rules, produce system top-up/cap lines.
- Period Pay lines included in finalization.
- needs_manager_review lines block InReview submission and finalization.
- Zero-pay finalization blocks (no lines with positive amount).
- Tests: test_m14.py

### M15 Driver Pay Rules Management
- GET /payroll/driver-pay-rules, POST (create), POST .../void, POST .../end
- Rule history, finalized-period protection (cannot void/end if Locked or Archived
  period was governed by the rule — Archived treated same as Locked).
- Tests: test_m15.py

### M16 Review-to-Payroll Wiring
- Open→InReview auto-creates a PeriodApproval review item in the same transaction.
- Approved decision → period moves to Approved.
- Rejected decision → period moves back to Open.
- EditRequested decision → period moves back to Open, edits allowed.
- Duplicate Pending PeriodApproval blocked by service guard.
- InReview→Approved blocked if any pending non-PeriodApproval review items remain.
- Tests: test_m16.py

### M17 Dashboard / Home API
- Migration 0013: partial unique index ux_ReviewItems_OnePendingPeriodApproval.
- GET /dashboard — read-only, requires payroll.entry.
- Response: generated_at, scope, period counts by status, review item counts,
  active driver count, last_finalized_period (Locked OR Archived), setup_warnings,
  branch_summaries.
- Four setup warnings: BRANCH_NO_PAYROLL_SETTINGS, OPEN_PERIOD_NEEDS_MANAGER_REVIEW,
  DRIVERS_NO_APPROVED_RATE, PAY_ITEM_MISSING_RATE_TYPE_MAP.
- AllCompanyBranches users see all branches; SpecificBranch users see only accessible branches.
- Per-branch payroll.entry permission check for SpecificBranch users.
- Tests: test_m17.py

### Codex P0/P1 Security and Concurrency Fixes
- [P0] PeriodApproval creation via POST /review/items blocked (422).
- [P0] decide_review_item validates entity_schema, entity_name, entity_id, company,
  and branch match before write-back.
- [P1] Open→InReview uses atomic UPDATE WHERE status='Open' RETURNING.
- [P1] SAIntegrityError on PeriodApproval INSERT caught → clean 422
  ("A pending review already exists for this payroll period.").
- [P1] DB partial unique index: at most one Pending PeriodApproval per (companyid, entityid).
- [P1] Admin _ensure_admin revalidates user.isactive, canlogin, company.status, issuspended.
- [P1] Dashboard payroll.entry checked per-branch for SpecificBranch users.
- [P1] DriverPayRules Archived = finalized in period-range checks.
- [P1] Draft-line and period-pay mutations write audit entries; rollback on audit failure.
- Tests: test_codex_p0p1.py

---

## Current Test Count

**734 passed, 1 skipped, 0 failed**

---

## Open Risks Per Milestone

### M7
- AllowSelfApproval default is TRUE — confirm production default before going live.

### M11
- end_date+1 pending date assumes contiguous periods. Supply effective_from explicitly
  for gaps.

### M13/M14
- `Fixed` RateBehavior for custom items: returns NULL CalculatedAmount, no review flag.
  Deferred for non-standard custom items.
- `_SYSTEM_LINE_TYPE_INFO` dict still used as a fast-path fallback for system items;
  full removal deferred until explicitly requested.

### M16/M17
- AllowSelfApproval applies to PeriodApproval items (same user who submitted may approve).
  Confirm production policy.

### General
- Production hardening (deployment, secrets, observability, backup) not done.
- PayProfiles exist in schema but are not calculation-active.
