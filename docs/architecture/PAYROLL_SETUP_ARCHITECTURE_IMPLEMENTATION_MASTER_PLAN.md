# Flussra Payroll Setup Architecture Implementation Master Plan

**Status:** Approved for Phase 0

**Purpose:** Implement the closed Payroll Setup Architecture Contract safely and make it the single payroll-schedule authority.

**Repository baseline:** `main` at `70176a2a61c11b3b34f26b99916622f3525ddd21`

**Alembic baseline:** `0065`

**Development database baseline:** zero business/application rows after clean reset
**Execution position:** This plan is a blocking architecture track before C2 Import. Resume C2 only after this plan reaches `CLOSED`.

---

## 1. Authority and scope

This document is the implementation authority for the Payroll Setup redesign.

It implements the already-approved architecture:

```text
Company
  └─ Payroll Setup
       └─ Draft / Published immutable Versions

Branch
  └─ Effective-dated persisted Assignment → Payroll Setup

Payroll Period
  └─ Exact Assignment
  └─ Exact Published Setup Version
  └─ Frozen schedule provenance
```

This plan does **not** reopen the architecture decision.

The following contract decisions are locked:

- Payroll Setups are company-owned and reusable across branches.
- A company may own multiple Payroll Setups.
- A company may designate one active default Payroll Setup.
- The default is an onboarding default only; payroll runtime never falls back to it.
- Branches receive persisted, effective-dated Payroll Setup assignments.
- Branch assignments may change over time but may not overlap.
- Published Payroll Setup Versions are immutable.
- Version authority is resolved by effective date, not by `CurrentVersionID`.
- `PayrollPeriod.StartDate` determines the governing assignment/version.
- A payroll period may not cross an assignment or version boundary.
- Every new payroll period permanently binds to the exact assignment and published version used to create it.
- Historical payroll never re-resolves mutable current setup state.
- `BranchPayrollSettings`, branch-owned schedule versions, and `CurrentScheduleVersionID` must cease being runtime authority.
- Legacy pay-date fields remain deferred/non-authoritative until a separate Pay Date Policy exists.
- Existing calculation, immutable snapshot, finalization, FinalLines, finalized reporting, and P6D evidence architecture must not be rewritten unless a concrete dependency requires adaptation.

---

## 2. Relationship to the Post-P6D roadmap

This is a focused architecture intervention inside Product Readiness.

The current Post-P6D roadmap remains authoritative for the broader cleanup/product-readiness sequence. This plan temporarily blocks **C2 Inbound Data and Import** because import must resolve payroll schedule authority correctly before it can safely validate or commit period-aware operational data.

Execution order is therefore:

```text
Existing completed cleanup / Current Payroll alignment
        ↓
Payroll Setup Architecture redesign — THIS PLAN
        ↓
C2 Import / inbound data
        ↓
Export / integrations / pilot validation
```

Do not begin C2 implementation while this plan is materially incomplete.

---

## 3. Non-goals

This redesign must not opportunistically absorb unrelated payroll domains.

The following remain separate unless a later explicit architecture decision says otherwise:

- Pay Items and branch PayItem activation
- Driver Rates
- Driver Pay Rules
- Status Keys
- Status Pay
- Bonus Events
- calculation formulas
- min/max behavior
- review/approval financial authority
- finalization
- FinalLines
- finalized-library evidence
- frozen report evidence
- P6D immutable audit evidence
- automatic Pay Date Policy

The redesign is a **schedule source-of-truth replacement**, not a payroll-engine rewrite.

---

## 4. Current baseline and special zero-data rule

The development database was reset and rebuilt from migrations through `0065`.

Verified business/application row counts after reset:

```text
Companies: 0
Branches: 0
Users: 0
Employees: 0
Drivers: 0
Payroll Setups: 0
Payroll Periods: 0
Audit Log: 0
Alembic: 0065
```

Implications:

1. We do not need a customer-data backfill for this development database.
2. We may make the correct long-term schema change without preserving demo rows.
3. We must **not** rewrite migrations `0001`–`0065` merely because local data is empty.
4. New architecture begins with forward migrations `0066+`.
5. Any destructive legacy retirement migration must fail safely if unexpected legacy business rows/references exist in another environment.
6. Empty local data does not justify silent truncation, cascade deletion, or permanent dual authority.

---

## 5. Execution principles

### 5.1 One authority at completion

At the end of this plan, payroll schedule authority must have one path only:

```text
Branch + PeriodStartDate
  → effective BranchPayrollSetupAssignment
  → PayrollSetup
  → effective Published PayrollSetupVersion
```

No completed runtime path may read `BranchPayrollSettings` as authoritative schedule configuration.

### 5.2 Forward-only migration discipline

- Never rewrite `0001`–`0065` for this redesign.
- Use `0066+` migrations.
- Fresh install from `0001 → head` must pass.
- Upgrade from an empty `0065` database to new head must pass.
- Legacy retirement must happen only after runtime cutover.

### 5.3 Preserve correct invariants

Keep working protections wherever possible:

- branch workflow advisory locking
- signed candidate identity/replay protection
- stale candidate rejection
- one-slot / overlap protections
- immutable PeriodDays
- frozen PayItem layout
- driver eligibility snapshots
- immutable submitted/approved calculation snapshots
- approved finalization authority
- FinalLines
- frozen reports
- P6D immutable period audit evidence

