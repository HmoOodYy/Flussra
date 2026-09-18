"""
Period Creation (CP-1C) policy/core — slot-matrix validation.

Extracted from app.payroll.service (Stage B4-3A) as a dependency-closed leaf
module — no behavior change, pure relocation. This is a minimal first step:
only the CP-1C-owned policy/core needed to establish correct ownership of
slot-matrix validation moves here. _decode_candidate_key, get_period_candidates,
and create_period_from_candidate remain in app.payroll.service for now (a
future unit may extract the rest of CP-1C).

_check_slot_matrix is genuinely shared: it is called both by Period Creation
(get_period_candidates, create_period_from_candidate, still in
app.payroll.service) and by Current Payroll Hub (_build_branch_entry, moved
to app.payroll.current_hub in Stage B4-3B) — each imports these symbols
directly from here.
"""
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# CP-1C: Branch-locked candidate-based period creation
# ---------------------------------------------------------------------------

# Active slot statuses that govern mode-eligibility checks:
_ACTIVE_SLOT_STATUSES = frozenset({"Draft", "Open", "InReview", "Returned"})


def _cp1c_error(code: str, message: str, http_status: int = 409) -> None:
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
    )


# ---------------------------------------------------------------------------
# Helper: slot matrix
# ---------------------------------------------------------------------------

def _check_slot_matrix(
    mode: str,
    periods: list[dict],
) -> tuple[bool, str | None]:
    """
    Apply the mode/slot matrix.
    Returns (creatable, error_code | None).
    """
    counts: dict[str, int] = {}
    for p in periods:
        s = p["status"]
        if s in _ACTIVE_SLOT_STATUSES:
            counts[s] = counts.get(s, 0) + 1

    for s, cnt in counts.items():
        if cnt > 1:
            return False, "SLOT_INVARIANT_VIOLATION"

    has_open = "Open" in counts
    has_draft = "Draft" in counts

    if mode == "OPEN_CREATION":
        if has_open and has_draft:
            return False, "ACTIVE_PERIOD_SLOTS_FULL"
        if has_open:
            return False, "OPEN_FILLED"
        if has_draft:
            return False, "DRAFT_WITHOUT_OPEN"
        return True, None

    if mode == "PREPARED_CREATION":
        if has_open and has_draft:
            return False, "ACTIVE_PERIOD_SLOTS_FULL"
        if has_draft and not has_open:
            return False, "DRAFT_WITHOUT_OPEN"
        if not has_open:
            return False, "OPEN_REQUIRED"
        return True, None

    _cp1c_error("INVALID_CANDIDATE_KEY", f"Unknown mode: {mode!r}.")
    return False, None  # unreachable
