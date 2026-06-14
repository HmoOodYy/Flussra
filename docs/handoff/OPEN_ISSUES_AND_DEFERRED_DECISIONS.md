# Open Issues and Deferred Decisions

Last verified: 2026-05-30 (M18 — Frontend Handoff / Backend Readiness Docs)

---

## NOT BLOCKING FRONTEND

### AllowSelfApproval Default

Status: implemented (migration 0004), default is TRUE.

- core.Companies.AllowSelfApproval = TRUE: same user who submitted can approve.
- Set FALSE to require separation of duties.
- This also applies to PeriodApproval items created during Open→InReview.
- Action needed: confirm desired production default before go-live.

### Pay Items Pending Date Behavior (end_date+1)

Status: documented, confirmed working for M11/M12.

- If no effective_from supplied and an open period exists, change is
  scheduled to current_period.end_date + 1.
- Assumes periods are contiguous. Supply effective_from explicitly if
  periods have gaps between them.
- Decision: keep current behavior; revisit when period scheduling is automated.

### Hardcoded System Rate Fallback (P2 Codex)

Status: open, low risk.

- `_SYSTEM_LINE_TYPE_INFO` dict in payroll/service.py is still used as a
  fast-path fallback for system item line type info.
- Full removal requires ensuring all 11 system items have complete
  PayItemRateTypeMap rows for every rate type they're expected to support.
- Not blocking any frontend workflow. Deferred cleanup.

### Fixed RateBehavior for Custom Items

Status: partially implemented, deferred.

- Custom items with RateBehavior=Fixed return NULL CalculatedAmount.
- No needs_manager_review flag raised either.
- Non-blocking for standard driver pay workflow. Deferred until Fixed
  behavior is fully specified.

### Period Pay Update Audit Coverage (P2 Codex)

Status: audit logging wired, extra test coverage deferred.

- `update_period_pay_line` writes PERIOD_PAY_UPDATED audit entry (code is in place).
- Update path has no dedicated integration test in the current suite
  (add/void are covered). Low risk — code path is same pattern.
- Deferred per explicit user instruction.

### Dashboard Setup Warnings — Deterministic "No Warnings" Test (P3 Codex)

Status: 1 test skipped.

- `TestDashboardSetupWarnings::test_no_warnings_when_all_clear` skips when
  the test environment has active drivers without approved rates.
- All four individual warning tests pass unconditionally.
- Not a coverage gap. Deferred cleanup.

### Stale Comments in Service Files (P3 Codex)

Status: open, cosmetic only.

- Some comments in payroll/service.py reference earlier milestone milestone
  numbers or describe deferred behavior that has since been implemented.
- Deferred cosmetic cleanup.

---

## DEFERRED BACKEND FEATURES (not started)

### PayProfiles

- PayProfiles, PayProfilePayItems, PayProfileRates, PersonPayProfileAssignments
  tables exist in the schema but are not calculation-active.
- Future milestone. Not blocking frontend.

### GUARANTEED_MINIMUM System Item

- Deferred to after the pay-rule engine is further refined.
- Not blocking any current workflow.

### Activity Feed Endpoint (/activity)

- Needs its own endpoint for recent audit trail events.
- Not yet designed. Deferred.

### Dashboard Caching

- GET /dashboard runs live queries on every call.
- Redis / TTL caching deferred.

### Per-Driver Pay Summary Drill-Down

- No per-driver summary endpoint yet.
- Deferred.

### Import Batch / Bulk Entry

- No bulk import endpoint.
- Deferred.

### Reports / Exports

- No report or export endpoint.
- Deferred.

### DRIVERS_NO_APPROVED_RATE — Per-Rate-Type Gap Check

- Current warning is broad: any driver without any approved rate triggers it.
- Finer-grained check (which specific rate types are missing for which drivers)
  deferred.

### Production Hardening

- Deployment, Docker, CI/CD, secrets management, observability, backup,
  load testing: not started.
- MFA, refresh tokens, advanced auth controls: deferred.

---

## RESOLVED / CLOSED

### Manual PeriodApproval Creation via POST /review/items
Resolved in Codex P0 fix. Blocked unconditionally with 422.

### Write-Back Period Validation in decide_review_item
Resolved in Codex P0 fix. Entity metadata, period existence, company, branch validated.

### Open→InReview Concurrency
Resolved in Codex P1 fix. Atomic UPDATE + DB unique index + SAIntegrityError catch.

### Admin _ensure_admin Stale Token
Resolved in Codex P1 fix. User and company status validated on every admin call.

### Dashboard Per-Branch Permission
Resolved in Codex P1 fix. payroll.entry checked per-branch for SpecificBranch users.

### DriverPayRules Archived Period Protection
Resolved in Codex P1 fix. Archived treated same as Locked in all period-range checks.

### Draft-Line and Period-Pay Audit Logging
Resolved in Codex P1 fix. All 6 mutations wired; rollback on audit failure.

### Review Entity Metadata Validation
Resolved in Codex P0 fix (for PeriodApproval items). General free-form review items
(Correction, etc.) are still free-form as designed.

### Custom Pay Item LineType in Draft Lines
Resolved in M13. LineType now validated against live active PayItemCodes for the branch.

### Rate Structures for Custom Items
Resolved in M13. OrdinalTier, RangeBracket, RangeProgressive, Block all implemented.

### Driver Pay Rules (Minimum / Maximum Pay)
Resolved in M13/M14/M15. Full lifecycle including finalization engine integration.

### Draft Line Audit Coverage
Resolved in Codex P1 fix. DRAFT_LINE_ADDED, DRAFT_LINE_UPDATED, DRAFT_LINE_VOIDED all wired.

### Local Development Database Credentials
Status: open but non-blocking. Fix `payroll_user` password in `backend\.env` as needed.
Dev database has been manually upgraded to 0013 by the project owner.
