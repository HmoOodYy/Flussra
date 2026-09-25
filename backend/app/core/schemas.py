"""
Pydantic schemas for the core domain (branches, people, drivers).
"""
from datetime import date

from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

class BranchSummary(BaseModel):
    branch_id: int
    branch_code: str
    branch_name: str
    status: str
    is_default: bool
    city: str | None = None
    state_province: str | None = None
    country: str | None = None


# ---------------------------------------------------------------------------
# People (unified employees + drivers view)
# ---------------------------------------------------------------------------

class PersonSummary(BaseModel):
    employee_id: int
    branch_id: int
    branch_name: str
    employee_key: str | None = None
    full_name: str
    preferred_name: str | None = None
    employee_type: str          # Driver | OfficeStaff | Manager | PayrollUser
    employment_status: str      # Active | Inactive | Terminated
    email: str | None = None
    primary_phone: str | None = None
    hire_date: date | None = None
    # Populated only when employee_type == 'Driver'
    driver_id: int | None = None
    driver_code: str | None = None
    driver_status: str | None = None
    cdl_number: str | None = None


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

class DriverSummary(BaseModel):
    driver_id: int
    employee_id: int
    branch_id: int
    branch_name: str
    full_name: str
    preferred_name: str | None = None
    employee_key: str | None = None
    driver_code: str | None = None
    cdl_number: str | None = None
    external_driver_id: str | None = None
    driver_status: str          # Active | Inactive | Terminated | OnLeave
    employment_status: str      # Active | Inactive | Terminated
    email: str | None = None
    primary_phone: str | None = None
    hire_date: date | None = None
    termination_date: date | None = None


class DriverCreate(BaseModel):
    branch_id: int
    full_name: str
    preferred_name: str | None = None
    employee_key: str | None = None
    email: str | None = None
    primary_phone: str | None = None
    hire_date: date | None = None
    driver_code: str | None = None
    cdl_number: str | None = None
    external_driver_id: str | None = None

    @field_validator("full_name")
    @classmethod
    def full_name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("full_name must not be blank")
        return v.strip()


_VALID_DRIVER_STATUSES = {"Active", "Inactive", "Terminated", "OnLeave"}
_VALID_EMPLOYMENT_STATUSES = {"Active", "Inactive", "Terminated"}


class DriverUpdate(BaseModel):
    """All fields optional — only supplied fields are updated."""
    full_name: str | None = None
    preferred_name: str | None = None
    email: str | None = None
    primary_phone: str | None = None
    driver_code: str | None = None
    cdl_number: str | None = None
    external_driver_id: str | None = None
    driver_status: str | None = None
    employment_status: str | None = None
    termination_date: date | None = None

    @field_validator("full_name")
    @classmethod
    def full_name_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("full_name must not be blank")
        return v.strip() if v else v

    @field_validator("driver_status")
    @classmethod
    def driver_status_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_DRIVER_STATUSES:
            raise ValueError(f"driver_status must be one of {sorted(_VALID_DRIVER_STATUSES)}")
        return v

    @field_validator("employment_status")
    @classmethod
    def employment_status_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_EMPLOYMENT_STATUSES:
            raise ValueError(f"employment_status must be one of {sorted(_VALID_EMPLOYMENT_STATUSES)}")
        return v
