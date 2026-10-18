"""
schema_guard.py — Dev-mode startup check that catches migration drift before
the first request can return a 500.

Problem it solves
-----------------
When a developer adds a new Alembic migration but doesn't run
`alembic upgrade head`, the backend starts fine but the first login hits a
missing table/function and returns an opaque 500.  This module makes that
failure loud and immediate — the process exits with a clear message before
accepting any connections.

Behaviour by environment
------------------------
  ENVIRONMENT=development  (default)
    • DB version must match the script head revision
    • All required schema objects must exist
    • Startup ABORTS with a clear message if either check fails

ENVIRONMENT=production
    • Legacy version/security checks may warn instead of blocking startup
    • Payroll trust and branch-timezone/instant authority checks always block
      startup when required database objects are missing or drifted

Usage
-----
Called once from app/main.py lifespan() before the first request is served.
Uses a raw psycopg2 connection (synchronous) so it runs before the async
engine pool is fully warmed up.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import psycopg2
from psycopg2 import OperationalError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required schema objects — security layer (original checks)
# ---------------------------------------------------------------------------
_REQUIRED_TABLES = {
    "core.supportedtimezones",
    "sec.companyroles",
    "sec.companyrolepermissions",
    "sec.userpermissionoverrides",
    "sec.users",
    "sec.userbranchroles",
    "sec.permissions",
}

_REQUIRED_FUNCTIONS = {
    "sec.fn_userhaspermission",
    "sec.fn_check_company_owner_unique",
}

_REQUIRED_TRIGGERS = {
    "trg_company_owner_unique",
}

# ---------------------------------------------------------------------------
# Payroll trust required objects — Phases 1 / 3B / 3C / 4C / 5 / 6 / 9
# Each entry: (object_type, schema, name, phase_label)
# ---------------------------------------------------------------------------

# Phase 1 / Migration 0033 — Duplicate daily draft line protection
_PT_INDEXES = [
    # (schema, index_name, phase_label)
    ("payroll", "uix_payrolldraftlines_daily_active_business_key", "Phase 1 (0033, rebuilt 0078)"),
    # Phase 5 / Migration 0037 — DriverRate void guard lookup index
    ("payroll", "ix_payrollfinallines_driverrateid", "Phase 5 (0037)"),
    # Phase 4C / Migration 0036 — RateTypes.CompanyID filter index
    ("payroll", "ix_ratetypes_companyid", "Phase 4C (0036)"),
]

# Phase 3B / Migration 0034 — Final line source snapshot scalar columns
_PT_COLUMNS = [
    # (schema, table, column, phase_label)
    ("payroll", "payrollfinallines", "payitemid",          "Phase 3B (0034)"),
    ("payroll", "payrollfinallines", "ratetypeid",         "Phase 3B (0034)"),
    ("payroll", "payrollfinallines", "driverrateid",       "Phase 3B (0034)"),
    ("payroll", "payrollfinallines", "resolvedrateamount", "Phase 3B (0034)"),
    ("payroll", "payrollfinallines", "ratebehavior",       "Phase 3B (0034)"),
    # Phase 4C / Migration 0036 — RateTypes ownership column
    ("payroll", "ratetypes",         "companyid",          "Phase 4C (0036)"),
    # Phase 9 / Migration 0039 — SourceSnapshot JSONB
    ("payroll", "payrollfinallines", "sourcesnapshot",     "Phase 9 (0039)"),
]

# Phase 3C / Migration 0035 — Locked ledger immutability
# Phase 4C / Migration 0036 — PayItemRateTypeMap ownership guard
# Phase 5  / Migration 0037 — DriverRate void guard
# Phase 6  / Migration 0038 — Final line INSERT guard
# Phase 9  / Migration 0039 — Used DriverRate mutation guard
# Phase 11 / Migration 0040 — DriverRateTier + Block field mutation guards
_PT_FUNCTIONS = [
    # (schema, function_name, phase_label)
    ("payroll", "fn_guard_final_line_immutable",            "Phase 3C (0035)"),
    ("payroll", "fn_guard_period_status_revert",            "Phase 3C (0035)"),
    ("payroll", "fn_guard_payitemratetypemap_ownership",    "Phase 4C (0036)"),
    ("payroll", "fn_guard_driverrate_void",                 "Phase 5 (0037)"),
    ("payroll", "fn_guard_final_line_insert",               "Phase 6 (0038)"),
    ("payroll", "fn_guard_driverrate_used_mutation",        "Phase 9 (0039)"),
    # Phase 11 / Migration 0040 — DriverRateTier mutation guard
    ("payroll", "fn_guard_driverratetier_used_mutation",   "Phase 11 (0040)"),
]

_PT_TRIGGERS = [
    # (trigger_name, table_schema, table_name, phase_label)
    ("trg_final_line_immutable",                   "payroll", "payrollfinallines",  "Phase 3C (0035)"),
    ("trg_period_status_revert",                   "payroll", "payrollperiods",     "Phase 3C (0035)"),
    ("trg_guard_payitemratetypemap_ownership",      "payroll", "payitemratetypemap", "Phase 4C (0036)"),
    ("trg_guard_driverrate_void",                  "payroll", "driverrates",        "Phase 5 (0037)"),
    ("trg_guard_final_line_insert",                "payroll", "payrollfinallines",  "Phase 6 (0038)"),
    ("trg_guard_driverrate_used_mutation",         "payroll", "driverrates",        "Phase 9 (0039)"),
    # Phase 11 / Migration 0040 — DriverRateTier mutation guard
    ("trg_guard_driverratetier_used_mutation",     "payroll", "driverratetiers",    "Phase 11 (0040)"),
]

# P3a (0076) Company-wide monetary denomination authority.
_P3A_COLUMNS = [
    ("core", "supportedcurrencies", "currencycode"),
    ("core", "supportedcurrencies", "minorunitdigits"),
    ("core", "supportedcurrencies", "numericcode"),
    ("core", "companies", "currencycode"),
    ("payroll", "payrollcalculationsnapshots", "currencycode"),
    ("payroll", "payrollcalculationsnapshots", "currencyminorunitdigits"),
    ("payroll", "payrollcalculationsnapshotusedratedefinitions", "currencycodesnapshot"),
    ("payroll", "payrollcalculationsnapshotusedratedefinitions", "currencyminorunitdigitssnapshot"),
    ("payroll", "payrollcalculationsnapshotbonusevents", "currencycodesnapshot"),
    ("payroll", "payrollcalculationsnapshotbonusevents", "currencyminorunitdigitssnapshot"),
    ("payroll", "payrollfinallines", "currencycode"),
    ("payroll", "payrollfinallines", "currencyminorunitdigits"),
]
_P3A_CONSTRAINTS = [
    ("core", "companies", "fk_companies_currency", "f"),
    ("core", "supportedcurrencies", "ck_supportedcurrencies_minorunitdigits", "c"),
    ("core", "supportedcurrencies", "ck_supportedcurrencies_code", "c"),
    ("core", "supportedcurrencies", "ck_supportedcurrencies_numeric", "c"),
    ("payroll", "payrollcalculationsnapshots", "fk_calculationsnapshots_currency", "f"),
    ("payroll", "payrollcalculationsnapshots", "ck_calculationsnapshots_currencyminor", "c"),
    ("payroll", "payrollcalculationsnapshotusedratedefinitions", "fk_usedratedefinitions_currency", "f"),
    ("payroll", "payrollcalculationsnapshotusedratedefinitions", "ck_usedratedefinitions_currencyminor", "c"),
    ("payroll", "payrollcalculationsnapshotbonusevents", "fk_bonuseventevidence_currency", "f"),
    ("payroll", "payrollcalculationsnapshotbonusevents", "ck_bonuseventevidence_currencyminor", "c"),
    ("payroll", "payrollfinallines", "fk_finallines_currency", "f"),
    ("payroll", "payrollfinallines", "ck_finallines_currencyminor", "c"),
]


# Alembic migrations directory — relative to this file's location
# backend/app/db/schema_guard.py -> ../../../../migrations/versions/
_VERSIONS_DIR = (
    Path(__file__).resolve().parent  # app/db/
    .parent                          # app/
    .parent                          # backend/
    .parent                          # Payroll_App_v3/
    / "migrations" / "versions"
)


def _get_script_head() -> str | None:
    """
    Determine the Alembic head revision by inspecting the versions/ directory.

    Walks all .py files looking for revision = "XXXX" with down_revision = None
    (or a chain end), or simply returns the highest numeric revision prefix.
    This avoids importing Alembic at startup (which is slow and imports env.py).
    """
    if not _VERSIONS_DIR.exists():
        return None

    # Build a map of revision -> down_revision
    rev_map: dict[str, str | None] = {}
    for f in _VERSIONS_DIR.glob("*.py"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        # Match both plain (`revision = "X"`) and type-annotated (`revision: str = "X"`) forms
        rev_m = re.search(r'^revision(?:\s*:\s*\w+)?\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        down_m = re.search(r'^down_revision(?:\s*:\s*\w+)?\s*=\s*(?:["\']([^"\']*)["\']|None)', text, re.MULTILINE)
        if rev_m:
            rev = rev_m.group(1)
            down = down_m.group(1) if (down_m and down_m.group(1)) else None
            rev_map[rev] = down

    # Head = revision not referenced as anyone's down_revision
    all_down = set(rev_map.values()) - {None}
    heads = [r for r in rev_map if r not in all_down]
    return heads[0] if len(heads) == 1 else None


def _build_sync_dsn(database_url: str) -> str:
    """
    Convert an asyncpg DATABASE_URL to a psycopg2-compatible one.
    postgresql+asyncpg://user:pass@host:port/db -> postgresql://user:pass@host:port/db
    """
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", database_url)


# ---------------------------------------------------------------------------
# Per-object helper checkers (take a psycopg2 cursor, return bool)
# ---------------------------------------------------------------------------

def _column_exists(cur, schema: str, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = %s
        """,
        (schema, table, column),
    )
    return cur.fetchone() is not None


