from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi import FastAPI

from app.imports import batch_service
from app.imports.headers import describe_headers
from app.imports.preview_model import _exact_decimal_sum, _sum_quantity
from app.imports.router import router as import_router
from app.imports.target_mapping import resolve_selector, selectors_overlap
from app.payroll.definition_calculation import PeriodDefinition


def _definition(input_type="Decimal"):
    return PeriodDefinition(
        payroll_period_definition_id=100,
        pay_definition_id=1100,
        rate_definition_id=2100,
        code="REGULAR",
        name="Regular work",
        input_type=input_type,
        unit="hours",
        calculation_method="PerUnit",
        calculation_method_version=1,
        rate_shape="Scalar",
        is_active=True,
    )


def test_changed_receiving_frozen_name_requires_only_that_targets_rule_remap():
    from app.imports.batch_service import _target_meaning

    definition_a = _definition()
    definition_b = replace(_definition(), pay_definition_id=1101,
                           payroll_period_definition_id=101, code="BONUS", name="Bonus")
    changed_b = replace(definition_b, payroll_period_definition_id=201, name="Total distance")
    payload_a = {"payload_version": 1, "transform": "Direct",
                 "inputs": [{"header": "Hours"}],
                 "shared_columns": {"DriverNumber": {"header": "DriverNumber"}}}

    def head_for(definition):
        inputs = payload_a["inputs"]
        return {"originstatus": "Completed", "targetmeaning": _target_meaning(definition),
                "rulepayload": payload_a, "sourcesignature": {"inputs": inputs, "ignored_columns": []},
                "rulecontractversion": batch_service.RULE_CONTRACT_VERSION,
                "sourcecontractversion": batch_service.SOURCE_CONTRACT,
                "targetcontractversion": batch_service.TARGET_CONTRACT,
                "canonicalizationversion": batch_service.CANONICALIZATION}

    headers = {1: ["DriverNumber", "Hours"]}
    assert batch_service._rule_reuse_reason(head_for(definition_a), definition_a, headers) == (
        "Matched the current successful relationship", True)
    original_b = head_for(definition_b)
    assert original_b["targetmeaning"] == _target_meaning(definition_b)
    assert batch_service._rule_reuse_reason(original_b, changed_b, headers) == (
        "The frozen target name or input meaning changed", False)
    assert definition_b.pay_definition_id == changed_b.pay_definition_id


def test_duplicate_selector_supports_unique_duplicate_and_explicitly_absent_arrangements():
    duplicate_signature = describe_headers(["Hours", "Hours"])["duplicate_header_signatures"][0]["signature"]
    absent_signature = describe_headers(["Hours", "Other", "Hours"])["duplicate_header_signatures"][0]["signature"]
    unique = {"header": " Hours ", "occurrences": [
        {"duplicate_signature": None, "occurrence_ordinal": 1},
        {"duplicate_signature": duplicate_signature, "occurrence_ordinal": 2},
        {"duplicate_signature": absent_signature, "occurrence_ordinal": None},
    ]}

    assert resolve_selector(unique, {1: ["Hours"], 2: ["Hours", "Hours"],
                                     3: ["Hours", "Other", "Hours"]},
                            allow_unobserved_arrangements=True) == {1: 1, 2: 2, 3: None}


def test_selector_overlap_uses_actual_accepted_signature_and_ordinal_atoms():
    first = {"header": "Hours", "occurrences": [
        {"duplicate_signature": "a" * 64, "occurrence_ordinal": 1},
        {"duplicate_signature": "b" * 64, "occurrence_ordinal": 2},
    ]}
    disjoint = {"header": " hours ", "occurrences": [
        {"duplicate_signature": "a" * 64, "occurrence_ordinal": 2},
        {"duplicate_signature": "b" * 64, "occurrence_ordinal": 1},
    ]}
    overlapping = {"header": "HOURS", "occurrences": [
        {"duplicate_signature": "a" * 64, "occurrence_ordinal": 1},
    ]}

    assert not selectors_overlap(first, disjoint)
    assert selectors_overlap(first, overlapping)


def test_unique_arrangement_can_be_explicitly_absent_for_one_sum_input():
    signature = describe_headers(["Hours", "Hours"])["duplicate_header_signatures"][0]["signature"]
    first = {"header": "Hours", "occurrences": [
        {"duplicate_signature": None, "occurrence_ordinal": 1},
        {"duplicate_signature": signature, "occurrence_ordinal": 1},
    ]}
    second = {"header": "Hours", "occurrences": [
        {"duplicate_signature": None, "occurrence_ordinal": None},
        {"duplicate_signature": signature, "occurrence_ordinal": 2},
    ]}

    assert resolve_selector(first, {1: ["Hours"], 2: ["Hours", "Hours"]}) == {1: 1, 2: 1}
    assert resolve_selector(second, {1: ["Hours"], 2: ["Hours", "Hours"]}) == {1: None, 2: 2}
    assert not selectors_overlap(first, second)


