"""
CDPI (Custom Daily Pay Item) — common backend contracts.

Covers the shared request container, status/event enumerations, and pure
validation helpers.  No services, routers, or DB access.
"""
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

# ---------------------------------------------------------------------------
# Lifecycle status enumeration
# ---------------------------------------------------------------------------

class CdpiStatus(StrEnum):
    """Valid values for CdpiRequests.Status.

    ReturnedToDraft is an event type (CdpiEventType), not a separate status.
    Returned requests reuse CdpiStatus.Draft.
    """
    Draft = "Draft"
    PendingCompanyApproval = "PendingCompanyApproval"
    Rejected = "Rejected"
    Approved = "Approved"


# ---------------------------------------------------------------------------
# Event type enumeration
# ---------------------------------------------------------------------------

class CdpiEventType(StrEnum):
    """Valid values for CdpiRequestEvents.EventType.

    ReturnedToDraft is an event type, not a request status.
    """
    DraftCreated = "DraftCreated"
    Submitted = "Submitted"
    Resubmitted = "Resubmitted"
    ReturnedToDraft = "ReturnedToDraft"
    Rejected = "Rejected"
    Approved = "Approved"
    CopiedFromRejected = "CopiedFromRejected"


# ---------------------------------------------------------------------------
# Domain value enumerations
# ---------------------------------------------------------------------------

class CdpiInputType(StrEnum):
    """Permitted values for CdpiRequests.InputType."""
    Time = "Time"
    Number = "Number"


class CdpiCalcMethodKey(StrEnum):
    """Permitted values for CdpiRequests.CalcMethodKey."""
    PerUnit = "PerUnit"
    OrdinalTier = "OrdinalTier"
    Block = "Block"
    RangeBracket = "RangeBracket"
    RangeProgressive = "RangeProgressive"


# Module-level value sets for fast membership checks in validators.
_VALID_INPUT_TYPES: frozenset[str] = frozenset(e.value for e in CdpiInputType)
_VALID_CALC_METHODS: frozenset[str] = frozenset(e.value for e in CdpiCalcMethodKey)

# Column-length mirrors for validation.
_MAX_UNIT_LENGTH = 50       # CdpiRequests.Unit VARCHAR(50)
_MAX_ITEM_NAME_LENGTH = 200  # CdpiRequests.ItemName VARCHAR(200)


# ---------------------------------------------------------------------------
# Pure validation helpers
# ---------------------------------------------------------------------------

def validate_input_type(value: str) -> str:
    """Raise ValueError if value is not a valid CdpiInputType."""
    if value not in _VALID_INPUT_TYPES:
        raise ValueError(
            f"input_type must be one of {sorted(_VALID_INPUT_TYPES)}, got {value!r}"
        )
    return value


def validate_calc_method_key(value: str) -> str:
    """Raise ValueError if value is not a valid CdpiCalcMethodKey."""
    if value not in _VALID_CALC_METHODS:
        raise ValueError(
            f"calc_method_key must be one of {sorted(_VALID_CALC_METHODS)}, got {value!r}"
        )
    return value


def validate_unit(value: str | None) -> str | None:
    """Raise ValueError if unit exceeds the column width.

    Unit is always optional regardless of InputType.
    """
    if value is not None and len(value) > _MAX_UNIT_LENGTH:
        raise ValueError(f"unit must not exceed {_MAX_UNIT_LENGTH} characters")
    return value


# ---------------------------------------------------------------------------
# Read-side request summary
# ---------------------------------------------------------------------------

class CdpiRequestSummary(BaseModel):
    """
    Read-side representation of a CDPI request row.

    Content fields (item_name, input_type, etc.) are optional so that
    incomplete Draft records can be represented without validation errors.
    """
    model_config = ConfigDict(from_attributes=True)

    request_id: UUID
    company_id: int
    requesting_branch_id: int
    item_name: str | None = None
    input_type: str | None = None
    unit: str | None = None
    calc_method_key: str | None = None
    notes: str | None = None
    status: str
    revision: int
    approved_pay_item_id: int | None = None
    copied_from_request_id: UUID | None = None
    submitted_by_user_id: int | None = None
    submitted_at_utc: datetime | None = None
    created_by_user_id: int
    created_at_utc: datetime
    updated_by_user_id: int | None = None
    updated_at_utc: datetime | None = None


# ---------------------------------------------------------------------------
# Write-side draft fields
# ---------------------------------------------------------------------------

class CdpiRequestDraftFields(BaseModel):
    """
    Write-side fields for creating or updating a Draft CDPI request.

    All content fields are optional to support partial/autosave patterns.
    Unit is always optional regardless of input_type — it is display metadata
    that does not affect computation.
    """
    item_name: str | None = None
    input_type: str | None = None
    unit: str | None = None
    calc_method_key: str | None = None
    notes: str | None = None

    @field_validator("input_type")
    @classmethod
    def input_type_valid(cls, v: str | None) -> str | None:
        if v is not None:
            validate_input_type(v)
        return v

    @field_validator("calc_method_key")
    @classmethod
    def calc_method_key_valid(cls, v: str | None) -> str | None:
        if v is not None:
            validate_calc_method_key(v)
        return v

    @field_validator("unit")
    @classmethod
    def unit_length(cls, v: str | None) -> str | None:
        return validate_unit(v)

    @field_validator("item_name")
    @classmethod
    def item_name_length(cls, v: str | None) -> str | None:
        if v is not None and len(v) > _MAX_ITEM_NAME_LENGTH:
            raise ValueError(
                f"item_name must not exceed {_MAX_ITEM_NAME_LENGTH} characters"
            )
        return v


