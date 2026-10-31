"""Deterministic, bounded date interpretation for PayrollWork source values."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date

from app.imports.mapping import infer_date_order, parse_date

DATE_INTERPRETATION_VERSION = 1
DATE_GRAMMAR_IDS = frozenset({"iso_or_native", "numeric_explicit_year",
                              "numeric_omitted_year", "written_month_day"})

_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,4})([/.-])(\d{1,2})\2(\d{1,4})(?!\d)", re.ASCII)
_NUMERIC_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})([/.-])(\d{1,2})(?!\d)", re.ASCII)
_MONTH_NAMES = (
    (1, ("january", "jan")), (2, ("february", "feb")), (3, ("march", "mar")),
    (4, ("april", "apr")), (5, ("may",)), (6, ("june", "jun")),
    (7, ("july", "jul")), (8, ("august", "aug")),
    (9, ("september", "sept", "sep")), (10, ("october", "oct")),
    (11, ("november", "nov")), (12, ("december", "dec")),
)


def _month_pattern(name: str) -> str:
    return "".join(char + (r"[._]*" if index < len(name) - 1 else "")
                   for index, char in enumerate(name))


_MONTH_PATTERNS = tuple((month, re.compile(
    rf"(?<![A-Za-z])(?:{'|'.join(_month_pattern(name) for name in names)})(?![A-Za-z])", re.I | re.ASCII))
                        for month, names in _MONTH_NAMES)
_NUMBER_TOKEN = re.compile(r"(?<!\d)(\d+)(?!\d)", re.ASCII)
_WHOLE_NUMERIC = re.compile(r"^\d{1,4}[/.-]\d{1,2}(?:[/.-]\d{1,4})?$", re.ASCII)


def _result(status: str, resolved: date | None, candidates: Iterable[date], reason: str,
            pattern: str | None, explicit_year: bool | None, explanation: str) -> dict:
    return {
        "version": DATE_INTERPRETATION_VERSION,
        "status": status,
        "resolved_date": resolved.isoformat() if resolved else None,
        "candidate_dates": sorted({item.isoformat() for item in candidates}),
        "reason": reason,
        "pattern": pattern,
        "explicit_year": explicit_year,
        "explanation": explanation,
    }


def _period_days(days: Iterable[date | str]) -> set[date]:
    result = set()
    for item in days:
        if isinstance(item, date):
            result.add(item)
        elif isinstance(item, str):
            try:
                parsed = date.fromisoformat(item)
            except ValueError:
                continue
            if parsed.isoformat() == item:
                result.add(parsed)
    return result


def date_grammar_id(pattern: str | None) -> str | None:
    """Map parser diagnostics to finite, versioned reusable grammar identifiers."""
    if pattern is None or pattern in {"manual_name_link", "selected_candidate"}:
        return None
    if pattern in {"native_or_iso", "numeric_order_independent", "same_date_multiple_supported_forms"}:
        return "iso_or_native" if pattern == "native_or_iso" else "numeric_explicit_year"
    if pattern == "numeric_order_independent_month_day":
        return "numeric_omitted_year"
    if pattern.startswith("numeric_") and "month_day" in pattern:
        return "numeric_omitted_year"
    if pattern.startswith("numeric_"):
        return "numeric_explicit_year"
    if pattern == "written_month":
        return "written_month_day"
    return None


def make_date_profile(authority: str, numeric_order: str | None,
                      results: Iterable[Mapping]) -> list[dict]:
    """Build compact persisted meaning from interpreted values, not text/IDs."""
    if authority not in {"column", "filename", "sheet"}:
        raise ValueError("unsupported date authority")
    result_list = list(results)
    orders = {numeric_order} if numeric_order else set()
    for item in result_list:
        pattern = item.get("pattern")
        if isinstance(pattern, str):
            match = re.match(r"numeric_(DMY|MDY|YMD)(?:_|$)", pattern)
            if match:
                orders.add(match.group(1))
    if not orders:
        orders.add(None)
    grammars = sorted({grammar for item in result_list
                       if (grammar := date_grammar_id(item.get("pattern"))) is not None})
    return [{"authority": authority, "numeric_order": order, "grammars": grammars}
            for order in sorted(orders, key=lambda value: value or "")]


def _cell_date(cell: Mapping, order: str | None, days: set[date]) -> dict:
    if cell.get("missing_cache"):
        return _result("error", None, (), "missing_formula_cache", None, None,
                       "The date formula has no saved calculated value.")
    value = cell.get("value")
    if value is None or isinstance(value, str) and not value.strip():
        return _result("unrecognized", None, (), "blank", None, None, "No date value was supplied.")

    # Keep the established parser authoritative for native dates, ISO dates, and
    # explicit-year numeric dates. Infer numeric order only using its existing
    # period-aware rules when the caller has not selected an order.
    explicit = parse_date(cell, order)
    if explicit is None and order is None and cell.get("kind") == "text":
        start, end = (min(days), max(days)) if days else (date.min, date.max)
        inferred_order, _state = infer_date_order([cell], start, end, None)
        if inferred_order:
            explicit = parse_date(cell, inferred_order)
            order = inferred_order
    if explicit is not None:
        pattern = "native_or_iso" if cell.get("kind") == "date" or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", str(value).strip(), flags=re.ASCII) else f"numeric_{order or 'selected'}"
        if explicit in days:
            return _result("resolved", explicit, (explicit,), "explicit_date", pattern, True,
                           "Used the explicit date as written.")
        return _result("outside_period", None, (explicit,), "explicit_date_outside_period", pattern, True,
                       "The explicit year/date is outside this payroll period and was not changed.")

    text = str(value).strip()
    if cell.get("kind") != "text":
        return _result("error", None, (), "invalid_typed_date", None, None,
                       "The typed value is not a supported date.")
    has_written_month = any(pattern.search(text) for _month, pattern in _MONTH_PATTERNS)
    if any(char.isdigit() for char in text) and not _WHOLE_NUMERIC.fullmatch(text) and not has_written_month:
        return _result("unrecognized", None, (), "unsupported_numeric_cell_text", None, None,
                       "The Date Column value is not one complete supported numeric date.")
    return _interpret_text(text, days, order, source_kind="column")


def _month_candidates(text: str) -> tuple[list[tuple[date, bool]], bool, bool]:
    month_tokens = [(month, match.start(), match.end())
                    for month, pattern in _MONTH_PATTERNS for match in pattern.finditer(text)]
    if not month_tokens:
        return [], False, False

    numbers = [(match.group(1), match.start(), match.end()) for match in _NUMBER_TOKEN.finditer(text)]
    years = [(int(token), start, end) for token, start, end in numbers if len(token) == 4]
    day_tokens = [(int(token), start, end) for token, start, end in numbers
                  if len(token) <= 2 and 1 <= int(token) <= 31]
    consumed = {index for index, (token, _start, _end) in enumerate(numbers)
                if len(token) == 4 or len(token) <= 2 and 1 <= int(token) <= 31}
    if len(years) > 1 or len(day_tokens) != 1:
        return [], True, False
    year = years[0][0] if years else None
    day = day_tokens[0][0]
    candidates = []
    invalid = False
    for month, _start, _end in month_tokens:
        possible_years = (year,) if year is not None else (2000,)
        try:
            value = date(possible_years[0], month, day)
        except ValueError:
            invalid = True
            continue
        candidates.append((value, year is not None))
    unique = {(value, explicit) for value, explicit in candidates}
    return sorted(unique, key=lambda item: (item[0], item[1])), len(consumed) != len(numbers), invalid


def _numeric_candidates(text: str, days: set[date], selected_order: str | None
                        ) -> tuple[list[tuple[date, bool, str]], bool, bool]:
    full_matches = list(_NUMERIC_DATE.finditer(text))
    short_matches = [match for match in _NUMERIC_MONTH_DAY.finditer(text)
                     if not any(match.start() < full.end() and full.start() < match.end()
                                for full in full_matches)]
    matches = [(match, True) for match in full_matches] + [(match, False) for match in short_matches]
    if not matches:
        return [], False, False
    result = []
    invalid_calendar = False
    for match, full_year in matches:
        source = match.group(0)
        if full_year:
            cell = {"value": source, "kind": "text", "missing_cache": False}
            orders = (selected_order,) if selected_order else ("DMY", "MDY", "YMD")
            parsed = []
            for order in orders:
                candidate = parse_date(cell, order)
                if candidate is not None:
                    parsed.append((candidate, True, f"numeric_{order}"))
            if not parsed:
                invalid_calendar = True
            result.extend(parsed)
        else:
            # A two-component numeric date can omit year only in DMY/MDY order.
            if selected_order is not None and selected_order not in ("DMY", "MDY"):
                continue
            orders = (selected_order,) if selected_order else ("DMY", "MDY")
            alternatives = []
            for order in orders:
                try:
                    first, second = int(match.group(1)), int(match.group(3))
                    month, day = (second, first) if order == "DMY" else (first, second)
                    candidate = date(2000, month, day)
                except (ValueError, TypeError):
                    continue
                alternatives.append((candidate, False, f"numeric_{order}_month_day"))
            if not alternatives:
                invalid_calendar = True
            if selected_order is None and len(alternatives) > 1:
                matching = [item for item in alternatives if any(
                    (day.month, day.day) == (item[0].month, item[0].day) for day in days)]
                if len(matching) == 1:
                    alternatives = matching
            result.extend(alternatives)
    meanings = {(value, explicit) for value, explicit, _pattern in result}
    return result, len(meanings) > 1, invalid_calendar


def _interpret_text(text: str, days: set[date], selected_order: str | None,
                    *, source_kind: str) -> dict:
    named, named_competing, named_invalid = _month_candidates(text)
    numeric, numeric_competing, numeric_invalid = _numeric_candidates(text, days, selected_order)
    # A recognized month token takes part in grammar only when it yields an
    # unambiguous month/day candidate. Competing number/date evidence is not
    # silently discarded in favor of whichever parser ran first.
    candidates = []
    missing_omitted_year = False
    for value, explicit, pattern in numeric:
        if explicit:
            candidates.append((value, True, pattern))
            continue
        matching = sorted(day for day in days if (day.month, day.day) == (value.month, value.day))
        if not matching:
            missing_omitted_year = True
        candidates.extend((day, False, pattern) for day in matching)
    for value, explicit in named:
        if explicit:
            candidates.append((value, True, "written_month"))
            continue
        matching = sorted(day for day in days if (day.month, day.day) == (value.month, value.day))
        if not matching:
            missing_omitted_year = True
        candidates.extend((day, False, "written_month") for day in matching)
    distinct = {(value, explicit, pattern) for value, explicit, pattern in candidates}
    if named_invalid or numeric_invalid:
        return _result("error", None, (item[0] for item in distinct), "invalid_calendar_date",
                       "numeric_or_written_date", True,
                       "The recognized date expression is not a valid calendar date.")
    meanings = {(value, explicit) for value, explicit, _pattern in distinct}
    if len(meanings) == 1 and len(distinct) > 1:
        value, explicit = next(iter(meanings))
        pattern = ("numeric_order_independent" if explicit else "numeric_order_independent_month_day") \
            if all(item[2].startswith("numeric_") for item in distinct) else "same_date_multiple_supported_forms"
        distinct = {(value, explicit, pattern)}
    if named_competing or numeric_competing or len(distinct) > 1:
        dates = [value for value, _explicit, _pattern in distinct]
        return _result("ambiguous", None, dates, "competing_date_evidence", None, None,
                       "More than one supported date interpretation is present; choose one explicitly.")
    if not distinct:
        if named_invalid:
            return _result("error", None, (), "invalid_calendar_date", "written_month", None,
                           "The written month and day do not form a valid calendar date.")
        if numeric_competing or named_competing:
            return _result("ambiguous", None, (), "competing_date_evidence", None, None,
                           "More than one supported date interpretation is present; choose one explicitly.")
        if missing_omitted_year and not numeric_competing and not named_competing:
            pattern = next((item[2] for item in numeric), "written_month")
            return _result("outside_period", None, (), "omitted_date_outside_period", pattern, False,
                           "No day with this month and day exists in the frozen payroll period.")
        return _result("unrecognized", None, (), "unsupported_or_incomplete_text", None, None,
                       "The text does not contain one complete supported date.")

    candidate, explicit, pattern = next(iter(distinct))
    if explicit:
        if candidate in days:
            return _result("resolved", candidate, (candidate,), "explicit_date", pattern, True,
                           "Used the explicit date as written.")
        return _result("outside_period", None, (candidate,), "explicit_date_outside_period", pattern, True,
                       "The explicit year/date is outside this payroll period and was not changed.")

    return _result("resolved", candidate, (candidate,), "period_year_inferred", pattern, False,
                   "The omitted year matches exactly one day in the frozen payroll period.")


def _name_candidates(text: str, days: set[date], selected_order: str | None):
    numeric, numeric_competing, numeric_invalid = _numeric_candidates(text, days, selected_order)
    named, named_competing, named_invalid = _month_candidates(text)
    candidates = []
    missing_omitted_year = False
    for candidate, explicit, pattern in numeric:
        if explicit:
            candidates.append((candidate, True, pattern))
            continue
        matching = sorted(day for day in days if (day.month, day.day) == (candidate.month, candidate.day))
        if not matching:
            missing_omitted_year = True
        candidates.extend((day, False, pattern) for day in matching)
    for candidate, explicit in named:
        if explicit:
            candidates.append((candidate, True, "written_month"))
            continue
        matching = sorted(day for day in days if (day.month, day.day) == (candidate.month, candidate.day))
        if not matching:
            missing_omitted_year = True
        candidates.extend((day, False, "written_month") for day in matching)
    meanings = {(value, explicit) for value, explicit, _pattern in candidates}
    return (candidates, numeric_competing or named_competing, named_invalid or numeric_invalid,
            missing_omitted_year, meanings)


def interpret_date(value, *, period_days: Iterable[date | str], selected_order: str | None = None,
                   source_kind: str = "column", selected_candidate: str | None = None) -> dict:
    """Interpret a Date Column cell or filename/sheet label without date guessing.

    ``source_kind`` is ``column``, ``filename`` or ``sheet``. Provider selection
    is performed by the caller; this function only interprets its chosen value.
    A filename/sheet may be explicitly resolved by passing a candidate ISO date.
    """
    days = _period_days(period_days)
    if source_kind not in {"column", "filename", "sheet"}:
        raise ValueError("source_kind must be column, filename, or sheet")
    if source_kind == "column":
        if not isinstance(value, Mapping):
            raise TypeError("Date Column values must use canonical cell metadata")
        return _cell_date(value, selected_order, days)

    text = "" if value is None else str(value)
    candidates, competing, invalid_calendar, missing_omitted_year, meanings = _name_candidates(
        text, days, selected_order)
    distinct = {(date_value, explicit, pattern) for date_value, explicit, pattern in candidates}
    if len(meanings) == 1 and len(distinct) > 1:
        value, explicit = next(iter(meanings))
        pattern = ("numeric_order_independent" if explicit else "numeric_order_independent_month_day") \
            if all(item[2].startswith("numeric_") for item in distinct) else "same_date_multiple_supported_forms"
        distinct = {(value, explicit, pattern)}
    candidate_dates = sorted({item[0] for item in distinct})
    if invalid_calendar:
        return _result("error", None, candidate_dates, "invalid_calendar_date", "written_month", None,
                       "The written month and day do not form a valid calendar date.")
    if missing_omitted_year and not candidate_dates:
        return _result("outside_period", None, (), "omitted_date_outside_period", "written_month", False,
                       "No day with this month and day exists in the frozen payroll period.")
    if selected_candidate is not None:
        try:
            selected = date.fromisoformat(selected_candidate)
        except ValueError:
            return _result("error", None, candidate_dates, "invalid_candidate_selection", None, None,
                           "The selected date candidate is not a valid ISO date.")
        if selected.isoformat() != selected_candidate or candidate_dates and selected not in candidate_dates:
            return _result("error", None, candidate_dates, "candidate_not_found", None, None,
                           "The selected date is not a candidate from this filename or sheet name.")
        if selected not in days:
            return _result("outside_period", None, (selected,), "explicit_date_outside_period", "selected_candidate", True,
                           "The selected filename/sheet date is outside this payroll period.")
        reason = "user_selected_candidate" if candidate_dates else "manual_name_link"
        selected_patterns = [item[2] for item in distinct if item[0] == selected]
        pattern = selected_patterns[0] if len(set(selected_patterns)) == 1 and selected_patterns else "manual_name_link"
        explanation = ("Used the date candidate explicitly selected by the user." if candidate_dates else
                       "Used the date explicitly supplied for otherwise unrecognized name evidence.")
        return _result("resolved", selected, (selected,), reason, pattern, True,
                       explanation)

    if competing or len(meanings) > 1:
        return _result("ambiguous", None, candidate_dates, "competing_date_evidence", None, None,
                       "More than one supported date interpretation is present; choose one explicitly.")

    if len(candidate_dates) > 1:
        return _result("ambiguous", None, candidate_dates, "multiple_name_date_candidates", None, None,
                       "The filename or sheet name contains multiple dates; select the intended candidate.")
    if not candidate_dates:
        if missing_omitted_year:
            return _result("outside_period", None, (), "omitted_date_outside_period", "written_month", False,
                           "No day with this month and day exists in the frozen payroll period.")
        return _result("unrecognized", None, (), "no_supported_date_candidate", None, None,
                       "No supported date candidate was found in the filename or sheet name.")
    candidate = candidate_dates[0]
    explicit = next(item[1] for item in distinct if item[0] == candidate)
    pattern = next(item[2] for item in distinct if item[0] == candidate)
    if explicit:
        if candidate not in days:
            return _result("outside_period", None, (candidate,), "explicit_date_outside_period", pattern, True,
                           "The explicit filename/sheet year is outside this payroll period and was not changed.")
        return _result("resolved", candidate, (candidate,), "explicit_date", pattern, True,
                       "Used the explicit filename/sheet date.")
    matching = sorted(day for day in days if (day.month, day.day) == (candidate.month, candidate.day))
    if len(matching) == 1:
        return _result("resolved", matching[0], (matching[0],), "period_year_inferred", pattern, False,
                       "The omitted year matches exactly one day in the frozen payroll period.")
    if len(matching) > 1:
        return _result("ambiguous", None, matching, "period_year_ambiguous", pattern, False,
                       "The omitted year matches more than one day in the frozen payroll period.")
    return _result("outside_period", None, (), "omitted_date_outside_period", pattern, False,
                   "No day with this month and day exists in the frozen payroll period.")
