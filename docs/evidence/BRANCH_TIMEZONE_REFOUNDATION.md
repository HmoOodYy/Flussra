# Branch Timezone Refoundation — Implementation Evidence

**Implementation branch:** `codex/branch-scoped-timezone`

**Baseline:** `63acc7f1efb0204613d72cf4de80008468fcd474` (`main` and `origin/main`)

**Dedicated worktree:** `C:\Projects\etbdnt\Payroll_App_v3\.artifacts\worktrees\branch-timezone`
**Architecture:** [Branch Timezone Authority Contract](../architecture/BRANCH_TIMEZONE_AUTHORITY.md)

## Baseline and forensic findings

- Before preparation, there was one worktree (main), one local branch (main), no worktree/branch collision at the requested target, and the main checkout was clean. The dedicated ignored `.artifacts/worktrees/branch-timezone` path was created from `origin/main` without changing the root checkout. No `.env` was copied.
- Remote `origin` was `https://github.com/HmoOodYy/Flussra.git`; `origin/main` resolved to `63acc7f1efb0204613d72cf4de80008468fcd474`. Parent investigation found no open PRs and reviewed merged PRs #55, #54, #53, #52 and #50 for recent test ownership and cleanup context.
- At the pre-cutover `0085` catalog, parent measured 128 `TIMESTAMPTZ` columns, 48 `DATE` columns and zero timestamp-without-time-zone/time columns. The only business timezone storage/clock authority was `core.Companies.TimeZoneName` and `core.fn_CompanyToday`; `fn_CompanyToday` was the only stored database clock. Python callers duplicated the Company clock in Payroll Setup, Workforce, compensation and inline SQL; host-local `date.today()`/`datetime.date.today()` and session `CURRENT_DATE` remained in selected payroll/default paths.
- Existing event timestamps were already absolute `TIMESTAMPTZ`; business DATEs were already DATE. No branch-local datetime values were found. Migration `0086` preserves that split and replaces only Driver assignment windows with instants.

## Authority changes and code map

| Area | Change classification | Implementation locations |
|---|---|---|
| Company timezone and clock | DELETE | `core.Companies.TimeZoneName`, `core.fn_CompanyToday`, Company profile/update schemas and UI; `payroll_setup/clock.py`, `workforce/clock.py`, compensation private/inline Company clocks |
| Branch timezone | ADD/MODIFY | `core.Branches.TimeZoneId`, supported identity registry, required create and omission-preserves / explicit-null-rejected patch contract, validation/audit, `backend/app/business_clock.py`, settings catalog endpoint, branch form and overview |
| Driver profile intervals | REPLACE | DATE `EffectiveFrom`/`EffectiveTo` and DATE resolver/range overloads replaced by `EffectiveFromAt`/`EffectiveToAt TIMESTAMPTZ`, shared instant resolver, half-open exclusion, current projection/admin/Self consumers |
| Transfer request timing | REPLACE | DATE `EffectiveDate` replaced by optional `EffectiveAt`; immediate completion resolves once using database `NOW()`, explicit schedule must be future; source/destination share the same boundary instant |
| Payroll eligibility | REPLACE | Snapshot interval DATE columns replaced by instant bounds, captured Branch timezone and exact `EligibleWorkDates DATE[]`; no no-marker live roster fallback; lifecycle WorkDate checks use captured timezone ranges |
| Payroll Setup and compensation clocks | MODIFY | Company-owned schedule chronology remains explicit DATE; optional Branch context supplies current-version reference; rates, applicability and Status defaults use owner Branch business date; Bulk operations resolve inside each Branch loop |
| Daily entry / off drivers | MODIFY | Server Branch WorkDate defaults and pure ISO calendar operations; initial off-driver request omits WorkDate and adopts server response |
| Frontend configuration | MODIFY | No Company timezone input; Branch timezone comes from server catalog; no browser date defaults/minimums in pay-rate or Pay Item default flows; policy version current/upcoming labels are Branch-contextual |
| Event presentation | KEEP | Event timestamps remain browser-local presentation; date-only values remain ISO strings and use pure ISO helpers |
| Compensation/finalization | KEEP | Explicit rates, WorkDate, pay rules, frozen evidence and P4c evidence/finalization gate are not redesigned |

