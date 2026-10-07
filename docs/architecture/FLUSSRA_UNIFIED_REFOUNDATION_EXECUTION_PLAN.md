# FLUSSRA UNIFIED REFOUNDATION EXECUTION PLAN

**Status:** Phase 1, Phase 2 (P2a–P2c), P3a and G0 (G0.1–G0.6) are CLOSED/merged. P3b — Target Compensation schema/invariants — is the current implementation work unit.<br>
**Execution authority:** P3b is current. P3c, P4, P5, P6, P7 and P8 have not started and remain gated by their stated prerequisites.<br>
**Repository baseline:** The accepted post-G0 baseline is `main` at the PR #45 merge, `f2937bfdcbe1266a00716905a8712f3fe29baff2`, with migration head `0081`. P3b advances the head to `0082`.<br>
**Compensation status:** P2c, P3a and G0 are closed. Legacy compensation remains the sole operational payroll authority; P3b adds a dormant target persistence model beside it. Required merge checks are enforced on `main`.<br>
**G0 closure evidence:** G0.1 PR #34 (migration `0077`); G0.2 PRs #35–#39 (validation authority, CI); G0.3 PR #40 (currency concurrency lock ownership); G0.4 PRs #41–#43 (generic Period Pay retirement, legacy period source retirement, calculation contract; migrations `0078`, `0079`); G0.5 PR #44 (predecessor custom Pay Item writers; migration `0080`); G0.6 PR #45 (Pay Profile family; migration `0081`).<br>
**P3a validation:** Full backend run reported by the user: 4 failed, 3312 passed, 3 skipped in 1143.67s. All four failures were stale pre-P3a Alembic-head assertions; they were corrected and the four focused reruns passed. The full suite was not rerun after correction.<br>
**Original planning baseline (historical provenance):** authored against `docs/people-workforce-access-contract-lock` at `a38c9306c51f00957719c22a1508e7e84a32503c`, migration-script head `0071`. The §2 inventory and the phase *Why now* text describe that baseline, not current execution status.  
**Phase 1 closure evidence:** P1a PR #19 (`6d3276e`, migration `0072`), P1b PR #20 (`3996089`, migration `0073`), P1c PR #22 (merge `3d78630`, implementation `bda65e2`, no migration).
**P2a closure evidence:** PR #24 merged to `main` as `d7dc63a4e1ceb461e4e1fcbb2abe3c4fc449abea`; migration `0074`. P2a established an explicit optional same-company User–Employee link, staged login-disabled accounts, atomic provisioning and staged flow, staged/login lifecycle enforcement, exact DRIVER provisioning preconditions, final-role staging lifecycle, race-safe archived-role assignment protection, and minimal People wizard compatibility. P2a did not remove the legacy Access→Workforce role-assignment side effect (P2c) or complete broad DRIVER/Self authorization hardening and route inventory (then P2b).
**P2b closure evidence:** PR #26 merged to `main` as `819fb9ae4a59b2c4835778c45e4f8bc49fdc0f45`. P2b established generic Self authorization with the current-product DRIVER ⇔ Self binding, effective User→Employee→Driver ownership, the generic DRIVER/Self capability ceiling, `/auth/me` Self projection, and the mechanically reviewable mounted-route authorization inventory. P2b left the legacy Access→Workforce role-assignment side effect for P2c, which removed it under PR #28.
**Intervening maintenance:** Test Hygiene Phase 1 closed/merged via PR #25, adding shared backend test builders and improving fixture isolation/maintainability. It changed no production behavior or migrations and is not a refoundation phase.
**Authority:** The locked [People / Workforce / Access contract](PEOPLE_WORKFORCE_ACCESS_ARCHITECTURE_CONTRACT.md) owns its domain rules. The [People master plan](PEOPLE_AND_ACCESS_REFOUNDATION_MASTER_PLAN.md) and [Compensation master plan](FLUSSRA_COMPENSATION_MODERNIZATION_MASTER_PLAN.md) retain their detailed product decisions. This document supersedes their separate execution orders. In particular, People-plan D6's proposed call to legacy `copy_driver_rates` is superseded by the target-assignment transfer copy required here. An implementation must reconcile any other genuine contract conflict before changing a locked decision.

## 1. Executive Verdict

Keep Company/Branch, the now-canonical Payroll Setup/period creation authority, period days and eligibility principles, payroll review/finalization and immutable snapshot machinery, BonusEvent, DriverPayRule, StatusKey/DayEntryState, audit, locks, and sound authentication primitives. Rebuild the Workforce write and effective-profile boundary, Access provisioning/linking and DRIVER authorization boundary, and Compensation definition/assignment/value authority. Adapt existing payroll orchestration and historical read models to consume target identities. Delete obsolete People write side effects and legacy compensation routing only after target paths pass cutover gates.

The two older execution orders would build a legacy DriverRate transfer-copy feature just before replacing DriverRates; the compensation order also assumes a pre-0071 period baseline. A unified sequence avoids that duplicate work, uses the one canonical period creator, and keeps each old authority live only until its successor is tested. **Eight macro phases** remain. **Phase 1 and Phase 2 (P2a–P2c) are closed and merged; P3a is closed via PR #30; G0 (G0.1–G0.6) is closed, ending with PR #45; P3b is the current implementation work unit.** P2b completed the generic `Self` authorization model with the current-product DRIVER ⇔ Self binding. Test Hygiene Phase 1 is an intervening maintenance milestone, not a refoundation phase. The hardest remaining phases are **Phase 4** (period/runtime/evidence authority cutover) and **Phase 5** (status plus complete OrdinalTier).

## 2. Current-State Inventory

Classifications describe the *current component's disposition*, not whether its underlying product idea survives. `TEMPORARILY_KEEP` means an old authority remains live for a short, named replacement interval; `DELETE_AFTER_CUTOVER` means remove its implementation/physical structure after its consumers are replaced. Paths are relative to the repository. The deletion phase is specified again in §7. This inventory records the **original planning baseline** (`a38c9306`, head `0071`) and is kept as provenance for why each unit exists; rows owned by Phase 1 (Employees, Drivers, DriverTransferRequests, Core People/Driver APIs, Transfer service, and the P1 portion of current-driver helpers/eligibility) are now closed by §6A P1a–P1c and are not future instructions.

