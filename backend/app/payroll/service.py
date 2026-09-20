"""
Payroll domain service — periods (list, create, status change) and draft lines.

All SQL is raw parameterised via sqlalchemy.text().
Branch-access enforcement is performed at the top of every mutating function;
read functions filter by the user's allowed branches directly in the query.
"""
from functools import wraps
import json
from datetime import date, timedelta
from decimal import Decimal
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _check_branch_access, _build_in_clause, _check_permission, _check_any_permission,
    _require_not_driver_role,
)
# Stage B4-13C: PerUnitInput/calculate_per_unit are no longer imported here —
# their only caller (_compute_calculated_amount) moved to
# app.payroll.draft_line_calculation, which imports them directly.
# PER_UNIT_CALCULATION_VERSION stays: _build_live_calculation_packet still
# reads it.
from app.payroll.calculation.per_unit import PER_UNIT_CALCULATION_VERSION
from app.payroll.immutable_evidence import (
    capture_snapshot_used_rate_definitions,
    capture_workflow_action_evidence,
)
# Stage B4-13A: capture_period_audit_evidence is no longer imported here — its
# only two callers in this module (_upsert_entry_state, _void_entry_state_field)
# moved to app.payroll.day_entry_state, which imports it directly. No other
# function in this module calls it, so this binding is no longer load-bearing.
from app.payroll.audit_evidence import (
    initialize_period_audit_evidence_coverage,
    link_unmapped_audit_evidence_to_snapshot,
)
from app.payroll.snapshot_hash import (
    CURRENT_PAYROLL_CALCULATION_VERSION,
    CURRENT_REPORT_EVIDENCE_VERSION,
    canonical_json,
    calculate_report_evidence_hash,
    calculate_snapshot_hash,
    calculate_source_config_hash,
)
from app.payroll import status_evidence
# Stage B4-2A: shared payroll access/guard helpers moved to app.payroll.guards.
# Re-exported here (same name) so this module stays a compatibility facade —
# the test_cp4f_finalization_snapshot_projection.py monkeypatch and other
# internal call sites (Driver Pay Rules, lifecycle, finalization, calculation,
# Day Grid) keep working unchanged. _check_own_driver_only,
# _check_not_in_finalized_period, and _check_driver_read_access are NOT
# re-exported (as of B4-4B): their only remaining callers were Rates
# functions, now in app.payroll.rates, which imports them directly from
# app.payroll.guards.
from app.payroll.guards import (
    _get_oda_own_driver_id,
)
# Stage B4-2B: Driver Pay Rules implementation moved to
# app.payroll.driver_pay_rules. The public functions are re-exported so
# router.py can still call them as service.<name>. Most private helpers were
# intentionally NOT re-exported: their only test coupling (test_m15.py's
# audit-rollback monkeypatches) was repointed to patch
# app.payroll.driver_pay_rules directly, where the moved functions actually
# resolve that name now.
#
# Stage B4-2C correction: _write_pay_rule_audit was re-exported here from
# B4-2C through B4-4A because copy_driver_rates (a Rates function that stayed
# in this file until B4-4B) called it directly. Stage B4-4B moved
# copy_driver_rates into app.payroll.rates, which now imports
# _write_pay_rule_audit directly from app.payroll.driver_pay_rules — this
# module no longer has any caller of it, so it is NOT re-exported here.
from app.payroll.driver_pay_rules import (
    create_driver_pay_rule as create_driver_pay_rule,
    get_driver_pay_rules as get_driver_pay_rules,
    get_driver_pay_rule_by_id as get_driver_pay_rule_by_id,
    end_driver_pay_rule as end_driver_pay_rule,
    update_driver_pay_rule_notes as update_driver_pay_rule_notes,
    void_driver_pay_rule as void_driver_pay_rule,
)
# Stage B4-3A/B4-4A: CP-1C (Period Creation) moved to app.payroll.period_creation
# in full. Re-exported here because these symbols still have real callers that
# remain in this module: legacy create_period (_auto_period_name, _month_end,
# _unique_period_code, ensure_current_schedule_version,
# _create_period_day_rows, _create_period_pay_item_rows). _cp1c_error and
# _check_slot_matrix are NOT re-exported (as of B4-4A): their last remaining
# callers in this module — get_current_workflow/_build_branch_entry (moved to
# current_hub.py in B4-3B) and get_period_candidates/create_period_from_candidate
# (moved to period_creation.py in B4-4A) — are both gone. get_period_candidates
# and create_period_from_candidate themselves are NOT re-exported either:
# router.py now calls period_creation directly (their only consumer).
# _validate_period_work_date, _period_has_pay_item_snapshot, and
# _get_period_pay_item_snapshot were returned to this module in a B4-4A
# ownership correction (see the "Work-date / pay-item snapshot accessors"
# section below) rather than staying under period_creation.py's ownership.
# _acquire_branch_workflow_lock was also returned and then extracted into its
# own small neutral module, app.payroll.workflow_lock (see below) — it is not
# period-creation-specific logic. _ACTIVE_SLOT_STATUSES, _decode_candidate_key,
# _make_candidate_key, _setup_fingerprint, _slot_fingerprint,
# _candidate_dates_at_offset, _generate_period_day_rows, _period_end, and
# _write_period_created_audit have no remaining caller in this module either.
from app.payroll.period_creation import (
    _auto_period_name,
    _month_end,
    _unique_period_code,
    ensure_current_schedule_version,
    _create_period_day_rows,
    _create_period_pay_item_rows,
)
# Stage B4-4A ownership correction: _acquire_branch_workflow_lock is a
# generic branch-level advisory-lock primitive genuinely shared by Period
# Creation, Lifecycle (change_period_status, resubmit_period), Finalization
# (finalize_period), legacy create_period, and app.review.service — none of
# which owns it more than the others. Extracted into its own small neutral
# module rather than left under period_creation.py's ownership.
from app.payroll.workflow_lock import _acquire_branch_workflow_lock
# Stage B4-4B: Rates moved to app.payroll.rates in full. No Rates public
# function or private helper has a remaining caller in this module. Stage
# B4-13C moved _compute_calculated_amount (the last remaining caller of
# _TIERED_BEHAVIORS and the four advanced-rate-method computation helpers)
# to app.payroll.draft_line_calculation, which now imports all four rate
# helpers directly from app.payroll.rates; this module no longer needs any
# binding from that module.
# Stage B4-5A: _lock_period_for_mutation and _write_line_audit are genuinely
# shared by domains that all still live in this module — Draft-line CRUD,
# Bonus, and Day Grid — none of which is more entitled to
# own either helper than the others. Stage B4-12 moved Period Pay Lines out
# of this module; it now imports both directly from their true-owner modules.
# They represent different
# responsibilities (a status/concurrency lock vs. an audit-log writer) and
# were extracted into two separately-named modules rather than bundled
# together or dumped into guards.py/workflow_lock.py. Stage B4-14 moved
# Draft-line CRUD (add_draft_line, update_draft_line, void_draft_line) out of
# this module; it now imports both directly from their true-owner modules.
# Stage B4-16 moved Day Grid (get_day_grid, save_day_grid) out of this
# module — save_day_grid was the last remaining caller of either helper here.
# Both bindings are nevertheless retained as test-only compatibility, not as
# real internal consumers: test_cp0a_mutation_status_guard.py's
# TestTrueRace._patch_lock reads and re-binds
# service._lock_period_for_mutation (it would raise AttributeError without
# this import), and test_cp3b2a_bonus_batch_safety.py imports
# _write_line_audit directly from app.payroll.service.
from app.payroll.mutation_lock import _lock_period_for_mutation  # noqa: F401
from app.payroll.line_audit import _write_line_audit  # noqa: F401
# Stage B4-11A: the live shared line-type vocabulary (_LineTypeInfo,
# _LEGACY_TO_CANONICAL, _INFORMATIONAL_ONLY) moved to
# app.payroll.line_type_vocabulary. _SYSTEM_ITEM_DB_CODES is NOT re-exported:
# its only purpose is constructing _LEGACY_TO_CANONICAL, which now happens
# inside the new module — it has no remaining caller here. The two dead
# legacy constants that construct _LineTypeInfo instances,
# _SYSTEM_LINE_TYPE_INFO and _SYSTEM_LINE_TYPES, deliberately stay in this
# module unchanged (B4-10 Decision Review): _SYSTEM_LINE_TYPES has zero real
# callers anywhere and _SYSTEM_LINE_TYPE_INFO is referenced only by that dead
# set, so neither is genuinely shared vocabulary — they are legacy residue
# left for a future dedicated dead-code cleanup stage, not this one. They
# continue to resolve _LineTypeInfo via this import at module-import time.
# Stage B4-16 moved Day Grid out of this module — get_day_grid was the only
# remaining caller of _INFORMATIONAL_ONLY here, so it is no longer
# re-exported; Day Grid imports its own copy directly.
from app.payroll.line_type_vocabulary import (
    _LEGACY_TO_CANONICAL,
    _LineTypeInfo,
)
# Stage B4-11B: the pay-item source-write lock (_lock_pay_item_for_source_write)
# moved to app.payroll.pay_item_write_lock in full. Stage B4-12 moved Period
# Pay Lines (add_period_pay_line, update_period_pay_line) out of this module;
# it now imports this helper directly from app.payroll.pay_item_write_lock.
# Stage B4-14 moved Draft-line CRUD (add_draft_line, update_draft_line) out
# of this module too; it now imports this helper directly from the same
# owner module. The five concurrency tests in test_cp0a_mutation_status_guard.py
# and test_cp0d_concurrency_regression.py that used to patch
# service._lock_pay_item_for_source_write / service._lock_period_for_mutation
# to intercept those two callers were retargeted to patch
# app.payroll.draft_line_mutation directly, where the moved functions now
# resolve those names. Stage B4-16 moved Day Grid (save_day_grid) out of
# this module — that was the last remaining caller of
# _lock_pay_item_for_source_write here, and no test patches it through this
# module's namespace (all of them target app.payroll.draft_line_mutation),
# so the binding is NOT re-exported anymore. Day Grid owns its own import
# and, with it, the unchanged pay-item-then-period lock ordering, which is
# enforced by each caller rather than by the function itself.
# Stage B4-11C: the shared source-line read model (_LINE_SELECT,
# _line_row_to_summary, _get_line_by_id) moved to
# app.payroll.source_line_read in full. Stage B4-12 moved Period Pay Lines
# out of this module; it now imports all three directly from
# app.payroll.source_line_read. Stage B4-14 moved Draft-line CRUD (which
# called _get_line_by_id) out of this module too, and it now imports its own
# copy directly from the same owner module — _get_line_by_id has no
# remaining caller here and is NOT re-exported. _LINE_SELECT and
# _line_row_to_summary keep a plain imported binding here: get_period_lines
# still lives in this module and still calls both by their bare names — this
# binding is load-bearing for that runtime caller, not incidental.
from app.payroll.source_line_read import (
    _LINE_SELECT,
    _line_row_to_summary,
)
# Stage B4-11D: the SOURCE-domain audit-evidence adapter
# (_capture_source_evidence) moved to app.payroll.source_evidence in full.
# Stage B4-12 moved Period Pay Lines out of this module; it now imports this
# helper directly from app.payroll.source_evidence. Stage B4-14 moved
# Draft-line CRUD (add_draft_line, update_draft_line, void_draft_line) out of
# this module too — those were the only remaining callers of
# _capture_source_evidence here (Day Grid never called it directly). It now
# imports its own copy directly from app.payroll.source_evidence, and this
# binding is NOT re-exported: no caller in this module resolves the bare
# name anymore. Not folded into app.payroll.audit_evidence: that module is
# deliberately generic infrastructure, and source_evidence.py is the SOURCE
# domain's own adapter over it (dependency direction: source_evidence.py ->
# audit_evidence.py, never the reverse, and never -> app.payroll.service).
# Stage B4-11E: the period pay-item snapshot read accessors
# (_period_has_pay_item_snapshot, _get_period_pay_item_snapshot) moved to
# app.payroll.period_pay_item_snapshot in full. Stage B4-12 moved Period Pay
# Lines out of this module; it now imports both directly from
# app.payroll.period_pay_item_snapshot. Stage B4-16 moved Day Grid
# (get_day_grid, save_day_grid) out of this module — those were the last
# remaining callers of either accessor here, so neither is re-exported
# anymore; Day Grid imports its own copies directly. Not placed under
# app.payroll.period_creation: see the historical B4-4A note at the
# "Work-date accessor" banner below.
# Stage B4-13A: the canonical Day Entry State domain (_validate_status_key,
# _resolve_status_key_id, _upsert_entry_state, _void_entry_state_field,
# _enforce_status_key_limits) moved to app.payroll.day_entry_state in full.
# _KEEP is NOT re-exported: it has zero real executable consumers anywhere
# in the repo (confirmed by a fresh whole-repo search at this stage) and was
# left behind in this module as legacy/dead residue rather than promoted
# into the new module without a live consumer. _canonical_aliases and
# _parse_quantity were evaluated and deliberately NOT moved: both are
# save_day_grid-only input-normalization helpers with zero coupling to the
# entry-state table, no StatusKey involvement, and no audit evidence of
# their own — they stay Day-Grid-owned, defined locally below, pending a
# future Day Grid ownership stage. Stage B4-14 moved Draft-line CRUD
# (add_draft_line, update_draft_line, void_draft_line) out of this module —
# those were the only remaining callers of _resolve_status_key_id and
# _void_entry_state_field here, so neither is re-exported anymore; Draft CRUD
# now imports its own copies directly from app.payroll.day_entry_state.
# Stage B4-16 moved Day Grid (save_day_grid) out of this module — that was
# the last remaining caller of _upsert_entry_state, _validate_status_key and
# _enforce_status_key_limits here, so none of the three is re-exported
# anymore; Day Grid imports its own copies directly. _canonical_aliases and
# _parse_quantity moved with it into app.payroll.day_grid, where the B4-13A
# ownership finding above (save_day_grid-only input normalization, no
# entry-state coupling) still holds.
# Stage B4-13B: the shared period-day calendar validator
# (_validate_period_work_date) moved to app.payroll.period_day_calendar in
# full. Not placed under app.payroll.period_creation: see the historical
# B4-4A note carried into the new module's own docstring. app.payroll.off_drivers
# no longer resolves this helper through this module's facade — it now
# imports it directly from app.payroll.period_day_calendar. Stage B4-14
# moved Draft-line CRUD (add_draft_line) out of this module — it now imports
# its own copy directly from app.payroll.period_day_calendar. Stage B4-16
# moved Day Grid (get_day_grid, save_day_grid) out of this module — those
# were the last remaining callers here, so this binding is NOT re-exported
# anymore; Day Grid imports its own copy directly.
# Stage B4-13C: the Draft-line calculation kernel (_CalcResult,
# _compute_calculated_amount) moved to app.payroll.draft_line_calculation in
# full. _CalcResult is NOT re-exported: every caller only accesses the
# returned instance's attributes (e.g. .calculated_amount), never the class
# name itself — confirmed by a fresh whole-module search at that stage — so
# no binding is load-bearing for it. Stage B4-14 moved Draft-line CRUD
# (add_draft_line, update_draft_line) out of this module — it now imports
# its own copy of _compute_calculated_amount directly from
# app.payroll.draft_line_calculation. This plain imported binding still
# stays here: _refresh_draft_calculations and _compute_draft_line_preview_amounts
# still live in this module and still call it by its bare name — this
# binding is load-bearing for those two runtime callers, not incidental.
from app.payroll.draft_line_calculation import _compute_calculated_amount
# Stage B4-14: the Draft-line mutation domain (add_draft_line,
# update_draft_line, void_draft_line, plus their private
# _validate_line_type / _SYSTEM_FINALIZATION_ONLY / _CALC_REQUIRED_BEHAVIORS
# policy) moved to app.payroll.draft_line_mutation in full. Only the three
# public mutation functions keep a plain imported binding here: save_day_grid
# still lives in this module and still calls all three by their bare names
# (void_draft_line for zeroed lines, add_draft_line for new lines,
# update_draft_line for quantity changes) — this binding is load-bearing for
# that runtime caller, not incidental, and stays until Day Grid itself
# moves. The read surfaces (get_period_lines, get_period_draft_summary) are
# NOT part of this domain and were not moved — "CRUD" here is mutation-only.
# _validate_line_type, _SYSTEM_FINALIZATION_ONLY, and _CALC_REQUIRED_BEHAVIORS
# are NOT re-exported: a fresh whole-module search at this stage confirmed
# zero remaining caller of any of the three in this module.
# Stage B4-16 moved Day Grid (save_day_grid) out of this module — that was
# the last remaining caller of all three, and router.py already calls
# app.payroll.draft_line_mutation directly, so none of them is re-exported
# anymore. Day Grid imports its own copies directly; the DailyStatus /
# DailyNote rows it writes with its own raw SQL were never routed through
# this domain and still are not.
# Stage B4-15: the Status Payment Sync domain (_STATUS_PAYMENT_PROJECTION_SQL,
# _calculate_status_payment_amount, _sync_status_payment_for_entry_state,
# _LiveStatusLine, _resolve_live_status_payment_lines,
# _refresh_status_payment_lines) moved to app.payroll.status_payment_sync in
# full. Four keep a plain imported binding here: Day Grid (save_day_grid)
# still calls _sync_status_payment_for_entry_state, Lifecycle
# (change_period_status, resubmit_period) still calls
# _refresh_status_payment_lines, and Calculation
# (_build_live_calculation_packet) still calls
# _resolve_live_status_payment_lines and references
# _STATUS_PAYMENT_PROJECTION_SQL by its bare name — all still live in this
# module in this unit, so these four bindings are load-bearing, not
# incidental. _calculate_status_payment_amount and _LiveStatusLine are NOT
# re-exported: a fresh whole-module search at this stage confirmed both are
# referenced only internally within the moved cluster itself (the latter's
# class name is never referenced by its one remaining caller, which only
# accesses the returned instances' attributes) — no binding is load-bearing
# for either.
# Stage B4-16 moved Day Grid (save_day_grid) out of this module — that was
# the only caller of _sync_status_payment_for_entry_state here, so it is no
# longer re-exported; Day Grid imports its own copy directly. The remaining
# three bindings are unchanged and still load-bearing for Lifecycle and
# Calculation, which both still live in this module.
from app.payroll.status_payment_sync import (
    _STATUS_PAYMENT_PROJECTION_SQL,
    _refresh_status_payment_lines,
    _resolve_live_status_payment_lines,
)
# Stage B4-7: the Period read model (_BASE_SELECT, _row_to_summary,
# get_periods, get_period_by_id) moved to app.payroll.period_read in full.
# Only get_period_by_id is re-exported here: it still has internal call
# sites across Period Create, Lifecycle, Resubmission, Finalization,
# Calculation, Bonus, Drivers Off, and Day Grid, none of which move in this
# unit. Stage B4-12 moved Period Pay Lines out of this module; it now
# imports get_period_by_id directly from app.payroll.period_read. Stage
# B4-14 moved Draft-line CRUD out of this module too; it now imports its own
# copy directly from app.payroll.period_read. app.payroll.off_drivers also
# resolves it via this facade (qualified service.get_period_by_id access) —
# left unchanged, not redirected, since the facade is load-bearing anyway.
# get_periods has no remaining caller here (router.py now calls
# period_read.get_periods directly); _BASE_SELECT and _row_to_summary are
# private implementation details of period_read.py with no caller anywhere
# else. Do not remove this binding until the remaining internal callers
# themselves move to their true domains in later stages.
from app.payroll.period_read import get_period_by_id
# Stage B4-1: driver eligibility helpers moved to app.payroll.eligibility.
# Re-exported here (same names) so this module stays a compatibility facade —
# every existing internal call site and external consumer (current_hub.py,
# off_drivers.py, finalized_library_read_model.py) keeps working unchanged.
from app.payroll.eligibility import (
    # Temporary compatibility re-export: test_cp2e_eligibility_snapshot.py
    # imports this directly from app.payroll.service.
    _get_driver_eligibility_row,  # noqa: F401
    _period_has_driver_eligibility_snapshot,
    # Stage B4-16: Day Grid (get_day_grid) was this symbol's last internal
    # caller here; it now imports its own copy directly from
    # app.payroll.eligibility. Re-exported here only because
    # test_cp2e_eligibility_snapshot.py imports it directly from
    # app.payroll.service — test-only compatibility, not a real internal
    # consumer of this module.
    _is_snapshot_row_eligible_for_workdate,  # noqa: F401
    # Stage B4-15: Status Payment Sync (_refresh_status_payment_lines) was
    # this symbol's last internal caller here; it now imports its own copy
    # directly from app.payroll.eligibility. Re-exported here only because
    # test_cp2e_eligibility_snapshot.py imports it directly from
    # app.payroll.service — test-only compatibility, not a real internal
    # consumer of this module.
    _driver_has_existing_daily_source_on_date,  # noqa: F401
    # Stage B4-8: Bonus (app.payroll.bonus) was this symbol's last internal
    # caller (_bonus_summary_driver_create_eligible); it now imports its own
    # copy directly from app.payroll.eligibility. Re-exported here only
    # because test_cp2e_eligibility_snapshot.py imports it directly from
    # app.payroll.service — test-only compatibility, not a real internal
    # consumer of this module.
    _driver_has_existing_period_pay_source,  # noqa: F401
    # Stage B4-16: Day Grid (save_day_grid) was this symbol's last internal
    # caller here; it now imports its own copy directly from
    # app.payroll.eligibility. Re-exported here only because
    # test_cp2e_eligibility_snapshot.py imports it directly from
    # app.payroll.service — test-only compatibility, not a real internal
    # consumer of this module.
    _assert_driver_eligible_for_workdate_via_snapshot,  # noqa: F401
    _create_period_driver_eligibility_rows,
    _regenerate_period_driver_eligibility_rows,
    # Temporary compatibility re-export: test_cp2e_eligibility_snapshot.py
    # imports this directly from app.payroll.service.
    _freeze_period_driver_eligibility_snapshot,  # noqa: F401
)
# Stage B4-11E: _WRITE_BLOCKED_STATUSES relocated to app.payroll.schemas from
# this module in full. Stage B4-12 moved Period Pay Lines (update_period_pay_line,
# void_period_pay_line) out of this module; it now imports this constant
# directly from app.payroll.schemas. Stage B4-14 moved Draft-line CRUD
# (update_draft_line, void_draft_line) out of this module too — those were
# the last two remaining callers here, so it now imports its own copy
# directly from app.payroll.schemas and this binding is NOT re-exported: no
# caller in this module resolves the bare name anymore.
# Stage B4-16 moved Day Grid (get_day_grid, save_day_grid) out of this
# module. It was the only remaining consumer here of the eight DayGrid*
# response/request models, of DraftLineCreate/DraftLineUpdate (which it
# passed to the Draft-line mutation functions) and of SOURCE_ENTRY_STATUSES,
# so none of those eleven names is re-exported anymore — router.py already
# imports the DayGrid* models straight from app.payroll.schemas, and
# app.payroll.day_grid imports its own copies of all eleven directly.
from app.payroll.schemas import (
    PeriodSummary, PeriodCreate, PeriodStatusChange, NextPeriodDates, PeriodEntryCount,
    _VALID_TRANSITIONS,
    DraftLineSummary,
    DriverPeriodSummary, ENTRY_ALLOWED_STATUSES,
)
# Stage B4-8: the Bonus domain (_BONUS_ENTRY_ALLOWED_STATUSES,
# _get_bonus_event_by_id, list_bonus_events, _increment_bonus_data_revision,
# create_bonus_event, update_bonus_event, void_bonus_event,
# _bonus_batch_canonical_payload, _bonus_batch_request_hash,
# _get_bonus_events_in_order, apply_bonus_batch,
# _bonus_summary_driver_create_eligible, get_bonus_summary) moved to
# app.payroll.bonus in full. No facade is kept here: router.py now calls
# app.payroll.bonus directly, and no internal service.py caller or test
# imports any Bonus symbol from this module. _load_active_bonus_events and
# get_period_eligible_drivers stay in this module — they are not Bonus CRUD
# ownership (see their own definitions below/above for why).
#
# Stage B4-9.5: the Ledger read domain (_FINAL_SELECT, get_final_lines) moved
# to app.payroll.ledger_read in full. No facade is kept here: router.py now
# calls app.payroll.ledger_read directly, and no internal service.py caller
# or test imports either symbol from this module. get_period_eligible_drivers
# and get_drivers_off / _finalized_drivers_off_entries stay in this module —
# B4-9 deferred both pending targeted architectural discovery, not a Ledger
# ownership question.


