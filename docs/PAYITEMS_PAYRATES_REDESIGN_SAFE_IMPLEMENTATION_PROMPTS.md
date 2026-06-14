# Pay Items / Pay Rates Redesign — Backend Implementation Prompts

## Sequencing Strategy

This implementation sequence is backend-only and keeps the confirmed architecture direction: system/default Pay Items remain on the existing RateType/DriverRates model, while new custom Pay Items move to a slot-based model after the database audit proves there is no legacy custom usage to preserve.

Prompt 1 must prove the actual database state before later prompts rely on the expected empty environment. The current working assumption is that there are no real drivers, payroll periods, payroll entries, driver rates, custom Pay Items, custom CPI rates, custom draft lines, custom final lines, or payroll history. That assumption is not trusted until Prompt 1 verifies it with read-only source and database inspection.

The sequence intentionally does not require a production feature flag or staged user rollout. There are no other active users to shield from incomplete workflows. The phases still remain independently testable, reviewable, and reversible.

The schema and integrity order is deliberate:

1. Add slot Pay Item definition schema.
2. Add branch-scoped slot parent and child rate schema.
3. Add draft-line `PayItemID` and calculation evidence schema.
4. Add final-line slot evidence references.
5. Add structural first-use immutability only after all tables and direct references needed to detect first use exist.
6. Add approved-version immutability and controlled supersession protection using a concrete DB-enforced mechanism, not a conceptual service-only rule.

The plan then adds custom Pay Item creation paths, read APIs, rate lifecycle APIs, WorkDate resolution, calculation, selective recalculation, audited Return/Reopen workflows, InReview write protection, operation-specific impact detection across both slot and legacy/system rate paths, reviewed-evidence preview/finalization, a separate safe historical void phase, backend ledger/report/export compatibility, full regression, and finally a read-only cleanup eligibility audit.

## Confirmed Architecture Decisions

These decisions are the target architecture for the prompts unless source inspection finds a blocking contradiction. If Claude finds such a contradiction, the affected prompt must stop with a NO-GO report instead of inventing a different design.

- Slot-based custom driver Pay Item rates should use new parent/child tables rather than making legacy `DriverRates` polymorphic. Source inspection shows legacy `DriverRates` is tightly tied to `RateTypeID`, `Amount`, existing constraints, legacy tiers, and finalized-payroll guards.
- The branch-scoped identity for every slot-based parent rate version is `CompanyID + BranchID + DriverID + PayItemID`. This identity must be used consistently in constraints, pending uniqueness, no-overlap rules, lookup indexes, resolver logic, approval, supersession, voiding, copying, history, audit, and permission checks.
- System/default Pay Items remain on the existing RateType-based model. Existing system/default RateTypes, mappings, reports, exports, and finalized payroll protections must not be deleted, migrated, hidden by bad joins, or weakened.
- If Prompt 1 confirms zero legacy custom usage, all newly created custom Pay Items must use the new slot-based model only. The backend should stop creating new legacy CPI-based custom Pay Items after that audit is proven. If Prompt 1 finds unexpected legacy custom data, later prompts that depend on emptiness must stop with NO-GO instead of deleting, converting, or hiding that data.
- One custom Daily Pay Item creates one operational Payroll Entry input column. A Pay Item owns its calculation structure; named monetary rate fields are slots belonging to that Pay Item, not unrelated independent RateTypes.
- One complete effective-dated parent configuration owns all monetary slot values for one company, branch, driver, and PayItem. Calculations must resolve exactly one parent version and must never mix slot values from different versions.
- Structural immutability must be enforced only after slot parent-rate tables, draft-line PayItem references, and final-line model-aware references exist. Permanent first-use guards must rely on direct `PayItemID`/rate references, not ambiguous `LineType` or PayItem code matching where a direct reference can exist.
- Approved slot parent versions are immutable except for a concrete DB-enforced controlled supersession operation. The plan requires Claude to inspect the repository and choose an enforceable mechanism, such as a protected database function/procedure plus direct-update guards or an equivalent repository-compatible DB-enforced pattern. Application-code checks alone are not sufficient.
- Controlled supersession must atomically validate the new PendingApproval version, mark it Approved, change the previous same-branch Approved version to Superseded, set the old version's `EffectiveTo` to one day before the new `EffectiveFrom`, preserve old child monetary values, write audit/history evidence, remain branch-scoped, and roll back completely if any step fails.
- Slot rate resolution must search historical effective versions with `Status IN ('Approved', 'Superseded')`, must exclude PendingApproval and Voided versions, and must enforce `EffectiveFrom`/`EffectiveTo` for the line's `WorkDate`.
- Pay Rates may become effective on any calendar date, including the middle of a payroll period. Each payroll line resolves the rate effective on its own `WorkDate`; earlier dates keep the previous rate, and dates on or after `EffectiveFrom` use the new rate.
- Effective-date impact protection applies to every payroll-effective rate path, not only slot rates. Slot parent lifecycle actions and legacy/system DriverRates approval/copy/void/batch/helper activation paths must all block protected periods and recalculate only affected Open WorkDates.
- Impact detection must compute operation-specific affected intervals. It must not use one generic `WorkDate >= EffectiveFrom` rule for every lifecycle operation.
- Approved/Superseded void is a historical correction and remains disabled until operation-specific impact detection, selective Open recalculation, Return/Reopen workflows, InReview write protection, reviewed evidence references, final-line references, and SourceSnapshot protections exist. Even then, it is allowed only when no protected historical evidence depends on that exact version.
- The default safe gap policy is to block Approved/Superseded void if it would create a rate gap. Do not silently extend another version, do not fall back to an older rate outside its original effective range, and do not adjust neighboring versions except through a separate explicit audited correction operation.
- InReview periods must be fully read-only across backend write paths. Corrections require an explicit audited Return for Correction to Open. Approved periods require a privileged audited Reopen for Correction to Open. Locked, finalized, and Archived history must never be retroactively changed.
- Finalization Preview and Finalization must use reviewed stored calculation evidence. They must not silently re-resolve newer rates or recalculate different amounts.
- Ledger, history, report, summary, export, review-detail, finalization-detail, and audit read paths must explicitly support both legacy RateType evidence and slot-based evidence without double-counting, omitting, or hiding slot lines through legacy-only joins.
- Legacy cleanup is not authorized by this plan.

## Prompt 1 — Read-Only Source And Database Audit

You are working in `C:\Projects\etbdnt\Payroll_App_v3`. Do not implement code in this phase.

Objective: perform a read-only audit of the actual application source, migrations, live/current development database structure, and data state before any redesign work. This prompt must prove whether the expected empty-database condition is true.

Inspect first:
- Backend source under likely areas such as `backend/app/payroll`, `backend/app/settings`, `backend/app/approval`, `backend/app/rates`, ledger/history/report/export modules, shared database/session modules, permissions, and API routers. Confirm actual paths before relying on them.
- Migration files under the real migrations folder, likely `migrations/sql`.
- Existing tests under likely areas such as `backend/tests`.
- The actual configured database connection and schema inspection utilities used by this repository.

In scope:
- Identify current migration head and applied migration state.
- List real tables and constraints relevant to Pay Items, RateTypes, DriverRates, PayrollPeriods, PayrollDraftLines, PayrollFinalLines, approval/review tables, audit/history tables, settings, permissions, reports/exports, and finalized-history triggers.
- Run read-only database queries to count drivers, payroll periods, payroll draft lines, payroll final lines, driver rates, custom Pay Items, CPI/custom RateTypes, PayItemRateTypeMap rows for custom items, PayItemSettings `rate_name_*` rows, custom request rows, and any historical payroll artifacts.
- Verify whether there are any intentionally created custom Pay Items, CPI custom rates, custom draft lines, custom final lines, real drivers, real payroll periods, real payroll entries, real driver rates, or payroll history.
- Identify all current backend paths that create custom Pay Items or legacy CPI custom rates, including admin creation, company settings creation, branch custom requests, request approval, shared helpers, seed helpers, repair helpers, clone/copy helpers, backfill helpers, and any path creating `CPI_*` RateTypes, `rate_name_*` PayItemSettings, or PayItemRateTypeMap rows for new custom items.
- Identify all current backend paths that can create or activate payroll-effective rates, including DriverRates approval/copy/void/batch/helper paths.
- Identify all current backend paths that can write payroll data, including direct service functions that bypass primary routes.
- Identify current period statuses and current finalization semantics. Do not assume a `Finalized` period status exists unless source/schema proves it.

Out of scope:
- No code edits.
- No migrations.
- No data cleanup.
- No test rewrites.
- No deletion, conversion, or deactivation of any legacy CPI object.

Behavior that must not change:
- The application and database must remain untouched.

Automated tests:
- Do not add or modify tests in this phase. You may run existing tests only if they do not modify persistent data unexpectedly.

Manual/database verification:
- Provide the exact read-only SQL queries or repository-native inspection commands used.
- Report row counts for every audited table/category.
- Report whether the expected empty condition is confirmed.
- If any unexpected nonzero real data exists, identify it precisely and mark downstream custom-slot-only prompts as NO-GO until reviewed.

Rollback/safe reversal:
- No changes are made, so rollback is not applicable.

Completion report required:
- Files inspected.
- Migrations inspected and current head.
- Database queries executed and results.
- Confirmation or rejection of the empty-database assumption.
- Current source paths for relevant services, routers, permissions, schemas, reports/exports, and tests.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 2.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 2 — Verify Current Workflows, Permissions, And NMR Meaning

Do not implement code in this phase unless a test-only probe is explicitly needed and approved. This is primarily source and behavior verification.

Objective: document the current period workflow, review workflow, permissions, payroll write paths, rate lifecycle paths, and exact meaning of `NeedsManagerReview` before changing review or freeze behavior.

Inspect first:
- Payroll period service/router/schema files.
- Payroll review or approval service/router/schema files.
- Payroll entry/day-grid/batch/import service files.
- Rate lifecycle services for DriverRates and any custom-rate flows.
- Permission definitions and branch/company scope checks.
- Existing tests for payroll entry, review, approval, finalization, NMR, day grid, imports, quick actions, rates, and permissions. Confirm actual paths.

In scope:
- Confirm allowed current period statuses and transitions.
- Confirm which permissions authorize payroll entry, review, approval, finalization, return/reopen-like actions, settings/rate changes, and report/export reads.
- Confirm every backend write path that can create, update, delete, void, import, batch-save, or quick-action payroll data.
- Confirm every backend path that can approve, copy, void, batch-create, or activate a payroll-effective rate.
- Determine whether `NeedsManagerReview` currently means "block review entry," "manager must resolve during review," "block approval," "block finalization," or something else.
- Compare current tests to actual behavior.
- Produce a short behavior matrix for Open, InReview, Approved, Locked, Archived, Cancelled/Draft if present.

Out of scope:
- No workflow changes.
- No NMR behavior change.
- No permission change.
- No migration.

Behavior that must not change:
- Existing routes and services must remain untouched.

Automated tests:
- Run only existing focused tests if safe, especially review/finalization/rate tests, and report results.

Manual/database verification:
- No database writes.
- If inspecting data, use read-only queries only.

Rollback/safe reversal:
- No changes are made.

