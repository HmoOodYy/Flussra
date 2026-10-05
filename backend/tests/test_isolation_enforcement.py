"""Behavioral proof that the test-isolation guards fail loudly.

These tests exercise tests/db_state.py and tests/ownership.py. They never leave
the shared test database changed: GUCs are restored, and trigger probes use a
scratch table that is dropped before each test returns.

This file is itself scanned by the static contract, so unsafe SQL used as scanner
input is assembled from fragments rather than written literally.
"""

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from tests import db_state
from tests.builders.owned_scope import create_owned_branch
from tests.db_state import (
    FINAL_LINE_IMMUTABLE_TRIGGER,
    FINAL_LINE_INSERT_GUC,
    FINALIZED_HISTORY_TRIGGERS,
    TriggerIdentity,
    allow_final_line_insert,
    assert_trigger_fingerprints_equal,
    preserve_test_guc_state,
    read_trigger_fingerprint,
    replica_replication_role,
    scan_tests_for_unsafe_sql,
    suspended_test_triggers,
    temporary_test_guc,
    trigger_fingerprint_diff,
    unsafe_test_source_literals,
)
from tests.ownership import (
    CleanupRunner,
    assert_no_mutable_period_state,
    delete_period_and_children,
    delete_review_items_and_children,
    retire_branch_periods_directly,
)

_TESTS_DIR = Path(__file__).parent


def _kw(*parts: str) -> str:
    """Assemble SQL keywords from fragments so this file holds no literal raw SQL."""
    return "".join(parts)


async def _guc_value(conn) -> str | None:
    return (await conn.execute(
        text(f"SELECT current_setting('{FINAL_LINE_INSERT_GUC}', true)")
    )).scalar_one() or None


async def _role(conn) -> str:
    return (await conn.execute(text("SHOW session_replication_role"))).scalar_one()


# ---------------------------------------------------------------------------
# GUC guard
# ---------------------------------------------------------------------------

async def test_temporary_guc_sets_then_restores_unset_state(db_conn):
    assert await _guc_value(db_conn) is None
    async with temporary_test_guc(db_conn, FINAL_LINE_INSERT_GUC, "true"):
        assert await _guc_value(db_conn) == "true"
    assert await _guc_value(db_conn) is None


async def test_temporary_guc_restores_a_prior_non_empty_value(db_conn):
    async with temporary_test_guc(db_conn, FINAL_LINE_INSERT_GUC, "outer"):
        async with temporary_test_guc(db_conn, FINAL_LINE_INSERT_GUC, "inner"):
            assert await _guc_value(db_conn) == "inner"
        assert await _guc_value(db_conn) == "outer"
    assert await _guc_value(db_conn) is None


async def test_temporary_guc_restores_after_body_exception_and_reraises_it(db_conn):
    with pytest.raises(RuntimeError, match="body failure"):
        async with temporary_test_guc(db_conn, FINAL_LINE_INSERT_GUC, "true"):
            assert await _guc_value(db_conn) == "true"
            raise RuntimeError("body failure")
    assert await _guc_value(db_conn) is None


async def test_allow_final_line_insert_authorizes_then_revokes(db_conn):
    async with allow_final_line_insert(db_conn):
        assert await _guc_value(db_conn) == "true"
    assert await _guc_value(db_conn) is None


async def test_temporary_guc_rejects_non_allowlisted_name_without_entering(db_conn):
    with pytest.raises(ValueError, match="not allowlisted"):
        async with temporary_test_guc(db_conn, "app.unapproved", "true"):
            pytest.fail("unapproved GUC context must not enter")


async def test_guc_restoration_failure_fails_visibly(db_conn, monkeypatch):
    real_write = db_state._write_guc

    async def write_without_restoring(conn, name, value):
        if value is not None:  # set succeeds; the restoring RESET silently does nothing
            await real_write(conn, name, value)

    monkeypatch.setattr(db_state, "_write_guc", write_without_restoring)
    try:
        with pytest.raises(AssertionError, match="restoration mismatch"):
            async with temporary_test_guc(db_conn, FINAL_LINE_INSERT_GUC, "true"):
                pass
    finally:
        monkeypatch.undo()
        await real_write(db_conn, FINAL_LINE_INSERT_GUC, None)
    assert await _guc_value(db_conn) is None


