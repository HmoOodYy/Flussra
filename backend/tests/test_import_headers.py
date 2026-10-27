from __future__ import annotations

import pytest

from app.imports.errors import ImportFailure
from app.imports.headers import analyze_header_sets, combined_column_count, describe_headers


def test_repeated_daily_headers_share_slots_but_same_file_duplicates_do_not_collapse():
    repeated = ["Driver", "Code", "Hours", "Hours", "Hours"]
    profile = analyze_header_sets([repeated, repeated])

    assert profile["combined_column_count"] == 5
    assert describe_headers(repeated)["headed_column_count"] == 5
    assert profile["maximum_multiplicity_by_label"]["hours"] == 3
    signature = describe_headers(repeated)["duplicate_header_signatures"][0]
    assert signature["columns"] == [3, 4, 5]
    assert len({item["occurrence_id"] for item in describe_headers(repeated)["header_occurrences"]
                if item["duplicate_signature"]}) == 3


def test_partial_overlap_counts_maximum_label_multiplicity_and_preserves_physical_positions():
    profile = analyze_header_sets([
        ["Driver", "", "Hours", "Hours"],
        [" driver ", "Hours", "Bonus"],
    ])

    assert profile["combined_column_count"] == 4
    first = profile["file_header_details"][0]
    assert first["header_cells"] == ["Driver", "", "Hours", "Hours"]
    assert [item["source_column"] for item in first["header_occurrences"]] == [1, 3, 4]
    assert profile["maximum_multiplicity_by_label"] == {"driver": 1, "hours": 2, "bonus": 1}


def test_header_normalization_is_only_surrounding_space_and_casefold():
    assert combined_column_count([["Hours"], [" hours "]]) == 1
    assert combined_column_count([["Work Date"], ["Work  Date"]]) == 2
    assert combined_column_count([["Work-Date"], ["Work Date"]]) == 2


def test_each_pair_must_share_a_header_even_when_an_intermediate_file_bridges_them():
    with pytest.raises(ImportFailure) as caught:
        analyze_header_sets([["A"], ["A", "B"], ["B"]])

    assert caught.value.code == "IMPORT_HEADER_STRUCTURE_INCOMPATIBLE"


def test_compatibility_validation_rejects_a_file_without_any_header():
    with pytest.raises(ImportFailure) as caught:
        analyze_header_sets([["", "   "]])

    assert caught.value.code == "IMPORT_HEADER_REQUIRED"


@pytest.mark.parametrize(("width", "maximum", "accepted"), [(40, 40, True), (41, 40, False)])
def test_combined_occurrence_count_boundary(width, maximum, accepted):
    first_unique = width // 2
    second_unique = width - 1 - first_unique
    left = ["Driver", *[f"Left {index}" for index in range(first_unique)]]
    right = ["Driver", *[f"Right {index}" for index in range(second_unique)]]
    if accepted:
        assert analyze_header_sets([left, right], maximum)["combined_column_count"] == width
    else:
        with pytest.raises(ImportFailure) as caught:
            analyze_header_sets([left, right], maximum)
        assert caught.value.code == "IMPORT_COMBINED_COLUMN_LIMIT_EXCEEDED"


def test_duplicate_arrangement_changes_duplicate_signature():
    before = describe_headers(["Driver", "Hours", "Hours", "Code"])
    after = describe_headers(["Driver", "Hours", "Code", "Hours"])

    assert before["duplicate_header_signatures"][0]["signature"] != after["duplicate_header_signatures"][0]["signature"]


def test_calendar_looking_labels_are_ordinary_header_names():
    profile = analyze_header_sets([
        ["Driver", "Monday", "Tuesday", "Jan", "Feb", "2026-10-20"],
        ["driver", " Monday ", "Tuesday", "Jan", "Feb", "2026-10-20"],
    ])

    assert profile["combined_column_count"] == 6
