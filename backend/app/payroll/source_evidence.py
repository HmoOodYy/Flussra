"""
SOURCE-domain audit-evidence adapter — translates source-line mutations into
the generic P6D immutable audit-evidence system.

Extracted from app.payroll.service (Stage B4-11D) as a dependency-closed leaf
module — no behavior change, pure relocation.

_capture_source_evidence is a shared adapter used by both Draft CRUD and
Period Pay: it resolves the relevant PayItemID from the period/pay-item
relationship, then calls app.payroll.audit_evidence.capture_period_audit_evidence
with domain="SOURCE", source_entity_type="PayrollDraftLines", and
required_permission_code="payroll.entry", preserving the
SOURCE_CREATED / SOURCE_UPDATED / SOURCE_VOIDED evidence semantics.

work_date is intentional domain data supplied by the caller, not module
policy: daily Draft source lines pass their real work date, Period Pay lines
pass None. This module does not interpret or branch on that difference.

app.payroll.audit_evidence is deliberately generic infrastructure — existing
domains (Bonus, Review, Status Note) each call capture_period_audit_evidence
with their own domain-specific adapter or call site rather than through a
shared SOURCE-aware wrapper. This module is the SOURCE domain's own adapter,
following that same pattern; it must not be folded into audit_evidence.py,
which would blur that module's generic, domain-neutral responsibility.

Dependency direction is one-way: source_evidence.py -> audit_evidence.py.
audit_evidence.py has no dependency on this module, and this module has no
dependency on app.payroll.service.

Genuinely shared by six call sites across two modules — Draft-line CRUD
(add_draft_line, update_draft_line, void_draft_line, in
app.payroll.draft_line_mutation) and Period Pay (add_period_pay_line,
update_period_pay_line, void_period_pay_line, in app.payroll.period_pay) —
none of which is more entitled to own it than the others.
"""
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.audit_evidence import capture_period_audit_evidence


async def _capture_source_evidence(
    *, company_id: int, branch_id: int, period_id: int, user_id: int,
    line_id: int, action_code: str, db: AsyncConnection,
    before_state: dict[str, Any] | None, after_state: dict[str, Any] | None,
    driver_id: int | None, work_date: date | None, line_type: str,
) -> None:
    """Capture one non-compatibility DraftLine mutation for P6D."""
    pay_item_id = (await db.execute(text("""
        SELECT payitemid
        FROM payroll.payrollperiodpayitems
        WHERE companyid = :company_id AND branchid = :branch_id
          AND payrollperiodid = :period_id AND payitemcode = :line_type
        LIMIT 1
    """), {
        "company_id": company_id, "branch_id": branch_id,
        "period_id": period_id, "line_type": line_type,
    })).scalar_one_or_none()
    await capture_period_audit_evidence(
        company_id=company_id, branch_id=branch_id, period_id=period_id,
        domain="SOURCE", action_code=action_code,
        source_entity_type="PayrollDraftLines", source_entity_id=line_id,
        user_id=user_id, required_permission_code="payroll.entry", db=db,
        before_state=before_state, after_state=after_state, driver_id=driver_id,
        work_date=work_date, pay_item_id=pay_item_id,
    )