# ---------------------------------------------------------------------------
# Task 3 write-side contracts
# ---------------------------------------------------------------------------

class CdpiRequestCreate(CdpiRequestDraftFields):
    """
    Body for POST /settings/cdpi/requests (create a new Draft).

    requesting_branch_id is required; the branch must belong to the caller's
    company and the caller must hold payitems.edit on it.

    All content fields are inherited from CdpiRequestDraftFields and remain
    optional so a caller can create an empty draft and fill it in later.
    """
    requesting_branch_id: int


class CdpiRequestUpdate(CdpiRequestDraftFields):
    """
    Body for PATCH /settings/cdpi/requests/{id} (update an existing Draft).

    expected_revision is required for optimistic concurrency: the service
    compares it against the current Revision in the database and rejects the
    update with HTTP 409 if they do not match.

    All content fields are inherited from CdpiRequestDraftFields and remain
    optional so a caller can update a single field without re-sending the rest.
    """
    expected_revision: int


# ---------------------------------------------------------------------------
# Task 4 write-side contracts
# ---------------------------------------------------------------------------

class CdpiSubmitRequest(BaseModel):
    """
    Body for POST /settings/cdpi/requests/{id}/submit.

    expected_revision is required for optimistic concurrency.
    Completeness (ItemName, InputType, CalcMethodKey) is validated by the
    service against the persisted Draft fields -- callers do not re-send
    definition content here.
    """
    expected_revision: int


class CdpiDecideAction(StrEnum):
    """Permitted decision actions for POST /settings/cdpi/requests/{id}/decide."""
    ReturnToDraft = "ReturnToDraft"
    Reject = "Reject"
    Approve = "Approve"


class CdpiDecideRequest(BaseModel):
    """
    Body for POST /settings/cdpi/requests/{id}/decide.

    action    -- one of CdpiDecideAction (ReturnToDraft or Reject).
    reason    -- required, non-empty; stored in the event log.
    expected_revision -- optimistic concurrency guard.
    """
    action: CdpiDecideAction
    expected_revision: int
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("reason must not be empty")
        return v


# ---------------------------------------------------------------------------
# Task 6 write-side contracts (direct company creation)
# ---------------------------------------------------------------------------

class CdpiDirectCreateRequest(BaseModel):
    """
    Body for POST /settings/cdpi/direct-company-items.

    Creates a PayItem + CdpiDefinition directly (no CDPI request workflow).
    All three required content fields must be provided; unit and notes are
    optional display metadata.

    Requires AllCompanyBranches scope + payitems.edit.
    """
    item_name: str
    input_type: str
    calc_method_key: str
    unit: str | None = None
    notes: str | None = None

    @field_validator("item_name")
    @classmethod
    def item_name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("item_name must not be empty")
        if len(v) > _MAX_ITEM_NAME_LENGTH:
            raise ValueError(f"item_name must not exceed {_MAX_ITEM_NAME_LENGTH} characters")
        return v

    @field_validator("input_type")
    @classmethod
    def input_type_valid(cls, v: str) -> str:
        validate_input_type(v)
        return v

    @field_validator("calc_method_key")
    @classmethod
    def calc_method_key_valid(cls, v: str) -> str:
        validate_calc_method_key(v)
        return v

    @field_validator("unit")
    @classmethod
    def unit_length(cls, v: str | None) -> str | None:
        return validate_unit(v)


class CdpiDirectCreateSummary(BaseModel):
    """
    Read-side response for a directly created CDPI company item.

    Returned by POST /settings/cdpi/direct-company-items.
    No CdpiRequest row exists for these items; the response surfaces the
    created PayItem and CdpiDefinition identifiers instead.
    """
    model_config = ConfigDict(from_attributes=True)

    pay_item_id: int
    pay_item_code: str
    company_id: int
    item_name: str
    input_type: str
    unit: str | None
    calc_method_key: str
    notes: str | None
    created_by_user_id: int


# ---------------------------------------------------------------------------
# Task 7 branch-control contracts
# ---------------------------------------------------------------------------

_MAX_BRANCH_DISPLAY_NAME_LENGTH = 200  # mirrors BranchPayItemConfig.BranchDisplayName


class CdpiBranchItemState(BaseModel):
    """
    Read-side view of a single CDPI PayItem as seen from a specific branch.

    Returned by:
      GET  /settings/cdpi/branches/{branch_id}/items
      PATCH /settings/cdpi/branches/{branch_id}/items/{pay_item_id}
    """
    model_config = ConfigDict(from_attributes=True)

    pay_item_id: int
    pay_item_code: str
    item_name: str
    branch_display_name_override: str | None
    effective_display_name: str
    is_active: bool
    data_type: str
    unit: str | None
    rate_behavior: str
    is_cdpi: bool = True


class CdpiBranchItemUpdate(BaseModel):
    """
    Body for PATCH /settings/cdpi/branches/{branch_id}/items/{pay_item_id}.

    Both fields are optional.  Send only the fields you want to change.
    Omitted fields are left unchanged.

    branch_display_name_override:
      - Omit altogether  → do not touch the current override.
      - null or ""       → clear the override (fall back to company PayItem name).
      - non-empty string → set the override (whitespace is trimmed).
    """
    is_active: bool | None = None
    branch_display_name_override: str | None = None

    @field_validator("branch_display_name_override")
    @classmethod
    def display_name_length(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if len(v) > _MAX_BRANCH_DISPLAY_NAME_LENGTH:
                raise ValueError(
                    f"branch_display_name_override must not exceed "
                    f"{_MAX_BRANCH_DISPLAY_NAME_LENGTH} characters"
                )
            return v or None  # empty string after strip → None (clear)
        return None
