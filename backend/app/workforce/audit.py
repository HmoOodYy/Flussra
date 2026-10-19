"""Write Workforce audit evidence for caller-owned transactions."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_AUDIT_REASONS = {
    "EMPLOYEE_CREATED": "Workforce Employee created",
    "EMPLOYEE_UPDATED": "Workforce Employee updated",
    "DRIVER_CREATED": "Workforce Driver and first assignment created",
    "DRIVER_UPDATED": "Workforce Driver updated",
    "DRIVER_TRANSFER_COMPLETED": "Workforce Driver transfer completed",
    "DRIVER_TRANSFER_SCHEDULED": "Workforce Driver transfer scheduled",
    "DRIVER_TRANSFER_CANCELLED": "Workforce Driver transfer cancelled",
    "DRIVER_TRANSFER_RESCHEDULED": "Workforce Driver transfer rescheduled",
    "DRIVER_EMPLOYEE_TERMINATED": "Workforce Driver Employee terminated",
}


async def write_workforce_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    actor_user_id: int,
    action_code: str,
    entity_name: str,
    entity_id: int,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    """Write Workforce audit evidence in the caller's transaction."""
    await db.execute(
        text("""
            INSERT INTO audit.AuditLog
                (CompanyID, BranchID, ActorUserID, ActionCode, EntitySchema,
                 EntityName, EntityID, OldValueJson, NewValueJson, Reason, SourceType)
            VALUES
                (:company_id, :branch_id, :actor_id, :action_code, 'core',
                 :entity_name, :entity_id, :old_value, :new_value, :reason, 'Application')
        """),
        {
            "company_id": company_id,
            "branch_id": branch_id,
            "actor_id": actor_user_id,
            "action_code": action_code,
            "entity_name": entity_name,
            "entity_id": str(entity_id),
            "old_value": json.dumps(old_value, default=str) if old_value is not None else None,
            "new_value": json.dumps(new_value, default=str) if new_value is not None else None,
            "reason": _AUDIT_REASONS[action_code],
        },
    )
