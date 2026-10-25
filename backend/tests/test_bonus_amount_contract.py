"""Typed amount validation for bonus create, update, and batch requests."""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.payroll.schemas import BonusBatchItem, BonusEventCreate, BonusEventUpdate


@pytest.mark.parametrize("model, payload", [
    (BonusEventCreate, {"driver_assignment_id": 1, "amount": "1.2345"}),
    (BonusEventUpdate, {"amount": "1.2345"}),
    (BonusBatchItem, {"driver_assignment_id": 1, "amount": "1.2345"}),
])
def test_shared_bonus_amount_contract_accepts_four_decimals(model, payload):
    assert model.model_validate(payload).amount == Decimal("1.2345")


@pytest.mark.parametrize("model, payload", [
    (BonusEventCreate, {"driver_assignment_id": 1, "amount": "1.23456"}),
    (BonusEventUpdate, {"amount": "1.23456"}),
    (BonusBatchItem, {"driver_assignment_id": 1, "amount": "1.23456"}),
])
def test_bonus_amount_models_accept_extra_typed_precision_for_entry_rounding(model, payload):
    """Typed-in precision is accepted and rounded on entry."""
    assert model.model_validate(payload).amount == Decimal("1.23456")


@pytest.mark.parametrize("amount", [
    "1.12345678901", "0", "-1", "100000000000000", "NaN", "Infinity", "-Infinity",
])
def test_all_bonus_amount_models_reject_invalid_typed_amounts(amount):
    for model, base in (
        (BonusEventCreate, {"driver_assignment_id": 1}),
        (BonusEventUpdate, {}),
        (BonusBatchItem, {"driver_assignment_id": 1}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate({**base, "amount": amount})