Runtime entry points: `backend/app/main.py::create_app` wires settings, Payroll Setup, payroll, Workforce, transfer, compensation, core and dashboard routers, then runs `run_schema_guard` at startup. `frontend/src/main.tsx` renders `App.tsx`; its `BrowserRouter` routes to the affected settings, Payroll, Pay Rates and transfer pages. These entry points were inspected and did not require changes.

### Clock dependency and temporal authority graph

| Domain/read | Caller and shared helper | Conversion source | Authority / outcome |
|---|---|---|---|
| Branch/settings and core summaries | Settings services use `business_clock.branch_today`; core summaries and settings `BRANCH_COLUMNS` call `core.fn_BranchToday` directly | `core.fn_BranchToday` → `core.fn_BusinessDate` | Branch timezone; missing Branch has no Company fallback |
| Prospective Branch onboarding | `settings/branches.py`, Payroll Setup onboarding choices → `business_clock.prospective_today` | `core.fn_BusinessDate` | Explicit selected Branch timezone; suggestions wait for it |
| Payroll Setup version classification | `payroll_setup/reads.py::list_versions(branch_id=...)` | `core.fn_BranchToday` | Company-owned schedule DATEs; currentness is NULL without selected Branch context |
| Payroll Setup assignment/onboarding | `payroll_setup/boundaries.py`, `payroll_setup/payroll_policy.py` | Branch/prospective business-clock wrapper | Owning Branch date; Company publication choices have no implicit today |
| Periods, setup versions, rates and compensation definitions | Payroll Setup chronology, `compensation/assignments.py`, `compensation/branch_config.py` | Explicit DATE inputs and effective dates | DATE-owned configuration chronology; never inferred from browser or Company clock |
| Daily entry, off/status, Bonus and eligibility | `payroll/day_grid.py`, `off_drivers.py`, `eligibility.py`, `bonus.py` | Branch WorkDate ranges from SQL; frozen roster arrays where captured | Explicit BranchID + WorkDate; captured timezone freezes historical roster interpretation |
| Driver current identity, Employee projection, DRIVER Self and Branch people counts | `workforce/effective.py`, `projection.py`, `access/policy.py`, admin linked-profile reads, `settings/branch_reads.py` | `core.fn_EffectiveDriverProfile` at one DB transaction instant; settings counts reuse the read-only projection expression | Instant assignment `[from,to)`; one boundary switches current-identity consumers while settings derives counts without writing Employee projection |
| Transfer request and completion | `transfer/service.py` | One persisted `EffectiveAt TIMESTAMPTZ`; immediate resolves with DB `NOW()` at completion | No midnight coercion and no payroll/pay generation |
| Payroll assignment eligibility | Snapshot capture and lifecycle guard in `payroll/eligibility.py` | `core.fn_WorkDateRange` + instant assignment overlap | Any overlap with Branch-local calendar day permits that captured WorkDate |
| Finalized payroll evidence | Calculation/finalized read models | Stored immutable DATE and TIMESTAMPTZ evidence | Frozen facts; P4c finalization gate remains active |
| Audit and system metadata | Audit writers and framework metadata | Aware event instants / `TIMESTAMPTZ` | Absolute instants; display timezone is presentation only |

### Test disposition

| Disposition | Scope and examples |
|---|---|
| KEEP | Monetary calculations, explicit effective dates, date chronology/resolver/readiness, RBAC, lock ownership, source immutability, frozen evidence, and the P4c gate |
| MODIFY | Shared Branch fixtures/builders and API payloads now supply explicit timezone; Workforce/Transfer fixtures use aware instants; Setup tests select a Branch context when asserting currentness; test filenames remain historical labels where they describe completed phases |
| REPLACE | Company-clock/current-identity assertions become Branch-clock or exact instant tests; date-window Driver resolver tests become instant range and Branch WorkDate overlap assertions; snapshot tests assert eligible DATE arrays and captured timezone |
| DELETE | Runtime Company clock helpers and the pre-snapshot live-roster fallback plus their exclusively-dead production helpers/tests; missing marker now returns a structural conflict, while a valid empty marker remains empty |