| Component | Current files/tables/routes | Target role | Classification | Reason from current repository | Replacement dependency | Deletion point |
|---|---|---|---|---|---|---|
| Company / Branch | `core.Companies`, `core.Branches`; `settings/service.py:444-804` | Tenant and branch ownership | KEEP_AND_ADAPT | Real company/branch keys and checks already used across payroll; add explicit company currency and boundary checks | P3 currency | None |
| Employees | `core.Employees`; `core/service.py:616-625` | Durable Workforce identity | KEEP_AND_ADAPT | Table has `EmployeeID`, `EmployeeKey`, employment fields, but creation is Driver-only and `EmployeeType` required (`0001_initial_schema.sql:184-207`) | P1 workforce API/integrity | Old write path P1 |
| Drivers / Driver profiles | `core.Drivers`; `0027`, `0031`; `core/service.py:643-729` | Branch-bound effective historical payroll identity | KEEP_AND_ADAPT | Transfer already creates a new `DriverID`; generic patch can change status and current lookups are status-only | P1 constraints/resolver/profile service | Generic mutation P1 |
| DriverTransferRequests | `core.DriverTransferRequests`; `transfer/router.py`, `transfer/service.py` | Retained movement approval workflow | KEEP_AND_ADAPT | Existing request/approval provenance works; completion moves `Employee.BranchID` early (`transfer/service.py:640-740`) | P1 transfer correction; P6 target copy | Old completion P1 |
| Core People/Driver APIs | `/core/people`, `/core/drivers`; `core/router.py`, `core/service.py:292-729` | Workforce reads and explicit writes | REBUILD | No independent Employee write API; `create_driver` creates Employee and profile together, patch mixes their fields | P1 Workforce APIs | P1 after route swap |
| Transfer service | `transfer/service.py:595-740` | Profile transition only | REBUILD | Destination starts `Active` before future effective date and Employee branch moves immediately; no target compensation copy | P1 profile resolver; P6 compensation copy | Old completion P1; old contract P6 |
| Current-driver helpers | `core/service.py:367-372`; `admin/service.py:323-352`; `payroll/guards.py:69-103`; `eligibility.py` | Effective-date resolver; WorkDate eligibility | KEEP_AND_ADAPT | Many current reads exclude `Transferred`/`Terminated` by status, while eligibility already checks effective windows (`eligibility.py:37-78`) | P1 shared resolver, P2 security | Status-only readers P1/P2 |
| EmployeeType | `core.Employees.EmployeeType`; `core/service.py:618`, People filters | No runtime authority | DELETE_AFTER_CUTOVER | Required free text in base schema; role-like values cannot define Driver identity | P1 profile truth and fixture updates | P8 |
| Users | `sec.Users`; `admin/service.py:590-817` | Optional login account | KEEP_AND_ADAPT | `EmployeeID` already nullable and `CanLogin`/`IsActive` exist; user create can enable login before role assignment | P2 staged/provisioned lifecycle | None |
| Roles (legacy global) | `sec.Roles`, `RolePermissions`, `admin/service.py:839-1105` | Existing assignments during cutover | TEMPORARILY_KEEP | Both legacy `RoleID` and company `CompanyRoleID` paths are still read by auth/permission evaluation | P2 audit and one canonical role path | P8 if zero consumer/FK proof |
| CompanyRoles | `sec.CompanyRoles`, `CompanyRolePermissions`; `admin/service.py:1215-1968` | Company-owned Access roles | KEEP_AND_ADAPT | Already has company ownership, grants, soft archival and owner transfer; DRIVER name substring and side effects violate boundary | P2 exact DRIVER and safe grants | Side effects P2 |
| UserBranchRoles | `sec.UserBranchRoles`; `admin/service.py:1769-1920` | Atomic Role/Scope assignment | KEEP_AND_ADAPT | Scope and role stored together; currently accepts DRIVER+SpecificBranch and can create Workforce | P2 DB/service rules | Old assignment behavior P2 |
| User.EmployeeID | `sec.Users.EmployeeID`; `admin/service.py:2386-2573` | Explicit 0..1 same-company link | KEEP_AND_ADAPT | Column exists but role assignment populates it implicitly; no uniqueness/same-company guarantee in base schema | P2 link transaction/constraints | Implicit linker P2 |
| Password hashing | `auth/security.py:16-33` | Credential verification | KEEP | bcrypt hash/verify already isolated | P2 account state gate only | None |
| Login / JWT / current-user | `auth/service.py:40-190`, `auth/security.py:40-77`, `dependencies.py:42-55`, `/auth/login`, `/auth/me` | Authentication mechanics | KEEP_AND_ADAPT | Signed expiring JWT and DB login checks exist; dependency only decodes token, while login reads role/access views | P2 staged/DRIVER guard in login and authorization | None |
| Admin password reset | `admin/service.py:762-817`; `/admin/users/{id}/reset-password` | Reset credential | KEEP | Row lock, bcrypt and audit already present | P2 UI/account lifecycle | None |
| Permission function/views | `sec.fn_UserHasPermission` (`migrations/sql/0021...`), `app.vw_UserBranchAccess` (`0016...`), `auth/service.py` | Branch-aware grants | KEEP_AND_ADAPT | Central SQL permission/view mechanism exists but ODA currently uses stored branch and mixed role paths | P2 resolver-derived ODA/security proof | Old ODA semantics P2 |
| People wizard | `frontend/src/pages/people/PeoplePage.tsx:318-690` | Final Workforce and Access views | DELETE_AFTER_CUTOVER | User list is primary and creation is a multi-call User→Role wizard | P7 target screens | P7 |
| DRIVER role behavior | `admin/service.py:1821-1829,1909-1916,2386-2573` | Future self-service only | REBUILD | Substring match creates/moves Employee/Driver and links User | P2 exact role, linked current profile | P2 |
| OwnDriverDataOnly | `payroll/guards.py:28-103`; `sec.UserBranchRoles` | Self-only scope, projected branch | KEEP_AND_ADAPT | Helper resolves first nonhistorical profile by status and branch-scope paths can leak roster | P2 effective identity plus negative permission tests | Old resolver P2 |
| PayItems | `payroll.PayItems`; `settings/service.py:1880-4500` | Company-owned PayDefinition | TEMPORARILY_KEEP | Global/system plus custom rows and `RateBehavior` mix input with Bonus/rules; period creator snapshots them (`period_creation.py:423-535`) | P3 target definitions; P4 runtime | P8 after P4/P5 consumers |
| BranchPayItemConfig | `payroll.BranchPayItemConfig`; `settings/service.py:2241-2651` | Effective branch activation for PayDefinitions | KEEP_AND_ADAPT | Effective-date activation already drives period layout; re-key to target definitions | P3 definition key, P4 snapshot | Old FK/shape P8 |
| CDPI | `cdpi/service.py`, `cdpi/methods.py`, `payroll.CdpiDefinitions` | Transitional legacy creation/governance/provenance workflow | TRANSITION_TO_GENERIC | Approval/direct-create flows exist but approval manufactures RateType/Map/Slot triple (`cdpi/service.py:821-822,1013-1014`) | P3/P4 generic PayDefinition governance and provenance | Legacy identity/runtime P4/P7; storage P8 after provenance proof |
| RateTypes | `payroll.RateTypes`; `payroll/rates.py`, `/payroll/rate-types` | None | DELETE_AFTER_CUTOVER | Generic routing identity is read by rates, status and immutable evidence | P3 RateDefinition; P4/P5 consumers | P8 |
| PayItemRateTypeMap | `payroll.PayItemRateTypeMap`; `settings/service.py:4332-4485` | None | DELETE_AFTER_CUTOVER | Mapping is required by legacy calculation and generated custom items | P3 direct definition relation; P4 runtime | P8 |
| RateSlots | `payroll.PayItemRateSlots`; `pay_item_rate_slots.py` | RateComponentDefinition | DELETE_AFTER_CUTOVER | Helper explicitly makes CDPI RateType+Map+Slot triple | P3 components | P8 |
| DriverRates | `payroll.DriverRates`; `payroll/rates.py:1002-1999` | DriverRateAssignment + Value | TEMPORARILY_KEEP | Effective dated approval exists but keyed to RateType and carries scalar/method details | P3 target assignments, P4/P5 calculation | P8 after all consumers |
| DriverRateTiers | `payroll.DriverRateTiers`; `payroll/rates.py:326-780` | Ordinal topology plus assignment values | DELETE_AFTER_CUTOVER | Legacy tiers and Range/Block calculators share rate identity | P5 target OrdinalTier | P8 |
| StatusRateColumns | `payroll.StatusRateColumns`; `0056` SQL; `settings/service.py:1621-1878` | Branch-owned status rate column | KEEP_AND_ADAPT | Unique branch default/name checks exist; currently FK to `RateTypes` | P5 direct RateDefinition FK | Old FK P8 |
| BonusEvent | `payroll.PayrollBonusEvents`; `payroll/bonus.py`; migration `0058` | Independent Bonus event | KEEP_AND_ADAPT | Event lifecycle, batch safety and snapshot consumer already exist | P3 currency gate; P4/P5 evidence alignment | Old BONUS PayItem authority P8 |
| DriverPayRule | `payroll.DriverPayRules`; `payroll/driver_pay_rules.py`; migration `0012` | Independent min/max rule | KEEP_AND_ADAPT | Separate rule CRUD and calculation consumer exist | P3 currency gate; P4 evidence alignment | Old synthetic PayItem authority P8 |
| Period Pay writer | `payroll/period_pay.py`; `/periods/{id}/period-pay` write routes | No generic writer | DELETE_AFTER_CUTOVER | Current generic amount writer is outside target measured-input/Bonus/rule model | P4 explicit target input paths | P8 after UI/caller audit |
| Calculation methods | `payroll/rates.py:378-780`, `cdpi/methods.py` | PerUnit and OrdinalTier for first production; explicit method-owned extension boundary | REBUILD | Legacy RangeBracket/RangeProgressive/Block persist; CDPI adapter has unimplemented variants | P3 generic definitions; P4 PerUnit; P5 OrdinalTier | Legacy implementations P8; future similar capabilities require explicit product design |
| Copy-rates service | `payroll/rates.py:2875+`, `/payroll/.../copy-rates` | Target assignment-copy service | DELETE_AFTER_CUTOVER | Existing copy inserts legacy DriverRates and offers optional pay-rule copy | P6 complete assignment/value copy | P8 |
| Period creation | `payroll/period_creation.py:602-1020`; Payroll Setup resolver; `0071` | Sole canonical creator | KEEP_AND_ADAPT | Candidate key, branch lock, published setup version/assignment and atomic period/day/layout/eligibility creation already operate; `0071` retires old schedule authority | P4 target layout snapshot hook | None |
| Period PayItem snapshot | `payroll.PayrollPeriodPayItems`; `period_creation.py:423-535`, `period_pay_item_snapshot.py` | Immutable period PayDefinition layout | KEEP_AND_ADAPT | Snapshot currently freezes PayItem data and branch config at period start, called only by canonical creator | P4 target identity/method/config snapshot | Old columns/FKs P8 |
| Periods / days | `payroll.PayrollPeriods`, `PayrollPeriodDays`; `period_creation.py:373-419,920-1003` | Period and day authority | KEEP | Published setup provenance and days are already frozen | P4 layout integration | None |
| Eligibility | `payroll/eligibility.py:37-78,355-555`; `PayrollPeriodDriverEligibility` | WorkDate/profile eligibility and snapshots | KEEP_AND_ADAPT | Date windows and period snapshots exist; current status checks can reject a historical profile for its valid date | P1 Workforce resolver consistency | None |
| Daily/Draft lines | `day_grid.py`, `draft_line_mutation.py`, `draft_line_calculation.py`, `PayrollDraftLines` | Work input and calculated draft output | KEEP_AND_ADAPT | Entry lifecycle works; rate calculation still uses PayItem→RateType mapping | P4/P5 target adapters | Legacy fields/line types P8 |
| Submit/review | `period_lifecycle.py`, `review/service.py`; `0062` | Approval workflow | KEEP | Submit captures calculation snapshot (`period_lifecycle.py:472-492`); review binds approvals | P4 target packet | None |
| Calculation snapshots | `period_calculation.py:1509-1799`; `0061-0063` | Immutable approved calculation | KEEP_AND_ADAPT | Existing snapshot lines/totals/hash are present, but source descriptors use legacy identities | P4 structured target evidence, P5 status/tier detail | Legacy fields P8 |
| Immutable evidence | `immutable_evidence.py:160-314`; `0064-0065` | Frozen used-rate and action evidence | KEEP_AND_ADAPT | Used-rate capture directly joins `DriverRates` and `RateTypes` | P4/P5 target identity/value/topology | Legacy descriptor P8 |
| Finalization | `finalization.py:158-314` | Snapshot-only projection to final lines | KEEP_AND_ADAPT | Loads approved snapshot packet and inserts final lines without live rate resolution | P4 target packet schema | None |
| Finalized library | `finalized_library_read_model.py`, `ledger_read.py` | Historical reads | KEEP_AND_ADAPT | Final lines and frozen evidence power library/ledger; labels and identities are PayItem/legacy-line oriented | P4/P5 target evidence projection | Legacy fallback P8 |
| Reports / ledger | `report_read_model.py:50-106,192-390`; `ledger_read.py`; `PeriodPayMatrix.tsx` | Current/final financial read models | KEEP_AND_ADAPT | Report columns read `PayrollPeriodPayItems`, financial packet reads snapshots/final lines | P4 target layout/evidence | Legacy-key fallback P8 |
| People frontend | `PeoplePage.tsx`, `peoplePermissionModel.ts` | Workforce / Access / Transfer tabs | REBUILD | User-centered list, wizard and role-profile coupling | P1/P2 APIs, P7 final UI | P7 |
| Access / Roles frontend | `PeoplePage.tsx`, `settings/roles/RolesPage.tsx` | Access account and role/permission authoring | KEEP_AND_ADAPT | Roles editor is distinct and useful; User lifecycle is trapped in People wizard | P2 API, P7 Access tab | Wizard P7 |
| Transfers frontend | `TransferRequestsTab.tsx`, `transferApi.ts` | Request/approval plus explicit copy choice | KEEP_AND_ADAPT | Existing workflow UI does not ask target compensation copy | P6 copy API, P7 final UI | Old form contract P7 |
| Pay Rates frontend | `people/pay-rates/PayRatesPage.tsx` | Target assignment authoring/history | REBUILD | Current matrix and copy action call legacy rate routes (`PayRatesPage.tsx:632`) | P3/P5 APIs, P7 final UI | P7 |
| Payroll entry frontend | `PayrollEntryDialog.tsx`, `PeriodDetailPage.tsx`, `periodPayTable.ts` | Target definition inputs; status/bonus separated | KEEP_AND_ADAPT | Consumes period PayItem layout and exposes generic period-pay presentation | P4/P5 API, P7 final UI | Generic controls P7 |
| Settings/CDPI/status UI | `settings/pay-items/PayItemsPage.tsx`, `StatusKeysPage.tsx`, `cdpiApi.ts` | Definition governance, activation, status columns | KEEP_AND_ADAPT | Existing governance/branch activation UX is useful; models/labels still PayItem/RateType | P3/P5 APIs, P7 UI | Legacy contracts P7 |

## 3. Target Architecture

```text
Company ── Branch
   │
   ├─ Payroll Setup ─ Published Version ─ Branch Assignment
   │       └─ canonical candidate/period creator ─ PayrollPeriod ─ PeriodDays
   │                                              └─ frozen PayDefinition layout + eligibility
   ├─ Workforce: Employee ── DriverProfile[DriverID, BranchID, effective window]*
   │                            └─ TransferRequest closes/creates profiles
   ├─ Access: User ── optional explicit Employee link
   │              └─ CompanyRole + Scope + overrides (DRIVER = self-only)
   ├─ Compensation: PayDefinition ─ CalculationMethod ─ RateDefinition
   │                                    └─ RateComponentDefinition(s)
   │                                         └─ DriverRateAssignment[DriverID, effective window]
   │                                              └─ DriverRateValue(s)
   ├─ StatusKey/DayEntryState ─ branch StatusRateColumn ─ RateDefinition
   ├─ BonusEvent; DriverPayRule (independent)
   └─ Payroll runtime: WorkDate input → target rate resolver → calculation packet
                         → submit/review → immutable snapshot/evidence/hash
                         → snapshot-only finalization → final lines/library/reports
```

DriverID, rather than EmployeeID, remains the intentional branch-bound rate and payroll-history key for current driver/fleet payroll. Each operational PayDefinition is company-owned; a company is valid with zero PayDefinitions. One company currency is the V1 product boundary; finalized monetary evidence freezes its code. PerUnit and OrdinalTier are the only first-production PayDefinition methods, with future methods requiring explicit product approval and the method-owned target boundary. No business code/display name has privileged runtime meaning. Zero is a configured value; missing is not zero. An assignment is one whole effective-dated schedule; components and values have no independent dates. Status, BonusEvent, and DriverPayRule do not become PayDefinitions.

### Compensation hard-coding boundary

Permanent invariants include finalized-payroll immutability; Employee/User separation; Status, BonusEvent, and DriverPayRule remaining separate from PayDefinition; missing not meaning zero; one complete effective-dated assignment supplying its complete value set; immutable historical evidence; and snapshot-only finalization. Current-product/V1 boundaries include one currency per company, PerUnit and OrdinalTier as first-production methods, Branch applicability, the current Status policy, and DriverID as the branch-bound driver/fleet payroll identity. These are not universal claims about all future products.

