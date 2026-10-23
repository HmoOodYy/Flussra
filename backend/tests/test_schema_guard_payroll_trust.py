"""
test_schema_guard_payroll_trust.py — Phase 10

Tests that schema_guard._check_payroll_trust() detects missing payroll trust
DB objects and that run_schema_guard() fails closed in dev mode.

Strategy
--------
Rather than mutating the shared test DB (dropping triggers, columns, or indexes
in a session-scoped fixture is too risky), we monkeypatch the individual helper
functions (_column_exists, _index_exists, _function_exists,
_trigger_exists_and_enabled) that _check_payroll_trust() delegates to.

This lets us simulate any missing object in isolation without touching the DB,
and makes the tests deterministic regardless of execution order.

The "guard passes on full DB" test uses the real test DB (via pg_instance DSN)
so we also confirm the helpers work correctly against a live PostgreSQL.
"""
from __future__ import annotations

from unittest.mock import patch

import psycopg2
import pytest

import app.db.schema_guard as _guard
from app.db.schema_guard import (
    _check_payroll_trust,
    _trigger_exists_and_enabled,
    run_schema_guard,
)

# ---------------------------------------------------------------------------
# Helper: build a fake cursor whose responses can be controlled per-call
# ---------------------------------------------------------------------------

class _StubCursor:
    """
    A minimal psycopg2-cursor stub.

    Pass ``missing`` as the set of (object_type, name) tuples to treat as
    absent.  All other objects return a truthy row.

    object_type values used internally:
      'column'   -> (schema, table, column)
      'index'    -> (schema, index_name)
      'function' -> (schema, fn_name)
      'trigger'  -> (trig_name, tbl_schema, tbl_name)
    """
    def __init__(self, missing: set):
        self._missing = missing
        self._result = None

    def execute(self, sql, params=None):
        sql_lower = sql.strip().lower()
        if "information_schema.columns" in sql_lower:
            # params: (schema, table, column)
            key = ("column", params[0], params[1], params[2])
        elif "pg_indexes" in sql_lower:
            # params: (schema, index_name)
            key = ("index", params[0], params[1])
        elif "information_schema.routines" in sql_lower:
            # params: (schema, fn_name) — fn_name already lowercased in helper
            key = ("function", params[0], params[1].lower())
        elif "pg_trigger" in sql_lower:
            # params: (trigger_name, table_schema, table_name)
            key = ("trigger", params[0].lower(), params[1], params[2])
        else:
            key = None
        self._result = None if (key in self._missing) else ("found",)

    def fetchone(self):
        return self._result


def _make_cur(missing: set = None):
    return _StubCursor(missing or set())


# ---------------------------------------------------------------------------
# T1 — guard passes on fully migrated DB (live test DB)
# ---------------------------------------------------------------------------

def test_p10_t1_guard_passes_on_full_db(apply_schema):
    """
    T1: _check_payroll_trust() returns zero errors against the fully-migrated
    test database.  Uses a real psycopg2 connection to the test cluster.
    """
    dsn = apply_schema.dsn()
    conn = psycopg2.connect(client_encoding="utf-8", **dsn)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        errors = _check_payroll_trust(cur)
    finally:
        cur.close()
        conn.close()

    assert errors == [], (
        "Expected no payroll trust errors on fully-migrated DB, got:\n"
        + "\n".join(f"  {e}" for e in errors)
    )


# ---------------------------------------------------------------------------
# T2 — missing SourceSnapshot column detected (Phase 9)
# ---------------------------------------------------------------------------

def test_p10_t2_missing_sourcesnapshot_column_detected():
    """
    T2: _check_payroll_trust() must report a Phase 9 error when
    payroll.payrollfinallines.sourcesnapshot is absent.
    """
    missing = {("column", "payroll", "payrollfinallines", "sourcesnapshot")}
    cur = _make_cur(missing)
    errors = _check_payroll_trust(cur)

    assert any("sourcesnapshot" in e and "Phase 9" in e for e in errors), (
        f"Expected Phase 9 / sourcesnapshot error, got: {errors}"
    )


# ---------------------------------------------------------------------------
# T3 — missing final line INSERT trigger detected (Phase 6)
# ---------------------------------------------------------------------------