async def test_body_and_restoration_failures_are_both_preserved(db_conn, monkeypatch):
    real_write = db_state._write_guc

    async def write_without_restoring(conn, name, value):
        if value is not None:
            await real_write(conn, name, value)

    monkeypatch.setattr(db_state, "_write_guc", write_without_restoring)
    try:
        with pytest.raises(BaseExceptionGroup) as raised:
            async with temporary_test_guc(db_conn, FINAL_LINE_INSERT_GUC, "true"):
                raise RuntimeError("body failure")
    finally:
        monkeypatch.undo()
        await real_write(db_conn, FINAL_LINE_INSERT_GUC, None)
    kinds = {type(exc) for exc in raised.value.exceptions}
    assert kinds == {RuntimeError, AssertionError}


# ---------------------------------------------------------------------------
# Fixture-boundary safety net (not per-test isolation for session fixtures)
# ---------------------------------------------------------------------------

async def test_boundary_fails_and_restores_when_body_leaves_guc_set(db_conn):
    with pytest.raises(AssertionError, match="left session state modified"):
        async with preserve_test_guc_state(db_conn):
            await db_state._write_guc(db_conn, FINAL_LINE_INSERT_GUC, "true")
    assert await _guc_value(db_conn) is None


async def test_boundary_restores_a_connection_that_arrives_dirty_before_failing(db_conn):
    # Dirty the connection directly -- NOT through an outer temporary_test_guc, whose own
    # restoration would mask whether the boundary cleaned up after itself.
    await db_state._write_guc(db_conn, FINAL_LINE_INSERT_GUC, "true")
    await db_conn.execute(text(_kw("SE", "T session_replication_role = replica")))
    with pytest.raises(AssertionError, match="arrived with leaked session state") as raised:
        async with preserve_test_guc_state(db_conn):
            pytest.fail("a dirty connection must not enter the boundary")
    message = str(raised.value)
    assert FINAL_LINE_INSERT_GUC in message and "session_replication_role" in message
    # The failure must not have left the pooled connection poisoned for later tests.
    assert await _guc_value(db_conn) is None
    assert await _role(db_conn) == "origin"


async def test_boundary_dirty_entry_restoration_failure_is_reported_with_the_leak(db_conn, monkeypatch):
    real_write = db_state._write_guc
    await real_write(db_conn, FINAL_LINE_INSERT_GUC, "true")

    async def write_without_restoring(conn, name, value):
        if value is not None:  # the restoring RESET silently does nothing
            await real_write(conn, name, value)

    monkeypatch.setattr(db_state, "_write_guc", write_without_restoring)
    try:
        with pytest.raises(BaseExceptionGroup) as raised:
            async with preserve_test_guc_state(db_conn):
                pytest.fail("a dirty connection must not enter the boundary")
    finally:
        monkeypatch.undo()
        await real_write(db_conn, FINAL_LINE_INSERT_GUC, None)
    messages = [str(exc) for exc in raised.value.exceptions]
    assert any("arrived with leaked session state" in m for m in messages)
    assert any("restoration mismatch" in m for m in messages)
    assert await _guc_value(db_conn) is None


async def test_boundary_fails_and_restores_when_body_leaves_replica_role(db_conn):
    with pytest.raises(AssertionError, match="left session state modified"):
        async with preserve_test_guc_state(db_conn):
            await db_conn.execute(text(_kw("SE", "T session_replication_role = replica")))
    assert await _role(db_conn) == "origin"


async def test_replica_role_is_scoped_and_restored_after_exception(db_conn):
    assert await _role(db_conn) == "origin"
    async with replica_replication_role(db_conn):
        assert await _role(db_conn) == "replica"
    assert await _role(db_conn) == "origin"
    with pytest.raises(RuntimeError, match="replica body failure"):
        async with replica_replication_role(db_conn):
            raise RuntimeError("replica body failure")
    assert await _role(db_conn) == "origin"


# ---------------------------------------------------------------------------
# Trigger suspension (exact names only)
# ---------------------------------------------------------------------------