### 5.4 No speculative compatibility layer

A temporary adapter is acceptable only for controlled deployment sequencing.

It must:

- never become authoritative;
- never accept writes after cutover;
- resolve through the same canonical resolver if it remains temporarily readable;
- have an explicit deletion gate.

---

# 6. Target domain model

The implementation must produce the following conceptual entities.

## 6.1 `PayrollSetup`

Stable company-owned identity for a reusable payroll schedule policy.

Required semantic fields:

- `PayrollSetupID`
- `CompanyID`
- stable integration-safe code
- display name
- description/notes
- lifecycle status: Active / Archived
- created metadata
- updated metadata for mutable identity fields only

Rules:

- belongs to exactly one Company;
- code is unique within Company;
- branches do not own it;
- historical usage prevents hard deletion;
- default Setup must be Active;
- archiving is rejected while a non-withdrawn assignment could still govern new period creation; historical bound periods remain readable without re-resolving current Setup state.

## 6.2 `PayrollSetupVersion`

Complete versioned schedule configuration.

Required semantic fields:

- `PayrollSetupVersionID`
- `PayrollSetupID`
- `CompanyID` if retained for composite integrity/performance
- lifecycle state: Draft / Published
- `VersionNumber` nullable until publication
- `EffectiveFromDate` required when Published
- complete schedule configuration
- canonical `ConfigHash`
- publication actor/time
- `ReplacesVersionID`, required for same-date append-only correction and otherwise nullable
- created metadata

Initial authoritative schedule configuration:

- `PayrollFrequency`
- `AnchorStartDate`
- `CustomIntervalDays`
- `NormalDaysOffMask`

Deferred/non-authoritative fields must not govern this version:

- `PayDateOffsetDays`
- `PayDayOfWeek`
- `FirstPayDate`
- `IncludePayDayAsWorkDay`

Rules:

- Draft may be edited/discarded.
- Published version is immutable and non-deletable.
- Version number is unique and monotonic per Setup.
- Several future published versions may exist.
- There is no authoritative `CurrentVersionID`.
- Effective end is derived from the next distinct effective start.
- Same-date published correction must append a replacement, never edit history.
- A same-date replacement must identify the version it replaces and form one non-forking replacement chain.

## 6.3 `BranchPayrollSetupAssignment`

Persisted authority assigning one Branch to one Setup for a date interval.

Required semantic fields:

- `BranchPayrollSetupAssignmentID`
- `CompanyID`
- `BranchID`
- `PayrollSetupID`
- `EffectiveFromDate`
- `EffectiveToDate` nullable, exclusive
- active/withdrawn state or equivalent immutable withdrawal evidence
- created actor/time
- change reason / audit correlation

Rules:

- range semantics are `[EffectiveFromDate, EffectiveToDate)`;
- non-withdrawn ranges for the same Branch may not overlap;
- a payroll-ready active Branch must not have a gap in required coverage;
- assignment and Setup must belong to the same Company as the Branch;
- an effective assignment is not silently rewritten;
- future assignments may be withdrawn/replaced if no non-cancelled period depends on them;
- reassignment boundary must be a valid period boundary for both authorities;
- an existing non-cancelled period at/after or crossing the proposed reassignment boundary blocks the change.

For every reassignment or assignment withdrawal, validate the proposed boundary against the predecessor and successor schedules, all existing non-cancelled periods, and any future assignment/version boundaries for that Branch. Do not use wall-clock today or Company timezone as an authority.

## 6.4 Company default Payroll Setup

Company gains a nullable default Setup reference during onboarding.

Rules:

- must reference an Active Setup in the same Company;
- runtime payroll never falls back to it;
- changing it affects future branch assignment creation only;
- existing Branch assignments never change automatically;
- a default Setup cannot be archived until another default is selected.

## 6.5 Payroll Period provenance

Every new Payroll Period must retain:

- exact `BranchPayrollSetupAssignmentID`
- exact `PayrollSetupVersionID`
- frozen canonical configuration hash
- frozen schedule-provenance fields needed for audit readability
- existing immutable period dates and snapshots

Frozen provenance fields:

- frequency
- anchor
- custom interval
- normal-days-off mask
- setup ID/code for readable provenance
- version number for readable provenance
- configuration hash

The FK identifies authority. The copied values are defense-in-depth/audit evidence, not a second configuration source.

---

# 7. Canonical effective-date resolver

There must be one canonical resolver used by all payroll-period workflows.

Conceptual contract:

```text
resolve_payroll_setup_version(
    company_id,
    branch_id,
    period_start_date
)
```

It must:

1. Verify Branch belongs to Company and is operationally eligible.
2. Find exactly one non-withdrawn assignment containing `period_start_date`.
3. Verify assigned Setup belongs to Company and is Active.
4. Find the latest Published version with `EffectiveFromDate <= period_start_date`.
5. If same-date replacements exist, resolve the terminal non-forking replacement.
6. Verify `period_start_date` is a valid boundary under that version.
7. Compute the candidate period end using the selected schedule.
8. Determine the next assignment/version authority boundary.
9. Reject if the proposed period crosses that boundary.
10. Return assignment, setup, version, configuration, hash, and boundary evidence.

