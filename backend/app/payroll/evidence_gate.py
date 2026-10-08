"""P4c readiness gate for target payroll.

Target payroll periods have live operational payroll (source entry, Day Grid, live
PerUnit calculation, calculation preview) but no immutable target calculation
evidence yet. Until that evidence exists, every path that would freeze or finalize
money is refused with ``TARGET_PAYROLL_EVIDENCE_NOT_READY`` BEFORE it creates a
calculation snapshot, a review item, a status transition or any financial row.

Workflow capabilities use the same code and message so the UI never offers an action
the backend always rejects.
"""
from fastapi import HTTPException

TARGET_PAYROLL_EVIDENCE_NOT_READY = "TARGET_PAYROLL_EVIDENCE_NOT_READY"

EVIDENCE_NOT_READY_MESSAGE = (
    "Submitting, resubmitting and finalizing payroll are not available yet: immutable "
    "calculation evidence for PayDefinition earnings has not been delivered."
)


def evidence_not_ready() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": TARGET_PAYROLL_EVIDENCE_NOT_READY,
            "message": EVIDENCE_NOT_READY_MESSAGE,
        },
    )


def require_target_payroll_evidence() -> None:
    """Refuse a path that needs immutable target calculation evidence.

    Callers invoke this before any lock, snapshot, review item or status change.
    The evidence work unit that delivers target calculation evidence removes the
    refusal here and nowhere else.
    """
    raise evidence_not_ready()
