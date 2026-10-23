"""Immutable evidence of an OrdinalTier calculation.

The snapshot freezes enough to reproduce the result without any live compensation state: the
tier topology, the values actually used, the expanded per-tier explanation, the schedule and
definition identity, the frozen method and version, the currency and the provenance. It is
covered by the one financial hash and the one verification: tampered or internally
inconsistent tier evidence fails closed, and finalization reads only the snapshot.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import text

from app.payroll.snapshot_hash import calculate_snapshot_hash, used_rate_fingerprint
from app.payroll.snapshot_verification import (
    SnapshotEvidenceError,
    load_snapshot_content,
    verify_snapshot,
)
from tests.calculation_snapshot_helpers import (
    add_bonus,
    allow_self_approval,
    capture,
    day,
    decide,
    enter,
    finalization_preview,
    finalize,
    mutate_live_sources,
    open_period,
    open_tier_period,
    pending_review_item,
    period_evidence_counts,
    period_status,
    rows,
    snapshot_ids,
    snapshot_table_counts,
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
    approve_tier_rate,
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    period_definitions,
    rated_definition,
    rated_ordinal_definition,
)

pytestmark = pytest.mark.asyncio
D = Decimal

CANONICAL_EXPLANATION = [
    {"SequenceNo": 1, "OrdinalFrom": 1, "OrdinalTo": 1, "Units": 1, "RateAmount": "3", "Amount": "3"},
    {"SequenceNo": 2, "OrdinalFrom": 2, "OrdinalTo": 2, "Units": 1, "RateAmount": "4", "Amount": "4"},
    {"SequenceNo": 3, "OrdinalFrom": 3, "OrdinalTo": None, "Units": 2, "RateAmount": "5",
     "Amount": "10"},
]


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _tier_scenario(client, engine, tenant, *, quantity="4"):
    period, ppd, definition = await open_tier_period(client, engine, tenant)
    await enter(client, tenant, period, 1, ppd=ppd, quantity=quantity)
    return period, ppd, definition


async def _content(engine, snapshot_id: int) -> dict:
    async with engine.connect() as conn:
        return await load_snapshot_content(conn, snapshot_id=snapshot_id)


async def _verify(engine, snapshot_id: int) -> dict:
    async with engine.connect() as conn:
        return await verify_snapshot(conn, snapshot_id=snapshot_id)


def _ordinary_line(content: dict) -> dict:
    (total,) = content["DriverTotals"]
    (line,) = [ln for ln in total["Lines"] if ln["SourceType"] == "DraftLine"]
    return line


def _tier_rows(tenant, snapshot_id: int) -> list[tuple]:
    return rows(tenant, """
        SELECT sequenceno, ordinalfrom, ordinalto, amountsnapshot
        FROM payroll.payrollcalculationsnapshotusedratecomponents
        WHERE payrollcalculationsnapshotid = %s ORDER BY payrollcalculationsnapshotusedratedefinitionid, sequenceno
    """, snapshot_id)


# ---------------------------------------------------------------------------
# What is frozen
# ---------------------------------------------------------------------------

async def test_the_snapshot_freezes_topology_values_explanation_and_identity(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, definition = await _tier_scenario(p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)
    content = await _content(p3c_engine, frozen.snapshot_id)

    assert content["CalculationVersion"] == "payroll-calculation-v4"
    assert (content["CurrencyCode"], content["CurrencyMinorUnitDigits"]) == ("USD", 2)
    (total,) = content["DriverTotals"]
    (component,) = [c for c in total["Components"] if c["ComponentKind"] == "ORDINARY"]
    assert (component["CalculationMethod"], component["CalculationMethodVersion"]) == ("OrdinalTier", 1)
    assert component["PayrollPeriodDefinitionID"] == ppd
    assert component["PayDefinitionID"] == definition["pay_definition_id"]
    assert (component["PreRoundingAmount"], component["OfficialAmount"]) == (D("17"), D("17.00"))

    line = _ordinary_line(content)
    assert (line["Quantity"], line["CalculatedAmount"]) == (D("4"), D("17"))
    assert line["ResolvedRateAmount"] is None
    assert line["CalculationVersion"] == "ordinal-tier-v1" and line["CalculationStatus"] == "Calculated"
    assert line["RateDefinitionID"] == definition["rate_definition_id"]
    assert line["SourceEvidenceJSONB"] == {"TierContributions": CANONICAL_EXPLANATION}

    (used,) = content["UsedRates"]
    assert used["RateShape"] == "OrdinalTierSchedule"
    assert (used["RateComponentDefinitionID"], used["RateAmount"]) == (None, None)
    assert used["DriverRateScheduleID"] == line["DriverRateScheduleID"]
    assert (used["PayDefinitionID"], used["StatusRateColumnID"]) == (
        definition["pay_definition_id"], None)
    assert (used["RateStatus"], used["EffectiveTo"]) == ("Approved", None)
    assert (used["CurrencyCodeSnapshot"], used["CurrencyMinorUnitDigitsSnapshot"]) == ("USD", 2)
    assert used["DriverID"] == tenant.driver_a_id
    assert [(c["SequenceNo"], c["OrdinalFrom"], c["OrdinalTo"], c["Amount"])
            for c in used["Components"]] == [
        (1, 1, 1, D("3")), (2, 2, 2, D("4")), (3, 3, None, D("5"))]
    assert [c["RateComponentDefinitionID"] for c in used["Components"]] == [
        c["rate_component_definition_id"] for c in definition["components"]]
    assert snapshot_table_counts(tenant, frozen.snapshot_id)["used_rates"] == 1
    assert _tier_rows(tenant, frozen.snapshot_id) == [
        (1, 1, 1, D("3")), (2, 2, 2, D("4")), (3, 3, None, D("5"))]


async def test_the_frozen_evidence_verifies_and_hashes_the_tier_values(
    p3c_client, p3c_engine, tenant,
):
    period, _, _ = await _tier_scenario(p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)
    content = await _verify(p3c_engine, frozen.snapshot_id)
    assert content["SnapshotHash"] == calculate_snapshot_hash(content)
    # The hash covers the tier values: two schedules that differ in one value hash apart.
    (used,) = content["UsedRates"]
    changed = {**content, "UsedRates": [{
        **used, "Components": [*used["Components"][:2],
                               {**used["Components"][2], "Amount": D("6")}]}]}
    assert calculate_snapshot_hash(changed) != content["SnapshotHash"]
    assert used_rate_fingerprint(used) == used["DefinitionFingerprint"]


async def test_one_schedule_used_by_several_lines_is_frozen_once(p3c_client, p3c_engine, tenant):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="4")
    await enter(p3c_client, tenant, period, 2, ppd=ppd, quantity="2")
    frozen = await capture(p3c_engine, tenant, period)
    content = await _verify(p3c_engine, frozen.snapshot_id)
    assert len(content["UsedRates"]) == 1
    (total,) = content["DriverTotals"]
    lines = [ln for ln in total["Lines"] if ln["SourceType"] == "DraftLine"]
    assert sorted(ln["CalculatedAmount"] for ln in lines) == [D("7"), D("17")]
    assert {ln["UsedRateFingerprint"] for ln in lines} == {content["UsedRates"][0]["DefinitionFingerprint"]}
    counts = snapshot_table_counts(tenant, frozen.snapshot_id)
    assert (counts["used_rates"], counts["lines"]) == (1, 2)


async def test_each_schedule_a_period_used_freezes_its_own_tier_values(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, definition = await open_tier_period(p3c_client, p3c_engine, tenant)
    await approve_tier_rate(
        p3c_client, tenant, definition, ("6", "7", "8"), effective_from=day(period, 3))
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="4")
    await enter(p3c_client, tenant, period, 4, ppd=ppd, quantity="4")
    frozen = await capture(p3c_engine, tenant, period)
    content = await _verify(p3c_engine, frozen.snapshot_id)

    assert len(content["UsedRates"]) == 2
    by_schedule = {u["DriverRateScheduleID"]: u for u in content["UsedRates"]}
    (total,) = content["DriverTotals"]
    for line in (ln for ln in total["Lines"] if ln["SourceType"] == "DraftLine"):
        used = by_schedule[line["DriverRateScheduleID"]]
        assert line["UsedRateFingerprint"] == used["DefinitionFingerprint"]
        expected = {D("17"): (3, 4, 5), D("29"): (6, 7, 8)}[line["CalculatedAmount"]]
        assert tuple(int(c["Amount"]) for c in used["Components"]) == expected
    assert snapshot_table_counts(tenant, frozen.snapshot_id)["used_rates"] == 2
    assert len(_tier_rows(tenant, frozen.snapshot_id)) == 6


async def test_per_unit_and_tier_lines_freeze_side_by_side_with_their_own_shapes(
    p3c_client, p3c_engine, tenant,
):
    tiered = await rated_ordinal_definition(p3c_client, tenant, definition_name="Beta route")
    plain = await rated_definition(p3c_client, tenant, rate="2", definition_name="Alpha item")
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    ppd = {d["pay_definition_id"]: d["payroll_period_definition_id"]
           for d in period_definitions(period["payroll_period_id"], tenant)}
    response = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": day(period, 1), "rows": [{
            "driver_assignment_id": tenant.driver_a,
            "values": {str(ppd[tiered["pay_definition_id"]]): "4",
                       str(ppd[plain["pay_definition_id"]]): "10"}}]},
        headers=tenant.admin)
    assert response.status_code == 200, response.text
    await add_bonus(p3c_client, tenant, period, "2.00")

    frozen = await capture(p3c_engine, tenant, period)
    content = await _verify(p3c_engine, frozen.snapshot_id)
    assert sorted((u["RateShape"], u["RateAmount"]) for u in content["UsedRates"]) == [
        ("OrdinalTierSchedule", None), ("Scalar", D("2"))]
    (total,) = content["DriverTotals"]
    by_method = {c["CalculationMethod"]: c for c in total["Components"]
                 if c["ComponentKind"] == "ORDINARY"}
    assert (by_method["OrdinalTier"]["OfficialAmount"], by_method["PerUnit"]["OfficialAmount"]) == (
        D("17.00"), D("20.00"))
    assert total["ExpectedPay"] == D("39.00")
    lines = {ln["CalculationVersion"]: ln for ln in total["Lines"] if ln["SourceType"] == "DraftLine"}
    assert lines["cp4a-per-unit-v1"]["ResolvedRateAmount"] == D("2")
    assert lines["ordinal-tier-v1"]["ResolvedRateAmount"] is None


# ---------------------------------------------------------------------------
# Tampering fails closed: the frozen hash
# ---------------------------------------------------------------------------

_TIER = "payroll.payrollcalculationsnapshotusedratecomponents"
_ORDINARY_OF_SNAPSHOT = (
    "sourcetype = 'DraftLine' AND payrollcalculationdrivertotalid IN "
    "(SELECT payrollcalculationdrivertotalid FROM payroll.payrollcalculationdrivertotals "
    "WHERE payrollcalculationsnapshotid = :snapshot)")

_HASH_TAMPERING = {
    "a tier value": f"UPDATE {_TIER} SET amountsnapshot = amountsnapshot + 1 "
                    "WHERE payrollcalculationsnapshotid = :snapshot AND sequenceno = 3",
    "a tier bound": f"UPDATE {_TIER} SET ordinalto = 2 "
                    "WHERE payrollcalculationsnapshotid = :snapshot AND sequenceno = 1",
    "a removed tier": f"DELETE FROM {_TIER} "
                      "WHERE payrollcalculationsnapshotid = :snapshot AND sequenceno = 3",
}


def _line_update(set_clause: str) -> str:
    """Change one field of the ordinary lines of the snapshot under test."""
    return f"UPDATE payroll.payrollcalculationsnapshotlines SET {set_clause} WHERE {_ORDINARY_OF_SNAPSHOT}"


_HASH_TAMPERING["the line amount"] = _line_update("calculatedamount = calculatedamount + 1")
_HASH_TAMPERING["the line quantity"] = _line_update("quantity = quantity + 1")
_HASH_TAMPERING["the line explanation"] = _line_update(
    "sourceevidencejsonb = '{\"TierContributions\": []}'::jsonb")
_HASH_TAMPERING["the line version"] = _line_update("calculationversion = 'cp4a-per-unit-v1'")
_HASH_TAMPERING["the frozen method"] = (
    "UPDATE payroll.payrollcalculationsnapshotcomponents SET calculationmethod = 'PerUnit' "
    "WHERE payrollcalculationsnapshotid = :snapshot AND componentkind = 'ORDINARY'")
_HASH_TAMPERING["the schedule window"] = (
    "UPDATE payroll.payrollcalculationsnapshotusedratedefinitions "
    "SET effectivefromsnapshot = effectivefromsnapshot - 30 "
    "WHERE payrollcalculationsnapshotid = :snapshot")


async def _tamper(engine, statement: str, **params) -> None:
    async with engine.begin() as conn:
        async with replica_replication_role(conn):
            result = await conn.execute(text(statement), params)
            assert result.rowcount >= 1, statement


@pytest.mark.parametrize("family", sorted(_HASH_TAMPERING))
async def test_tampered_tier_evidence_fails_the_frozen_hash(
    p3c_client, p3c_engine, tenant, family,
):
    period, _, _ = await _tier_scenario(p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)
    await _verify(p3c_engine, frozen.snapshot_id)
    await _tamper(p3c_engine, _HASH_TAMPERING[family], snapshot=frozen.snapshot_id)
    with pytest.raises(SnapshotEvidenceError) as failure:
        await _verify(p3c_engine, frozen.snapshot_id)
    assert failure.value.code == "SNAPSHOT_HASH_MISMATCH", family


# ---------------------------------------------------------------------------
# Tampering that keeps the hash consistent still fails the semantic verification
# ---------------------------------------------------------------------------

async def _rehash(engine, snapshot_id: int) -> None:
    """Make the stored fingerprints and snapshot hash agree with the (tampered) rows, so only
    the semantic verification of the evidence can still object."""
    async with engine.begin() as conn:
        content = await load_snapshot_content(conn, snapshot_id=snapshot_id)
        async with replica_replication_role(conn):
            for used in content["UsedRates"]:
                await conn.execute(
                    text("UPDATE payroll.payrollcalculationsnapshotusedratedefinitions "
                         "SET definitionfingerprint = :fp WHERE "
                         "payrollcalculationsnapshotusedratedefinitionid = :id"),
                    {"fp": used_rate_fingerprint(used),
                     "id": used["PayrollCalculationSnapshotUsedRateDefinitionID"]})
    async with engine.begin() as conn:
        content = await load_snapshot_content(conn, snapshot_id=snapshot_id)
        async with replica_replication_role(conn):
            await conn.execute(
                text("UPDATE payroll.payrollcalculationsnapshots SET snapshothash = :h "
                     "WHERE payrollcalculationsnapshotid = :snapshot"),
                {"h": calculate_snapshot_hash(content), "snapshot": snapshot_id})


_SEMANTIC_TAMPERING = {
    "a tier value no longer matches the amount": (
        f"UPDATE {_TIER} SET amountsnapshot = amountsnapshot + 1 "
        "WHERE payrollcalculationsnapshotid = :snapshot AND sequenceno = 3",
        "TIER_EVIDENCE_INTEGRITY_ERROR"),
    "the explanation no longer matches the tiers": (
        _line_update("sourceevidencejsonb = jsonb_set(sourceevidencejsonb, "
                     "'{TierContributions,0,Units}', '2')"),
        "TIER_EVIDENCE_INTEGRITY_ERROR"),
    "the explanation is dropped": (
        _line_update("sourceevidencejsonb = '{}'::jsonb"), "TIER_EVIDENCE_INTEGRITY_ERROR"),
    "the quantity no longer matches the amount": (
        _line_update("quantity = quantity + 1"), "TIER_EVIDENCE_INTEGRITY_ERROR"),
    "the line names another calculation version": (
        _line_update("calculationversion = 'cp4a-per-unit-v1'"), "TIER_EVIDENCE_INTEGRITY_ERROR"),
    "the line claims a single rate": (
        _line_update("resolvedrateamount = 5"), "USED_RATE_EVIDENCE_INTEGRITY_ERROR"),
    "the topology has a gap": (
        f"UPDATE {_TIER} SET ordinalfrom = 3, ordinalto = 3 "
        "WHERE payrollcalculationsnapshotid = :snapshot AND sequenceno = 2",
        "USED_RATE_EVIDENCE_INTEGRITY_ERROR"),
    "the topology is not closed by an open tier": (
        f"UPDATE {_TIER} SET ordinalto = 9 "
        "WHERE payrollcalculationsnapshotid = :snapshot AND sequenceno = 3",
        "USED_RATE_EVIDENCE_INTEGRITY_ERROR"),
    "a tier is removed": (
        f"DELETE FROM {_TIER} WHERE payrollcalculationsnapshotid = :snapshot AND sequenceno = 3",
        "USED_RATE_EVIDENCE_INTEGRITY_ERROR"),
}


@pytest.mark.parametrize("family", sorted(_SEMANTIC_TAMPERING))
async def test_internally_inconsistent_tier_evidence_fails_closed_even_with_a_matching_hash(
    p3c_client, p3c_engine, tenant, family,
):
    statement, code = _SEMANTIC_TAMPERING[family]
    period, _, _ = await _tier_scenario(p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)
    await _tamper(p3c_engine, statement, snapshot=frozen.snapshot_id)
    await _rehash(p3c_engine, frozen.snapshot_id)
    with pytest.raises(SnapshotEvidenceError) as failure:
        await _verify(p3c_engine, frozen.snapshot_id)
    assert failure.value.code == code, (family, failure.value)


# ---------------------------------------------------------------------------
# Database protection of the tier evidence
# ---------------------------------------------------------------------------

async def test_tier_evidence_rows_are_immutable(p3c_client, p3c_engine, tenant, cur):
    period, _, _ = await _tier_scenario(p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)
    for statement in (
        f"UPDATE {_TIER} SET amountsnapshot = 1 WHERE payrollcalculationsnapshotid = %s",
        f"DELETE FROM {_TIER} WHERE payrollcalculationsnapshotid = %s",
    ):
        with pytest.raises(psycopg2.errors.RestrictViolation, match="immutable"):
            cur.execute(statement, (frozen.snapshot_id,))
    assert len(_tier_rows(tenant, frozen.snapshot_id)) == 3


async def test_a_tier_used_rate_row_without_tier_values_cannot_be_written(
    p3c_client, p3c_engine, tenant, cur,
):
    period, _, _ = await _tier_scenario(p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)
    with pytest.raises(psycopg2.errors.CheckViolation, match="used_rate_tiers_missing"):
        cur.execute("""
            INSERT INTO payroll.payrollcalculationsnapshotusedratedefinitions
                (payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
                 driverassignmentid, driverid, definitionfingerprint, evidencekind,
                 sourcetypesnapshot, driverratescheduleid, ratedefinitionid, paydefinitionid,
                 rateshapesnapshot, effectivefromsnapshot, ratestatussnapshot,
                 currencycodesnapshot, currencyminorunitdigitssnapshot)
            SELECT payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
                   driverassignmentid, driverid, repeat('c', 64), evidencekind,
                   sourcetypesnapshot, driverratescheduleid, ratedefinitionid, paydefinitionid,
                   rateshapesnapshot, effectivefromsnapshot, ratestatussnapshot,
                   currencycodesnapshot, currencyminorunitdigitssnapshot
            FROM payroll.payrollcalculationsnapshotusedratedefinitions
            WHERE payrollcalculationsnapshotid = %s
        """, (frozen.snapshot_id,))
    assert snapshot_table_counts(tenant, frozen.snapshot_id)["used_rates"] == 1


async def test_tier_values_belong_only_to_a_tier_shaped_used_rate_row(
    p3c_client, p3c_engine, tenant, cur,
):
    period, ppd, _ = await open_period(
        p3c_client, p3c_engine, tenant, rate="2", definition_name="Plain item")
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="3")
    frozen = await capture(p3c_engine, tenant, period)
    with pytest.raises(psycopg2.errors.CheckViolation, match="used_rate_component_shape"):
        cur.execute("""
            INSERT INTO payroll.payrollcalculationsnapshotusedratecomponents
                (payrollcalculationsnapshotusedratedefinitionid, payrollcalculationsnapshotid,
                 ratedefinitionid, sequenceno, ratecomponentdefinitionid, ordinalfrom, amountsnapshot)
            SELECT payrollcalculationsnapshotusedratedefinitionid, payrollcalculationsnapshotid,
                   ratedefinitionid, 1, ratecomponentdefinitionid, 1, 1
            FROM payroll.payrollcalculationsnapshotusedratedefinitions
            WHERE payrollcalculationsnapshotid = %s
        """, (frozen.snapshot_id,))


# ---------------------------------------------------------------------------
# Lifecycle: submit, approve, finalize read only the snapshot
# ---------------------------------------------------------------------------

async def test_approval_fails_closed_when_the_bound_tier_evidence_no_longer_verifies(
    p3c_client, p3c_engine, tenant,
):
    period, _, _ = await _tier_scenario(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    (snapshot_id,) = snapshot_ids(tenant, period)
    item_id = await pending_review_item(tenant, period)
    await _tamper(
        p3c_engine, _HASH_TAMPERING["a tier value"], snapshot=snapshot_id)
    refused = await decide(p3c_client, tenant, item_id, expect=422)
    assert refused.json()["detail"].startswith("SNAPSHOT_HASH_MISMATCH")
    assert period_status(tenant, period) == "InReview"


async def test_finalized_tier_payroll_is_the_frozen_snapshot_whatever_changes_afterwards(
    p3c_client, p3c_engine, tenant,
):
    period, _, definition = await _tier_scenario(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    (snapshot_id,) = snapshot_ids(tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period))
    before = (await finalization_preview(p3c_client, tenant, period)).json()
    assert D(str(before["total_final_gross"])) == D("17.00")

    # Live schedule values, the definition label and every other live source change, and the
    # finalization neither re-resolves compensation nor re-derives anything.
    await mutate_live_sources(p3c_engine, tenant, period)
    after = (await finalization_preview(p3c_client, tenant, period)).json()
    assert after == before
    await finalize(p3c_client, tenant, period)

    pid = period["payroll_period_id"]
    assert (await p3c_client.get(f"/payroll/periods/{pid}/final-lines", headers=tenant.admin)).status_code == 404
    assert rows(tenant, "SELECT sum(finalamount) FROM payroll.payrollfinallines "
                        "WHERE payrollperiodid = %s", pid) == [(D("17.00"),)]
    content = await _verify(p3c_engine, snapshot_id)
    assert _ordinary_line(content)["SourceEvidenceJSONB"] == {
        "TierContributions": CANONICAL_EXPLANATION}

    rates = (await p3c_client.get(
        f"/payroll/finalized/{pid}/rates-used", headers=tenant.admin)).json()
    (used,) = [r for r in rates["used_rate_definitions"] if r["evidence_kind"] == "DriverRateSchedule"]
    assert used["rate_shape"] == "OrdinalTierSchedule" and used["rate_amount"] is None
    assert [(t["sequence_no"], t["ordinal_from"], t["ordinal_to"], D(t["amount"]))
            for t in used["rate_tiers"]] == [
        (1, 1, 1, D("3")), (2, 2, 2, D("4")), (3, 3, None, D("5"))]
    assert used["pay_definition_id"] == definition["pay_definition_id"]


async def test_submission_finalization_and_the_frozen_readers_never_resolve_compensation_again(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, _, _ = await _tier_scenario(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period))

    def resolution_is_forbidden(*args, **kwargs):
        raise AssertionError("a frozen action resolved live compensation")

    for target in ("app.compensation.resolver.resolve_many",
                   "app.compensation.resolver.resolve",
                   "app.payroll.live_source_calculation.resolve_many",
                   "app.payroll.status_calculation.resolve_many"):
        monkeypatch.setattr(target, resolution_is_forbidden)

    await finalization_preview(p3c_client, tenant, period)
    await finalize(p3c_client, tenant, period)
    pid = period["payroll_period_id"]
    for path in (f"/payroll/finalized/{pid}/overview",
                 f"/payroll/finalized/{pid}/rates-used", f"/payroll/finalized/{pid}/reports/drivers",
                 f"/payroll/periods/{pid}/reports/period-pay"):
        response = await p3c_client.get(path, headers=tenant.admin)
        assert response.status_code == 200, (path, response.text[:200])


async def test_resubmitting_unchanged_tier_source_freezes_an_identical_financial_hash(
    p3c_client, p3c_engine, tenant,
):
    period, _, _ = await _tier_scenario(p3c_client, p3c_engine, tenant)
    first = await capture(p3c_engine, tenant, period)
    second = await capture(p3c_engine, tenant, period)
    first_hash = rows(tenant, "SELECT snapshothash FROM payroll.payrollcalculationsnapshots "
                              "WHERE payrollcalculationsnapshotid = %s", first.snapshot_id)
    second_hash = rows(tenant, "SELECT snapshothash FROM payroll.payrollcalculationsnapshots "
                               "WHERE payrollcalculationsnapshotid = %s", second.snapshot_id)
    assert first_hash == second_hash
    assert second.snapshot_id != first.snapshot_id


async def test_a_blocked_tier_period_freezes_nothing(p3c_client, p3c_engine, tenant):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant, amounts=None)
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="4")
    allow_self_approval(tenant)
    before = period_evidence_counts(tenant, period)
    await submit(p3c_client, tenant, period, expect=422)
    assert period_evidence_counts(tenant, period) == before


async def test_tier_compensation_cannot_change_inside_a_finalized_period(
    p3c_client, p3c_engine, tenant,
):
    from tests.compensation_api_fixtures import approve_schedule, create_pending, set_tier_values

    period, _, definition = await _tier_scenario(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period))
    await finalize(p3c_client, tenant, period)

    pending = await create_pending(
        p3c_client, tenant, definition, effective_from=day(period, 0))
    assert (await set_tier_values(
        p3c_client, tenant, pending, definition, ("9", "9", "9"))).status_code == 200
    refused = await approve_schedule(p3c_client, tenant, pending)
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD"

    (current,) = [row for row in rows(
        tenant, "SELECT driverratescheduleid FROM payroll.driverrateschedules "
                "WHERE ratedefinitionid = %s AND status = 'Approved'", definition["rate_definition_id"])]
    voided = await p3c_client.post(
        f"/compensation/driver-rate-schedules/{current[0]}/void", headers=tenant.admin,
        json={"reason": "too late"})
    assert voided.status_code == 422
    assert voided.json()["detail"]["code"] == "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD"
