"""Configurable Company and Branch capacity for compensation concepts."""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.compensation import branch_config, definitions
from app.compensation.schemas import (
    BranchConfigUpdate,
    PayDefinitionDirectCreate,
    PayDefinitionRequestDecision,
)
from tests.compensation_api_fixtures import (
    build_tenant,
    create_definition,
    p3c_application,  # noqa: F401 - register shared fixtures
    p3c_database_engine,  # noqa: F401 - register shared fixtures
    p3c_http_client,  # noqa: F401 - register shared fixtures
    p3c_tenant,  # noqa: F401 - register shared fixtures
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.ownership import track_function_client_pay_definitions
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    period_definitions,
)
from tests.test_p3c_compensation_concurrency import _blocked_with_pid

pytestmark = pytest.mark.asyncio


async def test_definition_tracking_does_not_suppress_body_failure(
    test_app, test_engine,
):
    import httpx
    from httpx import ASGITransport

    async with httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="body failed"):
            async with track_function_client_pay_definitions(client, test_engine):
                raise RuntimeError("body failed")


async def test_definition_tracking_reports_body_and_cleanup_errors_and_continues(
    monkeypatch,
):
    import httpx

    from app.compensation import definitions
    from tests.ownership import track_function_client_pay_definitions

    class Result:
        def mappings(self):
            return self

        def one_or_none(self):
            return {"companyid": 1, "userid": 7}

    class Connection:
        async def execute(self, *_args, **_kwargs):
            return Result()

    class Transaction:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    class Engine:
        def begin(self):
            return Transaction()

    created_ids = iter((101, 102))
    attempts = []

    async def response(request):
        return httpx.Response(
            201, json={"company_id": 1, "pay_definition_id": next(created_ids)}, request=request,
        )

    async def retire(_company_id, _user_id, pay_definition_id, _db):
        attempts.append(pay_definition_id)
        if pay_definition_id == 101:
            raise ValueError("first retirement failed")

    monkeypatch.setattr(definitions, "retire_definition", retire)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response), base_url="http://test") as client:
        with pytest.raises(ExceptionGroup) as failure:
            async with track_function_client_pay_definitions(client, Engine()):
                await client.post("/compensation/pay-definitions")
                await client.post("/compensation/pay-definitions")
                raise RuntimeError("body failed")
    assert attempts == [101, 102]
    assert any(isinstance(error, RuntimeError) for error in failure.value.exceptions)
    assert any(isinstance(error, ValueError) for error in failure.value.exceptions)


async def test_function_client_retires_only_its_created_definitions(
    session_client, auth_token, test_engine, test_app,
):
    import httpx
    from httpx import ASGITransport
    from sqlalchemy import text

    headers = {"Authorization": f"Bearer {auth_token}"}
    session_created = await session_client.post(
        "/compensation/pay-definitions", headers=headers, json=_definition_body(),
    )
    assert session_created.status_code == 201, session_created.text
    session_id = session_created.json()["pay_definition_id"]
    company_id = session_created.json()["company_id"]

    async with httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        async with track_function_client_pay_definitions(client, test_engine):
            function_created = await client.post(
                "/compensation/pay-definitions", headers=headers, json=_definition_body(),
            )
            assert function_created.status_code == 201, function_created.text
            function_id = function_created.json()["pay_definition_id"]
            assert function_created.json()["company_id"] == company_id

    async with test_engine.connect() as db:
        rows = (await db.execute(text("""
            SELECT paydefinitionid, status FROM payroll.paydefinitions
            WHERE companyid=:company AND paydefinitionid = ANY(:ids)
            ORDER BY paydefinitionid
        """), {"company": company_id, "ids": [session_id, function_id]})).all()
        history = await db.scalar(text("""
            SELECT count(*) FROM payroll.ratedefinitions
            WHERE companyid=:company AND paydefinitionid=:definition
        """), {"company": company_id, "definition": function_id})
    statuses = {int(row.paydefinitionid): row.status for row in rows}
    assert statuses[session_id] == "Active"
    assert statuses[function_id] == "Retired"
    assert history == 1
    retired = await session_client.post(
        f"/compensation/pay-definitions/{session_id}/retire", headers=headers,
    )
    assert retired.status_code == 200, retired.text


