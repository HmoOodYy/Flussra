"""GET /compensation/drivers/{driver_id}/compensation-access reports the backend's decisions.

A client must not re-derive "may this caller see Pending rows / discard / approve" from the
Driver's tenures: a company-wide reader or editor keeps that standing after every tenure has
ended, while a past Branch's reader never has it. The endpoint returns the read scope's
``full`` flag, the discard authority as a boolean and whether any tenure is current or pending.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.builders.access import create_company_role_with_permissions, create_user_with_role_token
from tests.builders.transfer_scenarios import auth, db_now, sfx
from tests.builders.workforce_db import end_driver_assignment
from tests.test_driver_owned_rate_schedules import _manager, _transfer, _world
from tests.test_ended_assignment_write_guard import _expect

pytestmark = pytest.mark.asyncio


async def _access(client, token, driver_id):
    return await client.get(f"/compensation/drivers/{driver_id}/compensation-access", headers=auth(token))


def _flags(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


async def _company_wide(client, admin, permissions) -> str:
    role = await create_company_role_with_permissions(client, admin, f"DOS cw {sfx()}", permissions)
    return await create_user_with_role_token(client, admin, f"dos_cw_{sfx()}", role)


async def test_a_past_branch_reader_has_neither_full_history_nor_discard(client, auth_token, direct_db):
    w = await _world(client, auth_token, direct_db)
    viewer_role = await create_company_role_with_permissions(
        client, auth_token, f"DOS viewer {sfx()}", ["drivers.view", "payrates.view"])
    viewer = await _manager(client, auth_token, viewer_role, w.old_branch)
    await _transfer(client, w)  # the Driver now works in the new Branch; the old tenure ended

    assert _flags(await _access(client, viewer, w.driver_id)) == {
        "full_history": False, "can_discard_pending": False, "has_current_or_pending_tenure": True}
    # The current authority Branch has the full picture and may discard.
    assert _flags(await _access(client, w.new_manager, w.driver_id)) == {
        "full_history": True, "can_discard_pending": True, "has_current_or_pending_tenure": True}
    # The old Branch's editor still reads only its past, and cannot discard Pending rows now.
    assert _flags(await _access(client, w.old_manager, w.driver_id)) == {
        "full_history": False, "can_discard_pending": False, "has_current_or_pending_tenure": True}


async def test_an_unrelated_branch_and_a_driver_self_account_get_403(client, auth_token, direct_db):
    w = await _world(client, auth_token, direct_db)
    assert (await _access(client, w.other_manager, w.driver_id)).status_code == 403
    roles = (await client.get("/admin/company-roles", headers=auth(auth_token))).json()
    driver_role = next(r["company_role_id"] for r in roles if r["role_code"] == "DRIVER")
    self_token = await create_user_with_role_token(
        client, auth_token, f"dos_self_{sfx()}", driver_role,
        scope_type="Self", driver_branch_id=w.old_branch, password="TestPass1234!")
    assert (await _access(client, self_token, w.driver_id)).status_code == 403
    assert (await _access(client, auth_token, 2_000_000_000)).status_code == 404


async def test_company_wide_callers_keep_their_standing_after_every_tenure_ended(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    reader = await _company_wide(client, auth_token, ["drivers.view", "payrates.view"])
    await end_driver_assignment(
        direct_db, w.source, ended_at=await db_now(direct_db) - timedelta(hours=1),
        end_reason="Terminated")
    await direct_db.commit()

    # A company-wide reader still sees Pending rows (full history) but cannot discard.
    assert _flags(await _access(client, reader, w.driver_id)) == {
        "full_history": True, "can_discard_pending": False, "has_current_or_pending_tenure": False}
    # A company-wide editor may also discard an orphaned Pending row (approval stays refused).
    assert _flags(await _access(client, auth_token, w.driver_id)) == {
        "full_history": True, "can_discard_pending": True, "has_current_or_pending_tenure": False}
    # A Branch reader whose only tenure ended gets the past-Branch view.
    old_reader_role = await create_company_role_with_permissions(
        client, auth_token, f"DOS old viewer {sfx()}", ["drivers.view", "payrates.view"])
    old_reader = await _manager(client, auth_token, old_reader_role, w.old_branch)
    assert _flags(await _access(client, old_reader, w.driver_id)) == {
        "full_history": False, "can_discard_pending": False, "has_current_or_pending_tenure": False}
    await _expect(await client.get(
        f"/compensation/drivers/{w.driver_id}/rate-schedules/summary", headers=auth(old_reader)), 200)


# ---------------------------------------------------------------------------
# History is discovered from the Driver's schedules, not from the authoring matrix
# ---------------------------------------------------------------------------

async def test_schedules_of_a_retired_pay_definition_stay_discoverable_but_not_authorable(
    client, auth_token, direct_db,
):
    from tests.test_driver_owned_rate_schedules import LONG_AGO, _author, _create
    from tests.test_ended_assignment_write_guard import _definition

    w = await _world(client, auth_token, direct_db)
    retired = await _definition(client, auth_token)
    active = await _definition(client, auth_token)
    schedule = await _author(client, w.old_manager, w, retired, LONG_AGO)
    other = await _author(client, w.old_manager, w, active, LONG_AGO)
    viewer_role = await create_company_role_with_permissions(
        client, auth_token, f"DOS history {sfx()}", ["drivers.view", "payrates.view"])
    old_reader = await _manager(client, auth_token, viewer_role, w.old_branch)
    company_reader = await _company_wide(client, auth_token, ["drivers.view", "payrates.view"])

    # The real retirement path allows a definition that has approved schedules.
    done = await client.post(
        f"/compensation/pay-definitions/{retired['pay_definition_id']}/retire", headers=auth(auth_token))
    await _expect(done, 200)
    await _transfer(client, w)  # the old tenure ends

    for token in (old_reader, company_reader, auth_token):
        summary = (await _expect(await client.get(
            f"/compensation/drivers/{w.driver_id}/rate-schedules/summary", headers=auth(token)), 200)).json()
        by_definition = {row["rate_definition_id"]: row for row in summary}
        item = by_definition[retired["rate_definition_id"]]
        assert item["pay_definition_status"] == "Retired"
        assert item["definition_name"] == retired["definition_name"]
        assert item["definition_code"] == retired["definition_code"]
        assert item["approved_schedule_id"] == schedule
        assert by_definition[active["rate_definition_id"]]["pay_definition_status"] == "Active"
        history = (await _expect(await client.get(
            f"/compensation/drivers/{w.driver_id}/rate-definitions/{retired['rate_definition_id']}"
            "/rate-schedules", headers=auth(token)), 200)).json()
        assert [row["driver_rate_schedule_id"] for row in history] == [schedule]
        assert other in {row["approved_schedule_id"] for row in summary}

    # The authoring matrix stays Active-only, and a retired definition cannot be authored.
    for token in (old_reader, company_reader, auth_token):
        matrix = (await _expect(await client.get(
            f"/compensation/drivers/{w.driver_id}/pay-rates", headers=auth(token)), 200)).json()
        assert retired["pay_definition_id"] not in {row["pay_definition_id"] for row in matrix}
    refused = await _create(client, w.new_manager, w, retired, "2026-12-01")
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "PAY_DEFINITION_NOT_ACTIVE"


async def test_a_retired_definitions_pending_schedule_is_discoverable_and_discardable_but_not_approvable(
    client, auth_token, direct_db,
):
    from tests.test_driver_owned_rate_schedules import LONG_AGO, SCHEDULES, _filled
    from tests.test_ended_assignment_write_guard import _definition

    w = await _world(client, auth_token, direct_db)
    retired = await _definition(client, auth_token)
    pending = await _filled(client, w.old_manager, w, retired, LONG_AGO)
    await _expect(await client.post(
        f"/compensation/pay-definitions/{retired['pay_definition_id']}/retire", headers=auth(auth_token)), 200)
    await end_driver_assignment(
        direct_db, w.source, ended_at=await db_now(direct_db) - timedelta(hours=1),
        end_reason="Terminated")
    await direct_db.commit()  # no current or pending tenure is left: the Pending row is orphaned

    # The company-wide editor's backend decisions, and the summary/history that expose the row.
    assert _flags(await _access(client, auth_token, w.driver_id)) == {
        "full_history": True, "can_discard_pending": True, "has_current_or_pending_tenure": False}
    summary = (await _expect(await client.get(
        f"/compensation/drivers/{w.driver_id}/rate-schedules/summary", headers=auth(auth_token)), 200)).json()
    item = next(row for row in summary if row["rate_definition_id"] == retired["rate_definition_id"])
    assert item["pending_schedule_id"] == pending and item["pay_definition_status"] == "Retired"
    history = (await _expect(await client.get(
        f"/compensation/drivers/{w.driver_id}/rate-definitions/{retired['rate_definition_id']}"
        "/rate-schedules", headers=auth(auth_token)), 200)).json()
    assert [(row["driver_rate_schedule_id"], row["status"]) for row in history] == [(pending, "Pending")]
    assert history[0]["values"][0]["amount"] is not None

    # Approval stays refused (ended tenure and retired definition); discard succeeds.
    refused = await client.post(f"{SCHEDULES}/{pending}/approve", headers=auth(auth_token))
    assert refused.status_code in (409, 422), refused.text
    assert (await client.delete(f"{SCHEDULES}/{pending}", headers=auth(auth_token))).status_code == 204
    assert (await client.get(f"{SCHEDULES}/{pending}", headers=auth(auth_token))).status_code == 404
