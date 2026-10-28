from __future__ import annotations

import asyncio
import json
import threading
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.imports.errors import ImportFailure
from app.imports.rules import bind_pending_rule
from tests.compensation_api_fixtures import (  # noqa: F401 - register isolated API fixtures
    create_definition,
    grant_applicability,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.db_state import TriggerIdentity, suspended_test_triggers
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    map_import_columns,
    open_period_with_grid,
    rated_definition,
)
from tests.schema_helpers import _alembic, _disposable_database

pytestmark = pytest.mark.asyncio

@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _eligible_driver(engine, tenant, period):
    async with engine.connect() as db:
        row = (await db.execute(text("""
            SELECT d.drivernumber, x.workdate
            FROM payroll.payrollperioddrivereligibility p
            JOIN core.driverassignments a ON a.driverassignmentid=p.driverassignmentid
              AND a.companyid=p.companyid AND a.branchid=p.branchid
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            CROSS JOIN LATERAL unnest(p.eligibleworkdates) AS x(workdate)
            WHERE p.companyid=:cid AND p.payrollperiodid=:pid AND p.iseligibleforperiod
            ORDER BY d.driverid, x.workdate LIMIT 1
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})).mappings().one()
        return dict(row)


async def _definition(engine, tenant, period):
    from app.imports.mapping import targets
    from app.payroll.period_definitions import list_period_definitions

    async with engine.connect() as db:
        definitions = await list_period_definitions(period["payroll_period_id"], tenant.company_id,
                                                    db, active_only=True)
    target = next(item for item in targets(definitions) if item["target"].startswith("Definition:"))
    period_definition_id = int(target["target"].split(":", 1)[1])
    definition = next(item for item in definitions
                      if item.payroll_period_definition_id == period_definition_id)
    return period_definition_id, definition.pay_definition_id, target["code"]


async def _upload(client, engine, tenant, period, filename="rules.csv"):
    driver = await _eligible_driver(engine, tenant, period)
    period_definition_id, pay_definition_id, code = await _definition(engine, tenant, period)
    data = (f"DriverNumber,WorkDate,{code}\n"
            f"{driver['drivernumber']},{driver['workdate'].isoformat()},1\n").encode()
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/imports",
        files={"file": (filename, data, "text/csv")},
        headers={**tenant.admin, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    return response.json(), period_definition_id, pay_definition_id


async def _preview(client, tenant, created, definition_id):
    import_id = created["import_id"]
    target = f"Definition:{definition_id}"
    prepared = next((item.get("rule_state", {}).get("pending_rule")
                     for item in created.get("mapping_targets", []) if item["target"] == target), None)
    if prepared is None:
        mapped = await map_import_columns(client, tenant, created, {
            "1": "DriverNumber", "2": "WorkDate", "3": target})
        assert mapped.status_code == 200, mapped.text
        revision = mapped.json()["revision"]
    else:
        revision = created["revision"]
    preview = await client.post(
        f"/payroll/imports/{import_id}/preview",
        json={"expected_revision": revision}, headers=tenant.admin,
    )
    assert preview.status_code == 200, preview.text
    return preview.json()


async def _target(engine, tenant, period, period_definition_id):
    async with engine.connect() as db:
        row = (await db.execute(text("""
            SELECT PayDefinitionID, PayrollPeriodDefinitionID, RateDefinitionID
            FROM payroll.PayrollPeriodDefinitions
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid AND PayrollPeriodDefinitionID=:target
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
               "target": period_definition_id})).mappings().first()
    assert row is not None, f"missing frozen period definition {period_definition_id} for company={tenant.company_id}, period={period['payroll_period_id']}"
    return row["paydefinitionid"], row["payrollperioddefinitionid"], row["ratedefinitionid"]


async def _insert_rule(engine, tenant, created, period, period_definition_id, pay_definition_id,
                       *, source_contract=2):
    stable_id, period_definition_id, rate_definition_id = await _target(
        engine, tenant, period, period_definition_id)
    assert stable_id == pay_definition_id
    from app.imports.batch_service import _target_meaning
    from app.imports.headers import normalize_header
    from app.imports.mapping import fingerprint
    from app.payroll.period_definitions import list_period_definitions

    async with engine.connect() as db:
        pending = (await db.execute(text("""
            SELECT r.RuleFingerprint, r.SourceSignature, r.TargetMeaning, r.RulePayload
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": created["import_id"], "cid": tenant.company_id,
               "definition": pay_definition_id})).mappings().first()
        definitions = await list_period_definitions(period["payroll_period_id"], tenant.company_id,
                                                    db, active_only=True)
    if pending:
        rule_fingerprint = pending["rulefingerprint"]
        source_signature = pending["sourcesignature"]
        meaning = pending["targetmeaning"]
        payload = pending["rulepayload"]
    else:
        definition = next(item for item in definitions
                          if item.payroll_period_definition_id == period_definition_id)
        meaning = _target_meaning(definition)
        inputs = [{"header": normalize_header(definition.code)}]
        shared = {"DriverNumber": {"header": "drivernumber"},
                  "WorkDate": {"header": "workdate"}}
        payload = {"payload_version": 1, "transform": "Direct", "inputs": inputs,
                   "shared_columns": shared}
        source_signature = {"inputs": inputs, "ignored_columns": []}
        rule_fingerprint = fingerprint("payroll-work-rule-v1", {
            "target": meaning, "source": {"inputs": inputs}, "payload": payload})

    def json_value(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    async with engine.begin() as db:
        rule_id = (await db.execute(text("""
            INSERT INTO import.PayrollWorkRuleVersions
                (CompanyID, BranchID, PayDefinitionID, OriginImportID, OriginPayrollPeriodID,
                 RuleContractVersion, SourceContractVersion, TargetContractVersion,
                 CanonicalizationVersion, RuleFingerprint, SourceSignature, TargetMeaning,
                 RulePayload, CreatedByUserID)
            VALUES (:cid, :bid, :definition, :iid, :pid, 1, :source_version, 2, 3,
                    :fingerprint, CAST(:source AS jsonb), CAST(:meaning AS jsonb),
                    CAST(:payload AS jsonb), :uid)
            RETURNING RuleVersionID
        """), {"cid": tenant.company_id, "bid": period["branch_id"],
               "definition": pay_definition_id, "iid": created["import_id"],
               "pid": period["payroll_period_id"], "source_version": source_contract,
               "fingerprint": rule_fingerprint, "source": json_value(source_signature),
               "meaning": json_value(meaning), "payload": json_value(payload),
               "uid": tenant.owner})).scalar_one()
    return rule_id, period_definition_id, rate_definition_id


async def _mapped_preview_with_rule(client, engine, tenant, period):
    created, period_definition_id, pay_definition_id = await _upload(client, engine, tenant, period)
    preview = await _preview(client, tenant, created, period_definition_id)
    async with engine.connect() as db:
        pending = (await db.execute(text("""
            SELECT PayrollPeriodDefinitionID, RateDefinitionID, RuleVersionID
            FROM import.PayrollWorkPendingRules
            WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
        """), {"iid": created["import_id"], "cid": tenant.company_id,
               "definition": pay_definition_id})).one()
    return (created, period_definition_id, pay_definition_id,
            pending.payrollperioddefinitionid, pending.ratedefinitionid,
            pending.ruleversionid, preview)


async def _eligible_assignment(engine, tenant, period):
    async with engine.connect() as db:
        return (await db.execute(text("""
            SELECT a.DriverAssignmentID, d.DriverID, x.WorkDate
            FROM payroll.PayrollPeriodDriverEligibility p
            JOIN core.DriverAssignments a ON a.DriverAssignmentID=p.DriverAssignmentID
              AND a.CompanyID=p.CompanyID AND a.BranchID=p.BranchID
            JOIN core.Drivers d ON d.DriverID=a.DriverID AND d.CompanyID=a.CompanyID
            CROSS JOIN LATERAL unnest(p.EligibleWorkDates) AS x(WorkDate)
            WHERE p.CompanyID=:cid AND p.PayrollPeriodID=:pid AND p.IsEligibleForPeriod
            ORDER BY d.DriverID, x.WorkDate LIMIT 1
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})).mappings().one()