def _index_exists(cur, schema: str, index_name: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM pg_indexes
        WHERE schemaname = %s AND indexname = %s
        """,
        (schema, index_name),
    )
    return cur.fetchone() is not None


def _function_exists(cur, schema: str, function_name: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.routines
        WHERE routine_schema = %s AND LOWER(routine_name) = LOWER(%s)
        """,
        (schema, function_name),
    )
    return cur.fetchone() is not None


def _trigger_exists_and_enabled(cur, trigger_name: str, table_schema: str, table_name: str) -> bool:
    """
    Check that a trigger exists on the named table AND is enabled
    (tgenabled not in ('D', 'O') — pg_trigger.tgenabled values:
      'O' = origin only, 'D' = disabled, 'A' = always, 'R' = replica).
    We treat 'D' (disabled) as missing for safety.
    """
    cur.execute(
        """
        SELECT t.tgenabled
        FROM   pg_trigger  t
        JOIN   pg_class    c ON c.oid = t.tgrelid
        JOIN   pg_namespace n ON n.oid = c.relnamespace
        WHERE  LOWER(t.tgname)      = LOWER(%s)
          AND  n.nspname             = %s
          AND  c.relname             = %s
          AND  NOT t.tgisinternal
        """,
        (trigger_name, table_schema, table_name),
    )
    row = cur.fetchone()
    if row is None:
        return False
    tgenabled = row[0]
    # 'D' means disabled — treat as missing
    return tgenabled != 'D'


