"""What is payroll content for Submit and Resubmit, and the lifecycle of a Status-only period.

The canonical day-entry selection is the Status authority; the legacy DailyStatus duplicate row is
never a lifecycle prerequisite. A period whose only content is a canonical Status submits and
resubmits whether or not the duplicate exists, a period with nothing in it is still refused, and
a Status that exists only as a legacy duplicate (no canonical selection) stays refused by the
canonicalization blocker. A note is never payroll content. A period whose Statuses are all
non-monetary has frozen Status evidence but no official component, and runs the whole lifecycle
to Locked without a fabricated earning, a live calculation or any FinalLine.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.calculation_snapshot_helpers import (
    allow_self_approval,
    day,
    decide,
    enter,
    finalization_preview,
    finalize,
    open_period,
    pending_review_item,
    period_status,
    rows,
    snapshot_ids,
    status_pay_setup,
    submit,
)
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.db_state import replica_replication_role
from tests.payroll_runtime_fixtures import build_payroll_tenant, seed_status_key

pytestmark = pytest.mark.asyncio
D = Decimal


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _status_only_period(client, engine, tenant) -> dict:
    """An Open period whose only content is one paid Status day (1 hour x 25)."""
    status_pay_setup(tenant, "25")
    period, _, _ = await open_period(client, engine, tenant)
    await enter(client, tenant, period, 0, status_key="PAID")
    return period


async def _void_legacy_duplicate(engine, period) -> None:
    """Remove the legacy DailyStatus duplicate the Day Grid wrote beside the selection."""
    async with engine.begin() as conn:
        async with replica_replication_role(conn):
            result = await conn.execute(text("""
                UPDATE payroll.payrolldraftlines SET status = 'Void'
                WHERE payrollperiodid = :pid AND linetype = 'DailyStatus' AND status <> 'Void'
            """), {"pid": period["payroll_period_id"]})
            assert result.rowcount == 1


def _live_content(tenant, period) -> tuple[int, int]:
    """(non-void draft lines, non-void canonical Status selections)."""
    ((lines,),) = rows(tenant, """
        SELECT count(*) FROM payroll.payrolldraftlines
        WHERE payrollperiodid = %s AND status <> 'Void'
    """, period["payroll_period_id"])
    ((selections,),) = rows(tenant, """
        SELECT count(*) FROM payroll.payrollperioddriverdayentrystate
        WHERE payrollperiodid = %s AND NOT isvoided AND statuskeyid IS NOT NULL
    """, period["payroll_period_id"])
    return lines, selections


async def _resubmit(client, tenant, period, *, expect: int = 200):
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/resubmissions", json={},
        headers=tenant.admin)
    assert response.status_code == expect, response.text
    return response


async def test_a_status_only_period_submits_without_a_legacy_duplicate(
    p3c_client, p3c_engine, tenant,
):
    period = await _status_only_period(p3c_client, p3c_engine, tenant)
    await _void_legacy_duplicate(p3c_engine, period)
    assert _live_content(tenant, period) == (0, 1)       # no source line at all, one selection
    allow_self_approval(tenant)

    await submit(p3c_client, tenant, period)

    assert period_status(tenant, period) == "InReview"
    (snapshot_id,) = snapshot_ids(tenant, period)
    assert rows(tenant, """
        SELECT c.officialamount FROM payroll.payrollcalculationsnapshotcomponents c
        WHERE c.payrollcalculationsnapshotid = %s AND c.componentkind = 'STATUS'
    """, snapshot_id) == [(D("25.00"),)]


async def test_a_status_only_period_resubmits_without_a_legacy_duplicate(
    p3c_client, p3c_engine, tenant,
):
    period = await _status_only_period(p3c_client, p3c_engine, tenant)
    await _void_legacy_duplicate(p3c_engine, period)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period), "EditRequested")
    assert period_status(tenant, period) == "Returned"
    assert _live_content(tenant, period) == (0, 1)

    await _resubmit(p3c_client, tenant, period)

    assert period_status(tenant, period) == "InReview"
    assert len(snapshot_ids(tenant, period)) == 2


async def test_a_period_with_nothing_in_it_is_still_refused_at_submit(
    p3c_client, p3c_engine, tenant,
):
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    assert _live_content(tenant, period) == (0, 0)

    response = await submit(p3c_client, tenant, period, expect=422)

    assert "empty period" in response.text
    assert period_status(tenant, period) == "Open"
    assert snapshot_ids(tenant, period) == []


async def test_a_period_whose_only_status_was_cleared_is_refused_at_resubmit(
    p3c_client, p3c_engine, tenant,
):
    period = await _status_only_period(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period), "EditRequested")
    await enter(p3c_client, tenant, period, 0, status_key="")       # the Status is cleared
    assert _live_content(tenant, period) == (0, 0)

    response = await _resubmit(p3c_client, tenant, period, expect=422)

    assert "empty period" in response.text
    assert period_status(tenant, period) == "Returned"
    assert len(snapshot_ids(tenant, period)) == 1


async def test_a_legacy_only_status_is_refused_by_the_canonicalization_blocker(
    p3c_client, p3c_engine, tenant,
):
    """A DailyStatus row with no canonical selection is not valid Status data: it makes the
    period non-empty but is refused by the intended blocker, not accepted as content."""
    status_pay_setup(tenant, "25")
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    async with p3c_engine.begin() as conn:
        async with replica_replication_role(conn):
            await conn.execute(text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverassignmentid, workdate,
                     linetype, quantity, sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                VALUES (:cid, :bid, :pid, :did, :dt, 'DailyStatus', 0, 'Manual', 'Active', FALSE,
                        'PAID', :uid)
            """), {"cid": tenant.company_id, "bid": tenant.branch_a,
                   "pid": period["payroll_period_id"], "did": tenant.driver_a,
                   "dt": date.fromisoformat(day(period, 0)), "uid": tenant.owner})
    assert _live_content(tenant, period) == (1, 0)

    response = await submit(p3c_client, tenant, period, expect=422)

    assert "LEGACY_STATUS_NOT_CANONICAL" in response.text
    assert "empty period" not in response.text
    assert period_status(tenant, period) == "Open"
    assert snapshot_ids(tenant, period) == []


