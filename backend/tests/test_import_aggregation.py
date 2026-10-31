from datetime import date
from decimal import Decimal

import pytest

from app.imports.aggregation import (
    CategoryEvidenceError,
    aggregate_quantity_contributions,
    aggregate_rows,
    inspect_category_evidence,
    quantity_contribution,
    resolve_category_decisions,
    typed_category_key,
)
from app.imports.files import FileCell


def _cell(kind, value=None, **extra):
    return {"kind": kind, "value": value, "missing_cache": False, **extra}


def _row(quantity, *, category=None, identity_state="resolved", state="present"):
    return {"identity_state": identity_state,
            "category_cell": category,
            "targets": {"regular": {"state": state, "quantity": Decimal(quantity)}}}


def test_category_keys_preserve_typed_scalar_identity_and_exact_numeric_equivalence():
    assert typed_category_key(_cell("text", " 1 ")) == ("text", " 1 ")
    assert typed_category_key(_cell("text", "")) == ("text", "")
    assert typed_category_key(_cell("blank")) == ("blank", None)
    assert typed_category_key(_cell("number", Decimal("1.0"))) == ("number", "1")
    assert typed_category_key(_cell("number", Decimal("1"))) == ("number", "1")
    assert typed_category_key(_cell("boolean", True)) == ("boolean", True)
    assert typed_category_key(_cell("date", date(2026, 10, 16))) == ("date", "2026-10-16")
    assert typed_category_key(_cell("date", "2026-10-16")) == ("date", "2026-10-16")
    assert len({typed_category_key(_cell("text", "1")),
                typed_category_key(_cell("number", Decimal("1"))),
                typed_category_key(_cell("boolean", True))}) == 3
    assert typed_category_key(FileCell("Trip", "text")) == ("text", "Trip")
    assert typed_category_key(FileCell("2026-10-16T08:30:00", "date")) == (
        "date", "2026-10-16T08:30:00")


@pytest.mark.parametrize("cell", [
    {"kind": "text", "value": "Hours", "missing_cache": True},
    _cell("error", "#VALUE!"),
    _cell("time", "12:00"),
    _cell("number", "NaN"),
])
def test_invalid_or_unavailable_cells_cannot_be_category_evidence(cell):
    with pytest.raises(CategoryEvidenceError):
        typed_category_key(cell)


def test_category_suitability_requires_repeated_exact_key_unless_rule_was_validated():
    unique = inspect_category_evidence([_cell("text", "Trip"), _cell("text", "Load")])
    assert unique["suitable"] is False
    assert unique["repetition_observed"] is False
    assert unique["distinct_count"] == 2

    repeated = inspect_category_evidence([_cell("text", "Trip"), _cell("text", "Trip")])
    assert repeated["suitable"] is True
    assert repeated["repetition_observed"] is True
    assert repeated["values"] == [{"type": "text", "value": "Trip", "count": 2}]

    retained = inspect_category_evidence([_cell("text", "Trip")], previously_validated=True)
    assert retained["suitable"] is True
    assert retained["repetition_observed"] is False


def test_category_evidence_has_no_distinct_value_count_cutoff():
    evidence = inspect_category_evidence([_cell("text", f"Category {index}")
                                          for index in range(1000)])

    assert evidence["distinct_count"] == 1000
    assert evidence["repetition_observed"] is False
    assert evidence["suitable"] is False


def test_category_decisions_require_every_value_and_reject_unobserved_decisions():
    trip = typed_category_key(_cell("text", "Trip"))
    load = typed_category_key(_cell("text", "Load"))
    partial = resolve_category_decisions({trip, load}, {trip: "Include"})
    assert partial["ready"] is False
    assert partial["unresolved"] == [load]

    complete = resolve_category_decisions({trip, load}, {trip: "Include", load: "Exclude"})
    assert complete["ready"] is True
    assert complete["included"] == {trip}
    assert complete["excluded"] == {load}

    with pytest.raises(CategoryEvidenceError):
        resolve_category_decisions({trip}, {trip: "Include", load: "Exclude"})
    with pytest.raises(CategoryEvidenceError):
        resolve_category_decisions({trip}, {trip: "Maybe"})

    retained = resolve_category_decisions(
        {trip}, {trip: "Include", load: "Exclude"}, previously_validated_keys={load})
    assert retained["ready"] is True
    assert retained["retained_absent"] == {load: "Exclude"}


def test_aggregation_adds_exact_operands_before_final_canonical_validation():
    result = aggregate_quantity_contributions([
        {"state": "present", "quantity": Decimal("-99999999999999.9999")},
        {"state": "present", "quantity": Decimal("99999999999999.9999")},
        {"state": "present", "quantity": Decimal("0.5")},
        {"state": "present", "quantity": Decimal("0.5")},
    ], input_type="WholeNumber", max_work_digits=16_000)
    assert result == {"status": "complete", "reason": None, "quantity": Decimal("1.0")}


def test_quantity_contribution_parsing_allows_addends_outside_final_quantity_rules():
    assert quantity_contribution(_cell("text", "-5.25")) == {
        "state": "present", "quantity": Decimal("-5.25")}
    assert quantity_contribution(_cell("number", "100000000000000")) == {
        "state": "present", "quantity": Decimal("100000000000000")}
    assert quantity_contribution(_cell("text", "  ")) == {"state": "blank", "quantity": None}
    assert quantity_contribution(_cell("text", "not a number"))["state"] == "invalid"
    assert quantity_contribution(_cell("number", "4", missing_cache=True))["state"] == "invalid"


