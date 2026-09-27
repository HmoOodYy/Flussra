# Payroll Setup Phase 6 — Frontend Product Cutover Execution Plan

## 1. Status

- Status: IMPLEMENTATION COMPLETE (U0–U7 PASS, incl. U7 authorization fix round, plus product-correction pass C1–C3/A/B PASS — see entry 25) — HUMAN FULL VALIDATION PENDING (manual validation restarts after the correction pass); not merged or closed
- Branch: feature/payroll-setup-frontend-cutover
- Execution baseline SHA: d388aee43f33d58a79f3d60c83617565bba78f02 (docs: add compensation modernization master plan (#14)). Discovery was performed at cd1a0596d6ccfb165c588899e423a989959f80d5; the only change between them is the Compensation Modernization doc, so discovery remains valid.
- Parent plan: docs/architecture/PAYROLL_SETUP_ARCHITECTURE_IMPLEMENTATION_MASTER_PLAN.md (Phase 6). This document does not replace or reopen it.
- Owner model: Opus lead (architect, decision owner, reviewer of every diff) / Sonnet worker (scoped decision-complete units only) / Human (full suites, commits, pushes, PRs, merges).

## 2. Purpose

Phase 6 moves the UI from the obsolete branch-owned mutable Payroll Setup to:

```
Company
  -> reusable Payroll Setups
      -> Draft / Published immutable Versions
Branch
  -> effective-dated Assignment
Payroll Period
  -> backend-resolved exact Assignment + Version
```

No frontend authority duplication: the frontend never resolves Setup/Version, never computes cadence, never uses wall-clock today.

Goals of this document:

- Freeze discovery decisions so implementation does not re-litigate them.
- Freeze unit boundaries so each unit is independently scoped and reviewable.
- Freeze permission contracts so UI gating matches backend authority exactly.
- Record review corrections that override the original discovery report where they differ.
- Enable resume across sessions without re-discovery.
- Track status per unit through to exit.

## 3. Non-goals

Phase 6 does NOT:

- Rewrite the payroll resolver.
- Rewrite calculation/finalization/reporting/P6D.
- Redesign Current Payroll Hub.
- Replace candidate-driven Create Period.
- Implement Compensation Modernization.
- Touch the separate backend test-hygiene task/worktree.
- Add migrations unless a concrete approved blocker requires one.
- Add a setup->assignments backend endpoint (see Correction 6).
- Add an archive-eligibility preview endpoint.

## 4. Discovery summary (confirmed in code)

- frontend/src/pages/settings/payroll/PayrollSetupPage.tsx calls legacy GET/PUT /settings/branches/{id}/payroll-setup (lines ~370, ~433); backend backend/app/settings/router.py (~264-318) returns 410 LEGACY_PAYROLL_SETUP_ROUTE_DISABLED for both.
- Legacy page also computes schedule in TypeScript (computeUpcomingPeriods, first_custom_end_date -> interval) and enforces a client-only MAX_DAYS_OFF = 2.
- frontend/src/types/settings.ts still has legacy PayrollSetup / PayrollSetupUpsert; BranchAdmin type lacks schedule_readiness_reason.
- Route /settings/payroll is gated by canManageSettingsAdmin (company setup.manage) in App.tsx; AppShell Settings nav and DashboardPage quick action likewise use setup.manage-based gates.
- No frontend helpers for payroll_setup.view/manage/publish/assign; permissionLabels.ts lacks the payroll_setup module and publish/assign actions.
- RolesPage.tsx renders only codes listed in BUSINESS_GROUPS, so payroll_setup.* are invisible (preserved on save but not grantable). PeoplePage.tsx MODULE_ORDER/MODULE_LABELS omit payroll_setup, so per-user overrides hide them too. Company Owner implicitly has all permissions; other roles need explicit grants (catalogue seeded by migration 0068).
- Branch-scoped payroll.view users cannot reach any schedule/history view (Settings outer gate requires company admin or roles./users. permissions).
- Status Keys (tab inside legacy page; /settings/branches/{id}/status-keys CRUD; writes require company-wide setup.manage) are live and must be preserved.
- Current Payroll (PeriodsListPage) uses GET /payroll/current (getCurrentPayrollHub).
- CreatePeriodModal is backend-driven: GET /payroll/current-workflow capabilities -> GET /payroll/branches/{id}/period-candidates -> POST /payroll/branches/{id}/period-creations {candidate_key}. Compliant; preserve.
- GET /payroll-setup/branches/{id}/effective requires explicit period_start_date (Query(...)).
- CompanyBranchesPage has no first_payroll_start_date input; its "Setup Needed" badge deep-links to the legacy /settings/payroll?branchId=..&tab=pay-schedule.
- Backend canonical API exists under /payroll-setup (backend/app/payroll_setup/router.py). PolicyError envelope {detail:{code,message}}; *_NOT_FOUND -> 404, INVALID_* -> 422, else 409.

Backend surface:

| Endpoint(s) | Backend permission |
|---|---|
| GET /setups, GET /setups/{id}, GET /setups/{id}/versions, GET /setups/{id}/drafts, GET /default, GET /branches/{b}/assignments | company-wide + non-driver + payroll_setup.view |
| POST /setups/{id}/drafts/{d}/publication-impact, POST /branches/{b}/reassignment-impact | payroll_setup.view |
| POST /setups, PUT /setups/{id}, POST /setups/{id}/archive, POST/PUT/DELETE drafts | payroll_setup.manage |
| POST /setups/{id}/drafts/{d}/publish | payroll_setup.publish |
| PUT /default (setup_id may be null), POST /branches/{b}/assignments, POST /branches/{b}/reassignments, POST /assignments/{a}/withdraw | payroll_setup.assign |
| GET /branches/{b}/history, GET /branches/{b}/effective?period_start_date= | branch access + non-driver + payroll.view on that branch |

Also: BranchAdmin.schedule_readiness_reason is populated only for non-driver callers with payroll.view on that branch; NO_COMPANY_DEFAULT is only ever emitted in the POST /settings/branches create response.

## 5. Locked UX / authority decisions

A. Company Payroll Setups admin: /settings/payroll becomes the company-owned Payroll Setups page (Setups tab with ?setupId= master/detail; Branch Assignments tab). Never presents Setup as branch-owned mutable config. Legacy PayrollSetupPage.tsx + .module.css are deleted.

B. Branch read-only Payroll Schedule: /payroll/schedule?branchId=... in the Payroll nav group, outside Settings. Access: non-driver + exact branch + payroll.view. Branch picker lists branches from GET /settings/branches filtered by the branch helper. No company-wide Setup administration on this page; no mutations; no date input.

C. Status Keys: separate branch-owned domain moved verbatim to /settings/status-keys (same endpoints, same canManageSettingsAdmin gate, same filter/create/edit/deactivate/reactivate behavior). Legacy URL /settings/payroll?tab=status-keys[&branchId=] redirects to /settings/status-keys[?branchId=].

D. Effective authority: never derive current/effective Setup from wall-clock today, browser timezone, or frontend cadence math. Anchor = backend BranchAdmin.schedule_readiness_date (U0). Payroll Schedule page flow: GET /settings/branches/{b} -> reason+date; if reason === READY and date non-null -> GET /effective?period_start_date={date}, shown as "Next payroll period: {start}-{end}" with Setup, Version #, schedule, hash, and next_boundary_date/next_boundary_kind (Assignment | Version | AssignmentAndVersion) as the scheduled change; otherwise show the reason and do not call /effective. GET /history renders the full timeline: governing row identified by assignment_id/version_id match with /effective (not date comparison); rows with effective_from_date > schedule_readiness_date tagged "Scheduled"; withdrawn assignments muted; if no anchor, plain chronological list with no tags.

E. Current Payroll / Create Period: Hub already canonical; CreatePeriodModal stays candidate-driven, no changes. Only addition: on PeriodsListPage (~line 507, "Setup: {setup_status}"), when setup_status !== 'complete' and canViewBranchPayrollSchedule(user, branchId), add a "View payroll schedule" link to /payroll/schedule?branchId=.

F. Company Setup UX (for U5a-c):

- Setup list: code, name, Active/Archived, Default badge (GET /default).
- Create (manage): code (<=50, ^[A-Za-z0-9][A-Za-z0-9_.-]*$), name (<=200), description. Edit (manage): name + description; code read-only.
- Mark as Company default (assign; Active, non-default). Confirm copy: default affects only Branches created later; existing assignments never change.
- Clear Company Default (assign; shown when a default exists). Sends PUT /payroll-setup/default {"setup_id": null}. Confirm copy exactly: "New Branches will no longer have a default Payroll Setup available for automatic onboarding until another default is selected. Existing Branch assignments are unchanged."
- Versions timeline (view): chronological rows: v#, effective from, next change (effective_to_date or "-"), schedule summary, hash first 12 chars, "Replaced by ..." when !is_terminal. No current/future tagging (would need today).
- Drafts: list (view); New Draft (manage) editor with optional prefill from any published version (client copy of backend data) -> POST with all fields; Edit -> PUT with all fields; Discard -> confirm -> DELETE. Fields: frequency (Week/Biweek/Month/Custom), anchor date, custom interval (required only for Custom, null otherwise), 7 day-off toggles (mask bit 0 = Sunday ... bit 6 = Saturday, matching backend (weekday()+1)%7). No upcoming-period preview, no max days-off rule.
- Publish: enter effective_from_date -> Preview impact (POST publication-impact, view) -> render affected branch names (from GET /settings/branches), predecessor version/hash, successor schedule/hash, next_version_boundary, conflicts (branch, code, reason), allowed. If current_same_date_version_id != null, offer checkbox "Publish as a correction of v#" which re-previews with replaces_version_id. Publish (publish perm) enabled only when latest preview allowed === true and inputs unchanged since preview (any change clears preview). Confirm lists affected branches and states published versions are immutable. On 409 show code/message and re-run preview.
- Assigned Branches (view, read-only): N+1 composition: GET /settings/branches then GET /payroll-setup/branches/{b}/assignments per branch, grouped by setup_id, non-withdrawn only.
- Archive (manage, Active only): disabled with "Select another default or clear the default first" only when the Setup is the current default (direct fact). Otherwise confirm dialog lists this Setup's non-withdrawn assignments informationally and states the server decides. Render backend PolicyError (SETUP_ASSIGNED / DEFAULT_SETUP_IN_USE) message+code.
- Branch Assignments tab: per-branch rows with assignment timeline (withdrawn muted) and readiness reason where visible (else "-"). Assign (assign): Active setup + date + reason -> POST assignments. Reassign (assign): destination + date + reason -> Preview (reassignment-impact) showing source/destination, predecessor/successor versions, conflicts, allowed -> confirm only when allowed -> POST reassignments. Withdraw (assign): every non-withdrawn row, confirm with reason -> POST withdraw; server decides eligibility.
- Errors: show backend message plus code chip; no invented remapping of policy error codes. Hide mutation controls for Archived setups.

G. Readiness reason copy (single pure map; unknown codes shown raw, never hidden):

| Code | Label | Description |
|---|---|---|
| READY | Ready | Schedule resolves for the next payroll period. |
| NO_COMPANY_DEFAULT | No company default | No default Setup, so nothing was assigned. |
| NO_ASSIGNMENT | Not assigned | No assignment covers the next payroll period. |
| NO_PUBLISHED_VERSION | No published version | The assigned Setup has no published Version for that date. |
| AUTHORITY_BOUNDARY_CONFLICT | Boundary conflict | The next period would cross a scheduled Setup/Version change. |
| SETUP_NOT_ACTIVE | Setup not active | The assigned Setup is archived. |
| INVALID_SCHEDULE_BOUNDARY | Invalid period start | The next start is not a period boundary under the schedule. |
| BRANCH_NOT_OPERATIONAL | Branch not operational | The Branch or Company is not active. |

H. Navigation / routing (locked):

- Outer /settings gate: canViewSettings || canViewDailyPayItems || canViewPayrollSetups || canCreateBranches.
- SettingsDefaultRedirect order: canManageSettingsAdmin || canCreateBranches -> /settings/company-branches; else canViewSettings -> /settings/roles; else canViewPayrollSetups -> /settings/payroll; else /settings/pay-items.
- Settings nav items, each gated individually, order: Payroll Setups (canViewPayrollSetups); Status Keys (canManageSettingsAdmin); Daily Pay Items (canViewSettings || canViewDailyPayItems); Roles & Permissions (canViewSettings, unchanged); Company & Branches (canAccessCompanyBranches — aligned to its route gate). Group rendered only if non-empty.
- Route gates: /settings/payroll -> canViewPayrollSetups; /settings/status-keys -> canManageSettingsAdmin; /settings/company-branches -> canAccessCompanyBranches; /payroll/schedule -> canViewAnyBranchPayrollSchedule.
- AppShell titles/icons: "Payroll Setups", "Status Keys", "Payroll Schedule" (match /payroll/schedule before generic /payroll).
- DashboardPage quick action: gated canViewPayrollSetups, label "Payroll Setups", description "Company payroll schedules and branch assignments".
- Unit ownership of 5H: U4 adds the /settings/status-keys route and the Status Keys nav item/title/icon. U5a adds the /settings/payroll gate (canViewPayrollSetups), the canViewPayrollSetups term in the outer /settings gate, the canViewPayrollSetups step in SettingsDefaultRedirect, the per-item Settings nav for Payroll Setups / Daily Pay Items / Roles & Permissions, the Payroll Setups title, and the Dashboard quick action; U5a leaves the Company & Branches nav item on its current canViewSettings condition. U6 adds the /payroll/schedule route, Payroll nav item, and title/icon. U7 switches the /settings/company-branches route gate and the Company & Branches nav item to canAccessCompanyBranches, and adds the canCreateBranches terms to the outer /settings gate and SettingsDefaultRedirect.

## 6. Permission model

Helpers to add in frontend/src/lib/permissions.ts (U1):

```ts
function _hasCompanyPolicyPermission(u, code) { return !isDriverUser(u) && _hasCompanyPermission(u, code); }
canViewPayrollSetups(u)    // payroll_setup.view
canManagePayrollSetups(u)  // payroll_setup.manage
canPublishPayrollSetups(u) // payroll_setup.publish
canAssignPayrollSetups(u)  // payroll_setup.assign
canViewBranchPayrollSchedule(u, branchId) // !isDriverUser(u) && hasAuthorityPermission(u.authority, 'payroll.view', branchId)
canViewAnyBranchPayrollSchedule(u)        // !isDriverUser(u) && hasAuthorityPermissionAnywhere(u.authority, 'payroll.view')
canCreateBranches(u)       // !isDriverUser(u) && _hasCompanyPermission(u, 'branches.create')
canAccessCompanyBranches(u) // canManageSettingsAdmin(u) || canCreateBranches(u)
```

Rules: payroll_setup.* helpers require company-wide authority (company_permissions only; branch-scoped grants never satisfy) and deny Driver / OwnDriverDataOnly / mixed Driver+admin users (backend _require_not_driver_role blocks any driver assignment). Branch schedule uses exactly payroll.view (not the broader PAYROLL_READ list); payroll.entry alone does not grant it; company Setup permissions are not required. Mutation controls use their specific permission AND the page view gate — never one broad setup.manage gate. Backend remains authoritative; helpers drive visibility only.

## 7. Review corrections (override the original discovery report where they differ)

- CORRECTION 1 — U0 accepted: backend exposes the readiness evaluation date; frontend consumes, never recreates it.
- CORRECTION 2 — R3 pulled into Phase 6 (U7). Verified backend contracts (backend/app/settings/service.py): GET /settings/company = any company member with branch access; GET /settings/branches and GET /settings/branches/{id} = branch-scoped list/read (readiness reason/date only with non-driver + payroll.view on that branch); PATCH /settings/company, PATCH /settings/branches/{id}, POST /settings/branches/{id}/set-default = _ensure_company_admin (company-wide + setup.manage); POST /settings/branches = _ensure_branch_creator (company-wide + non-driver + branches.create, plus payroll_setup.assign only when first_payroll_start_date is supplied). Final frontend design: route gate canAccessCompanyBranches; "Add Branch" button and create modal gated by canCreateBranches (NOT canManageSettingsAdmin); first_payroll_start_date input shown only when canCreateBranches && canAssignPayrollSetups (field omitted from payload when blank or not shown); company edit, branch edit (row action menu), set-default branch, and the "View only" banner keep canManageSettingsAdmin exactly as today; is_default checkbox stays part of the create form (backend create path allows it under branches.create). Entering the page never grants edit powers. No permissions are broadened.
- CORRECTION 3 — Clear Company Default. Verified: DefaultSetupRequest.setup_id: int | None (schemas.py); router put_default passes it through; policy.set_default_setup accepts None, skips setup validation, updates core.Companies.DefaultPayrollSetupID to NULL and writes a DefaultChanged audit; backend/tests/test_payroll_setup_phase4_api.py already asserts PUT {"setup_id": null} -> 204 and GET /default -> {"setup": null}. Therefore U5a MUST expose "Clear Company Default" (payroll_setup.assign) with the copy in section 5F. Archive of the current default is unblocked by clearing, not only by selecting another default.
- CORRECTION 4 — client-only MAX_DAYS_OFF = 2 is dropped; backend Schedule only enforces mask 0..127. Any future maximum must be a backend invariant with tests.
- CORRECTION 5 — archive eligibility stays server-authoritative; no TypeScript reconstruction; UI may show facts, disable only when the Setup is the current default, send the request, and render the PolicyError.
- CORRECTION 6 — Assigned Branches via N+1 composition is acceptable at current scale; a setup->assignments endpoint is a future optimization, not a Phase 6 blocker.

## 8. Implementation units

### U0 — Backend readiness evaluation date

- Status: PASS
- Purpose: expose the exact date the backend used to evaluate schedule readiness, so the frontend never recreates it.
- Files: backend/app/payroll_setup/readiness.py (add branch_schedule_readiness_detail(company_id, branch_id, db, *, period_start_date=None) -> tuple[bool, str, date | None]; existing branch_schedule_readiness keeps signature and tuple[bool, str] return as a thin wrapper, no duplicated algorithm); backend/app/settings/schemas.py (BranchAdmin.schedule_readiness_date: date | None = None); backend/app/settings/service.py (date exposed from the same evaluation and under the identical gate as the reason; create_branch with explicit first_payroll_start_date returns that exact date, including the NO_COMPANY_DEFAULT result); new backend/tests/test_payroll_setup_phase6_readiness_date.py.
- Dependencies: none.
- Behavior — date semantics: day after latest non-cancelled period EndDate; else first non-withdrawn assignment EffectiveFromDate; else None; BRANCH_NOT_OPERATIONAL before a date is established -> None.
- Security/authority constraints: date is exposed under the identical gate as the reason (non-driver + payroll.view on that branch); no new authority surface.
- Focused tests: A) no period -> first assignment start; B) non-cancelled period -> EndDate+1; C) cancelled period does not advance; D) Driver/ODA -> reason and date hidden; E) no payroll.view on branch -> both hidden; F) create_branch + explicit date -> exact date; G) create_branch + explicit date + no default -> NO_COMPANY_DEFAULT + requested date.
- Non-goals: /effective, resolver, schedule arithmetic, frontend, permissions, migrations, Current Payroll, Create Period.
- Completion criteria: readiness.py wrapper unchanged in signature/return; new detail function backs both reason and date; schema field added; service exposes date under identical gate; tests A-G pass.
- Result (PASS, Opus-reviewed): changed backend/app/payroll_setup/readiness.py, backend/app/settings/schemas.py, backend/app/settings/service.py; added backend/tests/test_payroll_setup_phase6_readiness_date.py (10 tests: A-G, plus mixed Driver+company-admin fail-closed with positive control, mapped resolver failure returns evaluated date, NO_ASSIGNMENT -> None, BRANCH_NOT_OPERATIONAL -> None). Mapped resolver failures return the evaluated date alongside the reason; the frontend must still call /effective only when reason === READY. Observed: GET /settings/branches/{id} returns 200 to Driver/ODA callers with reason and date both null (not 403).

