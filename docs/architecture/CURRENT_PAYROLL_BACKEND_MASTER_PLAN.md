# Current Payroll Backend Master Plan

**Project:** Flussra Payroll App  
**Document role:** Official working plan for the Current Payroll backend rebuild/redesign  
**Plan status:** Active planning baseline  
**Source baseline reviewed:** Git commit `bb491cc600335b4c0a69b63692717b21ae361d62` (`2026-06-18`)  
**Database migration baseline:** Alembic `0047 (head)`  
**Last source revalidation:** 2026-06-19  
**Implementation status:** No roadmap phase is complete; all phases in this plan are `Pending`.

This document is authoritative for future Current Payroll backend work. Source code, current migrations, the live schema, and executable tests remain authoritative for statements about what exists today. Older planning/status markdown files are historical unless a statement is revalidated here.

---

## 1. Product Goal

Build a final-grade backend for Current Payroll in a multi-company, multi-branch driver payroll system. The backend must be the single source of truth for:

- company and branch scope;
- payroll permissions and action capabilities;
- payroll period creation and lifecycle;
- prepared, open, review, returned, approved, locked, and archived behavior;
- driver-date eligibility;
- period day/calendar behavior;
- daily operational source entries;
- system-owned statuses and future status pay effects;
- approved effective-dated rate and rule resolution;
- expected income and all payroll calculation reports;
- bonus events and aggregates;
- submitted calculation snapshots;
- review freezing and approval identity;
- finalization and immutable ledger snapshots;
- financial audit and security detail.

The frontend may render, filter already-authoritative rows, and change visual layout. It must not calculate financial totals, reconstruct workflow permissions, join fragile endpoint combinations to invent a hub summary, or infer whether data is editable.

The target is not a patched period list. It is a coherent payroll workflow and calculation domain that can support Current Payroll, review, finalized payroll, exports, and future correction workflows from the same backend truth.

---

## 2. Validation Baseline

### 2.1 Checks performed for this plan

- `git status --short` inspected before writing this file.
- Alembic current revision inspected: `0047 (head)`.
- Development schema guard executed successfully: `schema_guard=PASS`.
- Live PostgreSQL constraints inspected.
- Payroll, review, setup, calculation, bonus, grid, ledger, permission, and schema-guard source paths re-read.
- Targeted backend tests executed from `backend/`:

```text
331 passed, 1 warning in 60.88s
```

Test files included:

- `test_payroll_setup_safety.py`
- `test_cp2_workflow.py`
- `test_m16.py`
- `test_finalization_preview.py`
- `test_m15.py`
- `test_day_grid.py`
- `test_cp25_drivers_off.py`
- `test_ledger.py`
- `test_security_matrix.py`
- `test_schema_guard_payroll_trust.py`

### 2.2 Validation warnings

- Pytest could not update `backend/.pytest_cache` because of a permission error. Test execution still completed successfully.
- After the successful test run, `testing.common.database` reported that its temporary PostgreSQL process did not shut down automatically. This is an environment/test-harness cleanup issue and must not be hidden.
- The live database has 14 `NOT VALID` constraints. They protect new writes but existing rows have not been validated. Relevant payroll constraints include period/company/branch and draft/final line company/branch/driver relationships.
- The worktree already contained frontend changes before this document was created. This plan does not modify or validate those frontend changes.

### 2.3 Evidence anchors

| Area | Current source of truth |
| --- | --- |
| Period statuses/transitions | `backend/app/payroll/schemas.py:79` |
| Entry-allowed statuses | `backend/app/payroll/schemas.py:168` |
| Generic period creation | `backend/app/payroll/service.py:382` |
| Suggested next-period dates | `backend/app/payroll/service.py:598` |
| Submission/status transition | `backend/app/payroll/service.py:738` |
| Finalization | `backend/app/payroll/service.py:2641` |
| Approved finalization preview | `backend/app/payroll/service.py:3355` |
| Bonus/period-pay CRUD | `backend/app/payroll/service.py:6022` |
| Bonus eligible-driver query | `backend/app/payroll/service.py:6310` |
| Period off-record query | `backend/app/payroll/service.py:8457` |
| Day-grid read/write | `backend/app/payroll/service.py:8540`, `:8805` |
| Review decision/writeback | `backend/app/review/service.py:484`, `:634` |
| Branch payroll setup | `backend/app/settings/service.py:955` |
| Initial payroll schema | `migrations/sql/0001_initial_schema.sql` |
| Locked-ledger database guards | `migrations/versions/0035_locked_ledger_immutability.py` |

Passing tests validate current behavior. They do not prove compliance with requirements that current tests do not assert, including immutable submitted revisions, one InReview slot, bonus exclusion from min/max, or finalization from an approved calculation snapshot.

---

## 3. Current Backend Reality

### 3.1 Architecture that exists

- FastAPI endpoints use raw parameterized SQL through SQLAlchemy async connections.
- Each request runs inside `engine.begin()`, so application writes and audit writes can roll back together.
- JWT claims provide user and company identity.
- Branch scope is resolved through `app.vw_UserBranchAccess`.
- Permission decisions use `sec.fn_UserHasPermission`.
- Operational payroll paths hard-block driver/OwnDriverDataOnly users.
- Payroll uses `PayrollPeriods`, `PayrollDraftLines`, and `PayrollFinalLines` as the main workflow and ledger structures.
- Review uses generic `ManagerReviewItems` and `ManagerReviewDecisions` with payroll-specific writeback.
- Rates, tiers, branch pay-item activation, and min/max rules are effective-dated.
- Final lines carry source snapshots and are protected by database triggers once the period is Locked/Archived.

### 3.2 Current period state machine

Current database statuses are:

- `Draft`
- `Open`
- `InReview`
- `Approved`
- `Locked`
- `Cancelled`
- `Archived`

Current transitions are:

```text
Draft -> Open | Cancelled
Open -> InReview | Cancelled
InReview -> Open | Cancelled
Approved -> InReview | Cancelled
Locked -> Archived
Cancelled -> terminal
Archived -> terminal
```

Approval is written by the review service. Locked is reached only through finalization.

### 3.3 Current slot enforcement

The database currently enforces:

- at most one Draft per company/branch;
- at most one Open per company/branch.

It does not enforce one InReview period per branch.

### 3.4 Draft today

Draft is created by the generic create endpoint and is write-blocked by payroll entry services. Draft cannot submit directly, approve, finalize, or use the Approved finalization preview.

No separate operational meaning for Draft was found. Draft is therefore the correct internal status for product “Prepared,” but current Draft cannot yet satisfy the new pre-entry requirement.

### 3.5 Period creation today

The backend can suggest next dates from active `BranchPayrollSettings`, but the normal create endpoint accepts client-supplied period type, start date, end date, pay date, and name. It validates basic dates and overlap; it does not require those values to match Payroll Setup.

Current Payroll Setup supports:

- Week;
- Biweek;
- Month;
- Custom fixed interval;
- anchor start date;
- normal days-off bitmask;
- pay day of week;
- first pay date;
- include-pay-day-as-work-day;
- custom interval days.

Gaps:

- no SemiMonthly cadence;
- pay-date offset is forcibly stored as zero;
- first-pay-date/pay-day fields do not drive period creation;
- one mutable settings row replaces history;
- no schedule version is attached to a period;
- no period-day snapshot exists;
- no backend Add Day workflow exists.

### 3.6 Daily grid today

The grid:

- accepts a date only inside the period bounds;
- dynamically loads active Daily Pay Items for that date;
- orders columns by current `PayItems.SortOrder`;
- shows drivers that pass current date eligibility checks;
- writes quantities, DailyStatus, and DailyNote into `PayrollDraftLines`;
- stores the selected status code as mutable text in `PayrollDraftLines.Notes`;
- exposes calculated amounts and a day gross summary;
- currently permits Open and InReview writes.

It does not have a snapshotted period calendar, stable pay-item layout, or historical status label snapshot. It also filters on current `Employees.EmploymentStatus='Active'`, which can hide historically eligible terminated/inactive employees.

### 3.7 Status system today

- Status Keys are branch-scoped and have code, display name, off-reason flag, allowance fields, usage limits, and activity state.
- A selected status is not stored by StatusKeyID; only code text is stored.
- Historical reads join back to an active Status Key. Deactivating or renaming a key can hide/change historical meaning.
- `DailyStatus` and `DailyNote` are informational pseudo-line types in DraftLines.
- A system Pay Item named `PTO_STATUS` also exists, conflicting with the product rule that Status is not a Pay Item.
- There is no effective-dated `StatusKeyPayRule` domain.

### 3.8 Off-driver behavior today

`GET /payroll/periods/{id}/drivers-off` returns every matching off DailyStatus row across the period. Its `total_count` is a driver-day row count, not a count of drivers off for the whole period. It cannot be used for the Hub KPI.

The day grid itself contains per-day `is_off` fields, but there is no dedicated selected-day off-driver contract containing stable status snapshots and note metadata.

### 3.9 Calculation and expected income today

- Draft lines can store `RateAmount` and `CalculatedAmount`.
- Submission refreshes rate-dependent calculations.
- Approved finalization preview virtually resolves current rates and mirrors finalization.
- Finalization again refreshes/resolves current rates and writes final lines.
- There is no Open-period expected-income endpoint.
- There is no submitted calculation snapshot.
- Finalization can use rates approved after review, so the finalized amount can differ from what the reviewer saw.
- Current min/max calculations sum all non-system final lines, including BONUS.

