from __future__ import annotations

import asyncio
import hashlib
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
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.db_state import TriggerIdentity, suspended_test_triggers
from tests.payroll_runtime_fixtures import build_payroll_tenant, open_period_with_grid
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
    layout_id = created["layouts"][0]["layout_id"]
    mapped = await client.put(
        f"/payroll/imports/{import_id}/layouts/{layout_id}/mapping",
        json={"expected_revision": created["revision"], "column_mapping": {
            "1": "DriverNumber", "2": "WorkDate", "3": f"Definition:{definition_id}"}},
        headers=tenant.admin,
    )
    assert mapped.status_code == 200, mapped.text
    preview = await client.post(
        f"/payroll/imports/{import_id}/preview",
        json={"expected_revision": mapped.json()["revision"]}, headers=tenant.admin,
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


async def _insert_rule(engine, tenant, created, period, period_definition_id,
                       pay_definition_id, *, source_contract=2):
    stable_id, period_definition_id, rate_definition_id = await _target(
        engine, tenant, period, period_definition_id)
    assert stable_id == pay_definition_id
    async with engine.begin() as db:
        rule_id = (await db.execute(text("""
            INSERT INTO import.PayrollWorkRuleVersions
                (CompanyID, BranchID, PayDefinitionID, OriginImportID, OriginPayrollPeriodID,
                 RuleContractVersion, SourceContractVersion, TargetContractVersion,
                 CanonicalizationVersion, RuleFingerprint, SourceSignature, TargetMeaning,
                 RulePayload, CreatedByUserID)
            VALUES (:cid, :bid, :definition, :iid, :pid, 1, :source_version, 2, 3,
                    :fingerprint, '{}'::jsonb, '{}'::jsonb, '{"kind":"mapping"}'::jsonb, :uid)
            RETURNING RuleVersionID
        """), {"cid": tenant.company_id, "bid": period["branch_id"],
               "definition": pay_definition_id, "iid": created["import_id"],
               "pid": period["payroll_period_id"], "source_version": source_contract,
               "fingerprint": "a" * 64, "uid": tenant.owner})).scalar_one()
    return rule_id, period_definition_id, rate_definition_id


async def _mapped_preview_with_rule(client, engine, tenant, period):
    created, period_definition_id, pay_definition_id = await _upload(client, engine, tenant, period)
    first_preview = await _preview(client, tenant, created, period_definition_id)
    rule_id, ppd_id, rate_id = await _insert_rule(
        engine, tenant, created, period, period_definition_id, pay_definition_id)
    async with engine.begin() as db:
        await bind_pending_rule(db, company_id=tenant.company_id, user_id=tenant.owner,
            import_id=created["import_id"], expected_import_revision=first_preview["revision"],
            pay_definition_id=pay_definition_id, period_definition_id=ppd_id,
            rate_definition_id=rate_id, rule_version_id=rule_id, expected_head_revision=0)
    metadata = await client.get(f"/payroll/imports/{created['import_id']}", headers=tenant.admin)
    assert metadata.status_code == 200, metadata.text
    refreshed = await client.post(f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": metadata.json()["revision"]}, headers=tenant.admin)
    assert refreshed.status_code == 200, refreshed.text
    return created, period_definition_id, pay_definition_id, ppd_id, rate_id, rule_id, refreshed.json()


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
    async with p3c_engine.begin() as db:
        stored = (await db.execute(text("""
            SELECT Fingerprint FROM import.PayrollWorkPreviewRuleContexts
            WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).scalar_one()
        assert len(stored) == 64
        assert (await db.execute(text("""
            SELECT count(*) FROM import.PayrollWorkPendingRules WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).scalar_one() == 0

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
        file_id = (await db.execute(text("""
            SELECT FileSourceID FROM import.FileSources WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": import_id, "cid": tenant.company_id})).scalar_one()
    async with p3c_engine.begin() as db:
        async with suspended_test_triggers(db, (TriggerIdentity("import", "filesources", "trg_importdetail_writable"),)):
            await db.execute(text("UPDATE import.FileSources SET ContractVersion=99 WHERE FileSourceID=:fid"), {"fid": file_id})
    await assert_all_reads_reject()
    async with p3c_engine.begin() as db:
        async with suspended_test_triggers(db, (TriggerIdentity("import", "filesources", "trg_importdetail_writable"),)):
            await db.execute(text("UPDATE import.FileSources SET ContractVersion=2 WHERE FileSourceID=:fid"), {"fid": file_id})

    # Simulate an older stored canonical payload version; ordinary inserts reject it.
    _stable_id, period_definition_id, rate_definition_id = await _target(
        p3c_engine, tenant, period, period_definition_id)
    async with p3c_engine.begin() as db:
        canonical_payload = b'{"canonical_file_version":9}'
        async with suspended_test_triggers(db, (TriggerIdentity("import", "filecanonicaldata", "trg_filecanonicaldata_writable"),)):
            await db.execute(text("""
                INSERT INTO import.FileCanonicalData
                    (FileSourceID, ImportID, CompanyID, FormatVersion, Payload, ByteCount, HashSha256)
                VALUES (:file_id, :iid, :cid, 9, :payload, :byte_count, :hash)
            """), {"file_id": file_id, "iid": import_id, "cid": tenant.company_id,
                   "payload": canonical_payload, "byte_count": len(canonical_payload),
                   "hash": hashlib.sha256(canonical_payload).hexdigest()})
    await assert_all_reads_reject()
    async with p3c_engine.begin() as db:
        async with suspended_test_triggers(db, (TriggerIdentity("import", "filecanonicaldata", "trg_filecanonicaldata_writable"),)):
            await db.execute(text("DELETE FROM import.FileCanonicalData WHERE FileSourceID=:fid"), {"fid": file_id})

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
            await db.execute(text("""
            INSERT INTO import.PayrollWorkPendingRules
                (ImportID, CompanyID, BranchID, PayrollPeriodID, PayDefinitionID,
                 PayrollPeriodDefinitionID, RateDefinitionID, RuleVersionID, Revision,
                 ExpectedHeadRevision, UpdatedByUserID)
            VALUES (:iid, :cid, :bid, :pid, :definition, :ppd, :rate, :rule, 1, 0, :uid)
            """), {"iid": import_id, "cid": tenant.company_id, "bid": period["branch_id"],
                   "pid": period["payroll_period_id"], "definition": definition_id,
                   "ppd": period_definition_id, "rate": rate_definition_id,
                   "rule": rule_id, "uid": tenant.owner})
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
    canonical_payload = b'{"file":"canonical"}'
    replacement_proposal, _, _ = await _insert_rule(
        p3c_engine, tenant, created, period, target_period_definition_id, definition_id)
    async with p3c_engine.begin() as db:
        file_id = (await db.execute(text("""
            SELECT FileSourceID FROM import.FileSources
            WHERE ImportID=:iid AND CompanyID=:cid ORDER BY FileOrdinal LIMIT 1
        """), {"iid": created["import_id"], "cid": tenant.company_id})).scalar_one()
        await db.execute(text("""
            INSERT INTO import.FileCanonicalData
                (FileSourceID, ImportID, CompanyID, FormatVersion, Payload, ByteCount, HashSha256)
            VALUES (:fid, :iid, :cid, 1, :payload, :size, :digest)
        """), {"fid": file_id, "iid": created["import_id"], "cid": tenant.company_id,
               "payload": canonical_payload, "size": len(canonical_payload),
               "digest": hashlib.sha256(canonical_payload).hexdigest()})
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

    next_import, next_target_period_definition_id, next_definition_id = await _upload(
        p3c_client, p3c_engine, tenant, period, filename="reuse.csv")
    assert next_definition_id == definition_id
    next_preview = await _preview(p3c_client, tenant, next_import, next_target_period_definition_id)
    _stable_id, next_ppd, next_rate = await _target(
        p3c_engine, tenant, period, next_target_period_definition_id)
    async with p3c_engine.begin() as db:
        await bind_pending_rule(db, company_id=tenant.company_id, user_id=tenant.owner,
            import_id=next_import["import_id"], expected_import_revision=next_preview["revision"],
            pay_definition_id=definition_id, period_definition_id=next_ppd,
            rate_definition_id=next_rate, rule_version_id=rule_id, expected_head_revision=1)
    async with p3c_engine.connect() as db:
        reused = (await db.execute(text("""
            SELECT RuleVersionID, ExpectedHeadRevision FROM import.PayrollWorkPendingRules
            WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
        """), {"iid": next_import["import_id"], "cid": tenant.company_id,
               "definition": definition_id})).one()
    assert reused == (rule_id, 1)

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
    assert retained == (True, True, 0, 0, 2, 1, 1, 1)
    next_period = await create_period(p3c_client, tenant, tenant.branch_a)
    cross_period_import, new_target_period_definition_id, new_definition_id = await _upload(
        p3c_client, p3c_engine, tenant, next_period, filename="cross-period-reuse.csv")
    assert new_definition_id == definition_id
    cross_period_preview = await _preview(
        p3c_client, tenant, cross_period_import, new_target_period_definition_id)
    _stable_id, next_period_definition_id, next_period_rate_id = await _target(
        p3c_engine, tenant, next_period, new_target_period_definition_id)
    assert next_period_definition_id != ppd_id
    async with p3c_engine.begin() as db:
        await bind_pending_rule(db, company_id=tenant.company_id, user_id=tenant.owner,
            import_id=cross_period_import["import_id"],
            expected_import_revision=cross_period_preview["revision"],
            pay_definition_id=definition_id, period_definition_id=next_period_definition_id,
            rate_definition_id=next_period_rate_id, rule_version_id=replacement_rule,
            expected_head_revision=2)
    async with p3c_engine.connect() as db:
        cross_period_state = (await db.execute(text("""
            SELECT p.RuleVersionID, p.ExpectedHeadRevision, h.Revision,
                (SELECT count(*) FROM import.PayrollWorkAppliedChanges a
                 WHERE a.ImportID=p.ImportID AND a.CompanyID=p.CompanyID)
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleHeads h ON h.CompanyID=p.CompanyID
              AND h.BranchID=p.BranchID AND h.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=:definition
        """), {"iid": cross_period_import["import_id"], "cid": tenant.company_id,
               "definition": definition_id})).one()
    assert cross_period_state == (replacement_rule, 2, 2, 0)
    with pytest.raises(ImportFailure) as invalid_target:
        async with p3c_engine.begin() as db:
            await bind_pending_rule(db, company_id=tenant.company_id, user_id=tenant.owner,
                import_id=cross_period_import["import_id"],
                expected_import_revision=cross_period_preview["revision"] + 1,
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
    assert assignment_id > 0 and work_date is not None


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
