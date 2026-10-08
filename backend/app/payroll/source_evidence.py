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
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.audit_evidence import capture_period_audit_evidence
from app.payroll.definition_calculation import PeriodDefinition


def definition_context(definition: PeriodDefinition) -> dict[str, Any]:
    """The stable target identity carried by every ordinary source mutation."""
    return {
        "payroll_period_definition_id": definition.payroll_period_definition_id,
        "pay_definition_id": definition.pay_definition_id,
        "rate_definition_id": definition.rate_definition_id,
    }


async def _capture_source_evidence(
    *, company_id: int, branch_id: int, period_id: int, user_id: int,
    line_id: int, action_code: str, db: AsyncConnection,
    before_state: dict[str, Any] | None, after_state: dict[str, Any] | None,
    driver_id: int | None, work_date: date, definition: PeriodDefinition,
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
        driver_id=driver_id, work_date=work_date, pay_item_id=None,
    )