The last point directly conflicts with the required calculation order. Current behavior is effectively:

```text
normal pay + bonus -> apply minimum/maximum
```

Required behavior is:

```text
normal pay -> apply minimum/maximum -> add bonus
```

### 3.10 Bonus today

- Bonus is generic Period Pay stored as a `PayrollDraftLines` row with `LineScope='Period'` and `LineType='BONUS'`.
- Multiple active BONUS rows for one driver/period are intentionally permitted by current tests.
- Added-by and added-at fields exist through DraftLines; update/void metadata is incomplete.
- Eligible drivers and bonus rows are separate endpoint calls.
- There is no backend all-driver zero-inclusive aggregate, batch contract, revision control, or idempotency.
- The initial schema contains an unused `PayrollRunBonuses` table with richer bonus-specific fields.

The two storage models must never be active simultaneously.

### 3.11 Review today

- Open→InReview refreshes calculations, validates blockers, creates a Pending PeriodApproval review item, and updates the period in one request transaction.
- InReview source data remains editable.
- `SubmittedAtUtc` exists but is not populated.
- No submitted data revision or calculation snapshot is captured.
- Review approval only checks current status and some blockers; it does not approve a snapshot identity.
- Rejected/EditRequested writes the period back to Open.
- Manual InReview→Open/Cancelled can leave the Pending review item unresolved.
- Approved→InReview creates no new review item.

### 3.12 Transition concurrency today

Only selected operations use expected-status predicates. Most generic status changes update by period ID without including the previously-read status. The source comment claiming `get_period_by_id` holds `FOR UPDATE` is incorrect. A stale concurrent transition can therefore bypass the application transition graph.

Draft→Cancelled also lacks a transition-specific permission mapping, allowing a payroll-view-capable caller to reach cancellation without a dedicated action permission.

### 3.13 Finalization and ledger today

Strong existing foundation:

- Approved-only finalization;
- transaction-level branch advisory lock;
- rate refresh and structural blocker validation;
- atomic Approved→Locked claim;
- immutable final lines;
- immutable Locked/Archived status rules;
- source-rate/rule snapshot data;
- finalization audit event;
- ledger totals from final lines.

Missing for the required finalized information library:

- submitted calculation snapshot identity;
- stable period calendar and pay-item layout;
- immutable status labels/off-driver details;
- complete bonus-event metadata;
- final report endpoints;
- actor/role responsibility snapshot;
- full old/new audit history for source changes;
- rate-change actor/timestamp details in final report contracts.

---

## 4. Final Architecture Decisions

These are the recommended working decisions unless explicitly changed through a product decision recorded in this document.

### AD-1 — Draft is Prepared

Keep database status `Draft`; display it as “Prepared.” Do not add a separate Prepared status.

Prepared may contain provisional operational source entries, but:

- it has no expected-income response;
- financial totals are omitted/null;
- bonus entry is prohibited;
- it cannot submit, approve, or finalize;
- its source rows become official Open source rows only through atomic promotion;
- promotion does not copy data to another table.

Using the same non-final source tables is safe only after contracts distinguish operational source from derived financial output. Merely adding Draft to the existing editable-status set is unsafe because current responses expose calculated amounts and current calculation fields are treated as source data.

### AD-2 — Add a genuine Returned state

Add a period state named `Returned` (UI may say “Returned Previous Period” or “Needs Correction”). This is a real domain need, unlike Prepared.

Reason: after an Open period is submitted, a Prepared period should be promoted so operations can continue. If the older InReview period is then returned, it cannot become Open without violating the one-Open rule. It also cannot remain InReview because InReview is read-only and still awaiting a decision.

Returned rules:

- editable for correction;
- backend expected-income preview allowed;
- can resubmit to InReview;
- review return/rejection reason required and visible;
- appears as a prominent Hub alert;
- does not consume the Open slot;
- an unresolved Returned period blocks submission of a newer Open period and creation of another Prepared period, but does not block saving current Open operational work.

### AD-3 — Promote Prepared atomically when Open is submitted

Submission is the clean promotion boundary:

1. lock the branch workflow;
2. verify no existing InReview slot;
3. create the submitted calculation snapshot;
4. Open→InReview;
5. if Prepared exists, Draft→Open in the same transaction;
6. create the review item;
7. audit all linked actions.

Waiting until finalization would leave Prepared alone while the previous period is InReview and would prevent operational continuity. A separate routine promotion command creates an avoidable intermediate state and more race conditions.

### AD-4 — Smart creation is server-derived

Normal creation becomes a branch command, not client date entry:

```text
POST /payroll/branches/{branch_id}/periods/next
```

Under a branch-level transaction lock:

- no Open and no Prepared: create Open;
- Open and no Prepared: create Prepared/Draft;
- Open and Prepared: reject with a capability reason;
- InReview and no Open: create Open;
- InReview and Open but no Prepared: create Prepared unless a Returned blocker exists;
- Prepared without Open is an invalid invariant and must be repaired/promoted, not allowed to persist;
- at most one Open, one Draft, and one InReview per branch.

Arbitrary Custom periods require a separate privileged command, explicit reason, and audit event.

### AD-5 — Snapshot period configuration

Each period must retain:

- the payroll schedule version used;
- every calendar date in the period;
- default work-day/off-day classification;
- any Add Day activation;
- active pay-item layout, labels, order, and calculation classification;
- configuration version/hash.

Pay-item reordering affects future periods only. Existing Prepared/Open/InReview/final periods keep their snapshot. This is safer than blocking global ordering changes whenever any payroll data exists.

### AD-6 — Status is a separate operational domain

Do not model selected statuses as Pay Items or free-text pseudo-lines. Introduce normalized daily status storage with StatusKeyID plus immutable snapshots. Deprecate `PTO_STATUS` as a grid Pay Item while preserving historical records.

Future `StatusKeyPayRule` records may create derived payroll calculation components. Allowance use and financial behavior remain separate mappings.

### AD-7 — Bonuses are multiple auditable events

Use multiple bonus events per driver/period and aggregate them into `TotalBonus`. This preserves who gave each bonus, when, and why. One total row would lose required event-level detail.

Adopt one canonical bonus-event model. Preferred direction:

- redesign/rename the currently unused `PayrollRunBonuses` structure into the canonical bonus-event domain if a data audit confirms it is unused/clean;
- migrate legacy BONUS DraftLines once;
- stop accepting BONUS through generic period-pay CRUD;
- never dual-write or read from both models after cutover.

Bonus is always excluded from minimum/maximum comparison.

### AD-8 — One calculation core, immutable submitted snapshot

One backend calculation core produces:

- Open/Returned expected-income preview;
- submission snapshot;
- review views;
- approval totals;
- finalization inputs;
- final report/ledger values.

Open/Returned previews are recomputed read-only for current source/config versions. Submit persists an immutable snapshot. InReview and Approved read that exact snapshot. Finalization consumes the approved snapshot and must not re-resolve newer rates.

### AD-9 — Finalized periods are never reopened

Locked/Archived periods remain immutable. Future corrections use a separate correction workflow that references the original period and creates auditable adjustment records; it does not reopen source rows.

---

## 5. Final Desired Workflow

### 5.1 Normal branch sequence

```text
No period
  -> Create Next
Open P1
  -> Create Next
Open P1 + Prepared P2
  -> Submit P1
InReview P1 + Open P2
  -> Create Next (if no Returned blocker)
InReview P1 + Open P2 + Prepared P3
  -> Approve P1
Approved P1 + Open P2 + Prepared P3
  -> Finalize P1
Locked P1 + Open P2 + Prepared P3
```

### 5.2 Return path

```text
InReview P1 + Open P2
  -> Reviewer returns/rejects P1
Returned P1 + Open P2
  -> Hub forces prominent attention
  -> Correct P1
  -> Resubmit P1
InReview P1 + Open P2
```

While Returned P1 is unresolved:

- P2 can continue receiving saved operational data;
- P2 cannot be submitted;
- another Prepared period cannot be created;
- the Hub returns a blocking quick-action reason.

### 5.3 Prepared pre-entry

Allowed:

- daily quantities/source operational fields;
- normalized status selection;
- notes;
- Add Day activation for configured off days;
- audit of actor/time/source.

Not allowed:

- expected-income endpoint;
- financial total fields in grid/hub responses;
- bonus events;
- review submission;
- approval;
- finalization;
- official report/ledger representation.

### 5.4 Save versus Submit

- Save persists operational source data for a date or period.
- Save never changes period lifecycle.
- Submit always means the entire period.
- Submit requires an expected data revision.
- Submit creates the frozen calculation snapshot and review item.
- Backend—not UI—makes InReview read-only.

---

## 6. Backend Gaps

### 6.1 Lifecycle

- No one-InReview unique constraint.
- No Returned state.
- No smart slot-aware create command.
- No atomic submit-and-promote operation.
- Prepared-alone invariant is not enforced.
- Current create always returns Draft, including first payroll.
- Generic transition endpoint exposes unsafe paths.
- Transition writes are not uniformly expected-state safe.

### 6.2 Source data and calendar