Completion report required:
- Files inspected.
- Verified period statuses and transition rules.
- Verified NMR meaning and test coverage.
- Complete list of payroll write paths found.
- Complete list of payroll-effective rate lifecycle paths found.
- Permissions and scope checks found.
- Unresolved contradictions.
- Explicit GO or NO-GO recommendation for Prompt 3. If NMR source behavior conflicts with the intended principle, mark NO-GO for NMR-changing prompts until reviewed.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 3 — Fix Confirmed Finalization Rate-Selection Bug Only

Objective: if source inspection confirms the known finalization rate-selection issue, fix only that isolated bug before the larger redesign. Finalization must select the same intended rate evidence/source that was reviewed and approved, or must fail closed when evidence is missing.

Inspect first:
- Current finalization service and finalization preview service.
- Current draft calculation refresh logic.
- Current rate resolution logic for system/default Pay Items and any legacy custom CPI behavior.
- Existing tests around finalization, preview, `SourceSnapshot`, DriverRate selection, PayItemRateTypeMap, and finalized-line immutability.

In scope:
- Fix the smallest confirmed bug where finalization can select a different rate/mapping than the reviewed draft calculation.
- Preserve existing RateType-based system/default behavior.
- If the current system lacks sufficient stored evidence for a complete fix, implement the safest minimal fail-closed guard and document what later evidence prompts must complete.
- Add or update focused tests proving finalization cannot silently pick a newer or unrelated rate than the reviewed calculation for current supported models.

Out of scope:
- No slot schema.
- No custom slot calculation.
- No broad finalization rewrite.
- No migration unless source inspection proves a tiny additive evidence field is unavoidable and the change is explicitly approved for this phase.
- No frontend work.
- No legacy CPI cleanup.

Behavior that must not change:
- Existing system/default finalized payroll protections must remain intact.
- Existing tests unrelated to finalization rate selection should continue to pass.

Automated tests:
- Add focused regression tests for the confirmed bug.
- Run existing finalization and preview tests.
- Run any affected payroll calculation tests.

Manual/database verification:
- Verify finalization inserts final lines using the intended reviewed source, or fails closed when evidence is absent.
- Verify no locked/archived/finalized data can be mutated.

Rollback/safe reversal:
- Keep changes isolated to finalization/preview/rate-selection code and focused tests so the patch can be reverted independently.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Whether the known issue was confirmed.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 4.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 4 — Add Slot-Based Pay Item Definition Schema

Objective: add only the additive schema needed for slot-based custom Pay Item definitions. Do not connect the new schema to runtime calculation or payroll entry yet.

Inspect first:
- PayItems, PayItemSettings, PayItemRateTypeMap, RateTypes, and related migrations.
- Existing schema naming conventions, audit column conventions, timestamp conventions, soft-delete patterns, and DB trigger patterns.
- Existing settings tests and schema guard tests.

In scope:
- Add schema/migration for marking a Pay Item as slot-based and storing its calculation method and method settings, using the repository's naming and migration conventions.
- Add a child slot definition table for named monetary slots belonging to a Pay Item.
- Include fields needed for `SlotIndex`, display name, optional unit boundaries, open-ended marker, and any method-specific metadata supported by the chosen design.
- Add DB constraints where practical for ownership, uniqueness, positive indexes, company scope, and no duplicate active slot index/name within one Pay Item.
- Add check constraints for basic safe values, while leaving cross-table first-use immutability to Prompt 8 after parent-rate tables and draft/final direct references exist.
- Preserve existing system/default RateType-based Pay Items and RateTypes.

Method-specific validation requirements to support in this schema and later service validation:
- `PerUnit`: exactly one slot, no range boundaries, not open-ended.
- `Block`: exactly one monetary slot, valid positive `BlockSize`, valid `RoundingRule`, no range boundaries on the monetary slot.
- `OrdinalTier`: at least one slot, continuous `SlotIndex` values starting from 1, no `FromUnit` or `ToUnit`, no open-ended flag required.
- `RangeBracket` and `RangeProgressive`: at least two slots, first slot starts at zero, continuous ordered ranges, no gaps, no overlaps, intermediate slots have upper bounds, only final slot is open-ended, final slot has no upper bound, and comparisons must be Decimal-safe.

Out of scope:
- No parent driver rate version tables yet.
- No first-use immutability triggers yet.
- No draft/final line references yet.
- No runtime calculation.
- No API behavior changes.
- No legacy CPI deletion or conversion.
- No weakening of existing `DriverRates.RateTypeID` or `DriverRates.Amount` constraints.

Behavior that must not change:
- Current settings and payroll APIs must behave exactly as before.
- Existing system/default Pay Items must continue using RateTypes.

Automated tests:
- Add migration/schema tests proving new tables/columns/constraints exist.
- Add tests proving legacy RateType tables and constraints remain intact.

Manual/database verification:
- Apply migration in a disposable/test database.
- Inspect table definitions, indexes, foreign keys, and constraints.
- Verify current migration head advances cleanly.

Rollback/safe reversal:
- The migration should be additive and reversible by dropping only the newly added slot-definition objects if needed.

Completion report required:
- Files changed.
- Migration name(s) created/applied.
- Tests executed and results.
- Schema objects added.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 5.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 5 — Add Branch-Scoped Slot Rate Parent And Value Schema

Objective: add the additive schema for branch-scoped effective-dated parent slot-rate versions and their child monetary slot values. Do not add APIs, immutability triggers, or calculation behavior yet.

Inspect first:
- Existing `DriverRates` and `DriverRateTiers` migrations, constraints, history/audit patterns, approval statuses, and finalized-use guards.
- Existing branch/company/driver foreign keys and scope rules.
- Prompt 4 slot definition objects.

In scope:
- Create a new parent table for slot-based driver Pay Item rate versions with identity `CompanyID + BranchID + DriverID + PayItemID`.
- Create a child value table containing one monetary value per slot definition for the parent version.
- Use lifecycle statuses that support at least PendingApproval, Approved, Superseded, and Voided, matching repository conventions where practical.
- Add branch-scoped uniqueness for pending versions so one company/branch/driver/PayItem cannot have conflicting PendingApproval versions.
- Add branch-scoped no-overlap protection for approved/effective slot parent versions. Design it to account for historical Superseded versions with effective ranges so prior WorkDates remain resolvable.
- Add effective-date lookup indexes using `CompanyID`, `BranchID`, `DriverID`, `PayItemID`, lifecycle status, and `EffectiveFrom`/`EffectiveTo`.
- Add constraints tying child slot values to slots belonging to the same Pay Item as the parent.
- Add completeness validation strategy for "one complete slot set per parent." Use DB-level protection where practical and service-level validation where cross-row checks are not practical.

Out of scope:
- No structural first-use immutability yet; Prompt 8 handles it after draft/final direct references exist.
- No API endpoints.
- No approval behavior.
- No calculation resolver.
- No changes to legacy `DriverRates`.
- No loosening of `DriverRates.RateTypeID` or `DriverRates.Amount`.

Behavior that must not change:
- Existing system/default and legacy RateType behavior remains untouched.

Automated tests:
- Add schema/migration tests for parent/child tables.
- Add tests proving branch A rates do not conflict with branch B rates for the same driver and PayItem.
- Add tests for pending uniqueness, no-overlap, child slot ownership, and completeness validation strategy.

Manual/database verification:
- Inspect indexes and constraints.
- Insert disposable test rows proving branch-scoped identity, no-overlap behavior, and effective-range support for historical Superseded rows.

Rollback/safe reversal:
- Migration should be additive and removable by dropping only new slot-rate tables/triggers/indexes.

Completion report required:
- Files changed.
- Migration name(s) created/applied.
- Tests executed and results.
- Exact constraints/indexes added.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 6.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 6 — Add Draft Line PayItemID And Calculation Evidence Schema Fail-Closed

Objective: add additive draft-line support needed for direct PayItem first-use detection, slot evidence, and PayItem-aware recalculation, with a fail-closed backfill for `PayrollDraftLines.PayItemID` if the column is missing or incomplete.

Inspect first:
- Current `PayrollDraftLines` schema.
- Existing draft calculation and day-grid logic.
- Existing PayItem/RateType mapping tables.
- Existing schema guard tests.

In scope:
- Add nullable `PayItemID` to draft lines only if not already present.
- Add draft calculation evidence fields needed later to store reviewed calculation evidence, such as resolved model type, resolved rate IDs, parent slot-rate ID, slot breakdown JSON, calculation inputs, calculation amount, and evidence freshness markers. Use repository naming conventions.
- Backfill `PayrollDraftLines.PayItemID` only when exactly one unambiguous PayItem match exists inside the correct company scope.
- Leave `PayItemID` as NULL and report rows when there are multiple matches, company/system ambiguity, duplicate PayItem codes, missing company context, informational/non-PayItem lines, or any other ambiguous condition.
- Include pre-migration and post-migration audit queries for ambiguous rows.
- Ensure later permanent immutability guards can rely on direct `PayItemID` references where present and do not need ambiguous `LineType` or PayItem code matching.

Out of scope:
- No structural immutability yet.
- No recalculation behavior change.
- No slot resolver yet.
- No finalization behavior change.
- No guessing or silent preference in the backfill.

Behavior that must not change:
- Existing draft entry workflows must continue to work.
- Ambiguous draft rows must not be silently changed.

Automated tests:
- Add migration tests for unambiguous backfill.
- Add tests for ambiguous duplicate PayItem codes, company/system ambiguity, missing company context, and non-PayItem lines that must remain NULL and be reported.
- Run affected payroll entry/day-grid tests.

Manual/database verification:
- Run pre/post audit SQL in a disposable database.
- Verify ambiguous rows are reported and not guessed.

Rollback/safe reversal:
- Because this adds nullable evidence fields, rollback can remove the new columns if no later prompt depends on them.
- Backfilled values must be auditable by pre/post query output.

Completion report required:
- Files changed.
- Migration name(s) created/applied.
- Tests executed and results.
- Backfill audit results.
- Any rows left NULL and why.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 7.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 7 — Add Final Line Slot Evidence References Without Changing Finalization

Objective: add additive final-line fields needed to preserve slot calculation evidence and support direct PayItem first-use detection later, without changing finalization behavior yet.

Inspect first:
- `PayrollFinalLines` schema and migrations.
- Existing finalization insert/select SQL.
- Existing `SourceSnapshot` format and finalized-line immutability triggers.
- Existing finalization and schema guard tests.

In scope:
- Add nullable final-line fields needed to persist slot-model evidence, including direct `PayItemID` if missing, parent slot-rate ID, model discriminator, and slot breakdown/source evidence if not already covered by `SourceSnapshot`.
- Ensure the new final-line fields can identify final payroll lines using a slot-based Pay Item for the structural immutability phase.
- Extend immutable-final-history triggers/constraints so new final-line evidence fields cannot be modified for Locked/Archived/finalized periods.
- Ensure existing `SourceSnapshot` behavior is preserved.
- Add tests proving new evidence fields are protected by the same finalized-history immutability rules.

Out of scope:
- No finalization logic change.
- No preview change.
- No slot calculation integration.
- No historical data migration beyond nullable additive fields and fail-closed reference handling if needed.
- No ambiguous permanent guard based on `LineType` or PayItem code where direct PayItem references can exist.