Concrete affected test inventory (all other assertions retained unless specifically described by the disposition above):

- Frontend pure/static contracts: `companyBranchesU7.test.ts`, `payrollBoundaryView.test.ts`, `policyDetailPanel.test.ts`, `policyDetailView.test.ts`, `transferPresentation.test.ts`, `versionPlanningSection.test.ts`, `versionPlanningView.test.ts`.

- Timezone/API/schema guard: `test_branch_timezone.py`, `test_branch_timezone_api.py`, `test_schema_guard_payroll_trust.py`, `test_settings.py`, `test_settings_payroll.py`, `test_route_authorization_inventory.py`.
- Shared fixtures, ownership and historical head pins: `tests/builders/company.py`, `tests/builders/owned_scope.py`, `compensation_database_fixtures.py`, `conftest.py`, `currency_test_helpers.py`, `ownership.py`, `test_access_self_migration.py`, `test_cp1b_inreview_slot.py`, `test_cp1c_candidate_creation.py`, `test_cp1e_workflow_capabilities.py`, `test_cp2a_schedule_versioning.py`, `test_cp2b_period_days.py`, `test_db_integrity.py`, `test_p3b_target_compensation_migration.py`, `test_p3c_governance_migration.py`, `test_p4b_migration.py`, `test_payroll_setup_phase1_migrations.py`, `test_payroll_setup_phase1_schema.py`.
- Compensation and policy fixture adaptation: `test_p3a_company_currency_authority.py`, `test_p3a_currency_concurrency.py`, `test_p3a_currency_migration.py`, `test_p3a_transaction_retry.py`, `test_p3b_target_compensation_concurrency.py`, `test_p3b_target_compensation_schema.py`, `test_p3c_compensation_concurrency.py`, `test_p3c_compensation_resolver.py`, `test_p3c_driver_rate_assignments.py`, `test_p3c_pay_definition_governance.py`, `test_p4a_activation_policy.py`, `test_p4a_authoring_authority.py`, `test_p4a_authority_cutover.py`, `test_p4a_branch_applicability.py`, `test_p4a_migration.py`, `test_p4a_pay_rates_read_model.py`.
- Payroll eligibility, snapshots, Bonus and daily/off/status entry: `test_cp25_drivers_off.py`, `test_cp2d2_status_payment.py`, `test_cp2d_canonical_entry_state.py`, `test_cp2e_eligibility_snapshot.py`, `test_cp3a_bonus_events.py`, `test_cp3b1_bonus_summary.py`, `test_cp3b2a_bonus_batch_safety.py`, `test_cp3b2b_bonus_batch.py`, `test_cp3c_minmax_bonus.py`, `test_cp4a_perunit_core.py`, `test_cp4c_snapshot_schema.py`, `test_cp5a_current_hub.py`, `test_cp5b_off_drivers.py`, `test_payroll_bonus_lifecycle.py`, `test_payroll_status_characterization.py`.
- Workforce, transfer, access, dashboard and rates: `test_dashboard.py`, `test_dashboard_api.py`, `test_driver_transfer_workflow.py`, `test_g03_company_concurrency.py`, `test_p1a_workforce_integrity.py`, `test_p1c_transfer_termination_concurrency.py`, `test_p1c_transfer_termination_cutover.py`, `test_p2a_access_provisioning.py`, `test_p2b_self_authorization.py`, `test_p3a_acceptance_matrix.py`, `test_p3a_lifecycle_retry_integration.py`, `test_security_matrix.py`, `test_workforce_employees.py`.
- Payroll Setup clock/context/onboarding: `test_payroll_setup_boundary_choices.py`, `test_payroll_setup_branch_summaries.py`, `test_payroll_setup_current_version.py`, `test_payroll_setup_default_requires_published.py`, `test_payroll_setup_onboarding_window.py`, `test_payroll_setup_phase2_concurrency.py`, `test_payroll_setup_phase3_concurrency.py`, `test_payroll_setup_phase4_api.py`, `test_payroll_setup_phase4_default_lock.py`, `test_payroll_setup_phase5_onboarding.py`, `test_payroll_setup_phase6_readiness_date.py`.
- Payroll Setup and workflow fixture preservation: `test_payroll_setup_days_off_limit.py`, `test_payroll_setup_generated_code.py`, `test_payroll_setup_phase2_domain.py`, `test_payroll_setup_phase2_helpers.py`, `test_payroll_setup_phase3_creation.py`, `test_payroll_setup_phase3_hub.py`, `test_payroll_setup_phase3_legacy_routes.py`, `test_payroll_setup_safety.py`, `test_payroll_setup_phase8_migration.py`.
- P4b explicit-date and immutable-evidence regressions: `test_p4b_day_grid_contract.py`, `test_p4b_driver_pay_rules.py`, `test_p4b_entry_contract.py`, `test_p4b_evidence_gates.py`, `test_p4b_final_ledger_integrity.py`, `test_p4b_layout_coherence.py`, `test_p4b_live_calculation.py`, `test_p4b_period_creation.py`, `test_p4b_period_read_contract.py`, `test_p4b_preview_report_contract.py`, `test_p4b_resolver.py`, `test_p4b_source_lines.py`, `test_p4b_source_mutation_guards.py`.

