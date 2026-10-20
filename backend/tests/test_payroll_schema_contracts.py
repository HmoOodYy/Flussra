import pickle
from decimal import Decimal

from pydantic import ValidationError

import app.payroll.schemas as schemas
from app.payroll.schemas import (
    bonus,
    calculation,
    entry,
    finalized,
    pay_rules,
    periods,
    rates,
    reporting,
)


def test_schema_facade_preserves_owner_identity_and_legacy_pickle_names():
    owned_models = (
        (periods, "PeriodSummary"),
        (entry, "DraftLineCreate"),
        (rates, "DriverRateCreate"),
        (bonus, "BonusEventCreate"),
        (pay_rules, "DriverPayRuleCreate"),
        (calculation, "FinalizationPreviewResponse"),
        (reporting, "CalculationReportResponse"),
        (finalized, "FinalizedOverviewResponse"),
    )
    for owner, name in owned_models:
        model = getattr(owner, name)
        assert getattr(schemas, name) is model
        assert model.__module__ == "app.payroll.schemas"
        assert pickle.loads(pickle.dumps(model)) is model

    assert schemas._VALID_TRANSITIONS is periods._VALID_TRANSITIONS
    assert schemas._VALID_RULE_TYPES is pay_rules._VALID_RULE_TYPES
    assert "_VALID_TRANSITIONS" not in schemas.__all__
    assert "_VALID_RULE_TYPES" not in schemas.__all__
    assert schemas.date.__module__ == "datetime"
    assert schemas.Decimal is Decimal


def test_bonus_schema_facade_preserves_decimal_boundaries():
    accepted = schemas.BonusEventCreate(driver_assignment_id=7, amount=Decimal("99999999999999.9999"))
    assert accepted.amount == Decimal("99999999999999.9999")
    assert schemas.BonusEventCreate(driver_assignment_id=7, amount=Decimal("0.10000")).amount == Decimal("0.10000")

    # Typed-in precision beyond four decimals is accepted (the server rounds it to the Company
    # currency on entry) up to ten decimals; the magnitude and finiteness limits are unchanged.
    assert schemas.BonusEventCreate(driver_assignment_id=7, amount=Decimal("0.00001")).amount == Decimal("0.00001")
    assert schemas.BonusEventCreate(
        driver_assignment_id=7, amount=Decimal("1.1234567890")).amount == Decimal("1.1234567890")

    for amount in (
        Decimal("100000000000000"), Decimal("0.12345678901"), Decimal("NaN"), Decimal("Infinity"),
    ):
        try:
            schemas.BonusEventCreate(driver_assignment_id=7, amount=amount)
        except ValidationError:
            continue
        raise AssertionError(f"invalid amount unexpectedly accepted: {amount}")
