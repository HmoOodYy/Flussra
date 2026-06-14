# Project Handoff

Last verified: 2026-05-30 (M18 — Frontend Handoff / Backend Readiness Docs)

## Active Project

- Active project path: `C:\Projects\etbdnt\Payroll_App_v3`
- Reference-only old project path: `C:\Projects\etbdnt\Payroll_App_original_uploaded_backup`
- The old PySide project is for reference only. Do not modify it.

## Final Technology Direction

- Backend: Python, FastAPI, async SQLAlchemy connection layer, PostgreSQL
- Database: PostgreSQL is the source of truth
- Migrations: Alembic with raw SQL migration files
- Frontend direction: React and TypeScript (ready to start)
- Architecture style: API-first modular monolith
- UI rule: the frontend must call APIs only — no direct PostgreSQL access

## Current Backend Status

**Backend is complete through M17. All P0/P1 blockers resolved. Safe to start frontend.**

Completed milestones:

- M1  Auth
- M2  Core domain (branches, drivers, people)
- M3  Payroll periods
- M4  Payroll entry and draft lines
- M5  Finalization and ledger
- M6  Pay rates
- M7  Review and approvals foundation
- M8  Settings and admin API foundation
- M9  Branch payroll setup and status keys
- M10 Users and permissions
- M11 Pay items and rules API
- M12 Custom pay items catalog and request/approval flow
- M13 Rate structures, calculation engine, period pay, driver pay rules
- M14 Finalization engine hardening (min/max rules, period pay in final lines)
- M15 Driver pay rules management (void, end, history)
- M16 Review-to-payroll wiring (Open→InReview auto-creates PeriodApproval; decisions update period)
- M17 Dashboard / Home API (GET /dashboard, period counts, review counts, warnings, branch summaries)
- Codex P0/P1 fixes (concurrent race guard, write-back validation, audit logging, per-branch permission)

## Latest Test Result

Command (from `C:\Projects\etbdnt\Payroll_App_v3\backend`):

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Result: **734 passed, 1 skipped, 0 failed**

The 1 skipped test (`test_no_warnings_when_all_clear`) is an environment-conditional
best-effort check, not a coverage gap. All warning paths are tested individually.

## Migration Status

- Alembic head: `0013`
- Chain: `0001 → 0002 → 0003 → 0004 → 0005 → 0006 → 0007 → 0008 → 0009 → 0010 → 0011 → 0012 → 0013`
- Dev database has been upgraded to 0013.
- Fresh `alembic upgrade head` verified on isolated test PostgreSQL cluster.

## What Is Solid

- Full backend test suite: 734 passed, 1 skipped, 0 failed.
- Auth: JWT, bcrypt, /auth/me DB revalidation, generic error responses.
- Branch scope enforced consistently via service-layer `_check_branch_access()`.
- Action-level permissions for all sensitive operations (`payroll.entry`, `setup.manage`,
  `review.decide`, `payroll.finalize`, `payroll.approve_rate`, `drivers.manage`).
- Payroll period lifecycle: Draft → Open → InReview → Approved → Locked → Archived.
- Open→InReview is atomic (UPDATE WHERE status='Open' RETURNING) + DB unique index guard.
- Concurrent race on PeriodApproval insert catches SAIntegrityError → clean 422.
- Finalization is atomic, blocks double-finalization, immutable ledger.
- Pay rates: effective dating, approval/supersession, tiered structures (OrdinalTier,
  RangeBracket, RangeProgressive, Block).
- DriverPayRules: MinimumPay / MaximumPay with Archived-period protection.
- Period Pay lines: EnteredAmount, tracked separately from daily draft lines.
- Review: M16-wired — Open→InReview auto-creates PeriodApproval; Approved/Rejected/
  EditRequested decisions update the period status.
- Dashboard: read-only aggregation with four setup warnings and per-branch summaries.
- Settings/admin: company profile, branches, payroll setup, status keys, users, roles,
  pay items, custom pay items, pay item requests.
- Draft-line and period-pay mutations have full audit logging with rollback guarantee.
- Admin `_ensure_admin` revalidates user.isactive, canlogin, company.status, issuspended.
- PeriodApproval review items: manual creation blocked; write-back validates entity
  metadata, period existence, company, and branch match.

## What Is Still Open / Deferred

See `OPEN_ISSUES_AND_DEFERRED_DECISIONS.md` for full detail.

Not blocking frontend:
- PayProfiles (exist in schema, not calculation-active)
- GUARANTEED_MINIMUM system item (deferred to after pay-rule engine)
- Activity feed endpoint (/activity)
- Dashboard caching (Redis / TTL)
- Per-driver pay summary drill-down
- Import batch / bulk entry
- Reports / exports
- Hardcoded system rate fallback cleanup (P2 Codex — low risk)
- Stale comments in service files (P3 Codex)
- Period Pay update audit extra coverage (P2 Codex)
- Production hardening: deployment, secrets, observability, backup, load testing
- MFA, refresh tokens, advanced auth controls

## Start Procedure For A New Chat

1. Read all files in `docs\handoff\` first.
2. Confirm working directory is `C:\Projects\etbdnt\Payroll_App_v3`.
3. Run full test suite from `backend\`: `.\.venv\Scripts\python.exe -m pytest`
   Expected: 734 passed, 1 skipped, 0 failed.
4. Do not modify the old PySide project.
5. Confirm next task with the project owner before coding.

## What Must Not Happen

- Do not modify the old PySide project.
- Do not bypass API-first design with direct database access from UI.
- Do not edit old applied migrations. Add new migration files.
- Do not make finalized payroll mutable.
- Do not add new feature behavior without tests for branch scope, permissions,
  audit, and rollback.
- Do not expose ManualAmount as a user-selectable line type.
- System pay item codes must be blocked explicitly in service code (not just DB constraints).
