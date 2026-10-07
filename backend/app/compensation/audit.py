"""Generic audit entries for target Compensation configuration changes."""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def write_audit(
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
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, :branch_id, :actor_id, :action_code,
                 'payroll', :entity_name, :entity_id,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "company_id": company_id,
            "branch_id": branch_id,
            "actor_id": user_id,
            "action_code": action_code,
            "reason": action_code,
            "entity_name": entity_name,
            "entity_id": entity_id,
            "old_val": json.dumps(old_value) if old_value is not None else None,
            "new_val": json.dumps(new_value) if new_value is not None else None,
        },
    )