The resolver must have no dependency on wall-clock “today” or Company timezone for authority selection.

`PayrollPeriod.StartDate` is the canonical resolution date.

Boundary validation is performed per affected Branch against its complete persisted assignment and published-version timeline. For an existing Branch, derive legal period starts from non-cancelled period chronology and advance the predecessor authority's theoretical cadence through the proposed boundary, even when intervening periods have not been created; when no period exists, use the explicit first start. Validate that the proposed date is a boundary under both predecessor and successor schedules, and reject it if it splits or invalidates any existing non-cancelled period or conflicts with a future boundary. Validate all future assignments and versions affected by the change. Cancelled periods retain frozen history but do not reserve chronology. Cover Week, Biweek, Month, and Custom schedules, including shared Setups whose Branches have different period histories.

---

# 8. Concurrency and locking contract

The redesign must preserve existing branch workflow serialization while extending protection to shared policy mutation.

All mutating workflows use this global lock order, omitting locks not needed by an operation:

1. Company onboarding/default serialization lock, when changing the default or creating a Branch from it;
2. Setup timeline locks in ascending Setup ID order;
3. Branch workflow locks in ascending Branch ID order.

Assignment, reassignment, withdrawal, default-derived Branch creation, and publication must participate in Setup timeline serialization so the affected-Branch set cannot change during validation. Reassignment locks both source and destination Setup timelines in ascending ID order. Branch creation from the default reads and validates the default while holding Company serialization, then joins the Setup timeline before creating the Branch and Assignment atomically. Publication allocates version numbers while holding the Setup lock, materializes affected Branches, locks them in ascending Branch ID order, then re-reads assignments and non-cancelled periods before validation and commit. Period creation holds its Branch workflow lock, re-resolves authority, and must never acquire a Setup timeline lock after taking a Branch lock. Mutations that touch multiple lock classes must follow the order above to avoid deadlocks.

## 8.1 Period preview

```text
Branch
 → resolve assignment
 → resolve published version
 → compute dates
 → validate slot
 → sign candidate
```

Candidate identity must include enough evidence to stale correctly, including at minimum:

- Company
- Branch
- Assignment ID
- Setup Version ID
- configuration hash
- a Setup timeline revision/fingerprint and Branch assignment timeline revision/fingerprint
- candidate start/end
- slot/replay evidence already required by current logic

## 8.2 Period confirm/create

```text
acquire branch workflow lock
 → resolve again
 → compare with candidate
 → reject stale authority/dates/slot
 → create period
 → freeze assignment/version/provenance
 → create existing snapshots
 → commit atomically
```

A version publication or branch reassignment after preview must stale the candidate.

Any Setup publication/replacement or mutation of that Branch's assignment timeline after preview stales an uncreated candidate, even when the currently resolved Assignment ID, Version ID, and configuration hash remain unchanged. Recheck the signed timeline evidence under the Branch workflow lock. A Company default change alone must not stale a candidate for a Branch that already has a persisted Assignment. Preserve replay of an already-created candidate before stale revalidation, including existing cancelled-key behavior.

## 8.3 Shared Setup publication

Publishing a new shared version must:

- lock the Setup timeline;
- determine materialized affected Branches;
- lock every affected Branch in ascending Branch ID order;
- validate boundary compatibility and existing periods;
- publish atomically;
- write audit evidence containing affected Branch IDs.

The Setup timeline lock remains held while affected Branches are materialized, locked, re-read, validated, and the publication and audit evidence commit.

## 8.4 Branch reassignment

Reassignment must serialize against period creation for that Branch.

It must hold source/destination Setup timeline locks in ascending Setup ID order before the Branch workflow lock, then re-read the Branch assignment timeline and payroll periods before committing.

No transaction may create a period from Setup A while a concurrent transaction has successfully made Setup B authoritative for the same start date.

---

# 9. Migration strategy

Exact migration splitting may be adjusted if repository constraints demand it, but the logical sequence is fixed.

## Migration Group A — `0066+` foundation

Introduce:

- company-owned Payroll Setup identity
- Payroll Setup Version
- Branch assignment
- Company default reference
- required indexes/uniqueness
- assignment overlap protection
- published-version immutability foundation
- version-number uniqueness per Setup
- cross-company integrity

Do **not** drop legacy schedule tables in this migration group.

### Gate A

- fresh install `0001 → new head` works;
- empty `0065 → new head` upgrade works;
- schema constraints are tested;
- old runtime remains operational until code cutover begins.

## Migration Group B — Payroll Period provenance

Introduce/adapt:

- Period → Assignment FK
- Period → new Setup Version FK
- frozen schedule provenance fields
- mandatory PeriodDay authority/FK transition so every new PeriodDay is bound consistently to the Period's exact PayrollSetupVersion
- integrity constraints ensuring Period/Branch/Company/Assignment/Version agreement

The transitional schema must preserve existing legacy Period/PeriodDay references while allowing new Periods and PeriodDays to bind to the new Assignment and Version. Adapt the old PeriodDay `ScheduleVersionID NOT NULL` constraint/FK for the new creation path without weakening historical references. Do not impose unconditional new-provenance `NOT NULL` constraints on pre-cutover historical rows. New rows must not receive a synthetic legacy version to satisfy old constraints.