# ---------------------------------------------------------------------------
# Period status-change: permission gates and audit
# ---------------------------------------------------------------------------

# Each (from_status, to_status) pair maps to the permission code the caller
# must hold on the period's branch.
#
# Forward lightweight transitions (open/submit) require payroll.entry.
# Approval, reversal, cancellation, and archiving require payroll.finalize —
# these are irreversible or senior-level decisions.
_TRANSITION_PERMISSIONS: dict[tuple[str, str], str] = {
    # CP-1D: ("Draft", "Open") removed — Draft promotion is now done atomically
    # inside the Open→InReview submit path, not via a standalone PATCH transition.
    ("Draft",    "Cancelled"):  "payroll.finalize",   # CP-0C: was missing — any user could cancel Draft
    ("Open",     "InReview"):   "payroll.entry",
    ("Open",     "Cancelled"):  "payroll.finalize",
    # CP-1A: InReview→Open and InReview→Cancelled both removed.
    # InReview has no PATCH exits — the review decision flow is the only exit path.
    # CP-1A: Approved→Cancelled removed — Approved has no PATCH exits.
    #   Approved exits only via POST /finalize (→ Locked).
    ("Locked",   "Archived"):   "payroll.finalize",
}


def _translate_submit_transaction_failures(func):
    """Map retryable PostgreSQL submit/resubmit transaction failures to 409."""
    @wraps(func)
    async def wrapped(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except DBAPIError as exc:
            if _is_retryable_transaction_failure(exc):
                raise HTTPException(
                    status_code=409,
                    detail="Payroll changed concurrently. Refresh and retry the submission.",
                ) from exc
            raise
    return wrapped

_PERIOD_AUDIT_REASONS: dict[str, str] = {
    "PERIOD_STATUS_CHANGED": "Payroll period status changed",
}


async def _write_period_status_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    period_id: int,
    old_status: str,
    new_status: str,
    extra: dict | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for a period status-change event.

    Module-level so tests can monkeypatch it to verify that the preceding
    UPDATE rolls back when this raises.

    extra: optional additional fields merged into newvaluejson (e.g. trigger context).
    """
    new_val_dict: dict = {"status": new_status}
    if extra:
        new_val_dict.update(extra)
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:cid, :bid, :uid, 'PERIOD_STATUS_CHANGED',
                 'payroll', 'PayrollPeriods', :eid,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "cid":     company_id,
            "bid":     branch_id,
            "uid":     user_id,
            "eid":     str(period_id),
            "old_val": json.dumps({"status": old_status}),
            "new_val": json.dumps(new_val_dict),
            "reason":  _PERIOD_AUDIT_REASONS["PERIOD_STATUS_CHANGED"],
        },
    )


# ---------------------------------------------------------------------------
# Create period
# ---------------------------------------------------------------------------

async def create_period(
    company_id: int,
    user_id: int,
    data: PeriodCreate,
    db: AsyncConnection,
) -> PeriodSummary:
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    if not can_see_all and data.branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to the target branch.",
        )

    # Permission gate: creating a period requires payroll.period.create.
    # This is intentionally separate from payroll.entry (data entry/editing).
    # Migration 0030 seeds this permission and assigns it to appropriate roles.
    await _check_permission(company_id, user_id, data.branch_id, "payroll.period.create", db)

    # CP-1C: Acquire branch advisory lock before overlap check and insert.
    # Serializes all period creation (both legacy and candidate-based) for this branch.
    await _acquire_branch_workflow_lock(company_id, data.branch_id, db)

    # CP-1D: Draft creation guard — legacy POST /payroll/periods creates a Draft;
    # this is only valid when exactly one Open exists and no Draft already exists.
    _slot_result = await db.execute(
        text("""
            SELECT status FROM payroll.payrollperiods
            WHERE  companyid = :cid AND branchid = :bid
              AND  status IN ('Draft', 'Open', 'InReview', 'Returned')
        """),
        {"cid": company_id, "bid": data.branch_id},
    )
    _slot_statuses = [r["status"] for r in _slot_result.mappings().all()]
    if "Draft" in _slot_statuses:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "DRAFT_SLOT_OCCUPIED",
                "message": (
                    "A Draft period already exists for this branch. "
                    "Only one Draft period is permitted per branch at a time."
                ),
            },
        )
    _open_count = _slot_statuses.count("Open")
    if _open_count == 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "DRAFT_CREATION_REQUIRES_OPEN",
                "message": (
                    "No Open period exists for this branch. "
                    "Legacy Draft creation requires exactly one Open period. "
                    "Use the candidate-based period creation endpoint instead."
                ),
            },
        )
    if _open_count > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "WORKFLOW_SLOT_CONFLICT",
                "message": (
                    "More than one Open period exists for this branch — "
                    "the workflow is in an inconsistent state."
                ),
            },
        )
    # Exactly one Open, no Draft → allow (InReview/Returned co-existence is valid)

    # Verify branch belongs to this company and get its BranchCode for period_code
    br_result = await db.execute(
        text(
            "SELECT branchcode FROM core.branches "
            "WHERE branchid = :bid AND companyid = :cid"
        ),
        {"bid": data.branch_id, "cid": company_id},
    )
    br_row = br_result.mappings().first()
    if br_row is None:
        raise HTTPException(
            status_code=422,
            detail="branch_id does not exist in this company.",
        )
    branch_code: str = br_row["branchcode"]

    # Guard: reject dates that overlap any existing non-cancelled period for this branch.
    # Every status except Cancelled reserves the date range — Locked and Archived
    # represent official historical records that must not be overlapped.
    overlap_row = await db.execute(
        text("""
            SELECT payrollperiodid, status, startdate, enddate
            FROM   payroll.payrollperiods
            WHERE  branchid  = :bid
              AND  companyid = :cid
              AND  status    != 'Cancelled'
              AND  startdate <= :end_date
              AND  enddate   >= :start_date
            LIMIT  1
        """),
        {
            "bid":        data.branch_id,
            "cid":        company_id,
            "start_date": data.start_date,
            "end_date":   data.end_date,
        },
    )
    existing = overlap_row.mappings().first()
    if existing is not None:
        ex_id     = existing["payrollperiodid"]
        ex_status = existing["status"]
        ex_start  = existing["startdate"]
        ex_end    = existing["enddate"]
        raise HTTPException(
            status_code=422,
            detail=(
                f"A payroll period already exists for this date range. "
                f"Existing period (ID {ex_id}) status: {ex_status}, "
                f"dates: {ex_start} – {ex_end}. "
                f"Open the existing period instead of creating a new one. "
                f"Locked and Archived periods are official historical records and cannot be overlapped."
            ),
        )

    # Auto-generate period_name if not provided
    period_name = data.period_name or _auto_period_name(
        data.period_type, data.start_date, data.end_date
    )

    # Auto-generate a unique period_code
    base_code = f"{branch_code}-{data.start_date.strftime('%Y%m%d')}"
    period_code = await _unique_period_code(base_code, company_id, data.branch_id, db)

    # CP-2A: ensure schedule version and set on new period. Still under advisory lock.
    # None means no active setup — reject before inserting a period with NULL version.
    legacy_sv_id = await ensure_current_schedule_version(company_id, data.branch_id, user_id, db)
    if legacy_sv_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "PAYROLL_SETUP_REQUIRED",
                "message": (
                    "No active payroll setup or schedule version exists for this branch. "
                    "Configure payroll setup before creating periods."
                ),
            },
        )

    # Insert
    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, periodcode, periodname, periodtype,
                 startdate, enddate, paydate, status, notes, createdbyuserid,
                 scheduleversionid)
            VALUES
                (:company_id, :branch_id, :period_code, :period_name, :period_type,
                 :start_date, :end_date, :pay_date, 'Draft', :notes, :created_by,
                 :sv_id)
            RETURNING payrollperiodid
        """),
        {
            "company_id":  company_id,
            "branch_id":   data.branch_id,
            "period_code": period_code,
            "period_name": period_name,
            "period_type": data.period_type,
            "start_date":  data.start_date,
            "end_date":    data.end_date,
            "pay_date":    data.pay_date,
            "notes":       data.notes,
            "created_by":  user_id,
            "sv_id":       legacy_sv_id,
        },
    )
    period_id: int = insert_result.scalar_one()
    await initialize_period_audit_evidence_coverage(
        company_id=company_id, branch_id=data.branch_id, period_id=period_id, db=db,
    )

    # CP-2B: create period-day snapshot from the schedule version's mask.
    # Read from PayrollScheduleVersions (immutable) — not from mutable BranchPayrollSettings.
    sv_mask_row = (await db.execute(
        text(
            "SELECT normaldaysoffmask FROM payroll.PayrollScheduleVersions "
            "WHERE scheduleversionid = :sv_id"
        ),
        {"sv_id": legacy_sv_id},
    )).mappings().first()
    period_mask = sv_mask_row["normaldaysoffmask"] if sv_mask_row else None
    await _create_period_day_rows(
        period_id, company_id, data.branch_id,
        legacy_sv_id, data.start_date, data.end_date, period_mask, db,
    )

    # CP-2C: create period pay-item layout snapshot.
    await _create_period_pay_item_rows(
        period_id, company_id, data.branch_id, data.start_date, db,
    )

    # CP-2E: create driver eligibility snapshot for legacy Draft periods.
    # Draft stays provisional (freeze=False); freeze happens when promoted to Open.
    await _create_period_driver_eligibility_rows(
        period_id, company_id, data.branch_id, db,
        snapshot_source="Generated",
        freeze=False,
        created_by_user_id=user_id,
    )

    return await get_period_by_id(company_id, user_id, period_id, db)


# ---------------------------------------------------------------------------
# Period-date calculation from payroll setup
# ---------------------------------------------------------------------------

def compute_period_dates(
    frequency: str,
    anchor_start_date: date,
    last_end_date: date | None = None,
    custom_interval_days: int | None = None,
) -> tuple[date, date]:
    """
    Compute the next period's (start, end) dates from a branch's payroll setup.

    Rules
    -----
    - If *last_end_date* is None the first period starts on *anchor_start_date*.
    - Otherwise the next period starts the day after *last_end_date*.
    - Period length depends on *frequency*:

      ======= =============================================
      Week    7 days  (start + 6 days)
      Biweek  14 days (start + 13 days)
      Month   One calendar month (start to same day next month minus 1 day)
      Custom  Requires custom_interval_days > 0 (inclusive period length)
      ======= =============================================

    Both start and end are inclusive.

    Raises
    ------
    ValueError if *frequency* is unrecognised, or if 'Custom' and
    *custom_interval_days* is None or ≤ 0.
    """
    start = anchor_start_date if last_end_date is None else last_end_date + timedelta(days=1)

    if frequency == "Week":
        end = start + timedelta(days=6)
    elif frequency == "Biweek":
        end = start + timedelta(days=13)
    elif frequency == "Month":
        end = _month_end(start)
    elif frequency == "Custom":
        if not custom_interval_days or custom_interval_days <= 0:
            raise ValueError(
                "Custom frequency requires custom_interval_days > 0. "
                "Configure the custom cadence in Payroll Setup first."
            )
        end = start + timedelta(days=custom_interval_days - 1)
    else:
        raise ValueError(f"Unknown payroll frequency: {frequency!r}")

    return start, end


async def get_next_period_dates(
    company_id: int,
    user_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> NextPeriodDates:
    """
    Return suggested start/end dates for the next payroll period of *branch_id*,
    derived from its BranchPayrollSettings and the latest existing period.

    - If no prior non-cancelled periods exist the first period starts on the
      setup's anchor_start_date.
    - Returns ``is_custom=True`` and ``start_date=None`` for Custom frequency.

    Raises 403 if the caller lacks access; 404 if no payroll setup is configured.
    """
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to the requested branch.",
        )
    await _check_any_permission(
        company_id, user_id, branch_id,
        ["payroll.view", "payroll.entry", "payroll.finalize"],
        db,
    )

    # Fetch branch payroll setup
    setup_row = await db.execute(
        text("""
            SELECT payrollfrequency, anchorstartdate, customintervaldays
            FROM   payroll.branchpayrollsettings
            WHERE  branchid  = :bid
              AND  companyid = :cid
              AND  isactive  = TRUE
        """),
        {"bid": branch_id, "cid": company_id},
    )
    setup = setup_row.mappings().first()
    if setup is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No active payroll setup found for branch {branch_id}. "
                "Configure it in Settings → Payroll Setup first."
            ),
        )

    frequency: str         = setup["payrollfrequency"]
    anchor: date           = setup["anchorstartdate"]
    interval_days: int | None = setup.get("customintervaldays")

    # MAX end_date of non-cancelled periods for this branch
    last_row = await db.execute(
        text("""
            SELECT MAX(enddate) AS last_end
            FROM   payroll.payrollperiods
            WHERE  branchid  = :bid
              AND  companyid = :cid
              AND  status    != 'Cancelled'
        """),
        {"bid": branch_id, "cid": company_id},
    )
    last_end: date | None = last_row.scalar_one_or_none()

    is_custom = (frequency == "Custom")
    start_date_out: date | None = None
    end_date_out:   date | None = None

    if frequency == "Custom":
        if interval_days and interval_days > 0:
            # Custom with a valid saved interval — compute automatically
            start_date_out, end_date_out = compute_period_dates(
                frequency, anchor, last_end, custom_interval_days=interval_days
            )
        # else: interval missing → leave start/end as None (setup incomplete)
    else:
        start_date_out, end_date_out = compute_period_dates(frequency, anchor, last_end)

    return NextPeriodDates(
        branch_id=branch_id,
        period_type=frequency,
        anchor_start_date=anchor,
        last_period_end_date=last_end,
        start_date=start_date_out,
        end_date=end_date_out,
        is_custom=is_custom,
        custom_interval_days=interval_days,
    )


async def get_period_entry_count(
    company_id: int,
    user_id: int,
    period_id: int,
    db: AsyncConnection,
) -> PeriodEntryCount:
    """
    Return a count of non-voided draft entries in *period_id* for the caller.

    Counts all rows in payroll.PayrollDraftLines (both 'Day' and 'Period'
    linescope) that are not Void.  Used by the UI to show a data-loss warning
    before cancelling a period.

    Raises 403 / 404 via ``get_period_by_id`` if the caller lacks access.
    """
    # Access check reuses the existing get_period_by_id guard
    await get_period_by_id(company_id, user_id, period_id, db)

    row = await db.execute(
        text("""
            SELECT
                COUNT(DISTINCT driverid) AS driver_count,
                COUNT(*)                 AS entry_count
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  status          != 'Void'
        """),
        {"pid": period_id, "cid": company_id},
    )
    r = row.mappings().first()
    driver_count = int(r["driver_count"] or 0)
    entry_count  = int(r["entry_count"]  or 0)

    return PeriodEntryCount(
        period_id=period_id,
        driver_count=driver_count,
        entry_count=entry_count,
        has_data=(entry_count > 0),
    )


# ---------------------------------------------------------------------------
# Status change
# ---------------------------------------------------------------------------

