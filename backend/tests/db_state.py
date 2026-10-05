"""Test-only guards for PostgreSQL session state and production-trigger suspension.

This module is the ONLY approved place in backend/tests that may contain raw
``ALTER TABLE ... DISABLE/ENABLE TRIGGER`` or session-level ``app.*`` GUC SQL.
Tests reach that behavior through the context managers below, which restore the
prior state in ``finally`` and fail when restoration cannot be verified.

Pooled raw connections carry session state between tests, so:

* ``temporary_test_guc`` / ``suspended_test_triggers`` are the PRIMARY isolation
  mechanism: they restore and verify the instant their own context exits.
* ``preserve_test_guc_state`` is a fixture-boundary safety net only. For a
  session-scoped connection it runs once, at session end, so it cannot provide
  per-test isolation by itself.
* ``scan_tests_for_unsafe_sql`` (enforced by test_isolation_enforcement.py)
  prevents tests from bypassing the helpers.
"""

from __future__ import annotations

import ast
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

FINAL_LINE_INSERT_GUC = "app.allow_payroll_final_line_insert"
APPROVED_TEST_GUCS = frozenset({FINAL_LINE_INSERT_GUC})


@dataclass(frozen=True, order=True)
class TriggerIdentity:
    schema: str
    table: str
    name: str

    def __str__(self) -> str:
        return f"{self.schema}.{self.table}.{self.name}"


FINAL_LINE_IMMUTABLE_TRIGGER = TriggerIdentity(
    "payroll", "payrollfinallines", "trg_final_line_immutable"
)
PERIOD_STATUS_REVERT_TRIGGER = TriggerIdentity(
    "payroll", "payrollperiods", "trg_period_status_revert"
)
CDPI_REQUEST_EVENTS_IMMUTABLE_TRIGGER = TriggerIdentity(
    "payroll", "cdpirequestevents", "trg_guard_cdpi_request_events_immutable"
)
BONUS_EVENT_OWNERSHIP_TRIGGER = TriggerIdentity(
    "payroll", "payrollbonusevents", "trg_bonusevents_ownership"
)
PAY_ITEM_RATE_TYPE_MAP_OWNERSHIP_TRIGGER = TriggerIdentity(
    "payroll", "payitemratetypemap", "trg_guard_payitemratetypemap_ownership"
)
# The pair historically suspended to retire finalized history or force a status.
FINALIZED_HISTORY_TRIGGERS = (FINAL_LINE_IMMUTABLE_TRIGGER, PERIOD_STATUS_REVERT_TRIGGER)

# Immutable-evidence guards (migrations 0035/0061-0065), each suspended by exact name,
# never DISABLE TRIGGER ALL (which would also suspend unrelated cascades), when a
# test retires a whole evidenced period leaf-to-root.
PERIOD_EVIDENCE_IMMUTABLE_TRIGGERS = (
    TriggerIdentity("payroll", "payrollfinallines", "trg_final_line_immutable"),
    TriggerIdentity("payroll", "payrollcalculationsnapshotlines", "trg_payrollcalculationsnapshotlines_immutable"),
    TriggerIdentity("payroll", "payrollperiodauditevidencesnapshotevents", "trg_payrollperiodauditevidencesnapshotevents_immutable"),
    TriggerIdentity("payroll", "payrollperiodauditevidenceevents", "trg_payrollperiodauditevidenceevents_immutable"),
    TriggerIdentity("payroll", "payrollperiodauditevidencecoverage", "trg_payrollperiodauditevidencecoverage_immutable"),
    TriggerIdentity("payroll", "payrollcalculationsnapshotusedratedefinitions", "trg_payrollcalculationsnapshotusedratedefinitions_immutable"),
    TriggerIdentity("payroll", "payrollcalculationdrivertotals", "trg_payrollcalculationdrivertotals_immutable"),
    TriggerIdentity("payroll", "payrollcalculationsnapshotstatusentries", "trg_payrollcalculationsnapshotstatusentries_immutable"),
    TriggerIdentity("payroll", "payrollcalculationsnapshotbonusevents", "trg_payrollcalculationsnapshotbonusevents_immutable"),
    TriggerIdentity("payroll", "payrollperiodworkflowactionevidence", "trg_payrollperiodworkflowactionevidence_immutable"),
    TriggerIdentity("payroll", "payrollcalculationsnapshots", "trg_payrollcalculationsnapshots_immutable"),
    TriggerIdentity("payroll", "payrollperiods", "trg_payrollperiods_auditevidencedelete"),
)

TriggerFingerprint = dict[TriggerIdentity, str]


def _raise_with_restoration_errors(
    body_error: BaseException | None,
    restoration_errors: list[BaseException],
) -> None:
    errors = ([body_error] if body_error is not None else []) + restoration_errors
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup("test database state and restoration failed", errors)