# ---------------------------------------------------------------------------
# A note is not payroll content
# ---------------------------------------------------------------------------

async def _save_row(client, tenant, period, row, *, offset=0):
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": day(period, offset), "rows": [
            {"driver_assignment_id": tenant.driver_a, "values": {}, **row}]},
        headers=tenant.admin)
    assert response.status_code == 200, response.text


def _note_rows(tenant, period) -> int:
    """The compatibility DailyNote rows that hold the note beside the canonical entry."""
    return rows(tenant, """
        SELECT count(*) FROM payroll.payrolldraftlines
        WHERE payrollperiodid = %s AND linetype = 'DailyNote' AND status <> 'Void'
    """, period["payroll_period_id"])[0][0]


async def test_a_period_holding_only_a_note_cannot_be_submitted(p3c_client, p3c_engine, tenant):
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    await _save_row(p3c_client, tenant, period, {"notes": "only a note"})
    assert _note_rows(tenant, period) == 1          # the compatibility row exists and is non-void

    response = await submit(p3c_client, tenant, period, expect=422)

    assert "empty period" in response.text
    assert period_status(tenant, period) == "Open"
    assert snapshot_ids(tenant, period) == []


async def test_a_returned_period_whose_status_was_cleared_but_note_kept_cannot_be_resubmitted(
    p3c_client, p3c_engine, tenant,
):
    status_pay_setup(tenant, "25")
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    await _save_row(p3c_client, tenant, period, {"status_key": "PAID", "notes": "kept note"})
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period), "EditRequested")
    await _save_row(p3c_client, tenant, period, {"status_key": "", "notes": "kept note"})
    assert _live_content(tenant, period)[1] == 0     # no Status selection is left
    assert _note_rows(tenant, period) == 1           # the note is

    response = await _resubmit(p3c_client, tenant, period, expect=422)

    assert "empty period" in response.text
    assert period_status(tenant, period) == "Returned"
    assert len(snapshot_ids(tenant, period)) == 1