Do not weaken existing immutable period/history constraints.

### Gate B

- period persistence can represent the new authority fully;
- new Period and PeriodDay provenance agrees on Company, Branch, Period, Assignment, and Version;
- old runtime remains operational during coexistence, while the new schema path does not use a branch-owned version as authority;
- transitional legacy FKs remain valid for existing historical rows until retirement;
- existing historical/finalization schema remains intact.

## Migration Group C — Permissions and policy audit support

Introduce/seed:

- `payroll_setup.view`
- `payroll_setup.manage`
- `payroll_setup.publish`
- `payroll_setup.assign`

Add dedicated append-only policy-audit persistence with the integrity constraints required by the final implementation.

Do not reuse `setup.manage` as automatic authority for the new shared policy writes.

This group is part of the Phase 1 foundation and must be applied before Phase 2 policy-writing services become reachable. The append-only policy audit must establish actor, event, Setup/Version or Assignment authority, effective date, affected Branch IDs, old/new authority, and correlation. Generic `audit.AuditLog` may supplement this evidence but must not substitute for it. Phase 2 writes policy audit atomically with policy mutations; Phase 3 records PeriodCreated provenance atomically with period creation. Phase 4 exposes the already-authorized and already-audited services.

## Migration Group D — Legacy retirement

Only after runtime cutover and full regression.

Retire:

- `BranchPayrollSettings` authority
- branch-owned `PayrollScheduleVersions`
- `CurrentScheduleVersionID`
- obsolete FKs/columns/routes directly tied to old authority

The retirement migration must:

1. inspect all legacy settings/version rows;
2. inspect Period and PeriodDay references;
3. abort before destructive DDL if unexpected legacy business data/references exist;
4. never truncate or cascade-delete unexpected business history;
5. provide a clear failure message requiring a separately reviewed mapping/reconciliation migration.

### Gate D

- search proves no runtime read/write of old authority;
- seeded unexpected legacy-data test proves retirement aborts safely;
- fresh migration chain passes;
- empty-0065 upgrade path passes.

The retirement guard must inspect every legacy business row and discovered dependent reference before destructive DDL and abort without changing business data if any unexpected row/reference exists. It must not use `DELETE`, `TRUNCATE`, `CASCADE` deletion, or an assumption that the database is empty. Gate D additionally requires a clean-path retirement test, a seeded-data rollback test proving rows remain unchanged, a repository-wide classified reference inventory with no runtime authority or historical reader depending on dropped structures, and a complete backend regression run after retirement. Historical migration definitions remain in place.

---

# 10. Implementation phases

Each phase must finish with code review, focused tests, and an explicit PASS before the next phase starts.

---

## Phase 0 — Baseline lock and implementation map

### Goal

Turn the closed architecture contract into an exact code-impact map immediately before mutation.

### Required work

Read-only verification of:

- implementation-start branch, SHA, worktree status, latest available complete backend-suite result at that SHA, and Alembic chain/head;
- read-only generated database schema inventory for relevant constraints/FKs, without applying migrations or changing data;
- repository-wide references to legacy tables, columns, helpers, permissions, and routes, classified as runtime authority, compatibility, historical reader, migration history, test-only, or dead;
- current migrations `0050`–`0065` relevant to schedule/period snapshots;
- migration origins and FK evolution for Companies, Branches, BranchPayrollSettings, PayrollScheduleVersions, PayrollPeriods, and PayrollPeriodDays, including `0001`, `0025`, `0026`, `0051`, and `0052`;
- `backend/app/settings/service.py` setup write/read paths;
- `backend/app/settings/router.py` branch setup routes;
- `backend/app/settings/schemas.py`;
- `backend/app/payroll/period_creation.py`;
- Current Payroll readiness/hub code;
- off-drivers calendar/version fallback;
- Company/Branch creation path;
- authenticated Company/owner bootstrap dependency, distinguished from Payroll Setup onboarding;
- permission catalogue, role dependency/implication logic, endpoint scope checks, and frontend permission map;
- audit writers, immutable P6D evidence, and transaction boundaries;
- all candidate, legacy next-date, and direct period-creation routes and consumers;
- Current Payroll, dashboard, readiness, off-drivers, reports, and other scheduled/read-side consumers;
- Payroll Setup frontend page;
- Company/Branches frontend page;
- Create Period modal;
- tests/fixtures encoding branch-owned setup assumptions.

Record the latest available complete backend-suite result with its command, environment, and exact totals. If no result exists at the implementation-start SHA, record the baseline as unverified and require a complete run against an isolated disposable test database before Phase 1 implementation; targeted tests are not a substitute. Phase 0 itself remains read-only: no migration application, test-data writes, or generated repository files.

### Output

A short implementation map listing exact files/functions/tests per later phase.

### Exit gate

`PHASE_0_PASS`

No code changes yet.

---

## Phase 1 — New schema and database invariants

### Goal

Create the new authority model without yet making it runtime authority.

### Main work