@pytest.fixture
async def probe_table(db_conn):
    """A scratch table with two triggers, dropped before the test returns."""
    await db_conn.execute(text("CREATE TABLE public.b2_trigger_probe (id integer)"))
    await db_conn.execute(text(
        "CREATE FUNCTION public.b2_trigger_probe_fn() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
    ))
    for name in ("b2_probe_a", "b2_probe_b"):
        await db_conn.execute(text(
            f"CREATE TRIGGER {name} BEFORE INSERT ON public.b2_trigger_probe "
            "FOR EACH ROW EXECUTE FUNCTION public.b2_trigger_probe_fn()"
        ))
    try:
        yield (
            TriggerIdentity("public", "b2_trigger_probe", "b2_probe_a"),
            TriggerIdentity("public", "b2_trigger_probe", "b2_probe_b"),
        )
    finally:
        await db_conn.execute(text("DROP TABLE public.b2_trigger_probe"))
        await db_conn.execute(text("DROP FUNCTION public.b2_trigger_probe_fn()"))


async def test_trigger_suspension_disables_inside_and_restores_after_normal_exit(db_conn, probe_table):
    probe_a, probe_b = probe_table
    before = await read_trigger_fingerprint(db_conn)
    assert before[probe_a] == before[probe_b] == "O"
    async with suspended_test_triggers(db_conn, [probe_a]):
        during = await read_trigger_fingerprint(db_conn)
        assert during[probe_a] == "D"
        assert during[probe_b] == "O", "only the named trigger may be suspended"
    assert await read_trigger_fingerprint(db_conn) == before


async def test_trigger_suspension_restores_after_exception(db_conn):
    before = await read_trigger_fingerprint(db_conn)
    assert before[FINAL_LINE_IMMUTABLE_TRIGGER] == "O"
    with pytest.raises(RuntimeError, match="trigger body failure"):
        async with suspended_test_triggers(db_conn, FINALIZED_HISTORY_TRIGGERS):
            during = await read_trigger_fingerprint(db_conn)
            assert during[FINAL_LINE_IMMUTABLE_TRIGGER] == "D"
            raise RuntimeError("trigger body failure")
    assert await read_trigger_fingerprint(db_conn) == before


async def test_trigger_suspension_restores_the_original_enabled_mode(db_conn, probe_table):
    probe_a, _ = probe_table
    await db_conn.execute(text(
        "ALTER TABLE public.b2_trigger_probe " + _kw("ENABLE", " ALWAYS TRIGGER b2_probe_a")
    ))
    assert (await read_trigger_fingerprint(db_conn))[probe_a] == "A"
    async with suspended_test_triggers(db_conn, [probe_a]):
        assert (await read_trigger_fingerprint(db_conn))[probe_a] == "D"
    assert (await read_trigger_fingerprint(db_conn))[probe_a] == "A"


async def test_trigger_suspension_rejects_unknown_and_duplicate_triggers(db_conn):
    missing = TriggerIdentity("payroll", "payrollfinallines", "trg_that_does_not_exist")
    with pytest.raises(AssertionError, match="does not exist"):
        async with suspended_test_triggers(db_conn, [missing]):
            pytest.fail("a missing trigger must not enter")
    with pytest.raises(ValueError, match="duplicate"):
        async with suspended_test_triggers(
            db_conn, [FINAL_LINE_IMMUTABLE_TRIGGER, FINAL_LINE_IMMUTABLE_TRIGGER],
        ):
            pytest.fail("duplicates must not enter")
    with pytest.raises(ValueError, match="at least one"):
        async with suspended_test_triggers(db_conn, []):
            pytest.fail("an empty set must not enter")


def test_trigger_ddl_names_exactly_one_trigger():
    ddl = db_state._trigger_ddl(FINAL_LINE_IMMUTABLE_TRIGGER, "D")
    assert ddl == (
        'ALTER TABLE "payroll"."payrollfinallines" '
        + _kw("DIS", "ABLE") + ' TRIGGER "trg_final_line_immutable"'
    )
    assert not any(word == "ALL" for word in ddl.replace('"', " ").split())


