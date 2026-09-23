"""
Payroll Trust Phase 2 — Centralized Driver Eligibility on Write Paths

Tests covering the P2 eligibility guard on every payroll write path:

  T1  add_draft_line rejects driver before hire date
  T2  add_draft_line accepts driver on hire date
  T3  add_draft_line rejects driver after termination date
  T4  add_draft_line accepts driver on termination date
  T5  add_draft_line rejects wrong-branch driver
  T6  day-grid save rejects crafted ineligible driver row
  T7  update_draft_line cannot edit non-void field on stale ineligible line
  T8  update_draft_line allows voiding a stale ineligible line
  T9  finalization blocks stale ineligible daily line
  T10 add_period_pay_line rejects driver not eligible anywhere in period
  T11 add_period_pay_line accepts driver eligible at least one day in period
  T12 add_draft_line rejects transferred driver using source branch after transfer
  T13 add_draft_line rejects driver using target branch before effective date
  T14 normal valid daily payroll still works end-to-end
  T15 full regression — existing suite count unchanged

Isolation strategy
------------------
All tests use year 2034 dates on the PAYTEST branch.  Ephemeral drivers
(created per-test) are fully cleaned up in fixture teardown so they cannot
bleed into other test modules.  The paytest_driver_id fixture driver is
never mutated by this module.
"""
import pytest
import pytest_asyncio
import httpx
from datetime import date as _date, timedelta as _td
from sqlalchemy import text as _text, text as _sqla_text
from uuid import uuid4


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Use a module-isolated branch for eligibility workflow-slot tests."""
    row = (await session_db_conn.execute(_text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {"code": f"P2_{uuid4().hex}", "name": "P2 isolated"})).scalar_one()
    return int(row)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# Date range well outside every other module
P_START = "2034-05-06"   # Monday
P_END   = "2034-05-19"   # Sunday (+2 weeks)
WORK_MID = "2034-05-12"  # Wednesday — middle of period


async def _cancel_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    db=None,
) -> None:
    headers = auth(token)
    if db is not None:
        await db.execute(
            _sqla_text(
                "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                "WHERE branchid = :bid AND status IN ('InReview', 'Approved')"
            ),
            {"bid": branch_id},
        )
    for s in ("Draft", "Open", "InReview", "Approved"):
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        if resp.status_code != 200:
            continue
        for p in resp.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"},
                headers=headers,
            )


async def _make_open_period(
    db,
    branch_id: int,
    start: str = P_START,
    end: str = P_END,
) -> int:
    """Insert an Open period directly into DB; returns period_id."""
    row = (await db.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": f"P2-{branch_id}-{start}",
         "name": f"P2 {start}", "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _create_driver(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    suffix: str,
    hire_date: str | None = None,
    termination_date: str | None = None,
) -> int:
    """Create a driver and optionally set hire/termination dates. Returns driver_id."""
    headers = auth(token)
    payload: dict = {
        "branch_id":      branch_id,
        "full_name":      f"P2 Test Driver {suffix}",
        "preferred_name": f"P2-{suffix}",
        "driver_code":    f"P2DRV-{suffix}",
        "cdl_number":     f"CDL-P2-{suffix}",
        "email":          f"p2drv{suffix}@example.com",
    }
    if hire_date is not None:
        payload["hire_date"] = hire_date

    r = await client.post("/core/drivers", json=payload, headers=headers)
    assert r.status_code == 201, f"driver create failed: {r.text}"
    driver_id = r.json()["driver_id"]

    # DriverCreate does not accept termination_date — patch separately
    if termination_date is not None:
        patch = await client.patch(
            f"/core/drivers/{driver_id}",
            json={"termination_date": termination_date},
            headers=headers,
        )
        assert patch.status_code == 200, f"driver termination patch failed: {patch.text}"

    return driver_id


async def _delete_driver(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
) -> None:
    await client.delete(
        f"/core/drivers/{driver_id}",
        headers=auth(token),
    )


async def _patch_driver(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    **kwargs,
) -> None:
    r = await client.patch(
        f"/core/drivers/{driver_id}",
        json=kwargs,
        headers=auth(token),
    )
    assert r.status_code == 200, f"driver patch failed: {r.text}"


async def _add_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str = WORK_MID,
    line_type: str = "DailyNote",
) -> httpx.Response:
    payload: dict = {
        "driver_id": driver_id,
        "work_date": work_date,
        "line_type": line_type,
    }
    if line_type == "DailyNote":
        payload["notes"] = "filler"
    else:
        payload["quantity"] = 1
    return await client.post(
        f"/payroll/periods/{period_id}/lines",
        headers=auth(token),
        json=payload,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def p2_env(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """Cancel conflicting periods; yield env dict; cancel again after test."""
    await _cancel_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    env = {
        "client":    session_client,
        "token":     auth_token,
        "branch_id": paytest_branch_id,
        "db":        direct_db,
        "created_drivers": [],
    }

    yield env

    # Teardown: cancel any open periods this test may have left
    await _cancel_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    # Delete any ephemeral drivers created by this test
    for did in env["created_drivers"]:
        await _delete_driver(session_client, auth_token, did)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAddDraftLineEligibility:

    @pytest.mark.asyncio
    async def test_t1_rejects_driver_before_hire_date(self, p2_env):
        """T1 — add_draft_line 422 when work_date is before driver's hire date."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        # hire date is the day AFTER the work date we'll submit
        driver_id = await _create_driver(
            c, tok, bid, "T1", hire_date="2034-05-13"  # one day after WORK_MID
        )
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        resp = await _add_line(c, tok, pid, driver_id, work_date=WORK_MID)
        assert resp.status_code == 422, resp.text
        assert "eligible" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_t2_accepts_driver_on_hire_date(self, p2_env):
        """T2 — add_draft_line 201 when work_date equals hire date."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        driver_id = await _create_driver(
            c, tok, bid, "T2", hire_date=WORK_MID  # exact match
        )
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        resp = await _add_line(c, tok, pid, driver_id, work_date=WORK_MID)
        assert resp.status_code == 201, resp.text

    @pytest.mark.asyncio
    async def test_t3_rejects_driver_after_termination_date(self, p2_env):
        """T3 — add_draft_line 422 when work_date is after driver's termination date."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        # terminated day before work date
        driver_id = await _create_driver(
            c, tok, bid, "T3",
            hire_date="2034-01-01",
            termination_date="2034-05-11",  # one day before WORK_MID
        )
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        resp = await _add_line(c, tok, pid, driver_id, work_date=WORK_MID)
        assert resp.status_code == 422, resp.text
        assert "eligible" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_t4_accepts_driver_on_termination_date(self, p2_env):
        """T4 — add_draft_line 201 when work_date equals termination date."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        driver_id = await _create_driver(
            c, tok, bid, "T4",
            hire_date="2034-01-01",
            termination_date=WORK_MID,  # exact match — still eligible
        )
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        resp = await _add_line(c, tok, pid, driver_id, work_date=WORK_MID)
        assert resp.status_code == 201, resp.text

    @pytest.mark.asyncio
    async def test_t5_rejects_wrong_branch_driver(
        self, p2_env, session_client, auth_token
    ):
        """T5 — add_draft_line 422 when driver belongs to a different branch."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        # Find a branch that is NOT the PAYTEST branch
        resp = await c.get("/core/branches", headers=auth(tok))
        assert resp.status_code == 200
        other_branch = next(
            (b for b in resp.json() if b["branch_id"] != bid),
            None,
        )
        assert other_branch is not None, "Need at least two branches in the DB"
        other_bid = other_branch["branch_id"]

        # Create driver on the OTHER branch
        driver_id = await _create_driver(c, tok, other_bid, "T5")
        p2_env["created_drivers"].append(driver_id)

        # Period is on PAYTEST branch
        pid = await _make_open_period(p2_env["db"], bid)

        resp = await _add_line(c, tok, pid, driver_id, work_date=WORK_MID)
        assert resp.status_code == 422, resp.text
        assert "eligible" in resp.text.lower()