- Prepared cannot receive controlled pre-entry.
- Derived calculation values are mixed into operational draft responses.
- No period-day snapshot or Add Day state.
- Off-day mask does not govern grid navigation.
- Pay-item ordering and labels are not snapshotted.
- Historical driver rows can disappear after current status changes.

### 6.3 Status

- Mutable text code storage.
- No StatusKeyID snapshot.
- No historical label/off flag preservation.
- Status modeled partly as pseudo DraftLines and partly as `PTO_STATUS` Pay Item.
- No future pay-rule mapping.
- Status usage limits are read-then-write and are not concurrency-safe.

### 6.4 Calculation

- No Open/Returned expected-income contract.
- No calculation input/version contract.
- No submitted calculation snapshot.
- Review and finalization can observe different rate/config inputs.
- Bonus incorrectly participates in min/max.
- Current preview response cannot support required dynamic report views.
- Current day grid exposes financial calculated amounts directly.

### 6.5 Bonus

- Generic Period Pay contract only.
- Competing unused bonus table.
- No zero-inclusive all-driver summary.
- No batch transaction/idempotency/revision.
- Incomplete actor/update/void metadata.
- No explicit min/max exclusion invariant.

### 6.6 Review and audit

- InReview editable.
- `SubmittedAtUtc` unused.
- No submitted revision/snapshot ID.
- Return can conflict with a newer Open period.
- Manual returns can orphan review items.
- Approval is not tied to snapshot identity.
- Audit updates often omit old values.
- Period creation is not audited.
- Generic audit table is not database append-only.

### 6.7 Hub and reports

- No trusted Current Payroll aggregate.
- Existing period list pagination/filtering is not a hub contract.
- Existing off count is driver-day rows, not fully-off drivers.
- No Drivers Report, Period Work, Period Pay, or Mixed Summary backend contract.
- No finalized information-library contract.

---

## 7. Data Model Direction

Names below are planning names; implementation must use a reviewed migration design.

### 7.1 PayrollPeriods changes

Recommended fields:

- `DataRevision BIGINT NOT NULL DEFAULT 1`
- `SubmittedDataRevision BIGINT NULL`
- `SubmittedAtUtc TIMESTAMPTZ NULL` (existing, begin using)
- `SubmittedByUserID INTEGER NULL`
- `ApprovedCalculationSnapshotID BIGINT NULL`
- `ScheduleVersionID BIGINT NULL`
- `PeriodConfigurationVersion/Hash`
- `UpdatedAtUtc`
- `LastStatusReason`
- optional `ReturnedAtUtc`, `ReturnedByUserID`

Add `Returned` to the period status constraint. Do not add Prepared.

Add partial unique index for one InReview period per company/branch. Existing one-Draft and one-Open indexes remain.

### 7.2 Payroll schedule versioning

Introduce a versioned schedule model such as `PayrollScheduleVersions`:

- company/branch;
- frequency: Weekly, Biweekly, SemiMonthly, Monthly, Custom;
- anchor/first work date;
- semi-monthly boundary definition;
- normal off-day mask;
- pay-date rule;
- effective-from/effective-to;
- version/revision;
- created/approved actor and timestamps.

The current `BranchPayrollSettings` may remain as the current pointer/configuration façade, but historical periods must reference an immutable version.

SemiMonthly requires a product-supported boundary model, not a fixed “15 days” interval. Recommended default is configurable first and second boundaries, supporting 1–15 and 16–end-of-month.

### 7.3 PayrollPeriodDays

One row per date in the period:

- period/date/day-of-week;
- `IsDefaultWorkDay`;
- `IsConfiguredOffDay`;
- `IsAddedWorkDay`;
- added-by/at/reason;
- schedule version ID.

Only configured off days can be activated through Add Day. Dates outside the period can never be added.

### 7.4 PayrollPeriodPayItemSnapshot

Snapshot:

- PayItemID/code/name;
- item scope;
- work/pay data type and unit;
- display order;
- rate behavior;
- financial classification;
- include-in-normal-pay/minmax behavior;
- active date/config source.

This snapshot controls grid/report columns for the period. Reordering settings affects only later periods.

### 7.5 Normalized daily status

Introduce `PayrollDailyStatuses` or equivalent:

- company/branch/period/driver/work date;
- StatusKeyID;
- snapshot code, label, off flag, allowance category metadata;
- active/void state;
- actor/timestamps;
- data revision;
- unique active business key for period/driver/date.

Daily notes should be a typed operational record or typed field, not a fake Pay Item line.

Future `StatusKeyPayRules` should be effective-dated and independently map a Status Key to financial behavior. Allowance-category mapping remains separate.

### 7.6 Bonus events

Canonical `PayrollBonusEvents` direction:

- BonusEventID;
- company/branch/period/driver;
- positive amount;
- optional reason/notes;
- created/updated/voided actors and timestamps;
- state;
- batch correlation ID;
- idempotency key;
- data revision;
- source/import metadata.

Multiple events are allowed. Zero means no event; clearing/voiding is explicit. Bonus is invariantly excluded from min/max.

The existing unused `PayrollRunBonuses` table should be either deliberately migrated into this role or retired. Do not create a third active bonus representation.

### 7.7 Calculation snapshots

Recommended normalized snapshot model:

- `PayrollCalculationSnapshots`: header, period, source revision, calculation version, configuration/rate hash, created reason, actor/time, totals, immutable status.
- `PayrollCalculationSnapshotLines`: driver/date/pay item, work quantity, resolved rate/rule source, normal-pay amount, status-derived amount, snapshots.
- `PayrollCalculationDriverTotals`: normal base, min adjustment, max adjustment, normal after adjustment, bonus total, total pay.
- snapshot bonus/event linkage.

Open/Returned previews need not persist every read. Submission, approval, and finalization must reference a persisted immutable snapshot.

### 7.8 Final information snapshots

Existing FinalLines remain authoritative money lines. Extend/link the final record set to preserve:

- approved CalculationSnapshotID;
- bonus event ID/snapshot;
- status and note snapshots;
- period-day/configuration snapshot IDs;
- participant/responsibility snapshot;
- calculation version;
- submitted/reviewed/approved/finalized actors and timestamps.

### 7.9 Eligibility history

At minimum, eligibility queries must consistently use hire date, termination date, driver effective window, and branch assignment effective window without current EmploymentStatus hiding historical dates.

If inactive/suspended/reinstated intervals are product-relevant, add effective-dated employment/driver assignment history. A single current status column cannot model those intervals safely.

### 7.10 Constraint hardening

- Validate the 14 existing `NOT VALID` constraints after a data audit.
- Add one-InReview partial unique index.
- Add unique active daily-status business key.
- Add appropriate bonus idempotency/business indexes.
- Add period date overlap protection appropriate to the smart-create model.
- Add append-only/immutability guards for submitted calculation snapshots and financial audit records.

---

## 8. Required Backend Contracts

All contracts must return backend-calculated capabilities and reason codes. Names are recommended and may be adjusted during API review.

### 8.1 Current Payroll Hub

```text
GET /payroll/current
GET /payroll/current?branch_id={branch_id}
```

Response responsibilities:

- caller scope and accessible branches;
- Open, Prepared, InReview, and Returned period slots;
- returned-period alerts/actions;
- period dates/name/status/display status;
- total eligible drivers;
- working drivers;
- fully-off driver KPI;
- expected normal pay, bonus, adjustments, and total expected income where allowed;
- top expected-income drivers;
- blockers/warnings;
- action capabilities and read-only reason codes;
- period/source/calculation revision;
- no frontend aggregation across payroll endpoints.

### 8.2 Smart period creation

```text
POST /payroll/branches/{branch_id}/periods/next
POST /payroll/branches/{branch_id}/periods/custom   # privileged exception
```

The backend derives dates, pay date, initial status, schedule snapshot, period days, and pay-item layout.

### 8.3 Lifecycle commands

Replace generic free-form status PATCH as the public workflow with commands:

```text
POST /payroll/periods/{id}/submit
POST /payroll/periods/{id}/cancel
POST /payroll/periods/{id}/archive
POST /payroll/periods/{id}/resubmit-returned
```

Review approval/return remains a review command and carries expected snapshot/revision.

### 8.4 Period calendar and grid

```text
GET  /payroll/periods/{id}/calendar
POST /payroll/periods/{id}/days/{date}/activate
GET  /payroll/periods/{id}/day-grid?work_date={date}
PUT  /payroll/periods/{id}/day-grid/{date}
```

Calendar returns allowed navigation days, default work days, configured off days, added days, previous/next day, and capabilities.

Prepared grid responses omit financial calculated amounts and gross totals.

### 8.5 Off drivers

```text
GET /payroll/periods/{id}/off-drivers/summary
GET /payroll/periods/{id}/off-drivers?work_date={date}
```

Summary returns fully-off drivers using the backend rule below. Selected-day response returns driver, day name, date, status/reason, note indicator, note, and stable historical status identity.

### 8.6 Expected income and calculation preview

```text
GET /payroll/periods/{id}/calculation-preview
```

Allowed for Open and Returned. InReview/Approved returns the submitted snapshot representation rather than recalculating. Draft returns no financial preview. Locked/Archived redirects conceptually to final snapshot/report contracts.

### 8.7 Bonus