@_translate_submit_transaction_failures
async def change_period_status(
    company_id: int,
    user_id: int,
    period_id: int,
    change: PeriodStatusChange,
    db: AsyncConnection,
) -> PeriodSummary:
    # CP-4D: must be the first SQL on this request connection for submission.
    if change.status == "InReview":
        await _set_submit_transaction_isolation(db)

    # Load (and access-check) the existing period
    existing = await get_period_by_id(company_id, user_id, period_id, db)

    allowed = _VALID_TRANSITIONS.get(existing.status, set())
    if change.status not in allowed:
        if not allowed:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Period with status '{existing.status}' cannot be "
                    f"transitioned to any other status."
                ),
            )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot transition from '{existing.status}' to '{change.status}'. "
                f"Allowed transitions: {sorted(allowed)}."
            ),
        )

    # Action-level permission gate: each transition requires a specific code.
    required_perm = _TRANSITION_PERMISSIONS.get((existing.status, change.status))
    if required_perm:
        await _check_permission(company_id, user_id, existing.branch_id, required_perm, db)

    # M16: Open → InReview pre-submission guards + auto-create PeriodApproval review item.
    #
    # The review item is created inside this same transaction so that if any guard
    # fails — or if the audit write raises — everything rolls back atomically.
    # The period never reaches InReview without a corresponding review item existing.
    if existing.status == "Open" and change.status == "InReview":
        # CP-1D: Acquire branch advisory lock before touching any workflow rows.
        # Same lock used by period creation (CP-1C) and resubmission — ensures all
        # per-branch workflow mutations are fully serialized.
        await _acquire_branch_workflow_lock(company_id, existing.branch_id, db)

        # CP-1D: Lock all active workflow rows in a deterministic order to prevent
        # deadlock when two concurrent submits race on the same branch.
        _wf_result = await db.execute(
            text("""
                SELECT payrollperiodid, status, startdate, enddate
                FROM   payroll.payrollperiods
                WHERE  companyid = :cid
                  AND  branchid  = :bid
                  AND  status    IN ('Draft', 'Open', 'InReview', 'Returned')
                ORDER  BY payrollperiodid ASC
                FOR UPDATE
            """),
            {"cid": company_id, "bid": existing.branch_id},
        )
        _wf_rows = list(_wf_result.mappings().all())

        _open_row = next(
            (r for r in _wf_rows if r["payrollperiodid"] == period_id and r["status"] == "Open"),
            None,
        )
        if _open_row is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Period is no longer Open — a concurrent transition may have already "
                    "moved it. Please refresh and try again."
                ),
            )

        # CP-1D: Returned backlog and anomaly check — fail closed on any Returned presence.
        # Older Returned (EndDate < Open.StartDate): unresolved backlog → block.
        # Overlapping, same-boundary, or newer Returned: chronologically anomalous → fail closed.
        for _ret in _wf_rows:
            if _ret["status"] == "Returned":
                if _ret["enddate"] < _open_row["startdate"]:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code":    "RETURNED_BACKLOG_BLOCKS_SUBMIT",
                            "message": (
                                "A Returned period (ending "
                                f"{_ret['enddate']}) exists before this period's start date "
                                f"({_open_row['startdate']}). Resolve the backlog Returned "
                                "period before submitting."
                            ),
                        },
                    )
                else:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code":    "WORKFLOW_SLOT_CONFLICT",
                            "message": (
                                "A Returned period (ending "
                                f"{_ret['enddate']}) chronologically overlaps or is newer than "
                                f"this Open period (starting {_open_row['startdate']}). "
                                "This represents an anomalous workflow state. Resolve the "
                                "Returned period before submitting."
                            ),
                        },
                    )

        # CP-1D: Draft promotion eligibility.
        # If a Draft period exists it must start immediately after this Open period
        # ends (Draft.StartDate == Open.EndDate + 1 day); otherwise block the submit.
        from datetime import timedelta as _timedelta
        _draft_rows = [r for r in _wf_rows if r["status"] == "Draft"]
        _eligible_draft = None
        if _draft_rows:
            _draft_row = _draft_rows[0]
            _expected_draft_start = _open_row["enddate"] + _timedelta(days=1)
            if _draft_row["startdate"] == _expected_draft_start:
                _eligible_draft = _draft_row
            else:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code":    "DRAFT_PROMOTION_CONFLICT",
                        "message": (
                            f"A Draft period exists (start {_draft_row['startdate']}) "
                            f"but does not immediately follow this period's end date "
                            f"({_open_row['enddate']}). Resolve the Draft period before "
                            "submitting."
                        ),
                    },
                )

        # CP-1B: Friendly InReview slot guard.  The partial unique index is the
        # concurrency authority; this check gives a clearer 409 message on the
        # common (non-race) path.
        await _check_inreview_slot_available(company_id, existing.branch_id, period_id, db)

        # CP-2D2: re-sync status payment lines from canonical PPDES state before
        # refresh so that rate changes (new/backdated driver rates for STATUS_PAY)
        # are reflected before submit guards run.
        await _refresh_status_payment_lines(
            period_id=period_id,
            company_id=company_id,
            branch_id=existing.branch_id,
            user_id=user_id,
            db=db,
        )

        # Auto-refresh: re-compute calculatedamount + needsmanagerreview for all
        # rate-dependent draft lines using the currently approved effective-dated
        # rates.  This ensures that backdated approved rates added since lines
        # were entered are reflected BEFORE the submit guards run, so the user
        # does not need to manually touch each line to trigger recalculation.
        await _refresh_draft_calculations(
            period_id=period_id,
            company_id=company_id,
            period_start_date=existing.start_date,
            db=db,
        )

        # Guard 1: empty period — refuse to submit a period with no payroll data.
        # CP-3A: count non-BONUS DraftLines + Active BonusEvents (BONUS DraftLines
        # are no longer used; bonus data lives in PayrollBonusEvents).
        empty_result = await db.execute(
            text("""
                SELECT (
                    SELECT COUNT(*) FROM payroll.payrolldraftlines
                    WHERE  payrollperiodid = :pid AND companyid = :cid
                      AND  status != 'Void' AND linetype != 'BONUS'
                ) + (
                    SELECT COUNT(*) FROM payroll.payrollbonusevents
                    WHERE  payrollperiodid = :pid AND companyid = :cid
                      AND  status = 'Active'
                ) AS total_lines
            """),
            {"pid": period_id, "cid": company_id},
        )
        if int(empty_result.scalar_one()) == 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cannot submit an empty period for review. "
                    "Add at least one non-voided draft line before submitting."
                ),
            )

        # Guard 2: unresolved NeedsManagerReview lines.
        unresolved_result = await db.execute(
            text("""
                SELECT COUNT(*) AS cnt
                FROM   payroll.payrolldraftlines
                WHERE  payrollperiodid    = :pid
                  AND  companyid          = :cid
                  AND  status            != 'Void'
                  AND  needsmanagerreview  = TRUE
            """),
            {"pid": period_id, "cid": company_id},
        )
        unresolved_count = int(unresolved_result.scalar_one())
        if unresolved_count > 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot submit: {unresolved_count} draft line(s) still require "
                    f"manager review (calculatedamount unresolved or manually flagged). "
                    f"Resolve all flagged lines before submitting for review."
                ),
            )

        # Guard 3: zero-calc unresolved lines (same three-clause check as finalization).
        # CP-0: EXISTS subqueries now match system items (companyid IS NULL) as well as
        # custom items (companyid = dl.companyid) so that new rows storing canonical
        # PayItemCodes ("HOURS") are caught alongside legacy rows ("Hours").
        zero_calc_result = await db.execute(
            text("""
                SELECT COUNT(*) AS cnt
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid    = :pid
                  AND  dl.companyid          = :cid
                  AND  dl.status            != 'Void'
                  AND  dl.needsmanagerreview  = FALSE
                  AND  dl.calculatedamount   IS NULL
                  AND  (
                    EXISTS (
                        SELECT 1 FROM payroll.payitems pi
                        WHERE  pi.payitemcode  = dl.linetype
                          AND  (pi.companyid IS NULL OR pi.companyid = dl.companyid)
                          AND  pi.ratebehavior IN (
                              'OrdinalTier', 'RangeBracket',
                              'RangeProgressive', 'Block'
                          )
                    )
                    OR
                    (
                        dl.rateamount IS NULL
                        AND EXISTS (
                            SELECT 1 FROM payroll.payitems pi
                            WHERE  pi.payitemcode  = dl.linetype
                              AND  (pi.companyid IS NULL OR pi.companyid = dl.companyid)
                              AND  pi.ratebehavior = 'PerUnit'
                        )
                    )
                    OR dl.linescope = 'Period'
                  )
            """),
            {"pid": period_id, "cid": company_id},
        )
        zero_calc_count = int(zero_calc_result.scalar_one())
        if zero_calc_count > 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot submit: {zero_calc_count} rate-dependent draft line(s) have no "
                    f"resolved calculation amount. Fix or void these lines before submitting."
                ),
            )

        # Guard 4: duplicate Pending review item.
        # Block only if a Pending item exists — EditRequested/Rejected items are historical.
        dup_result = await db.execute(
            text("""
                SELECT reviewitemid FROM review.managerreviewitems
                WHERE  companyid    = :cid
                  AND  entityschema = 'payroll'
                  AND  entityname   = 'PayrollPeriods'
                  AND  entityid     = :eid
                  AND  requesttype  = 'PeriodApproval'
                  AND  status       = 'Pending'
                LIMIT 1
            """),
            {"cid": company_id, "eid": str(period_id)},
        )
        dup_row = dup_result.first()
        if dup_row is not None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A Pending review item already exists for this period "
                    f"(review item ID {dup_row[0]}). Resolve it before re-submitting."
                ),
            )

        packet = await _build_live_calculation_packet(existing, company_id, db)
        snapshot_id = await _capture_calculation_snapshot(
            period=existing,
            company_id=company_id,
            user_id=user_id,
            packet=packet,
            db=db,
            context="Submit",
        )
        await capture_workflow_action_evidence(
            company_id=company_id,
            branch_id=existing.branch_id,
            period_id=period_id,
            snapshot_id=snapshot_id,
            action_code="SUBMITTED",
            user_id=user_id,
            required_permission_code="payroll.entry",
            db=db,
        )
        await link_unmapped_audit_evidence_to_snapshot(
            company_id=company_id, branch_id=existing.branch_id, period_id=period_id,
            snapshot_id=snapshot_id, db=db,
        )

        # Auto-create the PeriodApproval review item inside this transaction.
        # The review item is owned by the submitting user; AllowSelfApproval
        # applies when the same user later tries to approve it.
        #
        # SAIntegrityError guard: in a true race two concurrent requests can
        # both pass the duplicate-Pending SELECT check above before either
        # INSERT commits.  The partial unique index
        # ux_ReviewItems_OnePendingPeriodApproval then rejects the second
        # INSERT.  We catch that violation here and surface a clean 422
        # instead of letting a raw DB error propagate to the client.
        try:
            ri_result = await db.execute(
                text("""
                    INSERT INTO review.managerreviewitems
                        (companyid, branchid, requestedbyuserid,
                         requesttype, entityschema, entityname, entityid,
                         title, description, priority, status, payrollcalculationsnapshotid)
                    VALUES
                        (:cid, :bid, :uid,
                         'PeriodApproval', 'payroll', 'PayrollPeriods', :eid,
                         :title, :description, 'Normal', 'Pending', :snapshot_id)
                    RETURNING reviewitemid
                """),
                {
                    "cid":         company_id,
                    "bid":         existing.branch_id,
                    "uid":         user_id,
                    "eid":         str(period_id),
                    "snapshot_id": snapshot_id,
                    "title":       f"Payroll Period Approval: {existing.period_name} ({existing.branch_name})",
                    "description": (
                        f"Period {existing.period_name} ({existing.period_code}) has been "
                        f"submitted for approval. Date range: {existing.start_date} to "
                        f"{existing.end_date}."
                    ),
                },
            )
        except SAIntegrityError:
            raise HTTPException(
                status_code=422,
                detail="A pending review already exists for this payroll period.",
            )
        review_item_id: int = ri_result.scalar_one()

        # Write review item creation audit (inside the same transaction).
        await db.execute(
            text("""
                INSERT INTO audit.auditlog
                    (companyid, branchid, actoruserid, actioncode,
                     entityschema, entityname, entityid,
                     newvaluejson, reason, sourcetype)
                VALUES
                    (:cid, :bid, :uid, 'REVIEW_ITEM_CREATED',
                     'review', 'ManagerReviewItems', :riid,
                     :new_val, 'Review item submitted', 'Application')
            """),
            {
                "cid":     company_id,
                "bid":     existing.branch_id,
                "uid":     user_id,
                "riid":    str(review_item_id),
                "new_val": json.dumps({
                    "request_type": "PeriodApproval",
                    "period_id":    period_id,
                    "period_name":  existing.period_name,
                }),
            },
        )

    # Build the SET clause — also stamp the relevant timestamp column
    extra_set = ""
    extra_params: dict[str, Any] = {}

    if change.status == "Locked":
        extra_set = ", lockedbyuserid = :locker, lockedatutc = NOW()"
        extra_params["locker"] = user_id
    elif existing.status in ("Open", "Returned") and change.status == "InReview":
        # CP-1D: populate SubmittedAtUtc atomically with the status transition
        # (covers both initial submission and Returned→InReview resubmission).
        extra_set = ", submittedatutc = NOW()"

    notes_set = ", notes = :notes" if change.notes is not None else ""
    notes_params = {"notes": change.notes} if change.notes is not None else {}

    # CP-1A: InReview→Open and InReview→Cancelled are now blocked via _VALID_TRANSITIONS.
    # The CP-0C lock acquisition for those exits is removed because the transitions
    # are unreachable — change_period_status raises before this point when they're
    # attempted.  Returned→anything is also blocked via PATCH.

    # For Open→InReview use an atomic UPDATE WHERE status='Open' RETURNING to
    # prevent a double-submit race.  Two concurrent requests that both passed the
    # duplicate-Pending guard could both try to update; only the first succeeds.
    # All other transitions keep the simple UPDATE (no race risk: the status guard
    # above already held a FOR UPDATE lock via get_period_by_id).
    if existing.status == "Open" and change.status == "InReview":
        try:
            update_result = await db.execute(
                text(
                    f"UPDATE payroll.payrollperiods "
                    f"SET    status = :new_status{extra_set}{notes_set} "
                    f"WHERE  payrollperiodid = :period_id "
                    f"  AND  companyid       = :company_id "
                    f"  AND  branchid        = :branch_id "
                    f"  AND  status          = 'Open' "
                    f"RETURNING payrollperiodid"
                ),
                {
                    "new_status": change.status,
                    "period_id": period_id,
                    "company_id": company_id,
                    "branch_id": existing.branch_id,
                    **extra_params,
                    **notes_params,
                },
            )
        except SAIntegrityError as exc:
            if _is_inreview_slot_violation(exc):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A concurrent submission created an InReview period for this "
                        "branch at the same time. Only one period may be in review per "
                        "branch. This submission has been rolled back."
                    ),
                )
            raise
        if update_result.first() is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Period is no longer Open — a concurrent submission may have "
                    "already moved it. Please refresh and try again."
                ),
            )

        # CP-1D: Atomically promote eligible adjacent Draft → Open in the same transaction.
        if _eligible_draft is not None:
            _draft_promote = await db.execute(
                text("""
                    UPDATE payroll.payrollperiods
                    SET    status = 'Open'
                    WHERE  payrollperiodid = :did
                      AND  companyid       = :cid
                      AND  branchid        = :bid
                      AND  status          = 'Draft'
                    RETURNING payrollperiodid
                """),
                {
                    "did": _eligible_draft["payrollperiodid"],
                    "cid": company_id,
                    "bid": existing.branch_id,
                },
            )
            if _draft_promote.first() is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Adjacent Draft period moved concurrently during submission. "
                        "Transaction rolled back. Please refresh and try again."
                    ),
                )
            await _write_period_status_audit(
                db,
                company_id=company_id,
                branch_id=existing.branch_id,
                user_id=user_id,
                period_id=_eligible_draft["payrollperiodid"],
                old_status="Draft",
                new_status="Open",
                extra={
                    "trigger":             "submit_promotion",
                    "submitted_period_id": period_id,
                },
            )
            # CP-2E: promoted period is now Open — generate and freeze its snapshot.
            await _regenerate_period_driver_eligibility_rows(
                _eligible_draft["payrollperiodid"],
                company_id,
                existing.branch_id,
                db,
                created_by_user_id=user_id,
                frozen_by_user_id=user_id,
            )
            # CP-2F: refresh status payment lines for Draft-era PPDES rows now that
            # the period is Open and rates are resolved.
            await _refresh_status_payment_lines(
                period_id=_eligible_draft["payrollperiodid"],
                company_id=company_id,
                branch_id=existing.branch_id,
                user_id=user_id,
                db=db,
            )
            # CP-2F: refresh daily calculations for Draft-era source lines now that
            # the period is Open and approved rates can be looked up.
            _draft_period_summary = await get_period_by_id(
                company_id, user_id, _eligible_draft["payrollperiodid"], db
            )
            await _refresh_draft_calculations(
                period_id=_eligible_draft["payrollperiodid"],
                company_id=company_id,
                period_start_date=_draft_period_summary.start_date,
                db=db,
            )
    else:
        # CP-0B: All non-Open→InReview transitions use an expected-status predicate
        # so that a stale request whose pre-flight read is now out of date cannot
        # silently overwrite a status that changed concurrently.
        #
        # The predicate is: payrollperiodid=:period_id AND companyid=:company_id AND
        # status=:old_status.  Zero RETURNING rows means another transaction already
        # moved this period; we surface a 409 Conflict rather than a silent no-op.
        update_result = await db.execute(
            text(
                f"UPDATE payroll.payrollperiods "
                f"SET    status = :new_status{extra_set}{notes_set} "
                f"WHERE  payrollperiodid = :period_id "
                f"  AND  companyid       = :company_id "
                f"  AND  branchid        = :branch_id "
                f"  AND  status          = :old_status "
                f"RETURNING payrollperiodid"
            ),
            {
                "new_status": change.status,
                "period_id": period_id,
                "company_id": company_id,
                "branch_id": existing.branch_id,
                "old_status": existing.status,
                **extra_params,
                **notes_params,
            },
        )
        if update_result.first() is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Period is no longer '{existing.status}' — "
                    "a concurrent transition may have already moved it. "
                    "Please refresh and try again."
                ),
            )

    # Audit: write inside the same transaction so a failure rolls back the UPDATE.
    await _write_period_status_audit(
        db,
        company_id=company_id,
        branch_id=existing.branch_id,
        user_id=user_id,
        period_id=period_id,
        old_status=existing.status,
        new_status=change.status,
    )

    return await get_period_by_id(company_id, user_id, period_id, db)


# ===========================================================================
# CP-1B: One-InReview-per-branch slot helpers
# ===========================================================================

def _is_inreview_slot_violation(exc: SAIntegrityError) -> bool:
    """Return True iff the IntegrityError is from the InReview-slot unique index."""
    orig = getattr(exc, "orig", None)
    if orig is not None:
        name = getattr(orig, "constraint_name", None)
        if name is not None:
            return name.lower() == "ux_payrollperiods_oneinreviewperbranch"
    return "ux_payrollperiods_oneinreviewperbranch" in str(exc).lower()


async def _check_inreview_slot_available(
    company_id: int,
    branch_id: int,
    current_period_id: int,
    db: AsyncConnection,
) -> None:
    """
    Friendly pre-write guard for the InReview slot.

    Raises HTTP 409 if another period for the same company/branch is already
    InReview.  current_period_id is excluded defensively (e.g., when the caller
    is a resubmit path and the period is Returned, not InReview).

    The partial unique index ux_payrollperiods_oneinreviewperbranch is the
    concurrency authority.  This check provides a friendlier error message on
    the non-race (sequential) path.
    """
    row = await db.execute(
        text("""
            SELECT payrollperiodid
            FROM   payroll.payrollperiods
            WHERE  companyid        = :cid
              AND  branchid         = :bid
              AND  status           = 'InReview'
              AND  payrollperiodid != :pid
            LIMIT 1
        """),
        {"cid": company_id, "bid": branch_id, "pid": current_period_id},
    )
    if row.first() is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "An InReview period already exists for this branch. "
                "Only one period may be in review at a time. "
                "Wait for the current review to complete before submitting another."
            ),
        )


# ===========================================================================
# CP-1A: Resubmission
# ===========================================================================

