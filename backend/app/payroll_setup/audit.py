"""Append-only Payroll Setup policy audit persistence."""

from collections.abc import Iterable
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection


async def write_policy_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    actor_user_id: int,
    event_type: str,
    payroll_setup_id: int | None = None,
    payroll_setup_version_id: int | None = None,
    branch_payroll_setup_assignment_id: int | None = None,
    payroll_period_id: int | None = None,
    branch_id: int | None = None,
    old_payroll_setup_id: int | None = None,
    new_payroll_setup_id: int | None = None,
    old_payroll_setup_version_id: int | None = None,
    new_payroll_setup_version_id: int | None = None,
    old_branch_payroll_setup_assignment_id: int | None = None,
    new_branch_payroll_setup_assignment_id: int | None = None,
    effective_date: date | None = None,
    old_config_hash: str | None = None,
    new_config_hash: str | None = None,
    old_state: dict[str, Any] | None = None,
    new_state: dict[str, Any] | None = None,
    affected_branch_ids: Iterable[int] = (),
    correlation_id: UUID | None = None,
) -> int:
    """Insert one policy event and its affected Branch set in the caller transaction."""
    insert_event = text(
        """
        INSERT INTO payroll.PayrollSetupPolicyAuditEvents (
            CompanyID, ActorUserID, EventType, PayrollSetupID,
            PayrollSetupVersionID, BranchPayrollSetupAssignmentID, PayrollPeriodID, BranchID,
            OldPayrollSetupID, NewPayrollSetupID,
            OldPayrollSetupVersionID, NewPayrollSetupVersionID,
            OldBranchPayrollSetupAssignmentID, NewBranchPayrollSetupAssignmentID,
            EffectiveDate, OldConfigHash, NewConfigHash, OldStateJSON, NewStateJSON,
            CorrelationID
        ) VALUES (
            :company_id, :actor_user_id, :event_type, :payroll_setup_id,
            :payroll_setup_version_id, :assignment_id, :payroll_period_id, :branch_id,
            :old_setup_id, :new_setup_id, :old_version_id, :new_version_id,
            :old_assignment_id, :new_assignment_id, :effective_date,
            :old_config_hash, :new_config_hash, :old_state, :new_state,
            :correlation_id
        ) RETURNING PayrollSetupPolicyAuditEventID
        """
    ).bindparams(
        bindparam("old_state", type_=JSONB(none_as_null=True)),
        bindparam("new_state", type_=JSONB(none_as_null=True)),
    )
    result = await db.execute(
        insert_event,
        {
            "company_id": company_id,
            "actor_user_id": actor_user_id,
            "event_type": event_type,
            "payroll_setup_id": payroll_setup_id,
            "payroll_setup_version_id": payroll_setup_version_id,
            "assignment_id": branch_payroll_setup_assignment_id,
            "payroll_period_id": payroll_period_id,
            "branch_id": branch_id,
            "old_setup_id": old_payroll_setup_id,
            "new_setup_id": new_payroll_setup_id,
            "old_version_id": old_payroll_setup_version_id,
            "new_version_id": new_payroll_setup_version_id,
            "old_assignment_id": old_branch_payroll_setup_assignment_id,
            "new_assignment_id": new_branch_payroll_setup_assignment_id,
            "effective_date": effective_date,
            "old_config_hash": old_config_hash,
            "new_config_hash": new_config_hash,
            "old_state": old_state,
            "new_state": new_state,
            "correlation_id": correlation_id,
        },
    )
    event_id = result.scalar_one()

    branches = sorted(set(affected_branch_ids))
    if branches:
        insert_branch = text(
            """
            INSERT INTO payroll.PayrollSetupPolicyAuditEventBranches
                (PayrollSetupPolicyAuditEventID, CompanyID, BranchID)
            VALUES (:event_id, :company_id, :branch_id)
            """
        )
        await db.execute(
            insert_branch,
            [
                {"event_id": event_id, "company_id": company_id, "branch_id": item}
                for item in branches
            ],
        )
    return event_id
