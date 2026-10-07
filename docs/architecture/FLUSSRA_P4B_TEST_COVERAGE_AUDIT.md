# P4b Test Coverage Audit — suites removed by the target cutover

**Status:** P4b review correction. Companion to `FLUSSRA_UNIFIED_REFOUNDATION_EXECUTION_PLAN.md`.

P4b is a clean pre-production cutover. The legacy PayItem / RateType / DriverRate runtime
and its PayrollPeriodPayItems snapshot no longer exist, and submit, resubmit, finalize and
finalization preview are closed (`TARGET_PAYROLL_EVIDENCE_NOT_READY`) until P4c. Test
modules that only characterized that retired authority were removed. Every assertion of
every removed module was classified:

| Class | Meaning | Action |
| --- | --- | --- |
| `OBSOLETE_AUTHORITY` | Characterizes the retired legacy runtime (PayItems, RateTypes, legacy DriverRate matrix/tiers, PayrollPeriodPayItems, LineType routing) | Not restored. Target equivalents of the *invariant* live in the P4b suites listed below. |
| `P4C_DEFERRED` | Needs immutable target calculation evidence (submit/resubmit success, snapshot capture, approval binding, finalization, frozen reports, finalized library) | Not restorable now. P4c must rebuild it against target evidence. |
| `STILL_LIVE` | A contract that survives P4b (status guards, source-mutation guards, workflow slots, permissions and scope, eligibility, validation, audit atomicity, DB immutability, read boundaries, DriverPayRule, workforce) | Ported to the target model or confirmed already covered by a surviving suite. |

## Removed modules

