"""
CP-2.5 — Period-level Drivers Off endpoint tests.

Tests for GET /payroll/periods/{period_id}/drivers-off

Uses year 2084 dates to avoid conflicts with existing test modules.
Status keys are inserted per-test (function-scoped direct_db) because the
seed data does not include PayrollStatusKeys rows.

Stage B3 Unit 8C-7: TestDriversOffFinalized (below) covers Locked/Archived,
where this endpoint now reads immutable calculation-snapshot Status
evidence via app.payroll.status_evidence instead of legacy DailyStatus
DraftLines joined to live PayrollStatusKeys. Uses year 2085 dates and its
own direct-SQL period/cleanup helpers (mirroring the proven approach in
test_cp5b_off_drivers.py) rather than the legacy _make_open_period POST
flow above, which requires exactly one pre-existing Open period per branch
and is unrelated to this unit.
"""
import itertools

import pytest
import pytest_asyncio
import httpx
from datetime import date as _date, timedelta as _timedelta
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


async def _insert_open_period_direct(
    direct_db,
    company_id: int,
    branch_id: int,
    start: str,
    end: str,
) -> dict:
    """
    Create an Open payroll period directly via SQL, bypassing the deprecated
    POST /payroll/periods Draft-creation endpoint, which now rejects fresh
    Draft creation with DRAFT_CREATION_REQUIRES_OPEN unless exactly one Open
    period already exists for the branch (see the endpoint's `deprecated`
    docstring in app/payroll/router.py). None of the TestDriversOff tests
    below assert anything about *how* a period reaches Open -- only that
    GET .../drivers-off behaves correctly once it is. Mirrors the direct-SQL
    period-construction technique already established for Locked/Archived
    coverage in TestDriversOffFinalized below (_insert_finalized_period) and
    in test_cp5b_off_drivers.py / test_cp2d_canonical_entry_state.py.
    Returns the same {"payroll_period_id": ...} shape the old two-step
    POST + PATCH-to-Open flow's response body gave callers.
    """
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (:cid, :bid, 'Open', :code, :name, 'Week', :start_date, :end_date)
            RETURNING payrollperiodid
        """),
        {
            "cid": company_id, "bid": branch_id,
            "code": f"CP25-{branch_id}-{start}", "name": f"CP25 {start}",
            "start_date": _date.fromisoformat(start), "end_date": _date.fromisoformat(end),
        },
    )).mappings().one()
    await direct_db.commit()
    return {"payroll_period_id": int(row["payrollperiodid"])}


async def _make_open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str,
    end: str,
    direct_db,
) -> dict:
    await _cancel_active_periods(client, token, branch_id)
    company_id = await _get_company_id(direct_db)
    return await _insert_open_period_direct(direct_db, company_id, branch_id, start, end)


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
            "2084-01-01", "2084-01-07", direct_db,
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
            "2084-01-08", "2084-01-14", direct_db,
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
            "2084-01-15", "2084-01-21", direct_db,
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
        direct_db,
    ):
        """Period with no off statuses → empty entries list and total_count=0."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-01-22", "2084-01-28", direct_db,
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
        direct_db,
    ):
        """ODA user → 403 on GET drivers-off endpoint."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-02-01", "2084-02-07", direct_db,
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
        direct_db,
    ):
        """User without payroll.view or payroll.entry → 403 on drivers-off endpoint."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2084-02-08", "2084-02-14", direct_db,
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
            "2084-02-15", "2084-02-21", direct_db,
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
            "2084-02-22", "2084-02-28", direct_db,
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


# ===========================================================================
# Stage B3 Unit 8C-7 — Locked/Archived immutable Status evidence
# ===========================================================================
#
# Mirrors the proven approach in test_cp5b_off_drivers.py (Unit 8C-5) and
# test_cp2d_canonical_entry_state.py's E25-E28 (Unit 8C-3): direct-SQL period
# construction, a real Submit -> Approve -> Finalize flow through the HTTP
# API for the drift/zero-FinalLine cases, and direct snapshot construction
# only for the legacy/unavailable/empty provenance cases that the supported
# workflow cannot produce on demand.

_FIN_BASE_DATE = _date(2085, 1, 1)
_FIN_COUNTER = itertools.count(1)


