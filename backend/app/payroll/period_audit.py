"""Write period-status audit evidence for caller-owned transactions."""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_PERIOD_AUDIT_REASONS: dict[str, str] = {
    "PERIOD_STATUS_CHANGED": "Payroll period status changed",
}


async def write_period_status_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    period_id: int,
    old_status: str,
    new_status: str,
    extra: dict | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog in the caller's transaction.

    extra: optional additional fields merged into newvaluejson (e.g. trigger context).
    """
    new_val_dict: dict = {"status": new_status}
    if extra:
        new_val_dict.update(extra)
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:cid, :bid, :uid, 'PERIOD_STATUS_CHANGED',
                 'payroll', 'PayrollPeriods', :eid,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "cid":     company_id,
            "bid":     branch_id,
            "uid":     user_id,
            "eid":     str(period_id),
            "old_val": json.dumps({"status": old_status}),
            "new_val": json.dumps(new_val_dict),
            "reason":  _PERIOD_AUDIT_REASONS["PERIOD_STATUS_CHANGED"],
        },
    )

