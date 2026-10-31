"""Pure category evidence and repeated-row quantity preparation helpers."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.imports.mapping import canonical
from app.imports.quantity_math import exact_decimal_sum, parse_sum_operand
from app.payroll.definition_calculation import quantity_error


class CategoryEvidenceError(ValueError):
    """The selected category column contains a value unsafe for exact matching."""


def category_key_from_json(value: Mapping) -> tuple[str, str | bool | None]:
    """Validate and canonicalize one public/persisted typed category key."""
    if not isinstance(value, Mapping) or set(value) != {"type", "value"}:
        raise CategoryEvidenceError("A category key must contain exactly type and value.")
    kind, item = value["type"], value["value"]
    if kind == "blank" and item is None:
        return "blank", None
    if kind == "text" and isinstance(item, str):
        return "text", item
    if kind == "number" and isinstance(item, str):
        return typed_category_key({"kind": "number", "value": item})
    if kind == "boolean" and isinstance(item, bool):
        return "boolean", item
    if kind == "date" and isinstance(item, str):
        return typed_category_key({"kind": "date", "value": item})
    raise CategoryEvidenceError("Category key type/value pair is invalid.")


def category_key_to_json(key: tuple[str, str | bool | None]) -> dict:
    return {"type": key[0], "value": key[1]}


def quantity_contribution(cell: dict) -> dict:
    """Parse a repeated-row quantity operand without pre-validating each addend."""
    value, error, is_blank = parse_sum_operand(cell)
    if error:
        return {"state": "invalid", "error": error, "quantity": None}
    if is_blank:
        return {"state": "blank", "quantity": None}
    return {"state": "present", "quantity": value}


def typed_category_key(cell: Mapping) -> tuple[str, str | bool | None]:
    """Return a JSON-safe, type-preserving key for one canonical source cell."""
    if not isinstance(cell, Mapping):
        raise CategoryEvidenceError("Category evidence must be a canonical cell object.")
    if cell.get("missing_cache"):
        raise CategoryEvidenceError("Formula without a calculated value cannot be category evidence.")
    kind = cell.get("kind")
    value = cell.get("value")
    if kind == "blank" or value is None and kind in {"text", "blank"}:
        return "blank", None
    if kind == "text" and isinstance(value, str):
        return "text", value
    if kind == "number" and not isinstance(value, bool):
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise CategoryEvidenceError("Category number is not a valid decimal.") from exc
        if not number.is_finite():
            raise CategoryEvidenceError("Category number must be finite.")
        return "number", canonical(number)
    if kind == "boolean" and isinstance(value, bool):
        return "boolean", value
    if kind == "date":
        if isinstance(value, datetime):
            return "date", value.isoformat()
        if isinstance(value, date):
            return "date", value.isoformat()
        if isinstance(value, str):
            try:
                return "date", date.fromisoformat(value).isoformat()
            except ValueError as exc:
                try:
                    return "date", datetime.fromisoformat(value).isoformat()
                except ValueError:
                    raise CategoryEvidenceError("Category date is not a canonical date or datetime.") from exc
    raise CategoryEvidenceError("Category values must be text, number, boolean, date, or blank.")


def inspect_category_evidence(cells: list[dict], *, previously_validated: bool = False) -> dict:
    """Describe typed values and suitability without a domain classifier or cap."""
    counts: dict[tuple[str, str | bool | None], int] = {}
    for cell in cells:
        key = typed_category_key(cell)
        counts[key] = counts.get(key, 0) + 1
    repeated = any(count > 1 for count in counts.values())
    return {
        "suitable": repeated or previously_validated,
        "repetition_observed": repeated,
        "distinct_count": len(counts),
        "values": [{"type": kind, "value": value, "count": count}
                   for (kind, value), count in sorted(
                       counts.items(), key=lambda item: (item[0][0], str(item[0][1])))],
    }


def resolve_category_decisions(observed_keys: set[tuple[str, str | bool | None]],
                               decisions: dict[tuple[str, str | bool | None], str], *,
                               previously_validated_keys: set[tuple[str, str | bool | None]] | None = None) -> dict:
    """Require an explicit Include/Exclude decision for every observed value."""
    remembered = previously_validated_keys or set()
    invalid = set(decisions) - observed_keys - remembered
    if invalid:
        raise CategoryEvidenceError("Category decisions contain values not present in this evidence.")
    invalid_outcomes = {value for value in decisions.values() if value not in {"Include", "Exclude"}}
    if invalid_outcomes:
        raise CategoryEvidenceError("Each category decision must be Include or Exclude.")
    unresolved = observed_keys - set(decisions) - remembered
    retained = {key: outcome for key, outcome in decisions.items() if key in remembered - observed_keys}
    return {
        "ready": not unresolved,
        "unresolved": sorted(unresolved, key=lambda item: (item[0], str(item[1]))),
        "included": {key for key, outcome in decisions.items() if outcome == "Include"},
        "excluded": {key for key, outcome in decisions.items() if outcome == "Exclude"},
        "retained_absent": retained,
    }


def aggregate_quantity_contributions(contributions: list[dict], *, input_type: str,
                                     max_work_digits: int) -> dict:
    """Prepare one exact target quantity, withholding incomplete/uncertain groups."""
    active = []
    errors = []
    withheld_reasons = set()
    for contribution in contributions:
        state = contribution.get("state")
        if state == "excluded":
            continue
        if state in {"unknown_category", "unresolved_identity"}:
            withheld_reasons.add(state)
            continue
        if state == "invalid":
            errors.append(contribution.get("error", "Invalid quantity."))
            continue
        if state in {"blank", "missing", "incomplete_sum"}:
            withheld_reasons.add("incomplete_contribution")
            continue
        if state != "present" or not isinstance(contribution.get("quantity"), Decimal):
            raise ValueError("Each contribution must have a recognized state and Decimal quantity.")
        quantity = contribution["quantity"]
        if not quantity.is_finite():
            errors.append("Quantity must be a finite number.")
            continue
        active.append(quantity)
    if errors:
        return {"status": "invalid", "reason": errors[0], "quantity": None}
    if withheld_reasons:
        priority = ("unknown_category", "unresolved_identity", "incomplete_contribution")
        reason = next(reason for reason in priority if reason in withheld_reasons)
        return {"status": "withheld", "reason": reason, "quantity": None}
    if not active:
        return {"status": "absent", "reason": "no_included_contributions", "quantity": None}
    try:
        total = exact_decimal_sum(active, max_work_digits)
    except ArithmeticError:
        return {"status": "invalid", "reason": "numeric_work_limit", "quantity": None}
    error = quantity_error(input_type, total)
    if error is None and total.as_tuple().exponent < -4:
        error = "At most four decimal places are supported."
    if error:
        return {"status": "invalid", "reason": error, "quantity": None}
    return {"status": "complete", "reason": None, "quantity": total}


def aggregate_rows(rows: list[dict], *, rule: str, category_column: str | None,
                   category_decisions: dict[tuple[str, str | bool | None], str] | None,
                   target: str, input_type: str, max_work_digits: int) -> dict:
    """Apply an explicit repeated-row rule to already-resolved Driver/day rows."""
    if rule not in {"RejectRepeated", "SumRows", "FilterThenSum"}:
        raise ValueError("Unknown repeated-row rule.")
    if rule == "FilterThenSum" and not category_column:
        raise ValueError("FilterThenSum requires a selected category column.")
    if rule != "FilterThenSum" and category_column is not None:
        raise ValueError("A category column is valid only for FilterThenSum.")

    selected = []
    category_keys = set()
    for row in rows:
        if rule == "FilterThenSum":
            cell = row.get("category_cell")
            if cell is None:
                item = row.get("targets", {}).get(target, {"state": "missing"})
                selected.append(item if item.get("state") == "invalid" else {"state": "unknown_category"})
                continue
            key = typed_category_key(cell)
            category_keys.add(key)
            outcome = (category_decisions or {}).get(key)
            if outcome is None:
                item = row.get("targets", {}).get(target, {"state": "missing"})
                selected.append(item if item.get("state") == "invalid" else {"state": "unknown_category"})
                continue
            if outcome == "Exclude":
                selected.append({"state": "excluded"})
                continue
            if outcome != "Include":
                raise CategoryEvidenceError("Each category decision must be Include or Exclude.")
        if row.get("identity_state") != "resolved":
            item = row.get("targets", {}).get(target, {"state": "missing"})
            selected.append(item if item.get("state") == "invalid" else {"state": "unresolved_identity"})
            continue
        item = row.get("targets", {}).get(target, {"state": "missing"})
        selected.append(item)

    if rule == "RejectRepeated":
        contributing = [item for item in selected if item.get("state") not in {"excluded"}]
        if len(contributing) > 1:
            return {"status": "invalid", "reason": "duplicate_fact", "quantity": None,
                    "category_keys": category_keys}
    result = aggregate_quantity_contributions(selected, input_type=input_type,
                                              max_work_digits=max_work_digits)
    return {**result, "category_keys": category_keys,
            "contribution_count": sum(item.get("state") == "present" for item in selected)}
