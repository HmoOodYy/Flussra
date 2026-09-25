"""Public request and response models for company-owned Payroll Setup policy."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetupCreateRequest(StrictModel):
    setup_code: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    setup_name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class SetupUpdateRequest(StrictModel):
    setup_name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class DraftCreateRequest(StrictModel):
    payroll_frequency: str | None = Field(
        default=None, pattern=r"^(Week|Biweek|Month|Custom)$",
    )
    anchor_start_date: date | None = None
    custom_interval_days: int | None = Field(default=None, gt=0)
    normal_days_off_mask: int | None = Field(default=None, ge=0, le=127)


class DraftUpdateRequest(StrictModel):
    payroll_frequency: str = Field(pattern=r"^(Week|Biweek|Month|Custom)$")
    anchor_start_date: date
    custom_interval_days: int | None = Field(gt=0)
    normal_days_off_mask: int = Field(ge=0, le=127)


class PublishRequest(StrictModel):
    effective_from_date: date
    replaces_version_id: int | None = Field(default=None, gt=0)


class PublicationImpactRequest(StrictModel):
    effective_from_date: date
    replaces_version_id: int | None = Field(default=None, gt=0)


class DefaultSetupRequest(StrictModel):
    setup_id: int | None


class AssignmentCreateRequest(StrictModel):
    setup_id: int = Field(gt=0)
    effective_from_date: date
    reason: str | None = None


class ReassignmentRequest(StrictModel):
    destination_setup_id: int = Field(gt=0)
    effective_from_date: date
    reason: str | None = None


class ReassignmentImpactRequest(StrictModel):
    destination_setup_id: int = Field(gt=0)
    effective_from_date: date


class WithdrawalRequest(StrictModel):
    reason: str | None = None


class SetupResponse(BaseModel):
    setup_id: int
    setup_code: str
    setup_name: str
    description: str | None
    status: str


class ScheduleResponse(BaseModel):
    payroll_frequency: str
    anchor_start_date: date
    custom_interval_days: int | None
    normal_days_off_mask: int


class DraftResponse(BaseModel):
    setup_id: int
    version_id: int
    lifecycle_state: str
    payroll_frequency: str | None
    anchor_start_date: date | None
    custom_interval_days: int | None
    normal_days_off_mask: int | None
    created_at_utc: datetime
    discarded_at_utc: datetime | None


class VersionResponse(BaseModel):
    setup_id: int
    version_id: int
    lifecycle_state: str
    version_number: int
    effective_from_date: date
    effective_to_date: date | None
    schedule: ScheduleResponse
    config_hash: str
    replaces_version_id: int | None
    replaced_by_version_id: int | None
    is_terminal: bool


class ConflictResponse(BaseModel):
    branch_id: int | None = None
    code: str
    reason: str


class PublicationImpactResponse(BaseModel):
    setup_id: int
    affected_branch_ids: list[int]
    effective_date: date
    predecessor_version_id: int | None
    predecessor_hash: str | None
    current_same_date_version_id: int | None
    successor_hash: str
    successor_schedule: ScheduleResponse
    next_version_boundary: date | None
    conflicts: list[ConflictResponse]
    allowed: bool


class ReassignmentImpactResponse(BaseModel):
    branch_id: int
    source_setup_id: int | None
    destination_setup_id: int
    predecessor_version_id: int | None
    successor_version_id: int | None
    effective_date: date
    conflicts: list[ConflictResponse]
    allowed: bool


class AssignmentResponse(BaseModel):
    assignment_id: int
    branch_id: int
    setup_id: int
    setup_code: str
    setup_name: str
    effective_from_date: date
    effective_to_date: date | None
    reason: str | None
    created_at_utc: datetime
    withdrawn_at_utc: datetime | None
    withdrawal_reason: str | None


class VersionSegmentResponse(BaseModel):
    version_id: int
    version_number: int
    effective_from_date: date
    effective_to_date: date | None
    schedule: ScheduleResponse
    config_hash: str


class AssignmentHistoryResponse(AssignmentResponse):
    versions: list[VersionSegmentResponse]


class BranchHistoryResponse(BaseModel):
    branch_id: int
    assignments: list[AssignmentHistoryResponse]


class DefaultSetupResponse(BaseModel):
    setup: SetupResponse | None


class EffectiveAuthorityResponse(BaseModel):
    company_id: int
    branch_id: int
    assignment_id: int
    setup_id: int
    setup_code: str
    setup_name: str
    version_id: int
    version_number: int
    schedule: ScheduleResponse
    config_hash: str
    period_start_date: date
    period_end_date: date
    next_boundary_date: date | None
    next_boundary_kind: str | None