```text
GET    /payroll/periods/{id}/bonuses
POST   /payroll/periods/{id}/bonuses
PATCH  /payroll/periods/{id}/bonuses/{bonus_event_id}
DELETE /payroll/periods/{id}/bonuses/{bonus_event_id}
POST   /payroll/periods/{id}/bonuses/batch
```

The list returns all eligible drivers, zero totals, event lists, total bonus, capabilities, and revision. Batch is all-or-nothing, validates all rows before writing, uses idempotency/correlation, and writes complete audit details.

### 8.8 Review

Submit response includes:

- period revision;
- submitted revision;
- calculation snapshot ID/version;
- submitted totals;
- review item ID;
- promoted Prepared/Open result.

Review detail returns the exact submitted snapshot. Approval includes the expected snapshot ID/revision. Return/reject requires a reason and returns the period to `Returned`.

### 8.9 Reports

```text
GET /payroll/periods/{id}/reports/drivers
GET /payroll/periods/{id}/reports/period-work
GET /payroll/periods/{id}/reports/period-pay
GET /payroll/periods/{id}/reports/mixed
```

Open/Returned use current preview. InReview/Approved use submitted snapshot. Locked/Archived use final snapshots.

The canonical response shape should be driver-centric:

- report metadata and dynamic column definitions;
- `drivers[]`;
- each driver contains daily rows, work totals, pay totals, status summaries, bonus events, min/max adjustments, and final totals.

This shape supports either a two-row Work/Pay visual or driver sections without making the backend encode UI layout such as merged cells.

### 8.10 Finalized information library

```text
GET /payroll/finalized/{period_id}/overview
GET /payroll/finalized/{period_id}/off-drivers
GET /payroll/finalized/{period_id}/rates-used
GET /payroll/finalized/{period_id}/reports/{view}
GET /payroll/finalized/{period_id}/audit
```

Every endpoint reads immutable final snapshot data, never current mutable settings.

---

## 9. Calculation Rules

### 9.1 Authoritative inputs

- period/calendar/configuration snapshot;
- driver-date eligibility;
- operational source entries by work date;
- normalized statuses and status pay rules;
- approved effective-dated rates resolved for each work date;
- pay-item calculation behavior;
- period-level min/max rules under a documented effective-date policy;
- canonical bonus events;
- source and calculation version.

### 9.2 Daily calculation

Every Daily Pay Item is calculated on its actual work date. Period-wide quantity multiplication by one rate is prohibited because rate, eligibility, status, and rules can change by date.

For each driver/date/pay item, retain:

- operational quantity/value;
- PayItemID and period snapshot metadata;
- resolved RateType/DriverRate/tier/rule IDs;
- resolved rate values;
- calculation behavior and version;
- unrounded and rounded amount according to one documented rounding policy;
- blocker/warning state.

### 9.3 Required calculation order

For each driver:

```text
normal_daily_pay = sum(all normal daily calculated pay)
normal_period_pay = sum(non-bonus normal period earnings, if supported)
normal_base = normal_daily_pay + normal_period_pay

minimum_adjustment = max(minimum - normal_base, 0)
after_minimum = normal_base + minimum_adjustment

maximum_adjustment = min(maximum - after_minimum, 0)
normal_after_minmax = after_minimum + maximum_adjustment

total_bonus = sum(active bonus events)
total_pay = normal_after_minmax + total_bonus
```

The calculation must reject minimum greater than maximum.

Bonus never changes minimum or maximum adjustment.

### 9.4 Expected income definition

Hub `total_expected_income` is the projected total payout:

```text
sum(driver.normal_after_minmax + driver.total_bonus)
```

The response must also expose normal pay, min adjustment, max adjustment, and bonus separately. This preserves the daily-based definition while making expected company liability unambiguous.

Prepared has no expected-income response.

### 9.5 Lifecycle calculation behavior

| Period status | Financial behavior |
| --- | --- |
| Draft/Prepared | No exposed expected income; operational source only |
| Open | Read-only calculation preview from current source/config version |
| Returned | Read-only correction preview from current source/config version |
| InReview | Read immutable submitted calculation snapshot |
| Approved | Read the approved submitted snapshot |
| Locked/Archived | Read immutable final snapshot/ledger |
| Cancelled | No financial preview |

### 9.6 No manual recalculate workflow

There is no user-operated Recalculate button. Calculation is invoked by backend reads and lifecycle commands:

- preview on demand;
- snapshot on submit;
- snapshot validation on approve;
- snapshot consumption on finalize.

### 9.7 Reports

#### Drivers Report

- dynamic work/pay columns from period Pay Item snapshot;
- daily source and calculated pay;
- bonuses with amount, giver, time, reason/notes;
- normal total;
- min/max adjustment;
- total bonus;
- total pay.

#### Period Work

- backend-calculated work totals by Pay Item;
- status summaries expressed as counts/categories, not concatenated labels;
- example: `2 off days`, `1 PTO day`.

#### Period Pay

- pay totals by Pay Item;
- normal gross;
- minimum adjustment;
- maximum adjustment;
- total bonus;
- total pay.

#### Mixed Summary

- one driver object with both `work` and `pay` sections;
- backend returns data semantics, not row-span layout instructions.

### 9.8 Rounding decision required before implementation

The calculation core must establish one documented money rounding policy, including line precision, per-day aggregation, per-driver aggregation, and currency rounding. Current Decimal behavior is not yet a complete product contract.

---

## 10. Status and Off-Driver Rules

### 10.1 Status rules

- Status is system-owned and not a Pay Item.
- Status selection references a StatusKeyID.
- Historical code, label, off flag, and relevant allowance metadata are snapshotted.
- Status Key deactivation prevents new selection but does not alter history.
- Allowance effects and payroll effects are independent.
- A future StatusKeyPayRule is effective-dated and calculation-versioned.
- Financial effects generated by status become derived calculation lines, not manual grid Pay Items.

### 10.2 Hub fully-off KPI

Official rule:

A driver counts as fully off when:

1. the driver has at least one eligible scheduled work day in the period;
2. every driver-eligible scheduled work day has an active status snapshotted as `IsOffReason=true`;
3. the driver has no normal working quantity/pay source on those days.

Configured branch off days that are not activated through Add Day are excluded from the denominator. A mid-period driver is evaluated over their eligible scheduled work days only.

The KPI counts distinct drivers, never driver-days.

### 10.3 Grid selected-day off list

The selected-day contract returns one row per off driver for that date. It does not reuse the Hub KPI. It includes stable reason/status label, note presence, and note text/detail capability.

### 10.4 Day navigation

- Default navigation uses snapshotted scheduled work days.
- Added configured off days become navigable.
- Previous/next values come from the backend calendar.
- Arbitrary dates outside the period are rejected.
- Configured off dates cannot receive normal grid entry until activated through Add Day.
- If no off days are configured, Add Day capability is false.

---

## 11. Status System Rules for Period Lifecycle

### 11.1 Target statuses

| Status | Product label | Editable | Submit | Financial preview | Review/finalize |
| --- | --- | --- | --- | --- | --- |
| Draft | Prepared | Provisional operational entry only | No | No | No |
| Open | Open | Yes | Yes | Current preview | No direct approval/finalize |
| InReview | In Review | No | No | Submitted snapshot | Review decision only |
| Returned | Returned / Needs Correction | Yes | Resubmit | Current correction preview | No finalize |
| Approved | Approved | No | No | Approved snapshot | Finalize only |
| Locked | Finalized | No | No | Final snapshot | Archive only |
| Archived | Archived | No | No | Final snapshot | Terminal |
| Cancelled | Cancelled | No | No | None | Terminal |

### 11.2 Allowed transitions

```text
Draft -> Open                 # promotion only
Open -> InReview              # submit command
InReview -> Approved          # review approve
InReview -> Returned          # review return/reject
Returned -> InReview          # resubmit command
Approved -> Locked            # finalization only
Locked -> Archived
Draft/Open/Returned -> Cancelled through privileged command
```

InReview/Approved cancellation requires a separately reviewed privileged policy if retained. No generic status PATCH may bypass command invariants.

Removed paths:

- InReview→Open manual transition;
- Approved→InReview;
- Locked reopen;
- Archived reopen;
- Cancelled revival.

### 11.3 Atomicity and concurrency

Every lifecycle command must:

- lock the branch workflow;
- assert expected current status and expected data revision;
- update using old-status/revision predicates;
- update all linked review/snapshot/promotion rows in the same transaction;
- return conflict on stale state;
- write audit events in the same transaction.

The existing CDPI revision pattern in this repository is a useful local implementation precedent.

---

## 12. Bonus Rules

- Bonus is an auditable event, not a rate and not a minimum guarantee.
- Multiple bonus events per driver/period are allowed.
- Each event has positive amount, giver, time, optional reason/notes, and lifecycle metadata.
- Bonus entry is allowed only for Open or Returned periods.
- Prepared cannot receive bonuses.
- InReview/Approved/Locked/Archived cannot mutate bonuses.
- All eligible drivers appear in bonus summary even with zero total.
- Drivers with nonzero totals sort first, then by backend-defined stable driver order/name.
- Batch operations are transactional, idempotent, revision-safe, and audit-correlated.
- Bonus is excluded from min/max by schema/domain invariant, not UI convention.
- Driver report shows events.
- Period Pay shows aggregated Total Bonus.
- Final ledger preserves both event detail and aggregate total.
- No dual source between BonusEvents and DraftLines/PayrollRunBonuses.