async def test_trigger_restoration_failure_fails_visibly(db_conn, probe_table, monkeypatch):
    probe_a, _ = probe_table
    real_ddl = db_state._trigger_ddl

    def ddl_that_never_re_enables(identity, state):
        return real_ddl(identity, state) if state == "D" else "SELECT 1"

    monkeypatch.setattr(db_state, "_trigger_ddl", ddl_that_never_re_enables)
    try:
        with pytest.raises(BaseExceptionGroup) as raised:
            async with suspended_test_triggers(db_conn, [probe_a]):
                raise RuntimeError("body failure")
    finally:
        monkeypatch.undo()
        await db_conn.execute(text(real_ddl(probe_a, "O")))
    kinds = {type(exc) for exc in raised.value.exceptions}
    assert kinds == {RuntimeError, AssertionError}
    assert "restoration mismatch" in str(raised.value.exceptions[1])
    assert (await read_trigger_fingerprint(db_conn))[probe_a] == "O"


# ---------------------------------------------------------------------------
# Trigger fingerprint
# ---------------------------------------------------------------------------

async def test_fingerprint_reads_decoded_enabled_states(db_conn):
    fingerprint = await read_trigger_fingerprint(db_conn)
    assert fingerprint[FINAL_LINE_IMMUTABLE_TRIGGER] == "O"
    assert all(isinstance(state, str) for state in fingerprint.values())


def test_fingerprint_mismatch_reports_missing_extra_and_changed_state():
    period_trigger = TriggerIdentity("payroll", "payrollperiods", "trg_period_status_revert")
    expected = {FINAL_LINE_IMMUTABLE_TRIGGER: "O", period_trigger: "O"}
    actual = {
        FINAL_LINE_IMMUTABLE_TRIGGER: "D",
        TriggerIdentity("payroll", "payrollperiods", "trg_added"): "O",
    }
    diff = trigger_fingerprint_diff(expected, actual)
    assert diff["missing"] == [str(period_trigger)]
    assert diff["extra"] == ["payroll.payrollperiods.trg_added"]
    assert len(diff["changed_enabled_state"]) == 1
    with pytest.raises(AssertionError) as raised:
        assert_trigger_fingerprints_equal(expected, actual)
    message = str(raised.value)
    assert "trigger fingerprint changed" in message
    assert "trg_final_line_immutable" in message and "trg_added" in message


def test_equal_fingerprints_pass():
    assert_trigger_fingerprints_equal(
        {FINAL_LINE_IMMUTABLE_TRIGGER: "O"}, {FINAL_LINE_IMMUTABLE_TRIGGER: "O"},
    )


# ---------------------------------------------------------------------------
# CleanupRunner
# ---------------------------------------------------------------------------

async def test_cleanup_runner_failure_is_visible_and_labelled(db_conn):
    runner = CleanupRunner()
    await runner.execute(db_conn, "SELECT * FROM table_that_does_not_exist", {}, label="probe")
    with pytest.raises(ExceptionGroup, match=r"1 cleanup step\(s\) failed") as raised:
        runner.raise_if_any()
    assert "cleanup step failed: probe" in raised.value.exceptions[0].__notes__


async def test_cleanup_runner_continues_after_a_failure_and_reports_every_one(db_conn):
    runner = CleanupRunner()
    await runner.execute(db_conn, "SELECT * FROM missing_one", {}, label="first")
    await runner.execute(db_conn, "SELECT 1", {}, label="succeeds")
    await runner.execute(db_conn, "SELECT * FROM missing_two", {}, label="second")
    with pytest.raises(ExceptionGroup, match=r"2 cleanup step\(s\) failed"):
        runner.raise_if_any()


async def test_cleanup_runner_raises_nothing_when_every_step_succeeds(db_conn):
    runner = CleanupRunner()
    await runner.execute(db_conn, "SELECT 1", {}, label="ok")
    runner.raise_if_any()


# ---------------------------------------------------------------------------
# Terminal-state contract (retained history is legitimate, mutable state is not)
# ---------------------------------------------------------------------------

async def _insert_period(direct_db, branch_id: int, status: str, month: int) -> int:
    return int((await direct_db.execute(
        text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, :status, :code, 'B2 terminal-state probe', 'Week',
                    make_date(2096, :month, 1), make_date(2096, :month, 7))
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "status": status, "code": f"B2-{branch_id}-{status}-{month}", "month": month},
    )).scalar_one())