@pytest.mark.parametrize(("contributions", "status", "reason"), [
    ([{"state": "present", "quantity": Decimal("4")}, {"state": "blank"}],
     "withheld", "incomplete_contribution"),
    ([{"state": "present", "quantity": Decimal("4")}, {"state": "missing"}],
     "withheld", "incomplete_contribution"),
    ([{"state": "present", "quantity": Decimal("4")}, {"state": "invalid", "error": "bad cell"}],
     "invalid", "bad cell"),
    ([{"state": "present", "quantity": Decimal("4")}, {"state": "unknown_category"}],
     "withheld", "unknown_category"),
    ([{"state": "present", "quantity": Decimal("4")}, {"state": "unresolved_identity"}],
     "withheld", "unresolved_identity"),
])
def test_incomplete_or_uncertain_included_contributions_withhold_the_group(contributions, status, reason):
    result = aggregate_quantity_contributions(contributions, input_type="Decimal", max_work_digits=16_000)
    assert result["status"] == status
    assert result["reason"] == reason
    assert result["quantity"] is None


def test_invalid_contribution_is_not_hidden_by_incomplete_contribution_or_row_order():
    invalid = {"state": "invalid", "error": "bad operand"}
    blank = {"state": "blank"}
    forward = aggregate_quantity_contributions([blank, invalid], input_type="Decimal", max_work_digits=100)
    reverse = aggregate_quantity_contributions([invalid, blank], input_type="Decimal", max_work_digits=100)
    assert forward == reverse == {"status": "invalid", "reason": "bad operand", "quantity": None}


def test_explicit_exclusions_do_not_make_an_included_group_incomplete():
    result = aggregate_quantity_contributions([
        {"state": "present", "quantity": Decimal("4")},
        {"state": "excluded"},
    ], input_type="Decimal", max_work_digits=16_000)
    assert result == {"status": "complete", "reason": None, "quantity": Decimal("4")}

    all_excluded = aggregate_quantity_contributions([{"state": "excluded"}],
                                                    input_type="Decimal", max_work_digits=16_000)
    assert all_excluded == {"status": "absent", "reason": "no_included_contributions", "quantity": None}


def test_filter_then_sum_excludes_whole_rows_and_unknown_categories_withhold():
    trip = _cell("text", "Trip")
    load = _cell("text", "Load")
    result = aggregate_rows([
        _row("3", category=trip),
        _row("900", category=load),
    ], rule="FilterThenSum", category_column="Activity", category_decisions={
        typed_category_key(trip): "Include", typed_category_key(load): "Exclude",
    }, target="regular", input_type="Decimal", max_work_digits=16_000)
    assert result["status"] == "complete"
    assert result["quantity"] == Decimal("3")
    assert result["contribution_count"] == 1

    unknown = aggregate_rows([
        _row("3", category=trip),
        _row("2", category=_cell("text", "New value")),
    ], rule="FilterThenSum", category_column="Activity",
        category_decisions={typed_category_key(trip): "Include"}, target="regular",
        input_type="Decimal", max_work_digits=16_000)
    assert unknown["status"] == "withheld"
    assert unknown["reason"] == "unknown_category"


def test_known_excluded_row_needs_no_valid_identity_or_quantity():
    trip, load = _cell("text", "Trip"), _cell("text", "Load")
    result = aggregate_rows([
        _row("3", category=trip),
        {"identity_state": "unresolved", "category_cell": load,
         "targets": {"regular": {"state": "invalid", "error": "bad ignored quantity"}}},
    ], rule="FilterThenSum", category_column="Activity", category_decisions={
        typed_category_key(trip): "Include", typed_category_key(load): "Exclude",
    }, target="regular", input_type="Decimal", max_work_digits=100)
    assert result["status"] == "complete"
    assert result["quantity"] == Decimal("3")


def test_unresolved_identity_is_not_added_to_the_aggregate():
    result = aggregate_rows([
        _row("3"),
        _row("100", identity_state="proposal"),
    ], rule="SumRows", category_column=None, category_decisions=None,
        target="regular", input_type="Decimal", max_work_digits=16_000)
    assert result["status"] == "withheld"
    assert result["reason"] == "unresolved_identity"


def test_repeated_rows_require_an_explicit_sum_rule_and_dates_are_external_bucket_keys():
    rows = [_row("3"), _row("2")]
    refused = aggregate_rows(rows, rule="RejectRepeated", category_column=None,
                             category_decisions=None, target="regular", input_type="Decimal",
                             max_work_digits=16_000)
    assert refused["status"] == "invalid"
    assert refused["reason"] == "duplicate_fact"

    accepted = aggregate_rows(rows, rule="SumRows", category_column=None,
                              category_decisions=None, target="regular", input_type="Decimal",
                              max_work_digits=16_000)
    assert accepted["status"] == "complete"
    assert accepted["quantity"] == Decimal("5")

    with pytest.raises(ValueError):
        aggregate_rows(rows, rule="Unknown", category_column=None, category_decisions=None,
                       target="regular", input_type="Decimal", max_work_digits=16_000)


def test_whole_number_validation_applies_once_to_the_final_row_total():
    contributions = [
        {"state": "present", "quantity": Decimal("0.5")},
        {"state": "present", "quantity": Decimal("0.5")},
    ]
    complete = aggregate_quantity_contributions(
        contributions, input_type="WholeNumber", max_work_digits=16_000)
    assert complete == {"status": "complete", "reason": None, "quantity": Decimal("1.0")}

    contributions[1] = {"state": "present", "quantity": Decimal("0.4")}
    invalid = aggregate_quantity_contributions(
        contributions, input_type="WholeNumber", max_work_digits=16_000)
    assert invalid["status"] == "invalid"
    assert invalid["reason"] == "This item requires a whole number quantity."
    assert invalid["quantity"] is None
