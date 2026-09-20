"""
Payroll domain service — periods (list, create, status change) and draft lines.

All SQL is raw parameterised via sqlalchemy.text().
Branch-access enforcement is performed at the top of every mutating function;
read functions filter by the user's allowed branches directly in the query.
"""
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _check_branch_access, _build_in_clause, _check_permission, _check_any_permission,
    _require_not_driver_role,
)
# Stage B4-13C: PerUnitInput/calculate_per_unit are no longer imported here —
# their only caller (_compute_calculated_amount) moved to
# app.payroll.draft_line_calculation, which imports them directly.
# Stage B4-17: PER_UNIT_CALCULATION_VERSION is no longer imported here either
# — its only caller (_build_live_calculation_packet) moved to
# app.payroll.period_calculation, which imports it directly.
# Stage B4-17: capture_snapshot_used_rate_definitions is no longer imported
# here — its only caller (_capture_calculation_snapshot) moved to
# app.payroll.period_calculation, which imports it directly.
from app.payroll.immutable_evidence import (
    capture_workflow_action_evidence,
)
# Stage B4-13A: capture_period_audit_evidence is no longer imported here — its
# only two callers in this module (_upsert_entry_state, _void_entry_state_field)
# moved to app.payroll.day_entry_state, which imports it directly. No other
# function in this module calls it, so this binding is no longer load-bearing.
# Stage B4-18 moved change_period_status and resubmit_period (Lifecycle) out
# of this module — that was the last remaining caller here of
# link_unmapped_audit_evidence_to_snapshot, so it is no longer re-exported;
# period_lifecycle imports its own copy directly.
from app.payroll.audit_evidence import (
    initialize_period_audit_evidence_coverage,
)
# Stage B4-17: app.payroll.snapshot_hash is no longer imported here —
# CURRENT_PAYROLL_CALCULATION_VERSION, CURRENT_REPORT_EVIDENCE_VERSION,
# canonical_json, calculate_report_evidence_hash, calculate_snapshot_hash,
# and calculate_source_config_hash had exactly one caller each
# (_capture_calculation_snapshot), which moved to
# app.payroll.period_calculation and imports all six directly.
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
# Creation, Lifecycle, Finalization (finalize_period), legacy create_period,
# and app.review.service — none of which owns it more than the others.
# Extracted into its own small neutral module rather than left under
# period_creation.py's ownership. Stage B4-18 moved Lifecycle
# (change_period_status, resubmit_period) out of this module; it now
# imports its own copy directly from app.payroll.workflow_lock. This plain
# imported binding still stays here: legacy create_period and
# finalize_period still live in this module and still call it by its bare
# name — this binding is load-bearing for those two runtime callers, not
# incidental.
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
# Stage B4-17: _LEGACY_TO_CANONICAL is no longer imported here — its only
# two callers (_refresh_draft_calculations, _compute_draft_line_preview_amounts)
# moved to app.payroll.period_calculation, which imports its own copy
# directly.
from app.payroll.line_type_vocabulary import (
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
# app.payroll.draft_line_calculation. Stage B4-17 moved
# _refresh_draft_calculations and _compute_draft_line_preview_amounts (the
# last two remaining PRODUCTION callers here) to app.payroll.period_calculation,
# which imports its own copy directly. This binding stays here, though, as
# test-only compatibility: test_cp4a_perunit_core.py and
# test_phase4_characterization_slice1.py both import
# _compute_calculated_amount directly from app.payroll.service.
from app.payroll.draft_line_calculation import _compute_calculated_amount  # noqa: F401
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
# Stage B4-17 moved _build_live_calculation_packet (Calculation) to
# app.payroll.period_calculation — that was the only remaining caller here
# of _resolve_live_status_payment_lines and _STATUS_PAYMENT_PROJECTION_SQL,
# so neither is re-exported anymore; period_calculation imports both
# directly. Stage B4-18 moved change_period_status and resubmit_period
# (Lifecycle) to app.payroll.period_lifecycle — that was the last remaining
# caller here of _refresh_status_payment_lines, so it is no longer
# re-exported either; period_lifecycle imports its own copy directly. No
# binding from app.payroll.status_payment_sync remains load-bearing in this
# module.
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
    # Stage B4-18 moved change_period_status (Lifecycle) out of this module
    # — that was the only remaining production caller here of
    # _regenerate_period_driver_eligibility_rows; period_lifecycle imports
    # its own copy directly. Re-exported here only because
    # test_cp2e_eligibility_snapshot.py imports it directly from
    # app.payroll.service — test-only compatibility, not a real internal
    # consumer of this module.
    _regenerate_period_driver_eligibility_rows,  # noqa: F401
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
# Stage B4-17 moved get_calculation_preview (Calculation) to
# app.payroll.period_calculation — that was the only remaining caller here
# of ENTRY_ALLOWED_STATUSES, so it is no longer re-exported;
# period_calculation imports its own copy directly.
# Stage B4-18 moved change_period_status (Lifecycle) to
# app.payroll.period_lifecycle — that was the only remaining caller here of
# PeriodStatusChange and _VALID_TRANSITIONS, so neither is re-exported
# anymore; period_lifecycle imports its own copy of both directly.
from app.payroll.schemas import (
    PeriodSummary, PeriodCreate, NextPeriodDates, PeriodEntryCount,
    DraftLineSummary,
    DriverPeriodSummary,
)
# Stage B4-17: the Period Calculation + Snapshot domain
# (_RATE_DEPENDENT_BEHAVIORS, _refresh_draft_calculations,
# _validate_period_can_finalize, _compute_draft_line_preview_amounts,
# _CalculationPacketLine, _CalculationPacketDriverTotal,
# _LiveCalculationPacket, _load_active_bonus_events, _load_report_evidence,
# _validate_legacy_status_canonicalized, _build_live_calculation_packet,
# get_calculation_preview, _packet_driver_totals_for_hash,
# _capture_calculation_snapshot) moved to app.payroll.period_calculation in
# full. Stage B4-18 moved Lifecycle (change_period_status, resubmit_period)
# out of this module too — those were the last remaining production callers
# here of _refresh_draft_calculations, so it is no longer re-exported;
# period_lifecycle imports its own copy directly. _build_live_calculation_packet
# and _capture_calculation_snapshot keep a plain imported binding here
# purely as test-only compatibility, not real internal consumers: ten test
# files (test_cp4d/e/f, test_cp5c_reports, test_cp5c_frozen_report_evidence,
# test_p6a/b/c, test_phase6_immutable_evidence, test_report_authority_resolver)
# import _capture_calculation_snapshot and/or the three packet classes
# directly from this module, and test_cp5c_frozen_report_evidence.py
# imports _build_live_calculation_packet directly too. _validate_period_can_finalize
# and get_calculation_preview are likewise retained as test-only
# compatibility: test_cp2e_eligibility_snapshot.py imports
# _validate_period_can_finalize directly, and test_cp4d_submit_snapshot_capture.py
# imports get_calculation_preview directly. _RATE_DEPENDENT_BEHAVIORS,
# _compute_draft_line_preview_amounts, _load_active_bonus_events,
# _load_report_evidence, _validate_legacy_status_canonicalized, and
# _packet_driver_totals_for_hash are NOT re-exported: a fresh whole-tree
# search at this stage confirmed none has a remaining caller or test import
# through this module's namespace. current_hub.py and report_read_model.py,
# previously reverse dependencies of this module for
# _build_live_calculation_packet (both) and _load_active_bonus_events
# (report_read_model only), import app.payroll.period_calculation directly.
from app.payroll.period_calculation import (
    _build_live_calculation_packet,  # noqa: F401
    _CalculationPacketDriverTotal,  # noqa: F401
    _CalculationPacketLine,  # noqa: F401
    _capture_calculation_snapshot,  # noqa: F401
    _LiveCalculationPacket,  # noqa: F401
    _validate_period_can_finalize,  # noqa: F401
    get_calculation_preview,  # noqa: F401
)
# Stage B4-18: the Period Lifecycle + Resubmission domain
# (_TRANSITION_PERMISSIONS, _is_retryable_transaction_failure,
# _translate_submit_transaction_failures, _PERIOD_AUDIT_REASONS,
# _write_period_status_audit, _is_inreview_slot_violation,
# _check_inreview_slot_available, _set_submit_transaction_isolation,
# change_period_status, resubmit_period) moved to app.payroll.period_lifecycle
# in full — router.py now calls change_period_status/resubmit_period
# directly, and app/review/service.py now imports _write_period_status_audit
# directly from app.payroll.period_lifecycle. No production caller in this
# module resolves any bare name anymore. Five bindings are nevertheless
# retained here as test-only compatibility, not real internal consumers:
# test_cp4d_submit_snapshot_capture.py, test_cp5c_frozen_report_evidence.py,
# and test_phase6_immutable_evidence.py import change_period_status and/or
# resubmit_period directly from this module, and
# test_cp4d_submit_snapshot_capture.py calls
# payroll_service._is_retryable_transaction_failure and
# payroll_service._set_submit_transaction_isolation directly (pure-logic
# calls, not resolution-sensitive monkeypatches). _write_period_status_audit
# is retained for the same reason: test_cp1d_submit_promotion.py's two
# monkeypatch sites were retargeted to app.payroll.period_lifecycle (where
# change_period_status now resolves it), but the plain import binding here
# costs nothing and matches the sibling pattern. _TRANSITION_PERMISSIONS,
# _translate_submit_transaction_failures, _PERIOD_AUDIT_REASONS,
# _is_inreview_slot_violation, and _check_inreview_slot_available are NOT
# re-exported: a fresh whole-tree search at this stage confirmed none has a
# remaining caller or test import through this module's namespace.
from app.payroll.period_lifecycle import (
    _is_retryable_transaction_failure,  # noqa: F401
    _set_submit_transaction_isolation,  # noqa: F401
    _write_period_status_audit,  # noqa: F401
    change_period_status,  # noqa: F401
    resubmit_period,  # noqa: F401
)
# Stage B4-8: the Bonus domain (_BONUS_ENTRY_ALLOWED_STATUSES,
# _get_bonus_event_by_id, list_bonus_events, _increment_bonus_data_revision,
# create_bonus_event, update_bonus_event, void_bonus_event,
# _bonus_batch_canonical_payload, _bonus_batch_request_hash,
# _get_bonus_events_in_order, apply_bonus_batch,
# _bonus_summary_driver_create_eligible, get_bonus_summary) moved to
# app.payroll.bonus in full. No facade is kept here: router.py now calls
# app.payroll.bonus directly, and no internal service.py caller or test
# imports any Bonus symbol from this module. Stage B4-17 moved
# _load_active_bonus_events to app.payroll.period_calculation (it was never
# Bonus CRUD ownership — see period_calculation.py's own docstring for why).
# get_period_eligible_drivers stays in this module — it is not Bonus CRUD
# ownership either (see its own definition below for why).
#
# Stage B4-9.5: the Ledger read domain (_FINAL_SELECT, get_final_lines) moved
# to app.payroll.ledger_read in full. No facade is kept here: router.py now
# calls app.payroll.ledger_read directly, and no internal service.py caller
# or test imports either symbol from this module. get_period_eligible_drivers
# and get_drivers_off / _finalized_drivers_off_entries stay in this module —
# B4-9 deferred both pending targeted architectural discovery, not a Ledger
# ownership question.




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




# ===========================================================================
# Finalization — Approved → Locked
# ===========================================================================



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