async def test_terminal_state_accepts_retained_finalized_history(direct_db, owned_branch_id):
    locked = await _insert_period(direct_db, owned_branch_id, "Locked", 1)
    archived = await _insert_period(direct_db, owned_branch_id, "Archived", 2)
    try:
        await assert_no_mutable_period_state(direct_db, owned_branch_id)
    finally:
        for period_id in (locked, archived):
            await delete_period_and_children(direct_db, period_id)


async def test_terminal_state_names_every_mutable_period_by_id_and_status(direct_db, owned_branch_id):
    mutable = {
        status: await _insert_period(direct_db, owned_branch_id, status, month)
        for month, status in enumerate(("Open", "InReview", "Approved"), start=1)
    }
    try:
        with pytest.raises(AssertionError, match="retains mutable workflow periods") as raised:
            await assert_no_mutable_period_state(direct_db, owned_branch_id)
        message = str(raised.value)
        for status, period_id in mutable.items():
            assert f"({period_id}, '{status}')" in message
    finally:
        for period_id in mutable.values():
            await delete_period_and_children(direct_db, period_id)


# ---------------------------------------------------------------------------
# Seeded PAYTEST activation must not depend on the shadowable `paytest_branch_id`
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Deliberately shadows the shared name with a fresh, un-activated owned branch,
    as many modules do. If session-autouse activation resolved this name it would
    activate pay items here instead of on the canonical seeded PAYTEST branch."""
    return await create_owned_branch(session_db_conn, "ISO", "Isolation shadow branch")


async def _active_pay_item_codes(client, token: str, branch_id: int) -> set[str]:
    resp = await client.get(
        f"/settings/branches/{branch_id}/pay-items", headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return {item["pay_item_code"] for item in resp.json() if item["is_active"]}


async def test_activation_targets_the_seeded_branch_when_a_module_shadows_paytest_branch_id(
    session_client, auth_token, seeded_paytest_branch_id, paytest_branch_id,
):
    assert paytest_branch_id != seeded_paytest_branch_id, "this module must shadow the name"
    seeded = await _active_pay_item_codes(session_client, auth_token, seeded_paytest_branch_id)
    assert {"OVERNIGHT", "WAIT_TIME", "PALLETS", "SILOS", "HOURS", "MILES"} <= seeded
    assert "OVERNIGHT" not in await _active_pay_item_codes(
        session_client, auth_token, paytest_branch_id,
    ), "session activation leaked onto a module-owned branch"


def test_activation_fixture_depends_on_the_seeded_fixture_never_the_shadowable_one(request):
    manager = request._fixturemanager
    for name in ("activate_paytest_system_items", "paytest_driver_id"):
        argnames = manager.getfixturedefs(name, request.node)[-1].argnames
        assert "seeded_paytest_branch_id" in argnames, name
        assert "paytest_branch_id" not in argnames, name


# ---------------------------------------------------------------------------
# Retirement must not orphan review state
# ---------------------------------------------------------------------------

async def _insert_review_item(
    direct_db, branch_id: int, request_type: str, status: str, period_id: int | None = None,
) -> int:
    return int((await direct_db.execute(
        text("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requesttype, entityschema, entityname,
                 entityid, title, status, priority)
            VALUES (1, :bid, :rtype, :schema, :entity, :eid, 'B2 retirement probe', :status, 'Normal')
            RETURNING reviewitemid
        """),
        {"bid": branch_id, "rtype": request_type, "status": status,
         "schema": "payroll" if period_id else None,
         "entity": "PayrollPeriods" if period_id else None,
         "eid": str(period_id) if period_id else None},
    )).scalar_one())


