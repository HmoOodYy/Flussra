"""A source line belongs to the component of its own identity, not just of its kind.

Two components of equal value (two Definitions paying 20 each, two Status columns paying the
same) are indistinguishable to the financial hash, because component links are generated ids that
the hash deliberately leaves out. Membership is therefore enforced by the database keys and,
independently, proved by the verifier from the evidence itself, so approval and finalization
refuse a snapshot whose lines were swapped between components.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.payroll.snapshot_verification import (
    SnapshotEvidenceError,
    load_snapshot_content,
    reconcile_snapshot_content,
    verify_snapshot,
)
from tests.calculation_snapshot_helpers import (
    allow_self_approval,
    day,
    decide,
    enter,
    finalize,
    pending_review_item,
    period_evidence_counts,
    period_status,
    rows,
    snapshot_ids,
    submit,
)
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.db_state import TriggerIdentity, replica_replication_role, suspended_test_triggers
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    get_grid,
    query,
    rated_definition,
    seed_status_key,
)

pytestmark = pytest.mark.asyncio
D = Decimal

CODE = "SNAPSHOT_COMPONENT_MEMBERSHIP_ERROR"
LINES_IMMUTABLE = TriggerIdentity(
    "payroll", "payrollcalculationsnapshotlines", "trg_payrollcalculationsnapshotlines_immutable")


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _two_equal_ordinary_components(client, engine, tenant) -> dict:
    """Two distinct PayDefinitions, one source line each, both paying 20."""
    await rated_definition(client, tenant, rate="10", definition_code="AAA", definition_name="Alpha")
    await rated_definition(client, tenant, rate="10", definition_code="BBB", definition_name="Bravo")
    await assign_weekly_setup(engine, tenant, tenant.branch_a, mask=0)
    period = await create_period(client, tenant, tenant.branch_a)
    columns = (await get_grid(client, tenant, period))["columns"]
    first, second = columns[0]["payroll_period_definition_id"], columns[1]["payroll_period_definition_id"]
    await enter(client, tenant, period, 0, ppd=first, quantity="2")
    await enter(client, tenant, period, 1, ppd=second, quantity="2")
    return period


def _swap_pair(tenant, period, column: str) -> tuple[list[int], list[int]]:
    """The two line ids and the two component ids of the first and second source family."""
    snapshot_id = snapshot_ids(tenant, period)[-1]
    components = [r[0] for r in rows(tenant, f"""
        SELECT payrollcalculationsnapshotcomponentid FROM payroll.payrollcalculationsnapshotcomponents
        WHERE payrollcalculationsnapshotid = %s AND {column} IS NOT NULL
        ORDER BY payrollcalculationsnapshotcomponentid
    """, snapshot_id)]
    lines = [r[0] for r in rows(tenant, f"""
        SELECT l.payrollcalculationsnapshotlineid FROM payroll.payrollcalculationsnapshotlines l
        JOIN payroll.payrollcalculationdrivertotals t
          ON t.payrollcalculationdrivertotalid = l.payrollcalculationdrivertotalid
        WHERE t.payrollcalculationsnapshotid = %s AND l.{column} IS NOT NULL
        ORDER BY l.payrollcalculationsnapshotlineid
    """, snapshot_id)]
    assert len(components) == len(lines) == 2
    return lines, components


_SWAP = text("""
    UPDATE payroll.payrollcalculationsnapshotlines
    SET payrollcalculationsnapshotcomponentid = CASE payrollcalculationsnapshotlineid
            WHEN CAST(:l1 AS BIGINT) THEN CAST(:c2 AS BIGINT) WHEN CAST(:l2 AS BIGINT) THEN CAST(:c1 AS BIGINT) END
    WHERE payrollcalculationsnapshotlineid IN (CAST(:l1 AS BIGINT), CAST(:l2 AS BIGINT))