Behavior that must not change:
- Current finalization output should be identical except for nullable new columns defaulting to NULL.

Automated tests:
- Add migration/schema tests for new fields.
- Add DB integrity tests for immutability of new fields after finalization/lock.
- Run existing finalization tests.

Manual/database verification:
- Inspect final-line table definition and triggers.
- Verify existing final rows, if any, are not modified.

Rollback/safe reversal:
- Migration should be additive and reversible by dropping only the new nullable fields/triggers if no later prompt has used them.

Completion report required:
- Files changed.
- Migration name(s) created/applied.
- Tests executed and results.
- Evidence/reference fields added.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 8.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 8 — Enforce Slot Structural Integrity And First-Use Immutability

Objective: add service-layer validation and DB-level protection, where practical, so slot-based Pay Item structure cannot be changed after first use. This phase must run only after slot definitions, slot parent-rate tables, draft-line PayItem references, and final-line model-aware references exist.

Inspect first:
- Pay Item settings services and routers.
- Prompt 4 slot definition schema.
- Prompt 5 parent/child slot-rate schema.
- Prompt 6 draft-line `PayItemID` and evidence schema.
- Prompt 7 final-line reference/evidence schema.
- Any existing DB triggers that block mutation after finalized use.

In scope:
- Define "first use" for a slot-based Pay Item as the existence of any of these:
  - a PendingApproval parent slot-rate version;
  - an Approved parent slot-rate version;
  - a Superseded parent slot-rate version;
  - a Voided parent slot-rate version;
  - a draft payroll line with direct `PayItemID` using the Pay Item;
  - a finalized payroll line with direct `PayItemID` or model-aware final-line reference using the Pay Item.
- Do not rely on ambiguous `LineType`, display name, or PayItem code matching inside permanent DB immutability guards when direct references can exist.
- Block structural changes after first use:
  - adding a slot;
  - deleting or retiring a slot;
  - changing `SlotIndex` or slot order;
  - changing `FromUnit` or `ToUnit`;
  - changing `IsOpenEnded`;
  - changing the Pay Item calculation method;
  - changing `BlockSize` or `RoundingRule`;
  - changing `IsSlotBased`;
  - moving a slot to another Pay Item.
- Allow display-name-only rename only if stored calculation evidence preserves the historical slot name used at calculation time. If evidence support is not yet present, either block rename after first use or document the temporary restriction.
- Add service validation for all settings APIs that can mutate Pay Item or slot structure.
- Add DB triggers or constraints only against tables and columns that exist by this phase.
- Validate method-specific slot structure before first use using Decimal-safe validation where range boundaries are involved.

Out of scope:
- No payroll calculation integration.
- No slot rate lifecycle APIs.
- No cleanup of legacy custom CPI objects.
- No frontend work.

Behavior that must not change:
- System/default Pay Items remain RateType-based.
- Existing default Pay Item settings behavior must remain compatible unless the item is explicitly slot-based.

Automated tests:
- Add tests that structural edits are allowed before first use.
- Add tests that every listed structural edit is blocked after each first-use category: PendingApproval, Approved, Superseded, Voided, draft line by direct PayItemID, and final line by direct/model-aware reference.
- Add tests proving ambiguous `LineType` or PayItem code alone is not used as the permanent first-use guard when direct references exist.
- Add tests that display rename behavior matches the chosen rule.
- Add DB-level protection tests if trigger/constraint support is added.

Manual/database verification:
- In a disposable database, create a slot Pay Item with and without each first-use row category and verify allowed/blocked changes.

Rollback/safe reversal:
- Keep triggers/validators isolated to slot-based objects so rollback only removes the new immutability protections.

Completion report required:
- Files changed.
- Migrations created/applied.
- Tests executed and results.
- Exact structural changes blocked.
- Direct first-use references used.
- Any rename limitation.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 9.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 9 — Protect Approved Slot Rate Versions With Enforced Controlled Supersession

Objective: enforce immutable approved/superseded/voided slot-rate versions at service and database layers while permitting controlled supersession only through a concrete repository-compatible DB-enforced mechanism.

Inspect first:
- New parent/child slot-rate tables from Prompt 5.
- Structural immutability from Prompt 8.
- Existing DriverRate mutation guards from migrations and services.
- Existing audit/history patterns for rate changes.
- Existing database privilege model, trigger patterns, stored procedure/function patterns, and any secure transaction-local context patterns already used by the repository.

In scope:
- Choose a concrete enforceable mechanism for controlled supersession. Acceptable approaches include:
  - a protected database function or stored procedure that performs the complete supersession transaction;
  - database privilege design where the application role cannot directly update protected lifecycle fields but may execute the protected function;
  - a safe transaction-local database context validated by a trigger, only if the repository already uses a secure equivalent pattern;
  - another concrete repository-compatible mechanism that prevents arbitrary direct updates.
- Do not leave "official service" as a conceptual rule without technical enforcement.
- Ensure child slot values may be inserted, updated, or deleted only while their parent status is `PendingApproval`.
- Block direct modification or deletion of monetary child values for `Approved`, `Superseded`, and `Voided` parents.
- Block direct mutation of critical parent fields after approval, including company, branch, driver, PayItem, effective dates/ranges, lifecycle identity, and fields used for rate resolution.
- Permit controlled supersession only through the selected protected mechanism. The protected operation must atomically:
  - validate the new PendingApproval version;
  - mark the new version Approved;
  - change the old same-branch version from Approved to Superseded;
  - set the old version's `EffectiveTo` to one day before the new `EffectiveFrom`;
  - preserve all old child monetary values;
  - write required audit/history evidence;
  - remain branch-scoped;
  - roll back completely if any step fails.
- Require changes to an approved rate's monetary values or identity to be represented as a new complete `PendingApproval` parent version.
- Preserve finalized-use guards as additional protection, not the only immutability mechanism.

Out of scope:
- No payroll calculation integration.
- No legacy `DriverRates` rewrite.
- No approval API expansion beyond support needed to call the protected mechanism later.

Behavior that must not change:
- Legacy DriverRate protections remain intact.
- System/default RateType rates continue using current behavior.

Automated tests:
- Add tests that arbitrary SQL-like direct mutation of Approved parent status or EffectiveTo is rejected.
- Add tests that direct parent critical-field mutation fails for Approved, Superseded, and Voided versions.
- Add tests that direct child monetary value mutation fails for Approved, Superseded, and Voided versions.
- Add tests that the protected supersession operation succeeds.
- Add tests that partial supersession cannot occur.
- Add tests that child monetary values remain unchanged during controlled supersession.
- Add tests that cross-branch supersession is impossible.
- Add tests that failed audit/history insertion rolls back the whole transition.
- Add concurrency tests proving concurrent approvals cannot produce overlapping effective ranges.

Manual/database verification:
- In a disposable database, attempt direct updates/deletes for child values and parent critical fields across statuses and verify failures.
- Use the selected protected mechanism to supersede an Approved parent and verify status, `EffectiveTo`, audit/history, unchanged child values, and branch scope.

Rollback/safe reversal:
- Keep immutability triggers/checks and protected mechanism isolated to new slot tables and lifecycle objects.
- If the selected mechanism uses database privileges or protected functions, document exact rollback steps for those objects.

Completion report required:
- Files changed.
- Migrations created/applied.
- Tests executed and results.
- Selected enforcement mechanism and why it matches the actual repository.
- Mutations blocked.
- Controlled supersession behavior verified.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 10.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 10 — Convert Direct Custom Pay Item Creation Paths To Slot-Only

Objective: update direct/admin/company-level backend custom Pay Item creation paths so, after Prompt 1 confirms zero legacy custom usage, newly created custom Pay Items use the slot model only and no longer create legacy CPI RateTypes, mappings, or `rate_name_*` settings.

Inspect first:
- Prompt 1 inventory of every custom Pay Item/CPI creation path.
- Settings services/routes/schemas that create or update Pay Items.
- Shared helper functions, seed helpers, repair helpers, clone/copy helpers, backfill helpers, and any service that creates `CPI_*` RateTypes, `rate_name_*` PayItemSettings, or PayItemRateTypeMap rows for new custom Pay Items.
- Existing tests for settings custom Pay Items, PayItemSettings, CPI RateType creation, and helper flows.

In scope:
- Update direct admin creation and company-level settings creation of new custom Pay Items to use slot-based metadata and slot definitions atomically.
- Apply exact method-specific validation:
  - `PerUnit`: exactly one slot, no range boundaries, not open-ended.
  - `Block`: exactly one monetary slot, positive `BlockSize`, valid `RoundingRule`, no range boundaries.
  - `OrdinalTier`: at least one slot, continuous slot indexes starting from 1, no `FromUnit`/`ToUnit`, no open-ended requirement.
  - `RangeBracket` and `RangeProgressive`: at least two slots, first starts zero, continuous ordered ranges, no gaps/overlaps, intermediate upper bounds, only final open-ended, final no upper bound, Decimal-safe validation.
- Stop all direct/helper creation of new CPI custom RateTypes, PayItemRateTypeMap rows, and `rate_name_*` settings for new custom Pay Items if Prompt 1 proved zero legacy custom usage.
- Keep system/default Pay Items and existing RateTypes untouched.
- If unexpected legacy custom data exists, stop with NO-GO instead of deleting, converting, or mixing architectures.

Out of scope:
- Branch custom Pay Item request storage and request approval paths are handled in Prompt 11.
- No driver slot rate assignment APIs.
- No payroll calculation integration.
- No frontend work.
- No deletion/deactivation of existing RateTypes or mappings.

Behavior that must not change:
- Existing system/default settings behavior remains intact.
- Existing default Pay Item RateType path remains intact.

Automated tests:
- Add tests for successful direct/admin/company-level slot-only custom Pay Item creation by method.
- Add validation failure tests for every method-specific rule.
- Add tests proving no CPI RateType/mapping/settings are created for new custom Pay Items when the audit gate is satisfied.
- Add tests for helper paths discovered in Prompt 1.
- Run existing settings custom Pay Item tests and update expectations only for new custom creation behavior.

Manual/database verification:
- In a disposable database, create one custom Pay Item per method through direct/admin/company-level paths and inspect PayItems, slot definitions, RateTypes, PayItemRateTypeMap, and PayItemSettings.
- Verify no new CPI legacy artifacts are created for custom Pay Items.

Rollback/safe reversal:
- Keep changes isolated to custom Pay Item creation services/schemas/tests.
- A rollback returns custom creation to the previous behavior, but should not be used after slot-based data is created without a deliberate migration plan.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Creation paths updated.
- Confirmation that direct new custom CPI creation is stopped after verified empty audit.
- Any legacy data found that blocks this phase.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 11.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 11 — Convert Custom Pay Item Request And Approval Paths To Slot-Only

Objective: update branch custom Pay Item request creation and custom request approval so request-based custom Pay Items use the slot model and do not create CPI artifacts after the empty legacy-custom audit succeeds.

Inspect first:
- Prompt 1 inventory of custom request creation and approval paths.
- Custom Pay Item request services/routes/schemas.
- Any request persistence tables that currently store proposed rate names or CPI settings.
- Approval services that create PayItems, RateTypes, PayItemRateTypeMap rows, or PayItemSettings from requests.
- Existing tests for custom request creation and approval.