def test_p10_t3_missing_final_line_insert_trigger_detected():
    """
    T3: _check_payroll_trust() must report a Phase 6 error when
    trg_guard_final_line_insert on payroll.payrollfinallines is absent.
    """
    missing = {("trigger", "trg_guard_final_line_insert", "payroll", "payrollfinallines")}
    cur = _make_cur(missing)
    errors = _check_payroll_trust(cur)

    assert any(
        "trg_guard_final_line_insert" in e and "Phase 6" in e
        for e in errors
    ), (
        f"Expected Phase 6 / trg_guard_final_line_insert error, got: {errors}"
    )


# ---------------------------------------------------------------------------
# Target draft identity index remains part of payroll trust.

def test_p10_t6_missing_target_draft_identity_index_detected():
    missing = {("index", "payroll", "ux_draftlines_targetbusinesskey")}
    errors = _check_payroll_trust(_make_cur(missing))
    assert any(
        "ux_draftlines_targetbusinesskey" in error and "Target source identity (0085)" in error
        for error in errors
    ), f"Expected target draft identity index error, got: {errors}"


# T7 — missing Phase 3C immutability triggers detected
# ---------------------------------------------------------------------------

def test_p10_t7_missing_ledger_immutability_trigger_detected():
    """
    T7: _check_payroll_trust() must report Phase 3C errors when both
    trg_final_line_immutable and trg_period_status_revert are absent.
    """
    missing = {
        ("trigger", "trg_final_line_immutable", "payroll", "payrollfinallines"),
        ("trigger", "trg_period_status_revert", "payroll", "payrollperiods"),
    }
    cur = _make_cur(missing)
    errors = _check_payroll_trust(cur)

    assert any(
        "trg_final_line_immutable" in e and "Phase 3C" in e
        for e in errors
    ), f"Expected Phase 3C / trg_final_line_immutable error, got: {errors}"

    assert any(
        "trg_period_status_revert" in e and "Phase 3C" in e
        for e in errors
    ), f"Expected Phase 3C / trg_period_status_revert error, got: {errors}"


# ---------------------------------------------------------------------------
# T8 — disabled trigger detected as missing (tgenabled = 'D')
# ---------------------------------------------------------------------------

def test_p10_t8_disabled_trigger_detected_as_missing(apply_schema):
    """
    T8: _trigger_exists_and_enabled() must return False for a trigger whose
    tgenabled = 'D' (disabled).  We simulate this via monkeypatching the
    underlying query rather than disabling a live trigger.
    """
    dsn = apply_schema.dsn()
    conn = psycopg2.connect(client_encoding="utf-8", **dsn)
    conn.autocommit = True
    cur = conn.cursor()

    # Confirm the trigger is currently enabled on the real DB
    assert _trigger_exists_and_enabled(
        cur, "trg_guard_final_line_insert", "payroll", "payrollfinallines"
    ), "Precondition: trg_guard_final_line_insert should be enabled on full DB"

    cur.close()
    conn.close()

    # Now simulate a disabled trigger via stub
    class _DisabledCursor:
        def execute(self, sql, params=None):
            self._row = None
            if "pg_trigger" in sql.lower():
                # Return 'D' (disabled) for our target trigger
                if params and params[0].lower() == "trg_guard_final_line_insert":
                    self._row = ('D',)
                else:
                    self._row = ('O',)

        def fetchone(self):
            return self._row

    disabled_cur = _DisabledCursor()
    result = _trigger_exists_and_enabled(
        disabled_cur, "trg_guard_final_line_insert", "payroll", "payrollfinallines"
    )
    assert result is False, (
        "Expected _trigger_exists_and_enabled to return False for tgenabled='D'"
    )


# ---------------------------------------------------------------------------
# T9 — run_schema_guard raises RuntimeError in dev mode on missing PT object
# ---------------------------------------------------------------------------

