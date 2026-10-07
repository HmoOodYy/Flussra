"""Request and response contracts for target Compensation."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_CODE_LENGTH = 50
MAX_NAME_LENGTH = 200
MAX_UNIT_LENGTH = 50

INPUT_TYPES = frozenset({"Decimal", "WholeNumber"})
CALCULATION_METHODS = frozenset({"PerUnit", "OrdinalTier"})

RateAmount = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]


class DecisionAction(StrEnum):
    ReturnToDraft = "ReturnToDraft"
    Reject = "Reject"
    Approve = "Approve"


def _code(value: str | None) -> str | None:
    if value is not None and (not value.strip() or len(value) > MAX_CODE_LENGTH):
        raise ValueError(f"definition_code must be 1-{MAX_CODE_LENGTH} characters")
    return value


def _name(value: str | None) -> str | None:
    if value is not None and (not value.strip() or len(value) > MAX_NAME_LENGTH):
        raise ValueError(f"definition_name must be 1-{MAX_NAME_LENGTH} characters")
    return value


def _input_type(value: str | None) -> str | None:
    if value is not None and value not in INPUT_TYPES:
        raise ValueError(f"input_type must be one of {sorted(INPUT_TYPES)}")
    return value


def _unit(value: str | None) -> str | None:
    if value is not None and len(value) > MAX_UNIT_LENGTH:
        raise ValueError(f"unit must not exceed {MAX_UNIT_LENGTH} characters")
    return value


def _method(value: str | None) -> str | None:
    if value is not None and value not in CALCULATION_METHODS:
        raise ValueError(f"calculation_method must be one of {sorted(CALCULATION_METHODS)}")
    return value


class _DefinitionFieldValidators(BaseModel):
    @field_validator("definition_code", check_fields=False)
    @classmethod
    def _validate_code(cls, value: str | None) -> str | None:
        return _code(value)

    @field_validator("definition_name", check_fields=False)
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        return _name(value)

    @field_validator("input_type", check_fields=False)
    @classmethod
    def _validate_input_type(cls, value: str | None) -> str | None:
        return _input_type(value)

    @field_validator("unit", check_fields=False)
    @classmethod
    def _validate_unit(cls, value: str | None) -> str | None:
        return _unit(value)

    @field_validator("calculation_method", check_fields=False)
    @classmethod
    def _validate_method(cls, value: str | None) -> str | None:
        return _method(value)


class DraftFields(_DefinitionFieldValidators):
    definition_code: str | None = None
    definition_name: str | None = None
    input_type: str | None = None
    unit: str | None = None
    calculation_method: str | None = None
    notes: str | None = None


class PayDefinitionRequestCreate(DraftFields):
    requesting_branch_id: int


class PayDefinitionRequestUpdate(DraftFields):
    expected_revision: int


class PayDefinitionRequestSubmit(BaseModel):
    expected_revision: int


class PayDefinitionRequestDecision(BaseModel):
    action: DecisionAction
    expected_revision: int
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be empty")
        return value


class PayDefinitionDirectCreate(_DefinitionFieldValidators):
    definition_code: str | None = None
    definition_name: str
    input_type: str
    unit: str | None = None
    calculation_method: str


class PayDefinitionRequestSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: UUID
    company_id: int
    requesting_branch_id: int
    definition_code: str | None = None
    definition_name: str | None = None
    input_type: str | None = None
    unit: str | None = None
    calculation_method: str | None = None
    notes: str | None = None
    status: str
    revision: int
    approved_pay_definition_id: int | None = None
    copied_from_request_id: UUID | None = None
    submitted_by_user_id: int | None = None
    submitted_at_utc: datetime | None = None
    created_by_user_id: int
    created_at_utc: datetime
    updated_by_user_id: int | None = None
    updated_at_utc: datetime | None = None


class PayDefinitionRequestEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: UUID
    request_id: UUID
    event_type: str
    from_status: str | None = None
    to_status: str
    actor_user_id: int
    reason: str | None = None
    request_revision: int
    occurred_at_utc: datetime


class PayDefinitionProvenance(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    creation_mode: str
    source_request_id: UUID | None = None
    requesting_branch_id: int | None = None
    created_by_user_id: int
    submitted_by_user_id: int | None = None
    submitted_at_utc: datetime | None = None
    approved_by_user_id: int | None = None
    approved_at_utc: datetime | None = None
    governance_schema_version: int
    calculation_method_version: int
    created_at_utc: datetime


class RateComponentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rate_component_definition_id: int
    sequence_no: int
    ordinal_from: int | None = None
    ordinal_to: int | None = None


class PayDefinitionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pay_definition_id: int
    company_id: int
    definition_code: str
    definition_name: str
    input_type: str
    unit: str | None = None
    calculation_method: str
    status: str
    rate_definition_id: int | None = None
    rate_shape: str | None = None
    structure_locked_at_utc: datetime | None = None
    components: list[RateComponentSummary] = []
    provenance: PayDefinitionProvenance | None = None


class AssignmentCreate(BaseModel):
    driver_id: int
    rate_definition_id: int
    effective_from: date
    effective_to: date | None = None
    notes: str | None = None


class AssignmentUpdate(BaseModel):
    effective_from: date | None = None
    effective_to: date | None = None
    notes: str | None = None


class AssignmentValueInput(BaseModel):
    rate_component_definition_id: int
    amount: RateAmount | None = None


class AssignmentValuesReplace(BaseModel):
    values: list[AssignmentValueInput]


class AssignmentVoid(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be empty")
        return value


class AssignmentValue(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rate_component_definition_id: int
    sequence_no: int
    ordinal_from: int | None = None
    ordinal_to: int | None = None
    amount: Decimal | None = None


class AssignmentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    driver_rate_assignment_id: int
    company_id: int
    branch_id: int
    driver_id: int
    rate_definition_id: int
    effective_from: date
    effective_to: date | None = None
    status: str
    created_by_user_id: int | None = None
    created_at_utc: datetime
    updated_by_user_id: int | None = None
    updated_at_utc: datetime | None = None
    approved_by_user_id: int | None = None
    approved_at_utc: datetime | None = None
    voided_by_user_id: int | None = None
    voided_at_utc: datetime | None = None
    void_reason: str | None = None
    notes: str | None = None
    values: list[AssignmentValue] = []


class DriverRateSummaryItem(BaseModel):
    rate_definition_id: int
    pay_definition_id: int | None = None
    definition_code: str | None = None
    approved_assignment_id: int | None = None
    approved_effective_from: date | None = None
    pending_assignment_id: int | None = None


class ResolvedComponent(BaseModel):
    rate_component_definition_id: int
    sequence_no: int
    ordinal_from: int | None = None
    ordinal_to: int | None = None
    amount: Decimal | None = None


class ResolvedCompensation(BaseModel):
    company_id: int
    driver_id: int
    branch_id: int
    pay_definition_id: int | None = None
    rate_definition_id: int
    rate_shape: str
    calculation_method: str | None = None
    driver_rate_assignment_id: int
    assignment_status: str
    effective_from: date
    effective_to: date | None = None
    components: list[ResolvedComponent]

    @property
    def scalar_amount(self) -> Decimal | None:
        if self.rate_shape != "Scalar" or len(self.components) != 1:
            raise ValueError("Resolved compensation is not a single scalar component.")
        return self.components[0].amount