The modified test modules preserve monetary calculations, access outcomes, rollback guarantees, lock ownership, explicit-date chronology and frozen evidence. Company-clock tests were replaced with Branch-authority tests; DATE-based current-Driver tests were replaced with instant-boundary tests; live-roster fallback tests were replaced with missing-marker conflict and valid-empty-marker tests.

## Locked temporal rules

- One absolute `EffectiveAt TIMESTAMPTZ` moves current Driver identity. Immediate transfers resolve at completion `NOW()` and scheduled transfers use one explicit future instant.
- A Driver can enter payroll/activity for a Branch `WorkDate` if any instant assignment overlaps that Branch's local calendar day. Transfer creates no payroll/pay rows, split, proration or automatic carry-forward.
- Termination remains a DATE; each profile cutoff is `core.fn_LocalDateBoundary(profile_branch_zone, TerminationDate + 1)`, including when that Branch's termination date is a skipped local day.
- Captured eligibility WorkDate arrays and captured Branch zones never change after capture, including after Branch timezone editing. Lifecycle checks still restrict new source by current assignment overlap using the captured zone.
- A destination profile may be appended to existing editable, already-marked periods through the shared snapshot capture implementation; preexisting rows remain frozen. No reads mutate snapshots. Lock order is Branch workflow lock, then period locks ascending.
- No-marker periods fail closed with a structural error. Empty marker is a valid empty roster. New Bonus authority also requires a captured WorkDate still overlapping a live nonempty assignment.

## Migration and equivalence

Migration `0086` is intentionally a clean preproduction cutover. It checks before any DDL that `core.Drivers`, `core.DriverTransferRequests` and `payroll.PayrollPeriods` are empty; otherwise it raises `BRANCH_TIMEZONE_REBUILD_REQUIRED` atomically. It initializes each existing Branch zone from its owning Company's old zone, only validates Companies that actually have Branches, and fails with corrective instructions for an invalid legacy Branch value. It does not invent instant history or backfill old payroll evidence. Direct SQL bootstrap and Alembic use the same SQL migration file; both are required to produce equivalent end catalogs.