- Add `PayrollSetup`.
- Add `PayrollSetupVersion`.
- Add `BranchPayrollSetupAssignment`.
- Add Company default reference.
- Add cross-company FKs/checks.
- Add assignment non-overlap enforcement.
- Add version-number uniqueness.
- Add published immutability guards.
- Add same-date replacement-chain integrity.
- Add required indexes.
- Add/seed dedicated `payroll_setup.*` permissions without implying them from operational payroll permissions.
- Add append-only policy-audit persistence and its tenant/authority integrity constraints.
- Prove the transitional Period/PeriodDay FK design needed for coexistence with the old runtime and the new authority path.

### Tests

Mandatory DB-focused cases:

- one Company owns multiple Setups;
- Setup cannot reference another Company;
- two Branches may share one Setup;
- assignment overlap blocked;
- cross-company assignment blocked;
- version number unique per Setup;
- published update/delete blocked;
- replacement chain cannot fork;
- default Setup must belong to Company;
- archived Setup cannot become default;
- policy permission seeds do not inherit from operational payroll permissions;
- append-only policy audit cannot be altered/deleted by ordinary policy operations;
- old and new period provenance coexist without synthetic legacy rows.

### Exit gate

`PHASE_1_PASS`

Requirements:

- migrations green;
- focused tests green;
- existing payroll runtime still works on old authority;
- no destructive legacy drop.

### Must not start yet

- period cutover;
- frontend redesign;
- legacy retirement.

---

## Phase 2 — Domain services, publishing, assignments, canonical resolver

### Goal

Implement the new policy domain and make resolution behavior correct before plugging it into period creation.

### Main work

Create/refactor service boundaries for:

- Setup CRUD metadata
- Draft Version lifecycle
- Publish Version
- same-date future correction/replacement
- Company default change
- Branch assignment/reassignment/withdrawal
- impact preview
- canonical date resolver

Every policy-writing service must enforce the dedicated company-wide permission and commit immutable policy audit evidence in the same transaction as the policy change. Routes remain unreachable until these checks are in place. Apply the lock order in §8 to every affected operation.

### Required business rules

- no runtime default fallback;
- branch reassignment uses payroll boundaries, not wall-clock today;
- non-cancelled periods block conflicting authority changes;
- period crossing a new boundary is rejected;
- future publications validate all affected Branches;
- multiple future published versions supported;
- default change never rewrites existing assignments.

### Tests

- Draft editable/discardable;
- Published immutable;
- multiple scheduled future versions;
- same-date replacement resolves terminal version;
- Branch A→B reassignment;
- no assignment overlap;
- no readiness gap where payroll-ready coverage is required;
- cancelled period does not reserve future authority timeline;
- non-cancelled period blocks conflicting reassignment/publication;
- boundary-crossing publication rejected;
- changing default does not alter old assignment;
- archiving is blocked while a non-withdrawn assignment could still govern new period creation;
- resolver selects exact Setup Version by StartDate;
- predecessor/successor boundary validation against each affected Branch's full non-cancelled period and future authority timeline;
- concurrent publish/replacement versus assignment, Branch creation/default assignment, and period creation, including deadlock checks;
- policy mutation authorization and immutable audit atomicity.

### Exit gate

`PHASE_2_PASS`

The resolver is independently trustworthy; policy writes are authorized, audited, serialized, and covered for boundary correctness before any period-creation cutover.

### Must not start yet

- candidate cutover until concurrency tests for resolver mutations pass.

---

## Phase 3 — Payroll Period authority cutover

### Goal

Make the new resolver the only authority for candidate and period creation.

### Main work

Adapt:

- candidate preview
- candidate signing/fingerprint
- confirm/create revalidation
- Period schema bindings
- mandatory PeriodDay version/authority FK transition established in Group B
- frozen schedule provenance
- candidate stale logic
- any legacy next-date path that can still create or influence periods

Cut over or retire candidate preview/confirm and every legacy next-date/direct-create path in the same phase. Disable the old branch-owned Payroll Setup write route at cutover; any temporarily retained setup read route must derive its response from the canonical authority with the new Branch read permission, or be disabled. No route may create or influence period dates from legacy BranchPayrollSettings, or keep writing it as schedule authority, after this gate. Do not deploy an externally reachable mixed-state release before the administration API, readiness, and frontend cutovers are complete.

### Preserve

- branch workflow lock
- replay semantics
- one-slot protections
- overlap checks
- cancellation behavior
- PeriodDay snapshots
- PayItem layout snapshots
- driver eligibility snapshots

### Required candidate stale conditions

A candidate must become stale if any of these change after preview:

- Assignment ID
- Setup Version ID
- any Setup or Branch timeline revision/fingerprint change after preview, even if resolved authority IDs and configuration hash are unchanged
- candidate dates
- configuration hash
- slot state

A Company default change alone does not stale an already-assigned Branch candidate.

### Tests

- preview/confirm same authority succeeds;
- version published between preview/confirm → stale;
- reassignment between preview/confirm → stale;
- concurrent create remains serialized;
- period binds exact assignment/version;
- frozen provenance equals resolved configuration;
- crossing boundary rejected;
- replay works;
- cancelled-key behavior preserved;
- PeriodDays remain correct;
- new Period and PeriodDay rows bind the same exact Assignment and Version;
- old historical Period/PeriodDay references remain valid through coexistence;
- all legacy period-creation/next-date routes are retired or delegate to the canonical resolver;
- selected broad payroll regression covers submitted snapshots, approved authority, finalization, reports, and P6D evidence;
- historical period does not re-resolve current Setup.

