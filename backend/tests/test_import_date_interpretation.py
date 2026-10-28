from datetime import date, timedelta

import pytest

from app.imports.date_interpretation import interpret_date, make_date_profile


def _days(start: str, count: int) -> list[date]:
    first = date.fromisoformat(start)
    return [first + timedelta(days=offset) for offset in range(count)]


def _cell(value, *, kind="text", missing_cache=False):
    return {"value": value, "kind": kind, "formula": False, "missing_cache": missing_cache}


@pytest.mark.parametrize(("value", "kind", "expected", "reason"), [
    ("2026-10-16", "text", "2026-10-16", "explicit_date"),
    (date(2026, 10, 16).isoformat(), "date", "2026-10-16", "explicit_date"),
    ("10/16/2026", "text", "2026-10-16", "explicit_date"),
])
def test_existing_iso_native_and_selected_numeric_dates_are_preserved(value, kind, expected, reason):
    result = interpret_date(_cell(value, kind=kind), period_days=_days("2026-10-01", 31), selected_order="MDY")

    assert result["status"] == "resolved"
    assert result["resolved_date"] == expected
    assert result["reason"] == reason
    assert result["explicit_year"] is True


def test_numeric_date_with_identical_day_month_order_is_not_ambiguous():
    result = interpret_date(_cell("01/01/2026"), period_days=[date(2026, 1, 1)])

    assert result["status"] == "resolved"
    assert result["resolved_date"] == "2026-01-01"
    assert result["pattern"] == "numeric_order_independent"


def test_profile_remembers_period_inferred_omitted_year_order():
    result = interpret_date(_cell("10/16"), period_days=[date(2026, 10, 16)])

    assert result["resolved_date"] == "2026-10-16"
    assert make_date_profile("column", None, [result]) == [{
        "authority": "column", "numeric_order": "MDY", "grammars": ["numeric_omitted_year"],
    }]


@pytest.mark.parametrize(("label", "period_day", "expected_pattern", "expected_order"), [
    ("payroll-2026-10-16.csv", "2026-10-16", "numeric_order_independent", None),
    ("payroll-10-10-2026.csv", "2026-10-10", "numeric_order_independent", None),
    ("payroll-10-10.csv", "2026-10-10", "numeric_order_independent_month_day", None),
    ("payroll-13-10.csv", "2026-10-13", "numeric_DMY_month_day", "DMY"),
])
def test_name_numeric_patterns_do_not_invent_orders_and_are_deterministic(
    label, period_day, expected_pattern, expected_order,
):
    result = interpret_date(label, period_days=[date.fromisoformat(period_day)], source_kind="filename")
    profile = make_date_profile("filename", None, [result])

    assert result["status"] == "resolved"
    assert result["pattern"] == expected_pattern
    assert profile == [{"authority": "filename", "numeric_order": expected_order,
                        "grammars": ["numeric_omitted_year"] if "month_day" in expected_pattern
                        else ["numeric_explicit_year"]}]


def test_unzoned_native_datetime_is_kept_and_timezone_aware_value_is_rejected():
    days = _days("2026-10-16", 1)
    naive = interpret_date(_cell("2026-10-16T23:30:00", kind="date"), period_days=days)
    aware = interpret_date(_cell("2026-10-16T23:30:00+02:00", kind="date"), period_days=days)

    assert naive["resolved_date"] == "2026-10-16"
    assert aware["status"] == "error"
    assert aware["reason"] == "invalid_typed_date"


@pytest.mark.parametrize(("text", "expected"), [
    ("October 16, 2026", "2026-10-16"),
    ("16 September 2026", "2026-09-16"),
    ("Sept 16, 2026", "2026-09-16"),
    ("O.c_t (Samsara) 16 2026", "2026-10-16"),
])
def test_written_month_dates_allow_supported_names_and_cleaning(text, expected):
    result = interpret_date(_cell(text), period_days=_days("2026-09-01", 61))

    assert result["status"] == "resolved"
    assert result["resolved_date"] == expected
    assert result["pattern"] == "written_month"
    assert result["explicit_year"] is True


@pytest.mark.parametrize(("text", "expected"), [
    ("Samsara_Oct_16", "2026-10-16"),
    ("O.c_t (Samsara)16", "2026-10-16"),
])
def test_month_token_cleanup_does_not_merge_adjacent_provider_words(text, expected):
    result = interpret_date(text, period_days=_days("2026-10-01", 31), source_kind="filename")

    assert result["resolved_date"] == expected


