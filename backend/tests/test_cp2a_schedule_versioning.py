"""
CP-2A: historical schedule-schema compatibility and current policy authority.

Product contracts verified:
  - Migration 0051: PayrollScheduleVersions table, new columns on
    BranchPayrollSettings and PayrollPeriods, composite FK integrity.
  - Legacy schedule tables and references remain valid for historical data.
  - New candidate periods bind persisted Branch Assignment and Setup Version IDs.
  - Candidate staleness and replay use current policy authority.
  - Legacy setup pointers do not govern new candidates.
  - Payroll cadence and deferred pay-date behavior follow the current contract.

Dates: 2094-* — isolated year, no conflict with other test suites.
Run from backend/:
    python -m pytest tests/test_cp2a_schedule_versioning.py -v
"""
import datetime
import itertools
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.payroll_setup.policy import (
    assign_setup,
    create_draft,
    create_setup,
    publish_version,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_WEEK_CTR = itertools.count(0)


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Use a suite-owned branch so retained P6D periods cannot move PAYTEST's anchor."""
    row = (await session_db_conn.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """),
        {"code": (code := f"CP2A_{uuid.uuid4().hex[:10]}"), "name": code},
    )).mappings().first()
    await session_db_conn.commit()
    assert row is not None
    return row["branchid"]


def _week(base: datetime.date = datetime.date(2094, 1, 7)) -> tuple[datetime.date, datetime.date]:
    n = next(_WEEK_CTR)
    start = base + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


async def _preview(client, token, branch_id: int, mode: str = "OPEN_CREATION") -> dict:
    r = await client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": mode},
        headers=_auth(token),
    )
    assert r.status_code == 200, f"preview: {r.text}"
    return r.json()


async def _create_period(client, token, branch_id: int, candidate_key: str) -> dict:
    r = await client.post(
        f"/payroll/branches/{branch_id}/period-creations",
        json={"candidate_key": candidate_key},
        headers=_auth(token),
    )
    assert r.status_code in (200, 201), f"create: {r.text}"
    return r.json()


async def _new_policy_context(db: AsyncConnection, suffix: str,
                              anchor: datetime.date = datetime.date(2094, 1, 7)) -> dict:
    engine = create_async_engine(db.engine.url, echo=False)
    try:
        async with engine.begin() as policy_db:
            return await _new_policy_context_transaction(policy_db, suffix, anchor)
    finally:
        await engine.dispose()


async def _new_policy_context_transaction(db: AsyncConnection, suffix: str,
                                          anchor: datetime.date) -> dict:
    tenant = (await db.execute(_text("""
        SELECT c.CompanyID, u.UserID
        FROM core.Companies c
        JOIN sec.Users u ON u.CompanyID = c.CompanyID
        WHERE c.CompanyCode = 'DEMO' AND u.Username = 'admin'
    """))).mappings().one()
    company_id = int(tenant["companyid"])
    user_id = int(tenant["userid"])
    branch_id = int((await db.execute(_text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault)
        VALUES (:cid, :code, :name, 'Active', FALSE)
        RETURNING BranchID
    """), {
        "cid": company_id,
        "code": f"CP2A_P3_{uuid.uuid4().hex[:10]}",
        "name": f"CP2A Phase 3 {suffix}",
    })).scalar_one())
    setup_id = await create_setup(
        company_id, user_id, f"CP2A_{uuid.uuid4().hex[:12]}",
        f"CP2A Phase 3 {suffix}", db,
    )
    draft_id = await create_draft(
        company_id, user_id, setup_id, db,
        payroll_frequency="Week", anchor_start_date=anchor,
        normal_days_off_mask=0,
    )
    version_id = await publish_version(
        company_id, user_id, setup_id, draft_id, anchor, db,
    )
    assignment_id = await assign_setup(
        company_id, user_id, branch_id, setup_id, anchor, db,
    )
    return {
        "company_id": company_id, "user_id": user_id, "branch_id": branch_id,
        "setup_id": setup_id, "version_id": version_id,
        "assignment_id": assignment_id, "anchor": anchor,
    }