# ---------------------------------------------------------------------------
# Payroll trust guard — checks all Phase 1–9 required objects
# ---------------------------------------------------------------------------

def _check_payroll_trust(cur) -> list[str]:
    """
    Verify all payroll trust schema objects required by Phases 1–11.
    Returns a list of error strings (empty = all good).
    All failures are hard (caller treats non-empty list as fatal).
    """
    errors: list[str] = []

    # ── Columns ────────────────────────────────────────────────────────────
    for schema, table, column, phase in _PT_COLUMNS:
        if not _column_exists(cur, schema, table, column):
            errors.append(
                f"Missing payroll trust schema object: "
                f"{schema}.{table}.{column} ({phase}). Run migrations."
            )

    # ── Indexes ────────────────────────────────────────────────────────────
    for schema, index_name, phase in _PT_INDEXES:
        if not _index_exists(cur, schema, index_name):
            errors.append(
                f"Missing payroll trust schema object: "
                f"index {schema}.{index_name} ({phase}). Run migrations."
            )

    # ── Functions ──────────────────────────────────────────────────────────
    for schema, fn_name, phase in _PT_FUNCTIONS:
        if not _function_exists(cur, schema, fn_name):
            errors.append(
                f"Missing payroll trust schema object: "
                f"function {schema}.{fn_name}() ({phase}). Run migrations."
            )

    # ── Triggers (existence + enabled) ─────────────────────────────────────
    for trig_name, tbl_schema, tbl_name, phase in _PT_TRIGGERS:
        if not _trigger_exists_and_enabled(cur, trig_name, tbl_schema, tbl_name):
            errors.append(
                f"Missing or disabled payroll trust schema object: "
                f"trigger {trig_name} on {tbl_schema}.{tbl_name} ({phase}). "
                f"Run migrations or re-enable the trigger."
            )

    # P3a presence and shape are always required, including in production.
    for schema, table, column in _P3A_COLUMNS:
        if not _column_exists(cur, schema, table, column):
            errors.append(f"Missing P3a currency column {schema}.{table}.{column}. Run migrations.")
    for schema, table, name, kind in _P3A_CONSTRAINTS:
        cur.execute("""
            SELECT 1 FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = %s AND rel.relname = %s
              AND con.conname = %s AND con.contype = %s AND con.convalidated
        """, (schema, table, name, kind))
        if cur.fetchone() is None:
            errors.append(f"Missing or unvalidated P3a currency constraint {schema}.{table}.{name}.")
    for name in ("fn_company_has_durable_monetary_state", "fn_guard_company_currency_change"):
        if not _function_exists(cur, "core", name):
            errors.append(f"Missing P3a currency function core.{name}().")
    if not _trigger_exists_and_enabled(cur, "trg_companies_currencychange", "core", "companies"):
        errors.append("Missing or disabled P3a Company currency-change trigger.")

    return errors


