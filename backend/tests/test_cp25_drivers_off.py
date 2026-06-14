"""
CP-2.5 — Period-level Drivers Off endpoint tests.

Tests for GET /payroll/periods/{period_id}/drivers-off

Uses year 2084 dates to avoid conflicts with existing test modules.
Status keys are inserted per-test (function-scoped direct_db) because the
seed data does not include PayrollStatusKeys rows.
"""
import pytest
import pytest_asyncio
import httpx
from datetime import date as _date
from sqlalchemy import text as _text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    headers = auth(token)
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
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str,
    end: str,
) -> dict:
    await _cancel_active_periods(client, token, branch_id)
    resp = await client.post(
        "/payroll/periods",
        json={"branch_id": branch_id, "period_type": "Week",
              "start_date": start, "end_date": end},
        headers=auth(token),
    )
    assert resp.status_code == 201, f"create failed: {resp.text}"
    pid = resp.json()["payroll_period_id"]
    r = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "Open"},
        headers=auth(token),
    )
    assert r.status_code == 200, f"open failed: {r.text}"
    return resp.json()


async def _create_role_with_perms(
    client: httpx.AsyncClient,
    token: str,
    role_name: str,
    perms: list,
) -> int:
    cr = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=auth(token),
    )
    assert cr.status_code == 201, f"Create role failed: {cr.text}"
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=auth(token),
        )
        assert pr.status_code == 200, f"Set permissions failed: {pr.text}"
    return role_id


async def _create_user_with_role(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    role_id: int,
    scope_type: str = "AllCompanyBranches",
    branch_id=None,
    password: str = "TestPass123!",
) -> str:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": username,
            "password": password,
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=auth(admin_token),
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"
    user_id = resp.json()["user_id"]

    assign_body: dict = {
        "company_role_id": role_id,
        "scope_type": scope_type,
    }
    if branch_id is not None:
        assign_body["branch_id"] = branch_id

    assign_resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=assign_body,
        headers=auth(admin_token),
    )
    assert assign_resp.status_code in (200, 201), f"Assign role failed: {assign_resp.text}"

    login = await client.post("/auth/login", json={
        "username": username,
        "password": password,
        "company_code": "DEMO",
    })
    assert login.status_code == 200, f"Login failed: {login.text}"
    return login.json()["access_token"]


async def _get_company_id(direct_db) -> int:
    result = await direct_db.execute(
        _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO'")
    )
    return result.scalar_one()


async def _insert_off_status_key(
    direct_db,
    company_id: int,
    branch_id: int,
    key_code: str,
) -> int:
    """Insert an off-reason status key; return statuskeyid."""
    result = await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                 hoursvalue, isoffreason, isactive, displayorder)
            VALUES
                (:cid, :bid, :code, :code, :name,
                 0, TRUE, TRUE, 99)
            RETURNING statuskeyid
        """),
        {
            "cid": company_id,
            "bid": branch_id,
            "code": key_code,
            "name": f"Test Off Key {key_code}",
        },
    )
    return result.scalar_one()


async def _delete_status_key(direct_db, key_id: int) -> None:
    await direct_db.execute(
        _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
        {"sid": key_id},
    )


async def _insert_daily_status_line(
    direct_db,
    company_id: int,
    branch_id: int,
    period_id: int,
    driver_id: int,
    work_date: str,   # "YYYY-MM-DD"
    status_key_code: str,
) -> int:
    """Insert a DailyStatus draft line; returns draftlineid."""
    y, m, d = work_date.split("-")
    dt = _date(int(y), int(m), int(d))
    result = await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid,
                 workdate, linetype, linescope, quantity,
                 rateamount, calculatedamount, sourcetype,
                 status, needsmanagerreview, notes, addedbyuserid)
            VALUES
                (:cid, :bid, :pid, :did,
                 :workdate, 'DailyStatus', 'Daily', 1,
                 NULL, NULL, 'Manual',
                 'Active', FALSE, :status_code, 1)
            RETURNING draftlineid
        """),
        {
            "cid": company_id,
            "bid": branch_id,
            "pid": period_id,
            "did": driver_id,
            "workdate": dt,
            "status_code": status_key_code,
        },
    )
    return result.scalar_one()


