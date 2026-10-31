"""
SOURCE-domain audit-evidence adapter — translates ordinary source-line mutations
into the generic P6D immutable audit-evidence system.

_capture_source_evidence records the stable TARGET context of a mutation
(PayrollPeriodDefinitionID, PayDefinitionID, RateDefinitionID) in the evidence
states. It never fabricates a legacy PayItemID: ordinary PayDefinition source has
no PayItem identity. It calls app.payroll.audit_evidence.capture_period_audit_evidence
with domain="SOURCE", source_entity_type="PayrollDraftLines", and
required_permission_code="payroll.entry".

app.payroll.audit_evidence is deliberately generic infrastructure; this module is the
SOURCE domain's own adapter and must not be folded into it. Dependency direction is
one-way: source_evidence.py -> audit_evidence.py.
"""
from collections.abc import Iterable
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.audit_evidence import (
    capture_period_audit_evidence,
    capture_period_audit_evidence_batch,
)
from app.payroll.definition_calculation import PeriodDefinition


def definition_context(definition: PeriodDefinition) -> dict[str, Any]:
    """The stable target identity carried by every ordinary source mutation."""
    return {
        "payroll_period_definition_id": definition.payroll_period_definition_id,
        "pay_definition_id": definition.pay_definition_id,
        "rate_definition_id": definition.rate_definition_id,
    }


async def capture_source_evidence_batch(
    *, company_id: int, branch_id: int, period_id: int, user_id: int,
    changes: Iterable[dict[str, Any]], correlation_id: str, db: AsyncConnection,
    source_revision: int | None = None,
) -> dict[int, int]:
    """SOURCE-owned adaptation of actual bulk quantity changes, not review choices."""
    def events():
        for change in changes:
            context = definition_context(change["definition"])
            yield {"entity_id": str(change["line_id"]), "action_code": change["evidence_action"],
                   "assignment_id": change["driver_assignment_id"], "work_date": change["work_date"],
                   "before_state": None if change["before"] is None else {**context, **change["before"]},
                   "after_state": {**context, **change["after"]}}

    result = await capture_period_audit_evidence_batch(
        company_id=company_id, branch_id=branch_id, period_id=period_id, user_id=user_id,
        domain="SOURCE", source_entity_type="PayrollDraftLines", required_permission_code="payroll.entry",
        events=events(), correlation_id=correlation_id, source_revision=source_revision, db=db,
    )
    return {int(line_id): event_id for line_id, event_id in result.items()}


async def _capture_source_evidence(
    *, company_id: int, branch_id: int, period_id: int, user_id: int,
    line_id: int, action_code: str, db: AsyncConnection,
    before_state: dict[str, Any] | None, after_state: dict[str, Any] | None,
    driver_assignment_id: int | None, work_date: date, definition: PeriodDefinition,
) -> None:
    """Capture one ordinary source mutation for P6D with its target identity."""
    context = definition_context(definition)
    await capture_period_audit_evidence(
        company_id=company_id, branch_id=branch_id, period_id=period_id,
        domain="SOURCE", action_code=action_code,
        source_entity_type="PayrollDraftLines", source_entity_id=line_id,
        user_id=user_id, required_permission_code="payroll.entry", db=db,
        before_state=None if before_state is None else {**context, **before_state},
        after_state=None if after_state is None else {**context, **after_state},
        driver_assignment_id=driver_assignment_id, work_date=work_date,
    )