@_translate_submit_transaction_failures
async def resubmit_period(
    company_id: int,
    user_id: int,
    period_id: int,
    db: AsyncConnection,
) -> "PeriodSummary":
    """
    POST /payroll/periods/{period_id}/resubmissions

    Resubmit a Returned period for review.  Requires payroll.entry permission.
    Driver/ODA roles are blocked.

    Steps:
      1. Driver/ODA guard.
      2. Load the period (access + permission check).
      3. Acquire FOR UPDATE lock; verify status is still Returned.
      4. Run all Open→InReview submission guards (refresh, empty, NMR, zero-calc,
         duplicate-Pending).
      5. Create a new Pending PeriodApproval review item.
      6. UPDATE period: status='InReview', CurrentReturnReviewItemID=NULL
         WHERE status='Returned' RETURNING.
      7. Write audits (review item created + period status changed).
      8. Return refreshed PeriodSummary.
    """
    # CP-4D: must precede every resubmission database helper.
    await _set_submit_transaction_isolation(db)

    # ── Step 1: driver/ODA guard ─────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Payroll resubmission is not accessible to driver-role users.",
        )

    # ── Step 2: load period (access check) ───────────────────────────────── #
    existing = await get_period_by_id(company_id, user_id, period_id, db)

    # Friendly pre-flight (race-safe lock comes next).
    if existing.status != "Returned":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only Returned periods can be resubmitted "
                f"(current status: '{existing.status}'). "
                "Use POST /payroll/periods/{id}/resubmissions only on Returned periods."
            ),
        )

    # Permission gate: resubmission requires payroll.entry.
    await _check_permission(company_id, user_id, existing.branch_id, "payroll.entry", db)

    # ── Step 3: acquire branch advisory lock, then period row lock ───────── #
    # CP-1D: branch lock must come first (same order as submit and creation) to
    # prevent deadlock when concurrent submit + resubmit race on the same branch.
    await _acquire_branch_workflow_lock(company_id, existing.branch_id, db)

    lock_result = await db.execute(
        text(
            "SELECT status FROM payroll.payrollperiods "
            "WHERE payrollperiodid = :pid AND companyid = :cid "
            "FOR UPDATE"
        ),
        {"pid": period_id, "cid": company_id},
    )
    lock_row = lock_result.mappings().first()
    locked_status = lock_row["status"] if lock_row else "unknown"
    if locked_status != "Returned":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Period is no longer Returned (current status: '{locked_status}'). "
                "A concurrent resubmission or state change may have moved it. "
                "Please refresh and try again."
            ),
        )

    # ── Step 3b: CP-1B InReview slot guard ───────────────────────────────── #
    await _check_inreview_slot_available(company_id, existing.branch_id, period_id, db)

    # ── Step 4: run submission guards (same as Open→InReview) ────────────── #

    # Keep Returned resubmission in parity with first submission. The stored
    # projection remains compatibility-only; the captured packet uses live PPDES.
    await _refresh_status_payment_lines(
        period_id=period_id,
        company_id=company_id,
        branch_id=existing.branch_id,
        user_id=user_id,
        db=db,
    )

    # Refresh draft calculations so the guards see current rates.
    await _refresh_draft_calculations(
        period_id=period_id,
        company_id=company_id,
        period_start_date=existing.start_date,
        db=db,
    )

    # Guard 1: empty period.
    # CP-3A: count non-BONUS DraftLines + Active BonusEvents.
    empty_result = await db.execute(
        text("""
            SELECT (
                SELECT COUNT(*) FROM payroll.payrolldraftlines
                WHERE  payrollperiodid = :pid AND companyid = :cid
                  AND  status != 'Void' AND linetype != 'BONUS'
            ) + (
                SELECT COUNT(*) FROM payroll.payrollbonusevents
                WHERE  payrollperiodid = :pid AND companyid = :cid
                  AND  status = 'Active'
            ) AS total_lines
        """),
        {"pid": period_id, "cid": company_id},
    )
    if int(empty_result.scalar_one()) == 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot resubmit an empty period for review. "
                "Add at least one non-voided draft line before resubmitting."
            ),
        )

    # Guard 2: unresolved NeedsManagerReview lines.
    unresolved_result = await db.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid    = :pid
              AND  companyid          = :cid
              AND  status            != 'Void'
              AND  needsmanagerreview  = TRUE
        """),
        {"pid": period_id, "cid": company_id},
    )
    unresolved_count = int(unresolved_result.scalar_one())
    if unresolved_count > 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot resubmit: {unresolved_count} draft line(s) still require "
                f"manager review (calculatedamount unresolved or manually flagged). "
                f"Resolve all flagged lines before resubmitting."
            ),
        )

    # Guard 3: zero-calc unresolved lines.
    zero_calc_result = await db.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM   payroll.payrolldraftlines dl
            WHERE  dl.payrollperiodid    = :pid
              AND  dl.companyid          = :cid
              AND  dl.status            != 'Void'
              AND  dl.needsmanagerreview  = FALSE
              AND  dl.calculatedamount   IS NULL
              AND  (
                EXISTS (
                    SELECT 1 FROM payroll.payitems pi
                    WHERE  pi.payitemcode  = dl.linetype
                      AND  (pi.companyid IS NULL OR pi.companyid = dl.companyid)
                      AND  pi.ratebehavior IN (
                          'OrdinalTier', 'RangeBracket',
                          'RangeProgressive', 'Block'
                      )
                )
                OR
                (
                    dl.rateamount IS NULL
                    AND EXISTS (
                        SELECT 1 FROM payroll.payitems pi
                        WHERE  pi.payitemcode  = dl.linetype
                          AND  (pi.companyid IS NULL OR pi.companyid = dl.companyid)
                          AND  pi.ratebehavior = 'PerUnit'
                    )
                )
                OR dl.linescope = 'Period'
              )
        """),
        {"pid": period_id, "cid": company_id},
    )
    zero_calc_count = int(zero_calc_result.scalar_one())
    if zero_calc_count > 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot resubmit: {zero_calc_count} rate-dependent draft line(s) have no "
                f"resolved calculation amount. Fix or void these lines before resubmitting."
            ),
        )

    # Guard 4: duplicate Pending review item.
    dup_result = await db.execute(
        text("""
            SELECT reviewitemid FROM review.managerreviewitems
            WHERE  companyid    = :cid
              AND  entityschema = 'payroll'
              AND  entityname   = 'PayrollPeriods'
              AND  entityid     = :eid
              AND  requesttype  = 'PeriodApproval'
              AND  status       = 'Pending'
            LIMIT 1
        """),
        {"cid": company_id, "eid": str(period_id)},
    )
    dup_row = dup_result.first()
    if dup_row is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A Pending review item already exists for this period "
                f"(review item ID {dup_row[0]}). Resolve it before resubmitting."
            ),
        )

    packet = await _build_live_calculation_packet(existing, company_id, db)
    snapshot_id = await _capture_calculation_snapshot(
        period=existing,
        company_id=company_id,
        user_id=user_id,
        packet=packet,
        db=db,
        context="Resubmit",
    )
    await capture_workflow_action_evidence(
        company_id=company_id,
        branch_id=existing.branch_id,
        period_id=period_id,
        snapshot_id=snapshot_id,
        action_code="RESUBMITTED",
        user_id=user_id,
        required_permission_code="payroll.entry",
        db=db,
    )
    await link_unmapped_audit_evidence_to_snapshot(
        company_id=company_id, branch_id=existing.branch_id, period_id=period_id,
        snapshot_id=snapshot_id, db=db,
    )

    # ── Step 5: create new Pending PeriodApproval review item ────────────── #
    try:
        ri_result = await db.execute(
            text("""
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requestedbyuserid,
                     requesttype, entityschema, entityname, entityid,
                     title, description, priority, status, payrollcalculationsnapshotid)
                VALUES
                    (:cid, :bid, :uid,
                     'PeriodApproval', 'payroll', 'PayrollPeriods', :eid,
                     :title, :description, 'Normal', 'Pending', :snapshot_id)
                RETURNING reviewitemid
            """),
            {
                "cid":         company_id,
                "bid":         existing.branch_id,
                "uid":         user_id,
                "eid":         str(period_id),
                "snapshot_id": snapshot_id,
                "title":       f"Payroll Period Resubmission: {existing.period_name} ({existing.branch_name})",
                "description": (
                    f"Period {existing.period_name} ({existing.period_code}) has been "
                    f"resubmitted for approval after correction. Date range: "
                    f"{existing.start_date} to {existing.end_date}."
                ),
            },
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=422,
            detail="A pending review already exists for this payroll period.",
        )
    new_review_item_id: int = ri_result.scalar_one()

    # Write review item creation audit.
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 newvaluejson, reason, sourcetype)
            VALUES
                (:cid, :bid, :uid, 'REVIEW_ITEM_CREATED',
                 'review', 'ManagerReviewItems', :riid,
                 :new_val, 'Review item submitted', 'Application')
        """),
        {
            "cid":     company_id,
            "bid":     existing.branch_id,
            "uid":     user_id,
            "riid":    str(new_review_item_id),
            "new_val": json.dumps({
                "request_type": "PeriodApproval",
                "period_id":    period_id,
                "period_name":  existing.period_name,
                "resubmission": True,
            }),
        },
    )

    # ── Step 6: atomic transition Returned→InReview, clear pointer ────────── #
    # CP-1D: Include branchid predicate and populate SubmittedAtUtc atomically.
    try:
        update_result = await db.execute(
            text("""
                UPDATE payroll.payrollperiods
                SET    status                    = 'InReview',
                       currentreturnreviewitemid = NULL,
                       submittedatutc            = NOW()
                WHERE  payrollperiodid = :pid
                  AND  companyid       = :cid
                  AND  branchid        = :bid
                  AND  status          = 'Returned'
                RETURNING payrollperiodid
            """),
            {"pid": period_id, "cid": company_id, "bid": existing.branch_id},
        )
    except SAIntegrityError as exc:
        if _is_inreview_slot_violation(exc):
            raise HTTPException(
                status_code=409,
                detail=(
                    "A concurrent resubmission created an InReview period for this "
                    "branch at the same time. Only one period may be in review per "
                    "branch. This resubmission has been rolled back."
                ),
            )
        raise
    if update_result.first() is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Period is no longer Returned — a concurrent resubmission may have "
                "already moved it. Please refresh and try again."
            ),
        )

    # ── Step 7: period status audit ──────────────────────────────────────── #
    await _write_period_status_audit(
        db,
        company_id=company_id,
        branch_id=existing.branch_id,
        user_id=user_id,
        period_id=period_id,
        old_status="Returned",
        new_status="InReview",
    )

    return await get_period_by_id(company_id, user_id, period_id, db)


# ===========================================================================
# Draft lines — entry service
# ===========================================================================


# ---------------------------------------------------------------------------
# List lines
# ---------------------------------------------------------------------------

async def get_period_lines(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    driver_id: int | None = None,
    work_date: date | None = None,
    line_status: str | None = None,
) -> list[DraftLineSummary]:
    """Return draft lines for a period (access-checked via the period lookup)."""
    period = await get_period_by_id(company_id, user_id, period_id, db)

    conditions = [
        "dl.payrollperiodid  = :period_id",
        "dl.companyid        = :company_id",
        # M14: exclude Period Pay lines from the daily lines list.
        # Period Pay lines have linescope='Period' and are returned by
        # get_period_pay_lines() instead.  Using the explicit LineScope column
        # (not WorkDate IS NOT NULL) because daily lines can also have NULL WorkDate.
        "dl.linescope        = 'Daily'",
    ]
    params: dict[str, Any] = {
        "period_id": period_id,
        "company_id": company_id,
    }

    # CP-2F: for Draft periods, exclude System-sourced lines, STATUS_PAYMENT, and
    # ADJUSTMENT / MINIMUM / MAXIMUM pay items — these are financial and must not be
    # visible until the period is promoted to Open.
    if period.status == "Draft":
        conditions.append("dl.sourcetype != 'System'")
        conditions.append(
            "dl.linetype NOT IN ('STATUS_PAYMENT', 'ADJUSTMENT', 'MINIMUM', 'MAXIMUM',"
            " 'SYS_MIN_TOPUP', 'SYS_MAX_CAP')"
        )

    if driver_id is not None:
        conditions.append("dl.driverid = :driver_id")
        params["driver_id"] = driver_id

    if work_date is not None:
        conditions.append("dl.workdate = :work_date")
        params["work_date"] = work_date

    if line_status is not None:
        conditions.append("dl.status = :line_status")
        params["line_status"] = line_status

    where = " AND ".join(conditions)
    result = await db.execute(
        text(f"{_LINE_SELECT} WHERE {where} ORDER BY dl.workdate, dl.driverid, dl.linetype"),
        params,
    )
    rows = [_line_row_to_summary(r) for r in result.mappings().all()]

    # CP-2F: sanitize money fields for Draft so callers never see stale rates/amounts.
    if period.status == "Draft":
        sanitized = []
        for ln in rows:
            ln = ln.model_copy(update={
                "rate_amount": None,
                "calculated_amount": None,
                "needs_manager_review": False,
            })
            sanitized.append(ln)
        return sanitized

    return rows


# ---------------------------------------------------------------------------
# Summary (aggregated per driver × line_type)
# ---------------------------------------------------------------------------

async def get_period_draft_summary(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[DriverPeriodSummary]:
    """
    Aggregated totals per driver × line_type for one period.

    Uses a direct query (not vw_PayrollDraftSummary) so that Void lines
    are explicitly excluded from the aggregation.
    """
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Draft periods have no financial summary — block to avoid returning
    # zero totals that could mislead callers into thinking the period is empty.
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Lines summary is not available for Prepared (Draft) periods.",
        )

    result = await db.execute(
        text("""
            SELECT
                dl.driverid,
                e.fullname                                                              AS drivername,
                dl.payrollperiodid,
                :period_name                                                            AS periodname,
                dl.linetype,
                SUM(dl.quantity)                                                        AS totalquantity,
                SUM(COALESCE(dl.calculatedamount, 0))                                  AS totalcalculatedamount,
                COUNT(*)                                                                AS linecount,
                SUM(CASE WHEN dl.status IN ('NeedsReview', 'Rejected')
                          OR dl.needsmanagerreview THEN 1 ELSE 0 END)                  AS linesneedingattention
            FROM   payroll.payrolldraftlines dl
            JOIN   core.drivers              d  ON d.driverid   = dl.driverid
            JOIN   core.employees            e  ON e.employeeid = d.employeeid
            WHERE  dl.payrollperiodid = :period_id
              AND  dl.companyid       = :company_id
              AND  dl.status         != 'Void'
              AND  dl.linescope       = 'Daily'
            GROUP  BY dl.driverid, e.fullname, dl.payrollperiodid, dl.linetype
            ORDER  BY e.fullname, dl.linetype
        """),
        {
            "period_id":   period_id,
            "company_id":  company_id,
            "period_name": period.period_name,
        },
    )
    return [
        DriverPeriodSummary(
            driver_id=r["driverid"],
            driver_name=r["drivername"],
            period_id=r["payrollperiodid"],
            period_name=r["periodname"],
            line_type=r["linetype"],
            total_quantity=r["totalquantity"],
            total_calculated_amount=r["totalcalculatedamount"],
            line_count=r["linecount"],
            lines_needing_attention=r["linesneedingattention"],
        )
        for r in result.mappings().all()
    ]


# ===========================================================================
# M13a: PayItem-driven line-type validation
# M13b: PerUnit / EnteredAmount calculated-amount engine
# ===========================================================================
#
# TRANSITIONAL DESIGN NOTE (target cleanup: M14)
# ─────────────────────────────────────────────
# System pay items were established before the PayItems catalog.
# Their line-type strings (e.g. "Hours", "Miles") are stored verbatim in
# PayrollDraftLines.LineType and differ from their PayItemCodes ("HOURS", "MILES").
#
# _SYSTEM_LINE_TYPES    — fast-path set; these bypass the full DB lookup.
# _SYSTEM_LINE_TYPE_INFO — rate_behavior, rate_code, and item_scope for each
#                          system string.
#
# Custom pay items (CompanyID IS NOT NULL) use their PayItemCode directly as
# LineType — no alias.  They are validated via the DB slow path.
#
# BranchPayItemConfig IS checked for system items (Fix 2, M13b): the fast path
# now validates branch activation via a DB query for items that have a DB counterpart
# (DailyStatus and DailyNote have no DB counterpart and are accepted unconditionally).
#
# PayItemRateTypeMap rows for system PerUnit items are seeded by migration 0008.
# The fast path queries PayItemRateTypeMap for rate_code (Fix 4, M13b) and falls
# back to the hardcoded _SYSTEM_LINE_TYPE_INFO value ONLY as a backward-compat
# measure during zero-downtime deploys.  On a properly migrated DB (head >= 0008)
# the fallback should never trigger.
#
# ─────────────────────────────────────────────


# Maps legacy system line-type strings to their calculation metadata.
# Any string NOT in this dict goes through the custom-item DB slow path.
_SYSTEM_LINE_TYPE_INFO: dict[str, _LineTypeInfo] = {
    "Hours":       _LineTypeInfo("PerUnit",       "HOURLY"),
    "Miles":       _LineTypeInfo("PerUnit",       "MILEAGE"),
    "Loads":       _LineTypeInfo("PerUnit",       "LOAD"),
    "Overnight":   _LineTypeInfo("Fixed",         None),   # fixed per-night; PayItemSettings (M13c+)
    "Wait":        _LineTypeInfo("PerUnit",       "WAIT"),
    "Pallets":     _LineTypeInfo("PerUnit",       "PALLET"),
    "Silos":       _LineTypeInfo("PerUnit",       "SILO"),
    "DailyStatus": _LineTypeInfo("None",          None),   # informational
    "DailyNote":   _LineTypeInfo("None",          None),   # informational
    "Bonus":       _LineTypeInfo("Fixed",         None,  "Period"),  # Period item; blocked from daily entry
    "Adjustment":  _LineTypeInfo("Fixed",         None,  "Period"),  # Period item; blocked from daily entry
}

_SYSTEM_LINE_TYPES: frozenset[str] = frozenset(_SYSTEM_LINE_TYPE_INFO)

# All behaviors that need a DriverRate (including Block which doesn't use tiers).
_RATE_USING_BEHAVIORS: frozenset[str] = frozenset(
    {"PerUnit", "OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
)

# System period items that can be entered via the Period Pay endpoint (M14).
# Both legacy display names ("Bonus") and canonical PayItemCodes ("BONUS") are
# accepted — both map to the same canonical code for DB lookup and storage.
_SYSTEM_PERIOD_ALLOWED: dict[str, str] = {
    "Bonus":       "BONUS",
    "Adjustment":  "ADJUSTMENT",
    "BONUS":       "BONUS",       # canonical alias
    "ADJUSTMENT":  "ADJUSTMENT",  # canonical alias
}
_SYSTEM_PERIOD_ALLOWED_TYPES: frozenset[str] = frozenset(_SYSTEM_PERIOD_ALLOWED)

# System period items blocked in M14 (need automated pay-rule engine, M15+).
# Both display-name and DB-code forms accepted so callers get a clear message
# regardless of which form they use.


_RATE_DEPENDENT_BEHAVIORS = frozenset(
    {"PerUnit", "OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
)


async def _refresh_draft_calculations(
    period_id: int,
    company_id: int,
    period_start_date: date,
    db: AsyncConnection,
) -> int:
    """
    Automatically re-compute calculatedamount + needsmanagerreview for every
    non-void, rate-dependent draft line in a period using the currently
    approved effective-dated rates for each line's work_date.

    Called automatically at:
      • Open → InReview (before submit guards) so newly approved backdated
        rates are reflected before blocking checks run.
      • finalize_period (after the period is confirmed Approved, before
        blocker guards) so finalization uses the most current rates.

    Only touches PerUnit / OrdinalTier / RangeBracket / RangeProgressive /
    Block lines.  EnteredAmount (BONUS, etc.), Fixed, and None lines are
    left unchanged — their calculatedamount is either entered directly by
    the user or not applicable.

    Returns the count of lines whose stored values were updated.
    """
    # Step 1: fetch all non-void, non-informational draft lines for the period.
    lines_result = await db.execute(
        text("""
            SELECT draftlineid, driverid, linetype, workdate,
                   quantity, rateamount, calculatedamount, needsmanagerreview
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  status         != 'Void'
              AND  linetype       NOT IN ('DailyStatus', 'DailyNote')
        """),
        {"pid": period_id, "cid": company_id},
    )
    rows = list(lines_result.mappings().all())
    if not rows:
        return 0

    # Step 2: for each unique canonical line type, fetch ratebehavior + rate_code
    # once and cache.  Avoids per-line round-trips for the metadata lookup.
    lt_info_cache: dict[str, "_LineTypeInfo | None"] = {}

    async def _get_lt_info(canonical: str) -> "_LineTypeInfo | None":
        if canonical in lt_info_cache:
            return lt_info_cache[canonical]
        pi_result = await db.execute(
            text("""
                SELECT pi.ratebehavior,
                       (
                           SELECT rt.ratecode
                           FROM   payroll.payitemratetypemap pirtm
                           JOIN   payroll.ratetypes rt ON rt.ratetypeid = pirtm.ratetypeid
                           WHERE  pirtm.payitemid = pi.payitemid
                             AND  pirtm.status    = 'Active'
                             AND  rt.isactive     = TRUE
                           ORDER BY pirtm.isprimary DESC
                           LIMIT 1
                       ) AS rate_code
                FROM   payroll.payitems pi
                WHERE  pi.payitemcode = :code
                  AND  (pi.companyid IS NULL OR pi.companyid = :cid)
                  AND  pi.status    != 'Retired'
                LIMIT 1
            """),
            {"code": canonical, "cid": company_id},
        )
        pi_row = pi_result.mappings().first()
        if pi_row is None:
            lt_info_cache[canonical] = None
            return None
        info = _LineTypeInfo(
            rate_behavior=pi_row["ratebehavior"],
            rate_code=pi_row["rate_code"],
        )
        lt_info_cache[canonical] = info
        return info

    # Step 3: for each line, recompute and update if values changed.
    refresh_count = 0
    for row in rows:
        canonical = _LEGACY_TO_CANONICAL.get(row["linetype"], row["linetype"])
        lt_info = await _get_lt_info(canonical)
        if lt_info is None:
            continue  # unknown/retired item — leave as-is
        if lt_info.rate_behavior not in _RATE_DEPENDENT_BEHAVIORS:
            continue  # EnteredAmount / Fixed / None — not our concern

        as_of: date = (
            row["workdate"] if row["workdate"] is not None else period_start_date
        )
        qty = Decimal(str(row["quantity"])) if row["quantity"] is not None else Decimal("0")
        rate_ovr = (
            Decimal(str(row["rateamount"])) if row["rateamount"] is not None else None
        )

        _cr = await _compute_calculated_amount(
            rate_behavior=lt_info.rate_behavior,
            rate_code=lt_info.rate_code,
            quantity=qty,
            rate_amount_override=rate_ovr,
            driver_id=row["driverid"],
            company_id=company_id,
            as_of_date=as_of,
            db=db,
        )
        new_calc, new_review = _cr.calculated_amount, _cr.needs_manager_review

        old_calc = (
            Decimal(str(row["calculatedamount"]))
            if row["calculatedamount"] is not None
            else None
        )
        old_review = bool(row["needsmanagerreview"])
        old_rate_ovr = (
            Decimal(str(row["rateamount"])) if row["rateamount"] is not None else None
        )

        # Guard: respect manager-controlled NMR flags.
        #
        # Two cases where we DO NOT auto-clear needsmanagerreview:
        #   a) NMR=True AND calc IS NOT NULL:
        #      The line already has a computed amount; the manager manually
        #      flagged it for human review.  The refresh must not overrule that.
        #   b) NMR=True AND rate_amount IS NOT NULL (but calc IS NULL):
        #      A manual rate override was supplied.  Finalization will use
        #      COALESCE(calc, qty * rate_amount), so the line is resolvable.
        #      The manager's flag is still deliberate — leave it alone.
        #
        # We DO refresh when:
        #   NMR=True AND calc IS NULL AND rate_amount IS NULL:
        #      Truly unresolved — no approved rate was found at entry time.
        #      A rate may now exist (backdated approval); re-compute and,
        #      if resolved, auto-clear NMR so submission is no longer blocked.
        #   NMR=False (regardless of calc state):
        #      Normal line — calc may have become stale if the approved rate
        #      changed since the line was entered.  Re-compute to stay current.
        if old_review and (old_calc is not None or old_rate_ovr is not None):
            continue  # manager-flagged with a resolvable path — do not touch

        if new_calc != old_calc or new_review != old_review:
            await db.execute(
                text("""
                    UPDATE payroll.payrolldraftlines
                    SET    calculatedamount   = :calc,
                           needsmanagerreview = :review
                    WHERE  draftlineid = :lid
                """),
                {"calc": new_calc, "review": new_review, "lid": row["draftlineid"]},
            )
            refresh_count += 1

    return refresh_count


# ===========================================================================
# Finalization — Approved → Locked
# ===========================================================================

# ---------------------------------------------------------------------------
# Phase 7: Shared finalization validator
# Used by both finalize_period and get_finalization_preview so that preview
# surfaces exactly the same blockers that finalize_period would enforce.
# Does NOT check: period status, permissions, empty-period, NMR/zero-calc,
# or min/max cross-rule guards (those depend on per-path state).
# ---------------------------------------------------------------------------

async def _validate_period_can_finalize(
    period_id: int,
    company_id: int,
    branch_id: int,
    period_start: "date",
    period_end: "date",
    db: AsyncConnection,
) -> list[str]:
    """
    Run shared pre-finalization checks used by both finalize_period and
    get_finalization_preview.  Returns a list of human-readable blocker
    strings (empty list = no blockers found).

    Checks (in order):
      1. Duplicate active Daily draft lines for the same (driver, date, type).
      2. Driver eligibility for Daily lines (per-date window).
      3. Driver eligibility for Period Pay lines (period overlap window).
      4. Contaminated/foreign RateType used by any rate-driven draft line.

    The messages are intentionally kept identical to the strings previously
    raised as individual HTTPException 422 details in finalize_period so that
    existing test assertions (e.g. "duplicate" in detail.lower()) continue to
    pass unchanged.
    """
    blockers: list[str] = []

    # ── 1. Duplicate active Daily draft lines ─────────────────────────────────
    dup_result = await db.execute(
        text("""
            SELECT driverid, workdate, linetype, COUNT(*) AS cnt
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :period_id
              AND  companyid       = :company_id
              AND  linescope       = 'Daily'
              AND  status         != 'Void'
            GROUP BY driverid, workdate, linetype
            HAVING COUNT(*) > 1
            LIMIT 5
        """),
        {"period_id": period_id, "company_id": company_id},
    )
    dup_rows = dup_result.mappings().all()
    if dup_rows:
        examples = "; ".join(
            f"driver {r['driverid']} {r['workdate']} {r['linetype']} ×{r['cnt']}"
            for r in dup_rows
        )
        blockers.append(
            f"Cannot finalize: duplicate active Daily draft lines detected "
            f"({examples}). Void the extra lines before finalizing."
        )

    # ── 2. Driver eligibility — Daily lines ───────────────────────────────────
    # CP-2E: use snapshot-based eligibility for snapshotted periods to correctly
    # handle IncludedByExistingData, TerminatedHistorical, and Transferred drivers.
    # Legacy live-query path retained for periods without a snapshot.
    _has_snapshot = await _period_has_driver_eligibility_snapshot(period_id, db)
    if _has_snapshot:
        elig_daily_result = await db.execute(
            text("""
                SELECT dl.draftlineid, dl.driverid, dl.workdate, dl.linetype
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :period_id
                  AND  dl.companyid       = :company_id
                  AND  dl.status         != 'Void'
                  AND  dl.linescope       = 'Daily'
                  AND  NOT EXISTS (
                           SELECT 1
                           FROM   payroll.payrollperioddrivereligibility ppde
                           WHERE  ppde.payrollperiodid = dl.payrollperiodid
                             AND  ppde.driverid        = dl.driverid
                             AND  ppde.iseligibleforperiod = TRUE
                             -- CP-2E: a DraftLine that already exists proves existing source
                             -- on that exact date for any reason code (including generated-row
                             -- drivers outside their date window). Pass if in snapshot at all.
                       )
                LIMIT 5
            """),
            {"period_id": period_id, "company_id": company_id},
        )
    else:
        elig_daily_result = await db.execute(
            text("""
                SELECT dl.draftlineid, dl.driverid, dl.workdate, dl.linetype
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :period_id
                  AND  dl.companyid       = :company_id
                  AND  dl.status         != 'Void'
                  AND  dl.linescope       = 'Daily'
                  AND  NOT EXISTS (
                           SELECT 1
                           FROM   core.drivers   d
                           JOIN   core.employees e ON e.employeeid = d.employeeid
                           WHERE  d.driverid         = dl.driverid
                             AND  d.companyid        = :company_id
                             AND  d.branchid         = :branch_id
                             AND  e.employmentstatus = 'Active'
                             AND  (
                                      d.driverstatus = 'Active'
                                   OR (d.driverstatus = 'Transferred'
                                       AND d.effectiveto IS NOT NULL
                                       AND d.effectiveto >= dl.workdate)
                                  )
                             AND  (e.hiredate IS NULL OR e.hiredate <= dl.workdate)
                             AND  (e.terminationdate IS NULL OR e.terminationdate >= dl.workdate)
                             AND  (d.effectivefrom IS NULL OR d.effectivefrom <= dl.workdate)
                             AND  (d.effectiveto   IS NULL OR d.effectiveto   >= dl.workdate)
                       )
                LIMIT 5
            """),
            {"period_id": period_id, "company_id": company_id, "branch_id": branch_id},
        )
    elig_daily_rows = elig_daily_result.mappings().all()
    if elig_daily_rows:
        examples = "; ".join(
            f"driver {r['driverid']} {r['workdate']} {r['linetype']}"
            for r in elig_daily_rows
        )
        blockers.append(
            f"Cannot finalize: {len(elig_daily_rows)} Daily draft line(s) reference "
            f"driver/date combinations that are no longer eligible "
            f"({examples}). Void these lines before finalizing."
        )

    # ── 3. Driver eligibility — Period Pay lines ──────────────────────────────
    if _has_snapshot:
        elig_period_result = await db.execute(
            text("""
                SELECT dl.draftlineid, dl.driverid, dl.linetype
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :period_id
                  AND  dl.companyid       = :company_id
                  AND  dl.status         != 'Void'
                  AND  dl.linescope       = 'Period'
                  AND  NOT EXISTS (
                           SELECT 1
                           FROM   payroll.payrollperioddrivereligibility ppde
                           WHERE  ppde.payrollperiodid = dl.payrollperiodid
                             AND  ppde.driverid        = dl.driverid
                             AND  ppde.iseligibleforperiod = TRUE
                       )
                LIMIT 5
            """),
            {"period_id": period_id, "company_id": company_id},
        )
    else:
        elig_period_result = await db.execute(
            text("""
                SELECT dl.draftlineid, dl.driverid, dl.linetype
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :period_id
                  AND  dl.companyid       = :company_id
                  AND  dl.status         != 'Void'
                  AND  dl.linescope       = 'Period'
                  AND  NOT EXISTS (
                           SELECT 1
                           FROM   core.drivers   d
                           JOIN   core.employees e ON e.employeeid = d.employeeid
                           WHERE  d.driverid         = dl.driverid
                             AND  d.companyid        = :company_id
                             AND  d.branchid         = :branch_id
                             AND  e.employmentstatus = 'Active'
                             AND  d.driverstatus     = 'Active'
                             AND  (e.hiredate IS NULL OR e.hiredate <= :period_end)
                             AND  (e.terminationdate IS NULL OR e.terminationdate >= :period_start)
                             AND  (d.effectivefrom IS NULL OR d.effectivefrom <= :period_end)
                             AND  (d.effectiveto   IS NULL OR d.effectiveto   >= :period_start)
                       )
                LIMIT 5
            """),
            {
                "period_id":    period_id,
                "company_id":   company_id,
                "branch_id":    branch_id,
                "period_start": period_start,
                "period_end":   period_end,
            },
        )
    elig_period_rows = elig_period_result.mappings().all()
    if elig_period_rows:
        examples = "; ".join(
            f"driver {r['driverid']} {r['linetype']}"
            for r in elig_period_rows
        )
        blockers.append(
            f"Cannot finalize: {len(elig_period_rows)} Period Pay draft line(s) reference "
            f"ineligible drivers ({examples}). Void these lines before finalizing."
        )

    # ── 4. Contaminated / foreign RateType ────────────────────────────────────
    # (unchanged from Phase 7)
    contaminated_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT rt.ratetypeid) AS cnt
            FROM   payroll.payrolldraftlines dl
            JOIN   payroll.payitems pi
                   ON pi.payitemcode = dl.linetype
                  AND (pi.companyid IS NULL OR pi.companyid = :company_id)
                  AND pi.status      != 'Retired'
                  AND pi.requiresrate = TRUE
            JOIN   payroll.payitemratetypemap pirm
                   ON pirm.payitemid = pi.payitemid AND pirm.status = 'Active'
            JOIN   payroll.ratetypes rt
                   ON rt.ratetypeid = pirm.ratetypeid AND rt.isactive = TRUE
            WHERE  dl.payrollperiodid = :period_id
              AND  dl.companyid       = :company_id
              AND  dl.status         != 'Void'
              AND NOT (rt.companyid IS NULL OR rt.companyid = :company_id)
        """),
        {"period_id": period_id, "company_id": company_id},
    )
    contaminated_count = int(contaminated_result.scalar_one())
    if contaminated_count > 0:
        blockers.append(
            f"Cannot finalize: {contaminated_count} rate type(s) used by draft lines "
            "in this period are not valid for this company (foreign-owned, contaminated, "
            "or orphaned). Investigate and void or correct the affected draft lines."
        )

    # ── 5. Unresolvable rate type mapping (Phase 8 — fail-closed) ────────────
    # Finds non-void, rate-dependent draft lines whose PayItem has no active
    # PayItemRateTypeMap entry.  These lines cannot be correctly calculated
    # because their rate_code is unknown — the rate behavior is unresolvable.
    # NOTE: such lines will also be caught by the NMR blocker (Blocker 2 in
    # preview / Step 1.8 in finalize) because _compute_calculated_amount
    # returns NMR=True when rate_code is None.  This check provides the
    # specific "configure the mapping" message that the generic NMR message
    # does not.
    unresolvable_result = await db.execute(
        text("""
            SELECT dl.draftlineid, dl.linetype, pi.ratebehavior
            FROM   payroll.payrolldraftlines dl
            JOIN   payroll.payitems pi
                   ON pi.payitemcode = dl.linetype
                  AND (pi.companyid IS NULL OR pi.companyid = :company_id)
                  AND pi.status     != 'Retired'
            WHERE  dl.payrollperiodid = :period_id
              AND  dl.companyid       = :company_id
              AND  dl.status         != 'Void'
              AND  pi.ratebehavior   IN ('PerUnit', 'OrdinalTier',
                                         'RangeBracket', 'RangeProgressive', 'Block')
              AND  NOT EXISTS (
                       SELECT 1
                       FROM   payroll.payitemratetypemap pirtm
                       JOIN   payroll.ratetypes rt
                              ON rt.ratetypeid = pirtm.ratetypeid
                       WHERE  pirtm.payitemid = pi.payitemid
                         AND  pirtm.status    = 'Active'
                         AND  rt.isactive     = TRUE
                   )
            LIMIT 5
        """),
        {"period_id": period_id, "company_id": company_id},
    )
    unresolvable_rows = unresolvable_result.mappings().all()
    if unresolvable_rows:
        examples = "; ".join(
            f"line {r['draftlineid']} ({r['linetype']}, {r['ratebehavior']})"
            for r in unresolvable_rows
        )
        cnt = len(unresolvable_rows)
        blockers.append(
            f"Cannot finalize: {cnt} rate-dependent draft line(s) have no pay item "
            f"rate type mapping configured ({examples}). "
            "Rate behavior could not be resolved — configure the pay item rate mapping "
            "or void these lines before finalizing."
        )

    return blockers