In scope:
- Add or update request storage for slot calculation method, method settings, and slot definitions if the current request model cannot store them safely.
- Validate request payloads using the same method-specific slot rules as Prompt 10.
- On approval, create the slot-based Pay Item and slot definitions atomically.
- Ensure request approval does not create `CPI_*` RateTypes, PayItemRateTypeMap rows, or `rate_name_*` PayItemSettings for new custom Pay Items when Prompt 1 proved zero legacy custom usage.
- Update shared helper paths used by request approval so they cannot continue generating CPI artifacts for new custom items.
- Preserve system/default RateTypes and existing legacy data if any unexpected data was found.

Out of scope:
- No direct/admin creation changes beyond shared helper fixes needed by request approval.
- No driver slot rate assignment.
- No payroll calculation integration.
- No frontend work.

Behavior that must not change:
- Existing request permissions, branch/company scope checks, and audit behavior must remain intact or be strengthened.

Automated tests:
- Add tests for request creation with valid slot definitions.
- Add validation failure tests for invalid method/slot structures.
- Add approval tests proving slot PayItem/slots are created and CPI artifacts are not.
- Add tests for branch/company permission scope.
- Add tests that unexpected legacy custom data causes NO-GO/fail-closed behavior rather than deletion or conversion.

Manual/database verification:
- In a disposable database, submit and approve a custom Pay Item request and inspect request rows, PayItems, slot definitions, RateTypes, PayItemRateTypeMap, and PayItemSettings.

Rollback/safe reversal:
- Keep request storage changes additive where possible. Revert request/approval service changes and tests if needed before real request data exists.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Request/approval paths updated.
- Any legacy data found that blocks this phase.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 12.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 12 — Add Read-Only Slot Pay Item And Slot Rate Views/APIs

Objective: expose read-only backend APIs/services for slot-based Pay Item definitions and slot-rate history without enabling mutations beyond earlier custom Pay Item creation prompts.

Inspect first:
- Existing settings/rates routers and service patterns.
- Permission and branch/company scope validation.
- Existing response schema conventions for rate history and settings pages.

In scope:
- Add read-only endpoints or service methods to list slot-based Pay Items and their slot definitions.
- Add read-only endpoints or service methods to list slot parent rate versions and child values by `CompanyID`, `BranchID`, `DriverID`, and `PayItemID`.
- Include Approved and Superseded historical ranges in read views where appropriate, and exclude Voided/PendingApproval unless the endpoint is explicitly for pending/history administration.
- Enforce branch/company visibility and permissions.
- Keep legacy RateType read APIs backward compatible.
- Clearly separate slot-based query paths from system/default RateType query paths.

Out of scope:
- No create/update/approve/copy/void slot rate endpoints.
- No payroll calculation integration.
- No frontend work.

Behavior that must not change:
- Existing rate and settings APIs must remain backward compatible unless explicitly extended additively.

Automated tests:
- Add read API tests for scope enforcement.
- Add tests proving branch A slot rates are not visible through branch B scope.
- Add tests proving historical Approved/Superseded ranges are represented correctly.
- Run existing settings/rates API tests.

Manual/database verification:
- Seed disposable slot rows and verify read responses include complete parent/child data and exclude unauthorized branches.

Rollback/safe reversal:
- Remove the additive read endpoints/services without affecting schema or legacy APIs.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- APIs added.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 13.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 13 — Add Pending Slot Rate Create And Update APIs

Objective: allow creating and editing complete PendingApproval slot-rate parent configurations only.

Inspect first:
- Existing DriverRates create/update APIs and approval patterns.
- New slot parent/child tables and immutability rules.
- Permission and branch scope validation.
- Transaction/session patterns.

In scope:
- Add create PendingApproval slot parent endpoint/service using full identity `CompanyID + BranchID + DriverID + PayItemID`.
- Add update endpoint/service for PendingApproval versions only.
- Require a complete monetary value set for every active slot on the Pay Item.
- Validate slot definitions match the Pay Item method.
- Use atomic transaction behavior so parent and child values succeed or fail together.
- Enforce branch-aware pending uniqueness and permission checks.
- Ensure a rate in one branch never blocks or modifies another branch.

Out of scope:
- No approval/supersession.
- No copy/void.
- No payroll calculation resolution.
- No legacy DriverRates change.

Behavior that must not change:
- Existing DriverRates APIs must remain backward compatible.

Automated tests:
- Add create/update tests for valid complete PendingApproval configurations.
- Add tests for missing slot values, extra slot values, wrong PayItem slots, duplicate pending versions, unauthorized branch, and cross-branch isolation.
- Add tests that Approved/Superseded/Voided parents cannot be updated.

Manual/database verification:
- Create and update pending versions in a disposable database and inspect parent/child rows.
- Verify failed requests leave no partial child rows.

Rollback/safe reversal:
- Remove the additive endpoints/services. Pending test data can be deleted only in disposable/test databases.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- API routes/services added.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 14.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 14 — Add Slot Rate Approval Through The Protected Supersession Mechanism

Objective: approve PendingApproval slot-rate versions and supersede prior Approved versions atomically using branch-scoped identity and the concrete protected mechanism selected in Prompt 9.

Inspect first:
- Existing DriverRates approval/supersession services.
- Slot parent/child schema and immutability guards.
- Protected supersession mechanism selected in Prompt 9.
- Existing audit/history conventions.
- Period status model, but do not enforce payroll impact rules yet.

In scope:
- Add approve service/API for PendingApproval slot parents.
- Approval must validate company, branch, driver, PayItem, permission scope, complete slot set, no overlap, and method-valid slot structure.
- Approval must call the protected mechanism from Prompt 9 to atomically mark the new parent Approved and supersede only affected prior Approved versions with the same `CompanyID + BranchID + DriverID + PayItemID`.
- Controlled supersession may only change prior Approved parent `Status` to Superseded, set `EffectiveTo` to the day before the new version's `EffectiveFrom`, and write audit/history evidence through the protected path.
- A rate in another branch must never be superseded or blocked.
- Do not align `EffectiveFrom` to payroll-period boundaries.

Out of scope:
- No effective-date impact blocking yet.
- No payroll recalculation.
- No copy/void.
- No preview/finalization changes.
- No legacy/system DriverRates lifecycle changes.

Behavior that must not change:
- Existing DriverRates approval remains unchanged until shared impact enforcement is introduced later.

Automated tests:
- Add approval tests for normal approval, branch isolation, overlap rejection, incomplete slot rejection, unauthorized branch rejection, and supersession of only same-branch/same-driver/same-PayItem versions.
- Add tests proving protected supersession succeeds while arbitrary mutation still fails.
- Add tests proving concurrent approvals cannot produce overlapping effective ranges.
- Add tests proving the resolver can read the resulting Approved/Superseded timeline correctly.

Manual/database verification:
- Approve versions in two branches for the same driver/PayItem and verify independent timelines.
- Inspect audit/history rows and EffectiveTo values.

Rollback/safe reversal:
- Approval code is additive. Revert services/routes/tests if needed before production data exists. Do not hand-edit approved test data outside disposable databases.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Lifecycle behavior implemented.
- Protected supersession mechanism used.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 15.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 15 — Add Pending-Only Slot Rate Void Lifecycle

Objective: allow voiding PendingApproval slot parent versions only. Do not enable Approved or Superseded voiding before operation-specific impact protection and reviewed-evidence safeguards exist.

Inspect first:
- Existing DriverRates void/delete patterns.
- Slot parent/child immutability guards.
- Existing audit/history tables.
- Prompt 9 approved-version immutability.

In scope:
- Add void service/API for PendingApproval slot parent versions only.
- Validate permission, company/branch scope, and lifecycle status.
- Voiding PendingApproval has no payroll-calculation impact because it was never effective. It must not trigger payroll recalculation or protected-period blocking unless source inspection proves Pending rows currently affect calculation; if so, report that as a bug and stop.
- Voiding must be atomic and branch-scoped.
- Voiding must never delete parent/child rows.
- Child monetary values remain protected by immutability rules.
- Add audit/history records.
- Return a clear not-yet-supported error for Approved or Superseded slot parent void requests until the later safe historical void prompt.

Out of scope:
- No Approved/Superseded voiding in this phase.
- No effective-date impact blocking yet.
- No recalculation.
- No cleanup/deletion of legacy CPI objects.
- No direct physical deletion of approved historical rate data.

Behavior that must not change:
- Existing DriverRates void/delete behavior remains unchanged until shared impact enforcement is introduced later.

Automated tests:
- Add tests for voiding PendingApproval.
- Add tests proving PendingApproval void does not invoke recalculation or protected-period blocking.
- Add tests proving Approved and Superseded void requests are rejected as not implemented/unsafe before impact protection.
- Add tests for branch scope, unauthorized access, and immutability after void.

Manual/database verification:
- Void disposable PendingApproval versions and inspect status/audit/history rows.
- Attempt Approved/Superseded void in a disposable database and verify rejection without row mutation.

Rollback/safe reversal:
- Revert additive route/service/tests. Do not physically delete historical rows outside disposable databases.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Pending-only void behavior implemented.
- Explicit confirmation that Approved/Superseded voiding remains disabled.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 16.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 16 — Add Explicit Same-PayItem Slot Rate Copy

Objective: add copy behavior for slot-rate configurations using an explicit source parent rate ID. The copied target must keep the same PayItemID as the source parent.

Inspect first:
- Existing DriverRates copy behavior.
- Slot rate parent/child schema and lifecycle APIs.
- Permission and branch scope checks.

In scope:
- Add copy service/API that accepts exactly the source/target inputs needed for same-PayItem copy:
  - `source_parent_rate_id`;
  - `target_driver_id`;
  - `target_branch_id`;
  - `effective_from`.
- The source parent determines the PayItem. Do not permit a request payload to substitute another target PayItemID.
- Validate the selected source parent:
  - belongs to the correct company;
  - belongs to a source branch visible to the caller;
  - uses a lifecycle status allowed for copying;
  - has a complete valid slot value set.
- Validate the target:
  - caller can edit the target branch;
  - target branch has the same PayItem active as of `effective_from`;
  - target driver belongs to the target company/branch;
  - no conflicting target PendingApproval or effective version exists for the same `CompanyID + BranchID + DriverID + PayItemID`.
- Create a new complete PendingApproval target parent and child values atomically.
- Preserve branch-scoped pending uniqueness and no-overlap rules.
- Do not infer source from source driver and PayItem because multiple branches and historical versions may exist.

Out of scope:
- No approval of the copied version in this phase.
- No copying into a different PayItem.
- No recalculation.
- No legacy DriverRates copy rewrite unless shared validation requires a small bug fix.

Behavior that must not change:
- Existing copy behavior for RateType-based system/default rates remains unchanged until shared impact enforcement later.

Automated tests:
- Add tests for successful copy from explicit source parent.
- Add tests for unauthorized source branch, unauthorized target branch, inactive target PayItem, incomplete source, disallowed source status, duplicate target pending/effective conflict, cross-company rejection, and target driver not in target branch.
- Add tests proving a caller cannot substitute another PayItemID or copy values into a structurally different PayItem.

