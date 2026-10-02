# PEOPLE & ACCESS REFOUNDATION MASTER PLAN

**Status:** Implementation-ready draft — derived from locked People / Workforce / Access contract  
**Locked contract:** `docs/architecture/PEOPLE_WORKFORCE_ACCESS_ARCHITECTURE_CONTRACT.md`  
**Locked contract commit:** `a38c9306c51f00957719c22a1508e7e84a32503c` (`docs: lock people workforce access architecture`)  
**Baseline commit:** `e15d3477c135647ed45e7fba9470f6acc80bf52a` (`main`, equal to `origin/main`)  
**Migration head at planning time:** `0071` (`0071_retire_legacy_payroll_schedule_authority`)  
**Planning position (historical):** This plan was prepared before Phase 1. Current execution status and sequencing are maintained in `FLUSSRA_UNIFIED_REFOUNDATION_EXECUTION_PLAN.md`.

**Supersession note (2026-10-02):** The Unified Plan controls current execution sequencing. The amended `PEOPLE_WORKFORCE_ACCESS_ARCHITECTURE_CONTRACT.md` controls DRIVER authorization semantics. Any earlier `OwnDriverDataOnly` branch-projection instruction in this older plan is superseded by generic `Self` scope: no Access BranchID, no branch-membership projection, and ownership through the explicit User–Employee relationship and canonical effective Driver profile. References below to the old scope are retained only as historical implementation/planning evidence or legacy behavior being retired.

The locked contract owns semantics. This older plan is a detailed design reference only where it does not conflict with the contract or Unified Plan. It does not select a mechanism that the Lead has left open.

---

## Executive Summary

- **Phases:** 9.
- **Sizes:** SMALL — none. MEDIUM — Phases 1, 4, 6, 7, 9. LARGE — Phases 2, 3, 5, 8.
- **Hardest phases:**
  1. **Phase 5 — Effective-dated transfer & Workforce projection sync.** Changes when "current" flips and makes the effective-date resolver the runtime authority for current-profile consumers. `Employee.BranchID` remains a Workforce projection; Driver Self authorization follows the linked Employee/effective-profile relationship and has no Access branch projection.
  2. **Phase 3 — Access decoupling & provisioning.** Removes the DRIVER-role workforce side effects that many existing tests and fixtures rely on, adds link/staged/provisioning semantics, and must replace the old wizard's backend contract in the same PR.
  3. **Phase 8 — Employee-centered People & Access frontend.** Replaces the 1,256-line User-centered `PeoplePage.tsx` with an Employee-centered workforce view plus a separate Access view.
- **Relative complexity:** comparable to the Payroll Setup refoundation in phase count, but lower in payroll-formula risk and higher in security and cross-module surface (admin, core, transfer, rates guards, frontend). The main risk is regression in authorization and transfer timing, not calculation.
- **C2 Import:** remains blocked until the Completion Condition at the end of this document is met.
- **Original plan began with:** Phase 1 — Workforce integrity foundation & effective-date resolver. Current execution is governed by the Unified Plan.

---

## 1. Current-State Anchor (what the phases change)

Facts from the repository at the baseline commit that shape the sequence:

| Area | Current implementation | Evidence |
|---|---|---|
| People list | User list from `GET /admin/users` | `frontend/src/pages/people/PeoplePage.tsx:332`; `backend/app/admin/service.py:532-577` |
| Add Person wizard | Step 1 commits a User; step 3 commits role (+ DRIVER side effects); step 4 commits overrides | `PeoplePage.tsx:382-452` |
| DRIVER side effects | Substring match `"DRIVER" in rolecode`; `ensure_driver_profile` creates Employee (`EMP-{UserID}`) + Driver, links `Users.EmployeeID`, moves branches | `admin/service.py:1824, 1909-1916, 2386-2573` |
| Driver create/edit | `POST/PATCH /core/drivers`, `drivers.manage`; PATCH also edits Employee fields and can reactivate historical profiles | `core/service.py:573-732`; `core/schemas.py:69-125` |
| Employee write API | None | — |
| "Current" driver lookups | Status-based (`driverstatus NOT IN ('Transferred','Terminated')`) | `admin/service.py:346, 2421, 2632`; `core/service.py:371`; `transfer/service.py:235, 288`; `payroll/guards.py:91` |
| Payroll eligibility | Already effective-window aware (Transferred path via `EffectiveTo`) | `payroll/eligibility.py:53-77, 395-420`; migration `0057` |
| Transfer completion | Creates destination (`EffectiveFrom = EffectiveDate`), sets source `Transferred` then `EffectiveTo` in separate UPDATEs, moves `Employee.BranchID` immediately, no row lock | `transfer/service.py:569-741` |
| Rate copy engine | `copy_driver_rates`: Approved-as-of-date, atomic, strict target-branch validation, `AllowSelfApproval`, optional pay rules | `payroll/rates.py:2872+`; route `payroll/router.py:1066` |
| ODA resolution | Scope-based; DRIVER+SpecificBranch escapes self-only | `payroll/guards.py:28-103`; `test_security_matrix.py:488` |
| Roster reads | `/core/people`, `/core/drivers`, `/core/drivers/{id}` check branch access only; ODA rows count as branch access | `core/service.py:25-75, 292-566` |
| DB constraints | No `Users.EmployeeID` uniqueness or company check; no Driver→Employee company check; no status CHECKs; partial unique `(EmployeeID, BranchID)` for non-historical profiles; composite branch FKs on payroll tables | `0001`, `0025`, `0026`, `0027`, `0031` |
| Company clock | `payroll_setup/clock.py: company_today()` (company timezone) | `backend/app/payroll_setup/clock.py` |
| Background jobs | None (FastAPI lifespan only) | `backend/app/main.py` |

---

## 2. Decisions Made by This Plan

These are the decisions the contract delegates to the master plan. They are fixed for implementation.

### D1. Effective-date transfer mechanism — resolver-driven activation with gated projection reconciliation

Options compared:

| Option | Assessment |
|---|---|
| **Delayed completion** (create destination only on/after `EffectiveDate`) | Rejected. The destination profile would not exist before the effective date, so target-branch periods created in advance (whose eligibility snapshot is built at period creation, migration `0057`) would miss the Driver; tested behaviour (`test_driver_transfer_workflow.py:1929-2212`) depends on the destination existing early. It also needs a human or scheduler to act on the exact day. |
| **Scheduled activation** (job flips state on the date) | Rejected as primary. The repository has no scheduler/worker infrastructure; a missed run leaves stale state; it still needs a resolver for correctness. |
| **Resolver / effective-date driven activation** | **Chosen.** Driver effective windows already exist and payroll eligibility already resolves by date. Completion creates the destination as a *pending* profile; "current" is always resolved by date. |

The chosen mechanism has three parts:

1. **Canonical resolver (authority).** SQL functions, all created in Phase 1:
   - `core.fn_CompanyToday(p_CompanyID) RETURNS date` — `(NOW() AT TIME ZONE c.TimeZoneName)::date`, the single definition of the company business date (same expression as `payroll_setup/clock.py`; Payroll Setup is not modified);
   - `core.fn_DriverEffectiveRange(p_From date, p_To date) RETURNS daterange` (IMMUTABLE; see D7);
   - `core.fn_EffectiveDriverProfile(p_EmployeeID, p_OnDate) RETURNS INTEGER` — the single `DriverID` whose `fn_DriverEffectiveRange(EffectiveFrom, EffectiveTo) @> p_OnDate`, or NULL.

   Python wrappers live in `backend/app/workforce/{clock,effective}.py` and call these functions. Every runtime "current profile" decision uses them.