# ---------------------------------------------------------------------------
# Audit helper — extracted so tests can monkeypatch it to verify rollback.
# All writes in finalize_period() share the same engine.begin() transaction,
# so if _write_finalization_audit() raises, the entire transaction rolls back:
# the UPDATE and INSERT are undone and the period reverts to Approved.
# ---------------------------------------------------------------------------

async def _write_finalization_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    period_id: int,
    line_count: int,
    total_amount: Decimal,
    approved_review_item_id: int | None = None,
    snapshot_id: int | None = None,
    revision_number: int | None = None,
    snapshot_hash: str | None = None,
) -> None:
    """Insert one row into audit.AuditLog for the finalization event."""
    old_val = json.dumps({"status": "Approved"})
    new_value = {
        "status":             "Locked",
        "final_line_count":   line_count,
        "total_final_amount": str(total_amount),
    }
    if approved_review_item_id is not None:
        new_value.update({
            "approved_review_item_id": approved_review_item_id,
            "payroll_calculation_snapshot_id": snapshot_id,
            "revision_number": revision_number,
            "snapshot_hash": snapshot_hash,
        })
    new_val = json.dumps(new_value)
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, :branch_id, :actor_id, 'PAYROLL_FINALIZED',
                 'payroll', 'PayrollPeriods', :entity_id,
                 :old_val, :new_val, 'Payroll period finalized', 'Application')
        """),
        {
            "company_id": company_id,
            "branch_id":  branch_id,
            "actor_id":   user_id,
            "entity_id":  str(period_id),
            "old_val":    old_val,
            "new_val":    new_val,
        },
    )



# ---------------------------------------------------------------------------
# CP-3A / CP-5 — Virtual rate refresh helper (read-only)
# ---------------------------------------------------------------------------

async def _compute_draft_line_preview_amounts(
    period_id: int,
    company_id: int,
    period_start_date: date,
    db: AsyncConnection,
) -> "dict[int, tuple[Decimal | None, bool]]":
    """
    Read-only virtual equivalent of _refresh_draft_calculations.

    Computes what (calculatedamount, needsmanagerreview) WOULD be after a
    real refresh for every rate-dependent draft line that passes the
    manager-NMR guard — without writing anything to the database.

    Returns {draftlineid: (refreshed_calc, refreshed_review)} for each
    eligible line.  Lines excluded by the manager-NMR guard are absent from
    the dict; callers must fall back to the stored values for those.

    Guarantees:
      • No UPDATE / INSERT / DELETE is executed.
      • Safe to call on any period status — purely read-only.
    """
    lines_result = await db.execute(
        text("""
            SELECT draftlineid, driverid, linetype, workdate,
                   quantity, rateamount, calculatedamount, needsmanagerreview
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  status         != 'Void'
              AND  linetype       NOT IN ('DailyStatus', 'DailyNote')
        """),
        {"pid": period_id, "cid": company_id},
    )
    rows = list(lines_result.mappings().all())
    if not rows:
        return {}

    lt_info_cache: "dict[str, _LineTypeInfo | None]" = {}

    async def _get_lt_info(canonical: str) -> "_LineTypeInfo | None":
        if canonical in lt_info_cache:
            return lt_info_cache[canonical]
        pi_result = await db.execute(
            text("""
                SELECT pi.ratebehavior,
                       (
                           SELECT rt.ratecode
                           FROM   payroll.payitemratetypemap pirtm
                           JOIN   payroll.ratetypes rt ON rt.ratetypeid = pirtm.ratetypeid
                           WHERE  pirtm.payitemid = pi.payitemid
                             AND  pirtm.status    = 'Active'
                             AND  rt.isactive     = TRUE
                           ORDER BY pirtm.isprimary DESC
                           LIMIT 1
                       ) AS rate_code
                FROM   payroll.payitems pi
                WHERE  pi.payitemcode = :code
                  AND  (pi.companyid IS NULL OR pi.companyid = :cid)
                  AND  pi.status    != 'Retired'
                LIMIT 1
            """),
            {"code": canonical, "cid": company_id},
        )
        pi_row = pi_result.mappings().first()
        if pi_row is None:
            lt_info_cache[canonical] = None
            return None
        info = _LineTypeInfo(
            rate_behavior=pi_row["ratebehavior"],
            rate_code=pi_row["rate_code"],
        )
        lt_info_cache[canonical] = info
        return info

    result: "dict[int, tuple[Decimal | None, bool]]" = {}
    for row in rows:
        canonical = _LEGACY_TO_CANONICAL.get(row["linetype"], row["linetype"])
        lt_info = await _get_lt_info(canonical)
        if lt_info is None or lt_info.rate_behavior not in _RATE_DEPENDENT_BEHAVIORS:
            continue  # not rate-dependent — stored value is authoritative

        old_calc     = Decimal(str(row["calculatedamount"])) if row["calculatedamount"] is not None else None
        old_review   = bool(row["needsmanagerreview"])
        old_rate_ovr = Decimal(str(row["rateamount"])) if row["rateamount"] is not None else None

        # Same manager-NMR guard as _refresh_draft_calculations:
        # skip if NMR=True AND (calc IS NOT NULL OR rate_amount IS NOT NULL)
        if old_review and (old_calc is not None or old_rate_ovr is not None):
            continue  # manager-flagged with a resolvable path — honour stored values

        as_of: date = row["workdate"] if row["workdate"] is not None else period_start_date
        qty = Decimal(str(row["quantity"])) if row["quantity"] is not None else Decimal("0")

        _cr_prev = await _compute_calculated_amount(
            rate_behavior=lt_info.rate_behavior,
            rate_code=lt_info.rate_code,
            quantity=qty,
            rate_amount_override=old_rate_ovr,
            driver_id=row["driverid"],
            company_id=company_id,
            as_of_date=as_of,
            db=db,
        )
        result[int(row["draftlineid"])] = _cr_prev

    return result


# ---------------------------------------------------------------------------
# CP-3A — Finalization Preview (read-only)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CP-4F — approved immutable snapshot finalization
# ---------------------------------------------------------------------------

def _snapshot_finalization_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{code}: {message}")


async def _load_approved_snapshot_packet(
    *, period_id: int, company_id: int, branch_id: int,
    db: AsyncConnection, lock_review_item: bool = False,
) -> dict[str, Any]:
    """Load the one immutable packet authorized by an Approved PeriodApproval."""
    review_lock = " FOR UPDATE OF ri" if lock_review_item else ""
    reviews = (await db.execute(text(f"""
        SELECT ri.reviewitemid, ri.payrollcalculationsnapshotid
        FROM review.managerreviewitems ri
        WHERE ri.companyid = :cid AND ri.branchid = :bid
          AND ri.requesttype = 'PeriodApproval'
          AND ri.entityschema = 'payroll' AND ri.entityname = 'PayrollPeriods'
          AND ri.entityid = :period_id AND ri.status = 'Approved'{review_lock}
    """), {"cid": company_id, "bid": branch_id, "period_id": str(period_id)})).mappings().all()
    if not reviews:
        raise _snapshot_finalization_error(
            "APPROVED_SNAPSHOT_NOT_FOUND_FOR_FINALIZATION",
            "no approved PeriodApproval review item authorizes this period.",
        )
    if len(reviews) != 1:
        raise _snapshot_finalization_error(
            "APPROVED_SNAPSHOT_INTEGRITY_ERROR",
            "more than one approved PeriodApproval review item exists for this period.",
        )
    review = reviews[0]
    snapshot_id = review["payrollcalculationsnapshotid"]
    if snapshot_id is None:
        raise _snapshot_finalization_error(
            "SNAPSHOT_REQUIRED_FOR_FINALIZATION",
            "the approved PeriodApproval is historical and has no immutable calculation snapshot.",
        )
    snapshot = (await db.execute(text("""
        SELECT payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
               revisionnumber, snapshothash, totalexpectedpay, createdatutc
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :cid AND branchid = :bid
    """), {"snapshot_id": snapshot_id, "cid": company_id, "bid": branch_id})).mappings().first()
    if snapshot is None or int(snapshot["payrollperiodid"]) != period_id:
        raise _snapshot_finalization_error(
            "APPROVED_SNAPSHOT_INTEGRITY_ERROR",
            "the approved review item does not reference a snapshot for this exact period.",
        )
    totals = (await db.execute(text("""
        SELECT dt.payrollcalculationdrivertotalid, dt.driverid,
               dt.drivercodesnapshot, dt.drivernamesnapshot,
               dt.dailypay, dt.statuspay, dt.periodpay, dt.minimumadjustment,
               dt.maximumadjustment, dt.bonustotal, dt.expectedpay
        FROM payroll.payrollcalculationdrivertotals dt
        JOIN core.drivers d ON d.driverid = dt.driverid
            AND d.companyid = dt.companyid AND d.branchid = dt.branchid
        WHERE dt.payrollcalculationsnapshotid = :snapshot_id
          AND dt.companyid = :cid AND dt.branchid = :bid
        ORDER BY dt.driverid, dt.payrollcalculationdrivertotalid
    """), {"snapshot_id": snapshot_id, "cid": company_id, "bid": branch_id})).mappings().all()
    lines = (await db.execute(text("""
        SELECT sl.payrollcalculationsnapshotlineid,
               sl.payrollcalculationdrivertotalid, dt.driverid, sl.sourcetype,
               sl.sourceid, sl.linetype, sl.linescope, sl.workdate, sl.payitemid,
               sl.ratetypeid, sl.driverrateid, sl.bonuseventid, sl.quantity,
               sl.resolvedrateamount, sl.calculatedamount, sl.sourceevidencejsonb
        FROM payroll.payrollcalculationsnapshotlines sl
        JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationdrivertotalid = sl.payrollcalculationdrivertotalid
        WHERE dt.payrollcalculationsnapshotid = :snapshot_id
          AND dt.companyid = :cid AND dt.branchid = :bid
        ORDER BY dt.driverid, sl.payrollcalculationsnapshotlineid
    """), {"snapshot_id": snapshot_id, "cid": company_id, "bid": branch_id})).mappings().all()
    return {"review": review, "snapshot": snapshot, "totals": totals, "lines": lines}


def _reconcile_approved_snapshot_packet(packet: dict[str, Any]) -> None:
    """Check persisted packet arithmetic only; never consult mutable sources."""
    snapshot, totals, lines = packet["snapshot"], packet["totals"], packet["lines"]
    header_total = Decimal(str(snapshot["totalexpectedpay"]))
    totals_total = sum((Decimal(str(row["expectedpay"])) for row in totals), Decimal("0"))
    if header_total != totals_total:
        raise _snapshot_finalization_error("APPROVED_SNAPSHOT_INTEGRITY_ERROR", "snapshot header total does not reconcile with driver totals.")
    line_totals: dict[int, Decimal] = {}
    for line in lines:
        key = int(line["payrollcalculationdrivertotalid"])
        line_totals[key] = line_totals.get(key, Decimal("0")) + Decimal(str(line["calculatedamount"]))
    for total in totals:
        key = int(total["payrollcalculationdrivertotalid"])
        components = sum((Decimal(str(total[column])) for column in (
            "dailypay", "statuspay", "periodpay", "minimumadjustment",
            "maximumadjustment", "bonustotal",
        )), Decimal("0"))
        expected = Decimal(str(total["expectedpay"]))
        if components != expected or line_totals.get(key, Decimal("0")) != expected:
            raise _snapshot_finalization_error("APPROVED_SNAPSHOT_INTEGRITY_ERROR", "snapshot lines do not reconcile with a driver total.")


def _snapshot_line_draft_line_id(line: Any) -> int | None:
    if line["sourcetype"] != "DraftLine" or line["sourceid"] is None:
        return None
    try:
        return int(str(line["sourceid"]))
    except ValueError:
        return None


def _snapshot_line_provenance(packet: dict[str, Any], line: Any) -> str:
    snapshot = packet["snapshot"]
    return json.dumps({
        "payroll_calculation_snapshot_id": int(snapshot["payrollcalculationsnapshotid"]),
        "revision_number": int(snapshot["revisionnumber"]),
        "snapshot_hash": snapshot["snapshothash"],
        "snapshot_line_id": int(line["payrollcalculationsnapshotlineid"]),
        "source_type": line["sourcetype"], "source_id": line["sourceid"],
        "source_evidence": line["sourceevidencejsonb"] or {},
    }, default=str)


async def _project_approved_snapshot_final_lines(
    *, packet: dict[str, Any], period_id: int, company_id: int, branch_id: int,
    user_id: int, db: AsyncConnection,
) -> None:
    await db.execute(text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', true)"))
    for line in packet["lines"]:
        evidence = line["sourceevidencejsonb"] or {}
        source_type = str(line["sourcetype"])
        rate_behavior = evidence.get("RateBehavior")
        if source_type == "System":
            rate_behavior = "System"
        elif source_type == "BonusEvent":
            rate_behavior = "Fixed"
        await db.execute(text("""
            INSERT INTO payroll.payrollfinallines
                (companyid, branchid, payrollperiodid, draftlineid, bonuseventid,
                 driverid, workdate, linetype, linescope, quantity, rateamount,
                 finalamount, sourcetype, sourceid, approvedbyuserid, approvedatutc,
                 lockedatutc, notes, payitemid, ratebehavior, ratetypeid,
                 driverrateid, resolvedrateamount, sourcesnapshot)
            VALUES
                (:cid, :bid, :period_id, :draft_line_id, :bonus_event_id,
                 :driver_id, :work_date, :line_type, :line_scope, :quantity,
                 :rate_amount, :final_amount, :source_type, :source_id,
                 :approved_by, NOW(), NOW(), :notes, :pay_item_id,
                 :rate_behavior, :rate_type_id, :driver_rate_id,
                 :resolved_rate_amount, CAST(:source_snapshot AS jsonb))
        """), {
            "cid": company_id, "bid": branch_id, "period_id": period_id,
            "draft_line_id": _snapshot_line_draft_line_id(line),
            "bonus_event_id": line["bonuseventid"], "driver_id": line["driverid"],
            "work_date": line["workdate"], "line_type": line["linetype"],
            "line_scope": line["linescope"] or "Period", "quantity": line["quantity"] or Decimal("0"),
            "rate_amount": line["resolvedrateamount"], "final_amount": line["calculatedamount"],
            "source_type": source_type, "source_id": line["sourceid"], "approved_by": user_id,
            "notes": evidence.get("Notes"), "pay_item_id": line["payitemid"],
            "rate_behavior": rate_behavior, "rate_type_id": line["ratetypeid"],
            "driver_rate_id": line["driverrateid"], "resolved_rate_amount": line["resolvedrateamount"],
            "source_snapshot": _snapshot_line_provenance(packet, line),
        })


async def finalize_period(period_id: int, company_id: int, user_id: int, db: AsyncConnection) -> PeriodSummary:
    """Project the exact approved immutable packet into FinalLines and lock the period."""
    if await _get_oda_own_driver_id(company_id, user_id, db) is not None:
        raise HTTPException(status_code=403, detail="Current Payroll is not accessible to driver-role users.")
    period = await get_period_by_id(company_id, user_id, period_id, db)
    if period.status != "Approved":
        raise HTTPException(status_code=422, detail=f"Only Approved periods can be finalized (current status: '{period.status}').")
    await _check_permission(company_id, user_id, period.branch_id, "payroll.finalize", db)
    await _acquire_branch_workflow_lock(company_id, period.branch_id, db)
    locked = (await db.execute(text("""
        SELECT payrollperiodid FROM payroll.payrollperiods
        WHERE payrollperiodid = :period_id AND companyid = :cid AND branchid = :bid AND status = 'Approved'
        FOR UPDATE
    """), {"period_id": period_id, "cid": company_id, "bid": period.branch_id})).scalar_one_or_none()
    if locked is None:
        raise HTTPException(status_code=422, detail="Period could not be claimed for finalization — its status may have changed concurrently.")
    packet = await _load_approved_snapshot_packet(period_id=period_id, company_id=company_id, branch_id=period.branch_id, db=db, lock_review_item=True)
    _reconcile_approved_snapshot_packet(packet)
    claimed = await db.execute(text("""
        UPDATE payroll.payrollperiods SET status = 'Locked', lockedbyuserid = :locker, lockedatutc = NOW()
        WHERE payrollperiodid = :period_id AND companyid = :cid AND status = 'Approved'
        RETURNING payrollperiodid
    """), {"locker": user_id, "period_id": period_id, "cid": company_id})
    if claimed.scalar_one_or_none() is None:
        raise HTTPException(status_code=422, detail="Period could not be claimed for finalization — its status may have changed concurrently.")
    await _project_approved_snapshot_final_lines(packet=packet, period_id=period_id, company_id=company_id, branch_id=period.branch_id, user_id=user_id, db=db)
    snapshot = packet["snapshot"]
    await capture_workflow_action_evidence(
        company_id=company_id,
        branch_id=period.branch_id,
        period_id=period_id,
        snapshot_id=int(snapshot["payrollcalculationsnapshotid"]),
        review_item_id=int(packet["review"]["reviewitemid"]),
        action_code="FINALIZED",
        user_id=user_id,
        required_permission_code="payroll.finalize",
        db=db,
    )
    await _write_finalization_audit(
        db, company_id=company_id, branch_id=period.branch_id, user_id=user_id,
        period_id=period_id, line_count=len(packet["lines"]),
        total_amount=Decimal(str(snapshot["totalexpectedpay"])),
        approved_review_item_id=int(packet["review"]["reviewitemid"]),
        snapshot_id=int(snapshot["payrollcalculationsnapshotid"]),
        revision_number=int(snapshot["revisionnumber"]), snapshot_hash=str(snapshot["snapshothash"]),
    )
    return await get_period_by_id(company_id, user_id, period_id, db)


async def get_finalization_preview(period_id: int, company_id: int, user_id: int, db: AsyncConnection) -> "FinalizationPreviewResponse":
    """Read the same immutable approved packet that finalization will project."""
    from app.payroll.schemas import BonusEventPreviewEntry, FinalizationPreviewDriverTotal, FinalizationPreviewLine, FinalizationPreviewResponse, FinalizationPreviewSysAdjustment
    if await _get_oda_own_driver_id(company_id, user_id, db) is not None:
        raise HTTPException(status_code=403, detail="Current Payroll is not accessible to driver-role users.")
    period = await get_period_by_id(company_id, user_id, period_id, db)
    if period.status != "Approved":
        raise HTTPException(status_code=422, detail=f"Finalization preview requires an Approved period. Current status: '{period.status}'.")
    await _check_permission(company_id, user_id, period.branch_id, "payroll.finalize", db)
    packet = await _load_approved_snapshot_packet(period_id=period_id, company_id=company_id, branch_id=period.branch_id, db=db)
    _reconcile_approved_snapshot_packet(packet)
    total_rows = {int(row["payrollcalculationdrivertotalid"]): row for row in packet["totals"]}
    lines, adjustments, bonuses = [], [], []
    for row in packet["lines"]:
        total = total_rows[int(row["payrollcalculationdrivertotalid"])]
        amount, evidence = Decimal(str(row["calculatedamount"])), row["sourceevidencejsonb"] or {}
        lines.append(FinalizationPreviewLine(
            draft_line_id=_snapshot_line_draft_line_id(row), source_key=f"snapshot-line:{row['payrollcalculationsnapshotlineid']}",
            driver_id=int(row["driverid"]), driver_name=total["drivernamesnapshot"], work_date=row["workdate"],
            line_type=row["linetype"], line_scope=row["linescope"] or "Period", quantity=row["quantity"],
            rate_amount=row["resolvedrateamount"], calculated_amount=amount, final_amount=amount,
            needs_manager_review=False, rate_behavior=evidence.get("RateBehavior"),
            driver_rate_id=row["driverrateid"], rate_type_id=row["ratetypeid"], resolved_rate_amount=row["resolvedrateamount"],
        ))
        normal_base = sum((Decimal(str(total[key])) for key in ("dailypay", "statuspay", "periodpay")), Decimal("0"))
        if row["linetype"] in {"SYS_MIN_TOPUP", "SYS_MAX_CAP"}:
            adjustments.append(FinalizationPreviewSysAdjustment(driver_id=int(row["driverid"]), driver_name=total["drivernamesnapshot"], adjustment_type=row["linetype"], gross_before=normal_base, adjustment_amount=amount, bonus_total=Decimal(str(total["bonustotal"])), final_pay=Decimal(str(total["expectedpay"]))))
        if row["sourcetype"] == "BonusEvent" and row["bonuseventid"] is not None:
            bonuses.append(BonusEventPreviewEntry(bonus_event_id=int(row["bonuseventid"]), driver_id=int(row["driverid"]), driver_name=total["drivernamesnapshot"], amount=amount, reason=evidence.get("Reason"), notes=evidence.get("Notes")))
    driver_totals = [FinalizationPreviewDriverTotal(
        driver_id=int(row["driverid"]), driver_name=row["drivernamesnapshot"],
        daily_pay=Decimal(str(row["dailypay"])), status_pay=Decimal(str(row["statuspay"])), period_pay=Decimal(str(row["periodpay"])),
        gross_pay=sum((Decimal(str(row[key])) for key in ("dailypay", "statuspay", "periodpay")), Decimal("0")),
        sys_adjustment=Decimal(str(row["minimumadjustment"])) + Decimal(str(row["maximumadjustment"])),
        bonus_total=Decimal(str(row["bonustotal"])), final_pay=Decimal(str(row["expectedpay"])),
        line_count=sum(1 for line in packet["lines"] if line["payrollcalculationdrivertotalid"] == row["payrollcalculationdrivertotalid"]),
    ) for row in packet["totals"]]
    non_bonus_non_system = [line for line in packet["lines"] if line["sourcetype"] not in {"BonusEvent", "System"}]
    return FinalizationPreviewResponse(
        period_id=period_id, period_name=period.period_name, period_status=period.status, branch_id=period.branch_id, branch_name=period.branch_name,
        can_finalize=True, blockers=[], warnings=[], driver_totals=driver_totals, sys_adjustments=adjustments, lines=lines, bonus_events=bonuses, bonus_event_count=len(bonuses),
        total_final_gross=Decimal(str(packet["snapshot"]["totalexpectedpay"])), draft_line_count=len(non_bonus_non_system), sys_adjustment_count=len(adjustments), final_line_count_estimate=len(packet["lines"]), driver_count=len(driver_totals),
    )


# ---------------------------------------------------------------------------
# CP-4B — Open/Returned live read-only calculation preview
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CalculationPacketLine:
    """Persistence-grade result from the live CP-4B calculation assembly."""

    source_type: str
    source_id: str | None
    line_type: str
    line_scope: str | None
    work_date: date | None
    driver_id: int
    quantity: Decimal | None
    resolved_rate_amount: Decimal | None
    calculated_amount: Decimal | None
    needs_manager_review: bool
    blocker_reason: str | None
    pay_item_id: int | None = None
    rate_column_id: int | None = None
    rate_type_id: int | None = None
    driver_rate_id: int | None = None
    bonus_event_id: int | None = None
    source_evidence: dict[str, Any] | None = None
    snapshot_source_type: str | None = None
    snapshot_source_id: str | None = None
    snapshot_calculated_amount: Decimal | None = None


@dataclass(frozen=True)
class _CalculationPacketDriverTotal:
    driver_id: int
    driver_code: str | None
    driver_name: str | None
    daily_pay: Decimal
    status_pay: Decimal
    period_pay: Decimal
    minimum_adjustment: Decimal
    maximum_adjustment: Decimal
    bonus_total: Decimal
    expected_pay: Decimal
    needs_manager_review: bool
    blockers: list[str]
    lines: list[_CalculationPacketLine]


@dataclass(frozen=True)
class _LiveCalculationPacket:
    payroll_period_id: int
    company_id: int
    branch_id: int
    status: str
    blockers: list[str]
    warnings: list[str]
    drivers: list[_CalculationPacketDriverTotal]
    total_expected_pay: Decimal


def _is_retryable_transaction_failure(exc: DBAPIError) -> bool:
    """PostgreSQL transaction failures which are safe for the client to retry."""
    return getattr(exc.orig, "sqlstate", None) in {"40001", "40P01"}


async def _set_submit_transaction_isolation(db: AsyncConnection) -> None:
    """Set the submit/resubmit request transaction before any database read."""
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))



async def _load_active_bonus_events(
    period_id: int,
    company_id: int,
    db: AsyncConnection,
) -> list[Any]:
    """Return the canonical Active BonusEvent selection used by CP-4B/CP-4D."""
    result = await db.execute(
        text("""
            SELECT
                be.payrollbonuseventid,
                be.driverid,
                d.drivercode,
                e.fullname AS drivername,
                be.amount,
                be.reason,
                be.notes,
                be.datarevision,
                be.createdbyuserid,
                creator.displayname AS creatordisplaynamesnapshot,
                be.createdatutc
            FROM payroll.payrollbonusevents be
            LEFT JOIN core.drivers d ON d.driverid = be.driverid
            LEFT JOIN core.employees e ON e.employeeid = d.employeeid
            LEFT JOIN sec.users creator ON creator.userid = be.createdbyuserid
            WHERE be.payrollperiodid = :period_id
              AND be.companyid = :company_id
              AND be.status = 'Active'
            ORDER BY be.driverid, be.payrollbonuseventid
        """),
        {"period_id": period_id, "company_id": company_id},
    )
    return list(result.mappings().all())


async def _load_report_evidence(
    *,
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read CP-5C report evidence from the same CP-4D transaction view."""
    status_result = await db.execute(
        text("""
            SELECT
                ppdes.payrollperioddriverdayentrystateid,
                ppdes.driverid,
                ppdes.workdate,
                ppdes.statuskeyid,
                sk.statuscode,
                sk.keyname,
                sk.isoffreason
            FROM payroll.payrollperioddriverdayentrystate ppdes
            JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = ppdes.statuskeyid
            WHERE ppdes.payrollperiodid = :pid
              AND ppdes.companyid = :cid
              AND ppdes.branchid = :bid
              AND ppdes.isvoided = FALSE
              AND ppdes.statuskeyid IS NOT NULL
              AND ppdes.workdate BETWEEN :period_start AND :period_end
            ORDER BY ppdes.driverid, ppdes.workdate,
                     ppdes.payrollperioddriverdayentrystateid
        """),
        {
            "pid": period.payroll_period_id,
            "cid": company_id,
            "bid": period.branch_id,
            "period_start": period.start_date,
            "period_end": period.end_date,
        },
    )
    status_entries = [
        {
            "PayrollPeriodDriverDayEntryStateID": int(row["payrollperioddriverdayentrystateid"]),
            "DriverID": int(row["driverid"]),
            "WorkDate": row["workdate"],
            "StatusKeyID": int(row["statuskeyid"]),
            "StatusCodeSnapshot": row["statuscode"],
            "StatusLabelSnapshot": row["keyname"],
            "StatusIsOffReasonSnapshot": bool(row["isoffreason"]),
        }
        for row in status_result.mappings().all()
    ]
    bonus_events = [
        {
            "PayrollBonusEventID": int(row["payrollbonuseventid"]),
            "DriverID": int(row["driverid"]),
            "Amount": Decimal(str(row["amount"])),
            "Reason": row["reason"],
            "Notes": row["notes"],
            "DataRevision": int(row["datarevision"]),
            "CreatedByUserID": (
                int(row["createdbyuserid"])
                if row["createdbyuserid"] is not None
                else None
            ),
            "CreatorDisplayNameSnapshot": row["creatordisplaynamesnapshot"],
            "CreatedAtUtc": row["createdatutc"],
        }
        for row in await _load_active_bonus_events(period.payroll_period_id, company_id, db)
    ]
    return status_entries, bonus_events


