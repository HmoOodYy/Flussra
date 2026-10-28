from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from app.imports.semantic_identity import (
    SEMANTIC_INPUT_VERSION,
    SemanticInputError,
    TargetReceiptAccumulator,
    build_target_receipt,
    canonical_rule_meaning,
    is_complete_repeat,
    matching_target_ids,
    prepare_rule_meaning,
    validate_target_receipt,
)

TARGET_A = {"code": "REGULAR", "name": "Regular hours", "input_type": "Decimal",
            "unit": "Hours", "method": "PerUnit", "method_version": 1,
            "rate_shape": "Hourly", "active": True}
TARGET_B = {**TARGET_A, "code": "OVERTIME", "name": "Overtime hours"}
DRIVER = {"driver_number": "00071", "employee_key": "EMP-71", "name": None}
HOURS = {"header": "Hours"}
CATEGORY = {"header": "Category"}
DRIVER_NUMBER = {"header": "DriverNumber"}
WORK_DATE = {"header": "WorkDate"}
SCOPE = {"company_id": 1, "branch_id": 2, "period_id": 3}
BINDINGS = [{"kind": "DriverNumber", "value": "00071", "driver_id": 71}]


def rule_meaning(target=TARGET_A, *, transform="Direct", inputs=None, mode="FilterThenSum",
                 header="WorkDate", date_kind="column"):
    selected = inputs if inputs is not None else [HOURS]
    shared = {"DriverNumber": DRIVER_NUMBER, "WorkDate": {"header": header}, "Filter": CATEGORY}
    return canonical_rule_meaning(
        target_meaning=target, transform=transform, inputs=selected, shared_columns=shared,
        row_handling={"mode": mode},
        date_authority={"kind": date_kind, **({"selector": {"header": header}} if date_kind == "column" else {})},
    )


def included_row(value, *, day="2026-10-16", state="value", category="Trip", **overrides):
    row = {"state": "included", "identity_status": "Exact", "requires_confirmation": False,
           "driver": DRIVER, "work_date": day,
           "operands": [{"selector": HOURS, "state": state, **({"value": value} if state == "value" else {})}],
           "category": {"type": "text", "value": category}, "category_decision": "Include"}
    row.update(overrides)
    return row


def receipt(rows, *, target=TARGET_A, rule=None, bindings=BINDINGS, pay_definition_id=11):
    return build_target_receipt(scope=SCOPE, pay_definition_id=pay_definition_id, target_meaning=target,
                                rule_meaning=rule or rule_meaning(target), rows=rows, bindings=bindings)


def test_semantic_input_ignores_layout_presentation_and_source_order():
    current = receipt([included_row(Decimal("1.00")), included_row(Decimal("2.0"), day=date(2026, 10, 17))])
    reordered = receipt([included_row("2", day="2026-10-17"), included_row(Decimal("1"))])
    assert current["input_sha256"] == reordered["input_sha256"]
    assert current["record_count"] == 2

    normalized = canonical_rule_meaning(
        target_meaning=TARGET_A, transform="Direct", inputs=[{"header": " hours "}],
        shared_columns={"DriverNumber": {"header": " drivernumber "}, "WorkDate": WORK_DATE,
                        "Filter": CATEGORY, "EmployeeKey": {"header": "unused presentation"}},
        row_handling={"mode": "FilterThenSum", "unused_choices": ["not a source fact"]},
        date_authority={"kind": "column", "selector": {"header": " WORKDATE "}},
    )
    baseline = rule_meaning()
    assert normalized["inputs"] == baseline["inputs"]
    assert normalized["date_authority"] == baseline["date_authority"]


def test_semantic_input_preserves_row_operand_and_duplicate_multiplicity():
    two_rows = receipt([included_row(Decimal("2")), included_row(Decimal("3"))])
    one_row = receipt([included_row(Decimal("5"))])
    assert two_rows["input_sha256"] != one_row["input_sha256"]

    left = receipt([{
        **included_row(Decimal("0")),
        "operands": [{"selector": {"header": "Hours", "occurrences": [
            {"duplicate_signature": "a" * 64, "occurrence_ordinal": 1}]}, "state": "value", "value": Decimal("2")},
                     {"selector": {"header": "Hours", "occurrences": [
                         {"duplicate_signature": "a" * 64, "occurrence_ordinal": 2}]}, "state": "value", "value": Decimal("3")}],
    }], rule=rule_meaning(transform="Sum", inputs=[
        {"header": "Hours", "occurrences": [{"duplicate_signature": "a" * 64, "occurrence_ordinal": 2}]},
        {"header": "Hours", "occurrences": [{"duplicate_signature": "a" * 64, "occurrence_ordinal": 1}]},
    ]))
    right = receipt([{
        **included_row(Decimal("0")),
        "operands": [{"selector": {"header": "Hours", "occurrences": [
            {"duplicate_signature": "a" * 64, "occurrence_ordinal": 1}]}, "state": "value", "value": Decimal("1")},
                     {"selector": {"header": "Hours", "occurrences": [
                         {"duplicate_signature": "a" * 64, "occurrence_ordinal": 2}]}, "state": "value", "value": Decimal("4")}],
    }], rule=rule_meaning(transform="Sum", inputs=[
        {"header": "Hours", "occurrences": [{"duplicate_signature": "a" * 64, "occurrence_ordinal": 1}]},
        {"header": "Hours", "occurrences": [{"duplicate_signature": "a" * 64, "occurrence_ordinal": 2}]},
    ]))
    assert left["input_sha256"] != right["input_sha256"]