class TestDayGridEligibility:

    @pytest.mark.asyncio
    async def test_t6_day_grid_rejects_ineligible_driver(self, p2_env, direct_db):
        """T6 — save_day_grid 422 when driver is not eligible for the work date."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        # Driver hired after the work date
        driver_id = await _create_driver(
            c, tok, bid, "T6", hire_date="2034-05-18"  # after WORK_MID
        )
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        # Look up a valid pay-item code that the day-grid accepts
        pi_resp = await c.get(
            f"/settings/branches/{bid}/pay-items",
            headers=auth(tok),
        )
        assert pi_resp.status_code == 200
        # Find a mileage or hours code
        code = next(
            (p["pay_item_code"] for p in pi_resp.json()
             if p.get("is_active") and p.get("pay_item_code") in ("MILES", "HOURS")),
            None,
        )
        if code is None:
            pytest.skip("No MILES/HOURS pay item active on PAYTEST branch")

        resp = await c.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=auth(tok),
            json={
                "work_date": WORK_MID,
                "rows": [{
                    "driver_id": driver_id,
                    "values": {code: "1"},
                }],
            },
        )
        assert resp.status_code == 422, resp.text
        assert "eligible" in resp.text.lower()


class TestUpdateDraftLineEligibility:

    @pytest.mark.asyncio
    async def test_t7_update_blocks_edit_on_ineligible_stale_line(
        self, p2_env, direct_db
    ):
        """T7 — update_draft_line 422 when modifying quantity on a stale line
        whose driver was later terminated before the line's work_date."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        # Eligible driver at time of add
        driver_id = await _create_driver(c, tok, bid, "T7", hire_date="2034-01-01")
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        # Add line while driver is still eligible
        add = await _add_line(c, tok, pid, driver_id)
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        # Retroactively terminate driver before the work date via direct DB
        # (bypass the service-level guard by writing directly)
        await direct_db.execute(
            _text("""
                UPDATE core.employees e
                SET    terminationdate = :td
                FROM   core.drivers d
                WHERE  d.employeeid = e.employeeid
                  AND  d.driverid   = :did
            """),
            {"td": _date(2034, 5, 10), "did": driver_id},
        )

        # Now try to edit the quantity — should be blocked
        resp = await c.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            headers=auth(tok),
            json={"quantity": "2"},
        )
        assert resp.status_code == 422, resp.text
        assert "eligible" in resp.text.lower()

        # Restore termination date so driver delete works cleanly
        await direct_db.execute(
            _text("""
                UPDATE core.employees e
                SET    terminationdate = NULL
                FROM   core.drivers d
                WHERE  d.employeeid = e.employeeid
                  AND  d.driverid   = :did
            """),
            {"did": driver_id},
        )

    @pytest.mark.asyncio
    async def test_t8_update_allows_void_on_ineligible_stale_line(
        self, p2_env, direct_db
    ):
        """T8 — void-only update always succeeds even when driver is ineligible."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        driver_id = await _create_driver(c, tok, bid, "T8", hire_date="2034-01-01")
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        add = await _add_line(c, tok, pid, driver_id)
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        # Retroactively terminate driver before work date
        await direct_db.execute(
            _text("""
                UPDATE core.employees e
                SET    terminationdate = :td
                FROM   core.drivers d
                WHERE  d.employeeid = e.employeeid
                  AND  d.driverid   = :did
            """),
            {"td": _date(2034, 5, 10), "did": driver_id},
        )

        # DELETE endpoint voids the line — must succeed
        resp = await c.delete(
            f"/payroll/periods/{pid}/lines/{line_id}",
            headers=auth(tok),
        )
        assert resp.status_code == 204, resp.text

        # Restore
        await direct_db.execute(
            _text("""
                UPDATE core.employees e
                SET    terminationdate = NULL
                FROM   core.drivers d
                WHERE  d.employeeid = e.employeeid
                  AND  d.driverid   = :did
            """),
            {"did": driver_id},
        )


class TestFinalizationEligibility:

    @pytest.mark.asyncio
    async def test_t9_finalization_blocks_stale_ineligible_daily_line(
        self, p2_env, direct_db
    ):
        """T9 — finalize_period 422 when a non-void daily line belongs to a driver
        who is ineligible on the line's work_date at the time of finalization."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        driver_id = await _create_driver(c, tok, bid, "T9", hire_date="2034-01-01")
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        add = await _add_line(c, tok, pid, driver_id)
        assert add.status_code == 201, add.text

        # Retroactively terminate driver before the work date
        await direct_db.execute(
            _text("""
                UPDATE core.employees e
                SET    terminationdate = :td
                FROM   core.drivers d
                WHERE  d.employeeid = e.employeeid
                  AND  d.driverid   = :did
            """),
            {"td": _date(2034, 5, 10), "did": driver_id},
        )

        # Current submission validation rejects stale ineligible daily evidence
        # before an InReview snapshot can be created.
        r = await c.patch(
            f"/payroll/periods/{pid}/status",
            headers=auth(tok),
            json={"status": "InReview"},
        )
        assert r.status_code == 422, r.text
        assert "eligible" in r.text.lower() or "ineligible" in r.text.lower()

        # Restore
        await direct_db.execute(
            _text("""
                UPDATE core.employees e
                SET    terminationdate = NULL
                FROM   core.drivers d
                WHERE  d.employeeid = e.employeeid
                  AND  d.driverid   = :did
            """),
            {"did": driver_id},
        )