### U1 — Frontend Payroll Setup permission helpers

- Status: PASS
- Purpose: give the frontend a single, correct set of permission predicates matching backend authority exactly.
- Files: frontend/src/lib/permissions.ts (section 6 helpers incl. canCreateBranches, canAccessCompanyBranches); frontend/src/lib/permissionLabels.ts (module payroll_setup -> "Payroll Setup"; actions publish -> "Publish", assign -> "Assign"); new test frontend/tests/payrollSetupAuthority.test.ts.
- Dependencies: none.
- Behavior: implement helpers exactly as specified in section 6.
- Security/authority constraints: payroll_setup.* helpers must require company-wide authority only (never branch-scoped grants) and must deny Driver / OwnDriverDataOnly / mixed Driver+admin users; canViewBranchPayrollSchedule must use exactly payroll.view, not the broader PAYROLL_READ list.
- Focused tests (payrollSetupAuthority.test.ts): company grant allows each helper; branch-only grant denies all four payroll_setup helpers and canCreateBranches; DRIVER, OwnDriverDataOnly, mixed Driver+company-admin denied for all; canViewBranchPayrollSchedule allows branch A, denies branch B; payroll.entry alone denies schedule; flat active_permissions alone grants nothing; canAccessCompanyBranches true for setup.manage-only and for branches.create-only.
- Non-goals: changing existing helpers.
- Completion criteria: all helpers present with exact semantics; permissionLabels updated; test file passes.
- Result (PASS, Opus-reviewed): changed frontend/src/lib/permissions.ts (additive section; no existing helper changed; helpers not yet wired into any route/page), frontend/src/lib/permissionLabels.ts; added frontend/tests/payrollSetupAuthority.test.ts (28 tests covering A-M incl. positive controls). Opus review applied one comment-only correction (canManagePayrollSetups scope description). Note: canAccessCompanyBranches inherits canManageSettingsAdmin's existing lack of a driver check (unchanged pre-Phase-6 behavior, mirrors backend _ensure_company_admin, which also has no driver check).