def run_schema_guard(database_url: str, is_dev: bool) -> None:
    """
    Entry point — called once at startup.

    Legacy / security checks (Alembic version, sec.* tables, sec.* functions,
    security triggers):
        dev  → RuntimeError
        prod → log.warning   (deployment pipeline is responsible)

    Payroll trust checks (Phases 1–11 columns / indexes / functions / triggers):
        ALWAYS → RuntimeError, regardless of is_dev.
        Payroll trust protections are not optional in any environment.
    """
    sync_dsn = _build_sync_dsn(database_url)

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------
    try:
        conn = psycopg2.connect(sync_dsn, connect_timeout=5)
        conn.autocommit = True
    except OperationalError as exc:
        msg = (
            f"Schema guard: cannot connect to the database.\n"
            f"  DSN : {_redact(sync_dsn)}\n"
            f"  Error: {exc}\n\n"
            f"Make sure PostgreSQL is running and the DATABASE_URL in backend/.env is correct.\n"
            f"If pg_hba.conf was recently edited, restart PostgreSQL to reload it."
        )
        if is_dev:
            raise RuntimeError(msg) from exc
        log.error(msg)
        return

    cur = conn.cursor()
    errors: list[str] = []              # legacy/security — dev: hard, prod: warning
    pt_errors: list[str] = []           # payroll trust    — ALWAYS hard
    temporal_errors: list[str] = []     # timezone/instant authority — ALWAYS hard

    # ------------------------------------------------------------------
    # 1. Alembic version check
    # ------------------------------------------------------------------
    script_head = _get_script_head()
    try:
        cur.execute("SELECT version_num FROM alembic_version")
        rows = cur.fetchall()
        db_versions = {r[0] for r in rows}
    except Exception as exc:
        errors.append(
            f"Cannot read alembic_version table: {exc}. "
            f"Has `alembic upgrade head` ever been run against this database?"
        )
        db_versions = set()

    if script_head and db_versions and script_head not in db_versions:
        errors.append(
            f"Alembic version mismatch:\n"
            f"  Script head : {script_head}\n"
            f"  DB current  : {', '.join(sorted(db_versions))}\n"
            f"  Fix: cd Payroll_App_v3 && python -m alembic upgrade head"
        )

    # ------------------------------------------------------------------
    # 2. Required tables
    # ------------------------------------------------------------------
    missing_tables: list[str] = []
    for qualified in _REQUIRED_TABLES:
        schema, table = qualified.split(".")
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        if not cur.fetchone():
            missing_tables.append(qualified)
    if missing_tables:
        errors.append(f"Missing tables: {missing_tables}")

    required_columns = {
        ("core", "supportedtimezones", "timezoneid"),
        ("core", "branches", "timezoneid"),
        ("core", "drivers", "effectivefromat"),
        ("core", "drivers", "effectivetoat"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivefromatsnapshot"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivetoatsnapshot"),
        ("core", "drivertransferrequests", "effectiveat"),
        ("payroll", "payrollperioddrivereligibility", "branchtimezoneidsnapshot"),
        ("payroll", "payrollperioddrivereligibility", "eligibleworkdates"),
    }
    missing_columns = []
    for schema, table, column in required_columns:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
            (schema, table, column),
        )
        if not cur.fetchone():
            missing_columns.append(f"{schema}.{table}.{column}")
    if missing_columns:
        temporal_errors.append(f"Missing branch-timezone/instant columns: {missing_columns}")

    cur.execute("""
        SELECT table_schema, table_name, column_name,
               is_nullable, column_default, data_type,
               character_maximum_length, udt_name
        FROM information_schema.columns
        WHERE (table_schema = 'core' AND table_name = 'branches' AND column_name = 'timezoneid')
           OR (table_schema = 'core' AND table_name = 'supportedtimezones' AND column_name = 'timezoneid')
           OR (table_schema = 'core' AND table_name = 'drivers' AND column_name IN ('effectivefromat','effectivetoat'))
           OR (table_schema = 'core' AND table_name = 'drivertransferrequests' AND column_name = 'effectiveat')
           OR (table_schema = 'payroll' AND table_name = 'payrollperioddrivereligibility'
               AND column_name IN ('drivereffectivefromatsnapshot','drivereffectivetoatsnapshot','branchtimezoneidsnapshot','eligibleworkdates'))
    """)
    temporal_shapes = {(row[0], row[1], row[2]): row[3:] for row in cur.fetchall()}
    # Registry and Branch timezone columns share a database name; alias the
    # registry shape separately so each authority is checked independently.
    cur.execute("""
        SELECT is_nullable, data_type, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = 'core' AND table_name = 'supportedtimezones'
          AND column_name = 'timezoneid'
    """)
    registry_zone_shape = cur.fetchone()
    if registry_zone_shape is None or registry_zone_shape[0] != "NO" \
       or registry_zone_shape[1] != "character varying" or registry_zone_shape[2] != 100:
        temporal_errors.append("core.SupportedTimeZones.TimeZoneId must be a NOT NULL VARCHAR(100) primary key.")

    required_not_null = (
        ("core", "branches", "timezoneid"),
        ("core", "drivers", "effectivefromat"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivefromatsnapshot"),
        ("payroll", "payrollperioddrivereligibility", "branchtimezoneidsnapshot"),
        ("payroll", "payrollperioddrivereligibility", "eligibleworkdates"),
    )
    for key in required_not_null:
        shape = temporal_shapes.get(key)
        if shape is None or shape[0] != "NO":
            temporal_errors.append(f"Temporal authority column {'.'.join(key)} must be NOT NULL.")
    branch_zone_shape = temporal_shapes.get(("core", "branches", "timezoneid"))
    if branch_zone_shape and branch_zone_shape[1] is not None:
        temporal_errors.append("core.Branches.TimeZoneId must not have a default.")
    for key in (("core", "branches", "timezoneid"),
                ("payroll", "payrollperioddrivereligibility", "branchtimezoneidsnapshot")):
        shape = temporal_shapes.get(key)
        if shape and (shape[2] != "character varying" or shape[3] != 100):
            temporal_errors.append(f"Temporal timezone column {'.'.join(key)} must be VARCHAR(100).")
    for key in (
        ("core", "drivers", "effectivetoat"),
        ("core", "drivertransferrequests", "effectiveat"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivetoatsnapshot"),
    ):
        shape = temporal_shapes.get(key)
        if shape and shape[0] != "YES":
            temporal_errors.append(f"Temporal boundary column {'.'.join(key)} must remain nullable.")
    for key in (
        ("core", "drivers", "effectivefromat"),
        ("core", "drivers", "effectivetoat"),
        ("core", "drivertransferrequests", "effectiveat"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivefromatsnapshot"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivetoatsnapshot"),
    ):
        shape = temporal_shapes.get(key)
        if shape and shape[2] != "timestamp with time zone":
            temporal_errors.append(f"Temporal authority column {'.'.join(key)} must be TIMESTAMPTZ.")
    work_dates_shape = temporal_shapes.get(("payroll", "payrollperioddrivereligibility", "eligibleworkdates"))
    if work_dates_shape and (work_dates_shape[2] != "ARRAY" or work_dates_shape[4] != "_date"):
        temporal_errors.append("PayrollPeriodDriverEligibility.EligibleWorkDates must be DATE[].")

    required_temporal_constraints = [
        ("core", "branches", "fk_branches_timezone", "f"),
        ("core", "drivers", "ck_drivers_effectivewindowat", "c"),
        ("core", "drivers", "ck_drivers_closedhistoryat", "c"),
        ("core", "drivers", "excl_drivers_employee_assignmentwindow", "x"),
        ("payroll", "payrollperioddrivereligibility", "fk_ppde_snapshottimezone", "f"),
        ("core", "drivertransferrequests", "ck_dtr_completedeffectiveat", "c"),
    ]
    for schema, table, name, kind in required_temporal_constraints:
        cur.execute("""
            SELECT con.condeferrable, con.condeferred FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = %s AND rel.relname = %s
              AND con.conname = %s AND con.contype = %s AND con.convalidated
        """, (schema, table, name, kind))
        constraint_shape = cur.fetchone()
        if constraint_shape is None:
            temporal_errors.append(f"Missing or unvalidated branch-timezone/instant constraint {schema}.{table}.{name}.")
        elif name == "excl_drivers_employee_assignmentwindow" and constraint_shape != (True, True):
            temporal_errors.append(
                "core.Drivers.excl_Drivers_Employee_AssignmentWindow must be "
                "DEFERRABLE INITIALLY DEFERRED."
            )

    retired_columns = [
        ("core", "drivers", "effectivefrom"),
        ("core", "drivers", "effectiveto"),
        ("core", "drivertransferrequests", "effectivedate"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivefromsnapshot"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivetosnapshot"),
    ]
    for schema, table, column in retired_columns:
        if _column_exists(cur, schema, table, column):
            temporal_errors.append(f"Retired date-based assignment authority still exists: {schema}.{table}.{column}")

    retired_column = ("core", "companies", "timezonename")
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
        retired_column,
    )
    if cur.fetchone():
        temporal_errors.append("Retired Company timezone column still exists: core.Companies.TimeZoneName")

    cur.execute(
        "SELECT 1 FROM information_schema.routines "
        "WHERE routine_schema = 'core' AND LOWER(routine_name) = 'fn_companytoday'"
    )
    if cur.fetchone():
        temporal_errors.append("Retired Company clock function still exists: core.fn_CompanyToday")
    for signature, label in (
        ("core.fn_effectivedriverprofile(integer,integer,date)", "core.fn_EffectiveDriverProfile(INT, INT, DATE)"),
        ("core.fn_drivereffectiverange(date,date)", "core.fn_DriverEffectiveRange(DATE, DATE)"),
    ):
        cur.execute("SELECT to_regprocedure(%s)", (signature,))
        if cur.fetchone()[0] is not None:
            temporal_errors.append(f"Retired date-based temporal function still exists: {label}.")
    for signature, label in (
        ("core.fn_businessdate(text,timestamp with time zone)", "core.fn_BusinessDate(TEXT, TIMESTAMPTZ)"),
        ("core.fn_branchtoday(integer,integer,timestamp with time zone)", "core.fn_BranchToday(INT, INT, TIMESTAMPTZ)"),
        ("core.fn_issupportedtimezone(text)", "core.fn_IsSupportedTimeZone(TEXT)"),
        ("core.fn_localdateboundary(text,date)", "core.fn_LocalDateBoundary(TEXT, DATE)"),
        ("core.fn_workdaterange(text,date,date)", "core.fn_WorkDateRange(TEXT, DATE, DATE)"),
        ("core.fn_branchworkdaterange(integer,integer,date,date)", "core.fn_BranchWorkDateRange(INT, INT, DATE, DATE)"),
        ("core.fn_drivercoversworkdate(integer,integer,integer,date)", "core.fn_DriverCoversWorkDate(INT, INT, INT, DATE)"),
        ("core.fn_effectivedriverprofile(integer,integer,timestamp with time zone)", "core.fn_EffectiveDriverProfile(INT, INT, TIMESTAMPTZ)"),
        ("core.fn_drivereffectiverange(timestamp with time zone,timestamp with time zone)", "core.fn_DriverEffectiveRange(TIMESTAMPTZ, TIMESTAMPTZ)"),
    ):
        cur.execute("SELECT to_regprocedure(%s)", (signature,))
        if cur.fetchone()[0] is None:
            temporal_errors.append(f"Missing required temporal function: {label}.")
    if not _trigger_exists_and_enabled(cur, "trg_drivers_historyimmutable", "core", "drivers"):
        temporal_errors.append("Missing or disabled temporal history trigger core.trg_Drivers_HistoryImmutable.")

    # ------------------------------------------------------------------
    # 3. Required functions (security layer)
    # ------------------------------------------------------------------
    missing_fns: list[str] = []
    for qualified in _REQUIRED_FUNCTIONS:
        schema, fn = qualified.split(".")
        cur.execute(
            "SELECT 1 FROM information_schema.routines "
            "WHERE routine_schema = %s AND LOWER(routine_name) = LOWER(%s)",
            (schema, fn),
        )
        if not cur.fetchone():
            missing_fns.append(qualified)
    if missing_fns:
        errors.append(f"Missing functions: {missing_fns}")

    # ------------------------------------------------------------------
    # 4. Required triggers (security layer)
    # ------------------------------------------------------------------
    missing_trig: list[str] = []
    for trig_name in _REQUIRED_TRIGGERS:
        cur.execute(
            "SELECT 1 FROM information_schema.triggers "
            "WHERE LOWER(trigger_name) = LOWER(%s)",
            (trig_name,),
        )
        if not cur.fetchone():
            missing_trig.append(trig_name)
    if missing_trig:
        errors.append(f"Missing triggers: {missing_trig}")

    # ------------------------------------------------------------------
    # 5. Payroll trust guard (Phases 1–11) — ALWAYS hard failure
    # ------------------------------------------------------------------
    pt_errors = _check_payroll_trust(cur)

    if temporal_errors:
        temporal_errors.extend(pt_errors)
    else:
        temporal_errors = pt_errors

    cur.close()
    conn.close()

    # ------------------------------------------------------------------
    # 6. Report
    # ------------------------------------------------------------------
    # ── 6a. Payroll trust and temporal-authority errors are always fatal ─
    if temporal_errors:
        pt_message = (
            "\n\n"
            "========================================================\n"
            "  PAYROLL TRUST OR TEMPORAL AUTHORITY SCHEMA IS INVALID\n"
            "  Startup blocked in all environments.\n"
            "========================================================\n"
            + "\n".join(f"  * {e}" for e in temporal_errors)
            + "\n\n"
            "  Run:  cd Payroll_App_v3 && python -m alembic upgrade head\n"
            "  Then restart the backend.\n"
            "========================================================\n"
        )
        raise RuntimeError(pt_message)

    # ── 6b. Legacy / security errors: dev=hard, prod=warning ───────────
    if not errors:
        log.info(
            "Schema guard: DB is at head (%s), all required objects present.",
            script_head or "unknown",
        )
        return

    legacy_message = (
        "\n\n"
        "========================================================\n"
        "  DATABASE SCHEMA IS NOT UP TO DATE\n"
        "========================================================\n"
        + "\n".join(f"  * {e}" for e in errors)
        + "\n\n"
        "  Run:  cd Payroll_App_v3 && python -m alembic upgrade head\n"
        "  Then restart the backend.\n"
        "========================================================\n"
    )

    if is_dev:
        raise RuntimeError(legacy_message)
    else:
        log.warning(legacy_message)


def _redact(dsn: str) -> str:
    """Remove password from DSN for safe logging."""
    return re.sub(r":[^:@]+@", ":***@", dsn)