---

## 13. Review, Finalization, Ledger, and Audit Rules

### 13.1 Submit

Submit must atomically:

- verify Open status and expected DataRevision;
- verify no other InReview period;
- resolve current source/config versions under lock;
- run calculation blockers;
- persist immutable calculation snapshot;
- populate SubmittedAtUtc/SubmittedBy/SubmittedDataRevision;
- create Pending PeriodApproval review item linked to snapshot;
- set Open→InReview;
- promote Prepared→Open if present;
- write audit events.

### 13.2 Review

- Reviewer sees exactly the submitted snapshot.
- Reviewer cannot trigger recalculation of different values.
- Approval request carries expected snapshot ID and submitted revision.
- Approval fails if linkage/status is inconsistent.
- Return/reject requires a reason, resolves the review item, and moves period to Returned.
- Comments do not resolve the review item.
- Self-approval policy is explicit and defaults off for new companies.

### 13.3 Finalization

- Approved-only.
- Uses the approved CalculationSnapshotID.
- Does not resolve newer current rates/rules/configuration.
- Rechecks structural snapshot integrity and idempotency.
- Writes FinalLines and final metadata atomically.
- Moves Approved→Locked.
- Captures all calculation, rate, rule, bonus, status, actor, and configuration snapshots needed for final reports.

### 13.4 Ledger

- Locked/Archived ledger is final truth.
- Ledger/report reads require a dedicated ledger/report permission, not incidental payroll entry permission.
- Finalized reports do not query mutable current labels/rates/status keys for meaning.
- Finalized periods never reopen.

### 13.5 Audit/security detail

Finalized period audit contract must expose:

- source-entry actor/time and changes;
- bonus actor/time/changes;
- status/note changes;
- rate/rule identifiers and actor/timestamps used;
- submit actor/time/revision;
- review comments and decisions;
- return/rejection reasons;
- approval actor/time/snapshot;
- finalization actor/time;
- responsible user/role snapshot;
- correlation IDs.

Financial audit records and calculation snapshots must be append-only/immutable at the database boundary.

---

## 14. Severity Findings

### P0 — Blockers

1. **InReview is editable.** Reviewed payroll source can change after submission.
2. **No immutable submitted revision/snapshot.** Reviewers do not approve a durable calculation identity.
3. **Finalization can re-resolve newer rates after review.** Final pay may differ from reviewed pay.
4. **Lifecycle writes are not uniformly concurrency-safe.** Stale transitions can bypass the state graph.
5. **Bonus participates in current min/max earnings.** This produces financially incorrect results under the required product rule.

### P1 — Must complete before real customer payroll

1. No one-InReview slot enforcement.
2. No Returned state for an older rejected period when a newer Open exists.
3. No smart schedule-derived creation/promotion workflow.
4. Client-supplied standard period dates/pay date remain authoritative.
5. No SemiMonthly cadence or durable schedule version.
6. No period-day/calendar/Add Day snapshot.
7. Prepared pre-entry cannot be enabled safely through current response contracts.
8. No backend Current Payroll Hub contract.
9. No Open/Returned expected-income contract.
10. No calculation/report contracts for required views.
11. Bonus is generic Period Pay with competing unused storage.
12. Status is mutable code text and historical labels can disappear.
13. Current employment status can hide historical eligibility.
14. Pay-item order/labels are not period-snapshotted.
15. Review return/manual transition paths can orphan workflow state.
16. Draft cancellation lacks a transition-specific action permission.
17. Fourteen tenant-integrity constraints remain unvalidated.
18. Finalized information-library metadata is incomplete.

### P2 — Important follow-up

1. Status usage limits need concurrency-safe enforcement.
2. Effective-dated status pay rules are missing.
3. Audit old/new detail is incomplete for several mutations.
4. Audit append-only database enforcement is missing.
5. Dedicated ledger/report permissions are missing or unclear.
6. Current dormant payroll tables create source-of-truth ambiguity.
7. Calculation rounding policy is not a formal contract.
8. Historical employee inactive/suspension intervals may require a dedicated history model.

### P3 — Cleanup

1. Router/OpenAPI transition documentation is stale.
2. Review comments describe behavior that no longer matches code.
3. Some mutable schema defaults should use safe factories.
4. Legacy naming (`DraftLines` containing non-pay informational records) should be clarified after domain cutover.

---

## 15. Roadmap Status Board

Allowed phase statuses are `Pending`, `In Progress`, and `Done`.

| Phase | Goal | Status | Owner | Review Gate |
| --- | --- | --- | --- | --- |
| Phase 0 | Workflow integrity lockdown | Done with Notes | Claude | Codex P0 review |
| Phase 1 | Lifecycle slots, Returned state, smart creation | Pending | Claude | Codex lifecycle review |
| Phase 2 | Schedule, calendar, pay-item, eligibility, and status snapshots | Pending | Claude | Codex data-model review |
| Phase 3 | Canonical bonus domain and min/max classification | Pending | Claude | Codex financial-rule review |
| Phase 4 | Unified calculation core and immutable review snapshot | Pending | Claude | Codex calculation parity review |
| Phase 5 | Hub and calculation-report contracts | Pending | Claude | Codex contract/security review |
| Phase 6 | Finalized payroll information library and audit | Pending | Claude | Codex ledger immutability review |
| Phase 7 | Constraint, permission, performance, and rollout hardening | Pending | Claude | Codex release review |

No phase may be marked Done unless implementation exists, required tests ran successfully, Codex review passed, and no phase-scoped P0/P1 finding remains.

---

## 16. Implementation Phases

### Phase 0 — Workflow Integrity Lockdown

**Status:** `Done with Notes`

- [x] P0A: make InReview and later source data read-only. — **Done with Notes**
- [x] P0B: make lifecycle transitions expected-state safe. — **Done with Notes**
- [x] P0C: repair transition permissions/review resolution. — **Done with Notes**
- [x] P0D: add concurrency regression tests. — **Done with Notes**

### Phase 0 completion note

**Status:** Done with Notes  
**Completed units:** CP-0A, CP-0B, CP-0C, CP-0D  
**Review result:** Phase 0 may be marked Done with Notes after CP-0D commit.

**Phase 0 completed:**
- Source mutations are blocked outside Open periods.
- Source mutations are protected against period transition races.
- PayItem deletion/retirement cannot orphan meaningful source rows.
- Lifecycle transitions are expected-state safe.
- Stale transition requests fail instead of overwriting newer statuses.
- Terminal-state revival protections are covered.
- Review/manual InReview exit concurrency is deadlock-safe.
- Transition permissions and review item resolution were repaired.
- Phase 0 concurrency regression tests now cover both transition-wins and source-write-wins outcomes.

**Phase 0 remaining debt:** See CP-0A, CP-0B, CP-0C, and CP-0D completion notes.

### CP-0D completion note

**Status:** Done with Notes  
**Codex verdict:** PASS WITH NOTES  
**Backend/test commit:** f57c63d  
**Review result:** No P0/P1 blockers remain.

**Validation reported by Codex:**
- CP-0D focused tests: 3 passed.
- CP-0A through CP-0D: 114 passed.
- Broad review/finalization/ledger/security/schema bundle: 192 passed, 1 failed.
- Failing case passed in isolation.
- Alembic current/head: 0047 / 0047.
- `git diff --check`: clean.

**What CP-0D completed:**
- Added Phase 0 concurrency regression coverage without production code changes.
- Added transition-wins coverage for source mutation vs Open -> Cancelled.
- Added concurrent double-submit stale predicate coverage.
- Added mutation-first serialization coverage proving a source mutation holds the real period row lock before submit can transition the period.
- Proved source-write-wins outcome:
  - source mutation acquires the Open-period lock first;
  - submit cannot complete while the source lock is held;
  - source commits first;
  - submit subsequently succeeds;
  - period ends InReview;
  - the racing source line remains Active and associated with the period;
  - Pending PeriodApproval item exists;
  - later source mutation is rejected after InReview.

**Remaining notes to revisit later:**
- P2: Order-dependent finalization-preview fixture contamination: `test_preview_gross_matches_finalize` fails in the broader order but passes in isolation.
- P2: Double-submit test does not run two complete submit APIs.
- P2: Transition permission mapping remains fail-open.
- P2: Automatic review cancellation lacks a dedicated review audit event.
- P2: Cancelled terminal behavior lacks equivalent database-trigger protection.
- P2: Approved cancellation policy remains unresolved.
- P2/P3: Temporary database shutdown and pytest-cache warnings.
- P3: Mutation-first test failure cleanup can reference an unassigned task.

**Environment warnings observed during review:**
- Pytest cache WinError 183.
- Temporary PostgreSQL shutdown/leak warning.
- Git global-ignore permission warning.

### CP-0C completion note

**Status:** Done with Notes  
**Codex verdict:** PASS WITH NOTES  
**Backend commit:** aa7ae8b  
**Review result:** No P0/P1 blockers remain.

**Validation reported by Codex:**
- CP-0A + CP-0B + CP-0C: 111 passed.
- Finalization-preview + ledger + finalize: 70 passed.
- Review + workflow + security + schema: 130 passed.
- Total: 311 passed, 0 failed.
- Alembic current/head: 0047 / 0047.
- `git diff --check`: clean apart from line-ending warnings.

