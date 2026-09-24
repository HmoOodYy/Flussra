"""Payroll Setup policy and candidate-based period safety integration tests.

Covers Week/Biweek/Month/Custom cadence and chaining, branch isolation,
non-Cancelled chronology and Cancelled reuse, immutable Version publication
boundaries, entry counts, and DRIVER/ODA and cross-branch access denial.
Tests use fresh setup-assigned branches; historical statuses needed for lower-
level chronology cases are seeded without rewriting finalized history.
"""

import itertools
import pytest
import httpx
from datetime import date, timedelta
from uuid import uuid4
from sqlalchemy import text as _text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from app.payroll_setup.policy import assign_setup, create_draft, create_setup, publish_version
from app.payroll_setup.errors import PolicyError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_counter = itertools.count(1)


def _uid() -> str:
    return f"pss{next(_counter):04d}"


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _fresh_candidate_branch(
    test_database_url: str, frequency: str, anchor: str,
    *, interval_days: int | None = None, assign: bool = True,
) -> int:
    """Create one branch with current company-owned setup authority per test."""
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as db:
            code = f"PSS_{uuid4().hex[:12]}"
            branch_id = (await db.execute(_text("""
                INSERT INTO core.branches
                    (companyid, branchcode, branchname, status, isdefault)
                VALUES (1, :code, :name, 'Active', FALSE)
                RETURNING branchid
            """), {"code": code, "name": f"PSS isolated {code}"})).scalar_one()
            user_id = (await db.execute(_text("""
                SELECT userid FROM sec.users
                WHERE companyid = 1 AND username = 'admin'
            """))).scalar_one()
            setup_id = await create_setup(1, user_id, code, "PSS cadence setup", db)
            draft_id = await create_draft(
                1, user_id, setup_id, db,
                payroll_frequency=frequency, anchor_start_date=date.fromisoformat(anchor),
                custom_interval_days=interval_days,
                normal_days_off_mask=0,
            )
            await publish_version(
                1, user_id, setup_id, draft_id, date.fromisoformat(anchor), db,
            )
            if assign:
                await assign_setup(
                    1, user_id, branch_id, setup_id, date.fromisoformat(anchor), db,
                )
            return branch_id
    finally:
        await engine.dispose()


async def _publish_candidate_version(
    test_database_url: str, branch_id: int, frequency: str,
    anchor: str, *, interval_days: int | None = None,
) -> int:
    """Publish a policy version on a test branch's existing company Setup."""
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as db:
            setup_id = (await db.execute(_text("""
                SELECT payrollsetupid
                FROM payroll.branchpayrollsetupassignments
                WHERE companyid = 1 AND branchid = :bid AND withdrawnatutc IS NULL
            """), {"bid": branch_id})).scalar_one()
            user_id = (await db.execute(_text("""
                SELECT userid FROM sec.users
                WHERE companyid = 1 AND username = 'admin'
            """))).scalar_one()
            draft_id = await create_draft(
                1, user_id, setup_id, db,
                payroll_frequency=frequency, anchor_start_date=date.fromisoformat(anchor),
                custom_interval_days=interval_days, normal_days_off_mask=0,
            )
            return await publish_version(
                1, user_id, setup_id, draft_id, date.fromisoformat(anchor), db,
            )
    finally:
        await engine.dispose()


async def _preview_candidate(
    client: httpx.AsyncClient, token: str, branch_id: int,
    mode: str = "OPEN_CREATION",
) -> dict:
    response = await client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": mode}, headers=_hdr(token),
    )
    assert response.status_code == 200, response.text
    return response.json()["selected"]