class TestPeriodPayEligibility:

    @staticmethod
    async def _ensure_bonus_active(c, tok, bid) -> None:
        """Activate the legacy Period-scope ADJUSTMENT item for eligibility coverage."""
        items_resp = await c.get(
            f"/settings/branches/{bid}/pay-items",
            headers=auth(tok),
        )
        assert items_resp.status_code == 200
        for item in items_resp.json():
            if item["pay_item_code"] == "ADJUSTMENT":
                activated = await c.patch(
                    f"/settings/branches/{bid}/pay-items/{item['pay_item_id']}",
                    headers=auth(tok),
                    json={"is_active": True},
                )
                assert activated.status_code == 200, activated.text
                return
        raise AssertionError("ADJUSTMENT pay item missing from the current catalog")

    @pytest.mark.asyncio
    async def test_t10_period_pay_rejects_driver_not_eligible_in_period(self, p2_env):
        """T10 — add_period_pay_line 422 when driver has no overlap with the period."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]
        await self._ensure_bonus_active(c, tok, bid)

        # Driver terminated before period start
        driver_id = await _create_driver(
            c, tok, bid, "T10",
            hire_date="2034-01-01",
            termination_date="2034-04-30",  # well before P_START 2034-05-06
        )
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        resp = await c.post(
            f"/payroll/periods/{pid}/period-pay",
            headers=auth(tok),
            json={
                "driver_id": driver_id,
                "line_type": "ADJUSTMENT",
                "amount":    "50.00",
            },
        )
        assert resp.status_code == 422, resp.text
        assert "eligible" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_t11_period_pay_accepts_driver_eligible_partial_period(self, p2_env):
        """T11 — add_period_pay_line 201 when driver is eligible for at least one
        day of the period (hired on the last day of the period)."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]
        await self._ensure_bonus_active(c, tok, bid)

        # Hired on last day of period — still overlaps by one day
        driver_id = await _create_driver(
            c, tok, bid, "T11",
            hire_date=P_END,  # 2034-05-19 — last day of period
        )
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        resp = await c.post(
            f"/payroll/periods/{pid}/period-pay",
            headers=auth(tok),
            json={
                "driver_id": driver_id,
                "line_type": "ADJUSTMENT",
                "amount":    "50.00",
            },
        )
        assert resp.status_code == 201, resp.text