### Exit gate

`PHASE_3_PASS`

At this point, no period-creation or next-date path may depend on `BranchPayrollSettings` or branch-owned `PayrollScheduleVersions`. Historical read dependencies and legacy physical structures may remain only as explicitly inventoried transitional dependencies until Phase 8.

### Critical rule

Do not leave a second writable creation path using legacy authority.

---

## Phase 4 — Administration API, permissions, and policy audit

### Goal

Expose the new authority safely to company configuration users.

This phase exposes the permission-enforced and audited services built and tested in Phases 1–3. It does not defer first-time authorization, audit persistence, or PeriodCreated provenance until this phase.

### API resource model

Conceptually expose:

- Company Payroll Setups
- Setup metadata
- Draft Versions
- Published/future Version timeline
- publication impact preview
- Branch assignment history/scheduled changes
- Company default Setup
- Branch read-only effective Setup/history

### Permissions

Exact contract:

- branch read-only effective Setup/history: Branch access + existing `payroll.view`
- company list/view: company-wide + `payroll_setup.view`
- Setup/Draft management: company-wide + `payroll_setup.manage`
- publish/version correction: company-wide + `payroll_setup.publish`
- assign/reassign/default: company-wide + `payroll_setup.assign`

Do not let branch operational permissions mutate shared Setup policy.

### Audit requirements

Record at minimum:

- SetupCreated
- SetupMetadataChanged
- DraftCreated
- DraftChanged
- DraftDiscarded
- VersionPublished
- FutureVersionScheduled
- VersionReplaced
- BranchAssigned
- BranchReassigned
- AssignmentWithdrawn
- DefaultChanged
- SetupArchived
- PeriodCreated with assignment/version/hash provenance

Shared publication audit must materialize affected Branch IDs.

### Exit gate

`PHASE_4_PASS`

Every policy write is authorized, transactional, and audited.

---

## Phase 5 — Company/Branch onboarding and readiness

### Goal

Make payroll configuration operable from a newly bootstrapped Company without legacy setup data.

Separate authenticated Company/owner bootstrap from Payroll Setup onboarding. The architecture track depends on an authenticated Company and authorized owner already existing; it does not redesign registration or developer authentication bootstrap. Document the supported dependency and ensure Payroll Setup onboarding itself requires no `ensure_dev_admin.py`, fixture SQL, demo rows, or legacy BranchPayrollSettings.

### Company onboarding flow

```text
Authenticated Company and owner (separate bootstrap prerequisite)
  → create Main/Default Branch identity
  → configure first Payroll Setup
  → publish first Version
  → designate Company default
  → provide explicit first payroll period start
  → persist Main Branch assignment
  → branch becomes schedule-ready
```

A Company/Branch may exist before schedule onboarding is complete, but payroll readiness must report the missing prerequisite explicitly.

### New Branch flow

If Company has active default Setup and an explicit first payroll period start:

This combined operation requires both the existing Branch-creation authority and company-wide `payroll_setup.assign`; `branches.create` alone must not grant shared-policy assignment authority.

```text
Create Branch
  → validate requested first period start against default Setup
  → persist Branch and assignment atomically while serializing the Company default and Setup timeline
  → expose readiness
```

If no default or no first payroll start:

```text
Create Branch
  → no implicit fallback
  → not payroll-ready
  → clear setup-required reason
```

If the caller lacks `payroll_setup.assign`, Branch creation must not silently persist a default-derived Assignment; it may create a clearly not-ready Branch only through the separately authorized Branch-create path.

### Readiness definition

Schedule-ready requires:

- exactly one effective persisted assignment for the intended payroll start;
- Active Setup;
- resolvable Published Version;
- valid boundary;
- no assignment conflict.

If Branch creation or Assignment creation fails, neither half may persist. Default changes do not rewrite existing Branch assignments.

Do not conflate this with unrelated readiness such as Drivers, Rates, Pay Items, etc.

### Exit gate

`PHASE_5_PASS`

Given the separately documented authenticated Company/owner bootstrap prerequisite, a new Company and Branch can complete payroll configuration and Assignment onboarding without fixture-only SQL or legacy `BranchPayrollSettings`.

---

## Phase 6 — Frontend product cutover

### Goal

Make the UI explain the new ownership model correctly.

### Settings → Payroll Setups

Must support conceptually:

- list Company Setups
- mark default
- Setup details
- Draft Version editing
- Published/future timeline
- publish confirmation
- affected Branches preview
- assigned Branches
- archive eligibility

### Branch view

Must show:

- effective Setup
- effective Version
- assignment start/history
- scheduled reassignment if any
- exact readiness reason

The read-only Branch view is gated by Branch access plus `payroll.view`; company Setup administration remains separately gated by company-wide `payroll_setup.*` permissions. Driver/OwnDriverDataOnly roles do not gain schedule/history visibility through this view.

### Branch user view

Branch-scoped operational users with `payroll.view` can see their Branch’s schedule/history read-only.

They must not see or mutate the complete company-wide assignment set unless authorized for company setup view.

### Create Period modal

Keep the existing backend-driven candidate experience where possible.