def test_absent_unique_variant_does_not_collide_with_distinct_selected_duplicates():
    signature = describe_headers(["Hours", "Hours", "Hours"])["duplicate_header_signatures"][0]["signature"]
    inputs = [
        {"header": "Hours", "occurrences": [
            {"duplicate_signature": None, "occurrence_ordinal": None},
            {"duplicate_signature": signature, "occurrence_ordinal": ordinal},
        ]}
        for ordinal in (1, 2, 3)
    ]

    assert all(not selectors_overlap(left, right)
               for index, left in enumerate(inputs) for right in inputs[index + 1:])
    assert [resolve_selector(item, {1: ["Hours"], 2: ["Hours", "Hours", "Hours"]},
                             allow_unobserved_arrangements=True)
            for item in inputs] == [{1: None, 2: 1}, {1: None, 2: 2}, {1: None, 2: 3}]


def test_sum_validates_final_whole_number_result_not_each_operand():
    cells = {
        "3": {"value": "0.5", "kind": "number", "formula": False, "missing_cache": False},
        "4": {"value": "0.5", "kind": "number", "formula": False, "missing_cache": False},
    }

    value, error, operands, missing, error_column = _sum_quantity(
        cells, [3, 4], _definition("WholeNumber"), 16000)

    assert value == Decimal("1")
    assert error is None
    assert len(operands) == 2
    assert missing is False
    assert error_column is None


def test_sum_blank_operand_warns_but_does_not_mask_present_invalid_operand():
    cells = {
        "3": {"value": "", "kind": "text", "formula": False, "missing_cache": False},
        "4": {"value": "bad", "kind": "text", "formula": False, "missing_cache": False},
    }

    value, error, _operands, missing, error_column = _sum_quantity(cells, [3, 4], _definition(), 16000)

    assert value is None
    assert error is not None
    assert "Numbers only" in error
    assert missing is True
    assert error_column == 4


def test_exact_sum_handles_large_cancellation_and_zero_with_extreme_exponent():
    assert _exact_decimal_sum([Decimal("1E+5000"), Decimal("-1E+5000"), Decimal("1")],
                              16000) == Decimal(1)
    assert _exact_decimal_sum([Decimal("0E-100000000"), Decimal("1")], 16000) == Decimal(1)


def test_exact_sum_refuses_excessive_numeric_work_without_expanding_exponent():
    with pytest.raises(ArithmeticError, match="work limit"):
        _exact_decimal_sum([Decimal("1E+100000000"), Decimal("1")], 16000)


def test_sum_preserves_tiny_finite_operand_for_payroll_precision_validation():
    cells = {
        "3": {"value": "1E-1000000", "kind": "number", "formula": False, "missing_cache": False},
        "4": {"value": "0", "kind": "number", "formula": False, "missing_cache": False},
    }

    value, error, _operands, missing, _error_column = _sum_quantity(cells, [3, 4], _definition(), 16000)

    assert value is None
    assert error == "At most four decimal places are supported."
    assert missing is False


def test_sum_reports_configured_work_limit_before_expanding_large_exponent():
    cells = {
        "3": {"value": "1E+100000000", "kind": "number", "formula": False, "missing_cache": False},
        "4": {"value": "1", "kind": "number", "formula": False, "missing_cache": False},
    }

    value, error, _operands, missing, _error_column = _sum_quantity(cells, [3, 4], _definition(), 16000)

    assert value is None
    assert error is not None and "numeric work limit" in error
    assert missing is False


def test_sum_reports_invalid_operand_column_instead_of_first_operand():
    cells = {
        "3": {"value": "1", "kind": "number", "formula": False, "missing_cache": False},
        "4": {"value": "bad", "kind": "text", "formula": False, "missing_cache": False},
    }

    value, error, _operands, missing, error_column = _sum_quantity(cells, [3, 4], _definition(), 16000)

    assert value is None
    assert error and "Numbers only" in error
    assert missing is False
    assert error_column == 4


def test_mapping_route_openapi_exposes_derived_required_json_contract():
    app = FastAPI()
    app.include_router(import_router)
    operation = app.openapi()["paths"]["/payroll/imports/{import_id}/mapping"]["put"]
    request_body = operation["requestBody"]
    schema = request_body["content"]["application/json"]["schema"]
    properties = schema["properties"]

    assert request_body["required"] is True
    assert "expected_revision" in schema["required"]
    assert {"shared_columns", "ignored_columns", "targets"} <= set(properties)
    target_properties = properties["targets"]["items"]["properties"]
    assert {"target", "transform", "inputs", "expected_head_revision"} <= set(target_properties)

    def contains_reference(value):
        if isinstance(value, dict):
            return "$ref" in value or any(contains_reference(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_reference(item) for item in value)
        return False

    assert not contains_reference(schema)