**What CP-0C completed:**
- Fixed Draft -> Cancelled permission gap.
- Blocked Approved -> InReview because it created an InReview period with no active Pending PeriodApproval review item.
- Removed Approved -> InReview from valid transitions and permission mapping.
- Updated backend router transition description so it no longer advertises Approved -> InReview.
- Resolved Pending PeriodApproval review items safely when InReview periods exit through allowed manual Open/Cancelled paths.
- Standardized lock ordering for payroll manual InReview exits and review decisions as ReviewItem -> Period.
- Added deterministic deadlock regression coverage proving the payroll PATCH attempts the review-item lock before the period lock.
- Updated finalization-preview, ledger, and finalize tests so test setup no longer depends on the blocked Approved -> InReview transition.

**Remaining notes to revisit later:**
- P2: Transition permission lookup remains fail-open for future unmapped transitions.
- P2: Automatic review cancellation lacks a dedicated review-domain audit event.
- P2: Approved cancellation policy remains unresolved.
- P3: No explicit decoy-review-item non-interference test.
- P2/P3: Test database shutdown, fixture-order, and line-ending hygiene remain.

**Environment warnings observed during review:**
- Pytest cache WinError 183.
- Temporary PostgreSQL automatic-shutdown/leak warning.
- Git global-ignore permission warnings.
- CRLF conversion warnings for three files.

### CP-0B completion note

**Status:** Done with Notes  
**Codex verdict:** PASS WITH NOTES  
**Backend commit:** 72fc222  
**Review result:** No P0/P1 blockers remain.

**Validation reported by Codex:**
- CP-0B focused tests: 27 passed.
- Relevant regression bundle: 304 passed, 1 unrelated order-dependent failure.
- Two isolated failing cases: 2 passed.
- Alembic current/head: 0047.
- `git diff --check`: passed.

**What CP-0B completed:**
- Generic payroll period transitions now require expected old status at the write boundary.
- Stale transition requests return conflict instead of overwriting newer statuses.
- Audit writes happen only after successful transitions.
- Existing safe paths remained intact:
  - Open → InReview
  - Review writeback from InReview
  - Finalization Approved → Locked

**Remaining notes to revisit later:**
- P2: Full-suite fixture contamination.
- P2: Submit/finalization tests do not prove their atomic write boundary.
- P2: Review test does not verify transactional rollback invariants.
- P2: Cancelled is terminal in services but lacks DB-trigger protection.
- P3: Open→InReview should include `companyid` for predicate consistency.
- P3: Existing comments incorrectly claim `get_period_by_id` holds `FOR UPDATE`.

**Environment warnings observed during review:**
- Pytest cache warning: WinError 183.
- Temporary PostgreSQL automatic shutdown reported possible leaked processes/files.
- Git global-ignore permission warning.

### CP-0A completion note

**Status:** Done with Notes  
**Codex verdict:** PASS WITH NOTES  
**Backend commit:** 38abcb9  
**Review result:** No P0/P1 blockers remain.

**Validation reported by Codex:**
- CP-0A focused tests: 74 passed.
- Broader payroll/day-grid/settings/security/schema regression selection: 350 passed.
- Alembic head: 0047.
- Alembic current: 0047 (head).
- `git diff --check`: passed.

**Remaining notes to revisit later:**
- P2: The update-wins test does not independently prove the recomputation branch.
- P2: No dedicated deterministic multi-item day-grid deadlock regression test exists.
- P3: Some historical service docstrings still mention Open or InReview editing.

**Environment warnings observed during review:**
- Pytest cache creation warning: WinError 183.
- Temporary PostgreSQL automatic shutdown reported possible leaked processes/files.
- Git could not read the user-level global ignore file due permissions.

**Objective**

Stop unreviewed mutations and stale state transitions before expanding functionality.

**Allowed backend areas**

- `backend/app/payroll/service.py`
- `backend/app/payroll/schemas.py`
- `backend/app/payroll/router.py`
- `backend/app/review/service.py`
- focused backend tests
- narrowly required migration only after a separate implementation prompt authorizes it

**Forbidden changes**

- frontend changes;
- hub/report implementation;
- calculation formula redesign;
- broad review-system refactor;
- relaxing tenant/branch checks.

**Required tests**

- every source mutation rejects InReview/Approved/Locked/Archived/Cancelled;
- stale transition loses with 409/422;
- no Cancelled revival;
- review approval racing with entry cannot finalize unreviewed data;
- permission matrix for cancellation;
- branch/company isolation.

**Acceptance criteria**

- only Open is editable until controlled Prepared/Returned entry is introduced later;
- all state writes predicate expected old status;
- stale requests do not alter state;
- InReview cannot mutate through generic line, period-pay, day-grid, or status paths;
- no phase-scoped P0 remains.

**Risk notes**

Existing tests intentionally permit InReview edits and manual returns. Those tests must be replaced, not preserved as desired behavior.

**Codex review checklist**

- [ ] Diff limited to allowed areas.
- [ ] Every mutator enumerated and tested.
- [ ] SQL includes company/branch/period ownership.
- [ ] Concurrency tests use independent transactions where needed.
- [ ] No hidden frontend compatibility workaround.
- [ ] Targeted and regression tests pass.

### Phase 1 — Lifecycle Slots and Smart Creation

**Status:** `Pending`

- [ ] P1A: add Returned domain state and reviewed transition graph.
- [ ] P1B: enforce one InReview slot.
- [ ] P1C: implement branch-locked smart create.
- [ ] P1D: atomically promote Prepared on submit.
- [ ] P1E: return Hub-ready workflow alerts/capabilities.

**Objective**

Implement Draft=Prepared, Open, InReview, Returned, and continuous branch operations without invalid slot combinations.

**Allowed backend areas**

- payroll/review service, schemas, routers;
- branch payroll setup reads;
- focused migrations for status/check/index changes;
- lifecycle/setup tests.

**Forbidden changes**

- financial preview implementation;
- frontend changes;
- arbitrary client date authority;
- adding a separate Prepared status.

**Required tests**

- first create produces Open;
- second create with Open produces Draft;
- third create blocks;
- one InReview enforced at database and service levels;
- submit promotes Draft→Open atomically;
- returned prior period coexists with current Open;
- unresolved Returned blocks newer submit/create-prepared;
- branch concurrency and idempotency;
- audit rollback.

**Acceptance criteria**

- no Prepared-alone state;
- no more than one Draft/Open/InReview per branch;
- returned prior period is representable and actionable;
- generic status PATCH cannot bypass commands;
- capabilities explain every blocked action.

**Risk notes**

Existing direct status transitions and cleanup fixtures assume Rejected→Open. Data migration and test-fixture design must be explicit.

**Codex review checklist**

- [ ] State transition matrix matches this plan.
- [ ] Partial unique indexes and service rules agree.
- [ ] Branch advisory/row locking reviewed.
- [ ] All multi-period updates are one transaction.
- [ ] Review items cannot be orphaned.
- [ ] Legacy period data migration assessed.

### Phase 2 — Period Configuration and Operational Source Snapshots

**Status:** `Pending`

- [ ] P2A: version Payroll Setup and add SemiMonthly design.
- [ ] P2B: create period-day snapshots and Add Day command.
- [ ] P2C: snapshot pay-item layout/order/classification.
- [ ] P2D: normalize daily statuses/notes.
- [ ] P2E: unify historical driver-date eligibility.
- [ ] P2F: enable controlled Prepared operational entry without financial exposure.

**Objective**

Make every period self-describing and historically stable before calculation snapshots depend on it.

**Allowed backend areas**

- payroll/settings/core source needed for schedule, pay items, eligibility, status;
- focused migrations and data backfill;
- related tests.

**Forbidden changes**

- frontend changes;
- status as a Pay Item;
- current mutable configuration for historical reports;
- financial output for Prepared.

**Required tests**

- weekly/biweekly/semi-monthly/monthly/custom boundaries;
- pay-date derivation;
- configured off days and Add Day restrictions;
- stable pay-item order after settings changes;
- mid-period hire/termination/transfer eligibility;
- historical reads after driver/status/pay-item changes;
- Prepared source saves with null/omitted financial fields;
- status-key deactivation preserves history.

**Acceptance criteria**

- period dates and days are server-derived;
- calendar navigation is authoritative;
- pay-item grid/report layout is stable per period;
- status records retain ID and snapshots;
- Prepared can store operational source safely but cannot expose money.

**Risk notes**

Backfilling historical status identity from free-text codes may be ambiguous. Ambiguities must be reported, not silently guessed.

**Codex review checklist**

- [ ] Migration/backfill is deterministic and reversible where practical.
- [ ] Historical data does not depend on active current rows.
- [ ] Prepared response schemas contain no derived financial totals.
- [ ] Period calendar prevents arbitrary dates.
- [ ] Pay-item reorder behavior is versioned, not UI-only.

### Phase 3 — Canonical Bonus Domain and Min/Max Classification

**Status:** `Pending`

- [ ] P3A: choose/convert one canonical bonus-event store.
- [ ] P3B: migrate legacy BONUS DraftLines once.
- [ ] P3C: add zero-inclusive summary and event CRUD.
- [ ] P3D: add transactional batch bonus.
- [ ] P3E: add typed normal-pay/minmax classification.
- [ ] P3F: correct min/max calculation order.

**Objective**