The modal should not duplicate version-resolution logic.

### Exit gate

`PHASE_6_PASS`

The frontend has no active legacy read, write, readiness, or navigation path that treats branch-owned mutable Payroll Setup as authority. The Branch read-only history path and company-wide permission gates are verified, and Create Period continues to use the backend candidate authority.

---

## Phase 7 — Test modernization and full regression

### Goal

Convert the suite from the old ownership contract to the new one without weakening real payroll invariants.

Phase 7 is the comprehensive suite/fixture modernization phase, not the first serious validation of Phases 1–6. Each phase must add and pass its own contract tests before its exit gate; Phase 3 also runs selected broad payroll regression, and Phases 5–6 validate onboarding/readiness and frontend permissions before proceeding.

### Preserve tests for

- candidate replay/tampering
- period overlap
- concurrency
- security and branch/company isolation
- PeriodDays
- PayItem snapshots
- eligibility snapshots
- lifecycle
- submitted/approved calculation snapshots
- finalization
- FinalLines
- frozen reporting
- P6D audit evidence

### Adapt fixtures

Replace helpers that manufacture `BranchPayrollSettings` with helpers that create:

- Company Setup
- Published Version
- Branch Assignment

### Rewrite old-contract tests

Rewrite tests asserting:

- one mutable Setup row per Branch;
- branch-owned version numbering;
- `CurrentScheduleVersionID` behavior;
- direct branch Setup upsert as authority;
- readiness based on existence of `BranchPayrollSettings`.

### New mandatory acceptance matrix

- Company with one Setup
- Company with multiple Setups
- multiple Branches sharing Setup A
- separate Branch on Setup B
- default assignment behavior
- changing default leaves existing assignments unchanged
- no-default onboarding
- future Version activation
- multiple future Versions
- same-date replacement
- Published immutability
- A→B Branch reassignment
- old periods remain bound to old authority
- assignment overlap blocked
- coverage gap behavior
- cancelled vs non-cancelled future periods
- boundary-crossing period rejected
- cross-company assignment rejected
- branch user cannot mutate shared Setup
- publication vs period-creation race
- reassignment vs period-creation race
- stale candidate after authority change
- stale candidate when a timeline changes but resolved Assignment/Version IDs remain unchanged
- Company default change does not stale an already-assigned Branch candidate
- archive/delete guards
- complete audit evidence
- finalized payroll remains unchanged
- fresh migration install
- empty `0065` upgrade

### Full regression gate

Run the complete backend suite and required frontend validation after fixture modernization. Record exact commands, environment, totals, skips, failures, and errors. Resolve or explicitly classify every failure; do not delete or weaken coverage simply to obtain a green suite. Run the complete suite again after Phase 8 retirement.

Target is no unexplained failures/errors and no weakening of supported behavior merely to make tests green.

### Exit gate

`PHASE_7_PASS`

---

## Phase 8 — Legacy authority retirement

### Goal

Remove the obsolete schedule model only after proof that nothing uses it.

### Pre-retirement evidence

Repository searches must show no runtime authority reads/writes through:

- `BranchPayrollSettings`
- branch-owned `PayrollScheduleVersions`
- `CurrentScheduleVersionID`
- old branch-specific full-replacement Setup write API
- old candidate/next-date direct lookups against legacy settings
- historical/read-side consumers, including off-drivers calendar fallback, dashboards, readiness, reports, and any scheduled behavior that joins legacy versions
- all frontend routes, API clients, navigation, and readiness consumers
- all test/fixture factories that create legacy authority
- schema FKs and other live references to legacy tables/columns

Historical migration references are allowed.

### Retirement migration behavior

Before destructive DDL, validate:

- zero unexpected `BranchPayrollSettings` business rows;
- zero unexpected legacy schedule-version rows;
- zero Period references to legacy versions;
- zero PeriodDay references to legacy versions;
- zero other live references discovered during implementation.

Run the complete inventory before any destructive statement. If any unexpected legacy business row or reference exists, the migration must abort before destructive DDL and the transaction must leave all business data unchanged. The check may not clear rows or references to satisfy itself.

If any exist:

```text
ABORT MIGRATION
ROLL BACK
REPORT EXACT RECONCILIATION REQUIREMENT
```

Never silently delete or cascade unexpected payroll data.

### Remove/retire

- old authority tables/columns/FKs as appropriate
- `CurrentScheduleVersionID`
- old write/read routes that contradict the new resource model
- repair helpers that exist only for the branch-owned pointer
- obsolete tests/fixtures after equivalent new coverage exists

### Exit gate

`PHASE_8_PASS`

One runtime schedule authority remains; clean-path retirement and seeded unexpected-data rollback are proven, all dropped-structure consumers are gone, and the complete backend suite passes after retirement with every residual failure explicitly classified.

---

# 11. Areas that should remain untouched unless evidence requires adaptation

Treat changes here as suspicious and require explicit justification:

- core payroll financial calculation formulas
- Bonus event domain
- Driver Rate resolution
- Driver Pay Rules
- calculation snapshot normalization
- Submit/Resubmit immutable capture
- Review/Approval financial authority
- Finalization preview from approved snapshot
- FinalLines projection
- finalized reporting authority
- finalized-library read model
- P6D immutable audit-evidence behavior

