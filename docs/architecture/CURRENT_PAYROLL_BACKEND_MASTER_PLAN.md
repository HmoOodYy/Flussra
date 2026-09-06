# Current Payroll Backend Master Plan

**Project:** Flussra Payroll App  
**Document role:** Official working plan for the Current Payroll backend rebuild/redesign  
**Plan status:** Active planning baseline  
**Source baseline reviewed:** Git commit `bb491cc600335b4c0a69b63692717b21ae361d62` (`2026-06-18`)  
**Database migration baseline (original planning baseline):** Alembic `0047 (head)` — this was the migration head when this document's original planning baseline was reviewed; it is not the current head.  
**Current migration head after implemented units:** Alembic `0063` (CP-5C frozen report-evidence persistence; CP-4F added no migration)
**Last source revalidation:** 2026-06-19  
**Implementation status:** Phase 0 is `Done with Notes`; Phase 1 is `Done with Notes`; CP-1A, CP-1B, CP-1C, CP-1D, and CP-1E are `Done with Notes`; Phase 2 is `Done with Notes`; CP-2A, CP-2B, CP-2C, CP-2D1, CP-2D2, CP-2E, and CP-2F are `Done with Notes`; Phase 3 is `Done with Notes`; Phase 4 (unified calculation core and immutable review snapshot) is `Complete` — CP-4A is `Completed` (commit `9b76aa9`), CP-4B is `Completed` (commit `f3988b7`), CP-4C is `Completed` (commit `d7f6b9e`), CP-4D is `Completed` (commit `9e4c32f`), CP-4E is `Completed` (commit `e332d02`), and CP-4F is `Completed` (commit `434f673`). Phase 5 (Current Payroll Hub and calculation reports) is `Complete`: CP-5A is `Completed` (commit `5d05756`), CP-5B is `Completed` (commit `9ecb300`), RP-1 is its completed internal report-authority foundation, the CP-5C frozen report-evidence persistence remedy is `Completed` (commit `1a106b8`), and CP-5C Calculation Report Bundle is `Completed` (commit `23bf68f`). Phase 6 is the next implementation direction and remains `Pending`; Phase 7 remains `Pending`.

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

Passing tests validate current behavior, but they do not prove requirements that no current test asserts. CP-4D through CP-4F now directly cover submitted immutable revisions, snapshot-bound approval, and finalization from the approved snapshot. **Bonus exclusion from min/max is now directly covered:** CP-3C added a focused 17-test suite (`test_cp3c_minmax_bonus.py`) that directly asserts minimum/maximum with and without bonus, bonus-only drivers, preview/finalization/ledger parity, batch-created bonus events, active-vs-voided bonuses, STATUS_PAYMENT and non-BONUS ADJUSTMENT classification, and legacy BONUS DraftLine exclusion. The older `test_m15.py` suite remains stale CP-1C/CP-3A debt (legacy period-creation helper and generic BONUS `/period-pay` assumptions) and is not a clean regression suite on its own, but CP-3C's own coverage now directly proves the corrected formula.

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
- `Returned`
- `Approved`
- `Locked`
- `Cancelled`
- `Archived`

Current transitions are:

```text
Draft -> Open | Cancelled
Open -> InReview | Cancelled
InReview -> Approved | Returned (through a PeriodApproval review decision)
Returned -> InReview (through Resubmit)
Approved -> Locked (through finalization)
Locked -> Archived
Cancelled -> terminal
Archived -> terminal
```

Approval is written by the review service. Locked is reached only through finalization.

### 3.3 Current slot enforcement

The database currently enforces:

- at most one Draft per company/branch;
- at most one Open per company/branch.
- at most one InReview per company/branch;
- at most one Returned per company/branch.

### 3.4 Draft today (updated: CP-2F)

Draft is the internal database status for the product concept “Prepared.” Draft is created by the candidate creation endpoint. Draft cannot submit, approve, finalize, or use the Approved finalization preview.

CP-2F enabled controlled operational source entry for Draft. Draft is now an active source-entry workspace with financial exposure fully suppressed. `SOURCE_ENTRY_STATUSES = {“Draft”, “Open”, “Returned”}` guards source-only paths; `ENTRY_ALLOWED_STATUSES = {“Open”, “Returned”}` continues to guard all financial paths. Draft lines store `CalculatedAmount = NULL`, `RateAmount = NULL`, and `NeedsManagerReview = FALSE`. Draft day-grid responses return `gross_total = null` and `financials_available = false`. Status payment derivation is suppressed in Draft saves. Draft→Open activation regenerates/freezes eligibility, derives status payment, and refreshes daily calculations in one transaction.

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

Gaps (pre-CP-2A/2B baseline; partially resolved):

- no SemiMonthly cadence (still pending);
- pay-date offset is forcibly stored as zero (still pending);
- first-pay-date/pay-day fields do not drive period creation (still pending);
- one mutable settings row replaces history — ~~resolved by CP-2A~~: `PayrollScheduleVersions` added; `BranchPayrollSettings.CurrentScheduleVersionID` and `PayrollPeriods.ScheduleVersionID` link periods to immutable versions;
- ~~no schedule version is attached to a period~~ — resolved by CP-2A;
- ~~no period-day snapshot exists~~ — resolved by CP-2B: `PayrollPeriodDays` added;
- no backend Add Day workflow exists (still pending — deferred to future CP-2B2).

### 3.6 Daily grid today

The grid:

- accepts a date only inside the period bounds;
- dynamically loads active Daily Pay Items for that date;
- orders columns by current `PayItems.SortOrder`;
- shows drivers that pass current date eligibility checks;
- writes quantities, DailyStatus, and DailyNote into `PayrollDraftLines`;
- stores the selected status code as mutable text in `PayrollDraftLines.Notes`;
- exposes calculated amounts and a day gross summary;
- permits source writes for Draft/Prepared (operational source only, no financial exposure), Open (normal operational entry), and Returned (correction entry). InReview remains read-only; entry writes are blocked for InReview and all later statuses.

CP-2B added `PayrollPeriodDays` (snapshotted period calendar). CP-2C added `PayrollPeriodPayItems` (stable snapshotted pay-item layout per period). CP-2E added canonical eligibility snapshots (`PayrollPeriodDriverEligibility` + `PayrollPeriodEligibilitySnapshots`): for marked periods, live `Employees.EmploymentStatus` no longer controls eligibility — the frozen snapshot does. Legacy fallback applies only to periods without a CP-2E marker. Add Day activation and full calendar navigation remain future work.

### 3.7 Status system today (updated: CP-2D1, CP-2D2)

- Status Keys are branch-scoped and have code, display name, off-reason flag, allowance fields, usage limits, and activity state.
- CP-2D1: selected status is now stored through `PayrollPeriodDriverDayEntryState` using `StatusKeyID` (FK). Code-text-only storage was the pre-CP-2D1 baseline; it no longer applies to new writes. Historical reads resolve via `StatusKeyID`; deactivated keys referenced in existing canonical rows are pre-fetched from a deactivated key map.
- `DailyStatus` and `DailyNote` pseudo-line dual-write is preserved for finalization legacy compatibility only.
- Status is not a Pay Item. `PTO_STATUS` was removed in commit `236a506` and must not return. Current status payment uses `StatusRateColumns` and CP-2D2 derived system payment lines; it is eligibility-aware and is suppressed for Draft periods.
- There is no effective-dated `StatusKeyPayRule` domain. It remains unimplemented future work — a separate backend rule domain, not part of Phase 4 and not automatically a generic Calculated Method (see "Phase 4 Status payment boundary").

### 3.8 Off-driver behavior today

CP-5B provides two distinct backend-owned contracts:

```text
GET /payroll/periods/{id}/off-drivers/summary
GET /payroll/periods/{id}/off-drivers?work_date={date}
```

The summary is the Hub's fully-off-driver KPI: a distinct period-eligible driver must be Off on every eligible scheduled work day in the period. The selected-day response is a separate date-scoped detail list with canonical Status, reason, and note metadata. The legacy `GET /payroll/periods/{id}/drivers-off` remains a compatibility driver-day-row query and is not the Hub KPI.

Fully-Off uses the period calendar and CP-2E or legacy driver-date eligibility semantics. Unactivated configured off days are excluded; already-activated added days are respected. Missing or non-Off Status, and normal operational work, prevent classification. Status-payment-only, Bonus-only, and system/min/max or period-financial-only values do not. Daily Grid eligibility remains separate: an eligible driver may appear with no work data.

### 3.9 Calculation and expected income today

- Draft (Prepared) lines store `RateAmount = NULL` and `CalculatedAmount = NULL`; CP-2F suppresses all financial fields for Draft. Open/Returned lines can store `RateAmount` and `CalculatedAmount`.
- Submission refreshes rate-dependent calculations.
- Approved finalization preview reads the exact approved immutable snapshot.
- Finalization projects that same approved snapshot into FinalLines without refreshing or resolving mutable financial inputs.
- CP-4B added a read-only Open/Returned expected-income endpoint. CP-4D captures an immutable submitted calculation snapshot on Submit/Resubmit, CP-4E makes that exact linked snapshot the review/approval financial authority, and CP-4F makes it the finalization-preview and FinalLine financial authority.
- Submitted calculation snapshots now exist for CP-4D Submit/Resubmit. Historical InReview/Approved periods are not backfilled.
- Post-approval rates or other mutable financial-source changes cannot alter the approved preview or finalized amount.
- ~~Current min/max calculations sum all non-system final lines, including BONUS.~~ **Resolved by CP-3C.** Min/max calculations now exclude BONUS from the comparison base in both finalization and finalization preview.

Current, enforced calculation order (both `finalize_period` and `get_finalization_preview`):

```text
normal pay -> apply minimum/maximum -> add bonus
```

The previously-documented conflicting order (`normal pay + bonus -> apply minimum/maximum`) was the pre-CP-3C bug and no longer describes current behavior.

Phase 4 completed the unified calculation-core and immutable-snapshot authority chain. CP-4B provides the Open/Returned expected-income endpoint; CP-4F consumes the approved immutable packet for finalization rather than post-review rate/configuration re-resolution.

### 3.10 Bonus today (updated: CP-3C)

- Bonus is now canonicalized as `payroll.PayrollBonusEvents`. The old unused `PayrollRunBonuses` table was converted and renamed by migration 0058.
- Generic Period Pay (`/period-pay`) no longer creates BONUS. The create path rejects `line_type='BONUS'` with a redirect message pointing to `POST /payroll/periods/{id}/bonuses`.
- Legacy BONUS `PayrollDraftLines` rows may remain as migration trace rows only, linked via `SourceDraftLineID`. They are hidden from `/period-pay` reads (`linetype != 'BONUS'` filter), and update/void through `/period-pay` is blocked by a 422 guard.
- Bonus events are strictly positive-only (`Amount > 0` DB constraint). Zero means no event; voiding is explicit.
- Multiple active bonus events per driver/period are allowed.
- Full actor/time/reason/notes/update/void metadata fields exist on `PayrollBonusEvents`.
- Bonus mutation is allowed only for Open and Returned periods. Draft/InReview/Approved/Locked/Archived/Cancelled reject bonus mutation.
- Bonus create validates CP-2E snapshot-aware driver eligibility and branch ownership (same guard as non-BONUS period-pay).
- Submit captures Active `PayrollBonusEvents` in the immutable calculation packet; CP-4F projects that captured bonus line into `PayrollFinalLines` without rereading current events. Voided events are excluded at capture.
- Approved finalization preview reads the captured normalized bonus lines rather than current bonus-event state.
- Ledger receives bonus through final lines as before.
- **CP-3B1 (Done with Notes):** `GET /payroll/periods/{id}/bonuses/summary` adds a zero-inclusive, backend-owned bonus summary. The driver roster comes strictly from the CP-2E `PayrollPeriodDriverEligibility` snapshot (gated by the `PayrollPeriodEligibilitySnapshots` marker) — every snapshot-eligible driver appears even with zero bonus events, and `PayrollBonusEvents` are aggregated onto that roster, never used to expand it. Periods without a snapshot marker return a controlled `422` (`BONUS_SUMMARY_UNAVAILABLE_NO_ELIGIBILITY_SNAPSHOT`); there is no live-roster fallback. Event aggregation is scoped by `PayrollPeriodID`, `CompanyID`, and `BranchID` together, so a same-company cross-branch row cannot contaminate a driver's total. Capabilities (`can_create`/`can_update`/`can_void`) are computed per driver, not period-wide: `can_create` is false for an `IncludedByExistingData` driver who would be rejected by `POST /bonuses` (no existing period-pay source), and `can_update`/`can_void` require at least one Active event. The existing `GET /bonuses` event-list endpoint is unchanged.
- **CP-3B2a (Done with Notes):** the safety foundation for the transactional bonus batch. `payroll.PayrollPeriods.BonusDataRevision` (`BIGINT NOT NULL DEFAULT 0`) is a period-level bonus-mutation concurrency token: every successful single-event `POST`/`PATCH`/`DELETE` on `/bonuses` increments it exactly once, in the same transaction as the event write. Idempotent void (an already-Voided event) does not bump it again. `PATCH /bonuses/{id}` enforces its optimistic-concurrency check via an atomic SQL predicate (`UPDATE ... WHERE ... AND DataRevision = :expected RETURNING ...`) rather than a read-then-write check with no predicate on the write itself — a stale `data_revision` returns `409` with no event or period-revision side effects. `GET /bonuses/summary` exposes `bonus_data_revision`, sourced directly from `PayrollPeriods.BonusDataRevision` (never `MAX(PayrollBonusEvents.DataRevision)`). A DB-level ownership trigger on `PayrollBonusEvents` rejects any insert/update whose `CompanyID`/`BranchID` doesn't match the owning period (closing the CP-3A P2 gap). `_write_line_audit` gained an optional `correlation_id` parameter.
- **CP-3B2b (Done with Notes):** `POST /payroll/periods/{id}/bonuses/batch` — the create-only transactional bonus batch endpoint now exists. Migration 0060 hardened `payroll.PayrollBonusBatchRequests` (ownership trigger + integrity constraints, `CreatedByUserID NOT NULL`) before this endpoint became its first writer. The batch is all-or-nothing (one request transaction; any failure rolls back every event, the batch-request row, the revision bump, and every audit row), uses `PayrollBonusEvents` only (never `PayrollDraftLines`, never generic Period Pay BONUS rows), requires `expected_bonus_data_revision` and increments `PayrollPeriods.BonusDataRevision` exactly once per successful batch (not per event), and is idempotent: an exact replay (same `idempotency_key` + same canonical request hash) returns the stored result read-only with **HTTP 200** and no new writes/revision bump, while a different payload or a different `expected_bonus_data_revision` under the same key returns **409**. A fresh apply returns **HTTP 201**. Every created event carries the shared `BatchCorrelationID`; the batch's `IdempotencyKey` is also stored on each event (non-unique there — the authoritative unique idempotency record is the `PayrollBonusBatchRequests` row). One `BONUS_BATCH_APPLIED` audit row and one `BONUS_EVENT_ADDED` row per created event share that same correlation id. New batch applies are Open/Returned only; Draft/InReview/Approved/Locked/Archived/Cancelled are rejected the same way single-event create is. **Update-batch, void-batch, desired-total reconciliation, zero-means-clear, and negative bonus remain out of scope/not implemented** — the batch is create-only; the existing `PATCH`/`DELETE /bonuses/{id}` endpoints remain the only update/void paths.
- **CP-3C (Done with Notes):** the min/max formula bug is corrected in both `finalize_period` and `get_finalization_preview`. Current, enforced behavior: `normal pay → apply minimum/maximum → add bonus`. Concretely: `normal_base = normal_daily_pay + non_bonus_normal_period_pay`; `minimum_adjustment = max(minimum - normal_base, 0)`; `after_minimum = normal_base + minimum_adjustment`; `maximum_adjustment = min(maximum - after_minimum, 0)`; `normal_after_minmax = after_minimum + maximum_adjustment`; `total_bonus = sum(Active canonical PayrollBonusEvents)`; `total_pay = normal_after_minmax + total_bonus`. Bonus is excluded from the comparison base in both the persisted finalization path and the read-only preview path — it never reduces a minimum top-up and never triggers or enlarges a maximum cap; it is added only after min/max is applied. A bonus-only driver (no normal pay lines at all) still receives correct min/max processing with `normal_base = 0` — the finalization SQL uses a `CASE`-based conditional sum rather than a `WHERE` row filter specifically so this driver isn't silently dropped from the aggregation. `STATUS_PAYMENT` and non-BONUS `ADJUSTMENT` lines are unchanged — both remain part of the normal min/max base, as before. Legacy BONUS DraftLines remain excluded from finalization entirely (unchanged from CP-3A). `GET /payroll/periods/{id}/finalization-preview` now separates `daily_pay`/`period_pay` (normal only), `gross_pay` (normal only), `bonus_total` (new field), `sys_adjustment`, and `final_pay` (the true total, `gross_pay + sys_adjustment + bonus_total`); `FinalizationPreviewSysAdjustment` gained the same `bonus_total` field so its own `final_pay` also reports the driver's true total rather than a bonus-free intermediate. Preview, finalization, and the finalized ledger all agree under this formula — covered by focused parity tests. No migration, no frontend, no bonus CRUD/batch behavior change, no Phase 4 calculation-core work. The zero-inclusive summary's `total_bonus`/`active_bonus_total` (CP-3B1) remain pure bonus aggregates, now consistent with the corrected finalization/preview formula above.

### 3.11 Review today

- Open→InReview / Returned→InReview refreshes and validates live calculation inputs, captures an immutable `PayrollCalculationSnapshot` revision, creates a Pending PeriodApproval review item linked to that snapshot, and updates the period in one request transaction.
- InReview source data is read-only. Open is the normal initial current-payroll entry/calculation state; Returned is a previously submitted period that remains editable for correction and uses the live CP-4B preview.
- `SubmittedAtUtc` is populated on successful Submit/Resubmit.
- CP-4D captures the submitted immutable calculation snapshot; CP-4E review financial display reads that ReviewItem-linked packet, and approval refers to that exact linked snapshot identity rather than live payroll financial data.
- Current review and approved financial authority is `ManagerReviewItems.PayrollCalculationSnapshotID` -> immutable submitted financial packet. Open/Returned remain live operational calculation states; CP-4F finalization preview and FinalLine projection consume the exact Approved ReviewItem-linked packet.
- Rejected/EditRequested resolves the historical ReviewItem and moves the period to Returned, preserving its existing immutable snapshot. Corrections remain live in Returned; Resubmit captures a new snapshot revision, creates a new Pending PeriodApproval, and returns the period to InReview.
- Manual InReview→Open/Cancelled is blocked. InReview exits only through the PeriodApproval review decision: Approved -> Approved; Rejected/EditRequested -> Returned.
- Approved has no direct PATCH transition back to InReview; CP-4F finalization consumes its exact approved snapshot before moving the period to Locked.

### 3.12 Transition concurrency today

Only selected operations use expected-status predicates. Most generic status changes update by period ID without including the previously-read status. The source comment claiming `get_period_by_id` holds `FOR UPDATE` is incorrect. A stale concurrent transition can therefore bypass the application transition graph.

Draft cancellation now requires the transition-specific `payroll.finalize` permission (CP-0C); the remaining concurrency concern is separate from that permission correction.

### 3.13 Finalization and ledger today

Current CP-4F finalization foundation:

- Approved-only finalization through exactly one approved snapshot-linked PeriodApproval ReviewItem;
- transaction-level branch/workflow and PayrollPeriod locking;
- structural reconciliation of persisted snapshot header, DriverTotals, SnapshotLines, and tenant/review/period identity;
- atomic SnapshotLine-to-FinalLine projection, audit, and Approved→Locked transition;
- immutable final lines and immutable Locked/Archived status rules;
- no live rates, DraftLines, Status, BonusEvents, NeedsManagerReview, or min/max calculation as financial authority.
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

Do not model selected statuses as Pay Items or free-text pseudo-lines. Introduce normalized daily status storage with StatusKeyID plus immutable snapshots. `PTO_STATUS` has been removed as a grid Pay Item (commit `236a506`); it must not be reintroduced. Historical records referencing `PTO_STATUS` are preserved. Current status payment uses `StatusRateColumns` and derived system payment lines.

Future `StatusKeyPayRule` records may create derived payroll calculation components. Allowance use and financial behavior remain separate mappings.

### AD-7 — Bonuses are multiple auditable events

Use multiple bonus events per driver/period and aggregate them into `TotalBonus`. This preserves who gave each bonus, when, and why. One total row would lose required event-level detail.

**Implemented by CP-3A.** The canonical bonus-event domain is now live:

- `PayrollRunBonuses` was converted and renamed to `payroll.PayrollBonusEvents` by migration 0058.
- Legacy BONUS `PayrollDraftLines` were backfilled into `PayrollBonusEvents` with `SourceDraftLineID` trace linkage.
- Generic Period Pay (`/period-pay`) no longer accepts BONUS creation; it is blocked at the route level with a 422 redirect to the canonical endpoints.
- There is no dual-write path; `PayrollBonusEvents` is the single source of truth for all bonus data.

Bonus is invariantly excluded from minimum/maximum comparison by domain rule. CP-3C now enforces this rule in the current finalization and approved finalization-preview paths: normal pay is adjusted by minimum/maximum rules first, then canonical active bonus events are added to total pay. Canonical active `PayrollBonusEvents` remain included in final payout and the final ledger throughout. Phase 4 still owns the unified calculation-core and immutable submitted-snapshot architecture.

### AD-8 — One calculation core, immutable submitted snapshot

One backend calculation core produces:

- Open/Returned expected-income preview;
- submission snapshot;
- review views;
- approval totals;
- finalization inputs;
- final report/ledger values.

Open/Returned previews are recomputed read-only for current source/config versions. Submit persists an immutable snapshot. InReview and Approved read that exact snapshot. CP-4F finalization preview and FinalLines consume the approved snapshot without re-resolving newer rates.

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

- ~~No one-InReview unique constraint.~~ Resolved by CP-1B: the one-InReview-per-branch partial unique index is the concurrency authority.
- ~~No Returned state.~~ Resolved by CP-1A: Returned is a first-class correction state with its own review-item pointer and slot protection.
- ~~No smart slot-aware create command.~~ Resolved by CP-1C: branch-locked candidate-based creation applies the Draft/Open/InReview/Returned slot matrix.
- ~~No atomic submit-and-promote operation.~~ Resolved by CP-1D: Open submission atomically promotes the adjacent Draft/Prepared period when applicable.
- ~~Prepared-alone invariant is not enforced.~~ Resolved by CP-1C: a Draft/Prepared without its required Open context is rejected or repaired by the candidate workflow.
- ~~Current create always returns Draft, including first payroll.~~ Resolved by CP-1C: the first valid period is Open; later creation follows the slot matrix.
- ~~Generic transition endpoint exposes unsafe paths.~~ Resolved by CP-0C/CP-1A: PATCH has only narrow permitted transitions; review decisions, Resubmit, and finalization own their respective lifecycle exits.
- Transition writes are not uniformly expected-state safe.

### 6.2 Source data and calendar