async def _insert_daily_note_line(
    direct_db,
    company_id: int,
    branch_id: int,
    period_id: int,
    driver_id: int,
    work_date: str,   # "YYYY-MM-DD"
    note_text: str,
) -> int:
    """Insert a DailyNote draft line; returns draftlineid."""
    y, m, d = work_date.split("-")
    dt = _date(int(y), int(m), int(d))
    result = await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid,
                 workdate, linetype, linescope, quantity,
                 rateamount, calculatedamount, sourcetype,
                 status, needsmanagerreview, notes, addedbyuserid)
            VALUES
                (:cid, :bid, :pid, :did,
                 :workdate, 'DailyNote', 'Daily', 1,
                 NULL, NULL, 'Manual',
                 'Active', FALSE, :note_text, 1)
            RETURNING draftlineid
        """),
        {
            "cid": company_id,
            "bid": branch_id,
            "pid": period_id,
            "did": driver_id,
            "workdate": dt,
            "note_text": note_text,
        },
    )
    return result.scalar_one()


# ===========================================================================
# CP-2.5 — Drivers Off tests
# ===========================================================================

class TestDriversOff:

    @pytest.mark.asyncio
    async def test_drivers_off_returns_all_period_days(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Off-driver records from day 1 AND day 5 both appear in a single response.
        The endpoint returns all work dates, not just one day.
        """
        company_id = await _get_company_id(direct_db)
        off_code = "CP25_OFF_ALL_DAYS"
        sk_id = await _insert_off_status_key(direct_db, company_id, paytest_branch_id, off_code)

        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-01-01", "2084-01-07",
        )
        pid = period["payroll_period_id"]

        try:
            # Insert off-status for day 1 and day 5
            await _insert_daily_status_line(
                direct_db, company_id, paytest_branch_id, pid,
                paytest_driver_id, "2084-01-01", off_code,
            )
            await _insert_daily_status_line(
                direct_db, company_id, paytest_branch_id, pid,
                paytest_driver_id, "2084-01-05", off_code,
            )

            resp = await session_client.get(
                f"/payroll/periods/{pid}/drivers-off",
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["period_id"] == pid
            work_dates = {e["work_date"] for e in data["entries"]}
            assert "2084-01-01" in work_dates, "Day-1 record missing"
            assert "2084-01-05" in work_dates, "Day-5 record missing"
            assert data["total_count"] == len(data["entries"])
            assert data["total_count"] >= 2

        finally:
            await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Cancelled"},
                headers=auth(auth_token),
            )
            await _delete_status_key(direct_db, sk_id)

    @pytest.mark.asyncio
    async def test_drivers_off_excludes_non_off_status_keys(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        A DailyStatus line with isoffreason=FALSE (or no matching key) is not returned.
        Also verifies that a real off-reason line IS returned while the non-off one is not.
        """
        company_id = await _get_company_id(direct_db)
        # Insert a real off-reason key
        off_code = "CP25_REAL_OFF"
        sk_id = await _insert_off_status_key(direct_db, company_id, paytest_branch_id, off_code)

        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-01-08", "2084-01-14",
        )
        pid = period["payroll_period_id"]

        try:
            # Insert a DailyStatus with a fake code (no matching key → excluded)
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         rateamount, calculatedamount, sourcetype,
                         status, needsmanagerreview, notes, addedbyuserid)
                    VALUES
                        (:cid, :bid, :pid, :did,
                         :dt, 'DailyStatus', 'Daily', 1,
                         NULL, NULL, 'Manual',
                         'Active', FALSE, 'NOTANOFFCODE_XYZ', 1)
                """),
                {
                    "cid": company_id, "bid": paytest_branch_id,
                    "pid": pid, "did": paytest_driver_id,
                    "dt": _date(2084, 1, 8),
                },
            )
            # Insert a real off line on a different day (should appear)
            await _insert_daily_status_line(
                direct_db, company_id, paytest_branch_id, pid,
                paytest_driver_id, "2084-01-09", off_code,
            )

            resp = await session_client.get(
                f"/payroll/periods/{pid}/drivers-off",
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            codes = [e["status_key_code"] for e in data["entries"]]
            assert "NOTANOFFCODE_XYZ" not in codes, "Non-off code must not appear"
            assert off_code in codes, "Real off code must appear"

        finally:
            await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Cancelled"},
                headers=auth(auth_token),
            )
            await _delete_status_key(direct_db, sk_id)

    @pytest.mark.asyncio
    async def test_drivers_off_includes_notes_from_daily_note_line(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """DailyNote text for the same driver/date appears in the response notes field."""
        company_id = await _get_company_id(direct_db)
        off_code = "CP25_OFF_WITH_NOTE"
        sk_id = await _insert_off_status_key(direct_db, company_id, paytest_branch_id, off_code)
        note_text = "Driver called in sick with doctor note"

        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-01-15", "2084-01-21",
        )
        pid = period["payroll_period_id"]

        try:
            await _insert_daily_status_line(
                direct_db, company_id, paytest_branch_id, pid,
                paytest_driver_id, "2084-01-15", off_code,
            )
            await _insert_daily_note_line(
                direct_db, company_id, paytest_branch_id, pid,
                paytest_driver_id, "2084-01-15", note_text,
            )

            resp = await session_client.get(
                f"/payroll/periods/{pid}/drivers-off",
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            entries = resp.json()["entries"]
            entry = next(
                (e for e in entries
                 if e["work_date"] == "2084-01-15" and e["driver_id"] == paytest_driver_id),
                None,
            )
            assert entry is not None, "Expected entry for 2084-01-15 not found"
            assert entry["notes"] == note_text

        finally:
            await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Cancelled"},
                headers=auth(auth_token),
            )
            await _delete_status_key(direct_db, sk_id)

    @pytest.mark.asyncio
    async def test_drivers_off_empty_when_no_off_drivers(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """Period with no off statuses → empty entries list and total_count=0."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-01-22", "2084-01-28",
        )
        pid = period["payroll_period_id"]

        resp = await session_client.get(
            f"/payroll/periods/{pid}/drivers-off",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["period_id"] == pid
        assert data["entries"] == []
        assert data["total_count"] == 0

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_drivers_off_oda_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """ODA user → 403 on GET drivers-off endpoint."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-02-01", "2084-02-07",
        )
        pid = period["payroll_period_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "CP25_ODA_DriversOff_Role",
            ["payroll.view", "payroll.entry"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "cp25_oda_driversoff_user", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/drivers-off",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403
        body = resp.json()
        assert "entries" not in body

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_drivers_off_requires_payroll_view(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """User without payroll.view or payroll.entry → 403 on drivers-off endpoint."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-02-08", "2084-02-14",
        )
        pid = period["payroll_period_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "CP25_NoPerms_DriversOff_Role",
            [],
        )
        no_perm_token = await _create_user_with_role(
            session_client, auth_token, "cp25_noperm_driversoff_user", role_id,
            scope_type="AllCompanyBranches",
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/drivers-off",
            headers=auth(no_perm_token),
        )
        assert resp.status_code == 403

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_drivers_off_respects_period_scope(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Off records from a different period do not appear in the response for
        the queried period.
        """
        company_id = await _get_company_id(direct_db)
        off_code = "CP25_OFF_SCOPE"
        sk_id = await _insert_off_status_key(direct_db, company_id, paytest_branch_id, off_code)

        # Period A — will hold the off record
        period_a = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-02-15", "2084-02-21",
        )
        pid_a = period_a["payroll_period_id"]

        # Insert off record in period A
        await _insert_daily_status_line(
            direct_db, company_id, paytest_branch_id, pid_a,
            paytest_driver_id, "2084-02-15", off_code,
        )

        # Cancel period A, then create period B
        await session_client.patch(
            f"/payroll/periods/{pid_a}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )
        period_b = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-02-22", "2084-02-28",
        )
        pid_b = period_b["payroll_period_id"]

        try:
            # Query period B — must return empty (no off records)
            resp = await session_client.get(
                f"/payroll/periods/{pid_b}/drivers-off",
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["period_id"] == pid_b
            assert data["total_count"] == 0, (
                "Off records from period A must not appear in period B's response"
            )

        finally:
            await session_client.patch(
                f"/payroll/periods/{pid_b}/status",
                json={"status": "Cancelled"},
                headers=auth(auth_token),
            )
            await _delete_status_key(direct_db, sk_id)