No PayDefinition business code or display name (including example names such as HOURS, MILES, or LOADS) has privileged runtime semantics. A company may validly have zero PayDefinitions. Optional future templates, if separately approved, only create ordinary company-owned definitions and are not part of P3/P4 acceptance. CDPI is transitional workflow/provenance terminology; target governance and runtime have one generic PayDefinition identity. Preserve required provenance until its generic target home is proven before retiring legacy storage. Do not branch on definition names, presentation labels, generated technical identities, CDPI origin, ordinary role names, or role substrings. Protected system-role semantics may be explicit and centrally defined. Self remains a generic non-branch resource scope; DRIVER is its current-product binding only. No generic formula/plugin engine, arbitrary scope framework, or CompensationSubject abstraction is introduced. The current mixed DRIVER/administrative denial remains authoritative; before final P7 Access UX is frozen, product/security design revisits whether one person needs both responsibilities. Separate accounts, explicit persona/session context, or another model remain undecided.

## 4. Domain Boundaries

| Owner | Writes / decisions it owns | Interface to neighbors |
|---|---|---|
| Payroll Setup | Published schedule versions, branch assignments and candidate boundary | Canonical period creator consumes resolved version/assignment; compensation cannot create a second period authority |
| Workforce | Employee identity/status, Driver profile windows/immutable branch, effective profile resolver, termination | Access receives explicit link target and effective Driver identity; Payroll receives eligibility by DriverID/WorkDate |
| Access | User credential/account state, explicit Employee link, role/scope/overrides, DRIVER Self gate | Does not create/retype/move Workforce; Self has no Access BranchID and ownership follows the linked Employee/effective Driver profile |
| Transfer | Approval workflow and source/destination Driver profile transition | Calls Compensation *only on explicit Yes* to copy target schedules; does not mutate source rates or pay rules |
| Compensation | PayDefinition/RateDefinition/component/assignment/value governance, effective resolution and target copy | Resolves one complete assignment for `(DriverID, RateDefinitionID, WorkDate)`; supplies immutable descriptors to Payroll |
| Payroll | Period lifecycle, work/status input, eligibility snapshot, calculation orchestration, approval, evidence and finalization | Uses frozen period definitions and Compensation resolver during live calculation; finalization only reads approved frozen packet |
| Status / Bonus / Rules | StatusKey/column choice, BonusEvent and DriverPayRule | Payroll computes their separate contributions; Compensation provides status scalar rate only |

## 5. Dependency Graph

```text
P1 Workforce integrity + effective profile + corrected core transfer/termination
 ├──────────────→ P2 Access link/provisioning + DRIVER security
 └──────────────→ P3 Compensation core + company currency + dormant authoring
                     └──────→ P4 canonical period layout + PerUnit runtime/evidence cutover
                                  ├────→ P5 status direct path + OrdinalTier completion
                                  └────→ P6 target assignment copy on transfer
P2 ─────────────────────────────────────→ P6 (Self authorization and security)
P1/P2/P3/P4/P5/P6 ───────────────────────→ P7 final frontend contracts
P7 + all backend cutovers ───────────────→ P8 legacy removal + clean DB/reseed/closure
P8 ──────────────────────────────────────→ C2 Import gate opens
```

P3 target compensation can be built while the legacy runtime remains authoritative. It establishes the generic PayDefinition model, method structure, governance/provenance seam, branch applicability, PerUnit target authoring, and OrdinalTier topology foundation without required named definitions or a privileged CDPI identity. P4a switches the single generic PayDefinition configuration/authoring authority before P4b makes the canonical creator snapshot it; P4c completes calculation evidence and lifts the temporary submit/finalize hold. P5 finishes status and OrdinalTier before any old status/tier authority is removed. Work units run in the §6A order; P6 begins only after P5 proves atomic tier copy and status-column mapping.

## 6. Unified Implementation Phases

### Phase 1 — Workforce authority and effective-date integrity

**Status: COMPLETE — P1a, P1b, and P1c accepted and merged to `main` through PR #22.**

#### Goal

Make Employee and branch-specific DriverProfile the sole Workforce write authority while preserving DriverID history.

#### Why now

Target rates, Access self identity and C2 all depend on trustworthy profiles. Current `core/service.py` has no Employee create path and `transfer/service.py` changes Employee branch before a future transfer date.

#### Build

Explicit Employee create/update, non-Driver, first/future Driver profile, termination and profile-history APIs; canonical company-date/effective-profile resolution. Rework core transfer completion to lock source/request, close source at `EffectiveDate - 1`, create a pending destination effective on `EffectiveDate`, and synchronize Employee branch only when the date becomes current. Do not add compensation copy yet. Make status/window rules and the never-effective terminated-pending case explicit.

#### Keep

`core.Employees`, `core.Drivers`, `DriverTransferRequests`, audit and WorkDate eligibility principles.

#### Cut over

All Workforce writes and current-profile reads, including transfer, to the new service/resolver. Existing transfer UI may call the corrected workflow without a copy choice until P6/P7.

#### Delete

Generic `/core/drivers` creation/patch semantics and status-only current-driver helpers once their callers use the new service. No rate table deletion.

#### Database

Add same-company Employee/Driver and branch integrity, immutable Driver branch/identity, non-overlapping effective windows, closed status sets, protected history and stable EmployeeKey. Retain schema history through correct new Alembic revisions; no demo-data backfill engine. Enforce the terminated pending profile's empty/never-effective window without treating it as current.

#### Backend

Create Workforce-owned service/router; align period eligibility with effective WorkDate including transferred source history and future destination; ensure transfer and termination are atomic and do not rewrite locked/finalized evidence.

#### Frontend

Only minimal API compatibility for existing transfer/roster screens. Do not rebuild the People wizard yet; disable any write action that could violate the new boundary until P7 replaces it.

#### Tests

Non-Driver and pending profiles, company mismatch, immutable branch, overlap/concurrent transfer, future-date current branch, source WorkDate eligibility, termination and unchanged historical payroll/rate keys.

#### Acceptance gate

Every Workforce write goes through explicit Workforce/Transfer authority; one effective Driver per Employee/date; generic editing cannot move or reactivate a historical profile; existing period creation and payroll eligibility regressions pass.

#### Risks

Future transfer must coexist with advance-created periods and eligibility snapshots; status alone cannot decide which Driver is current.

#### Size

LARGE

### Phase 2 — Access account and DRIVER boundary

**Execution status:** P2a–P2c and P3a are closed; P3a merged via PR #30. G0 (G0.1–G0.6) closed between P3a and P3b, ending with PR #45 at migration head `0081`. P3b is the current implementation work unit.

#### Goal

Separate login/account provisioning from Workforce identity without replacing sound authentication mechanics.

#### Why now

`assign_company_role` currently uses substring `DRIVER` and calls `ensure_driver_profile` (`admin/service.py:1824,1909-1916`), while legacy ODA branch checks can grant branch-wide visibility. This is current-code context; P2b replaces ODA as the target authorization scope.

#### Build

P2a established explicit same-company 0..1 User–Employee linking, staged/login-disabled accounts, and transactional provisioning. P2b migrated the current DRIVER authorization representation from legacy `OwnDriverDataOnly` to generic `Self`: exact `DRIVER` requires `Self`; a provisioned DRIVER assignment requires an explicit same-company Employee link and current effective Driver profile; pending-profile accounts remain staged until effective. Self has no Access BranchID and is not branch membership; current Driver ownership follows User→Employee→the canonical effective Driver profile on the operation's relevant date. Missing link, company mismatch, pending/no-current profile for a current action, or terminated/no-current identity fails closed. A Self-only authority is representable by login and `/auth/me` without a fabricated branch-access row. P2b preserves the DRIVER self-service capability ceiling, prevents overrides from widening resource scope, keeps Company Owner's dynamic permission path limited to a valid active company-wide assignment, and does not add mixed DRIVER/administrative behavior. Implementation mechanisms were selected and delivered in P2b; this section records the completed contract.

#### Keep

`sec.Users`, CompanyRoles, UserBranchRoles, permissions function/view mechanism, bcrypt, JWT/login/me and reset-password implementation.

#### Cut over

P2b secured the DRIVER Self authorization boundary while the temporary legacy Access→Workforce role-assignment side effect remains. Login and `/auth/me` represent valid Self authority independently of branch membership and fail closed for staged, unlinked, cross-company, pending/no-current-profile or terminated identities when current Driver authority is required. Self never turns an action or permission into branch-wide, company-wide, or generic administrative authority. DRIVER remains denied generic payroll, rate, Workforce, and Access administration; explicitly designed Self-compatible own-resource operations remain possible. P2c removes the legacy role-assignment side effect and completes the Access-only role-assignment cutover.

#### Delete

P2b retired OwnDriverDataOnly as the target representation and secured exact DRIVER/Self identity and resource access. P2b did not remove `ensure_driver_profile`, perform the broad Access-writer cleanup, redesign the People UI, or start Compensation work. P2c removes `ensure_driver_profile`, the `EMP-{UserID}` convention, implicit User.EmployeeID assignment, and remaining Access→Workforce mutations. The legacy global role path is removed only after usage/FK proof in P8.

#### Database

Preserve unique User.EmployeeID and same-company integrity; staged state forbids login. Enforce the semantic scope rules: current DRIVER assignments use Self with no Access BranchID, while non-DRIVER roles do not gain Self without explicit policy. Overrides may add actions only inside established resource scope. Company Owner dynamic permissions require a valid active company-wide assignment. P2b enforcement and storage are closed. P2c preserves these invariants while removing the remaining Access-to-Workforce role-assignment side effect. Protect the link across deactivation and require explicit unlink before relink.

#### Backend

Access account/link/provisioning endpoints remain as established by P2a. P2b applied Self semantics across permission evaluation, branch-access projections, login/`/auth/me`, payroll guards, core roster gates and permission grants. Self is evaluated as resource ownership and is not emitted as branch membership. Login checks provisioned state; hash/token algorithms stay.

#### Frontend

Minimal blocking/compatibility for wizard DRIVER actions. Final Workforce/Access split waits until P7; existing Roles editor can remain with tightened grants.

#### Tests

Independent Employee/User, same-company/cardinality, staged login denial, role changes without Workforce mutation, exact DRIVER/Self binding, effective-date identity across transfer and termination, Self-only login/`/auth/me` without branch membership, admin rate denial even for own Driver, roster privacy, scope-safe overrides, Company Owner malformed-scope denial, and mixed-authority bypass attempts.

P2b added a **mechanically enforced endpoint authorization inventory** over the current registered application route table, including the account, link and provision routes added in P2a. Every registered route has explicit, reviewable classification of authentication, action/permission policy, resource-scope policy, Self denial/allowance and (where relevant) company/branch policy. A Self-allowed route identifies the canonical subject-resource relationship that proves ownership. An unclassified or newly added route fails the test suite. Negative authorization coverage for relevant Self/DRIVER routes proves denial of another Driver's data, branch roster leakage, generic administrative mutation, missing User.EmployeeID, company mismatch, no effective profile for current actions, pending profiles treated as current, terminated/no-current identity, and override-based resource expansion.

#### Acceptance gate

P2b's acceptance gate is closed: every registered route is classified; every DRIVER Self path is self-safe; staged, unlinked, mismatched-company, pending/no-current-profile and terminated identities fail closed as applicable; a valid Self-only account works without branch membership; overrides cannot widen resources; Company Owner dynamic permissions require valid company-wide scope; and mixed role rows cannot bypass the DRIVER ceiling. Self identity resolves through User→Employee→the canonical effective Driver profile, never a branch projection or Driver status alone. At P2c close, Access role/Scope writes no longer create or move Workforce records or implicitly link Users.