Manual/database verification:
- Copy disposable versions across allowed branches/drivers and inspect exact parent/child values and preserved PayItemID.

Rollback/safe reversal:
- Remove the additive copy endpoint/service/tests. Pending copied test data should exist only in disposable databases.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Copy validation rules implemented.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 17.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 17 — Add WorkDate-Based Slot Rate Resolver

Objective: add a pure backend resolver that returns exactly one complete slot parent version for a company, branch, driver, PayItem, and WorkDate, including historical Superseded versions.

Inspect first:
- Existing RateType/DriverRates resolver logic.
- Payroll draft calculation code.
- Branch/company scope utilities.
- Slot parent/child schema and approved/superseded lifecycle.

In scope:
- Implement a resolver service/helper for slot-based custom Pay Items.
- Inputs must include `CompanyID`, `BranchID`, `DriverID`, `PayItemID`, and `WorkDate`.
- Resolve only parent versions with `Status IN ('Approved', 'Superseded')`.
- Exclude PendingApproval and Voided versions.
- Enforce `EffectiveFrom` and `EffectiveTo` so historical WorkDates resolve historical Superseded parents.
- Required example behavior:
  - old rate June 1 through June 6, now Superseded;
  - new rate June 7 onward, Approved;
  - WorkDate June 4 resolves the old Superseded parent;
  - WorkDate June 7 resolves the new Approved parent.
- Gaps must return unresolved/fail closed. Do not silently extend another version or fall back outside a version's stored effective range.
- Return the parent identity and all child slot values from that same parent.
- Fail closed if no effective version exists, multiple versions match, child values are incomplete, child values do not belong to the PayItem, a Voided/Pending version would otherwise match, or branch scope is invalid.
- Preserve mid-period effective dates. Do not align `EffectiveFrom` to payroll-period boundaries.

Out of scope:
- No payroll line recalculation.
- No API changes unless exposing a diagnostic endpoint is already part of repository patterns and approved.
- No legacy DriverRates changes.

Behavior that must not change:
- Existing RateType resolver remains unchanged.

Automated tests:
- Add resolver tests for historical Superseded resolution.
- Add resolver tests for current Approved resolution.
- Add tests for old Superseded June 1-6 and new Approved June 7 onward: June 4 resolves old, June 7 resolves new.
- Add tests for before/after mid-period effective dates.
- Add branch isolation tests.
- Add tests for Voided exclusion and PendingApproval exclusion.
- Add tests proving gaps return unresolved instead of silently extending another version.
- Add tests proving overlapping versions fail closed.
- Add tests proving all slot values come from the same parent ID and no parent versions are mixed.
- Add tests proving controlled supersession creates a timeline the resolver can read correctly.
- Add tests for missing, duplicate, incomplete, and corrupted structures failing closed.

Manual/database verification:
- Seed disposable approved/superseded versions with mid-period dates and verify WorkDate-specific resolution.

Rollback/safe reversal:
- Remove the new helper/tests. No schema rollback needed.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Resolver behavior implemented.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 18.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 18 — Add Slot Calculation Helper With Defensive Structure Validation

Objective: add a pure calculation helper for slot-based Pay Items that uses resolved slot values and validates stored structure at runtime.

Inspect first:
- Existing payroll calculation helpers.
- Decimal/money handling conventions.
- PayItem calculation method settings.
- Tests for quantity, hours, miles, loads, block rounding, and tier behavior.

In scope:
- Implement slot calculation for supported methods:
  - `PerUnit`;
  - `Block`;
  - `OrdinalTier`;
  - `RangeBracket`;
  - `RangeProgressive`.
- Use Decimal-safe arithmetic and comparisons, never binary floating-point for money or boundaries.
- Defensively validate stored slot structure at runtime and fail closed if corrupted structure somehow exists.
- Enforce method rules:
  - `PerUnit`: exactly one slot, no boundaries, not open-ended.
  - `Block`: exactly one monetary slot, positive `BlockSize`, valid `RoundingRule`, no boundaries.
  - `OrdinalTier`: at least one continuous slot index starting from 1, no boundaries, payroll quantity must be a whole number, highest slot handles later ordinal positions.
  - `RangeBracket`/`RangeProgressive`: at least two slots, first starts zero, continuous ranges, no gaps/overlaps, intermediate upper bounds, only final open-ended, final no upper bound.
- Return calculation evidence including method, inputs, slot names, slot IDs, slot indexes, slot rates, breakdown, parent slot-rate ID, and calculated amount.

Out of scope:
- No payroll entry integration.
- No review/finalization changes.
- No legacy calculation rewrite.

Behavior that must not change:
- Existing RateType-based calculations continue using existing code paths.

Automated tests:
- Add unit tests for every calculation method.
- Add edge tests for mid-boundary values, open-ended final range, ordinal overflow to highest slot, non-whole ordinal quantity rejection, Decimal precision, and corrupted structure fail-closed behavior.

Manual/database verification:
- None required beyond reviewing test fixtures unless repository has calculation diagnostic scripts.

Rollback/safe reversal:
- Remove the helper/tests. No schema rollback needed.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Calculation methods implemented.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 19.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 19 — Integrate Slot Calculation Into Open Payroll Draft Calculations

Objective: use the slot resolver and calculation helper for slot-based custom Pay Items during Open-period draft payroll calculation while preserving system/default RateType behavior.

Inspect first:
- Current draft calculation refresh services.
- Payroll entry/day-grid line creation/update logic.
- PayItem-to-column behavior for custom Daily Pay Items.
- Existing tests for day grid, entry APIs, draft line calculations, and custom Pay Items.

In scope:
- Route slot-based custom Pay Items through the slot resolver and calculation helper.
- Continue routing system/default Pay Items through existing RateType-based logic.
- If Prompt 1 confirmed zero legacy custom usage, newly created custom Pay Items should be slot-only; do not preserve legacy CPI creation paths for new custom items.
- Store draft calculation evidence for slot-based lines.
- Ensure one Daily custom Pay Item still creates one operational input column in Payroll Entry.
- For Open periods, allow recalculation when inputs or allowed rates change.
- Fail closed when required slot rate configuration is missing, invalid, or has a WorkDate gap.

Out of scope:
- No InReview freeze yet.
- No selective effective-date impact enforcement yet.
- No preview/finalization change.
- No frontend work.

Behavior that must not change:
- System/default Pay Item calculations must remain unchanged.
- Existing entry APIs must remain backward compatible except for new custom slot items using the slot path.

Automated tests:
- Add draft calculation tests for slot-based custom Pay Items.
- Add tests proving system/default RateType calculations remain unchanged.
- Add tests for missing slot rate and rate-gap failure.
- Run day-grid and payroll entry tests.

Manual/database verification:
- In a disposable database, create a slot Pay Item, approve rates, create Open draft lines, and inspect stored evidence.

Rollback/safe reversal:
- Keep integration behind the `IsSlotBased` data path, not a production feature flag. Reverting this prompt returns draft calculation to legacy behavior while leaving additive schema in place.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Draft integration behavior.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 20.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 20 — Add Selective Recalculation For Affected Open Lines

Objective: add a service that recalculates only affected Open-period draft lines for a given rate or Pay Item change, based on operation-specific affected intervals and actual WorkDate.

Inspect first:
- Current draft recalculation/refresh implementation.
- Payroll period status logic.
- Draft line schema and evidence fields.
- Existing tests around recalculation, period transitions, and finalization.

In scope:
- Implement a selective recalculation helper for Open periods.
- Inputs should support both slot-based identities and legacy/system RateType identities, because later impact enforcement applies to both architectures.
- Accept an explicit affected date interval or set of affected WorkDates computed by the impact detector. Do not hard-code a generic `WorkDate >= EffectiveFrom` rule for every lifecycle operation.
- For slot rates, match company, branch, driver, PayItem, and WorkDates inside the affected interval.
- For legacy/system DriverRates, match company, branch if applicable, driver, RateType/PayItem mapping as applicable, and WorkDates inside the affected interval.
- Leave unaffected WorkDates unchanged.
- Preserve mid-period effective dates.
- Store refreshed evidence only for affected Open lines.
- Use repository transaction/session patterns.

Out of scope:
- No operation-specific impact detection yet.
- No blocking of InReview/Approved impacts yet.
- No Return/Reopen workflow.
- No preview/finalization change.

Behavior that must not change:
- Existing full refresh behavior should remain available where still used.
- System/default recalculation should remain correct.

Automated tests:
- Add tests where a slot rate changes mid-period and only WorkDates inside the provided affected interval recalculate.
- Add tests where a system/default DriverRate changes mid-period and only WorkDates inside the provided affected interval recalculate.
- Add tests where unaffected earlier and later WorkDates remain unchanged.
- Add branch/driver/PayItem/RateType isolation tests.
- Run existing draft calculation tests.

Manual/database verification:
- Seed an Open period with before/during/after interval WorkDate lines and verify only affected lines update for both slot and system/default rate models.

Rollback/safe reversal:
- Helper is additive until wired into lifecycle prompts. Revert helper/tests if needed.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Recalculation scope implemented.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 21.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 21 — Add Audited Return For Correction: InReview To Open

Objective: implement one authoritative service path for returning an InReview period to Open for correction.

Inspect first:
- Payroll period transition service.
- Review decision service that currently handles Return/EditRequested/Rejected if present.
- Audit/history tables and permission system.
- Existing review tests.

In scope:
- Add or refactor to a single Return for Correction service for `InReview -> Open`.
- Require a specific permission.
- Require a non-empty reason.
- Record audit with actor, timestamp, reason, previous status, and new status.
- Invalidate or clear the previous review decision as appropriate while preserving historical audit evidence.
- Make the period editable again.
- Ensure no silent rate changes, payroll changes, or recalculation occur during the transition.
- Require later resubmission to InReview and review again before approval.
- Ensure review decisions such as Return, EditRequested, or Rejected call this same service instead of directly updating `PayrollPeriods`.

Out of scope:
- No Approved reopen in this phase.
- No InReview write protection in this phase.
- No recalculation workflow change.
- No preview/finalization change.

Behavior that must not change:
- Other period transitions remain unchanged unless they are direct bypasses of Return for Correction and must be routed through the new service.

Automated tests:
- Add tests for allowed InReview -> Open with permission and reason.
- Add tests for missing permission, missing reason, wrong status, audit creation, review invalidation, and no recalculation side effects.
- Add tests proving review Return/EditRequested paths use the same service behavior.

Manual/database verification:
- Transition a disposable InReview period to Open and inspect period status, audit rows, review state, and unchanged draft calculations.

Rollback/safe reversal:
- Keep transition logic isolated. Revert service/router/test changes if needed.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Audit fields recorded.
- Bypass paths removed or routed.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 22.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 22 — Add Privileged Reopen For Correction: Approved To Open

Objective: implement one authoritative privileged service path for reopening an Approved period to Open for correction.

Inspect first:
- Payroll period transitions and approval service.
- Permission definitions.
- Finalization preconditions.
- Audit/history and review approval records.

In scope:
- Add Reopen for Correction service for `Approved -> Open`.
- Require stronger permission than normal payroll entry.
- Require a non-empty reason.
- Record full audit with actor, timestamp, reason, previous status, and new status.
- Invalidate the previous approval for operational purposes while preserving historical approval audit evidence.
- Prevent finalization until recalculation, review, and approval are completed again.
- Ensure no silent payroll/rate recalculation happens during reopen.
- Ensure Approved -> Open cannot be performed by direct status update or review bypass path.