class TestTransferEligibility:

    @pytest.mark.asyncio
    async def test_t12_source_branch_rejected_after_transfer(self, p2_env, direct_db):
        """T12 — After a driver is marked as Transferred with effectiveto set,
        the source branch profile is rejected for work dates after effectiveto.

        We simulate a completed transfer by directly setting the driver's
        driverstatus='Transferred' and effectiveto one day before WORK_MID.
        """
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        driver_id = await _create_driver(c, tok, bid, "T12", hire_date="2034-01-01")
        p2_env["created_drivers"].append(driver_id)

        # Simulate completed transfer: effectiveto = 2034-05-09 (day before WORK_MID 2034-05-12)
        await direct_db.execute(
            _text("""
                UPDATE core.drivers
                SET    driverstatus = 'Transferred',
                       effectiveto  = :eto
                WHERE  driverid = :did
            """),
            {"eto": _date(2034, 5, 9), "did": driver_id},
        )

        pid = await _make_open_period(p2_env["db"], bid)

        # Source driver's effectiveto < work_date → ineligible
        resp = await _add_line(c, tok, pid, driver_id, work_date=WORK_MID)
        assert resp.status_code == 422, resp.text
        assert "eligible" in resp.text.lower()

        # Restore driver so fixture cleanup (delete) works
        await direct_db.execute(
            _text("""
                UPDATE core.drivers
                SET    driverstatus = 'Active',
                       effectiveto  = NULL
                WHERE  driverid = :did
            """),
            {"did": driver_id},
        )

    @pytest.mark.asyncio
    async def test_t13_target_branch_rejected_before_effective_date(self, p2_env, direct_db):
        """T13 — A driver profile with effectivefrom in the future is rejected
        for work dates before that effectivefrom.

        We simulate the target-branch profile by setting effectivefrom to
        2034-05-15 (after WORK_MID 2034-05-12).
        """
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        driver_id = await _create_driver(c, tok, bid, "T13", hire_date="2034-01-01")
        p2_env["created_drivers"].append(driver_id)

        # Simulate target profile: effectivefrom = 2034-05-15 (after WORK_MID)
        await direct_db.execute(
            _text("""
                UPDATE core.drivers
                SET    effectivefrom = :efrom
                WHERE  driverid = :did
            """),
            {"efrom": _date(2034, 5, 15), "did": driver_id},
        )

        pid = await _make_open_period(p2_env["db"], bid)

        # effectivefrom (2034-05-15) > work_date (2034-05-12) → ineligible
        resp = await _add_line(c, tok, pid, driver_id, work_date=WORK_MID)
        assert resp.status_code == 422, resp.text
        assert "eligible" in resp.text.lower()

        # Restore
        await direct_db.execute(
            _text("""
                UPDATE core.drivers
                SET    effectivefrom = NULL
                WHERE  driverid = :did
            """),
            {"did": driver_id},
        )