def test_written_month_omitted_year_resolves_only_when_exactly_one_period_day_matches():
    unique = interpret_date(_cell("O.c_t (Samsara)16"), period_days=_days("2026-10-01", 31))
    absent = interpret_date(_cell("Oct 16"), period_days=_days("2026-09-01", 30))
    repeated = interpret_date(_cell("Oct 16"), period_days=[date(2025, 10, 16), date(2026, 10, 16)])

    assert (unique["resolved_date"], unique["reason"], unique["explicit_year"]) == (
        "2026-10-16", "period_year_inferred", False)
    assert absent["status"] == "outside_period"
    assert absent["resolved_date"] is None
    assert repeated["status"] == "ambiguous"
    assert repeated["candidate_dates"] == ["2025-10-16", "2026-10-16"]


def test_omitted_year_numeric_date_uses_selected_or_unique_calendar_fit():
    days = _days("2026-03-01", 31)
    selected = interpret_date(_cell("03/04"), period_days=days, selected_order="MDY")
    inferred = interpret_date(_cell("13/03"), period_days=days)
    uniquely_fitting = interpret_date(_cell("03/04"), period_days=[date(2026, 3, 4)])
    absent = interpret_date(_cell("03/04"), period_days=[date(2026, 3, 3), date(2026, 3, 5)])

    assert selected["resolved_date"] == "2026-03-04"
    assert selected["explicit_year"] is False
    assert inferred["resolved_date"] == "2026-03-13"
    assert uniquely_fitting["resolved_date"] == "2026-03-04"
    assert uniquely_fitting["reason"] == "period_year_inferred"
    assert absent["status"] == "ambiguous"
    assert absent["resolved_date"] is None


def test_omitted_leap_day_resolves_from_period_day_snapshot():
    result = interpret_date(_cell("Feb 29"), period_days=[date(2024, 2, 29)])

    assert result["resolved_date"] == "2024-02-29"
    assert result["explicit_year"] is False


def test_explicit_wrong_year_is_never_rewritten_to_fit_period():
    result = interpret_date(_cell("Oct 16 2025"), period_days=[date(2026, 10, 16)])

    assert result["status"] == "outside_period"
    assert result["resolved_date"] is None
    assert result["candidate_dates"] == ["2025-10-16"]
    assert result["explicit_year"] is True
    assert result["reason"] == "explicit_date_outside_period"


@pytest.mark.parametrize("text", [
    "October", "16", "Driver 12345", "October 16 17", "October 16 2025 2026",
    "October 16 and November 17",
])
def test_incomplete_or_competing_text_is_not_silently_interpreted(text):
    result = interpret_date(_cell(text), period_days=_days("2026-10-01", 31))

    assert result["resolved_date"] is None
    assert result["status"] in {"unrecognized", "ambiguous"}


def test_explicit_two_digit_year_and_invalid_month_day_are_not_guessed():
    short_year = interpret_date(_cell("10/16/26"), period_days=_days("2026-10-01", 31),
                                selected_order="MDY")
    invalid = interpret_date(_cell("Feb 30 2026"), period_days=_days("2026-02-01", 28))

    assert short_year["resolved_date"] is None
    assert invalid["status"] == "error"
    assert invalid["resolved_date"] is None


@pytest.mark.parametrize("text", ["Oct 16 99", "Oct 16 2026 999", "Oct 16 2026 10/17/2026"])
def test_unconsumed_numbers_and_competing_evidence_block_written_month_inference(text):
    result = interpret_date(text, period_days=_days("2026-10-01", 31), source_kind="filename")

    assert result["status"] in {"ambiguous", "error"}
    assert result["resolved_date"] is None


def test_filename_and_sheet_candidates_require_explicit_choice_when_competing():
    days = _days("2026-10-01", 31)
    ambiguous = interpret_date("Daily 10/15/2026 and 10/16/2026", period_days=days,
                               source_kind="filename")
    one_in_period = interpret_date("Daily 10/15/2025 and 10/16/2026", period_days=days,
                                   source_kind="filename")
    selected = interpret_date("Daily 10/15/2026 and 10/16/2026", period_days=days,
                              source_kind="sheet", selected_candidate="2026-10-16")
    not_candidate = interpret_date("Daily 10/15/2026 and 10/16/2026", period_days=days,
                                   source_kind="filename", selected_candidate="2026-10-17")

    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["candidate_dates"] == ["2026-10-15", "2026-10-16"]
    assert one_in_period["status"] == "ambiguous"
    assert one_in_period["candidate_dates"] == ["2025-10-15", "2026-10-16"]
    assert selected["resolved_date"] == "2026-10-16"
    assert selected["reason"] == "user_selected_candidate"
    assert not_candidate["status"] == "error"


