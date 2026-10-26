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
    "core.drivers",
    "core.driverassignments",
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

# Current source identity and immutable component projection.
_PT_INDEXES = [
    ("payroll", "ux_draftlines_targetbusinesskey", "Target source identity (0085)"),
    ("payroll", "ix_payrollfinallines_snapshot", "Component projection (0089)"),
    ("payroll", "uix_branchpaydefinitionconfig_opendefinitionversion", "Branch applicability (0093)"),
]
_PT_COLUMNS = [
    ("payroll", "payrollfinallines", "sourcesnapshot", "Phase 9 (0039)"),
    ("payroll", "payrollfinallines", "payrollcalculationsnapshotcomponentid", "Component projection (0089)"),
    ("payroll", "payrolldraftlines", "payrollperioddefinitionid", "Target source identity (0085)"),
    ("payroll", "payrolldraftlines", "needsmanagerreview", "Source workflow"),
    ("payroll", "branchpaydefinitionconfig", "paydefinitionid", "Branch applicability (0093)"),
]
_PT_FUNCTIONS = [
    ("payroll", "fn_guard_final_line_immutable", "Phase 3C (0035)"),
    ("payroll", "fn_guard_period_status_revert", "Phase 3C (0035)"),
    ("payroll", "fn_guard_final_line_insert", "Phase 6 (0038)"),
]
_PT_TRIGGERS = [
    ("trg_final_line_immutable", "payroll", "payrollfinallines", "Phase 3C (0035)"),
    ("trg_period_status_revert", "payroll", "payrollperiods", "Phase 3C (0035)"),
    ("trg_guard_final_line_insert", "payroll", "payrollfinallines", "Phase 6 (0038)"),
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
        # Match plain (`revision = "X"`) and any type-annotated form (`revision: str = "X"`,
        # `down_revision: Union[str, None] = "X"`, `down_revision: str | None = "X"`).
        rev_m = re.search(r'^revision(?:\s*:[^=\n]+)?\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        down_m = re.search(r'^down_revision(?:\s*:[^=\n]+)?\s*=\s*(?:["\']([^"\']*)["\']|None)', text, re.MULTILINE)
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

# Immutable calculation evidence (migrations 0089, 0090 and 0091): the tables of the evidence
# shape, the columns the writer and the readers depend on, and the objects that protect them.
_EVIDENCE_TABLES = (
    "payrollcalculationsnapshotcomponents",
    "payrollcalculationsnapshotminmaxevaluations",
    "payrollcalculationsnapshotminmaxrules",
    "payrollcalculationsnapshotusedratecomponents",
)
_EVIDENCE_COLUMNS = (
    ("payrollcalculationsnapshots", "periodstartdate"),
    ("payrollcalculationsnapshots", "periodenddate"),
    ("payrollcalculationsnapshots", "payableroundingversion"),
    ("payrollcalculationsnapshots", "minmaxprorationversion"),
    ("payrollcalculationsnapshots", "scheduledworkdates"),
    ("payrollcalculationsnapshots", "warnings"),
    ("payrollcalculationdrivertotals", "employeeid"),
    ("payrollcalculationsnapshotlines", "payrollcalculationsnapshotcomponentid"),
    ("payrollcalculationsnapshotlines", "driverratescheduleid"),
    ("payrollcalculationsnapshotlines", "payrollperioddefinitionid"),
    ("payrollcalculationsnapshotusedratedefinitions", "driverratescheduleid"),
    ("payrollfinallines", "payrollcalculationsnapshotid"),
    ("payrollfinallines", "payrollcalculationsnapshotcomponentid"),
    ("payrollfinallines", "employeeid"),
    ("payrollfinallines", "componentkind"),
    # Calculation contract v3 (migration 0090): Status lines and entries.
    ("payrollcalculationsnapshotusedratedefinitions", "statusratecolumnid"),
    ("payrollcalculationsnapshotstatusentries", "statushoursvaluesnapshot"),
    ("payrollcalculationsnapshotstatusentries", "statuscategorysnapshot"),
    # Calculation contract v4 (migration 0091): the shape and tier values of a used rate.
    ("payrollcalculationsnapshotusedratedefinitions", "rateshapesnapshot"),
    ("payrollcalculationsnapshotusedratecomponents", "amountsnapshot"),
    ("paydefinitionrequests", "ordinaltiers"),
)
_EVIDENCE_RETIRED_COLUMNS = (
    ("payrollcalculationsnapshotlines", "payitemid"),
    ("payrollcalculationsnapshotusedratedefinitions", "payitemid"),
    ("payrollcalculationsnapshotusedratedefinitions", "driverpayruleid"),
    ("payrollcalculationsnapshotlines", "ratetypeid"),
    ("payrollcalculationsnapshotlines", "driverrateid"),
    ("payrollcalculationsnapshotusedratedefinitions", "ratetypeid"),
    ("payrollcalculationsnapshotusedratedefinitions", "driverrateid"),
)
_EVIDENCE_IMMUTABLE_TRIGGERS = (
    ("trg_payrollcalculationsnapshotcomponents_immutable", "payrollcalculationsnapshotcomponents"),
    ("trg_payrollcalculationsnapshotminmaxevaluations_immutable", "payrollcalculationsnapshotminmaxevaluations"),
    ("trg_payrollcalculationsnapshotminmaxrules_immutable", "payrollcalculationsnapshotminmaxrules"),
    ("trg_payrollcalculationsnapshotusedratecomponents_immutable",
     "payrollcalculationsnapshotusedratecomponents"),
    ("trg_payrollcalculationsnapshotusedratecomponents_shape",
     "payrollcalculationsnapshotusedratecomponents"),
    ("trg_payrollcalculationsnapshotusedratedefinitions_tiers",
     "payrollcalculationsnapshotusedratedefinitions"),
)
_EVIDENCE_FUNCTIONS = (
    "payroll.fn_iscanonicaldateset(date[])",
)


def _check_calculation_evidence(cur) -> list[str]:
    errors: list[str] = []
    for table in _EVIDENCE_TABLES:
        cur.execute("SELECT to_regclass(%s)", (f"payroll.{table}",))
        if cur.fetchone()[0] is None:
            errors.append(f"Missing calculation evidence table payroll.{table}")
    for table, column in _EVIDENCE_COLUMNS:
        if not _column_exists(cur, "payroll", table, column):
            errors.append(f"Missing calculation evidence column payroll.{table}.{column}")
    for table, column in _EVIDENCE_RETIRED_COLUMNS:
        if _column_exists(cur, "payroll", table, column):
            errors.append(f"Retired calculation evidence column still exists: payroll.{table}.{column}")
    for trigger, table in _EVIDENCE_IMMUTABLE_TRIGGERS:
        if not _trigger_exists_and_enabled(cur, trigger, "payroll", table):
            errors.append(f"Missing or disabled calculation evidence immutability trigger {trigger}.")
    for signature in _EVIDENCE_FUNCTIONS:
        cur.execute("SELECT to_regprocedure(%s)", (signature,))
        if cur.fetchone()[0] is None:
            errors.append(f"Missing calculation evidence function {signature}.")
    cur.execute(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
        "WHERE c.conrelid = to_regclass('payroll.payrollcalculationsnapshots') "
        "AND c.conname = 'ck_payrollcalculationsnapshots_calculationversion'"
    )
    version_check = cur.fetchone()
    if version_check is None or "payroll-calculation-v4" not in version_check[0]:
        errors.append("The calculation snapshot version check is not payroll-calculation-v4.")
    return errors


# Company Status catalog (migration 0090): Company-owned Status keys with aliases, Branch usage
# limits, Company-owned Status rate columns, and the objects that protect them.
_STATUS_CATALOG_TABLES = (
    "payrollstatuskeyaliases",
    "payrollstatuskeybranchlimits",
)
_STATUS_CATALOG_COLUMNS = (
    ("payrollstatuskeys", "normalizedkeyname"),
    ("payrollstatuskeyaliases", "normalizedalias"),
    ("payrollstatuskeybranchlimits", "branchid"),
    ("driverrateschedules", "statusratecolumnid"),
)
_STATUS_CATALOG_RETIRED_COLUMNS = (
    ("payrollstatuskeys", "branchid"),
    ("payrollstatuskeys", "limitusesperperiodenabled"),
    ("statusratecolumns", "branchid"),
    ("statusratecolumns", "ratetypeid"),
    ("ratedefinitions", "ownerbranchid"),
)
_STATUS_CATALOG_TRIGGERS = (
    ("trg_payrollstatuskeys_name", "payrollstatuskeys"),
    ("trg_payrollstatuskeys_semantics", "payrollstatuskeys"),
    ("trg_payrollstatuskeyaliases_alias", "payrollstatuskeyaliases"),
    ("trg_payrolldraftlines_statusoffwork", "payrolldraftlines"),
    ("trg_payrollperioddriverdayentrystate_statusoffwork", "payrollperioddriverdayentrystate"),
)
_STATUS_CATALOG_FUNCTIONS = (
    "payroll.fn_lockstatuscatalog(integer)",
    "payroll.fn_createstatusratecolumn(integer,character varying,character varying,boolean,integer)",
    "payroll.fn_ensuredefaultstatusratecolumn(integer)",
    "payroll.fn_guard_status_off_work()",
)


def _check_company_status_catalog(cur) -> list[str]:
    errors: list[str] = []
    for table in _STATUS_CATALOG_TABLES:
        cur.execute("SELECT to_regclass(%s)", (f"payroll.{table}",))
        if cur.fetchone()[0] is None:
            errors.append(f"Missing Company Status catalog table payroll.{table}")
    for table, column in _STATUS_CATALOG_COLUMNS:
        if not _column_exists(cur, "payroll", table, column):
            errors.append(f"Missing Company Status catalog column payroll.{table}.{column}")
    for table, column in _STATUS_CATALOG_RETIRED_COLUMNS:
        if _column_exists(cur, "payroll", table, column):
            errors.append(f"Retired Branch-owned Status column still exists: payroll.{table}.{column}")
    for trigger, table in _STATUS_CATALOG_TRIGGERS:
        if not _trigger_exists_and_enabled(cur, trigger, "payroll", table):
            errors.append(f"Missing or disabled Company Status catalog trigger {trigger}.")
    for signature in _STATUS_CATALOG_FUNCTIONS:
        cur.execute("SELECT to_regprocedure(%s)", (signature,))
        if cur.fetchone()[0] is None:
            errors.append(f"Missing Company Status catalog function {signature}.")
    return errors


# Physical authority after 0093: removed predecessors cannot silently reappear.
_RETIRED_TABLES = (
    "sec.roles", "sec.rolepermissions", "payroll.branchpayitemconfig",
    "payroll.payitems", "payroll.ratetypes", "payroll.payitemratetypemap",
    "payroll.payitemrateslots", "payroll.payitemlinetypemap",
    "payroll.driverrates", "payroll.driverratetiers",
    "payroll.cdpidefinitions", "payroll.cdpirequests", "payroll.cdpirequestevents",
    "import.importbatches", "import.importfiles", "import.importrows",
    "import.importdetectedcolumns", "import.importmappingtemplates",
    "import.importmappingtemplatelines", "import.importvalidationissues",
    "integration.integrationconnections", "integration.integrationsyncruns",
    "app.vw_importbatchsummary",
)
_RETIRED_COLUMNS = (
    ("sec", "userbranchroles", "roleid"),
    ("core", "employees", "employeetype"),
    ("payroll", "branchpaydefinitionconfig", "payitemid"),
    ("payroll", "payrollperiodauditevidenceevents", "payitemid"),
) + tuple(("payroll", "payrolldraftlines", column) for column in (
    "rateamount", "calculatedamount", "linetype", "sourceid", "sourcesnapshot",
)) + tuple(("payroll", "payrollfinallines", column) for column in (
    "payitemid", "ratetypeid", "driverrateid", "resolvedrateamount", "ratebehavior",
    "draftlineid", "bonuseventid", "workdate", "quantity", "rateamount", "notes",
))


_IMPORT_TABLES = (
    "imports", "importsourcerecords", "importissues", "filesourcebatches", "filesources",
    "filesourceunits", "sourcelayouts", "filesourcerecords", "payrollworkimports",
    "payrollworkrecords", "payrollworkpreviewgroups", "payrollworkdrivermatches",
    "payrollworkrecordidentifiers",
)


def _check_import_foundation(cur) -> list[str]:
    errors: list[str] = []
    for table in _IMPORT_TABLES:
        cur.execute("SELECT to_regclass(%s)", (f"import.{table}",))
        if cur.fetchone()[0] is None:
            errors.append(f"Missing Import foundation table import.{table} (0095).")
        triggers = (("trg_imports_envelope", "fn_guard_import_envelope", 31),
                    ("trg_imports_notruncate", "fn_guard_import_envelope", 34),
                    ("trg_imports_payrollwork", "fn_guard_payroll_work_envelope", 19)) if table == "imports" else (
                        ("trg_importdetail_writable", "fn_guard_payroll_work_detail", 31),
                        ("trg_importdetail_notruncate", "fn_guard_payroll_work_detail", 34))
        for trigger, function, events in triggers:
            cur.execute("""
                SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE t.tgrelid = to_regclass(%s) AND t.tgname = %s
                  AND t.tgenabled IN ('O','A') AND t.tgtype = %s
                  AND n.nspname = 'import' AND p.proname = %s
            """, (f"import.{table}", trigger, events, function))
            if cur.fetchone() is None:
                errors.append(f"Missing or disabled Import guard {table}.{trigger} -> {function} (0095).")
    for obsolete in ("payrollworkcells", "payrollworkstatusremovals"):
        cur.execute("SELECT to_regclass(%s)", (f"import.{obsolete}",))
        if cur.fetchone()[0] is not None:
            errors.append(f"Retired per-cell Import authority remains: import.{obsolete} (0095).")
    for table, columns in (
        ("imports", ("companyid", "importkind", "sourcekind", "revision", "contractversion", "idempotencykey", "creationfingerprint")),
        ("filesourcebatches", ("importid", "companyid", "contractversion", "sourcemanifest")),
        ("filesources", ("filesourceid", "importid", "companyid", "fileordinal", "contractversion", "filehashsha256", "filesizebytes")),
        ("filesourceunits", ("sourceunitid", "filesourceid", "companyid", "importid", "unitname", "unitordinal", "unitkind", "isselected", "headerrownumber", "datesource")),
        ("sourcelayouts", ("sourcelayoutid", "companyid", "importid", "structurefingerprint")),
        ("payrollworkimports", ("companyid", "branchid", "payrollperiodid", "contractversion", "canonicalizationversion")),
        ("payrollworkrecords", ("previewgroupid", "rowfingerprint", "valuesjson", "issuesjson", "contextjson", "branchid", "payrollperiodid")),
        ("filesourcerecords", ("sourcerecordid", "sourceunitid", "sourcerownumber")),
        ("payrollworkrecordidentifiers", ("sourcerecordid", "sourcecolumnindex", "drivermatchid")),
    ):
        for column in columns:
            cur.execute("""SELECT attnotnull FROM pg_attribute WHERE attrelid = to_regclass(%s)
                AND attname = %s AND NOT attisdropped""", (f"import.{table}", column))
            if cur.fetchone() != (True,):
                errors.append(f"Import foundation requires NOT NULL {table}.{column} (0095).")
    for table, columns in (
        ("imports", ("payrollperiodid", "driverid", "sheetname", "filecontent", "columnmapping")),
        ("payrollworkimports", ("columnmapping", "sourcemanifest")),
        ("filesources", ("sheetname", "headerrownumber", "dateorder")),
    ):
        cur.execute("""SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s)
            AND attname = ANY(%s::text[]) AND NOT attisdropped""", (f"import.{table}", list(columns)))
        if cur.fetchall():
            errors.append(f"Obsolete source/target ownership on import.{table} (0095).")
    for table, column, expected in (
        ("filesourceunits", "dateorder", "character varying(3)"),
        ("filesourceunits", "datesource", "jsonb"),
        ("sourcelayouts", "columnmapping", "jsonb"),
        ("filesourcebatches", "sourcemanifest", "jsonb"),
        ("payrollworkimports", "detailpurgedatutc", "timestamp with time zone"),
        ("payrollworkimports", "mappingfingerprint", "text"),
        ("payrollworkimports", "contentfingerprint", "text"),
        ("payrollworkrecords", "valuesjson", "jsonb"),
        ("payrollworkrecords", "issuesjson", "jsonb"),
        ("payrollworkrecords", "isexcluded", "boolean"),
    ):
        cur.execute("""SELECT format_type(atttypid, atttypmod) FROM pg_attribute
            WHERE attrelid = to_regclass(%s) AND attname = %s AND NOT attisdropped""", (f"import.{table}", column))
        if cur.fetchone() != (expected,):
            errors.append(f"Missing or drifted Import contract column {table}.{column} (0095).")
    for table, constraint, required in (
        ("filesourceunits", "ck_filesourceunits_dateorder", ("'DMY'", "'MDY'", "'YMD'")),
        ("filesources", "ck_filesources_inspectionpurge", ("inspectionmetadata",)),
    ):
        cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = to_regclass(%s)
            AND conname = %s AND contype = 'c' AND convalidated""", (f"import.{table}", constraint))
        row = cur.fetchone()
        if row is None or any(token not in row[0] for token in required):
            errors.append(f"Missing or drifted Import contract check {table}.{constraint} (0095).")
    scope = ["importid", "companyid"]
    target = ["importid", "companyid", "branchid", "payrollperiodid"]
    source = ["sourcerecordid", "importid", "companyid"]
    ownership = (
        ("imports", ["branchid", "companyid"], "core.branches", ["branchid", "companyid"]),
        ("filesourcebatches", scope, "import.imports", scope),
        ("filesources", scope, "import.filesourcebatches", scope),
        ("filesourceunits", ["filesourceid", *scope], "import.filesources", ["filesourceid", *scope]),
        ("filesourceunits", ["sourcelayoutid", *scope], "import.sourcelayouts", ["sourcelayoutid", *scope]),
        ("sourcelayouts", scope, "import.imports", scope),
        ("filesourcerecords", ["sourceunitid", *scope], "import.filesourceunits", ["sourceunitid", *scope]),
        ("filesourcerecords", source, "import.importsourcerecords", source),
        ("payrollworkimports", ["payrollperiodid", "companyid", "branchid"], "payroll.payrollperiods", ["payrollperiodid", "companyid", "branchid"]),
        ("payrollworkpreviewgroups", target, "import.payrollworkimports", target),
        ("payrollworkrecords", target, "import.payrollworkimports", target),
        ("payrollworkrecords", source, "import.importsourcerecords", source),
        ("payrollworkrecords", ["previewgroupid", *scope], "import.payrollworkpreviewgroups", ["previewgroupid", *scope]),
        ("payrollworkrecords", ["driverassignmentid", "companyid", "branchid"], "core.driverassignments", ["driverassignmentid", "companyid", "branchid"]),
        ("payrollworkrecords", ["driverid", "companyid"], "core.drivers", ["driverid", "companyid"]),
        ("payrollworkrecords", ["payrollperiodid", "workdate"], "payroll.payrollperioddays", ["payrollperiodid", "workdate"]),
        ("payrollworkdrivermatches", ["driverid", "companyid"], "core.drivers", ["driverid", "companyid"]),
        ("payrollworkrecordidentifiers", source, "import.payrollworkrecords", source),
        ("payrollworkrecordidentifiers", ["drivermatchid", *scope], "import.payrollworkdrivermatches", ["drivermatchid", *scope]),
    )
    for table, columns, parent, parent_columns in ownership:
        cur.execute("""
            SELECT 1 FROM pg_constraint c WHERE c.conrelid = to_regclass(%s)
              AND c.confrelid = to_regclass(%s) AND c.contype = 'f' AND c.convalidated
              AND ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(num, pos)
                        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.num ORDER BY k.pos) = %s::text[]
              AND ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY k(num, pos)
                        JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.num ORDER BY k.pos) = %s::text[]
        """, (f"import.{table}", parent, columns, parent_columns))
        if cur.fetchone() is None:
            errors.append(f"Missing validated Import ownership FK {table}({', '.join(columns)}) -> {parent} (0095).")
    for table, index_name, columns in (
        ("filesourceunits", "ix_filesourceunits_layout", ["sourcelayoutid", "importid", "companyid"]),
        ("payrollworkrecords", "ix_payrollworkrecords_group", ["previewgroupid", "importid", "companyid", "sourcerecordid"]),
        ("payrollworkrecords", "ix_payrollworkrecords_import", ["importid", "companyid"]),
        ("importsourcerecords", "ix_importsourcerecords_import", ["importid", "companyid", "sourcerecordid"]),
        ("payrollworkrecordidentifiers", "ix_payrollworkrecordidentifiers_matchscope", ["drivermatchid", "importid", "companyid"]),
        ("importissues", "ix_importissues_sourcerecordscope", ["sourcerecordid", "importid", "companyid"]),
    ):
        method = "brin" if index_name == "ix_payrollworkrecords_import" else "btree"
        cur.execute("""SELECT 1 FROM pg_index i
            JOIN pg_class x ON x.oid=i.indexrelid JOIN pg_am am ON am.oid=x.relam
            WHERE i.indrelid=to_regclass(%s) AND x.relname=%s AND am.amname=%s
              AND i.indisvalid AND i.indisready AND i.indpred IS NULL AND i.indexprs IS NULL
              AND ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(num,pos)
                  JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num
                  WHERE k.pos <= i.indnkeyatts ORDER BY k.pos)=%s::text[]""",
                    (f"import.{table}", index_name, method, columns))
        if cur.fetchone() is None:
            errors.append(f"Missing or drifted Import lookup index {table}.{index_name} (0095).")
    cur.execute("SELECT prosrc FROM pg_proc WHERE oid = to_regprocedure('import.fn_guard_payroll_work_detail()')")
    row = cur.fetchone()
    if row is None or any(token not in row[0].lower() for token in (
        "import_payload_scope", "valuesjson", "payrollperioddefinitions", "previewgroupid",
        "detailpurgedatutc", "import_manifest_scope", "import_purge_scope",
    )):
        errors.append("Missing compact Import payload/retention ownership validation (0095).")
    return errors


def _check_target_physical_authorities(cur) -> list[str]:
    errors: list[str] = []
    for qualified in _RETIRED_TABLES:
        cur.execute("SELECT to_regclass(%s)", (qualified,))
        if cur.fetchone()[0] is not None:
            errors.append(f"Retired physical authority still exists: {qualified}")
    for schema, table, column in _RETIRED_COLUMNS:
        if _column_exists(cur, schema, table, column):
            errors.append(f"Retired physical column still exists: {schema}.{table}.{column}")
    for schema, table, column in (
        ("sec", "userbranchroles", "companyroleid"),
        ("payroll", "payrolldraftlines", "payrollperioddefinitionid"),
    ):
        cur.execute("""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = %s
        """, (schema, table, column))
        row = cur.fetchone()
        if row is None or row[0] != "NO":
            errors.append(f"Target identity must be NOT NULL: {schema}.{table}.{column}")
    # Source facts never store derived review state: consumers keep reading the column,
    # but the database admits only a non-null FALSE (0001 NOT NULL, 0093 CHECK).
    cur.execute("""
        SELECT a.attnotnull, pg_get_constraintdef(c.oid)
        FROM pg_attribute a
        LEFT JOIN pg_constraint c
          ON c.conrelid = a.attrelid AND c.conname = 'ck_draftlines_nostoredreview'
         AND c.contype = 'c' AND c.convalidated
        WHERE a.attrelid = to_regclass('payroll.payrolldraftlines')
          AND a.attname = 'needsmanagerreview' AND NOT a.attisdropped
    """)
    row = cur.fetchone()
    if (row is None or row[0] is not True or row[1] is None
            or re.sub(r"[\s()]", "", row[1]).lower() != "checkneedsmanagerreview=false"):
        errors.append("Missing or drifted source-fact invariant ck_DraftLines_NoStoredReview: "
                      "payroll.PayrollDraftLines.NeedsManagerReview must be NOT NULL and FALSE.")
    cur.execute("SELECT 1 FROM sec.Permissions WHERE PermissionCode = 'drivers.manage'")
    if cur.fetchone() is not None:
        errors.append("Retired drivers.manage permission still exists.")
    for name, kind in (
        ("fk_ppd_branchconfig_scope", "f"),
        ("excl_branchpaydefinitionconfig_definitionversionoverlap", "x"),
    ):
        table = "payrollperioddefinitions" if kind == "f" else "branchpaydefinitionconfig"
        cur.execute("""
            SELECT 1 FROM pg_constraint
            WHERE conrelid = to_regclass(%s) AND conname = %s
              AND contype = %s AND convalidated
        """, (f"payroll.{table}", name, kind))
        if cur.fetchone() is None:
            errors.append(f"Missing target branch applicability constraint {name}.")
    # Verify both event coverage and function binding. A replica-only, disabled,
    # wrong-event or wrong-function trigger does not protect application history.
    for name, event_type in (("trg_auditlog_immutable", 27), ("trg_auditlog_notruncate", 34)):
        cur.execute("""
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = to_regclass('audit.auditlog') AND tgname = %s
              AND tgtype = %s AND tgenabled IN ('O', 'A')
              AND tgfoid = to_regprocedure('audit.fn_guard_audit_log_immutable()')
        """, (name, event_type))
        if cur.fetchone() is None:
            errors.append(f"Missing or drifted AuditLog append-only trigger {name}.")
    return errors


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
        ("core", "driverassignments", "effectivefromat"),
        ("core", "driverassignments", "effectivetoat"),
        ("core", "driverassignments", "driverid"),
        ("core", "driverassignments", "endreason"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivefromatsnapshot"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivetoatsnapshot"),
        ("core", "drivertransferrequests", "effectiveat"),
        ("core", "drivertransferrequests", "sourcedriverassignmentid"),
        ("core", "drivertransferrequests", "destinationdriverassignmentid"),
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
           OR (table_schema = 'core' AND table_name = 'driverassignments' AND column_name IN ('effectivefromat','effectivetoat'))
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
        ("core", "driverassignments", "effectivefromat"),
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
        ("core", "driverassignments", "effectivetoat"),
        ("core", "drivertransferrequests", "effectiveat"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivetoatsnapshot"),
    ):
        shape = temporal_shapes.get(key)
        if shape and shape[0] != "YES":
            temporal_errors.append(f"Temporal boundary column {'.'.join(key)} must remain nullable.")
    for key in (
        ("core", "driverassignments", "effectivefromat"),
        ("core", "driverassignments", "effectivetoat"),
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
        ("core", "driverassignments", "ck_driverassignments_effectivewindowat", "c"),
        ("core", "driverassignments", "ck_driverassignments_endreason", "c"),
        ("core", "driverassignments", "ck_driverassignments_endreasonwindow", "c"),
        ("core", "driverassignments", "excl_driverassignments_driver_window", "x"),
        ("core", "driverassignments", "fk_driverassignments_driver", "f"),
        ("core", "drivers", "ck_drivers_status", "c"),
        ("core", "drivers", "uq_drivers_employee", "u"),
        ("payroll", "payrollperioddrivereligibility", "fk_ppde_snapshottimezone", "f"),
        ("core", "drivertransferrequests", "ck_dtr_completedscheduledeffectiveat", "c"),
        ("core", "drivertransferrequests", "ck_dtr_completedscheduleddestination", "c"),
        ("core", "drivertransferrequests", "fk_dtr_sourceassignment", "f"),
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
        elif name == "excl_driverassignments_driver_window" and constraint_shape != (True, True):
            temporal_errors.append(
                "core.DriverAssignments.excl_DriverAssignments_Driver_Window must be "
                "DEFERRABLE INITIALLY DEFERRED."
            )

    retired_columns = [
        ("core", "driverassignments", "effectivefrom"),
        ("core", "driverassignments", "effectiveto"),
        ("core", "drivertransferrequests", "effectivedate"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivefromsnapshot"),
        ("payroll", "payrollperioddrivereligibility", "drivereffectivetosnapshot"),
    ]
    for schema, table, column in retired_columns:
        if _column_exists(cur, schema, table, column):
            temporal_errors.append(f"Retired date-based assignment authority still exists: {schema}.{table}.{column}")

    retired_driver_columns = [
        ("core", "driverassignments", "drivercode"),
        ("core", "driverassignments", "cdlnumber"),
        ("core", "driverassignments", "externaldriverid"),
        ("core", "driverassignments", "driverstatus"),
        ("core", "drivertransferrequests", "newdriverid"),
        ("payroll", "payrollperioddrivereligibility", "driverid"),
        ("payroll", "payrollperioddrivereligibility", "drivercodesnapshot"),
        ("payroll", "payrollcalculationdrivertotals", "drivercodesnapshot"),
    ]
    for schema, table, column in retired_driver_columns:
        if _column_exists(cur, schema, table, column):
            temporal_errors.append(f"Retired branch-profile Driver authority still exists: {schema}.{table}.{column}")
    cur.execute("SELECT to_regclass('core.driveraliases')")
    if cur.fetchone()[0] is not None:
        temporal_errors.append("Retired Driver alias table still exists: core.DriverAliases")

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
        ("core.fn_effectivedriverprofile(integer,integer,timestamp with time zone)", "core.fn_EffectiveDriverProfile(INT, INT, TIMESTAMPTZ)"),
        ("core.fn_drivercoversworkdate(integer,integer,integer,date)", "core.fn_DriverCoversWorkDate(INT, INT, INT, DATE)"),
        ("core.fn_drivereffectiverange(date,date)", "core.fn_DriverEffectiveRange(DATE, DATE)"),
    ):
        cur.execute("SELECT to_regprocedure(%s)", (signature,))
        if cur.fetchone()[0] is not None:
            temporal_errors.append(f"Retired Driver temporal function still exists: {label}.")
    for signature, label in (
        ("core.fn_businessdate(text,timestamp with time zone)", "core.fn_BusinessDate(TEXT, TIMESTAMPTZ)"),
        ("core.fn_branchtoday(integer,integer,timestamp with time zone)", "core.fn_BranchToday(INT, INT, TIMESTAMPTZ)"),
        ("core.fn_issupportedtimezone(text)", "core.fn_IsSupportedTimeZone(TEXT)"),
        ("core.fn_localdateboundary(text,date)", "core.fn_LocalDateBoundary(TEXT, DATE)"),
        ("core.fn_workdaterange(text,date,date)", "core.fn_WorkDateRange(TEXT, DATE, DATE)"),
        ("core.fn_branchworkdaterange(integer,integer,date,date)", "core.fn_BranchWorkDateRange(INT, INT, DATE, DATE)"),
        ("core.fn_driverassignmentcoversworkdate(integer,integer,integer,date)", "core.fn_DriverAssignmentCoversWorkDate(INT, INT, INT, DATE)"),
        ("core.fn_effectivedriverassignment(integer,integer,timestamp with time zone)", "core.fn_EffectiveDriverAssignment(INT, INT, TIMESTAMPTZ)"),
        ("core.fn_drivertransferstatus(text,timestamp with time zone,timestamp with time zone)", "core.fn_DriverTransferStatus(TEXT, TIMESTAMPTZ, TIMESTAMPTZ)"),
        ("core.fn_drivereffectiverange(timestamp with time zone,timestamp with time zone)", "core.fn_DriverEffectiveRange(TIMESTAMPTZ, TIMESTAMPTZ)"),
    ):
        cur.execute("SELECT to_regprocedure(%s)", (signature,))
        if cur.fetchone()[0] is None:
            temporal_errors.append(f"Missing required temporal function: {label}.")
    for trigger_name, trigger_table in (
        ("trg_driverassignments_historyimmutable", "driverassignments"),
        ("trg_driverassignments_identityimmutable", "driverassignments"),
        ("trg_drivers_identityimmutable", "drivers"),
    ):
        if not _trigger_exists_and_enabled(cur, trigger_name, "core", trigger_table):
            temporal_errors.append(f"Missing or disabled Driver identity trigger core.{trigger_name}.")
    # Driver-owned compensation (migration 0088).
    for retired_table in ("driverrateassignments", "driverratevalues"):
        cur.execute("SELECT to_regclass(%s)", (f"payroll.{retired_table}",))
        if cur.fetchone()[0] is not None:
            temporal_errors.append(f"Retired assignment-owned rate table still exists: payroll.{retired_table}")
    for table, column in (
        ("driverrateschedules", "driverratescheduleid"),
        ("driverrateschedules", "driverid"),
        ("driverrateschedules", "paydefinitionid"),
        ("driverrateschedulevalues", "driverratescheduleid"),
        ("driverpayrules", "driverid"),
        ("driverpayrules", "calendarfrequency"),
        ("driverpayrules", "calendaranchorstartdate"),
        ("driverpayrules", "calendarcustomintervaldays"),
        ("driverpayrules", "calendarsourcebranchid"),
        ("driverpayrules", "calendarsourcepayrollsetupversionid"),
    ):
        if not _column_exists(cur, "payroll", table, column):
            temporal_errors.append(f"Missing Driver-owned compensation column payroll.{table}.{column}")
    for table, column in (
        ("driverrateschedules", "driverassignmentid"),
        ("driverrateschedules", "branchid"),
        ("driverpayrules", "driverassignmentid"),
        ("driverpayrules", "branchid"),
    ):
        if _column_exists(cur, "payroll", table, column):
            temporal_errors.append(f"Retired assignment/Branch compensation key still exists: payroll.{table}.{column}")
    for signature, label in (
        ("core.fn_driverassignmentforbusinessdate(integer,integer,date)",
         "core.fn_DriverAssignmentForBusinessDate(INT, INT, DATE)"),
        ("payroll.fn_driverpayrollperiodsforfootprint(integer,integer,daterange,text)",
         "payroll.fn_DriverPayrollPeriodsForFootprint(INT, INT, DATERANGE, TEXT)"),
        ("payroll.fn_driverfinalizedpayrolltouched(integer,integer,daterange,text)",
         "payroll.fn_DriverFinalizedPayrollTouched(INT, INT, DATERANGE, TEXT)"),
        ("core.trg_drivercompensationcontextwritable()", "core.trg_DriverCompensationContextWritable()"),
    ):
        cur.execute("SELECT to_regprocedure(%s)", (signature,))
        if cur.fetchone()[0] is None:
            temporal_errors.append(f"Missing Driver-owned compensation function: {label}.")
    for context_table in ("driverrateschedules", "driverpayrules"):
        if not _trigger_exists_and_enabled(
            cur, "trg_drivercompensationcontextwritable", "payroll", context_table
        ):
            temporal_errors.append(
                "Missing or disabled Driver compensation context trigger "
                f"trg_DriverCompensationContextWritable on payroll.{context_table}."
            )
    if not _trigger_exists_and_enabled(cur, "trg_driverpayrules_beforeupdate", "payroll", "driverpayrules"):
        temporal_errors.append("Missing or disabled pay-rule integrity trigger trg_DriverPayRules_BeforeUpdate.")

    temporal_errors.extend(_check_calculation_evidence(cur))
    temporal_errors.extend(_check_company_status_catalog(cur))
    temporal_errors.extend(_check_target_physical_authorities(cur))
    temporal_errors.extend(_check_import_foundation(cur))

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