async def test_repeated_function_client_scopes_do_not_accumulate_active_definitions(
    auth_token, test_engine, test_app,
):
    import httpx
    from httpx import ASGITransport
    from sqlalchemy import text

    headers = {"Authorization": f"Bearer {auth_token}"}
    active_before = None
    async with test_engine.connect() as db:
        active_before = await db.scalar(text("""
            SELECT count(*) FROM payroll.paydefinitions p
            JOIN core.companies c ON c.companyid=p.companyid
            WHERE c.companycode='DEMO' AND p.status='Active'
        """))
    async with httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        for _ in range(51):
            async with track_function_client_pay_definitions(client, test_engine):
                response = await client.post(
                    "/compensation/pay-definitions", headers=headers, json=_definition_body(),
                )
                assert response.status_code == 201, response.text
    async with test_engine.connect() as db:
        active_after = await db.scalar(text("""
            SELECT count(*) FROM payroll.paydefinitions p
            JOIN core.companies c ON c.companyid=p.companyid
            WHERE c.companycode='DEMO' AND p.status='Active'
        """))
    assert active_after == active_before


@pytest.fixture(name="tenant")
def capacity_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _branch_today(engine, company_id: int, branch_id: int) -> date:
    async with engine.connect() as conn:
        return await conn.scalar(
            text("SELECT core.fn_BranchToday(:company, :branch)"),
            {"company": company_id, "branch": branch_id},
        )


async def _count(engine, sql: str, **params) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(text(sql), params))


def _definition_body(code: str | None = None) -> dict:
    return {
        "definition_code": code or f"CAP_{uuid4().hex[:12]}",
        "definition_name": "Capacity test input",
        "input_type": "Decimal",
        "unit": "unit",
        "calculation_method": "PerUnit",
    }


async def _create(client, tenant, *, code: str | None = None):
    return await client.post(
        "/compensation/pay-definitions", json=_definition_body(code), headers=tenant.admin,
    )


async def _configure(client, tenant, branch_id: int, definition: dict, *, active: bool = True,
                     effective_from: date | None = None):
    response = await client.patch(
        f"/compensation/branches/{branch_id}/pay-definitions/{definition['pay_definition_id']}",
        json={"is_active": active,
              "effective_from": effective_from.isoformat() if effective_from else None},
        headers=tenant.admin,
    )
    return response