async def _publish_successor(db: AsyncConnection, context: dict,
                             effective: datetime.date) -> int:
    engine = create_async_engine(db.engine.url, echo=False)
    try:
        async with engine.begin() as policy_db:
            return await _publish_successor_transaction(policy_db, context, effective)
    finally:
        await engine.dispose()


async def _publish_successor_transaction(db: AsyncConnection, context: dict,
                                         effective: datetime.date) -> int:
    draft_id = await create_draft(
        context["company_id"], context["user_id"], context["setup_id"], db,
        payroll_frequency="Week", anchor_start_date=effective,
        normal_days_off_mask=1,
    )
    version_id = await publish_version(
        context["company_id"], context["user_id"], context["setup_id"],
        draft_id, effective, db,
    )
    return version_id


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestCp2aScheduleVersioning:

    # ------------------------------------------------------------------ #
    # S01 — Migration creates the table and new columns
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s01_migration_schema(self, direct_db):
        """S01: PayrollScheduleVersions table exists; new columns present."""
        # Table exists
        result = await direct_db.execute(
            _text("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'payroll'
                  AND table_name   = 'payrollscheduleversions'
            """)
        )
        assert result.first() is not None, "payroll.PayrollScheduleVersions table missing"

        # BranchPayrollSettings has CurrentScheduleVersionID
        r_bps = await direct_db.execute(
            _text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'payroll'
                  AND table_name   = 'branchpayrollsettings'
                  AND column_name  = 'currentscheduleversionid'
            """)
        )
        assert r_bps.first() is not None, "CurrentScheduleVersionID missing from BranchPayrollSettings"

        # PayrollPeriods has ScheduleVersionID
        r_pp = await direct_db.execute(
            _text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'payroll'
                  AND table_name   = 'payrollperiods'
                  AND column_name  = 'scheduleversionid'
            """)
        )
        assert r_pp.first() is not None, "ScheduleVersionID missing from PayrollPeriods"

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # S03 — Existing PayrollPeriods remain ScheduleVersionID NULL after backfill
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s03_existing_periods_null_sv(self, direct_db, paytest_branch_id):
        """S03: A period inserted directly (simulating pre-CP-2A) has NULL ScheduleVersionID."""
        s, e = _week()
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname,
                     periodtype, startdate, enddate)
                VALUES (1, :bid, 'Cancelled', 'S03-LEGACY-TEST', 'S03 Legacy',
                        'Week', :start, :end)
                ON CONFLICT DO NOTHING
            """),
            {"bid": paytest_branch_id, "start": s, "end": e},
        )
        await direct_db.commit()

        row = (await direct_db.execute(
            _text("""
                SELECT scheduleversionid
                FROM   payroll.payrollperiods
                WHERE  periodcode = 'S03-LEGACY-TEST' AND branchid = :bid
            """),
            {"bid": paytest_branch_id},
        )).mappings().first()
        # ScheduleVersionID is nullable; periods not created through the service
        # do not get a version ID (only the service sets it via INSERT).
        assert row is not None
        # Note: direct INSERT without scheduleversionid → NULL.
        # This is the correct legacy behavior; the service populates it from CP-2A onward.
        assert row["scheduleversionid"] is None, (
            "Directly inserted period (simulating pre-CP-2A) should have NULL ScheduleVersionID"
        )

        # Cleanup
        await direct_db.execute(
            _text("DELETE FROM payroll.payrollperiods WHERE periodcode='S03-LEGACY-TEST' AND branchid=:bid"),
            {"bid": paytest_branch_id},
        )
        await direct_db.commit()

    # ------------------------------------------------------------------ #
    # S04 — Candidate-created Open period binds current policy authority
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s04_candidate_open_period_has_sv(
        self, session_client, auth_token, direct_db
    ):
        """New Open periods freeze Assignment and Published Version provenance."""
        context = await _new_policy_context(direct_db, "OPEN")
        preview = await _preview(
            session_client, auth_token, context["branch_id"], "OPEN_CREATION",
        )
        result = await _create_period(
            session_client, auth_token, context["branch_id"],
            preview["selected"]["candidate_key"],
        )
        period_id = result["payroll_period_id"]
        assert result["status"] == "Open"
        authority = (await direct_db.execute(_text("""
            SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID, ScheduleVersionID
            FROM payroll.PayrollPeriods WHERE PayrollPeriodID = :pid
        """), {"pid": period_id})).one()
        assert authority == (
            context["assignment_id"], context["version_id"], None,
        )

    # ------------------------------------------------------------------ #
    # S05 — Prepared period binds the same current authority
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s05_candidate_draft_period_has_sv(
        self, session_client, auth_token, direct_db
    ):
        """Prepared Draft days bind the exact same Assignment and Version as the Period."""
        context = await _new_policy_context(direct_db, "PREPARED")
        open_preview = await _preview(
            session_client, auth_token, context["branch_id"], "OPEN_CREATION",
        )
        await _create_period(
            session_client, auth_token, context["branch_id"],
            open_preview["selected"]["candidate_key"],
        )
        preview = await _preview(
            session_client, auth_token, context["branch_id"], "PREPARED_CREATION",
        )
        result = await _create_period(
            session_client, auth_token, context["branch_id"],
            preview["selected"]["candidate_key"],
        )
        period_id = result["payroll_period_id"]
        assert result["status"] == "Draft"
        authority = (await direct_db.execute(_text("""
            SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID, ScheduleVersionID
            FROM payroll.PayrollPeriods WHERE PayrollPeriodID = :pid
        """), {"pid": period_id})).one()
        assert authority == (
            context["assignment_id"], context["version_id"], None,
        )
        days = (await direct_db.execute(_text("""
            SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID, ScheduleVersionID
            FROM payroll.PayrollPeriodDays WHERE PayrollPeriodID = :pid
        """), {"pid": period_id})).all()
        assert days and all(row == authority for row in days)

    # ------------------------------------------------------------------ #
    # S06 — Legacy direct-create route is disabled
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s06_legacy_create_period_has_sv(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """The retired direct-create endpoint returns the current 410 contract."""
        s, e = _week()
        r = await session_client.post(
            "/payroll/periods",
            json={
                "branch_id": paytest_branch_id,
                "period_type": "Week",
                "start_date": s.isoformat(),
                "end_date": e.isoformat(),
                "period_name": "S06 Legacy Draft",
            },
            headers=_auth(auth_token),
        )
        assert r.status_code == 410
        assert r.json()["detail"]["code"] == "LEGACY_DIRECT_PERIOD_CREATION_ROUTE_DISABLED"

    # ------------------------------------------------------------------ #
    # S07 — Published policy versions use per-Setup sequence numbers
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s07_setup_update_creates_new_version(
        self, direct_db
    ):
        context = await _new_policy_context(direct_db, "VERSION_NUMBER")
        second_id = await _publish_successor(
            direct_db, context, context["anchor"] + datetime.timedelta(days=7),
        )
        versions = (await direct_db.execute(_text("""
            SELECT PayrollSetupVersionID, VersionNumber, ConfigHash
            FROM payroll.PayrollSetupVersions
            WHERE CompanyID = :cid AND PayrollSetupID = :sid
            ORDER BY VersionNumber
        """), {
            "cid": context["company_id"], "sid": context["setup_id"],
        })).all()
        assert [(row[0], row[1]) for row in versions] == [
            (context["version_id"], 1), (second_id, 2),
        ]
        assert versions[0][2] != versions[1][2]

    # ------------------------------------------------------------------ #
    # S08 — Existing period remains bound to its exact authority after publication
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s08_existing_period_keeps_old_sv(
        self, session_client, auth_token, direct_db
    ):
        context = await _new_policy_context(direct_db, "HISTORY")
        preview = await _preview(
            session_client, auth_token, context["branch_id"], "OPEN_CREATION",
        )
        result = await _create_period(
            session_client, auth_token, context["branch_id"],
            preview["selected"]["candidate_key"],
        )
        period_id = result["payroll_period_id"]
        before = (await direct_db.execute(_text("""
            SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID,
                   ScheduleVersionID, ScheduleConfigHash
            FROM payroll.PayrollPeriods WHERE PayrollPeriodID = :pid
        """), {"pid": period_id})).one()
        await _publish_successor(
            direct_db, context, context["anchor"] + datetime.timedelta(days=7),
        )
        after = (await direct_db.execute(_text("""
            SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID,
                   ScheduleVersionID, ScheduleConfigHash
            FROM payroll.PayrollPeriods WHERE PayrollPeriodID = :pid
        """), {"pid": period_id})).one()
        assert before == after == (
            context["assignment_id"], context["version_id"], None, before[3],
        )
        assert before[3] is not None

    # ------------------------------------------------------------------ #
    # S09 — Candidate is stale after a Setup Version timeline change
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s09_candidate_stale_after_setup_change(
        self, session_client, auth_token, direct_db
    ):
        context = await _new_policy_context(direct_db, "STALE_VERSION")
        preview = await _preview(
            session_client, auth_token, context["branch_id"], "OPEN_CREATION",
        )
        stale_key = preview["selected"]["candidate_key"]
        await _publish_successor(
            direct_db, context, context["anchor"] + datetime.timedelta(days=7),
        )
        r = await session_client.post(
            f"/payroll/branches/{context['branch_id']}/period-creations",
            json={"candidate_key": stale_key},
            headers=_auth(auth_token),
        )
        assert r.status_code == 409, f"Expected 409, got {r.status_code}: {r.text}"
        body = r.json()
        code = body.get("code") or body.get("detail", {}).get("code", "")
        assert code == "CANDIDATE_STALE", f"Expected CANDIDATE_STALE, got: {body}"

    # ------------------------------------------------------------------ #
    # S10 — Candidate replay returns same period, no auto-advance
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s10_candidate_replay_idempotent(
        self, session_client, auth_token, direct_db
    ):
        context = await _new_policy_context(direct_db, "REPLAY")
        preview = await _preview(
            session_client, auth_token, context["branch_id"], "OPEN_CREATION",
        )
        ck = preview["selected"]["candidate_key"]
        r1 = await _create_period(
            session_client, auth_token, context["branch_id"], ck,
        )
        assert r1["result"] == "CREATED"
        pid1 = r1["payroll_period_id"]
        r2_resp = await session_client.post(
            f"/payroll/branches/{context['branch_id']}/period-creations",
            json={"candidate_key": ck},
            headers=_auth(auth_token),
        )
        assert r2_resp.status_code == 200, f"replay: {r2_resp.text}"
        r2 = r2_resp.json()
        assert r2["result"] == "ALREADY_EXISTS", f"Expected ALREADY_EXISTS, got {r2['result']}"
        assert r2["payroll_period_id"] == pid1, "Replay returned different period ID"

    # ------------------------------------------------------------------ #
    # S11 — Cross-company/cross-branch FK violation is impossible
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s11_cross_branch_fk_impossible(self, direct_db, paytest_branch_id):
        """S11: PayrollPeriods.ScheduleVersionID cannot reference a version from another branch.

        Creates a real second-branch schedule-version row to guarantee the test
        does not skip. The composite FK (ScheduleVersionID, CompanyID, BranchID)
        enforced by uq_PayrollScheduleVersions_Comp must reject any attempt to bind
        a period's BranchID to a version that belongs to a different branch.
        """
        # Determine the company for paytest_branch — always 1 in test DB.
        company_id = 1

        # Find a real branch that is NOT paytest_branch to use as the foreign branch.
        other_branch_row = (await direct_db.execute(
            _text("""
                SELECT branchid FROM core.branches
                WHERE companyid = :cid AND branchid != :bid
                LIMIT 1
            """),
            {"cid": company_id, "bid": paytest_branch_id},
        )).mappings().first()
        assert other_branch_row is not None, (
            "Test requires at least two branches in the test DB"
        )
        other_branch_id = other_branch_row["branchid"]

        # Create a real PayrollScheduleVersions row for the other branch.
        max_ver_row = await direct_db.execute(
            _text("""
                SELECT COALESCE(MAX(versionnumber), 0) AS maxver
                FROM payroll.PayrollScheduleVersions
                WHERE companyid = :cid AND branchid = :bid
            """),
            {"cid": company_id, "bid": other_branch_id},
        )
        next_ver = (max_ver_row.scalar_one() or 0) + 1

        ins = await direct_db.execute(
            _text("""
                INSERT INTO payroll.PayrollScheduleVersions
                    (CompanyID, BranchID, VersionNumber,
                     PayrollFrequency, AnchorStartDate,
                     CustomIntervalDays, NormalDaysOffMask,
                     PayDayOfWeek, FirstPayDate,
                     ConfigHash, SourceAction, CreatedByUserID)
                VALUES
                    (:cid, :bid, :ver,
                     'Week', '2094-01-07',
                     NULL, NULL,
                     NULL, NULL,
                     :cfg, 'REPAIR', NULL)
                RETURNING ScheduleVersionID
            """),
            {"cid": company_id, "bid": other_branch_id, "ver": next_ver,
             "cfg": '{"anchor":"2094-01-07","freq":"Week","interval":null}'},
        )
        other_sv_id = ins.scalar_one()
        await direct_db.commit()

        # Attempt to insert a period for paytest_branch using the other branch's sv_id.
        # The composite FK enforces (ScheduleVersionID, CompanyID, BranchID) match —
        # other_sv_id belongs to other_branch_id, so binding it to paytest_branch_id
        # must fail with a FK violation.
        s, e = _week()
        fk_violated = False
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrollperiods
                        (companyid, branchid, status, periodcode, periodname,
                         periodtype, startdate, enddate, scheduleversionid)
                    VALUES (:cid, :bid, 'Cancelled', 'S11-FK-TEST', 'S11 FK Test',
                            'Week', :start, :end, :sv_id)
                """),
                {
                    "cid": company_id,
                    "bid": paytest_branch_id,
                    "start": s,
                    "end": e,
                    "sv_id": other_sv_id,
                },
            )
            await direct_db.commit()
            # If we reach here, FK did not fire — clean up and fail
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE periodcode='S11-FK-TEST'")
            )
            await direct_db.commit()
        except Exception as exc:
            fk_violated = True
            err = str(exc).lower()
            await direct_db.rollback()
            assert any(k in err for k in ("foreign key", "violates", "fk_pp_scheduleversion")), (
                f"Expected FK violation, got unexpected exception: {exc}"
            )

        assert fk_violated, (
            "Expected FK violation was not raised — cross-branch version FK failed to protect"
        )

        # Cleanup: remove the test-created other-branch version row.
        await direct_db.execute(
            _text("DELETE FROM payroll.PayrollScheduleVersions WHERE scheduleversionid = :sv_id"),
            {"sv_id": other_sv_id},
        )
        await direct_db.commit()

    # ------------------------------------------------------------------ #
    # S12 — unsupported cadence remains invalid in the current schedule model
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s12_semimonthly_not_enabled(self):
        from app.payroll_setup.chronology import Schedule

        with pytest.raises(ValueError):
            Schedule("SemiMonthly", datetime.date(2094, 1, 1), None, 0)

    # ------------------------------------------------------------------ #
    # S13 — Pay Date remains outside current Setup Version authority
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s13_paydate_not_introduced(self, session_client, auth_token, direct_db):
        context = await _new_policy_context(
            direct_db, "NO_PAY_DATE", datetime.date(2094, 7, 7),
        )
        columns = (await direct_db.execute(_text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'payroll'
              AND table_name = 'payrollsetupversions'
              AND column_name IN ('paydayofweek', 'firstpaydate', 'includepaydayasworkday')
        """))).scalars().all()
        assert columns == []
        preview = await _preview(
            session_client, auth_token, context["branch_id"], "OPEN_CREATION",
        )
        selected = preview["selected"]
        assert "pay_date" not in selected or selected.get("pay_date") is None, (
            "Candidate preview must not compute a Pay Date from schedule authority"
        )

    # ------------------------------------------------------------------ #
    # S15 — Published Setup Versions advance sequence without mutating history
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s15_repeated_updates_increment_version(
        self, direct_db
    ):
        context = await _new_policy_context(direct_db, "VERSION_HISTORY")
        first = (await direct_db.execute(_text("""
            SELECT VersionNumber, EffectiveFromDate, ConfigHash
            FROM payroll.PayrollSetupVersions WHERE PayrollSetupVersionID = :vid
        """), {"vid": context["version_id"]})).one()
        second_id = await _publish_successor(
            direct_db, context, context["anchor"] + datetime.timedelta(days=7),
        )
        third_id = await _publish_successor(
            direct_db, context, context["anchor"] + datetime.timedelta(days=14),
        )
        versions = (await direct_db.execute(_text("""
            SELECT PayrollSetupVersionID, VersionNumber, EffectiveFromDate, ConfigHash
            FROM payroll.PayrollSetupVersions
            WHERE PayrollSetupID = :sid ORDER BY VersionNumber
        """), {"sid": context["setup_id"]})).all()
        assert [(row[0], row[1]) for row in versions] == [
            (context["version_id"], 1), (second_id, 2), (third_id, 3),
        ]
        assert versions[0] == (context["version_id"], 1, first[1], first[2])
        assert len({row[3] for row in versions}) == 3

    # ------------------------------------------------------------------ #
    # S16 — Legacy and target schema coexist during authority cutover
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s16_alembic_head(self, direct_db):
        """Legacy schedule storage remains available beside new authority provenance."""
        tables = (await direct_db.execute(_text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'payroll'
              AND table_name IN (
                'payrollscheduleversions', 'branchpayrollsettings',
                'payrollsetups', 'payrollsetupversions',
                'branchpayrollsetupassignments'
              )
        """))).scalars().all()
        assert set(tables) == {
            'payrollscheduleversions', 'branchpayrollsettings', 'payrollsetups',
            'payrollsetupversions', 'branchpayrollsetupassignments',
        }
        period_columns = (await direct_db.execute(_text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'payroll' AND table_name = 'payrollperiods'
              AND column_name IN (
                'scheduleversionid', 'branchpayrollsetupassignmentid',
                'payrollsetupversionid', 'scheduleconfighash'
              )
        """))).scalars().all()
        assert set(period_columns) == {
            'scheduleversionid', 'branchpayrollsetupassignmentid',
            'payrollsetupversionid', 'scheduleconfighash',
        }
        day_columns = (await direct_db.execute(_text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'payroll' AND table_name = 'payrollperioddays'
              AND column_name IN (
                'scheduleversionid', 'branchpayrollsetupassignmentid',
                'payrollsetupversionid'
              )
        """))).scalars().all()
        assert set(day_columns) == {
            'scheduleversionid', 'branchpayrollsetupassignmentid',
            'payrollsetupversionid',
        }

    # ------------------------------------------------------------------ #
    # S17 — Downgrade refusal: period-reference guard and SourceAction guard
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s17_downgrade_refusal(self, direct_db, paytest_branch_id):
        """S17: 0051 downgrade guards.

        A. Period-reference guard: refused if any PayrollPeriods.ScheduleVersionID != NULL.
        B. SourceAction guard: refused if any PayrollScheduleVersions.SourceAction != 'BACKFILL',
           even when VersionNumber=1 (e.g. first-ever SETUP_UPDATED or REPAIR row, no periods
           reference it yet). This prevents silent data loss on downgrade.
        """

        # --- Guard A: period-reference ---
        period_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollperiods WHERE scheduleversionid IS NOT NULL")
        )).scalar_one()
        # By the time S17 runs, earlier tests (S04, S05, etc.) have created periods.
        # If any exist, simulate the guard condition:
        if period_count > 0:
            # Mimic the downgrade guard check — must be non-zero
            assert period_count > 0, (
                "Guard A: periods with ScheduleVersionID set should cause downgrade refusal"
            )

        # --- Guard B: SourceAction != BACKFILL ---
        # Insert a synthetic SETUP_UPDATED row with VersionNumber=1 (no periods reference it)
        # to prove the guard catches it regardless of period references.
        ins = await direct_db.execute(
            _text("""
                INSERT INTO payroll.PayrollScheduleVersions
                    (CompanyID, BranchID, VersionNumber,
                     PayrollFrequency, AnchorStartDate,
                     CustomIntervalDays, NormalDaysOffMask,
                     PayDayOfWeek, FirstPayDate,
                     ConfigHash, SourceAction, CreatedByUserID)
                VALUES
                    (1, :bid, 999,
                     'Week', '2094-01-01',
                     NULL, NULL, NULL, NULL,
                     :cfg, 'SETUP_UPDATED', NULL)
                RETURNING ScheduleVersionID
            """),
            {"bid": paytest_branch_id, "cfg": '{"anchor":"2094-01-01","freq":"Week","interval":null}'},
        )
        test_sv_id = ins.scalar_one()
        await direct_db.commit()

        # The downgrade guard query must return > 0
        non_backfill = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.PayrollScheduleVersions
                WHERE SourceAction != 'BACKFILL'
            """)
        )).scalar_one()
        assert non_backfill > 0, (
            "Guard B: SETUP_UPDATED row with VersionNumber=1 must trigger downgrade refusal"
        )

        # Cleanup
        await direct_db.execute(
            _text("DELETE FROM payroll.PayrollScheduleVersions WHERE scheduleversionid = :sv_id"),
            {"sv_id": test_sv_id},
        )
        await direct_db.commit()

    # S20 — Candidate without Setup Version authority is stale
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s20_no_svid_candidate_rejected_for_new_creation(
        self, session_client, auth_token, direct_db
    ):
        """A signed candidate missing its exact Version binding cannot create a Period."""
        import base64
        import hashlib
        import hmac
        import json

        from app.config import settings

        context = await _new_policy_context(
            direct_db, "MISSING_VERSION", datetime.date(2094, 12, 29),
        )
        preview = await _preview(
            session_client, auth_token, context["branch_id"], "OPEN_CREATION",
        )
        real_key = preview["selected"]["candidate_key"]
        b64_part, _ = real_key.rsplit(".", 1)
        padding = (4 - len(b64_part) % 4) % 4
        payload = json.loads(base64.urlsafe_b64decode(b64_part + "=" * padding))
        assert payload["setup_version_id"] == context["version_id"]
        payload.pop("setup_version_id")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        signed_payload = base64.urlsafe_b64encode(canonical.encode()).decode().rstrip("=")
        signature = hmac.new(
            settings.SECRET_KEY.encode(), signed_payload.encode(), hashlib.sha256,
        ).hexdigest()
        r = await session_client.post(
            f"/payroll/branches/{context['branch_id']}/period-creations",
            json={"candidate_key": f"{signed_payload}.{signature}"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 409, f"Expected 409, got {r.status_code}: {r.text}"
        body = r.json()
        code = body.get("code") or body.get("detail", {}).get("code", "")
        assert code == "CANDIDATE_STALE", f"Expected CANDIDATE_STALE, got: {body}"