2. **Self authorization is resource-scoped, not branch-derived (amended architecture).** The former ODA design derived a branch from the linked Driver and treated Self like branch access; that design is superseded. Current authorization checks are distributed through SQL permission evaluation, branch-access projections, endpoint services, login and `/auth/me`. P2b must apply the generic Self semantics in the amended contract: a valid Self assignment has no Access BranchID; ownership is proven from User.EmployeeID → Employee → the relevant Employee-owned resource, using the canonical effective Driver profile for current/date-sensitive Driver actions. Missing link, company mismatch, or no effective profile for a current action fails closed. A branch-access function/view must not fabricate branch membership for Self, and login/`/auth/me` must be able to represent valid Self authority without a branch-access row. Permission overrides may add actions only within already-established resource scope; they cannot make an action Self-compatible or bypass the DRIVER ceiling. Company Owner dynamic permissions require a valid active company-wide owner assignment. The exact authorization/storage mechanism remains for the Lead's P2b design pass.

3. **Workforce branch projection remains separate.** `Employee.BranchID` for Driver Employees remains a Workforce projection of the current (or pending, where defined) profile under contract §3.5. Transfer/effective-transition operations and any established Workforce reconciliation maintain this projection. No Access Self assignment or Access BranchID is synchronized on transfer or termination. The former ODA branch projection is legacy state to migrate/retire, not a projection maintained for display or authorization.

### D2. Permissions