# ---------------------------------------------------------------------------
# Session-level GUCs
# ---------------------------------------------------------------------------

def _validate_guc(name: str) -> None:
    if name not in APPROVED_TEST_GUCS:
        raise ValueError(f"test GUC is not allowlisted: {name}")


def _normalize_guc(value: str | None) -> str | None:
    # PostgreSQL reports a custom GUC that was set and then RESET as '' rather
    # than NULL; both mean "not authorizing anything".
    return value or None


async def _read_guc(conn, name: str) -> str | None:
    _validate_guc(name)
    value = (await conn.execute(
        text("SELECT current_setting(:setting_name, true)"),
        {"setting_name": name},
    )).scalar_one()
    return _normalize_guc(value)


async def _write_guc(conn, name: str, value: str | None) -> None:
    _validate_guc(name)
    if value is None:
        await conn.execute(text(f"RESET {name}"))
    else:
        await conn.execute(
            text("SELECT set_config(:setting_name, :setting_value, false)"),
            {"setting_name": name, "setting_value": value},
        )


async def _restore_guc(conn, name: str, value: str | None) -> None:
    await _write_guc(conn, name, value)
    observed = await _read_guc(conn, name)
    if observed != value:
        raise AssertionError(
            f"test GUC restoration mismatch for {name}: expected {value!r}, got {observed!r}"
        )


@asynccontextmanager
async def temporary_test_guc(conn, name: str, value: str) -> AsyncIterator[None]:
    """Set one allowlisted session GUC for the body; restore and verify on exit.

    The body's own exception is never swallowed; if restoration also fails both
    are raised together.
    """
    _validate_guc(name)
    if not value:
        raise ValueError("temporary test GUC value must be non-empty")
    previous = await _read_guc(conn, name)
    body_error: BaseException | None = None
    restoration_errors: list[BaseException] = []
    try:
        await _write_guc(conn, name, value)
        observed = await _read_guc(conn, name)
        if observed != value:
            raise AssertionError(f"test GUC {name} was not set to {value!r}: got {observed!r}")
        yield
    except BaseException as exc:
        body_error = exc
    try:
        await _restore_guc(conn, name, previous)
    except BaseException as exc:
        restoration_errors.append(exc)
    _raise_with_restoration_errors(body_error, restoration_errors)


def allow_final_line_insert(conn):
    """Authorize direct PayrollFinalLines inserts for the body, then revoke."""
    return temporary_test_guc(conn, FINAL_LINE_INSERT_GUC, "true")


_REPLICATION_ROLE_SETTING = "session_replication_role"
_DEFAULT_REPLICATION_ROLE = "origin"


async def _read_replication_role(conn) -> str:
    return (await conn.execute(text("SHOW session_replication_role"))).scalar_one()


@asynccontextmanager
async def replica_replication_role(conn) -> AsyncIterator[None]:
    """Run the body with session_replication_role = replica (FK and ordinary
    triggers suppressed); restore and verify the prior role on exit.

    ``replica`` also silences the production-integrity triggers, so a role left
    behind on a pooled connection would disable them for later tests.
    """
    previous = await _read_replication_role(conn)
    body_error: BaseException | None = None
    restoration_errors: list[BaseException] = []
    try:
        await conn.execute(text("SET session_replication_role = replica"))
        yield
    except BaseException as exc:
        body_error = exc
    try:
        await conn.execute(text(f"SET session_replication_role = {previous}"))
        observed = await _read_replication_role(conn)
        if observed != previous:
            raise AssertionError(
                f"session_replication_role restoration mismatch: expected {previous!r}, got {observed!r}"
            )
    except BaseException as exc:
        restoration_errors.append(exc)
    _raise_with_restoration_errors(body_error, restoration_errors)


async def _read_guarded_session_state(conn) -> dict[str, str | None]:
    names = sorted(APPROVED_TEST_GUCS)
    row = (await conn.execute(text(
        "SELECT current_setting('session_replication_role')"
        + "".join(f", current_setting('{name}', true)" for name in names)
    ))).one()
    state: dict[str, str | None] = {_REPLICATION_ROLE_SETTING: row[0]}
    state.update({name: _normalize_guc(value) for name, value in zip(names, row[1:])})
    return state


def _guarded_state_deviations(state: dict[str, str | None]) -> dict[str, str | None]:
    return {
        name: value for name, value in state.items()
        if value != (_DEFAULT_REPLICATION_ROLE if name == _REPLICATION_ROLE_SETTING else None)
    }