#### P2b hard stops

These were P2b stop conditions. P2b is complete; no unresolved P2b stop condition is carried into P2c by this plan.

#### Risks

Auth decisions are spread across login and `/auth/me`, SQL permission/view paths, route services and UI projections; server-side tests must cover every path.

#### Size

LARGE

### Phase 3 — Target Compensation core, dormant

#### Goal

Create clean company-owned definitions and complete effective-dated assignments before redirecting payroll.

#### Why now

Current CDPI and rates still generate/resolve RateType+Map+Slot (`pay_item_rate_slots.py:138-206`), but target tables can be tested with legacy runtime untouched.

#### Build

**P3a is the first and independently accepted work unit in this phase:** establish Company `CurrencyCode` as an authority gate across *every currently reachable durable monetary writer*, including temporary legacy writers. Existing history remains readable and is not relabeled or backfilled by guessing a currency. Require a configured, valid company currency before a new monetary write; freeze that code in new monetary snapshot/evidence records. First explicit configuration is allowed for an unconfigured company. Once durable company monetary state exists, reject a change from one configured code to another; include legacy approved/superseded rates, BonusEvents, active/ended pay rules, monetary DraftLines, calculation snapshots and final lines in that immutability check. Apply the guard at each write transaction and enforce the company-currency change rule under a lock/DB-backed invariant so a racing monetary write cannot permit a currency switch.

**G0 — Post-Currency Contract Closure:** before P3b begins, complete the intervening closure gate. See [G0_POST_CURRENCY_CONTRACT_CLOSURE.md](G0_POST_CURRENCY_CONTRACT_CLOSURE.md) for the full architectural scope and closure criteria.

Then build generic company-owned PayDefinition, CalculationMethod, RateDefinition, one component abstraction, DriverRateAssignment and DriverRateValue, including generic creation/governance and provenance, branch applicability, PerUnit authoring, approval/supersession, pending/discard, and the OrdinalTier topology foundation. No business-code starter catalog, mandatory seed, or named-definition prerequisite is created. Transition existing CDPI request/approval/direct-create semantics into generic PayDefinition governance without a permanent CDPI identity or dual-writing one request. The legacy CDPI operational path remains until P4a cutover. Expose target APIs without wiring payroll until P4.

#### Reachable monetary writer inventory for P3a

The gate covers direct routes **and** service calls reached indirectly through batch, transfer-copy, day-state and lifecycle flows. Read/preview endpoints remain readable without currency if they do not persist monetary state.

| Currently reachable writer | Repository evidence | P3a gate location/scope |
|---|---|---|
| Legacy DriverRate scalar/tier create, edit, batch save, approve/supersede, and copy | `payroll/rates.py:1002,1207,1580,2279,2875`; `DriverRates` inserts/updates at 1148,1294,1723,2511-2556,3131-3150; `DriverRateTiers` insert at 364 | Guard all amount/tier writes and approval, including copy's destination writes and optional pay-rule copy at 3259; keep legacy routes gated until removed |
| BonusEvent create, amount update, batch insert | `payroll/bonus.py:268,350,633`; amount writes at 298,405-447,766 | Guard single and batch transactions before mutation |
| DriverPayRule amount create and lifecycle changes; copied pay rule | `payroll/driver_pay_rules.py:220,387,519`; insert at 291, status updates at 439/559; `rates.py:3259` | Guard create/reactivation or monetary-state-changing lifecycle/copy; notes-only read/edit need not be blocked |
| Generic Period Pay amount add/update | `payroll/period_pay.py:306,489`; DraftLine calculated-amount writes at 383,543-592 | Guard until writer is retired; void/read-only history remains available |
| Daily DraftLine amount/rate override add/update and calculation refresh | `payroll/draft_line_mutation.py:319,639` (amount insert 551 and update 881); `payroll/period_calculation.py:115-263` refreshes calculated amount | Guard every monetary field write; source-only quantity/note edits may remain permitted if no amount is persisted |
| Legacy status-payment DraftLine projection | `payroll/status_payment_sync.py:102,444` (calculated amount update/insert at 272/293), invoked by day-state/grid flows | Guard computed monetary projection while reachable through P5; nonmonetary status state itself stays available |
| Submit/resubmit calculation snapshot and used-rate evidence | `payroll/period_lifecycle.py:472,1028`; `payroll/period_calculation.py:1509-1794`; `immutable_evidence.py:160-314` | Guard before capture, and stamp new snapshot/evidence with configured CurrencyCode; review approval of that frozen monetary packet also requires it |
| Finalization/final-line projection | `payroll/finalization.py:272-355`, final-line insert at 286 | Guard before finalization transaction; project only approved snapshot, never re-resolve rates; stamp/retain frozen currency |

P3a also audits any monetary trigger, SQL function, script or route reached in the migrated application; a newly discovered durable writer joins this gate *before* P3a acceptance. The new target DriverRateAssignment/value writer built in P3b/P3c must inherit this gate before its first value write or approval, with its own tests in P3c. `day_grid.py`'s shown DailyStatus/DailyNote rows are nonmonetary, but its call into status-payment sync is covered. Period creation's layout/day/eligibility rows are not monetary and do not acquire a second currency authority.

#### Currency precision and payable-rounding decision gate (P3a opens, P4c closes)

At the post-P1 baseline there is no `CurrencyCode`; internal line calculation quantizes to `0.0001` with `ROUND_HALF_EVEN` into `NUMERIC(18,4)` columns, no payable rounding to a currency minor unit exists, BonusEvent input is separately limited to two decimals, and the frontend formats `$…toFixed(2)`. P3a keeps internal precision unchanged and adds only what currency authority requires: the supported-currency list carries each code's minor-unit precision as reference metadata, and no API or UI hard-codes two decimals or `$` for new currency-aware surfaces. P3a records — it does not choose — the **payable-rounding product decision**: rounding method, rounding boundary (source line, component, Driver total, final payable, or export only), residual/reconciliation treatment, and whether unrounded and rounded values are both retained. Internal calculation precision, final payable rounding and display formatting are three separate concerns. Until P4c, no implicit payable-rounding rule is introduced anywhere in backend or frontend. P4c cannot pass until one explicit, currency-aware payable-rounding policy is approved, implemented, tested and frozen in evidence (see §12A); a no-rounding deferral is not an acceptable P4c outcome. Internal calculation may continue to retain higher precision.

#### Keep

CDPI governance/provenance, branch activation policy, rate approval/finalized guard and lock invariants as behaviors; old RateTypes/DriverRates remain the only payroll runtime until P4/P5.

#### Cut over

New authoring endpoints write target only; they are not shown as payroll-operational before P4. No dual-write or legacy synchronization.

#### Delete

No live authority yet. Remove only proven-dead legacy writers with zero consumers; defer structural deletion to P8.

#### Database

Correct fresh-upgrade revisions, company/branch/Driver ownership, assignment non-overlap, complete child-set approval, no child dates, one structural lock and pending-vs-structure race guard. P3a adds currency configuration, new-evidence currency snapshot fields, the durable-state immutability check and concurrency protection before any target table becomes operational.

#### Backend

P3a wires a shared fail-closed currency requirement into every inventory writer and catches indirect batch/copy/lifecycle paths at the transaction boundary. Compensation-owned resolver `(DriverID, RateDefinitionID, WorkDate)` returns a whole assignment/value set; missing remains distinct from zero; positive/whole-number input constraints are definition-owned. Generic PayDefinition governance creates ordinary definitions; transitional CDPI behavior does not create a separate target identity or generated RateTypes.

#### Frontend

Expose currency configuration and currency-aware money formatting in existing admin/read surfaces; only narrow admin/API inspection is needed for target authoring QA. Final Pay Rates and Settings screens wait until P7.

#### Tests

P3a table-driven endpoint/service tests for every row of the currently reachable writer inventory, including legacy rate batch/copy/tier, Bonus batch, pay-rule copy, generic Period Pay, DraftLine overrides/refresh, status projection and submit/resubmit/review/finalize; unconfigured-write rejection, history-read allowance, first configuration, post-state currency-change rejection and write-vs-change concurrency. P3c separately tests target value writing/approval under the same gate. Then test company A/B method independence with arbitrary PayDefinition names/codes, ownership/FK, assignment overlap and concurrency, pending structure lock/discard, zero vs missing, negative rejection and WholeNumber fractional rejection.

#### Acceptance gate

P3a independently proves an unconfigured company can read old history but cannot write any listed monetary state; a configured company can use each still-reachable legacy writer; first configuration works; a change after durable state is rejected even under a concurrent monetary write. Target PerUnit schedules can then be created and resolved by date without RateType; legacy payroll still behaves as before. Target writes never manufacture CPI_/CDPI_/SRC_ identities.

#### Risks

Company currency touches Bonus/rule/evidence writes; enforce currency before new durable monetary state and avoid a half-configured tenant.

#### Size

LARGE

### Phase 4 — Canonical period and PerUnit payroll/evidence cutover

#### Goal

Make operational PayDefinitions and target PerUnit assignments authoritative in existing Payroll Setup period creation, calculation and frozen evidence.

#### Why now

`period_creation.py:753-1003` already owns candidate validation, lock, published setup provenance, period/day/layout/eligibility rows. Its `_create_period_pay_item_rows` is the single layout hook. `0071` physically retired the old payroll-schedule tables.

#### Build

**P4a must complete before any target period snapshot:** make generic company-owned PayDefinitions operational through one target governance/configuration authority (no starter activation is required); move `BranchPayItemConfig`/branch applicability to target PayDefinition identity; switch operational Settings/list/read/config paths to company definitions; move legacy CDPI request/approval/direct-create semantics into the single PayDefinition governance authority and preserve generic provenance; stop all new PayDefinition-related RateType, PayItemRateTypeMap and legacy RateSlot generation (including old custom-item/backfill writers); make target PerUnit authoring the operational Pay Rates authority; retire global/system PayItems as operational PayDefinitions while leaving physical rows/tables for P8; and remove any transitional marker whose stated cutover purpose has ended. P4a switches the old PayDefinition creation/configuration routes off in the same cutover as the target routes turn on. It must fail closed on new period creation for an affected company until P4b enables target snapshots; old creation/configuration cannot reopen as an alternate authority. Status-only RateType writes remain explicitly isolated until P5.

**Only after P4a's authority gate passes**, P4b replaces the PayItem layout read in the existing canonical creator with target operational PayDefinition snapshots: ID/code/name, method/version, unit/input type, branch activation and effective source. Keep the same candidate key/period authority and atomic transaction. Adapt Daily/Draft inputs, calculation packet and effective resolver to target PerUnit.

**P4b Daily/Draft financial authority.** At the post-P1 baseline `PayrollDraftLines` holds operational source facts (quantity, WorkDate, line type, notes) *and* mutable derived money (`RateAmount`, `CalculatedAmount`, `NeedsManagerReview`). Those persisted values are read as financial truth by the Day Grid gross total (`day_grid.py`), the draft lines summary (`source_line_read.py`), the submit completeness blocker (`period_lifecycle.py`), and the packet fallback when a line is not refreshed (`period_calculation.py`), while live preview and submit also recompute. For target periods, P4b establishes: operational persistence owns source facts; backend calculation owns live derived money; immutable submitted/final evidence owns frozen money. Persisted draft money for a target period is at most a non-authoritative, reconstructable cache: no live total, blocker, preview, packet or read model may take a stored draft amount in place of the target calculation, and a stale or null stored amount cannot change a result. A user-entered monetary value that is genuinely *input* (not derived) must be an explicitly typed source fact of its owning domain, not an overloaded derived column. No new Daily table is required; existing storage is adapted. Legacy/pre-cutover periods keep their current behavior until reset/removal in P8.