def test_duplicate_arrangement_signature_is_not_semantic_after_mapping_validation():
    rule_a = rule_meaning(inputs=[{"header": "Hours", "occurrences": [
        {"duplicate_signature": "a" * 64, "occurrence_ordinal": 1}]}])
    rule_b = rule_meaning(inputs=[{"header": "Hours", "occurrences": [
        {"duplicate_signature": "b" * 64, "occurrence_ordinal": 1}]}])
    assert rule_a == rule_b
    row_a = included_row(Decimal("2"), operands=[{
        "selector": {"header": "Hours", "occurrences": [
            {"duplicate_signature": "a" * 64, "occurrence_ordinal": 1}]},
        "state": "value", "value": Decimal("2")}])
    row_b = included_row(Decimal("2"), operands=[{
        "selector": {"header": "Hours", "occurrences": [
            {"duplicate_signature": "b" * 64, "occurrence_ordinal": 1}]},
        "state": "value", "value": Decimal("2")}])
    assert receipt([row_a], rule=rule_a)["input_sha256"] == receipt([row_b], rule=rule_b)["input_sha256"]


def test_mixed_source_rule_meaning_is_order_independent_and_inactive_date_column_is_ignored():
    column_rule = rule_meaning(date_kind="column")
    filename_rule = rule_meaning(date_kind="filename")
    rows = [included_row(Decimal("2"), rule_meaning=column_rule),
            included_row(Decimal("3"), day="2026-10-17", rule_meaning=filename_rule)]
    first = receipt(rows, rule=column_rule)
    reordered = receipt(list(reversed(rows)), rule=filename_rule)
    assert first["input_sha256"] == reordered["input_sha256"]
    assert first["rule_meaning_sha256"] == reordered["rule_meaning_sha256"]

    inactive_column = rule_meaning(header="WorkDate", date_kind="filename")
    renamed_inactive_column = rule_meaning(header="Old Date", date_kind="filename")
    assert inactive_column == renamed_inactive_column
    assert receipt([included_row(Decimal("2"))], rule=inactive_column)["input_sha256"] == \
        receipt([included_row(Decimal("2"))], rule=renamed_inactive_column)["input_sha256"]


def test_blank_and_explicit_zero_are_distinct_input_states():
    blank = receipt([included_row(None, state="blank")])
    zero = receipt([included_row(Decimal("0"))])
    absent = receipt([included_row(None, state="absent")])
    assert len({blank["input_sha256"], zero["input_sha256"], absent["input_sha256"]}) == 3


def test_excluded_raw_values_do_not_change_business_input_but_invalid_rows_block_receipts():
    excluded = {"state": "excluded", "raw": {"code": "unknown", "date": "wrong-year", "quantity": "NaN"}}
    base = receipt([included_row(Decimal("4"))])
    changed_excluded = receipt([included_row(Decimal("4")), {**excluded, "raw": {"code": "different"}}])
    assert base["input_sha256"] == changed_excluded["input_sha256"]
    for invalid in (
        {**included_row(Decimal("4")), "state": "invalid"},
        {**included_row(Decimal("4")), "identity_status": "Proposed"},
        {**included_row(Decimal("4")), "requires_confirmation": True},
    ):
        with pytest.raises(SemanticInputError):
            receipt([invalid])


def test_stable_public_identity_is_separate_from_private_driver_binding():
    original = receipt([included_row(Decimal("4"))], bindings=BINDINGS)
    remapped = receipt([included_row(Decimal("4"))],
                       bindings=[{"kind": "DriverNumber", "value": "00071", "driver_id": 99}])
    renamed = receipt([included_row(Decimal("4"))], bindings=BINDINGS,
                      rule=rule_meaning())
    assert original["input_sha256"] == remapped["input_sha256"] == renamed["input_sha256"]
    assert original["binding_sha256"] != remapped["binding_sha256"]


def test_target_receipts_match_independently_without_stitching_rows_or_dates():
    a = receipt([included_row(Decimal("2"))], target=TARGET_A, pay_definition_id=11)
    b = receipt([included_row(Decimal("5"))], target=TARGET_B, rule=rule_meaning(TARGET_B),
                pay_definition_id=12)
    changed_b = receipt([included_row(Decimal("6"))], target=TARGET_B, rule=rule_meaning(TARGET_B),
                        pay_definition_id=12)
    prior_ab = [[a, b]]
    assert matching_target_ids([a], prior_ab) == {11}
    assert is_complete_repeat([a], prior_ab)
    assert not is_complete_repeat([a, changed_b], prior_ab)
    # Each prior receipt contains a complete target input; exact per-target matches can span Imports.
    assert is_complete_repeat([a, b], [[a], [b]])
    # A subset of an earlier target's rows still cannot match its full target digest.
    partial_a = receipt([included_row(Decimal("2"))], target=TARGET_A, pay_definition_id=11)
    full_a = receipt([included_row(Decimal("2")), included_row(Decimal("3"))],
                     target=TARGET_A, pay_definition_id=11)
    assert not is_complete_repeat([partial_a], [[full_a]])


