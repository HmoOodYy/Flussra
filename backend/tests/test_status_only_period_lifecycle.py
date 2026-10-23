"""What is payroll content for Submit and Resubmit, and the lifecycle of a Status-only period.

The canonical day-entry selection is the Status authority. A period whose only content is a
canonical Status submits and resubmits, a period with nothing in it is still refused, and a
historical pseudo-line without a canonical selection is ignored as period content. A note is never
payroll content. A period whose Statuses are all
non-monetary has frozen Status evidence but no official component, and runs the whole lifecycle
to Locked without a fabricated earning, a live calculation or any FinalLine.
"""
from __future__ import annotations

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
from tests.payroll_runtime_fixtures import (
    add_driver,
    build_payroll_tenant,
    seed_status_key,
)

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


async def _assert_no_status_pseudo_lines(engine, period) -> None:
    """Day Grid writes canonical entry state without duplicating Status as a DraftLine."""
    async with engine.connect() as conn:
        count = await conn.scalar(text("""
            SELECT count(*) FROM payroll.payrolldraftlines
            WHERE payrollperiodid = :pid AND status <> 'Void'
        """), {"pid": period["payroll_period_id"]})
    assert count == 0


def _live_content(tenant, period) -> tuple[int, int]:
    """(non-void draft lines, non-void canonical Status selections)."""
    ((lines,),) = rows(tenant, """
        SELECT count(*) FROM payroll.payrolldraftlines
        WHERE payrollperiodid = %s AND status <> 'Void'
          AND payrollperioddefinitionid IS NOT NULL
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
    await _assert_no_status_pseudo_lines(p3c_engine, period)
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
    await _assert_no_status_pseudo_lines(p3c_engine, period)
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


async def test_status_selections_are_not_duplicated_as_source_lines(p3c_client, p3c_engine, tenant):
    status_pay_setup(tenant, "25")
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    await _save_row(p3c_client, tenant, period, {"status_key": "PAID"})
    await _assert_no_status_pseudo_lines(p3c_engine, period)
    assert _live_content(tenant, period) == (0, 1)


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
    """No note pseudo-lines are stored in ordinary quantity rows."""
    return rows(tenant, """
        SELECT count(*) FROM payroll.payrolldraftlines
        WHERE payrollperiodid = %s AND status <> 'Void'
    """, period["payroll_period_id"])[0][0]


async def test_a_period_holding_only_a_note_cannot_be_submitted(p3c_client, p3c_engine, tenant):
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    await _save_row(p3c_client, tenant, period, {"notes": "only a note"})
    assert _note_rows(tenant, period) == 0
    assert rows(tenant, """
        SELECT notetext FROM payroll.payrollperioddriverdayentrystate
        WHERE payrollperiodid = %s AND NOT isvoided
    """, period["payroll_period_id"]) == [("only a note",)]

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
    assert _note_rows(tenant, period) == 0

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
    await _assert_no_status_pseudo_lines(p3c_engine, period)
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
    await _assert_no_status_pseudo_lines(engine, period)
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

    raw_lines = await p3c_client.get(f"/payroll/periods/{pid}/final-lines", headers=tenant.admin)
    assert raw_lines.status_code == 404

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

    overview = await p3c_client.get(f"/payroll/finalized/{pid}/overview", headers=tenant.admin)
    assert overview.status_code == 200, overview.text
    assert overview.json()["financial_summary"]["total_pay"] is None
    unavailable = overview.json()["section_availability"]["financials"]
    assert unavailable["state"] == "UNAVAILABLE"
    assert unavailable["reason_code"] == "FINAL_LINES_EMPTY"


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


# ---------------------------------------------------------------------------
# Frozen reports list a Driver that only non-monetary Status evidence represents
# ---------------------------------------------------------------------------

REPORT_VIEWS = ("drivers", "period-work", "period-pay", "mixed")


async def _get_ok(client, tenant, path: str) -> dict:
    response = await client.get(path, headers=tenant.admin)
    assert response.status_code == 200, (path, response.text)
    return response.json()


def _roster_identity(tenant, period, assignment_id: int) -> tuple:
    """The frozen number and name of a Driver on the period's eligibility roster."""
    return rows(tenant, """
        SELECT drivernumbersnapshot, drivernamesnapshot
        FROM payroll.payrollperioddrivereligibility
        WHERE payrollperiodid = %s AND driverassignmentid = %s
    """, period["payroll_period_id"], assignment_id)[0]


def _assert_evidence_only_driver(body: dict, tenant, period) -> None:
    (driver,) = body["drivers"]
    number, name = _roster_identity(tenant, period, tenant.driver_a)
    assert driver["driver_assignment_id"] == tenant.driver_a
    assert (driver["driver_number"], driver["driver_name"]) == (number, name)
    (entry,) = driver["work"]["status_entries"]
    assert (entry["code"], entry["label"], entry["is_off"]) == ("UNPAID", "UNPAID", True)
    assert D(str(entry["hours_value"])) == D("8") and entry["category"] == "Other"
    assert driver["work"]["daily_rows"] == []
    assert driver["pay"] is None and driver["bonus_events"] == []
    # The financial sections stay empty: nothing was fabricated for the Driver.
    assert all(D(str(value)) == 0 for value in body["pay_totals"].values())
    assert body["work_totals"] == []


async def test_approved_period_reports_list_a_driver_represented_only_by_status_evidence(
    p3c_client, p3c_engine, tenant,
):
    period, snapshot_id = await _approved_non_monetary_period(p3c_client, p3c_engine, tenant)
    pid = period["payroll_period_id"]

    for view in REPORT_VIEWS:
        body = await _get_ok(p3c_client, tenant, f"/payroll/periods/{pid}/reports/{view}")
        assert body["metadata"]["snapshot_id"] == snapshot_id
        assert body["metadata"]["report_evidence_available"] is True
        _assert_evidence_only_driver(body, tenant, period)

    # No total, component or FinalLine was created for the Driver.
    assert rows(tenant, """
        SELECT (SELECT count(*) FROM payroll.payrollcalculationdrivertotals
                WHERE payrollcalculationsnapshotid = %s),
               (SELECT count(*) FROM payroll.payrollcalculationsnapshotcomponents
                WHERE payrollcalculationsnapshotid = %s),
               (SELECT count(*) FROM payroll.payrollfinallines WHERE payrollperiodid = %s)
    """, snapshot_id, snapshot_id, pid) == [(0, 0, 0)]


async def test_finalized_reports_list_the_driver_from_the_frozen_evidence_only(
    p3c_client, p3c_engine, tenant,
):
    period, snapshot_id = await _approved_non_monetary_period(p3c_client, p3c_engine, tenant)
    pid = period["payroll_period_id"]
    await finalize(p3c_client, tenant, period)
    # Live changes after finalization must not reach the report: the Status is renamed and the
    # Driver's roster identity in the live Employee record changes.
    rows(tenant, "UPDATE payroll.payrollstatuskeys SET keyname = 'Renamed', "
                 "normalizedkeyname = 'renamed' WHERE statuscode = 'UNPAID' RETURNING 1")
    rows(tenant, """
        UPDATE core.employees SET fullname = 'Renamed After Finalization'
        WHERE employeeid = (SELECT employeeid FROM core.driverassignments
                            WHERE driverassignmentid = %s) RETURNING 1
    """, tenant.driver_a)

    for view in REPORT_VIEWS:
        body = await _get_ok(p3c_client, tenant, f"/payroll/finalized/{pid}/reports/{view}")
        assert body["metadata"]["snapshot_id"] == snapshot_id
        assert body["metadata"]["section_availability"]["report_evidence"]["state"] == "AVAILABLE"
        _assert_evidence_only_driver(body, tenant, period)


async def test_a_monetary_driver_and_an_evidence_only_driver_share_one_frozen_report(
    p3c_client, p3c_engine, tenant,
):
    """The roster grows by the evidence-only Driver only: the monetary Driver keeps its pay, and
    the evidence-only Driver has no pay section."""
    other = add_driver(tenant)
    status_pay_setup(tenant, "25")
    seed_status_key(tenant, "UNPAID", off=True, hoursvalue=D("8"))
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    await enter(p3c_client, tenant, period, 0, status_key="PAID")
    await enter(p3c_client, tenant, period, 0, status_key="UNPAID", driver=other)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period))
    pid = period["payroll_period_id"]

    for path in (f"/payroll/periods/{pid}/reports/mixed", f"/payroll/periods/{pid}/reports/period-work"):
        body = await _get_ok(p3c_client, tenant, path)
        by_id = {d["driver_assignment_id"]: d for d in body["drivers"]}
        assert set(by_id) == {tenant.driver_a, other}
        assert by_id[other]["pay"] is None
        assert by_id[other]["work"]["status_entries"][0]["code"] == "UNPAID"
        assert [e["code"] for e in by_id[tenant.driver_a]["work"]["status_entries"]] == ["PAID"]
    mixed = await _get_ok(p3c_client, tenant, f"/payroll/periods/{pid}/reports/mixed")
    monetary = next(d for d in mixed["drivers"] if d["driver_assignment_id"] == tenant.driver_a)
    assert D(str(monetary["pay"]["status_pay"])) == D("25.00")
    assert D(str(mixed["pay_totals"]["total_pay"])) == D("25.00")