P4c captures RateDefinition/Assignment/value/currency/source context in immutable evidence and hash, then re-keys reports/ledger and final library to snapshot identities. Block submit/resubmit/finalize for target periods until P4c's evidence path is accepted. Keep status legacy path only until P5, clearly segregated in the packet.

**P4c additional evidence obligations.**

- *DriverPayRule outputs (§8.8).* Replace the synthetic `SYS_MIN_TOPUP`/`SYS_MAX_CAP` identity with explicit rule-output evidence that alone reproduces the adjustment: DriverPayRuleID, RuleType, the rule amount and its effective dates/status as applied, the PayrollPeriod scope and the rule-selection date, the qualifying base and its composition (which contributions are in or out; Bonus is excluded today), the resulting top-up/cap, CurrencyCode, and — only if a policy is approved — the partial-period policy/version. The period-scoped semantics of §8.8 are carried over unchanged; P4c does not reinterpret them.
- *Payable rounding.* Implement the payable-rounding policy approved through the Phase 3 currency-precision/payable-rounding decision gate, and freeze in the packet and hash: the approved policy/version; the applicable CurrencyCode and minor-unit context; the rounding boundary; the authoritative rounded payable result; the unrounded/intermediate value needed to reproduce or audit it; and any residual/reconciliation outcome the approved policy requires.
- *Financial execution identity.* Keep PayrollPeriod as the one regular execution and workflow identity (no PayrollRun entity, no second lifecycle). Frozen target evidence and final lines must reference the approved calculation snapshot identity (`PayrollCalculationSnapshotID` already exists per period revision) and must not add constraints that make "one financial result per PayrollPeriod/Driver forever" a schema assumption, so a future separately approved correction/off-cycle execution can reference original evidence without rewriting it.
- *Read authority.* Approved/finalized read models expose backend-computed Driver totals, period totals and CurrencyCode from frozen evidence so no client reconstructs official totals (P7c consumes this).

#### Keep

Payroll Setup versions/assignments, PayrollPeriods/Days, draft/review/submit, BonusEvent, DriverPayRule, existing snapshot/final-line lifecycle and locking.

#### Cut over

The **generic PayDefinition operational authority switch is P4a's accepted cutover transaction/release gate**: immediately before it, old PayItems/legacy CDPI/branch config/legacy PerUnit rate authoring are operational and target definitions are dormant; immediately after it, company PayDefinitions, generic governance/configuration and target PerUnit authoring alone accept PayDefinition writes. No request is dual-written or accepted by both authorities. New target period creation stays fail closed until P4b attaches its snapshot to the existing creator; target submit/finalize stays fail closed until P4c evidence is ready. The isolated status RateType path remains temporarily live until P5. In-flight disposable demo periods may be reset; never silently reinterpret a real locked/finalized period. Finalization continues to project the approved snapshot only.

#### Delete

PerUnit RateType lookup and PayItemRateTypeMap runtime; physical tables remain until P8 after status/tier/report consumers are gone.

#### Database

Adapt/replace `PayrollPeriodPayItems` as the immutable target definition snapshot without another period table; evidence-v2 identities/values/currency and source snapshots; fresh DB upgrade. Do not modify Payroll Setup ownership or reintroduce old schedule FKs.

#### Backend

P4a makes target Settings, generic PayDefinition governance, branch configuration, and PerUnit authoring the only operational PayDefinition APIs. After its gate, P4b calls target snapshot writer once from the current creator and draft calculator requests complete assignment by DriverID/RateDefinitionID/WorkDate. P4c makes submit bind the target packet; finalization reads only frozen packet. Reports and ledger read frozen target labels/values for approved/finalized periods.

#### Frontend

Minimal contract bridge to keep entry working for PerUnit; defer polished Pay Rates/definition screens to P7. Hide unsupported legacy method creation at cutover.

#### Tests

P4a proves generic company PayDefinition governance/provenance, target branch applicability, transitioned request/approval/direct-create behavior, target PerUnit authoring and zero reachable PayDefinition writer that creates RateType/Map/Slot; simultaneous old/target route attempts cannot both succeed. P4b proves candidate replay/staleness and Setup provenance unchanged, branch activation snapshot, old WorkDate uses old assignment, 8×25=200, missing blocks approval and zero is allowed. P4b also proves, for a target period, that corrupting or nulling a stored draft `CalculatedAmount`/`RateAmount` changes no Day Grid total, draft summary, submit blocker, preview or packet value. P4c proves snapshot hash/evidence stable after live rate edit, finalization performs no live compensation query, and report/ledger agree with frozen packet. P4c also proves: a Min/Max adjustment is reproducible from frozen rule-output evidence after the rule is later ended/voided/edited; Minimum > Maximum for the same period remains a hard blocker (never silently clamped); a large legitimate top-up is not altered; a Payroll Setup schedule change leaves every DriverPayRule amount unchanged. P4c rounding closure tests prove: an approved payable-rounding policy is mandatory, and submit/finalization of a target period cannot pass without it; the selected policy/version is frozen in immutable evidence and hash; the authoritative payable result follows that policy; a later change to live configuration cannot reinterpret finalized payroll; and frontend formatting cannot alter the authoritative backend payable amount. Fail-closed interval gates are tested at each PR boundary.

#### Acceptance gate

P4a first proves exactly one operational generic PayDefinition creation/configuration authority, with no target period snapshot yet; companies with zero definitions remain valid. P4b then proves the current canonical creator freezes only those target definitions, and persisted draft money is not a competing financial authority for target periods. P4c proves a fresh target PerUnit period can be created, entered, reviewed, finalized and explained without an operational RateType/PayItem lookup, including DriverPayRule outputs. P4c is not accepted merely because target calculation works at internal precision: authoritative final payable semantics must be defined by an approved currency-aware rounding contract and be reproducible from frozen evidence. There is no "P4c complete with rounding still undecided" state. No fail-closed interval gate remains after P4c.

#### Risks

The largest cross-module cutover: period layout, draft calculation, evidence hash and read models must agree on identities and currency in the same PR sequence.

#### Size

LARGE

### Phase 5 — Direct status and complete OrdinalTier

#### Goal

Finish first-production calculation coverage and retire status compatibility projection.

#### Why now

`StatusRateColumns` currently FK to RateTypes (`0056_status_rate_columns.sql:37-64`) and `status_payment_sync.py` writes `STATUS_PAYMENT` DraftLines. Legacy tiers share `rates.py` with Range/Block methods.

#### Build

Branch-owned StatusRateColumn→scalar RateDefinition, direct DayEntryState status calculation, status evidence. OrdinalTier definition-owned gapless 1-based topology, final open-ended tier, complete assignment-owned value set, exact arithmetic and frozen tier evidence. Use generic PayDefinition governance for either method; no named starter customization is required. Preserve StatusKey/DayEntryState and branch default-column semantics.

#### Keep

StatusKey, DayEntryState, BonusEvent, DriverPayRule, period/review/finalization core.

#### Cut over

Status calculation uses direct canonical day state and target assignment; `STATUS_PAYMENT` line becomes non-authoritative and then stops being written. OrdinalTier operational definitions use target assignment only. No unsupported Range/Block method remains selectable.

#### Delete

`STATUS_PAYMENT` projection writer/reader after direct status evidence passes; legacy tier and advanced-method runtime references after target arithmetic passes. Physical RateType/DriverRate/Tier structures in P8.

#### Database

Re-key StatusRateColumn FK, enforce topology and atomic complete values, structure lock after first Approved assignment or period reference, and pending/structure concurrency. Snapshot status and tier descriptor topology/values/currency.

#### Backend

Status rate resolver uses StatusRateColumn's RateDefinition and WorkDate; ordinal calculator consumes only one AssignmentID. Reject unsupported generic methods; retain separate Bonus/rule contributions.

#### Frontend

Minimal existing status/entry compatibility; final configuration and tier authoring UX in P7.

#### Tests

VAC 8h×25=200; status branch isolation; direct status survives projection deletion; `1=3,2=4,3+=5, quantity=4 → 17`; gaps/overlap/final-open-ended, zero/missing, no mixed AssignmentIDs, immutable tier evidence and finalization snapshot-only.

#### Acceptance gate

PerUnit, OrdinalTier and status all calculate through target schedules with complete immutable evidence; no new status projection is written.

#### Risks

Tier shape edits racing Pending/Approved schedules, and status duplication during projection removal. Test one authoritative contribution per day.

#### Size

LARGE

### Phase 6 — Transfer and target compensation copy

#### Goal

Add the explicit transfer copy choice against target schedules only.

#### Why now

P1 owns correct profile movement; P3-P5 supply target schedules, status mapping and complete OrdinalTier semantics. Building this earlier against `copy_driver_rates` would be throwaway work.

#### Build

Transfer completion choice `Copy compensation to destination: Yes / No`, recorded with audit/outcome. Compensation service resolves source schedules at transfer EffectiveDate and creates independent destination assignments/values in one transaction. Copy whole PerUnit or OrdinalTier value sets. Default status column may map default→default; custom branch-owned columns require stable explicit mapping, otherwise skip and report. Never move source history. Do not silently copy DriverPayRules. No copy on No.

#### Keep

TransferRequest approval/lineage, DriverID history, target rate approval/finalized-period locks and explicit standalone pay-rule management.

#### Cut over

Transfer calls target Compensation copy only; legacy `/copy-rates` ceases to be the transfer design and can be retired after P7 removes its UI callers.

#### Delete

Any legacy transfer-copy bridge if one exists; `copy_driver_rates` endpoint/service in P8 after final UI/caller audit.

#### Database

Record explicit choice and mapping/skipped outcome on request/audit; destination FK/branch and overlap rules enforced. Rollback profile transition and copied schedules together if required copy fails; documented skips are explicit, not silent.

#### Backend

Keep Transfer as orchestrator of profile transition; Compensation owns schedule-copy selection/validation/persistence. For future effective dates, destination assignments begin at effective date; old source schedules and frozen payroll remain untouched.

#### Frontend

Add the choice to final transfer form in P7, not an interim legacy rate-copy UI.

#### Tests

Yes/No, future transfer, whole tier atomicity/rollback, custom status skip report, default mapping, no pay-rule copy, destination identity/branch, unchanged source assignments and historical payroll, DRIVER scope follows effective profile.

#### Acceptance gate

Complete transfer plus optional target copy works end to end without a `DriverRates` write.

#### Risks

Branch-specific custom status columns cannot be matched by name alone; explicit mapping identity or reported skip is required.

#### Size

MEDIUM

### Phase 7 — Final People and Compensation frontend cutover

#### Goal

Replace transitional UI with stable target contracts once backend authorities are proven.

#### Why now

Current People UI is User-centered, Pay Rates uses legacy matrix/copy routes, and Settings/CDPI still exposes PayItem/RateType language. Rebuilding these repeatedly during backend transitions wastes work.

#### Build

People tabs: Workforce (Employee list/detail, non-Driver/current/pending/history, termination), Access (User without Employee, explicit link, staged/provision/enable/reset, role/scope), Transfer Requests (effective date and copy Yes/No/result). Target Pay Rates definition/assignment/value authoring and history, including full OrdinalTier. Target definition/branch config, generic Definition Requests/governance, and status-column settings; CDPI is not a separate final compensation identity. Optional templates, if later product-approved, are convenience UX only. Payroll entry reads frozen target definition layout; status/bonus/rules stay distinct.

#### Keep

Useful page shell, roles editor, payroll period/review/ledger navigation and UI permission gates where their semantics remain sound.

#### Cut over

All live UI calls target Workforce, Access, Compensation and period APIs; no menu/action reaches legacy rate or generic Period Pay mutation endpoints.

#### Delete

Old People wizard, legacy Pay Rates matrix/copy UI, obsolete settings mappings and generic period-pay controls.

#### Database

No new domain tables expected; add only constraints required by surfaced target flow.

#### Backend

Tighten API response/read models discovered by end-to-end UI tests; remove temporary compatibility routes only after final client has moved.

