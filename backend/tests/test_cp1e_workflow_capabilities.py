"""
CP-1E: GET /payroll/current-workflow — hub-ready workflow slots, alerts, capabilities.

Product contracts verified:
  A. Slots: correct mapping of DB statuses to slot positions
  B. Capabilities: period-level workflow/permission gating (no data validation)
  C. Candidate capabilities: slot-matrix and permission gating
  D. Permissions/security: driver block, ODA block, branch scoping, permission checks
  E. Alerts: correct alert codes and affected_action_codes

Key correction: Returned backlog blocks can_submit_for_review only;
                it does NOT block can_create_prepared_candidate.

Dates: 2097-* — isolated year, no conflict with other test suites.
Run from backend/:
    python -m pytest tests/test_cp1e_workflow_capabilities.py -v
"""
import datetime
import itertools
import uuid

import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

# ---------------------------------------------------------------------------
# Unique-username counter (avoids collisions between security tests)
# ---------------------------------------------------------------------------

_SEC_CTR = itertools.count(1)


def _sec_uid() -> str:
    return f"cp1e{next(_SEC_CTR):04d}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMPANY_ID = 1
_DATE_CTR = itertools.count(0)
_BASE_DATE = datetime.date(2097, 1, 6)  # Monday


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _week(offset: int = 0) -> tuple[datetime.date, datetime.date]:
    n = next(_DATE_CTR) + offset
    start = _BASE_DATE + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


async def _clean(db: AsyncConnection, branch_id: int) -> None:
    # Cancel Returned periods first (clears FK + satisfies ck_payrollperiods_returnedpointerconsistency)
    await db.execute(
        _text("""
            UPDATE payroll.payrollperiods
            SET status = 'Cancelled', currentreturnreviewitemid = NULL
            WHERE branchid = :bid AND status = 'Returned'
        """),
        {"bid": branch_id},
    )
    # Cancel remaining active periods
    await db.execute(
        _text("""
            UPDATE payroll.payrollperiods
            SET status = 'Cancelled'
            WHERE branchid = :bid AND status IN ('Draft', 'Open', 'InReview')
        """),
        {"bid": branch_id},
    )
    # Delete only review rows not retained by immutable P6D evidence.
    await db.execute(
        _text("""
            DELETE FROM review.managerreviewdecisions decision
            WHERE decision.reviewitemid IN (
                SELECT item.reviewitemid FROM review.managerreviewitems item WHERE item.branchid = :bid
            )
            AND NOT EXISTS (
                SELECT 1 FROM payroll.payrollperiodworkflowactionevidence evidence
                WHERE evidence.reviewdecisionid = decision.reviewdecisionid
            )
        """),
        {"bid": branch_id},
    )
    await db.execute(
        _text("""
            DELETE FROM review.managerreviewitems item
            WHERE item.branchid = :bid
              AND NOT EXISTS (
                  SELECT 1 FROM payroll.payrollperiodauditevidenceevents evidence
                  WHERE evidence.reviewitemid = item.reviewitemid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM review.managerreviewdecisions decision
                  WHERE decision.reviewitemid = item.reviewitemid
              )
        """),
        {"bid": branch_id},
    )
    await db.execute(
        _text("""
            DELETE FROM payroll.payrolldraftlines
            WHERE payrollperiodid IN (
                SELECT payrollperiodid FROM payroll.payrollperiods
                WHERE branchid = :bid AND startdate >= '2097-01-01'
            )
        """),
        {"bid": branch_id},
    )
    await db.execute(
        _text(
            "DELETE FROM payroll.payrollperiods "
            "WHERE branchid = :bid AND startdate >= '2097-01-01'"
        ),
        {"bid": branch_id},
    )
    await db.commit()


async def _insert_period(
    db: AsyncConnection,
    branch_id: int,
    status: str,
    start: datetime.date,
    end: datetime.date,
    suffix: str = "",
) -> int:
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (:cid, :bid, :st, :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {
            "cid": _COMPANY_ID, "bid": branch_id, "st": status,
            "code": f"2097-{status[:2].upper()}{suffix}-{start}",
            "name": f"{status} {start}{suffix}",
            "start": start, "end": end,
        },
    )).mappings().first()
    await db.commit()
    return row["payrollperiodid"]


async def _insert_returned(
    db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    suffix: str = "",
) -> tuple[int, int]:
    """Insert review item + Returned period. Returns (period_id, review_item_id)."""
    ri_row = (await db.execute(
        _text("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requestedbyuserid, requesttype,
                 entityschema, entityname, entityid,
                 title, description, priority, status,
                 finaldecisionbyuserid, finaldecisionatutc, finaldecisionreason)
            VALUES
                (:cid, :bid, 1, 'PeriodApproval',
                 'payroll', 'PayrollPeriods', '0',
                 'CP-1E test return', 'CP-1E test return', 'Normal', 'Rejected',
                 1, NOW(), 'CP-1E test')
            RETURNING reviewitemid
        """),
        {"cid": _COMPANY_ID, "bid": branch_id},
    )).mappings().first()
    ri_id = ri_row["reviewitemid"]

    per_row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype,
                 startdate, enddate, currentreturnreviewitemid)
            VALUES (:cid, :bid, 'Returned', :code, :name, 'Week', :start, :end, :ri_id)
            RETURNING payrollperiodid
        """),
        {
            "cid": _COMPANY_ID, "bid": branch_id,
            "code": f"2097-RET{suffix}-{start}",
            "name": f"Returned {start}{suffix}",
            "start": start, "end": end, "ri_id": ri_id,
        },
    )).mappings().first()
    await db.commit()
    return per_row["payrollperiodid"], ri_id


