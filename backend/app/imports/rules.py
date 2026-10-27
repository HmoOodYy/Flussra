"""Internal durable-rule references and preview freshness for PayrollWork Imports."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.imports.contracts import require_supported_import_contract
from app.imports.errors import ImportFailure
from app.imports.runtime import authorize, failure
from app.imports.staging import clear_derived_state
from app.payroll.mutation_lock import _lock_period_for_mutation


def _fingerprint(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def current_rule_context(db: AsyncConnection, company_id: int, import_id: int) -> str:
    """Return internal relational freshness identity; this is not a payable-content hash."""
    rows = (await db.execute(text("""
        SELECT p.PayDefinitionID, p.PayrollPeriodDefinitionID, p.RateDefinitionID,
            p.RuleVersionID, p.Revision AS PendingRevision, p.ExpectedHeadRevision,
            r.RuleContractVersion, r.SourceContractVersion, r.TargetContractVersion,
            r.CanonicalizationVersion, r.RuleFingerprint,
            h.RuleVersionID AS HeadRuleVersionID, h.Revision AS HeadRevision,
            hr.RuleContractVersion AS HeadRuleContractVersion,
            hr.SourceContractVersion AS HeadSourceContractVersion,
            hr.TargetContractVersion AS HeadTargetContractVersion,
            hr.CanonicalizationVersion AS HeadCanonicalizationVersion,
            hr.RuleFingerprint AS HeadRuleFingerprint
        FROM import.PayrollWorkPendingRules p
        JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID = p.RuleVersionID
            AND r.CompanyID = p.CompanyID AND r.BranchID = p.BranchID
            AND r.PayDefinitionID = p.PayDefinitionID
        LEFT JOIN import.PayrollWorkRuleHeads h ON h.CompanyID = p.CompanyID
            AND h.BranchID = p.BranchID AND h.PayDefinitionID = p.PayDefinitionID
        LEFT JOIN import.PayrollWorkRuleVersions hr ON hr.RuleVersionID = h.RuleVersionID
            AND hr.CompanyID = h.CompanyID AND hr.BranchID = h.BranchID
            AND hr.PayDefinitionID = h.PayDefinitionID
        WHERE p.CompanyID = :cid AND p.ImportID = :iid
        ORDER BY p.PayDefinitionID
    """), {"cid": company_id, "iid": import_id})).mappings().all()
    material = []
    for row in rows:
        actual_head = row["headrevision"] or 0
        if row["expectedheadrevision"] != actual_head:
            raise failure("IMPORT_RULE_CONTEXT_STALE",
                          "A saved rule's expected current head changed. Reconfirm the rule before refreshing.")
        material.append({key: row[key] for key in row.keys()})
    return _fingerprint({"rule_context_version": 1, "pending_rules": material})


async def require_preview_rule_context(db: AsyncConnection, company_id: int, import_id: int) -> str:
    """Fail closed when a preview was built against different pending rules or heads."""
    current = await current_rule_context(db, company_id, import_id)
    has_pending = (await db.execute(text("""
        SELECT EXISTS (SELECT 1 FROM import.PayrollWorkPendingRules
                       WHERE CompanyID = :cid AND ImportID = :iid)
    """), {"cid": company_id, "iid": import_id})).scalar_one()
    stored = (await db.execute(text("""
        SELECT Fingerprint FROM import.PayrollWorkPreviewRuleContexts
        WHERE CompanyID = :cid AND ImportID = :iid
    """), {"cid": company_id, "iid": import_id})).scalar_one_or_none()
    if (stored is None and has_pending) or (stored is not None and stored != current):
        raise failure("IMPORT_RULE_CONTEXT_STALE",
                      "Saved mapping rules changed after this preview. Refresh the preview before continuing.")
    return current


async def bind_pending_rule(db: AsyncConnection, *, company_id: int, user_id: int, import_id: int,
                            expected_import_revision: int, pay_definition_id: int,
                            period_definition_id: int, rate_definition_id: int,
                            rule_version_id: int | None, expected_head_revision: int | None = None) -> None:
    """Internal CAS boundary for a pending rule; caller owns the transaction."""
    owner_row = (await db.execute(text("""
        SELECT w.PayrollPeriodID, w.BranchID
        FROM import.PayrollWorkImports w
        JOIN import.Imports i ON i.ImportID = w.ImportID AND i.CompanyID = w.CompanyID
        WHERE w.ImportID = :iid AND w.CompanyID = :cid
    """), {"iid": import_id, "cid": company_id})).mappings().first()
    if owner_row is None:
        raise ImportFailure("IMPORT_NOT_FOUND", "Import not found.", 404)
    owner = owner_row["payrollperiodid"]
    await authorize(db, company_id, user_id, owner, mutate=True)
    await _lock_period_for_mutation(owner, company_id, db)
    await db.execute(text("""
        SELECT ImportID FROM import.Imports WHERE ImportID = :iid AND CompanyID = :cid FOR UPDATE
    """), {"iid": import_id, "cid": company_id})
    await require_supported_import_contract(db, company_id, import_id)
    row = (await db.execute(text("""
        SELECT i.Revision, i.Status, w.DetailPurgedAtUtc, w.BranchID, w.PayrollPeriodID
        FROM import.Imports i JOIN import.PayrollWorkImports w
          ON w.ImportID = i.ImportID AND w.CompanyID = i.CompanyID
        WHERE i.ImportID = :iid AND i.CompanyID = :cid
    """), {"iid": import_id, "cid": company_id})).mappings().one()
    if row["revision"] != expected_import_revision:
        raise failure("IMPORT_REVISION_CONFLICT", "Import changed. Read its current revision and retry.")
    if row["status"] not in ("NeedsMapping", "NeedsReview") or row["detailpurgedatutc"] is not None:
        raise failure("IMPORT_NOT_EDITABLE", "This Import can no longer change its saved rule configuration.")

    head = (await db.execute(text("""
        SELECT RuleVersionID, Revision FROM import.PayrollWorkRuleHeads
        WHERE CompanyID = :cid AND BranchID = :bid AND PayDefinitionID = :definition
        FOR UPDATE
    """), {"cid": company_id, "bid": row["branchid"], "definition": pay_definition_id})).mappings().first()
    actual_head_revision = head["revision"] if head else 0
    if rule_version_id is None:
        await db.execute(text("""
            DELETE FROM import.PayrollWorkPendingRules
            WHERE ImportID = :iid AND CompanyID = :cid AND PayDefinitionID = :definition
        """), {"iid": import_id, "cid": company_id, "definition": pay_definition_id})
    else:
        if expected_head_revision != actual_head_revision:
            raise failure("IMPORT_RULE_HEAD_STALE", "The current saved rule changed. Read it again and retry.")
        target = (await db.execute(text("""
            SELECT 1 FROM payroll.PayrollPeriodDefinitions
            WHERE PayrollPeriodDefinitionID = :ppd AND PayrollPeriodID = :pid
              AND CompanyID = :cid AND BranchID = :bid AND PayDefinitionID = :definition
              AND RateDefinitionID = :rate
        """), {"ppd": period_definition_id, "pid": owner, "cid": company_id,
               "bid": row["branchid"], "definition": pay_definition_id,
               "rate": rate_definition_id})).scalar_one_or_none()
        if target is None:
            raise failure("IMPORT_RULE_TARGET_INVALID", "Saved rule target is outside this period's frozen definitions.", 422)
        version = (await db.execute(text("""
            SELECT OriginImportID, RuleContractVersion, SourceContractVersion,
                TargetContractVersion, CanonicalizationVersion
            FROM import.PayrollWorkRuleVersions
            WHERE RuleVersionID = :rid AND CompanyID = :cid AND BranchID = :bid
              AND PayDefinitionID = :definition
        """), {"rid": rule_version_id, "cid": company_id,
               "bid": row["branchid"], "definition": pay_definition_id})).mappings().first()
        if version is None:
            raise failure("IMPORT_RULE_SCOPE", "Saved rule is outside this Company, Branch, or definition.", 404)
        if (version["rulecontractversion"] != 1 or version["sourcecontractversion"] != 2
                or version["targetcontractversion"] != 2 or version["canonicalizationversion"] != 3):
            raise failure("IMPORT_CONTRACT_UNSUPPORTED", "Saved rule uses an unsupported protocol version. Reset/re-upload the files.")
        if version["originimportid"] != import_id:
            confirmed_current = head is not None and head["ruleversionid"] == rule_version_id
            if not confirmed_current:
                raise failure("IMPORT_RULE_REUSE_UNCONFIRMED",
                              "Only this Import's proposal or the currently confirmed saved rule may be reused.")
            origin_status = (await db.execute(text("""
                SELECT i.Status FROM import.Imports i WHERE i.ImportID = :origin AND i.CompanyID = :cid
            """), {"origin": version["originimportid"], "cid": company_id})).scalar_one_or_none()
            if origin_status != "Completed":
                raise failure("IMPORT_RULE_REUSE_UNCONFIRMED",
                              "Only a rule from a completed Import may be reused from another period.")
        existing_revision = (await db.execute(text("""
            SELECT Revision FROM import.PayrollWorkPendingRules
            WHERE ImportID = :iid AND CompanyID = :cid AND PayDefinitionID = :definition
        """), {"iid": import_id, "cid": company_id, "definition": pay_definition_id})).scalar_one_or_none()
        values = {"iid": import_id, "cid": company_id, "bid": row["branchid"], "pid": owner,
                  "definition": pay_definition_id, "ppd": period_definition_id,
                  "rate": rate_definition_id, "rule": rule_version_id,
                  "revision": (existing_revision or 0) + 1,
                  "head_revision": expected_head_revision, "uid": user_id}
        if existing_revision is None:
            await db.execute(text("""
                INSERT INTO import.PayrollWorkPendingRules
                    (ImportID, CompanyID, BranchID, PayrollPeriodID, PayDefinitionID,
                     PayrollPeriodDefinitionID, RateDefinitionID, RuleVersionID, Revision,
                     ExpectedHeadRevision, UpdatedByUserID)
                VALUES (:iid, :cid, :bid, :pid, :definition, :ppd, :rate, :rule,
                        :revision, :head_revision, :uid)
            """), values)
        else:
            await db.execute(text("""
                UPDATE import.PayrollWorkPendingRules SET RuleVersionID=:rule,
                    Revision=:revision, ExpectedHeadRevision=:head_revision,
                    UpdatedByUserID=:uid, UpdatedAtUtc=NOW()
                WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
            """), values)
    await clear_derived_state(db, company_id, import_id, reset_mapping=False)
    await db.execute(text("""
        UPDATE import.Imports SET Revision = Revision + 1, Status = 'NeedsMapping',
            UpdatedByUserID = :uid, UpdatedAtUtc = NOW()
        WHERE ImportID = :iid AND CompanyID = :cid
    """), {"uid": user_id, "iid": import_id, "cid": company_id})
