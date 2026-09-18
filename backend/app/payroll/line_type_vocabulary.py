"""
Line-type vocabulary — shared type metadata and naming facts for payroll
source-line line types.

Extracted from app.payroll.service (Stage B4-11A) as a dependency-closed leaf
module — no behavior change, pure relocation.

These four symbols are genuine shared vocabulary/type metadata, not
validation policy: _LineTypeInfo is a result/type shape, _SYSTEM_ITEM_DB_CODES
and _LEGACY_TO_CANONICAL are legacy-to-canonical naming facts, and
_INFORMATIONAL_ONLY is a catalog-shape fact (which line types have no
PayItems catalog row). Live consumers span Draft CRUD, Period Pay, Day Grid,
and Calculation in app.payroll.service, none of which is more entitled to own
this vocabulary than the others.

_SYSTEM_ITEM_DB_CODES is NOT re-exported from app.payroll.service: its only
purpose is constructing _LEGACY_TO_CANONICAL (below), which now happens
inside this module. It is private to this module.

B4-10 Decision Review deliberately did NOT move _SYSTEM_LINE_TYPE_INFO or
_SYSTEM_LINE_TYPES here, even though the former constructs _LineTypeInfo
instances: both are dead legacy constants (_SYSTEM_LINE_TYPES has zero real
callers anywhere in the backend; _SYSTEM_LINE_TYPE_INFO is referenced only by
that dead set) and are not genuinely shared. They remain in
app.payroll.service unchanged, importing _LineTypeInfo from this module at
module-import time, pending a future dedicated dead-code cleanup stage.
"""
from typing import NamedTuple


class _LineTypeInfo(NamedTuple):
    """Validation result for a draft line's line_type value."""
    rate_behavior: str         # 'PerUnit', 'EnteredAmount', 'Fixed', 'None', etc.
    rate_code: str | None      # RateTypes.RateCode for PerUnit; None for other behaviors
    item_scope: str = "Daily"  # 'Daily' | 'Period' — Period items are blocked from daily entry


# Maps each legacy line-type string to its PayItemCode in payroll.PayItems
# (where CompanyID IS NULL).  None means no DB counterpart exists for that
# legacy string (informational items only — no branch-activation check needed).
_SYSTEM_ITEM_DB_CODES: dict[str, str | None] = {
    "Hours":       "HOURS",
    "Miles":       "MILES",
    "Loads":       "LOADS",
    "Overnight":   "OVERNIGHT",
    "Wait":        "WAIT_TIME",
    "Pallets":     "PALLETS",
    "Silos":       "SILOS",
    "DailyStatus": None,      # no DB counterpart; informational only
    "DailyNote":   None,      # no DB counterpart; informational only
    "Bonus":       "BONUS",
    "Adjustment":  "ADJUSTMENT",
}

# CP-0: Maps legacy display-name line-type strings to their canonical PayItemCode.
# Callers may send either form; the service normalises to canonical before validation
# and stores the canonical code in PayrollDraftLines.LineType for new rows.
# Historical rows created before CP-0 still contain legacy strings — reads return
# them verbatim.  Validation re-normalises on update so old rows validate correctly.
_LEGACY_TO_CANONICAL: dict[str, str] = {
    legacy: code
    for legacy, code in _SYSTEM_ITEM_DB_CODES.items()
    if code is not None   # DailyStatus / DailyNote have no canonical PayItemCode
}
# e.g. {"Hours": "HOURS", "Miles": "MILES", ..., "Silos": "SILOS", "Bonus": "BONUS", ...}

# Pure-informational items that have no PayItems catalog counterpart.
# Accepted unconditionally (no scope, branch, or rate checks).
_INFORMATIONAL_ONLY: frozenset[str] = frozenset({"DailyStatus", "DailyNote"})