### U2 — Roles / People permission visibility and grant UI

- Status: PASS
- Purpose: make the four payroll_setup permissions visible and grantable in Roles and People, where they are currently silently preserved but invisible.
- Files: frontend/src/pages/settings/roles/RolesPage.tsx (new BUSINESS_GROUPS entry id 'payroll-setup', label 'Payroll Setup Policy', the four codes, placed after 'Payroll Configuration'; optional `note` field on BusinessGroup rendered as a muted line "Effective only on All-Branches role assignments."; PERM_DEPS manage/publish/assign -> view; risk tier publish = critical, assign = approval); frontend/src/pages/people/PeoplePage.tsx (MODULE_LABELS payroll_setup: 'Payroll Setup Policy'; same PERM_DEPS).
- Dependencies: U1.
- Behavior: payroll_setup.* codes render in both pages' permission matrices with the dependency chain manage/publish/assign -> view.
- Security/authority constraints: visibility change only; no change to what a grant actually authorizes server-side.
- Focused tests: tsc + eslint on touched files.
- Non-goals: backend role seeding.
- Completion criteria: codes visible and grantable in both pages; PERM_DEPS enforced in UI; touched-file tsc/eslint clean.
- Result (PASS, Opus-reviewed): to make the grant logic testable under `node --test` (which cannot import .tsx), the pure permission-grant data/logic was moved verbatim out of the pages into new frontend/src/pages/settings/roles/rolePermissionModel.ts (BUSINESS_GROUP_DEFS without icons, HIDDEN_PERM_CODES, getRiskTier, PERM_DEPS/PERM_DEPENDENTS, permsReducer, groupPermissionsByDomain) and frontend/src/pages/people/peoplePermissionModel.ts (MODULE_LABELS/ORDER, MODULE_NOTES, PERM_DEPS/PERM_DEPENDENTS, groupPerms, applyToggle); RolesPage keeps icons in GROUP_ICONS keyed by group id. U2 additions: Roles group 'payroll-setup' "Payroll Setup Policy" after 'payroll-config' with note "Effective only on All-Branches role assignments."; People module payroll_setup "Payroll Setup Policy" after payroll with note "Effective only for members with an All-Branches role assignment." (per-user overrides only become company authority for members with an All-Branches assignment); manage/publish/assign -> view as a commented UI-only dependency in both; risk tiers publish=critical, assign=approval. setup.manage stays in Payroll Configuration. Test: new frontend/tests/permissionGrantUi.test.ts (20 tests: grouping/placement, dependency on/off, unknown/hidden code preservation on save, pre-U2 groups/order/deps snapshot, risk tiers, and "visible/grantable is not effective" authority checks with positive controls). Modified: RolesPage.tsx, PeoplePage.tsx. U1 files unchanged.

### U3 — Canonical TypeScript types + API + pure helpers

- Status: PASS
- Purpose: establish the canonical typed client surface for /payroll-setup, with pure, testable formatting/error helpers.
- Files: new frontend/src/types/payrollSetup.ts (1:1 with backend/app/payroll_setup/schemas.py); new frontend/src/lib/payrollSetupApi.ts (typed wrappers for all /payroll-setup endpoints, cdpiApi.ts precedent); new frontend/src/lib/payrollSetupErrors.ts (pure readApiError(err, fallback) -> {status, code, message}, handles string detail, {code,message} detail, 422 detail arrays); new frontend/src/lib/payrollSetupReadiness.ts (pure reason map, mask/frequency/schedule-summary formatters); frontend/src/types/settings.ts adds schedule_readiness_reason: string | null and schedule_readiness_date: string | null to BranchAdmin — legacy PayrollSetup/PayrollSetupUpsert NOT removed here (U5a removes them).
- Dependencies: none.
- Behavior: pure modules must not import apiClient.
- Security/authority constraints: none beyond faithfully mirroring backend contracts (no client-side reconstruction of authority or eligibility).
- Focused tests: frontend/tests/payrollSetupErrors.test.ts, frontend/tests/payrollSetupReadiness.test.ts (every reason code, unknown code, mask round-trip Sun/Sat, three error shapes).
- Non-goals: none beyond scope above.
- Completion criteria: types mirror backend schemas; API wrappers cover all /payroll-setup endpoints; pure modules import-clean; both test files pass.
- Result (PASS, Opus-reviewed): created frontend/src/types/payrollSetup.ts (all request/response models incl. nested ConflictResponse, VersionSegmentResponse, AssignmentHistoryResponse; required-but-nullable keys DraftUpdateRequest.custom_interval_days and DefaultSetupRequest.setup_id kept required; PayrollFrequency union on requests only), frontend/src/lib/payrollSetupApi.ts, frontend/src/lib/payrollSetupErrors.ts, frontend/src/lib/payrollSetupReadiness.ts, frontend/tests/payrollSetupErrors.test.ts, frontend/tests/payrollSetupReadiness.test.ts; modified frontend/src/types/settings.ts (BranchAdmin schedule_readiness_reason/date only; legacy PayrollSetup/PayrollSetupUpsert untouched). New modules are not yet imported by any page.
- Canonical endpoints wrapped (21, verified verb+path against backend/app/payroll_setup/router.py; the discovery count of 19 was low): GET/POST /setups; GET/PUT /setups/{id}; POST /setups/{id}/archive; GET/POST /setups/{id}/drafts; PUT/DELETE /setups/{id}/drafts/{d}; GET /setups/{id}/versions; POST /setups/{id}/drafts/{d}/publication-impact; POST /setups/{id}/drafts/{d}/publish; GET/PUT /default (set sends {setup_id}, clear sends {setup_id: null}); GET/POST /branches/{b}/assignments; POST /branches/{b}/reassignments; POST /branches/{b}/reassignment-impact; POST /assignments/{a}/withdraw; GET /branches/{b}/history; GET /branches/{b}/effective?period_start_date= (required argument, no today fallback).
- API wrappers are not unit-tested (apiClient depends on Vite import.meta.env; no mocking framework added per U3 scope); Opus reviewed them statically against router.py. Runtime integration is verified in U5a-U7.
- Mask convention verified in backend/app/payroll/period_creation.py: bit 0 = Sunday ... bit 6 = Saturday. No max-days rule. Review fixes applied: describeReadiness uses an own-property lookup (inherited keys like 'constructor' stay unknown/visible); toggleDay throws RangeError outside 0..6.

### U4 — Extract Status Keys

