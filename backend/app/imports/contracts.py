"""Supported stored Import contract versions shared by reads and mutations."""
from __future__ import annotations

from sqlalchemy import text

from app.imports.errors import ImportFailure

IMPORT_CONTRACT_VERSION = 1
SOURCE_CONTRACT_VERSION = 2
FILE_SOURCE_CONTRACT_VERSION = 2
TARGET_CONTRACT_VERSION = 2
CANONICALIZATION_VERSION = 3
CANONICAL_FILE_VERSION = 1
RULE_CONTRACT_VERSION = 1
ROW_HANDLING_VERSION = 1
REVIEW_DECISION_CONTRACT_VERSION = 1
APPLIED_CHANGE_CONTRACT_VERSION = 1


async def require_supported_import_contract(db, company_id: int, import_id: int) -> None:
    """Reject an Import whose envelope or attached durable contract is unsupported."""
    row = (await db.execute(text("""
        SELECT i.ImportKind AS importkind, i.ContractVersion AS importversion,
            i.SourceKind AS sourcekind, b.ContractVersion AS sourceversion,
            w.ContractVersion AS targetversion, w.CanonicalizationVersion AS canonicalizationversion
        FROM import.Imports i
        JOIN import.FileSourceBatches b ON b.ImportID = i.ImportID AND b.CompanyID = i.CompanyID
        JOIN import.PayrollWorkImports w ON w.ImportID = i.ImportID AND w.CompanyID = i.CompanyID
        WHERE i.CompanyID = :cid AND i.ImportID = :iid
    """), {"cid": company_id, "iid": import_id})).mappings().first()
    if row is None:
        raise ImportFailure("IMPORT_NOT_FOUND", "Import not found.", 404)
    supported = (
        row["importkind"] == "PayrollWork"
        and row["importversion"] == IMPORT_CONTRACT_VERSION
        and row["sourcekind"] == "File"
        and row["sourceversion"] == SOURCE_CONTRACT_VERSION
        and row["targetversion"] == TARGET_CONTRACT_VERSION
        and row["canonicalizationversion"] == CANONICALIZATION_VERSION
    )
    if supported:
        unsupported = (await db.execute(text("""
            SELECT
                EXISTS (SELECT 1 FROM import.FileSources f
                    WHERE f.ImportID = :iid AND f.CompanyID = :cid
                      AND f.ContractVersion <> :file_version)
                OR EXISTS (SELECT 1 FROM import.FileCanonicalData c
                    WHERE c.ImportID = :iid AND c.CompanyID = :cid
                      AND c.FormatVersion <> :canonical_file_version)
                OR EXISTS (SELECT 1 FROM import.PayrollWorkReviewDecisions d
                    WHERE d.ImportID = :iid AND d.CompanyID = :cid
                      AND d.ContractVersion <> :decision_version)
                OR EXISTS (SELECT 1 FROM import.PayrollWorkAppliedChanges a
                    WHERE a.ImportID = :iid AND a.CompanyID = :cid
                      AND a.ContractVersion <> :applied_version)
                OR EXISTS (
                    SELECT 1 FROM import.PayrollWorkRuleVersions r
                    WHERE r.CompanyID = :cid AND r.RuleVersionID IN (
                            SELECT p.RuleVersionID FROM import.PayrollWorkPendingRules p
                            WHERE p.ImportID = :iid AND p.CompanyID = :cid
                            UNION
                            SELECT h.RuleVersionID FROM import.PayrollWorkRuleHeads h
                            JOIN import.PayrollWorkPendingRules p
                              ON p.CompanyID = h.CompanyID AND p.BranchID = h.BranchID
                             AND p.PayDefinitionID = h.PayDefinitionID
                            WHERE p.ImportID = :iid AND p.CompanyID = :cid
                              AND EXISTS (SELECT 1 FROM import.Imports current_import
                                  WHERE current_import.ImportID = p.ImportID AND current_import.CompanyID = p.CompanyID
                                    AND current_import.Status IN ('NeedsMapping','NeedsReview'))
                            UNION
                            SELECT a.RuleVersionID FROM import.PayrollWorkAppliedChanges a
                            WHERE a.ImportID = :iid AND a.CompanyID = :cid
                        ) AND (r.RuleContractVersion <> :rule_version
                        OR r.SourceContractVersion <> :source_version
                        OR r.TargetContractVersion <> :target_version
                        OR r.CanonicalizationVersion <> :canonicalization_version)
                )
        """), {"iid": import_id, "cid": company_id,
               "file_version": FILE_SOURCE_CONTRACT_VERSION,
               "canonical_file_version": CANONICAL_FILE_VERSION,
               "decision_version": REVIEW_DECISION_CONTRACT_VERSION,
               "applied_version": APPLIED_CHANGE_CONTRACT_VERSION,
               "rule_version": RULE_CONTRACT_VERSION,
               "source_version": SOURCE_CONTRACT_VERSION,
               "target_version": TARGET_CONTRACT_VERSION,
               "canonicalization_version": CANONICALIZATION_VERSION})).scalar_one()
        supported = not unsupported
    if not supported:
        raise ImportFailure("IMPORT_CONTRACT_UNSUPPORTED",
                            "This staged Import or an attached saved rule version is unsupported. Reset/re-upload the files.",
                            409)