Represent required bonus event metadata without dual truth and eliminate the current financial calculation error.

**Allowed backend areas**

- payroll bonus/period-pay/calculation code;
- pay-item financial classification;
- focused migrations and tests.

**Forbidden changes**

- dual writes;
- bonus in Prepared;
- zero-value bonus events;
- configurable inclusion of bonus in min/max;
- frontend aggregation.

**Required tests**

- multiple bonus events aggregate correctly;
- actor/time/reason retained;
- all eligible drivers returned with zero;
- batch all-or-nothing and idempotent;
- Open/Returned-only mutation;
- bonus excluded from minimum top-up comparison;
- bonus excluded from maximum cap comparison;
- formula example normal 1000 + bonus 200 = total 1200;
- final snapshot/ledger linkage.

**Acceptance criteria**

- one canonical bonus source;
- `TotalBonus` is backend-computed;
- complete event audit exists;
- min/max base excludes all bonus events by invariant;
- no P0 financial-rule gap remains.

**Risk notes**

The unused PayrollRunBonuses table must be audited before repurposing. Existing tests deliberately allow multiple BONUS DraftLines and require controlled replacement.

**Codex review checklist**

- [ ] Source-of-truth cutover is one-way and explicit.
- [ ] No generic Period Pay endpoint can create bonus after cutover.
- [ ] Min/max tests include bonus boundary cases.
- [ ] Batch rollback proven.
- [ ] Tenant/branch/driver eligibility checked for each event.

### Phase 4 — Unified Calculation Core and Immutable Review Snapshot

**Status:** `Pending`

- [ ] P4A: extract pure/versioned daily calculation core.
- [ ] P4B: add Open/Returned calculation preview.
- [ ] P4C: add calculation snapshots and source/config hashes.
- [ ] P4D: submit captures snapshot/revision.
- [ ] P4E: approval binds to snapshot.
- [ ] P4F: finalization consumes approved snapshot.

**Objective**

Guarantee that expected income, review, approval, finalization, and ledger are different lifecycle views of the same calculation.

**Allowed backend areas**

- payroll calculation/finalization/review modules;
- snapshot migrations/schemas;
- rate/rule read paths required for deterministic resolution;
- focused tests.

**Forbidden changes**

- manual recalculate action;
- calculation in frontend;
- finalization against newer current rates;
- financial snapshot for Prepared;
- mutating source during preview.

**Required tests**

- date-specific rate changes;
- mid-period eligibility;
- every supported rate behavior;
- status-generated pay;
- min/max then bonus;
- preview/submission/approval/finalization parity;
- rate changes after submit do not change reviewed/final amounts;
- stale revision/snapshot conflicts;
- immutable snapshot database guards;
- rounding boundaries.

**Acceptance criteria**

- one calculation service owns all totals;
- InReview/Approved values are frozen;
- finalization consumes approved snapshot identity;
- expected income includes backend breakdown;
- no manual recalc workflow exists;
- current finalization ledger immutability remains intact.

**Risk notes**

This phase changes the highest-risk financial path. Parallel “old vs new” parity tests should precede cutover, but only one engine may remain authoritative after cutover.

**Codex review checklist**

- [ ] Formula reviewed independently from implementation.
- [ ] Snapshot inputs and version are complete.
- [ ] Preview and finalization parity proven with fixtures.
- [ ] No current-rate lookup occurs after approval during finalization.
- [ ] Decimal/rounding policy tested.
- [ ] Existing locked-ledger triggers preserved.

### Phase 5 — Current Payroll Hub and Calculation Reports

**Status:** `Pending`

- [ ] P5A: Current Payroll Hub aggregate.
- [ ] P5B: fully-off driver KPI.
- [ ] P5C: selected-day off-driver contract.
- [ ] P5D: Drivers Report.
- [ ] P5E: Period Work/Pay/Mixed views.
- [ ] P5F: capabilities/read-only reasons.

**Objective**

Expose complete backend-owned workflow and financial read contracts without frontend aggregation.

**Allowed backend areas**

- payroll read services, schemas, routers;
- calculation snapshot/report readers;
- permission helpers;
- focused tests.

**Forbidden changes**

- frontend changes;
- client-calculated totals;
- report-time reads from current mutable settings for frozen periods;
- N+1 endpoint composition as the official Hub contract.

**Required tests**

- company scope across branches;
- branch-only scope;
- pagination independence;
- Open/Prepared/InReview/Returned slot responses;
- fully-off KPI distinct-driver semantics;
- selected-day off rows;
- dynamic Pay Item report columns;
- backend status summaries;
- bonus/min/max report breakdown;
- capability permission/status matrix;
- ODA denial.

**Acceptance criteria**

- one Hub response supplies all required workflow data;
- no financial field is frontend-derived;
- Prepared returns no expected income;
- report totals match calculation snapshot;
- Mixed response supports both intended visual styles.

**Risk notes**

Hub KPI definitions must not reuse current day-grid `worked/off` counters or period off-row count.

**Codex review checklist**

- [ ] Every metric definition traced to backend query/calculation.
- [ ] Scope and permission filtering reviewed per branch.
- [ ] No hidden frontend dependency required.
- [ ] Frozen statuses read snapshots.
- [ ] Performance query plan reviewed for realistic branch counts.

### Phase 6 — Finalized Payroll Information Library

**Status:** `Pending`

- [ ] P6A: final overview and reports.
- [ ] P6B: final off/status source snapshot views.
- [ ] P6C: rates/rules/bonus used views.
- [ ] P6D: security/audit detail.
- [ ] P6E: participant/role snapshot.
- [ ] P6F: future correction boundary contract.

**Objective**

Make Locked/Archived payroll a complete immutable information library, not only a list of money lines.

**Allowed backend areas**

- finalization/ledger/audit/report readers;
- final snapshot migrations;
- permission catalog and tests.

**Forbidden changes**

- reopening Locked/Archived;
- recalculating final reports from current configuration;
- frontend changes;
- implementing the future correction workflow itself unless separately scoped.

**Required tests**

- all final report views match final totals;
- deactivating/changing current rates, Pay Items, statuses, employees, or roles does not change final reports;
- audit actor/time/reason fields complete;
- dedicated ledger/report permission matrix;
- immutable update/delete/insert guards;
- archived parity with locked.

**Acceptance criteria**

- finalized library contains required reports, off drivers, used rates/rules, bonus detail, and security/audit details;
- all content is immutable and historically stable;
- future correction entry point references but never reopens original period.

**Risk notes**

Do not rely solely on mutable AuditLog joins for user roles/responsibility; snapshot what the finalized library promises to preserve.

**Codex review checklist**

- [ ] Every final view reads immutable data.
- [ ] Database guards tested directly.
- [ ] No mutable label/config dependency.
- [ ] Permission is least-privilege.
- [ ] Finalization remains atomic.

### Phase 7 — Hardening and Rollout

**Status:** `Pending`

- [ ] P7A: validate legacy composite constraints.
- [ ] P7B: append-only audit hardening.
- [ ] P7C: permission cleanup and self-approval defaults.
- [ ] P7D: query/index/load testing.
- [ ] P7E: migration rehearsal and reconciliation.
- [ ] P7F: remove/deprecate obsolete contracts.

**Objective**

Prove tenant integrity, performance, migration safety, and operational readiness before beta/customer payroll.

**Allowed backend areas**

- schema/migrations;
- payroll/review/settings/auth permission paths;
- tests and operational verification scripts;
- API deprecation documentation.

**Forbidden changes**

- silent destructive cleanup;
- skipping legacy data reconciliation;
- marking Done based only on unit tests;
- frontend patches masking backend failures.

**Required tests/checks**

- `VALIDATE CONSTRAINT` rehearsal after data audit;
- tenant/branch security matrix;
- concurrency/load tests;
- large-period calculation/report performance;
- migration forward/rollback rehearsal where supported;
- old/new financial reconciliation;
- complete targeted and full backend test runs;
- test PostgreSQL cleanup investigation.

**Acceptance criteria**

- all intended constraints validated;
- no P0/P1 remains;
- permissions and self-approval defaults approved;
- performance budgets met;
- production migration and reconciliation runbook exists;
- Codex release review passes.

**Risk notes**

The current schema guard verifies required objects but does not fail on `NOT VALID` constraints. Add explicit validation coverage.

**Codex review checklist**

- [ ] Live schema matches migration head.
- [ ] All legacy rows reconciled.
- [ ] Full suite and concurrency suite pass cleanly.
- [ ] No temporary database processes leak.
- [ ] API deprecations are explicit.
- [ ] Release decision records remaining P2/P3 debt.

---

## 17. Suggested Claude Implementation Units

Each unit must be executed as a separate, reviewable prompt. Claude must not combine later units opportunistically.