- New catalog codes: `employees.view`, `employees.manage` (module `employees`). Company Owner receives them automatically (owner permissions are dynamic, `admin/service.py:396-428`).
- `employees.view` gates Employee reads and `/core/people`; `employees.manage` gates Employee create/update, Driver-profile creation, Driver-owned field edits, and termination (checked against the Employee's branch via `fn_UserHasPermission`).
- `drivers.view` / `drivers.edit` keep their current meaning for Driver-profile reads and transfers.
- `drivers.manage` (legacy, used only by `core/service.py:597, 681` and fixtures) is retired from runtime checks.

### D3. EmployeeType — temporarily retained, retired from runtime

After the `driver_state` conversion no product or runtime consumer needs `EmployeeType`. Its only current readers are the `/core/people` filter and response field (`core/router.py:59-74`, `core/service.py:326-389`, `core/schemas.py:34`), the Transfer Requests driver picker (`TransferRequestsTab.tsx:318`, switched to `driver_state` in Phase 2) and the frontend type (`frontend/src/types/core.ts:22`). Its only writers are `core/service.py:618` and the retired `ensure_driver_profile`. No new taxonomy is introduced.

- **Phase 2:** migration drops `NOT NULL` from `core.Employees.EmployeeType` (no CHECK added); runtime code stops reading and writing it; the API field and filter are removed. The column is kept only because ~20 raw-SQL test fixtures insert it (e.g. `test_db_integrity.py`, `test_p6a_finalized_library.py`, `test_cp5c_reports.py`).
- **Phase 9:** after proving no application reference remains, the fixtures are updated and the column is dropped.

Under no outcome is `EmployeeType` used for authorization, Driver identity, or current-Driver resolution.

### D4. EmployeeKey

Optional on input. If omitted, the workforce service generates `E{EmployeeID:06d}` after insert. Company-unique (existing index `ux_Employees_Company_EmployeeKey`); a duplicate returns 409. **Immutable after creation** (stable import identity; changing it later requires a contract amendment or C2 decision).

### D5. Staged accounts

New column `sec.Users.IsStaged BOOLEAN NOT NULL DEFAULT FALSE` with `CHECK (NOT IsStaged OR NOT CanLogin)`. Staged users have no active role assignment (service-enforced) and are rejected by the login gate. Provisioning clears `IsStaged`, assigns Role/Scope, and optionally enables login in one transaction.

### D6. Rate copy on transfer

- The choice is made **at completion**, the moment the destination profile is created, and is stored on the transfer request.
- Copy calls the existing `copy_driver_rates` service (`payroll/rates.py:2875`) unchanged, inside the completion transaction, with `effective_from = transfer EffectiveDate`, source = source profile, target = destination profile, `include_pay_rules = false`.
- **Repository result (resolves former H6): the existing engine supports a pending destination safely.** It never reads the target profile's status or effective window: it resolves the target only for its `BranchID` (`rates.py:2919-2926`); selects source Approved rates effective on `effective_from` (`2955-2978`); validates rate-type activation for the target branch *as of* `effective_from` (`3016-3055`); applies the finalized-period guard for the target branch at `effective_from` under the finalization advisory lock (`3083-3091`); applies the future-approved conflict guard on the target (`3095-3099`, vacuous for a new destination); and inserts rows with the target's `BranchID`/`DriverID` (`3129-3160`), satisfying the composite FK. It uses the caller's connection and raises `HTTPException` on any failure, so the whole completion rolls back. No rate code path assumes the driver is current (only a display read at `rates.py:2019`), so PendingApproval copies can be approved later by the normal flow. No engine change or extraction is needed.
- Pay rules are **not** copied by the transfer option in this refoundation; the existing explicit copy endpoint remains available afterwards.

### D7. Never-effective profiles (pending profiles cancelled by termination)

A pending profile of a terminated Employee is closed as **never-effective**: `DriverStatus = 'Terminated'`, `EffectiveTo = EffectiveFrom - 1`. Rows (and any copied rates) are preserved as evidence.

Exact representation (Phase 1):

- A plain `daterange(EffectiveFrom, EffectiveTo, '[]')` raises an error when `EffectiveTo < EffectiveFrom`, so it cannot be used directly. All range logic goes through one IMMUTABLE function:
  ```sql
  core.fn_DriverEffectiveRange(p_From date, p_To date) RETURNS daterange
    = CASE WHEN p_From IS NOT NULL AND p_To IS NOT NULL AND p_To < p_From
           THEN 'empty'::daterange
           ELSE daterange(p_From, p_To, '[]') END
  ```
  NULL bounds stay open; normal historical, current and pending windows keep inclusive `[EffectiveFrom, EffectiveTo]` semantics.
- The sentinel is constrained so it cannot appear by accident:
  `CHECK (EffectiveFrom IS NULL OR EffectiveTo IS NULL OR EffectiveTo >= EffectiveFrom OR (EffectiveTo = EffectiveFrom - 1 AND DriverStatus = 'Terminated'))`.
- Overlap constraint: `EXCLUDE USING gist (EmployeeID WITH =, core.fn_DriverEffectiveRange(EffectiveFrom, EffectiveTo) WITH &&)`. An empty range overlaps nothing, so a never-effective profile never conflicts.
- Resolver: `core.fn_EffectiveDriverProfile` tests `core.fn_DriverEffectiveRange(...) @> p_OnDate`; an empty range contains no date, so a never-effective profile is never returned.
- Payroll eligibility needs no change: its scalar predicates `EffectiveFrom <= d AND EffectiveTo >= d` are unsatisfiable when `EffectiveTo = EffectiveFrom - 1`.

### D8. Frontend shape

A single `/people` page with three tabs: **Workforce** (Employee-centered, default), **Access** (User accounts, including Users without an Employee), **Transfer Requests** (existing). Employee detail shows Driver profile history and an Access panel with explicit Create/Link actions. This reuses the existing tabbed page structure (`PeoplePage.tsx:560-583`) and gives external admin Users a home without making User the primary record.

---

## 3. Database Invariant Plan

| Invariant | Enforcement | Why | Phase |
|---|---|---|---|
| Driver and Employee same company | **DB**: unique `(EmployeeID, CompanyID)` on Employees + composite FK `Drivers(EmployeeID, CompanyID)` | Tenant integrity; cheap and exact | 1 |
| `Drivers.BranchID`, `EmployeeID`, `CompanyID` immutable | **DB** trigger (BEFORE UPDATE) + service rejects early | Contract §4.4 requires DB where expressible; closes the incomplete service guard | 1 |
| One Employee's profile windows never overlap / ≤1 effective per date | **DB** `EXCLUDE USING gist (EmployeeID WITH =, core.fn_DriverEffectiveRange(EffectiveFrom, EffectiveTo) WITH &&)` (`btree_gist` already used by `excl_DriverRates_no_date_overlap`); never-effective windows map to `'empty'` (D7) | Exact, prevents double-branch payroll | 1 |
| Never-effective sentinel only as `EffectiveTo = EffectiveFrom - 1` on a `Terminated` profile; no other inverted window | **DB** CHECK (D7) | Keeps the empty-range mapping unambiguous | 1 |
| Status value sets | **DB** CHECK on `DriverStatus` and `EmploymentStatus` | Closed sets per contract §4.6 | 1 |
| Historical profile protection (no reactivation, no window change after `Transferred`/`Terminated`) | **DB** trigger + service. Controlled workforce operations (termination shrink, pending cancellation) set a transaction-local `flussra.workforce_op` that the trigger recognises | Generic writes cannot reopen history; named operations can close it | 1 |
| `EmployeeType` has no runtime authority | **Service** (no reads/writes); column made nullable, later dropped | D3 | 2, 9 |
| `Users.EmployeeID` 0..1 per Employee | **DB** partial unique index on `EmployeeID WHERE EmployeeID IS NOT NULL` | Contract §5.4 | 3 |
| User↔Employee same company | **DB** composite FK `Users(EmployeeID, CompanyID)` → Employees + `CHECK (EmployeeID IS NULL OR CompanyID IS NOT NULL)` (sysadmin users may have NULL company, `0001:1161-1163`) | Tenant integrity | 3 |
| Staged ⇒ login disabled | **DB** CHECK | D5 | 3 |
| `DRIVER ⇔ Self`; Self has no Access BranchID | Semantic assignment invariant; exact enforcement is deferred to the Lead's P2b design pass | Current DRIVER is the only supported Self binding; branch scopes are invalid for DRIVER | P2b |
| DRIVER current identity (explicit same-company link and effective profile for current action) | Canonical User–Employee relationship and P1 effective-profile authority; no branch match | Time-dependent identity; missing/mismatched/no-current identity fails closed | P2b |
| Login-enabled ⇒ active Role/Scope | **Service** + existing login gate (`auth/service.py:127-150`) as backstop | Cross-table, state-dependent | 3 |
| COMPANY_OWNER dynamic permission catalogue | Valid active company-wide owner assignment required; malformed branch or Self scope grants no company-wide authority | Protected system role remains; scope validity is required | P2b |
| Self-compatible actions and DRIVER capability ceiling; overrides cannot widen resource scope | Semantic authorization invariant; exact implementation is for Lead design | Generic administrative actions remain invalid under Self | P2b |
| Self ownership is evaluated independently of branch access | Semantic authorization invariant; exact implementation is for Lead design | Self must not fabricate branch membership; use linked subject/resource identity | P2b |
| `Employee.BranchID` = current profile branch | Workforce service/projection authority (D1 part 3) | Date-dependent Workforce projection; no Access Self projection | 1 / 5 |
| One pending destination per Employee / no new transfer while one is pending | **Service** + existing active-request guard | Workflow rule | 5 |

No constraint is added for dormant `import.*` tables.

Demo data: constraints are created `VALID`. If the local development database violates them, it is reset (drop, `alembic upgrade head`, `ensure_dev_admin.py`); no repair SQL is written.

---

## Phase 1 — Workforce integrity foundation & effective-date resolver

### Goal
Make the workforce invariants true in the database and provide the single effective-date resolver every later phase uses.

### Why now
Every later phase relies on these invariants and on one definition of "current". Adding constraints before new write paths prevents building code on unenforced assumptions.

### In scope
- Migration `0072` (SQL file + Alembic wrapper, repository convention `migrations/sql/NNNN_*.sql`):
  - unique index `(EmployeeID, CompanyID)` on `core.Employees`; composite FK `core.Drivers(EmployeeID, CompanyID)`;
  - CHECK constraints on `Drivers.DriverStatus` (`Active, Inactive, OnLeave, Transferred, Terminated`) and `Employees.EmploymentStatus` (`Active, Inactive, Terminated`);
  - trigger blocking UPDATE of `Drivers.BranchID`, `EmployeeID`, `CompanyID`;
  - trigger protecting historical profiles (status and window frozen once `Transferred`/`Terminated`, except under `flussra.workforce_op` for named operations);
  - `core.fn_CompanyToday`, `core.fn_DriverEffectiveRange` and `core.fn_EffectiveDriverProfile` exactly as specified in D1/D7;
  - never-effective sentinel CHECK (D7);
  - EXCLUDE constraint for non-overlapping windows per Employee using `core.fn_DriverEffectiveRange`.
- `backend/app/workforce/` package skeleton: `clock.py` (`company_today` via `core.fn_CompanyToday`), `effective.py` (resolver wrappers: effective profile on date, current profile, pending profile).
- Changes forced by the new triggers (keep the repository coherent):
  - `transfer/service.py` completion: close the source in a single UPDATE (status + `TransferredToDriverID` + `EffectiveTo`) so the historical trigger permits it.
  - `admin/service.py: ensure_driver_profile`: remove the two direct branch-sync blocks (`2432-2459`, `2529-2555`); a branch mismatch now returns 422 "use Driver Transfer" (the function itself is retired in Phase 3).
  - `core/service.py: update_driver`: reject status changes on `Transferred`/`Terminated` profiles and to `Transferred`/`Terminated` with a 422.

### Out of scope
New endpoints; replacing status-based "current" readers (Phases 2–5); Users constraints (Phase 3).

### Database
Migration `0072` as above. Reset local dev DB if existing demo data violates constraints.

### Backend
`migrations/sql/0072_*.sql`, `migrations/versions/0072_*.py`, `backend/app/workforce/{__init__,clock,effective}.py`, `backend/app/transfer/service.py` (completion UPDATE shape), `backend/app/admin/service.py` (branch-sync removal), `backend/app/core/service.py` (reactivation guard), `backend/app/db/schema_guard.py` if it pins the expected head.

### Frontend
None.

### Legacy behavior retired
- Direct Driver branch mutation via role assignment (`ensure_driver_profile` branch sync).
- Reactivating `Transferred`/`Terminated` profiles through `PATCH /core/drivers`.

### Tests required
- New `backend/tests/test_workforce_integrity.py`: branch/employee/company immutability trigger; Driver/Employee company FK; status CHECKs; overlap exclusion (overlapping insert rejected, adjacent windows allowed, open-ended NULL bounds handled); never-effective representation (`fn_DriverEffectiveRange(d, d-1)` is empty; a never-effective profile coexists with any window; resolver never returns it for any date; sentinel rejected on a non-`Terminated` profile; `EffectiveTo < EffectiveFrom - 1` rejected); historical trigger (reactivation rejected; named-operation shrink allowed); resolver returns current / historical / pending correctly around a transfer boundary; `fn_CompanyToday` honours company timezone.
- Update `test_driver_transfer.py`: reassignment of a history-free driver to another branch now returns 422 (old "can be reassigned" expectation inverted).
- `test_core.py`: PATCH reactivation of Transferred rejected.
- Existing transfer workflow tests pass unchanged.

### Validation commands
From repo root: `backend\.venv\Scripts\python.exe -m alembic upgrade head`.
From `backend\`: `.\.venv\Scripts\python.exe -m pytest tests/test_workforce_integrity.py tests/test_driver_transfer.py tests/test_driver_transfer_workflow.py tests/test_core.py tests/test_people.py -q`; `.\.venv\Scripts\python.exe -m ruff check app tests`.

### Acceptance gate
- Migration upgrades cleanly on a fresh database and on the (reset if needed) dev DB.
- All listed constraints exist and are proven by tests.
- No runtime code path updates `Drivers.BranchID`.
- Transfer workflow tests green.

### Risks
Existing fixtures that create drivers with overlapping or odd windows; historical trigger interacting with any other code that updates transferred rows (search all `UPDATE core.drivers`).

### Expected size
MEDIUM — one focused migration plus three small forced code adjustments and a new test module.

---

## Phase 2 — Employee & Driver workforce write authority

### Goal
Provide the real Workforce write domain: Employees (Driver and non-Driver), initial and first Driver profiles, Driver-owned field edits, with audit and permissions, independent of Users.

### Why now
Access linking (Phase 3) needs existing Employees to link to; transfer and termination (Phases 5, 7) need a workforce service to extend.

### In scope
- Migration `0073`: catalog `employees.view`, `employees.manage`; drop `NOT NULL` on `core.Employees.EmployeeType` (D3; no CHECK, no new taxonomy).
- `backend/app/workforce/{router,service,schemas}.py`, mounted at `/workforce`:
  - `GET /workforce/employees`, `GET /workforce/employees/{id}` (includes current/pending/historical profiles via resolver, linked-account summary);
  - `POST /workforce/employees` — non-Driver, or Driver when `driver_profile` is supplied (Employee + initial profile, one transaction);
  - `PATCH /workforce/employees/{id}` — Employee-owned fields; `BranchID` only when no current/pending profile; `EmploymentStatus`/`TerminationDate` not editable for Driver Employees (termination is Phase 7); `HireDate` change rejected if it would exclude dates that already carry payroll lines;
  - `POST /workforce/employees/{id}/driver-profiles` — first profile for an existing Employee, created in `Employee.BranchID`.
- Runtime code no longer reads or writes `EmployeeType` (D3); `PersonSummary.employee_type` and the `employee_type` filter are removed.
- Profile creation always writes `EffectiveFrom` (defaults to `HireDate` or company today).
- `EmployeeKey` per D4; duplicate key/code → 409 (no uncaught IntegrityError).
- Audit rows in `audit.AuditLog` (`entityschema='core'`) for every workforce write, matching the admin audit convention (`admin/service.py:73-111`).
- `POST /core/drivers` becomes a thin alias of "Create Driver Employee" (same service; `employees.manage`), kept for existing fixtures.
- `PATCH /core/drivers/{id}` limited to Driver-owned, non-branch fields (`DriverCode`, `CDLNumber`, `ExternalDriverID`, status among `Active/Inactive/OnLeave` on a current or pending profile); Employee fields removed.
- `/core/people`: require `employees.view`; current profile via resolver; replace `employee_type` filtering with `driver_state` (`current`, `pending`, `none`).

### Out of scope
User linking; DRIVER security; transfer changes; termination.

### Database
Migration `0073` (permission catalog + `EmployeeType` nullable).

### Backend
New `backend/app/workforce/*`; `backend/app/main.py` (router include); `backend/app/core/{service,schemas,router}.py`; `backend/tests/conftest.py` (grant `employees.*` to test roles in place of `drivers.manage`).

### Frontend
`frontend/src/pages/people/TransferRequestsTab.tsx:318` — switch `employee_type=Driver` to `driver_state=current`; `frontend/src/types/core.ts` — add `driver_state`, remove `employee_type`. No other UI.

### Legacy behavior retired
- `drivers.manage` runtime checks.
- Employee fields editable through `PATCH /core/drivers`.
- `EmployeeType` as a runtime field, filter, or input.

### Tests required
New `backend/tests/test_workforce_employees.py`: scenario 1 (Staff Employee without User); scenario 2 (Driver Employee without User, one transaction — failure in profile insert leaves no Employee); first-profile addition; EmployeeKey generation/uniqueness/immutability; BranchID editability rules; audit rows; permission gates; retries (duplicate key → 409, no duplicate rows — scenario 18 workforce half); scenario 19 enabler (no User rows created). Update `test_core.py` for PATCH scope and alias behaviour.

### Validation commands
`pytest tests/test_workforce_employees.py tests/test_core.py tests/test_driver_transfer_workflow.py tests/test_branch_access.py -q`; ruff; alembic upgrade; frontend `npm run build` in `frontend\`.

### Acceptance gate
- Non-Driver and Driver Employees can be created with no User.
- No workforce endpoint creates or modifies `sec.*` rows.
- Every workforce write is audited.
- Frontend builds; Transfer Requests driver picker still lists current Drivers.

### Risks
Fixture churn from permission rename; HireDate guard needs a correct "earliest payroll date" query across draft/final lines.

### Expected size
LARGE — new domain package, two migrations' worth of catalog/constraint work, core endpoint reshaping and fixture updates.

---

## Phase 3 — Access decoupling & provisioning

**Status/reference:** This is the original P2a design record; P2a completed in PR #24 with migration `0074`. The implemented OwnDriverDataOnly representation is transitional legacy state. The amended contract and Unified Plan govern the P2b Self migration; the details below must not be treated as the target authorization model.

### Goal
Access can no longer touch Workforce; Users link explicitly to Employees; Access accounts are created atomically or explicitly staged.

### Why now
Needs Employees (Phase 2). Must precede DRIVER hardening (Phase 4) and transfer projection sync (Phase 5), which assume the link is the only identity path.

### In scope
- Migration `0074`: `Users.EmployeeID` partial unique index; composite FK `Users(EmployeeID, CompanyID)`; `CHECK (EmployeeID IS NULL OR CompanyID IS NOT NULL)`; `Users.IsStaged` + CHECK; `DRIVER ⇔ OwnDriverDataOnly` trigger on `sec.UserBranchRoles`.
- `admin/service.py`:
  - delete `ensure_driver_profile`, `_driver_has_payroll_history` and the call in `assign_company_role`;
  - exact role-code comparison (`rolecode == 'DRIVER'`);
  - Historical P2a implementation preconditions used the linked same-company Employee, resolver-backed current profile, and legacy ODA branch scope; ODA was rejected for non-DRIVER roles. P2b replaces that representation with Self and removes the branch match; see the amended contract;
  - reject archived roles (`isarchived`) in `assign_company_role`;
  - reject `DRIVER` as `replacement_company_role_id` in owner transfer (it is assigned with `AllCompanyBranches`);
  - The legacy role endpoint retains its P2a compatibility behavior pending P2b scope migration and P2c writer cleanup; it must not become an alternate route for branch/company authority under Self.
  - `PUT /admin/users/{id}/employee-link` and `DELETE /admin/users/{id}/employee-link` (explicit target; unlink rejected while the User holds DRIVER; relink requires prior unlink);
  - `POST /admin/users` = Create Access Account: User + optional `employee_id` + required `company_role_assignment` in one transaction, or `staged: true` (no assignment, `CanLogin=false`);
  - `POST /admin/users/{id}/provision`: assignment + clear `IsStaged` + optional enable login, one transaction;
  - `PATCH` `can_login=true` rejected when staged or no active assignment; revoking the last active assignment of a login-enabled User rejected.
- `auth/service.py`: login gate rejects staged users.
- `_fetch_driver_ids_for_users` / `get_user_driver_info` use the resolver (current profile) instead of status.

### Out of scope
Historically out of scope for P2a; DRIVER Self-compatible actions, capability ceiling and roster blocking now belong to P2b. Full People UI remains later work (Phase 8).

### Database
Migration `0074`. Reset dev DB if existing demo links violate uniqueness/company FK.

### Backend
`backend/app/admin/{service,router,schemas}.py`, `backend/app/auth/service.py`, `backend/tests/conftest.py` and fixtures that relied on DRIVER auto-creation (e.g. `test_driver_transfer.py`, `test_security_matrix.py`, `test_cp25_drivers_off.py`, `test_pay_rates.py` helpers): create the Employee/Driver via `/workforce`, link, then assign DRIVER.

### Frontend
Minimum to keep the app coherent with the new backend contract:
- `PeoplePage.tsx`: replace the 5-step `WizardModal` with a single-submit **Create access account** modal (user fields + role/scope, or "staged"); remove step-by-step persistence, `partialSuccess`, and the DRIVER "pay rates next" hint.
- The P2a UI represented DRIVER with legacy ODA. P2b updates the Access representation to generic Self with no branch, without redesigning the People UI.
- Link/unlink action on the User detail.
- `frontend/src/types/admin.ts` updates.

### Legacy behavior retired
DRIVER role creating Employee/Driver; implicit linking; `EMP-{UserID}` keys; substring role detection; wizard partial persistence and back/resubmit duplicate Users; legacy ODA as target authority; DRIVER with branch scope; archived-role assignment.

### Tests required
New `backend/tests/test_access_provisioning.py`: scenario 3 (existing Driver Employee gets a DRIVER User; no new Employee/Driver rows); scenario 4 (admin User without Employee); scenario 5 (staged User linked to a future Driver; cannot log in; DRIVER assignment rejected while profile pending); scenario 11 (DRIVER → other role leaves Workforce unchanged); scenario 12 (deactivation leaves Workforce unchanged); scenario 18 (retry of Create Access Account with same username → 422, no second User; atomic rollback when assignment invalid); cardinality/company FK tests; trigger tests for both assignment paths; stale role save cannot move branches. Update `test_people.py` (Driver scope rules), `test_driver_transfer.py` (reassignment tests become "role save never touches workforce").

### Validation commands
`pytest tests/test_access_provisioning.py tests/test_people.py tests/test_admin.py tests/test_company_roles.py tests/test_auth.py tests/test_security_matrix.py tests/test_driver_transfer.py -q`; ruff; alembic upgrade; `npm run build`; `npm run lint`; `node --test tests/peopleRolesAuthority.test.ts`.

### Acceptance gate
- `grep` finds no `ensure_driver_profile`, no substring `DRIVER` checks, no `EMP-` key generation.
- No Access endpoint writes `core.*`.
- A User can never be created login-enabled without an assignment.
- Old wizard removed; the People page still works for existing flows.

### Risks
Large fixture surface relied on DRIVER auto-creation; the DB trigger must not break existing legacy-role fixtures used for branch users.

### Expected size
LARGE — removes the central coupling, adds three Access operations, touches many fixtures and the People page modal.

---

## Phase 4 — DRIVER self-service security boundary

**Current authority:** This older phase maps to P2b only where consistent with the amended locked contract and Unified Plan. The earlier ODA branch-based mechanism is superseded.

### Goal

Enforce generic `Self` resource scope for the current DRIVER role. Self is not branch access; DRIVER is the only currently supported Self binding, and future bindings require explicit policy.

### Semantic requirements

- DRIVER requires Self; `SpecificBranch` and `AllCompanyBranches` are invalid for DRIVER. Self has no Access BranchID and grants no branch/company-wide or generic operational/administrative authority. Explicitly designed own-resource operations remain possible after canonical ownership is proven.
- Current Driver identity derives from the explicit same-company User.EmployeeID link and the canonical effective Driver profile for the operation's relevant date. History is available only for that linked Employee where the endpoint contract allows it. Missing link, company mismatch, pending/no-current profile for a current action, or terminated/no-current identity fails closed.
- A valid provisioned Self-only account must be representable through login and `/auth/me` without a fabricated branch-access row. Transfer/termination do not mutate an Access Self assignment.
- Permission overrides may expand actions only within the established resource set; they do not create scope, make generic actions Self-compatible, bypass ownership checks, or exceed the DRIVER capability ceiling. Mixed DRIVER/administrative rows must not create a privilege bypass. The system must not assume only one assignment row can exist, and P2b does not define mixed-mode product behavior.
- COMPANY_OWNER's dynamic permission shortcut requires a valid active company-wide owner assignment. Malformed branch-scoped or Self-scoped owner rows are not company-wide authority.
- Every registered application route has explicit reviewable classification for authentication, action/permission, resource scope, Self denial/allowance, ownership relation when Self is allowed, and relevant company/branch policy. A new or unclassified route fails automated tests.

### Negative authorization evidence

For relevant Self/DRIVER routes, tests prove denial of another Driver's data, branch-roster leakage, generic administrative mutation, missing User.EmployeeID, company mismatch, no effective profile for a current action, a pending profile treated as current, terminated/no-current identity, and override-based resource expansion. Company Owner malformed-scope and mixed-authority bypass cases are also covered.

### Mechanism boundary

This plan does not select the SQL, API, policy, route-inventory, or override-storage mechanism. The Lead locks those remaining architectural choices in a separate P2b design pass before application implementation. No new general authorization framework or mixed-role UX is authorized here.

### Out of scope

Removing `ensure_driver_profile` or the remaining Access→Workforce mutations (P2c); People UI redesign; Compensation work; and specific future Driver self-service commands.

---

## Phase 5 — Effective-dated transfer & projection sync

### Goal
Transfers never make the destination current early; every "current" consumer resolves by date; projections follow the effective date via D1.

### Why now
Needs the resolver (1), link-only identity (3) and the Self authorization semantics established before this transfer work. Rate copy (6) and termination (7) extend the transfer/pending model built here.

### In scope
- Migration `0076` may add `core.WorkforceProjectionSync(CompanyID PK, SyncedThroughDate DATE NOT NULL)` for the Workforce `Employee.BranchID` projection. No ODA branch resolver or Access Self-to-branch projection is part of this transfer phase.
- Branch-scoped workforce reads (`/core/people`, `/core/drivers`, `/workforce/employees`) scope Driver Employees by the resolver-derived current (else pending) profile branch, not by stored `Employee.BranchID`.
- Workforce projection reconciliation maintains Driver Employees' `Employee.BranchID` from the effective profile as required by the contract. It never updates an Access Self assignment or creates Access branch membership.
- Request-gated daily reconciliation dependency attached to every authenticated router through `include_router(..., dependencies=[...])` in `backend/app/main.py` (and to the auth `me`/login paths), so no Payroll Setup file changes; operator script `backend/scripts/sync_workforce_projections.py`.
- Transfer completion rework (`transfer/service.py`):
  - `SELECT … FOR UPDATE` on the request and the source profile;
  - source must be the Employee's current profile and `EffectiveDate > source.EffectiveFrom`; reject if the Employee already has a pending profile;
  - create destination (`EffectiveFrom = EffectiveDate`, lineage), close source (single UPDATE), complete request;
  - **do not** change Access Self scope or create Access branch membership. The Employee's Workforce `BranchID` projection follows its existing effective-date authority.
- New transfer requests rejected while a completed transfer's destination is pending; source-branch resolution (`_get_driver_source_branch`, `_assert_driver_belongs_to_branch`) uses the resolver.
- All remaining status-based current lookups switched to the resolver: `core/service.py` people/driver reads, admin driver lookups, dashboard counts that mean "current drivers".

### Out of scope
Rate copy; termination; changes to payroll eligibility queries (already window-based); Self authorization implementation (owned by P2b).

### Database
Migration `0076` (if required for the Workforce projection-sync table only; no Access ODA function or Self branch projection).

### Backend
`migrations/sql/0076_*.sql`, `backend/app/transfer/service.py`, `backend/app/workforce/{projections,effective}.py`, `backend/app/dependencies.py` (reconciliation dependency), `backend/app/main.py` (router wiring), `backend/app/auth/service.py`, `backend/app/payroll/immutable_evidence.py`, `backend/app/core/service.py`, `backend/app/admin/service.py`, `backend/app/dashboard/service.py`, `backend/scripts/sync_workforce_projections.py`.

### Frontend
`TransferRequestsTab.tsx`: show "Completed — effective {date}" and pending destination state; Employee branch display from API (resolver-based).

### Legacy behavior retired
Immediate `Employee.BranchID` move at completion; status-based "current"; completion without row locks.

### Tests required
New `backend/tests/test_transfer_effective_dating.py` (Workforce sync uses explicit `as_of`): scenario 9 verifies `Employee.BranchID`, the effective-profile resolver, and `/core/people` remain on the source before the effective date and follow the destination on that date; scenario 7 verifies A→B no copy; scenario 10 verifies source rates/lines/snapshots stay unchanged; scenario 17 verifies an admin user's explicit branch scope does not follow an Employee transfer; transfer Self identity follows the linked Employee's effective Driver profile on the relevant date without changing Access assignment or producing a branch row. Also cover concurrency (two completions → one succeeds), pending-destination blocking, retroactive completion, and Workforce projection reconciliation idempotence. Existing transfer workflow effective-date tests must pass unchanged.

### Validation commands
`pytest tests/test_transfer_effective_dating.py tests/test_driver_transfer_workflow.py tests/test_cp2e_eligibility_snapshot.py tests/test_day_grid.py tests/test_security_matrix.py tests/test_dashboard.py -q`; ruff; alembic upgrade; `npm run build`.

### Acceptance gate
- No runtime query decides "current" by status alone (grep for `NOT IN ('Transferred'` outside payroll eligibility returns nothing).
- Self authorization remains independent of Access branch membership; branch-access views do not fabricate membership for Self. Effective Driver identity follows the linked Employee and operation date.
- Future-dated transfer leaves all current views on the source until the effective date.
- Payroll eligibility and day-grid tests unchanged and green.

### Risks
The Workforce projection reconciliation adds a write in its own transaction and must be lock-safe; dashboard/report counts may change meaning; company-timezone edges remain. Self authorization does not depend on Workforce branch-projection synchronization.

### Expected size
LARGE — effective-date and Workforce projection semantics across current-profile consumers, with no Access Self projection.

---

## Phase 6 — Transfer rate copy

### Goal
Transfer completion supports the explicit copy/no-copy rate choice with copy semantics only.

### Why now
Requires the pending-destination completion flow (Phase 5).

### In scope
- Migration `0077`: `core.DriverTransferRequests.CopyRates BOOLEAN NULL` (NULL until completion) and `RatesCopiedCount INTEGER NULL`.
- `GET /driver-transfers/{id}/rate-copy-preview`: source Approved rates effective on `EffectiveDate` (existing engine query), with per-row copyability for the target branch (same validation as the engine) — read-only.
- `POST /driver-transfers/{id}/complete` body `{ copy_rates: bool }` (required — no default):
  - `false`: destination gets no rates;
  - `true`: after destination insert, call the existing `copy_driver_rates(target_driver_id=destination, source_driver_id=source, company_id, user_id, CopyRatesRequest(effective_from=EffectiveDate, include_pay_rules=False), db)` on the completion connection — unchanged, per D6 (it already supports a pending destination); copied rows are new destination records; `AllowSelfApproval` decides Approved vs PendingApproval exactly as today; `RatesCopiedCount` records the result;
  - any copy validation failure (e.g. rate type not active in target branch, finalized-period guard) fails the whole completion — no partial copy; the user may complete with `copy_rates=false` instead.
- Permissions: completion as today (`drivers.edit` on source or target) **plus** `payrates.edit` on both branches when `copy_rates=true` (engine rule).

### Out of scope
Pay-rule copy via the transfer option; general DriverRates redesign.

### Database
Migration `0077`.

### Backend
`backend/app/transfer/{service,router,schemas}.py`. `backend/app/payroll/rates.py` is not modified (the preview reuses its source-rate and target-branch validation queries through a read-only helper added in the transfer module or a small shared read function, without changing `copy_driver_rates`).

### Frontend
`TransferRequestsTab.tsx`: completion dialog with explicit Yes/No choice and preview list; show copy result on the request.

### Legacy behavior retired
Completion without an explicit rate decision.

### Tests required
New `backend/tests/test_transfer_rate_copy.py`: scenario 7 (no copy → zero destination rates); scenario 8 with a **future** `EffectiveDate` (destination pending at completion; copied rows have destination `DriverID` and target `BranchID`, `EffectiveFrom = EffectiveDate`; source rows byte-identical before/after); copy with `AllowSelfApproval=false` creates PendingApproval rows that can later be approved while the destination is still pending; retroactive `EffectiveDate` inside a finalized target period → whole completion rejected; invalid rate type for target branch → completion rolled back, no destination profile; advanced/tiered source rate → rejected atomically (engine rule); permissions; preview accuracy. Existing `test_pay_rates.py` copy tests unchanged.

### Validation commands
`pytest tests/test_transfer_rate_copy.py tests/test_pay_rates.py tests/test_transfer_effective_dating.py -q`; ruff; alembic upgrade; `npm run build`.

### Acceptance gate
- Completion requires an explicit choice.
- Source rates never change (asserted by test).
- Copy is all-or-nothing within completion.

### Risks
The engine performs its own permission checks and rejects Driver self-service users from rate-copy administration; the completion flow must surface its 403/422 unchanged so the user can retry with `copy_rates=false`. Preview and engine validation must stay in sync (the preview is advisory; the engine is authoritative).

### Expected size
MEDIUM — reuses the existing engine unchanged; main work is the completion integration, preview and UI choice.

---

## Phase 7 — Driver Employee termination

### Goal
One Workforce operation terminates a Driver Employee and closes the current profile atomically, without touching history.

### Why now
Needs pending-profile and transfer semantics (Phase 5) to close pending profiles and in-flight requests correctly.

### In scope
- `POST /workforce/employees/{id}/terminate { termination_date: D, reason }` (`employees.manage`), one transaction under `flussra.workforce_op='terminate'`:
  - Employee: `EmploymentStatus='Terminated'`, `TerminationDate=D`;
  - the profile effective on D: `DriverStatus='Terminated'`, `EffectiveTo=D` (shrinking a `Transferred` source's `EffectiveTo` when D precedes a pending transfer);
  - pending profiles (`EffectiveFrom > D`): closed never-effective per D7 — `DriverStatus='Terminated'`, `EffectiveTo = EffectiveFrom - 1` (permitted by the Phase 1 sentinel CHECK, treated as an empty range by the overlap constraint and never returned by the resolver);
  - non-terminal transfer requests: `Cancelled` with system reason;
  - projections synced if D ≤ today;
  - audit row.
- Guards: reject if D is before the profile's `EffectiveFrom`; reject if any locked/finalized payroll evidence for the Employee's profiles is dated after D (termination may not cut off paid days); reject for non-Driver Employees in this endpoint only if they have no profile (plain Employee termination uses the same endpoint without profile steps).
- Access records and Self scope are untouched; after termination a current-profile Driver action fails closed when the linked Employee has no effective profile.

### Out of scope
Rehire; Driver → non-Driver transition; changes to eligibility snapshots.

### Database
None (uses Phase 1 triggers and constraints).

### Backend
`backend/app/workforce/{service,router,schemas}.py`, `backend/app/transfer/service.py` (cancel helper).

### Frontend
None in this phase (UI in Phase 8).

### Legacy behavior retired
Termination via `PATCH /core/drivers` Employee fields (already removed in Phase 2 — confirm no path remains).

### Tests required
New `backend/tests/test_workforce_termination.py`: scenario 13 (atomic close; failure in any step leaves nothing changed); scenario 14 (linked DRIVER user gets 403 on own-driver endpoints after D); pending profile cancelled as never-effective (resolver returns nothing for any date; payroll eligibility never includes it; overlap constraint accepts it alongside the closed source); in-flight request cancelled; termination before pending transfer effective date; finalized-evidence guard; prior profiles, rates, lines and snapshots unchanged; payroll eligibility for dates ≤ D unchanged in an open period (live and snapshot paths).

### Validation commands
`pytest tests/test_workforce_termination.py tests/test_cp2e_eligibility_snapshot.py tests/test_day_grid.py tests/test_security_matrix.py -q`; ruff.

### Acceptance gate
- Termination is a single committed operation or none.
- No historical row changes except the closed current profile and cancelled pending rows.
- Eligibility for pre-termination dates is unchanged.

### Risks
Interaction with already-snapshotted open periods (snapshot rows keep pre-termination values) — see Hard Stop H7.

### Expected size
MEDIUM — one operation, but several edge paths (pending, transfers, finalized guard).

---

## Phase 8 — Employee-centered People & Access frontend

### Goal
Replace the User-centered People page with the Employee-centered Workforce view and a separate Access view (D8).

### Why now
All backend authority exists (Phases 2–7); the UI can bind to final contracts once.

### In scope
- `/people` tabs: **Workforce** (default), **Access**, **Transfer Requests**.
- Workforce: Employee list (search, status, branch, driver state); detail with Employee fields, current/pending/historical Driver profiles, Pay Rates link for current profile, Access panel (linked account state: none / staged / provisioned / disabled; actions Create access account, Link existing account, Unlink); actions Add Employee (single submit, optional initial Driver profile), Edit Employee, Add Driver profile, Terminate (confirmation), Request transfer (existing flow).
- Access: User list (including Users without Employee), detail with login state, role/scope, overrides, link state, staged provisioning action, reset password, owner transfer.
- Clear separation of labels: "Employment status" vs "Login".
- Remove `PersonDetail` role-based Driver section and the "Drivers module" gap text.
- Extract pure view-model helpers (e.g. `peopleViewModel.ts`) for testability.
- Frontend types for workforce endpoints (`frontend/src/types/workforce.ts`), `permissions.ts` helpers `canViewEmployees` / `canManageEmployees`.

### Out of scope
Driver portal; visual redesign beyond the page; Pay Rates page redesign.

### Database
None.

### Backend
None expected; any gap found is a small additive read field.

### Frontend
`frontend/src/pages/people/PeoplePage.tsx` (replaced/split into `WorkforceTab.tsx`, `AccessTab.tsx` and modals), `TransferRequestsTab.tsx`, `frontend/src/lib/permissions.ts`, `frontend/src/types/*`, `frontend/src/App.tsx` route guard for `/people` (visible with `employees.view` or `users.view`).

### Legacy behavior retired
User list as People; remaining wizard remnants; Driver visibility by role code.

### Tests required
`frontend/tests/peopleWorkforceViewModel.test.ts` (driver state, access state, action availability by permission); extend `peopleRolesAuthority.test.ts` for new helpers. Browser smoke (preview) of scenarios 1–8, 11–13 through the UI.

### Validation commands
In `frontend\`: `npm run lint`; `npm run build`; `node --test tests/peopleWorkforceViewModel.test.ts tests/peopleRolesAuthority.test.ts tests/pageVisibilityAuthority.test.ts`.

### Acceptance gate
- No UI path persists a partial multi-step operation.
- Workforce actions work for a user with `employees.*` but no `users.*`, and vice versa.
- Browser smoke recorded for the listed scenarios.

### Risks
Scope creep into visual polish; permission-combination edge cases in visibility.

### Expected size
LARGE — full replacement of a 1,256-line page and its modals.

---

## Phase 9 — Legacy cleanup, clean reset/reseed, closure validation

### Goal
Remove obsolete code, reset development data to a clean baseline, prove the refoundation end to end, and close it.

### Why now
Last; depends on every prior phase.

### In scope
- Cleanup (verify each is unreferenced first): `drivers.manage` references (`dashboard/service.py:73`, docs), `/core/drivers` POST alias if fixtures have moved to `/workforce` (otherwise keep and document), dead helpers left by Phases 3–5, stale OpenAPI descriptions (e.g. `admin/router.py:398-402`), stale comments mentioning auto-created driver profiles, `PayRatesPage` `driverUserId` path if replaced.
- `EmployeeType` retirement (D3): prove no application reference remains (`grep -ri employeetype backend/app frontend/src` empty), update the raw-SQL test fixtures that insert it, then drop the column in migration `0078`.
- Keep: dormant `import.*` tables (no concrete reason to drop); legacy `/admin/users/{id}/roles` endpoint (fixture use, trigger-protected).
- Reset/reseed:
  - drop and recreate the development database; `alembic upgrade head`;
  - update `backend/scripts/ensure_dev_admin.py` (no `drivers.manage`; owner permissions dynamic);
  - new `backend/scripts/seed_workforce_demo.py`: company `DEMO`; branches HQ + one second branch; roles `COMPANY_OWNER`, `DRIVER`, one custom "Payroll Manager" role with company scope; owner admin User (no Employee); one Staff Employee; one Driver Employee with a current profile and a small approved rate set; one linked DRIVER User for that Driver; one future Driver with a pending profile and a staged linked User.
- Documentation: mark this plan CLOSED with phase results; update `docs/architecture/C1_KNOWN_PRODUCT_AND_WORKFLOW_GAPS.md` §4; note in `POST_P6D_CLEANUP_AND_PRODUCT_READINESS_PLAN.md` that C2 is unblocked.
- Independent read-only audit against the contract §19 acceptance rules.

### Out of scope
Any new feature.

### Database
Migration `0078`: drop `core.Employees.EmployeeType` (after the reference proof above); optionally remove the now-unused `drivers.manage` catalog row if nothing references it. Development reset as described.

### Backend
Cleanup files as listed; scripts.

### Frontend
Cleanup of unused imports/components/styles.

### Legacy behavior retired
Everything listed in contract §16 confirmed absent.

### Tests required
Full backend suite; full frontend node tests; security matrix; scenario checklist below executed.

### Validation commands
From repo root: `backend\.venv\Scripts\python.exe -m alembic upgrade head` on a fresh database.
From `backend\`: `.\.venv\Scripts\python.exe -m pytest -q`; `.\.venv\Scripts\python.exe -m ruff check app tests`; `.\.venv\Scripts\python.exe scripts\ensure_dev_admin.py`; `.\.venv\Scripts\python.exe scripts\seed_workforce_demo.py`.
From `frontend\`: `npm run lint`; `npm run build`; `node --test tests/*.test.ts`.
Browser smoke via `.claude/launch.json` dev servers.

### Acceptance gate
- Full backend and frontend suites green; build and lint clean; fresh-DB migration clean.
- Every scenario in §4 passes (test or recorded smoke).
- Independent audit reports no contract violation.
- Worktree clean; plan marked CLOSED.

### Risks
Removing a helper still referenced by a rarely-run path; seed drift from test fixtures.

### Expected size
MEDIUM — broad but mechanical, dominated by validation.

---

## 4. Scenario Coverage

| # | Scenario | Phase(s) | Test |
|---|---|---|---|
| 1 | Office Staff Employee, no User | 2, 8 | `test_workforce_employees.py`; UI smoke |
| 2 | Driver Employee, no User | 2, 8 | `test_workforce_employees.py`; UI smoke |
| 3 | Existing Driver gets DRIVER User, no duplicates | 3, 8 | `test_access_provisioning.py` |
| 4 | External/admin User, no Employee | 3, 8 | `test_access_provisioning.py` |
| 5 | Future Driver + staged User | 2, 3 | `test_access_provisioning.py` |
| 6 | DRIVER active only when profile current | 3, 5 | `test_access_provisioning.py`, `test_transfer_effective_dating.py` |
| 7 | Transfer A→B, no copy | 5, 6 | `test_transfer_rate_copy.py` |
| 8 | Transfer A→B, explicit copy | 6 | `test_transfer_rate_copy.py` |
| 9 | Future transfer no early branch change | 5 | `test_transfer_effective_dating.py` |
| 10 | Source rates/payroll unchanged | 5, 6 | `test_transfer_effective_dating.py`, `test_transfer_rate_copy.py`, existing workflow tests |
| 11 | DRIVER → other role, Workforce unchanged | 3 | `test_access_provisioning.py` |
| 12 | User deactivation, Workforce unchanged | 3 | `test_access_provisioning.py` |
| 13 | Terminate atomically | 7 | `test_workforce_termination.py` |
| 14 | DRIVER after termination fails closed | 4, 7 | `test_workforce_termination.py` |
| 15 | DRIVER cannot browse roster | 4 | `test_security_matrix.py` |
| 16 | DRIVER cannot mutate own rates/payroll/workforce | 4 | `test_security_matrix.py` |
| 17 | Admin access does not follow transfer | 5 | `test_transfer_effective_dating.py` |
| 18 | Retry/back-navigation no duplicates | 2, 3, 8 | `test_workforce_employees.py`, `test_access_provisioning.py`, view-model test |
| 19 | Future Import can create workforce without logins | 2 | `test_workforce_employees.py` (no `sec.*` writes) |

---

## 5. Dependency Graph

```text
Phase 1  Workforce integrity + resolver
   ↓
Phase 2  Employee & Driver write authority
   ↓
Phase 3  Access decoupling & provisioning
   ↓
Phase 4  DRIVER self-service boundary
   ↓
Phase 5  Effective-dated transfer & projection sync   (also needs Phase 1 resolver)
   ├──────────────┐
   ↓              ↓
Phase 6        Phase 7
Rate copy      Termination
   └──────┬───────┘
          ↓
Phase 8  Employee-centered People & Access frontend   (needs 2, 3, 5, 6, 7)
          ↓
Phase 9  Cleanup, reset/reseed, closure
```

Phases 6 and 7 are independent of each other and may be implemented in either order.

---

## 6. Phase Size Summary

| Phase | Name | Size | Backend effort | Frontend effort | Migration risk | Security risk | Depends on |
|---|---|---|---|---|---|---|---|
| 1 | Workforce integrity + resolver | MEDIUM | Medium | Low | High | Low | — |
| 2 | Employee & Driver write authority | LARGE | High | Low | Medium | Medium | 1 |
| 3 | Access decoupling & provisioning | LARGE | High | Medium | Medium | High | 2 |
| 4 | DRIVER self-service boundary | MEDIUM | Medium | Low | Low | High | 3 |
| 5 | Effective-dated transfer & projection sync | LARGE | High | Low | Low | Medium | 1, 3, 4 |
| 6 | Transfer rate copy | MEDIUM | Medium | Medium | Low | Medium | 5 |
| 7 | Driver Employee termination | MEDIUM | Medium | Low | Low | Medium | 5 |
| 8 | Employee-centered People & Access UI | LARGE | Low | High | Low | Medium | 2, 3, 5, 6, 7 |
| 9 | Cleanup, reset/reseed, closure | MEDIUM | Medium | Low | Medium | Low | 1–8 |

---

## 7. PR / Review Strategy

One PR per phase. No phases are combined: each is either security-critical or large enough that combining would produce an unreviewable diff.

| PR | Contents | Review focus | Required tests | Independent review |
|---|---|---|---|---|
| PR-1 | Phase 1 | Constraint correctness vs. existing history; trigger escape hatch scope; resolver semantics | Phase 1 list + transfer workflow | **Yes** |
| PR-2 | Phase 2 | Workforce/Access separation; audit; permission gates; HireDate guard | Phase 2 list | Recommended |
| PR-3 | Phase 3 | No residual workforce side effects; DRIVER preconditions; staged/provision atomicity; trigger coverage of legacy path | Phase 3 list + security matrix | **Yes** |
| PR-4 | Phase 4 / P2b | Generic Self identity; resource-scope ceiling; route inventory completeness; override and Company Owner scope safety | Security matrix + focused authorization routes | **Yes** |
| PR-5 | Phase 5 | "Current" semantics and Workforce `Employee.BranchID` projection; transfer does not rewrite Access Self scope; reconciliation safety (locks, transactions, timezone); completion concurrency | Phase 5 list + eligibility/day-grid + transfer identity tests | **Yes** |
| PR-6 | Phase 6 | Copy-not-move; atomicity; `copy_driver_rates` used unchanged | Phase 6 list + `test_pay_rates.py` | Recommended |
| PR-7 | Phase 7 | Atomic close; history untouched; finalized guard; pending/transfer handling | Phase 7 list + eligibility | **Yes** |
| PR-8 | Phase 8 | No partial persistence; permission-based visibility; contract §10 compliance | Frontend tests + browser smoke | Recommended |
| PR-9 | Phase 9 | Cleanup safety; reset reproducibility; closure evidence | Full suites + scenarios | **Yes** (closure audit) |

Each PR uses its own branch from `main` after the previous PR merges; no PR is opened until its phase's acceptance gate passes locally.

---

## 8. Hard Stop / Replan Conditions

Stop implementation and revisit the plan (or, if a locked rule is at stake, propose a contract amendment) when:

- **H1.** The non-overlap exclusion or historical-profile trigger rejects rows that existing payroll, snapshot, or transfer code legitimately writes (for example a transfer path that needs to rewrite a closed window), i.e. an invariant conflicts with real history-writing behaviour.
- **H2.** Resolver-based "current" disagrees with payroll eligibility (`payroll/eligibility.py`) for transferred or terminated profiles on any date, meaning the chosen mechanism cannot preserve source eligibility.
- **H3.** Self cannot be represented or enforced without creating fake branch membership, or the canonical User–Employee/effective-profile subject relationship cannot be applied consistently.
- **H4.** Permission overrides cannot preserve the established resource set, a registered-route inventory cannot be made mechanically complete, or a current Driver behavior demonstrably requires authority beyond the locked self-service ceiling. Return for Lead review; do not widen scope silently.
- **H5.** A malformed COMPANY_OWNER assignment cannot be prevented from turning the dynamic permission shortcut into unrestricted company authority.
- **H6.** The DRIVER self-service capability policy or required current behavior is contradictory and cannot be represented within Self without a new product decision.
- **H7.** Removed. The rate-copy engine's behaviour for a pending destination was resolved from the repository (D6): it is supported without engine changes.
- **H8.** Terminating on date D cannot keep pre-termination eligibility intact or cannot prevent post-D work entry without modifying eligibility snapshot semantics for open periods.
- **H9.** The composite `Users(EmployeeID, CompanyID)` FK or the staged-account rule conflicts with system/sysadmin accounts in a way that requires changing a contract rule.

---

# Original Execution Order (historical; current sequencing is in the Unified Plan)

1. Phase 1 — Workforce integrity foundation & effective-date resolver
2. Phase 2 — Employee & Driver workforce write authority
3. Phase 3 — Access decoupling & provisioning
4. Phase 4 — DRIVER self-service security boundary
5. Phase 5 — Effective-dated transfer & projection sync
6. Phase 6 — Transfer rate copy
7. Phase 7 — Driver Employee termination
8. Phase 8 — Employee-centered People & Access frontend
9. Phase 9 — Legacy cleanup, clean reset/reseed, closure validation

# Original Phase 1 Start Condition (historical)

These were the preconditions recorded for the original plan before Phase 1 began:

- the locked contract (`a38c9306c51f00957719c22a1508e7e84a32503c`) and this master plan are merged into `main`;
- `main` equals `origin/main` and the worktree is clean;
- the repository migration head is `0071` and the local development database is upgraded to `0071`;
- the baseline targeted suites pass on that database (`test_people`, `test_driver_transfer`, `test_driver_transfer_workflow`, `test_core`, `test_security_matrix`, `test_pay_rates`);
- Phase 1 is implemented on its own branch from `main`.

# Completion Condition

The People / Workforce / Access refoundation is closed, and C2 Import may resume, when:

- Phases 1–9 are merged into `main`, each with its acceptance gate met and required independent reviews recorded;
- the full backend suite, frontend tests, frontend build and lint, and Ruff pass on a freshly migrated database;
- all 19 scenarios in §4 pass;
- every acceptance rule in contract §19 is demonstrated and every behaviour in contract §16 is confirmed absent;
- the development database has been reset and reseeded from `seed_workforce_demo.py`;
- an independent read-only audit confirms compliance with the locked contract;
- this plan's status is updated to CLOSED and `POST_P6D_CLEANUP_AND_PRODUCT_READINESS_PLAN.md` records C2 as unblocked.
