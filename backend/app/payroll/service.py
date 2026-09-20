"""
Payroll domain service — periods (list, create, status change) and draft lines.

All SQL is raw parameterised via sqlalchemy.text().
Branch-access enforcement is performed at the top of every mutating function;
read functions filter by the user's allowed branches directly in the query.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# B4-21 correction: _build_in_clause was briefly (and incorrectly) removed
# from this import as an orphan — it is production-load-bearing until B4-22
# retargets current_hub.py, which resolves it via qualified module-attribute
# access (service._build_in_clause), not a direct symbol import from this
# module's namespace. _check_any_permission and _require_not_driver_role
# remain load-bearing for get_drivers_off, the last real implementation here.
from app.core.service import (
    _build_in_clause,  # noqa: F401
    _check_any_permission, _require_not_driver_role,
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
# Stage B4-19 moved finalize_period (Finalization) out of this module — that
# was the last remaining caller here of capture_workflow_action_evidence, so
# it is no longer re-exported; app.payroll.finalization imports its own copy
# directly.
# Stage B4-13A: capture_period_audit_evidence is no longer imported here — its
# only two callers in this module (_upsert_entry_state, _void_entry_state_field)
# moved to app.payroll.day_entry_state, which imports it directly. No other
# function in this module calls it, so this binding is no longer load-bearing.
# Stage B4-18 moved change_period_status and resubmit_period (Lifecycle) out
# of this module — that was the last remaining caller here of
# link_unmapped_audit_evidence_to_snapshot, so it is no longer re-exported;
# period_lifecycle imports its own copy directly.
# Stage B4-17: app.payroll.snapshot_hash is no longer imported here —
# CURRENT_PAYROLL_CALCULATION_VERSION, CURRENT_REPORT_EVIDENCE_VERSION,
# canonical_json, calculate_report_evidence_hash, calculate_snapshot_hash,
# and calculate_source_config_hash had exactly one caller each
# (_capture_calculation_snapshot), which moved to
# app.payroll.period_calculation and imports all six directly.
from app.payroll import status_evidence
# Stage B4-2A: shared payroll access/guard helpers moved to app.payroll.guards.
# _check_own_driver_only, _check_not_in_finalized_period, and
# _check_driver_read_access are NOT re-exported (as of B4-4B): their only
# remaining callers were Rates functions, now in app.payroll.rates, which
# imports them directly from app.payroll.guards.
# Stage B4-19 moved finalize_period and get_finalization_preview
# (Finalization) out of this module — that was the last remaining caller
# here of _get_oda_own_driver_id, so it is no longer re-exported;
# app.payroll.finalization imports its own copy directly.
# test_cp4f_finalization_snapshot_projection.py's no_access_checks fixture,
# previously documented here, is retargeted to app.payroll.finalization as
# part of this stage.
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
# in full. _cp1c_error, _check_slot_matrix, get_period_candidates, and
# create_period_from_candidate were never re-exported here (router.py and
# current_hub.py call period_creation directly). _validate_period_work_date,
# _period_has_pay_item_snapshot, and _get_period_pay_item_snapshot were
# returned to this module in a B4-4A ownership correction (see the
# "Work-date / pay-item snapshot accessors" section below) rather than
# staying under period_creation.py's ownership.
# Stage B4-21 moved legacy create_period (and with it _auto_period_name,
# _month_end, _unique_period_code, and _create_period_day_rows — its only
# remaining callers here) to app.payroll.period_creation directly; none of
# those four is re-exported anymore, as a fresh whole-tree search confirmed
# no test imports any of them from this module. ensure_current_schedule_version
# and _create_period_pay_item_rows are retained below as test-only
# compatibility, not real internal consumers: test_cp2a_schedule_versioning.py
# imports ensure_current_schedule_version directly from this module, and
# test_cp5c_reports.py, test_p6a/b/c_finalized_*.py, test_p6d_finalized_audit.py,
# and test_lg1_cdpi_ledger.py import _create_period_pay_item_rows directly.
from app.payroll.period_creation import (
    ensure_current_schedule_version,  # noqa: F401
    _create_period_pay_item_rows,  # noqa: F401
)
# Stage B4-4A ownership correction: _acquire_branch_workflow_lock is a
# generic branch-level advisory-lock primitive genuinely shared by Period
# Creation, Lifecycle, Finalization, and app.review.service — none of which
# owns it more than the others; it lives in its own small neutral module,
# app.payroll.workflow_lock. Stage B4-19 moved Finalization (finalize_period)
# out of this module, and Stage B4-21 moved legacy create_period out too —
# those were this module's last two runtime callers, so this binding is no
# longer re-exported here; a fresh whole-tree search confirmed no test
# imports it from this module either.
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
# inside the new module — it has no remaining caller here. Stage B4-16 moved
# Day Grid out of this module — get_day_grid was the only remaining caller
# of _INFORMATIONAL_ONLY here, so it is no longer re-exported; Day Grid
# imports its own copy directly. Stage B4-17: _LEGACY_TO_CANONICAL is no
# longer imported here either — its only two callers
# (_refresh_draft_calculations, _compute_draft_line_preview_amounts) moved to
# app.payroll.period_calculation, which imports its own copy directly.
# Stage B4-21 deleted the two dead legacy constants that used to construct
# _LineTypeInfo instances here, _SYSTEM_LINE_TYPE_INFO and _SYSTEM_LINE_TYPES
# (B4-10 Decision Review had left both as residue pending a dedicated
# dead-code cleanup stage; a fresh whole-repository search confirmed both
# remained dead) — _LineTypeInfo has no remaining caller here either and is
# no longer imported.
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
# copy directly from the same owner module. Stage B4-21 moved get_period_lines
# (the last remaining caller here of _LINE_SELECT and _line_row_to_summary)
# to app.payroll.source_line_read directly — neither binding is re-exported
# here anymore, and a fresh whole-tree search confirmed no test imports
# either from this module.
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
# _KEEP was never re-exported: it had zero real executable consumers
# anywhere in the repo (confirmed by a fresh whole-repo search at B4-13A,
# and reconfirmed at B4-21) and was left behind here as legacy/dead residue
# rather than promoted into the new module without a live consumer — Stage
# B4-21 deleted it. _canonical_aliases and
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
# Only get_period_by_id is re-exported here: get_drivers_off (Drivers Off,
# deferred to B4-22) is this module's last remaining internal caller. Stage
# B4-21 moved Period Create's own call site to app.payroll.period_creation
# directly, alongside get_period_entry_count/get_period_lines/
# get_period_draft_summary/get_period_eligible_drivers's call sites, which
# each now import get_period_by_id directly from their own new owner
# modules. app.payroll.off_drivers also resolves it via this facade
# (qualified service.get_period_by_id access) — left unchanged, not
# redirected, since the facade is load-bearing anyway pending B4-22.
# get_periods has no remaining caller here (router.py now calls
# period_read.get_periods directly); _BASE_SELECT and _row_to_summary are
# private implementation details of period_read.py with no caller anywhere
# else. Do not remove this binding until Drivers Off itself moves in B4-22.
from app.payroll.period_read import get_period_by_id
# Stage B4-1: driver eligibility helpers moved to app.payroll.eligibility.
# Re-exported here (same names) so this module stays a compatibility facade —
# every existing internal call site and external consumer (current_hub.py,
# off_drivers.py, finalized_library_read_model.py) keeps working unchanged.
from app.payroll.eligibility import (
    # Temporary compatibility re-export: test_cp2e_eligibility_snapshot.py
    # imports this directly from app.payroll.service.
    _get_driver_eligibility_row,  # noqa: F401
    # Stage B4-21 moved get_period_eligible_drivers (this symbol's last
    # INTERNAL caller here) to app.payroll.eligibility directly, but this
    # binding stays production-load-bearing, not test-only: current_hub.py
    # and off_drivers.py both resolve it via qualified module-attribute
    # access (service._period_has_driver_eligibility_snapshot), not a direct
    # symbol import, so no static import-graph search finds them. Also
    # re-exported for test_cp2e_eligibility_snapshot.py, which imports it
    # directly from app.payroll.service.
    _period_has_driver_eligibility_snapshot,  # noqa: F401
    # B4-21 correction: this binding is production-load-bearing, not
    # test-only — current_hub.py and off_drivers.py both resolve it via
    # qualified module-attribute access (service._is_snapshot_row_eligible_for_workdate),
    # not a direct symbol import, so no static import-graph search finds
    # them. Day Grid (get_day_grid) stopped being an INTERNAL caller here at
    # B4-16 and imports its own copy directly from app.payroll.eligibility;
    # that is unrelated to the two qualified-access consumers above. Also
    # re-exported for test_cp2e_eligibility_snapshot.py, which imports it
    # directly from app.payroll.service.
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
    # Stage B4-21 moved create_period (this symbol's last internal caller
    # here) to app.payroll.period_creation directly. Re-exported here only
    # because test_cp2e_eligibility_snapshot.py imports it directly from
    # app.payroll.service — test-only compatibility, not a real internal
    # consumer of this module.
    _create_period_driver_eligibility_rows,  # noqa: F401
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
# Stage B4-21 moved create_period, get_next_period_dates,
# get_period_entry_count, get_period_lines, get_period_draft_summary, and
# get_period_eligible_drivers — the last remaining callers here of
# PeriodCreate, NextPeriodDates, PeriodEntryCount, DraftLineSummary, and
# DriverPeriodSummary — to their new owner modules, which each import their
# own copy directly; none of those five is re-exported anymore. Only
# PeriodSummary remains: get_drivers_off (Drivers Off, deferred to B4-22)
# still uses it as a parameter type in this module.
from app.payroll.schemas import PeriodSummary
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
# Stage B4-19: the Payroll Finalization domain (_write_finalization_audit,
# _snapshot_finalization_error, _load_approved_snapshot_packet,
# _reconcile_approved_snapshot_packet, _snapshot_line_draft_line_id,
# _snapshot_line_provenance, _project_approved_snapshot_final_lines,
# finalize_period, get_finalization_preview) moved to
# app.payroll.finalization in full — router.py now calls finalize_period
# and get_finalization_preview directly, and no production caller in this
# module resolves any bare name anymore. Two bindings are nevertheless
# retained here as test-only compatibility, not real internal consumers:
# seven test files (test_cp4f, test_cp5c_reports, test_p6a/b/c/d,
# test_phase6_immutable_evidence) import finalize_period and/or
# get_finalization_preview directly from this module. The seven private
# snapshot/audit helpers are NOT re-exported: a fresh whole-tree search at
# this stage confirmed none has a remaining caller or test import through
# this module's namespace — the two test files that previously patched
# _write_finalization_audit and _project_approved_snapshot_final_lines via
# this module (test_finalize.py, test_cp4f_finalization_snapshot_projection.py)
# were retargeted to app.payroll.finalization, where finalize_period now
# resolves both as bare names through that module's own globals.
from app.payroll.finalization import (
    finalize_period,  # noqa: F401
    get_finalization_preview,  # noqa: F401
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
# get_period_eligible_drivers was never Bonus CRUD ownership either — B4-21
# moved it to app.payroll.eligibility (see that module's own docstring for
# why).
#
# Stage B4-9.5: the Ledger read domain (_FINAL_SELECT, get_final_lines) moved
# to app.payroll.ledger_read in full. No facade is kept here: router.py now
# calls app.payroll.ledger_read directly, and no internal service.py caller
# or test imports either symbol from this module. get_drivers_off /
# _finalized_drivers_off_entries stay in this module — B4-9 deferred both
# pending targeted architectural discovery (B4-20 resolved the underlying
# structural cause; the move itself is reserved for B4-22), not a Ledger
# ownership question.


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