async def test_an_ordinary_pay_line_alone_is_payroll_content(p3c_client, p3c_engine, tenant):
    """Ordinary pay lines carry no line type: excluding the note row must not exclude them."""
    period, ppd, _ = await open_period(p3c_client, p3c_engine, tenant, rate="10")
    await enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="2")
    assert _live_content(tenant, period) == (1, 0)
    allow_self_approval(tenant)

    await submit(p3c_client, tenant, period)

    assert period_status(tenant, period) == "InReview"


async def test_a_note_beside_a_canonical_status_does_not_change_that_the_period_has_content(
    p3c_client, p3c_engine, tenant,
):
    status_pay_setup(tenant, "25")
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    await _save_row(p3c_client, tenant, period, {"status_key": "PAID", "notes": "with a note"})
    await _void_legacy_duplicate(p3c_engine, period)
    allow_self_approval(tenant)

    await submit(p3c_client, tenant, period)

    assert period_status(tenant, period) == "InReview"


# ---------------------------------------------------------------------------
# A period whose Statuses are all non-monetary: evidence without an official component
# ---------------------------------------------------------------------------

async def _approved_non_monetary_period(client, engine, tenant) -> tuple[dict, int]:
    """An Approved period whose only content is one non-monetary Status day (no rate column)."""
    seed_status_key(tenant, "UNPAID", off=True, hoursvalue=D("8"))
    period, _, _ = await open_period(client, engine, tenant)
    await enter(client, tenant, period, 0, status_key="UNPAID")
    await _void_legacy_duplicate(engine, period)
    allow_self_approval(tenant)
    await submit(client, tenant, period)
    await decide(client, tenant, await pending_review_item(tenant, period))
    (snapshot_id,) = snapshot_ids(tenant, period)
    return period, snapshot_id


async def test_a_non_monetary_status_only_period_runs_the_whole_lifecycle_to_locked(
    p3c_client, p3c_engine, tenant,
):
    period, snapshot_id = await _approved_non_monetary_period(p3c_client, p3c_engine, tenant)
    pid = period["payroll_period_id"]
    assert period_status(tenant, period) == "Approved"
    # The snapshot holds the frozen Status selection and no official component.
    assert rows(tenant, """
        SELECT (SELECT count(*) FROM payroll.payrollcalculationsnapshotcomponents
                WHERE payrollcalculationsnapshotid = %s),
               (SELECT count(*) FROM payroll.payrollcalculationdrivertotals
                WHERE payrollcalculationsnapshotid = %s),
               (SELECT count(*) FROM payroll.payrollcalculationsnapshotstatusentries
                WHERE payrollcalculationsnapshotid = %s)
    """, snapshot_id, snapshot_id, snapshot_id) == [(0, 0, 1)]

    preview = (await finalization_preview(p3c_client, tenant, period)).json()
    assert preview["lines"] == [] and preview["driver_totals"] == []
    assert D(str(preview["total_final_gross"])) == D("0")

    await finalize(p3c_client, tenant, period)

    assert period_status(tenant, period) == "Locked"
    # Nothing was projected, fabricated or recalculated: no FinalLine and no component exist.
    assert rows(tenant, "SELECT count(*) FROM payroll.payrollfinallines "
                        "WHERE payrollperiodid = %s", pid) == [(0,)]
    assert rows(tenant, """
        SELECT count(*) FROM payroll.payrollcalculationsnapshotcomponents
        WHERE payrollcalculationsnapshotid = %s
    """, snapshot_id) == [(0,)]
    assert rows(tenant, """
        SELECT count(*) FROM payroll.payrollperiodworkflowactionevidence
        WHERE payrollcalculationsnapshotid = %s AND actioncode = 'FINALIZED'
    """, snapshot_id) == [(1,)]