| Removed module (tests at `83234d4`) | Class | Where the STILL_LIVE part lives now |
| --- | --- | --- |
| `test_cp0_linetype` (13) | OBSOLETE_AUTHORITY | LineType routing replaced by `PayrollPeriodDefinitionID`: `test_p4b_source_lines` |
| `test_cp0a_mutation_status_guard` (23) | STILL_LIVE | `test_p4b_source_mutation_guards` (status matrix, rejected writes leave no row/audit); true-race coverage survives in `test_cp0d_concurrency_regression` |
| `test_cp1a_returned_lifecycle` (30) | STILL_LIVE / P4C_DEFERRED | Schema/pointer/slot invariants, reserved PATCH, cancellation, Returned editability, return decisions: `test_p4b_source_mutation_guards`; resubmit success paths P4C_DEFERRED |
| `test_cp1d_submit_promotion` (33) | P4C_DEFERRED | Submit/promotion/backlog/serialization depend on submit evidence. Candidate creation, `Draft→Open` removal and slot checks survive in `test_cp1c_candidate_creation`, `test_cp0b_transition_safety`, `test_cp1b_inreview_slot` |
| `test_cp2c_pay_item_snapshot` (31) | OBSOLETE_AUTHORITY | Snapshot semantics (retired/inactive/rename/reorder stability, no fallback, immutability, replay): `test_p4b_period_creation`, `test_p4b_migration` |
| `test_cp2f_prepared_operational_entry` (43) | STILL_LIVE | Draft source-only behavior, blocked financial surfaces, hub capabilities: `test_p4b_source_mutation_guards`, `test_p4b_source_lines` |
| `test_cp4b_calculation_preview` (56) | STILL_LIVE / OBSOLETE | Lifecycle, permission and scope, read-only, driver union, Bonus: `test_p4b_preview_report_contract`; legacy PerUnit/Status-rate parity is OBSOLETE (`test_p4b_live_calculation`); Min/Max in `test_cp3c_minmax_bonus`; the rest was test-harness cleanup code |
| `test_cp4d_submit_snapshot_capture` (24), `test_cp4e_approval_snapshot_binding` (14), `test_cp4f_finalization_snapshot_projection` (11) | P4C_DEFERRED | Snapshot schema/immutability survives in `test_cp4c_snapshot_schema` |
| `test_cp5_calc_consistency` (11) | STILL_LIVE / P4C_DEFERRED | Eligibility by date, source survives termination, work-date rate lookup: `test_p4b_day_grid_contract`, `test_p4b_live_calculation`; submit/finalize refresh P4C_DEFERRED |
| `test_cp5c_frozen_report_evidence` (8) | P4C_DEFERRED | — |
| `test_cp5c_reports` (35) | STILL_LIVE / P4C_DEFERRED | Live report authority, reconciliation, permissions, leak and closed states: `test_p4b_preview_report_contract`; frozen authorities fail closed there; frozen content P4C_DEFERRED |
| `test_cp6_review` (19) | STILL_LIVE / P4C_DEFERRED | Return decisions, scope, permissions: `test_p4b_source_mutation_guards`, `test_review`; approval binding P4C_DEFERRED |
| `test_day_grid` (78) | STILL_LIVE / OBSOLETE | Roster, bounds, validation, Status keys and limits, audit, access: `test_p4b_day_grid_contract`; PayItem/CDPI/legacy-duplicate column behavior is OBSOLETE (`test_p4b_source_lines` for definition identity) |
| `test_entry` (33) | STILL_LIVE | `test_p4b_entry_contract` |
| `test_finalization_preview` (26), `test_finalize` (23), `test_ledger` (19) | P4C_DEFERRED / STILL_LIVE | Permission-before-gate ordering and gate: `test_p4b_period_read_contract`, `test_p4b_evidence_gates`; final-line DB guarantees and read boundary: `test_p4b_final_ledger_integrity` |
| `test_m13a` (31), `test_m13c` (62) | OBSOLETE_AUTHORITY | PayItem validation and legacy tier calculation. Target equivalents: `test_p4b_source_lines`, `test_p4b_live_calculation`, `test_p3c_*`. OrdinalTier calculation is not operational (`test_p4b_resolver`) |
| `test_m16` (25) | P4C_DEFERRED / STILL_LIVE | Submit/audit success is P4C_DEFERRED; self-approval and non-period items survive in `test_review` |
| `test_p4a_period_creation_hold` (2) | OBSOLETE | The hold was removed by P4b: `test_p4b_period_creation` |
| `test_p6a_finalized_library` (11), `test_p6b_finalized_off_drivers` (6), `test_p6c_finalized_rates_used` (4), `test_p6d_finalized_audit` (24), `test_phase6_immutable_evidence` (3), `test_report_authority_resolver` (7) | P4C_DEFERRED | Access boundary, no leaks, fail-closed: `test_p4b_final_ledger_integrity`. Evidence-bearing assertions P4C_DEFERRED |
| `test_pay_rates` (81), `test_rates` (62), `test_rate_calc_boundaries` (13) | OBSOLETE_AUTHORITY | Legacy DriverRate matrix/CRUD/copy. The old routes are denied (`test_p4a_authority_cutover`); target authoring in `test_p3c_driver_rate_assignments`; Status rates in `test_cp2d2_status_payment`, `test_cp5b_off_drivers` |
| `test_payroll` (36) | STILL_LIVE / OBSOLETE | List/detail/filter/paging/permissions: `test_p4b_period_read_contract`; transitions in `test_cp0b/c`; creation in `test_cp1c` |
| `test_payroll_trust_p1` (9) | STILL_LIVE | Duplicate protection, idempotent grid: `test_p4b_source_lines` |
| `test_payroll_trust_p2` (15) | STILL_LIVE | Eligibility on write paths: `test_p4b_entry_contract`, `test_p4b_day_grid_contract`; finalization eligibility P4C_DEFERRED |
| `test_payroll_trust_p3b` (5), `p9` (7), `p11` (9), `p12` (13) | OBSOLETE_AUTHORITY / P4C_DEFERRED | Final-line source snapshots and legacy tier/rate locking |
| `test_payroll_trust_p3c` (9), `p5` (6), `p6` (5) | STILL_LIVE | Final-line immutability, Locked/Archived transitions, controlled insert, used-DriverRate guard: `test_p4b_final_ledger_integrity` |
| `test_payroll_trust_p4b` (13), `p4c` (11), `p7` (5), `p8` (5) | OBSOLETE_AUTHORITY | RateType ownership/mapping, fail-closed PayItem rate behavior |
| `test_phase2c_additions` (17), `test_phase2c_final` (38) | STILL_LIVE / OBSOLETE | DriverPayRule contract: `test_p4b_driver_pay_rules`; driver branch immutability: `test_workforce_driver_branch_immutability`; copy-rates, bulk summary and rate-matrix parts are OBSOLETE_AUTHORITY |
| `test_phase4_characterization_slice1` (17) | OBSOLETE / covered | PerUnit kernel and Decimal context: `test_cp4a_perunit_core`; four-decimal precision: `test_p4b_live_calculation` |
| `tests/legacy_shim.py` | OBSOLETE_AUTHORITY | Test-only emulation of the retired activation route |

## P4c rebuild list

P4c must rebuild, against immutable target evidence: submit/resubmit success and rollback,
Draft promotion on submit, serialization and lock boundaries of submit, snapshot capture
and hash reconciliation, approval binding to the submitted packet, finalization
projection and rollback, finalization preview, frozen report evidence, the finalized
library, finalized Off-drivers/rates-used/audit content, and workflow capability success
cases. The refusal tests in `test_p4b_evidence_gates` are removed or inverted by that work
unit, and only there.
