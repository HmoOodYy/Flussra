"""
Draft-line / Period-Pay / Bonus-event mutation audit writer.

Extracted from app.payroll.service (Stage B4-5A) as a dependency-closed leaf
module — no behavior change, pure relocation.

_write_line_audit is a pure immutable audit-log writer (INSERT INTO
audit.auditlog only — no reads, no locking, no status logic). Unlike
app.payroll.workflow_lock._acquire_branch_workflow_lock or
app.payroll.mutation_lock._lock_period_for_mutation, it has nothing to do
with concurrency safety; it is the audit-trail counterpart, a genuinely
separate responsibility that happens to be invoked by some of the same
callers.

It already writes audit rows for three different entity types (DraftLines,
PeriodPay lines, BonusEvents — see its own action-code docstring below), so
it was never owned by a single domain the way _write_rate_audit belongs to
Rates or _write_pay_rule_audit belongs to Driver Pay Rules. It is genuinely
shared by four domains that all still live in app.payroll.service —
Draft-line CRUD, Period Pay Lines, Bonus, and Day Grid — none of which is
more entitled to own it than the others. app.payroll.service imports it back
via facade for all of its current callers, which are not being extracted in
this unit.

Do not add unrelated helpers here. This is not a general utilities module.
"""
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def _write_line_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    line_id: int,
    action_code: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
    entity_name: str = "PayrollDraftLines",
    correlation_id: str | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for a draft-line, period-pay, or bonus-event mutation.

    Known action codes:
      DRAFT_LINE_ADDED      — new draft line inserted
      DRAFT_LINE_UPDATED    — draft line fields changed
      DRAFT_LINE_VOIDED     — draft line status set to Void
      PERIOD_PAY_ADDED      — new period-pay line inserted
      PERIOD_PAY_UPDATED    — period-pay line fields changed
      PERIOD_PAY_VOIDED     — period-pay line status set to Void
      BONUS_EVENT_ADDED     — new canonical bonus event created (CP-3A)
      BONUS_EVENT_UPDATED   — bonus event fields changed (CP-3A)
      BONUS_EVENT_VOIDED    — bonus event voided (CP-3A)

    correlation_id: optional UUID string (CP-3B2a).  audit.AuditLog.CorrelationID
    defaults to gen_random_uuid() per row; when correlation_id is supplied,
    this row's CorrelationID is set to that value instead, so every audit row
    written by one logical operation (e.g. a future bonus batch) can share a
    single BatchCorrelationID. When not supplied, behavior is unchanged —
    the INSERT omits the column entirely and the table default applies.

    Module-level so tests can monkeypatch it to verify that all preceding
    writes roll back when this raises.
    """
    _LINE_AUDIT_REASONS: dict[str, str] = {
        "DRAFT_LINE_ADDED":    "Draft line added",
        "DRAFT_LINE_UPDATED":  "Draft line updated",
        "DRAFT_LINE_VOIDED":   "Draft line voided",
        "PERIOD_PAY_ADDED":    "Period pay line added",
        "PERIOD_PAY_UPDATED":  "Period pay line updated",
        "PERIOD_PAY_VOIDED":   "Period pay line voided",
        "BONUS_EVENT_ADDED":   "Bonus event added",
        "BONUS_EVENT_UPDATED": "Bonus event updated",
        "BONUS_EVENT_VOIDED":  "Bonus event voided",
        "BONUS_BATCH_APPLIED": "Bonus batch applied",
    }
    params = {
        "cid":         company_id,
        "bid":         branch_id,
        "uid":         user_id,
        "action_code": action_code,
        "entity_name": entity_name,
        "eid":         str(line_id),
        "old_val":     json.dumps(old_value)  if old_value  is not None else None,
        "new_val":     json.dumps(new_value)  if new_value  is not None else None,
        "reason":      _LINE_AUDIT_REASONS.get(action_code, action_code),
    }
    if correlation_id is not None:
        params["correlation_id"] = correlation_id
        await db.execute(
            text("""
                INSERT INTO audit.auditlog
                    (companyid, branchid, actoruserid, actioncode,
                     entityschema, entityname, entityid,
                     oldvaluejson, newvaluejson, reason, sourcetype, correlationid)
                VALUES
                    (:cid, :bid, :uid, :action_code,
                     'payroll', :entity_name, :eid,
                     :old_val, :new_val, :reason, 'Application', :correlation_id)
            """),
            params,
        )
    else:
        await db.execute(
            text("""
                INSERT INTO audit.auditlog
                    (companyid, branchid, actoruserid, actioncode,
                     entityschema, entityname, entityid,
                     oldvaluejson, newvaluejson, reason, sourcetype)
                VALUES
                    (:cid, :bid, :uid, :action_code,
                     'payroll', :entity_name, :eid,
                     :old_val, :new_val, :reason, 'Application')
            """),
            params,
        )