def test_p10_t9_run_schema_guard_fails_closed_in_dev(apply_schema):
    """
    T9: run_schema_guard() must raise RuntimeError in dev mode when a payroll
    trust object is reported missing.
    """
    dsn = apply_schema.dsn()
    sync_url = (
        f"postgresql://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    )

    synthetic_error = (
        "Missing payroll trust schema object: "
        "payroll.payrollfinallines.sourcesnapshot (Phase 9). Run migrations."
    )

    with patch.object(_guard, "_check_payroll_trust", return_value=[synthetic_error]):
        with pytest.raises(RuntimeError) as exc_info:
            run_schema_guard(sync_url, is_dev=True)

    assert "Phase 9" in str(exc_info.value)
    assert "sourcesnapshot" in str(exc_info.value)


# ---------------------------------------------------------------------------
# T9b — run_schema_guard raises RuntimeError in PROD mode on missing PT object
# ---------------------------------------------------------------------------

def test_p10_t9b_run_schema_guard_fails_closed_in_prod(apply_schema):
    """
    T9b: run_schema_guard() must raise RuntimeError even in prod mode
    (is_dev=False) when a payroll trust object is missing.
    Payroll trust protections are not optional in any environment.
    """
    dsn = apply_schema.dsn()
    sync_url = (
        f"postgresql://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    )

    synthetic_error = (
        "Missing or disabled payroll trust schema object: "
        "trigger trg_guard_final_line_insert on payroll.payrollfinallines "
        "(Phase 6 (0038)). Run migrations or re-enable the trigger."
    )

    with patch.object(_guard, "_check_payroll_trust", return_value=[synthetic_error]):
        with pytest.raises(RuntimeError) as exc_info:
            run_schema_guard(sync_url, is_dev=False)   # <-- prod mode

    assert "Phase 6" in str(exc_info.value)
    assert "trg_guard_final_line_insert" in str(exc_info.value)
    assert "PAYROLL TRUST OR TEMPORAL AUTHORITY SCHEMA IS INVALID" in str(exc_info.value)


def test_branch_temporal_function_drift_blocks_production_startup(apply_schema):
    """The canonical timezone/instant function contract is hard in production too."""
    dsn = apply_schema.dsn()
    sync_url = f"postgresql://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    conn = psycopg2.connect(client_encoding="utf-8", **dsn)
    conn.autocommit = True
    cur = conn.cursor()
    renamed = False
    try:
        cur.execute("ALTER FUNCTION core.fn_BusinessDate(TEXT, TIMESTAMPTZ) RENAME TO fn_BusinessDate_guard_test")
        renamed = True
        with pytest.raises(RuntimeError, match="Startup blocked in all environments") as exc_info:
            run_schema_guard(sync_url, is_dev=False)
        assert "fn_BusinessDate(TEXT, TIMESTAMPTZ)" in str(exc_info.value)
    finally:
        if renamed:
            cur.execute("ALTER FUNCTION core.fn_BusinessDate_guard_test(TEXT, TIMESTAMPTZ) RENAME TO fn_BusinessDate")
        cur.close()
        conn.close()


def test_branch_temporal_constraint_drift_blocks_production_startup(apply_schema):
    """Transfer relies on the assignment exclusion being deferred to commit."""
    dsn = apply_schema.dsn()
    sync_url = f"postgresql://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    conn = psycopg2.connect(client_encoding="utf-8", **dsn)
    conn.autocommit = True
    cur = conn.cursor()
    replaced = False
    try:
        cur.execute("ALTER TABLE core.DriverAssignments DROP CONSTRAINT excl_DriverAssignments_Driver_Window")
        cur.execute("""
            ALTER TABLE core.DriverAssignments ADD CONSTRAINT excl_DriverAssignments_Driver_Window
            EXCLUDE USING gist (
                DriverID WITH =,
                core.fn_DriverEffectiveRange(EffectiveFromAt, EffectiveToAt) WITH &&
            ) NOT DEFERRABLE
        """)
        replaced = True
        with pytest.raises(RuntimeError, match="Startup blocked in all environments") as exc_info:
            run_schema_guard(sync_url, is_dev=False)
        assert "must be DEFERRABLE INITIALLY DEFERRED" in str(exc_info.value)
    finally:
        if replaced:
            cur.execute("ALTER TABLE core.DriverAssignments DROP CONSTRAINT excl_DriverAssignments_Driver_Window")
            cur.execute("""
                ALTER TABLE core.DriverAssignments ADD CONSTRAINT excl_DriverAssignments_Driver_Window
                EXCLUDE USING gist (
                    DriverID WITH =,
                    core.fn_DriverEffectiveRange(EffectiveFromAt, EffectiveToAt) WITH &&
                ) DEFERRABLE INITIALLY DEFERRED
            """)
        cur.close()
        conn.close()


def test_branch_timezone_registry_shape_drift_blocks_production_startup(apply_schema):
    """The DB-generated registry must keep its documented VARCHAR(100) key."""
    dsn = apply_schema.dsn()
    sync_url = f"postgresql://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    conn = psycopg2.connect(client_encoding="utf-8", **dsn)
    conn.autocommit = True
    cur = conn.cursor()
    altered = False
    try:
        cur.execute("ALTER TABLE core.Branches DROP CONSTRAINT fk_Branches_TimeZone")
        cur.execute("ALTER TABLE payroll.PayrollPeriodDriverEligibility DROP CONSTRAINT fk_PPDE_SnapshotTimeZone")
        cur.execute("ALTER TABLE core.SupportedTimeZones ALTER COLUMN TimeZoneId TYPE VARCHAR(99)")
        altered = True
        with pytest.raises(RuntimeError, match="Startup blocked in all environments") as exc_info:
            run_schema_guard(sync_url, is_dev=False)
        assert "SupportedTimeZones.TimeZoneId must be a NOT NULL VARCHAR(100)" in str(exc_info.value)
    finally:
        if altered:
            cur.execute("ALTER TABLE core.SupportedTimeZones ALTER COLUMN TimeZoneId TYPE VARCHAR(100)")
            cur.execute("""
                ALTER TABLE core.Branches ADD CONSTRAINT fk_Branches_TimeZone
                FOREIGN KEY (TimeZoneId) REFERENCES core.SupportedTimeZones(TimeZoneId)
            """)
            cur.execute("""
                ALTER TABLE payroll.PayrollPeriodDriverEligibility
                ADD CONSTRAINT fk_PPDE_SnapshotTimeZone
                FOREIGN KEY (BranchTimeZoneIdSnapshot)
                REFERENCES core.SupportedTimeZones(TimeZoneId)
            """)
        cur.close()
        conn.close()


def test_retired_company_clock_blocks_production_startup(apply_schema):
    """A resurrected Company clock is temporal authority drift, not a prod warning."""
    dsn = apply_schema.dsn()
    sync_url = f"postgresql://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    conn = psycopg2.connect(client_encoding="utf-8", **dsn)
    conn.autocommit = True
    cur = conn.cursor()
    created = False
    try:
        cur.execute("CREATE FUNCTION core.fn_CompanyToday(INTEGER) RETURNS DATE LANGUAGE SQL STABLE AS $$ SELECT CURRENT_DATE $$")
        created = True
        with pytest.raises(RuntimeError, match="Startup blocked in all environments") as exc_info:
            run_schema_guard(sync_url, is_dev=False)
        assert "Retired Company clock function" in str(exc_info.value)
    finally:
        if created:
            cur.execute("DROP FUNCTION core.fn_CompanyToday(INTEGER)")
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# T9c — legacy-only errors still only warn in prod mode (not raised)
# ---------------------------------------------------------------------------

def test_p10_t9c_legacy_error_only_warns_in_prod(apply_schema):
    """
    T9c: A legacy schema error (e.g. Alembic version mismatch) must NOT raise
    RuntimeError in prod mode — it should log.warning only.
    This confirms payroll trust handling is isolated and doesn't tighten
    unrelated prod-mode behaviour.
    """
    dsn = apply_schema.dsn()
    sync_url = (
        f"postgresql://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    )

    # Payroll trust: no errors. Legacy: one synthetic error.
    with patch.object(_guard, "_check_payroll_trust", return_value=[]):
        with patch.object(_guard, "log") as mock_log:
            # Simulate a legacy error by injecting a version mismatch via
            # patching _get_script_head to return a value that won't match.
            with patch.object(_guard, "_get_script_head", return_value="9999"):
                # Should NOT raise — prod mode + no PT errors
                run_schema_guard(sync_url, is_dev=False)

            # log.warning must have been called (version mismatch)
            assert mock_log.warning.called, (
                "Expected log.warning for legacy mismatch in prod mode, but it was not called"
            )
