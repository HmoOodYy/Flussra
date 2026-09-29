"""Canonical Payroll Setup authority invariants formerly covered by CP-2A.

The retired BranchPayrollSettings/PayrollScheduleVersions architecture is
covered only by the Phase 8 migration tests. These tests retain the business
invariants that still matter: candidate creation freezes the exact canonical
assignment/version, and later policy publication cannot rewrite that history.
"""
from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import text


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _context(direct_db, suffix: str) -> dict[str, int | datetime.date]:
    tenant = (await direct_db.execute(text("""
        SELECT c.companyid, u.userid
        FROM core.companies c
        JOIN sec.users u ON u.companyid = c.companyid
        WHERE c.companycode = 'DEMO' AND u.username = 'admin'
        LIMIT 1
    """))).mappings().one()
    anchor = datetime.date(2094, 1, 7)
    branch_id = int((await direct_db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (:company_id, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {
        "company_id": tenant["companyid"], "code": f"CP2A_{uuid.uuid4().hex[:10]}",
        "name": f"CP2A {suffix}",
    })).scalar_one())
    setup_code = f"CP2A_{uuid.uuid4().hex[:12]}"
    setup_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollsetups
            (companyid, setupcode, setupname, status, createdbyuserid)
        VALUES (:company_id, :setup_code, :setup_name, 'Active', :user_id)
        RETURNING payrollsetupid
    """), {
        "company_id": tenant["companyid"], "setup_code": setup_code,
        "setup_name": f"CP2A {suffix}", "user_id": tenant["userid"],
    })).scalar_one())
    version_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollsetupversions
            (companyid, payrollsetupid, lifecyclestate, versionnumber,
             effectivefromdate, payrollfrequency, anchorstartdate,
             normaldaysoffmask, confighash, publishedbyuserid, publishedatutc)
        VALUES (:company_id, :setup_id, 'Published', 1, :anchor, 'Week', :anchor,
                0, :config_hash, :user_id, NOW())
        RETURNING payrollsetupversionid
    """), {
        "company_id": tenant["companyid"], "setup_id": setup_id, "anchor": anchor,
        "config_hash": "c" * 64, "user_id": tenant["userid"],
    })).scalar_one())
    assignment_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.branchpayrollsetupassignments
            (companyid, branchid, payrollsetupid, effectivefromdate, createdbyuserid)
        VALUES (:company_id, :branch_id, :setup_id, :anchor, :user_id)
        RETURNING branchpayrollsetupassignmentid
    """), {
        "company_id": tenant["companyid"], "branch_id": branch_id,
        "setup_id": setup_id, "anchor": anchor, "user_id": tenant["userid"],
    })).scalar_one())
    await direct_db.commit()
    return {
        "company_id": int(tenant["companyid"]), "user_id": int(tenant["userid"]),
        "branch_id": branch_id, "setup_id": setup_id, "version_id": version_id,
        "assignment_id": assignment_id, "setup_code": setup_code, "anchor": anchor,
    }


async def _create_candidate_period(client, token: str, context: dict) -> int:
    preview = await client.get(
        f"/payroll/branches/{context['branch_id']}/period-candidates",
        params={"mode": "OPEN_CREATION"}, headers=_auth(token),
    )
    assert preview.status_code == 200, preview.text
    created = await client.post(
        f"/payroll/branches/{context['branch_id']}/period-creations",
        json={"candidate_key": preview.json()["selected"]["candidate_key"]},
        headers=_auth(token),
    )
    assert created.status_code in (200, 201), created.text
    return int(created.json()["payroll_period_id"])


@pytest.mark.asyncio
async def test_candidate_period_freezes_canonical_authority(
    session_client, auth_token, direct_db,
):
    context = await _context(direct_db, "FREEZE")
    period_id = await _create_candidate_period(session_client, auth_token, context)

    period = (await direct_db.execute(text("""
        SELECT branchpayrollsetupassignmentid, payrollsetupversionid,
               frozenpayrollsetupid, frozenpayrollsetupcode,
               frozenpayrollsetupversionnumber, frozenpayrollfrequency,
               frozenanchorstartdate, frozennormaldaysoffmask, scheduleconfighash
        FROM payroll.payrollperiods WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})).mappings().one()
    assert period["branchpayrollsetupassignmentid"] == context["assignment_id"]
    assert period["payrollsetupversionid"] == context["version_id"]
    assert period["frozenpayrollsetupid"] == context["setup_id"]
    assert period["frozenpayrollsetupversionnumber"] == 1
    assert period["frozenpayrollfrequency"] == "Week"
    assert period["frozenanchorstartdate"] == context["anchor"]

    days = (await direct_db.execute(text("""
        SELECT DISTINCT branchpayrollsetupassignmentid, payrollsetupversionid
        FROM payroll.payrollperioddays WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})).all()
    assert len(days) == 1
    assert days[0] == (context["assignment_id"], context["version_id"])


@pytest.mark.asyncio
async def test_published_successor_cannot_rewrite_frozen_period_authority(
    session_client, auth_token, direct_db,
):
    context = await _context(direct_db, "SUCCESSOR")
    period_id = await _create_candidate_period(session_client, auth_token, context)

    successor_date = context["anchor"] + datetime.timedelta(days=7)
    successor_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollsetupversions
            (companyid, payrollsetupid, lifecyclestate, versionnumber,
             effectivefromdate, payrollfrequency, anchorstartdate,
             normaldaysoffmask, confighash, publishedbyuserid, publishedatutc)
        VALUES (:company_id, :setup_id, 'Published', 2, :effective_date,
                'Week', :effective_date, 1, :config_hash, :user_id, NOW())
        RETURNING payrollsetupversionid
    """), {
        "company_id": context["company_id"], "setup_id": context["setup_id"],
        "effective_date": successor_date, "config_hash": "d" * 64,
        "user_id": context["user_id"],
    })).scalar_one())
    await direct_db.commit()

    authority = (await direct_db.execute(text("""
        SELECT branchpayrollsetupassignmentid, payrollsetupversionid,
               frozennormaldaysoffmask
        FROM payroll.payrollperiods WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})).one()
    assert authority == (context["assignment_id"], context["version_id"], 0)
    assert successor_id != context["version_id"]
