"""Settings audit-log writer shared by company, branch, and status mutations."""
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_SETTINGS_AUDIT_REASONS: dict[str, str] = {
    "COMPANY_UPDATED":           "Company profile updated",
    "BRANCH_CREATED":            "Branch created",
    "BRANCH_UPDATED":            "Branch updated",
    "BRANCH_DEFAULT_CHANGED":    "Default branch changed",
    "PAYROLL_SETUP_SAVED":       "Branch payroll setup saved",
    "STATUS_KEY_CREATED":        "Payroll status key created",
    "STATUS_KEY_UPDATED":        "Payroll status key updated",
    "STATUS_KEY_DEACTIVATED":    "Payroll status key deactivated",
}


async def write_settings_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int | None,
    user_id: int,
    action_code: str,
    entity_name: str,
    entity_id: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for a settings-domain event.

    Extracted as a module-level function so tests can monkeypatch it to verify
    that all preceding writes roll back when this raises.
    """
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, :branch_id, :actor_id, :action_code,
                 'core', :entity_name, :entity_id,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "company_id":  company_id,
            "branch_id":   branch_id,
            "actor_id":    user_id,
            "action_code": action_code,
            "entity_name": entity_name,
            "entity_id":   entity_id,
            "old_val":     json.dumps(old_value) if old_value is not None else None,
            "new_val":     json.dumps(new_value) if new_value is not None else None,
            "reason":      _SETTINGS_AUDIT_REASONS.get(action_code, action_code),
        },
    )
