# P3a acceptance matrix evidence

This is the traceability index for the Lead-locked P3a acceptance rows. Existing
configured-path tests are referenced where they already exercise the writer;
the P3a currency gate itself is covered by the shared authority tests and the
durable-state matrix. This index does not replace the tests.

## Durable Company currency lock

`test_p3a_acceptance_matrix.py::test_each_durable_monetary_state_locks_company_currency`
is table-driven over: DriverRate PendingApproval, Approved, Superseded, and
Voided; DriverRateTier; active and voided BonusEvent; active, ended, and voided
DriverPayRule; RateAmount, CalculatedAmount, and voided monetary DraftLine;
PayrollCalculationSnapshot; PayrollFinalLine; and voided PayProfileRate. Each
applicable cleanup transition is followed by a second attempted Company
currency change. `test_unconfigured_company_with_durable_state_fails_closed`
constructs Company.CurrencyCode NULL with a monetary DraftLine and verifies
both settings configuration read and currency read return
`COMPANY_CURRENCY_INVARIANT_VIOLATION`.

## Reachable writer and lifecycle rows

| Acceptance rows | Focused proof |
|---|---|
| DriverRate scalar create, amount edit, tier edit, batch save, approve/supersede, copy, and copy with pay rules | `test_rates.py::TestCreateRate::test_create_minimal`, `TestUpdateRate::test_update_amount`, `TestApproveRate::test_approve_sets_status_and_approver`, `TestApproveRate::test_approve_supersedes_previous_approved`; `test_m13c.py::TestTierUpdate::test_update_tiers_on_pending_rate`; `test_cp2d2_status_payment.py::TestDriverPayRatesMatrix::test_batch_save_creates_status_pay_rate`; `test_phase2c_additions.py::TestCopyRates::test_copy_rates_same_branch`; `test_phase2c_final.py::TestCopyPayRulesEffectiveFrom::test_copy_pay_rules_uses_request_effective_from` |
| Bonus create/update, fresh batch, exact replay/list, notes-only mutation, and void cleanup | `test_cp0a_mutation_status_guard.py::TestPeriodPayMutationStatusGuard::test_add_bonus_open_succeeds`, `test_update_bonus_open_succeeds`, `test_void_bonus_open_succeeds`; `test_cp3b2b_bonus_batch.py::test_multi_driver_batch_succeeds_201`, `test_replay_same_key_same_payload_returns_200`, `test_get_bonuses_lists_created_events` |
| DriverPayRule create, end, notes-only, and void cleanup | `test_phase2c_final.py::TestPayRulesCRUD::test_create_minimum_pay`, `test_end_minimum_pay`, `TestPayRulesCurrentContract::test_end_void_and_notes_lifecycle_guards`, `TestPayRulesCRUD::test_void_active_rule` |
| Period Pay add, amount/notes update, and void | `test_cp0a_mutation_status_guard.py::TestPeriodPayMutationStatusGuard::test_add_line_open_succeeds`, `test_update_line_open_succeeds`, `test_void_line_open_succeeds`; snapshot-backed paths: `test_cp2c_pay_item_snapshot.py::TestCp2cPayItemSnapshot::test_s20_add_period_pay_line_accepts_bonus`, `test_s29_update_period_pay_line_snapshot_aware` |
| DraftLine monetary add/update, Prepared/Open/Returned source-only behavior, and configured calculations | `test_cp2c_pay_item_snapshot.py::TestCp2cPayItemSnapshot::test_s18_add_draft_line_accepts_hours`, `test_s28_update_draft_line_snapshot_aware`; `test_cp2f_prepared_operational_entry.py` Prepared source-entry tests; `test_cp4b_calculation_preview.py` Open/Returned and configured calculation tests; currency-free stale STATUS_PAYMENT handling is asserted directly by `test_p3a_acceptance_matrix.py::test_status_payment_without_currency_keeps_source_and_clears_stale_money` |
| Status remains nonmonetary without currency; no STATUS_PAYMENT projection without currency; stale projection clears; configured currency regenerates it | `test_p3a_acceptance_matrix.py::test_status_payment_without_currency_keeps_source_and_clears_stale_money`; configured creation/replacement/void: `test_cp2d2_status_payment.py::TestStatusPaymentLines::test_payment_line_created_with_rate`, `test_status_change_replaces_old_line`, and `test_status_cleared_voids_payment_line` |
| Submit and Resubmit require Company currency; whole-transaction retry | `test_p3a_lifecycle_retry_integration.py::test_submit_and_resubmit_require_company_currency`, `test_lifecycle_change_first_retries_and_freezes_new_currency`; runner SQLSTATE policy: `test_p3a_transaction_retry.py` and the central-policy characterization in `test_cp4d_submit_snapshot_capture.py` |
| Review approval requires frozen matching currency; mismatch fails closed; EditRequested remains nonmonetary | `test_cp4e_approval_snapshot_binding.py::test_snapshot_linked_pending_approval_keeps_exact_snapshot_and_creates_no_final_lines`, `test_approval_fails_closed_when_frozen_currency_mismatches_company`, `test_edit_requested_remains_nonmonetary_with_frozen_currency_mismatch` |
| Finalization match, mismatch, and missing immutable currency without Company repair | `test_cp4f_finalization_snapshot_projection.py::test_finalize_projects_exact_approved_snapshot_and_audits_provenance`, `test_finalization_fails_closed_on_frozen_currency_mismatch`, `test_finalization_never_repairs_missing_frozen_currency_from_company` |

