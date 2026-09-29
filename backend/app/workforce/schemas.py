"""Request and response contracts for Workforce Employees and Driver profiles."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DriverProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_code: str | None = None
    cdl_number: str | None = None
    external_driver_id: str | None = None
    effective_from: date | None = None


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
    driver_profile: DriverProfileCreate | None = None

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


class DriverProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_code: str | None = None
    cdl_number: str | None = None
    external_driver_id: str | None = None
    driver_status: Literal["Active", "Inactive", "OnLeave"] | None = None


class UserSummary(BaseModel):
    user_id: int
    username: str
    display_name: str
    is_active: bool
    can_login: bool


class DriverProfileSummary(BaseModel):
    driver_id: int
    branch_id: int
    branch_name: str
    driver_code: str | None = None
    cdl_number: str | None = None
    external_driver_id: str | None = None
    driver_status: str
    effective_from: date | None = None
    effective_to: date | None = None


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
    current_or_pending_driver: DriverProfileSummary | None = None


class EmployeeDetail(EmployeeSummary):
    driver_profiles: list[DriverProfileSummary] = Field(default_factory=list)
    linked_user: UserSummary | None = None
