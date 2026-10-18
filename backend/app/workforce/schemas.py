"""Request and response contracts for Workforce Employees, Drivers and Driver assignments."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class DriverCreate(BaseModel):
    """Create the permanent Driver together with its first home-Branch assignment."""

    model_config = ConfigDict(extra="forbid")

    driver_number: str | None = None
    cdl_number: str | None = None
    effective_from_at: AwareDatetime | None = None


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_id: int
    employee_key: str | None = None
    full_name: str
    preferred_name: str | None = None
    email: str | None = None
    primary_phone: str | None = None
    hire_date: date | None = None
    employment_status: Literal["Active", "Inactive"] = "Active"
    termination_date: date | None = None
    driver: DriverCreate | None = None

    @field_validator("full_name")
    @classmethod
    def full_name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("full_name must not be blank")
        return value.strip()


class EmployeeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_id: int | None = None
    full_name: str | None = None
    preferred_name: str | None = None
    email: str | None = None
    primary_phone: str | None = None
    hire_date: date | None = None
    employment_status: Literal["Active", "Inactive", "Terminated"] | None = None
    termination_date: date | None = None

    @field_validator("full_name")
    @classmethod
    def full_name_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("full_name must not be blank")
        return value.strip() if value else value


class EmployeeTermination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    termination_date: date
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class DriverUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_number: str | None = None
    cdl_number: str | None = None
    driver_status: Literal["Active", "Inactive", "OnLeave"] | None = None


class UserSummary(BaseModel):
    user_id: int
    username: str
    display_name: str
    is_active: bool
    can_login: bool


class DriverSummary(BaseModel):
    """The permanent Driver identity."""

    driver_id: int
    driver_number: str | None = None
    cdl_number: str | None = None
    driver_status: str


class DriverAssignmentSummary(BaseModel):
    """One continuous home-Branch tenure of the permanent Driver."""

    driver_assignment_id: int
    driver_id: int
    branch_id: int
    branch_name: str
    effective_from_at: AwareDatetime
    effective_to_at: AwareDatetime | None = None
    end_reason: str | None = None
    transferred_from_assignment_id: int | None = None
    transferred_to_assignment_id: int | None = None


class EmployeeSummary(BaseModel):
    employee_id: int
    company_id: int
    branch_id: int
    branch_name: str
    employee_key: str | None = None
    full_name: str
    preferred_name: str | None = None
    email: str | None = None
    primary_phone: str | None = None
    hire_date: date | None = None
    employment_status: str
    termination_date: date | None = None
    driver_state: Literal["current", "pending", "none"]
    driver: DriverSummary | None = None
    current_or_pending_assignment: DriverAssignmentSummary | None = None


class EmployeeDetail(EmployeeSummary):
    driver_assignments: list[DriverAssignmentSummary] = Field(default_factory=list)
    linked_user: UserSummary | None = None