def test_filename_omitted_year_uses_only_a_unique_month_day_in_period():
    result = interpret_date("O.c_t (Samsara) 16", period_days=_days("2026-10-01", 31),
                            source_kind="filename")

    assert result["resolved_date"] == "2026-10-16"
    assert result["reason"] == "period_year_inferred"
    assert result["explicit_year"] is False


def test_selected_omitted_year_candidate_uses_actual_period_year_not_sentinel():
    result = interpret_date("O.c_t (Samsara)16", period_days=_days("2026-10-01", 31),
                            source_kind="filename", selected_candidate="2026-10-16")

    assert result["resolved_date"] == "2026-10-16"
    assert result["reason"] == "user_selected_candidate"


def test_unrecognized_name_can_be_manually_linked_but_invalid_calendar_date_cannot():
    days = _days("2026-10-01", 31)
    manual = interpret_date("field log", period_days=days, source_kind="filename",
                            selected_candidate="2026-10-16")
    invalid = interpret_date("Feb 30 2026", period_days=days, source_kind="filename",
                             selected_candidate="2026-10-16")
    invalid_numeric = interpret_date("2026-02-30", period_days=days, source_kind="filename",
                                     selected_candidate="2026-10-16")

    assert manual["resolved_date"] == "2026-10-16"
    assert manual["reason"] == "manual_name_link"
    assert invalid["status"] == "error"
    assert invalid["resolved_date"] is None
    assert invalid_numeric["status"] == "error"
    assert invalid_numeric["resolved_date"] is None


@pytest.mark.parametrize(("text", "source_kind"), [
    ("2026-02-30", "column"), ("31/31/2026", "filename"),
])
def test_recognized_invalid_numeric_calendar_dates_are_not_manual_unknowns(text, source_kind):
    value = _cell(text) if source_kind == "column" else text
    result = interpret_date(value, period_days=_days("2026-10-01", 31), source_kind=source_kind,
                            selected_candidate="2026-10-16" if source_kind != "column" else None)

    assert result["status"] == "error"
    assert result["reason"] == "invalid_calendar_date"


def test_recognized_outside_period_name_and_explicit_wrong_year_cannot_be_manually_rewritten():
    days = _days("2026-10-01", 31)
    omitted_outside = interpret_date("Nov 16", period_days=days, source_kind="filename",
                                     selected_candidate="2026-10-16")
    wrong_year_with_extra_number = interpret_date("Oct 16 2025 99", period_days=days,
                                                  source_kind="filename",
                                                  selected_candidate="2026-10-16")

    assert omitted_outside["status"] == "outside_period"
    assert omitted_outside["resolved_date"] is None
    assert wrong_year_with_extra_number["status"] == "error"
    assert wrong_year_with_extra_number["resolved_date"] is None


def test_ambiguous_omitted_year_numeric_candidates_never_expose_sentinel_year():
    result = interpret_date(_cell("03/04"), period_days=[date(2026, 3, 4), date(2026, 4, 3)])

    assert result["status"] == "ambiguous"
    assert result["resolved_date"] is None
    assert result["candidate_dates"] == ["2026-03-04", "2026-04-03"]


def test_column_formula_without_cache_is_an_error_and_authorities_stay_separate():
    days = _days("2026-10-01", 31)
    formula = interpret_date(_cell("Oct 16", missing_cache=True), period_days=days)
    column = interpret_date(_cell("Oct 16"), period_days=days, source_kind="column")
    filename = interpret_date("Oct 16", period_days=days, source_kind="filename")

    assert formula["status"] == "error"
    assert formula["reason"] == "missing_formula_cache"
    assert column["resolved_date"] == "2026-10-16"
    assert filename["resolved_date"] == "2026-10-16"


@pytest.mark.parametrize("text", [
    "prefix 2026-10-16", "2026-10-16T23:30:00+02:00", "2026-10-16 extra 10/17/2026",
])
def test_date_column_does_not_extract_numeric_dates_from_unrecognized_text(text):
    result = interpret_date(_cell(text), period_days=_days("2026-10-01", 31))

    assert result["resolved_date"] is None