""")


async def _keyed_swap(engine, lines, components) -> str:
    """The swap with only the immutability trigger suspended: the error of the identity key."""
    async with engine.begin() as conn:
        async with suspended_test_triggers(conn, [LINES_IMMUTABLE]):
            savepoint = await conn.begin_nested()
            with pytest.raises(IntegrityError) as keyed:
                await conn.execute(_SWAP, {
                    "l1": lines[0], "l2": lines[1], "c1": components[0], "c2": components[1]})
            await savepoint.rollback()
    return str(keyed.value)


async def _bypass_swap(engine, lines, components) -> None:
    """The controlled bypass: the swap with every trigger and key suppressed."""
    async with engine.begin() as conn:
        async with replica_replication_role(conn):
            await conn.execute(_SWAP, {
                "l1": lines[0], "l2": lines[1], "c1": components[0], "c2": components[1]})


async def test_equal_valued_components_with_correctly_linked_lines_verify(
    p3c_client, p3c_engine, tenant,
):
    period = await _two_equal_ordinary_components(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)

    (snapshot_id,) = snapshot_ids(tenant, period)
    async with p3c_engine.connect() as conn:
        content = await verify_snapshot(conn, snapshot_id=snapshot_id)
    components = content["DriverTotals"][0]["Components"]
    assert [c["OfficialAmount"] for c in components] == [D("20"), D("20")]
    assert len({c["PayrollPeriodDefinitionID"] for c in components}) == 2


async def test_the_database_refuses_to_move_a_line_to_a_component_of_another_identity(
    p3c_client, p3c_engine, tenant,
):
    period = await _two_equal_ordinary_components(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    lines, components = _swap_pair(tenant, period, "payrollperioddefinitionid")

    # Evidence is immutable: a plain update never reaches the key.
    with pytest.raises(psycopg2.Error) as refused:
        query(tenant, "UPDATE payroll.payrollcalculationsnapshotlines "
                      "SET payrollcalculationsnapshotcomponentid = %s "
                      "WHERE payrollcalculationsnapshotlineid = %s", (components[1], lines[0]))
    assert "payroll_calculation_snapshot_immutable" in str(refused.value)

    # With only the immutability trigger suspended the identity key itself refuses the swap.
    keyed = await _keyed_swap(p3c_engine, lines, components)
    assert "fk_payrollcalculationsnapshotlines_ordinarycomponentidentity" in keyed.lower()
    assert rows(tenant, "SELECT payrollcalculationsnapshotcomponentid FROM "
                        "payroll.payrollcalculationsnapshotlines "
                        "WHERE payrollcalculationsnapshotlineid = %s", lines[0]) == [(components[0],)]


async def test_a_line_swap_that_bypasses_the_database_keys_is_rejected_by_verification(
    p3c_client, p3c_engine, tenant,
):
    period = await _two_equal_ordinary_components(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    (snapshot_id,) = snapshot_ids(tenant, period)
    lines, components = _swap_pair(tenant, period, "payrollperioddefinitionid")
    async with p3c_engine.connect() as conn:
        before = await load_snapshot_content(conn, snapshot_id=snapshot_id)

    await _bypass_swap(p3c_engine, lines, components)

    async with p3c_engine.connect() as conn:
        after = await load_snapshot_content(conn, snapshot_id=snapshot_id)
        # Everything the hash covers is unchanged: amounts, identities and rate evidence.
        assert [ln["SourceID"] for t in after["DriverTotals"] for ln in t["Lines"]] == [
            ln["SourceID"] for t in before["DriverTotals"] for ln in t["Lines"]]
        with pytest.raises(SnapshotEvidenceError) as failed:
            await verify_snapshot(conn, snapshot_id=snapshot_id)
    assert failed.value.code == CODE and failed.value.http().status_code == 422


async def test_approval_is_refused_for_a_snapshot_whose_lines_were_swapped(
    p3c_client, p3c_engine, tenant,
):
    period = await _two_equal_ordinary_components(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    item_id = await pending_review_item(tenant, period)
    await _bypass_swap(p3c_engine, *_swap_pair(tenant, period, "payrollperioddefinitionid"))

    refused = await decide(p3c_client, tenant, item_id, expect=422)

    assert refused.json()["detail"].startswith(CODE)
    assert period_status(tenant, period) == "InReview"
    assert rows(tenant, "SELECT status FROM review.managerreviewitems WHERE reviewitemid = %s",
                item_id) == [("Pending",)]


async def test_finalization_is_refused_for_a_snapshot_whose_lines_were_swapped(
    p3c_client, p3c_engine, tenant,
):
    period = await _two_equal_ordinary_components(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period))
    await _bypass_swap(p3c_engine, *_swap_pair(tenant, period, "payrollperioddefinitionid"))

    refused = await finalize(p3c_client, tenant, period, expect=422)

    assert refused.json()["detail"].startswith(CODE)
    assert period_status(tenant, period) == "Approved"
    assert period_evidence_counts(tenant, period)["final_lines"] == 0


async def _two_equal_status_components(client, engine, tenant) -> dict:
    """Two Status rate columns, one entry each, paying the same."""
    # A Branch has one Status rate column per RateType, so two columns need two RateTypes.
    for code, default in (("PAIDA", True), ("PAIDB", False)):
        rate_type_id = query(tenant, """
            INSERT INTO payroll.ratetypes (ratecode, ratename, unitname)
            VALUES (%s, %s, 'hour') RETURNING ratetypeid
        """, (f"STATUS_{code}_{tenant.company_id}", f"Status {code}"))[0][0]
        column_id = query(tenant, """
            INSERT INTO payroll.statusratecolumns
                (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE) RETURNING statusratecolumnid
        """, (tenant.company_id, tenant.branch_a, rate_type_id, f"Status {code}", f"STATUS {code}",
              default))[0][0]
        seed_status_key(tenant, code, off=False, hoursvalue=D("1"), statusratecolumnid=column_id)
        query(tenant, """
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverassignmentid, ratetypeid, amount, effectivefrom, status,
                 createdbyuserid, approvedbyuserid, approvedatutc)
            VALUES (%s, %s, %s, %s, 5, DATE '2000-01-01', 'Approved', %s, %s, NOW())
        """, (tenant.company_id, tenant.branch_a, tenant.driver_a, rate_type_id,
              tenant.owner, tenant.owner))
    await assign_weekly_setup(engine, tenant, tenant.branch_a, mask=0)
    period = await create_period(client, tenant, tenant.branch_a)
    await enter(client, tenant, period, 0, status_key="PAIDA")
    await enter(client, tenant, period, 1, status_key="PAIDB")
    assert day(period, 0) != day(period, 1)
    return period


async def test_status_lines_swapped_between_equal_status_columns_are_rejected(
    p3c_client, p3c_engine, tenant,
):
    period = await _two_equal_status_components(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    (snapshot_id,) = snapshot_ids(tenant, period)
    lines, components = _swap_pair(tenant, period, "statusratecolumnid")
    async with p3c_engine.connect() as conn:
        content = await verify_snapshot(conn, snapshot_id=snapshot_id)      # correctly linked
    assert [c["OfficialAmount"] for c in content["DriverTotals"][0]["Components"]] == [
        D("5"), D("5")]

    keyed = await _keyed_swap(p3c_engine, lines, components)
    assert "fk_payrollcalculationsnapshotlines_statuscomponentidentity" in keyed.lower()

    await _bypass_swap(p3c_engine, lines, components)
    async with p3c_engine.connect() as conn:
        with pytest.raises(SnapshotEvidenceError) as failed:
            await verify_snapshot(conn, snapshot_id=snapshot_id)
    assert failed.value.code == CODE


async def test_membership_is_proved_from_the_evidence_alone(p3c_client, p3c_engine, tenant):
    """The verifier needs no database key: each way a line can be misplaced is refused."""
    period = await _two_equal_ordinary_components(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    (snapshot_id,) = snapshot_ids(tenant, period)
    async with p3c_engine.connect() as conn:
        content = await load_snapshot_content(conn, snapshot_id=snapshot_id)
    reconcile_snapshot_content(content)

    def broken(change):
        import copy
        clone = copy.deepcopy(content)
        change(clone["DriverTotals"][0])
        return clone

    first, second = content["DriverTotals"][0]["Components"]
    cases = {
        "another component": lambda t: t["Lines"][0].update(
            {"PayrollCalculationSnapshotComponentID": second["PayrollCalculationSnapshotComponentID"]}),
        "a component of no Driver total": lambda t: t["Lines"][0].update(
            {"PayrollCalculationSnapshotComponentID": 987654}),
        "another kind": lambda t: t["Lines"][0].update({"LineType": "STATUS"}),
        "another pay definition": lambda t: t["Lines"][0].update({"PayDefinitionID": 987654}),
        "another rate definition": lambda t: t["Lines"][0].update({"RateDefinitionID": 987654}),
        "a component left without a source line": lambda t: t["Lines"].pop(),
    }
    for label, change in cases.items():
        with pytest.raises(SnapshotEvidenceError) as failed:
            reconcile_snapshot_content(broken(change))
        assert failed.value.code == CODE, label