async def _write_source_change(engine, tenant, period, *, import_id, revision, plan_fingerprint,
                               definition_id, ppd_id, rate_id, rule_id, complete, insert_link=True,
                               attempt_rebind_rule_id=None):
    from app.payroll.quantity_batch import (
        ReviewedQuantityChange,
        apply_reviewed_quantities,
        prepare_quantity_mutation_scope,
    )

    assignment = await _eligible_assignment(engine, tenant, period)
    async with engine.connect() as db:
        import_row = (await db.execute(text("""
            SELECT CorrelationID FROM import.Imports
            WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).mappings().one()
    async with engine.begin() as db:
        scope = await prepare_quantity_mutation_scope(db, company_id=tenant.company_id,
            period_id=period["payroll_period_id"], user_id=tenant.owner)
        result = await apply_reviewed_quantities(scope, [ReviewedQuantityChange(
            driver_assignment_id=assignment["driverassignmentid"], work_date=assignment["workdate"],
            payroll_period_definition_id=ppd_id, quantity=Decimal("1"),
            expected_present=False, expected_quantity=None)],
            correlation_id=str(import_row["correlationid"]), source_revision=revision)
        line_id, evidence_id = result.changed[(assignment["driverassignmentid"], assignment["workdate"], ppd_id)]
        if insert_link:
            await db.execute(text("""
                INSERT INTO import.PayrollWorkAppliedChanges
                    (ImportID, CompanyID, BranchID, PayrollPeriodID, PayDefinitionID,
                     PayrollPeriodDefinitionID, RateDefinitionID, RuleVersionID, DriverID,
                     DriverAssignmentID, WorkDate, DraftLineID, EvidenceEventID, ReviewedRevision,
                     PlanFingerprint, ContractVersion, Action)
                VALUES (:iid, :cid, :bid, :pid, :definition, :ppd, :rate, :rule, :driver,
                        :assignment, :workdate, :line, :evidence, :revision, :fingerprint, 1, 'Created')
            """), {"iid": import_id, "cid": tenant.company_id, "bid": period["branch_id"],
                   "pid": period["payroll_period_id"], "definition": definition_id, "ppd": ppd_id,
                   "rate": rate_id, "rule": rule_id, "driver": assignment["driverid"],
                   "assignment": assignment["driverassignmentid"], "workdate": assignment["workdate"],
                   "line": line_id, "evidence": evidence_id, "revision": revision,
                   "fingerprint": plan_fingerprint})
            if attempt_rebind_rule_id is not None:
                try:
                    async with db.begin_nested():
                        await db.execute(text("""
                            UPDATE import.PayrollWorkPendingRules SET RuleVersionID=:rule,
                                Revision=Revision+1, UpdatedByUserID=:uid, UpdatedAtUtc=NOW()
                            WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
                        """), {"rule": attempt_rebind_rule_id, "uid": tenant.owner,
                               "iid": import_id, "cid": tenant.company_id,
                               "definition": definition_id})
                except DBAPIError as exc:
                    assert "fk_payrollworkappliedchanges_pendingruleversion" in str(exc).lower()
                else:
                    raise AssertionError("pending rule changed while an applied-change link referenced its version")
        if complete and insert_link:
            await db.execute(text("""
                UPDATE import.Imports SET Status='Completed', Revision=Revision+1,
                    CompletedByUserID=:uid, CompletedAtUtc=NOW(), UpdatedByUserID=:uid, UpdatedAtUtc=NOW()
                WHERE ImportID=:iid AND CompanyID=:cid
            """), {"uid": tenant.owner, "iid": import_id, "cid": tenant.company_id})
            await db.execute(text("""
                INSERT INTO import.PayrollWorkRuleHeads
                    (CompanyID, BranchID, PayDefinitionID, RuleVersionID, Revision,
                     ActivatedByImportID, ActivatedPayrollPeriodID, ActivatedByUserID)
                VALUES (:cid, :bid, :definition, :rule, 1, :iid, :pid, :uid)
            """), {"cid": tenant.company_id, "bid": period["branch_id"],
                   "definition": definition_id, "rule": rule_id, "iid": import_id,
                   "pid": period["payroll_period_id"], "uid": tenant.owner})
    return line_id, evidence_id, assignment["driverassignmentid"], assignment["workdate"], import_row["correlationid"]


async def test_preview_publishes_rule_context_and_attached_unsupported_version_fails_closed(
    p3c_client, p3c_engine, tenant,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, period_definition_id, definition_id = await _upload(p3c_client, p3c_engine, tenant, period)
    preview = await _preview(p3c_client, tenant, created, period_definition_id)
    import_id = created["import_id"]
    _stable_id, frozen_definition_id, frozen_rate_id = await _target(
        p3c_engine, tenant, period, period_definition_id)
    async with p3c_engine.begin() as db:
        stored = (await db.execute(text("""
            SELECT Fingerprint FROM import.PayrollWorkPreviewRuleContexts
            WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).scalar_one()
        assert len(stored) == 64
        pending = (await db.execute(text("""
            SELECT PayDefinitionID, PayrollPeriodDefinitionID, RateDefinitionID
            FROM import.PayrollWorkPendingRules WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).one()
        assert pending == (definition_id, frozen_definition_id, frozen_rate_id)
        assert (await db.execute(text("""
            SELECT count(*) FROM import.PayrollWorkRuleHeads
            WHERE CompanyID=:cid AND BranchID=:bid AND PayDefinitionID=:definition
        """), {"cid": tenant.company_id, "bid": period["branch_id"],
               "definition": definition_id})).scalar_one() == 0

    query = (f"expected_revision={preview['revision']}&plan_fingerprint={preview['preview']['plan_fingerprint']}")

    async def assert_all_reads_reject() -> None:
        requests = [
            await p3c_client.get(f"/payroll/imports/{import_id}", headers=tenant.admin),
            await p3c_client.get(f"/payroll/imports/{import_id}/rows?{query}", headers=tenant.admin),
            await p3c_client.get(f"/payroll/imports/{import_id}/issues?{query}", headers=tenant.admin),
            await p3c_client.get(f"/payroll/imports/{import_id}/source-records?{query}", headers=tenant.admin),
        ]
        assert all(response.status_code == 409 for response in requests), [r.text for r in requests]
        assert all(response.json()["detail"]["code"] == "IMPORT_CONTRACT_UNSUPPORTED" for response in requests)
        cancelled = await p3c_client.post(f"/payroll/imports/{import_id}/cancel",
            json={"expected_revision": preview["revision"]}, headers=tenant.admin)
        assert cancelled.status_code == 409 and cancelled.json()["detail"]["code"] == "IMPORT_CONTRACT_UNSUPPORTED"

    trigger_for = {
        ("import.Imports", "trg_imports_envelope"): TriggerIdentity("import", "imports", "trg_imports_envelope"),
        ("import.Imports", "trg_imports_payrollwork"): TriggerIdentity("import", "imports", "trg_imports_payrollwork"),
        ("import.FileSourceBatches", "trg_importdetail_writable"): TriggerIdentity("import", "filesourcebatches", "trg_importdetail_writable"),
        ("import.PayrollWorkImports", "trg_importdetail_writable"): TriggerIdentity("import", "payrollworkimports", "trg_importdetail_writable"),
        ("import.PayrollWorkImports", "trg_imports_payrollwork"): TriggerIdentity("import", "payrollworkimports", "trg_imports_payrollwork"),
    }
    base_version_cases = (
        ("import.Imports", "trg_imports_envelope", "ContractVersion", "ImportID", import_id, 1,
         "trg_imports_payrollwork"),
        ("import.FileSourceBatches", "trg_importdetail_writable", "ContractVersion", "ImportID", import_id, 2, None),
        ("import.PayrollWorkImports", "trg_importdetail_writable", "ContractVersion", "ImportID", import_id, 2, None),
        ("import.PayrollWorkImports", "trg_importdetail_writable", "CanonicalizationVersion", "ImportID", import_id, 3, None),
    )
    for table, first_trigger, column, owner_column, owner_id, baseline, second_trigger in base_version_cases:
        trigger_ids = (trigger_for[(table, first_trigger)],) + (
            (trigger_for[(table, second_trigger)],) if second_trigger else ()
        )
        async with p3c_engine.begin() as db:
            async with suspended_test_triggers(db, trigger_ids):
                await db.execute(text(f"UPDATE {table} SET {column}=99 WHERE {owner_column}=:iid"), {"iid": owner_id})
        await assert_all_reads_reject()
        async with p3c_engine.begin() as db:
            async with suspended_test_triggers(db, trigger_ids):
                await db.execute(text(f"UPDATE {table} SET {column}=:version WHERE {owner_column}=:iid"),
                                 {"version": baseline, "iid": owner_id})

    async with p3c_engine.connect() as db:
        file_row = (await db.execute(text("""
            SELECT f.FileSourceID AS file_source_id, c.FormatVersion AS format_version
            FROM import.FileSources f JOIN import.FileCanonicalData c
              ON c.FileSourceID=f.FileSourceID AND c.ImportID=f.ImportID AND c.CompanyID=f.CompanyID
            WHERE f.ImportID=:iid AND f.CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).one()
        file_id = file_row.file_source_id
    async with p3c_engine.begin() as db:
        async with suspended_test_triggers(db, (TriggerIdentity("import", "filesources", "trg_importdetail_writable"),)):
            await db.execute(text("UPDATE import.FileSources SET ContractVersion=99 WHERE FileSourceID=:fid"), {"fid": file_id})
    await assert_all_reads_reject()
    async with p3c_engine.begin() as db:
        async with suspended_test_triggers(db, (TriggerIdentity("import", "filesources", "trg_importdetail_writable"),)):
            await db.execute(text("UPDATE import.FileSources SET ContractVersion=2 WHERE FileSourceID=:fid"), {"fid": file_id})

    # Simulate a preexisting stored format version unknown to the shared readers.
    _stable_id, period_definition_id, rate_definition_id = await _target(
        p3c_engine, tenant, period, period_definition_id)
    canonical_trigger = (TriggerIdentity("import", "filecanonicaldata", "trg_filecanonicaldata_writable"),)
    async with p3c_engine.begin() as db:
        async with suspended_test_triggers(db, canonical_trigger):
            await db.execute(text("UPDATE import.FileCanonicalData SET FormatVersion=9 WHERE FileSourceID=:fid"),
                             {"fid": file_id})
    await assert_all_reads_reject()
    async with p3c_engine.begin() as db:
        async with suspended_test_triggers(db, canonical_trigger):
            await db.execute(text("UPDATE import.FileCanonicalData SET FormatVersion=:version WHERE FileSourceID=:fid"),
                             {"fid": file_id, "version": file_row.format_version})

    # Simulate an old attached rule version with an unsupported source contract.
    async with p3c_engine.begin() as db:
        async with suspended_test_triggers(db, (TriggerIdentity("import", "payrollworkruleversions", "trg_payrollworkruleversions_writable"),)):
            rule_id = (await db.execute(text("""
            INSERT INTO import.PayrollWorkRuleVersions
                (CompanyID, BranchID, PayDefinitionID, OriginImportID, OriginPayrollPeriodID,
                 RuleContractVersion, SourceContractVersion, TargetContractVersion,
                 CanonicalizationVersion, RuleFingerprint, SourceSignature, TargetMeaning,
                 RulePayload, CreatedByUserID)
            VALUES (:cid, :bid, :definition, :iid, :pid, 1, 9, 2, 3,
                    :fingerprint, '{}'::jsonb, '{}'::jsonb, '{"kind":"mapping"}'::jsonb, :uid)
            RETURNING RuleVersionID
            """), {"cid": tenant.company_id, "bid": period["branch_id"],
                   "definition": definition_id, "iid": import_id, "pid": period["payroll_period_id"],
                   "fingerprint": "b" * 64, "uid": tenant.owner})).scalar_one()
        async with suspended_test_triggers(db, (TriggerIdentity("import", "payrollworkpendingrules", "trg_payrollworkpendingrules_writable"),)):
            attached = await db.execute(text("""
                UPDATE import.PayrollWorkPendingRules SET RuleVersionID=:rule, UpdatedByUserID=:uid
                WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
            """), {"iid": import_id, "cid": tenant.company_id, "definition": definition_id,
                   "rule": rule_id, "uid": tenant.owner})
            assert attached.rowcount == 1
    await assert_all_reads_reject()
    assert period_definition_id > 0 and rate_definition_id > 0


async def test_rule_binding_advances_import_revision_and_rejects_an_unconfirmed_foreign_proposal(
    p3c_client, p3c_engine, tenant,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, target_period_definition_id, definition_id = await _upload(
        p3c_client, p3c_engine, tenant, period)
    preview = await _preview(p3c_client, tenant, created, target_period_definition_id)
    own_rule, ppd_id, rate_id = await _insert_rule(
        p3c_engine, tenant, created, period, target_period_definition_id, definition_id)
    with pytest.raises(DBAPIError):
        async with p3c_engine.begin() as db:
            await db.execute(text("""
                INSERT INTO import.PayrollWorkRuleVersions
                    (CompanyID, BranchID, PayDefinitionID, OriginImportID, OriginPayrollPeriodID,
                     RuleContractVersion, SourceContractVersion, TargetContractVersion,
                     CanonicalizationVersion, RuleFingerprint, SourceSignature, TargetMeaning,
                     RulePayload, CreatedByUserID)
                VALUES (:cid, :other_branch, :definition, :iid, :pid, 1, 2, 2, 3,
                        :fingerprint, '{}'::jsonb, '{}'::jsonb, '{"kind":"wrong-branch"}'::jsonb, :uid)
            """), {"cid": tenant.company_id, "other_branch": tenant.branch_b,
                   "definition": definition_id, "iid": created["import_id"],
                   "pid": period["payroll_period_id"], "fingerprint": "c" * 64,
                   "uid": tenant.owner})
    with pytest.raises(DBAPIError, match="IMPORT_IMMUTABLE"):
        async with p3c_engine.begin() as db:
            await db.execute(text("""
                UPDATE import.PayrollWorkRuleVersions SET RuleFingerprint=:fingerprint
                WHERE RuleVersionID=:rid AND CompanyID=:cid
            """), {"fingerprint": "e" * 64, "rid": own_rule, "cid": tenant.company_id})
    async with p3c_engine.begin() as db:
        await bind_pending_rule(db, company_id=tenant.company_id, user_id=tenant.owner,
            import_id=created["import_id"], expected_import_revision=preview["revision"],
            pay_definition_id=definition_id, period_definition_id=ppd_id,
            rate_definition_id=rate_id, rule_version_id=own_rule, expected_head_revision=0)
    async with p3c_engine.connect() as db:
        current = (await db.execute(text("""
            SELECT i.Revision, i.Status, w.PlanFingerprint,
                (SELECT count(*) FROM import.PayrollWorkPendingRules p
                 WHERE p.ImportID=i.ImportID AND p.CompanyID=i.CompanyID),
                (SELECT count(*) FROM import.PayrollWorkPreviewRuleContexts c
                 WHERE c.ImportID=i.ImportID AND c.CompanyID=i.CompanyID)
            FROM import.Imports i JOIN import.PayrollWorkImports w
              ON w.ImportID=i.ImportID AND w.CompanyID=i.CompanyID
            WHERE i.ImportID=:iid AND i.CompanyID=:cid
        """), {"iid": created["import_id"], "cid": tenant.company_id})).one()
    assert current == (preview["revision"] + 1, "NeedsMapping", None, 1, 0)

    other, other_target_period_definition_id, other_definition_id = await _upload(
        p3c_client, p3c_engine, tenant, period, filename="other.csv")
    assert other_definition_id == definition_id
    unconfirmed_rule, _, _ = await _insert_rule(
        p3c_engine, tenant, other, period, other_target_period_definition_id, definition_id)
    with pytest.raises(ImportFailure) as caught:
        async with p3c_engine.begin() as db:
            await bind_pending_rule(db, company_id=tenant.company_id, user_id=tenant.owner,
                import_id=created["import_id"], expected_import_revision=current[0],
                pay_definition_id=definition_id, period_definition_id=ppd_id,
                rate_definition_id=rate_id, rule_version_id=unconfirmed_rule,
                expected_head_revision=0)
    assert caught.value.code == "IMPORT_RULE_REUSE_UNCONFIRMED"
    assert other["import_id"] != created["import_id"]


async def test_cancelled_import_keeps_historical_page_reads_available(p3c_client, p3c_engine, tenant):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, period_definition_id, definition_id = await _upload(p3c_client, p3c_engine, tenant, period)
    preview = await _preview(p3c_client, tenant, created, period_definition_id)
    cancelled = await p3c_client.post(
        f"/payroll/imports/{created['import_id']}/cancel",
        json={"expected_revision": preview["revision"]}, headers=tenant.admin)
    assert cancelled.status_code == 200, cancelled.text
    query = (f"expected_revision={cancelled.json()['revision']}"
             f"&plan_fingerprint={preview['preview']['plan_fingerprint']}")
    responses = [
        await p3c_client.get(f"/payroll/imports/{created['import_id']}/rows?{query}", headers=tenant.admin),
        await p3c_client.get(f"/payroll/imports/{created['import_id']}/issues?{query}", headers=tenant.admin),
        await p3c_client.get(f"/payroll/imports/{created['import_id']}/source-records?{query}", headers=tenant.admin),
    ]
    assert all(response.status_code == 200 for response in responses), [response.text for response in responses]


async def test_old_identity_resolution_context_requires_live_refresh_but_not_terminal_reads(
    p3c_client, p3c_engine, tenant,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, period_definition_id, definition_id = await _upload(p3c_client, p3c_engine, tenant, period)
    preview = await _preview(p3c_client, tenant, created, period_definition_id)
    import_id = created["import_id"]
    context_trigger = TriggerIdentity(
        "import", "payrollworkpreviewrulecontexts", "trg_payrollworkpreviewrulecontexts_writable")
    async with p3c_engine.begin() as db:
        pending_before = (await db.execute(text("""
            SELECT PayDefinitionID, RuleVersionID, Revision
            FROM import.PayrollWorkPendingRules WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).all()
        async with suspended_test_triggers(db, [context_trigger]):
            await db.execute(text("""
                UPDATE import.PayrollWorkPreviewRuleContexts SET Fingerprint=:old
                WHERE ImportID=:iid AND CompanyID=:cid
            """), {"old": "0" * 64, "iid": import_id, "cid": tenant.company_id})

    query = (f"expected_revision={preview['revision']}&"
             f"plan_fingerprint={preview['preview']['plan_fingerprint']}")
    stale = await p3c_client.get(f"/payroll/imports/{import_id}/rows?{query}", headers=tenant.admin)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "IMPORT_RULE_CONTEXT_STALE"

    refreshed = await p3c_client.post(f"/payroll/imports/{import_id}/preview",
        json={"expected_revision": preview["revision"]}, headers=tenant.admin)
    assert refreshed.status_code == 200, refreshed.text
    async with p3c_engine.begin() as db:
        pending_after = (await db.execute(text("""
            SELECT PayDefinitionID, RuleVersionID, Revision
            FROM import.PayrollWorkPendingRules WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).all()
        async with suspended_test_triggers(db, [context_trigger]):
            await db.execute(text("""
                UPDATE import.PayrollWorkPreviewRuleContexts SET Fingerprint=:old
                WHERE ImportID=:iid AND CompanyID=:cid
            """), {"old": "0" * 64, "iid": import_id, "cid": tenant.company_id})
    assert pending_after == pending_before

    cancelled = await p3c_client.post(f"/payroll/imports/{import_id}/cancel",
        json={"expected_revision": refreshed.json()["revision"]}, headers=tenant.admin)
    assert cancelled.status_code == 200, cancelled.text
    history_query = (f"expected_revision={cancelled.json()['revision']}&"
                     f"plan_fingerprint={refreshed.json()['preview']['plan_fingerprint']}")
    pages = [
        await p3c_client.get(f"/payroll/imports/{import_id}/rows?{history_query}", headers=tenant.admin),
        await p3c_client.get(f"/payroll/imports/{import_id}/issues?{history_query}", headers=tenant.admin),
        await p3c_client.get(f"/payroll/imports/{import_id}/source-records?{history_query}", headers=tenant.admin),
    ]
    assert all(page.status_code == 200 for page in pages), [page.text for page in pages]


@pytest.mark.parametrize("version_name", ["DATE_INTERPRETATION_VERSION", "ROW_HANDLING_VERSION"])
async def test_old_versioned_import_rule_context_requires_live_refresh_but_not_terminal_reads(
    p3c_client, p3c_engine, tenant, monkeypatch, version_name,
):
    from app.imports import rules

    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, period_definition_id, _definition_id = await _upload(p3c_client, p3c_engine, tenant, period)
    preview = await _preview(p3c_client, tenant, created, period_definition_id)
    import_id = created["import_id"]
    current_version = getattr(rules, version_name)
    monkeypatch.setattr(rules, version_name, current_version - 1)
    async with p3c_engine.connect() as db:
        old_date_context = await rules.current_rule_context(db, tenant.company_id, import_id)
    monkeypatch.setattr(rules, version_name, current_version)

    context_trigger = TriggerIdentity(
        "import", "payrollworkpreviewrulecontexts", "trg_payrollworkpreviewrulecontexts_writable")
    async with p3c_engine.begin() as db:
        pending_before = (await db.execute(text("""
            SELECT PayDefinitionID, RuleVersionID, Revision
            FROM import.PayrollWorkPendingRules WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).all()
        async with suspended_test_triggers(db, [context_trigger]):
            await db.execute(text("""
                UPDATE import.PayrollWorkPreviewRuleContexts SET Fingerprint=:old
                WHERE ImportID=:iid AND CompanyID=:cid
            """), {"old": old_date_context, "iid": import_id, "cid": tenant.company_id})

    query = (f"expected_revision={preview['revision']}&"
             f"plan_fingerprint={preview['preview']['plan_fingerprint']}")
    stale = await p3c_client.get(f"/payroll/imports/{import_id}/rows?{query}", headers=tenant.admin)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "IMPORT_RULE_CONTEXT_STALE"

    refreshed = await p3c_client.post(
        f"/payroll/imports/{import_id}/preview",
        json={"expected_revision": preview["revision"]}, headers=tenant.admin)
    assert refreshed.status_code == 200, refreshed.text
    async with p3c_engine.connect() as db:
        pending_after = (await db.execute(text("""
            SELECT PayDefinitionID, RuleVersionID, Revision
            FROM import.PayrollWorkPendingRules WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).all()
    assert pending_after == pending_before

    async with p3c_engine.begin() as db:
        async with suspended_test_triggers(db, [context_trigger]):
            await db.execute(text("""
                UPDATE import.PayrollWorkPreviewRuleContexts SET Fingerprint=:old
                WHERE ImportID=:iid AND CompanyID=:cid
            """), {"old": old_date_context, "iid": import_id, "cid": tenant.company_id})

    cancelled = await p3c_client.post(
        f"/payroll/imports/{import_id}/cancel",
        json={"expected_revision": refreshed.json()["revision"]}, headers=tenant.admin)
    assert cancelled.status_code == 200, cancelled.text
    history_query = (f"expected_revision={cancelled.json()['revision']}&"
                     f"plan_fingerprint={refreshed.json()['preview']['plan_fingerprint']}")
    pages = [
        await p3c_client.get(f"/payroll/imports/{import_id}/rows?{history_query}", headers=tenant.admin),
        await p3c_client.get(f"/payroll/imports/{import_id}/issues?{history_query}", headers=tenant.admin),
        await p3c_client.get(f"/payroll/imports/{import_id}/source-records?{history_query}", headers=tenant.admin),
    ]
    assert all(page.status_code == 200 for page in pages), [page.text for page in pages]


async def test_preview_publication_rejects_rule_head_change_during_preparation(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    from app.imports import batch_service

    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    target_import, target_period_definition_id, definition_id, ppd_id, rate_id, _rule_id, old_preview = await _mapped_preview_with_rule(
        p3c_client, p3c_engine, tenant, period)
    old_fingerprint = old_preview["preview"]["plan_fingerprint"]
    async with p3c_engine.connect() as db:
        old_state = (await db.execute(text("""
            SELECT w.PlanFingerprint, i.Revision,
                (SELECT count(*) FROM import.PayrollWorkPreviewGroups g
                 WHERE g.ImportID=i.ImportID AND g.CompanyID=i.CompanyID)
            FROM import.Imports i JOIN import.PayrollWorkImports w
              ON w.ImportID=i.ImportID AND w.CompanyID=i.CompanyID
            WHERE i.ImportID=:iid AND i.CompanyID=:cid
        """), {"iid": target_import["import_id"], "cid": tenant.company_id})).one()

    changing_import, changing_period_definition_id, changing_definition_id = await _upload(
        p3c_client, p3c_engine, tenant, period, filename="head-change.csv")
    assert changing_definition_id == definition_id
    changing_preview = await _preview(p3c_client, tenant, changing_import, changing_period_definition_id)
    new_rule, new_ppd, new_rate = await _insert_rule(
        p3c_engine, tenant, changing_import, period, changing_period_definition_id, definition_id)
    async with p3c_engine.begin() as db:
        await bind_pending_rule(db, company_id=tenant.company_id, user_id=tenant.owner,
            import_id=changing_import["import_id"], expected_import_revision=changing_preview["revision"],
            pay_definition_id=definition_id, period_definition_id=new_ppd,
            rate_definition_id=new_rate, rule_version_id=new_rule, expected_head_revision=0)
    changing_metadata = await p3c_client.get(
        f"/payroll/imports/{changing_import['import_id']}", headers=tenant.admin)
    assert changing_metadata.status_code == 200, changing_metadata.text
    changing_preview = await p3c_client.post(
        f"/payroll/imports/{changing_import['import_id']}/preview",
        json={"expected_revision": changing_metadata.json()["revision"]}, headers=tenant.admin)
    assert changing_preview.status_code == 200, changing_preview.text

    entered = threading.Event()
    release = threading.Event()
    real_prepare = batch_service.prepare_preview

    def pause_preparation(result):
        entered.set()
        if not release.wait(15):
            raise TimeoutError("test did not release preview preparation")
        return real_prepare(result)

    monkeypatch.setattr(batch_service, "prepare_preview", pause_preparation)
    metadata = await p3c_client.get(f"/payroll/imports/{target_import['import_id']}", headers=tenant.admin)
    assert metadata.status_code == 200, metadata.text
    request_task = asyncio.create_task(p3c_client.post(
        f"/payroll/imports/{target_import['import_id']}/preview",
        json={"expected_revision": metadata.json()["revision"]}, headers=tenant.admin))
    assert await asyncio.to_thread(entered.wait, 15), "preview did not enter its preparation phase"
    try:
        async with p3c_engine.begin() as db:
            await db.execute(text("""
                UPDATE import.Imports SET Status='Completed', Revision=Revision+1,
                    CompletedByUserID=:uid, CompletedAtUtc=NOW(), UpdatedByUserID=:uid, UpdatedAtUtc=NOW()
                WHERE ImportID=:iid AND CompanyID=:cid
            """), {"uid": tenant.owner, "iid": changing_import["import_id"], "cid": tenant.company_id})
            await db.execute(text("""
                INSERT INTO import.PayrollWorkRuleHeads
                    (CompanyID, BranchID, PayDefinitionID, RuleVersionID, Revision,
                     ActivatedByImportID, ActivatedPayrollPeriodID, ActivatedByUserID)
                VALUES (:cid, :bid, :definition, :rule, 1, :iid, :pid, :uid)
            """), {"cid": tenant.company_id, "bid": period["branch_id"], "definition": definition_id,
                   "rule": new_rule, "iid": changing_import["import_id"],
                   "pid": period["payroll_period_id"], "uid": tenant.owner})
    finally:
        release.set()
    response = await request_task
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "IMPORT_RULE_CONTEXT_STALE"
    async with p3c_engine.connect() as db:
        new_state = (await db.execute(text("""
            SELECT w.PlanFingerprint, i.Revision,
                (SELECT count(*) FROM import.PayrollWorkPreviewGroups g
                 WHERE g.ImportID=i.ImportID AND g.CompanyID=i.CompanyID)
            FROM import.Imports i JOIN import.PayrollWorkImports w
              ON w.ImportID=i.ImportID AND w.CompanyID=i.CompanyID
            WHERE i.ImportID=:iid AND i.CompanyID=:cid
        """), {"iid": target_import["import_id"], "cid": tenant.company_id})).one()
    assert new_state == old_state
    assert old_state[0] == old_fingerprint


async def test_actual_quantity_evidence_link_requires_completion_and_completed_head_is_reusable(
    p3c_client, p3c_engine, tenant,
):
    from tests.calculation_snapshot_helpers import finalize, submit_and_approve
    from tests.payroll_runtime_fixtures import create_period

    # Advance the stable Company definition sequence without creating a frozen
    # period row, so the later PayDefinitionID and period-local target ID differ.
    await create_definition(p3c_client, tenant)
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, target_period_definition_id, definition_id, ppd_id, rate_id, rule_id, preview = await _mapped_preview_with_rule(
        p3c_client, p3c_engine, tenant, period)
    assert definition_id != ppd_id, "fixture must distinguish stable pay definition from period target identity"
    replacement_proposal, _, _ = await _insert_rule(
        p3c_engine, tenant, created, period, target_period_definition_id, definition_id)
    async with p3c_engine.connect() as db:
        durable_rule_versions_before_finalize = set((await db.execute(text("""
            SELECT RuleVersionID FROM import.PayrollWorkRuleVersions
            WHERE OriginImportID=:iid AND CompanyID=:cid
        """), {"iid": created["import_id"], "cid": tenant.company_id})).scalars())
    assert rule_id in durable_rule_versions_before_finalize
    assert replacement_proposal in durable_rule_versions_before_finalize
    async with p3c_engine.begin() as db:
        await db.execute(text("""
            INSERT INTO import.PayrollWorkReviewDecisions
                (ImportID, CompanyID, BranchID, PayrollPeriodID, DecisionKey, ContractVersion,
                 ReviewedRevision, PlanFingerprint, DecisionKind, Choice, BasisJSON,
                 PayrollPeriodDefinitionID, PayDefinitionID, RateDefinitionID, ActorUserID)
            VALUES (:iid, :cid, :bid, :pid, 'reviewed-target', 1, :revision, :fingerprint,
                    'MappingReview', 'Keep', '{}'::jsonb, :ppd, :definition, :rate, :uid)
        """), {"iid": created["import_id"], "cid": tenant.company_id,
               "bid": period["branch_id"], "pid": period["payroll_period_id"],
               "revision": preview["revision"], "fingerprint": preview["preview"]["plan_fingerprint"],
               "ppd": ppd_id, "definition": definition_id, "rate": rate_id, "uid": tenant.owner})
    line_id, evidence_id, assignment_id, work_date, _correlation = await _write_source_change(
        p3c_engine, tenant, period, import_id=created["import_id"], revision=preview["revision"],
        plan_fingerprint=preview["preview"]["plan_fingerprint"], definition_id=definition_id,
        ppd_id=ppd_id, rate_id=rate_id, rule_id=rule_id, complete=True,
        attempt_rebind_rule_id=replacement_proposal)
    async with p3c_engine.connect() as db:
        state = (await db.execute(text("""
            SELECT i.Status, a.Action, a.DraftLineID, a.EvidenceEventID,
                e.EvidenceDomain, e.ActionCode, e.SourceRevision,
                e.AfterStateJson->>'source_type', e.AfterStateJson->>'pay_definition_id'
            FROM import.Imports i
            JOIN import.PayrollWorkAppliedChanges a ON a.ImportID=i.ImportID AND a.CompanyID=i.CompanyID
            JOIN payroll.PayrollPeriodAuditEvidenceEvents e
              ON e.PayrollPeriodAuditEvidenceEventID=a.EvidenceEventID AND e.CompanyID=a.CompanyID
            WHERE i.ImportID=:iid AND i.CompanyID=:cid
        """), {"iid": created["import_id"], "cid": tenant.company_id})).one()
        head = (await db.execute(text("""
            SELECT RuleVersionID, Revision FROM import.PayrollWorkRuleHeads
            WHERE CompanyID=:cid AND BranchID=:bid AND PayDefinitionID=:definition
        """), {"cid": tenant.company_id, "bid": period["branch_id"],
               "definition": definition_id})).one()
    assert state == ("Completed", "Created", line_id, evidence_id, "SOURCE", "SOURCE_CREATED",
                     preview["revision"], "Import", str(definition_id))
    assert head == (rule_id, 1)

    async def assert_terminal_pages(import_id: int, revision: int, fingerprint: str) -> None:
        query = f"expected_revision={revision}&plan_fingerprint={fingerprint}"
        responses = [
            await p3c_client.get(f"/payroll/imports/{import_id}/rows?{query}", headers=tenant.admin),
            await p3c_client.get(f"/payroll/imports/{import_id}/issues?{query}", headers=tenant.admin),
            await p3c_client.get(f"/payroll/imports/{import_id}/source-records?{query}", headers=tenant.admin),
        ]
        assert all(response.status_code == 200 for response in responses), [response.text for response in responses]

    original_fingerprint = preview["preview"]["plan_fingerprint"]
    await assert_terminal_pages(created["import_id"], preview["revision"] + 1, original_fingerprint)

    next_driver = await _eligible_driver(p3c_engine, tenant, period)
    async with p3c_engine.connect() as db:
        next_code = await db.scalar(text("""
            SELECT DefinitionCodeSnapshot FROM payroll.PayrollPeriodDefinitions
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid AND PayDefinitionID=:definition
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
               "definition": definition_id})
    next_target_period_definition_id = target_period_definition_id
    next_import_response = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/imports",
        files={"file": (f"{next_driver['workdate'].isoformat()}.csv",
                         (f"DriverNumber,{next_code}\n{next_driver['drivernumber']},1\n").encode(),
                         "text/csv")},
        headers={**tenant.admin, "Idempotency-Key": str(uuid4())})
    assert next_import_response.status_code == 200, next_import_response.text
    next_import = next_import_response.json()
    next_metadata_response = await p3c_client.get(
        f"/payroll/imports/{next_import['import_id']}", headers=tenant.admin)
    assert next_metadata_response.status_code == 200, next_metadata_response.text
    async with p3c_engine.connect() as db:
        pending_without_date = await db.execute(text("""
            SELECT p.RuleVersionID, p.ExpectedHeadRevision, r.RulePayload
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayrollPeriodDefinitionID=:ppd
        """), {"iid": next_import["import_id"], "cid": tenant.company_id,
               "ppd": next_target_period_definition_id})
        pending_without_date = pending_without_date.mappings().one_or_none()
    assert pending_without_date is not None
    assert pending_without_date["ruleversionid"] == rule_id
    assert pending_without_date["expectedheadrevision"] == 1
    assert pending_without_date["rulepayload"]["inputs"][0]["header"] == next_code.casefold()
    blocked_preview = await p3c_client.post(
        f"/payroll/imports/{next_import['import_id']}/preview",
        json={"expected_revision": next_import["revision"]}, headers=tenant.admin)
    assert blocked_preview.status_code == 422, blocked_preview.text
    interpreted = await p3c_client.put(
        f"/payroll/imports/{next_import['import_id']}/interpretation",
        json={"expected_revision": next_import["revision"], "units": [
            {"source_unit_id": unit["source_unit_id"], "selected": True,
             "date_source": {"kind": "filename"}}
            for file in next_import["files"] for unit in file["units"]]},
        headers=tenant.admin)
    assert interpreted.status_code == 200, interpreted.text
    next_import["revision"] = interpreted.json()["revision"]
    await _preview(p3c_client, tenant, next_import, next_target_period_definition_id)
    _stable_id, next_ppd, next_rate = await _target(
        p3c_engine, tenant, period, next_target_period_definition_id)
    async with p3c_engine.connect() as db:
        reused = (await db.execute(text("""
            SELECT p.RuleVersionID, p.ExpectedHeadRevision, p.PayrollPeriodDefinitionID,
                   p.RateDefinitionID, r.RulePayload, r.SourceSignature, r.TargetMeaning
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": next_import["import_id"], "cid": tenant.company_id,
               "definition": definition_id})).one()
    assert reused[1:4] == (1, next_ppd, next_rate)
    assert reused[0] != rule_id  # The explicit filename authority changes only the date dependency.
    assert reused[4]["inputs"][0]["header"] == next_code.casefold()
    assert reused[4]["date_interpretation"]["profiles"][0]["authority"] == "filename"
    async with p3c_engine.connect() as db:
        original_rule = (await db.execute(text("""
            SELECT RulePayload, SourceSignature, TargetMeaning
            FROM import.PayrollWorkRuleVersions
            WHERE RuleVersionID=:rid AND CompanyID=:cid
        """), {"rid": rule_id, "cid": tenant.company_id})).mappings().one()
    assert {key: value for key, value in reused[4].items() if key != "date_interpretation"} == \
        {key: value for key, value in original_rule["rulepayload"].items()
         if key != "date_interpretation"}
    assert reused[5] == original_rule["sourcesignature"]
    assert reused[6] == original_rule["targetmeaning"]

    # Complete a later Import with a replacement rule for the same target.
    # Its head revision must not make the first Import's immutable pages stale.
    replacement_rule, next_ppd, next_rate = await _insert_rule(
        p3c_engine, tenant, next_import, period, next_target_period_definition_id, definition_id)
    current_metadata = await p3c_client.get(
        f"/payroll/imports/{next_import['import_id']}", headers=tenant.admin)
    assert current_metadata.status_code == 200, current_metadata.text
    async with p3c_engine.begin() as db:
        await bind_pending_rule(db, company_id=tenant.company_id, user_id=tenant.owner,
            import_id=next_import["import_id"], expected_import_revision=current_metadata.json()["revision"],
            pay_definition_id=definition_id, period_definition_id=next_ppd,
            rate_definition_id=next_rate, rule_version_id=replacement_rule, expected_head_revision=1)
    rebound_preview = await p3c_client.post(
        f"/payroll/imports/{next_import['import_id']}/preview",
        json={"expected_revision": current_metadata.json()["revision"] + 1}, headers=tenant.admin)
    assert rebound_preview.status_code == 200, rebound_preview.text
    with pytest.raises(DBAPIError, match="IMPORT_RULE_HEAD_GATE"):
        async with p3c_engine.begin() as db:
            await db.execute(text("""
                UPDATE import.PayrollWorkRuleHeads SET RuleVersionID=:rule, Revision=Revision+1,
                    ActivatedByImportID=:iid, ActivatedPayrollPeriodID=:pid,
                    ActivatedByUserID=:uid, ActivatedAtUtc=NOW()
                WHERE CompanyID=:cid AND BranchID=:bid AND PayDefinitionID=:definition
            """), {"rule": replacement_rule, "iid": next_import["import_id"],
                   "pid": period["payroll_period_id"], "uid": tenant.owner,
                   "cid": tenant.company_id, "bid": period["branch_id"], "definition": definition_id})
    async with p3c_engine.connect() as db:
        unchanged_head = (await db.execute(text("""
            SELECT RuleVersionID, Revision FROM import.PayrollWorkRuleHeads
            WHERE CompanyID=:cid AND BranchID=:bid AND PayDefinitionID=:definition
        """), {"cid": tenant.company_id, "bid": period["branch_id"],
               "definition": definition_id})).one()
    assert unchanged_head == (rule_id, 1)
    async with p3c_engine.begin() as db:
        await db.execute(text("""
            UPDATE import.Imports SET Status='Completed', Revision=Revision+1,
                CompletedByUserID=:uid, CompletedAtUtc=NOW(), UpdatedByUserID=:uid, UpdatedAtUtc=NOW()
            WHERE ImportID=:iid AND CompanyID=:cid
        """), {"uid": tenant.owner, "iid": next_import["import_id"], "cid": tenant.company_id})
        await db.execute(text("""
            UPDATE import.PayrollWorkRuleHeads SET RuleVersionID=:rule, Revision=Revision+1,
                ActivatedByImportID=:iid, ActivatedPayrollPeriodID=:pid,
                ActivatedByUserID=:uid, ActivatedAtUtc=NOW()
            WHERE CompanyID=:cid AND BranchID=:bid AND PayDefinitionID=:definition
        """), {"rule": replacement_rule, "iid": next_import["import_id"],
                   "pid": period["payroll_period_id"], "uid": tenant.owner,
               "cid": tenant.company_id, "bid": period["branch_id"], "definition": definition_id})
    await assert_terminal_pages(created["import_id"], preview["revision"] + 1, original_fingerprint)

    await submit_and_approve(p3c_client, tenant, period)
    finalized = await finalize(p3c_client, tenant, period)
    assert finalized.status_code == 200, finalized.text
    async with p3c_engine.connect() as db:
        retained = (await db.execute(text("""
            SELECT c.Payload IS NULL, c.PurgedAtUtc IS NOT NULL,
                (SELECT count(*) FROM import.PayrollWorkReviewDecisions d
                 WHERE d.ImportID=:iid AND d.CompanyID=:cid),
                (SELECT count(*) FROM import.PayrollWorkPreviewRuleContexts x
                 WHERE x.ImportID=:iid AND x.CompanyID=:cid),
                (SELECT count(*) FROM import.PayrollWorkRuleVersions r
                 WHERE r.OriginImportID=:iid AND r.CompanyID=:cid),
                (SELECT count(*) FROM import.PayrollWorkPendingRules p
                 WHERE p.ImportID=:iid AND p.CompanyID=:cid),
                (SELECT count(*) FROM import.PayrollWorkAppliedChanges a
                 WHERE a.ImportID=:iid AND a.CompanyID=:cid),
                (SELECT count(*) FROM import.PayrollWorkRuleHeads h
                 WHERE h.CompanyID=:cid AND h.BranchID=:bid AND h.PayDefinitionID=:definition)
            FROM import.FileCanonicalData c
            WHERE c.ImportID=:iid AND c.CompanyID=:cid
        """), {"iid": created["import_id"], "cid": tenant.company_id,
               "bid": period["branch_id"], "definition": definition_id})).one()
    assert retained[:4] == (True, True, 0, 0)
    assert retained[4:] == (len(durable_rule_versions_before_finalize), 1, 1, 1)
    async with p3c_engine.connect() as db:
        durable_rule_versions_after_finalize = set((await db.execute(text("""
            SELECT RuleVersionID FROM import.PayrollWorkRuleVersions
            WHERE OriginImportID=:iid AND CompanyID=:cid
        """), {"iid": created["import_id"], "cid": tenant.company_id})).scalars())
        next_import_content_purged = await db.scalar(text("""
            SELECT bool_and(c.Payload IS NULL AND c.PurgedAtUtc IS NOT NULL)
            FROM import.FileCanonicalData c
            WHERE c.ImportID=:iid AND c.CompanyID=:cid
        """), {"iid": next_import["import_id"], "cid": tenant.company_id})
    assert durable_rule_versions_after_finalize == durable_rule_versions_before_finalize
    assert next_import_content_purged is True
    next_period = await create_period(p3c_client, tenant, tenant.branch_a)
    cross_period_driver = await _eligible_driver(p3c_engine, tenant, next_period)
    cross_period_import, new_target_period_definition_id, new_definition_id = await _upload(
        p3c_client, p3c_engine, tenant, next_period,
        filename=f"cross-period-{cross_period_driver['workdate'].isoformat()}.csv")
    assert new_definition_id == definition_id
    cross_period_preview = await _preview(
        p3c_client, tenant, cross_period_import, new_target_period_definition_id)
    _stable_id, next_period_definition_id, next_period_rate_id = await _target(
        p3c_engine, tenant, next_period, new_target_period_definition_id)
    assert next_period_definition_id != ppd_id
    async with p3c_engine.connect() as db:
        cross_period_state = (await db.execute(text("""
            SELECT p.RuleVersionID, p.ExpectedHeadRevision, h.RuleVersionID, h.Revision,
                p.PayrollPeriodDefinitionID, p.RateDefinitionID,
                (SELECT count(*) FROM import.PayrollWorkAppliedChanges a
                 WHERE a.ImportID=p.ImportID AND a.CompanyID=p.CompanyID)
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleHeads h ON h.CompanyID=p.CompanyID
              AND h.BranchID=p.BranchID AND h.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": cross_period_import["import_id"], "cid": tenant.company_id,
               "definition": definition_id})).one()
    assert cross_period_state[0] == replacement_rule
    assert cross_period_state[1:] == (2, replacement_rule, 2, next_period_definition_id, next_period_rate_id, 0)
    async with p3c_engine.connect() as db:
        cross_period_payload = await db.scalar(text("""
            SELECT RulePayload FROM import.PayrollWorkRuleVersions
            WHERE RuleVersionID=:rid AND CompanyID=:cid
        """), {"rid": cross_period_state[0], "cid": tenant.company_id})
    assert cross_period_payload["date_interpretation"]["profiles"][0]["authority"] == "filename"
    with pytest.raises(ImportFailure) as invalid_target:
        async with p3c_engine.begin() as db:
            await bind_pending_rule(db, company_id=tenant.company_id, user_id=tenant.owner,
                import_id=cross_period_import["import_id"],
                expected_import_revision=cross_period_preview["revision"],
                pay_definition_id=definition_id, period_definition_id=ppd_id,
                rate_definition_id=rate_id, rule_version_id=replacement_rule,
                expected_head_revision=2)
    assert invalid_target.value.code == "IMPORT_RULE_TARGET_INVALID"
    async with p3c_engine.connect() as db:
        retained_binding = (await db.execute(text("""
            SELECT RuleVersionID, Revision, ExpectedHeadRevision
            FROM import.PayrollWorkPendingRules
            WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
        """), {"iid": cross_period_import["import_id"], "cid": tenant.company_id,
               "definition": definition_id})).one()
    assert retained_binding == (replacement_rule, 1, 2)

    # A completed Branch A rule must not become a binding in another Branch,
    # even when the target's frozen meaning and source headers are compatible.
    grant_applicability(tenant, {"pay_definition_id": definition_id}, tenant.branch_b)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_b)
    branch_b_period = await create_period(p3c_client, tenant, tenant.branch_b)
    branch_b_import, branch_b_target, branch_b_definition = await _upload(
        p3c_client, p3c_engine, tenant, branch_b_period, filename="other-branch.csv")
    assert branch_b_definition == definition_id
    branch_b_state = next(item["rule_state"] for item in branch_b_import["mapping_targets"]
                          if item["target"] == f"Definition:{branch_b_target}")
    assert branch_b_state["current_head_revision"] == 0
    assert branch_b_state["pending_rule_version_id"] is None
    assert branch_b_state["state"] == "unmapped"

    # A second Company with the same definition meaning and file headers also
    # starts unmapped; the source rule remains scoped to its originating Company.
    async with p3c_engine.connect() as db:
        frozen = (await db.execute(text("""
            SELECT DefinitionCodeSnapshot, DefinitionNameSnapshot
            FROM payroll.PayrollPeriodDefinitions
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid AND PayDefinitionID=:definition
        """), {"cid": tenant.company_id, "pid": next_period["payroll_period_id"],
               "definition": definition_id})).one()
    other_tenant = build_payroll_tenant(tenant.dsn)
    other_period, _other_grid = await open_period_with_grid(
        p3c_client, p3c_engine, other_tenant,
        definition_code=frozen[0], definition_name=frozen[1])
    other_import, other_target, other_definition = await _upload(
        p3c_client, p3c_engine, other_tenant, other_period, filename="other-company.csv")
    assert other_definition != definition_id
    other_state = next(item["rule_state"] for item in other_import["mapping_targets"]
                       if item["target"] == f"Definition:{other_target}")
    assert other_state["current_head_revision"] == 0
    assert other_state["pending_rule_version_id"] is None
    assert other_state["state"] == "unmapped"

    async with p3c_engine.connect() as db:
        driver_number = await db.scalar(text("""
            SELECT d.DriverNumber FROM core.DriverAssignments a
            JOIN core.Drivers d ON d.DriverID=a.DriverID AND d.CompanyID=a.CompanyID
            WHERE a.CompanyID=:cid AND a.DriverAssignmentID=:assignment
        """), {"cid": tenant.company_id, "assignment": assignment_id})
    replacement_data = (f"DriverNumber,WorkDate,ReplacementUnits\n"
                        f"{driver_number},{work_date.isoformat()},3\n").encode()
    replacement_upload = await p3c_client.post(
        f"/payroll/periods/{next_period['payroll_period_id']}/imports",
        files={"file": ("replacement.csv", replacement_data, "text/csv")},
        headers={**tenant.admin, "Idempotency-Key": str(uuid4())})
    assert replacement_upload.status_code == 200, replacement_upload.text
    replacement_import = replacement_upload.json()
    replacement_target = f"Definition:{next_period_definition_id}"

    async def save_replacement(*, expected_head_revision, replace_existing):
        return await p3c_client.put(
            f"/payroll/imports/{replacement_import['import_id']}/mapping",
            json={"expected_revision": replacement_import["revision"],
                  "shared_columns": {"DriverNumber": {"header": "DriverNumber"},
                                     "WorkDate": {"header": "WorkDate"}},
                  "targets": [{"target": replacement_target, "transform": "Direct",
                               "inputs": [{"header": "ReplacementUnits"}],
                               "expected_head_revision": expected_head_revision,
                               "replace_existing": replace_existing}]},
            headers=tenant.admin)

    stale = await save_replacement(expected_head_revision=1, replace_existing=True)
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "IMPORT_RULE_HEAD_STALE"
    unconfirmed = await save_replacement(expected_head_revision=2, replace_existing=False)
    assert unconfirmed.status_code == 409, unconfirmed.text
    assert unconfirmed.json()["detail"]["code"] == "IMPORT_RULE_REPLACEMENT_REQUIRED"
    unchanged = await p3c_client.get(
        f"/payroll/imports/{replacement_import['import_id']}", headers=tenant.admin)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["revision"] == replacement_import["revision"]
    accepted = await save_replacement(expected_head_revision=2, replace_existing=True)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["revision"] == replacement_import["revision"] + 1
    async with p3c_engine.connect() as db:
        replacement_state = (await db.execute(text("""
            SELECT p.ExpectedHeadRevision, p.PayrollPeriodDefinitionID, p.RateDefinitionID,
                   h.RuleVersionID, h.Revision
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleHeads h ON h.CompanyID=p.CompanyID
              AND h.BranchID=p.BranchID AND h.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": replacement_import["import_id"], "cid": tenant.company_id,
               "definition": definition_id})).one()
    assert replacement_state == (2, next_period_definition_id, next_period_rate_id,
                                 replacement_rule, 2)
    assert assignment_id > 0 and work_date is not None


async def test_explicit_remap_replaces_only_the_changed_target_and_reuses_unchanged_head(
    p3c_client, p3c_engine, tenant,
):
    from app.imports.mapping import targets as mapping_targets
    from app.payroll.period_definitions import list_period_definitions

    hours = await rated_definition(p3c_client, tenant, definition_code=f"HOURS_{uuid4().hex[:8]}",
                                   definition_name="Hours input")
    miles = await rated_definition(p3c_client, tenant, definition_code=f"MILES_{uuid4().hex[:8]}",
                                   definition_name="Miles input")
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    driver = await _eligible_driver(p3c_engine, tenant, period)
    async with p3c_engine.connect() as db:
        definitions = await list_period_definitions(
            period["payroll_period_id"], tenant.company_id, db, active_only=True)
    targets = {item["code"]: item for item in mapping_targets(definitions) if item.get("code")}
    hours_target = targets[hours["definition_code"]]
    miles_target = targets[miles["definition_code"]]
    hours_ppd = int(hours_target["target"].split(":", 1)[1])
    miles_ppd = int(miles_target["target"].split(":", 1)[1])

    async def upload_source(filename, quantity_headers):
        header = ["DriverNumber", "WorkDate", *quantity_headers]
        row = [driver["drivernumber"], driver["workdate"].isoformat(), *(["1"] * len(quantity_headers))]
        response = await p3c_client.post(
            f"/payroll/periods/{period['payroll_period_id']}/imports",
            files={"file": (filename, (",".join(header) + "\n" + ",".join(row) + "\n").encode(), "text/csv")},
            headers={**tenant.admin, "Idempotency-Key": str(uuid4())})
        assert response.status_code == 200, response.text
        return response.json()

    async def complete_target(target, target_ppd, source_header):
        created = await upload_source(f"{source_header}.csv", ["Hours", "Miles"])
        mapping = await p3c_client.put(
            f"/payroll/imports/{created['import_id']}/mapping",
            json={"expected_revision": created["revision"],
                  "shared_columns": {"DriverNumber": {"header": "DriverNumber"},
                                     "WorkDate": {"header": "WorkDate"}},
                  "targets": [{"target": target["target"], "transform": "Direct",
                               "inputs": [{"header": source_header}],
                               "expected_head_revision": 0}]},
            headers=tenant.admin)
        assert mapping.status_code == 200, mapping.text
        preview = await p3c_client.post(
            f"/payroll/imports/{created['import_id']}/preview",
            json={"expected_revision": mapping.json()["revision"]}, headers=tenant.admin)
        assert preview.status_code == 200, preview.text
        stable_id, frozen_ppd, rate_id = await _target(
            p3c_engine, tenant, period, target_ppd)
        async with p3c_engine.begin() as db:
            rule_id = await db.scalar(text("""
                SELECT RuleVersionID FROM import.PayrollWorkPendingRules
                WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
            """), {"iid": created["import_id"], "cid": tenant.company_id,
                   "definition": stable_id})
            await db.execute(text("""
                INSERT INTO import.PayrollWorkReviewDecisions
                    (ImportID, CompanyID, BranchID, PayrollPeriodID, DecisionKey, ContractVersion,
                     ReviewedRevision, PlanFingerprint, DecisionKind, Choice, BasisJSON,
                     PayrollPeriodDefinitionID, PayDefinitionID, RateDefinitionID, ActorUserID)
                VALUES (:iid,:cid,:bid,:pid,'reviewed-target',1,:revision,:fingerprint,
                        'MappingReview','Keep','{}'::jsonb,:ppd,:definition,:rate,:uid)
            """), {"iid": created["import_id"], "cid": tenant.company_id,
                   "bid": period["branch_id"], "pid": period["payroll_period_id"],
                   "revision": preview.json()["revision"],
                   "fingerprint": preview.json()["preview"]["plan_fingerprint"],
                   "ppd": frozen_ppd, "definition": stable_id, "rate": rate_id,
                   "uid": tenant.owner})
        await _write_source_change(
            p3c_engine, tenant, period, import_id=created["import_id"],
            revision=preview.json()["revision"],
            plan_fingerprint=preview.json()["preview"]["plan_fingerprint"],
            definition_id=stable_id, ppd_id=frozen_ppd, rate_id=rate_id,
            rule_id=rule_id, complete=True)
        return rule_id

    hours_head_rule = await complete_target(hours_target, hours_ppd, "Hours")
    miles_head_rule = await complete_target(miles_target, miles_ppd, "Miles")
    async with p3c_engine.connect() as db:
        heads_before = (await db.execute(text("""
            SELECT PayDefinitionID, RuleVersionID, Revision
            FROM import.PayrollWorkRuleHeads
            WHERE CompanyID=:cid AND BranchID=:bid
            ORDER BY PayDefinitionID
        """), {"cid": tenant.company_id, "bid": period["branch_id"]})).all()
        line_count_before = await db.scalar(text("""
            SELECT count(*) FROM payroll.PayrollDraftLines
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})
    assert heads_before == [(hours["pay_definition_id"], hours_head_rule, 1),
                            (miles["pay_definition_id"], miles_head_rule, 1)]

    changed_import = await upload_source("changed-source.csv", ["Hours", "TotalDistance"])
    changed_metadata = await p3c_client.get(
        f"/payroll/imports/{changed_import['import_id']}", headers=tenant.admin)
    assert changed_metadata.status_code == 200, changed_metadata.text
    state_by_target = {item["target"]: item["rule_state"]
                       for item in changed_metadata.json()["mapping_targets"]
                       if item["target"].startswith("Definition:")}
    hours_state = state_by_target[f"Definition:{hours_ppd}"]
    miles_state = state_by_target[f"Definition:{miles_ppd}"]
    assert hours_state["pending_rule_version_id"] == hours_head_rule
    assert miles_state["pending_rule_version_id"] is None
    assert miles_state["state"] == "missing_inputs"

    remapped = await p3c_client.put(
        f"/payroll/imports/{changed_import['import_id']}/mapping",
        json={"expected_revision": changed_import["revision"],
              "shared_columns": {"DriverNumber": {"header": "DriverNumber"},
                                 "WorkDate": {"header": "WorkDate"}},
              "targets": [{"target": f"Definition:{miles_ppd}", "transform": "Direct",
                           "inputs": [{"header": "TotalDistance"}],
                           "expected_head_revision": 1, "replace_existing": True}]},
        headers=tenant.admin)
    assert remapped.status_code == 200, remapped.text
    async with p3c_engine.connect() as db:
        pending_result = await db.execute(text("""
            SELECT p.PayDefinitionID, p.RuleVersionID, p.ExpectedHeadRevision,
                   p.PayrollPeriodDefinitionID, p.RateDefinitionID
            FROM import.PayrollWorkPendingRules p
            WHERE p.ImportID=:iid AND p.CompanyID=:cid
        """), {"iid": changed_import["import_id"], "cid": tenant.company_id})
        pending = {row["paydefinitionid"]: row for row in pending_result.mappings()}
        heads_after = (await db.execute(text("""
            SELECT PayDefinitionID, RuleVersionID, Revision
            FROM import.PayrollWorkRuleHeads
            WHERE CompanyID=:cid AND BranchID=:bid
            ORDER BY PayDefinitionID
        """), {"cid": tenant.company_id, "bid": period["branch_id"]})).all()
        created_versions = await db.scalar(text("""
            SELECT count(*) FROM import.PayrollWorkRuleVersions
            WHERE CompanyID=:cid AND OriginImportID=:iid
        """), {"cid": tenant.company_id, "iid": changed_import["import_id"]})
        line_count_after = await db.scalar(text("""
            SELECT count(*) FROM payroll.PayrollDraftLines
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})
    assert pending[hours["pay_definition_id"]]["ruleversionid"] == hours_head_rule
    assert pending[hours["pay_definition_id"]]["expectedheadrevision"] == 1
    assert pending[miles["pay_definition_id"]]["ruleversionid"] != miles_head_rule
    assert pending[miles["pay_definition_id"]]["expectedheadrevision"] == 1
    assert pending[miles["pay_definition_id"]]["payrollperioddefinitionid"] == miles_ppd
    assert heads_after == heads_before
    assert created_versions == 1
    assert line_count_after == line_count_before


async def test_applied_change_and_import_source_roll_back_when_import_does_not_complete(
    p3c_client, p3c_engine, tenant,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, _target_period_definition_id, definition_id, ppd_id, rate_id, rule_id, preview = await _mapped_preview_with_rule(
        p3c_client, p3c_engine, tenant, period)
    with pytest.raises(DBAPIError, match="IMPORT_APPLIED_CHANGE_PENDING"):
        await _write_source_change(p3c_engine, tenant, period, import_id=created["import_id"],
            revision=preview["revision"], plan_fingerprint=preview["preview"]["plan_fingerprint"],
            definition_id=definition_id, ppd_id=ppd_id, rate_id=rate_id, rule_id=rule_id,
            complete=False)
    async with p3c_engine.connect() as db:
        rolled_back = (await db.execute(text("""
            SELECT i.Status,
                (SELECT count(*) FROM import.PayrollWorkAppliedChanges a
                 WHERE a.ImportID=i.ImportID AND a.CompanyID=i.CompanyID),
                (SELECT count(*) FROM payroll.PayrollDraftLines l
                 WHERE l.CompanyID=i.CompanyID AND l.PayrollPeriodID=w.PayrollPeriodID
                   AND l.SourceType='Import'),
                (SELECT count(*) FROM payroll.PayrollPeriodAuditEvidenceEvents e
                 WHERE e.CompanyID=i.CompanyID AND e.PayrollPeriodID=w.PayrollPeriodID
                   AND e.CorrelationID=i.CorrelationID)
            FROM import.Imports i JOIN import.PayrollWorkImports w
              ON w.ImportID=i.ImportID AND w.CompanyID=i.CompanyID
            WHERE i.ImportID=:iid AND i.CompanyID=:cid
        """), {"iid": created["import_id"], "cid": tenant.company_id})).one()
    assert rolled_back == ("NeedsReview", 0, 0, 0)


async def test_applied_change_rejects_nullable_or_incomplete_source_evidence(
    p3c_client, p3c_engine, tenant,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, _target_period_definition_id, definition_id, ppd_id, rate_id, rule_id, preview = await _mapped_preview_with_rule(
        p3c_client, p3c_engine, tenant, period)
    line_id, evidence_id, assignment_id, work_date, _correlation = await _write_source_change(
        p3c_engine, tenant, period, import_id=created["import_id"], revision=preview["revision"],
        plan_fingerprint=preview["preview"]["plan_fingerprint"], definition_id=definition_id,
        ppd_id=ppd_id, rate_id=rate_id, rule_id=rule_id, complete=False, insert_link=False)

    async with p3c_engine.connect() as db:
        driver_id = (await db.execute(text("""
            SELECT DriverID FROM payroll.PayrollPeriodAuditEvidenceEvents
            WHERE PayrollPeriodAuditEvidenceEventID=:eid
        """), {"eid": evidence_id})).scalar_one()
    invalid_events = (("CorrelationID", _correlation), ("SourceRevision", preview["revision"]),
                      ("AfterStateJson", None))
    for column, original_value in invalid_events:
        async with p3c_engine.begin() as db:
            async with suspended_test_triggers(db, (TriggerIdentity(
                "payroll", "payrollperiodauditevidenceevents",
                "trg_payrollperiodauditevidenceevents_immutable",
            ),)):
                if column == "AfterStateJson":
                    await db.execute(text("UPDATE payroll.PayrollPeriodAuditEvidenceEvents SET AfterStateJson=AfterStateJson - 'pay_definition_id' WHERE PayrollPeriodAuditEvidenceEventID=:eid"),
                                     {"eid": evidence_id})
                else:
                    await db.execute(text(f"UPDATE payroll.PayrollPeriodAuditEvidenceEvents SET {column}=NULL WHERE PayrollPeriodAuditEvidenceEventID=:eid"),
                                     {"eid": evidence_id})
        with pytest.raises(DBAPIError, match="IMPORT_APPLIED_CHANGE_EVIDENCE"):
            async with p3c_engine.begin() as db:
                await db.execute(text("""
                    INSERT INTO import.PayrollWorkAppliedChanges
                        (ImportID, CompanyID, BranchID, PayrollPeriodID, PayDefinitionID,
                         PayrollPeriodDefinitionID, RateDefinitionID, RuleVersionID, DriverID,
                         DriverAssignmentID, WorkDate, DraftLineID, EvidenceEventID, ReviewedRevision,
                         PlanFingerprint, ContractVersion, Action)
                    VALUES (:iid, :cid, :bid, :pid, :definition, :ppd, :rate, :rule, :driver,
                            :assignment, :workdate, :line, :evidence, :revision, :fingerprint, 1, 'Created')
                """), {"iid": created["import_id"], "cid": tenant.company_id, "bid": period["branch_id"],
                       "pid": period["payroll_period_id"], "definition": definition_id,
                       "ppd": ppd_id, "rate": rate_id, "rule": rule_id,
                       "driver": driver_id,
                       "assignment": assignment_id, "workdate": work_date, "line": line_id,
                       "evidence": evidence_id, "revision": preview["revision"],
                       "fingerprint": preview["preview"]["plan_fingerprint"]})
        async with p3c_engine.begin() as db:
            async with suspended_test_triggers(db, (TriggerIdentity(
                "payroll", "payrollperiodauditevidenceevents",
                "trg_payrollperiodauditevidenceevents_immutable",
            ),)):
                if column == "AfterStateJson":
                    await db.execute(text("""
                        UPDATE payroll.PayrollPeriodAuditEvidenceEvents SET AfterStateJson=AfterStateJson ||
                        jsonb_build_object('pay_definition_id', CAST(:definition AS INTEGER),
                                           'payroll_period_definition_id', CAST(:ppd AS BIGINT),
                                           'rate_definition_id', CAST(:rate AS INTEGER))
                        WHERE PayrollPeriodAuditEvidenceEventID=:eid
                    """), {"eid": evidence_id, "definition": definition_id, "ppd": ppd_id, "rate": rate_id})
                else:
                    await db.execute(text(f"UPDATE payroll.PayrollPeriodAuditEvidenceEvents SET {column}=:value WHERE PayrollPeriodAuditEvidenceEventID=:eid"),
                                     {"eid": evidence_id,
                                      "value": original_value})
    assert assignment_id > 0


async def test_forward_upgrade_preserves_populated_0095_import_and_payroll_evidence(pg_instance):
    from scripts.import_benchmark_support import application, seed_period

    with _disposable_database(pg_instance, "import_rule_lifecycle_0095") as (env, dsn):
        upgraded = _alembic(env, "upgrade", "0095")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        async with application(dsn) as (client, engine, _app):
            fixture = await seed_period(client, engine, dsn, drivers=2, definitions=1, days=7)
            tenant, period = fixture["tenant"], fixture["period"]
            line = await client.post(
                f"/payroll/periods/{period['payroll_period_id']}/lines", headers=tenant.admin,
                json={"driver_assignment_id": fixture["drivers"][0]["driverassignmentid"],
                      "work_date": fixture["days"][0].isoformat(),
                      "payroll_period_definition_id": fixture["definitions"][0].payroll_period_definition_id,
                      "quantity": "3", "notes": "retained before lifecycle upgrade"},
            )
            assert line.status_code == 201, line.text
            async with engine.begin() as db:
                import_id = await db.scalar(text("""
                    INSERT INTO import.Imports
                        (CompanyID, BranchID, ImportKind, SourceKind, CorrelationID, IdempotencyKey,
                         CreationFingerprint, CreatedByUserID, UpdatedByUserID)
                    VALUES (:cid, :bid, 'PayrollWork', 'File', :correlation, :idempotency,
                            :fingerprint, :uid, :uid) RETURNING ImportID
                """), {"cid": tenant.company_id, "bid": period["branch_id"],
                       "correlation": str(uuid4()), "idempotency": str(uuid4()),
                       "fingerprint": "c" * 64, "uid": tenant.owner})
                await db.execute(text("""
                    INSERT INTO import.PayrollWorkImports
                        (ImportID, CompanyID, BranchID, PayrollPeriodID, ContractVersion, CanonicalizationVersion)
                    VALUES (:iid, :cid, :bid, :pid, 2, 3)
                """), {"iid": import_id, "cid": tenant.company_id, "bid": period["branch_id"],
                       "pid": period["payroll_period_id"]})
                await db.execute(text("""
                    INSERT INTO import.FileSourceBatches (ImportID, CompanyID, ContractVersion, SourceManifest)
                    VALUES (:iid, :cid, 2, CAST(:manifest AS jsonb))
                """), {"iid": import_id, "cid": tenant.company_id,
                       "manifest": '[{"name":"preserved.csv","type":"csv","sha256":"' + "d" * 64
                                   + '","size":1,"units":[{"name":"preserved.csv","index":1}]}]'})
                file_id = await db.scalar(text("""
                    INSERT INTO import.FileSources
                        (ImportID, CompanyID, FileOrdinal, ContractVersion, OriginalFileName,
                         FileType, FileSizeBytes, FileHashSha256, FileContent, InspectionMetadata)
                    VALUES (:iid, :cid, 1, 2, 'preserved.csv', 'csv', 1, :hash, '\x61', '{}')
                    RETURNING FileSourceID
                """), {"iid": import_id, "cid": tenant.company_id, "hash": "d" * 64})
                await db.execute(text("""
                    INSERT INTO import.FileSourceUnits
                        (FileSourceID, ImportID, CompanyID, UnitName, UnitOrdinal, UnitKind,
                         IsSelected, HeaderRowNumber, DateSource)
                    VALUES (:file_id, :iid, :cid, 'preserved.csv', 1, 'CSV', TRUE, 1, '{}')
                """), {"file_id": file_id, "iid": import_id, "cid": tenant.company_id})
                await db.execute(text("""
                    UPDATE import.Imports SET Status='NeedsReview', Revision=Revision+1,
                        UpdatedByUserID=:uid, UpdatedAtUtc=NOW()
                    WHERE ImportID=:iid AND CompanyID=:cid
                """), {"uid": tenant.owner, "iid": import_id, "cid": tenant.company_id})
                await db.execute(text("""
                    UPDATE import.Imports SET Status='Completed', Revision=Revision+1,
                        CompletedByUserID=:uid, CompletedAtUtc=NOW(), UpdatedByUserID=:uid, UpdatedAtUtc=NOW()
                    WHERE ImportID=:iid AND CompanyID=:cid
                """), {"uid": tenant.owner, "iid": import_id, "cid": tenant.company_id})
            async with engine.connect() as db:
                before = {
                    "import": await db.scalar(text("SELECT to_jsonb(i) FROM import.Imports i WHERE ImportID=:iid"),
                                               {"iid": import_id}),
                    "source": await db.scalar(text("SELECT to_jsonb(f) FROM import.FileSources f WHERE FileSourceID=:fid"),
                                               {"fid": file_id}),
                    "target": await db.scalar(text("SELECT to_jsonb(w) FROM import.PayrollWorkImports w WHERE ImportID=:iid"),
                                               {"iid": import_id}),
                    "payroll": await db.scalar(text("SELECT to_jsonb(l) FROM payroll.PayrollDraftLines l WHERE DraftLineID=:id"),
                                                {"id": line.json()["draft_line_id"]}),
                }
                evidence_before = await db.scalar(text("""
                    SELECT count(*) FROM payroll.PayrollPeriodAuditEvidenceEvents
                    WHERE PayrollPeriodID=:pid AND CompanyID=:cid
                """), {"pid": period["payroll_period_id"], "cid": tenant.company_id})

        result = _alembic(env, "upgrade", "0096")
        assert result.returncode == 0, result.stdout + result.stderr
        import psycopg2
        with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_jsonb(i) FROM import.Imports i WHERE ImportID=%s", (import_id,))
            assert cur.fetchone()[0] == before["import"]
            cur.execute("SELECT to_jsonb(f) FROM import.FileSources f WHERE FileSourceID=%s", (file_id,))
            assert cur.fetchone()[0] == before["source"]
            cur.execute("SELECT to_jsonb(w) FROM import.PayrollWorkImports w WHERE ImportID=%s", (import_id,))
            assert cur.fetchone()[0] == before["target"]
            cur.execute("SELECT to_jsonb(l) FROM payroll.PayrollDraftLines l WHERE DraftLineID=%s",
                        (line.json()["draft_line_id"],))
            assert cur.fetchone()[0] == before["payroll"]
            cur.execute("""SELECT count(*) FROM payroll.PayrollPeriodAuditEvidenceEvents
                WHERE PayrollPeriodID=%s AND CompanyID=%s""", (period["payroll_period_id"], tenant.company_id))
            assert cur.fetchone()[0] == evidence_before
            for table, import_column in (
                ("FileCanonicalData", "ImportID"), ("PayrollWorkRuleVersions", "OriginImportID"),
                ("PayrollWorkPendingRules", "ImportID"), ("PayrollWorkRuleHeads", "ActivatedByImportID"),
                ("PayrollWorkReviewDecisions", "ImportID"), ("PayrollWorkAppliedChanges", "ImportID"),
                ("PayrollWorkPreviewRuleContexts", "ImportID"),
            ):
                cur.execute(f"SELECT count(*) FROM import.{table} WHERE {import_column}=%s", (import_id,))
                assert cur.fetchone()[0] == 0, table
            cur.execute("SELECT version_num FROM public.alembic_version")
            assert cur.fetchone()[0] == "0096"


async def test_period_day_snapshot_mismatch_preserves_existing_preview_and_pending_rules(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    from datetime import timedelta

    from app.imports import batch_service

    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, _target_id, definition_id, _ppd_id, _rate_id, _rule_id, old_preview = \
        await _mapped_preview_with_rule(p3c_client, p3c_engine, tenant, period)
    import_id = created["import_id"]
    async with p3c_engine.connect() as db:
        old_state = (await db.execute(text("""
            SELECT w.PlanFingerprint, i.Revision,
                (SELECT count(*) FROM import.PayrollWorkPreviewGroups g
                 WHERE g.ImportID=i.ImportID AND g.CompanyID=i.CompanyID)
            FROM import.Imports i JOIN import.PayrollWorkImports w
              ON w.ImportID=i.ImportID AND w.CompanyID=i.CompanyID
            WHERE i.ImportID=:iid AND i.CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).one()
        old_pending = (await db.execute(text("""
            SELECT PayDefinitionID, RuleVersionID, Revision
            FROM import.PayrollWorkPendingRules WHERE ImportID=:iid AND CompanyID=:cid
            ORDER BY PayDefinitionID
        """), {"iid": import_id, "cid": tenant.company_id})).all()
        old_line_count = await db.scalar(text("""
            SELECT count(*) FROM payroll.PayrollDraftLines
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})
    original_load_context = batch_service.load_context
    injected = False

    async def changed_day_snapshot(*args, **kwargs):
        nonlocal injected
        context = await original_load_context(*args, **kwargs)
        if not injected:
            context["days"] = {**context["days"], context["period"]["end"] + timedelta(days=1): False}
            injected = True
        return context

    monkeypatch.setattr(batch_service, "load_context", changed_day_snapshot)
    rejected = await p3c_client.post(
        f"/payroll/imports/{import_id}/preview",
        json={"expected_revision": old_preview["revision"]}, headers=tenant.admin)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "IMPORT_CONTEXT_STALE"
    assert injected is True

    query = (f"expected_revision={old_preview['revision']}&"
             f"plan_fingerprint={old_preview['preview']['plan_fingerprint']}")
    old_rows = await p3c_client.get(f"/payroll/imports/{import_id}/rows?{query}", headers=tenant.admin)
    assert old_rows.status_code == 200, old_rows.text
    async with p3c_engine.connect() as db:
        new_state = (await db.execute(text("""
            SELECT w.PlanFingerprint, i.Revision,
                (SELECT count(*) FROM import.PayrollWorkPreviewGroups g
                 WHERE g.ImportID=i.ImportID AND g.CompanyID=i.CompanyID)
            FROM import.Imports i JOIN import.PayrollWorkImports w
              ON w.ImportID=i.ImportID AND w.CompanyID=i.CompanyID
            WHERE i.ImportID=:iid AND i.CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).one()
        new_pending = (await db.execute(text("""
            SELECT PayDefinitionID, RuleVersionID, Revision
            FROM import.PayrollWorkPendingRules WHERE ImportID=:iid AND CompanyID=:cid
            ORDER BY PayDefinitionID
        """), {"iid": import_id, "cid": tenant.company_id})).all()
        new_line_count = await db.scalar(text("""
            SELECT count(*) FROM payroll.PayrollDraftLines
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})
        unchanged_head = await db.execute(text("""
            SELECT RuleVersionID, Revision FROM import.PayrollWorkRuleHeads
            WHERE CompanyID=:cid AND BranchID=:bid AND PayDefinitionID=:definition
        """), {"cid": tenant.company_id, "bid": period["branch_id"],
               "definition": definition_id})
        unchanged_head = unchanged_head.one_or_none()
    assert new_state == old_state
    assert new_pending == old_pending
    assert new_line_count == old_line_count
    assert unchanged_head is None


async def test_completed_filter_rule_reuse_and_explicit_reset_survive_origin_purge(
    p3c_client, p3c_engine, tenant,
):
    from tests.calculation_snapshot_helpers import finalize, submit_and_approve

    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver(p3c_engine, tenant, period)
    target_period_id, pay_definition_id, code = await _definition(p3c_engine, tenant, period)
    initial_csv = (f"DriverNumber,WorkDate,{code},Category\n"
                   f"{driver['drivernumber']},{driver['workdate'].isoformat()},1,Trip\n"
                   f"{driver['drivernumber']},{driver['workdate'].isoformat()},2,Trip\n").encode()
    upload = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/imports",
        files={"file": ("filter-head.csv", initial_csv, "text/csv")},
        headers={**tenant.admin, "Idempotency-Key": str(uuid4())})
    assert upload.status_code == 200, upload.text
    created = upload.json()
    mapped = await map_import_columns(p3c_client, tenant, created, {
        "1": "DriverNumber", "2": "WorkDate", "3": f"Definition:{target_period_id}"})
    assert mapped.status_code == 200, mapped.text
    selected = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/row-handling",
        json={"expected_revision": mapped.json()["revision"], "mode": "FilterThenSum",
              "filter_column": {"header": "Category"},
              "choices": [{"category": {"type": "text", "value": "Trip"}, "decision": "Include"}]},
        headers=tenant.admin)
    assert selected.status_code == 200, selected.text
    preview = await p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": selected.json()["revision"]}, headers=tenant.admin)
    assert preview.status_code == 200, preview.text
    stable_id, frozen_ppd, rate_id = await _target(
        p3c_engine, tenant, period, target_period_id)
    assert stable_id == pay_definition_id
    async with p3c_engine.connect() as db:
        rule_id = await db.scalar(text("""
            SELECT p.RuleVersionID FROM import.PayrollWorkPendingRules p
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": created["import_id"], "cid": tenant.company_id,
               "definition": pay_definition_id})
    async with p3c_engine.begin() as db:
        await db.execute(text("""
            INSERT INTO import.PayrollWorkReviewDecisions
                (ImportID, CompanyID, BranchID, PayrollPeriodID, DecisionKey, ContractVersion,
                 ReviewedRevision, PlanFingerprint, DecisionKind, Choice, BasisJSON,
                 PayrollPeriodDefinitionID, PayDefinitionID, RateDefinitionID, ActorUserID)
            VALUES (:iid,:cid,:bid,:pid,'reviewed-filter-target',1,:revision,:fingerprint,
                    'MappingReview','Keep','{}'::jsonb,:ppd,:definition,:rate,:uid)
        """), {"iid": created["import_id"], "cid": tenant.company_id,
               "bid": period["branch_id"], "pid": period["payroll_period_id"],
               "revision": preview.json()["revision"],
               "fingerprint": preview.json()["preview"]["plan_fingerprint"],
               "ppd": frozen_ppd, "definition": stable_id, "rate": rate_id, "uid": tenant.owner})
    await _write_source_change(
        p3c_engine, tenant, period, import_id=created["import_id"],
        revision=preview.json()["revision"], plan_fingerprint=preview.json()["preview"]["plan_fingerprint"],
        definition_id=stable_id, ppd_id=frozen_ppd, rate_id=rate_id, rule_id=rule_id, complete=True)
    await submit_and_approve(p3c_client, tenant, period)
    finalized = await finalize(p3c_client, tenant, period)
    assert finalized.status_code == 200, finalized.text
    async with p3c_engine.connect() as db:
        purged = await db.scalar(text("""
            SELECT c.Payload IS NULL AND c.PurgedAtUtc IS NOT NULL
            FROM import.FileCanonicalData c WHERE c.ImportID=:iid AND c.CompanyID=:cid
        """), {"iid": created["import_id"], "cid": tenant.company_id})
        completed_head = (await db.execute(text("""
            SELECT h.RuleVersionID, h.Revision, r.RulePayload
            FROM import.PayrollWorkRuleHeads h
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=h.RuleVersionID
              AND r.CompanyID=h.CompanyID AND r.BranchID=h.BranchID AND r.PayDefinitionID=h.PayDefinitionID
            WHERE h.CompanyID=:cid AND h.BranchID=:bid AND h.PayDefinitionID=:definition
        """), {"cid": tenant.company_id, "bid": period["branch_id"],
               "definition": pay_definition_id})).one()
    assert purged is True
    assert completed_head.rulepayload["row_handling"]["mode"] == "FilterThenSum"

    next_period = await create_period(p3c_client, tenant, tenant.branch_a)
    next_driver = await _eligible_driver(p3c_engine, tenant, next_period)
    next_target, _, next_code = await _definition(p3c_engine, tenant, next_period)
    later_csv = (f"DriverNumber,WorkDate,{next_code},Category\n"
                 f"{next_driver['drivernumber']},{next_driver['workdate'].isoformat()},4,Trip\n").encode()
    later_upload = await p3c_client.post(
        f"/payroll/periods/{next_period['payroll_period_id']}/imports",
        files={"file": ("single-category.csv", later_csv, "text/csv")},
        headers={**tenant.admin, "Idempotency-Key": str(uuid4())})
    assert later_upload.status_code == 200, later_upload.text
    later = later_upload.json()
    async with p3c_engine.connect() as db:
        before_later_payroll = await db.scalar(text("""
            SELECT count(*) FROM payroll.PayrollDraftLines
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid
        """), {"cid": tenant.company_id, "pid": next_period["payroll_period_id"]})
        reused = (await db.execute(text("""
            SELECT p.RuleVersionID, p.ExpectedHeadRevision, r.RulePayload
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": later["import_id"], "cid": tenant.company_id,
               "definition": pay_definition_id})).one()
    assert reused.expectedheadrevision == completed_head.revision
    assert reused.rulepayload["row_handling"]["choices"] == completed_head.rulepayload["row_handling"]["choices"]
    inspected = await p3c_client.post(
        f"/payroll/imports/{later['import_id']}/categories/inspect",
        json={"expected_revision": later["revision"], "column": {"header": "Category"}, "limit": 1},
        headers=tenant.admin)
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["suitable"] is True
    assert inspected.json()["items"][0]["previous_decision"] == "Include"

    reset = await p3c_client.put(
        f"/payroll/imports/{later['import_id']}/row-handling",
        json={"expected_revision": later["revision"], "mode": "RejectRepeated"}, headers=tenant.admin)
    assert reset.status_code == 200, reset.text
    reset_mapping = await p3c_client.put(
        f"/payroll/imports/{later['import_id']}/mapping",
        json={"expected_revision": reset.json()["revision"],
              "shared_columns": {"DriverNumber": {"header": "DriverNumber"},
                                 "WorkDate": {"header": "WorkDate"}},
              "targets": [{"target": f"Definition:{next_target}", "transform": "Direct",
                           "inputs": [{"header": next_code}], "expected_head_revision": completed_head.revision,
                           "replace_existing": True}]}, headers=tenant.admin)
    assert reset_mapping.status_code == 200, reset_mapping.text
    assert reset_mapping.json()["revision"] == reset.json()["revision"] + 1
    async with p3c_engine.connect() as db:
        after_reset = (await db.execute(text("""
            SELECT p.RuleVersionID, p.Revision, r.RulePayload
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": later["import_id"], "cid": tenant.company_id,
               "definition": pay_definition_id})).one()
        current_head = await db.scalar(text("""
            SELECT RuleVersionID FROM import.PayrollWorkRuleHeads
            WHERE CompanyID=:cid AND BranchID=:bid AND PayDefinitionID=:definition
        """), {"cid": tenant.company_id, "bid": next_period["branch_id"],
               "definition": pay_definition_id})
    assert "row_handling" not in after_reset.rulepayload
    assert "Filter" not in after_reset.rulepayload["shared_columns"]
    assert "date_interpretation" in after_reset.rulepayload
    assert current_head == completed_head.ruleversionid

    proof = await p3c_client.post(
        f"/payroll/imports/{later['import_id']}/categories/inspect",
        json={"expected_revision": reset_mapping.json()["revision"],
              "column": {"header": "Category"}, "limit": 1}, headers=tenant.admin)
    assert proof.status_code == 200, proof.text
    assert proof.json()["suitable"] is True
    assert proof.json()["items"][0]["previous_decision"] is None
    reselected = await p3c_client.put(
        f"/payroll/imports/{later['import_id']}/row-handling",
        json={"expected_revision": reset_mapping.json()["revision"], "mode": "FilterThenSum",
              "filter_column": {"header": "Category"}, "choices": []}, headers=tenant.admin)
    assert reselected.status_code == 200, reselected.text
    refreshed = await p3c_client.post(
        f"/payroll/imports/{later['import_id']}/preview",
        json={"expected_revision": reselected.json()["revision"]}, headers=tenant.admin)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["preview"]["counts"]["unresolved"] == 1
    async with p3c_engine.connect() as db:
        final_head = await db.scalar(text("""
            SELECT RuleVersionID FROM import.PayrollWorkRuleHeads
            WHERE CompanyID=:cid AND BranchID=:bid AND PayDefinitionID=:definition
        """), {"cid": tenant.company_id, "bid": next_period["branch_id"],
               "definition": pay_definition_id})
        final_payload = await db.scalar(text("""
            SELECT r.RulePayload FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": later["import_id"], "cid": tenant.company_id,
               "definition": pay_definition_id})
    assert final_head == completed_head.ruleversionid
    assert final_payload["row_handling"]["choices"] == []
    assert "date_interpretation" in final_payload
    async with p3c_engine.connect() as db:
        after_later_payroll = await db.scalar(text("""
            SELECT count(*) FROM payroll.PayrollDraftLines
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid
        """), {"cid": tenant.company_id, "pid": next_period["payroll_period_id"]})
    assert after_later_payroll == before_later_payroll

    repair_period = next_period
    repair_driver = next_driver
    repair_code = next_code
    missing_filter_csv = (f"DriverNumber,WorkDate,{repair_code},Activity\n"
                          f"{repair_driver['drivernumber']},{repair_driver['workdate'].isoformat()},1,Trip\n"
                          f"{repair_driver['drivernumber']},{repair_driver['workdate'].isoformat()},2,Trip\n").encode()
    missing_filter_upload = await p3c_client.post(
        f"/payroll/periods/{repair_period['payroll_period_id']}/imports",
        files={"file": ("missing-filter.csv", missing_filter_csv, "text/csv")},
        headers={**tenant.admin, "Idempotency-Key": str(uuid4())})
    assert missing_filter_upload.status_code == 200, missing_filter_upload.text
    missing_filter = missing_filter_upload.json()
    async with p3c_engine.connect() as db:
        preserved = (await db.execute(text("""
            SELECT r.RulePayload, p.RuleVersionID, p.ExpectedHeadRevision
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": missing_filter["import_id"], "cid": tenant.company_id,
               "definition": pay_definition_id})).one()
    assert preserved.rulepayload["row_handling"] == completed_head.rulepayload["row_handling"]
    assert preserved.rulepayload["shared_columns"]["Filter"] == {"header": "category"}
    assert preserved.rulepayload["inputs"] == completed_head.rulepayload["inputs"]
    assert preserved.rulepayload["date_interpretation"] == completed_head.rulepayload["date_interpretation"]
    assert preserved.expectedheadrevision == completed_head.revision
    blocked_preview = await p3c_client.post(
        f"/payroll/imports/{missing_filter['import_id']}/preview",
        json={"expected_revision": missing_filter["revision"]}, headers=tenant.admin)
    assert blocked_preview.status_code == 409, blocked_preview.text
    assert blocked_preview.json()["detail"]["code"] == "IMPORT_RULE_PAYLOAD_UNSUPPORTED"
    repaired_filter = await p3c_client.put(
        f"/payroll/imports/{missing_filter['import_id']}/row-handling",
        json={"expected_revision": missing_filter["revision"], "mode": "FilterThenSum",
              "filter_column": {"header": "Activity"},
              "choices": [{"category": {"type": "text", "value": "Trip"}, "decision": "Include"}]},
        headers=tenant.admin)
    assert repaired_filter.status_code == 200, repaired_filter.text
    async with p3c_engine.connect() as db:
        repaired_payload = await db.scalar(text("""
            SELECT r.RulePayload FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": missing_filter["import_id"], "cid": tenant.company_id,
               "definition": pay_definition_id})
    assert repaired_payload["shared_columns"]["Filter"] == {"header": "activity"}
    assert repaired_payload["inputs"] == preserved.rulepayload["inputs"]
    assert repaired_payload["date_interpretation"] == preserved.rulepayload["date_interpretation"]
    repaired_preview = await p3c_client.post(
        f"/payroll/imports/{missing_filter['import_id']}/preview",
        json={"expected_revision": repaired_filter.json()["revision"]}, headers=tenant.admin)
    assert repaired_preview.status_code == 200, repaired_preview.text
    unit_id = missing_filter["files"][0]["units"][0]["source_unit_id"]
    date_refresh = await p3c_client.put(
        f"/payroll/imports/{missing_filter['import_id']}/interpretation",
        json={"expected_revision": repaired_preview.json()["revision"], "units": [{
            "source_unit_id": unit_id, "selected": True,
            "date_source": {"kind": "filename", "value": repair_driver["workdate"].isoformat()},
        }]}, headers=tenant.admin)
    assert date_refresh.status_code == 200, date_refresh.text
    date_refreshed_preview = await p3c_client.post(
        f"/payroll/imports/{missing_filter['import_id']}/preview",
        json={"expected_revision": date_refresh.json()["revision"]}, headers=tenant.admin)
    assert date_refreshed_preview.status_code == 200, date_refreshed_preview.text
    async with p3c_engine.connect() as db:
        after_date_refresh = await db.scalar(text("""
            SELECT r.RulePayload FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": missing_filter["import_id"], "cid": tenant.company_id,
               "definition": pay_definition_id})
    assert after_date_refresh["row_handling"] == repaired_payload["row_handling"]
    assert after_date_refresh["date_interpretation"]["profiles"][0]["authority"] == "filename"


async def test_conflicting_completed_filter_decisions_require_explicit_source_choice(
    p3c_client, p3c_engine, tenant,
):
    first_definition = await rated_definition(p3c_client, tenant, definition_code=f"FILTER_A_{uuid4().hex[:8]}")
    second_definition = await rated_definition(p3c_client, tenant, definition_code=f"FILTER_B_{uuid4().hex[:8]}")
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    driver = await _eligible_driver(p3c_engine, tenant, period)
    from app.imports.mapping import targets as mapping_targets
    from app.payroll.period_definitions import list_period_definitions
    async with p3c_engine.connect() as db:
        definitions = await list_period_definitions(period["payroll_period_id"], tenant.company_id,
                                                    db, active_only=True)
    targets = {item["code"]: item for item in mapping_targets(definitions) if item.get("code")}
    target_choices = [targets[first_definition["definition_code"]],
                      targets[second_definition["definition_code"]]]
    assert len(target_choices) == 2
    first_target, second_target = target_choices
    first_definition_id = int(first_target["target"].split(":", 1)[1])
    second_definition_id = int(second_target["target"].split(":", 1)[1])
    csv = (f"DriverNumber,WorkDate,Category,{first_target['code']},{second_target['code']}\n"
           f"{driver['drivernumber']},{driver['workdate'].isoformat()},Trip,1,2\n"
           f"{driver['drivernumber']},{driver['workdate'].isoformat()},Trip,3,4\n").encode()

    async def complete_target(target, decision, filename):
        target_csv = (f"DriverNumber,WorkDate,Category,{target['code']}\n"
                      f"{driver['drivernumber']},{driver['workdate'].isoformat()},Trip,1\n"
                      f"{driver['drivernumber']},{driver['workdate'].isoformat()},Trip,3\n").encode()
        uploaded = await p3c_client.post(
            f"/payroll/periods/{period['payroll_period_id']}/imports",
            files={"file": (filename, target_csv, "text/csv")},
            headers={**tenant.admin, "Idempotency-Key": str(uuid4())})
        assert uploaded.status_code == 200, uploaded.text
        created = uploaded.json()
        target_ppd = first_definition_id if target == first_target else second_definition_id
        mapping = {"1": "DriverNumber", "2": "WorkDate", "4": f"Definition:{target_ppd}"}
        mapped = await map_import_columns(p3c_client, tenant, created, mapping)
        assert mapped.status_code == 200, mapped.text
        selected = await p3c_client.put(
            f"/payroll/imports/{created['import_id']}/row-handling",
            json={"expected_revision": mapped.json()["revision"], "mode": "FilterThenSum",
                  "filter_column": {"header": "Category"},
                  "choices": [{"category": {"type": "text", "value": "Trip"},
                               "decision": decision}]}, headers=tenant.admin)
        assert selected.status_code == 200, selected.text
        preview = await p3c_client.post(
            f"/payroll/imports/{created['import_id']}/preview",
            json={"expected_revision": selected.json()["revision"]}, headers=tenant.admin)
        assert preview.status_code == 200, preview.text
        stable_id, ppd_id, rate_id = await _target(p3c_engine, tenant, period, target_ppd)
        async with p3c_engine.connect() as db:
            rule_id = await db.scalar(text("""
                SELECT RuleVersionID FROM import.PayrollWorkPendingRules
                WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
            """), {"iid": created["import_id"], "cid": tenant.company_id,
                   "definition": stable_id})
        await _write_source_change(
            p3c_engine, tenant, period, import_id=created["import_id"],
            revision=preview.json()["revision"], plan_fingerprint=preview.json()["preview"]["plan_fingerprint"],
            definition_id=stable_id, ppd_id=ppd_id, rate_id=rate_id, rule_id=rule_id, complete=True)
        return stable_id

    first_id = await complete_target(first_target, "Include", "filter-include-head.csv")
    second_id = await complete_target(second_target, "Exclude", "filter-exclude-head.csv")
    assert first_id != second_id

    receiving = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/imports",
        files={"file": ("conflicting-filter-heads.csv", csv, "text/csv")},
        headers={**tenant.admin, "Idempotency-Key": str(uuid4())})
    assert receiving.status_code == 200, receiving.text
    created = receiving.json()
    receiving_id = created["import_id"]
    async with p3c_engine.connect() as db:
        before = (await db.execute(text("""
            SELECT p.PayDefinitionID, p.RuleVersionID, p.Revision, p.ExpectedHeadRevision,
                   r.RulePayload
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid ORDER BY p.PayDefinitionID
        """), {"iid": receiving_id, "cid": tenant.company_id})).all()
        heads_before = (await db.execute(text("""
            SELECT h.PayDefinitionID, h.RuleVersionID, h.Revision
            FROM import.PayrollWorkRuleHeads h
            WHERE h.CompanyID=:cid AND h.BranchID=:bid AND h.PayDefinitionID=ANY(:definitions)
            ORDER BY h.PayDefinitionID
        """), {"cid": tenant.company_id, "bid": period["branch_id"],
               "definitions": [first_id, second_id]})).all()
        payroll_before = await db.scalar(text("""
            SELECT count(*) FROM payroll.PayrollDraftLines
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})
    assert {row.rulepayload["row_handling"]["choices"][0]["decision"] for row in before} == {
        "Include", "Exclude"}
    conflict = await p3c_client.put(
        f"/payroll/imports/{receiving_id}/row-handling",
        json={"expected_revision": created["revision"], "mode": "FilterThenSum",
              "filter_column": {"header": "Category"}, "choices": []}, headers=tenant.admin)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IMPORT_ROW_HANDLING_CONFLICT"
    async with p3c_engine.connect() as db:
        unchanged = (await db.execute(text("""
            SELECT p.PayDefinitionID, p.RuleVersionID, p.Revision, p.ExpectedHeadRevision,
                   r.RulePayload
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid ORDER BY p.PayDefinitionID
        """), {"iid": receiving_id, "cid": tenant.company_id})).all()
    assert unchanged == before

    resolved = await p3c_client.put(
        f"/payroll/imports/{receiving_id}/row-handling",
        json={"expected_revision": created["revision"], "mode": "FilterThenSum",
              "filter_column": {"header": "Category"},
              "choices": [{"category": {"type": "text", "value": "Trip"}, "decision": "Include"}]},
        headers=tenant.admin)
    assert resolved.status_code == 200, resolved.text
    async with p3c_engine.connect() as db:
        after = (await db.execute(text("""
            SELECT p.PayDefinitionID, p.RuleVersionID, p.Revision, p.ExpectedHeadRevision,
                   r.RulePayload
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid ORDER BY p.PayDefinitionID
        """), {"iid": receiving_id, "cid": tenant.company_id})).all()
        heads_after = (await db.execute(text("""
            SELECT h.PayDefinitionID, h.RuleVersionID, h.Revision
            FROM import.PayrollWorkRuleHeads h
            WHERE h.CompanyID=:cid AND h.BranchID=:bid AND h.PayDefinitionID=ANY(:definitions)
            ORDER BY h.PayDefinitionID
        """), {"cid": tenant.company_id, "bid": period["branch_id"],
               "definitions": [first_id, second_id]})).all()
        payroll_after = await db.scalar(text("""
            SELECT count(*) FROM payroll.PayrollDraftLines
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})
    assert {row.rulepayload["row_handling"]["choices"][0]["decision"] for row in after} == {"Include"}
    assert heads_after == heads_before
    assert payroll_after == payroll_before