- Status: PASS
- Purpose: separate the Status Keys domain out of the legacy page verbatim, ahead of deleting that page in U5a.
- Files: new frontend/src/pages/settings/status-keys/StatusKeysPage.tsx + StatusKeysPage.module.css (only styles used); App.tsx route; AppShell.tsx nav/title/icon.
- Dependencies: none (must precede U5a).
- Behavior: verbatim behavior — same endpoints, same canManageSettingsAdmin gate, same filter/create/edit/deactivate/reactivate behavior. The legacy PayrollSetupPage (including its Status Keys tab) is left untouched until U5a; during the interim both surfaces work. The /settings/payroll?tab=status-keys redirect is implemented in U5a.
- Security/authority constraints: gate unchanged (canManageSettingsAdmin).
- Focused tests: touched-file eslint; manual smoke of CRUD flows.
- Non-goals: behavior changes, deleting legacy page (deleted in U5a).
- Completion criteria: new page live at /settings/status-keys with identical behavior.
- Result (PASS, Opus-reviewed): created frontend/src/pages/settings/status-keys/StatusKeysPage.tsx, StatusKeysPage.module.css, frontend/tests/statusKeysRoute.test.ts; modified frontend/src/App.tsx (route `status-keys` gated by canManageSettingsAdmin, directly after `payroll`) and frontend/src/components/AppShell.tsx (Status Keys nav item after Payroll Setup, only when canManageSettingsAdmin(user); title/icon; new KeyIcon). Legacy PayrollSetupPage.tsx/.module.css untouched — its Status Keys tab and ?tab=status-keys handling remain temporarily (both surfaces work until U5a); no redirect added.
- Behavior equivalence (Opus, mechanical + line review): every line of the new page exists verbatim in the legacy page except the declared differences — page header text ("Status Keys" / "Configure branch payroll entry status keys."), branch <select> calls setSelectedBranchId directly (legacy unsaved-schedule guard dropped), ConfirmKind reduced to save-key / deactivate-key / reactivate-key (titles, messages, labels, danger variant unchanged; loading drops the schedule `saving` flag; onCancel drops pendingBranchId), and the branch-card right side (legacy schedule status chip, which always showed "Not Configured" since the legacy GET returns 410) removed. Key CRUD handlers, validation messages, payloads, endpoints, sorting, toasts, KeyFormFields/StatusKeyDetail are byte-identical to legacy. CSS: every line copied verbatim from PayrollSetupPage.module.css (only a new header comment); all referenced classes defined; the only unreferenced classes (btnRowEdit, select, textarea) ride along in combined selectors.
- Preserved pre-existing quirk (not fixed, recorded): the "Inactive" filter sends include_inactive=true, so it lists active and inactive keys together.
- Authority note: the gate is unchanged canManageSettingsAdmin (company setup.manage, no driver check), matching backend status-key writes (_ensure_company_admin); pure Driver/ODA users without company setup.manage are denied; no payroll_setup.* dependency.

### U5a — Company Payroll Setups page foundation