async def _clean_finalized(direct_db, branch_id: int) -> None:
    """Surgical teardown for CP25FIN-* periods, disabling each P6D/CP-4D/
    CP-5C immutable-evidence trigger by exact name (never DISABLE TRIGGER
    ALL -- see test_cp5b_off_drivers.py._clean for why), leaf-to-root."""
    period_ids = (await direct_db.execute(
        _text("""
            SELECT payrollperiodid FROM payroll.payrollperiods
            WHERE branchid = :branch_id AND periodcode LIKE 'CP25FIN-%'
        """),
        {"branch_id": branch_id},
    )).scalars().all()
    if period_ids:
        guards = [
            ("payroll.payrollfinallines", "trg_final_line_immutable"),
            ("payroll.payrollcalculationsnapshotlines", "trg_PayrollCalculationSnapshotLines_Immutable"),
            ("payroll.payrollperiodauditevidencesnapshotevents", "trg_PayrollPeriodAuditEvidenceSnapshotEvents_Immutable"),
            ("payroll.payrollperiodauditevidenceevents", "trg_PayrollPeriodAuditEvidenceEvents_Immutable"),
            ("payroll.payrollperiodauditevidencecoverage", "trg_PayrollPeriodAuditEvidenceCoverage_Immutable"),
            ("payroll.payrollcalculationsnapshotusedratedefinitions", "trg_PayrollCalculationSnapshotUsedRateDefinitions_Immutable"),
            ("payroll.payrollcalculationdrivertotals", "trg_PayrollCalculationDriverTotals_Immutable"),
            ("payroll.payrollcalculationsnapshotstatusentries", "trg_PayrollCalculationSnapshotStatusEntries_Immutable"),
            ("payroll.payrollcalculationsnapshotbonusevents", "trg_PayrollCalculationSnapshotBonusEvents_Immutable"),
            ("payroll.payrollperiodworkflowactionevidence", "trg_PayrollPeriodWorkflowActionEvidence_Immutable"),
            ("payroll.payrollcalculationsnapshots", "trg_PayrollCalculationSnapshots_Immutable"),
            ("payroll.payrollperiods", "trg_PayrollPeriods_AuditEvidenceDelete"),
        ]
        for table, trigger in guards:
            await direct_db.execute(_text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
        try:
            snapshot_subq = (
                "(SELECT payrollcalculationsnapshotid FROM payroll.payrollcalculationsnapshots "
                "WHERE payrollperiodid = ANY(:ids))"
            )
            drivertotal_subq = (
                "(SELECT payrollcalculationdrivertotalid FROM payroll.payrollcalculationdrivertotals "
                f"WHERE payrollcalculationsnapshotid IN {snapshot_subq})"
            )
            review_items_subq = (
                "(SELECT reviewitemid FROM review.managerreviewitems "
                "WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods' "
                "AND entityid IN (SELECT payrollperiodid::text FROM payroll.payrollperiods "
                "WHERE payrollperiodid = ANY(:ids)))"
            )
            for stmt in (
                f"DELETE FROM payroll.payrollcalculationsnapshotlines "
                f"WHERE payrollcalculationdrivertotalid IN {drivertotal_subq}",
                "DELETE FROM payroll.payrollperiodauditevidencesnapshotevents WHERE payrollperiodid = ANY(:ids)",
                "DELETE FROM payroll.payrollperiodauditevidenceevents WHERE payrollperiodid = ANY(:ids)",
                "DELETE FROM payroll.payrollperiodauditevidencecoverage WHERE payrollperiodid = ANY(:ids)",
                "DELETE FROM payroll.payrollcalculationsnapshotusedratedefinitions WHERE payrollperiodid = ANY(:ids)",
                f"DELETE FROM payroll.payrollcalculationdrivertotals WHERE payrollcalculationsnapshotid IN {snapshot_subq}",
                "DELETE FROM payroll.payrollcalculationsnapshotstatusentries WHERE payrollperiodid = ANY(:ids)",
                "DELETE FROM payroll.payrollcalculationsnapshotbonusevents WHERE payrollperiodid = ANY(:ids)",
                "DELETE FROM payroll.payrollperiodworkflowactionevidence WHERE payrollperiodid = ANY(:ids)",
                "UPDATE payroll.payrollperiods SET currentreturnreviewitemid = NULL WHERE payrollperiodid = ANY(:ids)",
                f"DELETE FROM review.managerreviewdecisions WHERE reviewitemid IN {review_items_subq}",
                f"DELETE FROM review.managerreviewitems WHERE reviewitemid IN {review_items_subq}",
                "DELETE FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = ANY(:ids)",
                "DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = ANY(:ids)",
                "DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = ANY(:ids)",
                "DELETE FROM payroll.payrollperioddriverdayentrystate WHERE payrollperiodid = ANY(:ids)",
                "DELETE FROM payroll.payrollperiods WHERE payrollperiodid = ANY(:ids)",
            ):
                await direct_db.execute(_text(stmt), {"ids": period_ids})
        finally:
            for table, trigger in reversed(guards):
                await direct_db.execute(_text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
    await direct_db.execute(
        _text("DELETE FROM payroll.payrollstatuskeys WHERE branchid = :branch_id AND statuscode LIKE 'CP25FIN_%'"),
        {"branch_id": branch_id},
    )
    await direct_db.commit()


async def _insert_finalized_period(
    direct_db, company_id: int, branch_id: int, suffix: str, status: str = "Open",
) -> tuple[int, _date]:
    offset = next(_FIN_COUNTER) * 14
    start = _FIN_BASE_DATE + _timedelta(days=offset)
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (:cid, :bid, :status, :code, :name, 'Week', :start_date, :end_date)
            RETURNING payrollperiodid
        """),
        {
            "cid": company_id, "bid": branch_id, "status": status,
            "code": f"CP25FIN-{suffix}-{offset}", "name": f"CP25FIN {suffix}",
            "start_date": start, "end_date": start + _timedelta(days=6),
        },
    )).mappings().one()
    await direct_db.commit()
    return int(row["payrollperiodid"]), start


async def _save_day_grid_status(
    client: httpx.AsyncClient, token: str, period_id: int, driver_id: int,
    work_date: _date, status_key_code: str,
) -> None:
    resp = await client.post(
        f"/payroll/periods/{period_id}/day-grid",
        headers=auth(token),
        json={"work_date": work_date.isoformat(),
              "rows": [{"driver_id": driver_id, "values": {}, "status_key": status_key_code}]},
    )
    assert resp.status_code == 200, resp.text


async def _submit_approve_finalize(
    client: httpx.AsyncClient, token: str, period_id: int, driver_id: int, work_date: _date,
) -> None:
    """Drive Open -> InReview -> Approved -> Locked through the real HTTP
    workflow, capturing immutable Status evidence at Submit (same sequence
    as test_cp5b_off_drivers.py._submit_approve_finalize)."""
    headers = auth(token)
    lines_resp = await client.get(
        f"/payroll/periods/{period_id}/lines", headers=headers, params={"status": "Active"},
    )
    if lines_resp.status_code == 200 and len(lines_resp.json()) == 0:
        await client.post(
            f"/payroll/periods/{period_id}/lines",
            headers=headers,
            json={"driver_id": driver_id, "work_date": work_date.isoformat(),
                  "line_type": "DailyNote", "notes": "filler"},
        )
    submit = await client.patch(
        f"/payroll/periods/{period_id}/status", headers=headers, json={"status": "InReview"},
    )
    assert submit.status_code == 200, f"InReview failed: {submit.text}"
    review_resp = await client.get("/review/items", headers=headers)
    assert review_resp.status_code == 200
    item = next(
        (i for i in review_resp.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, f"No pending review item for period {period_id}"
    decide = await client.post(
        f"/review/items/{item['review_item_id']}/decide",
        headers=headers,
        json={"decision": "Approved"},
    )
    assert decide.status_code == 200, f"Approval failed: {decide.text}"
    finalize = await client.post(f"/payroll/periods/{period_id}/finalize", headers=headers)
    assert finalize.status_code == 200, f"Finalize failed: {finalize.text}"


async def _insert_approved_review_item(
    direct_db, company_id: int, branch_id: int, period_id: int, snapshot_id: int,
) -> int:
    """Bind a snapshot to a period the way an Approved PeriodApproval review
    item does -- the exact selector status_evidence.resolve_finalized_snapshot
    trusts (same technique as test_cp5b_off_drivers.py._insert_approved_review_item)."""
    row = (await direct_db.execute(
        _text("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requesttype, entityschema, entityname, entityid,
                 title, status, payrollcalculationsnapshotid)
            VALUES (:cid, :bid, 'PeriodApproval', 'payroll', 'PayrollPeriods', :pid_text,
                    'Test review item', 'Approved', :sid)
            RETURNING reviewitemid
        """),
        {"cid": company_id, "bid": branch_id, "pid_text": str(period_id), "sid": snapshot_id},
    )).mappings().first()
    await direct_db.commit()
    return row["reviewitemid"]


