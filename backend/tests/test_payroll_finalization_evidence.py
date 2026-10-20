"""Finalization and its preview project the bound, verified snapshot into FinalLines.

One FinalLine per official component, summing to the frozen ExpectedPay. The preview and the
finalization share one loader, one verification and one projection, and neither reads a live
rate, schedule, definition, rule or Bonus. Real tenants and routes throughout.
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

import pytest
from sqlalchemy import event, text

from app.payroll import finalization
from tests.calculation_snapshot_helpers import (
    allow_self_approval,
    bound_snapshot_id,
    decide,
    finalization_preview,
    finalize,
    mutate_live_sources,
    pending_review_item,
    period_evidence_counts,
    period_status,
    rows,
    snapshot_ids,
    standard_scenario,
    submit,
    submit_and_approve,
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
from tests.db_state import TriggerIdentity, suspended_test_triggers
from tests.payroll_runtime_fixtures import build_payroll_tenant

pytestmark = pytest.mark.asyncio
D = Decimal

COMPONENT_LINE_TYPES = {
    "ORDINARY": "ORDINARY", "STATUS": "STATUS_PAY", "BONUS": "BONUS",
    "MINIMUM_ADJUSTMENT": "SYS_MIN_TOPUP", "MAXIMUM_ADJUSTMENT": "SYS_MAX_CAP",
}


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _approved(client, engine, tenant):
    scenario = await standard_scenario(client, engine, tenant)
    item_id = await submit_and_approve(client, tenant, scenario.period)
    return scenario, item_id


async def _ledger(client, tenant, period) -> list[dict]:
    response = await client.get(
        f"/payroll/periods/{period['payroll_period_id']}/final-lines", headers=tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

async def test_finalization_writes_one_final_line_per_official_component(
    p3c_client, p3c_engine, tenant,
):
    scenario, item_id = await _approved(p3c_client, p3c_engine, tenant)
    period = scenario.period
    bound = bound_snapshot_id(tenant, item_id)

    result = (await finalize(p3c_client, tenant, period)).json()

    assert result["status"] == "Locked" and result["final_lines"] == 3
    assert D(result["final_gross"]) == D("42.00")
    components = rows(tenant, """
        SELECT payrollcalculationsnapshotcomponentid, componentkind, officialamount
        FROM payroll.payrollcalculationsnapshotcomponents
        WHERE payrollcalculationsnapshotid = %s ORDER BY payrollcalculationsnapshotcomponentid
    """, bound)
    final_lines = rows(tenant, """
        SELECT payrollcalculationsnapshotid, payrollcalculationsnapshotcomponentid, linetype,
               linescope, sourcetype, sourceid, finalamount, workdate, draftlineid, bonuseventid,
               payitemid, ratetypeid, driverrateid, ratebehavior
        FROM payroll.payrollfinallines WHERE payrollperiodid = %s
        ORDER BY payrollcalculationsnapshotcomponentid
    """, period["payroll_period_id"])
    assert [(c[0], c[2]) for c in components] == [(f[1], f[6]) for f in final_lines]
    for component, line in zip(components, final_lines, strict=True):
        assert line[0] == bound
        assert line[2] == COMPONENT_LINE_TYPES[component[1]]
        assert (line[3], line[4], line[5]) == ("Period", "SnapshotComponent", str(component[0]))
        assert all(value is None for value in line[7:])     # no legacy per-source provenance
    assert sorted(c[1] for c in components) == ["BONUS", "MINIMUM_ADJUSTMENT", "ORDINARY"]
    assert sum(line[6] for line in final_lines) == D("42.00")


async def test_final_lines_carry_the_frozen_snapshot_provenance(p3c_client, p3c_engine, tenant):
    scenario, item_id = await _approved(p3c_client, p3c_engine, tenant)
    period = scenario.period
    bound = bound_snapshot_id(tenant, item_id)
    await finalize(p3c_client, tenant, period)

    ledger = await _ledger(p3c_client, tenant, period)

    ((snapshot_hash_value,),) = rows(tenant, "SELECT snapshothash FROM "
                                    "payroll.payrollcalculationsnapshots WHERE "
                                    "payrollcalculationsnapshotid = %s", bound)
    assert [line["component_kind"] for line in ledger] == [
        "ORDINARY", "BONUS", "MINIMUM_ADJUSTMENT"]
    assert [D(line["final_amount"]) for line in ledger] == [D("0.67"), D("2.00"), D("39.33")]
    for line in ledger:
        provenance = line["source_snapshot"]
        assert line["payroll_calculation_snapshot_id"] == bound
        assert provenance["payroll_calculation_snapshot_id"] == bound
        assert provenance["snapshot_hash"] == snapshot_hash_value
        assert provenance["revision_number"] == 1
        assert provenance["snapshot_component_id"] == line["payroll_calculation_snapshot_component_id"]
        assert line["source_type"] == "SnapshotComponent" and line["line_scope"] == "Period"
        assert line["employee_id"] and line["driver_id"] and line["driver_number"]
    ordinary = ledger[0]
    assert ordinary["component_label"] == "Haul" and ordinary["pay_definition_id"]
    assert ledger[0]["source_snapshot"]["source_ids"] and ledger[1]["source_snapshot"]["source_ids"]
    # Min/Max adjustments carry the rule that produced them.
    assert ledger[2]["source_snapshot"]["driver_pay_rule_id"] == scenario.rule["driver_pay_rule_id"]
    assert ledger[2]["pay_definition_id"] is None


async def test_the_preview_shows_exactly_what_finalization_writes(p3c_client, p3c_engine, tenant):
    scenario, _ = await _approved(p3c_client, p3c_engine, tenant)
    period = scenario.period

    preview = (await finalization_preview(p3c_client, tenant, period)).json()
    assert preview["can_finalize"] is True and preview["blockers"] == []
    assert period_evidence_counts(tenant, period)["final_lines"] == 0   # a preview writes nothing
    assert period_status(tenant, period) == "Approved"
    await finalize(p3c_client, tenant, period)
    ledger = await _ledger(p3c_client, tenant, period)

    assert preview["final_line_count_estimate"] == len(ledger) == len(preview["lines"]) == 3
    assert [(line["component_kind"], line["line_type"], D(line["final_amount"]))
            for line in preview["lines"]] == [
        (line["component_kind"], line["line_type"], D(line["final_amount"])) for line in ledger]
    assert D(preview["total_final_gross"]) == sum(D(line["final_amount"]) for line in ledger)
    (driver_total,) = preview["driver_totals"]
    assert D(driver_total["final_pay"]) == D("42.00")
    assert (D(driver_total["gross_pay"]), D(driver_total["sys_adjustment"]),
            D(driver_total["bonus_total"])) == (D("0.67"), D("39.33"), D("2.00"))
    (adjustment,) = preview["sys_adjustments"]
    assert adjustment["adjustment_type"] == "SYS_MIN_TOPUP"
    assert D(adjustment["gross_before"]) == D("0.67") and D(adjustment["adjustment_amount"]) == D("39.33")
    assert [line["label"] for line in preview["lines"]][0] == "Haul"


# ---------------------------------------------------------------------------
# Frozen money wins
# ---------------------------------------------------------------------------

async def test_finalization_and_preview_ignore_every_live_source_changed_after_submission(
    p3c_client, p3c_engine, tenant,
):
    scenario, _ = await _approved(p3c_client, p3c_engine, tenant)
    period = scenario.period
    before = (await finalization_preview(p3c_client, tenant, period)).json()
    before.pop("period_status", None)

    await mutate_live_sources(p3c_engine, tenant, period)

    after = (await finalization_preview(p3c_client, tenant, period)).json()
    assert after == before | {"period_status": after["period_status"]}
    await finalize(p3c_client, tenant, period)
    ledger = await _ledger(p3c_client, tenant, period)
    assert [D(line["final_amount"]) for line in ledger] == [D("0.67"), D("2.00"), D("39.33")]
    assert ledger[0]["component_label"] == "Haul"
    assert ledger[0]["driver_name"] == "P3b Driver"
    assert rows(tenant, "SELECT sum(finalamount) FROM payroll.payrollfinallines "
                        "WHERE payrollperiodid = %s", period["payroll_period_id"]) == [(D("42.00"),)]


async def test_a_voided_rule_does_not_change_the_frozen_minimum_adjustment(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    voided = await p3c_client.post(
        f"/payroll/driver-pay-rules/{scenario.rule['driver_pay_rule_id']}/void",
        json={"reason": "no longer applies"}, headers=tenant.admin)
    assert voided.status_code in (200, 204), voided.text
    await decide(p3c_client, tenant, await pending_review_item(tenant, period))

    await finalize(p3c_client, tenant, period)

    ledger = await _ledger(p3c_client, tenant, period)
    assert [D(line["final_amount"]) for line in ledger] == [D("0.67"), D("2.00"), D("39.33")]
    rates = (await p3c_client.get(
        f"/payroll/finalized/{period['payroll_period_id']}/rates-used", headers=tenant.admin)).json()
    (rule_evidence,) = [row for row in rates["used_rate_definitions"]
                        if row["evidence_kind"] == "DriverPayRule"]
    assert rule_evidence["rule_type"] == "MinimumPay" and D(rule_evidence["rule_amount"]) == D("70")
    assert rule_evidence["rule_status"] == "Active"        # the status the snapshot froze


# ---------------------------------------------------------------------------
# Refusals and atomicity
# ---------------------------------------------------------------------------

async def test_a_tampered_snapshot_blocks_finalization_and_preview_and_writes_nothing(
    p3c_client, p3c_engine, tenant,
):
    scenario, item_id = await _approved(p3c_client, p3c_engine, tenant)
    period = scenario.period
    bound = bound_snapshot_id(tenant, item_id)
    async with p3c_engine.begin() as conn:
        async with suspended_test_triggers(conn, [TriggerIdentity(
                "payroll", "payrollcalculationsnapshotcomponents",
                "trg_payrollcalculationsnapshotcomponents_immutable")]):
            await conn.execute(text(
                "UPDATE payroll.payrollcalculationsnapshotcomponents SET officialamount = "
                "officialamount + 1 WHERE payrollcalculationsnapshotid = :s AND "
                "componentkind = 'BONUS'"), {"s": bound})

    for refused in (await finalization_preview(p3c_client, tenant, period, expect=422),
                    await finalize(p3c_client, tenant, period, expect=422)):
        assert refused.json()["detail"].startswith("SNAPSHOT_HASH_MISMATCH")
    assert period_status(tenant, period) == "Approved"
    assert period_evidence_counts(tenant, period)["final_lines"] == 0


async def test_finalization_rolls_back_every_write_when_the_audit_entry_fails(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    scenario, _ = await _approved(p3c_client, p3c_engine, tenant)
    period = scenario.period

    async def failing_audit(*_args, **_kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(finalization, "_write_finalization_audit", failing_audit)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        await finalize(p3c_client, tenant, period)

    assert period_status(tenant, period) == "Approved"
    assert period_evidence_counts(tenant, period)["final_lines"] == 0
    monkeypatch.undo()
    await finalize(p3c_client, tenant, period)                 # and it is retryable
    assert period_status(tenant, period) == "Locked"


async def test_a_finalized_period_cannot_be_finalized_again(p3c_client, p3c_engine, tenant):
    scenario, _ = await _approved(p3c_client, p3c_engine, tenant)
    period = scenario.period
    await finalize(p3c_client, tenant, period)

    again = await finalize(p3c_client, tenant, period, expect=422)

    assert "Only Approved periods" in again.json()["detail"]
    assert period_evidence_counts(tenant, period)["final_lines"] == 3


async def test_concurrent_finalizations_have_exactly_one_winner(p3c_client, p3c_engine, tenant):
    scenario, _ = await _approved(p3c_client, p3c_engine, tenant)
    period = scenario.period
    url = f"/payroll/periods/{period['payroll_period_id']}/finalize"

    responses = await asyncio.gather(
        *(p3c_client.post(url, headers=tenant.admin) for _ in range(3)))

    statuses = sorted(response.status_code for response in responses)
    assert statuses[0] == 200 and all(code in (409, 422) for code in statuses[1:])
    assert period_evidence_counts(tenant, period)["final_lines"] == 3
    assert period_status(tenant, period) == "Locked"


# ---------------------------------------------------------------------------
# No live compensation authority after submission
# ---------------------------------------------------------------------------

_LIVE_COMPENSATION = re.compile(
    r"\bpayroll\.(driverrateschedulevalues|driverrateschedules|ratedefinitions|"
    r"ratecomponentdefinitions|paydefinitions|driverpayrules|bonusevents|payrolldraftlines|"
    r"payrollperioddefinitions|statusentries)\b", re.IGNORECASE)
_RETIRED_PAY_ITEM_MODEL = re.compile(r"payroll\.(payitems|payitemratetypemap)", re.IGNORECASE)
_LEGACY_COMPENSATION = re.compile(
    r"\bpayroll\.(payitems|payitemratetypemap|ratetypes|driverrates|driverratetiers)\b",
    re.IGNORECASE)


def _record_statements(engine) -> list[str]:
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, *_rest):
        statements.append(statement)

    return statements


async def test_preview_and_finalization_read_only_the_frozen_evidence(
    p3c_client, p3c_engine, tenant,
):
    scenario, _ = await _approved(p3c_client, p3c_engine, tenant)
    period = scenario.period
    statements = _record_statements(p3c_engine)

    await finalization_preview(p3c_client, tenant, period)
    await finalize(p3c_client, tenant, period)

    assert statements, "the statement recorder saw no SQL"
    live = [s for s in statements if _LIVE_COMPENSATION.search(s)]
    legacy = [s for s in statements if _LEGACY_COMPENSATION.search(s)]
    assert live == [] and legacy == []


async def test_the_whole_lifecycle_never_touches_the_retired_pay_item_model(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    statements = _record_statements(p3c_engine)

    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period))
    await finalize(p3c_client, tenant, period)
    for view in ("overview", "reports/drivers", "off-drivers", "rates-used", "audit"):
        response = await p3c_client.get(
            f"/payroll/finalized/{period['payroll_period_id']}/{view}", headers=tenant.admin)
        assert response.status_code == 200, (view, response.text)

    assert [s for s in statements if _RETIRED_PAY_ITEM_MODEL.search(s)] == []
    assert len(snapshot_ids(tenant, period)) == 1
