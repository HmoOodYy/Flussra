"""Admin audit-log writers shared by user, role, permission, and ownership mutations."""
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_ADMIN_AUDIT_REASONS: dict[str, str] = {
    "USER_CREATED":                    "User account created",
    "USER_UPDATED":                    "User account updated",
    "USER_PASSWORD_RESET":             "User password reset by administrator",
    "USER_EMPLOYEE_LINKED":             "User linked to Employee",
    "USER_EMPLOYEE_UNLINKED":           "User unlinked from Employee",
    "USER_PROVISIONED":                 "Staged User provisioned",
    "USER_STAGED":                      "User staged after final role revocation",
    "ROLE_ASSIGNED":                   "Role assigned to user",
    "ROLE_REVOKED":                    "Role assignment revoked",
    "COMPANY_ROLE_ASSIGNED":           "Company role assigned to user",
    "COMPANY_ROLE_REVOKED":            "Company role assignment revoked",
    "OWNER_TRANSFERRED":               "Company ownership transferred to new owner",
    "USER_PERMISSION_OVERRIDES_SET":    "User extra permissions replaced",
    "COMPANY_ROLE_CREATED":            "Company role created",
    "COMPANY_ROLE_UPDATED":            "Company role updated",
    "COMPANY_ROLE_DELETED":            "Company role deleted",
    "COMPANY_ROLE_PERMISSIONS_SET":    "Company role permissions replaced",
}


async def write_admin_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    user_id: int,
    action_code: str,
    entity_name: str,
    entity_id: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for an admin-domain event.

    Module-level so tests can monkeypatch it to verify that all preceding
    writes roll back when this raises.
    """
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, NULL, :actor_id, :action_code,
                 'sec', :entity_name, :entity_id,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "company_id":  company_id,
            "actor_id":    user_id,
            "action_code": action_code,
            "entity_name": entity_name,
            "entity_id":   entity_id,
            "old_val":     json.dumps(old_value) if old_value is not None else None,
            "new_val":     json.dumps(new_value) if new_value is not None else None,
            "reason":      _ADMIN_AUDIT_REASONS.get(action_code, action_code),
        },
    )


async def write_user_lifecycle_audit(
    db: AsyncConnection, *, company_id: int, caller_id: int, action: str,
    user_id: int, old_value: dict | None = None, new_value: dict | None = None,
) -> None:
    await write_admin_audit(
        db, company_id=company_id, user_id=caller_id, action_code=action,
        entity_name="Users", entity_id=str(user_id), old_value=old_value, new_value=new_value,
    )
