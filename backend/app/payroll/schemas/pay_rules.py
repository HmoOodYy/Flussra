"""Driver pay-rule contracts."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, field_validator, model_validator

from .bonus import _validate_positive_numeric_18_4

# Compatibility metadata: Pydantic models retain the legacy app.payroll.schemas identity.



# ---------------------------------------------------------------------------
# Driver Pay Rules (M15 — Minimum / Maximum Pay)
# ---------------------------------------------------------------------------

class DriverPayRuleSummary(BaseModel):
    """One row from payroll.DriverPayRules."""
    __module__ = "app.payroll.schemas"
    driver_pay_rule_id: int
    company_id: int
    branch_id: int
    driver_id: int
    rule_type: str           # 'MinimumPay' | 'MaximumPay'
    amount: Decimal          # > 0
    effective_from: date
    effective_to: date | None = None
    status: str              # 'Active' | 'Ended' | 'Voided'
    created_by_user_id: int | None = None
    created_at_utc: datetime
    updated_by_user_id: int | None = None
    updated_at_utc: datetime | None = None
    notes: str | None = None



_VALID_RULE_TYPES = {"MinimumPay", "MaximumPay"}



class DriverPayRuleCreate(BaseModel):
    __module__ = "app.payroll.schemas"
    driver_id: int
    rule_type: str
    amount: Decimal
    effective_from: date
    effective_to: date | None = None
    notes: str | None = None

    @field_validator("rule_type")
    @classmethod
    def rule_type_valid(cls, v: str) -> str:
        if v not in _VALID_RULE_TYPES:
            raise ValueError(f"rule_type must be one of {sorted(_VALID_RULE_TYPES)}")
        return v

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: Decimal) -> Decimal:
        return _validate_positive_numeric_18_4(v, amount_label="Driver pay rule amount")

    @model_validator(mode="after")
    def effective_to_after_from(self) -> "DriverPayRuleCreate":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be on or after effective_from")
        return self



class DriverPayRuleEnd(BaseModel):
    """Payload for POST /driver-pay-rules/{id}/end — close the rule at a specific date."""
    __module__ = "app.payroll.schemas"
    effective_to: date



class DriverPayRuleNotesUpdate(BaseModel):
    """Payload for PATCH /driver-pay-rules/{id} — notes-only update."""
    __module__ = "app.payroll.schemas"
    notes: str | None = None