async def _validate_legacy_status_canonicalized(
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> list[str]:
    """
    CP-4D completeness guard: reject Submit/Resubmit if a legacy DailyStatus
    DraftLine exists for a driver/day with no corresponding live-selected
    canonical PayrollPeriodDriverDayEntryState row.

    `_load_report_evidence` (the CP-4D immutable Status-evidence reader) reads
    only canonical entry-state rows with a non-voided StatusKeyID -- it never
    falls back to DraftLines. Without this guard, such a day would silently
    capture zero Status evidence while the snapshot still reports a versioned,
    "complete" ReportEvidenceVersion.

    A canonical row that exists but only carries NoteText (StatusKeyID NULL --
    e.g. a note-only save on an old period whose legacy Status code was never
    re-entered) does not satisfy this check; the legacy Status is still
    unrepresented. DailyNote-only legacy lines are out of scope: NoteText is
    never part of the immutable Status evidence contract (`_load_report_evidence`
    requires `StatusKeyID IS NOT NULL`), so a missing canonical row can never
    cause a note to disappear from that evidence.

    Read-only. Returns a blocker list (empty = no gap found).
    """
    rows = (await db.execute(
        text("""
            SELECT DISTINCT ds.driverid, ds.workdate
            FROM   payroll.payrolldraftlines ds
            WHERE  ds.payrollperiodid = :pid
              AND  ds.companyid       = :cid
              AND  ds.linetype        = 'DailyStatus'
              AND  ds.status         != 'Void'
              AND  ds.workdate BETWEEN :period_start AND :period_end
              AND  NOT EXISTS (
                       SELECT 1
                       FROM   payroll.payrollperioddriverdayentrystate e
                       WHERE  e.payrollperiodid = ds.payrollperiodid
                         AND  e.companyid       = ds.companyid
                         AND  e.driverid        = ds.driverid
                         AND  e.workdate        = ds.workdate
                         AND  e.statuskeyid    IS NOT NULL
                         AND  e.isvoided        = FALSE
                   )
            ORDER BY ds.driverid, ds.workdate
            LIMIT 5
        """),
        {
            "pid": period.payroll_period_id,
            "cid": company_id,
            "period_start": period.start_date,
            "period_end": period.end_date,
        },
    )).mappings().all()
    if not rows:
        return []

    examples = "; ".join(f"driver {r['driverid']} on {r['workdate']}" for r in rows)
    return [
        "LEGACY_STATUS_NOT_CANONICAL: one or more days have a Status set only "
        "through the legacy Daily Status representation, with no matching "
        f"entry in the current Day Grid entry state ({examples}). Open the "
        "Day Grid for the affected day(s), re-select the Status, and save "
        "before this period can be submitted."
    ]


async def _build_live_calculation_packet(
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> _LiveCalculationPacket:
    """
    Read-only, live provisional expected-income breakdown for an Open or
    Returned period, calculated from CURRENT effective source/config data.

    Distinct from `get_finalization_preview` above (Approved-only, mirrors
    exactly what `finalize_period` will write): this is a live preview for
    the two lifecycle statuses that still allow correction/entry. It is
    never a submitted snapshot -- InReview/Approved/Locked/Archived/
    Cancelled all remain out of scope (see the CP-4C+ future snapshot
    contract for those).

    Absolute read-only guarantee: no INSERT/UPDATE/DELETE, no audit write,
    no period/source/derived-state mutation of any kind. Does not call
    `_refresh_draft_calculations`, `_sync_status_payment_for_entry_state`,
    `_refresh_status_payment_lines`, or `finalize_period`.

    Reuses, unchanged:
      - `_compute_draft_line_preview_amounts` -> `_compute_calculated_amount`
        -> CP-4A's `calculate_per_unit` for daily PerUnit lines (and the
        existing EnteredAmount/Fixed/None/manual dispatch for the rest);
      - the canonical PayrollBonusEvents Active-only read;
      - the CP-3C minimum/maximum-then-bonus ordering.

    Adds, new to CP-4B:
      - `_resolve_live_status_payment_lines`, which reads the canonical
        `PayrollPeriodDriverDayEntryState.StatusKeyID` selection directly
        and resolves the CURRENT applicable DriverRate live -- the stored
        STATUS_PAYMENT/STATUS_PAY compatibility-projection DraftLine is
        excluded from the stored-line aggregation below and is never used
        as live truth, so a stale projection can never be double-counted.

    Driver inclusion is financial-source-driven only (a driver with a
    current daily line, a canonical selected Status, a non-BONUS period-pay
    line, or an Active bonus event) -- not a full eligible-driver roster.
    """
    period_id = period.payroll_period_id

    blockers: list[str] = []
    warnings: list[str] = []

    # ── CP-4B fix (Codex P1): shared structural blockers (duplicate active
    # Daily lines, driver eligibility violations, contaminated/foreign
    # RateType references, unresolvable rate mapping) — the SAME read-only
    # checks enforced by finalize_period / get_finalization_preview. These
    # checks are structural, not Approved-specific (none of them reference
    # period.status), so they apply directly and unmodified to Open/Returned
    # periods. Surfacing them here prevents a preview from looking
    # financially complete (has_blockers=false) while a structural condition
    # that would block finalize_period is silently present.
    blockers.extend(await _validate_period_can_finalize(
        period_id=period_id,
        company_id=company_id,
        branch_id=period.branch_id,
        period_start=period.start_date,
        period_end=period.end_date,
        db=db,
    ))

    # ── CP-4D completeness guard: a legacy DailyStatus DraftLine with no
    # canonical entry-state row would silently vanish from the immutable
    # Status evidence captured at Submit/Resubmit (_load_report_evidence
    # reads canonical rows only). Block until the day is re-saved through
    # the Day Grid so the Status is canonically represented.
    blockers.extend(await _validate_legacy_status_canonicalized(
        period=period,
        company_id=company_id,
        db=db,
    ))

    # ── Daily/period-pay lines: virtual (unpersisted) rate refresh, exactly
    # like get_finalization_preview — but excluding the persisted
    # STATUS_PAYMENT/STATUS_PAY compatibility projection and legacy BONUS
    # lines, since Status and Bonus are supplied live/canonically below.
    refreshed_calcs = await _compute_draft_line_preview_amounts(
        period_id, company_id, period.start_date, db
    )

    lines_result = await db.execute(
        text(f"""
            SELECT
                dl.draftlineid,
                dl.driverid,
                d.drivercode,
                pi.payitemid,
                e.fullname          AS drivername,
                dl.workdate,
                dl.linetype,
                dl.linescope,
                dl.quantity,
                dl.rateamount,
                dl.calculatedamount,
                dl.needsmanagerreview,
                dl.sourcetype,
                dl.sourceid
            FROM   payroll.payrolldraftlines dl
            LEFT JOIN core.drivers   d ON d.driverid   = dl.driverid
            LEFT JOIN core.employees e ON e.employeeid = d.employeeid
            LEFT JOIN LATERAL (
                SELECT pi.payitemid
                FROM payroll.payitems pi
                WHERE pi.payitemcode = dl.linetype
                  AND (pi.companyid IS NULL OR pi.companyid = dl.companyid)
                ORDER BY CASE WHEN pi.companyid = dl.companyid THEN 0 ELSE 1 END
                LIMIT 1
            ) pi ON TRUE
            WHERE  dl.payrollperiodid = :period_id
              AND  dl.companyid       = :company_id
              AND  dl.status         != 'Void'
              AND  dl.linetype       != 'BONUS'
              AND  dl.linetype       NOT IN ('DailyStatus', 'DailyNote')
              AND  NOT {_STATUS_PAYMENT_PROJECTION_SQL}
            ORDER BY dl.driverid, dl.workdate NULLS LAST, dl.draftlineid
        """),
        {"period_id": period_id, "company_id": company_id},
    )
    raw_lines = lines_result.mappings().fetchall()

    driver_names: dict[int, str | None] = {}
    driver_codes: dict[int, str | None] = {}
    driver_daily: dict[int, Decimal] = {}
    driver_period: dict[int, Decimal] = {}
    driver_status: dict[int, Decimal] = {}
    driver_bonus: dict[int, Decimal] = {}
    driver_line_nmr: dict[int, bool] = {}
    driver_lines: dict[int, list[_CalculationPacketLine]] = {}

    stale_count = 0
    for r in raw_lines:
        lid = int(r["draftlineid"])
        drv = int(r["driverid"])
        driver_names.setdefault(drv, r["drivername"])
        driver_codes.setdefault(drv, r["drivercode"])
        driver_lines.setdefault(drv, [])

        stored_calc = Decimal(str(r["calculatedamount"])) if r["calculatedamount"] is not None else None
        qty = Decimal(str(r["quantity"])) if r["quantity"] is not None else Decimal("0")
        rate = Decimal(str(r["rateamount"])) if r["rateamount"] is not None else None

        if lid in refreshed_calcs:
            _cr = refreshed_calcs[lid]
            effective_calc = _cr.calculated_amount
            effective_nmr = _cr.needs_manager_review
            resolved_rate = _cr.resolved_rate_amount
            if effective_calc != stored_calc:
                stale_count += 1
        else:
            effective_calc = stored_calc
            effective_nmr = bool(r["needsmanagerreview"])
            resolved_rate = rate

        if effective_nmr:
            driver_line_nmr[drv] = True

        amt = effective_calc if effective_calc is not None else qty * (rate if rate is not None else Decimal("0"))

        if r["linescope"] == "Daily":
            driver_daily[drv] = driver_daily.get(drv, Decimal("0")) + amt
        else:
            driver_period[drv] = driver_period.get(drv, Decimal("0")) + amt

        driver_lines[drv].append(_CalculationPacketLine(
            source_type=r["sourcetype"] or "DraftLine",
            source_id=r["sourceid"],
            line_type=r["linetype"],
            line_scope=r["linescope"],
            work_date=r["workdate"],
            driver_id=drv,
            quantity=qty,
            resolved_rate_amount=resolved_rate,
            calculated_amount=effective_calc,
            needs_manager_review=effective_nmr,
            blocker_reason=(
                "Calculated amount unresolved or manually flagged for manager review."
                if effective_nmr else None
            ),
            rate_type_id=(
                _cr.rate_type_id if lid in refreshed_calcs else None
            ),
            pay_item_id=(int(r["payitemid"]) if r["payitemid"] is not None else None),
            driver_rate_id=(
                _cr.driver_rate_id if lid in refreshed_calcs else None
            ),
            source_evidence={
                "DraftLineID": lid,
                "StoredSourceType": r["sourcetype"],
                "StoredSourceID": r["sourceid"],
                "StoredCalculatedAmount": stored_calc,
                "StoredRateAmount": rate,
                "RateBehavior": (
                    _cr.rate_behavior if lid in refreshed_calcs else "Stored"
                ),
                "PerUnitCalculationVersion": (
                    PER_UNIT_CALCULATION_VERSION
                    if lid in refreshed_calcs and _cr.rate_behavior == "PerUnit"
                    else None
                ),
            },
            snapshot_source_type="DraftLine",
            snapshot_source_id=str(lid),
            # Preserve CP-4B's historical public NULL CalculatedAmount while
            # freezing the actual fallback amount used in the packet total.
            snapshot_calculated_amount=amt,
        ))

    if stale_count > 0:
        warnings.append(
            f"{stale_count} line(s) had stale stored calculations. "
            f"Preview amounts reflect the latest effective-dated rates."
        )

    # ── Canonical live Status-derived pay (CP-4B) — never the stored
    # STATUS_PAYMENT/STATUS_PAY projection, which was already excluded above.
    live_status_lines = await _resolve_live_status_payment_lines(
        period_id, company_id, period.branch_id, db,
    )
    for sl in live_status_lines:
        drv = sl.driver_id
        driver_names.setdefault(drv, None)
        driver_lines.setdefault(drv, [])

        amt = sl.calculated_amount if sl.calculated_amount is not None else Decimal("0")
        driver_status[drv] = driver_status.get(drv, Decimal("0")) + amt

        if sl.needs_manager_review:
            driver_line_nmr[drv] = True

        driver_lines[drv].append(_CalculationPacketLine(
            source_type="StatusEntryState",
            source_id=f"STATUS_LIVE:{drv}:{sl.work_date}:{sl.status_key_id}",
            line_type=sl.line_type,
            line_scope="Daily",
            work_date=sl.work_date,
            driver_id=drv,
            quantity=sl.hours_value,
            resolved_rate_amount=sl.resolved_rate_amount,
            calculated_amount=sl.calculated_amount,
            needs_manager_review=sl.needs_manager_review,
            blocker_reason=(
                "No applicable approved DriverRate found for this driver's "
                "selected Status as of its work date."
                if sl.needs_manager_review else None
            ),
            rate_type_id=sl.rate_type_id,
            rate_column_id=sl.status_rate_column_id,
            driver_rate_id=sl.driver_rate_id,
            source_evidence={
                "PayrollPeriodDriverDayEntryStateID": sl.entry_state_id,
                "StatusKeyID": sl.status_key_id,
                "StatusCode": sl.status_code,
                "StatusRateColumnID": sl.status_rate_column_id,
                "HoursValue": sl.hours_value,
                "WorkDate": sl.work_date,
                "RateTypeID": sl.rate_type_id,
                "DriverRateID": sl.driver_rate_id,
            },
            snapshot_source_type="StatusEntryState",
            snapshot_source_id=str(sl.entry_state_id),
        ))

    # ── Canonical Active bonus (never Voided; never legacy BONUS DraftLines).
    for b in await _load_active_bonus_events(period_id, company_id, db):
        drv = int(b["driverid"])
        driver_names.setdefault(drv, b["drivername"])
        driver_codes.setdefault(drv, b["drivercode"])
        driver_lines.setdefault(drv, [])
        amt = Decimal(str(b["amount"]))
        driver_bonus[drv] = driver_bonus.get(drv, Decimal("0")) + amt
        driver_lines[drv].append(_CalculationPacketLine(
            source_type="BonusEvent",
            source_id=str(b["payrollbonuseventid"]),
            line_type="BONUS",
            line_scope="Period",
            work_date=None,
            driver_id=drv,
            quantity=None,
            resolved_rate_amount=None,
            calculated_amount=amt,
            needs_manager_review=False,
            blocker_reason=None,
            bonus_event_id=int(b["payrollbonuseventid"]),
            source_evidence={
                "PayrollBonusEventID": int(b["payrollbonuseventid"]),
                "Amount": amt,
                "Reason": b["reason"],
                "Notes": b["notes"],
                "DataRevision": b["datarevision"],
                "Status": "Active",
            },
        ))

    # ── Financial-source-driven driver union (CP-4B: not a full roster).
    all_driver_ids = (
        set(driver_daily) | set(driver_period) | set(driver_status) | set(driver_bonus)
    )

    if not all_driver_ids:
        warnings.append("No current financial source lines for this period.")
    else:
        driver_identity_result = await db.execute(
            text("""
                SELECT d.driverid, d.drivercode, e.fullname
                FROM core.drivers d
                JOIN core.employees e ON e.employeeid = d.employeeid
                WHERE d.companyid = :cid
                  AND d.driverid = ANY(:driver_ids)
            """),
            {"cid": company_id, "driver_ids": sorted(all_driver_ids)},
        )
        for identity in driver_identity_result.mappings().all():
            driver_names.setdefault(int(identity["driverid"]), identity["fullname"])
            driver_codes.setdefault(int(identity["driverid"]), identity["drivercode"])

    # ── Minimum/maximum: same as-of-period-start rule and ordering as
    # get_finalization_preview — normal base excludes bonus by construction;
    # bonus is added back in only after minimum/maximum is applied (CP-3C).
    period_start = period.start_date
    driver_blockers: dict[int, list[str]] = {}
    driver_min_adj: dict[int, Decimal] = {}
    driver_max_adj: dict[int, Decimal] = {}
    driver_min_rule: dict[int, Any] = {}
    driver_max_rule: dict[int, Any] = {}

    for drv_id in all_driver_ids:
        normal_base = (
            driver_daily.get(drv_id, Decimal("0"))
            + driver_status.get(drv_id, Decimal("0"))
            + driver_period.get(drv_id, Decimal("0"))
        )

        min_row = (await db.execute(
            text("""
                SELECT driverpayruleid, amount, status, effectivefrom, effectiveto
                FROM payroll.driverpayrules
                WHERE  driverid      = :did
                  AND  companyid     = :cid
                  AND  ruletype      = 'MinimumPay'
                  AND  status        IN ('Active', 'Ended')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": drv_id, "cid": company_id, "as_of": period_start},
        )).mappings().first()
        max_row = (await db.execute(
            text("""
                SELECT driverpayruleid, amount, status, effectivefrom, effectiveto
                FROM payroll.driverpayrules
                WHERE  driverid      = :did
                  AND  companyid     = :cid
                  AND  ruletype      = 'MaximumPay'
                  AND  status        IN ('Active', 'Ended')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": drv_id, "cid": company_id, "as_of": period_start},
        )).mappings().first()

        min_amount = Decimal(str(min_row["amount"])) if min_row else None
        max_amount = Decimal(str(max_row["amount"])) if max_row else None
        if min_row is not None:
            driver_min_rule[drv_id] = min_row
        if max_row is not None:
            driver_max_rule[drv_id] = max_row

        if min_amount is not None and max_amount is not None and min_amount > max_amount:
            driver_blockers.setdefault(drv_id, []).append(
                f"Minimum pay ({min_amount}) exceeds maximum pay ({max_amount}). "
                f"Correct the pay rules before this driver's total can be trusted."
            )
            driver_min_adj[drv_id] = Decimal("0")
            driver_max_adj[drv_id] = Decimal("0")
            continue

        if min_amount is not None and normal_base < min_amount:
            driver_min_adj[drv_id] = min_amount - normal_base
        else:
            driver_min_adj[drv_id] = Decimal("0")

        if max_amount is not None and normal_base > max_amount:
            driver_max_adj[drv_id] = max_amount - normal_base
        else:
            driver_max_adj[drv_id] = Decimal("0")

    # ── Assemble driver totals.
    driver_totals: list[_CalculationPacketDriverTotal] = []
    for drv_id in sorted(all_driver_ids):
        daily = driver_daily.get(drv_id, Decimal("0"))
        status_pay = driver_status.get(drv_id, Decimal("0"))
        period_pay = driver_period.get(drv_id, Decimal("0"))
        normal_base = daily + status_pay + period_pay
        min_adj = driver_min_adj.get(drv_id, Decimal("0"))
        max_adj = driver_max_adj.get(drv_id, Decimal("0"))
        bonus = driver_bonus.get(drv_id, Decimal("0"))
        expected_pay = normal_base + min_adj + max_adj + bonus
        drv_blockers = driver_blockers.get(drv_id, [])
        drv_nmr = driver_line_nmr.get(drv_id, False)

        if min_adj != 0:
            min_rule = driver_min_rule[drv_id]
            driver_lines[drv_id].append(_CalculationPacketLine(
                source_type="System",
                source_id=str(min_rule["driverpayruleid"]),
                line_type="SYS_MIN_TOPUP",
                line_scope="Period",
                work_date=None,
                driver_id=drv_id,
                quantity=Decimal("1"),
                resolved_rate_amount=None,
                calculated_amount=min_adj,
                needs_manager_review=False,
                blocker_reason=None,
                source_evidence={
                    "DriverPayRuleID": int(min_rule["driverpayruleid"]),
                    "RuleType": "MinimumPay",
                    "RuleAmount": Decimal(str(min_rule["amount"])),
                    "RuleStatus": min_rule["status"],
                    "EffectiveFrom": min_rule["effectivefrom"],
                    "EffectiveTo": min_rule["effectiveto"],
                    "NormalBase": normal_base,
                },
            ))
        if max_adj != 0:
            max_rule = driver_max_rule[drv_id]
            driver_lines[drv_id].append(_CalculationPacketLine(
                source_type="System",
                source_id=str(max_rule["driverpayruleid"]),
                line_type="SYS_MAX_CAP",
                line_scope="Period",
                work_date=None,
                driver_id=drv_id,
                quantity=Decimal("1"),
                resolved_rate_amount=None,
                calculated_amount=max_adj,
                needs_manager_review=False,
                blocker_reason=None,
                source_evidence={
                    "DriverPayRuleID": int(max_rule["driverpayruleid"]),
                    "RuleType": "MaximumPay",
                    "RuleAmount": Decimal(str(max_rule["amount"])),
                    "RuleStatus": max_rule["status"],
                    "EffectiveFrom": max_rule["effectivefrom"],
                    "EffectiveTo": max_rule["effectiveto"],
                    "NormalBase": normal_base,
                },
            ))

        driver_totals.append(_CalculationPacketDriverTotal(
            driver_id=drv_id,
            driver_code=driver_codes.get(drv_id),
            driver_name=driver_names.get(drv_id),
            daily_pay=daily,
            status_pay=status_pay,
            period_pay=period_pay,
            minimum_adjustment=min_adj,
            maximum_adjustment=max_adj,
            bonus_total=bonus,
            expected_pay=expected_pay,
            needs_manager_review=drv_nmr,
            blockers=drv_blockers,
            lines=driver_lines.get(drv_id, []),
        ))
        if drv_blockers:
            blockers.extend(f"Driver {drv_id}: {b}" for b in drv_blockers)
        if drv_nmr:
            blockers.append(
                f"Driver {drv_id}: one or more lines require manager review "
                f"(calculation unresolved or manually flagged)."
            )

    total_expected_pay = sum((dt.expected_pay for dt in driver_totals), Decimal("0"))

    return _LiveCalculationPacket(
        payroll_period_id=period_id,
        company_id=company_id,
        branch_id=period.branch_id,
        status=period.status,
        blockers=blockers,
        warnings=warnings,
        drivers=driver_totals,
        total_expected_pay=total_expected_pay,
    )


async def get_calculation_preview(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> "CalculationPreviewResponse":
    """Adapt the shared live packet to CP-4B's unchanged public contract."""
    from app.payroll.schemas import (
        CalculationPreviewResponse,
        CalculationPreviewDriverTotal,
        CalculationPreviewLine,
    )

    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Current Payroll is not accessible to driver-role users.",
        )
    period = await get_period_by_id(company_id, user_id, period_id, db)
    if period.status not in ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                "Calculation preview requires an Open or Returned period. "
                f"Current status: '{period.status}'."
            ),
        )
    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db,
    )
    packet = await _build_live_calculation_packet(period, company_id, db)
    return CalculationPreviewResponse(
        payroll_period_id=packet.payroll_period_id,
        company_id=packet.company_id,
        branch_id=packet.branch_id,
        branch_name=period.branch_name,
        status=packet.status,
        provisional=True,
        financials_available=True,
        has_blockers=bool(packet.blockers),
        blockers=packet.blockers,
        warnings=packet.warnings,
        drivers=[
            CalculationPreviewDriverTotal(
                driver_id=driver.driver_id,
                driver_name=driver.driver_name,
                daily_pay=driver.daily_pay,
                status_pay=driver.status_pay,
                period_pay=driver.period_pay,
                normal_base=driver.daily_pay + driver.status_pay + driver.period_pay,
                minimum_adjustment=driver.minimum_adjustment,
                maximum_adjustment=driver.maximum_adjustment,
                bonus_total=driver.bonus_total,
                expected_pay=driver.expected_pay,
                needs_manager_review=driver.needs_manager_review,
                blockers=driver.blockers,
                lines=[
                    CalculationPreviewLine(
                        source_type=line.source_type,
                        source_id=line.source_id,
                        line_type=line.line_type,
                        work_date=line.work_date,
                        pay_item_id=line.pay_item_id,
                        rate_column_id=line.rate_column_id,
                        driver_id=line.driver_id,
                        quantity=line.quantity,
                        resolved_rate=line.resolved_rate_amount,
                        calculated_amount=line.calculated_amount,
                        needs_manager_review=line.needs_manager_review,
                        blocker_reason=line.blocker_reason,
                    )
                    for line in driver.lines
                ],
            )
            for driver in packet.drivers
        ],
        total_expected_pay=packet.total_expected_pay,
    )


