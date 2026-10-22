"""Immutable evidence writers shared by payroll and review actions."""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.company_currency import CompanyCurrency
from app.payroll.snapshot_hash import used_rate_fingerprint


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


def _schedule_descriptor(line: dict[str, Any]) -> dict[str, Any]:
    """The target DriverRateSchedule a line used, as the packet resolved it.

    An ordinary line is owned by a PayDefinition, a Status line by a Status rate column; the
    descriptor records whichever owner the line has, never both. A line of a single-rate method
    records the one schedule value and its amount (shape ``Scalar``); a line of a multi-value
    method (OrdinalTier) has ``RateValues`` and records every tier value of the schedule
    instead (shape ``OrdinalTierSchedule``), with no single amount.
    """
    source_type = line["SourceType"]
    owner_field = "PayDefinitionID" if source_type == "DraftLine" else "StatusRateColumnID"
    rate_values = line.get("RateValues") or None
    if rate_values is not None and source_type != "DraftLine":
        raise HTTPException(
            status_code=422,
            detail="Cannot submit: a Status line carries tier values instead of one rate.",
        )
    required = (
        "DriverRateScheduleID", "RateDefinitionID", owner_field, "ScheduleStatus",
        "ScheduleEffectiveFrom",
    ) + (() if rate_values is not None else ("RateComponentDefinitionID", "ResolvedRateAmount"))
    if any(line.get(field) is None for field in required):
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot submit: a "
                + ("ordinary" if source_type == "DraftLine" else "Status")
                + " line does not identify the rate it used."
            ),
        )
    return {
        "EvidenceKind": "DriverRateSchedule",
        "SourceType": source_type,
        "DriverAssignmentID": int(line["DriverAssignmentID"]),
        "DriverID": int(line["DriverID"]),
        "DriverRateScheduleID": int(line["DriverRateScheduleID"]),
        "RateDefinitionID": int(line["RateDefinitionID"]),
        "PayDefinitionID": (
            int(line["PayDefinitionID"]) if source_type == "DraftLine" else None),
        "StatusRateColumnID": (
            int(line["StatusRateColumnID"]) if source_type == "StatusEntryState" else None),
        "RateShape": "Scalar" if rate_values is None else "OrdinalTierSchedule",
        "RateComponentDefinitionID": (
            int(line["RateComponentDefinitionID"]) if rate_values is None else None),
        "RateAmount": line["ResolvedRateAmount"] if rate_values is None else None,
        "Components": None if rate_values is None else [dict(value) for value in rate_values],
        "EffectiveFrom": line["ScheduleEffectiveFrom"],
        "EffectiveTo": line["ScheduleEffectiveTo"],
        "RateStatus": line["ScheduleStatus"],
    }


def build_used_rate_evidence(
    *,
    lines: Iterable[dict[str, Any]],
    currency: CompanyCurrency,
) -> tuple[list[dict[str, Any]], list[str | None]]:
    """The rates actually used by the snapshot's lines, one descriptor per distinct rate.

    Ordinary and Status lines record the target DriverRateSchedule they used (schedule,
    definition and owner, the schedule value and its amount or, for OrdinalTier, every tier
    value, status and effective window) exactly as the calculation packet resolved them. Bonus lines use no rate. DriverPayRules
    are not captured here: the Min/Max rule evidence of the snapshot is their one authority.

    Returns the distinct descriptors (each with its ``DefinitionFingerprint``) and, per input
    line in order, the fingerprint of the rate that line used (``None`` for a line that uses
    none). Nothing is read or written: the descriptors are hashed with the snapshot first.
    """
    descriptors: dict[str, dict[str, Any]] = {}
    line_fingerprints: list[str | None] = []
    for line in lines:
        if line["SourceType"] not in ("DraftLine", "StatusEntryState"):
            line_fingerprints.append(None)
            continue
        descriptor = _schedule_descriptor(line)
        descriptor["CurrencyCodeSnapshot"] = currency.code
        descriptor["CurrencyMinorUnitDigitsSnapshot"] = currency.minor_unit_digits
        fingerprint = used_rate_fingerprint(descriptor)
        descriptors.setdefault(fingerprint, {**descriptor, "DefinitionFingerprint": fingerprint})
        line_fingerprints.append(fingerprint)
    return list(descriptors.values()), line_fingerprints