async def _definition_counts(engine, tenant, pay_definition_id: int) -> tuple[int, int, int]:
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT
                (SELECT count(*) FROM payroll.paydefinitions
                 WHERE companyid = :company AND paydefinitionid = :definition),
                (SELECT count(*) FROM payroll.ratedefinitions
                 WHERE companyid = :company AND paydefinitionid = :definition),
                (SELECT count(*) FROM payroll.paydefinitionprovenance
                 WHERE companyid = :company AND paydefinitionid = :definition)
        """), {"company": tenant.company_id, "definition": pay_definition_id})).one()
        return tuple(map(int, row))


async def test_default_company_capacity_accepts_fifty_and_rejects_fifty_first(
    p3c_client, tenant,
):
    for _ in range(50):
        response = await _create(p3c_client, tenant)
        assert response.status_code == 201, response.text

    rejected = await _create(p3c_client, tenant)

    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "COMPANY_PAY_ITEM_CAPACITY_EXCEEDED"
    assert "50" in rejected.json()["detail"]["message"]


async def test_default_branch_capacity_counts_one_status_and_allows_noop_at_limit(
    p3c_client, p3c_app, tenant,
):
    definitions = [await create_definition(p3c_client, tenant) for _ in range(15)]

    for definition in definitions[:14]:
        response = await _configure(p3c_client, tenant, tenant.branch_a, definition)
        assert response.status_code == 200, response.text

    noop = await _configure(p3c_client, tenant, tenant.branch_a, definitions[0])
    rejected = await _configure(p3c_client, tenant, tenant.branch_a, definitions[14])

    assert noop.status_code == 200, noop.text
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "BRANCH_INPUT_CAPACITY_EXCEEDED"
    assert "16" in rejected.json()["detail"]["message"]
    assert "15" in rejected.json()["detail"]["message"]


async def test_http_settings_override_company_and_branch_capacity_and_failed_approval_rolls_back(
    p3c_client, p3c_app, p3c_engine, tenant, monkeypatch,
):
    settings = p3c_app.state.settings
    monkeypatch.setattr(settings, "PRODUCT_MAX_COMPANY_PAY_ITEMS", 1)
    first = await _create(p3c_client, tenant)
    assert first.status_code == 201, first.text
    first_definition = first.json()

    draft = await p3c_client.post("/compensation/pay-definition-requests",
                                  headers=tenant.headers(tenant.branch_user),
                                  json={"requesting_branch_id": tenant.branch_a,
                                        **_definition_body()})
    assert draft.status_code == 201, draft.text
    submitted = await p3c_client.post(
        f"/compensation/pay-definition-requests/{draft.json()['request_id']}/submit",
        headers=tenant.headers(tenant.branch_user),
        json={"expected_revision": draft.json()["revision"]},
    )
    assert submitted.status_code == 200, submitted.text

    counts_before = await _definition_counts(p3c_engine, tenant, first_definition["pay_definition_id"])
    rejected_approval = await p3c_client.post(
        f"/compensation/pay-definition-requests/{submitted.json()['request_id']}/decide",
        headers=tenant.admin,
        json={"action": "Approve", "expected_revision": submitted.json()["revision"],
              "reason": "capacity test"},
    )
    assert rejected_approval.status_code == 422
    assert rejected_approval.json()["detail"]["code"] == "COMPANY_PAY_ITEM_CAPACITY_EXCEEDED"
    request_after = await p3c_client.get(
        f"/compensation/pay-definition-requests/{submitted.json()['request_id']}",
        headers=tenant.admin,
    )
    assert request_after.json()["status"] == "PendingCompanyApproval"
    assert request_after.json()["revision"] == submitted.json()["revision"]
    assert await _definition_counts(p3c_engine, tenant, first_definition["pay_definition_id"]) == counts_before
    assert await _count(p3c_engine, "SELECT count(*) FROM payroll.paydefinitions WHERE companyid=:c",
                        c=tenant.company_id) == 1

    # Raising the effective Settings value is observed by the next HTTP operation.
    monkeypatch.setattr(settings, "PRODUCT_MAX_COMPANY_PAY_ITEMS", 2)
    accepted_approval = await p3c_client.post(
        f"/compensation/pay-definition-requests/{submitted.json()['request_id']}/decide",
        headers=tenant.admin,
        json={"action": "Approve", "expected_revision": submitted.json()["revision"],
              "reason": "capacity raised"},
    )
    assert accepted_approval.status_code == 200, accepted_approval.text

    monkeypatch.setattr(settings, "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", 2)
    branch_second = accepted_approval.json()["approved_pay_definition_id"]
    initially_deactivated = await p3c_client.patch(
        f"/compensation/branches/{tenant.branch_a}/pay-definitions/{branch_second}",
        headers=tenant.admin, json={"is_active": False},
    )
    assert initially_deactivated.status_code == 200, initially_deactivated.text
    branch_first = first_definition
    active = await _configure(p3c_client, tenant, tenant.branch_a, branch_first)
    assert active.status_code == 200, active.text
    rejected_branch = await p3c_client.patch(
        f"/compensation/branches/{tenant.branch_a}/pay-definitions/{branch_second}",
        headers=tenant.admin, json={"is_active": True},
    )
    assert rejected_branch.status_code == 422
    assert rejected_branch.json()["detail"]["code"] == "BRANCH_INPUT_CAPACITY_EXCEEDED"

    monkeypatch.setattr(settings, "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", 3)
    raised_branch = await p3c_client.patch(
        f"/compensation/branches/{tenant.branch_a}/pay-definitions/{branch_second}",
        headers=tenant.admin, json={"is_active": True},
    )
    assert raised_branch.status_code == 200, raised_branch.text

    # Even if a limit is reduced below the current count, deactivation remains possible.
    monkeypatch.setattr(settings, "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", 2)
    deactivated = await _configure(p3c_client, tenant, tenant.branch_a, branch_first,
                                   active=False)
    assert deactivated.status_code == 200, deactivated.text


async def test_retirement_frees_company_and_branch_slots_without_deleting_configuration(
    p3c_client, p3c_app, p3c_engine, tenant, monkeypatch,
):
    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_COMPANY_PAY_ITEMS", 1)
    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", 2)
    original = await create_definition(p3c_client, tenant)
    configured = await _configure(
        p3c_client, tenant, tenant.branch_a, original, effective_from=date(2000, 1, 1))
    assert configured.status_code == 200, configured.text
    async with p3c_engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
            SELECT roles.companyroleid, permissions.permissioncode
            FROM (
                SELECT DISTINCT companyroleid FROM sec.userbranchroles
                WHERE companyid=:company AND userid=:owner
            ) AS roles
            CROSS JOIN unnest(ARRAY[
                'payroll_setup.manage', 'payroll_setup.publish',
                'payroll_setup.assign', 'payroll_setup.view', 'payroll.period.create'
            ]) AS permissions(permissioncode)
            ON CONFLICT DO NOTHING
        """), {"company": tenant.company_id, "owner": tenant.owner})
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    before_period = await create_period(p3c_client, tenant, tenant.branch_a)
    frozen_before = period_definitions(before_period["payroll_period_id"], tenant)
    from tests.compensation_api_fixtures import authored_schedule

    schedule = await authored_schedule(
        p3c_client, tenant, original, amount="25", effective_from=str(date.today()))
    assert (await _create(p3c_client, tenant)).status_code == 422

    retired = await p3c_client.post(
        f"/compensation/pay-definitions/{original['pay_definition_id']}/retire",
        headers=tenant.admin,
    )
    assert retired.status_code == 200, retired.text
    replacement_response = await _create(p3c_client, tenant)
    assert replacement_response.status_code == 201, replacement_response.text
    replacement = replacement_response.json()
    activated = await _configure(p3c_client, tenant, tenant.branch_a, replacement)
    assert activated.status_code == 200, activated.text
    assert period_definitions(before_period["payroll_period_id"], tenant) == frozen_before

    async with p3c_engine.connect() as conn:
        retained = (await conn.execute(text("""
            SELECT pd.status, pd.definitioncode, rd.ratedefinitionid,
                   (SELECT count(*) FROM payroll.ratecomponentdefinitions rcd
                    WHERE rcd.ratedefinitionid=rd.ratedefinitionid),
                   (SELECT count(*) FROM payroll.paydefinitionprovenance prov
                    WHERE prov.paydefinitionid=pd.paydefinitionid),
                   (SELECT count(*) FROM payroll.driverrateschedules drs
                    WHERE drs.ratedefinitionid=rd.ratedefinitionid AND drs.status='Approved'),
                   (SELECT count(*) FROM payroll.payrollperioddefinitions ppd
                    WHERE ppd.paydefinitionid=pd.paydefinitionid
                      AND ppd.payrollperiodid=:period)
            FROM payroll.paydefinitions pd
            JOIN payroll.ratedefinitions rd ON rd.paydefinitionid=pd.paydefinitionid
            WHERE pd.companyid=:company AND pd.paydefinitionid=:definition
        """), {"company": tenant.company_id,
               "definition": original["pay_definition_id"],
               "period": before_period["payroll_period_id"]})).one()
    assert tuple(retained) == (
        "Retired", original["definition_code"], original["rate_definition_id"], 1, 1,
        1, 1,
    )
    assert schedule["status"] == "Approved"

    assert await _count(p3c_engine, """
        SELECT count(*) FROM payroll.branchpaydefinitionconfig
        WHERE companyid=:company AND branchid=:branch AND paydefinitionid=:definition
    """, company=tenant.company_id, branch=tenant.branch_a,
         definition=original["pay_definition_id"]) == 1