| Unit | Goal | Allowed areas | Forbidden changes | Expected tests | Acceptance |
| --- | --- | --- | --- | --- | --- |
| CP-0A | Freeze InReview source mutations | payroll service/schemas and focused tests | migrations, frontend, Prepared editing, formula changes | mutation status matrix and real race regression | Every source mutation accepts only Open; InReview is read-only |
| CP-0B | Make transitions expected-state safe | payroll/review transition code and tests | new statuses, calculation changes, frontend | stale status/revision races and terminal-state cases | Stale transitions conflict; no terminal-state revival |
| CP-0C | Repair transition permissions/review resolution | payroll/review/core permission paths and tests | broad role redesign or UI changes | cancellation matrix, pending-review resolution, rollback | Cancellation/manual paths cannot bypass policy or orphan review |
| CP-1A | Add Returned state and transition graph | payroll/review schemas, one focused migration, tests | Prepared enum, Locked reopen, frontend | return with newer Open, edit, resubmit, cancellation | Return works with newer Open; resubmit is coherent |
| CP-1B | Enforce Draft/Open/InReview slots | focused migration, payroll service, tests | report/calculation work | concurrent slot claims and legacy-data preflight | One Draft/Open/InReview slot each under concurrency |
| CP-1C | Smart create and submit-time promotion | payroll/settings service and lifecycle tests | arbitrary standard dates or financial preview | no-period/Open/Prepared/InReview/Returned state matrix | First Open, second Prepared, third blocked, atomic promotion |
| CP-2A | Version schedule and derive period/pay dates | settings/payroll models, focused migration, tests | frontend and client date authority | all cadences, pay-date boundaries, version history | All cadences including SemiMonthly are server-derived |
| CP-2B | Period calendar and Add Day | payroll model/service and tests | arbitrary dates or UI-only enforcement | off-mask, activation, navigation, bounds | Only valid period/configured off days are navigable/activatable |
| CP-2C | Pay-item layout snapshot | payroll/settings, focused migration, tests | blocking all future settings changes | reorder/rename/retire after period creation | Historical order, labels, and classifications remain stable |
| CP-2D | Normalize daily status/note | payroll/settings, focused migration/backfill, tests | new pseudo status Pay Items or silent guesses | deactivation/rename/history/duplicate/race tests | StatusKeyID plus snapshots; no new pseudo status lines |
| CP-2E | Canonical eligibility service | payroll/core eligibility paths and tests | frontend filtering or unrelated employee redesign | hire/termination/transfer/current-status history matrix | All endpoints agree on driver-date eligibility and history |
| CP-2F | Controlled Prepared pre-entry | payroll schemas/service and tests | bonus, expected income, submit/finalize for Draft | source save/read matrix and financial-field absence | Operational saves allowed; no financial exposure or actions |
| CP-3A | Canonical bonus-event migration | payroll bonus model, focused migration, tests | dual writes, zero events, Prepared bonus | legacy migration, multiple events, actor metadata | One source, multiple events, historical migration reconciled |
| CP-3B | Bonus summary and transactional batch | payroll service/router and tests | frontend aggregation or partial-success batch | zero-inclusive list, idempotency, revision, rollback | Zero-inclusive, revision-safe, all-or-nothing behavior |
| CP-3C | Financial classification and min/max fix | payroll/pay-item/calculation paths and tests | hardcoded UI totals or bonus opt-in to min/max | min/max boundary cases with and without bonus | Bonus is excluded from min/max in preview and final paths |
| CP-4A | Extract versioned calculation core | payroll calculation module and tests | manual recalc or frontend calculation | daily effective-rate, eligibility, behavior, rounding cases | Daily/date-effective deterministic results from one core |
| CP-4B | Open/Returned calculation preview | payroll read contracts and tests | writes during preview or Draft money | parity, blocker, scope, Prepared denial | Backend expected-income breakdown with no source mutation |
| CP-4C | Immutable submission snapshot | payroll/review, focused migration, tests | mutable snapshots or UI-only freeze | submit revision, snapshot immutability, rate-change cases | Submitted revision, inputs, and totals are frozen |
| CP-4D | Approval/finalization snapshot parity | payroll/review/finalization and tests | current-rate re-resolution after approval | post-submit rate/rule changes and final parity | Finalization equals approved snapshot despite later changes |
| CP-5A | Current Payroll Hub | payroll read API and tests | frontend aggregation or N+1 official contract | company/branch scope, slot/status/capability matrix | Complete scope, slots, metrics, and capabilities in one response |
| CP-5B | Off-driver contracts | payroll read API and tests | driver-day count as Hub KPI | fully-off denominator and selected-day detail cases | Fully-off KPI and selected-day list remain distinct |
| CP-5C | Calculation report bundle | payroll report API and tests | frontend sums or mutable config for frozen reports | dynamic columns and Drivers/Work/Pay/Mixed parity | All required report totals are backend-owned |
| CP-6A | Finalized report library | ledger/report API and tests | Locked reopen or current-config recalculation | config mutation after finalization and report parity | Final reports read immutable snapshots |
| CP-6B | Final audit/security library | audit/ledger/permission paths and tests | mutable audit promises or broad entry permission | actor/rate/review/role snapshot and access matrix | Required actor, rate, review, and role details are preserved |
| CP-7A | Constraint and permission hardening | migrations/auth/schema tests | destructive cleanup without audit | constraint validation and least-privilege matrix | Legacy constraints validated; permissions least-privilege |
| CP-7B | Performance/reconciliation rollout | tests, verification scripts, runbook | product behavior changes or hidden failures | load, concurrency, full suite, financial reconciliation | Scale and financial reconciliation approved |

### Exact first implementation unit: CP-0A

**Goal:** Make every payroll source mutation reject InReview and all later/terminal states.

**Allowed files/areas:**

- `backend/app/payroll/service.py`
- `backend/app/payroll/schemas.py`
- focused tests covering draft lines, period pay, day grid, and bonus compatibility

**Forbidden changes:**

- no migrations;
- no frontend;
- no Prepared editing yet;
- no status enum changes;
- no calculation formula changes;
- no review UI/API redesign.

**Required implementation behavior:**

- Open is the only editable period for this unit.
- Add/update/void draft-line operations recheck current period status within the write transaction.
- Period-pay add/update/void operations do the same.
- Day-grid save does the same.
- SQL updates are scoped by company/period/line ownership.
- A concurrent status change cannot allow a late mutation to commit after the period leaves Open.

**Expected tests:**

- Open mutation success;
- Draft/InReview/Approved/Locked/Archived/Cancelled rejection for each mutation family;
- race test using separate transactions/connections;
- ODA, company, branch, and permission regressions;
- audit rollback remains intact.

**Acceptance criteria:**

- no application source mutation can commit unless the period is Open at the protected write boundary;
- existing finalization/ledger tests remain green;
- Codex review confirms all mutation paths are covered.

---

## 18. Codex Review Gates After Each Unit

Every implementation unit receives the following base gate plus its phase-specific checklist.

### Gate A — Scope

- Diff is limited to the unit’s allowed areas.
- No frontend change.
- No unrelated refactor or migration.
- Existing user work is preserved.

### Gate B — Source truth

- Behavior is traced through service, schema, migration, and tests.
- No old markdown claim is accepted without code/test evidence.
- API documentation matches actual transitions and permissions.

### Gate C — Tenant/security

- CompanyID is enforced in every query.
- Branch scope is checked.
- ODA/driver restrictions remain.
- Action permission is explicit.
- Error bodies do not leak payroll data.

### Gate D — Workflow/concurrency

- Expected status/revision is enforced at the write boundary.
- Multi-row state changes are transactional.
- At least one real concurrency regression test exists for race-sensitive changes.
- Review/snapshot/audit records cannot be orphaned.

### Gate E — Financial correctness

- Formula matches this document.
- Daily effective date resolution is tested.
- Bonus/min/max classification is explicit.
- Preview/submission/finalization/ledger parity is tested where applicable.
- No frontend aggregation is required.

### Gate F — Historical durability

- Frozen periods do not depend on mutable current configuration.
- Actor/time/source snapshots are sufficient.
- Locked/Archived immutability remains database-enforced.

### Gate G — Verification

- Focused tests pass.
- Relevant regression suite passes.
- Migration head/schema guard verified when migrations are involved.
- Environment failures and warnings are reported.
- Unit is not marked Done while a scoped P0/P1 remains.

---

## 19. Product Decisions Still Required

These do not block CP-0A but must be resolved before their listed phases.

1. **SemiMonthly boundaries and pay-date behavior** — confirm supported patterns and holiday/weekend adjustment rules before Phase 2A.
2. **Money rounding policy** — line/day/driver/currency rounding before Phase 4A.
3. **Self-approval default and exceptions** — recommended default is disabled before Phase 4C.
4. **Cancellation policy for InReview/Approved** — recommended default is no direct cancellation without a dedicated privileged command and reason.
5. **Returned backlog policy** — this plan recommends blocking newer submit and further Prepared creation while allowing current Open saves.
6. **Historical employment intervals** — confirm whether suspension/inactive/reinstatement ranges must affect payroll eligibility; if yes, effective-dated employment history is required.
7. **Custom-period authority** — define which role may create exceptions and whether company approval is required.
8. **Correction workflow boundary** — define future correction records/permissions, while retaining the rule that finalized periods never reopen.

---

## 20. Definition of Done

A phase may move to `Done` only when all are true:

- all phase checklist items are checked;
- implementation exists in current source and migrations;
- data migration/reconciliation has been performed where applicable;
- required focused and regression tests were run successfully;
- Codex review gate passed;
- API contracts and source documentation match implementation;
- no P0 or P1 finding remains within the phase scope;
- no frontend placeholder or aggregation is required to make the backend appear complete;
- environment failures/warnings are recorded and dispositioned.

Until then, the phase remains `Pending` or `In Progress`.