async def _restore_guarded_state(
    conn, deviations: dict[str, str | None],
) -> list[BaseException]:
    """Return every guarded setting in `deviations` to its clean value and verify the
    connection is clean afterwards. Never raises: restoration failures are returned so
    the caller can report them next to the leak that made restoration necessary."""
    failures: list[BaseException] = []
    for name in deviations:
        try:
            if name == _REPLICATION_ROLE_SETTING:
                await conn.execute(
                    text(f"SET session_replication_role = {_DEFAULT_REPLICATION_ROLE}")
                )
            else:
                await _write_guc(conn, name, None)
        except BaseException as exc:
            failures.append(exc)
    try:
        still_dirty = _guarded_state_deviations(await _read_guarded_session_state(conn))
        if still_dirty:
            raise AssertionError(
                f"session state restoration mismatch, still modified: {still_dirty}"
            )
    except BaseException as exc:
        failures.append(exc)
    return failures


@asynccontextmanager
async def preserve_test_guc_state(conn) -> AsyncIterator[None]:
    """Fixture-boundary safety net for a raw test connection.

    The connection must enter with every approved GUC unset and
    session_replication_role = origin, and must leave the same way. In BOTH
    directions a deviation is recorded, the connection is restored and verified
    clean (so nothing leaks into later tests), and only then does the boundary
    fail, naming the original leak. A restoration failure is reported alongside it.
    """
    leaked_in = _guarded_state_deviations(await _read_guarded_session_state(conn))
    if leaked_in:
        failures = await _restore_guarded_state(conn, leaked_in)
        _raise_with_restoration_errors(
            AssertionError(
                f"pooled test connection arrived with leaked session state: {leaked_in}"
            ),
            failures,
        )
    body_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        body_error = exc

    errors: list[BaseException] = []
    try:
        left = _guarded_state_deviations(await _read_guarded_session_state(conn))
    except BaseException as exc:
        errors.append(exc)
    else:
        if left:
            errors.append(AssertionError(
                f"test left session state modified on a raw connection: {left}; "
                "use temporary_test_guc / replica_replication_role so it is "
                "restored when the block exits"
            ))
            errors.extend(await _restore_guarded_state(conn, left))
    _raise_with_restoration_errors(body_error, errors)


# ---------------------------------------------------------------------------
# Exact trigger suspension and the shared-session fingerprint
# ---------------------------------------------------------------------------

_TRIGGER_STATE_SQL = """
    SELECT t.tgenabled::text
    FROM pg_catalog.pg_trigger AS t
    JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = :schema_name
      AND c.relname = :table_name
      AND t.tgname = :trigger_name
      AND NOT t.tgisinternal
"""


async def _read_trigger_state(conn, identity: TriggerIdentity) -> str:
    state = (await conn.execute(
        text(_TRIGGER_STATE_SQL),
        {
            "schema_name": identity.schema,
            "table_name": identity.table,
            "trigger_name": identity.name,
        },
    )).scalar_one_or_none()
    if state is None:
        raise AssertionError(f"requested test trigger does not exist: {identity}")
    return state


_ENABLE_ACTIONS = {"D": "DISABLE", "O": "ENABLE", "A": "ENABLE ALWAYS", "R": "ENABLE REPLICA"}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _trigger_ddl(identity: TriggerIdentity, state: str) -> str:
    relation = f"{_quote_identifier(identity.schema)}.{_quote_identifier(identity.table)}"
    return (
        f"ALTER TABLE {relation} {_ENABLE_ACTIONS[state]} TRIGGER "
        f"{_quote_identifier(identity.name)}"
    )


@asynccontextmanager
async def suspended_test_triggers(
    conn,
    triggers: Iterable[TriggerIdentity],
) -> AsyncIterator[None]:
    """Disable exact named triggers for the body; restore their exact modes.

    Never touches any trigger not named; never uses DISABLE TRIGGER ALL.
    Restoration is attempted in reverse order for every trigger even if an
    earlier one fails, and each is verified.
    """
    identities = tuple(triggers)
    if not identities:
        raise ValueError("at least one exact trigger identity is required")
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate trigger identities are not allowed")
    before = {identity: await _read_trigger_state(conn, identity) for identity in identities}
    body_error: BaseException | None = None
    restoration_errors: list[BaseException] = []
    try:
        for identity in identities:
            await conn.execute(text(_trigger_ddl(identity, "D")))
        yield
    except BaseException as exc:
        body_error = exc
    for identity in reversed(identities):
        try:
            await conn.execute(text(_trigger_ddl(identity, before[identity])))
            observed = await _read_trigger_state(conn, identity)
            if observed != before[identity]:
                raise AssertionError(
                    f"trigger restoration mismatch for {identity}: "
                    f"expected enabled state {before[identity]!r}, got {observed!r}"
                )
        except BaseException as exc:
            restoration_errors.append(exc)
    _raise_with_restoration_errors(body_error, restoration_errors)