async def test_a_finalized_non_monetary_period_reads_coherently_from_its_approved_snapshot(
    p3c_client, p3c_engine, tenant,
):
    period, snapshot_id = await _approved_non_monetary_period(p3c_client, p3c_engine, tenant)
    pid = period["payroll_period_id"]
    await finalize(p3c_client, tenant, period)

    ledger = await p3c_client.get(f"/payroll/periods/{pid}/final-lines", headers=tenant.admin)
    assert ledger.status_code == 200, ledger.text
    assert ledger.json() == []

    overview = await p3c_client.get(f"/payroll/finalized/{pid}/overview", headers=tenant.admin)
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert D(str(body["financial_summary"]["total_pay"])) == D("0")
    assert body["financial_summary"]["final_line_count"] == 0
    assert body["snapshot_provenance"]["snapshot_id"] == snapshot_id

    detail = await p3c_client.get(f"/payroll/periods/{pid}", headers=tenant.admin)
    assert detail.status_code == 200 and detail.json()["status"] == "Locked"
    assert D(str(detail.json()["final_gross"])) == D("0")

    # The Status evidence is the frozen one: renaming the live Status changes nothing here.
    rows(tenant, "UPDATE payroll.payrollstatuskeys SET keyname = 'Renamed', "
                 "normalizedkeyname = 'renamed' WHERE statuscode = 'UNPAID' RETURNING 1")
    off = await p3c_client.get(f"/payroll/finalized/{pid}/off-drivers", headers=tenant.admin)
    assert off.status_code == 200, off.text
    (entry,) = off.json()["status_entries"]
    assert entry["status_label"] == "UNPAID" and entry["is_off_reason"] is True
    assert off.json()["metadata"]["section_availability"]["status_evidence"]["state"] == "AVAILABLE"

    for suffix in ("rates-used", "audit"):
        response = await p3c_client.get(f"/payroll/finalized/{pid}/{suffix}", headers=tenant.admin)
        assert response.status_code == 200, (suffix, response.text)


async def test_a_finalized_period_without_final_lines_needs_a_snapshot_that_projects_none(
    p3c_client, p3c_engine, tenant,
):
    """The absence of FinalLines is only accepted for a snapshot with no official component: a
    period whose FinalLines vanished from a snapshot that has components stays unavailable."""
    status_pay_setup(tenant, "25")
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    await enter(p3c_client, tenant, period, 0, status_key="PAID")
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period))
    await finalize(p3c_client, tenant, period)
    pid = period["payroll_period_id"]
    async with p3c_engine.begin() as conn:
        async with replica_replication_role(conn):
            await conn.execute(text("DELETE FROM payroll.payrollfinallines "
                                    "WHERE payrollperiodid = :pid"), {"pid": pid})

    ledger = await p3c_client.get(f"/payroll/periods/{pid}/final-lines", headers=tenant.admin)

    assert ledger.status_code == 422, ledger.text
    assert "FINAL_LINES_EMPTY" in ledger.text


async def test_a_monetary_status_with_an_explicit_zero_rate_still_finalizes_with_its_component(
    p3c_client, p3c_engine, tenant,
):
    status_pay_setup(tenant, "0")
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    await enter(p3c_client, tenant, period, 0, status_key="PAID")
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period))

    await finalize(p3c_client, tenant, period)

    assert period_status(tenant, period) == "Locked"
    assert rows(tenant, """
        SELECT componentkind, finalamount FROM payroll.payrollfinallines
        WHERE payrollperiodid = %s
    """, period["payroll_period_id"]) == [("STATUS", D("0.00"))]


async def test_a_snapshot_with_neither_a_component_nor_status_evidence_cannot_be_finalized(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    """Defense in depth. No route can produce a verified snapshot that is empty of both money and
    Status evidence (submission refuses an empty period), so the verified content is stubbed:
    finalization still refuses it and leaves the period Approved with nothing projected."""
    from app.payroll import finalization

    period, _ = await _approved_non_monetary_period(p3c_client, p3c_engine, tenant)

    async def empty_snapshot(*args, **kwargs):
        return {"ReportEvidence": {"StatusEntries": [], "BonusEvents": []}}

    monkeypatch.setattr(finalization, "verify_snapshot", empty_snapshot)
    monkeypatch.setattr(finalization, "project_final_lines", lambda content: [])

    response = await finalize(p3c_client, tenant, period, expect=422)

    assert "neither an official component nor Status evidence" in response.text
    assert period_status(tenant, period) == "Approved"
    assert rows(tenant, "SELECT count(*) FROM payroll.payrollfinallines "
                        "WHERE payrollperiodid = %s", period["payroll_period_id"]) == [(0,)]
