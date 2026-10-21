"""FinalLine provenance is compared under strict JSON typing.

The SourceSnapshot of a FinalLine is evidence, so it must equal its projection value for value and
type for type: Python would call ``True`` equal to ``1`` and ``1.0`` equal to ``1``, which JSON does
not. The whole document is compared, with the same comparator wherever parsed JSON evidence is
compared with an expected value.
"""
from __future__ import annotations

import pytest

from app.payroll.final_line_projection import verify_final_lines
from app.payroll.snapshot_hash import json_native, strict_json_equal
from app.payroll.snapshot_verification import SnapshotEvidenceError, verify_snapshot
from tests.calculation_snapshot_helpers import (
    finalize,
    snapshot_ids,
    standard_scenario,
    submit_and_approve,
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
from tests.db_state import replica_replication_role
from tests.payroll_runtime_fixtures import build_payroll_tenant


@pytest.mark.parametrize("left, right", [
    (1, 1), (0, 0), (True, True), (False, False), ("1", "1"), (None, None), ("", ""),
    ([1, "a", None], [1, "a", None]), ({"a": 1, "b": [True]}, {"b": [True], "a": 1}),
])
def test_equal_json_documents_are_equal(left, right):
    assert strict_json_equal(left, right)


@pytest.mark.parametrize("left, right", [
    (True, 1), (1, True), (False, 0), (0, False), (1, 1.0), (1.0, 1), (1.0, 1.0),
    ("1", 1), (1, "1"), (None, 0), (0, None), (None, False), (None, ""), ("", None),
    ([1], 1), (1, [1]), ({}, []), ([], {}), ({"a": 1}, "a"), ("abc", 5),
    ([1, 2], [2, 1]), ([1], [1, 1]), ({"a": 1}, {"a": 1, "b": 2}), ({"a": 1}, {"b": 1}),
    ({"a": True}, {"a": 1}), ([True], [1]), ({"a": None}, {"a": 0}),
])
def test_json_documents_of_another_type_or_shape_are_not_equal(left, right):
    assert not strict_json_equal(left, right)


def test_the_expected_projection_is_normalized_the_way_it_is_stored():
    from decimal import Decimal
    assert json_native({"ids": [1, 2], "amount": "0.6666", "none": None}) == {
        "ids": [1, 2], "amount": "0.6666", "none": None}
    with pytest.raises(TypeError):
        json_native({"amount": Decimal("1")})        # amounts are stored as strings



@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


_FIRST_LINE = ("WHERE finallineid = (SELECT min(finallineid) FROM payroll.payrollfinallines "
               "WHERE payrollperiodid = :period)")

# (label, replacement of one SourceSnapshot field, or a whole-document edit)
_TYPE_CONFUSION = {
    "a boolean for the revision number 1": (
        "jsonb_set(sourcesnapshot, '{revision_number}', 'true'::jsonb)"),
    "a fraction form of the revision number": (
        "jsonb_set(sourcesnapshot, '{revision_number}', '1.0'::jsonb)"),
    "a string for the revision number": (
        "jsonb_set(sourcesnapshot, '{revision_number}', '\"1\"'::jsonb)"),
    "a boolean for the snapshot id": (
        "jsonb_set(sourcesnapshot, '{payroll_calculation_snapshot_id}', 'true'::jsonb)"),
    "a boolean zero for the snapshot id": (
        "jsonb_set(sourcesnapshot, '{payroll_calculation_snapshot_id}', 'false'::jsonb)"),
    "null for a populated id": (
        "jsonb_set(sourcesnapshot, '{snapshot_component_id}', 'null'::jsonb)"),
    "a list for an id": (
        "jsonb_set(sourcesnapshot, '{payroll_calculation_snapshot_id}', '[1]'::jsonb)"),
    "an object for the currency code": (
        "jsonb_set(sourcesnapshot, '{currency_code}', '{\"code\": \"USD\"}'::jsonb)"),
    "a number for the hash": (
        "jsonb_set(sourcesnapshot, '{snapshot_hash}', '5'::jsonb)"),
    "an extra key": "jsonb_set(sourcesnapshot, '{extra}', '1'::jsonb)",
    "a missing key": "sourcesnapshot - 'currency_code'",
    "a source id list of numbers": (
        "jsonb_set(sourcesnapshot, '{source_ids}', '[1, 2]'::jsonb)"),
}


async def _finalized(client, engine, tenant) -> dict:
    scenario = await standard_scenario(client, engine, tenant)
    await submit_and_approve(client, tenant, scenario.period)
    await finalize(client, tenant, scenario.period)
    return scenario.period


async def _set_provenance(engine, period, expression: str) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        async with replica_replication_role(conn):
            result = await conn.execute(text(
                f"UPDATE payroll.payrollfinallines SET sourcesnapshot = {expression} {_FIRST_LINE}"),
                {"period": period["payroll_period_id"]})
            assert result.rowcount == 1


@pytest.mark.asyncio
async def test_the_legitimate_provenance_of_every_final_line_verifies(
    p3c_client, p3c_engine, tenant,
):
    period = await _finalized(p3c_client, p3c_engine, tenant)
    (snapshot_id,) = snapshot_ids(tenant, period)

    async with p3c_engine.connect() as conn:
        content = await verify_snapshot(conn, snapshot_id=snapshot_id)
        rows = await verify_final_lines(conn, content)

    assert len(rows) == 3
    overview = await p3c_client.get(
        f"/payroll/finalized/{period['payroll_period_id']}/overview", headers=tenant.admin)
    assert overview.status_code == 200
    assert overview.json()["section_availability"]["financials"]["state"] == "AVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(_TYPE_CONFUSION))
async def test_provenance_of_another_json_type_or_shape_fails_the_final_lines_closed(
    p3c_client, p3c_engine, tenant, label,
):
    period = await _finalized(p3c_client, p3c_engine, tenant)
    pid = period["payroll_period_id"]
    (snapshot_id,) = snapshot_ids(tenant, period)
    await _set_provenance(p3c_engine, period, _TYPE_CONFUSION[label])

    async with p3c_engine.connect() as conn:
        content = await verify_snapshot(conn, snapshot_id=snapshot_id)
        with pytest.raises(SnapshotEvidenceError) as failed:
            await verify_final_lines(conn, content)
    assert failed.value.code == "FINAL_LINES_INTEGRITY_ERROR", label

    for path in (f"/payroll/periods/{pid}/final-lines", f"/payroll/finalized/{pid}/overview",
                 f"/payroll/finalized/{pid}/reports/drivers", f"/payroll/periods/{pid}/reports/drivers"):
        response = await p3c_client.get(path, headers=tenant.admin)
        assert response.status_code == 422, (label, path, response.status_code)
        assert response.json()["detail"].startswith("FINAL_LINES_INTEGRITY_ERROR"), (label, path)
    summary = (await p3c_client.get(f"/payroll/periods/{pid}", headers=tenant.admin)).json()
    assert summary["final_gross"] is None and summary["financial_authority"] == "UNAVAILABLE"