async def _create_candidate(
    client: httpx.AsyncClient, token: str, branch_id: int,
    selected: dict,
) -> dict:
    response = await client.post(
        f"/payroll/branches/{branch_id}/period-creations",
        json={"candidate_key": selected["candidate_key"]}, headers=_hdr(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Helper: create a driver-role user (for 403 tests)
# ---------------------------------------------------------------------------

async def _make_driver_user_token(
    client: httpx.AsyncClient,
    admin_token: str,
    branch_id: int,
) -> str:
    """Create a user with DRIVER companyrole (ODA scope) and return its JWT."""
    uname = f"drv_{_uid()}"

    # Create user
    u = await client.post(
        "/admin/users",
        json={
            "username": uname,
            "display_name": uname,
            "password": "TestPass1234!",
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=_hdr(admin_token),
    )
    assert u.status_code == 201, f"User create failed: {u.text}"
    user_id = u.json()["user_id"]

    # Get DRIVER companyrole id
    roles_resp = await client.get("/admin/company-roles", headers=_hdr(admin_token))
    assert roles_resp.status_code == 200
    driver_role = next(
        (r for r in roles_resp.json() if r["role_code"] == "DRIVER"), None
    )
    assert driver_role is not None, "DRIVER company role not found"
    driver_role_id = driver_role["company_role_id"]

    # Assign DRIVER role with ODA scope
    ar = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json={
            "company_role_id": driver_role_id,
            "scope_type": "OwnDriverDataOnly",
            "branch_id": branch_id,
        },
        headers=_hdr(admin_token),
    )
    assert ar.status_code == 201, f"Role assign failed: {ar.text}"

    # Login
    login = await client.post("/auth/login", json={
        "username": uname,
        "password": "TestPass1234!",
        "company_code": "DEMO",
    })
    assert login.status_code == 200, f"Driver login failed: {login.text}"
    return login.json()["access_token"]


# ===========================================================================
# A. Weekly period generation
# ===========================================================================

class TestWeeklyPeriodDates:
    """Rule A+B: weekly periods — anchor start, 7-day span, day-after chaining."""

    # Anchor: 2090-01-07 (Monday)
    ANCHOR = "2090-01-07"

    async def test_weekly_setup_first_period_start_equals_anchor(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """First period start_date MUST equal anchor_start_date."""
        branch_id = await _fresh_candidate_branch(test_database_url, "Week", self.ANCHOR)
        body = await _preview_candidate(client, auth_token, branch_id)
        assert body["start_date"] == self.ANCHOR
        assert body["period_type"] == "Week"

    async def test_weekly_setup_first_period_end_is_anchor_plus_6(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """First period end_date MUST be anchor + 6 days."""
        branch_id = await _fresh_candidate_branch(test_database_url, "Week", self.ANCHOR)
        selected = await _preview_candidate(client, auth_token, branch_id)
        anchor = date.fromisoformat(self.ANCHOR)
        expected_end = (anchor + timedelta(days=6)).isoformat()
        assert selected["end_date"] == expected_end

    async def test_weekly_next_period_starts_day_after_prior_end(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """After creating P1, the next candidate starts the day after P1 ends."""
        branch_id = await _fresh_candidate_branch(test_database_url, "Week", self.ANCHOR)
        anchor = date.fromisoformat(self.ANCHOR)
        p1_end = anchor + timedelta(days=6)
        first = await _preview_candidate(client, auth_token, branch_id)
        created = await _create_candidate(client, auth_token, branch_id, first)
        assert created["end_date"] == p1_end.isoformat()
        body = await _preview_candidate(client, auth_token, branch_id, "PREPARED_CREATION")
        expected_start = (p1_end + timedelta(days=1)).isoformat()
        expected_end   = (p1_end + timedelta(days=7)).isoformat()
        assert body["start_date"] == expected_start
        assert body["end_date"]   == expected_end


# ===========================================================================
# B. Bi-weekly period generation
# ===========================================================================

class TestBiweeklyPeriodDates:
    """Rules C+D: bi-weekly periods — anchor start, 14-day span, day-after chaining."""

    ANCHOR = "2090-02-03"

    async def test_biweekly_first_period_start_equals_anchor(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        branch_id = await _fresh_candidate_branch(test_database_url, "Biweek", self.ANCHOR)
        body = await _preview_candidate(client, auth_token, branch_id)
        assert body["start_date"] == self.ANCHOR
        assert body["period_type"] == "Biweek"

    async def test_biweekly_first_period_end_is_anchor_plus_13(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        branch_id = await _fresh_candidate_branch(test_database_url, "Biweek", self.ANCHOR)
        selected = await _preview_candidate(client, auth_token, branch_id)
        anchor = date.fromisoformat(self.ANCHOR)
        expected_end = (anchor + timedelta(days=13)).isoformat()
        assert selected["end_date"] == expected_end

    async def test_biweekly_next_period_starts_day_after_prior_end(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        branch_id = await _fresh_candidate_branch(test_database_url, "Biweek", self.ANCHOR)
        anchor = date.fromisoformat(self.ANCHOR)
        p1_end = anchor + timedelta(days=13)
        first = await _preview_candidate(client, auth_token, branch_id)
        created = await _create_candidate(client, auth_token, branch_id, first)
        assert created["end_date"] == p1_end.isoformat()
        body = await _preview_candidate(client, auth_token, branch_id, "PREPARED_CREATION")
        expected_start = (p1_end + timedelta(days=1)).isoformat()
        expected_end   = (p1_end + timedelta(days=14)).isoformat()
        assert body["start_date"] == expected_start
        assert body["end_date"]   == expected_end


# ===========================================================================
# C. Monthly period generation
# ===========================================================================

class TestMonthlyPeriodDates:
    """Rule E: monthly is start-date anchored; Feb edge case respected."""

    # 2090-03-01 → end = 2090-03-31 (one calendar month)
    ANCHOR = "2090-03-01"

    async def test_monthly_first_period_end_is_one_calendar_month(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """First period must span exactly one calendar month (start-date anchored)."""
        branch_id = await _fresh_candidate_branch(test_database_url, "Month", self.ANCHOR)
        body = await _preview_candidate(client, auth_token, branch_id)
        assert body["start_date"] == self.ANCHOR
        # 2090-03-01 → next month same day = 2090-04-01 → end = 2090-03-31
        assert body["end_date"] == "2090-03-31"
        assert body["period_type"] == "Month"

    async def test_monthly_next_period_starts_day_after_prior_end(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """After P1 (Mar), next period starts Apr 01 and ends Apr 30."""
        branch_id = await _fresh_candidate_branch(test_database_url, "Month", self.ANCHOR)
        p1_end = "2090-03-31"
        first = await _preview_candidate(client, auth_token, branch_id)
        created = await _create_candidate(client, auth_token, branch_id, first)
        assert created["end_date"] == p1_end
        body = await _preview_candidate(client, auth_token, branch_id, "PREPARED_CREATION")
        # Apr 1 → next same day = May 1 → end = Apr 30
        assert body["start_date"] == "2090-04-01"
        assert body["end_date"]   == "2090-04-30"

    async def test_monthly_feb_edge_case(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """
        Anchor on Jan 31 → end = Feb 27 (next month same day = Feb 28 min(31,28),
        minus 1 day = Feb 27).  This proves _month_end() handles the short-month
        edge case without raising an exception.
        """
        branch_id = await _fresh_candidate_branch(test_database_url, "Month", "2090-01-31")
        body = await _preview_candidate(client, auth_token, branch_id)
        assert body["start_date"] == "2090-01-31"
        # 2090 is not a leap year (not div by 4 with appropriate conditions):
        # Feb 2090 has 28 days.
        # min(31, 28) = 28 → next_same_day = 2090-02-28, end = 2090-02-27
        assert body["end_date"] == "2090-02-27"


# ===========================================================================
# D. Custom frequency — fixed cadence
# ===========================================================================

class TestCustomFrequency:
    """
    Custom frequency = fixed cadence.  Saving custom_interval_days makes the
    system auto-compute every subsequent period by advancing start+interval−1.

    Tests:
    1.  Published Custom version persists custom_interval_days.
    2.  First-period dates are anchor → anchor + interval − 1.
    3.  Second period chains from day-after first period end.
    4.  Third period chains correctly (multi-hop).
    5.  Invalid Custom intervals fail publication.
    6.  Non-Custom frequency with null interval still publishes.
    7.  Branch A (10-day) and Branch B (14-day) are independent.
    8.  Branch B period does not change Branch A's candidate dates.
    9.  Policy publication respects a Custom period's boundary.
    10. An unassigned branch fails closed in candidate preview.
    """

    # Anchor in the far future to avoid conflicts with other test classes.
    ANCHOR = "2096-06-21"
    INTERVAL = 10                            # 10-day cycle
    # Derived: end = 2096-06-30
    FIRST_END = "2096-06-30"
    SECOND_START = "2096-07-01"             # day after FIRST_END
    SECOND_END   = "2096-07-10"             # SECOND_START + 10 − 1
    THIRD_START  = "2096-07-11"
    THIRD_END    = "2096-07-20"

    # Branch B uses a 14-day cycle, different anchor
    ANCHOR_B  = "2096-08-01"
    INTERVAL_B = 14
    FIRST_END_B = "2096-08-14"

    async def test_custom_setup_saves_interval(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """The published company Version stores the explicit Custom interval."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR, interval_days=self.INTERVAL,
        )
        version = (await direct_db.execute(_text("""
            SELECT v.payrollfrequency, v.customintervaldays, v.anchorstartdate,
                   v.lifecyclestate
            FROM payroll.payrollsetupversions v
            JOIN payroll.branchpayrollsetupassignments a
              ON a.payrollsetupid = v.payrollsetupid
            WHERE a.branchid = :bid AND a.companyid = 1
        """), {"bid": branch_id})).mappings().one()
        assert version["lifecyclestate"] == "Published"
        assert version["payrollfrequency"] == "Custom"
        assert version["customintervaldays"] == self.INTERVAL
        assert version["anchorstartdate"] == date.fromisoformat(self.ANCHOR)

    async def test_custom_first_period_start_equals_anchor(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """First candidate starts at the published Custom anchor."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR, interval_days=self.INTERVAL,
        )
        body = await _preview_candidate(client, auth_token, branch_id)
        assert body["start_date"] == self.ANCHOR
        assert body["period_type"] == "Custom"

    async def test_custom_first_period_end_is_anchor_plus_interval_minus_1(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """First period end = anchor + interval − 1 days."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR, interval_days=self.INTERVAL,
        )
        selected = await _preview_candidate(client, auth_token, branch_id)
        assert selected["end_date"] == self.FIRST_END

    async def test_custom_second_period_chains_from_first(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """After P1 is created, the next candidate starts one day later."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR, interval_days=self.INTERVAL,
        )
        first = await _preview_candidate(client, auth_token, branch_id)
        created = await _create_candidate(client, auth_token, branch_id, first)
        assert created["end_date"] == self.FIRST_END
        body = await _preview_candidate(client, auth_token, branch_id, "PREPARED_CREATION")
        assert body["start_date"] == self.SECOND_START
        assert body["end_date"]   == self.SECOND_END

    async def test_custom_third_period_chains_correctly(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """After P1 Open and P2 Draft, preview derives P3 without creating it."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR, interval_days=self.INTERVAL,
        )
        first = await _preview_candidate(client, auth_token, branch_id)
        await _create_candidate(client, auth_token, branch_id, first)
        second = await _preview_candidate(client, auth_token, branch_id, "PREPARED_CREATION")
        assert second["start_date"] == self.SECOND_START
        assert second["end_date"] == self.SECOND_END
        await _create_candidate(client, auth_token, branch_id, second)
        body = await _preview_candidate(client, auth_token, branch_id, "PREPARED_CREATION")
        assert body["creatable"] is False  # Open + Draft fills the current slots.
        assert body["start_date"] == self.THIRD_START
        assert body["end_date"]   == self.THIRD_END

    async def test_custom_interval_zero_rejected(
        self,
        test_database_url: str,
    ):
        """Custom interval zero cannot be published."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2097-03-01",
        )
        with pytest.raises(IntegrityError) as error:
            await _publish_candidate_version(
                test_database_url, branch_id, "Custom", "2097-03-08", interval_days=0,
            )
        assert "ck_payrollsetupversions_interval" in str(error.value.orig)

    async def test_custom_interval_negative_rejected(
        self,
        test_database_url: str,
    ):
        """A negative Custom interval cannot be published."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2097-03-01",
        )
        with pytest.raises(IntegrityError) as error:
            await _publish_candidate_version(
                test_database_url, branch_id, "Custom", "2097-03-08", interval_days=-1,
            )
        assert "ck_payrollsetupversions_interval" in str(error.value.orig)

    async def test_custom_missing_interval_rejected(
        self,
        test_database_url: str,
    ):
        """Custom frequency without an interval cannot be published."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2097-03-01",
        )
        with pytest.raises(PolicyError) as error:
            await _publish_candidate_version(
                test_database_url, branch_id, "Custom", "2097-03-08",
            )
        assert error.value.code == "INVALID_SCHEDULE"

    async def test_non_custom_frequency_with_null_interval_still_works(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """
        Publishing a Weekly Version without custom_interval_days succeeds.
        """
        branch_id = await _fresh_candidate_branch(test_database_url, "Week", "2097-06-01")
        interval = (await direct_db.execute(_text("""
            SELECT v.customintervaldays
            FROM payroll.payrollsetupversions v
            JOIN payroll.branchpayrollsetupassignments a
              ON a.payrollsetupid = v.payrollsetupid
            WHERE a.branchid = :bid AND a.companyid = 1
        """), {"bid": branch_id})).scalar_one_or_none()
        assert interval is None
        selected = await _preview_candidate(client, auth_token, branch_id)
        assert selected["period_type"] == "Week"

    async def test_custom_branch_isolation_a_and_b_independent(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """
        Branch A (10-day) and Branch B (14-day) maintain separate intervals and
        generate independent next-period-dates.
        """
        branch_a = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR, interval_days=self.INTERVAL,
        )
        branch_b = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR_B, interval_days=self.INTERVAL_B,
        )
        body_a = await _preview_candidate(client, auth_token, branch_a)
        body_b = await _preview_candidate(client, auth_token, branch_b)

        assert body_a["start_date"] == self.ANCHOR
        assert body_a["end_date"]   == self.FIRST_END          # 10-day end
        assert (date.fromisoformat(body_a["end_date"]) - date.fromisoformat(body_a["start_date"])).days == self.INTERVAL - 1

        assert body_b["start_date"] == self.ANCHOR_B
        assert body_b["end_date"]   == self.FIRST_END_B        # 14-day end
        assert (date.fromisoformat(body_b["end_date"]) - date.fromisoformat(body_b["start_date"])).days == self.INTERVAL_B - 1

    async def test_custom_period_in_branch_b_does_not_affect_branch_a(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """Creating a period in Branch B must not shift Branch A's next-dates."""
        branch_a = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR, interval_days=self.INTERVAL,
        )
        branch_b = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR_B, interval_days=self.INTERVAL_B,
        )
        first_b = await _preview_candidate(client, auth_token, branch_b)
        await _create_candidate(client, auth_token, branch_b, first_b)
        next_b = await _preview_candidate(client, auth_token, branch_b, "PREPARED_CREATION")
        assert next_b["start_date"] == "2096-08-15"
        body_a = await _preview_candidate(client, auth_token, branch_a)
        # Branch A has no periods — must still start at ANCHOR
        assert body_a["start_date"] == self.ANCHOR

    async def test_setup_change_safety_respected_for_custom(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """
        A policy Version boundary inside the existing Custom period fails;
        the adjacent boundary after its end may be published.
        """
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR, interval_days=self.INTERVAL,
        )
        first = await _preview_candidate(client, auth_token, branch_id)
        created = await _create_candidate(client, auth_token, branch_id, first)
        assert created["end_date"] == self.FIRST_END
        with pytest.raises(PolicyError) as error:
            await _publish_candidate_version(
                test_database_url, branch_id, "Custom", self.FIRST_END,
                interval_days=self.INTERVAL,
            )
        assert error.value.code in {"PREDECESSOR_BOUNDARY_INVALID", "PERIOD_HISTORY_CONFLICT"}
        safe_anchor = (date.fromisoformat(self.FIRST_END) + timedelta(days=1)).isoformat()
        version_id = await _publish_candidate_version(
            test_database_url, branch_id, "Custom", safe_anchor,
            interval_days=self.INTERVAL,
        )
        assert version_id > 0
        next_period = await _preview_candidate(client, auth_token, branch_id, "PREPARED_CREATION")
        assert next_period["start_date"] == self.SECOND_START
        assert next_period["end_date"] == self.SECOND_END

    async def test_no_setup_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """A published company Setup without a Branch Assignment fails closed."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Custom", self.ANCHOR,
            interval_days=self.INTERVAL, assign=False,
        )
        resp = await client.get(
            f"/payroll/branches/{branch_id}/period-candidates",
            params={"mode": "OPEN_CREATION"},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "PAYROLL_SETUP_REQUIRED"


# ===========================================================================
# E. Branch isolation
# ===========================================================================

class TestBranchIsolation:
    """
    Rules G–I: Branch A and Branch B are independent.
    - Their setups are separate; each generates dates from its own anchor.
    - A period in Branch B does not influence Branch A's candidate dates.
    """

    ANCHOR_A = "2090-05-05"   # weekly
    ANCHOR_B = "2090-05-12"   # biweekly

    async def test_branch_a_first_period_from_own_anchor(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """Branch A candidate uses its own Week setup, not Branch B's."""
        branch_a = await _fresh_candidate_branch(test_database_url, "Week", self.ANCHOR_A)
        await _fresh_candidate_branch(test_database_url, "Biweek", self.ANCHOR_B)
        body_a = await _preview_candidate(client, auth_token, branch_a)
        assert body_a["start_date"] == self.ANCHOR_A
        assert body_a["period_type"] == "Week"

    async def test_branch_b_first_period_from_own_anchor(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        await _fresh_candidate_branch(test_database_url, "Week", self.ANCHOR_A)
        branch_b = await _fresh_candidate_branch(test_database_url, "Biweek", self.ANCHOR_B)
        body_b = await _preview_candidate(client, auth_token, branch_b)
        assert body_b["start_date"] == self.ANCHOR_B
        assert body_b["period_type"] == "Biweek"

    async def test_period_in_branch_b_does_not_affect_branch_a(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """Creating Branch B's candidate leaves Branch A's dates unchanged."""
        branch_a = await _fresh_candidate_branch(test_database_url, "Week", self.ANCHOR_A)
        branch_b = await _fresh_candidate_branch(test_database_url, "Biweek", self.ANCHOR_B)
        anchor_b = date.fromisoformat(self.ANCHOR_B)
        b_end = anchor_b + timedelta(days=13)
        first_b = await _preview_candidate(client, auth_token, branch_b)
        created_b = await _create_candidate(client, auth_token, branch_b, first_b)
        assert created_b["end_date"] == b_end.isoformat()
        body_a = await _preview_candidate(client, auth_token, branch_a)
        assert body_a["start_date"] == self.ANCHOR_A

    async def test_branch_a_cannot_use_branch_b_setup(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """
        Candidate dates for Branch A must reflect Branch A's frequency (Week),
        not Branch B's (Biweek), even if Branch B has existing periods.
        """
        branch_a = await _fresh_candidate_branch(test_database_url, "Week", self.ANCHOR_A)
        branch_b = await _fresh_candidate_branch(test_database_url, "Biweek", self.ANCHOR_B)
        first_b = await _preview_candidate(client, auth_token, branch_b)
        await _create_candidate(client, auth_token, branch_b, first_b)
        body_a = await _preview_candidate(client, auth_token, branch_a)
        # Branch A is weekly: end - start should be 6 days
        a_start = date.fromisoformat(body_a["start_date"])
        a_end   = date.fromisoformat(body_a["end_date"])
        assert (a_end - a_start).days == 6, (
            f"Expected 6-day weekly span, got {(a_end - a_start).days} days. "
            f"Branch A may be using Branch B's bi-weekly setup."
        )


# ===========================================================================
# F. Overlap prevention
# ===========================================================================

class TestOverlapPrevention:
    """
    Candidate chronology excludes Cancelled history and follows every other
    status. Active Open/Draft slots also prevent duplicate creation.

    Each test owns a fresh assigned branch. Historical statuses that cannot be
    reached without a full workflow are seeded once by SQL and never changed.
    """
    RANGE_START = "2091-02-03"
    RANGE_END   = "2091-02-09"

    async def _seed_historical_period(self, direct_db, branch_id: int, status: str) -> int:
        """Construct a historical fixture without status rewrites or trigger changes."""
        return (await direct_db.execute(_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname,
                 periodtype, startdate, enddate)
            VALUES (1, :bid, :status, :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """), {
            "bid": branch_id, "status": status,
            "code": f"PSS-{status}-{uuid4().hex[:8]}",
            "name": f"PSS {status} history",
            "start": date.fromisoformat(self.RANGE_START),
            "end": date.fromisoformat(self.RANGE_END),
        })).scalar_one()

    async def _assert_historical_chronology(
        self, client: httpx.AsyncClient, token: str, test_database_url: str,
        direct_db, status: str,
    ) -> None:
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", self.RANGE_START,
        )
        historical_id = await self._seed_historical_period(direct_db, branch_id, status)
        selected = await _preview_candidate(client, token, branch_id)
        expected_start = (date.fromisoformat(self.RANGE_END) + timedelta(days=1)).isoformat()
        expected_end = (date.fromisoformat(self.RANGE_END) + timedelta(days=7)).isoformat()
        assert selected["creatable"] is True
        assert selected["start_date"] == expected_start
        assert selected["end_date"] == expected_end
        created = await _create_candidate(client, token, branch_id, selected)
        assert created["start_date"] == expected_start
        assert created["end_date"] == expected_end
        assert created["status"] == "Open"
        retained_status = (await direct_db.execute(_text("""
            SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid
        """), {"pid": historical_id})).scalar_one()
        assert retained_status == status

    async def test_overlap_with_draft_is_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """An existing Draft occupies the prepared slot and advances chronology."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", self.RANGE_START,
        )
        first = await _preview_candidate(client, auth_token, branch_id)
        opened = await _create_candidate(client, auth_token, branch_id, first)
        prepared = await _preview_candidate(client, auth_token, branch_id, "PREPARED_CREATION")
        drafted = await _create_candidate(client, auth_token, branch_id, prepared)
        assert opened["end_date"] == self.RANGE_END
        assert drafted["start_date"] == "2091-02-10"
        assert drafted["end_date"] == "2091-02-16"
        for mode in ("OPEN_CREATION", "PREPARED_CREATION"):
            selected = await _preview_candidate(client, auth_token, branch_id, mode)
            assert selected["creatable"] is False
            assert selected["start_date"] == "2091-02-17"

    async def test_overlap_with_open_is_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """An Open slot blocks another Open; its next Draft is adjacent."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", self.RANGE_START,
        )
        first = await _preview_candidate(client, auth_token, branch_id)
        opened = await _create_candidate(client, auth_token, branch_id, first)
        assert opened["start_date"] == self.RANGE_START
        assert opened["end_date"] == self.RANGE_END
        another_open = await _preview_candidate(client, auth_token, branch_id)
        assert another_open["creatable"] is False
        prepared = await _preview_candidate(client, auth_token, branch_id, "PREPARED_CREATION")
        assert prepared["creatable"] is True
        assert prepared["start_date"] == "2091-02-10"

    async def test_overlap_with_inreview_is_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """InReview history remains reserved; the next candidate follows it."""
        await self._assert_historical_chronology(
            client, auth_token, test_database_url, direct_db, "InReview",
        )

    async def test_overlap_with_approved_is_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """Approved history remains reserved; the next candidate follows it."""
        await self._assert_historical_chronology(
            client, auth_token, test_database_url, direct_db, "Approved",
        )

    async def test_overlap_with_locked_is_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """
        Locked history reserves its range and is retained unchanged.
        """
        await self._assert_historical_chronology(
            client, auth_token, test_database_url, direct_db, "Locked",
        )

    async def test_overlap_with_archived_is_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """
        Archived history reserves its range and is retained unchanged.
        """
        await self._assert_historical_chronology(
            client, auth_token, test_database_url, direct_db, "Archived",
        )

    async def test_cancelled_period_does_not_block_same_dates(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """
        A Cancelled historical row does not reserve the candidate's dates.
        """
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", self.RANGE_START,
        )
        cancelled_id = await self._seed_historical_period(direct_db, branch_id, "Cancelled")
        selected = await _preview_candidate(client, auth_token, branch_id)
        assert selected["creatable"] is True
        assert selected["start_date"] == self.RANGE_START
        assert selected["end_date"] == self.RANGE_END
        created = await _create_candidate(client, auth_token, branch_id, selected)
        assert created["start_date"] == self.RANGE_START
        assert created["end_date"] == self.RANGE_END
        assert created["payroll_period_id"] != cancelled_id
        retained_status = (await direct_db.execute(_text("""
            SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid
        """), {"pid": cancelled_id})).scalar_one()
        assert retained_status == "Cancelled"

    async def test_adjacent_period_after_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """Period starting the day after an existing period ends must be accepted."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", self.RANGE_START,
        )
        first = await _preview_candidate(client, auth_token, branch_id)
        opened = await _create_candidate(client, auth_token, branch_id, first)
        assert opened["end_date"] == self.RANGE_END
        adj_start = (date.fromisoformat(self.RANGE_END) + timedelta(days=1)).isoformat()
        adj_end   = (date.fromisoformat(self.RANGE_END) + timedelta(days=7)).isoformat()
        selected = await _preview_candidate(client, auth_token, branch_id, "PREPARED_CREATION")
        assert selected["start_date"] == adj_start
        assert selected["end_date"] == adj_end
        prepared = await _create_candidate(client, auth_token, branch_id, selected)
        assert prepared["status"] == "Draft"
        assert prepared["start_date"] == adj_start
        assert prepared["end_date"] == adj_end


# ===========================================================================
# G. Setup-change safety
# ===========================================================================

class TestSetupChangeSafety:
    """
    Published Version boundaries cannot rewrite non-Cancelled period history.
    The adjacent boundary is allowed; Cancelled periods do not reserve it.
    """

    PERIOD_START = "2092-01-06"
    PERIOD_END   = "2092-01-12"
    ANCHOR       = "2092-01-06"

    async def _seed_period(
        self, direct_db, branch_id: int, status: str, start: str, end: str,
    ) -> int:
        """Insert a historical fixture on a fresh branch without status rewrites."""
        return (await direct_db.execute(_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname,
                 periodtype, startdate, enddate)
            VALUES (1, :bid, :status, :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """), {
            "bid": branch_id, "status": status,
            "code": f"PSS-SETUP-{uuid4().hex[:12]}",
            "name": f"PSS {status} policy boundary",
            "start": date.fromisoformat(start), "end": date.fromisoformat(end),
        })).scalar_one()

    async def test_setup_blocked_when_existing_period_not_cancelled(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """
        A valid weekly boundary cannot change authority for an existing Open.
        """
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", self.ANCHOR,
        )
        period_id = await self._seed_period(
            direct_db, branch_id, "Open", "2092-01-13", "2092-01-19",
        )
        with pytest.raises(PolicyError) as error:
            await _publish_candidate_version(
                test_database_url, branch_id, "Week", "2092-01-13",
            )
        assert error.value.code == "PERIOD_HISTORY_CONFLICT"
        status = (await direct_db.execute(_text("""
            SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid
        """), {"pid": period_id})).scalar_one()
        assert status == "Open"

    async def test_setup_blocked_when_anchor_before_period_end(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """A Version boundary inside a non-Cancelled period is rejected."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", self.ANCHOR,
        )
        period_id = await self._seed_period(
            direct_db, branch_id, "Open", "2092-01-13", "2092-01-19",
        )
        with pytest.raises(PolicyError) as error:
            await _publish_candidate_version(
                test_database_url, branch_id, "Week", "2092-01-16",
            )
        assert error.value.code == "PERIOD_HISTORY_CONFLICT"
        status = (await direct_db.execute(_text("""
            SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid
        """), {"pid": period_id})).scalar_one()
        assert status == "Open"

    async def test_setup_allowed_when_anchor_after_period_end(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """The first weekly boundary after an Open period may be published."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", self.ANCHOR,
        )
        first = await _preview_candidate(client, auth_token, branch_id)
        opened = await _create_candidate(client, auth_token, branch_id, first)
        assert opened["end_date"] == self.PERIOD_END
        version_id = await _publish_candidate_version(
            test_database_url, branch_id, "Week", "2092-01-13",
        )
        assert version_id > 0
        next_period = await _preview_candidate(
            client, auth_token, branch_id, "PREPARED_CREATION",
        )
        assert next_period["start_date"] == "2092-01-13"
        assert next_period["end_date"] == "2092-01-19"

    async def test_setup_allowed_after_all_periods_cancelled(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """Once all periods are Cancelled, their dates do not block publication."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", self.ANCHOR,
        )
        first = await _preview_candidate(client, auth_token, branch_id)
        opened = await _create_candidate(client, auth_token, branch_id, first)
        cancelled = await client.patch(
            f"/payroll/periods/{opened['payroll_period_id']}/status",
            json={"status": "Cancelled"}, headers=_hdr(auth_token),
        )
        assert cancelled.status_code == 200, cancelled.text
        await self._seed_period(
            direct_db, branch_id, "Cancelled", "2092-01-13", "2092-01-19",
        )
        version_id = await _publish_candidate_version(
            test_database_url, branch_id, "Week", "2092-01-13",
        )
        assert version_id > 0
        statuses = (await direct_db.execute(_text("""
            SELECT status FROM payroll.payrollperiods WHERE branchid = :bid
        """), {"bid": branch_id})).scalars().all()
        assert sorted(statuses) == ["Cancelled", "Cancelled"]

    async def test_setup_change_does_not_delete_locked_periods(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """
        Adjacent publication does not delete or rewrite a Locked period.
        """
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2093-02-01",
        )
        period_id = await self._seed_period(
            direct_db, branch_id, "Locked", "2093-02-01", "2093-02-07",
        )
        version_id = await _publish_candidate_version(
            test_database_url, branch_id, "Week", "2093-02-08",
        )
        assert version_id > 0
        chk = await client.get(
            f"/payroll/periods/{period_id}",
            headers=_hdr(auth_token),
        )
        assert chk.status_code == 200, chk.text
        assert chk.json()["payroll_period_id"] == period_id
        assert chk.json()["status"] == "Locked"

    async def test_setup_blocked_by_locked_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """
        Locked history rejects a publication at its valid weekly boundary.
        """
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2093-06-01",
        )
        period_id = await self._seed_period(
            direct_db, branch_id, "Locked", "2093-06-08", "2093-06-14",
        )
        with pytest.raises(PolicyError) as error:
            await _publish_candidate_version(
                test_database_url, branch_id, "Week", "2093-06-08",
            )
        assert error.value.code == "PERIOD_HISTORY_CONFLICT"
        status = (await direct_db.execute(_text("""
            SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid
        """), {"pid": period_id})).scalar_one()
        assert status == "Locked"

    async def test_cancelled_period_does_not_block_setup_change(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
        direct_db,
    ):
        """
        Cancelled history at a weekly boundary does not block publication.
        """
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2091-01-01",
        )
        period_id = await self._seed_period(
            direct_db, branch_id, "Cancelled", "2091-01-08", "2091-01-14",
        )
        version_id = await _publish_candidate_version(
            test_database_url, branch_id, "Week", "2091-01-08",
        )
        assert version_id > 0
        status = (await direct_db.execute(_text("""
            SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid
        """), {"pid": period_id})).scalar_one()
        assert status == "Cancelled"


# ===========================================================================
# H. Entry count
# ===========================================================================

class TestPeriodEntryCount:
    """Rule M: entry-count endpoint returns correct counts."""

    async def test_entry_count_zero_for_new_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """A period with no draft lines must report entry_count=0, has_data=False."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2094-01-02",
        )
        selected = await _preview_candidate(client, auth_token, branch_id)
        created = await _create_candidate(client, auth_token, branch_id, selected)
        period_id = created["payroll_period_id"]
        resp = await client.get(
            f"/payroll/periods/{period_id}/entry-count",
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "period_id"    in body
        assert "driver_count" in body
        assert "entry_count"  in body
        assert "has_data"     in body
        assert body["period_id"] == period_id
        assert body["entry_count"] == 0
        assert body["driver_count"] == 0
        assert body["has_data"] is False
        assert body["has_data"] == (body["entry_count"] > 0)

    async def test_entry_count_shape_on_fresh_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """Create a fresh period; before any lines, counts are 0."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2094-06-02",
        )
        selected = await _preview_candidate(client, auth_token, branch_id)
        created = await _create_candidate(client, auth_token, branch_id, selected)
        pid = created["payroll_period_id"]

        resp = await client.get(
            f"/payroll/periods/{pid}/entry-count",
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["entry_count"]  == 0
        assert body["driver_count"] == 0
        assert body["has_data"]     is False

    async def test_entry_count_404_for_nonexistent_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/payroll/periods/999999/entry-count",
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 404

    async def test_entry_count_requires_auth(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2094-08-01",
        )
        selected = await _preview_candidate(client, auth_token, branch_id)
        created = await _create_candidate(client, auth_token, branch_id, selected)
        resp = await client.get(
            f"/payroll/periods/{created['payroll_period_id']}/entry-count"
        )
        assert resp.status_code == 401


# ===========================================================================
# I. Driver / ODA access denied
# ===========================================================================

class TestDriverOdaDenied:
    """Rule N: users with DRIVER companyrole must get 403 on period management endpoints."""

    async def test_driver_cannot_access_next_period_dates(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """DRIVER/ODA user gets 403 on current candidate preview."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2095-01-06",
        )
        driver_token = await _make_driver_user_token(client, auth_token, branch_id)

        resp = await client.get(
            f"/payroll/branches/{branch_id}/period-candidates",
            params={"mode": "OPEN_CREATION"},
            headers=_hdr(driver_token),
        )
        assert resp.status_code == 403, (
            f"Driver should be denied candidate preview, got {resp.status_code}"
        )

    async def test_driver_cannot_access_entry_count(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """DRIVER/ODA user gets 403 on GET /payroll/periods/{id}/entry-count."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2095-02-03",
        )
        selected = await _preview_candidate(client, auth_token, branch_id)
        created = await _create_candidate(client, auth_token, branch_id, selected)
        driver_token = await _make_driver_user_token(client, auth_token, branch_id)

        resp = await client.get(
            f"/payroll/periods/{created['payroll_period_id']}/entry-count",
            headers=_hdr(driver_token),
        )
        assert resp.status_code == 403, (
            f"Driver should be denied entry-count, got {resp.status_code}"
        )

    async def test_driver_cannot_create_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        """DRIVER/ODA user gets 403 on candidate confirmation."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2095-03-03",
        )
        selected = await _preview_candidate(client, auth_token, branch_id)
        driver_token = await _make_driver_user_token(client, auth_token, branch_id)

        resp = await client.post(
            f"/payroll/branches/{branch_id}/period-creations",
            json={"candidate_key": selected["candidate_key"]},
            headers=_hdr(driver_token),
        )
        assert resp.status_code == 403, (
            f"Driver should be denied period creation, got {resp.status_code}"
        )


# ===========================================================================
# J. Cross-branch access denial
# ===========================================================================

class TestCrossBranchDenied:
    """branch_user (scoped to HQ only) must be denied access to other branches."""

    async def test_branch_user_denied_next_period_dates_other_branch(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        test_database_url: str,
    ):
        """HQ-scoped branch_user cannot preview another branch's candidate."""
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2095-04-02",
        )
        resp = await client.get(
            f"/payroll/branches/{branch_id}/period-candidates",
            params={"mode": "OPEN_CREATION"},
            headers=_hdr(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_branch_user_denied_entry_count_for_other_branch_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        test_database_url: str,
    ):
        """
        Create a period on a branch outside branch_user's scope.
        branch_user must get 403 on entry-count for that period.
        """
        branch_id = await _fresh_candidate_branch(
            test_database_url, "Week", "2095-06-02",
        )
        selected = await _preview_candidate(client, auth_token, branch_id)
        created = await _create_candidate(client, auth_token, branch_id, selected)
        pid = created["payroll_period_id"]

        resp = await client.get(
            f"/payroll/periods/{pid}/entry-count",
            headers=_hdr(branch_user_token),
        )
        assert resp.status_code == 403