#### Frontend

Implement final screens once; show explicit skip report and accurate pending/current profile distinction. The `/people/pay-rates` URL may redirect to the new compensation surface without preserving its legacy API. Official financial totals, finalized amounts and currency come from backend contracts; the client may format, sort, filter and group for display but may not reconstruct an official total (the post-P1 `FinalSummaryDialog` sums FinalLines in JavaScript floats and prints `$…toFixed(2)`) or assume two decimals.

#### Tests

Frontend build/lint and focused permission/UI tests; Workforce without User, external User without Employee, staged lifecycle, transfer choice, PerUnit/OrdinalTier authoring and entry, status separation, DRIVER cannot see admin screens or roster; payroll/ledger/final-summary surfaces display backend totals and currency, with no hardcoded `$` or fixed two-decimal money formatting.

#### Acceptance gate

Every supported product workflow is reachable through target UI and target APIs; no active frontend dependency on old authority remains.

#### Risks

UI permission hints are not an authorization boundary; backend negative tests remain mandatory.

#### Size

LARGE

### Phase 8 — Legacy deletion, clean reset and closure

#### Goal

Remove obsolete authority promptly and verify a fresh first-production shape.

#### Why now

P1-P7 have built and cut over every replacement. Pre-production demo data is disposable; keeping compatibility tables and adapters would invite new work against dead architecture.

#### Build

Static runtime/source consumer audit **and an implementation-time PostgreSQL catalog dependency inventory against the actual migrated database**, followed by technically correct cleanup migrations, a target-only seed where scenario data is useful (with zero-definition company validity also proven), clean development DB reset/reseed, fresh-upgrade and end-to-end smoke. Rework tests for business invariants; delete tests that only assert old RateType/PayItem map/tier structure. The catalog inventory is the final deletion authority, not source grep.

#### Keep

Payroll Setup authority, period/review/finalization/audit/locking, Workforce history, target compensation, BonusEvent, DriverPayRule and durable business-invariant tests.

#### Cut over

No remaining alternate authority. Stop old writes before dropping physical structures; old periods containing disposable demo data are reset by controlled dev-only process. Future production history semantics remain enforced by target schema/evidence.

#### Delete

Legacy PayItems authority and synthetic BONUS/min/max/ADJUSTMENT rows after separate evidence exists; RateTypes, Map, Slots, DriverRates, DriverRateTiers, generated CPI_/CDPI_/SRC_/STATUS_PAY, legacy implementations outside first-production methods, old generic Period Pay writer, `STATUS_PAYMENT` projection, dead CPI/backfill paths, CDPI-specific active runtime terminology/API/UI/service identity, obsolete mandatory starter/provisioning scaffolding if any remains, and obsolete EmployeeType/global-role tables only with zero-consumer proof. Do not delete `CdpiDefinitions` or other provenance storage solely because of its legacy name; retain it until equivalent generic PayDefinition governance/provenance is proven for origin, request, creator/approver, creation mode, approval/direct-create, schema/method version, lock, audit/timestamps, and applicable branch context. `Origin=Custom` alone is insufficient. Draft-line derived money columns (`RateAmount`, `CalculatedAmount`) are either dropped or explicitly documented and tested as non-authoritative caches (P4b rule); none may survive as a competing financial authority.

**Generic audit append-only hardening (P8b).** `audit.AuditLog` (base schema `0001`) is the generic application audit table written by Workforce, Access, payroll and settings services. Unlike the already-immutable domain evidence (`PayrollPeriodAuditEvidence*` in `0065`, `PayrollSetupPolicyAuditEvent*` in `0068`, calculation snapshots and final lines), it has no UPDATE/DELETE/TRUNCATE protection. No runtime path mutates it at the post-P1 baseline; only test fixtures delete from it. P8b adds DB-enforced append-only behavior (INSERT allowed; UPDATE, DELETE and TRUNCATE rejected), reusing the existing immutability-trigger pattern rather than a second audit system, and P8 test rework replaces fixture deletes with isolation that does not mutate audit rows. The immutable payroll evidence domains are not redesigned.

#### Database

Forward Alembic chain from 0071 must upgrade a fresh DB. **Before drafting or running any destructive DROP** for `RateTypes`, `DriverRates`, `PayItems`, Map/Slots/Tiers or related structures, query the actual migrated PostgreSQL catalogs: `pg_constraint` for inbound/outbound FKs and checks, `pg_depend` with `pg_class`/`pg_attribute` for dependent relations/columns/sequences, `pg_rewrite` for views/materialized views, `pg_trigger`, `pg_proc` and relevant index/policy metadata. Record each object and owner in a reviewable dependency register as **repointed**, **replaced**, **intentionally removed**, **historical-only**, or **blocker**. A historical-only database dependency still needs a deliberate retained/repointed evidence path; it is not permission to cascade-drop history. Abort the destructive migration for any unexplained dependency, validate the register against a fresh-upgraded target DB, drop in proven dependency order with `RESTRICT` rather than unreviewed `CASCADE`, then repeat the catalog query after migration. Historical migration files are never edited. Validate constraints and target seed. Do not build an elaborate business-data migration solely for disposable demos. Never drop a table still referenced by target finalized evidence.

#### Backend

Remove dead routes/services/schemas and test fixtures; search for runtime RateType/DriverRate/PayItem/PeriodPay references, not migration history. Ensure schema guard expects target head/invariants.

#### Frontend

Delete dead API clients/types/screens after routes are gone; smoke all final navigation and permission boundaries.

#### Tests

Fresh upgrade/reset/reseed, full backend regression, DB integrity, frontend build/lint/tests, target payroll E2E through finalized library/ledger, static no-legacy-runtime audit, pre/post DROP PostgreSQL catalog register review with zero unexplained dependencies, `audit.AuditLog` UPDATE/DELETE/TRUNCATE rejection with INSERT still working for every writer, a final alternate-financial-authority check (no read model, report, UI or export path derives official money from draft rows, live rates or client arithmetic), and independent architecture review.

#### Acceptance gate

Only target authorities remain operational; generic application audit is append-only; every physical legacy DROP has a catalog-backed dependency classification and no unexplained object; fresh DB and all product flows pass. C2 prerequisites in §10 are then met, subject to its own design review.

#### Risks

Hidden report/fixture/FK/view/function consumers may appear; a real or unexplained catalog dependency is a hard stop for its particular DROP, not license to remove its authority early.

#### Size

LARGE

## 6A. Dependency-Closed Implementation Work Units

These are review gates **inside** the eight macro phases, not more phases. Execute and review **one work unit at a time**; the next unit starts only after its prerequisite gate passes. Each row is normally its own PR. “Before/after” names the live authority at the end of that PR, including deliberately dormant target code or a short fail-closed operation gate. No row permits dual-writing an authoritative business record.