async def _setup_payroll_weekly(client, token, branch_id: int) -> None:
    r = await client.put(
        f"/settings/branches/{branch_id}/payroll-setup",
        json={"payroll_frequency": "Week", "anchor_start_date": "2097-01-06"},
        headers=_auth(token),
    )
    assert r.status_code in (200, 201), f"payroll-setup: {r.text}"


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_client, auth_token, session_db_conn):
    """Use a fresh branch so slot/capability tests do not share period history."""
    branch_code = f"CP1E_{uuid.uuid4().hex[:10]}"
    row = (await session_db_conn.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """),
        {"code": branch_code, "name": f"CP1E {branch_code}"},
    )).mappings().first()
    await session_db_conn.commit()
    branch_id = row["branchid"]
    await _setup_payroll_weekly(session_client, auth_token, branch_id)
    return branch_id


# ---------------------------------------------------------------------------
# Security test helpers (mirroring test_security_matrix.py patterns)
# ---------------------------------------------------------------------------

async def _create_user(client, admin_token: str, username: str) -> dict:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": username,
            "password": "TestPass1234!",
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, f"Create user {username!r}: {resp.text}"
    return resp.json()


async def _login_as(client, username: str, password: str = "TestPass1234!") -> str:
    resp = await client.post("/auth/login", json={
        "username": username, "password": password, "company_code": "DEMO",
    })
    assert resp.status_code == 200, f"Login {username!r}: {resp.text}"
    return resp.json()["access_token"]


async def _get_driver_role_id(client, admin_token: str) -> int:
    resp = await client.get("/admin/company-roles", headers=_auth(admin_token))
    assert resp.status_code == 200
    role = next((r for r in resp.json() if r["role_code"] == "DRIVER"), None)
    assert role is not None, "DRIVER company role not found"
    return role["company_role_id"]


async def _create_role_with_perms(
    client, admin_token: str, role_name: str, perms: list[str],
) -> int:
    cr = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=_auth(admin_token),
    )
    assert cr.status_code == 201, f"Create role: {cr.text}"
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=_auth(admin_token),
        )
        assert pr.status_code == 200, f"Set perms: {pr.text}"
    return role_id


async def _assign_role(
    client, admin_token: str, user_id: int, role_id: int,
    scope: str = "AllCompanyBranches", branch_id: int | None = None,
) -> None:
    payload: dict = {"company_role_id": role_id, "scope_type": scope}
    if branch_id is not None:
        payload["branch_id"] = branch_id
    resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=payload,
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, f"Assign role: {resp.text}"


async def _make_driver_user(client, admin_token: str, branch_id: int) -> str:
    """Create SpecificBranch DRIVER-role user; return its JWT."""
    uname = _sec_uid()
    driver_role_id = await _get_driver_role_id(client, admin_token)
    user = await _create_user(client, admin_token, uname)
    await _assign_role(client, admin_token, user["user_id"], driver_role_id,
                       scope="SpecificBranch", branch_id=branch_id)
    return await _login_as(client, uname)


async def _make_oda_user(client, admin_token: str, branch_id: int) -> str:
    """Create OwnDriverDataOnly user; return its JWT."""
    uname = _sec_uid()
    driver_role_id = await _get_driver_role_id(client, admin_token)
    user = await _create_user(client, admin_token, uname)
    await _assign_role(client, admin_token, user["user_id"], driver_role_id,
                       scope="OwnDriverDataOnly", branch_id=branch_id)
    return await _login_as(client, uname)


async def _get_workflow(
    client, token, branch_id: int | None = None
) -> httpx.Response:
    params = {}
    if branch_id is not None:
        params["branch_id"] = branch_id
    return await client.get(
        "/payroll/current-workflow",
        params=params,
        headers=_auth(token),
    )


def _branch_entry(resp: dict, branch_id: int) -> dict:
    for b in resp["branches"]:
        if b["branch_id"] == branch_id:
            return b
    raise AssertionError(f"branch {branch_id} not in response: {resp}")


# ===========================================================================
# Group A: Slots
# ===========================================================================

class TestSlots:

    @pytest.mark.asyncio
    async def test_a01_no_active_period_slots_null(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """A01: No active periods → all slots None."""
        await _clean(direct_db, paytest_branch_id)
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        assert resp.status_code == 200, resp.text
        b = _branch_entry(resp.json(), paytest_branch_id)
        slots = b["slots"]
        assert slots["open"] is None
        assert slots["prepared"] is None
        assert slots["in_review"] is None
        assert slots["returned"] is None

    @pytest.mark.asyncio
    async def test_a02_open_only(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """A02: Open period → open slot populated, others None."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        pid = await _insert_period(direct_db, paytest_branch_id, "Open", s, e, "A02")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        assert resp.status_code == 200
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["slots"]["open"]["period_id"] == pid
        assert b["slots"]["prepared"] is None
        assert b["slots"]["in_review"] is None
        assert b["slots"]["returned"] is None

    @pytest.mark.asyncio
    async def test_a03_open_and_draft_prepared_slot(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """A03: Open + Draft → Draft surfaces as prepared slot with display_status='Prepared'."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        s2 = e + datetime.timedelta(days=1)
        e2 = s2 + datetime.timedelta(days=6)
        await _insert_period(direct_db, paytest_branch_id, "Open", s, e, "A03O")
        dpid = await _insert_period(direct_db, paytest_branch_id, "Draft", s2, e2, "A03D")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        assert resp.status_code == 200
        b = _branch_entry(resp.json(), paytest_branch_id)
        prepared = b["slots"]["prepared"]
        assert prepared is not None
        assert prepared["period_id"] == dpid
        assert prepared["status"] == "Draft"
        assert prepared["display_status"] == "Prepared"

    @pytest.mark.asyncio
    async def test_a04_open_and_inreview(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """A04: Open + InReview → both slots populated."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        s2, e2 = _week()
        opid = await _insert_period(direct_db, paytest_branch_id, "Open", s2, e2, "A04O")
        irpid = await _insert_period(direct_db, paytest_branch_id, "InReview", s, e, "A04IR")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        assert resp.status_code == 200
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["slots"]["open"]["period_id"] == opid
        assert b["slots"]["in_review"]["period_id"] == irpid

    @pytest.mark.asyncio
    async def test_a05_open_and_older_returned(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """A05: Open + older Returned → both slots populated."""
        await _clean(direct_db, paytest_branch_id)
        rs, re = _week()
        os, oe = _week()
        rpid, _ = await _insert_returned(direct_db, paytest_branch_id, rs, re, "A05R")
        opid = await _insert_period(direct_db, paytest_branch_id, "Open", os, oe, "A05O")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        assert resp.status_code == 200
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["slots"]["open"]["period_id"] == opid
        assert b["slots"]["returned"]["period_id"] == rpid

    @pytest.mark.asyncio
    async def test_a06_draft_alone_prepared_slot_and_invariant_alert(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """A06: Draft alone → prepared slot populated + SLOT_INVARIANT alert."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        dpid = await _insert_period(direct_db, paytest_branch_id, "Draft", s, e, "A06")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        assert resp.status_code == 200
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["slots"]["prepared"]["period_id"] == dpid
        assert b["slots"]["open"] is None
        alert_codes = [a["code"] for a in b["alerts"]]
        assert "SLOT_INVARIANT" in alert_codes

    @pytest.mark.asyncio
    async def test_a07_inreview_read_only(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """A07: InReview slot is_read_only=True with PERIOD_IN_REVIEW_READ_ONLY code."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        await _insert_period(direct_db, paytest_branch_id, "InReview", s, e, "A07")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        ir = b["slots"]["in_review"]
        assert ir["is_read_only"] is True
        assert ir["read_only_reason_code"] == "PERIOD_IN_REVIEW_READ_ONLY"

    @pytest.mark.asyncio
    async def test_a08_lifecycle_positions(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """A08: Lifecycle positions: Returned=1, Open=2, Draft/Prepared=3, InReview=4."""
        await _clean(direct_db, paytest_branch_id)
        # InReview on older dates (position 4)
        s_ir, e_ir = _week()
        await _insert_period(direct_db, paytest_branch_id, "InReview", s_ir, e_ir, "A08IR")
        # Returned backlog (position 1)
        s_ret, e_ret = _week()
        await _insert_returned(direct_db, paytest_branch_id, s_ret, e_ret, "A08RET")
        # Open (position 2)
        s_op, e_op = _week()
        await _insert_period(direct_db, paytest_branch_id, "Open", s_op, e_op, "A08OP")
        # Draft adjacent (position 3)
        s_dr = e_op + datetime.timedelta(days=1)
        e_dr = s_dr + datetime.timedelta(days=6)
        await _insert_period(direct_db, paytest_branch_id, "Draft", s_dr, e_dr, "A08DR")

        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["slots"]["returned"]["lifecycle_position"] == 1
        assert b["slots"]["open"]["lifecycle_position"] == 2
        assert b["slots"]["prepared"]["lifecycle_position"] == 3
        assert b["slots"]["in_review"]["lifecycle_position"] == 4


# ===========================================================================
# Group B: Period-level capabilities
# ===========================================================================

class TestPeriodCapabilities:

    @pytest.mark.asyncio
    async def test_b01_open_submit_allowed_no_blockers(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B01: Open with no blockers → can_submit_for_review allowed."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        pid = await _insert_period(direct_db, paytest_branch_id, "Open", s, e, "B01")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        cap = b["capabilities"]["periods"][str(pid)]["can_submit_for_review"]
        assert cap["allowed"] is True

    @pytest.mark.asyncio
    async def test_b02_open_submit_blocked_by_inreview(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B02: Open + existing InReview → can_submit_for_review blocked."""
        await _clean(direct_db, paytest_branch_id)
        s_ir, e_ir = _week()
        s_op, e_op = _week()
        await _insert_period(direct_db, paytest_branch_id, "InReview", s_ir, e_ir, "B02IR")
        opid = await _insert_period(direct_db, paytest_branch_id, "Open", s_op, e_op, "B02OP")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        cap = b["capabilities"]["periods"][str(opid)]["can_submit_for_review"]
        assert cap["allowed"] is False
        assert cap["reason_code"] == "INREVIEW_SLOT_OCCUPIED"

    @pytest.mark.asyncio
    async def test_b03_open_submit_blocked_by_returned_backlog(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B03: Older Returned + Open → can_submit_for_review false, RETURNED_BACKLOG_BLOCKS_SUBMIT."""
        await _clean(direct_db, paytest_branch_id)
        rs, re = _week()
        os, oe = _week()
        await _insert_returned(direct_db, paytest_branch_id, rs, re, "B03R")
        opid = await _insert_period(direct_db, paytest_branch_id, "Open", os, oe, "B03O")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        cap = b["capabilities"]["periods"][str(opid)]["can_submit_for_review"]
        assert cap["allowed"] is False
        assert cap["reason_code"] == "RETURNED_BACKLOG_BLOCKS_SUBMIT"

    @pytest.mark.asyncio
    async def test_b04_draft_cannot_submit(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B04: Draft period → can_submit_for_review false, PERIOD_NOT_OPEN."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        dpid = await _insert_period(direct_db, paytest_branch_id, "Draft", s, e, "B04")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        cap = b["capabilities"]["periods"][str(dpid)]["can_submit_for_review"]
        assert cap["allowed"] is False
        assert cap["reason_code"] == "PERIOD_NOT_OPEN"

    @pytest.mark.asyncio
    async def test_b05_returned_can_resubmit(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B05: Returned with no InReview → can_resubmit_returned allowed."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        rpid, _ = await _insert_returned(direct_db, paytest_branch_id, s, e, "B05")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        cap = b["capabilities"]["periods"][str(rpid)]["can_resubmit_returned"]
        assert cap["allowed"] is True

    @pytest.mark.asyncio
    async def test_b06_returned_resubmit_blocked_by_inreview(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B06: Returned + InReview occupied → can_resubmit_returned false."""
        await _clean(direct_db, paytest_branch_id)
        rs, re = _week()
        irs, ire = _week()
        rpid, _ = await _insert_returned(direct_db, paytest_branch_id, rs, re, "B06R")
        await _insert_period(direct_db, paytest_branch_id, "InReview", irs, ire, "B06IR")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        cap = b["capabilities"]["periods"][str(rpid)]["can_resubmit_returned"]
        assert cap["allowed"] is False
        assert cap["reason_code"] == "INREVIEW_SLOT_OCCUPIED"

    @pytest.mark.asyncio
    async def test_b07_open_can_enter_source(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B07: Open → can_enter_source true (admin has payroll.entry)."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        opid = await _insert_period(direct_db, paytest_branch_id, "Open", s, e, "B07")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["capabilities"]["periods"][str(opid)]["can_enter_source"]["allowed"] is True

    @pytest.mark.asyncio
    async def test_b08_returned_can_enter_source(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B08: Returned → can_enter_source true."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        rpid, _ = await _insert_returned(direct_db, paytest_branch_id, s, e, "B08")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["capabilities"]["periods"][str(rpid)]["can_enter_source"]["allowed"] is True

    @pytest.mark.asyncio
    async def test_b09_inreview_source_false(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B09: InReview → can_enter_source false."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        irpid = await _insert_period(direct_db, paytest_branch_id, "InReview", s, e, "B09")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        cap = b["capabilities"]["periods"][str(irpid)]["can_enter_source"]
        assert cap["allowed"] is False
        assert cap["reason_code"] == "PERIOD_IN_REVIEW_READ_ONLY"

    @pytest.mark.asyncio
    async def test_b10_display_status_draft_is_prepared(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B10: Draft status → display_status='Prepared'."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        await _insert_period(direct_db, paytest_branch_id, "Draft", s, e, "B10")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        prepared = b["slots"]["prepared"]
        assert prepared["status"] == "Draft"
        assert prepared["display_status"] == "Prepared"

    @pytest.mark.asyncio
    async def test_b11_draft_promotion_conflict_blocks_submit(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """B11: Non-adjacent Draft + Open → can_submit_for_review blocked DRAFT_PROMOTION_CONFLICT."""
        await _clean(direct_db, paytest_branch_id)
        os, oe = _week()
        # Draft not adjacent (gap of 7 days between oe and draft start)
        ds = oe + datetime.timedelta(days=8)
        de = ds + datetime.timedelta(days=6)
        opid = await _insert_period(direct_db, paytest_branch_id, "Open", os, oe, "B11O")
        await _insert_period(direct_db, paytest_branch_id, "Draft", ds, de, "B11D")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        cap = b["capabilities"]["periods"][str(opid)]["can_submit_for_review"]
        assert cap["allowed"] is False
        assert cap["reason_code"] == "DRAFT_PROMOTION_CONFLICT"


# ===========================================================================
# Group C: Candidate capabilities
# ===========================================================================

class TestCandidateCapabilities:

    @pytest.mark.asyncio
    async def test_c01_no_periods_complete_setup_can_create_open(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """C01: No active periods + complete setup → can_create_open_candidate allowed."""
        await _clean(direct_db, paytest_branch_id)
        await _setup_payroll_weekly(session_client, auth_token, paytest_branch_id)
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["capabilities"]["can_create_open_candidate"]["allowed"] is True

    @pytest.mark.asyncio
    async def test_c02_open_no_draft_can_create_prepared(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """C02: Open + no Draft → can_create_prepared_candidate allowed."""
        await _clean(direct_db, paytest_branch_id)
        await _setup_payroll_weekly(session_client, auth_token, paytest_branch_id)
        s, e = _week()
        await _insert_period(direct_db, paytest_branch_id, "Open", s, e, "C02")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["capabilities"]["can_create_prepared_candidate"]["allowed"] is True

    @pytest.mark.asyncio
    async def test_c03_open_plus_draft_both_creation_blocked(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """C03: Open + Draft → both create candidates blocked (slots full)."""
        await _clean(direct_db, paytest_branch_id)
        await _setup_payroll_weekly(session_client, auth_token, paytest_branch_id)
        os, oe = _week()
        ds = oe + datetime.timedelta(days=1)
        de = ds + datetime.timedelta(days=6)
        await _insert_period(direct_db, paytest_branch_id, "Open", os, oe, "C03O")
        await _insert_period(direct_db, paytest_branch_id, "Draft", ds, de, "C03D")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["capabilities"]["can_create_open_candidate"]["allowed"] is False
        assert b["capabilities"]["can_create_prepared_candidate"]["allowed"] is False

    @pytest.mark.asyncio
    async def test_c04_setup_missing_candidate_blocked(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """C04: No payroll setup → candidate capabilities false, NO_PAYROLL_SETUP."""
        await _clean(direct_db, paytest_branch_id)
        # Remove payroll setup
        await direct_db.execute(
            _text("DELETE FROM payroll.branchpayrollsettings WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        await direct_db.commit()
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        co = b["capabilities"]["can_create_open_candidate"]
        cp = b["capabilities"]["can_create_prepared_candidate"]
        assert co["allowed"] is False
        assert co["reason_code"] == "NO_PAYROLL_SETUP"
        assert cp["allowed"] is False
        assert cp["reason_code"] == "NO_PAYROLL_SETUP"

    @pytest.mark.asyncio
    async def test_c05_returned_backlog_does_not_block_prepared_creation(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """C05: Older Returned + Open → can_create_prepared_candidate still true (Returned backlog does NOT block)."""
        await _clean(direct_db, paytest_branch_id)
        await _setup_payroll_weekly(session_client, auth_token, paytest_branch_id)
        rs, re = _week()
        os, oe = _week()
        await _insert_returned(direct_db, paytest_branch_id, rs, re, "C05R")
        await _insert_period(direct_db, paytest_branch_id, "Open", os, oe, "C05O")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        # can_submit blocked
        from_open_period_id = b["slots"]["open"]["period_id"]
        assert b["capabilities"]["periods"][str(from_open_period_id)]["can_submit_for_review"]["allowed"] is False
        # but prepared creation is still allowed
        assert b["capabilities"]["can_create_prepared_candidate"]["allowed"] is True

    @pytest.mark.asyncio
    async def test_c06_returned_backlog_blocks_submit_but_not_prepared_creation(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """C06: RETURNED_BACKLOG alert affects can_submit_for_review only, not can_create_prepared_candidate."""
        await _clean(direct_db, paytest_branch_id)
        await _setup_payroll_weekly(session_client, auth_token, paytest_branch_id)
        rs, re = _week()
        os, oe = _week()
        await _insert_returned(direct_db, paytest_branch_id, rs, re, "C06R")
        await _insert_period(direct_db, paytest_branch_id, "Open", os, oe, "C06O")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        backlog_alerts = [a for a in b["alerts"] if a["code"] == "RETURNED_BACKLOG"]
        assert backlog_alerts, "Expected RETURNED_BACKLOG alert"
        alert = backlog_alerts[0]
        assert "can_submit_for_review" in alert["affected_action_codes"]
        assert "can_create_prepared_candidate" not in alert["affected_action_codes"]


# ===========================================================================
# Group D: Permissions / Security
# ===========================================================================

class TestPermissionsAndSecurity:

    @pytest.mark.asyncio
    async def test_d01_driver_role_denied(
        self, session_client, auth_token, paytest_branch_id,
    ):
        """D01: SpecificBranch DRIVER-role user → 403 from GET /payroll/current-workflow."""
        driver_token = await _make_driver_user(session_client, auth_token, paytest_branch_id)
        r = await _get_workflow(session_client, driver_token, paytest_branch_id)
        assert r.status_code == 403, (
            f"Driver role must be denied workflow access; got {r.status_code}: {r.text}"
        )
        assert "branches" not in r.json() or r.json().get("branches") is None

    @pytest.mark.asyncio
    async def test_d01b_oda_role_denied(
        self, session_client, auth_token, paytest_branch_id,
    ):
        """D01b: OwnDriverDataOnly user → 403 from GET /payroll/current-workflow."""
        oda_token = await _make_oda_user(session_client, auth_token, paytest_branch_id)
        r = await _get_workflow(session_client, oda_token, paytest_branch_id)
        assert r.status_code == 403, (
            f"ODA user must be denied workflow access; got {r.status_code}: {r.text}"
        )

    @pytest.mark.asyncio
    async def test_d02_branch_user_sees_only_own_branch(
        self, session_client,
    ):
        """D02: branch_user (SpecificBranch HQ) sees only HQ, not PAYTEST."""
        resp = await session_client.post("/auth/login", json={
            "username": "branch_user", "password": "TestPass123!", "company_code": "DEMO",
        })
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        r = await session_client.get("/payroll/current-workflow", headers=_auth(token))
        assert r.status_code == 200
        data = r.json()
        branch_ids = [b["branch_id"] for b in data["branches"]]
        # branch_user is SpecificBranch HQ; should not see PAYTEST
        assert all(bid == 1 for bid in branch_ids), f"Expected only HQ (id=1), got: {branch_ids}"

    @pytest.mark.asyncio
    async def test_d03_company_scope_user_sees_all_branches(
        self, session_client, auth_token,
    ):
        """D03: admin (AllCompanyBranches) sees all active branches."""
        r = await session_client.get("/payroll/current-workflow", headers=_auth(auth_token))
        assert r.status_code == 200
        data = r.json()
        assert data["scope"] == "company"
        assert len(data["branches"]) >= 2  # at least HQ and PAYTEST

    @pytest.mark.asyncio
    async def test_d04_view_only_user_submit_capability_denied(
        self, session_client, direct_db, paytest_branch_id, auth_token,
    ):
        """D04: payroll.view-only user → can_submit_for_review false PERMISSION_DENIED (fail-closed)."""
        # Create a view-only user scoped to PAYTEST
        role_id = await _create_role_with_perms(
            session_client, auth_token, f"ViewOnly{_sec_uid()}", ["payroll.view"],
        )
        uname = _sec_uid()
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(session_client, auth_token, user["user_id"], role_id,
                           scope="SpecificBranch", branch_id=paytest_branch_id)
        view_token = await _login_as(session_client, uname)

        # Clear any leftover Open/Draft/InReview periods that would block insertion
        await _clean(direct_db, paytest_branch_id)

        # Insert Open period on PAYTEST
        s, e = _week()
        opid = await _insert_period(direct_db, paytest_branch_id, "Open", s, e, "D04")

        r = await session_client.get(
            "/payroll/current-workflow",
            params={"branch_id": paytest_branch_id},
            headers=_auth(view_token),
        )
        assert r.status_code == 200, f"View-only user should get 200: {r.text}"
        b = _branch_entry(r.json(), paytest_branch_id)

        # Fail-closed: period MUST be present in capabilities
        assert str(opid) in b["capabilities"]["periods"], (
            f"Period {opid} must appear in capabilities.periods for view-only user"
        )
        cap = b["capabilities"]["periods"][str(opid)]["can_submit_for_review"]
        assert cap["allowed"] is False, "View-only must not be able to submit"
        assert cap["reason_code"] == "PERMISSION_DENIED"

        # Cleanup
        await direct_db.execute(
            _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": opid},
        )
        await direct_db.commit()

    @pytest.mark.asyncio
    async def test_d05_requested_branch_scopes_response(
        self, session_client, auth_token, paytest_branch_id,
    ):
        """D05: branch_id query param → only that branch returned."""
        r = await _get_workflow(session_client, auth_token, paytest_branch_id)
        assert r.status_code == 200
        data = r.json()
        assert data["requested_branch_id"] == paytest_branch_id
        assert all(b["branch_id"] == paytest_branch_id for b in data["branches"])

    @pytest.mark.asyncio
    async def test_d06_no_payroll_permission_denied(
        self, session_client, auth_token, paytest_branch_id,
    ):
        """D06: User with branch access but NO payroll.view/entry/finalize → 403."""
        # Role with setup.manage only — no payroll read permissions
        role_id = await _create_role_with_perms(
            session_client, auth_token, f"SetupOnly{_sec_uid()}", ["setup.manage"],
        )
        uname = _sec_uid()
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(session_client, auth_token, user["user_id"], role_id,
                           scope="SpecificBranch", branch_id=paytest_branch_id)
        tok = await _login_as(session_client, uname)

        r = await _get_workflow(session_client, tok, paytest_branch_id)
        assert r.status_code == 403, (
            f"User with no payroll perms must be denied; got {r.status_code}: {r.text}"
        )

    @pytest.mark.asyncio
    async def test_d07_setup_manage_only_denied(
        self, session_client, auth_token, paytest_branch_id,
    ):
        """D07: setup.manage-only (AllCompanyBranches) → 403; setup.manage alone does not grant payroll workflow read."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, f"SetupMgr{_sec_uid()}", ["setup.manage"],
        )
        uname = _sec_uid()
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(session_client, auth_token, user["user_id"], role_id,
                           scope="AllCompanyBranches")
        tok = await _login_as(session_client, uname)

        r = await session_client.get("/payroll/current-workflow", headers=_auth(tok))
        assert r.status_code == 403, (
            f"setup.manage-only must be denied workflow; got {r.status_code}: {r.text}"
        )

    @pytest.mark.asyncio
    async def test_d08_missing_period_create_candidate_caps_denied(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """D08: payroll.view without payroll.period.create → 200, but candidate caps PERMISSION_DENIED."""
        await _clean(direct_db, paytest_branch_id)  # clear any leftover state first
        await _setup_payroll_weekly(session_client, auth_token, paytest_branch_id)
        await _clean(direct_db, paytest_branch_id)  # empty slots so creation would otherwise pass

        role_id = await _create_role_with_perms(
            session_client, auth_token, f"ViewNoCrt{_sec_uid()}", ["payroll.view"],
        )
        uname = _sec_uid()
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(session_client, auth_token, user["user_id"], role_id,
                           scope="SpecificBranch", branch_id=paytest_branch_id)
        tok = await _login_as(session_client, uname)

        r = await _get_workflow(session_client, tok, paytest_branch_id)
        assert r.status_code == 200, f"payroll.view user should get 200: {r.text}"
        b = _branch_entry(r.json(), paytest_branch_id)

        assert b["capabilities"]["can_view_current_workflow"]["allowed"] is True
        co = b["capabilities"]["can_create_open_candidate"]
        cp = b["capabilities"]["can_create_prepared_candidate"]
        cv = b["capabilities"]["can_view_candidates"]
        assert co["allowed"] is False
        assert co["reason_code"] == "PERMISSION_DENIED"
        assert cp["allowed"] is False
        assert cp["reason_code"] == "PERMISSION_DENIED"
        assert cv["allowed"] is False
        assert cv["reason_code"] == "PERMISSION_DENIED"

    @pytest.mark.asyncio
    async def test_d09_cross_company_branch_denied(
        self, session_client, auth_token, direct_db,
    ):
        """D09: Requesting a real branch belonging to Company B as a Company A user →
        403 or 200 with empty branches. Uses a real foreign branch so the test
        fails if the production query omits the company predicate."""
        # Create a real second company + branch so the branch_id exists in the DB
        foreign_co_id = (await direct_db.execute(
            _text("""
                INSERT INTO core.companies
                    (companycode, companyname, legalname, status, issuspended, timezonename)
                VALUES ('CP1E_XCOTEST', 'CP1E Cross-Co', 'CP1E Cross-Co Ltd',
                        'Active', FALSE, 'UTC')
                ON CONFLICT (companycode) DO UPDATE SET companyname = EXCLUDED.companyname
                RETURNING companyid
            """)
        )).scalar_one()
        foreign_br_id = (await direct_db.execute(
            _text("""
                INSERT INTO core.branches
                    (companyid, branchcode, branchname, status, isdefault)
                VALUES (:cid, 'CP1E_XCOBR', 'CP1E Cross-Co Branch', 'Active', TRUE)
                ON CONFLICT (companyid, branchcode) DO UPDATE SET branchname = EXCLUDED.branchname
                RETURNING branchid
            """),
            {"cid": foreign_co_id},
        )).scalar_one()
        await direct_db.commit()

        try:
            # auth_token is a DEMO (Company A, id=1) admin — must not see Company B branch
            r = await session_client.get(
                "/payroll/current-workflow",
                params={"branch_id": foreign_br_id},
                headers=_auth(auth_token),
            )
            # Must be denied: either 403 or 200 with no branches for the foreign branch
            if r.status_code == 200:
                data = r.json()
                returned_ids = [b["branch_id"] for b in data["branches"]]
                assert foreign_br_id not in returned_ids, (
                    f"Company A caller must NOT receive Company B branch {foreign_br_id}; "
                    f"got branches: {returned_ids}"
                )
                assert data["branches"] == [], (
                    f"Requesting a foreign branch must yield no branch entries; got: {data['branches']}"
                )
            else:
                assert r.status_code == 403, (
                    f"Cross-company branch must be 403 or empty 200; got {r.status_code}: {r.text}"
                )
        finally:
            # Cleanup — delete branch first (FK), then company
            await direct_db.execute(
                _text("DELETE FROM core.branches WHERE companyid = :cid"),
                {"cid": foreign_co_id},
            )
            await direct_db.execute(
                _text("DELETE FROM core.companies WHERE companyid = :cid"),
                {"cid": foreign_co_id},
            )
            await direct_db.commit()


# ===========================================================================
# Group E: Alerts
# ===========================================================================

class TestAlerts:

    @pytest.mark.asyncio
    async def test_e01_returned_backlog_alert(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """E01: Older Returned + Open → RETURNED_BACKLOG alert, severity blocker."""
        await _clean(direct_db, paytest_branch_id)
        rs, re = _week()
        os, oe = _week()
        await _insert_returned(direct_db, paytest_branch_id, rs, re, "E01R")
        await _insert_period(direct_db, paytest_branch_id, "Open", os, oe, "E01O")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        alerts = {a["code"]: a for a in b["alerts"]}
        assert "RETURNED_BACKLOG" in alerts
        assert alerts["RETURNED_BACKLOG"]["severity"] == "blocker"

    @pytest.mark.asyncio
    async def test_e02_inreview_awaiting_alert(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """E02: InReview → INREVIEW_AWAITING info alert."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        await _insert_period(direct_db, paytest_branch_id, "InReview", s, e, "E02")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        codes = [a["code"] for a in b["alerts"]]
        assert "INREVIEW_AWAITING" in codes

    @pytest.mark.asyncio
    async def test_e03_prepared_notice_alert(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """E03: Draft exists → PREPARED_NOTICE info alert."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        await _insert_period(direct_db, paytest_branch_id, "Draft", s, e, "E03")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        codes = [a["code"] for a in b["alerts"]]
        assert "PREPARED_NOTICE" in codes

    @pytest.mark.asyncio
    async def test_e04_slot_invariant_draft_alone(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """E04: Draft alone → SLOT_INVARIANT alert."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        await _insert_period(direct_db, paytest_branch_id, "Draft", s, e, "E04")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        codes = [a["code"] for a in b["alerts"]]
        assert "SLOT_INVARIANT" in codes

    @pytest.mark.asyncio
    async def test_e05_clean_open_only_no_blocker_alerts(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """E05: Open only, no other active periods → no blocker alerts."""
        await _clean(direct_db, paytest_branch_id)
        s, e = _week()
        await _insert_period(direct_db, paytest_branch_id, "Open", s, e, "E05")
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        blocker_alerts = [a for a in b["alerts"] if a["severity"] == "blocker"]
        assert not blocker_alerts, f"Expected no blocker alerts: {blocker_alerts}"

    @pytest.mark.asyncio
    async def test_e06_setup_missing_alert(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """E06: No payroll setup → SETUP_MISSING warning alert."""
        await _clean(direct_db, paytest_branch_id)
        await direct_db.execute(
            _text("DELETE FROM payroll.branchpayrollsettings WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        await direct_db.commit()
        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        b = _branch_entry(resp.json(), paytest_branch_id)
        codes = [a["code"] for a in b["alerts"]]
        assert "SETUP_MISSING" in codes


# ===========================================================================
# Group F: P2 — composition and setup coverage
# ===========================================================================

class TestCompositionAndSetup:

    @pytest.mark.asyncio
    async def test_f01_returned_open_draft_composition(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """F01: Returned backlog + Open + adjacent Draft → all three slots populated.
        Backlog blocks submit. Prepared creation is blocked because Draft already fills
        the prepared slot — NOT because of the Returned backlog (backlog alone never
        blocks can_create_prepared_candidate)."""
        await _clean(direct_db, paytest_branch_id)
        await _setup_payroll_weekly(session_client, auth_token, paytest_branch_id)
        rs, re = _week()
        os, oe = _week()
        ds = oe + datetime.timedelta(days=1)
        de = ds + datetime.timedelta(days=6)
        rpid, _ = await _insert_returned(direct_db, paytest_branch_id, rs, re, "F01R")
        opid = await _insert_period(direct_db, paytest_branch_id, "Open", os, oe, "F01O")
        dpid = await _insert_period(direct_db, paytest_branch_id, "Draft", ds, de, "F01D")

        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        assert resp.status_code == 200
        b = _branch_entry(resp.json(), paytest_branch_id)

        # All three slots populated
        assert b["slots"]["returned"]["period_id"] == rpid
        assert b["slots"]["open"]["period_id"] == opid
        assert b["slots"]["prepared"]["period_id"] == dpid

        # Backlog blocks submit
        submit_cap = b["capabilities"]["periods"][str(opid)]["can_submit_for_review"]
        assert submit_cap["allowed"] is False
        assert submit_cap["reason_code"] == "RETURNED_BACKLOG_BLOCKS_SUBMIT"

        # Prepared creation blocked (Draft slot already filled)
        assert b["capabilities"]["can_create_prepared_candidate"]["allowed"] is False

        # RETURNED_BACKLOG alert present + does NOT list can_create_prepared_candidate
        backlog_alerts = [a for a in b["alerts"] if a["code"] == "RETURNED_BACKLOG"]
        assert backlog_alerts
        assert "can_create_prepared_candidate" not in backlog_alerts[0]["affected_action_codes"]

    @pytest.mark.asyncio
    async def test_f02_inactive_setup_candidate_blocked(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """F02: Inactive payroll setup → setup_status='inactive', candidate caps blocked.
        Provisions setup inside the test so it passes independently (not relying on
        earlier tests having run)."""
        await _clean(direct_db, paytest_branch_id)
        # Provision setup so the row exists before we deactivate it
        await _setup_payroll_weekly(session_client, auth_token, paytest_branch_id)
        # Set setup to inactive
        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayrollsettings
                SET isactive = FALSE
                WHERE branchid = :bid
            """),
            {"bid": paytest_branch_id},
        )
        await direct_db.commit()

        resp = await _get_workflow(session_client, auth_token, paytest_branch_id)
        assert resp.status_code == 200
        b = _branch_entry(resp.json(), paytest_branch_id)
        assert b["setup_status"] == "inactive"
        co = b["capabilities"]["can_create_open_candidate"]
        assert co["allowed"] is False
        # reason must be setup-related (not slot-related)
        assert co["reason_code"] in ("SETUP_INCOMPLETE", "NO_PAYROLL_SETUP", "SETUP_INACTIVE"), (
            f"Unexpected reason_code for inactive setup: {co['reason_code']}"
        )

        # Restore active setup for other tests
        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayrollsettings
                SET isactive = TRUE
                WHERE branchid = :bid
            """),
            {"bid": paytest_branch_id},
        )
        await direct_db.commit()