async def read_trigger_fingerprint(conn) -> TriggerFingerprint:
    rows = (await conn.execute(text("""
        SELECT n.nspname, c.relname, t.tgname, t.tgenabled::text
        FROM pg_catalog.pg_trigger AS t
        JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal
        ORDER BY n.nspname, c.relname, t.tgname
    """))).all()
    return {
        TriggerIdentity(schema, table, name): enabled
        for schema, table, name, enabled in rows
    }


def trigger_fingerprint_diff(
    expected: TriggerFingerprint,
    actual: TriggerFingerprint,
) -> dict[str, list[str]]:
    expected_keys, actual_keys = set(expected), set(actual)
    return {
        "missing": [str(item) for item in sorted(expected_keys - actual_keys)],
        "extra": [str(item) for item in sorted(actual_keys - expected_keys)],
        "changed_enabled_state": sorted(
            f"{identity}: expected {expected[identity]!r}, got {actual[identity]!r}"
            for identity in expected_keys & actual_keys
            if expected[identity] != actual[identity]
        ),
    }


def assert_trigger_fingerprints_equal(
    expected: TriggerFingerprint,
    actual: TriggerFingerprint,
) -> None:
    diff = trigger_fingerprint_diff(expected, actual)
    if any(diff.values()):
        raise AssertionError(f"PostgreSQL trigger fingerprint changed: {diff}")


# ---------------------------------------------------------------------------
# Static enforcement
# ---------------------------------------------------------------------------

# Raw SQL is permitted only in these files, named by exact path relative to the tests
# root (posix separators): this module, and one migration test that operates on its
# own disposable database (dropped afterwards, never shared). A same-named file in a
# subdirectory does NOT inherit the exemption.
APPROVED_RAW_SQL_FILES = frozenset({
    "db_state.py",
    "test_g0_1_bonus_precision_migration.py",
})

_TRIGGER_TOGGLE = re.compile(
    r"\b(?:DISABLE|ENABLE(?:\s+(?:ALWAYS|REPLICA))?)\s+TRIGGER\b", re.I
)
_REPLICATION_ROLE = re.compile(
    r"\bSET\s+(?:SESSION\s+)?session_replication_role\b"
    r"|\bset_config\s*\(\s*'session_replication_role'",
    re.I,
)
_SESSION_GUC_STATEMENT = re.compile(r"\b(?:SET\s+(?:SESSION\s+)?|RESET\s+)app\.", re.I)
_SET_CONFIG_APP = re.compile(
    r"\bset_config\s*\(\s*'app\.[^']*'\s*,\s*(?:'[^']*'|[^,()]+)\s*,\s*([^,()]+?)\s*\)",
    re.I,
)


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
    return None


def _unsafe_reason(value: str) -> str | None:
    if _TRIGGER_TOGGLE.search(value):
        return "raw trigger DISABLE/ENABLE; use suspended_test_triggers"
    if _REPLICATION_ROLE.search(value):
        return "raw session_replication_role change bypasses triggers; use replica_replication_role"
    if _SESSION_GUC_STATEMENT.search(value):
        return "raw SET/RESET of an app.* GUC; use temporary_test_guc"
    for match in _SET_CONFIG_APP.finditer(value):
        if match.group(1).lower() != "true":
            return "set_config of an app.* GUC that is not transaction-local; use temporary_test_guc"
    return None


def unsafe_test_source_literals(source: str) -> list[str]:
    """Return unsafe SQL string literals in real code, ignoring docstrings,
    bare string statements and comments."""
    tree = ast.parse(source)
    ignored: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and _literal_text(node.value) is not None:
            ignored.update(id(child) for child in ast.walk(node.value))
    findings: list[str] = []
    nested: set[int] = set()
    for node in ast.walk(tree):
        if id(node) in ignored or id(node) in nested:
            continue
        value = _literal_text(node)
        if value is None:
            continue
        if isinstance(node, ast.JoinedStr):
            nested.update(id(child) for child in ast.walk(node) if child is not node)
        reason = _unsafe_reason(value)
        if reason:
            findings.append(f"line {node.lineno}: {reason}: {' '.join(value.split())[:120]!r}")
    return findings


def scan_tests_for_unsafe_sql(tests_dir: Path) -> dict[str, list[str]]:
    """Scan every backend test module outside the approved raw-SQL files."""
    violations: dict[str, list[str]] = {}
    for path in sorted(tests_dir.rglob("*.py")):
        relative = path.relative_to(tests_dir).as_posix()
        if relative in APPROVED_RAW_SQL_FILES:
            continue
        findings = unsafe_test_source_literals(path.read_text(encoding="utf-8"))
        if findings:
            violations[relative] = findings
    return violations