| Unit | Prerequisite | Authority before → after | Affected surface | Tests | Acceptance gate | Own PR? |
|---|---|---|---|---|---|---|
| **P1a COMPLETE** — DB invariants + company/effective-date resolver | §14 start condition | Status-only current Driver / weak constraints → resolver and integrity rules available; legacy write routes still live | `Employees`, `Drivers`, DB constraints/functions, eligibility helper | Same-company, immutable branch, overlap/concurrency, company date, old WorkDate | Resolver selects at most one DriverID/date; current writes cannot violate new DB invariants | Yes |
| **P1b COMPLETE** — Workforce write authority | P1a | Combined `/core/drivers` write → explicit Employee/profile service; transfer stays on old completion temporarily | Workforce service/router, core reads, non-Driver/pending profile | Independent Employee, first/pending profile, key/branch/status, authorization | Every non-transfer Workforce mutation uses new owner; incompatible old write route disabled | Yes |
| **P1c COMPLETE** — Transfer/termination cutover | P1b | Premature transfer branch mutation → effective transfer and atomic termination | Transfer service, projections, payroll eligibility, audit | Future/current transfer, pending termination, history/period eligibility, concurrent transition | Source/destination windows and Employee branch obey effective date; old completion path gone | Yes |
| **P2a COMPLETE — PR #24 / migration 0074** — User–Employee link + staged provisioning | P1c | Implicit/optional loose link and role-less login edge → explicit same-company link/staged account lifecycle; DRIVER side effect held until P2c | `sec.Users`, Access service, login, reset | Cardinality/company, unlink-relink, staged login, enable/disable/reset | Account provisioned atomically and cannot authenticate while staged/unprovisioned | Yes |
| **P2b COMPLETE — PR #26** — DRIVER/Self authorization hardening + route authorization inventory | P2a | Legacy ODA branch-like scope → generic Self resource scope; no Access BranchID; Access→Workforce role side effect removed in P2c | Permission evaluation, branch-access projection, login/me, payroll guards, roster/rate/payroll routes | Exact DRIVER/Self binding, effective identity, scope-safe overrides, owner scope, mixed-authority ceiling, fail-closed identity, route completeness | No Self path grants branch/company access; no DRIVER path obtains another resource or generic administrative mutation authority; every route is classified | Yes |
| **P2c COMPLETE — PR #28** — Remove Access→Workforce effects | P2b | Role save may create/move Workforce → Role/Scope write only | `admin/service.py`, legacy role path, People API compatibility | Assign/revoke/swap role leaves Employee/Driver/link unchanged | `ensure_driver_profile` and `EMP-{UserID}` role path removed; P2 security stays green | Yes |
| **P3a CLOSED — PR #30** — Company currency authority gate | P2c | Monetary writes without company currency → all reachable writers fail closed without configured code | Company config, legacy rate/bonus/rule/period/draft/status writers, submit/review/finalize, evidence | Each inventoried route and indirect batch/copy path, old-history reads, immutable currency and concurrency | No new durable money without configured code; no configured-code change after durable state; currency metadata available; payable-rounding decision remains open | Yes |
| **G0.1 CLOSED — PR #34** — Monetary Precision Contract Closure | P3a closed via PR #30; baseline `ddab66b7b14c8ddf2b986cc71df01a5fbe6bd663`, starting migration head `0076`; migration head after G0.1 `0077` | Bonus source precision 2 decimals / `NUMERIC(18,2)` → 4 decimals / `NUMERIC(18,4)`; batch canonical v2 with v1 replay compatibility | Bonus DB/API/batch, Bonus and DriverPayRule amount inputs, precision evidence tests, migration `0077` | Upgrade/downgrade safety; create/update/batch precision and idempotency; snapshot/finalization evidence; frontend regression | No silent rounding; currency minor units do not control source precision; payable rounding remains undecided | Yes |
| **G0.2 CLOSED — PRs #35–#39** — Continuous Validation / CI Baseline | G0.1 closed via PR #34; migration head `0077` | Validation remains a local execution gate → establish continuous repository validation | CI workflows, backend/frontend validation commands | Reproducible CI execution and required validation gates | Continuous validation baseline is implemented and enforceable | Yes |
| **G0.3 CLOSED — PR #40** — Currency concurrency lock ownership | G0.2 | Company currency guard coupled to unrelated locks → explicit Company row lock ownership | `company_concurrency.py`, monetary writers | Write-vs-currency-change concurrency | Currency lock ownership is explicit and narrow | Yes |
| **G0.4 CLOSED — PRs #41–#43** — Generic Period Pay, legacy period source and calculation contract | G0.3 | Generic Period Pay writer and legacy period source architecture → retired; calculation contract stabilized; migrations `0078`, `0079` | Period Pay, period source, calculation snapshot contract | Characterization and migration tests | No generic Period Pay mutation authority remains | Yes |
| **G0.5 CLOSED — PR #44** — Predecessor custom Pay Item writers | G0.4 | Predecessor custom Pay Item writers → retired; migration `0080` | Custom Pay Item request/write paths | Ownership retirement and migration tests | No predecessor custom writer remains reachable | Yes |
| **G0.6 CLOSED — PR #45** — Pay Profile family | G0.5 | Dead Pay Profile family → retired; migration `0081` | PayProfiles, PayProfilePayItems, PayProfileRates, PersonPayProfileAssignments | Migration and currency-authority tests | Dead profile state no longer locks Company currency | Yes |
| **P3b CURRENT — Target Compensation schema/invariants** | G0 closed (G0.1–G0.6 accepted) | Legacy rate tables operational → legacy remains operational; target schema dormant; migration `0082` | PayDefinition/RateDefinition/component/assignment/value tables, FK/locks | Ownership, no overlap, complete set, structure/pending race, fresh upgrade | Target persistence cannot admit mixed/partial effective schedules | Yes |
| P3c Dormant target authoring/resolver | P3b | Legacy PayDefinition/PerUnit operational → legacy still operational; target authoring testable but dormant | Target APIs, generic governance/provenance, resolver; no named template requirement | PerUnit date/zero/missing, company variation, no generated RateType | Target resolver returns one whole assignment; no target write is exposed as operational | Yes |
| P4a Generic PayDefinition/branch-config authority switch | P3c | Global/system PayItems and legacy CDPI/config/PerUnit authoring → one generic company PayDefinition governance/config authority; new period creation temporarily fail closed | Settings reads/writes, generic definition governance, branch applicability, Pay Rates API, transitional state if needed | Generic request/approval/direct-create, arbitrary definitions, old-route denial, concurrent old/target attempts, fail-closed period gate | Exactly one PayDefinition creation/config authority; no new RateType/Map/Slot generated; zero definitions remains valid | Yes |
| P4b Canonical period layout + PerUnit calculation | P4a | No new period allowed after P4a → existing creator snapshots target layout and target PerUnit calculation works; submit/finalize temporarily fail closed | `period_creation.py`, period snapshot, draft calculation/entry | Candidate replay, Setup provenance, branch activation, 8×25, WorkDate, gate, stored-draft-money tamper | Target period creation is atomic in existing creator; no second creator or legacy PayItem snapshot; stored draft money is not read as authority for target periods | Yes |
| P4c Immutable evidence/finalization/read-model cutover | P4b | Target period cannot submit/finalize → target packet, hash, evidence, final lines, reports/ledger operational; interval gates removed | Submit/review/finalization, used-rate evidence, library/reports/ledger | Live-rate edit cannot alter snapshot, no finalization re-resolution, totals/read parity, Min/Max reproducibility, mandatory approved rounding policy frozen in hash | End-to-end target PerUnit period, including DriverPayRule outputs, is explainable from frozen evidence alone; final payable follows an approved, implemented currency-aware rounding policy | Yes |
| P5a Direct Status compensation | P4c | Status RateType plus `STATUS_PAYMENT` projection → branch StatusRateColumn→RateDefinition and direct DayEntryState evidence | Status config, resolver, calculation, evidence, projection writer | VAC 8×25, branch isolation, no duplicate payment, history | Status amount comes once from target schedule; projection stops writing | Yes |
| P5b OrdinalTier completion | P5a | Legacy tier/unsupported method paths → target definition topology and atomic assignment values | Definition governance, authoring, calculator, evidence, method availability | 3+4+5+5=17, gaps/overlaps/open end, zero/missing, mixed-ID refusal | PerUnit/OrdinalTier only; tier result and topology frozen in evidence | Yes |
| P6a Transfer target compensation copy | P5b and P2c | Correct profile transfer without copy → explicit Yes/No and target assignment/value copy | Transfer request/service, Compensation copy API/audit | Yes/No, full tier rollback, status default/custom mapping/skips, no pay-rule move | Destination owns complete new schedules; source history untouched | Yes |
| P7a Final People/Access/Transfer UI | P6a | User-centered wizard/old transfer form → Workforce, Access, Transfer tabs on target APIs | `PeoplePage`, Access/role flow, transfer form | Employee without User, external User, staged, copy result, DRIVER privacy | No UI call can trigger Access→Workforce side effect or legacy transfer copy | Yes |
| P7b Final Compensation/Settings UI | P7a | Legacy Pay Rates/Pay Items UI → target assignments, generic definitions/governance, and status config | Pay Rates, Pay Items, Status settings, clients/types | PerUnit/Ordinal authoring, branch config, definition governance, status mapping | All configuration and rates screens call target APIs only; CDPI is not a separate target identity | Yes |
| P7c Payroll-entry target-contract cleanup | P7b | Old PayItem/generic Period Pay controls → target period layout and separate status/bonus/rules UX | Entry, period detail, reports/ledger navigation | Frozen layout, missing rate, status/bonus/rules, frontend build/lint | No active frontend legacy rate or generic monetary writer call | Yes |
| P8a Runtime legacy deletion | P7c | Legacy code/routes still present but unused → target-only runtime/source | Backend routes/services/schemas, frontend clients/tests | Full regression, static runtime consumer audit | No reachable legacy authority remains; destructive DB schema still intact | Yes |
| P8b Catalog-proven FK/schema cleanup | P8a | Unused legacy tables/FKs remain → target-only physical schema | Actual migrated PostgreSQL catalogs, forward Alembic cleanup | Pre/post catalog register, fresh-upgrade DB integrity, `DROP ... RESTRICT`, `audit.AuditLog` append-only | Every dependency classified; zero unexplained dependencies before any DROP; generic audit rejects UPDATE/DELETE/TRUNCATE | Yes |
| P8c Clean reset/reseed/closure | P8b | Target schema with disposable old demo DB state → one target seed and verified clean DB | Dev reset, seed, backend/frontend/E2E validation | Fresh upgrade, payroll finalized evidence, permission matrix, full suites | §15 completion condition and §10 C2 gate demonstrably pass | Yes |

## 7. Legacy Deletion Schedule

The table names the *earliest safe* deletion. A cutover is accepted only with working replacement and regression proof. Historical migration files remain immutable; deletion means new forward migrations and removal of runtime code/UI. Execution status: the P1-scheduled runtime replacements (generic Driver create/patch, premature transfer branch mutation, core current-profile resolution) are complete through PR #22; their P8 column/schema cleanup remains. Remaining status-filtered roster and period-overlap readers (for example dashboard/current-hub rosters and the no-snapshot legacy fallback in `eligibility.py`) are not current-profile resolvers; P2b owns any DRIVER/roster exposure among them and P8a's static consumer audit owns the rest.

| Legacy component | Replacement | Replacement phase | Cutover phase | Delete phase |
|---|---|---:|---:|---:|
| User-centered People wizard / implicit Employee creation | Workforce and Access transactions | P1/P2 | P2 backend, P7 UI | P7 |
| Generic Driver create/patch and status-only helpers | Workforce profile service/effective resolver | P1 | P1 | P1/P8 column cleanup |
| Premature transfer branch mutation | Effective profile transition/projection | P1 | P1 | P1 |
| `EmployeeType` runtime/column | Effective Driver profile truth | P1 | P1 | P8 |
| DRIVER substring matching and ODA as target scope | Exact DRIVER identity + generic Self resource scope | P2b | P2b | P2b |
| Temporary `ensure_driver_profile`, EMP-User key and implicit User.EmployeeID assignment | Explicit Access role assignment with separate link authority | P2c | P2c | P2c |
| Legacy global Role path | CompanyRole assignment/permission path | P2 | P2 | P8 after proof |
| Global/system PayItems operational authority | Company PayDefinition | P3 | P4 | P8 |
| BranchPayItemConfig old FK/shape | Target branch definition config | P3 | P4 | P8 |
| CDPI RateType/Map/Slot triple and CPI backfills | Generic PayDefinition governance/provenance | P3 | P4 | P8 |
| RateTypes and generated CPI_/CDPI_ identities | RateDefinition/components | P3 | P4 | P8 |
| PayItemRateTypeMap and RateSlots | Direct definition/component relation | P3 | P4 | P8 |
| PerUnit legacy DriverRates | Target Assignment/Value | P3 | P4 | P8 |
| StatusRateColumn RateType FK and SRC_/STATUS_PAY | Column→RateDefinition | P5 | P5 | P8 |
| STATUS_PAYMENT DraftLine projection | Direct DayEntryState status calculation/evidence | P5 | P5 | P5/P8 schema cleanup |
| DriverRateTiers, RangeBracket/RangeProgressive/Block | Target OrdinalTier + PerUnit only | P5 | P5 | P8 |
| Generic Period Pay writer / Adjustment PayItem | Target measured input, BonusEvent, DriverPayRule | P4/P5 | P4/P5 | P8 |
| Synthetic BONUS/min/max PayItem authority | Separate event/rule evidence | P4/P5 | P4/P5 | P8 |
| Legacy `copy_driver_rates` and optional pay-rule copy | Target complete assignment copy | P6 | P6/P7 | P8 |
| PayItem-keyed report/ledger fallbacks | Frozen target definition/evidence read model | P4/P5 | P4/P5 | P8 |
| Temporary target provisioning scaffolding, if introduced | Final company definition state | P3 | P4a | P4a |

## 8. Cross-Domain Contracts

1. **Transfer:** Workforce owns Employee/Driver lifecycle. Source profile closes at `EffectiveDate - 1`; destination starts at `EffectiveDate`; old DriverID and its rates/payroll never move. Future completion cannot make destination current early. Termination atomically closes current and pending activity, without changing historical evidence.
2. **Compensation copy:** The transfer request records Yes/No. Yes invokes Compensation target copy within the transition transaction. It copies an entire effective source PayDefinition assignment to new destination-owned assignment/values, including all OrdinalTier values atomically. Default status column may map to target default. Custom StatusRateColumns require stable explicit mapping or are skipped with a returned/audited report; normalized name is insufficient. DriverPayRules never ride this option. No invokes no copy.
3. **Payroll calculation:** Payroll requests exactly one complete `(DriverID, RateDefinitionID, WorkDate)` assignment, checks target definition/branch/Driver applicability, and uses only that AssignmentID's values. Payroll never receives RateType as an operational input. Missing is an approval blocker; zero is legitimate.
4. **Period snapshots:** The existing candidate creator (`period_creation.py:753-1003`) remains sole authority. In its existing atomic period/day/layout/eligibility transaction it freezes operational company PayDefinitions and branch activation for the period. Prepared Draft eligibility remains provisional and Open frozen per existing lifecycle; compensation snapshot rules must use the same boundary without reopening Payroll Setup.
5. **Evidence/finalization:** Live calculation resolves current applicable target assignments and freezes definition/method/version, RateDefinition, AssignmentID, full values/topology, WorkDate/input, currency and result. Submit/review binds a hash of the packet. Finalization projects only the approved packet, never live compensation tables (`finalization.py:158-314`). Finalized library/reports read frozen meaning.
6. **Termination:** Workforce closes effective activity and handles pending profiles; Compensation assignments remain attached to original DriverID and are not rewritten. Access link remains, but DRIVER current-profile authorization fails closed. Locked/finalized payroll never changes.
7. **Access/security:** `DRIVER ⇔ Self` is the only currently supported Self binding. Self has no Access BranchID and never grants branch access. Current Driver identity comes from User.EmployeeID→Employee→the effective Driver profile for the operation date; history is limited to that Employee where explicitly allowed. Overrides may widen actions only within existing resource scope and cannot bypass Self ownership or the DRIVER capability ceiling. No generic roster, payroll, rate, Workforce, role or permission mutation is allowed to DRIVER, including against its own Driver. Mixed DRIVER/admin rows do not create mixed-mode authority. COMPANY_OWNER dynamic permissions require a valid active company-wide assignment. Workforce transfer changes Employee.BranchID according to its own authority and does not mutate Access Self scope.
8. **DriverPayRule Minimum/Maximum:** DriverPayRule remains an independent payroll-rule domain. At the post-P1 baseline `MinimumPay`/`MaximumPay` are evaluated per DriverID per PayrollPeriod: the rule effective on the period start date is selected, the qualifying base is that period's daily + status + period-scoped pay (Bonus excluded and added after), and the full amount produces a period-scoped top-up or cap. That is the v1 meaning: an amount is scoped to one applicable PayrollPeriod. It is not a salary periodicity value, an annual/monthly/weekly amount, a year-to-date floor, a recoverable draw or a lifetime cap, and no generic periodicity/`Basis` field is added to DriverPayRule. Payroll Setup schedule/frequency changes never rewrite DriverPayRule amounts; any economic-impact notice is advisory and any amount change is an explicit DriverPayRule edit. Minimum greater than Maximum for the same period is a hard blocker, never silently clamped; a large legitimate top-up is valid arithmetic and is not altered. Transfer never copies or transforms DriverPayRules (§8.2). Partial-period application (hire, termination, transfer or destination start inside a period) is an open product decision (§12A) that must be resolved or explicitly deferred before the P4c target Min/Max contract is frozen. Annual/cumulative earnings guarantees, recoverable draws, annual settlement targets or category-specific yearly caps are distinct future product capabilities and must not be introduced through a DriverPayRule frequency field.
9. **Financial authority layers:** For target periods, operational persistence owns source facts, backend calculation owns live derived money, and immutable submitted/final evidence owns frozen money. Stored draft amounts, live rate tables and client arithmetic never compete with those layers (P4b, P4c, P7c, P8). Internal calculation precision, payable rounding and display formatting are separate; the payable-rounding policy is a §12A decision that must be approved and implemented before P4c acceptance.
10. **Finalized corrections:** Locked/Finalized payroll is never reopened, re-projected or rewritten. "Correction" never means editing a Locked/Finalized period. A future correction capability is a separate auditable record that references the original frozen evidence and settles through a separately approved payment process; carry-forward into a later regular payroll is a candidate, not a decision. Off-cycle and retro payroll are not part of this plan. P4c keeps the snapshot-identity seam that makes such a record additive (Phase 4 *P4c additional evidence obligations*).
11. **Downstream identity:** Employee is the durable person/Workforce identity; DriverID is a branch-bound historical payroll-profile identity, so one transferred Employee legitimately has several DriverIDs. No export, report or integration may assume `DriverID == person`. Employee-level roll-up is derivable because Driver EmployeeID/CompanyID are immutable (P1a). EmployeeKey is not presumed to be an external payroll-vendor identifier; the export contract (Product Readiness C3, after P8) must distinguish EmployeeID, EmployeeKey and any external mapping identifier and define Employee-level roll-up of multiple Driver-profile contributions.