def _packet_driver_totals_for_hash(
    packet: _LiveCalculationPacket,
) -> list[dict[str, Any]]:
    """Project the shared live packet into CP-4C's hash contract."""
    return [
        {
            "DriverID": driver.driver_id,
            "DriverCodeSnapshot": driver.driver_code,
            "DriverNameSnapshot": driver.driver_name,
            "DailyPay": driver.daily_pay,
            "StatusPay": driver.status_pay,
            "PeriodPay": driver.period_pay,
            "MinimumAdjustment": driver.minimum_adjustment,
            "MaximumAdjustment": driver.maximum_adjustment,
            "BonusTotal": driver.bonus_total,
            "ExpectedPay": driver.expected_pay,
            "Lines": [
                {
                    "SourceType": line.snapshot_source_type or line.source_type,
                    "SourceID": line.snapshot_source_id if line.snapshot_source_id is not None else line.source_id,
                    "LineType": line.line_type,
                    "LineScope": line.line_scope,
                    "WorkDate": line.work_date,
                    "PayItemID": line.pay_item_id,
                    "RateTypeID": line.rate_type_id,
                    "DriverRateID": line.driver_rate_id,
                    "BonusEventID": line.bonus_event_id,
                    "Quantity": line.quantity,
                    "ResolvedRateAmount": line.resolved_rate_amount,
                    "CalculatedAmount": (
                        line.snapshot_calculated_amount
                        if line.snapshot_calculated_amount is not None
                        else line.calculated_amount
                    ),
                    "SourceEvidenceJSONB": line.source_evidence or {},
                }
                for line in driver.lines
            ],
        }
        for driver in packet.drivers
    ]