async def insert_used_rate_evidence(
    *,
    snapshot_id: int,
    company_id: int,
    branch_id: int,
    period_id: int,
    used_rates: Iterable[dict[str, Any]],
    db: AsyncConnection,
) -> dict[str, int]:
    """Persist the used-rate descriptors of one snapshot; returns fingerprint -> row id."""
    ids: dict[str, int] = {}
    for descriptor in used_rates:
        result = await db.execute(text("""
            INSERT INTO payroll.payrollcalculationsnapshotusedratedefinitions
                (payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
                 driverassignmentid, driverid, definitionfingerprint, evidencekind,
                 sourcetypesnapshot, driverratescheduleid, ratedefinitionid,
                 paydefinitionid, statusratecolumnid, rateshapesnapshot,
                 ratecomponentdefinitionid,
                 rateamountsnapshot, effectivefromsnapshot, effectivetosnapshot,
                 ratestatussnapshot, currencycodesnapshot,
                 currencyminorunitdigitssnapshot)
            VALUES
                (:snapshot_id, :company_id, :branch_id, :period_id,
                 :driver_assignment_id, :driver_id, :fingerprint, :evidence_kind,
                 :source_type, :schedule_id, :rate_definition_id, :pay_definition_id,
                 :status_rate_column_id, :rate_shape, :component_id,
                 :rate_amount, :effective_from, :effective_to, :rate_status,
                 :currency_code, :currency_minor)
            RETURNING payrollcalculationsnapshotusedratedefinitionid
        """), {
            "snapshot_id": snapshot_id,
            "company_id": company_id,
            "branch_id": branch_id,
            "period_id": period_id,
            "driver_assignment_id": descriptor["DriverAssignmentID"],
            "driver_id": descriptor["DriverID"],
            "fingerprint": descriptor["DefinitionFingerprint"],
            "evidence_kind": descriptor["EvidenceKind"],
            "source_type": descriptor["SourceType"],
            "schedule_id": descriptor.get("DriverRateScheduleID"),
            "rate_definition_id": descriptor.get("RateDefinitionID"),
            "pay_definition_id": descriptor.get("PayDefinitionID"),
            "status_rate_column_id": descriptor.get("StatusRateColumnID"),
            "rate_shape": descriptor["RateShape"],
            "component_id": descriptor.get("RateComponentDefinitionID"),
            "rate_amount": descriptor["RateAmount"],
            "effective_from": descriptor["EffectiveFrom"],
            "effective_to": descriptor["EffectiveTo"],
            "rate_status": descriptor["RateStatus"],
            "currency_code": descriptor["CurrencyCodeSnapshot"],
            "currency_minor": descriptor["CurrencyMinorUnitDigitsSnapshot"],
        })
        used_rate_id = int(result.scalar_one())
        ids[descriptor["DefinitionFingerprint"]] = used_rate_id
        for component in descriptor.get("Components") or ():
            await db.execute(text("""
                INSERT INTO payroll.payrollcalculationsnapshotusedratecomponents
                    (payrollcalculationsnapshotusedratedefinitionid, payrollcalculationsnapshotid,
                     ratedefinitionid, sequenceno, ratecomponentdefinitionid, ordinalfrom,
                     ordinalto, amountsnapshot)
                VALUES
                    (:used_rate_id, :snapshot_id, :rate_definition_id, :sequence_no,
                     :component_id, :ordinal_from, :ordinal_to, :amount)
            """), {
                "used_rate_id": used_rate_id,
                "snapshot_id": snapshot_id,
                "rate_definition_id": descriptor["RateDefinitionID"],
                "sequence_no": component["SequenceNo"],
                "component_id": component["RateComponentDefinitionID"],
                "ordinal_from": component["OrdinalFrom"],
                "ordinal_to": component["OrdinalTo"],
                "amount": component["Amount"],
            })
    return ids