A Setup architecture change is not permission to clean up or redesign these areas opportunistically.

---

# 12. Known high-risk points

## 12.1 Shared publication blast radius

A single Setup Version may affect many Branches.

Mitigation:

- materialized affected-branch preview;
- boundary validation for every affected Branch;
- deterministic locking;
- immutable audit evidence;
- one global lock order from §8, with re-read of the affected Branch set and period history before commit.

## 12.2 Period/version boundary mismatch

A Version effective date that lands inside a theoretical payroll period is invalid for that affected Branch.

Mitigation:

- resolver computes next authority boundary;
- publication/reassignment validates period boundaries before commit;
- period creation rejects boundary crossing.

## 12.3 Candidate race

Preview can become stale after version publication or reassignment.

Mitigation:

- assignment/version identity in candidate;
- signed Setup and Branch timeline revision/fingerprint;
- re-resolve under workflow lock;
- reject stale candidate.

## 12.4 Hidden legacy authority

An old route/helper may continue reading `BranchPayrollSettings` after main cutover.

Mitigation:

- repository-wide authority search before retirement;
- tests against all creation/readiness paths;
- no legacy writable fallback.

## 12.5 Historical authority weakening

Changing schedule FKs could accidentally weaken immutable history.

Mitigation:

- period binds exact new Version;
- frozen provenance;
- preserve PeriodDays and existing immutable financial evidence;
- full regression before retirement.

---

# 13. Phase review template

Every implementation phase must end with a report using this structure:

```text
PHASE: <number/name>
VERDICT: PASS | FAIL_P0_P1 | ITERATE

BASELINE
- branch:
- starting SHA:
- ending SHA:
- Alembic before/after:
- worktree status:

IMPLEMENTED
- ...

CONTRACT INVARIANTS VERIFIED
- ...

TESTS
- focused:
- full suite if required:
- failures/errors/skips:

MIGRATION SAFETY
- fresh install status:
- 0065 upgrade status:
- rollback/fail-safe checks:

REGRESSIONS / OPEN RISKS
- ...

FILES / DOMAINS TOUCHED
- ...

NEXT PHASE ALLOWED: YES | NO
```

No phase advances on an unresolved P0/P1 affecting payroll correctness, tenant/branch isolation, immutable history, security, or migration safety.

---

# 14. Git and change-management discipline

For implementation work:

- Start from current `main` after verifying clean worktree.
- Use a dedicated feature branch for the Payroll Setup architecture track.
- Do not rewrite shared history.
- Keep migrations and runtime changes reviewable.
- Do not stage unrelated local files.
- Do not remove tests before replacement coverage exists.
- Do not merge a phase merely because focused tests pass if its exit gate requires broader regression.
- Final merge occurs only after the complete architecture track reaches all mandatory gates.

Recommended commit boundaries should follow architectural responsibility rather than arbitrary file count, for example:

```text
schema, permission seeds, and immutable policy-audit foundation
resolver/domain services with authorized and audited policy writes
period authority cutover with PeriodCreated provenance
administration API
onboarding/frontend
regression modernization
legacy retirement
```

Exact commit count is not a contract requirement.

---

# 15. Definition of Done

This plan is `CLOSED` only when all of the following are true:

- [ ] Company-owned Payroll Setups exist.
- [ ] Published effective-dated immutable Versions exist.
- [ ] Branches use persisted effective-dated assignments.
- [ ] Company default is onboarding-only and never runtime fallback.
- [ ] New Branch onboarding can create a valid assignment without legacy Setup rows.
- [ ] `PayrollPeriod.StartDate` drives one canonical resolver.
- [ ] Periods cannot cross authority boundaries.
- [ ] Candidate preview/confirm revalidate exact assignment/version authority.
- [ ] Every new Period freezes exact Assignment + Version + schedule provenance.
- [ ] Historical payroll never resolves mutable current schedule state.
- [ ] Branch read-only access uses branch access + `payroll.view`.
- [ ] Company shared-policy actions use dedicated `payroll_setup.*` permissions.
- [ ] Shared policy changes leave immutable, branch-attributable audit evidence.
- [ ] Existing immutable calculation/finalization/reporting authority remains correct.
- [ ] Frontend no longer treats Payroll Setup as a mutable Branch-owned object.
- [ ] Full backend regression is green at the agreed baseline.
- [ ] Fresh `0001 → head` migration works.
- [ ] Empty `0065 → head` migration works.
- [ ] Legacy retirement aborts safely if unexpected old data is seeded.
- [ ] No runtime reads/writes remain against legacy schedule authority.
- [ ] `BranchPayrollSettings`, branch-owned schedule versions, and `CurrentScheduleVersionID` are retired as authority.
- [ ] One schedule source of truth remains.

Final state:

```text
PAYROLL_SETUP_ARCHITECTURE_IMPLEMENTATION = CLOSED
C2_IMPORT = UNBLOCKED
```

---

# 16. First action from this plan

Begin with **Phase 0 — Baseline lock and implementation map**.

Do not start coding from this document blindly. The Phase 0 owner must confirm the exact current file/function/test blast radius against the repository at the implementation-start SHA, then return a report only.

Once Phase 0 passes, proceed to Phase 1 schema work.