async def _capture_calculation_snapshot(
    *,
    period: PeriodSummary,
    company_id: int,
    user_id: int,
    packet: _LiveCalculationPacket,
    db: AsyncConnection,
    context: str,
) -> int:
    """Persist one complete immutable CP-4D submission packet.

    The caller already owns the period/workflow locks.  This writer performs no
    calculation and never uses generated IDs in either hash.
    """
    if packet.blockers:
        raise HTTPException(
            status_code=422,
            detail="Cannot submit an incomplete calculation packet: " + "; ".join(packet.blockers),
        )
    if any(
        line.snapshot_calculated_amount is None and line.calculated_amount is None
        for driver in packet.drivers
        for line in driver.lines
    ):
        raise HTTPException(
            status_code=422,
            detail="Cannot submit: an authoritative calculation line is unresolved.",
        )

    status_entries, bonus_events = await _load_report_evidence(
        period=period,
        company_id=company_id,
        db=db,
    )
    snapshot_bonus_lines = sorted(
        (
            line.bonus_event_id,
            line.driver_id,
            line.snapshot_calculated_amount
            if line.snapshot_calculated_amount is not None
            else line.calculated_amount,
        )
        for driver in packet.drivers
        for line in driver.lines
        if line.source_type == "BonusEvent" and line.bonus_event_id is not None
    )
    evidence_bonus_lines = sorted(
        (event["PayrollBonusEventID"], event["DriverID"], event["Amount"])
        for event in bonus_events
    )
    if snapshot_bonus_lines != evidence_bonus_lines:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot submit: captured Bonus evidence does not reconcile "
                "with the authoritative calculation packet."
            ),
        )
    report_evidence_hash = calculate_report_evidence_hash(
        status_entries=status_entries,
        bonus_events=bonus_events,
    )

    eligibility_rows = (await db.execute(
        text("""
            SELECT driverid, iseligibleforperiod, eligibilityreasoncode,
                   hiredatesnapshot, terminationdatesnapshot,
                   drivereffectivefromsnapshot, drivereffectivetosnapshot,
                   drivercodesnapshot, drivernamesnapshot
            FROM payroll.payrollperioddrivereligibility
            WHERE payrollperiodid = :pid AND companyid = :cid AND branchid = :bid
            ORDER BY driverid
        """),
        {"pid": period.payroll_period_id, "cid": company_id, "bid": period.branch_id},
    )).mappings().all()

    source_config_payload = {
        "PacketContract": "cp4d-source-config-v1",
        "PayrollPeriod": {
            "PayrollPeriodID": period.payroll_period_id,
            "CompanyID": company_id,
            "BranchID": period.branch_id,
            "PeriodCode": period.period_code,
            "PeriodType": period.period_type,
            "StartDate": period.start_date,
            "EndDate": period.end_date,
        },
        "Eligibility": [dict(row) for row in eligibility_rows],
        "Sources": [
            {
                "DriverID": driver.driver_id,
                "Lines": [
                    {
                        "SourceType": line.snapshot_source_type or line.source_type,
                        "SourceID": line.snapshot_source_id if line.snapshot_source_id is not None else line.source_id,
                        "LineType": line.line_type,
                        "LineScope": line.line_scope,
                        "WorkDate": line.work_date,
                        "Quantity": line.quantity,
                        "ResolvedRateAmount": line.resolved_rate_amount,
                        "SourceEvidenceJSONB": line.source_evidence or {},
                    }
                    for line in driver.lines
                ],
            }
            for driver in packet.drivers
        ],
    }
    source_config_hash = calculate_source_config_hash(source_config_payload)
    revision_result = await db.execute(
        text("""
            SELECT COALESCE(MAX(revisionnumber), 0) + 1
            FROM payroll.payrollcalculationsnapshots
            WHERE payrollperiodid = :pid
        """),
        {"pid": period.payroll_period_id},
    )
    revision_number = int(revision_result.scalar_one())
    hash_totals = _packet_driver_totals_for_hash(packet)
    snapshot_hash = calculate_snapshot_hash(
        company_id=company_id,
        branch_id=period.branch_id,
        payroll_period_id=period.payroll_period_id,
        revision_number=revision_number,
        calculation_version=CURRENT_PAYROLL_CALCULATION_VERSION,
        source_config_hash=source_config_hash,
        driver_totals=hash_totals,
    )

    header_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollcalculationsnapshots
                (companyid, branchid, payrollperiodid, revisionnumber,
                 calculationversion, sourceconfighash, snapshothash,
                 reportevidenceversion, reportevidencehash,
                 createdbyuserid, totalexpectedpay)
            VALUES
                (:cid, :bid, :pid, :revision, :version, :source_hash,
                 :snapshot_hash, :report_evidence_version, :report_evidence_hash,
                 :uid, :total)
            RETURNING payrollcalculationsnapshotid
        """),
        {
            "cid": company_id,
            "bid": period.branch_id,
            "pid": period.payroll_period_id,
            "revision": revision_number,
            "version": CURRENT_PAYROLL_CALCULATION_VERSION,
            "source_hash": source_config_hash,
            "snapshot_hash": snapshot_hash,
            "report_evidence_version": CURRENT_REPORT_EVIDENCE_VERSION,
            "report_evidence_hash": report_evidence_hash,
            "uid": user_id,
            "total": packet.total_expected_pay,
        },
    )
    snapshot_id = int(header_result.scalar_one())
    snapshot_lines = [
        {**line, "DriverID": hash_total["DriverID"]}
        for hash_total in hash_totals
        for line in hash_total["Lines"]
    ]
    used_rate_definition_ids = await capture_snapshot_used_rate_definitions(
        snapshot_id=snapshot_id,
        company_id=company_id,
        branch_id=period.branch_id,
        period_id=period.payroll_period_id,
        snapshot_line_rows=snapshot_lines,
        db=db,
    )
    snapshot_line_ordinal = 0

    for driver, hash_total in zip(packet.drivers, hash_totals, strict=True):
        driver_result = await db.execute(
            text("""
                INSERT INTO payroll.payrollcalculationdrivertotals
                    (payrollcalculationsnapshotid, companyid, branchid, driverid,
                     drivercodesnapshot, drivernamesnapshot, dailypay, statuspay,
                     periodpay, minimumadjustment, maximumadjustment, bonustotal,
                     expectedpay)
                VALUES
                    (:snapshot_id, :cid, :bid, :driver_id, :driver_code, :driver_name,
                     :daily, :status, :period, :minimum, :maximum, :bonus, :expected)
                RETURNING payrollcalculationdrivertotalid
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "driver_id": driver.driver_id,
                "driver_code": driver.driver_code,
                "driver_name": driver.driver_name,
                "daily": driver.daily_pay,
                "status": driver.status_pay,
                "period": driver.period_pay,
                "minimum": driver.minimum_adjustment,
                "maximum": driver.maximum_adjustment,
                "bonus": driver.bonus_total,
                "expected": driver.expected_pay,
            },
        )
        driver_total_id = int(driver_result.scalar_one())
        for line in hash_total["Lines"]:
            await db.execute(
                text("""
                    INSERT INTO payroll.payrollcalculationsnapshotlines
                        (payrollcalculationdrivertotalid, sourcetype, sourceid,
                         linetype, linescope, workdate, payitemid, ratetypeid,
                         driverrateid, bonuseventid, quantity, resolvedrateamount,
                         calculatedamount, sourceevidencejsonb, usedratedefinitionid)
                    VALUES
                        (:driver_total_id, :source_type, :source_id, :line_type,
                         :line_scope, :work_date, :pay_item_id, :rate_type_id,
                         :driver_rate_id, :bonus_event_id, :quantity, :resolved_rate,
                         :calculated_amount, CAST(:evidence AS jsonb), :used_rate_definition_id)
                """),
                {
                    "driver_total_id": driver_total_id,
                    "source_type": line["SourceType"],
                    "source_id": line["SourceID"],
                    "line_type": line["LineType"],
                    "line_scope": line["LineScope"],
                    "work_date": line["WorkDate"],
                    "pay_item_id": line["PayItemID"],
                    "rate_type_id": line["RateTypeID"],
                    "driver_rate_id": line["DriverRateID"],
                    "bonus_event_id": line["BonusEventID"],
                    "quantity": line["Quantity"],
                    "resolved_rate": line["ResolvedRateAmount"],
                    "calculated_amount": line["CalculatedAmount"],
                    "evidence": canonical_json(line["SourceEvidenceJSONB"]),
                    "used_rate_definition_id": used_rate_definition_ids.get(snapshot_line_ordinal),
                },
            )
            snapshot_line_ordinal += 1

    for entry in status_entries:
        await db.execute(
            text("""
                INSERT INTO payroll.payrollcalculationsnapshotstatusentries
                    (payrollcalculationsnapshotid, companyid, branchid,
                     payrollperiodid, driverid, workdate,
                     payrollperioddriverdayentrystateid, statuskeyid,
                     statuscodesnapshot, statuslabelsnapshot,
                     statusisoffreasonsnapshot)
                VALUES
                    (:snapshot_id, :cid, :bid, :pid, :driver_id, :work_date,
                     :entry_state_id, :status_key_id, :status_code,
                     :status_label, :status_is_off_reason)
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "pid": period.payroll_period_id,
                "driver_id": entry["DriverID"],
                "work_date": entry["WorkDate"],
                "entry_state_id": entry["PayrollPeriodDriverDayEntryStateID"],
                "status_key_id": entry["StatusKeyID"],
                "status_code": entry["StatusCodeSnapshot"],
                "status_label": entry["StatusLabelSnapshot"],
                "status_is_off_reason": entry["StatusIsOffReasonSnapshot"],
            },
        )

    for event in bonus_events:
        await db.execute(
            text("""
                INSERT INTO payroll.payrollcalculationsnapshotbonusevents
                    (payrollcalculationsnapshotid, companyid, branchid,
                     payrollperiodid, payrollbonuseventid, driverid, amount,
                     reason, notes, datarevision, createdbyuserid,
                     creatordisplaynamesnapshot, createdatutc)
                VALUES
                    (:snapshot_id, :cid, :bid, :pid, :bonus_event_id, :driver_id,
                     :amount, :reason, :notes, :data_revision, :created_by_user_id,
                     :creator_display_name, :created_at)
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "pid": period.payroll_period_id,
                "bonus_event_id": event["PayrollBonusEventID"],
                "driver_id": event["DriverID"],
                "amount": event["Amount"],
                "reason": event["Reason"],
                "notes": event["Notes"],
                "data_revision": event["DataRevision"],
                "created_by_user_id": event["CreatedByUserID"],
                "creator_display_name": event["CreatorDisplayNameSnapshot"],
                "created_at": event["CreatedAtUtc"],
            },
        )

    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid, newvaluejson, reason, sourcetype)
            VALUES
                (:cid, :bid, :uid, 'CALCULATION_SNAPSHOT_CAPTURED',
                 'payroll', 'PayrollCalculationSnapshots', :snapshot_id, :new_value,
                 :reason, 'Application')
        """),
        {
            "cid": company_id,
            "bid": period.branch_id,
            "uid": user_id,
            "snapshot_id": str(snapshot_id),
            "new_value": json.dumps({
                "payroll_period_id": period.payroll_period_id,
                "snapshot_id": snapshot_id,
                "revision_number": revision_number,
                "source_config_hash": source_config_hash,
                "snapshot_hash": snapshot_hash,
                "report_evidence_version": CURRENT_REPORT_EVIDENCE_VERSION,
                "report_evidence_hash": report_evidence_hash,
                "context": context,
            }),
            "reason": "Immutable calculation snapshot captured for review submission",
        },
    )
    return snapshot_id


# ---------------------------------------------------------------------------
# Period-eligible drivers (P1 #1)
# ---------------------------------------------------------------------------

async def get_period_eligible_drivers(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[dict]:
    """
    Return drivers eligible for a Bonus (or other period-pay line) for the
    given period.

    Eligibility = period-scoped, NOT day-scoped:
      1. Active drivers (employmentstatus='Active' AND driverstatus='Active')
         whose hire/termination window overlaps the period dates.
      2. OR any driver who already has period-pay lines in this period —
         so existing bonuses stay voidable even if the driver was later
         terminated.

    ODA/Driver users are blocked unconditionally (same boundary as day-grid).
    payroll.view OR payroll.entry permission is required.
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Period Pay / Bonus eligible driver list is a financial path — block for Draft.
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Period eligible drivers are not available for Prepared (Draft) periods.",
        )

    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    # CP-2E: For snapshotted periods use the snapshot roster instead of live tables.
    _has_snap = await _period_has_driver_eligibility_snapshot(period_id, db)
    if _has_snap:
        # Active / TerminatedHistorical / Transferred → prospective choices
        # IncludedByExistingData → only if they already have a period-pay line
        snap_result = await db.execute(
            text("""
                SELECT ppde.driverid,
                       COALESCE(ppde.drivernamesnapshot, '') AS drivername,
                       COALESCE(ppde.drivercodesnapshot, '') AS drivercode,
                       ppde.eligibilityreasoncode
                FROM   payroll.payrollperioddrivereligibility ppde
                WHERE  ppde.payrollperiodid = :period_id
                  AND  ppde.companyid       = :cid
                  AND  ppde.branchid        = :bid
                  AND  ppde.iseligibleforperiod = TRUE
                ORDER BY ppde.drivernamesnapshot
            """),
            {"period_id": period_id, "cid": company_id, "bid": period.branch_id},
        )
        snap_rows = list(snap_result.mappings().all())

        # For IBED: check which have existing period-pay lines
        ibed_ids = [r["driverid"] for r in snap_rows if r["eligibilityreasoncode"] == "IncludedByExistingData"]
        ibed_with_period_pay: set[int] = set()
        if ibed_ids:
            in_cl, in_pr = _build_in_clause(ibed_ids, "ibed")
            ibed_res = await db.execute(
                text(f"""
                    SELECT DISTINCT driverid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :period_id AND linescope = 'Period'
                      AND status != 'Void'
                      AND driverid IN ({in_cl})
                """),
                {"period_id": period_id, **in_pr},
            )
            ibed_with_period_pay = {r["driverid"] for r in ibed_res.mappings().all()}

        out = []
        for r in snap_rows:
            if r["eligibilityreasoncode"] == "IncludedByExistingData":
                if r["driverid"] not in ibed_with_period_pay:
                    continue
            out.append({
                "driver_id":   int(r["driverid"]),
                "driver_name": r["drivername"],
                "driver_code": r["drivercode"],
            })
        return out

    result = await db.execute(
        text("""
            SELECT DISTINCT d.driverid, e.fullname AS drivername, d.drivercode
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            WHERE  d.companyid = :cid
              AND  d.branchid  = :bid
              AND  (
                    -- Active driver whose hire/termination window overlaps the period
                    (    e.employmentstatus = 'Active'
                     AND d.driverstatus     = 'Active'
                     AND (e.hiredate IS NULL OR e.hiredate <= :period_end)
                     AND (e.terminationdate IS NULL OR e.terminationdate >= :period_start)
                    )
                    OR
                    -- Driver who already has period-pay lines in this period
                    -- (keeps existing bonuses voidable even if driver was terminated)
                    EXISTS (
                        SELECT 1
                        FROM   payroll.payrolldraftlines pdl
                        WHERE  pdl.driverid        = d.driverid
                          AND  pdl.payrollperiodid = :period_id
                          AND  pdl.linescope        = 'Period'
                    )
              )
            ORDER BY e.fullname
        """),
        {
            "cid":          company_id,
            "bid":          period.branch_id,
            "period_start": period.start_date,
            "period_end":   period.end_date,
            "period_id":    period_id,
        },
    )
    rows = result.mappings().all()
    return [
        {
            "driver_id":   int(r["driverid"]),
            "driver_name": r["drivername"],
            "driver_code": r["drivercode"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# CP-2D1: canonical daily driver/day entry-state helpers
# ---------------------------------------------------------------------------

_KEEP = object()  # sentinel: do not modify this field in ON CONFLICT UPDATE


async def _finalized_drivers_off_entries(
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> tuple[list[dict], dict[str, str | None]]:
    """
    Stage B3 Unit 8C-7: Locked/Archived Status evidence for CP-2.5 Drivers Off.

    Reuses the same Approved-PeriodApproval-review-item snapshot authority
    and shared read primitives as Day Grid (Unit 8C-3) and CP-5B Off Drivers
    (Unit 8C-5) via status_evidence.py -- never the EntryState freeze columns
    (StatusCodeSnapshot/StatusLabelSnapshot/StatusIsOffReasonSnapshot/
    FinalizedAtUtc), never live PayrollStatusKeys, never legacy DailyStatus
    DraftLines. Returns (entries, {state, reason_code}); entries is always
    [] unless state is AVAILABLE with at least one captured off-reason row.
    """
    snapshot, availability = await status_evidence.resolve_finalized_snapshot(
        db, period_id=period.payroll_period_id, company_id=company_id, branch_id=period.branch_id,
    )
    if snapshot is None:
        # No usable snapshot provenance -- never fall back to another
        # snapshot or to mutable current state; evidence is unavailable.
        return [], availability

    status_rows = await status_evidence.read_status_entries(
        db,
        snapshot_id=snapshot["payrollcalculationsnapshotid"],
        company_id=company_id,
        branch_id=period.branch_id,
        period_id=period.payroll_period_id,
    )
    evidence_state = status_evidence.status_evidence_availability(snapshot, status_rows)
    off_rows = [row for row in status_rows if row["is_off_reason"]]
    if not off_rows:
        return [], evidence_state

    driver_ids = sorted({row["driver_id"] for row in off_rows})
    identity_rows = (await db.execute(
        text("""
            SELECT d.driverid, e.fullname AS drivername, d.drivercode
            FROM core.drivers d
            JOIN core.employees e ON e.employeeid = d.employeeid
            WHERE d.companyid = :company_id
              AND d.driverid  = ANY(:driver_ids)
        """),
        {"company_id": company_id, "driver_ids": driver_ids},
    )).mappings().all()
    identities = {int(r["driverid"]): r for r in identity_rows}

    # NoteText is not part of the immutable Status-evidence table's contract
    # (see status_evidence.read_status_entries) -- read it separately from
    # canonical EntryState, which no write path can change once a period is
    # Locked (Locked/Archived are in _WRITE_BLOCKED_STATUSES).
    note_rows = (await db.execute(
        text("""
            SELECT driverid, workdate, notetext
            FROM payroll.payrollperioddriverdayentrystate
            WHERE payrollperiodid = :period_id
              AND companyid = :company_id
              AND isvoided = FALSE
        """),
        {"period_id": period.payroll_period_id, "company_id": company_id},
    )).mappings().all()
    notes_by_day = {
        (int(row["driverid"]), row["workdate"]): row["notetext"] for row in note_rows
    }

    entries: list[dict] = []
    for row in sorted(off_rows, key=lambda r: (r["work_date"], r["driver_id"])):
        identity = identities.get(row["driver_id"])
        if identity is None:
            continue
        entries.append({
            "driver_id":       row["driver_id"],
            "driver_name":     identity["drivername"],
            "driver_code":     identity["drivercode"],
            "work_date":       row["work_date"],
            "status_key_code": row["status_code"],
            "status_label":    row["status_label"],
            "notes":           notes_by_day.get((row["driver_id"], row["work_date"])),
        })
    return entries, evidence_state


async def get_drivers_off(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> tuple[list[dict], dict[str, str | None] | None]:
    """
    Return all off-driver records for the entire period (all work dates).

    Draft/Open/InReview/Returned/Approved (unchanged): an off-driver record
    is a legacy DailyStatus line whose status code maps to a live
    PayrollStatusKeys row with IsOffReason = TRUE. The status key code is
    stored in the Notes column of DailyStatus lines. An optional DailyNote
    line for the same driver/date is joined to supply the driver-level notes
    text.

    Locked/Archived (Stage B3 Unit 8C-7): Status meaning comes only from
    immutable calculation-snapshot evidence via _finalized_drivers_off_entries
    -- never live PayrollStatusKeys, never legacy DailyStatus DraftLines.
    Returns (entries, {state, reason_code}) instead of (entries, None).

    ODA/Driver users are blocked unconditionally.
    payroll.view OR payroll.entry permission is required.
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    if period.status in ("Locked", "Archived"):
        entries, evidence_state = await _finalized_drivers_off_entries(period, company_id, db)
        return entries, evidence_state

    result = await db.execute(
        text("""
            SELECT
                d.driverid,
                e.fullname       AS drivername,
                d.drivercode,
                dl.workdate,
                dl.notes         AS status_key_code,
                sk.keyname       AS status_label,
                dn.notes         AS driver_notes
            FROM payroll.payrolldraftlines dl
            JOIN core.drivers   d  ON d.driverid  = dl.driverid
            JOIN core.employees e  ON e.employeeid = d.employeeid
            JOIN payroll.payrollstatuskeys sk
                ON  sk.companyid  = dl.companyid
                AND sk.branchid   = dl.branchid
                AND sk.statuscode = dl.notes
                AND sk.isoffreason = TRUE
                AND sk.isactive    = TRUE
            LEFT JOIN payroll.payrolldraftlines dn
                ON  dn.payrollperiodid = dl.payrollperiodid
                AND dn.driverid        = dl.driverid
                AND dn.workdate        = dl.workdate
                AND dn.linetype        = 'DailyNote'
                AND dn.status         != 'Void'
            WHERE dl.payrollperiodid = :period_id
              AND dl.companyid       = :company_id
              AND dl.linetype        = 'DailyStatus'
              AND dl.status         != 'Void'
            ORDER BY dl.workdate, e.fullname
        """),
        {"period_id": period_id, "company_id": company_id},
    )

    rows = result.mappings().all()
    return [
        {
            "driver_id":        int(r["driverid"]),
            "driver_name":      r["drivername"],
            "driver_code":      r["drivercode"],
            "work_date":        r["workdate"],
            "status_key_code":  r["status_key_code"],
            "status_label":     r["status_label"],
            "notes":            r["driver_notes"],
        }
        for r in rows
    ], None