async def test_company_count_excludes_inactive_and_other_company_definitions(
    p3c_client, p3c_engine, tenant, p3b_dsn, monkeypatch, p3c_app,
):
    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_COMPANY_PAY_ITEMS", 1)
    original = await create_definition(p3c_client, tenant)
    other_tenant = build_tenant(p3b_dsn)
    other_company_definition = await _create(p3c_client, other_tenant)
    assert other_company_definition.status_code == 201, other_company_definition.text

    async with p3c_engine.begin() as conn:
        await conn.execute(text("""
            UPDATE payroll.paydefinitions SET status='Inactive'
            WHERE companyid=:company AND paydefinitionid=:definition
        """), {"company": tenant.company_id,
               "definition": original["pay_definition_id"]})
    next_definition = await _create(p3c_client, tenant)
    assert next_definition.status_code == 201, next_definition.text


async def test_future_peak_rejects_activation_that_fits_on_its_start_date(
    p3c_client, p3c_app, tenant, monkeypatch,
):
    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", 3)
    today = await _branch_today(p3c_app.state.engine, tenant.company_id, tenant.branch_a)
    future = today + timedelta(days=15)
    active_today, future_item, attempted = [await create_definition(p3c_client, tenant)
                                             for _ in range(3)]
    assert (await _configure(p3c_client, tenant, tenant.branch_a, active_today)).status_code == 200
    assert (await _configure(p3c_client, tenant, tenant.branch_a, future_item,
                             effective_from=future)).status_code == 200

    rejected = await _configure(p3c_client, tenant, tenant.branch_a, attempted)

    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "BRANCH_INPUT_CAPACITY_EXCEEDED"
    assert str(future) in rejected.json()["detail"]["message"]
    assert "4" in rejected.json()["detail"]["message"]