@pytest.mark.asyncio
class TestDriversOffFinalized:

    async def test_drift_regression_historical_status_survives_statuskey_mutation(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """Case A: Submit -> Approve -> Finalize captures immutable Status
        evidence; mutating the CURRENT StatusKey afterward (label AND
        IsOffReason) must not change the historical /drivers-off result for
        the Locked period."""
        await _clean_finalized(direct_db, paytest_branch_id)
        company_id = await _get_company_id(direct_db)
        off_code = f"CP25FIN_DRIFT_{next(_FIN_COUNTER)}"
        sk_id = await _insert_off_status_key(direct_db, company_id, paytest_branch_id, off_code)
        period_id, start = await _insert_finalized_period(direct_db, company_id, paytest_branch_id, "DRIFT")

        try:
            await _save_day_grid_status(
                session_client, auth_token, period_id, paytest_driver_id, start, off_code,
            )

            # Case G sanity check: before Submit, the live (non-finalized)
            # path is unaffected by this unit -- same shape as the existing
            # TestDriversOff tests above, with status_evidence absent.
            pre_submit = await session_client.get(
                f"/payroll/periods/{period_id}/drivers-off", headers=auth(auth_token),
            )
            assert pre_submit.status_code == 200, pre_submit.text
            assert pre_submit.json()["total_count"] == 1
            assert pre_submit.json().get("status_evidence") is None

            await _submit_approve_finalize(
                session_client, auth_token, period_id, paytest_driver_id, start,
            )

            # Drift the CURRENT StatusKey after Locking -- label AND IsOffReason.
            await direct_db.execute(
                _text("""
                    UPDATE payroll.payrollstatuskeys
                    SET keyname = 'Mutated', isoffreason = FALSE
                    WHERE statuskeyid = :sid
                """),
                {"sid": sk_id},
            )
            await direct_db.commit()

            resp = await session_client.get(
                f"/payroll/periods/{period_id}/drivers-off", headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total_count"] == 1
            entry = data["entries"][0]
            assert entry["driver_id"] == paytest_driver_id
            assert entry["status_key_code"] == off_code
            assert entry["status_label"] == f"Test Off Key {off_code}", (
                "Must show the ORIGINAL captured label, not the drifted 'Mutated'"
            )
            assert data.get("status_evidence") == {"state": "AVAILABLE", "reason_code": None}
        finally:
            await _clean_finalized(direct_db, paytest_branch_id)
            await _delete_status_key(direct_db, sk_id)

    async def test_archived_period_uses_immutable_status_evidence(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """Case B: same immutable-evidence authority holds after Locked ->
        Archived, proving Archived is not treated as a live/editable status."""
        await _clean_finalized(direct_db, paytest_branch_id)
        company_id = await _get_company_id(direct_db)
        off_code = f"CP25FIN_ARCH_{next(_FIN_COUNTER)}"
        sk_id = await _insert_off_status_key(direct_db, company_id, paytest_branch_id, off_code)
        period_id, start = await _insert_finalized_period(direct_db, company_id, paytest_branch_id, "ARCH")

        try:
            await _save_day_grid_status(
                session_client, auth_token, period_id, paytest_driver_id, start, off_code,
            )
            await _submit_approve_finalize(
                session_client, auth_token, period_id, paytest_driver_id, start,
            )
            archive = await session_client.patch(
                f"/payroll/periods/{period_id}/status", headers=auth(auth_token),
                json={"status": "Archived"},
            )
            assert archive.status_code == 200, archive.text

            await direct_db.execute(
                _text("""
                    UPDATE payroll.payrollstatuskeys
                    SET keyname = 'Mutated', isoffreason = FALSE
                    WHERE statuskeyid = :sid
                """),
                {"sid": sk_id},
            )
            await direct_db.commit()

            resp = await session_client.get(
                f"/payroll/periods/{period_id}/drivers-off", headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total_count"] == 1
            assert data["entries"][0]["status_key_code"] == off_code
            assert data.get("status_evidence") == {"state": "AVAILABLE", "reason_code": None}
        finally:
            await _clean_finalized(direct_db, paytest_branch_id)
            await _delete_status_key(direct_db, sk_id)

    async def test_locked_period_legacy_snapshot_status_evidence_unavailable(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """Case C: a Locked period whose authoritative snapshot predates
        CP-5C (ReportEvidenceVersion IS NULL) must report status_evidence as
        UNAVAILABLE/LEGACY_NOT_CAPTURED, with no row fabricated from mutable
        PayrollStatusKeys or legacy DraftLines."""
        await _clean_finalized(direct_db, paytest_branch_id)
        company_id = await _get_company_id(direct_db)
        period_id, start = await _insert_finalized_period(
            direct_db, company_id, paytest_branch_id, "LEGACYSNAP", status="Locked",
        )
        snapshot_id = int((await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollcalculationsnapshots
                    (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
                     sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay)
                VALUES (:cid, :bid, :pid, 1, 'legacy', :source_hash, :snapshot_hash, 1, 0)
                RETURNING payrollcalculationsnapshotid
            """),
            {
                "cid": company_id, "bid": paytest_branch_id, "pid": period_id,
                "source_hash": "0" * 64, "snapshot_hash": "4" * 64,
            },
        )).scalar_one())
        await _insert_approved_review_item(
            direct_db, company_id, paytest_branch_id, period_id, snapshot_id,
        )
        await direct_db.execute(
            _text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)")
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     linescope, quantity, finalamount, sourcetype, approvedbyuserid,
                     approvedatutc, lockedatutc, sourcesnapshot)
                VALUES (:cid, :bid, :pid, :did, :wdate, 'HOURS', 'Daily', 1, 12.0000,
                        'DraftLine', 1, NOW(), NOW(), CAST(:snap AS JSONB))
            """),
            {
                "cid": company_id, "bid": paytest_branch_id, "pid": period_id, "did": paytest_driver_id,
                "wdate": start,
                "snap": (
                    '{"payroll_calculation_snapshot_id": %d, "revision_number": 1, '
                    '"snapshot_hash": "%s"}' % (snapshot_id, "4" * 64)
                ),
            },
        )
        await direct_db.commit()

        try:
            resp = await session_client.get(
                f"/payroll/periods/{period_id}/drivers-off", headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total_count"] == 0
            assert data["entries"] == []
            assert data.get("status_evidence") == {
                "state": "UNAVAILABLE", "reason_code": "LEGACY_NOT_CAPTURED",
            }
        finally:
            await _clean_finalized(direct_db, paytest_branch_id)

    async def test_locked_period_captured_snapshot_zero_status_rows_is_empty(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """Case D: a Locked period whose authoritative snapshot IS versioned
        (captured) but has zero PayrollCalculationSnapshotStatusEntries rows
        must report status_evidence as EMPTY -- a positive historical fact,
        not UNAVAILABLE -- with no fabricated off-driver rows."""
        await _clean_finalized(direct_db, paytest_branch_id)
        company_id = await _get_company_id(direct_db)
        period_id, start = await _insert_finalized_period(
            direct_db, company_id, paytest_branch_id, "EMPTYSNAP", status="Locked",
        )
        snapshot_id = int((await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollcalculationsnapshots
                    (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
                     sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
                     reportevidenceversion, reportevidencehash)
                VALUES (:cid, :bid, :pid, 1, 'legacy', :source_hash, :snapshot_hash, 1, 0,
                        1, :evidence_hash)
                RETURNING payrollcalculationsnapshotid
            """),
            {
                "cid": company_id, "bid": paytest_branch_id, "pid": period_id,
                "source_hash": "0" * 64, "snapshot_hash": "5" * 64,
                "evidence_hash": "6" * 64,
            },
        )).scalar_one())
        await _insert_approved_review_item(
            direct_db, company_id, paytest_branch_id, period_id, snapshot_id,
        )
        await direct_db.execute(
            _text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)")
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     linescope, quantity, finalamount, sourcetype, approvedbyuserid,
                     approvedatutc, lockedatutc, sourcesnapshot)
                VALUES (:cid, :bid, :pid, :did, :wdate, 'HOURS', 'Daily', 1, 12.0000,
                        'DraftLine', 1, NOW(), NOW(), CAST(:snap AS JSONB))
            """),
            {
                "cid": company_id, "bid": paytest_branch_id, "pid": period_id, "did": paytest_driver_id,
                "wdate": start,
                "snap": (
                    '{"payroll_calculation_snapshot_id": %d, "revision_number": 1, '
                    '"snapshot_hash": "%s"}' % (snapshot_id, "5" * 64)
                ),
            },
        )
        await direct_db.commit()

        try:
            resp = await session_client.get(
                f"/payroll/periods/{period_id}/drivers-off", headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total_count"] == 0
            assert data["entries"] == []
            assert data.get("status_evidence") == {"state": "EMPTY", "reason_code": None}
        finally:
            await _clean_finalized(direct_db, paytest_branch_id)

    async def test_locked_period_missing_snapshot_provenance_is_unavailable(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """Case E: a Locked period with no Approved PeriodApproval review
        item bound to a snapshot must report status_evidence as UNAVAILABLE/
        PROVENANCE_UNAVAILABLE -- never falling back to another snapshot or
        to mutable current state."""
        await _clean_finalized(direct_db, paytest_branch_id)
        company_id = await _get_company_id(direct_db)
        period_id, start = await _insert_finalized_period(
            direct_db, company_id, paytest_branch_id, "NOPROV", status="Locked",
        )
        await direct_db.execute(
            _text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)")
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     linescope, quantity, finalamount, sourcetype, approvedbyuserid,
                     approvedatutc, lockedatutc)
                VALUES (:cid, :bid, :pid, :did, :wdate, 'HOURS', 'Daily', 1, 12.0000,
                        'DraftLine', 1, NOW(), NOW())
            """),
            {
                "cid": company_id, "bid": paytest_branch_id, "pid": period_id, "did": paytest_driver_id,
                "wdate": start,
            },
        )
        await direct_db.commit()

        try:
            resp = await session_client.get(
                f"/payroll/periods/{period_id}/drivers-off", headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total_count"] == 0
            assert data["entries"] == []
            assert data.get("status_evidence") == {
                "state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE",
            }
        finally:
            await _clean_finalized(direct_db, paytest_branch_id)

    async def test_locked_period_zero_final_lines_still_resolves_via_review_item_binding(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """Case F: a period whose only entries are Status (no billable
        pay-item line) finalizes with zero FinalLines -- /drivers-off must
        still resolve the immutable Status evidence correctly through the
        Approved review-item binding, not treat empty FinalLines as
        unavailable provenance."""
        await _clean_finalized(direct_db, paytest_branch_id)
        company_id = await _get_company_id(direct_db)
        off_code = f"CP25FIN_ZEROFL_{next(_FIN_COUNTER)}"
        await _insert_off_status_key(direct_db, company_id, paytest_branch_id, off_code)
        period_id, start = await _insert_finalized_period(direct_db, company_id, paytest_branch_id, "ZEROFL")

        try:
            await _save_day_grid_status(
                session_client, auth_token, period_id, paytest_driver_id, start, off_code,
            )
            await _submit_approve_finalize(
                session_client, auth_token, period_id, paytest_driver_id, start,
            )

            final_lines = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
                {"pid": period_id},
            )).scalar_one()
            assert final_lines == 0, (
                "A Status-only day must finalize with zero FinalLines for this test to be meaningful"
            )

            resp = await session_client.get(
                f"/payroll/periods/{period_id}/drivers-off", headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total_count"] == 1
            assert data["entries"][0]["status_key_code"] == off_code
            assert data.get("status_evidence") == {"state": "AVAILABLE", "reason_code": None}
        finally:
            await _clean_finalized(direct_db, paytest_branch_id)
