"""Immutable Phase 6 evidence writers shared by payroll and review actions."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _canonical_json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


async def _participant_snapshot(
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    required_permission_code: str,
    db: AsyncConnection,
) -> tuple[str, str]:
    user = (await db.execute(text("""
        SELECT displayname
        FROM sec.users
        WHERE userid = :user_id
    """), {"user_id": user_id})).mappings().first()
    if user is None:
        raise HTTPException(
            status_code=422,
            detail="Cannot capture workflow evidence: action actor no longer exists.",
        )

    roles = (await db.execute(text("""
        SELECT DISTINCT
               COALESCE(cr.rolecode, r.rolecode) AS role_code,
               COALESCE(cr.rolename, r.rolename) AS role_name,
               ubr.scopetype,
               CASE
                   WHEN cr.rolecode = 'COMPANY_OWNER' THEN 'CompanyOwner'
                   WHEN cr.companyroleid IS NOT NULL THEN 'CompanyRole'
                   ELSE 'LegacyRole'
               END AS grant_source
        FROM sec.userbranchroles ubr
        LEFT JOIN sec.companyroles cr ON cr.companyroleid = ubr.companyroleid
        LEFT JOIN sec.roles r ON r.roleid = ubr.roleid
        WHERE ubr.userid = :user_id
          AND ubr.companyid = :company_id
          AND ubr.isactive = TRUE
          AND (ubr.scopetype = 'AllCompanyBranches' OR ubr.branchid = :branch_id)
          AND (
              cr.rolecode = 'COMPANY_OWNER'
              OR EXISTS (
                  SELECT 1
                  FROM sec.companyrolepermissions crp
                  WHERE crp.companyroleid = ubr.companyroleid
                    AND crp.permissioncode = :permission_code
              )
              OR EXISTS (
                  SELECT 1
                  FROM sec.rolepermissions rp
                  JOIN sec.permissions p ON p.permissionid = rp.permissionid
                  WHERE rp.roleid = ubr.roleid
                    AND p.permissioncode = :permission_code
              )
          )
        ORDER BY role_code, role_name, ubr.scopetype
    """), {
        "user_id": user_id,
        "company_id": company_id,
        "branch_id": branch_id,
        "permission_code": required_permission_code,
    })).mappings().all()
    override_exists = bool((await db.execute(text("""
        SELECT EXISTS (
            SELECT 1
            FROM sec.userpermissionoverrides
            WHERE userid = :user_id
              AND companyid = :company_id
              AND permissioncode = :permission_code
              AND effect = 'ALLOW'
              AND isactive = TRUE
        )
    """), {
        "user_id": user_id,
        "company_id": company_id,
        "permission_code": required_permission_code,
    })).scalar_one())
    context = {
        "required_permission_code": required_permission_code,
        "roles": [
            {
                "grant_source": str(row["grant_source"]),
                "role_code": str(row["role_code"]),
                "role_name": str(row["role_name"]),
                "scope_type": str(row["scopetype"]),
            }
            for row in roles
        ],
        "user_permission_override": override_exists,
    }
    return str(user["displayname"]), _canonical_json(context)


async def capture_workflow_action_evidence(
    *,
    company_id: int,
    branch_id: int,
    period_id: int,
    action_code: str,
    user_id: int,
    required_permission_code: str,
    db: AsyncConnection,
    snapshot_id: int | None = None,
    review_item_id: int | None = None,
    review_decision_id: int | None = None,
    reason_snapshot: str | None = None,
) -> None:
    """Write participant evidence in the transaction that owns the action."""
    display_name, role_context = await _participant_snapshot(
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        required_permission_code=required_permission_code,
        db=db,
    )
    await db.execute(text("""
        INSERT INTO payroll.payrollperiodworkflowactionevidence
            (companyid, branchid, payrollperiodid, payrollcalculationsnapshotid,
             reviewitemid, reviewdecisionid, actioncode, actoruserid,
             actordisplaynamesnapshot, requiredpermissioncode,
             responsibilitycontextsnapshot, reasonsnapshot)
        VALUES
            (:company_id, :branch_id, :period_id, :snapshot_id,
             :review_item_id, :review_decision_id, :action_code, :user_id,
             :display_name, :permission_code, CAST(:role_context AS jsonb), :reason_snapshot)
    """), {
        "company_id": company_id,
        "branch_id": branch_id,
        "period_id": period_id,
        "snapshot_id": snapshot_id,
        "review_item_id": review_item_id,
        "review_decision_id": review_decision_id,
        "action_code": action_code,
        "user_id": user_id,
        "display_name": display_name,
        "permission_code": required_permission_code,
        "role_context": role_context,
        "reason_snapshot": reason_snapshot,
    })


def _definition_fingerprint(descriptor: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(descriptor).encode("utf-8")).hexdigest()


async def capture_snapshot_used_rate_definitions(
    *,
    snapshot_id: int,
    company_id: int,
    branch_id: int,
    period_id: int,
    snapshot_line_rows: Iterable[dict[str, Any]],
    db: AsyncConnection,
) -> dict[int, int]:
    """Persist only the rate/rule definitions actually referenced by snapshot lines.

    The returned mapping uses the caller's stable snapshot-line ordinal.  This
    lets the CP-4D writer link every applicable immutable line without a later
    mutable-rate lookup.
    """
    rows = list(snapshot_line_rows)
    rate_ids = sorted({
        int(row["DriverRateID"])
        for row in rows
        if row.get("DriverRateID") is not None
    })
    rule_ids = sorted({
        int((row.get("SourceEvidenceJSONB") or {}).get("DriverPayRuleID"))
        for row in rows
        if (row.get("SourceEvidenceJSONB") or {}).get("DriverPayRuleID") is not None
    })
    rate_rows: dict[int, Any] = {}
    if rate_ids:
        rate_rows = {
            int(row["driverrateid"]): row
            for row in (await db.execute(text("""
                SELECT dr.driverrateid, dr.companyid, dr.branchid, dr.driverid,
                       dr.ratetypeid, dr.amount, dr.effectivefrom, dr.effectiveto,
                       dr.status, dr.blocksize, dr.roundingrule,
                       rt.ratecode, rt.ratename, rt.unitname
                FROM payroll.driverrates dr
                JOIN payroll.ratetypes rt ON rt.ratetypeid = dr.ratetypeid
                WHERE dr.driverrateid = ANY(:rate_ids)
                  AND dr.companyid = :company_id
                  AND dr.branchid = :branch_id
            """), {
                "rate_ids": rate_ids,
                "company_id": company_id,
                "branch_id": branch_id,
            })).mappings().all()
        }
    rule_rows: dict[int, Any] = {}
    if rule_ids:
        rule_rows = {
            int(row["driverpayruleid"]): row
            for row in (await db.execute(text("""
                SELECT driverpayruleid, companyid, branchid, driverid, ruletype,
                       amount, effectivefrom, effectiveto, status
                FROM payroll.driverpayrules
                WHERE driverpayruleid = ANY(:rule_ids)
                  AND companyid = :company_id
                  AND branchid = :branch_id
            """), {
                "rule_ids": rule_ids,
                "company_id": company_id,
                "branch_id": branch_id,
            })).mappings().all()
        }

    definition_ids: dict[str, int] = {}
    line_definition_ids: dict[int, int] = {}
    for ordinal, line in enumerate(rows):
        evidence = line.get("SourceEvidenceJSONB") or {}
        driver_rate_id = line.get("DriverRateID")
        rule_id = evidence.get("DriverPayRuleID")
        if driver_rate_id is None and rule_id is None:
            continue

        if driver_rate_id is not None:
            rate = rate_rows.get(int(driver_rate_id))
            if rate is None or int(rate["driverid"]) != int(line["DriverID"]):
                raise HTTPException(
                    status_code=422,
                    detail="Cannot submit: used DriverRate evidence is outside the snapshot scope.",
                )
            descriptor = {
                "EvidenceKind": "DriverRate",
                "SourceType": line["SourceType"],
                "DriverID": int(line["DriverID"]),
                "PayItemID": line.get("PayItemID"),
                "RateTypeID": int(rate["ratetypeid"]),
                "DriverRateID": int(rate["driverrateid"]),
                "RateBehavior": evidence.get("RateBehavior"),
                "RateTypeCode": rate["ratecode"],
                "RateTypeName": rate["ratename"],
                "UnitName": rate["unitname"],
                "RateAmount": rate["amount"],
                "EffectiveFrom": rate["effectivefrom"],
                "EffectiveTo": rate["effectiveto"],
                "RateStatus": rate["status"],
                "BlockSize": rate["blocksize"],
                "RoundingRule": rate["roundingrule"],
            }
        else:
            rule = rule_rows.get(int(rule_id))
            if rule is None or int(rule["driverid"]) != int(line["DriverID"]):
                raise HTTPException(
                    status_code=422,
                    detail="Cannot submit: used DriverPayRule evidence is outside the snapshot scope.",
                )
            descriptor = {
                "EvidenceKind": "DriverPayRule",
                "SourceType": line["SourceType"],
                "DriverID": int(line["DriverID"]),
                "PayItemID": line.get("PayItemID"),
                "DriverPayRuleID": int(rule["driverpayruleid"]),
                "RuleType": rule["ruletype"],
                "RuleAmount": rule["amount"],
                "EffectiveFrom": rule["effectivefrom"],
                "EffectiveTo": rule["effectiveto"],
                "RuleStatus": rule["status"],
            }
        fingerprint = _definition_fingerprint(descriptor)
        definition_id = definition_ids.get(fingerprint)
        if definition_id is None:
            result = await db.execute(text("""
                INSERT INTO payroll.payrollcalculationsnapshotusedratedefinitions
                    (payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
                     driverid, definitionfingerprint, evidencekind, sourcetypesnapshot,
                     payitemid, ratetypeid, driverrateid, driverpayruleid,
                     ratebehaviorsnapshot, ratetypecodesnapshot, ratetypenamesnapshot,
                     unitnamesnapshot, rateamountsnapshot, effectivefromsnapshot,
                     effectivetosnapshot, ratestatussnapshot, blocksizesnapshot,
                     roundingrulesnapshot, ruletypesnapshot, ruleamountsnapshot,
                     rulestatussnapshot)
                VALUES
                    (:snapshot_id, :company_id, :branch_id, :period_id,
                     :driver_id, :fingerprint, :evidence_kind, :source_type,
                     :pay_item_id, :rate_type_id, :driver_rate_id, :driver_pay_rule_id,
                     :rate_behavior, :rate_type_code, :rate_type_name, :unit_name,
                     :rate_amount, :effective_from, :effective_to, :rate_status,
                     :block_size, :rounding_rule, :rule_type, :rule_amount, :rule_status)
                RETURNING payrollcalculationsnapshotusedratedefinitionid
            """), {
                "snapshot_id": snapshot_id,
                "company_id": company_id,
                "branch_id": branch_id,
                "period_id": period_id,
                "driver_id": descriptor["DriverID"],
                "fingerprint": fingerprint,
                "evidence_kind": descriptor["EvidenceKind"],
                "source_type": descriptor["SourceType"],
                "pay_item_id": descriptor.get("PayItemID"),
                "rate_type_id": descriptor.get("RateTypeID"),
                "driver_rate_id": descriptor.get("DriverRateID"),
                "driver_pay_rule_id": descriptor.get("DriverPayRuleID"),
                "rate_behavior": descriptor.get("RateBehavior"),
                "rate_type_code": descriptor.get("RateTypeCode"),
                "rate_type_name": descriptor.get("RateTypeName"),
                "unit_name": descriptor.get("UnitName"),
                "rate_amount": descriptor.get("RateAmount"),
                "effective_from": descriptor.get("EffectiveFrom"),
                "effective_to": descriptor.get("EffectiveTo"),
                "rate_status": descriptor.get("RateStatus"),
                "block_size": descriptor.get("BlockSize"),
                "rounding_rule": descriptor.get("RoundingRule"),
                "rule_type": descriptor.get("RuleType"),
                "rule_amount": descriptor.get("RuleAmount"),
                "rule_status": descriptor.get("RuleStatus"),
            })
            definition_id = int(result.scalar_one())
            definition_ids[fingerprint] = definition_id
        line_definition_ids[ordinal] = definition_id
    return line_definition_ids