- Status: PASS
- Purpose: replace the legacy branch-owned mutable Payroll Setup page with the company-owned Payroll Setups foundation (list/detail, metadata, default, versions, assigned branches read, archive).
- Files: delete PayrollSetupPage.tsx/.module.css; new frontend/src/pages/settings/payroll/PayrollSetupsPage.tsx + .module.css; App.tsx (gates, outer gate, redirect, legacy tab=status-keys redirect); AppShell.tsx (per-item Settings nav per 5H, U5a-owned parts only); DashboardPage.tsx quick action; remove PayrollSetup/PayrollSetupUpsert from types/settings.ts.
- Dependencies: U1, U3, U4.
- Behavior: Setups tab list/detail, create/edit metadata, mark default, Clear Company Default, versions timeline, assigned branches (read), archive — per section 5F.
- Security/authority constraints: view content gated by canViewPayrollSetups; mutation controls gated by their specific permission (manage/assign) AND the page view gate — never one broad setup.manage gate.
- Focused tests: new frontend/tests/payrollSetupLegacyGuard.test.ts using fs to scan frontend/src and assert absence of: a /settings/branches/.../payroll-setup path, PayrollSetupUpsert, first_custom_end_date, computeUpcomingPeriods, tab=pay-schedule, MAX_DAYS_OFF.
- Non-goals: publish flow (U5b), branch assignment flow (U5c).
- Completion criteria: legacy page deleted; new page live at /settings/payroll with list/detail, default management, versions timeline, assigned branches (read), archive; legacyGuard test passes; legacy /settings/payroll?tab=status-keys[&branchId=] redirects to /settings/status-keys[?branchId=].
- Result (PASS, Opus-reviewed after one fix round):
  - Deleted: frontend/src/pages/settings/payroll/PayrollSetupPage.tsx and PayrollSetupPage.module.css (legacy GET/PUT /settings/branches/{id}/payroll-setup calls, computeUpcomingPeriods, first_custom_end_date, MAX_DAYS_OFF all gone with them).
  - New: frontend/src/pages/settings/payroll/PayrollSetupsPage.tsx + PayrollSetupsPage.module.css (classes 1:1 with usage, no globals), frontend/src/pages/settings/payroll/payrollSetupsView.ts (pure: legacyPayrollSettingsRedirect, validateSetupCreate/Update mirroring backend limits only, normalizeDescription, archiveBlockedReason, shortHash, versionRelationship, groupAssignedBranches), frontend/tests/payrollSetupLegacyGuard.test.ts, frontend/tests/payrollSetupsPage.test.ts.
  - Removed types: PayrollSetup and PayrollSetupUpsert from frontend/src/types/settings.ts after grep showed no remaining consumer; the types/payrollSetup.ts header comment no longer names them.
  - Routes/nav: /settings/payroll -> <LegacyStatusKeysRedirect><Gate check={canViewPayrollSetups}><PayrollSetupsPage/></Gate></LegacyStatusKeysRedirect> (redirect runs outside the gate; /settings/status-keys keeps its own canManageSettingsAdmin gate); outer /settings gate adds canViewPayrollSetups; SettingsDefaultRedirect adds canViewPayrollSetups -> /settings/payroll after canViewSettings; AppShell Settings items gated individually (Payroll Setups: canViewPayrollSetups; Status Keys: canManageSettingsAdmin; Daily Pay Items: canViewSettings || canViewDailyPayItems; Roles & Permissions and Company & Branches: canViewSettings, unchanged until U7); title "Payroll Setups"; Dashboard quick action "Payroll Setups" gated by canViewPayrollSetups.
  - Default: company default card; Mark as Company Default (assign, Active non-default) and Clear Company Default (assign, sends {setup_id: null}) with the locked confirmation copy.
  - Versions: read-only chronological table (v#, effective from, next change, scheduleSummary, 12-char hash with full hash in title, replaces/replaced-by relationship); no current/future labels, no Date usage.
  - Assigned Branches: N+1 composition (GET /settings/branches + per-branch GET assignments), non-withdrawn rows for the selected Setup; any failure is shown, never an empty list. Branch Assignments tab: read-only overview (all assignments incl. withdrawn muted, readiness label where visible); no mutation controls.
  - Archive: manage + Active only; disabled only when the Setup is the current default; otherwise confirm and render backend PolicyError code+message. Metadata edit/default/archive controls hidden for Archived Setups (backend requires Active).
  - Review fix: action errors are now scoped to the Setup they belong to (stale archive/set-default errors no longer render under a different selected Setup).
  - Additional touches required by the legacy guard: CompanyBranchesPage "Setup Needed" badge now navigates to /settings/payroll (one line; U7 reworks the badge), and tests/statusKeysRoute.test.ts updated only where it read the deleted legacy page.

### U5b — Draft editing + publication impact/publish

- Status: PASS
- Purpose: add draft version editing and the publish workflow with mandatory impact preview.
- Files: PayrollSetupsPage.tsx (+ css); new pure frontend/src/pages/settings/payroll/publishPreview.ts (isPreviewCurrent(inputs, preview)).
- Dependencies: U5a.
- Behavior: Drafts list/create/edit/discard per 5F; Publish flow (enter date -> preview -> optional correction checkbox -> publish only when preview allowed and inputs unchanged) per 5F.
- Security/authority constraints: draft CRUD gated by payroll_setup.manage; publish gated by payroll_setup.publish; publish button disabled whenever inputs have changed since the last preview.
- Focused tests: frontend/tests/publishPreview.test.ts.
- Non-goals: branch assignment/reassignment/withdraw (U5c).
- Completion criteria: draft editor and publish flow implemented per 5F; publishPreview.test.ts passes.
- Result (PASS, Opus-reviewed after one fix round):
  - Files: modified frontend/src/pages/settings/payroll/PayrollSetupsPage.tsx + PayrollSetupsPage.module.css (classes 1:1 with usage); created frontend/src/pages/settings/payroll/draftEditor.ts and publishPreview.ts (pure), frontend/tests/draftEditor.test.ts, frontend/tests/publishPreview.test.ts; extended frontend/tests/payrollSetupsPage.test.ts (U5a scope test narrowed to forbid only U5c wrappers).
  - Backend contract re-verified: GET drafts (view); POST draft (manage, Active Setup); PUT/DELETE draft (manage; DRAFT_NOT_EDITABLE for non-drafts); POST publication-impact (view, Active Setup; incomplete draft -> INVALID_SCHEDULE); POST publish (publish, Active Setup; INVALID_EFFECTIVE_DATE, SUCCESSOR_BOUNDARY_INVALID, REPLACEMENT_REQUIRED, REPLACEMENT_NOT_TERMINAL, CONCURRENT_ASSIGNMENT_CHANGE); publishing promotes the Draft row itself to the Published Version (same id).
  - Draft workflow: Drafts section above Published Versions (Draft #id, schedule summary or "Incomplete schedule", created_at_utc as-is; never labelled current). New Draft / Edit / Discard (canManagePayrollSetups) with a shared editor: frequency Week/Biweek/Month/Custom, anchor date, interval only for Custom (non-Custom sends null), seven day-off toggles via the U3 mask helpers with no maximum. Create may prefill from a Published Version (client copy of the 4 schedule fields only; notice "Saving creates a new Draft; the published Version is not changed."). Discard confirmation states only the Draft is discarded. Success always reloads drafts and versions from the backend.
  - Preview/publish workflow (canPublishPayrollSetups only; manage alone never shows publish controls): enter effective date -> "Preview impact" -> render every PublicationImpactResponse field (allowed, effective_date, affected branches mapped to names with `Branch #id` fallback, predecessor version/hash, successor schedule/hash, next_version_boundary, current same-date version, conflicts with "All Branches" for null branch_id). Staleness key = setup, draft id, draft schedule key, effective date, replaces_version_id; any change disables Publish ("Inputs changed — preview again."). Publish enabled only for a current preview with allowed === true. Confirmation restates date, correction and affected branches from the last valid preview and that Published Versions are immutable. Success: toast, close panel, clear preview, reload drafts + versions. Failure: show code + message, clear preview, re-run the read-only preview once with unchanged inputs; publish is never retried.
  - Correction: checkbox "Publish as a correction of v#" appears only from the backend's current_same_date_version_id, unchecked by default; ticking sets replaces_version_id to exactly that id and invalidates the preview. Review fixes: changing the effective date clears the replacement (it is date-specific), and a set replacement stays visible so it can always be cleared (previously it could get stuck after a date change); discarding the Draft open in the Publish panel closes the panel.
  - Archived Setups: Drafts list still shown read-only; no draft/publish controls (note: backend would accept draft edit/discard on an Archived Setup, but the UI keeps U5a's read-only rule).

### U5c — Branch assignment / reassignment / withdraw UI

- Status: PASS
- Purpose: add the Branch Assignments tab (assign, reassign with preview, withdraw).
- Files: PayrollSetupsPage.tsx (+ css).
- Dependencies: U5a.
- Behavior: per section 5F Branch Assignments tab — assign, reassign (with mandatory reassignment-impact preview, confirm only when allowed), withdraw (server decides eligibility).
- Security/authority constraints: all three mutations gated by payroll_setup.assign; reassign confirm disabled unless the latest preview says allowed.
- Focused tests: manual/E2E per section 10; no dedicated pure-module unit test (UI is composition over U3 API wrappers).
- Non-goals: setup metadata/versions/publish (U5a/U5b).
- Completion criteria: Branch Assignments tab implemented per 5F; assign/reassign/withdraw wired to backend with preview-gated confirm.
- Result (PASS, Opus-reviewed; no functional fix round needed):
  - Files: modified frontend/src/pages/settings/payroll/PayrollSetupsPage.tsx + PayrollSetupsPage.module.css (two new classes, all classes 1:1 with usage); created frontend/src/pages/settings/payroll/branchAssignmentPreview.ts (pure) and frontend/tests/branchAssignmentPreview.test.ts; extended frontend/tests/payrollSetupsPage.test.ts (U5b-era "no U5c wrapper" checks inverted; U6 calls and /payroll/schedule still forbidden). Supersedes this unit's original "no pure-module test" note.
  - Backend contract (verified): POST /branches/{b}/assignments {setup_id, effective_from_date, reason?} (assign; no preview endpoint; rejects BRANCH_NOT_OPERATIONAL, SETUP_NOT_ACTIVE, CONCURRENT_ASSIGNMENT_CHANGE, ASSIGNMENT_OVERLAP, ASSIGNMENT_GAP, VERSION_NOT_FOUND, boundary conflicts); POST /branches/{b}/reassignment-impact {destination_setup_id, effective_from_date} (view, read-only; branch/destination problems are HTTP errors, ASSIGNMENT_NOT_FOUND / UNCHANGED_ASSIGNMENT / VERSION_NOT_FOUND / boundary codes are conflicts with branch_id null); POST /branches/{b}/reassignments {destination_setup_id, effective_from_date, reason?} (assign); POST /assignments/{a}/withdraw {reason?} (assign; no preview; PERIOD_HISTORY_CONFLICT etc.; on success may extend the predecessor assignment). GET /settings/branches needs only branch access, so a company-wide payroll_setup user sees every branch without setup.manage — no blocker.
  - Initial assignment: "Assign Setup to Branch" -> branch, Active Setup (no version-coverage prefilter), effective date, optional reason -> confirmation restating them and that the server validates (nothing is previewed) -> assign; errors shown with code + message; no retry.
  - Reassignment: preview-first. "Reassign…" per branch -> branch (changeable), Active destination (no client exclusion), date, reason -> "Preview impact" renders every response field (branch, backend source setup, destination, predecessor/successor version ids, effective date, allowed, conflicts). Staleness = branch + destination + date (reason excluded); Reassign enabled only for a current allowed preview; confirmation uses the source Setup from the backend preview and states a new effective-dated transition is created without rewriting history. Failure: code + message, preview cleared, one read-only re-preview with unchanged inputs; mutation never retried.
  - Withdrawal: "Withdraw…" only on non-withdrawn rows -> branch, setup, interval, optional reason -> confirmation says the server decides and the withdrawn record remains in history (no eligibility claims). Withdrawn rows stay visible with withdrawn_at_utc and withdrawal_reason; history shows setup, interval and reason in API order with no current/future labels.
  - Permission boundary: every assignment control (buttons and the modals/panel themselves) gated by canAssignPayrollSetups only; view-only users see history with no controls; no manage/publish/setup.manage shortcut.
  - Refresh: every successful assign/reassign/withdraw bumps the composition reload key (branches + readiness + assignments), which also feeds the Setups tab's Assigned Branches; selected setup and tab preserved; no local splicing.
  - Opus review edit: moved the AssignForm/ReassignForm interfaces and EMPTY_* constants from inside the component to module scope to match the file's existing pattern (no behavior change).
  - Preserved open finding (not fixed here): the backend allows Draft edit/discard on an Archived Setup while the UI keeps Archived Setups read-only.

### U6 — Branch read-only Payroll Schedule page

- Status: PASS
- Purpose: give branch-scoped payroll.view users a read-only schedule/history view outside Settings.
- Files: new frontend/src/pages/payroll/schedule/BranchPayrollSchedulePage.tsx + .module.css; new pure frontend/src/pages/payroll/schedule/branchScheduleView.ts (annotateHistory(history, effective | null, anchor | null)); App.tsx route; AppShell.tsx Payroll nav item "Payroll Schedule" + title/icon; PeriodsListPage.tsx link (5E).
- Dependencies: U0, U1, U3.
- Behavior: per section 5D — anchor from schedule_readiness_date; effective call only when reason === READY and date non-null; history governing row by id match, Scheduled tag for rows after anchor, withdrawn muted, no anchor -> plain chronological list.
- Security/authority constraints: route gated by canViewAnyBranchPayrollSchedule; page content additionally scoped per-branch by canViewBranchPayrollSchedule; no date input; no mutations; no company-wide Setup administration surfaced here.
- Focused tests: frontend/tests/branchScheduleView.test.ts (governing row by id match, Scheduled tag, withdrawn, null anchor).
- Non-goals: date input, mutations, company admin data.
- Completion criteria: page live at /payroll/schedule; branchScheduleView.test.ts passes; PeriodsListPage link per 5E DEFERRED — FUTURE FRONTEND PRODUCT/POLISH PASS.
- Result (U6 PASS):
  - Route: /payroll/schedule?branchId=<id> in the Payroll block of App.tsx (outside /settings), gated by canViewAnyBranchPayrollSchedule; Payroll nav item "Payroll Schedule" (same gate), title/icon matched before the generic /payroll mapping.
  - Permission model: non-driver + exact payroll.view only (route: any branch; page: per branch via canViewBranchPayrollSchedule). No payroll_setup.*, setup.manage, settings.manage or canManageSettingsAdmin anywhere in the route or page. Picker lists only GET /settings/branches entries that pass the branch helper.
  - Branch selection: URL is the single source; absent -> replace to the first viewable branch (existing StatusKeys convention); malformed id -> "not a valid Branch id"; id not among viewable branches -> "Branch #N is not available to you, or does not exist" with no requests and no substitution.
  - Readiness: GET /settings/branches/{id}; schedule_readiness_reason + schedule_readiness_date shown as returned; null reason -> "Readiness is not available" (never Ready); unknown codes shown raw with a generic explanation; not-ready explanations per spec copy for NO_ASSIGNMENT / NO_PUBLISHED_VERSION / AUTHORITY_BOUNDARY_CONFLICT, section 5G descriptions otherwise.
  - Effective authority: GET /effective only via effectiveRequestDate() — reason === 'READY' exactly and an ISO date present; period_start_date is that exact backend string. All response fields rendered directly (setup, assignment, version, schedule, config_hash, period start/end, next_boundary_date/kind with raw kind); nothing reconstructed from history.
  - History: GET /payroll-setup/branches/{id}/history loads independently of readiness (effect deps [branchId] only); rows in API order with withdrawn evidence (muted, withdrawn_at_utc + withdrawal_reason) and version segments (v#, id, interval, schedule summary, full hash). Governing tag by assignment_id/version_id match with /effective (and branch_id match), never by date. "Scheduled transition" only for non-withdrawn rows whose effective_from_date is after schedule_readiness_date (pure ISO string compare); no anchor -> no scheduled tags plus an explanatory note. No Date/wall-clock anywhere.
  - Error isolation: branch list, readiness, history and effective each keep their own state and render backend message + code + HTTP status; 403 is never shown as schedule absence.
  - Opus fix round: state was not tied to a branch, so on a branch switch one render could call /effective for the NEW branch with the OLD branch's readiness date (and flash stale data). Fixed by tagging readiness/history state with branchId and effective state with a `${branchId}|${date}` key, deriving current-only values for render and for effectiveDate; also removed an exhaustive-deps eslint-disable on the redirect effect. Six regression tests added.
  - 5E PeriodsListPage "View payroll schedule" link: DEFERRED — FUTURE FRONTEND PRODUCT/POLISH PASS (explicit product-scope decision after U6; Current Payroll changes were excluded from U6 and U7). Not a Phase 6 mechanics blocker. The Company & Branches entry point is delivered by U7; the Payroll nav item is the U6 entry point.
  - Files: new frontend/src/pages/payroll/schedule/{BranchPayrollSchedulePage.tsx, BranchPayrollSchedulePage.module.css, branchScheduleView.ts}; new frontend/tests/{branchScheduleView.test.ts, branchPayrollSchedulePage.test.ts}; App.tsx (import + route); AppShell.tsx (import + nav item + title + icon).
  - Human validation owed: full node --test sweep, npm run build, npm run lint; browser check of /payroll/schedule for a branch-scoped payroll.view user (READY with effective + next boundary; each not-ready reason; history with withdrawn and scheduled rows; branch switching; ?branchId= absent / malformed / inaccessible; a 403 path), and that Driver / ODA / payroll_setup-only users see no nav item and are gated from the route.

### U7 — Branch onboarding / first payroll start + CompanyBranches authorization alignment

- Status: PASS (after authorization fix round)
- Purpose: add first_payroll_start_date to branch creation and align CompanyBranches gating with actual backend authority (Correction 2).
- Files: frontend/src/pages/settings/company-branches/CompanyBranchesPage.tsx, App.tsx (route gate canAccessCompanyBranches (also the canCreateBranches terms in the outer /settings gate and SettingsDefaultRedirect)), AppShell.tsx (Company & Branches item gate), plus test additions in frontend/tests/payrollSetupAuthority.test.ts if helpers change.
- Dependencies: U1, U3.
- Behavior: Correction 2 gating (route: canAccessCompanyBranches; Add Branch + create modal: canCreateBranches; company/branch edit, set-default, "View only" banner: unchanged canManageSettingsAdmin); create modal first_payroll_start_date optional date input shown only when canCreateBranches && canAssignPayrollSetups, omitted from payload when blank or not shown; if canViewPayrollSetups, load GET /payroll-setup/default and show "Assigns default Setup: {name}" or a no-default notice (submission not blocked); success copy by response reason: READY -> "Payroll schedule assigned from {date}."; other non-null -> "Created, but payroll isn't ready: {label}"; null -> "Created. No payroll schedule assigned yet."; create PolicyError shown in modal (nothing persisted — backend is atomic); readiness column: reason label + tooltip when reason present, else legacy payroll_setup_done Complete/Setup Needed; badge links to /payroll/schedule?branchId= if canViewBranchPayrollSchedule, else /settings/payroll if canViewPayrollSetups, else plain text; "Needs Setup" KPI unchanged.
- Security/authority constraints: no permission broadened relative to backend contracts verified in Correction 2; entering the page never grants edit powers.
- Focused tests: payrollSetupAuthority.test.ts additions if helper behavior changes; otherwise manual smoke per section 10.
- Non-goals: none beyond scope above.
- Completion criteria: gating matches Correction 2 exactly; first_payroll_start_date wired end to end with correct success/error copy; readiness column and badge linking implemented.
- U7 acceptance warning (carried from U2 review): CompanyBranches access must distinguish branches.create, setup.manage and payroll_setup.assign per action. canAccessCompanyBranches is a route/nav gate only and must NOT be used as a shortcut for any action: Add Branch / create modal use canCreateBranches (company-wide, non-driver branches.create); company edit, branch edit and set-default keep canManageSettingsAdmin; the first_payroll_start_date field requires canCreateBranches && canAssignPayrollSetups. Note canAccessCompanyBranches admits setup.manage holders without a driver check (inherited, mirrors backend _ensure_company_admin) — it must never gate branch creation.
- Result (U7 PASS):
  - Backend (initial U7 pass): no change — superseded by the authorization fix round below, which added the is_default settings-admin check. Verified _ensure_branch_creator (company-wide + non-driver + branches.create; payroll_setup.assign only when first_payroll_start_date is sent; setup.manage not required); company edit, branch PATCH and set-default keep _ensure_company_admin (setup.manage); GET /settings/company and /settings/branches need branch access only.
  - Page access: /settings/company-branches route and the Company & Branches nav item use canAccessCompanyBranches; outer /settings gate adds canCreateBranches(u); SettingsDefaultRedirect's first step is canAccessCompanyBranches -> /settings/company-branches (remaining order unchanged).
  - Action permissions (new pure companyBranchesView.ts, no generic canEdit): Add Branch = canCreateBranches; Edit Company, row edit and Set as Default = canManageSettingsAdmin; first payroll start = canCreateBranches && canAssignPayrollSetups (independent of payroll_setup.view/manage/publish). Truthful per-capability banner ("Limited access") replaces the generic view-only banner.
  - Onboarding: create-only optional "First payroll start date" (type=date) with factual help text (Company default used only for this onboarding step; afterwards the Branch's own assignment and Published Version govern). Without assign: a help line; creation still allowed. Payload via buildBranchCreatePayload: existing fields + is_default, first_payroll_start_date only when authorized and non-blank; never setup/version/assignment/readiness fields. Edit uses buildBranchUpdatePayload (unchanged PATCH contract). No /payroll-setup call, no Setup selector, no readiness computation.
  - Result UX: success dialog uses the response's schedule_readiness_reason/date verbatim — READY, NO_COMPANY_DEFAULT (successful creation, incomplete onboarding), known non-READY (label + code + description), unknown (raw code), null; evaluated date shown; optional follow-up links (View Payroll Schedule if canViewBranchPayrollSchedule for the new branch; Open Payroll Setups if canViewPayrollSetups). Create errors via readApiError with code chip and HTTP status; no retry; modal stays open; nothing claimed created.
  - Setup Needed: /settings/payroll only if canViewPayrollSetups; else /payroll/schedule?branchId= if canViewBranchPayrollSchedule; else a non-interactive badge. Readiness reason label+code added as tooltip. "Needs Setup" KPI unchanged. The U5a link/access mismatch is CLOSED.
  - Branch row: "View Payroll Schedule" link to /payroll/schedule?branchId=<id> gated by canViewBranchPayrollSchedule (no payroll_setup.view dependency).
  - Tests: new frontend/tests/companyBranchesU7.test.ts (60, spec items A–U + notices/date validation); two intended assertion updates for the new gates (payrollSetupsPage.test.ts outer gate regex; statusKeysRoute.test.ts company-branches gate).
  - Opus review: full line review, no defects, no fix round. Deliberate plan change: the optional GET /payroll-setup/default "Assigns default Setup: {name}" preview from the original U7 bullet was not built — the U7 request forbids making that fetch a prerequisite and it would add a permission-dependent, potentially stale pre-submit claim; deferred to the product/polish pass. Success copy follows the U7 request (supersedes the original bullet's copy).
  - Files: new companyBranchesView.ts, companyBranchesU7.test.ts; modified CompanyBranchesPage.tsx, CompanyBranchesPage.module.css (scheduleLink, fieldHelp, errorCode, successLinks), App.tsx, AppShell.tsx, payrollSetupsPage.test.ts, statusKeysRoute.test.ts.
  - Authorization fix round (human-identified defect): POST /settings/branches with is_default=true only required branches.create, yet create_branch clears the existing default — letting a branches.create-only user change the Company default that POST /settings/branches/{id}/set-default protects with setup.manage. Backend fix (smallest enforcement, canonical helper reused): _ensure_branch_creator takes required keyword with_default and, when true, calls _ensure_company_admin (company-wide + setup.manage) before any mutation; create_branch passes with_default=data.is_default. Ordinary create (branches.create) and the first_payroll_start_date + payroll_setup.assign rule are unchanged; set-default is unchanged; router description documents the rule. Frontend: capability canSetDefaultOnCreate = canCreateBranches && canManageSettingsAdmin; the create-form "Set as default branch" checkbox renders only with it (otherwise a one-line explanation); buildBranchCreatePayload(form, date, { canOnboard, canSetDefaultOnCreate }) forces is_default=false without that authority. Tests: backend test_branch_create_default_requires_settings_admin (create-only ordinary 201; create-only / create+assign / setup.manage-only with is_default 403 and nothing created; create+setup.manage with is_default 201 and old default demoted; create+setup.manage+date without assign 403; set-default still 403 for create-only and allowed for setup.manage); frontend companyBranchesU7 extended to 75 (capability truth table, payload forcing, page wiring, set-default path unchanged). Opus review edits: router description updated; test docstring trimmed. Opus-rerun: phase5 onboarding (-k create/default/onboarding_default_null) 3 passed; test_settings -k "default or create" 24 passed; phase6_readiness_date 10 passed; ruff clean (app.__file__ verified inside the phase6 worktree); frontend companyBranchesU7 75/75 + 15 related files 428/428; tsc clean; eslint clean; diff --check clean.
  - Human validation owed: full node --test sweep, npm run build, npm run lint, backend suite; browser role matrix on /settings/company-branches (setup.manage+create admin; branches.create-only; setup.manage-only; create+assign without payroll_setup.view; payroll_setup.view-only denied; Driver/ODA denied); create with and without a first payroll start (READY, NO_COMPANY_DEFAULT, SETUP_NOT_ACTIVE default, NO_PUBLISHED_VERSION, assignment/boundary PolicyErrors, 403/422); Setup Needed and View Payroll Schedule links per role; /settings default redirect for a create-only user.

## 9. Dependency graph

```
U0  : no dependencies
U1  : no dependencies
U3  : no dependencies
U2  depends on U1
U4  : no dependencies (must be completed before U5a)
U5a depends on U1, U3, U4
U5b depends on U5a
U5c depends on U5a
U6  depends on U0, U1, U3
U7  depends on U1, U3
```

State: implementation is sequential in practice; no parallel implementation.

## 10. Validation ownership

- Sonnet: narrow unit tests only; touched-file eslint; `npx tsc -p tsconfig.app.json --noEmit` when short; git diff --check.
- Opus: diff review of every changed line, architecture/security review, unit verdict.
- Human: full backend suite, npm run build, full npm run lint, full frontend node --test sweep, manual role matrix (Owner; company role with only payroll_setup.view; only publish; only assign; branches.create-only; branch manager with payroll.view; Driver; OwnDriverDataOnly; mixed Driver + company admin), E2E walkthrough (create Setup -> draft -> preview -> publish -> mark default -> create branch with first start -> READY -> Payroll Schedule page -> Create Period still works -> clear default -> create branch with date -> NO_COMPANY_DEFAULT).
- Backend test runtime note: the only Python venv is C:\Projects\etbdnt\Payroll_App_v3\backend\.venv and its `app` package is an editable install pointing at the MAIN checkout. When testing the phase6 worktree, run from C:\Projects\etbdnt\Payroll_App_v3-phase6\backend with PYTHONPATH=C:\Projects\etbdnt\Payroll_App_v3-phase6\backend and confirm `import app; app.__file__` resolves inside the phase6 worktree before trusting results. Tests use testing.postgresql (pg_instance fixture).
- Frontend tests: node --test <file> (Node 24 native TS); pure modules only.

## 11. Final Phase 6 exit criteria

- [ ] No active frontend legacy branch-payroll-setup GET/PUT path.
- [ ] No UI treating branch-owned mutable Payroll Setup as authority.
- [ ] Company Setup administration driven by payroll_setup.*.
- [ ] Branch payroll.view users can inspect their own schedule/history read-only.
- [ ] Driver / OwnDriverDataOnly schedule/history denial preserved.
- [ ] Status Keys still work.
- [ ] Roles and People can represent the new permissions.
- [ ] Branch onboarding supports first_payroll_start_date correctly.
- [ ] CompanyBranches gating aligned with backend (branches.create / setup.manage / payroll_setup.assign) without broadening.
- [ ] Clear Company Default available to payroll_setup.assign.
- [ ] Readiness reason/date backend-derived.
- [ ] Frontend does not choose authority based on today.
- [ ] Create Period remains candidate-driven.
- [ ] Current Payroll remains on canonical Hub.
- [ ] Legacy TypeScript schedule calculation helpers removed.
- [ ] No MAX_DAYS_OFF client authority remains.
- [ ] Server stays authoritative for publish/reassign/archive validation.
- [ ] Focused unit tests pass.
- [ ] Human final validation passes.

## 12. Progress table

| Unit | Status | Review | Focused validation | Notes |
|---|---|---|---|---|
| U0 | PASS | Opus: approved after one test fix round (driver gate isolation, mapped-failure date) | Opus-rerun: phase6_readiness_date 10 passed; phase5_onboarding -k readiness 7 passed; ruff clean; diff --check clean | Human: full backend suite still owed |
| U1 | PASS | Opus: approved; one comment-only correction applied during review | Opus-rerun: payrollSetupAuthority 28/28; existing authority/gate tests (pageVisibility, auth, peopleRoles, cdpi, ledgerDiscovery, workflowCapabilityGate) 165/165; tsc app clean; eslint touched files clean; diff --check clean | Human: full node --test sweep, npm run build, npm run lint still owed |
| U2 | PASS | Opus: approved; verbatim extraction verified line-by-line against removed page code | Opus-rerun: permissionGrantUi 20/20; payrollSetupAuthority + peopleRoles + pageVisibility + auth 146/146; tsc app clean; eslint touched files clean; diff --check clean | Human: full node --test sweep, npm run build, npm run lint, visual check of Roles/People notes still owed |
| U3 | PASS | Opus: types + 21 wrappers approved against schemas.py/router.py; one fix round (readiness own-property lookup, toggleDay range check, BranchAdmin comment) | Opus-rerun: payrollSetupErrors + payrollSetupReadiness 30/30; tsc app clean; eslint 7 touched files clean; diff --check clean | Human: full node --test sweep, npm run build, npm run lint still owed; API wrappers exercised at runtime from U5a on |
| U4 | PASS | Opus: behavior equivalence verified mechanically and line-by-line against legacy code; no fix round needed | Opus-rerun: statusKeysRoute 16/16; pageVisibility + payrollSetupAuthority + permissionGrantUi 95/95; tsc app clean; eslint touched files clean; diff --check clean | Human: full node --test sweep, npm run build, npm run lint, manual Status Keys CRUD smoke + visual check at /settings/status-keys still owed |
| U5a | PASS | Opus: full line review; one fix round (action errors scoped to their Setup) | Opus-rerun: payrollSetupLegacyGuard + payrollSetupsPage + statusKeysRoute 70/70; payrollSetupAuthority + pageVisibility + readiness + errors + permissionGrantUi 125/125; tsc app clean; eslint touched files clean (one pre-existing DashboardPage exhaustive-deps warning in untouched code); diff --check clean | Human: full node --test sweep, npm run build, npm run lint, manual browser check of /settings/payroll (create/edit/default/clear/archive incl. SETUP_ASSIGNED and DEFAULT_SETUP_IN_USE paths), legacy ?tab=status-keys redirect, role matrix for Settings nav still owed |
| U5b | PASS | Opus: full line review; one fix round (stuck same-date correction after date change; discard closes the open Publish panel) | Opus-rerun: draftEditor + publishPreview + payrollSetupsPage + legacyGuard + statusKeysRoute 127/127; readiness + errors + payrollSetupAuthority + permissionGrantUi + pageVisibility 125/125; tsc app clean; eslint payroll page dir + U5b tests clean; diff --check clean | Human: full node --test sweep, npm run build, npm run lint, manual browser check of draft create/prefill/edit/discard and preview/publish incl. same-date correction, allowed=false conflicts, and publish 409 re-preview still owed |
| U5c | PASS | Opus: full line review; no functional defects; one structure-only edit (form types/constants to module scope) | Opus-rerun: branchAssignmentPreview + payrollSetupsPage + publishPreview + draftEditor + legacyGuard + statusKeysRoute 167/167; readiness + errors + payrollSetupAuthority + permissionGrantUi + pageVisibility 125/125; tsc app clean; eslint payroll page dir + U5c tests clean; diff --check clean | Human: full node --test sweep, npm run build, npm run lint, manual browser check of assign (incl. ASSIGNMENT_OVERLAP/GAP, VERSION_NOT_FOUND), reassign preview/confirm (allowed=false, UNCHANGED_ASSIGNMENT, mutation 409 re-preview), withdraw (incl. PERIOD_HISTORY_CONFLICT, predecessor extension visible after reload), view-only vs assign roles still owed |
| U6 | PASS | Opus: full line review; one fix round (branch-keyed state so /effective can never pair a new branch with a stale readiness date; removed redirect-effect eslint-disable) | Opus-rerun: branchScheduleView + branchPayrollSchedulePage 65/65; payrollSetupAuthority + pageVisibility + payrollSetupsPage + statusKeysRoute + legacyGuard + readiness + errors + permissionGrantUi + branchAssignmentPreview + publishPreview + draftEditor 292/292; tsc app clean; eslint schedule dir + App + AppShell + U6 tests clean; diff --check clean | Human: full node --test sweep, npm run build, npm run lint, browser check of /payroll/schedule (READY vs each not-ready reason, history incl. withdrawn/scheduled rows, branch switching, absent/malformed/inaccessible branchId, 403 path, Driver/ODA/payroll_setup-only users) still owed; 5E PeriodsListPage link deferred |
| U7 | PASS | Opus: full line review; initial implementation had no defects; human-identified authorization defect (create with is_default=true bypassed setup.manage) fixed in one round (backend _ensure_company_admin when is_default + frontend gating), re-reviewed | Opus-rerun: companyBranchesU7 60/60; payrollSetupAuthority + pageVisibility + payrollSetupsPage + statusKeysRoute + legacyGuard + branchPayrollSchedulePage + branchScheduleView + peopleRoles + authAuthority + permissionGrantUi + readiness + errors + branchAssignmentPreview + publishPreview + draftEditor 428/428; tsc app clean; eslint company-branches dir + App + AppShell + touched tests clean; diff --check clean; auth-fix Opus-rerun: backend focused 3 + 24 + 10 passed, ruff clean; companyBranchesU7 75/75; related 428/428 | Human: full node --test sweep, npm run build, npm run lint, backend suite, browser role matrix + create/onboarding outcomes + default-on-create by role + link targets still owed |

Update this table after every unit.

## 13. Decision / change log

Dated 2026-09-25 unless noted otherwise:

1. Phase 6 discovery accepted (PHASE_6_DISCOVERY_PASS at cd1a059).
2. U0 backend readiness evaluation date accepted as the only backend change in Phase 6.
3. R3 (CompanyBranches setup.manage vs branches.create mismatch) pulled into Phase 6 as part of U7.
4. Nullable company default verified end-to-end; Clear Company Default added to U5a.
5. Client-only MAX_DAYS_OFF = 2 rejected.
6. Current Payroll Hub and candidate-driven Create Period preserved unchanged (link addition only).
7. Status Keys separated to /settings/status-keys.
8. Archive eligibility server-authoritative; no eligibility preview endpoint added.
9. Assigned Branches via N+1 composition; setup->assignments endpoint deferred as optimization.
10. Long/full regression reserved for the human.
11. Execution baseline moved to d388aee (docs-only delta).
12. Execution plan reviewed by Opus (4 fixes: explicit dependency wording, redirect owned by U5a, per-unit ownership of nav/route changes, no invented double-click edit) and approved.
13. U0 PASS. Readiness detail returns the evaluated date on mapped resolver failures too; the reason, not the date alone, decides whether /effective is called.
14. U1 PASS. canCreateBranches / canAccessCompanyBranches delivered in U1 per section 6 (helpers only; wired in U7). Phase6 worktree frontend dependencies installed via `npm ci` (lockfile unchanged; node_modules git-ignored) so focused frontend tests run against this worktree.
15. U2 PASS. Pure grant models extracted from RolesPage/PeoplePage for testability (verbatim). manage/publish/assign -> view recorded as a UI-only administration dependency, not a backend implication. Discovered (out of Phase 6 scope, not fixed): catalogue codes ledger.view, ledger.audit.view and payroll.edit are still in no Roles BUSINESS_GROUP, so they remain invisible/ungrantable in the Roles UI (preserved on save). Process note: the U2 worker also ran the full frontend node sweep (252/252 pass) although it was reserved for the human; the human sweep is still owed.
16. U3 PASS. /payroll-setup router has 21 endpoints (discovery said 19); all wrapped. No backend/frontend contract gaps found. API wrappers statically reviewed only (no node-testable apiClient); later units provide runtime coverage.
17. U4 PASS. Status Keys extracted to /settings/status-keys (gate unchanged: canManageSettingsAdmin). Transitional duplication accepted: the legacy PayrollSetupPage Status Keys tab remains until U5a deletes the page and adds the ?tab=status-keys redirect. Existing "Inactive" filter semantics (shows all keys) preserved as-is. Route/nav assertions are static source checks (App.tsx/AppShell.tsx are .tsx and not importable by node --test).
18. U5a PASS. Legacy branch-owned Payroll Setup page, its GET/PUT calls, schedule math and types removed; /settings/payroll is the company-owned Payroll Setups page gated by payroll_setup.view. The legacy guard test fails if any of these legacy patterns is reintroduced into frontend/src. Transitional note until U7: the CompanyBranches "Setup Needed" badge links to /settings/payroll, which a setup.manage-only user (without payroll_setup.view) cannot open. Pre-existing lint warning noted, not fixed: DashboardPage.tsx line ~180 react-hooks/exhaustive-deps (setWarnings).
19. U5b PASS. Draft authoring and preview-first publication on /settings/payroll; publish gated only by payroll_setup.publish (never manage), drafts by payroll_setup.manage; the same-date replacement id comes only from the backend impact response and is cleared on date change.
20. Housekeeping: the U0-era concern about a worker-created backend/.env is RESOLVED — the human verified `Test-Path .\backend\.env` is False (Opus existence check agrees); no env file exists or will be created. It is not an open finding.
21. U5c PASS. Branch assignment mechanics complete on /settings/payroll: initial assignment and withdrawal are server-validated without a client preview (none exists in the backend); reassignment is preview-first with staleness on branch + destination + date; all three mutations gated only by payroll_setup.assign; history (incl. withdrawn evidence) is always reloaded from the backend. Open finding carried forward: Archived Setup Draft edit/discard semantics differ between backend (allowed) and UI (read-only).
22. U6 PASS. Branch read-only Payroll Schedule at /payroll/schedule (Payroll area), gated only by non-driver exact payroll.view; readiness from BranchAdmin, /effective only for READY + backend date (exact string as period_start_date), history independent of readiness, governing by id match, "Scheduled transition" only against schedule_readiness_date. Deviation: the section 5E PeriodsListPage link is deferred (U6 request excludes Current Payroll changes). Open findings carried forward unchanged: (1) Archived Setup Draft edit/discard allowed by backend but read-only in UI; (2) Company & Branches "Setup Needed" link/access mismatch until U7; (3) Roles UI cannot grant ledger.view, ledger.audit.view, payroll.edit; (4) pre-existing DashboardPage exhaustive-deps lint warning.
23. U7 PASS — Phase 6 implementation complete (U0–U7 PASS); human full validation, commit/PR and merge remain. Company & Branches page access = canAccessCompanyBranches; actions split (create = canCreateBranches; company/branch edit and set-default = canManageSettingsAdmin; first payroll start = canCreateBranches && canAssignPayrollSetups, never requiring payroll_setup.view). NO_COMPANY_DEFAULT is a successful creation with incomplete onboarding. The U5a "Setup Needed" link/access mismatch is CLOSED. The 5E Current Payroll "View payroll schedule" link is DEFERRED — FUTURE FRONTEND PRODUCT/POLISH PASS (not a mechanics blocker). New finding for human/product decision (not changed): creating a Branch with is_default=true requires only branches.create, so a creator without setup.manage can replace the Company's default Branch (backend create semantics preserved as instructed). Open findings carried forward: (1) Archived Setup Draft edit/discard allowed by backend, read-only in UI; (2) Roles UI cannot grant ledger.view, ledger.audit.view, payroll.edit; (3) pre-existing DashboardPage exhaustive-deps lint warning.
24. U7 authorization fix round PASS. Creating a Branch with is_default=true now requires settings-admin (setup.manage via _ensure_company_admin) in addition to branches.create; ordinary creation and the payroll_setup.assign onboarding rule are unchanged; the dedicated set-default endpoint is unchanged. The frontend offers "Set as default branch" on create only to users with both branches.create and setup.manage and never sends is_default=true otherwise. This resolves the finding recorded in entry 23. Phase 6 implementation is complete (U0–U7 PASS); human full validation, commit/PR and merge remain.
25. Product-correction pass PASS (after manual browser validation). Authority architecture unchanged: Setup = a policy followed by Branches; Versions = immutable effective-dated updates that may change cadence; Company Default onboarding-only.
    - C1: Normal days off limited to at most two — canonical chronology validator, service/API code INVALID_NORMAL_DAYS_OFF (422), forward migration 0069 (fail-closed precheck; tightened ck_PayrollSetupVersions_Mask; discarded Drafts exempt as inert evidence). Supersedes entry 5: the rule is backend-enforced and the UI only guides.
    - C2a: pure next/previous legal-start and version-timeline stepping helpers (chronology.py); clock.company_today (company-local date, used only for the first-onboarding window and display/suggestion labels — never payroll authority); first-onboarding look-back guardrail (current period + two previous legal periods + any future period, ONBOARDING_START_TOO_EARLY) enforced unconditionally inside assign_setup for any Branch without a non-withdrawn assignment — no opt-out; read-only evaluate_assignment / preview_assignment_impact.
    - C2b: one canonical boundary-choices service (boundaries.py) behind publication-choices, assignment-choices, reassignment-choices and /settings/branches/onboarding-options (branches.create + payroll_setup.assign). Validity comes only from the write-path validators; today only drives relation labels and the suggested date. policy.py renamed payroll_policy.py (no shim).
    - C2c: GET /payroll-setup/branch-summaries (current / scheduled change / readiness per Branch). Setup code optional on create: server-generated references use the reserved PPOL- namespace plus an 8-character random suffix, with collision safety from uq_PayrollSetups_Company_Code and retry; explicit PPOL- codes rejected (INVALID_SETUP_CODE). PayrollSetupID remains the identity.
    - C3: reusable Day/Month/Year DateInput; PayrollBoundaryDateInput stepper (arrows move only to backend-provided legal dates); useBoundaryChoices; friendly error copy with codes under Technical details; isoDate pure calendar helpers (no Date usage).
    - Unit A: Payroll Policies page split into components; terminology pass; welcome empty state and next steps; two-day days-off guidance; publication date stepper with automatic debounced stale-safe preview (execution uses the inputs snapshot the preview was fetched with); compact Branch Assignments table (Assign policy / Manage) with a Manage drawer (change policy with automatic stale-safe preview, cancel scheduled change, policy history).
    - Unit B: Company & Branches "Set up payroll for this branch now" workflow using the company default and the server-suggested first period start; human-readable Payroll Schedule page with technical evidence collapsed.
    - Open findings: (1) the backend test harness creates a WIN1252 database on this Windows host, so no backend test can store Arabic text (harness fix outside this pass); (2) Archived Setup Draft edit/discard allowed by backend, read-only in UI; (3) Roles UI cannot grant ledger.view, ledger.audit.view, payroll.edit; (4) pre-existing DashboardPage exhaustive-deps lint warning; (5) the 5E Current Payroll "View payroll schedule" link remains deferred. Development databases holding a live Draft or Published Version with more than two normal days off must be rebuilt from migrations before upgrading to 0069.