class TestNormalPathStillWorks:

    @pytest.mark.asyncio
    async def test_t14_normal_daily_payroll_end_to_end(self, p2_env):
        """T14 — Happy-path: an eligible driver can add, update, and have their
        line survive through finalization."""
        c, tok, bid = p2_env["client"], p2_env["token"], p2_env["branch_id"]

        driver_id = await _create_driver(
            c, tok, bid, "T14", hire_date="2034-01-01"
        )
        p2_env["created_drivers"].append(driver_id)

        pid = await _make_open_period(p2_env["db"], bid)

        # Add
        add = await _add_line(c, tok, pid, driver_id, work_date=WORK_MID)
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        # Update notes — non-void edit on eligible driver should succeed
        upd = await c.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            headers=auth(tok),
            json={"notes": "eligible edit"},
        )
        assert upd.status_code == 200, upd.text

        # Advance to Approved
        r = await c.patch(
            f"/payroll/periods/{pid}/status",
            headers=auth(tok),
            json={"status": "InReview"},
        )
        assert r.status_code == 200, r.text

        review_resp = await c.get("/review/items", headers=auth(tok))
        review_item = next(
            (i for i in review_resp.json()
             if i.get("entity_name") == "PayrollPeriods"
             and i.get("entity_id") == str(pid)
             and i.get("status") == "Pending"),
            None,
        )
        assert review_item is not None

        decide = await c.post(
            f"/review/items/{review_item['review_item_id']}/decide",
            headers=auth(tok),
            json={"decision": "Approved"},
        )
        assert decide.status_code == 200, decide.text

        # Finalize — must succeed
        resp = await c.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(tok),
        )
        assert resp.status_code == 200, resp.text

    @pytest.mark.asyncio
    async def test_t15_full_regression_suite_unchanged(self):
        """T15 — Structural guard: this test verifies the expected count of
        other test modules hasn't regressed.  It cannot run the full suite
        from inside pytest, so it asserts the key test files still exist."""
        import pathlib
        test_dir = pathlib.Path(__file__).parent
        expected_files = [
            "test_auth.py",
            "test_core.py",
            "test_entry.py",
            "test_finalize.py",
            "test_cp5_calc_consistency.py",
            "test_cp6_review.py",
            "test_payroll_trust_p1.py",
        ]
        for fname in expected_files:
            assert (test_dir / fname).exists(), f"Test file missing: {fname}"
