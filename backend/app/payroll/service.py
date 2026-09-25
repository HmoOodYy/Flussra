"""
Legacy payroll compatibility facade.

Production code imports canonical payroll domain modules directly. These
re-exports remain only for legacy test compatibility; this module owns no
business logic, SQL, or FastAPI routes of its own.
"""

# Compatibility for test_cp2a_schedule_versioning.py, test_cp5c_reports.py,
# test_lg1_cdpi_ledger.py, and test_p6a/b/c/d_finalized_*.py.
# Compatibility for test_cp4a_perunit_core.py and
# test_phase4_characterization_slice1.py.
from app.payroll.draft_line_calculation import _compute_calculated_amount  # noqa: F401

# Compatibility for test_cp2e_eligibility_snapshot.py.
from app.payroll.eligibility import (
    _assert_driver_eligible_for_workdate_via_snapshot,  # noqa: F401
    _create_period_driver_eligibility_rows,  # noqa: F401
    _driver_has_existing_daily_source_on_date,  # noqa: F401
    _driver_has_existing_period_pay_source,  # noqa: F401
    _freeze_period_driver_eligibility_snapshot,  # noqa: F401
    _get_driver_eligibility_row,  # noqa: F401
    _is_snapshot_row_eligible_for_workdate,  # noqa: F401
    _period_has_driver_eligibility_snapshot,  # noqa: F401
    _regenerate_period_driver_eligibility_rows,  # noqa: F401
)

# Compatibility for test_cp4f_finalization_snapshot_projection.py,
# test_cp5c_reports.py, test_p6a/b/c/d_finalized_*.py, and
# test_phase6_immutable_evidence.py.
from app.payroll.finalization import (
    finalize_period,  # noqa: F401
    get_finalization_preview,  # noqa: F401
)

# Compatibility for test_cp3b2a_bonus_batch_safety.py.
from app.payroll.line_audit import _write_line_audit  # noqa: F401

# Compatibility for test_cp0a_mutation_status_guard.py, which reassigns this
# module attribute directly (a resolution-sensitive raw patch, not a
# wrapper).
from app.payroll.mutation_lock import _lock_period_for_mutation  # noqa: F401

# Compatibility for test_cp4d/e/f_*.py, test_cp5c_reports.py,
# test_cp5c_frozen_report_evidence.py, test_p6a/b/c_finalized_*.py,
# test_phase6_immutable_evidence.py, and test_report_authority_resolver.py.
# _build_live_calculation_packet is also resolution-sensitive: patched via
# module-attribute setattr by test_cp4e_approval_snapshot_binding.py and
# test_cp6_review.py. _validate_period_can_finalize and
# get_calculation_preview are also used directly by
# test_cp2e_eligibility_snapshot.py and test_cp4d_submit_snapshot_capture.py
# respectively.
from app.payroll.period_calculation import (
    _build_live_calculation_packet,  # noqa: F401
    _CalculationPacketDriverTotal,  # noqa: F401
    _CalculationPacketLine,  # noqa: F401
    _capture_calculation_snapshot,  # noqa: F401
    _LiveCalculationPacket,  # noqa: F401
    _validate_period_can_finalize,  # noqa: F401
    get_calculation_preview,  # noqa: F401
)
from app.payroll.period_creation import (
    _create_period_pay_item_rows,  # noqa: F401
    ensure_current_schedule_version,  # noqa: F401
)

# Compatibility for test_cp4d_submit_snapshot_capture.py (also calls
# _is_retryable_transaction_failure / _set_submit_transaction_isolation
# directly as pure-logic helpers), test_cp5c_frozen_report_evidence.py, and
# test_phase6_immutable_evidence.py.
from app.payroll.period_lifecycle import (
    _is_retryable_transaction_failure,  # noqa: F401
    _set_submit_transaction_isolation,  # noqa: F401
    change_period_status,  # noqa: F401
    resubmit_period,  # noqa: F401
)

# Compatibility for test_cp4d_submit_snapshot_capture.py and
# test_cp5c_frozen_report_evidence.py.
from app.payroll.period_read import get_period_by_id  # noqa: F401