def test_receipt_validation_is_strict_and_driver_binding_is_set_semantics():
    repeated_bindings = [*BINDINGS, deepcopy(BINDINGS[0])]
    result = receipt([included_row(Decimal("1"))], bindings=repeated_bindings)
    assert result["version"] == SEMANTIC_INPUT_VERSION
    assert result["binding_count"] == 1
    assert validate_target_receipt(result) == result
    for malformed in (
        {**result, "version": True},
        {**result, "version": SEMANTIC_INPUT_VERSION + 1},
        {**result, "input_sha256": "bad"},
        {**result, "pay_definition_id": 0},
        {**result, "unexpected": "legacy fallback is not allowed"},
    ):
        with pytest.raises(SemanticInputError):
            validate_target_receipt(malformed)


def test_receipt_validation_rejects_malformed_frozen_definition_scalar_types():
    valid = receipt([included_row(Decimal("1"))])
    for field, bad_value in (("active", 1), ("method_version", True),
                             ("name", 17), ("unit", False),
                             ("input_type", "Unsupported"), ("method", "Unknown")):
        malformed = deepcopy(valid)
        malformed["target_meaning"][field] = bad_value
        with pytest.raises(SemanticInputError):
            validate_target_receipt(malformed)


def test_prepared_rule_is_deeply_immutable_and_keeps_generic_add_validation():
    source_rule = rule_meaning(transform="Direct", inputs=[{"header": "Hours"}], mode="RejectRepeated")
    prepared = prepare_rule_meaning(source_rule)
    accumulator = TargetReceiptAccumulator(
        scope=SCOPE, pay_definition_id=11, target_meaning=TARGET_A,
        rule_meaning=source_rule, prepared_rule=prepared)

    source_rule["inputs"][0]["header"] = "Changed"
    with pytest.raises(TypeError):
        prepared.meaning["transform"] = "Sum"
    with pytest.raises(TypeError):
        prepared.selectors[0]["header"] = "Changed"

    # The public add path still validates arbitrary row mappings, including a
    # copied selector equivalent to (but not identical with) the prepared one.
    generic = included_row(Decimal("1"), category="unused")
    generic["operands"][0]["selector"] = deepcopy(HOURS)
    generic.pop("category")
    generic.pop("category_decision")
    accumulator.add(generic, BINDINGS)

    invalid = included_row(Decimal("2"), category="unused")
    invalid["operands"][0]["selector"] = deepcopy(HOURS)
    invalid["rule_meaning"] = {**source_rule, "inputs": [{"header": "Unsupported"}]}
    invalid.pop("category")
    invalid.pop("category_decision")
    with pytest.raises(SemanticInputError):
        accumulator.add(invalid, BINDINGS)


def test_prepared_equivalent_rules_keep_their_own_frozen_selectors_and_target_guard():
    source_rule = rule_meaning(transform="Direct", inputs=[{"header": "Hours"}], mode="RejectRepeated")
    first = prepare_rule_meaning(source_rule)
    second = prepare_rule_meaning(deepcopy(source_rule))
    accumulator = TargetReceiptAccumulator(
        scope=SCOPE, pay_definition_id=11, target_meaning=TARGET_A,
        rule_meaning=source_rule, prepared_rule=first)
    row = included_row(Decimal("1"), category="unused")
    row.pop("category")
    row.pop("category_decision")
    row["operands"][0]["selector"] = second.selectors[0]

    accumulator.add_prepared(row, BINDINGS, second)
    assert accumulator.build()["record_count"] == 1

    mismatched = prepare_rule_meaning(rule_meaning(
        target=TARGET_B, transform="Direct", inputs=[{"header": "Hours"}], mode="RejectRepeated"))
    with pytest.raises(SemanticInputError):
        accumulator.add_prepared(row, BINDINGS, mismatched)

    with pytest.raises(SemanticInputError):
        TargetReceiptAccumulator(scope=SCOPE, pay_definition_id=11, target_meaning=TARGET_A,
            rule_meaning=rule_meaning(target=TARGET_B, transform="Direct", inputs=[HOURS], mode="RejectRepeated"),
            prepared_rule=first)


def test_generic_accumulator_can_register_a_prepared_rule_after_construction():
    meaning = rule_meaning(transform="Direct", inputs=[HOURS], mode="RejectRepeated")
    prepared = prepare_rule_meaning(meaning)
    accumulator = TargetReceiptAccumulator(
        scope=SCOPE, pay_definition_id=11, target_meaning=TARGET_A, rule_meaning=meaning)
    row = included_row(Decimal("3"))
    row.pop("category")
    row.pop("category_decision")
    row["operands"][0]["selector"] = prepared.selectors[0]

    accumulator.add_prepared(row, BINDINGS, prepared)

    assert accumulator.build()["record_count"] == 1