async def test_failed_branch_capacity_approval_rolls_back_definition_and_request(
    p3c_client, p3c_app, p3c_engine, tenant, monkeypatch,
):
    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_COMPANY_PAY_ITEMS", 3)
    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", 2)
    existing = await create_definition(p3c_client, tenant)
    assert (await _configure(p3c_client, tenant, tenant.branch_a, existing)).status_code == 200
    definition_code = f"ROLLBACK_{uuid4().hex[:10]}"
    draft = await p3c_client.post(
        "/compensation/pay-definition-requests",
        headers=tenant.headers(tenant.branch_user),
        json={"requesting_branch_id": tenant.branch_a,
              **_definition_body(definition_code)},
    )
    assert draft.status_code == 201, draft.text
    submitted = await p3c_client.post(
        f"/compensation/pay-definition-requests/{draft.json()['request_id']}/submit",
        headers=tenant.headers(tenant.branch_user),
        json={"expected_revision": draft.json()["revision"]},
    )
    assert submitted.status_code == 200, submitted.text
    request_id = submitted.json()["request_id"]
    before_audit = await _count(p3c_engine,
                                "SELECT count(*) FROM audit.auditlog WHERE companyid=:c",
                                c=tenant.company_id)

    rejected = await p3c_client.post(
        f"/compensation/pay-definition-requests/{request_id}/decide",
        headers=tenant.admin,
        json={"action": "Approve", "expected_revision": submitted.json()["revision"],
              "reason": "branch capacity"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "BRANCH_INPUT_CAPACITY_EXCEEDED"
    assert await _count(p3c_engine,
                        "SELECT count(*) FROM payroll.paydefinitions "
                        "WHERE companyid=:c AND definitioncode=:code",
                        c=tenant.company_id, code=definition_code) == 0
    assert await _count(p3c_engine, """
        SELECT count(*) FROM payroll.ratedefinitions rd
        JOIN payroll.paydefinitions pd ON pd.paydefinitionid=rd.paydefinitionid
        WHERE pd.companyid=:c AND pd.definitioncode=:code
    """, c=tenant.company_id, code=definition_code) == 0
    assert await _count(p3c_engine, """
        SELECT count(*) FROM payroll.ratecomponentdefinitions rcd
        JOIN payroll.ratedefinitions rd ON rd.ratedefinitionid=rcd.ratedefinitionid
        JOIN payroll.paydefinitions pd ON pd.paydefinitionid=rd.paydefinitionid
        WHERE pd.companyid=:c AND pd.definitioncode=:code
    """, c=tenant.company_id, code=definition_code) == 0
    assert await _count(p3c_engine, """
        SELECT count(*) FROM payroll.paydefinitionprovenance
        WHERE companyid=:c AND sourcerequestid=:request
    """, c=tenant.company_id, request=request_id) == 0
    assert await _count(p3c_engine, """
        SELECT count(*) FROM payroll.paydefinitionrequestevents
        WHERE paydefinitionrequestid=:request AND eventtype='Approved'
    """, request=request_id) == 0
    assert await _count(p3c_engine,
                        "SELECT count(*) FROM audit.auditlog WHERE companyid=:c",
                        c=tenant.company_id) == before_audit
    unchanged_request = await p3c_client.get(
        f"/compensation/pay-definition-requests/{request_id}", headers=tenant.admin)
    assert unchanged_request.json()["status"] == "PendingCompanyApproval"
    assert unchanged_request.json()["revision"] == submitted.json()["revision"]

    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", 3)
    accepted = await p3c_client.post(
        f"/compensation/pay-definition-requests/{request_id}/decide",
        headers=tenant.admin,
        json={"action": "Approve", "expected_revision": submitted.json()["revision"],
              "reason": "capacity raised"},
    )
    assert accepted.status_code == 200, accepted.text


async def test_branch_capacity_checks_future_peaks_and_effective_date_boundaries(
    p3c_client, p3c_app, tenant, monkeypatch,
):
    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", 2)
    today = await _branch_today(p3c_app.state.engine, tenant.company_id, tenant.branch_a)
    day_20, day_30 = (today + timedelta(days=n) for n in (20, 30))
    alpha, beta, gamma = [await create_definition(p3c_client, tenant) for _ in range(3)]

    assert (await _configure(p3c_client, tenant, tenant.branch_a, alpha)).status_code == 200
    assert (await _configure(p3c_client, tenant, tenant.branch_a, alpha,
                             active=False, effective_from=day_20)).status_code == 200
    assert (await _configure(p3c_client, tenant, tenant.branch_a, beta,
                             effective_from=day_20)).status_code == 200

    before_boundary = await _configure(
        p3c_client, tenant, tenant.branch_a, gamma, effective_from=day_20 - timedelta(days=1))
    assert before_boundary.status_code == 422
    assert before_boundary.json()["detail"]["code"] == "BRANCH_INPUT_CAPACITY_EXCEEDED"
    assert str(day_20 - timedelta(days=1)) in before_boundary.json()["detail"]["message"]

    # The old interval ends on day 29; a new one starts on day 30. Inclusive interval
    # boundaries are swept as a single date so the adjacent handoff uses one ordinary slot.
    assert (await _configure(p3c_client, tenant, tenant.branch_a, beta,
                             active=False, effective_from=day_30)).status_code == 200
    assert (await _configure(p3c_client, tenant, tenant.branch_a, gamma,
                             effective_from=day_30)).status_code == 200

    # Moving an open future version earlier must re-check the resulting complete timeline.
    amended = await _configure(
        p3c_client, tenant, tenant.branch_a, beta,
        effective_from=today + timedelta(days=25))
    assert amended.status_code == 422
    assert amended.json()["detail"]["code"] == "BRANCH_INPUT_CAPACITY_EXCEEDED"
    assert str(day_30) in amended.json()["detail"]["message"]


async def test_bulk_branch_activation_rolls_back_earlier_branch_write_and_audit(
    p3c_client, p3c_app, p3c_engine, tenant, monkeypatch,
):
    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", 2)
    full = await create_definition(p3c_client, tenant)
    assert (await _configure(p3c_client, tenant, tenant.branch_b, full)).status_code == 200
    new = await create_definition(p3c_client, tenant)
    response = await p3c_client.patch(
        f"/compensation/pay-definitions/{new['pay_definition_id']}/branch-config",
        headers=tenant.admin,
        json={"target": "AllBranches", "is_active": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "BRANCH_INPUT_CAPACITY_EXCEEDED"
    assert await _count(p3c_engine, """
        SELECT count(*) FROM payroll.branchpaydefinitionconfig
        WHERE companyid=:company AND paydefinitionid=:definition
    """, company=tenant.company_id, definition=new["pay_definition_id"]) == 0
    assert await _count(p3c_engine, """
        SELECT count(*) FROM audit.auditlog
        WHERE companyid=:company AND entityname='BranchPayDefinitionConfig'
          AND entityid IN (:first, :second)
    """, company=tenant.company_id,
         first=f"{tenant.branch_a}:{new['pay_definition_id']}",
         second=f"{tenant.branch_b}:{new['pay_definition_id']}") == 0


async def test_concurrent_last_company_slot_direct_create_and_approval_serialize(
    p3c_client, p3c_app, p3c_engine, tenant, monkeypatch,
):
    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_COMPANY_PAY_ITEMS", 1)
    draft = await p3c_client.post(
        "/compensation/pay-definition-requests",
        headers=tenant.headers(tenant.branch_user),
        json={"requesting_branch_id": tenant.branch_a, **_definition_body()},
    )
    submitted = await p3c_client.post(
        f"/compensation/pay-definition-requests/{draft.json()['request_id']}/submit",
        headers=tenant.headers(tenant.branch_user),
        json={"expected_revision": draft.json()["revision"]},
    )
    request_id = submitted.json()["request_id"]
    policy = p3c_app.state.settings.product_policy
    direct_body = PayDefinitionDirectCreate.model_validate(_definition_body())
    decision = PayDefinitionRequestDecision(
        action="Approve", expected_revision=submitted.json()["revision"], reason="race")

    async def approve(conn):
        return await definitions.decide_request(
            tenant.company_id, tenant.owner, request_id, decision, conn, policy=policy)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            direct_result = await definitions.create_direct(
                tenant.company_id, tenant.owner, direct_body, holder, policy=policy)
            approval, waiter_pid = await _blocked_with_pid(p3c_engine, approve)
            async with p3c_engine.connect() as observer:
                blockers = await observer.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})
            assert blockers == [holder_pid]

    approval_result = await approval
    assert direct_result.status == "Active"
    assert isinstance(approval_result, HTTPException)
    assert approval_result.detail["code"] == "COMPANY_PAY_ITEM_CAPACITY_EXCEEDED"
    assert await _count(p3c_engine, """
        SELECT count(*) FROM payroll.paydefinitions
        WHERE companyid=:company AND status='Active'
    """, company=tenant.company_id) == 1
    request_after = await p3c_client.get(
        f"/compensation/pay-definition-requests/{request_id}", headers=tenant.admin)
    assert request_after.json()["status"] == "PendingCompanyApproval"


async def test_concurrent_last_branch_slot_activations_serialize(
    p3c_client, p3c_app, p3c_engine, tenant, monkeypatch,
):
    monkeypatch.setattr(p3c_app.state.settings, "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", 2)
    first, second = [await create_definition(p3c_client, tenant) for _ in range(2)]

    policy = p3c_app.state.settings.product_policy

    async def activate_second(conn):
        return await branch_config.update_branch_config(
            tenant.company_id, tenant.owner, tenant.branch_a, second["pay_definition_id"],
            BranchConfigUpdate(is_active=True), conn, policy=policy)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            first_result = await branch_config.update_branch_config(
                tenant.company_id, tenant.owner, tenant.branch_a, first["pay_definition_id"],
                BranchConfigUpdate(is_active=True), holder, policy=policy)
            activation, waiter_pid = await _blocked_with_pid(p3c_engine, activate_second)
            async with p3c_engine.connect() as observer:
                blockers = await observer.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})
            assert blockers == [holder_pid]

    activation_result = await activation
    assert first_result.pay_definition_id == first["pay_definition_id"]
    assert isinstance(activation_result, HTTPException)
    assert activation_result.detail["code"] == "BRANCH_INPUT_CAPACITY_EXCEEDED"
    active = await _count(p3c_engine, """
        SELECT count(*) FROM payroll.branchpaydefinitionconfig bpic
        JOIN payroll.paydefinitions pd
          ON pd.paydefinitionid=bpic.paydefinitionid AND pd.companyid=bpic.companyid
        WHERE bpic.companyid=:company AND bpic.branchid=:branch
          AND bpic.isactive AND pd.status='Active'
          AND bpic.effectivefrom <= core.fn_BranchToday(:company, :branch)
          AND (bpic.effectiveto IS NULL OR
               bpic.effectiveto >= core.fn_BranchToday(:company, :branch))
    """, company=tenant.company_id, branch=tenant.branch_a)
    assert active == 1