## 9. Authentication Boundary

| Mechanic | Decision | Evidence and required adaptation |
|---|---|---|
| bcrypt hash/verify | KEEP | `auth/security.py:16-33`; `admin/service.py:762-817` reuses it for reset |
| Login credential/company checks | KEEP_AND_ADAPT | `auth/service.py:40-150` checks `CanLogin`, `IsActive`, company state, password and active role; add explicit staged/provisioned and DRIVER effective-profile fail-closed checks |
| JWT issuance/verification | KEEP | `auth/security.py:40-77` creates signed expiring HS256 token; `dependencies.py:42-55` decodes it |
| `/auth/me` and permission authority | KEEP_AND_ADAPT | Reload current DB authority and staged state; represent Self independently from branch membership without fabricating a branch row |
| Admin password reset | KEEP | `admin/service.py:762-817` locks row, resets hash/failure state and audits |
| Public signup placeholder | KEEP | `frontend/src/App.tsx:108` routes to `SignupPage.tsx`, whose inputs and button are disabled; it is not an account creation endpoint. No public self-signup is added for this refoundation. |

The account lifecycle required now is: admin creates User (optionally unlinked and staged/login-disabled), explicitly links Employee when intended, provisions Role+Scope atomically, enables/disables login, and resets password. A linked pending Driver remains staged until effective if DRIVER is its only intended role. Invitation email and first-login onboarding are deferred product UX and do not gate closure. Authentication mechanics remain separate from Access provisioning.

## 10. C2 Import Gate

**Full C2 remains blocked through P8.** Before C2 implementation starts: (1) Workforce import must be able to resolve one canonical Employee by company and stable key/external mapping, then one effective branch-specific DriverProfile by branch and date; (2) Access import, if any, must call explicit same-company link and staged/provisioning transactions, never role-to-Employee side effects; (3) rate import must resolve company PayDefinition/RateDefinition and write one complete effective-dated DriverRateAssignment with DriverRateValues, including OrdinalTier atomicity and status-column mapping rules; (4) period snapshots/calculation/evidence must consume those target identities; (5) old rate authorities and demo seed dependencies must be gone; (6) fresh DB upgrade/reset and full target regression must pass. Workforce completion alone can support design/spike work, not full C2 authority. C2 is the next program after this plan, not a ninth phase.

## 11. PR Strategy

Use §6A in order: one reviewable PR/work unit at a time, with its stated gate passing before the next unit. Do not combine P1-P8 into one rewrite PR. Every PR states old authority still live, new authority status, caller/FK audit, tests and rollback/reset behavior. A target table may land dormant before cutover; no dual-writing old and new rates. P4a's authority-switch PR includes generic PayDefinition governance and configuration paths plus legacy-CDPI transition readers and writers and the temporary period-creation hold; P4b/c release their respective holds only after target snapshot and evidence tests pass. Deletion PRs follow passed target cutovers, not merely schema creation. Review each cutover explicitly; preserve the locked contract.

## 12. Hard Stop Conditions

- A locked People/Workforce/Access rule cannot be satisfied without changing its product meaning; amend the locked contract before code changes.
- Current canonical period creator or finalization is found to have a second live authority contrary to `period_creation.py`/`0071`, requiring an actual architecture decision rather than an adapter.
- An old table proposed for deletion still supplies unique immutable finalized meaning that target evidence has not captured; build/test the missing evidence before deletion.
- The migrated PostgreSQL catalog shows an unexplained FK, view, function, trigger, index or other dependency on a legacy object proposed for DROP; classify/repoint it and rerun the catalog audit before that destructive migration.
- Target assignment completeness/non-overlap or effective Driver exclusivity cannot be enforced under the actual DB transaction/concurrency model; choose and prove a DB/service mechanism before cutover.
- A custom status-column copy cannot be mapped safely; skip/report that column rather than guessing. This is an operation-level stop for that copy, not a plan-wide redesign.

These are implementation discoveries to investigate at their phase. Phase 1 and Phase 2 are closed. P3a is closed and merged via PR #30. G0 (G0.1–G0.6) is closed, ending with PR #45 at migration head `0081`. P3b is the current implementation work unit.

## 12A. Open Product Decisions and Deferred Capabilities

These are deliberately **not** decided by this plan. Each row states the latest point at which the decision must be resolved and whether deferral is permitted. Architecture must leave room for each answer; none authorizes a new phase or speculative abstraction.

| Decision / capability | Status | Latest resolution point / deferral rule | Constraint meanwhile |
|---|---|---|---|
| Partial-period DriverPayRule Min/Max (hire, termination, transfer, destination start inside a period); candidates such as full amount or proration by eligible days | OPEN | Before P4c freezes the target Min/Max evidence contract; resolve or Lead-recorded explicit deferral permitted (§8.8) | Current full-amount, period-start-selected behavior is the baseline; no worked-day thresholds, attendance formulas or policy framework; if a policy is approved, evidence records it (§8.8) |
| Payable rounding method, boundary, residual/reconciliation treatment, retention of unrounded values | OPEN | MUST be resolved before P4c acceptance; deferral not permitted — the approved policy must be implemented, tested and frozen in evidence by P4c | Internal `0.0001`/`ROUND_HALF_EVEN` calculation precision unchanged; no implicit payable-rounding rule before P4c; no universal two-decimal rule (Phase 3 decision gate) |
| Explicit `PayrollRun` entity vs PayrollPeriod as execution identity | OPEN — not required now | Only when a real off-cycle/correction requirement is approved | PayrollPeriod stays the one regular execution; evidence references snapshot identity; no second creator or lifecycle |
| Finalized-payroll correction settlement mechanics (carry-forward, separate payment, other) | OPEN | Before any correction feature is designed | §8.10: never reopen or rewrite finalized payroll |
| Annual/cumulative guarantees, recoverable draws, yearly caps | DEFERRED future capability | Separate product definition (window, eligible earnings, accumulation, settlement, true-up, proration, recovery, evidence) | Never modeled as a DriverPayRule frequency (§8.8) |
| Payroll Setup schedule-change economic-impact notice for affected DriverPayRules | DEFERRED product enhancement | Not a refoundation gate | Advisory only; Payroll Setup never mutates DriverPayRule amounts |
| Export identity and Employee-level roll-up contract | DEFERRED to Product Readiness C3 | Before export implementation | §8.11 |

## 13. Execution Order

1. P1 Workforce authority and effective-date integrity; correct core transfer and termination. **COMPLETE (P1a–P1c, PR #22).**
2. P2 Access account/link/provisioning and DRIVER Self boundary (P2a–P2c complete).
3. P3 company currency and dormant target Compensation definitions/assignments.
4. P4 current canonical period PayDefinition snapshots, PerUnit calculation and immutable evidence cutover.
5. P5 direct status compensation and complete OrdinalTier, including frozen evidence.
6. P6 transfer's explicit target compensation copy with status mapping/skip reporting.
7. P7 final People/Access/Transfer, Pay Rates, payroll entry, generic definition governance, and Status UI cutover.
8. P8 remove obsolete authority, clean development reset/reseed and closure validation.
9. After P8 acceptance, separately authorize/design C2 Import against canonical Workforce and Compensation.

## 14. Initial Start Condition — SATISFIED

The original program start condition was satisfied before P1a began. Phase 1 and Phase 2 are closed. P2c merged through PR #28 as `af00a22915f93810d123aadea18c3a73da1360aa`; Test Hygiene PR #29 merged afterward. Historical start-condition baseline: `main` at `f671a2ee61e9d31b0804af95eaff9973978a94af`, migration head `0075`; P3a was then implemented under review on a branch at head `0076`. Current status: P3a is closed and merged via PR #30. G0 (G0.1–G0.6) is closed. G0.1 closed via PR #34; it started from `main` at `ddab66b7b14c8ddf2b986cc71df01a5fbe6bd663` with migration head `0076` and brought the head to `0077`. G0 ended with PR #45 at `f2937bfdcbe1266a00716905a8712f3fe29baff2`, migration head `0081`. P3b is the current implementation work unit.

Original start condition (historical record): record acceptance of this unified order against both source plans and the locked contract; confirm the exact branch/HEAD and migration-script/actual DB heads; confirm a disposable development DB and reset procedure; identify every active caller of current-driver, legacy-rate and period-layout paths; select P1a with its acceptance tests as the first reviewable work unit. This hardening task does not implement it.

## 15. Completion Condition

The refoundation is done when: Employee and DriverProfile are independent from User and effective/branch history is enforced; Access linking/provisioning has no Workforce side effects and the completed DRIVER/Self boundary remains enforced; generic company-owned PayDefinitions and branch StatusRateColumns resolve complete PerUnit/OrdinalTier target assignments without named starter requirements; canonical Payroll Setup period creation freezes target definitions; WorkDate calculation and immutable evidence use target IDs/values/currency; finalization is snapshot-only; transfer copies target schedules only on explicit Yes; BonusEvent and DriverPayRule stay separate; active UI uses target APIs and generic definition governance rather than CDPI as a separate identity; no operational RateType/Map/Slot/DriverRate/Tier, generic Period Pay writer, generated compatibility rate identity or STATUS_PAYMENT projection remains; fresh DB upgrade and target regression pass. This is the existing eight-phase program; no additional macro phase is introduced.

## K. Additional Refoundation Candidates

No additional refoundation candidate identified from current repository evidence.