async def _insert_review_decision_and_audit(direct_db, branch_id: int, item_id: int) -> None:
    await direct_db.execute(
        text("""
            INSERT INTO review.managerreviewdecisions (reviewitemid, decidedbyuserid, decision)
            VALUES (:rid, 1, 'Comment')
        """), {"rid": item_id},
    )
    await direct_db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode, entityschema, entityname,
                 entityid, reason, sourcetype)
            VALUES (1, :bid, 1, 'ReviewItemCreated', 'review', 'ManagerReviewItems',
                    :eid, 'B2 retirement probe', 'Application')
        """), {"bid": branch_id, "eid": str(item_id)},
    )


async def _review_residue(direct_db, item_ids: list[int]) -> dict[str, int]:
    row = (await direct_db.execute(
        text("""
            SELECT
                (SELECT COUNT(*) FROM review.managerreviewitems WHERE reviewitemid = ANY(:ids)) AS items,
                (SELECT COUNT(*) FROM review.managerreviewdecisions WHERE reviewitemid = ANY(:ids)) AS decisions,
                (SELECT COUNT(*) FROM audit.auditlog
                 WHERE entityname = 'ManagerReviewItems' AND entityid = ANY(:id_strs)) AS audit
        """), {"ids": item_ids, "id_strs": [str(i) for i in item_ids]},
    )).mappings().one()
    return dict(row)


async def test_retire_removes_the_exact_review_state_of_an_in_review_period(direct_db, owned_branch_id):
    period_id = await _insert_period(direct_db, owned_branch_id, "InReview", 4)
    item_id = await _insert_review_item(direct_db, owned_branch_id, "PeriodApproval", "Pending", period_id)
    await _insert_review_decision_and_audit(direct_db, owned_branch_id, item_id)
    # A control on the same branch that no retired period references must survive.
    control_id = await _insert_review_item(direct_db, owned_branch_id, "Other", "Pending")
    await _insert_review_decision_and_audit(direct_db, owned_branch_id, control_id)
    assert (await _review_residue(direct_db, [item_id])) == {"items": 1, "decisions": 1, "audit": 1}
    try:
        await retire_branch_periods_directly(direct_db, owned_branch_id, retain_finalized_history=True)
        await assert_no_mutable_period_state(direct_db, owned_branch_id)
        assert (await direct_db.execute(
            text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )).scalar_one() == "Cancelled"
        assert await _review_residue(direct_db, [item_id]) == {"items": 0, "decisions": 0, "audit": 0}
        assert await _review_residue(direct_db, [control_id]) == {"items": 1, "decisions": 1, "audit": 1}
    finally:
        await delete_review_items_and_children(direct_db, [control_id])
        await delete_period_and_children(direct_db, period_id)


async def test_retire_clears_the_return_pointer_and_review_items_of_returned_and_approved_periods(
    direct_db, owned_branch_id,
):
    returned_id = await _insert_period(direct_db, owned_branch_id, "InReview", 5)
    rejected_item = await _insert_review_item(direct_db, owned_branch_id, "PeriodApproval", "Rejected", returned_id)
    await _insert_review_decision_and_audit(direct_db, owned_branch_id, rejected_item)
    await direct_db.execute(
        text("UPDATE payroll.payrollperiods SET status = 'Returned', currentreturnreviewitemid = :rid "
             "WHERE payrollperiodid = :pid"),
        {"rid": rejected_item, "pid": returned_id},
    )
    approved_id = await _insert_period(direct_db, owned_branch_id, "Approved", 6)
    approved_item = await _insert_review_item(direct_db, owned_branch_id, "PeriodApproval", "Approved", approved_id)
    await _insert_review_decision_and_audit(direct_db, owned_branch_id, approved_item)
    try:
        await retire_branch_periods_directly(direct_db, owned_branch_id, retain_finalized_history=True)
        await assert_no_mutable_period_state(direct_db, owned_branch_id)
        rows = (await direct_db.execute(
            text("SELECT status, currentreturnreviewitemid FROM payroll.payrollperiods "
                 "WHERE payrollperiodid = ANY(:ids)"),
            {"ids": [returned_id, approved_id]},
        )).all()
        assert sorted(rows) == [("Cancelled", None), ("Cancelled", None)]
        assert await _review_residue(direct_db, [rejected_item, approved_item]) == {
            "items": 0, "decisions": 0, "audit": 0,
        }
    finally:
        for period_id in (returned_id, approved_id):
            await delete_period_and_children(direct_db, period_id)


# ---------------------------------------------------------------------------
# Static scanner
# ---------------------------------------------------------------------------

def _scan(sql: str, *, wrap: str = "{!r}") -> list[str]:
    return unsafe_test_source_literals("value = " + wrap.format(sql))


def test_scanner_accepts_the_approved_helpers():
    source = (
        "async def t(db):\n"
        "    async with suspended_test_triggers(db, FINALIZED_HISTORY_TRIGGERS):\n"
        "        pass\n"
        "    async with allow_final_line_insert(db):\n"
        "        pass\n"
    )
    assert unsafe_test_source_literals(source) == []


def test_scanner_rejects_raw_trigger_disable_and_enable():
    for verb in ("DIS", "EN"):
        sql = f"ALTER TABLE payroll.t {_kw(verb, 'ABLE')} TRIGGER trg_x"
        assert _scan(sql), verb
    assert _scan("ALTER TABLE payroll.t " + _kw("DIS", "ABLE") + " TRIGGER ALL")
    assert _scan("ALTER TABLE x " + _kw("ENABLE", " ALWAYS TRIGGER t"))


def test_scanner_rejects_dynamic_trigger_sql_built_with_an_fstring():
    source = f'sql = f"ALTER TABLE {{table}} {_kw("DIS", "ABLE")} TRIGGER {{trigger}}"'
    assert unsafe_test_source_literals(source)


def test_scanner_rejects_raw_session_level_app_guc_mutation():
    guc = "app.allow_payroll_final_line_insert"
    assert _scan(f"SELECT set_config('{guc}', 'true', false)")
    assert _scan(f"SELECT set_config('{guc}','true',false)")
    assert _scan(f"SELECT set_config('{guc}', :value, false)")
    assert _scan(_kw("SE", "T ") + f"{guc} = 'true'")
    assert _scan(_kw("SE", "T SESSION ") + f"{guc} = 'true'")
    assert _scan(_kw("RES", "ET ") + guc)


def test_scanner_rejects_session_replication_role_changes():
    assert _scan(_kw("SE", "T session_replication_role = replica"))


def test_scanner_allows_transaction_local_guc_and_unrelated_settings():
    guc = "app.allow_payroll_final_line_insert"
    assert _scan(f"SELECT set_config('{guc}', 'true', true)") == []
    assert _scan("SELECT set_config('lock_timeout', '5s', false)") == []
    assert _scan(_kw("SE", "T LOCAL ") + f"{guc} = 'true'") == []


def test_scanner_ignores_comments_docstrings_and_bare_strings():
    sql = "ALTER TABLE payroll.t " + _kw("DIS", "ABLE") + " TRIGGER trg_x"
    source = (
        f'"""Module note: never run {sql}."""\n'
        f"# {sql}\n"
        "def helper():\n"
        f'    """Do not {sql}."""\n'
        f"    '{sql}'\n"
        "    return 1\n"
    )
    assert unsafe_test_source_literals(source) == []


def test_scanner_exempts_only_the_approved_infrastructure_files(tmp_path):
    sql = "ALTER TABLE payroll.t " + _kw("DIS", "ABLE") + " TRIGGER trg_x"
    (tmp_path / "test_bad.py").write_text(f"SQL = {sql!r}\n", encoding="utf-8")
    (tmp_path / "db_state.py").write_text(f"SQL = {sql!r}\n", encoding="utf-8")
    sub = tmp_path / "builders"
    sub.mkdir()
    (sub / "helper.py").write_text(f"SQL = {sql!r}\n", encoding="utf-8")
    (sub / "db_state.py").write_text(f"SQL = {sql!r}\n", encoding="utf-8")
    (sub / "test_g0_1_bonus_precision_migration.py").write_text(f"SQL = {sql!r}\n", encoding="utf-8")
    (tmp_path / "test_g0_1_bonus_precision_migration.py").write_text(f"SQL = {sql!r}\n", encoding="utf-8")
    # Exact test-root-relative paths are exempt; a same-named file in a subdirectory is not.
    assert sorted(scan_tests_for_unsafe_sql(tmp_path)) == [
        "builders/db_state.py",
        "builders/helper.py",
        "builders/test_g0_1_bonus_precision_migration.py",
        "test_bad.py",
    ]


def test_backend_tests_contain_no_unapproved_raw_trigger_or_session_guc_sql():
    scanned = list(_TESTS_DIR.rglob("*.py"))
    assert len(scanned) > 100, "the scanner must actually see the test tree"
    violations = scan_tests_for_unsafe_sql(_TESTS_DIR)
    assert not violations, (
        "raw production-trigger suspension or session-level app.* GUC mutation found; "
        "use tests.db_state helpers instead:\n"
        + "\n".join(f"  {path}: {finding}" for path, found in violations.items() for finding in found)
    )