- ~~Prepared cannot receive controlled pre-entry.~~ Resolved by CP-2F: Draft/Prepared is now an operational source-entry workspace with financial exposure suppressed.
- ~~Derived calculation values are mixed into operational draft responses.~~ Resolved by CP-2F: Draft responses return `calculated_amount = null`, `rate_amount = null`, `gross_total = null`, `financials_available = false`.
- ~~No period-day snapshot.~~ Resolved by CP-2B: `PayrollPeriodDays` provides snapshotted period-day calendar. Remaining future work: Add Day activation / calendar extension controls.
- Off-day mask does not yet govern grid navigation (pending Add Day activation workflow).
- ~~Pay-item ordering and labels are not snapshotted.~~ Resolved by CP-2C: `PayrollPeriodPayItems` provides stable snapshotted pay-item layout, labels, and sort order per period.
- ~~Historical driver rows can disappear after current status changes.~~ Resolved by CP-2E: canonical eligibility snapshot controls marked periods; live `EmploymentStatus`/`DriverStatus` no longer hides historically eligible drivers for those periods.

### 6.3 Status

- ~~Mutable text code storage.~~ Resolved by CP-2D1: selected status is now stored in `PayrollPeriodDriverDayEntryState` with `StatusKeyID` (FK, ON DELETE RESTRICT). Code-text-only storage is the pre-CP-2D1 historical baseline; it no longer applies to new writes.
- ~~No StatusKeyID snapshot.~~ Resolved by CP-2D1: canonical rows carry `StatusKeyID`; deactivated keys referenced in existing rows are resolved via a deactivated-key map. Finalization snapshot fields (`StatusCodeSnapshot`, `StatusLabelSnapshot`, `StatusIsOffReasonSnapshot`) are populated at finalization time. Remaining future work: immutable per-period StatusKey availability snapshot (`PayrollPeriodStatusKeys`) is deferred.
- ~~No historical label/off flag preservation.~~ Resolved by CP-2D1: finalization freezes `StatusCodeSnapshot`, `StatusLabelSnapshot`, `StatusIsOffReasonSnapshot`, `StatusHoursValueSnapshot` on the canonical row. Pre-finalization editable periods resolve label/is_off from live StatusKey map with deactivated-key fallback.
- Historically, status was modeled partly as pseudo DraftLines and partly as a `PTO_STATUS` Pay Item. `PTO_STATUS` was removed (commit `236a506`). CP-2D1/CP-2D2 moved the design to canonical `PayrollPeriodDriverDayEntryState` rows and `StatusRateColumns`-backed derived payment lines. Legacy `DailyStatus`/`DailyNote` DraftLine dual-write is preserved for finalization compatibility only.
- No future pay-rule mapping.
- Status usage limits are read-then-write and are not concurrency-safe.

### 6.4 Calculation

- ~~No Open/Returned expected-income contract.~~ Resolved by CP-4B: Open/Returned now have a backend-owned, read-only live calculation preview.
- ~~No calculation input/version contract.~~ Resolved for submitted packets by CP-4C/CP-4D: `current-payroll-v1`, `SourceConfigHash`, `SnapshotHash`, normalized immutable outputs, and source evidence are captured at Submit/Resubmit.
- ~~No submitted calculation snapshot.~~ Resolved by CP-4D: successful Submit/Resubmit atomically captures an immutable revision and links the new Pending PeriodApproval review item to it.
- ~~Review and finalization can observe different rate/config inputs.~~ Resolved by CP-4F: Approved preview and finalization consume the exact Approved ReviewItem-linked snapshot.
- ~~Bonus incorrectly participates in min/max.~~ Resolved by CP-3C (`90a6dbe`): current finalization and approved finalization preview exclude canonical bonus events from the min/max base and add them afterward (`normal pay -> minimum/maximum -> add bonus`). CP-4D captures the submitted calculation packet, CP-4E binds review/approval to it, and CP-4F projects that captured result without live recomputation.
- Current preview response cannot support required dynamic report views.
- Current day grid exposes financial calculated amounts directly.

### 6.5 Bonus (updated: CP-3C)

- ~~Generic Period Pay contract only.~~ Resolved by CP-3A: canonical `PayrollBonusEvents` table with dedicated CRUD endpoints. Generic Period Pay path blocks BONUS creation.
- ~~Competing unused bonus table.~~ Resolved by CP-3A: `PayrollRunBonuses` converted/renamed to `PayrollBonusEvents`; legacy BONUS DraftLines backfilled and hidden/blocked from period-pay paths.
- ~~Incomplete actor/update/void metadata.~~ Resolved by CP-3A: full actor/time/reason/notes/update/void metadata on `PayrollBonusEvents`.
- ~~No zero-inclusive all-driver summary.~~ Resolved by CP-3B1: `GET /bonuses/summary` returns every CP-2E snapshot-eligible driver, including zero-bonus drivers, with per-driver capabilities.
- ~~No DB-level enforcement that a bonus event's Company/Branch matches its owning period.~~ Resolved by CP-3B2a: a DB trigger on `PayrollBonusEvents` rejects mismatched inserts/updates.
- ~~No period-level bonus concurrency token; no durable batch idempotency/correlation storage.~~ Resolved by CP-3B2a: `PayrollPeriods.BonusDataRevision` plus the `PayrollBonusBatchRequests` table (unique idempotency-key index, unique `BatchCorrelationID`) are in place; CP-3B2b then gave that table its writer.
- ~~No batch endpoint, create-only transactional batch writes, or idempotent replay behavior.~~ Resolved by CP-3B2b: `POST /bonuses/batch` exists, is all-or-nothing, and supports exact idempotent replay. Update-batch, void-batch, and desired-total reconciliation remain out of scope/not implemented.
- ~~No explicit min/max exclusion invariant enforced in the formula; bonus entered the current min/max base.~~ Resolved by CP-3C: both `finalize_period` and `get_finalization_preview` now exclude bonus from the min/max comparison base and add it back in only after min/max — `normal pay → min/max → add bonus`.

### 6.6 Review and audit

- ~~InReview editable.~~ Resolved by CP-0A: source entry is limited to Open/Returned (with Draft operational pre-entry); InReview is read-only.
- ~~`SubmittedAtUtc` unused.~~ Resolved by CP-1D: successful Submit/Resubmit writes it atomically; failed submissions leave it unchanged.
- ~~No submitted revision/snapshot ID.~~ Resolved by CP-4D/CP-4E: `ManagerReviewItems.PayrollCalculationSnapshotID` identifies the exact submitted packet and the financial packet an Approved PeriodApproval decision refers to.
- ~~Return can conflict with a newer Open period.~~ Resolved by CP-1A: Returned is distinct from Open, does not consume the Open slot, and has explicit Returned-slot/backlog rules.
- ~~Manual returns can orphan review items.~~ Resolved by CP-0C/CP-1A: direct unsafe exits are blocked and the review decision atomically resolves the item while updating the period and return pointer.
- ~~Approval is not tied to snapshot identity.~~ Resolved by CP-4E: snapshot-backed approval validates and approves the exact ReviewItem-linked immutable packet rather than current live financial data.
- Audit updates often omit old values.
- Period creation is not audited.
- Generic audit table is not database append-only.

### 6.7 Hub and reports

- ~~No trusted Current Payroll aggregate.~~ Resolved by CP-5A: `GET /payroll/current` is the company-scoped Hub aggregate with optional branch filtering, Open/Prepared/InReview/Returned slots, reused CP-1E alerts/capabilities, eligible/Working Drivers metrics, and Open/Returned CP-4B financial summaries. `/payroll/current-workflow` remains compatible.
- ~~Existing period list pagination/filtering is not a hub contract.~~ Resolved for the Hub by CP-5A's dedicated current-workflow aggregate; reports and later Hub extensions remain separate contracts.
- ~~Existing off count is driver-day rows, not fully-off drivers.~~ Resolved by CP-5B: the distinct fully-off-driver KPI and selected-day off-driver contract are backend-owned; the legacy driver-day count remains compatibility-only.
- ~~No lifecycle financial-authority foundation for future reports.~~ Resolved by RP-1: future report services can resolve `SOURCE_ONLY`, `LIVE`, exact ReviewItem-linked submitted/approved snapshots, or `FINAL_LINES` without choosing an authority client-side or selecting a latest snapshot.
- ~~No Drivers Report, Period Work, Period Pay, or Mixed Summary backend contract.~~ Resolved by CP-5C: all four backend-owned calculation report contracts are implemented.
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
- `ScheduleVersionID BIGINT NULL`
- `PeriodConfigurationVersion/Hash`
- `UpdatedAtUtc`
- `LastStatusReason`
- optional `ReturnedAtUtc`, `ReturnedByUserID`

`PayrollPeriods.ApprovedCalculationSnapshotID` is not recommended and does not exist. The Approved PeriodApproval `ManagerReviewItems` row and its non-null `PayrollCalculationSnapshotID` are the durable approved financial identity. CP-4F locates that Approved PeriodApproval row for the PayrollPeriod and uses its linked immutable snapshot as finalization authority; no migration `0063`, period-level duplicate pointer, or historical backfill is required.

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

### 7.5 Normalized daily status (updated: canonical entry-state model)

The canonical per-driver/per-day Status selection already lives in `PayrollPeriodDriverDayEntryState`. Phase 4 must extend or reference that existing entry-state model rather than introduce `PayrollDailyStatuses` or another parallel Status source. Compatibility `DailyStatus` and derived payment lines remain projections; they must not become competing source-of-truth records.

- One driver/day has one canonical selected `StatusKeyID` (in `PayrollPeriodDriverDayEntryState`).
- Phase 4 calculation resolution reads that canonical entry-state fact.
- Compatibility `DailyStatus` DraftLines remain projections/outputs only.
- Derived `STATUS_PAYMENT` lines remain financial compatibility projections, not source truth.
- Future snapshot work may freeze the resolved status-related values by value (see "Phase 4 Status payment boundary").
- No second status persistence model is introduced.

Phase 4 may snapshot payroll-payment inputs and resolved payment results. Allowance-category linkage, entitlement deduction, allowance rules, and usage ledger belong to the future DAC domain; Phase 4 must not store allowance-ledger authority inside its payroll calculation snapshot model. The future DAC domain may independently reference the same canonical StatusKey selection.

Daily notes should be a typed operational record or typed field, not a fake Pay Item line.

Future `StatusKeyPayRules` should be effective-dated and independently map a Status Key to financial behavior. Allowance-category mapping remains separate, through future `StatusKeyAllowanceRules` (future DAC architecture, not implemented by Phase 4).

### 7.6 Bonus events (updated: CP-3C)

`payroll.PayrollBonusEvents` exists after migration 0058 (CP-3A). The old unused `PayrollRunBonuses` table was converted into this canonical domain:

- BonusEventID;
- company/branch/period/driver;
- positive amount (DB-enforced `Amount > 0`);
- optional reason/notes;
- created/updated/voided actors and timestamps;
- state (`Active` / `Voided`);
- `SourceDraftLineID` trace FK (links migrated rows to their legacy DraftLine origin; NULL for events created through the API after cutover);
- batch correlation ID — now populated by CP-3B2b for every event created through `POST /bonuses/batch` (still NULL for single-event-created rows);
- idempotency key — now also populated on batch-created events (non-unique there; the authoritative unique idempotency record lives on the `PayrollBonusBatchRequests` row);
- **event-level** data revision (per-event optimistic-concurrency token, used by `PATCH /bonuses/{id}`'s atomic update predicate).

Multiple events are allowed. Zero means no event; voiding is explicit. Bonus is invariantly excluded from min/max by the required product rule; **CP-3C now enforces this in the calculation formula itself** (previously the rule existed at the schema/domain level — e.g. CP-3A's removal of `IncludeInMinimumPayComparison` — but the finalization/preview arithmetic still summed bonus into the comparison base until CP-3C corrected it).

CP-3B1 added a read-only aggregate view over this table: `GET /bonuses/summary` groups `PayrollBonusEvents` by driver onto the CP-2E `PayrollPeriodDriverEligibility` snapshot roster (never the reverse — events cannot expand the roster), scoped by period, company, and branch together. This did not add a new bonus data source, a new mutation path, or any batch/idempotency/revision machinery.

**CP-3B2a** added the safety foundation the batch depends on, migration 0059:

- `payroll.PayrollPeriods.BonusDataRevision BIGINT NOT NULL DEFAULT 0` — a **period-level** bonus-mutation concurrency token, distinct from the event-level `DataRevision` above. Every successful single-event bonus create/update/void increments it exactly once, in the same transaction as the event write; it is never derived from `MAX(PayrollBonusEvents.DataRevision)`. `GET /bonuses/summary` exposes it as `bonus_data_revision`.
- `payroll.PayrollBonusBatchRequests` — created foundation-only in CP-3B2a (no writer at that point) with a unique `(CompanyID, BranchID, PayrollPeriodID, IdempotencyKey)` index and a unique `BatchCorrelationID`.
- A DB-level ownership trigger on `PayrollBonusEvents` rejecting any insert/update whose `CompanyID`/`BranchID` doesn't match the owning period — closes the CP-3A P2 gap that was previously only mitigated at the read/aggregation layer (CP-3B1's branch-scoped summary query).
- `_write_line_audit` gained an optional `correlation_id` parameter (backward compatible).

**CP-3B2b** made `PayrollBonusBatchRequests` a live table via migration 0060 and the new endpoint:

- Migration 0060 hardened `PayrollBonusBatchRequests` **before** it got a writer: `CreatedByUserID NOT NULL`; check constraints for non-negative revisions, SHA-256-hex `RequestHash`, JSON-object `RequestPayloadJSON`, JSON-array `CreatedEventIDs` with `CreatedEventCount` matching its length; and a `BEFORE INSERT OR UPDATE` ownership trigger (mirrors the `PayrollBonusEvents` one) enforcing Company/Branch consistency with the owning period.
- `POST /payroll/periods/{id}/bonuses/batch` is that table's first and only writer. It stores the canonical request hash, the full request payload, the shared `BatchCorrelationID`, both `Expected`/`ResultBonusDataRevision`, and the created event IDs/count for exact idempotent replay.
- Created `PayrollBonusEvents` rows now populate `BatchCorrelationID` and `IdempotencyKey` (see above).
- `_write_line_audit`'s `correlation_id` parameter is now used: every event's `BONUS_EVENT_ADDED` row and the one `BONUS_BATCH_APPLIED` row per batch share the batch's `BatchCorrelationID`.

**CP-3C** corrected the min/max formula in both `finalize_period` and `get_finalization_preview`: bonus is now excluded from the `normal_base` comparison used against `MinimumPay`/`MaximumPay` rules, and is added back into `total_pay` only after any minimum top-up or maximum cap is applied. A bonus-only driver still receives correct min/max processing (`normal_base = 0`) because finalization's earned-base aggregation uses a `CASE`-based conditional sum, not a `WHERE` row filter — a filter would have silently dropped that driver from the aggregation entirely. `STATUS_PAYMENT` and non-BONUS `ADJUSTMENT` lines are unchanged and remain part of the normal base. This did not touch the canonical bonus data source, mutation paths, or batch/idempotency/revision machinery in any way.

Do not create a third active bonus representation. Update-batch, void-batch, and desired-total reconciliation are explicitly out of scope — not planned as part of CP-3B2b, CP-3C, or any currently-scheduled unit.

### 7.7 Calculation snapshots

Implemented normalized snapshot model:

- `PayrollCalculationSnapshots`: immutable header for one period revision with aggregate calculation version, source/config hash, snapshot hash, actor/time metadata, and total expected pay.
- `PayrollCalculationDriverTotals`: immutable per-driver totals with composite tenant/branch integrity to both snapshot and driver.
- `PayrollCalculationSnapshotLines`: immutable normalized financial lines with source identity, quantity, resolved rate, amount, and `SourceEvidenceJSONB`; ownership is inherited through DriverTotal.
- CP-4D captures the header, totals, lines, hashes, and a linked Pending PeriodApproval atomically on Submit/Resubmit. No `SourceDataRevision`, PayrollPeriods snapshot pointer, or historical backfill was introduced.
- CP-4E uses the linked snapshot as the immutable review/approval financial authority. It added no migration: Alembic head remains `0062` and no duplicate period-level approved-snapshot pointer exists.

Open/Returned previews remain live and read-only. CP-4E binds snapshot-backed review/approval to the exact submitted snapshot; CP-4F finalization consumes that approved snapshot.

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

**Implemented by CP-5A** (`5d05756` — `feat: add current payroll hub`). The current Hub is company-scoped with optional `branch_id`, honors current-payroll read/branch access, preserves company isolation and driver/`OwnDriverDataOnly` denial, and does not require `reports.view`.

Current response responsibilities:

- caller scope and accessible branches;
- Open, Prepared, InReview, and Returned period slots;
- returned-period alerts/actions;
- period dates/name/status/display status;
- total eligible drivers;
- working drivers;
- Open/Returned compact CP-4B financial summaries: expected normal pay, bonus, adjustments, total expected income, calculation blockers/warnings, and top expected-income drivers;
- Prepared source/workflow-only behavior with no financial authority; InReview does not use live calculation;
- action capabilities and read-only reason codes;
- no frontend aggregation across payroll endpoints; `/payroll/current-workflow` remains compatible.

Working Drivers is a Hub metric, not a Daily Grid roster rule: it counts distinct period-eligible drivers with at least one genuine normal daily operational work/activity source (for example Hours, Miles, Loads, Pallets, or another normal Daily PayItem value) inside the period and on an eligible driver date. Status alone, Bonus, system or period-level financial adjustments, and eligibility with no work data do not qualify. CP-2E periods use the canonical snapshotted driver-date eligibility window; legacy periods use established legacy eligibility semantics. Eligible drivers may still appear in the Daily Grid without work data.

The separate fully-off-driver KPI and selected-day off-driver detail are implemented by CP-5B. Drivers Report, Period Work, Period Pay, and Mixed Summary are implemented by the completed CP-5C Calculation Report Bundle.

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

The client acts on a ReviewItem. For snapshot-backed PeriodApproval, the server resolves and validates that ReviewItem's linked snapshot/revision and records the linked identity in audit metadata; the decision request does not select an arbitrary snapshot.

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

**Implemented by CP-5B** (`9ecb300` — `feat: add off-driver payroll contracts`). The shared resolver uses canonical Status, the snapshotted period calendar, scheduled work-day denominator, and CP-2E or legacy driver-date eligibility. It excludes unactivated configured off days, respects already-activated added days, protects zero denominators, and counts distinct drivers only when every eligible scheduled work day is Off. Normal work disqualifies Fully-Off; Status-payment-only, Bonus-only, and system/min/max or period-financial-only values do not. The contracts add no financial calculation, frontend, migration, Add Day mutation, or report implementation.

### 8.6 Expected income and calculation preview

```text
GET /payroll/periods/{id}/calculation-preview
```

Implemented by CP-4B for Open and Returned as a backend-owned, live read-only expected-income breakdown. InReview/Approved do not use this live calculation-preview contract; CP-4E review reads the linked submitted snapshot instead. Draft returns no financial preview. Locked/Archived redirect conceptually to final snapshot/report contracts.

### 8.7 Bonus (updated: CP-3C)

Implemented by CP-3A:

```text
GET    /payroll/periods/{id}/bonuses                           — list active + voided events
POST   /payroll/periods/{id}/bonuses                           — create canonical bonus event
PATCH  /payroll/periods/{id}/bonuses/{bonus_event_id}          — update amount/reason/notes
DELETE /payroll/periods/{id}/bonuses/{bonus_event_id}          — void (idempotent)
```

Implemented by CP-3B1:

```text
GET    /payroll/periods/{id}/bonuses/summary                   — zero-inclusive driver summary
```

`GET /bonuses/summary` returns every CP-2E snapshot-eligible driver (including zero-bonus drivers), each with `total_bonus` (Active events only), `active_event_count`, `voided_event_count`, the full `events` list (Active + Voided, for audit visibility), and per-driver `capabilities` (`can_create`/`can_update`/`can_void` + `reason_codes`). Sorting is nonzero-total-first descending, then `driver_name`/`driver_code`/`driver_id`. Draft periods and periods with no CP-2E eligibility snapshot both reject with a controlled `422` — there is no live-roster fallback. The plain `GET /bonuses` event-list contract above is unchanged by this addition. Since CP-3B2a, the response also includes a top-level `bonus_data_revision` (from `PayrollPeriods.BonusDataRevision`, not an event aggregate) — the concurrency token batch writes read-then-expect.

CP-3B2a made no contract changes to the four CP-3A endpoints above beyond internal behavior: `POST`/`PATCH`/`DELETE` each also increment `PayrollPeriods.BonusDataRevision` by one on success (idempotent void does not double-increment), and `PATCH`'s existing `data_revision` optimistic-concurrency check is enforced by an atomic SQL predicate rather than a read-then-write check — the response shape and status codes for these four endpoints are unchanged.

**Implemented by CP-3B2b:**

```text
POST   /payroll/periods/{id}/bonuses/batch                     — create-only, all-or-nothing batch
```

`POST /bonuses/batch` is create-only and all-or-nothing: every item is validated (positive amount, CP-2E eligibility) before any event is inserted, and the entire request runs in one transaction, so any failure rolls back every event, the batch-request row, the revision bump, and every audit row. The request requires `expected_bonus_data_revision` (validated against `PayrollPeriods.BonusDataRevision` via the same predicated helper CP-3B2a introduced) and an `idempotency_key`. On success it increments `BonusDataRevision` exactly once for the whole batch (never per event) and returns **HTTP 201** with the created events, a server-generated `BatchCorrelationID`, and the result revision. An exact replay — same `idempotency_key` and the same canonical request hash (SHA-256 over sorted, compact JSON; item order and `expected_bonus_data_revision` are both part of the hash) — returns the durably-stored result read-only as **HTTP 200**, with no new writes and no revision bump; it succeeds even if the period has since become non-editable, because replay never re-checks lifecycle status. The same `idempotency_key` with a different payload or a different `expected_bonus_data_revision` returns **409**, as does a stale `expected_bonus_data_revision` against a fresh apply. Every created `PayrollBonusEvents` row stores the shared `BatchCorrelationID` and the batch's `IdempotencyKey` (events do not store the batch-request row itself). The one `PayrollBonusBatchRequests` row is the durable record of the batch as a whole: it stores the request hash, the full request payload, the expected/result `BonusDataRevision`, the created event IDs/count, and the `BatchCorrelationID`. Per-event `BONUS_EVENT_ADDED` audit rows and one `BONUS_BATCH_APPLIED` audit row all share that same correlation id. New batch applies are Open/Returned only, matching single-event create's lifecycle gate exactly.

Update-batch, void-batch, and desired-total reconciliation are **not implemented and out of scope** for CP-3B2b — the existing `PATCH`/`DELETE /bonuses/{id}` endpoints remain the only update/void paths.

**CP-3C (calculation-path fix, not a new bonus contract):** `GET /payroll/periods/{id}/finalization-preview`'s `driver_totals[]` now reports `gross_pay` as normal pay only (bonus-free), a new `bonus_total` field, and `sys_adjustment` computed from that bonus-free base; `final_pay = gross_pay + sys_adjustment + bonus_total` is the driver's true total. `sys_adjustments[]` (`FinalizationPreviewSysAdjustment`) also gained `bonus_total`, and its own `final_pay` now reports the same true total rather than a bonus-free intermediate — the two lists agree, and both agree with the finalized ledger. No bonus endpoint, request/response contract, or status code changed as part of CP-3C — only the calculation these existing fields report.

### 8.8 Review

Submit response includes:

- period revision;
- submitted revision;
- calculation snapshot ID/version;
- submitted totals;
- review item ID;
- promoted Prepared/Open result.

For snapshot-backed PeriodApproval, `GET /review/items/{item_id}/payroll-snapshot` returns the immutable submitted financial packet linked to that ReviewItem for review display; it is not an assertion that the generic ReviewItem detail endpoint embeds the full packet. Approval validates that exact linked snapshot rather than accepting an arbitrary client snapshot ID. Legacy Pending items without a snapshot fail approval closed with `SNAPSHOT_REQUIRED_FOR_APPROVAL` but may still be Returned. Return/reject requires a reason and returns the period to `Returned`.

### 8.9 Reports

```text
GET /payroll/periods/{id}/reports/drivers
GET /payroll/periods/{id}/reports/period-work
GET /payroll/periods/{id}/reports/period-pay
GET /payroll/periods/{id}/reports/mixed
```

Open/Returned use live backend calculation. InReview uses the exact submitted ReviewItem-linked snapshot; Approved uses the exact approved ReviewItem-linked snapshot. Locked/Archived use FinalLines.

**Implementation status:** CP-5C implements these backend-owned calculation/report read models in commit `23bf68f` (`feat: add current payroll calculation reports`). It reuses RP-1 (`187d552e00ff26f2bd55015c8072a861e93cca7e` — `feat: add payroll report authority resolver`) rather than owning a second lifecycle authority resolver. Frontend clients must never choose live vs. snapshot vs. FinalLines authority. The report bundle is not a Phase 6 finalized-information-library route.

The canonical response shape should be driver-centric:

- report metadata and dynamic column definitions;
- `drivers[]`;
- each driver contains daily rows, work totals, pay totals, status summaries, bonus events, min/max adjustments, and final totals.

This shape supports either a two-row Work/Pay visual or driver sections without making the backend encode UI layout such as merged cells.

**CP-5C frozen-report evidence remedy (implemented):** New Submit/Resubmit calculation snapshots preserve immutable Status evidence sufficient for historical status reporting, and immutable Bonus evidence sufficient for Drivers Report detail: amount, giver/creator identity, event timestamp, reason, and notes where applicable. Status remains a separate source channel, not a PayItem; Bonus remains the canonical Bonus-event domain, distinct from generic manual financial rows. Future frozen report states must not rebuild those historical fields from mutable current driver-day Status or BonusEvent rows.

**Implemented persistence architecture: dedicated immutable report-evidence tables.** CP-5C now uses `PayrollCalculationSnapshotStatusEntries` and `PayrollCalculationSnapshotBonusEvents` attached to each calculation snapshot. It does not use snapshot-level JSON arrays as the primary evidence model and does not overload financial `PayrollCalculationSnapshotLines` with non-financial Status evidence.

- Status evidence preserves snapshot, company, branch, period, driver, WorkDate, canonical entry-state identity, StatusKey provenance identity, frozen Status code/key, frozen display label, and frozen off classification. It includes complete canonical Status evidence required for reporting, including no-pay Status entries, while using existing CP-2E/date-valid source semantics rather than a second eligibility model. Status remains distinct from PayItems and financial lines; future-only Status fields are not part of this remedy.
- Bonus evidence preserves snapshot, company, branch, period, BonusEvent, and driver identities; amount, reason, notes, and currently relevant data revision; creator/giver user identity; creator/giver display identity snapshot; and `CreatedAtUtc` timestamp snapshot. Frozen reports must not depend on a live user join to reconstruct giver identity. Captured Bonus evidence uses the same canonical BonusEvent selection as the submitted financial packet and must reconcile with its authoritative Bonus snapshot lines/totals; no second Bonus-selection algorithm is introduced.
- Calculation snapshot headers contain `ReportEvidenceVersion` and `ReportEvidenceHash`. Newly captured evidence uses `ReportEvidenceVersion = 1`; these markers distinguish legacy snapshots for which report evidence was never captured from new snapshots with successful capture but zero Status and/or Bonus rows, version future evidence-schema evolution, and provide deterministic report-evidence integrity verification. `ReportEvidenceHash` is based on deterministically ordered canonical serialization: identical captured evidence produces the same hash and different evidence produces a different hash.

Existing `SnapshotHash` and `SourceConfigHash` retain their CP-4C/CP-4D financial semantics. Report-only evidence receives its own hash and does not redefine either financial hash contract. Existing snapshots receive no synthetic hash or evidence backfill: legacy missing Status/Bonus fields remain explicitly unavailable, distinguishable from legitimate null or zero values, and do not by themselves fail an entire report unless an existing integrity rule requires failure.

Evidence is captured atomically inside the same CP-4D `REPEATABLE READ` Submit/Resubmit snapshot transaction as the header, DriverTotals, SnapshotLines, ReviewItem linkage, and InReview transition. Each Resubmit creates an independent immutable evidence revision, and old revisions remain unchanged. The new evidence structures require equivalent no-UPDATE/no-DELETE immutable-trigger protection and repository-consistent Snapshot/Company/Branch/PayrollPeriod/Driver integrity; this is not Phase 7 tenant-hardening work.

Locked/Archived financial authority remains FinalLines. CP-5C Status/Bonus report evidence may remain snapshot-owned where CP-4F provenance deterministically identifies the originating calculation snapshot; it is not duplicated into FinalLines merely because a period locks. The remedy changes CP-4D capture. CP-4E approval and CP-4F finalization remain unchanged unless implementation discovery proves a necessary narrow adjustment. It does not implement any Phase 6 finalized-information-library route, rate library, full source library, audit/security library, participant/role history, or correction workflow.

The remedy shipped in migration `0063` with no historical backfill. Legacy snapshots retain unavailable report-evidence markers and must not be reconstructed from mutable Status, Bonus, or User data. The persistence blocker is closed.

**CP-5C frozen report-evidence remedy closure.** Implementation commit: `1a106b8e8adb0ca2e89cba1726a7cc8f0e43debd` — `feat: persist frozen payroll report evidence`. The remedy captures Status and Bonus evidence atomically within the existing CP-4D `REPEATABLE READ` Submit/Resubmit transaction for each exact snapshot revision. Returned → Resubmit creates independent evidence, while prior revisions remain immutable. The tables use the existing snapshot no-UPDATE/no-DELETE trigger pattern and enforce snapshot/company/branch/period/driver scope integrity.

The same active BonusEvent selection used by the submitted financial packet is reused and reconciled with snapshot Bonus lines/totals. Frozen Bonus evidence includes identity, driver, amount, reason, notes, revision, creator/giver identity, creator display-name snapshot, and `CreatedAtUtc`; Status evidence includes canonical driver-day provenance, code, label, and off classification, including no-pay Status. `SnapshotHash` and `SourceConfigHash` retain their financial semantics; `ReportEvidenceHash` is separate.

Independent review: `PASS_WITH_NOTES`; P0 none; P1 none; final recommendation `SAFE_TO_COMMIT_EVIDENCE_REMEDY`. Evidence: focused frozen report evidence 8 passed; CP-4B 72; CP-4D 27; CP-4E 12; CP-4F 8; RP-1 13; CP-2D2 45; CP-2E 75 with existing warnings; canonical Bonus 26; CP-5A 17; CP-5B 12; compile/import passed; Alembic head `0063`. This does not claim the full backend suite is green.

Non-blocking notes: CP-5A + CP-5B retain pre-existing same-process test isolation/order debt although each passes independently; `service.py` retains broad pre-existing Ruff debt while the focused CP-5C code/hash/migration checks pass; Status capture relies on canonical validated source-write invariants plus period/company/branch/date scope rather than duplicating the full CP-2E predicate; and the Windows `testing.postgresql` shutdown warning remains environment debt.

Prepared/Draft is source-only: Drivers Report exposes operational/source data, Period Work is available, Period Pay is explicitly unavailable, and Mixed exposes work with an unavailable financial section. Prepared reporting must not invoke live calculation or expose expected-pay money; unavailable financials are not zero. Cancelled periods expose no CP-5C reports and must not fall back to source data.

CP-5C report routes use `reports.view` and preserve company/branch scope, tenant isolation, driver-role denial, and `OwnDriverDataOnly` denial. `payroll.view` is not additionally required solely for report access. The lifecycle authority remains Draft/Prepared source-only; Open/Returned live; InReview exact submitted ReviewItem-linked snapshot; Approved exact approved ReviewItem-linked snapshot; Locked/Archived immutable final/frozen financial authority; Cancelled unavailable.

**CP-5C Calculation Report Bundle — Complete.** Commit `23bf68f` (`feat: add current payroll calculation reports`) changed `backend/app/payroll/report_read_model.py`, `backend/app/payroll/router.py`, `backend/app/payroll/schemas.py`, and `backend/tests/test_cp5c_reports.py` (`4 files changed, 1479 insertions(+), 1 deletion(-)`). It introduced no migration, frontend work, or Phase 6 work; Alembic remains `0063`. Independent review: `PASS_WITH_NOTES`; P0 none; P1 none; final recommendation `SAFE_TO_COMMIT_CP5C`.

The four public routes are `GET /payroll/periods/{period_id}/reports/drivers`, `GET /payroll/periods/{period_id}/reports/period-work`, `GET /payroll/periods/{period_id}/reports/period-pay`, and `GET /payroll/periods/{period_id}/reports/mixed`. They use a shared, batched report read model with no separate payroll engine or per-driver/day/status/bonus N+1 loops identified. Mixed reuses normalized Work and Pay semantics, so its totals reconcile with Period Work and Period Pay rather than following a third calculation path.

Open and Returned obtain financial authority from the shared CP-4B live packet; Returned reflects corrected current source, not prior submitted snapshot money. InReview resolves only the exact submitted ReviewItem-linked snapshot and Approved only the exact approved snapshot: neither selects a latest snapshot or recalculates live financial data. Locked and Archived use FinalLines as financial authority and locate report-only Status/Bonus history through the deterministically linked originating immutable calculation snapshot, without duplicating evidence into FinalLines or rebuilding history from mutable rows.

For `ReportEvidenceVersion = 1`, frozen Status/Bonus evidence is available even when it has legitimate zero rows. For legacy `ReportEvidenceVersion = NULL`, missing report-only evidence is explicitly unavailable, not zero or empty, and is never rebuilt from current Status, Bonus, or User data. Status remains a separate source channel, not a PayItem: live reports use canonical driver-day Status, frozen reports use immutable Status evidence, summaries are backend-owned, and no-pay Status entries remain reportable. Bonus remains canonical `PayrollBonusEvents` live; frozen detail comes from immutable snapshot evidence (event and driver identity, amount, creator/giver identity and frozen display identity, `CreatedAtUtc`, reason, notes, and revision where available), while financial Bonus totals remain authoritative financial-line values.

Dynamic report columns come from frozen `PayrollPeriodPayItems` metadata, preserving period order, label, and unit against later current PayItem rename or retirement. Period Work is operational aggregation only: it neither calculates pay nor treats Bonus or min/max as work. Its implementation resolves the period-snapshotted PayItem identity through `PayrollPeriodPayItems`, because `PayrollDraftLines` does not own `PayItemID`. Period Pay aggregates authoritative financial lines only; it does not resolve rates, multiply work by rates, replay min/max, recalculate Bonus or Status pay, or otherwise create report-local financial authority. Characterization covers an effective-dated example of `3 x $10 + 3 x $20 = $90`, rather than the incorrect `6 x $20 = $120`; reports return the authoritative `$90`. Minimum/maximum adjustments remain authoritative system lines and Bonus remains distinct.

Official totals and lifecycle/capability metadata are backend-owned. Frontend clients do not sum official financial totals, calculate Bonus/min/max, multiply work by rates, or choose lifecycle authority. LIVE CP-4B blockers/warnings propagate as report metadata; normal calculation blockers do not become unrelated authorization/not-found failures and do not cause reports to fabricate money. Access requires `reports.view`, not `payroll.view` solely for reports, and preserves company/branch scope, foreign-company denial, inaccessible-branch denial, driver-role denial, and `OwnDriverDataOnly` denial. The current public creation path has no supported non-BONUS manual period-level EnteredAmount fixture, so that endpoint case is N/A rather than fabricated through invalid SQL or a restored generic BONUS path.

Accepted validation evidence: CP-5C focused 24 passed; CP-5C + RP-1 + frozen evidence 45 passed; CP-4B 72 passed; CP-4D/CP-4E/CP-4F/RP-1 combined 60 passed; CP-2D2 + CP-2E + CP-3A Bonus 146 passed with 12 existing warnings; CP-5A 17 passed; CP-5B 12 passed; Branch access 11 passed; Permission catalog 15 passed; CP-2C 34 passed with 2 known stale failures; Ruff and compile/import passed. This does not claim the full backend suite is green.

Non-blocking CP-5C notes: `AppearsInReports` filters dynamic report columns, while row/totals payloads are not independently filtered for hidden-column semantics; Prepared source-only reports currently omit live Status evidence; and pre-existing CP-2C stale Alembic/BONUS expectations, Windows `testing.postgresql` shutdown warnings, CP-2E asyncio-marker warnings, and CP-5A/CP-5B same-process isolation debt remain outside CP-5C. These are not CP-5C P0/P1 blockers.

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

CP-4A established and tested an explicit numeric compatibility contract that preserves the current 4-decimal calculation behavior, including line precision, quantization boundaries, Decimal context, and rounding mode. Final payable-total currency rounding and reconciliation remain separate product decisions for later finalization work; CP-4A introduced no new 2-decimal payout rule.

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
- All eligible drivers appear in bonus summary even with zero total. **Implemented by CP-3B1** (`GET /bonuses/summary`), roster sourced strictly from the CP-2E eligibility snapshot.
- Drivers with nonzero totals sort first, then by backend-defined stable driver order/name. **Implemented by CP-3B1.**
- Batch operations are transactional, idempotent, revision-safe, and audit-correlated. **Implemented by CP-3B2a (foundation) + CP-3B2b (endpoint).** `POST /bonuses/batch` is create-only, all-or-nothing, keyed on `expected_bonus_data_revision` + `idempotency_key`, and every event plus the batch record share one `BatchCorrelationID`. `BonusDataRevision` is concurrency metadata only, not a financial value, and this batch does not change any bonus product rule. Update-batch, void-batch, and desired-total reconciliation remain unimplemented — out of scope, not merely deferred.
- Bonus is excluded from min/max by schema/domain invariant, not UI convention. **Implemented by CP-3C.** The calculation formula now enforces this in both `finalize_period` and `get_finalization_preview`: `normal pay → apply minimum/maximum → add bonus`. Bonus never reduces a minimum top-up and never triggers or enlarges a maximum cap.
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

- For snapshot-backed PeriodApproval, the reviewer sees exactly the submitted snapshot through the review-scoped snapshot read.
- Approval validates the Pending ReviewItem, InReview period, tenant/branch scope, and exact ReviewItem/snapshot/period relationship; it does not recalculate live financial inputs or rehash the snapshot.
- Legacy Pending PeriodApproval items with no snapshot fail approval closed with `SNAPSHOT_REQUIRED_FOR_APPROVAL`; Return/reject remains available so the period can be corrected and Resubmitted.
- Return/reject requires a reason, resolves the review item, preserves its historical snapshot link, and moves the period to Returned.
- Comments do not resolve the review item.
- Self-approval policy is explicit and defaults off for new companies.

### 13.3 Finalization

- Approved-only.
- Requires exactly one Approved PeriodApproval ReviewItem with a non-null linked snapshot matching the target period, company, and branch; missing, ambiguous, or corrupt identity fails closed.
- Uses the approved CalculationSnapshotID and does not resolve newer current rates/rules/configuration.
- Rechecks persisted snapshot structural integrity and duplicate-finalization state without recomputing its hash or financial inputs.
- Writes FinalLines, concise immutable provenance, and finalization audit metadata atomically.
- Moves Approved→Locked.
- Projects the submitted snapshot's existing captured calculation, rate, rule, bonus, Status, and configuration evidence; it creates no snapshot or revision.

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

1. ~~**InReview is editable.**~~ **Resolved by CP-0A.** Normal source entry is limited to Open/Returned (with controlled Draft pre-entry); InReview source data is read-only.
2. ~~No immutable submitted revision/snapshot.~~ **Resolved by CP-4D/CP-4E.** Submit/Resubmit captures the durable immutable revision, and snapshot-backed review/approval binds to that exact ReviewItem-linked identity.
3. ~~**Finalization can re-resolve newer rates after review.**~~ **Resolved by CP-4F.** Approved finalization preview and FinalLines consume the exact Approved ReviewItem-linked immutable snapshot; final pay cannot differ from the reviewed packet because of post-approval live rate/configuration drift.
4. **Lifecycle writes are not uniformly concurrency-safe.** Stale transitions can bypass the state graph.
5. ~~Bonus participates in current min/max earnings.~~ **Resolved by CP-3C.** `finalize_period` and `get_finalization_preview` both now exclude bonus from the min/max comparison base and add it back in only after minimum/maximum is applied.

### P1 — Must complete before real customer payroll

1. ~~No one-InReview slot enforcement.~~ Resolved by CP-1B.
2. ~~No Returned state for an older rejected period when a newer Open exists.~~ Resolved by CP-1A.
3. ~~No smart schedule-derived creation/promotion workflow.~~ Resolved by CP-1C/CP-1D.
4. Client-supplied standard period dates/pay date remain authoritative.
5. No SemiMonthly cadence. Durable schedule versioning was resolved by CP-2A.
6. ~~No period-day/calendar/Add Day snapshot.~~ Period-day snapshot resolved by CP-2B (`PayrollPeriodDays`). Add Day activation workflow remains pending.
7. ~~Prepared pre-entry cannot be enabled safely through current response contracts.~~ Resolved by CP-2F.
8. ~~No backend Current Payroll Hub contract.~~ Resolved by CP-5A: `GET /payroll/current` provides the backend-owned aggregate. The distinct fully-off and selected-day off-driver contracts are resolved by CP-5B; the four calculation report contracts are resolved by CP-5C.
9. ~~No Open/Returned expected-income contract.~~ Resolved by CP-4B: Open/Returned now have a backend-owned, read-only live calculation preview; Draft/Prepared remains source-only with no financial preview.
10. ~~No calculation/report contracts for required views.~~ Resolved by CP-5C: Drivers Report, Period Work, Period Pay, and Mixed are implemented as backend-owned report contracts.
11. ~~Bonus is generic Period Pay with competing unused storage.~~ Resolved by CP-3A: canonical `PayrollBonusEvents` domain; generic Period Pay blocks BONUS; legacy BONUS DraftLines hidden/blocked from period-pay paths. The min/max debt noted here at the time is now also resolved — see P0 #5 above (CP-3C).
12. ~~Status is mutable code text and historical labels can disappear.~~ Resolved by CP-2D1: canonical `PayrollPeriodDriverDayEntryState` stores `StatusKeyID`; finalization freezes label/off-reason snapshots. Remaining future work: per-period StatusKey availability snapshot.
13. ~~Current employment status can hide historical eligibility.~~ Resolved by CP-2E canonical eligibility snapshot.
14. ~~Pay-item order/labels are not period-snapshotted.~~ Resolved by CP-2C (`PayrollPeriodPayItems`).
15. ~~Review return/manual transition paths can orphan workflow state.~~ Resolved by CP-0C/CP-1A.
16. ~~Draft cancellation lacks a transition-specific action permission.~~ Resolved by CP-0C.
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
| Phase 1 | Lifecycle slots, Returned state, smart creation | Done with Notes | Claude | Codex lifecycle review |
| Phase 2 | Schedule, calendar, pay-item, eligibility, and status snapshots | Done with Notes | Claude | Codex data-model review |
| Phase 3 | Canonical bonus domain and min/max classification | Done with Notes | Claude | Codex financial-rule review |
| Phase 4 | Unified calculation core and immutable review snapshot | Complete (CP-4A–CP-4F Completed) | Claude | Codex calculation parity review |
| Phase 5 | Hub and calculation-report contracts | Complete | Claude | Codex contract/security review |
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

### CP-1E completion note

**Status:** Done with Notes
**Codex verdict:** PASS WITH NOTES
**Implementation commit:** `2a4b95d` — feat: add cp-1e current workflow capabilities
**Review result:** No P0/P1 blockers remain. CP-1E is safely closed.

**What CP-1E completed:**
- Added `GET /payroll/current-workflow`.
- Added backend-owned workflow slots for Open, Prepared/Draft, InReview, and Returned.
- Added workflow capability response model.
- Added blocked-action reason codes.
- Added workflow alerts, including Returned backlog, Prepared notice, InReview awaiting review, setup alerts, and slot invariant alerts.
- Added company-scope and branch-scope workflow responses.
- Enforced driver/ODA denial.
- Enforced branch access and company/tenant boundaries.
- Kept Returned backlog behavior aligned with CP-1D:
  - blocks submit;
  - does not block Prepared creation by itself.
- Excluded Approved slot and finalization capability from CP-1E.
- Excluded expected income, financial totals, reports, calculation preview, migrations, frontend work, and Phase 2 snapshots.
- Added focused CP-1E workflow capability/security tests.

**Validation:**
- D09 isolation: 1 passed
- F02 isolation: 1 passed
- CP-1E: 43 passed
- CP-1A/B/C/D: 151 passed
- Phase 0: 114 passed
- Payroll/security/branch: 82 passed
- Regression matrix: 347 passed
- Alembic current/head: 0050 / 0050
- `git diff --check`: clean

**Remaining P2/P3 notes:**
- P2: Prefer `default_factory` for schema collection defaults later.
- P2: Inactive-setup test could assert one exact reason code later.
- P2: Fixed foreign-company identifiers could conflict under parallel test execution.
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: Router LF→CRLF warning remains environment-only.
- P3: Global Git ignore permission warning remains environment-only.

### Phase 2 status note

**Status:** Done with Notes
**Completed:** CP-2A, CP-2B, CP-2C, CP-2D1, CP-2D2, CP-2E, CP-2F — all Done with Notes.
**Review result:** No CP-2A through CP-2F P0/P1 blockers remain. CP-2D was split into CP-2D1 (canonical daily entry state) and CP-2D2 (status-driven payment lane). Add Day activation is deferred and is not part of CP-2C, CP-2D2, or CP-2F closure. Phase 2 is closed. Phase 3 is Done with Notes: CP-3A, CP-3B1, CP-3B2a, CP-3B2b, and CP-3C are all Done with Notes.

### CP-2A completion note

**Status:** Done with Notes
**Codex verdict:** PASS WITH NOTES
**Implementation commit:** `99ee02e` — feat: add cp-2a payroll schedule versioning
**Review result:** No P0/P1 blockers remain. CP-2A is safely closed.

**What CP-2A completed:**
- Added migration 0051.
- Added `payroll.PayrollScheduleVersions`.
- Added `BranchPayrollSettings.CurrentScheduleVersionID`.
- Added `PayrollPeriods.ScheduleVersionID`.
- Added setup backfill to VersionNumber=1.
- Added downgrade guards to avoid dropping meaningful schedule-version history (SourceAction != BACKFILL check).
- Added schedule version creation on payroll setup update.
- Added lazy `ensure_current_schedule_version` repair path.
- Added CP-1C candidate `sv_id` binding and stale candidate rejection.
- Preserved candidate replay behavior (pre-CP-2A candidates can still replay already-created periods).
- Bound legacy period creation to ScheduleVersionID.
- Rejected legacy period creation when no active setup/version can be ensured.
- Confirmed SemiMonthly was not enabled.
- Confirmed PayDate behavior was not reintroduced.
- Added focused CP-2A tests (20 tests, 0 skipped) and related fixture updates.
- Updated Alembic head expectations to 0051.

**Validation:**
- CP-2A: 20 passed, 0 skipped
- CP-1C/D/E: 152 passed
- setup safety + payroll: 93 passed
- CP-1A/B: 42 passed
- CP-0A/B/C/D: 114 passed
- Alembic current/head: 0051 / 0051
- `git diff --check`: no whitespace errors

**Remaining P2/P3 notes:**
- P2: test_s17_downgrade_refusal could be tightened later.
- P2: test_s20_no_svid_candidate_rejected_for_new_creation could also assert period count unchanged later.
- P3: Some comments/docstrings can be cleaned later without behavior impact.
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: User global git ignore warning remains environment-only.
- P3: LF/CRLF warnings remain environment-only.

### CP-2B completion note

**Status:** Done with Notes
**Codex verdict:** PASS WITH NOTES
**Implementation commit:** `072c9f8` — feat: add cp-2b payroll period-day snapshots
**Review result:** No P0/P1 blockers remain. CP-2B calendar snapshot foundation is safely closed.

**What CP-2B completed:**
- Added migration 0052.
- Added `payroll.PayrollPeriodDays`.
- Added period-day calendar rows for periods created after CP-2B.
- Candidate-created Open periods now create period-day rows.
- Candidate-created Draft/Prepared periods now create period-day rows.
- Legacy-created periods now create period-day rows.
- Period-day rows use the period's `ScheduleVersionID`.
- Period-day rows read `NormalDaysOffMask` from `PayrollScheduleVersions`, not mutable current setup.
- Added day-of-week convention: Sun=0 through Sat=6.
- Added configured-off/default-work-day metadata.
- Added future Add Day fields: `IsAddedWorkDay`, `AddedByUserID`, `AddedAtUtc`, `AddedReason`.
- Add Day activation was explicitly deferred; no Add Day endpoint or workflow was added.
- Added snapshot-aware date validation for `get_day_grid`, `save_day_grid`, and direct draft-line creation.
- Legacy periods without day rows continue to use StartDate/EndDate fallback.
- Draft → Open promotion preserves existing period-day rows.
- Setup changes do not mutate existing period-day rows.
- Confirmed SemiMonthly was not enabled.
- Confirmed PayDate behavior was not introduced.
- Confirmed Prepared entry was not enabled.
- Added focused CP-2B tests.
- Updated Alembic head expectations to 0052.

**Validation:**
- CP-2B: 26 passed, 0 skipped
- CP-2A + CP-1C/D/E: 172 passed, 0 skipped
- setup safety + payroll: 93 passed, 0 skipped
- CP-1A/B: 42 passed, 0 skipped
- CP-0A/B/C/D: 114 passed, 0 skipped
- Alembic current/head: 0052 / 0052
- `git diff --check`: clean, LF→CRLF warning only

**Remaining P2/P3 notes:**
- P2: Add Day activation is deferred to a later CP-2B2 / pre-CP-2F unit.
- P2: `update_draft_line` does not accept/move `work_date`; snapshot validation there is future hardening only.
- P2: DB-level same-period/same-schedule composite FK hardening remains future hardening.
- P2: Add audit-count assertion for D26 later if desired.
- P2: Add custom interval exact day-count test later if desired.
- P2: Add rollback proof for day-row insert failure later if desired.
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: User global git ignore warning remains environment-only.
- P3: LF/CRLF warnings remain environment-only.

### CP-2C completion note

**Status:** Done with Notes
**Codex verdict:** PASS WITH NOTES
**Implementation commit:** `13d99c0` — feat: add cp-2c payroll pay-item snapshots
**Review result:** No P0/P1 blockers remain. CP-2C pay-item layout snapshot foundation is safely closed.

**What CP-2C completed:**
- Added migration 0053.
- Added `payroll.PayrollPeriodPayItems`.
- Added period-owned pay-item layout snapshots.
- Snapshot rows freeze: PayItem identity/code/name/display label; category/data type/unit/scope/rate behavior; visibility flags; RequiresRate; system/custom flags; PayItem status at snapshot time; active-in-period state; sort order; source branch config metadata where available.
- Snapshots all non-retired eligible system/company PayItems.
- Stores inactive branch items with `IsActiveInPeriod = false`.
- Candidate-created Open periods create snapshot rows.
- Candidate-created Draft/Prepared periods create snapshot rows.
- Legacy-created periods create snapshot rows.
- Candidate replay is idempotent and does not duplicate rows.
- Draft → Open promotion preserves existing snapshot rows.
- No historical backfill was performed.
- Legacy periods without snapshot rows retain live-config fallback.
- `get_day_grid` uses snapshot rows for snapshotted periods.
- `save_day_grid` validates against snapshot rows for snapshotted periods.
- Direct daily-line creation validates against snapshot rows.
- Daily-line meaningful updates validate against snapshot rows.
- Period Pay creation validates against snapshot rows.
- Period Pay meaningful updates validate against snapshot rows.
- Snapshot-first validation allows live-retired custom PayItems to remain usable for old periods when active in that period snapshot.
- `get_day_grid` / `save_day_grid` do not fall back to live config when a snapshot exists but has zero active Daily items.
- Custom Pay Item delete/retire guard treats snapshot references as historical usage.
- DailyStatus / DailyNote remain pseudo/informational lines and are not snapshotted.
- At the time CP-2C closed, PTO_STATUS remained unchanged as a Daily PayItem. PTO_STATUS was subsequently removed as a Pay Item in commit `236a506`. CP-2D2 did not reintroduce PTO_STATUS.
- BONUS, ADJUSTMENT, and GUARANTEED_MINIMUM are snapshot metadata only.
- No calculation/rate/finalization/ledger/report/expected-income/bonus workflow behavior was added.
- No Add Day activation was added.
- Prepared entry was not enabled.
- SemiMonthly and PayDate behavior were not introduced.

**Validation:**
- CP-2C focused: 36 passed, 0 skipped
- CP-2B + CP-2A + CP-1C/D/E: 198 passed, 0 skipped
- payroll setup + payroll + CP-1A/B: 135 passed, 0 skipped
- CP-0A/B/C/D: 114 passed, 0 skipped
- Alembic current/head: 0053 / 0053
- `git diff --check`: clean

**Remaining P2/P3 notes:**
- P2: Consider stronger DB-level composite integrity for PayrollPeriodPayItems company/branch vs parent period.
- P2: Clean minor comment mojibake later.
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: User global git ignore warning remains environment-only.
- P3: LF/CRLF warnings remain environment-only.

### CP-2D1 completion note

**Status:** Done with Notes
**Codex verdict:** PASS WITH NOTES (initial FAIL due to P1 — direct DailyStatus write accepted invalid/inactive codes; fixed before commit)
**Implementation commit:** `d872889` — feat: add cp-2d1 canonical daily entry state
**Review result:** No P0/P1 blockers remain. CP-2D1 canonical daily entry state is safely closed. CP-2D has been split — CP-2D1 covers the canonical table and read/write plumbing; CP-2D2 (status-driven payment lane) was implemented subsequently and is also Done with Notes.

**What CP-2D1 completed:**
- Added migration 0054.
- Added `payroll.PayrollPeriodDriverDayEntryState` — one row per driver per work-date per period.
- Selected StatusKey stored as `StatusKeyID` (FK, ON DELETE RESTRICT).
- User note stored as `NoteText` (plain text, independent of StatusKeyID).
- `IsVoided` soft-delete flag: set TRUE when both `StatusKeyID` and `NoteText` are cleared.
- Finalization snapshot fields (`StatusCodeSnapshot`, `StatusLabelSnapshot`, `StatusIsOffReasonSnapshot`, `StatusHoursValueSnapshot`, `FinalizedAtUtc`) remain NULL for editable periods; frozen at finalization.
- UNIQUE constraint on `(PayrollPeriodID, DriverID, WorkDate)`.
- ON DELETE CASCADE from `PayrollPeriods` (period delete cascades canonical rows).
- 5 supporting indexes added.
- Live StatusKey dropdown from `PayrollStatusKeys` for editable periods — no availability snapshot at period open; no `PayrollPeriodStatusKeys` table.
- `_upsert_entry_state` shared helper with `set_status` / `set_note` flags for partial-field writes.
- `_void_entry_state_field` partial-clear helper with `clear_status` / `clear_note` flags.
- `_finalize_canonicalize_entry_state` finalization helper: Part A creates canonical rows from legacy DailyStatus/DailyNote DraftLines; Part B fills snapshot fields and timestamps note-only rows.
- `save_day_grid` dual-writes canonical row after DraftLine write; deactivated-key bypass for unchanged resubmissions preserved.
- `get_day_grid` batch-loads canonical rows; per-driver canonical-first read with DraftLine legacy fallback.
- Finalized/locked periods use snapshot values for status code, label, and is_off display.
- Editable periods resolve label/is_off from live `status_key_id_map`; deactivated keys referenced in existing canonical rows pre-fetched via `deactivated_key_map`.
- `add_draft_line` with `DailyStatus`: validates status code as active before inserting DraftLine; blank/missing notes rejected; invalid/inactive codes raise 422 before any mutation.
- `update_draft_line` with `DailyStatus`: validates new code as active before updating DraftLine; policy — direct path always requires active key, no deactivated bypass (unlike `save_day_grid`).
- `add_draft_line` / `update_draft_line` / `void_draft_line` with `DailyNote`: dual-write canonical `NoteText`; no StatusKey validation required.
- `void_draft_line` clears canonical status and/or note field depending on line type; sets `IsVoided = TRUE` when both fields become NULL.
- Legacy DailyStatus/DailyNote DraftLine dual-write fully preserved for compatibility (finalization Step 3, usage-limit enforcement, off-driver query).
- CP-2D1 itself did not implement payment, `StatusKeyPayRules`, or derived payment lines; those were deferred to CP-2D2, which subsequently implemented the status-driven payment lane and is now Done with Notes.
- No DAC/allowance ledger, no off allowance.
- No `PTO_STATUS` behavior change.
- No finalization total/rate/ledger/report changes.
- No `PayrollPeriodStatusKeys` (no availability snapshot at period open).
- No frontend work.
- Added CP-2D1 focused tests (23 tests, 0 skipped); updated Alembic head expectations in CP-2B and CP-2C test files to 0054.

**Validation:**
- CP-2D1 focused: 23 passed, 0 skipped
- CP-2C + CP-2B: 62 passed, 0 skipped
- CP-0A: 74 passed, 0 skipped
- Alembic current/head: 0054 / 0054
- `git diff --check`: clean (LF→CRLF warning only, environment-only)

**Remaining P2/P3 notes:**
- P2: `update_draft_line` deactivated-bypass is intentionally absent on the direct path; document in API notes if a UI caller needs it.
- P2: `_resolve_status_key_id` (no `isactive` filter) still used in `update_draft_line` when `notes` is not changing but other fields are; this is correct behavior (canonical stays in sync without forcing active-key re-validation on non-status edits).
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: User global git ignore warning remains environment-only.
- P3: LF/CRLF warnings remain environment-only.

### CP-2D2 completion note

**Status:** Done with Notes
**Codex verdict:** PASS_WITH_NOTES
**Implementation commit:** `8bbd1fd` — feat: add cp-2d2 status payment
**Review result:** No P0/P1 blockers remain. CP-2D2 status-driven payment lane is safely closed.

**What CP-2D2 completed:**

- Added migration 0056.
- Added `payroll.StatusRateColumns`.
- Added default system `Status Pay`.
- Status rate columns are backed by `payroll.RateTypes`.
- Custom status columns create company-owned `SRC_*` RateTypes.
- Status Keys can link to a status rate column.
- Existing `StatusKey.HoursValue` is used as the status payment quantity.
- Daily Grid status selection can derive system-owned status payment DraftLines.
- Derived amount = `StatusKey.HoursValue × DriverRate.Amount` for the linked status rate column.
- Missing driver status rate produces `NeedsManagerReview=True` / blocker behavior, not silent zero.
- Driver Pay Rates matrix includes default/custom status rate columns.
- Driver Pay Rates batch save supports status rate columns without `PayItemRateTypeMap`.
- Generic DriverRate create/update/approve paths are guarded against cross-branch status RateType misuse.
- `_resolve_rate_behavior` no longer trusts `SRC_*` prefix alone; it verifies active `StatusRateColumns` membership.
- DB triggers enforce StatusRateColumn RateType ownership and BranchID/CompanyID ownership.
- StatusKey linkage enforces same branch/company and `HoursValue > 0` when payment is configured.
- SourceSnapshot is populated for derived status payment DraftLines.
- SourceSnapshot is preserved through finalization.
- System-derived status payment lines are protected from manual edit/void paths.
- `PTO_STATUS` did not return.
- No status Pay Item was created.
- No frontend work was included.
- No allowance/DAC/leave accrual workflow was implemented.

**Validation:**

- CP-2D2 focused tests: 45 passed.
- Alembic current/head: 0056 / 0056.
- `git diff --check`: clean.
- Frontend lint: passed with one unrelated existing warning.
- Representative suite retained 5 known unrelated/pre-existing/order-dependent failures.

**Remaining P2/P3 notes:**

- P2: Add explicit unauthorized-branch access test for `GET /settings/branches/{branch_id}/status-rate-columns` once a limited-scope user fixture exists.
- P3: Temporary PostgreSQL/test database shutdown warning remains environment-only where observed.
- P3: Representative suite has known unrelated/order-dependent failures outside CP-2D2.

### CP-2E completion note

**Status:** Done with Notes
**Implementation commit:** `c100b3c` — feat: add cp-2e canonical eligibility
**Alembic revision:** 0057 (head)
**Review result:** No P0/P1 blockers remain. CP-2E canonical eligibility snapshot is safely closed.

**What CP-2E completed:**

- Added migration 0057.
- Added `payroll.PayrollPeriodEligibilitySnapshots` — one marker row per snapshotted period. Marker prevents empty-snapshot periods from falling back to live roster.
- Added `payroll.PayrollPeriodDriverEligibility` — one detail row per `PayrollPeriodID + DriverID` with reason code and effective window.
- Eligibility reason codes: `Active`, `TerminatedHistorical`, `Transferred`, `IncludedByExistingData`.
- Draft creation inserts a provisional marker and snapshot rows.
- Draft→Open promotion regenerates and freezes eligibility (marker `IsFrozen = TRUE`).
- Direct Open creation freezes immediately.
- Existing Open, Returned, InReview, and Approved periods are backfilled with a marker by migration 0057 and frozen (`IsFrozen = TRUE`).
- Locked/Archived/Cancelled are not backfilled by the CP-2E migration.
- Returned periods remain frozen and editable only for source corrections.
- Day grid, save, direct draft lines, period-pay eligibility, finalization, and status payment refresh use snapshot eligibility for marked periods.
- Legacy fallback applies only when no marker exists.
- Existing-source rescue: any snapshot row, regardless of reason code, keeps a driver/date visible and manageable if an existing `DraftLine` or `PPDES` source row exists for that exact `work_date`.
- Existing period-pay source uses `LineScope='Period'`, not `WorkDate IS NULL`.
- Snapshot validation replaces live `EmploymentStatus`/`DriverStatus` checks during finalization preview and finalization for marked periods.
- Non-snapshot driver lines block finalization.
- CP-2D2 status payment derivation is eligibility-aware; no `PTO_STATUS`; status is not a PayItem.

**Validation:**

- CP-2E focused tests: 75 passed.
- CP-2D2 focused: 45 passed.
- CP-0A mutation guard: 70 passed.
- Alembic current/head: 0057 / 0057.
- `git diff --check`: clean.

**Remaining P2/P3 notes:**

- P2: `PayrollPeriodStatusKeys` (availability snapshot at period open) remains deferred.
- P2: Concurrency-safe status usage limits remain a future hardening item.
- P3: Temporary PostgreSQL shutdown warning remains environment-only.

### CP-2F completion note

**Status:** Done with Notes
**Implementation commit:** `1019b16` — feat: add cp-2f prepared source entry
**Alembic revision:** 0057 (no new migration; CP-2F added no schema changes)
**Review result:** No P0/P1 blockers remain. CP-2F controlled Prepared operational entry is safely closed.

**What CP-2F completed:**

- `SOURCE_ENTRY_STATUSES = {"Draft", "Open", "Returned"}` added to `schemas.py` — guards source-only operational paths.
- `ENTRY_ALLOWED_STATUSES = {"Open", "Returned"}` remains unchanged — guards all financial paths. Draft is not added to financial guards.
- Draft (Prepared) is an operational source-entry workspace only. DB status remains `Draft`; "Prepared" is display/workflow language.
- Draft GET day grid suppresses financial fields: `calculated_amount = null`, `rate_amount = null`, `gross_total = null`, `financials_available = false`.
- Draft SAVE day grid writes source only (quantity, status, notes) and skips status payment money derivation at the `save_day_grid` call site (`if period.status == "Draft": continue` before `_sync_status_payment_for_entry_state`).
- Draft POST `/lines` supports daily Manual source rows; rejects System source, missing `work_date`, `rate_amount` (universally, regardless of PayItem/RateBehavior), and `needs_manager_review`. `CalculatedAmount = NULL`, `RateAmount = NULL`, `NeedsManagerReview = FALSE` are stored.
- Draft PATCH `/lines` clears stale `CalculatedAmount`, `RateAmount`, `NeedsManagerReview` on every allowed non-void edit (not only on qty/rate changes).
- Draft DELETE `/lines` allows daily source cleanup only.
- `/lines` for Draft is sanitized: Period-scope, System-source, `STATUS_PAYMENT`, and `ADJUSTMENT` rows are filtered; financial fields are nulled.
- `/lines/summary`, `/period-pay`, and eligible-drivers endpoints return 422 for Draft.
- Draft→Open activation (inside the existing workflow lock): CP-2E regenerates/freezes eligibility → `_refresh_status_payment_lines` derives status payment → `_refresh_draft_calculations` refreshes daily calculations. No duplicate STATUS_PAYMENT lines.
- Hub `can_enter_source` and `can_open_day_grid` allow Draft; `can_submit_for_review` requires `status == "Open"`.
- Period Pay, Bonus, Expected Income, finalization preview, finalization, approval, lock, and archive remain blocked for Draft.

**Validation:**

- CP-2F focused tests: 46 passed (includes `test_cp2f_draft_direct_daily_line_rejects_rate_amount_for_non_perunit_item` with custom Fixed-behavior pay item proving CP-2F guard fires before PerUnit guard).
- CP-2E focused: 75 passed.
- CP-2D2 focused: 45 passed.
- payroll_trust_p2: 15 passed.
- CP-0A mutation guard: 70 passed.
- Alembic current/head: 0057 / 0057.
- `git diff --check`: clean.

**Remaining P2/P3 notes:**

- P2: Add Day activation for Draft remains deferred (CP-2B2 scope).
- P2: Hub does not yet expose Draft money totals — confirmed by design; no future work needed here.
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: LF/CRLF working-copy warnings remain environment-only.

### Phase 3 — Canonical Bonus Domain and Min/Max Classification

**Status:** `Done with Notes`

- [x] P3A: Canonical bonus event domain (CP-3A). — **Done with Notes**
- [x] P3B1: Zero-inclusive all-driver bonus summary (CP-3B1). — **Done with Notes**
- [x] P3B2a: Bonus batch safety foundation — revision token, idempotency/correlation schema, DB ownership hardening (CP-3B2a). — **Done with Notes**
- [x] P3B2b: Create-only transactional batch endpoint (idempotent replay, all-or-nothing writes). — **Done with Notes**
- [x] P3C: Min/max formula correction (bonus excluded from min/max base). — **Done with Notes**

Phase 3 is complete. All five units (CP-3A, CP-3B1, CP-3B2a, CP-3B2b, CP-3C) are Done with Notes; no phase-scoped P0/P1 blocker remains. Phase 4 was Pending at Phase 3's historical close; it has since completed CP-4A through CP-4F.

### CP-3A completion note

**Status:** Done with Notes
**Implementation commit:** `ade9234` — feat: add cp-3a canonical bonus events
**Alembic revision:** 0058
**Review result:** Codex PASS_WITH_NOTES; no P0/P1 blockers remain.

**What CP-3A completed:**

- Converted/renamed unused `payroll.PayrollRunBonuses` into canonical `payroll.PayrollBonusEvents` (migration 0058).
- Backfilled positive legacy BONUS Period Pay DraftLines into `PayrollBonusEvents` with `SourceDraftLineID` trace linkage. Voided legacy rows mapped to `Voided` status in the canonical table.
- Removed `IncludeInMinimumPayComparison` as a configurable behavior (dropped; bonus is invariantly excluded from min/max by domain rule — the formula correction that enforces this was pending CP-3C at the time of this commit and is now done; see the CP-3C completion note below).
- Added canonical bonus-event CRUD endpoints:
  - `GET /payroll/periods/{id}/bonuses`
  - `POST /payroll/periods/{id}/bonuses`
  - `PATCH /payroll/periods/{id}/bonuses/{bonus_event_id}`
  - `DELETE /payroll/periods/{id}/bonuses/{bonus_event_id}` (void, idempotent)
- Blocked new BONUS creation through generic `/period-pay` (422 with redirect message).
- Hidden legacy BONUS DraftLines from `/period-pay` reads (`linetype != 'BONUS'` filter).
- Blocked update/void of legacy BONUS DraftLines through `/period-pay` (422 guard).
- Added CP-2E snapshot-aware eligibility and branch ownership validation for bonus creation (same `_assert_driver_eligible_for_period_via_snapshot` guard as non-BONUS period-pay).
- Enforced strictly positive bonus amount (`Amount > 0` DB constraint; zero and negative rejected).
- Allowed multiple active bonus events per driver/period.
- Enforced Open/Returned-only mutation; Draft/InReview/Approved/Locked/Archived/Cancelled reject bonus mutation.
- Added finalization bridge: active `PayrollBonusEvents` are written as `BONUS` `PayrollFinalLines` with `BonusEventID` linkage. Voided events are excluded. Old BONUS DraftLines are not included in finalization.
- Added finalization preview bridge: canonical bonus events populate `bonus_events` list in the preview response. BONUS does not appear in the DraftLine-based `lines` list.
- Preserved ledger bonus inclusion through final lines.
- Added `BonusEventID` column to `PayrollFinalLines` for finalization bridge.
- Updated submit/resubmit empty-period guards to count non-BONUS DraftLines plus active BonusEvents (bonus-only periods can submit).
- Added full `BONUS_EVENT_ADDED` / `BONUS_EVENT_UPDATED` / `BONUS_EVENT_VOIDED` audit events with `entity_name = 'PayrollBonusEvents'`.

**Not implemented by CP-3A:**

- No batch bonus (added later by CP-3B2a/CP-3B2b).
- No min/max formula correction (added later by CP-3C).
- No Hub, reports, frontend, submitted snapshot, or manual recalculation work.
- No zero-inclusive all-driver bonus summary (added later by CP-3B1).

**Validation:**

- CP-3A focused tests: 26 passed.
- test_finalization_preview: passed.
- test_ledger: 19 passed.
- test_cp0a_mutation_status_guard: 70 passed.
- test_m14: 32 passed.
- Combined sanity after alembic upgrade: 73 passed (cp3a + finalization_preview + ledger).
- Full 7-suite regression: 296 passed, 12 warnings.
- Alembic current/head: 0058 / 0058.
- `git diff --check`: clean (LF→CRLF warnings only, environment-only).

**Remaining P2/P3 notes:**

- P2: Defense-in-depth DB branch predicates (e.g., composite FK trigger ensuring BonusEvent branch matches period branch) — **resolved by CP-3B2a.** (CP-3B1 first mitigated the read-side risk by scoping summary aggregation on `BranchID` explicitly; CP-3B2a then added the actual DB-level ownership trigger on `PayrollBonusEvents`.)
- P2: Zero-inclusive all-driver bonus summary — **resolved by CP-3B1.**
- P2: Batch bonus with idempotency/correlation — **resolved: foundation by CP-3B2a, endpoint by CP-3B2b.**
- P3: Bonus participating in the current min/max base — **resolved by CP-3C.**
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: LF/CRLF working-copy warnings remain environment-only.

---

### CP-3B1 completion note

**Status:** Done with Notes
**Implementation commit:** `7a34a52` — feat: add cp-3b1 bonus summary
**Alembic revision:** 0058 (no new migration; CP-3B1 is read-only over existing CP-3A schema)
**Review result:** Codex PASS_WITH_NOTES after two P1 fixes; no P0/P1 blockers remain.

**What CP-3B1 completed:**

- Added `GET /payroll/periods/{id}/bonuses/summary` — a zero-inclusive, backend-owned bonus summary.
- Preserved the existing `GET /payroll/periods/{id}/bonuses` event-list endpoint unchanged.
- Roster source is the CP-2E `PayrollPeriodDriverEligibility` snapshot only, gated by the `PayrollPeriodEligibilitySnapshots` marker. No live-roster fallback; periods without a marker return a controlled `422` (`BONUS_SUMMARY_UNAVAILABLE_NO_ELIGIBILITY_SNAPSHOT`).
- All snapshot-eligible drivers appear, including zero-bonus drivers.
- Mid-period removed/transferred/terminated snapshot drivers (`TerminatedHistorical`, `Transferred`) remain visible with their reason codes.
- `IncludedByExistingData` snapshot rows remain visible.
- `PayrollBonusEvents` are aggregated onto the snapshot roster; the roster is never expanded from `PayrollBonusEvents`.
- Event aggregation is scoped by `PayrollPeriodID`, `CompanyID`, **and `BranchID`** together — a same-company, cross-branch contaminated event cannot inflate a driver's total (P1 fix from Codex re-review).
- Active events contribute to `total_bonus`; Voided events remain visible in the `events` array but do not affect totals.
- Multiple active events per driver aggregate correctly.
- Sorting: nonzero totals first (descending), then `driver_name` / `driver_code` / `driver_id`.
- Capabilities (`can_create` / `can_update` / `can_void`) are computed **per driver**, not period/user-wide (P1 fix from Codex re-review):
  - `can_create` is false for an `IncludedByExistingData` driver who would be rejected by `POST /bonuses` (no existing period-pay source) — mirrors `_assert_driver_eligible_for_period_via_snapshot` via a new read-only helper, `_bonus_summary_driver_create_eligible`.
  - `can_update` / `can_void` require at least one Active event for that driver; zero-event and voided-only drivers get `false` with a `no_active_bonus_event` reason code.
- Draft/Prepared periods return `422` (no financial exposure).
- Open/Returned periods can return mutation capabilities for `payroll.entry` users.
- InReview/Approved/Locked/Archived/Cancelled are read-only where the snapshot marker exists.
- ODA/driver users are denied (`_require_not_driver_role`).
- View-only (`payroll.view`-only) users can read the summary but get all capabilities `false` with a `permission_entry_required` reason code.

**Not implemented by CP-3B1:**

- No batch bonus endpoint, idempotency keys, or batch correlation IDs at the time of CP-3B1 (foundation added by CP-3B2a; the endpoint itself was added by CP-3B2b).
- No aggregate/period-level bonus revision or concurrency contract at the time of CP-3B1 (event-level `data_revision` was sufficient for this read-only summary; `PayrollPeriods.BonusDataRevision` was added later by CP-3B2a and is now exposed in this summary's response).
- No min/max formula correction at the time of CP-3B1 (added later by CP-3C).
- No migration, no frontend changes, no Hub/reports/finalization redesign.

**Validation:**

- CP-3B1 focused tests: 25 passed (19 initial + 6 added for the two P1 fixes).
- test_cp3a_bonus_events: 26 passed.
- test_finalization_preview: 28 passed.
- test_ledger: 19 passed.
- test_cp2e_eligibility_snapshot: 75 passed, 12 warnings.
- test_cp2f_prepared_operational_entry: 46 passed.
- Alembic current/head: 0058 / 0058.
- `git diff --check`: clean (LF→CRLF warnings only, environment-only).

**Remaining P2/P3 notes:**

- P2: Locked/Archived read-only capability paths rely on the shared Open/Returned-only mutation check rather than a dedicated fixture test for those two specific statuses (reaching them requires the full finalize flow); InReview/Approved/Cancelled are tested directly.
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: LF/CRLF working-copy warnings remain environment-only.

---

### CP-3B2a completion note

**Status:** Done with Notes
**Implementation commit:** `5feacf5` — feat: add cp-3b2a bonus batch safety
**Alembic revision:** 0059 (migration `0059_cp3b2_bonus_batch_safety`)
**Review result:** Codex PASS_WITH_NOTES; no P0/P1 blockers remain.

**What CP-3B2a completed — safety foundation only, no batch endpoint at the time this unit shipped (the endpoint was added later by CP-3B2b — see that completion note below):**

- Migration 0059:
  - `payroll.PayrollPeriods.BonusDataRevision BIGINT NOT NULL DEFAULT 0` — period-level bonus-mutation concurrency token.
  - `payroll.PayrollBonusBatchRequests` — foundation-only durable idempotency/correlation table (no writer existed as of CP-3B2a; CP-3B2b later made it live): `IdempotencyKey`, `RequestHash`, `RequestPayloadJSON`, `BatchCorrelationID`, `ExpectedBonusDataRevision`/`ResultBonusDataRevision`, `CreatedEventIDs`/`CreatedEventCount`, `Status` (currently `'Applied'`-only), actor/timestamps.
  - Unique index on `(CompanyID, BranchID, PayrollPeriodID, IdempotencyKey)`; unique index on `BatchCorrelationID`.
  - Partial index on `PayrollBonusEvents.BatchCorrelationID WHERE ... IS NOT NULL`.
  - A DB-level ownership trigger on `PayrollBonusEvents` (`trg_bonusevents_ownership`) rejecting any insert/update whose `CompanyID`/`BranchID` doesn't match the owning `PayrollPeriods` row, with a preflight check that fails the migration outright if any existing contaminated row is found (no silent fix).
  - Downgrade guards: refuses if any `PayrollBonusBatchRequests` rows exist, if any `PayrollBonusEvents.BatchCorrelationID IS NOT NULL`, or if any `PayrollPeriods.BonusDataRevision <> 0`. No bonus events are ever deleted by this migration's downgrade.
- Single-event mutation retrofits (`POST`/`PATCH`/`DELETE /bonuses`):
  - `POST` increments `BonusDataRevision` by one on success, in the same transaction as the event insert.
  - `PATCH` increments it by one on success; its existing `data_revision` optimistic-concurrency check is now enforced by an atomic SQL predicate (`UPDATE ... WHERE ... AND DataRevision = :expected AND Status = 'Active' RETURNING ...`) instead of a read-then-write check with no predicate on the write — a stale/concurrent-void race now correctly 409s instead of silently succeeding or corrupting a voided event.
  - `DELETE` (void) increments it by one only on an actual Active→Voided transition; the atomic predicate means a concurrent void race is treated as the same idempotent case (no double-increment, no error).
  - All three failure paths (validation 422, stale-revision 409, wrong-period 404) leave `BonusDataRevision` unchanged.
- `_write_line_audit` gained an optional `correlation_id` parameter. Omitted: byte-identical behavior to before (the column is omitted from the INSERT so the table's `DEFAULT gen_random_uuid()` applies). Supplied: that exact value is stored — reserved for CP-3B2b to link a batch's per-event audit rows under one `BatchCorrelationID`. No current single-event caller passes it.
- `GET /bonuses/summary` now returns `bonus_data_revision`, sourced directly from `PayrollPeriods.BonusDataRevision` — never `MAX(PayrollBonusEvents.DataRevision)`. All other CP-3B1 summary behavior (roster, branch scoping, capabilities, no-marker handling) is unchanged.

**Not implemented by CP-3B2a (at the time of this unit):**

- No `POST /payroll/periods/{id}/bonuses/batch` endpoint — added later by CP-3B2b.
- No batch service function, no batch route, no idempotent-replay logic — nothing wrote to `PayrollBonusBatchRequests` at this point (CP-3B2b became its first writer).
- No update/void-batch, no desired-total reconciliation, no zero-means-clear, no negative bonus — still true after CP-3B2b; these remain out of scope entirely.
- No min/max formula correction at the time of CP-3B2a (added later by CP-3C).
- No frontend changes, no reports/Hub/finalization redesign.

**Validation:**

- CP-3B2a focused tests: 25 passed.
- test_cp3b1_bonus_summary (one test updated for the new DB trigger): 25 passed.
- test_cp3a_bonus_events: 26 passed.
- test_finalization_preview: 28 passed.
- test_ledger: 19 passed.
- test_cp2e_eligibility_snapshot: 75 passed, 12 warnings.
- test_cp2f_prepared_operational_entry: 46 passed.
- Alembic current/heads: 0059 / 0059.
- `git diff --check`: clean (LF→CRLF warnings only, environment-only).

**Remaining P2/P3 notes:**

- P2: No direct tests yet for the ownership trigger firing on `UPDATE` (only `INSERT` is directly tested) — **resolved by CP-3B2b's test suite**, which directly tests `UPDATE` rejection on the analogous `PayrollBonusBatchRequests` ownership trigger; the downgrade-refusal guards for migration 0059 itself remain untested individually (each guard's predicate is documented and one is exercised via the CP-3B2b batch-request downgrade-guard test).
- P2: `PayrollBonusBatchRequests` had independent FKs but no composite ownership trigger of its own — **resolved by CP-3B2b's migration 0060**, which added that trigger before the table's writer existed.
- P2: `GET /bonuses` (the plain event list) and `_get_bonus_event_by_id` are not independently `BranchID`-filtered at the query level — correct today only because the DB ownership trigger and the CP-3B1 summary's explicit `BranchID` filter both hold; if either weakens, this becomes a live gap. Acceptable at current scale; documented as known debt.
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: LF/CRLF working-copy warnings remain environment-only.

---

### CP-3B2b completion note

**Status:** Done with Notes
**Implementation commit:** `0124718` — feat: add cp-3b2b bonus batch endpoint
**Alembic revision:** 0060 (migration `0060_cp3b2b_batch_request_ownership`)
**Review result:** Codex PASS_WITH_NOTES; no P0/P1 blockers remain.

**What CP-3B2b completed:**

- Added `POST /payroll/periods/{id}/bonuses/batch` — the create-only transactional bonus batch endpoint.
- Migration 0060 hardened `payroll.PayrollBonusBatchRequests` **before** this endpoint became its first writer: `CreatedByUserID NOT NULL`; check constraints for non-negative `Expected`/`ResultBonusDataRevision`, SHA-256-hex `RequestHash`, JSON-object `RequestPayloadJSON`, JSON-array `CreatedEventIDs` with `CreatedEventCount` matching its length; and a `BEFORE INSERT OR UPDATE` ownership trigger (`trg_bonusbatchrequests_ownership`) mirroring `PayrollBonusEvents`' own trigger, with a preflight check that fails the migration outright if any existing contaminated row is found.
- The batch is all-or-nothing: every item is validated (positive amount, NUMERIC(18,2) range/precision, CP-2E snapshot-aware eligibility — identical guard to single-event `POST /bonuses`) before any event is inserted, and the whole request runs in one transaction, so any failure rolls back every event, the batch-request row, the revision bump, and every audit row.
- Uses `PayrollBonusEvents` only — never creates `PayrollDraftLines`, never creates generic Period Pay BONUS rows.
- Requires `expected_bonus_data_revision`; validated against `PayrollPeriods.BonusDataRevision` via the same predicated helper CP-3B2a introduced (`_increment_bonus_data_revision`). Increments that revision **exactly once per successful batch**, never per event.
- Idempotency: keyed on `(CompanyID, BranchID, PayrollPeriodID, IdempotencyKey)`. An exact replay (same key + same canonical request hash — SHA-256 over sorted, compact JSON; item order and `expected_bonus_data_revision` are both part of the hash) returns the durably-stored result read-only, **HTTP 200**, no new writes, no revision bump — and succeeds even if the period has since become non-editable, since replay never re-checks lifecycle status. Same key + different payload, or same key + different `expected_bonus_data_revision`, returns **409**. A concurrent duplicate-key race is caught via `SAIntegrityError` on the unique idempotency index and mapped to a clean 409.
- A fresh apply returns **HTTP 201**.
- Every created event stores the shared, server-generated `BatchCorrelationID` and the batch's `IdempotencyKey` (non-unique on events — the authoritative unique idempotency record remains the `PayrollBonusBatchRequests` row).
- Writes one `BONUS_EVENT_ADDED` audit row per created event plus one `BONUS_BATCH_APPLIED` row (`entity_name = 'PayrollBonusBatchRequests'`), all sharing the same `correlation_id` via `_write_line_audit`'s CP-3B2a-added parameter.
- New batch applies are Open/Returned only; Draft/InReview/Approved/Locked/Archived/Cancelled are all rejected — verified individually for each non-editable status.
- ODA/driver users and view-only (`payroll.view`-only) users are denied, matching single-event guards.

**Not implemented by CP-3B2b (explicitly out of scope):**

- No update-batch or void-batch — the existing `PATCH`/`DELETE /bonuses/{id}` endpoints remain the only update/void paths.
- No desired-total reconciliation, no zero-means-clear, no negative bonus.
- No min/max formula correction at the time of CP-3B2b (added later by CP-3C).
- No frontend changes, no reports/Hub/finalization redesign.

**Validation:**

- CP-3B2b focused tests: 41 passed.
- Requested regression group (CP-3B2a, CP-3B1, CP-3A, finalization_preview, ledger, CP-2E, CP-2F): 244 passed, 12 pre-existing CP-2E warnings.
- Alembic current/heads: 0060 / 0060.
- `git diff --check`: clean (LF→CRLF warnings only, environment-only).
- Final working tree after commit: clean.

**Remaining P2/P3 notes:**

- P2: Harden `_get_bonus_events_in_order` to additionally require the replayed period and branch match, and verify all stored event IDs actually resolve (defense-in-depth; not a known live gap today).
- P2: Add a true two-connection concurrency test for two requests racing with the same `expected_bonus_data_revision` (current coverage is a sequential-retry proxy relying on the period `FOR UPDATE` lock plus the unique idempotency index; the ASGI test transport cannot drive genuine parallel requests).
- P3: Replay intentionally returns the events' *current* state if they were later updated/voided through the single-event endpoints — no event snapshot is stored. The immutable batch metadata (`PayrollBonusBatchRequests` row itself: hash, payload, correlation id, revisions, event IDs/count) remains correct regardless.
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: LF/CRLF working-copy warnings remain environment-only.

---

### CP-3C completion note

**Status:** Done with Notes
**Implementation commit:** `90a6dbe` — fix: exclude bonus from payroll min max
**Alembic revision:** 0060 (unchanged; no new migration)
**Review result:** Codex PASS_WITH_NOTES; no P0/P1 blockers remain.

**Formula before CP-3C (bug):**
```
earned = SUM(FinalLines.finalamount WHERE linetype NOT IN (SYS_MIN_TOPUP, SYS_MAX_CAP))
       = normal_pay + BONUS                      ← bug: BONUS included
if earned < min: SYS_MIN_TOPUP = min - earned
if earned > max: SYS_MAX_CAP  = max - earned
```

**Formula after CP-3C (corrected):**
```
normal_base = normal_daily_pay + non_bonus_normal_period_pay
minimum_adjustment = max(minimum - normal_base, 0)
after_minimum = normal_base + minimum_adjustment
maximum_adjustment = min(maximum - after_minimum, 0)
normal_after_minmax = after_minimum + maximum_adjustment
total_bonus = sum(Active canonical PayrollBonusEvents)
total_pay = normal_after_minmax + total_bonus
```

**What CP-3C completed:**

- Corrected `finalize_period`'s Step 3b earned-base aggregation: changed from `WHERE linetype NOT IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP')` (which included BONUS) to a `CASE`-based conditional `SUM` that also excludes `'BONUS'` from the comparison base — while still including every driver with any final line (a `WHERE` row filter would have silently dropped a bonus-only driver from the aggregation entirely, since a bonus-only driver has no non-BONUS rows to match).
- Corrected `get_finalization_preview`: bonus amounts are tracked in a separate `driver_bonus` accumulator instead of being folded into `driver_period` — the min/max comparison (`normal_base = driver_daily + driver_period`) is now bonus-free by construction, matching finalization exactly.
- Added a shared `_MINMAX_BASE_EXCLUDED_LINETYPES = ("BONUS", "SYS_MIN_TOPUP", "SYS_MAX_CAP")` constant, used directly in finalization's SQL; preview enforces the identical *rule* via its own accumulator (preview has no unified LineType-tagged row set to filter, so the constant isn't executable there — documented explicitly in-code to avoid overclaiming shared execution).
- Canonical active `PayrollBonusEvents` remain fully included in total payout and the final ledger — only their treatment relative to min/max changed, not their inclusion.
- Legacy BONUS DraftLines remain excluded from finalization entirely (unchanged from CP-3A).
- Bonus-only drivers remain in min/max processing with `normal_base = 0` (tested both in preview and after finalization).
- `STATUS_PAYMENT` and non-BONUS `ADJUSTMENT` lines are unchanged — both remain part of the normal min/max base.
- `FinalizationPreviewDriverTotal` gained `bonus_total` (default `Decimal("0")`, additive/backward-compatible); `gross_pay` now correctly means normal pay only; `final_pay = gross_pay + sys_adjustment + bonus_total`.
- `FinalizationPreviewSysAdjustment` also gained `bonus_total`, and its own `final_pay` was corrected to report the driver's true total (`gross_before + adjustment_amount + bonus_total`) rather than a bonus-free intermediate — this was the Codex-flagged P1 fixed after the first implementation pass (the two `final_pay` fields disagreed with each other and with the finalized ledger).
- Preview, finalization, and the finalized ledger were verified to agree under the corrected formula via focused parity tests (minimum and maximum directions, including direct inspection of `sys_adjustments[]` against the actual persisted `SYS_MIN_TOPUP`/`SYS_MAX_CAP`/`BONUS` final-line amounts).

**Not implemented by CP-3C (explicit non-goals):**

- No migration — this was a pure calculation-logic fix over existing schema.
- No frontend changes.
- No Phase 4 calculation-core extraction or calculation-snapshot architecture.
- No bonus CRUD or batch behavior change (CP-3B2b's endpoint, idempotency, and revision contract are untouched).
- No manual recalculation workflow.
- No change to Prepared/Draft financial exposure (still no financial preview for Draft periods).
- No change to `STATUS_PAYMENT` or `ADJUSTMENT` classification.

**Validation:**

- CP-3C focused tests (`test_cp3c_minmax_bonus.py`): 17 passed.
- test_finalization_preview: 28 passed (one existing assertion updated — it had directly encoded the pre-CP-3C bug, asserting bonus was counted in `period_pay`; now asserts `bonus_total`, plus an added before/after baseline proving `period_pay`/`gross_pay` are unchanged by bonus and `final_pay` grows by exactly the bonus amount).
- test_ledger: 19 passed.
- test_cp3a_bonus_events: 26 passed.
- test_cp3b1_bonus_summary: 25 passed.
- test_cp3b2a_bonus_batch_safety: 25 passed.
- test_cp3b2b_bonus_batch: 41 passed.
- test_cp2d2_status_payment: 45 passed.
- test_cp2f_prepared_operational_entry: 46 passed.
- `git diff --check`: clean.
- Alembic: no migration; head remains 0060.
- Working tree clean after commit.

**Remaining P2/P3 notes:**

- P2: The strengthened bonus-preview test asserts `period_pay`/`gross_pay` are unchanged and `final_pay` grows by exactly the bonus amount; a future pass could add an even more explicit exact-delta assertion directly on `bonus_total` alone for extra redundancy.
- P3: `test_m15.py` remains stale pre-existing CP-1C/CP-3A test debt — not caused by CP-3C and not modernized here. Two distinct, independent root causes: (1) the file's own `_open_period` helper calls the legacy `POST /payroll/periods` Draft-creation path, which current CP-1C behavior rejects with `DRAFT_CREATION_REQUIRES_OPEN`; (2) independently, some tests (e.g. via the `m15_bonus_activated` fixture) rely on generic `POST /period-pay` with `line_type: "Bonus"`, which CP-3A intentionally blocks.
- P3: Temporary PostgreSQL shutdown warning remains environment-only.
- P3: LF/CRLF working-copy warnings remain environment-only.

---

### Phase 3 closure note

**Status:** Done with Notes
**Units closed:** CP-3A, CP-3B1, CP-3B2a, CP-3B2b, CP-3C — all Done with Notes; no phase-scoped P0/P1 blocker remains.
**Commits:** `ade9234` (CP-3A), `7a34a52` (CP-3B1), `5feacf5` (CP-3B2a), `0124718` (CP-3B2b), `90a6dbe` (CP-3C).

Phase 3 (Canonical Bonus Domain and Min/Max Classification) is complete: bonus events are canonicalized as `payroll.PayrollBonusEvents` with full CRUD, a zero-inclusive summary, a create-only transactional batch with idempotency/revision safety, and a corrected min/max formula that excludes bonus from the comparison base and adds it back only after minimum/maximum is applied. Phase 4 followed that closure and has since completed CP-4A through CP-4F.

---

### Phase 1 completion note

**Status:** Done with Notes
**Completed units:** CP-1A, CP-1B, CP-1C, CP-1D, CP-1E
**Review result:** No phase-scoped P0/P1 blockers remain after CP-1E. Phase 2 is Done with Notes: CP-2A, CP-2B, CP-2C, CP-2D1, CP-2D2, CP-2E, and CP-2F are all Done with Notes. Phase 3 is Done with Notes: CP-3A, CP-3B1, CP-3B2a, CP-3B2b, and CP-3C are all Done with Notes.

**Phase 1 completed:**
- Returned domain state and reviewed transition graph (CP-1A).
- One InReview slot enforced at database and service levels (CP-1B).
- Branch-locked candidate-based period creation with deterministic replay (CP-1C).
- Atomic Open → InReview submission with Draft → Open promotion (CP-1D).
- Backend-owned workflow slots, alerts, capabilities, and reason codes (CP-1E).

### CP-1B completion note

**Status:** Done with Notes  
**Codex verdict:** PASS WITH NOTES  
**Implementation commit:** f906817  
**Review result:** No P0/P1 blockers remain.

**What CP-1B completed:**
- Added migration 0049 for one `InReview` payroll-period slot per CompanyID/BranchID.
- Added partial unique index:
  `ux_payrollperiods_oneinreviewperbranch`
- Added duplicate-InReview preflight before index creation.
- Migration fails rather than auto-repairing duplicate InReview rows.
- Downgrade drops only the CP-1B InReview index.
- Added friendly service guard for existing branch InReview periods.
- Guard applies to Open submit and Returned resubmit.
- Added narrow safe 409 translation for the exact InReview slot unique-index violation.
- Preserved Draft/Open/Returned indexes and CP-1A Returned pointer behavior.
- Did not add branch workflow lock.
- Did not implement Smart Create.
- Did not implement Draft/Prepared promotion.
- Did not implement Returned-backlog submit/create blockers.
- Replaced the now-impossible CP-1A two-InReview return-race test.
- Added deterministic Open-submit vs Returned-resubmit race coverage in both winner directions.
- Added rollback proof for:
  - calculation refresh;
  - REVIEW_ITEM_CREATED audit;
  - Pending PeriodApproval creation;
  - Returned pointer preservation;
  - Returned old review item preservation;
  - status-change audit rollback.

**Validation reported by Codex:**
- CP-1B: 12 passed.
- CP-1A: 30 passed.
- CP-1A + CP-1B: 42 passed.
- Phase 0: 114 passed.
- Lifecycle/payroll: 83 passed.
- Finalization/ledger: 70 passed.
- Security/schema: 85 passed.
- `test_codex_p0p1.py`: 18 passed.
- Previously failing combined order: 60 passed.
- Alembic: 0049 head/current; upgrade successful/no-op at current head.
- `git diff --check`: passed.

**Remaining P2/P3 notes:**
- P2: Snapshot `ManagerReviewDecisions` rows explicitly in the Returned-loser test.
- P2: `_ensure_hourly_rate` treats any overlapping approved HOURLY rate as suitable without confirming it is 25.00 and covers the actual work dates.
- P2: Prefer SQLSTATE plus driver diagnostic constraint metadata over exception-text fallback.
- P3: Clean up mojibake and the inaccurate comment claiming `_create_and_open_period` calls `_cancel_active`.
- P3: Temporary PostgreSQL/pytest-cache infrastructure warnings remain.

### CP-1D completion note

**Status:** Done with Notes
**Codex verdict:** PASS WITH NOTES
**Implementation commit:** `3e0a867` — feat: add cp-1d branch-locked submit promotion
**Review result:** No P0/P1 blockers remain. CP-1D is safely closed.

**What CP-1D completed:**
- Added branch-locked Open submit and Returned resubmit workflow hardening.
- Supports atomic Open → InReview submission with adjacent Draft/Prepared → Open promotion.
- Enforces Returned backlog blockers and workflow slot conflict handling.
- Preserves CP-1C candidate replay behavior after Draft promotion.
- Populates SubmittedAtUtc transactionally on successful submit/resubmit.
- Keeps Locked/Archived immutable.
- Keeps finalization outside CP-1D scope.
- Does not implement CP-1E Hub/capabilities/read model.
- Does not introduce PayDate behavior.
- Does not create a migration.

**Validation:**
- CP-1D: 37 passed.
- CP-1C: 72 passed.
- CP-1B + CP-1A: 42 passed.
- Phase 0: 114 passed.
- Payroll/review/finalization/ledger: 153 passed.
- Setup/branch/security: 97 passed.
- Total: 515 passed, 0 failed, 0 errors, 0 skipped.
- Alembic head/current: 0050 / 0050.
- `git diff --check`: clean.

**Remaining P2/P3 notes:**
- P2: D6 could additionally assert zero ManagerReviewDecisions and unchanged REVIEW_ITEM_DECIDED audit count explicitly.
- P2: D5 could add a direct post-response query confirming Branch B period is InReview.
- P2: Rollback hooks could inspect the in-transaction HOURS calc value before raising to prove it reached 25.00.
- P3: Temporary PostgreSQL automatic shutdown warning remains — not a CP-1D blocker.
- P3: Git global-ignore warning remains — not a CP-1D blocker.
- P3: LF/CRLF working-copy warnings remain — not a CP-1D blocker.

### CP-1C completion note

**Status:** Done with Notes
**Codex verdict:** CLEAN WITH NOTES
**Implementation commit:** `0f63787156cd01dc43c2318e26b4af1277256f51`
**Fix-forward commit:** `540ae86`
**Review result:** No P0/P1 blockers remain. CP-1C implementation and P1 fix-forward are safely closed.

**What CP-1C completed:**
- Added backend-owned candidate-based payroll-period creation.
- Replaced sequential create-next semantics with selected-candidate creation semantics.
- Added candidate preview endpoint for explicit Open and Prepared creation modes.
- Added candidate creation endpoint that accepts `candidate_key` only.
- Derived displayed candidates from `BranchPayrollSettings` / Payroll Setup.
- Prevented frontend-submitted dates, status, period type, name, or PayDate from controlling creation.
- Added deterministic signed candidate keys.
- Added idempotent replay behavior: repeating the same candidate returns the same period and never advances to the next candidate.
- Added explicit Prepared mode that creates database status `Draft`.
- Preserved `Open` and `Draft` as database statuses; did not add a `Prepared` database status.
- Added migration 0050 with `CreationCandidateKeyHash` and a partial unique index for durable candidate replay identity.
- Added branch workflow advisory lock usage for candidate creation.
- Added the same branch workflow lock to legacy `POST /payroll/periods` creation to serialize it with candidate creation.
- Added the same branch workflow lock to Payroll Setup mutation so setup updates and candidate creation cannot interleave.
- Revalidated setup fingerprints under lock so stale setup candidates are rejected.
- Kept PayDate completely outside CP-1C product/API/audit behavior.
- Added focused CP-1C candidate creation tests and setup/candidate TOCTOU fix-forward tests.

**Validation reported by Codex:**
- CP-1C: 72 passed.
- Payroll setup/payroll: 93 passed.
- CP-1A/CP-1B: 42 passed.
- Smoke validation total: 207 passed.
- Alembic head/current: 0050.
- `git diff --check`: clean.

**Remaining P2/P3 notes:**
- P2: Replace timing-only lock tests with event-based synchronization.
- P2: Add a real different-branch setup mutation lock test.
- P2: Assert no period/audit writes after stale setup rejection.
- P2: Harden connection cleanup in concurrency tests.
- P2: Add audit-failure rollback coverage.
- P2: Improve broader synchronized concurrency coverage.
- P3: Tighten the CP-1B Alembic-head test.
- P3: Retain known Windows temporary PostgreSQL / pytest-cache / global-ignore warning notes.

### CP-1A completion note

**Status:** Done with Notes  
**Codex verdict:** PASS WITH NOTES  
**Implementation commit:** a584234  
**Review result:** No P0/P1 blockers remain.

**What CP-1A completed:**
- Added pre-finalization `Returned` payroll-period status.
- Added `CurrentReturnReviewItemID` as the current return pointer.
- Added migration 0048 with:
  - Returned status CHECK support;
  - pointer consistency CHECK;
  - company/branch-safe composite FK;
  - one-Returned-per-company/branch partial unique index;
  - safe downgrade refusal while Returned data or pointers exist.
- Changed PeriodApproval Rejected/EditRequested to move `InReview -> Returned`.
- Required nonblank reason only for PeriodApproval Rejected/EditRequested.
- Kept unrelated review types unaffected.
- Blocked direct PATCH into Returned.
- Blocked manual `InReview -> Open`.
- Blocked `InReview -> Cancelled`, `Returned -> Cancelled`, and `Approved -> Cancelled`.
- Preserved `Draft -> Cancelled` and `Open -> Cancelled`.
- Added dedicated Returned resubmission endpoint:
  `POST /payroll/periods/{period_id}/resubmissions`
- Resubmission creates a new Pending PeriodApproval item and does not reuse the old returned review item.
- Resubmission clears `CurrentReturnReviewItemID` atomically.
- Made exactly Open and Returned source-mutable.
- Added deterministic one-Returned race test proving the DB partial unique index rejects the losing transaction and rolls it back fully.

**Validation reported by Codex:**
- CP-1A focused: 30 passed.
- CP-0A/B/C/D: 114 passed.
- M16 + CP6 + payroll: 83 passed.
- Finalization preview + finalize + ledger: 70 passed.
- Broad payroll/day-grid/security: 297 passed, 1 failed.
- Broad failure passed in isolation and was classified as P2 order-dependent fixture contamination, not a CP-1A blocker.
- Alembic: 0048 head/current, upgrade passed.
- `git diff --check`: passed.

**Remaining P2/P3 notes:**
- P2: Broad-suite Draft fixture contamination.
- P2: Prefer DBAPI constraint diagnostics over exception-string matching later.
- P2: Returned source-mutation family coverage remains incomplete.
- P2: Submit/resubmit guard logic is duplicated.
- P3: Router filter description still omits Returned.
- P3: Temporary database/cache warnings remain.

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

**Status:** `Done with Notes`

- [x] P1A: add Returned domain state and reviewed transition graph.
- [x] P1B: enforce one InReview slot. — **Done with Notes**
- [x] P1C: branch-locked candidate-based period creation. — **Done with Notes**
- [x] P1D: atomically promote Prepared on submit. — **Done with Notes**
- [x] P1E: return Hub-ready workflow alerts/capabilities. — **Done with Notes**

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

Pre-CP-1A direct status transitions and cleanup fixtures assumed Rejected→Open. That historical assumption was superseded by Rejected/EditRequested→Returned; any migration or test-fixture design must preserve the Returned lifecycle explicitly.

**Codex review checklist**

- [ ] State transition matrix matches this plan.
- [ ] Partial unique indexes and service rules agree.
- [ ] Branch advisory/row locking reviewed.
- [ ] All multi-period updates are one transaction.
- [ ] Review items cannot be orphaned.
- [ ] Legacy period data migration assessed.

### Phase 2 — Period Configuration and Operational Source Snapshots

**Status:** `Done with Notes`

- [x] P2A: schedule versioning. — **Done with Notes**
- [x] P2B: period-day calendar snapshots. — **Done with Notes**
- [x] P2C: snapshot pay-item layout/order/classification. — **Done with Notes**
- [x] P2D1: canonical daily status/note entry state. — **Done with Notes**
- [x] P2D2: status-driven payment lane. — **Done with Notes**
- [x] P2E: unify historical driver-date eligibility. — **Done with Notes**
- [x] P2F: enable controlled Prepared operational entry without financial exposure. — **Done with Notes**

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

### Phase 4 — Unified Calculation Core and Immutable Review Snapshot

**Status:** `Complete` — CP-4A through CP-4F are `Completed`.

- [x] P4A: extract pure/versioned daily calculation core (authoritative PerUnit only; Status-payment caller kept separate; direct/manual boundaries preserved; dormant M13c legacy methods left completely untouched and out of scope). — **Completed** (commit `9b76aa9`; see "CP-4A completion record" immediately below).
- [x] P4B: add Open/Returned calculation preview. — **Completed** (commit `f3988b7`; see "CP-4B completion record" immediately below).
- [x] P4C: add calculation snapshots and source/config hashes. — **Completed** (commit `d7f6b9e`; see "CP-4C completion record" below).
- [x] P4D: submit captures snapshot/revision. — **Completed** (commit `9e4c32f`; see "CP-4D completion record" below).
- [x] P4E: approval binds to snapshot. — **Completed** (commit `e332d02`; see "CP-4E completion record" below).
- [x] P4F: finalization consumes approved snapshot. — **Completed** (commit `434f673`; see "CP-4F completion record" below).

### CP-4A completion record

CP-4A is closed. Implementation commit: `9b76aa9` — "feat: add cp-4a perunit calculation core".

- The authoritative current PerUnit arithmetic was extracted into `backend/app/payroll/calculation/per_unit.py`.
- The production PerUnit path now delegates through this pure core from `_compute_calculated_amount` in `backend/app/payroll/service.py`; every existing live PerUnit caller continues to route through `_compute_calculated_amount` unchanged.
- The extracted core is: PerUnit-specific only; Decimal-only; deterministic; immutable/typed (frozen input/result dataclasses); infrastructure-free (no DB/SQL/HTTP/permissions/audit/workflow); explicitly versioned internally (an internal version constant, not a persisted or future `CalculatedMethod` identity).
- Exact numeric compatibility preserved: (1) resolved Decimal quantity × resolved Decimal rate; (2) full ambient Decimal-context precision; (3) no intermediate rounding; (4) one quantization to `Decimal("0.0001")`; (5) explicit `ROUND_HALF_EVEN`; (6) ambient traps remain active (not suppressed); (7) no new final `0.01` payout rounding was added.
- Float inputs are rejected at the core boundary rather than silently coerced.
- No public API response or persistence contract changed; no migration was added (Alembic head remains `0060`); no frontend change occurred; no manual Recalculate workflow was added.
- Scope unchanged by CP-4A: Status-derived payment remains a separate calculation caller (Status is not a PayItem; `PTO_STATUS` remains absent; no DAC or `StatusKeyPayRule` work was implemented); `EnteredAmount`, `Fixed`, `None`/manual compatibility, and the `Quantity × RateAmount` fallback all remain outside the PerUnit core as non-core compatibility boundaries; the dormant M13c legacy methods (`OrdinalTier`, `RangeBracket`, `RangeProgressive`, `Block`) remain completely untouched and outside Phase 4 implementation scope — no new characterization, adapter, deletion, redesign, or migration work was done for them; no future Calculated Methods framework, registry, plugin loader, formula DSL, or `CalculatedMethodID` was introduced (that design remains deliberately deferred).
- Independent review: final Codex verdict `PASS_WITH_NOTES`, no P0/P1 blockers, recommendation "commit CP-4A".
- Test evidence: CP-4A focused suite 51 passed (run successfully twice); Phase 4 Characterization Slice 1 21 passed; Phase 4 Characterization Slice 2 24 passed; rate calculation boundaries 15 passed; financial regression group 155 passed (status payment, prepared operational entry, CP-3C min/max and bonus behavior, finalization preview, ledger behavior).
- Known non-blocking environment note: the `testing.common.database: failed to shutdown the server automatically` warning observed during test runs is existing Windows/Python test-infrastructure debt, not a CP-4A calculation failure, and is not CP-4A product behavior.
- CP-4A was the first Phase 4 unit; the completed Phase 4 closure record below supersedes its then-future CP-4B through CP-4F wording.

### CP-4B completion record

CP-4B is closed. Implementation commit: `f3988b7` — "feat: add cp-4b calculation preview".

- Added the dedicated read-only `GET /payroll/periods/{period_id}/calculation-preview` contract for Open and Returned periods. Draft/Prepared remains source-only with no financial preview contract; InReview/Approved remain reserved for immutable submitted-snapshot behavior in later Phase 4 work; Locked/Archived remain final snapshot/ledger views; Cancelled has no financial preview.
- The permission contract is `payroll.view` OR `payroll.entry`; finalize-only is not an independent CP-4B path. ODA and driver-role access remain denied.
- Preview is backend-owned, source-driven, and read-only. It performs no draft refresh, Status synchronization, audit write, lifecycle transition, FinalLine write, or snapshot write.
- CP-4A's PerUnit calculation core is reused rather than reimplemented. Live canonical Status calculation remains a separate caller and uses the canonical selected Status source; genuine persisted Status payment projections are excluded from stored aggregation. `PTO_STATUS`, DAC, and `StatusKeyPayRule` remain out of scope.
- Canonical active `PayrollBonusEvents` are included and Voided events are excluded. Non-BONUS period-pay sources remain included. The calculation order remains daily + live Status + non-BONUS period pay, then minimum, then maximum, then bonus.
- Driver inclusion remains source-driven across daily, canonical Status, non-BONUS period-pay, and active bonus-event sources. An eligible driver with no financial source is not synthesized into the preview.
- Calculation incompleteness returns HTTP 200 with blockers/NeedsManagerReview-style information rather than inventing zero financial amounts. Structural findings from `_validate_period_can_finalize` are propagated without category filtering.
- Existing Approved `finalization-preview` behavior remains unchanged. No immutable snapshot, Submit capture, approval binding, finalization cutover, Add Day workflow, manual Recalculate workflow, generic calculated-method framework, or future formula/plugin DSL was introduced.
- Independent review result: `PASS_WITH_NOTES`, with no P0/P1 blockers; recommendation was to commit CP-4B.
- Test evidence: CP-4B focused suite 72 collected and 72 passed, run successfully twice; P7 5 passed; P8 5 passed; CP-4A 51 passed; Phase 4 Characterization Slice 1 21 passed; Phase 4 Characterization Slice 2 24 passed; rate boundaries 15 passed; CP-2D2 45 passed; CP-3A 26 passed; CP-3C 17 passed; finalization preview 28 passed; ledger 19 passed.
- Known non-blocking notes: route-specific `EnteredAmount` coverage and route-specific `CalculatedAmount IS NULL -> Quantity × RateAmount` fallback coverage remain P2 test gaps; both inherit unchanged lower-level behavior and were not blockers to CP-4B closure. Windows/Python `testing.postgresql` shutdown behavior may leave ephemeral PostgreSQL processes after pytest sessions; this remains environment/test-infrastructure debt, not a CP-4B lifecycle defect.
- CP-4B was an intermediate Phase 4 closure; CP-4C through CP-4F subsequently completed.

### CP-4C completion record

CP-4C is closed. Implementation commit: `d7f6b9e` — "feat: add cp-4c calculation snapshot foundation". Alembic head is now `0061`.

- CP-4C added the immutable persistence and hashing foundation only: `payroll.PayrollCalculationSnapshots` identifies one calculation revision for one payroll period and stores `CalculationVersion`, `SourceConfigHash`, `SnapshotHash`, creator/time metadata, and `TotalExpectedPay`; `payroll.PayrollCalculationDriverTotals` stores immutable per-driver financial totals; and `payroll.PayrollCalculationSnapshotLines` stores immutable normalized financial lines with source identity, quantity/rate/amount, and `SourceEvidenceJSONB`.
- Snapshot revisions use positive `RevisionNumber`, unique per `(PayrollPeriodID, RevisionNumber)`. `SourceDataRevision` was intentionally not introduced; broader source-revision semantics remain deferred. CP-4C itself did not create snapshots during normal execution; CP-4D is the first lifecycle writer.
- `current-payroll-v1` is the aggregate frozen Current Payroll calculation-packet version. It is distinct from CP-4A's PerUnit component version and does not introduce a version registry or future-method framework.
- `SourceConfigHash` represents the captured source/configuration packet and excludes calculated outputs, generated IDs, actor, timestamp, and audit metadata. `SnapshotHash` represents the complete immutable financial packet: tenant/period/revision identity, `CalculationVersion`, `SourceConfigHash`, driver totals, normalized lines, and evidence; it excludes generated surrogate IDs, actor, timestamp, and audit rows. CP-4C provides the pure deterministic canonicalization/hash utility; CP-4D constructs and persists capture packets with it.
- Canonical hashing is Decimal-safe with no float conversion; negative zero normalizes to zero; dict ordering and UTC-aware datetime encoding are deterministic; floats and naive datetimes are rejected. Driver totals and lines receive explicit semantic ordering independent of database row order, generated snapshot surrogate IDs are excluded, and duplicate identical lines preserve multiplicity deterministically.
- Snapshot financial values retain the existing `NUMERIC(18,4)` contract for relevant quantities, rates, amounts, and totals. CP-4C adds no financial rounding, final `0.01` payout rounding, PerUnit behavior change, or min/max ordering change.
- DB tenant/branch integrity is enforced: DriverTotals carry `CompanyID` and `BranchID` with composite FKs proving both snapshot and driver belong to the same tenant/branch. SnapshotLines intentionally contain neither `CompanyID`, `BranchID`, nor `SnapshotID`; they inherit ownership through the required DriverTotal relationship.
- Mutable calculation sources are retained as scalar traceability identities plus immutable `SourceEvidenceJSONB`, rather than coupled to live mutable-source FKs. Future captured evidence can explain PayItem/rate configuration, resolved DriverRate, canonical Status, bonus events, and min/max rules without changing the frozen result.
- Status remains a System Status Entry Channel, not a PayItem; `PTO_STATUS`, `StatusKeyPayRule`, and DAC were not introduced. Canonical `PayrollBonusEvents` remains the bonus source (Active contributes; Voided does not), and bonus remains after min/max. The schema supports existing `SYS_MIN_TOPUP` and `SYS_MAX_CAP` system lines without performing calculations: daily + Status + non-BONUS period pay, then minimum, then maximum, then bonus.
- Persistence is DB-enforced immutable. One reusable PostgreSQL trigger function rejects every `UPDATE` and `DELETE` on all three tables after insertion; `INSERT` remains permitted. No trigger bypass/test mode or `ON DELETE CASCADE` was added.
- No snapshot backfill occurred. Existing InReview/Approved periods receive no synthetic snapshots, Locked/Archived history is untouched, and CP-4C does not manufacture historical authority from mutable data.
- CP-4C did not implement Submit/Resubmit capture, snapshot pointers on `PayrollPeriods`, Review snapshot reads, approval binding, finalization-from-snapshot, FinalLine projection changes, public snapshot endpoints, frontend changes, manual Recalculate, DAC, `StatusKeyPayRule`, or a future calculated-method framework. CP-4D subsequently added the first normal lifecycle writer, and CP-4E/CP-4F subsequently completed review/approval binding and finalization consumption.
- Independent final review: `PASS_WITH_NOTES`, no P0/P1 blockers, recommendation `SAFE_TO_COMMIT_CP4C`. Test evidence: CP-4C focused suite 33 collected and 33 passed; relevant current regression suites totaled 588 passing tests across CP-4A, CP-4B, characterization Slices 1/2, rate boundaries, Status, bonuses, min/max, finalization preview, ledger, Returned lifecycle, Review, schedule versioning, and eligibility snapshots. This does not claim the entire repository suite is green.
- Known non-blocking debt: Windows `testing.postgresql` shutdown warnings remain environment/test-infrastructure debt. Three historical suites retain obsolete expectations independent of CP-4C: CP-1B hardcodes Alembic head `0055`; CP-2B hardcodes head `0054`; CP-2C hardcodes head `0055` and expects the retired generic Period Pay BONUS path. Their repair is not CP-4D work unless separately prioritized.
- CP-4C established the schema foundation; CP-4D through CP-4F subsequently completed capture, approval binding, and exact FinalLine projection from the approved stored snapshot.

### CP-4D completion record

CP-4D is closed. Implementation commit: `9e4c32f` — "feat: capture cp-4d submit snapshots". Alembic head is now `0062`.

- CP-4D makes Submit/Resubmit atomically capture the exact live calculation packet as an immutable snapshot revision with the transition to `InReview`: Open uses live provisional calculation, Submit creates a snapshot and linked Pending PeriodApproval, and Returned corrections/resubmit create a new revision and new linked Pending review item. Prior revisions remain immutable history.
- Migration `0062_cp4d_submit_snapshot_capture` (down revision `0061`) adds nullable `review.ManagerReviewItems.PayrollCalculationSnapshotID`, a unique non-null relationship, and a tenant-safe composite FK to the immutable snapshot with `ON DELETE RESTRICT`. There is no PayrollPeriods snapshot pointer, synthetic backfill, or CP-4E approval field; historical review rows may remain `NULL`.
- The review-item link means "this exact snapshot was submitted for this review item." CP-4E also makes it the financial packet an Approved PeriodApproval decision refers to; it is not a duplicate period-level approved-snapshot pointer.
- Snapshot capture runs in the same request transaction as validation, calculation-packet construction, immutable header/DriverTotals/SnapshotLines, both hashes, Pending review linkage, relevant snapshot/review/status audit writes, and the InReview transition. A failed transaction leaves no authoritative partial snapshot, review, or status state.
- Submit/Resubmit sets PostgreSQL `REPEATABLE READ` before service reads. Existing branch advisory and period/workflow locks plus duplicate Pending-review protection remain in force. Only SQLSTATE `40001` and `40P01` become retryable conflict responses.
- CP-4D adds no second financial engine. CP-4B and CP-4D share a private internal live-calculation packet builder: CP-4B remains the unchanged public live read-only preview, while CP-4D persists the submitted packet.
- The captured packet preserves current authority: CP-4A PerUnit calculation, canonical Status from entry state rather than `STATUS_PAYMENT` projection, non-BONUS period pay/manual boundaries, Active `PayrollBonusEvents`, and daily + Status + non-BONUS period pay, then minimum, then maximum, then bonus. No new rounding or positive-money requirement was introduced; valid zero results remain allowed. `PTO_STATUS`, legacy generic BONUS authority, DAC, `StatusKeyPayRule`, and dormant legacy calculation methods were not introduced or reopened.
- Source evidence is constructed for Daily PerUnit, canonical Status, non-BONUS period pay/manual, Active BonusEvent, `SYS_MIN_TOPUP`, and `SYS_MAX_CAP`. `SourceConfigHash` records deterministic source/config evidence; `SnapshotHash` records immutable packet semantics, with CP-4C canonicalization and no generated surrogate IDs in semantic hash identity.
- Snapshot capture writes `CALCULATION_SNAPSHOT_CAPTURED` audit metadata with the snapshot identity, revision, and hashes; it does not duplicate the full packet into AuditLog.
- No historical snapshot backfill occurs. Existing legacy InReview/Approved periods may remain without a snapshot link, Locked/Archived remain unchanged, and legacy Returned periods may enter with revision 1 without a synthetic prior revision.
- CP-4D itself did not implement approval binding, Review snapshot reads, finalization from snapshot, FinalLine projection changes, public snapshot APIs, frontend changes, manual Recalculate, or CP-4E/CP-4F behavior. CP-4E subsequently implemented the focused review/approval binding and review display; CP-4F subsequently completed finalization consumption.
- Independent review: `PASS_WITH_NOTES`, no P0/P1 blockers, recommendation `SAFE_TO_COMMIT_CP4D`. Evidence: CP-4D 27 passed; CP-4B 72 passed; CP-1D 37 passed; CP-3A 26 passed; CP-3C 17 passed; Phase 4 Slice 1 21 passed; Phase 4 Slice 2 24 passed; CP-2C 34 passed with two known stale failures; CP-1B 11 passed with one known stale failure. This does not claim the entire repository suite is green.
- Known non-blocking notes: Windows `testing.postgresql` process/shutdown exhaustion during extended regression execution; `SourceConfigHash` currently includes some explanatory derived/stored `SourceEvidenceJSONB` values; valid-zero Submit coverage uses a DailyNote-only zero-total period rather than a zero-valued financial line; some rich evidence tests construct internal capture packets rather than exercising every evidence type end-to-end; and deterministic retry seams exist while real two-connection timing coverage remains limited.
- CP-4D updated several existing tests only for immutable-history isolation: they retain snapshot-backed periods and clean mutable state without weakening restrictive FKs, deleting snapshots, disabling triggers, changing product assertions, or modifying conftest/shared fixtures.
- CP-4D established lifecycle capture; CP-4E and CP-4F subsequently completed approval binding and finalization projection from the exact approved stored snapshot.

### CP-4E completion record

CP-4E is closed. Implementation commit: `e332d02` — "feat: bind cp-4e approval to submitted snapshot". Alembic head remains `0062`; CP-4E introduced no migration.

- The authority chain is now: Open/Returned live calculation -> Submit/Resubmit -> immutable snapshot revision -> Pending PeriodApproval linked to `PayrollCalculationSnapshotID` -> snapshot-backed review -> approval of that exact linked snapshot -> Approved -> snapshot-backed finalization preview -> exact FinalLine projection -> Locked.
- `review.ManagerReviewItems.PayrollCalculationSnapshotID` remains the CP-4D submitted-packet identity and is now also the financial packet an Approved PeriodApproval decision refers to. No `PayrollPeriods.ApprovedCalculationSnapshotID`, migration `0063`, or other duplicate pointer was introduced.
- Legacy Pending PeriodApproval items with `PayrollCalculationSnapshotID IS NULL` cannot be approved: approval fails closed with `SNAPSHOT_REQUIRED_FOR_APPROVAL`. Return/EditRequested remains available; a Returned legacy period can Resubmit into CP-4D revision 1 and its new snapshot-linked Pending item follows the normal CP-4E path. No legacy snapshot backfill occurred; legacy Approved, Locked, and Archived records remain untouched.
- `GET /review/items/{item_id}/payroll-snapshot` is a review-scoped immutable read, not a generic snapshot browser. It takes only the ReviewItem identity, derives the linked snapshot server-side, enforces review permission and company/branch scope, preserves ODA/driver-role restrictions, requires a snapshot-backed PeriodApproval, and verifies the snapshot period matches the ReviewItem period.
- Reviewer-facing financial display now reads the persisted snapshot's revision/capture metadata, total expected pay, DriverTotals, and normalized lines. `ReviewDetailDialog` does not aggregate or calculate payroll totals and no longer treats live Current Payroll financial aggregates as submitted-review authority.
- Approval retains live workflow/security checks: branch advisory lock, ReviewItem `FOR UPDATE`, PayrollPeriod `FOR UPDATE`, permission and tenant/branch scope, Pending PeriodApproval state, InReview period state, and the exact ReviewItem/snapshot/period relation. It neither reruns live financial blockers nor resolves current rates, PerUnit, Status, bonuses, or min/max; it neither creates nor rehashes a snapshot. State-based retry remains safe because the first decision resolves the item and moves the period out of InReview.
- Submission-time validation remains authoritative: NeedsManagerReview, unresolved calculation, structural, canonical Status, min/max, and other submission blockers must clear before snapshot capture. Later live source drift, including a newly set `PayrollDraftLines.NeedsManagerReview`, does not redefine the submitted packet; correction is Return -> edit live source -> Resubmit -> new snapshot revision. The CP-6 expectation was updated accordingly: post-Submit live NeedsManagerReview drift does not block approval of the already captured snapshot, while pre-Submit NeedsManagerReview remains a blocker.
- Returned history is retained: a historical Returned/EditRequested ReviewItem keeps Snapshot 1; Resubmit creates Snapshot 2 and a new Pending ReviewItem; only that current Pending item may approve. Snapshot-backed Approve and Return/EditRequested preserve existing `REVIEW_ITEM_DECIDED` audit behavior enriched with concise period, snapshot, revision, and hash identity, without duplicating the full packet.
- CP-4E changed no payroll calculation code, finalization execution, FinalLines, ledger projection, finalization-preview authority, manual Recalculate, DAC, StatusKeyPayRule, or generic snapshot API. Approved remains distinct from finalized/locked.
- Implementation scope was limited to `backend/app/review/router.py`, `backend/app/review/schemas.py`, `backend/app/review/service.py`, focused CP-4E/CP-6 tests, and the focused review frontend API, types, and `ReviewDetailDialog`; no migration or payroll service change was required.
- Independent review: `PASS_WITH_NOTES`, no P0/P1 blockers, recommendation `SAFE_TO_COMMIT_CP4E`. Evidence: CP-4E 12 passed; CP-6 19 passed; Review 32 passed; Returned lifecycle 30 passed; CP-1D 37 passed; CP-4B 72 passed; CP-4C 33 passed; CP-4D 27 passed; finalization preview 28 passed; ledger 19 passed; CP-0C transition permissions 10 passed; branch access 11 passed; and permission catalog 15 passed. Frontend `npm run build` passed; `npm run lint` passed with one pre-existing DashboardPage exhaustive-deps warning. This does not claim the entire repository suite is green.
- Known non-blocking debt: `test_security_matrix.py` retains a stale generic BONUS period-pay expectation (31 passed / 1 stale failure); `test_cp1b_inreview_slot.py` retains its old Alembic-head `0055` expectation (11 passed / 1 stale failure); Windows `testing.postgresql` shutdown behavior remains environment debt; the DashboardPage lint warning and Vite large-chunk warning are pre-existing; source/config hash semantic clarity, valid-zero coverage shape, constructed-packet evidence coverage, and real two-connection timing coverage remain P2 follow-up.

### CP-4F completion record

CP-4F is closed. Implementation commit: `434f673` — "feat: finalize payroll from approved snapshot". Alembic head remains `0062`; CP-4F added no migration `0063`, no `PayrollPeriods` approved-snapshot pointer, no `FinalizedFromSnapshotID`, and no generic snapshot-schema expansion.

- Phase 4's completed financial authority chain is: Open/Returned -> live calculation; Submit/Resubmit -> immutable `PayrollCalculationSnapshot` revision; InReview -> submitted ReviewItem-linked snapshot; Approved -> Approved PeriodApproval ReviewItem-linked snapshot; finalization preview -> that exact approved snapshot; Finalize -> deterministic projection of that exact snapshot into FinalLines; Locked/Archived -> FinalLines and ledger as operational history with the approved snapshot retained as their financial origin/provenance.
- CP-4F guarantees that the money written into FinalLines and locked payroll history is the exact immutable financial packet the reviewer approved. Finalization no longer financially re-derives an approved payroll from mutable live data.
- Finalization requires exactly one Approved `PeriodApproval` ReviewItem for the target period with matching entity, company, branch, non-null `PayrollCalculationSnapshotID`, and a linked snapshot whose `PayrollPeriodID` matches. Missing, ambiguous, or corrupt identity fails closed; it does not choose a latest snapshot or use a duplicate period-level pointer.
- A legacy Approved PeriodApproval with `PayrollCalculationSnapshotID IS NULL` fails closed with `SNAPSHOT_REQUIRED_FOR_FINALIZATION`. There is no live-calculation fallback, synthetic snapshot, backfill, migration remediation, or new Approved-to-Returned correction path. Existing Locked/Archived history is untouched.
- Snapshot-backed Approved preview/finalization does not financially rerun DraftLines, current DriverRates/rate resolution, PerUnit, Status resolution, current BonusEvents, NeedsManagerReview, min/max, CP-4B live calculation, or Submit-time financial blockers. It structurally reconciles persisted snapshot header, DriverTotals, SnapshotLines, and review/period/tenant identity without recomputing `SnapshotHash`, then projects persisted values into FinalLines.
- SnapshotLines are the detailed projection authority; DriverTotals and the header provide stored per-driver and period-total reconciliation. Supported lines include daily PerUnit/DraftLine-backed payment, canonical Status, non-BONUS period/manual pay, Active BonusEvent, `SYS_MIN_TOPUP`, and `SYS_MAX_CAP`. Amount, applicable quantity/rate, date/scope, source/pay-item metadata, and captured evidence/provenance are projected without live financial re-resolution.
- Existing `NUMERIC(18,4)` and Decimal semantics are preserved for snapshot values and FinalLines. CP-4F adds no float arithmetic, 2dp payout rounding, or gratuitous requantization. The CP-3A assertion adjustment is representation-only Decimal comparison, not a changed rounding contract.
- FinalLine provenance records concise immutable origin including snapshot ID, revision, persisted hash, snapshot line/source identity, and captured evidence where applicable. `PAYROLL_FINALIZED` audit metadata identifies the period, Approved ReviewItem, snapshot, revision, and persisted hash; the packet/hash is not recomputed or duplicated in audit. Finalize neither updates, deletes, creates, revises, nor rehashes a snapshot.
- Finalization runs atomically in the normal request transaction with existing branch/workflow and period locking, Approved ReviewItem lookup/locking, workflow/permission/tenant validation, packet reconciliation, projection, audit, and Approved->Locked transition. No CP-4D-style `REPEATABLE READ` was added. A successful finalization leaves the period no longer Approved, blocking a second projection; focused tests also prove a failure rolls back partial FinalLines, status transition, and success audit/state.
- Finalization-preview now reads the same approved immutable packet Finalize consumes: review packet X -> Approved preview X -> Finalize X. Post-approval live drift cannot change either result. Open/Returned remain CP-4B live-calculation states; CP-4F does not freeze them before successful Submit/Resubmit.
- The minimal public/frontend compatibility change makes finalization-preview `draft_line_id` nullable and supplies a stable backend `source_key`, so Status/system/non-DraftLine snapshot rows do not receive invented DraftLine IDs. `FinalizationPreviewDialog` and payroll types render, group, and format this data; they do not calculate totals or choose snapshot authority.
- Older tests that encoded Approved live-recalculation behavior were classified and migrated rather than disabled: Approved asserts frozen snapshot authority; Open/Returned retains live authority; workflow/security/integrity remains live. CP-2D2 test cleanup now retains immutable history, and legacy direct-Approved/no-snapshot finalization fails closed. Canonical Status remains EntryState authority before Submit and the captured Status line becomes financial authority after Submit/Approve.
- Implementation scope: `backend/app/payroll/schemas.py`, `backend/app/payroll/service.py`, `backend/tests/test_cp2d2_status_payment.py`, `backend/tests/test_cp3a_bonus_events.py`, `backend/tests/test_cp4f_finalization_snapshot_projection.py`, `backend/tests/test_finalization_preview.py`, `backend/tests/test_payroll_trust_p7.py`, `backend/tests/test_payroll_trust_p8.py`, `backend/tests/test_rate_calc_boundaries.py`, `frontend/src/pages/payroll/FinalizationPreviewDialog.tsx`, and `frontend/src/types/payroll.ts`. No migration, router, conftest, or shared fixture changed.
- Independent review: `PASS_WITH_NOTES`, no P0/P1 blockers, recommendation `SAFE_TO_COMMIT_CP4F`. Evidence: CP-4F focused 8 passed; finalization preview 28; rate boundaries 15; P7 5; P8 5; CP-3A 26; CP-3C 17; CP-2D2 Status 45; CP-4B 72; CP-4C 33; CP-4D 27; CP-4E 12; Review 32; CP-6 19; Returned lifecycle 30; CP-1D 37; ledger 19; branch access 11; permission catalog 15; transition permissions 10. Frontend build passed; lint passed with the pre-existing DashboardPage exhaustive-deps warning. Security matrix remained 31 passed / 1 known stale generic BONUS expectation. This does not claim the full repository suite is green.
- Non-blocking P2/environment debt remains: the stale security-matrix generic BONUS expectation; Windows `testing.postgresql` shutdown warnings; the existing DashboardPage lint warning; the Vite large-chunk warning; and non-runtime stale wording in router/finalization-preview/P8 comments. These are not Phase 4 correctness blockers.

**Phase 4 closure:** CP-4A calculation core, CP-4B Open/Returned live preview, CP-4C immutable snapshot/hash foundation, CP-4D Submit/Resubmit capture, CP-4E snapshot-backed review/approval, and CP-4F snapshot-backed preview/finalization are complete. The final invariant is: **Calculate -> Freeze -> Review -> Approve -> Finalize uses one immutable financial packet after Submit.**

Phase 4 completion does not close the broader backend/product backlog. Transition concurrency hardening, Off-day/Add Day activation, future Status pay-rule/usage-limit work, audit append-only/completeness, Current Payroll Hub/report contracts, client-supplied standard-date validation, SemiMonthly cadence, broader tenant integrity, finalized-library metadata, and broader frontend alignment remain separately prioritized roadmap work. The next direction is end-to-end Current Payroll product alignment and hardening according to the existing roadmap, not another calculation/snapshot abstraction phase; no follow-on implementation is started here.

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
- each characterization group in the approved CP-4A scope (authoritative PerUnit core, Status-payment caller, direct/manual boundaries) — dormant M13c legacy methods (`OrdinalTier`, `RangeBracket`, `RangeProgressive`, `Block`) are out of CP-4A scope and require no further characterization (see "Phase 4 calculation compatibility characterization gate");
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
- CP-4F finalization consumes approved snapshot identity;
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

### Phase 4 calculation-authority classification

Not every existing `RateBehavior` implementation becomes a first-class method inside the new CP-4A calculation core. The authoritative classification is:

- **A. Current authoritative daily calculation core:** `PerUnit`.
- **B. Separate current derived calculation caller:** Status-derived payment (a system calculation lane, not a `RateBehavior` method).
- **C. Current direct/manual financial boundaries outside the daily method core:** `EnteredAmount`; Fixed/manual/fallback behavior.
- **D. Dormant M13c legacy methods (out of CP-4A scope):** `OrdinalTier`, `RangeBracket`, `RangeProgressive`, `Block`. Product decision: these remain dormant legacy code, completely untouched by CP-4A — no extraction, no adapter, no caller cutover, no additional characterization, no deletion, no redesign. Their existence must not block CP-4A. Their possible removal or cleanup is a later, separately approved task. This is not a today deletion or deprecation decision.
- **E. Future-only reserved behavior/domain:** `Calculated` (schema-legal but explicitly rejected downstream — reserved for a future automated calculation engine); future Calculated Methods; future `StatusKeyPayRule`.
- **F. Non-core/retired compatibility value:** `None` — preserve safely if encountered, but do not promote it into the new core or the future method model.

Current `RateBehavior` values do not equal future `CalculatedMethod` identities. Classification here governs CP-4A scope only; it deprecates nothing and removes nothing.

**Operational CDPI boundary (current state):**

- CDPI operationally implements `PerUnit` only.
- Advanced registered keys (`OrdinalTier`, `Block`, `RangeBracket`, `RangeProgressive`) may exist as placeholders/adapters in the CDPI registry, but they are not approved or operational Calculated Methods.
- Modern custom Daily PayItem creation is restricted to `PerUnit`.
- `OrdinalTier`, `RangeBracket`, `RangeProgressive`, and `Block` are not supported for new modern creation.
- Their old M13c algorithms remain dormant legacy code for historical/pre-existing rows; CP-4A leaves them completely untouched (see classification D above).
- Existing code presence and test coverage do not promote them into the future method domain.
- This docs classification does not remove or deprecate the legacy algorithms today.

**CP-4A implementation boundary:**

1. Extract `PerUnit` into the authoritative pure daily calculation core.
2. Preserve Decimal-only numeric compatibility.
3. Keep Status payment as a separate current derived calculation caller, not a `RateBehavior` method.
4. Preserve `EnteredAmount` and Fixed/manual contracts as direct/non-core boundaries.
5. Leave the dormant M13c legacy methods (`OrdinalTier`/`RangeBracket`/`RangeProgressive`/`Block`) completely untouched: no extraction into the calculation core, no new legacy compatibility adapter, no caller cutover, no additional characterization Slice, no deletion, no redesign. Their existence must not block CP-4A; their possible removal or cleanup is a later, separately approved task.
6. Keep `Calculated` out of scope as future automated calculation work.
7. Do not create `CalculatedMethodID`.
8. Do not introduce a formula DSL.
9. Do not introduce speculative method dependency ordering.
10. Do not add new final payable 2dp rounding.
11. Require exact Decimal and result-contract parity before caller cutover.

**Pure-core exclusions.** The pure PerUnit core accepts already-resolved typed Decimal inputs and returns a deterministic typed result. It contains no database/SQL access, permissions, audit, persistence, workflow, HTTP/request handling, or dry_run logic. Specifically, it contains no:

- database access;
- SQL;
- permissions;
- audit writes;
- persistence;
- workflow/status transitions;
- HTTP/request handling;
- dry_run branching.

### Phase 4 calculation compatibility characterization gate

**Status:** Prerequisite — not itself a lifecycle feature. **Closed for the approved CP-4A scope** (see "Characterization gate closure" below); closing this prerequisite did not by itself mark Phase 4 or CP-4A implementation as started. Production CP-4A implementation has since completed (commit `9b76aa9`), followed by CP-4B (commit `f3988b7`), CP-4C (commit `d7f6b9e`), CP-4D (commit `9e4c32f`), CP-4E (commit `e332d02`), and CP-4F finalization projection (commit `434f673`); Phase 4 overall is `Complete`.

Before CP-4A production extraction or any caller cutover:

- Focused characterization tests must lock the current numeric and compatibility behavior of each characterization group below (authoritative core, Status-payment caller, direct/manual boundaries). Characterizing a path locks its current behavior; it does not promote that path into the new core.
- **No production calculation behavior changes as a result of this gate.** Characterization tests observe and pin down current behavior; they do not alter it.
- Current `RateBehavior` values (`PerUnit`, `EnteredAmount`, `Fixed`, `OrdinalTier`, `RangeBracket`, `RangeProgressive`, `Block`, `None`) are **existing calculation paths**, not approved future `CalculatedMethod` identities. Future Calculated Methods remain a separate product/domain concept whose relationship to PayItem, RateBehavior, rate slots, source inputs, and method configuration is designed deliberately later — not implied by this gate.
- CP-4A is a **behavior-preserving extraction**, not a rewrite or a cleanup.
- Caller cutover (any live code path switching from the current calculation logic to the extracted core) is blocked until old-vs-new exact Decimal and result-contract parity is proven by the characterization suite.
- The dormant M13c legacy methods (`OrdinalTier`, `RangeBracket`, `RangeProgressive`, `Block`) are **out of the approved CP-4A scope** and are not required by this gate — see classification D above and "Characterization gate closure" below. This gate does not claim every possible calculation path in the codebase is characterized; it states the gate is closed for the approved authoritative CP-4A scope specifically.

**Required characterization areas:**

*Authoritative CP-4A core characterization:*

- PerUnit 4dp monetary rounding (single multiply, quantize once to `Decimal("0.0001")`).
- Exact preview/finalization/ledger Decimal and result-contract parity (not merely equal after display rounding).
- The status-derived payment integration boundary where applicable.
- Min/max comparisons and adjustment deltas at 4dp (no explicit rounding on the delta today).
- The 4dp normal total plus 2dp bonus total combination in `final_pay` (no reconciling rounding step exists today).
- Actual HTTP/JSON Decimal serialization behavior (4dp for calculation fields, 2dp for bonus fields — an existing, intentional asymmetry, not a bug to characterize away).
- The Decimal-only calculation-core boundary (no float conversion anywhere in the arithmetic path).

*Separate current calculation-caller characterization (Status payment):*

- Status payment as a derived system calculation: canonical StatusKey selection → resolved paid hours (current `HoursValue` compatibility) → resolved status rate → derived monetary result (`HoursValue × resolved rate`, quantized once to 4dp).

*Direct/manual compatibility characterization:*

- EnteredAmount compatibility (no quantization applied — the user-supplied amount passes through verbatim).
- Fixed/None/manual compatibility (Fixed and None are dispatched by the current compatibility path but currently return no computed amount; their downstream fallback/manual behavior must be characterized separately).
- The legacy/manual fallback: `CalculatedAmount IS NULL -> Quantity * RateAmount`, computed by the live calculation/preview COALESCE pattern before snapshot capture rather than by the rate-behavior dispatch.
- PostgreSQL `NUMERIC(18,4)` coercion behavior at insert time.

**Dormant M13c legacy methods are excluded from this gate.** `OrdinalTier`, `RangeBracket`, `RangeProgressive`, and `Block` are not characterized further by CP-4A and are not a prerequisite for closing this gate — they remain dormant legacy code outside the current Phase 4 implementation scope (see classification D above and the CP-4A implementation boundary). No characterization Slice targeting these four methods is required or planned as part of CP-4A.

**Characterization gate closure.**

The characterization prerequisite for the approved CP-4A scope (authoritative PerUnit core, Status-payment caller, direct/manual boundaries) is closed by two completed characterization Slices:

- **Characterization Slice 1** (commit `46d71d2`): PerUnit numeric behavior; Decimal context and rounding; EnteredAmount; Fixed/None; manual/fallback preview-vs-finalization behavior; PostgreSQL `NUMERIC(18,4)` coercion; HTTP Decimal serialization.
- **Characterization Slice 2** (commit `5ee38c2`): canonical Status source; status-derived payment identity and formula; rate resolution; missing-rate behavior; status payment min/max participation; current preview/finalization divergence; `PTO_STATUS` absence; Status not being a PayItem; DAC/`StatusKeyPayRule` boundaries.

No characterization Slice 3 is required. This closed the characterization prerequisite for the approved CP-4A scope only — it does not claim every possible calculation path in the codebase is characterized. CP-4A implementation has since been completed (commit `9b76aa9`; see "CP-4A completion record" above).

### CP-4A numeric compatibility contract

Applies to CP-4A (P4A) only. This is a compatibility contract, not a redesign — it does not claim every current path follows the same quantization rule, and it explicitly preserves the known fallback caveat below.

1. CP-4A is a behavior-preserving extraction.
2. Arithmetic in the pure core uses `Decimal` only.
3. Float calculation inputs and outputs are prohibited in the core.
4. Current formula intermediate precision is preserved (no new intermediate rounding is introduced anywhere it doesn't already exist).
5. Current computed-line behavior quantizes a completed calculated line **once**, to `Decimal("0.0001")` — this is the existing per-line convention, not a new one.
6. Current compatibility rounding is `ROUND_HALF_EVEN` (Python's implicit `decimal` module default, never previously set explicitly anywhere in the codebase). CP-4A should make this mode explicit in the extracted core rather than continuing to rely on Python's implicit context default.
7. Decimal context precision/traps must be treated as compatibility inputs and locked by characterization tests before extraction, not assumed.
8. The dormant M13c legacy methods (`OrdinalTier`, `RangeBracket`, `RangeProgressive`, `Block`) are out of the approved CP-4A scope and this contract does not apply to them — they are left completely untouched, per the CP-4A implementation boundary and classification D above. This contract makes no claim about their rounding behavior.
9. Min/max uses the existing 4dp calculated-line amounts as its comparison base; the adjustment delta itself is not currently quantized, and CP-4A must not introduce a new quantization step there.
10. Canonical bonus remains a 2dp amount, added after min/max, per the CP-3C-corrected order.
11. **CP-4A adds no new final-payout `0.01` quantization.** Whether a final-payout rounding step should ever exist is a deferred product decision (see below), not something CP-4A decides unilaterally.
12. EnteredAmount, Fixed, None/manual, and the legacy fallback path must preserve their actual current behavior exactly and must **not** be forced through a universal quantization step they don't currently pass through.
13. Current `RateBehavior` values must not be declared or persisted as future `CalculatedMethod` identities as part of CP-4A.
14. No live caller may cut over from the current calculation logic to the extracted core until exact old-vs-new Decimal and result-contract parity is proven by the characterization suite.

**Known fallback caveat (must be characterized, not resolved, by CP-4A):** preview currently computes `Quantity * RateAmount` in Python when `CalculatedAmount IS NULL`; finalization writes the equivalent SQL product directly into a `NUMERIC(18,4)` column. This path can diverge *before* PostgreSQL's column-scale coercion applies, whenever the raw product carries more than four decimal places, because the Python-side and SQL-side computations are two independent expressions of the same fallback, not one shared code path. Characterization tests must lock this path's current behavior before CP-4A decides how its non-core compatibility boundary represents it — this fallback remains outside the authoritative PerUnit core and is not resolved by this contract.

### Phase 4 decisions safely deferred

These are intentionally deferred and do not block CP-4A:

**A. Final payable-total rounding to 2 decimals.**
Current rule: authoritative financial calculations remain at the current 4dp behavior; CP-4A added no payout-rounding step and CP-4C added no rounding behavior. A later finalization decision is needed on whether future payable reconciliation uses (a) presentation/export formatting only, (b) separate unrounded and payable totals, or (c) a typed `SYS_ROUNDING_ADJUSTMENT` line. **No option is approved yet.**

**B. Future Calculated Methods domain.**
Current rule during CP-4A: only the current PerUnit calculation path is extracted into the authoritative pure calculation core. EnteredAmount, Fixed/None/manual, and the `CalculatedAmount`-null `Quantity × RateAmount` fallback remain non-core compatibility boundaries. `OrdinalTier`, `RangeBracket`, `RangeProgressive`, and `Block` remain untouched outside CP-4A: no extraction, adapter, caller cutover, additional characterization, deletion, or redesign. Do not create a `CalculatedMethodID`; do not equate `RateBehavior` with future Calculated Methods; do not introduce a formula DSL; do not introduce method dependency ordering speculatively. This domain remains a separate, later, deliberate design exercise.

### Phase 4 Status payment boundary

Alignment with the accepted future architecture (`DRIVER_ALLOWANCE_TRACKING_FUTURE_PLAN.md` — future-only, read-only reference):

- Status is a System Status Entry Channel, not a PayItem.
- There is one Status column and one canonical selected StatusKey per driver/day.
- Current canonical status input is the driver/day entry-state record, not a monetary PayItem.
- Current status-derived payment is a separate financial calculation lane (a derived system calculation caller, not a `RateBehavior` method).
- The compatibility `DailyStatus`/derived draft-line representation must not become the future source of truth.
- Future `StatusKeyPayRule` is a separate backend rule domain.
- Future `StatusKeyPayRule` is not automatically a generic Calculated Method.
- Future Status payment may resolve: paid-hours policy; no-payment policy; rate type/rate slot; effective date; rule revision.
- Status allowance deduction remains a completely separate future DAC lane.
- Paid payroll hours do not necessarily equal deducted allowance hours.
- AllowanceCategory is never a payroll-rate source.

Conceptual split:

```
StatusKey
→ optional payroll-payment effect through future StatusKeyPayRule
→ optional allowance-deduction effect through future StatusKeyAllowanceRules
```

The two effects are independent. `StatusKeyAllowanceRules` is future DAC architecture and is not implemented by Phase 4.

**Current-state clarifications relative to the accepted DAC future plan.** The DAC plan is future-only and contains historical "current system" findings from before later Current Payroll work; the newer current state is:

- Status selection is canonical through the driver/day entry-state model.
- `DailyStatus` draft-line representation is compatibility output, not the future authoritative source.
- Current Status payment exists as a derived system calculation.
- `PTO_STATUS` has been removed (commit `236a506`) and must not return.
- Status payment must not be modeled as a user-configurable Status PayItem.
- Future `StatusKeyPayRule` remains unimplemented.
- DAC allowance categories, entitlements, usage ledger, and adjustment flow remain future-only and must not be started in Phase 4.

**Status-payment snapshot implications (architecture note only — nothing implemented here).** Phase 4 must not preclude the future correct snapshot model. For status-derived payment, future submitted snapshots may need to freeze by value:

- StatusKeyID;
- StatusCode/name snapshot;
- the canonical selected status fact;
- paid-hours rule or the current compatibility `HoursValue`;
- effective `StatusKeyPayRule` revision when implemented;
- resolved RateType/rate-slot identity;
- resolved DriverRate identity and amount;
- WorkDate/effective-date context;
- calculation version;
- derived amount and classification.

For future allowance behavior, separate snapshots/ledger metadata may include: allowance-rule revision; AllowanceCategory; deduction-hours rule; resolved deduction amount. Payroll-payment data and allowance-ledger data must not be combined.

### Phase 5 — Current Payroll Hub and Calculation Reports

**Status:** `Complete`

- [x] P5A: Current Payroll Hub aggregate — completed by CP-5A (`5d05756`).
- [x] P5B: fully-off driver KPI — completed by CP-5B (`9ecb300`).
- [x] P5C: selected-day off-driver contract — completed by CP-5B (`9ecb300`).
- [x] P5D: Drivers Report — completed by CP-5C (`23bf68f`).
- [x] P5E: Period Work/Pay/Mixed views — completed by CP-5C (`23bf68f`).
- [x] P5F: report capabilities/read-only reasons — completed by CP-5C (`23bf68f`).

#### Post-Phase-4 frontend/core alignment and RP-1 foundation

The completed frontend/product alignment slices below are not separate backend roadmap phases. They align the frontend with the completed Phase 0–4 backend foundation:

- FE-1: `61ad686dde96e2b22c9b9602d369b4ce9c7ae4e5` — `fix: align current payroll lifecycle ui`
- FE-2: `984306433443ed169d8cd3a8c9194a2d7a24e5d2` — `fix: align bonus and pay item frontend contracts`
- FE-3: `500fce3e6d76cc8c980a36eb6c3cc0ebd3db4954` — `feat: align payroll hub with workflow candidates`
- FE-4: `123ce6613bc263dc7ce078f5b6bcbd0c97fa0ff0` — `feat: add live payroll calculation preview`
- FE-5: `7995e465cf3e381a7c343679ab9da1b2f77694f1` — `feat: align payroll review with snapshot history`
- FE-6: `135487a2586e872b6047e2d9a2404f7c0f1997b7` — `fix: align finalization and locked payroll history`

The final audit was `CORE_WORKFLOW_PASS_WITH_NOTES` with no P0 or P1 findings and the closure verdict `SAFE_TO_CLOSE_CURRENT_PAYROLL_CORE_WORKFLOW`.

Validated core journey:

```text
Create -> Prepared/Open -> Source Entry -> Live Expected Payroll -> Submit -> InReview
-> Review -> Return for Correction when needed -> Correction -> Resubmit -> Approve
-> Finalization Preview -> Finalize -> Locked -> Locked/Archived History
```

Financial authority is lifecycle-bound: Draft/Prepared is source-only; Open/Returned uses live backend calculation; InReview uses the exact submitted ReviewItem-linked immutable snapshot; Approved uses the exact approved ReviewItem-linked immutable snapshot; Locked/Archived uses FinalLines. No frontend financial calculation authority exists.

**RP-1 Report Financial Authority Resolver — Complete.** Commit `187d552e00ff26f2bd55015c8072a861e93cca7e` (`feat: add payroll report authority resolver`) added `backend/app/payroll/reporting.py` and `backend/tests/test_report_authority_resolver.py`. It is an internal Phase 5 reporting foundation reused by the CP-5C report bundle under official P5D/P5E; it is not a new official phase.

RP-1 mapping is `Draft -> SOURCE_ONLY`, `Open/Returned -> LIVE`, `InReview -> SUBMITTED_SNAPSHOT`, `Approved -> APPROVED_SNAPSHOT`, `Locked/Archived -> FINAL_LINES`, and `Cancelled -> UNAVAILABLE`. InReview and Approved resolve the exact ReviewItem-linked snapshot, never a latest snapshot. RP-1 adds no financial calculation, report rows, public endpoint, or migration. Independent review: `PASS_WITH_NOTES`, P0 none, P1 none, `SAFE_TO_COMMIT_RP1`. Evidence: RP-1 13 passed; CP-4E regression 12 passed; CP-4F regression 8 passed; Ruff and compile/import passed. P2: an explicit InReview ambiguity test remains missing, though the shared cardinality path fails closed.

**CP-5A Current Payroll Hub — Complete.** Commit `5d05756` (`feat: add current payroll hub`) added `backend/app/payroll/current_hub.py`, `backend/app/payroll/router.py`, `backend/app/payroll/schemas.py`, and `backend/tests/test_cp5a_current_hub.py`. It implements `GET /payroll/current` with optional `branch_id`: a backend-owned company/branch-scoped aggregate for Open, Prepared/Draft, InReview, and Returned slots. It reuses CP-1E alerts/capabilities; returns total eligible and Working Drivers metrics; returns compact live CP-4B expected-income summaries, blockers/warnings, and top drivers only for Open/Returned; gives Prepared no financial authority; and does not use live calculation for InReview. It adds no frontend, migration, or report implementation, and leaves `/payroll/current-workflow` compatible.

Working Drivers is explicitly distinct operational source truth, not expected-pay totals or Daily Grid membership: a distinct period-eligible driver qualifies with one non-zero normal daily work/activity source inside the period and within the applicable CP-2E snapshot or legacy driver-date eligibility window. Status-only, Bonus-only, system/period-level financial-only, and eligible-with-no-data cases are excluded. An eligible driver remains visible in the Daily Grid even with no work data.

Independent CP-5A review after the pre-commit fix was `PASS_WITH_NOTES`, P0 none, P1 none, `SAFE_TO_COMMIT_CP5A`. The initial P1 allowing out-of-period or driver-date-ineligible daily rows to count as Working Drivers was fixed before commit and is not unresolved debt. Focused evidence: CP-5A 17 passed. Relevant regressions: CP-1E 43 passed; CP-4B 72 passed; CP-2E 75 passed with existing warnings only; Returned lifecycle 30 passed; branch access plus permission catalog 26 passed; RP-1 13 passed; Ruff and compile/import passed. P2: existing Windows `testing.postgresql` automatic-shutdown warning, and future Hub growth should watch multi-branch/slot-by-slot live-calculation cost; neither blocks Phase 5.

**CP-5B Off-driver contracts — Complete.** Commit `9ecb300` (`feat: add off-driver payroll contracts`) added `backend/app/payroll/off_drivers.py`, integrated the shared Fully-Off resolver with `backend/app/payroll/current_hub.py`, updated payroll router/schemas, added `backend/tests/test_cp5b_off_drivers.py`, and adjusted the CP-5A regression test. It implements the separate fully-off KPI and selected-day off-driver read contracts. Canonical Status, the period calendar, scheduled work-day denominator, CP-2E/legacy driver-date eligibility, and distinct-driver counting are authoritative. A driver is Fully-Off only when every eligible scheduled work day is Off; missing/non-Off Status or normal work disqualifies, while Status-payment-only, Bonus-only, and system/min/max or period-financial-only data do not. Unactivated configured off days are excluded and already-activated added days are respected without implementing Add Day mutation. The Hub reuses this resolver; it does not duplicate financial calculation.

Independent CP-5B review was `PASS_WITH_NOTES`, P0 none, P1 none, `SAFE_TO_COMMIT_CP5B`. Evidence: CP-5B 12 passed; CP-5A 17 passed; CP-2D2 45 passed; CP-2E 75 passed with existing warnings only; branch access plus permission catalog 26 passed; Ruff and compile/import passed; Alembic remains `0062`. P2: direct new-endpoint ODA/foreign-period coverage remains to be added despite reuse of canonical guards; low-risk PayItem snapshot/live metadata fallback hardening remains for impossible/corrupt post-CP-2C rows; pre-existing stale-test/environment debt remains outside CP-5B. These do not block Phase 5.

**CP-5C frozen report-evidence remedy closure:** The dedicated immutable Status/Bonus evidence tables and `ReportEvidenceVersion`/`ReportEvidenceHash` markers are implemented by `1a106b8` (migration `0063`). The requirement applies to new snapshots; legacy snapshots retain explicit unavailable evidence markers with no synthetic historical truth. Product decisions and persistence architecture are resolved, and the persistence blocker is closed.

**CP-5C Calculation Report Bundle — Complete.** Commit `23bf68f` (`feat: add current payroll calculation reports`) implements Drivers Report, Period Work, Period Pay, and Mixed under official P5D/P5E, reusing RP-1 rather than recreating lifecycle authority. It is independently reviewed `PASS_WITH_NOTES` with P0 none, P1 none, and `SAFE_TO_COMMIT_CP5C`.

**Formal Phase 5 closure — Complete.** CP-5A, CP-5B, and CP-5C are complete; the frozen report-evidence blocker is closed by `1a106b8` (Alembic `0063`), and CP-5C implementation and documentation are recorded by `23bf68f` and `b194f6d`. Their independent reviews record no Phase 5 P0 or P1 blocker. Accepted Phase 5 P2/P3 and environment debt remain non-blocking, including `AppearsInReports` row/totals visibility semantics, Prepared live-Status report coverage, CP-5A/CP-5B same-process isolation, CP-2C stale expectations, CP-2E asyncio warnings, and Windows `testing.postgresql` shutdown warnings. Backend-owned Hub, off-driver, and calculation-report contracts are complete. Phase 6 is the next implementation direction and remains Pending; no Phase 6 work is implied or started here.

**Objective**

Expose complete backend-owned workflow and financial read contracts without frontend aggregation.

CP-1E current-workflow, FE-3 workflow slots, and FE-4 live calculation preview were useful foundations. CP-5A closes the trusted backend-owned aggregate for its implemented workflow slots, metrics, expected-income information where allowed, blockers/warnings, and capabilities/reason codes. CP-5B closes the distinct fully-off KPI and selected-day detail; CP-5C closes the calculation-report bundle.

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

- [x] Every metric definition traced to backend query/calculation.
- [x] Scope and permission filtering reviewed per branch.
- [x] No hidden frontend dependency required.
- [x] Frozen statuses read snapshots.
- [x] Performance query plan reviewed for realistic branch counts; ongoing multi-branch cost monitoring remains non-blocking P2 debt.

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
| CP-2D1 | Canonical daily status/note entry state | payroll/settings, focused migration/backfill, tests | new pseudo status Pay Items or silent guesses | deactivation/rename/history/duplicate/race tests | StatusKeyID plus snapshots; no new pseudo status lines |
| CP-2D2 | Status-driven payment lane | payroll/settings service, focused migration, tests | DAC/allowance, PTO_STATUS revival, frontend | status rate columns, derived lines, finalization preservation | System-derived status payment from HoursValue × DriverRate; SourceSnapshot preserved |
| CP-2E | Canonical eligibility service | payroll/core eligibility paths and tests | frontend filtering or unrelated employee redesign | hire/termination/transfer/current-status history matrix | All endpoints agree on driver-date eligibility and history |
| CP-2F | Controlled Prepared pre-entry | payroll schemas/service and tests | bonus, expected income, submit/finalize for Draft | source save/read matrix and financial-field absence | Operational saves allowed; no financial exposure or actions |
| CP-3A | Canonical bonus-event migration — **Done with Notes** (`ade9234`) | payroll bonus model, focused migration, tests | dual writes, zero events, Prepared bonus | legacy migration, multiple events, actor metadata | One source, multiple events, historical migration reconciled |
| CP-3B1 | Zero-inclusive bonus summary — **Done with Notes** (`7a34a52`) | payroll service/router/schemas and tests | frontend aggregation, batch, idempotency, revision | zero-inclusive roster, branch-scoped aggregation, per-driver capabilities | Every eligible driver appears with zero-inclusive totals; no cross-branch contamination; capabilities match single-event guards |
| CP-3B2a | Bonus batch safety foundation — **Done with Notes** (`5feacf5`) | migration, payroll service/schemas, tests | batch endpoint, batch writer, idempotent replay, frontend | DB ownership hardening, revision bump/atomicity, downgrade guards | `BonusDataRevision`/`PayrollBonusBatchRequests`/ownership trigger exist; no batch endpoint at CP-3B2a completion — the endpoint was added later by CP-3B2b (see that row below) |
| CP-3B2b | Create-only transactional batch endpoint — **Done with Notes** (`0124718`) | payroll service/router/schemas, migration, tests | frontend aggregation, update-batch, void-batch, reconciliation | idempotency, batch correlation, revision/concurrency, rollback | Revision-safe, all-or-nothing batch behavior — implemented and tested |
| CP-3C | Financial classification and min/max fix — **Done with Notes** (`90a6dbe`) | payroll/pay-item/calculation paths and tests | hardcoded UI totals or bonus opt-in to min/max | min/max boundary cases with and without bonus | Bonus is excluded from min/max in preview and final paths — implemented and tested |
| CP-4A | P4A — calculation-core compatibility extraction (authoritative PerUnit core only; Status-payment caller kept separate; direct/manual boundaries preserved; dormant M13c legacy methods left completely untouched and out of scope) — **Completed** (commit `9b76aa9`) | payroll calculation module and characterization tests | manual recalc, frontend calculation, snapshot schema, new final-payout rounding, extracting/adapting/deleting/redesigning the dormant M13c legacy methods | daily effective-rate, eligibility, authoritative/caller/direct-manual characterization groups (closed by Slice 1 `46d71d2` and Slice 2 `5ee38c2`; no Slice 3), rounding boundaries (see Phase 4 characterization gate below) | Authoritative PerUnit calculation and the current Status-payment caller integrate with exact Decimal and result-contract parity, while direct/manual boundaries retain exact compatibility; the dormant M13c legacy methods (`OrdinalTier`/`RangeBracket`/`RangeProgressive`/`Block`) remain completely untouched, uncharacterized further, not adapted, and not deleted — independently reviewed (Codex `PASS_WITH_NOTES`, no P0/P1 blockers); see "CP-4A completion record" |
| CP-4B | P4B — Open/Returned calculation preview — **Completed** (commit `f3988b7`) | payroll read contracts and tests | writes during preview or Draft money | parity, blocker, scope, Prepared denial | Backend expected-income breakdown with no source mutation — implemented and independently reviewed |
| CP-4C | P4C — calculation snapshot schema, hashes, and immutability — **Completed** (commit `d7f6b9e`) | focused migration, pure hash utility, and focused tests | lifecycle integration, mutable snapshots, UI-only freeze, snapshot APIs, frontend | schema/tenant integrity, immutable UPDATE/DELETE guards, rollback-only test isolation, canonical hash behavior | Three additive immutable tables plus deterministic hashing exist; no normal lifecycle path reads or writes them until CP-4D |
| CP-4D | P4D — Submit captures an immutable snapshot revision — **Completed** (commit `9e4c32f`) | payroll/review submit path and tests | binding approval to the snapshot, finalization changes | submit creates exactly one snapshot revision; resubmission creates a new revision; stale-revision conflicts | Every new InReview submission has exactly one associated immutable snapshot; independently reviewed with no P0/P1 blockers |
| CP-4E | P4E — review/approval binds to the submitted snapshot identity — **Completed** (commit `e332d02`) | payroll/review approval path and focused frontend/tests | re-resolving live rates at approval, finalization changes | Approved values proven frozen even if rates change afterward | Review reads and approval binds to the exact existing ReviewItem-linked snapshot rather than recomputing |
| CP-4F | P4F — finalization consumes the approved snapshot and projects it to FinalLines — **Completed** (commit `434f673`) | payroll finalization module and tests | current-rate re-resolution after approval | post-submit rate/rule changes do not alter final parity | Finalization equals the approved snapshot despite later rate/rule changes; one immutable packet is authoritative after Submit |
| CP-5A | Current Payroll Hub — **Completed** (commit `5d05756`) | `GET /payroll/current`, typed Hub schemas, and focused tests | frontend aggregation or N+1 official contract; fully-off KPI/selected-day off detail; reports | company/branch scope, slot/status/capability matrix; CP-2E date-window Working Drivers | Company-scoped optional-branch aggregate serves Open/Prepared/InReview/Returned slots, CP-1E alerts/capabilities, eligibility/Working Drivers metrics, and Open/Returned CP-4B summaries without frontend financial authority |
| CP-5B | Off-driver contracts — **Completed** (commit `9ecb300`) | `off_drivers` resolver, payroll read API, Hub integration, and focused tests | driver-day count as Hub KPI, frontend/Add Day/report expansion | calendar/eligibility denominator, Status/work classification, and selected-day detail cases | Fully-off KPI and selected-day list are distinct backend-owned contracts; Hub reuses the shared resolver without financial calculation |
| CP-5C | Calculation report bundle — **Completed** (commit `23bf68f`) | payroll report API and tests | frontend sums or mutable source reconstruction for frozen reports | dynamic columns, persisted frozen Status/Bonus evidence, and Drivers/Work/Pay/Mixed parity | All required report totals and historical evidence are backend-owned; legacy unavailable evidence is explicit rather than invented |
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
2. **Money rounding policy** — numeric compatibility characterization was completed before CP-4A so the existing 4-decimal calculation behavior could be preserved exactly. Final payable-total rounding and reconciliation remain separate product decisions for later finalization work; no 2-decimal payout mechanism is approved yet.
3. **Self-approval default and exceptions** — recommended default is disabled for snapshot-backed approval.
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