Out of scope:
- No InReview -> Open changes beyond using Prompt 21 service.
- No Locked/Archived reopening.
- No finalization rewrite.

Behavior that must not change:
- Locked and Archived periods must remain protected.
- Existing approval audit evidence must not be deleted.

Automated tests:
- Add tests for allowed Approved -> Open with privileged permission and reason.
- Add tests for normal entry permission rejection, missing reason, wrong status, audit creation, approval invalidation, and finalization blocked until re-review/reapproval.
- Add tests proving Locked/Archived cannot reopen.

Manual/database verification:
- Reopen a disposable Approved period and inspect status, audit, approval history, and finalization preconditions.

Rollback/safe reversal:
- Keep reopen service isolated. Revert route/service/tests if needed.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Reopen behavior implemented.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 23.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 23 — Make InReview Fully Read-Only Across Payroll Write Paths

Objective: enforce that InReview payroll periods are read-only across every backend path that can mutate payroll data.

Inspect first:
- Prompt 2 write-path inventory.
- Payroll entry create/update/delete/void services.
- Daily grid save services.
- Batch entry and import paths.
- Pay-period quick actions.
- Bonus, adjustment, notes, status, quantity, hours, miles, loads, and other operational write services.
- Any direct service functions that bypass primary routes.

In scope:
- Remove InReview from allowed payroll data write statuses.
- Block create draft line, update draft line, delete/void draft line, daily grid save, batch entry, imports, quick actions, bonus/adjustment writes, notes/status/quantity/hours/miles/loads changes, and any other operational mutation for InReview periods.
- Require corrections to use the audited Return for Correction service from Prompt 21.
- Ensure no silent recalculation or evidence mutation occurs in InReview.
- Keep read endpoints available as appropriate.

Out of scope:
- No Return/Reopen service changes except using them.
- No finalization change.
- No frontend work.

Behavior that must not change:
- Open periods remain editable.
- Approved/Locked/Archived protections remain at least as strict as before.

Automated tests:
- Add tests for every write path found in Prompt 2 proving InReview mutations are rejected.
- Add tests proving Open remains editable.
- Add tests proving direct service calls cannot bypass route-level protection.
- Run payroll entry, day-grid, import, batch, review, and finalization tests.

Manual/database verification:
- Attempt representative InReview write operations in a disposable database and verify no row changes or recalculation timestamps/evidence changes.

Rollback/safe reversal:
- Changes should be centralized in status validation helpers where possible. Revert validation changes/tests if needed.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Complete list of protected write paths.
- Any path not yet protected and why.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 24.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 24 — Add Operation-Specific Effective-Date Impact Detection For All Rate Models

Objective: add a read-only impact detector that identifies affected payroll periods and payroll lines for any proposed payroll-effective rate lifecycle change, covering both slot rates and legacy/system DriverRates with operation-specific affected intervals.

Inspect first:
- Payroll period schema/statuses.
- Draft line WorkDate, PayItemID, RateTypeID/evidence fields.
- Final line evidence fields and `SourceSnapshot`.
- Branch/company/driver scope utilities.
- Slot rate lifecycle services.
- Legacy/system DriverRates approval, copy, void, batch, and helper activation paths.
- Existing rate resolution rules for both architectures.

In scope:
- Implement a shared detector for proposed approve, copy, supersede, void, batch-create, or helper activation operations that can affect payroll calculations.
- Detection must support slot parent identity and legacy/system DriverRate identity without forcing either architecture into the other.
- Compute operation-specific affected date intervals:
  - New approval or copied rate: affected interval begins at the new version's `EffectiveFrom`; its effective end depends on the next valid version or open-ended timeline; identify lines whose WorkDate falls inside the interval whose resolved rate would change.
  - Supersession: account for both the new version becoming effective and the previous Approved version receiving a new `EffectiveTo`; identify all WorkDates whose resolution changes because of either timeline change.
  - PendingApproval void: no payroll-calculation impact because it was never effective; do not trigger recalculation or protected-period blocking unless source inspection proves Pending rows affect calculation, which must be reported as a bug.
  - Approved or Superseded void: affected interval is the existing effective range of the version being voided, from existing `EffectiveFrom` through existing `EffectiveTo`, or open-ended when `EffectiveTo` is NULL. Determine which payroll lines currently resolve to that exact version, whether voiding would expose a previous version, expose a later version, create a gap with no applicable rate, or change amounts/create unresolved/NMR lines.
  - Legacy/system DriverRates: apply equivalent operation-specific interval logic using their actual effective dates, statuses, mappings, and applicable rate-resolution rules.
- For slot rates, detection must be branch-aware, driver-aware, company-aware, PayItem-aware, and based on actual affected `WorkDate` values.
- For legacy/system DriverRates, detection must be company-aware, branch-aware where branch applies, driver-aware, RateType/PayItem-mapping-aware where applicable, and based on actual affected `WorkDate` values.
- A change beginning after the end of an InReview or Approved period must be allowed because it does not affect that period.
- Detect affected Open, InReview, Approved, Locked, Archived/finalized periods and matching draft/final lines.
- Preserve mid-period effective dates.
- Return enough detail for lifecycle services to block, recalculate, or report gaps later.

Out of scope:
- Do not enforce blocking rules yet.
- Do not recalculate.
- Do not enable Approved/Superseded void.
- Do not change rate lifecycle endpoints.
- Do not rewrite system/default RateTypes into slots.

Behavior that must not change:
- Existing rate lifecycle behavior remains unchanged until Prompt 25.

Automated tests:
- Add tests for new approval/copied-rate intervals for slot rates and system/default DriverRates.
- Add tests for supersession intervals where both the new version and old `EffectiveTo` change matter.
- Add tests proving PendingApproval void has no payroll impact.
- Add tests for Approved/Superseded void detection over the exact existing effective range.
- Add tests for affected vs unaffected WorkDates within the same period for both architectures.
- Add tests where `EffectiveFrom` is after an InReview/Approved period and therefore does not block.
- Add branch/driver/PayItem/RateType isolation tests.
- Add tests for Locked/Archived/finalized detection.
- Add tests for gap detection.

Manual/database verification:
- Seed disposable periods in multiple statuses and verify detector output for both slot and legacy/system rate models across approval, copy, supersession, Pending void, and historical void scenarios.

Rollback/safe reversal:
- Detector is additive. Revert helper/tests if needed.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Operation-specific detection rules implemented.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 25.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 25 — Enforce Effective-Date Impact Rules Across Rate Lifecycle Paths Except Historical Void

Objective: wire operation-specific impact detection into payroll-effective rate lifecycle paths so protected periods cannot be silently affected. Keep Approved/Superseded historical void disabled for the separate later prompt.

Inspect first:
- Prompt 24 impact detector.
- Selective recalculation helper from Prompt 20.
- Return/Reopen services from Prompts 21 and 22.
- Slot approve/copy/Pending void services.
- Legacy/system DriverRates approval, copy, void, batch, and helper activation services.

In scope:
- Before approving, copying, superseding, batch-creating, or helper-activating a payroll-effective rate, determine affected periods and lines using operation-specific intervals.
- Apply these rules to slot parent approval, slot supersession, slot copy, legacy/system DriverRates approval, legacy/system DriverRates copy, legacy/system DriverRates void where currently supported, and any batch/helper path that creates or activates a payroll-effective rate:
  - Open period: change is allowed and only affected lines in the computed affected interval are recalculated.
  - InReview period: change is blocked until audited Return for Correction to Open.
  - Approved period: change is blocked until privileged audited Reopen for Correction to Open.
  - Locked, Archived, or finalized payroll: change is permanently blocked for dates affecting that period.
  - A change beginning after a protected period ends remains allowed.
- PendingApproval slot void remains no-impact and must not trigger recalculation or protected-period blocking unless Prompt 24 exposed an existing bug.
- Preserve mid-period effective dates; do not align to payroll-period boundaries.
- Ensure branch A changes never block/recalculate branch B.
- Reuse shared impact-detection infrastructure safely for both architectures without converting system/default rates to slots.
- Use atomic transaction behavior for lifecycle change plus any Open-period recalculation.

Out of scope:
- Do not enable Approved/Superseded slot void in this phase.
- No review freeze changes.
- No preview/finalization changes.
- No legacy cleanup.
- No polymorphic rewrite of `DriverRates`.

Behavior that must not change:
- System/default RateType storage remains on DriverRates.
- Existing RateType calculations remain RateType-based.

Automated tests:
- Add lifecycle tests for Open selective recalculation for slot rates using operation-specific intervals.
- Add lifecycle tests for Open selective recalculation for system/default DriverRates using operation-specific intervals.
- Add blocking tests for InReview, Approved, Locked, Archived/finalized impacts for both architectures.
- Add tests for changes after a protected period end remaining allowed.
- Add branch isolation tests.
- Add tests proving PendingApproval void does not recalculate or block protected periods.
- Add tests proving Approved/Superseded slot void remains disabled and is not accidentally routed through general impact enforcement.

Manual/database verification:
- In a disposable database, approve/copy/supersede mid-period rate changes for slot and system/default models and verify only affected Open lines recalculate.
- Verify protected statuses block with clear error messages.
- Verify Approved/Superseded slot void remains unavailable.

Rollback/safe reversal:
- Revert lifecycle wiring while keeping detector/helper if needed.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Impact enforcement behavior.
- Rate lifecycle paths covered.
- Confirmation that Approved/Superseded slot void remains disabled.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 26.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 26 — Store Current Calculation Evidence Before Entering InReview

Objective: ensure Open -> InReview stores current calculation evidence for all relevant payroll lines without approving or finalizing anything.

Inspect first:
- Payroll period transition code for Open -> InReview.
- Draft calculation refresh logic.
- Evidence fields added in Prompt 6.
- Slot and RateType calculation paths.
- Prompt 2 NMR findings.

In scope:
- On Open -> InReview, refresh or validate current evidence for all draft payroll lines that require calculation.
- Store evidence for slot-based items, system/default RateType items, and any remaining legacy CPI lines if they exist.
- Do not mutate an InReview period after transition completes.
- Preserve current product principle for NMR: Open -> InReview must store current evidence; a line may enter review if NMR is intended for manager resolution during review; later approval/finalization must be blocked while unresolved NMR remains.
- If Prompt 2 found that source-defined NMR behavior differs materially, document the verified behavior and stop for review before changing it.

Out of scope:
- No review approval validation in this prompt.
- No preview/finalization changes.
- No Return/Reopen changes.
- No Approved/Superseded void.

Behavior that must not change:
- Open periods remain editable until submitted.
- InReview is read-only after transition.

Automated tests:
- Add tests proving Open -> InReview stores fresh evidence.
- Add tests for slot-based, system/default, and any supported legacy CPI evidence paths.
- Add tests proving evidence is not silently mutated after InReview.
- Add NMR tests matching verified source behavior and intended principle.

Manual/database verification:
- Submit a disposable Open period to InReview and inspect draft evidence fields before and after transition.