The allowed identifier registry is seeded once from PostgreSQL `pg_timezone_names` using the approved UTC/regional-prefix subset. An index-backed table avoids per-date catalog enumeration in capture; Branch and captured-zone foreign keys preserve allowed identities. SQL helpers perform timezone calculations, including local-midnight folds and skipped dates.

## Performance evidence

Disposable capture measurement for one 31-day period and 100 Driver profiles (3,100 candidate WorkDates):

- Per-Driver/day implementation exceeded 90 seconds and was interrupted; this is only a lower bound.
- Set-based materialized WorkDate windows with runtime catalog validation measured 46.724 seconds.
- Approved indexed `core.SupportedTimeZones` registry plus materialized per-period WorkDate windows measured 0.042 seconds for the same fixture, approximately 1,100× faster than the measured 46.724-second version.

No runtime cache or unchecked timezone conversion path was added.

## Implementation notes

Detailed file/symbol inventory and verification results are maintained here during implementation. The first backend milestone included `migrations/sql/0086_branch_timezone_authority.sql`, `migrations/versions/0086_branch_timezone_authority.py`, canonical SQL helpers, instant assignment/transfer schema, snapshot capture, and Branch clock wrappers. Subsequent additions cover settings and Payroll Setup contracts, compensation/payroll date defaults, workforce/admin/access transfer identity, frontend Branch/Payroll Setup/rates/pay-items/daily entry/transfers, schema drift guard, tests and docs.

## Validation record

The canonical run used `scripts/validate.ps1` from the dedicated worktree. Its transcript is `C:\Temp\branch_timezone_validation_after_projection.txt`.

- Backend suite: **2,154 passed, 1 skipped, 2 warnings** in 958.39 seconds. The skip was `tests/test_dashboard_api.py:624` because HQ has no payroll settings prerequisite. The two pytest warnings are pre-existing synchronous tests marked `asyncio`: `test_p4a_activation_policy.py:59` and `test_p4a_branch_applicability.py:172`.
- Canonical backend Ruff (`ruff check --no-cache tests`), backend `compileall`, and Alembic head `0086`: **passed**.
- Frontend suite: **901 passed, 0 failed** across 39 files. Frontend lint passed with one existing warning in `src/pages/DashboardPage.tsx:180` about `setWarnings`; the production build and TypeScript check passed. Vite emitted its bundle-size advisory for the 921.68 kB minified JavaScript bundle.
- Additional Ruff command, run from `backend/`: `.venv/Scripts/python.exe -m ruff check app tests --no-cache` — **passed**. This includes application code beyond the canonical test-only Ruff gate.
- `scripts/validate_schema_authorities.ps1`: **passed**, with zero direct-SQL/Alembic differences across 9 schemas, 78 tables, 1,137 columns, 78 primary keys, 281 foreign keys, 51 unique constraints, 177 checks, 7 exclusions, 286 indexes, 1,190 triggers, 316 functions, 7 views and 68 sequences.
- Fresh disposable direct-SQL bootstrap through `0086` measured **133 `TIMESTAMPTZ`, 43 `DATE`, and 0 timestamp/time-without-time-zone columns**. The focused catalog test also checks named event/WorkDate/configuration columns rather than relying on counts as semantic assertions.
- Focused semantic evidence includes Branch timezone catalog/API contracts, five-zone same-instant conversion, DST and skipped-day ranges, captured-zone WorkDate eligibility, exact transfer boundary/current-identity behavior, frozen snapshot/appender behavior, no-copy transfer rules, and read-only settings projection metrics. The measured 31-day / 100-Driver snapshot capture was **0.042s**.
- Final whitespace checks: `git diff --check` and `git diff --cached --check` both **passed** after staging the complete task change set.

No repository formatter is configured; no wholesale reformatting was performed. P4c remains incomplete by design and its finalization gate is unchanged.

## Deferred release gate

P4c remains incomplete by design. Target payroll submit, resubmit, finalization and immutable evidence stay blocked by `TARGET_PAYROLL_EVIDENCE_NOT_READY`. This timezone/identity cutover does not remove that gate or claim financial finalization completion.
