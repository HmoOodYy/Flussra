"""Period creation is held until the creator snapshots the target PayDefinition layout."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.payroll import period_creation
from tests.test_cp1c_candidate_creation import cp1c_setup  # noqa: F401 - registers the fixture


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _period_state(db, branch_id: int) -> dict[str, int]:
    counts = {}
    for table in ("payrollperiods", "payrollperiodpayitems", "payrollperioddays",
                  "payrollperioddrivereligibility", "payrollperiodeligibilitysnapshots"):
        column = "branchid" if table in ("payrollperiods", "payrollperiodpayitems") else None
        if column:
            sql = f"SELECT count(*) FROM payroll.{table} WHERE branchid = :bid"
        elif table == "payrollperioddays":
            sql = ("SELECT count(*) FROM payroll.payrollperioddays d JOIN payroll.payrollperiods p "
                   "ON p.payrollperiodid = d.payrollperiodid WHERE p.branchid = :bid")
        else:
            sql = (f"SELECT count(*) FROM payroll.{table} d JOIN payroll.payrollperiods p "
                   "ON p.payrollperiodid = d.payrollperiodid WHERE p.branchid = :bid")
        counts[table] = (await db.execute(text(sql), {"bid": branch_id})).scalar_one()
    return counts


@pytest.mark.parametrize("mode", ["OPEN_CREATION", "PREPARED_CREATION"])
async def test_candidates_remain_readable_but_confirmation_fails_closed(
    session_client, auth_token, cp1c_setup, direct_db, mode,  # noqa: F811
):
    branch_id = cp1c_setup["branch_id"]
    preview = await session_client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": mode}, headers=_auth(auth_token))
    assert preview.status_code == 200, preview.text
    key = preview.json()["selected"]["candidate_key"]
    before = await _period_state(direct_db, branch_id)

    created = await session_client.post(
        f"/payroll/branches/{branch_id}/period-creations",
        json={"candidate_key": key}, headers=_auth(auth_token))

    assert created.status_code == 409, created.text
    assert created.json()["detail"]["code"] == "TARGET_PAYROLL_LAYOUT_NOT_READY"
    assert await _period_state(direct_db, branch_id) == before
    assert before["payrollperiods"] == 0


async def test_the_hold_precedes_every_persisted_period_row_and_has_no_runtime_switch(
    session_client, auth_token, cp1c_setup, direct_db, monkeypatch,  # noqa: F811
):
    branch_id = cp1c_setup["branch_id"]
    # Neither the environment nor settings can lift the hold.
    monkeypatch.setenv("PAYROLL_ALLOW_LEGACY_PERIOD_LAYOUT", "1")
    with pytest.raises(HTTPException) as hold:
        period_creation._require_target_payroll_layout()
    assert hold.value.status_code == 409
    assert hold.value.detail["code"] == "TARGET_PAYROLL_LAYOUT_NOT_READY"

    preview = await session_client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": "OPEN_CREATION"}, headers=_auth(auth_token))
    key = preview.json()["selected"]["candidate_key"]
    created = await session_client.post(
        f"/payroll/branches/{branch_id}/period-creations",
        json={"candidate_key": key}, headers=_auth(auth_token))
    assert created.status_code == 409
    assert (await _period_state(direct_db, branch_id))["payrollperiods"] == 0