Rollback/safe reversal:
- Revert transition/evidence changes if needed. Existing draft evidence columns remain additive.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Evidence stored.
- NMR behavior confirmed or blocking discrepancy reported.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 27.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 27 — Validate Evidence And NMR Before Review Approval

Objective: block InReview -> Approved when reviewed evidence is missing, stale, unresolved, invalid, or blocked by unresolved required NMR conditions.

Inspect first:
- Review approval service.
- Period approval records.
- Evidence freshness markers.
- Prompt 2 NMR findings and Prompt 26 implementation.

In scope:
- Add validation before approving an InReview period.
- Reject approval if any required line has missing, unresolved, invalid, or stale evidence.
- Reject approval while unresolved required NMR conditions remain.
- If NMR is intended for manager resolution during review, allow entry into review but block approval/finalization until resolved.
- Ensure review decisions that return/edit-request/reject route through the single Return for Correction service from Prompt 21.
- Preserve audit history of both failed and successful approval decisions according to repository conventions.

Out of scope:
- No preview change.
- No finalization change.
- No InReview write protection changes beyond using existing protections.
- No Approved/Superseded void.

Behavior that must not change:
- Approved periods remain non-editable except through privileged Reopen for Correction.

Automated tests:
- Add tests for approval blocked by missing evidence, stale evidence, unresolved slot rate, invalid structure, and unresolved required NMR.
- Add tests for successful approval with valid stored evidence.
- Add tests that Return/EditRequested does not bypass Return for Correction audit.

Manual/database verification:
- Attempt approval with tampered/missing evidence in a disposable database and verify fail-closed behavior.

Rollback/safe reversal:
- Revert approval validation changes/tests if needed.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Approval validation rules.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 28.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 28 — Make Finalization Preview Use Reviewed Evidence Only

Objective: update Finalization Preview so it displays stored reviewed evidence and totals exactly as finalization will use them, without recalculating or re-resolving newer rates.

Inspect first:
- Finalization preview service/router.
- Draft evidence fields and finalization source snapshot format.
- Existing preview tests.
- Slot/system/default/legacy calculation evidence structures.

In scope:
- For InReview/Approved periods as appropriate, preview must read stored reviewed evidence.
- Preview must not call calculation refresh that can change reviewed amounts.
- Preview must not re-resolve newer slot or RateType rates.
- Preview must show parent slot-rate ID, rate information, slot breakdown, and calculated amount for slot-based lines.
- Preview must continue to handle system/default RateType lines and any remaining legacy CPI evidence without breaking historical behavior.
- Preview must fail closed if reviewed evidence is missing or invalid.

Out of scope:
- No finalization insert behavior change.
- No approval validation changes.
- No recalculation.
- No Approved/Superseded void.

Behavior that must not change:
- Existing preview response shape should remain backward compatible where possible, extended additively for evidence details.

Automated tests:
- Add tests proving preview totals come from stored evidence.
- Add tests proving preview does not change when a newer rate is added after review.
- Add tests for missing/invalid evidence fail-closed.
- Run existing preview tests.

Manual/database verification:
- Create a reviewed disposable period, add a newer rate, run preview, and verify reviewed amounts remain unchanged.

Rollback/safe reversal:
- Revert preview service/test changes if needed. Evidence schema remains additive.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Preview behavior implemented.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 29.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 29 — Make Finalization Use Reviewed Evidence Only

Objective: update finalization so final lines and `SourceSnapshot` are created from reviewed stored evidence, matching Finalization Preview exactly.

Inspect first:
- Finalization service and SQL inserts.
- Finalization Preview behavior from Prompt 28.
- Final-line evidence columns and immutable-final triggers.
- Existing finalization tests.

In scope:
- Finalization must require Approved status and valid reviewed evidence.
- Finalization must not call a calculation refresh that can change reviewed amounts.
- Finalization must not re-resolve newer slot or RateType rates.
- Final lines and `SourceSnapshot` must preserve reviewed parent slot-rate ID, rate information, slot breakdown, calculation inputs, and calculated amount for slot-based lines.
- System/default and any remaining legacy CPI lines must use their reviewed evidence paths.
- Preview and finalization totals must match exactly.
- Finalized/Locked/Archived history protections must remain intact.
- Finalization must always be blocked while unresolved NMR remains.

Out of scope:
- No preview changes except test coordination if needed.
- No rate lifecycle changes.
- No cleanup.
- No ledger/report/export compatibility changes; Prompt 31 handles read-path compatibility separately.
- No Approved/Superseded void.

Behavior that must not change:
- Finalized historical payroll must not be modified.
- Existing final-line immutability guards must remain intact or stronger.

Automated tests:
- Add tests proving finalization uses stored evidence after a newer rate exists.
- Add tests proving preview/finalization totals match exactly.
- Add tests for slot-based, system/default, and any remaining legacy CPI evidence.
- Add tests for missing/invalid evidence and unresolved NMR blocking finalization.
- Run full finalization and DB integrity tests.

Manual/database verification:
- Finalize a disposable Approved period and inspect final lines and `SourceSnapshot`.
- Verify no recalculation/evidence mutation occurs during finalization.

Rollback/safe reversal:
- Revert finalization service/test changes if needed. Do not manually edit final data outside disposable databases.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Preview/finalization match proof.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 30.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 30 — Add Safe Historical Void For Approved Or Superseded Slot Rates

Objective: add a small, independently testable historical void workflow for Approved or Superseded slot parent versions only after all impact, recalculation, Return/Reopen, InReview write protection, reviewed evidence, final-line, and SourceSnapshot protections exist.

Inspect first:
- Prompt 24 operation-specific impact detector.
- Prompt 25 impact enforcement.
- Prompt 20 selective recalculation helper.
- Prompt 21 Return for Correction.
- Prompt 22 Reopen for Correction.
- Prompt 23 InReview write protection.
- Prompt 26 and Prompt 27 reviewed evidence behavior.
- Prompt 29 final-line and `SourceSnapshot` protections.
- Slot parent/child immutability and audit/history services.

In scope:
- Enable voiding Approved or Superseded slot parent versions only when all safety rules pass.
- Use the exact existing effective range of the version being voided: existing `EffectiveFrom` through existing `EffectiveTo`, or open-ended when `EffectiveTo` is NULL.
- Determine which payroll lines currently resolve to that exact parent/version.
- Block the void if any final line references it.
- Block the void if any Locked, Archived, or finalized payroll depends on it.
- Block the void if any InReview or Approved period depends on it.
- Block the void if any reviewed evidence or final `SourceSnapshot` depends on it.
- Allow the void only if affected Open-period lines can be recalculated safely and the resulting timeline remains valid.
- Default gap policy: block the void if it would create a rate gap. Do not automatically fall back to an older rate outside its original effective range.
- Do not automatically extend another version's `EffectiveTo` or alter neighboring versions. Such changes require a separate explicit audited correction operation.
- Preserve full audit/history.
- Keep child monetary values immutable and preserve all historical rows.

Out of scope:
- No legacy/system DriverRates historical void redesign unless existing code already supports it and must receive the same protection.
- No cleanup/deletion.
- No finalization changes.
- No automatic gap-to-NMR behavior unless source inspection proves that is an intentional product rule and the prompt stops for review before implementing it.

Behavior that must not change:
- PendingApproval void remains handled by Prompt 15.
- System/default RateType storage remains on DriverRates.
- Finalized historical payroll remains immutable.

Automated tests:
- Add tests that void is blocked when a final line references the exact parent.
- Add tests that void is blocked when reviewed evidence or `SourceSnapshot` references the exact parent.
- Add tests that void is blocked for InReview, Approved, Locked, Archived, or finalized dependencies.
- Add tests that affected Open lines recalculate only when safe.
- Add tests that gap creation blocks the void by default.
- Add tests that no automatic fallback outside original effective ranges occurs.
- Add tests that neighboring versions are not silently extended or modified.
- Add tests for branch isolation and audit/history preservation.

Manual/database verification:
- In a disposable database, attempt historical void scenarios with Open-only, InReview, Approved, Locked/Archived, finalized, SourceSnapshot, and gap conditions.
- Verify row preservation, audit/history, and unchanged child monetary values.

Rollback/safe reversal:
- Keep historical void behavior isolated. Rollback must disable Approved/Superseded void while leaving PendingApproval void intact.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Historical void policy implemented.
- Gap behavior implemented.
- Evidence dependency checks implemented.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 31.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 31 — Add Backend Compatibility For Ledger, History, Reports, Summaries, And Exports

Objective: update backend read paths that may assume all rate evidence comes from legacy DriverRates or RateTypeID so they correctly represent mixed system/default and slot-based finalized payroll.

Inspect first:
- Ledger APIs and services.
- Payroll history APIs.
- Finalized-line detail APIs.
- Payroll summary APIs.
- Reports and export services.
- CSV/file-generation services.
- Driver payroll history services.
- Review detail queries.
- Finalization result/detail queries.
- Audit/history display queries.
- Any SQL JOIN that assumes `RateTypeID` or `DriverRateID` is always present.
- Any calculation evidence serializer that assumes only the legacy model.

In scope:
- Update read/query/serialization paths so system/default finalized lines continue showing legacy RateType/DriverRate evidence.
- Update read/query/serialization paths so slot-based finalized lines show PayItem, slot parent ID, slot breakdown, and reviewed amount.
- Ensure totals do not double-count or omit slot-based lines.
- Ensure queries distinguish the two models explicitly.
- Replace unsafe legacy-only `INNER JOIN` assumptions with model-aware joins so slot-based lines are not hidden.
- Preserve reviewed historical evidence in exports and reports.
- Preserve branch and permission scoping.
- Add compatibility in backend read paths before the final regression-only phase.

Out of scope:
- No new calculation behavior.
- No finalization behavior change.
- No frontend work.
- No cleanup.
- No broad report redesign beyond making existing backend outputs correct.

Behavior that must not change:
- Existing ledger/history/report/export behavior for system/default RateType lines must remain compatible.
- Historical finalized payroll must not be modified.

Automated tests:
- Add tests for mixed system/default and slot-based final payroll.
- Add tests for Ledger totals.
- Add tests for payroll summary totals.
- Add tests for finalized-line detail endpoints.
- Add tests for payroll history and driver payroll history.
- Add tests for reports/exports where repository test patterns allow.
- Add tests for review detail and finalization detail queries if they expose evidence.
- Add tests for permission and branch isolation.
- Add tests proving mixed joins do not duplicate rows and do not hide slot-based lines.

Manual/database verification:
- In a disposable database, create mixed finalized payroll evidence and verify ledger, history, summary, detail, report/export, review-detail, and finalization-detail outputs.

Rollback/safe reversal:
- Keep changes localized to read/query/serializer paths. Revert compatibility changes/tests if needed.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- Read paths inspected and updated.
- Any read path not yet covered and why.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 32.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 32 — Run Compatibility And Regression Validation

Objective: verify that the completed backend supports slot-based custom Pay Items while preserving system/default RateType behavior, backend read compatibility, and finalized-history protections.

Inspect first:
- All changed services, routers, schemas, migrations, and tests from previous prompts.
- Existing regression tests for settings, rates, payroll entry, review, preview, finalization, permissions, ledger, reports, exports, and DB integrity.