The shared required-currency gate and the immutable Company change guard are
covered in `test_p3a_company_currency_authority.py` and the durable matrix
above. `test_p3a_acceptance_matrix.py::test_unauthorized_rate_request_cannot_probe_company_currency`
verifies authorization fails before a `COMPANY_CURRENCY_REQUIRED` response;
`test_cp3b2b_bonus_batch.py::test_driver_self_user_denied`,
`test_cp3b1_bonus_summary.py::test_driver_self_user_denied`, and
`test_p6a_finalized_library.py::test_driver_self_user_cannot_receive_ledger_override_or_read_finalized_routes`
preserve the DRIVER/Self ceiling.

## Frozen evidence and finalized currency reads

| Contract | Focused proof |
|---|---|
| Snapshot header freezes CurrencyCode and MinorUnitDigits; `cp4d-source-config-v2` includes both in its hash contract | `test_cp4d_submit_snapshot_capture.py::test_capture_persists_hash_reconciling_driver_total_and_line`, `test_source_config_v2_hash_includes_frozen_company_currency` |
| UsedRateDefinition and BonusEvent evidence freeze currency | `test_cp4d_submit_snapshot_capture.py::test_capture_persists_hash_reconciling_driver_total_and_line`; `test_cp5c_frozen_report_evidence.py::test_submit_captures_versioned_status_and_bonus_evidence` |
| Report-evidence v2 version/hash reflects frozen v2 shape | `test_cp5c_frozen_report_evidence.py::test_submit_captures_versioned_status_and_bonus_evidence`, `test_report_evidence_remains_frozen_after_live_source_drift` |
| FinalLines copy currency from approved snapshot; SourceSnapshot provenance includes that currency | `test_cp4f_finalization_snapshot_projection.py::test_finalize_projects_exact_approved_snapshot_and_audits_provenance` |
| FinalLines report authority rejects zero rows and conflicting currency pairs | `test_p3a_acceptance_matrix.py::test_report_final_lines_currency_rejects_empty_or_corrupt_authority` |
| Finalized period list resolves multiple immutable currencies set-wise with fixed query count | `test_p3a_acceptance_matrix.py::test_finalized_currency_page_resolves_multiple_periods_setwise`; HTTP list output for two real finalized periods: `test_p6a_finalized_library.py::test_finalized_period_discovery_returns_only_minimal_locked_archived_items` |
