# Next Steps

Last verified: 2026-05-30 (M18 — Frontend Handoff / Backend Readiness Docs)

## Backend Is Complete and Safe to Start Frontend

All P0/P1 backend blockers have been resolved and verified:
- 734 passed, 1 skipped, 0 failed
- Alembic head: 0013
- Dev database upgraded to 0013
- Codex confirmed all P0/P1 issues closed

## Start Procedure For A New Chat

1. Read all files in `docs\handoff\` first.
2. Confirm working directory is `C:\Projects\etbdnt\Payroll_App_v3`.
3. Run full test suite from `backend\`:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest
   ```
   Expected: 734 passed, 1 skipped, 0 failed.
4. Do not modify the old PySide project.
5. Confirm next task with the project owner before coding.

---

## Recommended Frontend Implementation Order

Start with authentication and foundational navigation before building data-entry screens.
Each phase builds on the previous one.

### Phase 1 — Auth and Shell (start here)
1. **Login screen** — POST /auth/login, store JWT, redirect on success
2. **Auth guard** — wrap all routes, redirect to login if no valid token
3. **App shell** — top nav, sidebar placeholder, logout (clears token, redirects)
4. **GET /auth/me** — verify token on app load, show display name

### Phase 2 — Dashboard and Navigation
5. **Dashboard** — GET /dashboard, show period counts, review counts, active drivers,
   last finalized period, setup warnings, branch summaries
6. **Branch selector** — if AllCompanyBranches scope: show branch picker to filter view.
   If SpecificBranch scope: active branch is locked, no picker needed.
7. **GET /core/branches** — populates branch picker

### Phase 3 — Payroll Periods List
8. **Periods list** — GET /payroll/periods (filter by branch), show status badges,
   link to period detail
9. **Create period** — POST /payroll/periods (requires payroll.entry)
10. **Period status chip** — color-code Draft/Open/InReview/Approved/Locked/Archived

### Phase 4 — Payroll Period Detail and Daily Entry
11. **Period detail** — GET /payroll/periods/{id}/lines, lines summary, period metadata
12. **Add daily line** — POST /payroll/periods/{id}/lines (driver, line type, qty, rate, date)
13. **Edit daily line** — PATCH /payroll/periods/{id}/lines/{line_id}
14. **Void daily line** — DELETE /payroll/periods/{id}/lines/{line_id}
15. **Lines summary** — GET .../lines/summary (per-driver totals)
16. **Submit for review** — PATCH .../status to InReview (requires payroll.entry)

### Phase 5 — Period Pay
17. **Period pay list** — GET /payroll/periods/{id}/period-pay
18. **Add period pay line** — POST .../period-pay (driver, line type, amount)
19. **Edit period pay** — PATCH .../period-pay/{line_id}
20. **Void period pay** — DELETE .../period-pay/{line_id}

### Phase 6 — Review and Approval
21. **Review queue** — GET /review/items (filter by status=Pending, by branch)
22. **Review item detail** — show entity linked, submitter, period name
23. **Decide review item** — POST /review/items/{id}/decide (Approved/Rejected/EditRequested)
24. **Create manual review item** — POST /review/items (for Correction, Dispute, etc.
    — NOT PeriodApproval which is auto-created)

### Phase 7 — Finalization and Ledger
25. **Finalize period** — POST /payroll/periods/{id}/finalize (requires payroll.finalize)
26. **Final lines view** — GET .../final-lines (immutable ledger)
27. **Archive period** — PATCH .../status to Archived

### Phase 8 — Pay Rates
28. **Driver rates list** — GET /payroll/rates (filter by driver)
29. **Create rate** — POST /payroll/rates
30. **Approve rate** — POST /payroll/rates/{id}/approve (requires payroll.approve_rate)
31. **Rate lookup** — GET /payroll/rates/lookup (effective rate for driver + date + type)
32. **Driver pay rules** — GET/POST /payroll/driver-pay-rules (min/max pay rules)

### Phase 9 — Settings
33. **Branch payroll setup** — GET/PUT /settings/branches/{id}/payroll-setup
34. **Status keys** — GET/POST/PATCH/DELETE /settings/branches/{id}/status-keys
35. **Pay items** — GET /settings/branches/{id}/pay-items, PATCH to activate/deactivate
36. **Company profile** — GET/PATCH /settings/company

### Phase 10 — Admin
37. **Users list** — GET /admin/users
38. **Create user** — POST /admin/users
39. **Assign role** — POST /admin/users/{id}/roles
40. **Custom pay items** — GET/POST/PATCH/DELETE /settings/pay-items (admin direct)
41. **Pay item requests** — GET/POST /settings/pay-item-requests, decide flow

---

## Deferred Backend Work (do not start without explicit instruction)

### Do not block frontend on these — can be done in parallel or later:
- PayProfiles activation
- GUARANTEED_MINIMUM system item
- Activity feed endpoint (/activity)
- Dashboard caching (Redis)
- Per-driver pay summary
- Import / bulk entry
- Reports / exports
- Stale comments cleanup (P3)
- Hardcoded system rate fallback removal (P2)
- Period Pay update audit extra test coverage (P2)
- Production hardening (deployment, secrets, CI/CD, observability)
- MFA, refresh tokens

### Must not be forgotten before production:
- Confirm AllowSelfApproval production default (currently TRUE)
- Fix local dev DB credentials in backend\.env
- Production hardening pass

---

## What Must Not Happen

- Do not modify the old PySide project.
- Do not bypass API-first design with direct database access from UI.
- Do not edit old applied migrations. Add new migration files.
- Do not make finalized payroll mutable.
- Do not expose ManualAmount as a user-selectable line type.
- System pay item codes must be blocked explicitly in service code.
- Do not add new backend feature behavior without tests for branch scope,
  permissions, audit, and rollback.