In scope:
- Add or update integration tests covering end-to-end slot-based custom Pay Item flow: create PayItem, define slots, create pending rate, approve through protected supersession, enter Open payroll, mid-period rate change, operation-specific selective recalculation, submit to InReview, approve, preview, finalize, and view through ledger/history/report/export read paths.
- Add regression tests proving system/default Pay Items still use existing RateType path.
- Add tests proving new custom Pay Items no longer create CPI RateTypes when Prompt 1 audit proved zero legacy custom usage.
- Add tests proving unexpected legacy custom data causes relevant paths to stop with NO-GO/fail-closed behavior rather than deletion/conversion.
- Add tests for branch-scoped identity across resolver, approval, copy, void, history, audit, permissions, ledger, and exports.
- Add tests for InReview read-only and Return/Reopen workflows.

Out of scope:
- No new features.
- No cleanup/deletion.
- No frontend work.
- No broad refactor.

Behavior that must not change:
- Existing public APIs remain backward compatible except explicitly changed custom Pay Item creation behavior after verified empty audit.

Automated tests:
- Run the full backend test suite if feasible.
- At minimum run all payroll, settings, rates, review, preview, finalization, permission, ledger, report/export, migration, and DB integrity tests.

Manual/database verification:
- In a disposable database, manually walk through a complete slot-based payroll lifecycle and record key row IDs/evidence values.
- Verify existing default RateTypes remain present and unmigrated.

Rollback/safe reversal:
- Regression-only changes should be test-only unless small bug fixes are discovered. Any bug fix must be narrowly scoped and reported.

Completion report required:
- Files changed.
- Migrations created/applied, if any.
- Tests executed and results.
- End-to-end scenarios validated.
- Any regressions found.
- Unresolved risks.
- Explicit GO or NO-GO recommendation for Prompt 33.

Stop after completing this phase and wait for review and explicit authorization.

## Prompt 33 — Read-Only Legacy Cleanup Eligibility Audit

Objective: run a read-only audit to determine whether any legacy custom CPI artifacts remain and whether a future separate cleanup plan is eligible. Do not perform cleanup in this plan.

Inspect first:
- Prompt 1 audit results.
- Current database after all prior prompts.
- Legacy CPI creation paths and whether they were removed/disabled for new custom items.
- RateTypes, PayItemRateTypeMap, PayItemSettings, DriverRates, DriverRateTiers, draft/final lines, final snapshots, audit/history tables, reports/exports.

In scope:
- Run read-only queries identifying legacy custom CPI RateTypes, mappings, `rate_name_*` settings, legacy custom Pay Items, driver rates, draft lines, final lines, and snapshots referencing legacy custom artifacts.
- Confirm system/default RateTypes remain in use and must not be deleted.
- Report whether any legacy custom CPI artifacts are unused and potentially eligible for cleanup in a future separately authorized plan.
- Confirm no finalized historical payroll, ledger/history output, report, export, or audit display depends on any candidate object.
- Produce recommended cleanup prerequisites, not cleanup steps.

Out of scope:
- No deletion.
- No deactivation.
- No migration.
- No historical rewrite.
- No app behavior change.

Behavior that must not change:
- All data remains untouched.

Automated tests:
- No new tests required unless an audit helper is added. If an audit helper is added, test it with fixtures.

Manual/database verification:
- Provide exact read-only SQL queries and results.
- Clearly separate system/default RateTypes from legacy custom CPI artifacts.

Rollback/safe reversal:
- No data changes are made.

Completion report required:
- Files changed, if any.
- Migrations created/applied: none expected.
- Tests executed and results.
- Read-only audit queries and results.
- Cleanup eligibility findings.
- Explicit statement that cleanup is not authorized by this plan.
- Remaining risks.
- Explicit GO or NO-GO recommendation for any future, separately authorized cleanup plan.

Stop after completing this phase and wait for review and explicit authorization.

## Dependency Map

- Prompt 1 blocks all later prompts that rely on the empty-database condition, especially Prompts 10, 11, 19, 32, and 33. If unexpected legacy custom or payroll data exists, those prompts must stop with NO-GO until reviewed.
- Prompt 2 blocks NMR, review, InReview read-only, Return/Reopen, impact enforcement, and finalization behavior changes in Prompts 21 through 29.
- Prompt 3 should be completed before evidence/finalization work so the isolated finalization rate-selection bug is not buried inside the redesign.
- Prompt 4 is required before Prompts 5 through 19.
- Prompt 5 is required before Prompt 8 because structural first-use immutability references parent slot-rate tables.
- Prompt 6 is required before Prompt 8 because permanent draft-line first-use detection must rely on direct `PayItemID` references rather than ambiguous line codes where possible.
- Prompt 7 is required before Prompt 8 because final-line first-use detection must have model-aware final references before immutable structural guards are created.
- Prompt 8 structural immutability should be in place before slot rates or payroll lines can use a slot-based Pay Item.
- Prompt 9 approved-version immutability and protected supersession is required before lifecycle prompts can safely approve/supersede slot rates.
- Prompts 10 and 11 are required before creating real slot-based custom Pay Items through direct settings or request/approval flows.
- Prompts 13 through 16 provide lifecycle operations required by resolver and payroll integration tests.
- Prompt 14 depends on Prompt 9 because approval must use the concrete protected supersession mechanism.
- Prompt 15 intentionally permits PendingApproval voiding only and blocks Approved/Superseded voiding until Prompt 30.
- Prompt 16 depends on same-PayItem copy semantics and must not allow target PayItem substitution.
- Prompts 17 and 18 are required before payroll draft integration in Prompt 19.
- Prompt 20 is required before effective-date impact enforcement in Prompt 25 because Open-period impact handling must selectively recalculate affected WorkDates.
- Prompts 21 and 22 are required before Prompt 25 can tell users how to unblock InReview or Approved periods.
- Prompt 23 should be completed before evidence freeze and approval/finalization prompts so InReview data cannot silently mutate.
- Prompt 24 is required before Prompt 25 because impact enforcement must use operation-specific affected intervals.
- Prompt 25 applies impact protection to both slot rates and legacy/system DriverRates, but must not enable historical Approved/Superseded slot void.
- Prompts 26 and 27 are required before Preview and Finalization can rely on reviewed evidence.
- Prompt 28 must not be combined with Prompt 29; preview and finalization must be independently testable.
- Prompt 30 depends on Prompts 20 through 29 and is the first phase allowed to enable safe Approved/Superseded slot void.
- Prompt 31 must occur before final regression so ledger/history/report/export compatibility fixes are not deferred to a regression-only phase.
- Prompt 32 depends on the implementation prompts but must not perform cleanup.
- Prompt 33 is read-only and depends on the completed implementation and audit results.

## Highest-Risk Phases

- Prompt 3: finalization rate-selection bug fix touches payroll money movement and must remain narrowly scoped.
- Prompt 5: branch-scoped effective-dated schema and constraints must support historical Superseded resolution without cross-branch conflicts.
- Prompt 6 and Prompt 7: draft/final references must be safe and fail-closed because later immutability depends on direct references.
- Prompt 8: structural immutability must be correct because later payroll evidence depends on stable slot definitions and must detect all first-use categories.
- Prompt 9: approved-version immutability must prevent direct DB mutation while allowing controlled supersession only through a concrete enforceable mechanism.
- Prompt 10 and Prompt 11: switching new custom Pay Item creation away from legacy CPI is safe only if Prompt 1 proves no legacy custom usage.
- Prompt 17: WorkDate resolver must include Approved and Superseded effective historical versions, exclude Voided/Pending versions, and fail closed on gaps or overlaps.
- Prompt 19: draft calculation integration can affect payroll entry behavior and must keep system/default RateType paths unchanged.
- Prompt 23: InReview read-only enforcement must cover every backend write path, including indirect service calls.
- Prompt 24: operation-specific impact detection must be correct across slot rates and legacy/system DriverRates.
- Prompt 25: impact enforcement applies to every payroll-effective rate path across both architectures.
- Prompt 26: Open -> InReview evidence storage must avoid stale or silently mutable reviewed calculations.
- Prompt 28: preview must not recalculate or re-resolve newer rates.
- Prompt 29: finalization must exactly match preview and preserve reviewed evidence.
- Prompt 30: historical Approved/Superseded void can corrupt history if dependency checks, gap blocking, or audit preservation are incomplete.
- Prompt 31: ledger/history/report/export compatibility must avoid hidden legacy-only joins that omit or duplicate slot lines.

These prompts should never be combined into one implementation session: Prompts 3, 5, 6, 7, 8, 9, 10, 11, 14, 17, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, and 31.

## Production-Data Safety Gates

- The database is expected to be empty of real payroll/custom-rate data, but that is only an expected condition. Prompt 1 must prove it with read-only database queries.
- If Prompt 1 finds real drivers, payroll periods, entries, driver rates, custom Pay Items, legacy CPI custom rates, custom draft lines, custom final lines, or finalized payroll history, later prompts that depend on emptiness must stop with NO-GO.
- No prompt may delete, deactivate, or convert legacy CPI data based only on the stated expectation that the database is empty.
- Permanent integrity rules remain mandatory even in an empty single-user environment: branch-scoped identity, no-overlap protection, slot completeness, fail-closed draft PayItemID backfill, model-aware final references, structural immutability, approved-version immutability with concrete protected supersession only, Approved/Superseded WorkDate resolution, WorkDate-based mid-period rate changes, operation-specific impact intervals, InReview read-only protection, audited Return/Reopen workflows, impact protection across all rate models, reviewed-evidence preview/finalization, strict historical void dependency checks, gap blocking by default, ledger/report/export compatibility, and finalized-history immutability.
- Temporary multi-user rollout complexity is intentionally removed: no production feature flag is required, and there is no staged user rollout requirement in this plan.
- Approved and Superseded slot-rate voiding is not permitted before Prompt 30. After Prompt 30, it is allowed only when no final line, Locked/Archived/finalized payroll, InReview/Approved period, reviewed evidence, final SourceSnapshot, ledger/history record, or other historical payroll evidence depends on the exact version; affected Open lines can recalculate safely; the resulting timeline remains valid; and no rate gap is created.
- Rate gaps caused by historical void are blocked by default. No prompt may silently extend neighboring versions or fall back to an older rate outside its original effective range.
- Locked, Archived, and finalized payroll protections implemented in DB triggers/constraints must remain intact or be strengthened. They must never be weakened for convenience.

## Legacy Cleanup Authorization Statement

Legacy cleanup is not authorized by this plan.

If Prompt 1 confirms zero legacy custom usage, the plan permits stopping new legacy CPI-based custom Pay Item creation and making new custom Pay Items slot-only. That is not cleanup. It does not authorize deleting CPI RateTypes, deactivating RateTypes, removing PayItemRateTypeMap rows, removing PayItemSettings, rewriting DriverRates, rewriting draft lines, rewriting final lines, rewriting ledger/history/report/export evidence, or modifying historical finalized payroll.

Prompt 33 may perform only a read-only cleanup eligibility audit. Any actual cleanup requires a future separately authorized plan after the new model is proven stable and the audit confirms no data depends on the old custom CPI artifacts. System/default RateTypes are not legacy custom cleanup candidates and must remain on the existing RateType-based model.
