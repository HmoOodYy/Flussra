"""Stable domain errors for target Compensation services."""

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError


def compensation_error(code: str, message: str, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


class CompensationOwnershipError(LookupError):
    """A Company, Driver or RateDefinition does not belong together."""


class CompensationIntegrityError(RuntimeError):
    """Persisted state violates an invariant the database should have prevented."""


# Database invariant marker or constraint name -> (code, status, message).
_DATABASE_ERRORS: tuple[tuple[str, str, int, str], ...] = (
    ("RATE_STRUCTURE_LOCKED", "RATE_STRUCTURE_LOCKED", 409,
     "The compensation structure is locked."),
    ("RATE_STRUCTURE_PENDING_ASSIGNMENT", "RATE_STRUCTURE_PENDING_ASSIGNMENT", 409,
     "Discard the Pending assignment before changing the compensation structure."),
    ("RATE_STRUCTURE_INCOMPLETE", "RATE_STRUCTURE_INCOMPLETE", 422,
     "The compensation structure is incomplete."),
    ("RATE_STRUCTURE_INVALID_TOPOLOGY", "RATE_STRUCTURE_INVALID_TOPOLOGY", 422,
     "The compensation structure topology is invalid."),
    ("RATE_ASSIGNMENT_INCOMPLETE", "ASSIGNMENT_INCOMPLETE", 422,
     "Every required component needs a value before approval."),
    ("RATE_VALUES_IMMUTABLE_AFTER_APPROVAL", "ASSIGNMENT_VALUES_IMMUTABLE", 409,
     "Values of an approved assignment cannot change."),
    ("RATE_ASSIGNMENT_AUTHORITATIVE_IMMUTABLE", "ASSIGNMENT_IMMUTABLE", 409,
     "An approved assignment cannot be modified."),
    ("RATE_ASSIGNMENT_INVALID_TRANSITION", "ASSIGNMENT_INVALID_TRANSITION", 409,
     "The assignment lifecycle does not allow that transition."),
    ("RATE_ASSIGNMENT_DELETE_BLOCKED", "ASSIGNMENT_NOT_PENDING", 409,
     "Only a Pending assignment can be discarded."),
    ("RATE_ASSIGNMENT_BRANCH_MISMATCH", "ASSIGNMENT_BRANCH_MISMATCH", 422,
     "The Driver's Branch does not match the rate definition's Branch."),
    ("COMPANY_CURRENCY_REQUIRED", "COMPANY_CURRENCY_REQUIRED", 422,
     "Configure Company currency before creating monetary state."),
    ("ux_driverrateassignments_pending", "PENDING_ASSIGNMENT_EXISTS", 409,
     "A Pending assignment already exists for this Driver and rate definition."),
    ("ux_driverrateassignments_approved", "APPROVED_ASSIGNMENT_EXISTS", 409,
     "An Approved assignment already exists for this Driver and rate definition."),
    ("excl_driverrateassignments_authoritativeoverlap", "ASSIGNMENT_WINDOW_OVERLAP", 409,
     "The effective window overlaps another authoritative assignment."),
    ("uq_paydefinitions_company_code", "DEFINITION_CODE_CONFLICT", 409,
     "A PayDefinition with this code already exists in the Company."),
)


def translate_database_error(exc: DBAPIError) -> HTTPException | None:
    """Map a known database invariant failure to a stable API error."""
    text = str(getattr(exc, "orig", exc)).lower()
    for marker, code, status_code, message in _DATABASE_ERRORS:
        if marker.lower() in text:
            return compensation_error(code, message, status_code)
    return None
