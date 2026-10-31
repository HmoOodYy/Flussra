"""Pure, versioned semantic identities for completed PayrollWork input."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from app.compensation.schemas import CALCULATION_METHODS, INPUT_TYPES
from app.imports.aggregation import category_key_from_json, category_key_to_json
from app.imports.headers import normalize_header
from app.imports.mapping import canonical, encoded, normalized_identifier

SEMANTIC_INPUT_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_TARGET_MEANING_KEYS = frozenset({
    "code", "name", "input_type", "unit", "method", "method_version", "rate_shape", "active",
})
_IDENTITY_KINDS = frozenset({"DriverNumber", "EmployeeKey", "DriverName"})


class SemanticInputError(ValueError):
    """Semantic input is unresolved, malformed, or unsupported for repeat matching."""


def _target_meaning(value: Mapping) -> dict:
    if not isinstance(value, Mapping) or set(value) != _TARGET_MEANING_KEYS:
        raise SemanticInputError("Target meaning does not match the supported frozen definition shape.")
    if (not isinstance(value["code"], str) or not isinstance(value["name"], str)
            or not isinstance(value["input_type"], str) or value["input_type"] not in INPUT_TYPES
            or (value["unit"] is not None and not isinstance(value["unit"], str))
            or not isinstance(value["method"], str) or value["method"] not in CALCULATION_METHODS
            or type(value["method_version"]) is not int or value["method_version"] < 1
            or not isinstance(value["rate_shape"], str) or type(value["active"]) is not bool):
        raise SemanticInputError("Target meaning contains malformed frozen definition fields.")
    return canonical(dict(value))


def _digest(value: object) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def _selector(value: Mapping) -> dict:
    if not isinstance(value, Mapping) or set(value) - {"header", "occurrences"} or "header" not in value:
        raise SemanticInputError("A source selector must contain a header and optional occurrences.")
    header = value["header"]
    if not isinstance(header, str) or not normalize_header(header):
        raise SemanticInputError("A source selector needs a non-empty header.")
    result = {"header": normalize_header(header)}
    occurrences = value.get("occurrences")
    if occurrences is not None:
        if not isinstance(occurrences, list) or not occurrences:
            raise SemanticInputError("Selector occurrence alternatives must be a non-empty list.")
        normalized = []
        for occurrence in occurrences:
            if not isinstance(occurrence, Mapping) or set(occurrence) not in (
                {"duplicate_signature", "occurrence_ordinal"}, {"occurrence_ordinal"}
            ):
                raise SemanticInputError("A selector occurrence is malformed.")
            signature = occurrence.get("duplicate_signature")
            ordinal = occurrence["occurrence_ordinal"]
            if signature is not None and (not isinstance(signature, str) or not _SHA256.fullmatch(signature)):
                raise SemanticInputError("A duplicate occurrence signature is malformed.")
            if ordinal is not None and (type(ordinal) is not int or ordinal < 1):
                raise SemanticInputError("A duplicate occurrence ordinal is malformed.")
            # Existing mapping validation checks the accepted geometry. Semantic meaning
            # retains only the logical duplicate position, not a layout-wide signature.
            normalized.append({"occurrence_ordinal": ordinal})
        unique_occurrences = {encoded(item): item for item in normalized}
        result["occurrences"] = [unique_occurrences[key] for key in sorted(unique_occurrences)]
    return result


def canonical_rule_meaning(*, target_meaning: Mapping, transform: str, inputs: Sequence,
                           shared_columns: Mapping, row_handling: Mapping,
                           date_authority: Mapping) -> dict:
    """Build relevant rule meaning without IDs, file layout, or presentation metadata."""
    target_meaning = _target_meaning(target_meaning)
    if transform not in {"Direct", "Sum"} or not isinstance(inputs, Sequence) or isinstance(inputs, (str, bytes)):
        raise SemanticInputError("Quantity transform or selected inputs are unsupported.")
    canonical_inputs = [_selector(item) for item in inputs]
    if (transform == "Direct" and len(canonical_inputs) != 1) or (
        transform == "Sum" and len(canonical_inputs) < 2
    ):
        raise SemanticInputError("Direct needs one input and Sum needs at least two inputs.")
    if len({encoded(item) for item in canonical_inputs}) != len(canonical_inputs):
        raise SemanticInputError("A quantity occurrence cannot be consumed more than once.")
    if not isinstance(shared_columns, Mapping):
        raise SemanticInputError("Shared source roles are malformed.")
    shared = {}
    for role, selector in shared_columns.items():
        if not isinstance(role, str) or role not in {
            "DriverNumber", "EmployeeKey", "DriverName", "WorkDate", "Filter"
        }:
            raise SemanticInputError("A shared source role is unsupported.")
        shared[role] = _selector(selector)
    if not isinstance(row_handling, Mapping):
        raise SemanticInputError("Repeated-row meaning is malformed.")
    mode = row_handling.get("mode", "RejectRepeated")
    if mode not in {"RejectRepeated", "SumRows", "FilterThenSum"}:
        raise SemanticInputError("Repeated-row mode is unsupported.")
    if mode == "FilterThenSum":
        if "Filter" not in shared:
            raise SemanticInputError("Filtered rows require a selected category column.")
        row_meaning = {"mode": mode, "filter": shared["Filter"]}
    else:
        row_meaning = {"mode": mode}
        shared.pop("Filter", None)
    if not isinstance(date_authority, Mapping) or set(date_authority) - {"kind", "selector"}:
        raise SemanticInputError("Date authority meaning is malformed.")
    date_kind = date_authority.get("kind")
    if date_kind not in {"column", "filename", "sheet"}:
        raise SemanticInputError("Date authority kind is unsupported.")
    if date_kind == "column":
        date_selector = date_authority.get("selector")
        if date_selector is None:
            date_selector = shared.get("WorkDate")
        if date_selector is None:
            raise SemanticInputError("Column date authority needs its selected source column.")
        date_meaning = {"kind": date_kind, "selector": _selector(date_selector)}
    else:
        if "selector" in date_authority:
            raise SemanticInputError("Filename/sheet date authority has no column selector.")
        # A remembered WorkDate column is ancillary while name authority is selected.
        shared.pop("WorkDate", None)
        date_meaning = {"kind": date_kind}
    # Sum is commutative; sorting keeps UI/request ordering out of rule identity while
    # the occurrence selectors still bind each operand to its actual source meaning.
    canonical_inputs.sort(key=encoded)
    return canonical({
        "target": target_meaning, "transform": transform, "inputs": canonical_inputs,
        "shared_columns": shared, "row_handling": row_meaning, "date_authority": date_meaning,
    })


def _validated_rule_meaning(value: Mapping) -> dict:
    if not isinstance(value, Mapping) or set(value) != {
        "target", "transform", "inputs", "shared_columns", "row_handling", "date_authority"
    }:
        raise SemanticInputError("Source-rule meaning has an unsupported shape.")
    normalized = canonical_rule_meaning(
        target_meaning=value["target"], transform=value["transform"], inputs=value["inputs"],
        shared_columns=value["shared_columns"], row_handling=value["row_handling"],
        date_authority=value["date_authority"],
    )
    if encoded(normalized) != encoded(value):
        raise SemanticInputError("Source-rule meaning is not in canonical form.")
    return normalized


_PREPARED_RULE_TOKEN = object()


def _freeze_json(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class PreparedRuleMeaning:
    """Preview-local immutable rule snapshot with canonical selector keys."""

    meaning: Mapping
    encoded_meaning: str
    selectors: tuple[Mapping, ...]
    selector_keys: tuple[str, ...]
    _token: object


def prepare_rule_meaning(value: Mapping) -> PreparedRuleMeaning:
    meaning = _validated_rule_meaning(value)
    selectors = tuple(_freeze_json(item) for item in meaning["inputs"])
    selector_keys = tuple(encoded(item) for item in selectors)
    return PreparedRuleMeaning(_freeze_json(meaning), encoded(meaning), selectors, selector_keys,
                               _PREPARED_RULE_TOKEN)


def _day(value) -> str:
    if isinstance(value, datetime):
        raise SemanticInputError("WorkDate must be a calendar date, not a datetime.")
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise SemanticInputError("Resolved WorkDate is missing or invalid.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SemanticInputError("Resolved WorkDate is missing or invalid.") from exc
    if parsed.isoformat() != value:
        raise SemanticInputError("Resolved WorkDate must use canonical YYYY-MM-DD form.")
    return value


def _driver_meaning(value: Mapping) -> dict:
    if not isinstance(value, Mapping) or set(value) != {"driver_number", "employee_key", "name"}:
        raise SemanticInputError("Resolved Driver meaning is malformed.")
    result = {}
    for key, kind in (("driver_number", "DriverNumber"), ("employee_key", "EmployeeKey"), ("name", "DriverName")):
        item = value[key]
        if item is not None and not isinstance(item, str):
            raise SemanticInputError("Canonical Driver identifiers must be text or null.")
        result[key] = normalized_identifier(kind, item) or None
    if not any(result.values()):
        raise SemanticInputError("A resolved Driver needs a canonical stable meaning.")
    if result["driver_number"] or result["employee_key"]:
        result["name"] = None
    return result


def _operand(value: Mapping, *, prepared_selector: Mapping | None = None) -> dict:
    if not isinstance(value, Mapping) or set(value) - {"selector", "state", "value"}:
        raise SemanticInputError("Quantity operand meaning is malformed.")
    if prepared_selector is None:
        selector = _selector(value.get("selector"))
    else:
        if value.get("selector") is not prepared_selector:
            raise SemanticInputError("Prepared quantity operand does not match its immutable selector.")
        selector = prepared_selector
    state = value.get("state")
    if state in {"absent", "blank"}:
        if "value" in value:
            raise SemanticInputError("Blank or absent operands cannot carry a quantity.")
        return {"selector": selector, "state": state}
    if state != "value" or "value" not in value:
        raise SemanticInputError("Quantity operand is unresolved or invalid.")
    raw = value["value"]
    if isinstance(raw, bool):
        raise SemanticInputError("Boolean values are not numeric quantity operands.")
    try:
        number = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SemanticInputError("Quantity operand is not a canonical finite decimal.") from exc
    if not number.is_finite():
        raise SemanticInputError("Quantity operand is not a canonical finite decimal.")
    return {"selector": selector, "state": "value", "value": canonical(number)}


def _binding_entries(bindings: Iterable) -> list[dict]:
    if not isinstance(bindings, Iterable) or isinstance(bindings, (str, bytes, Mapping)):
        raise SemanticInputError("Driver binding evidence must be iterable.")
    result = []
    for binding in bindings:
        if not isinstance(binding, Mapping) or set(binding) != {"kind", "value", "driver_id"}:
            raise SemanticInputError("Driver binding evidence is malformed.")
        kind, value, driver_id = binding["kind"], binding["value"], binding["driver_id"]
        if kind not in _IDENTITY_KINDS or not isinstance(value, str) or not value.strip():
            raise SemanticInputError("Driver binding stable identifier is invalid.")
        if type(driver_id) is not int or driver_id < 1:
            raise SemanticInputError("Driver binding ID must be a positive integer.")
        result.append({"kind": kind, "value": normalized_identifier(kind, value), "driver_id": driver_id})
    unique = {encoded(item): item for item in result}
    return [unique[key] for key in sorted(unique)]


class TargetReceiptAccumulator:
    """Incrementally fold one target's normalized rows into bounded hash tokens."""

    def __init__(self, *, scope: Mapping, pay_definition_id: int, target_meaning: Mapping,
                 rule_meaning: Mapping, prepared_rule: PreparedRuleMeaning | None = None):
        if not isinstance(scope, Mapping) or set(scope) != {"company_id", "branch_id", "period_id"}:
            raise SemanticInputError("Receipt scope is malformed.")
        if any(type(scope[key]) is not int or scope[key] < 1 for key in scope):
            raise SemanticInputError("Receipt scope IDs must be positive integers.")
        if type(pay_definition_id) is not int or pay_definition_id < 1:
            raise SemanticInputError("Receipt target ID must be a positive integer.")
        target_meaning = _target_meaning(target_meaning)
        self.scope = dict(scope)
        self.pay_definition_id = pay_definition_id
        self.target_meaning = canonical(dict(target_meaning))
        if prepared_rule is None:
            self.rule_meaning = _validated_rule_meaning(rule_meaning)
        elif not isinstance(prepared_rule, PreparedRuleMeaning):
            raise SemanticInputError("Prepared source-rule meaning is malformed.")
        elif prepared_rule._token is not _PREPARED_RULE_TOKEN:
            raise SemanticInputError("Prepared source-rule meaning is not validated.")
        elif encoded(rule_meaning) != prepared_rule.encoded_meaning:
            raise SemanticInputError("Prepared and supplied source-rule meanings disagree.")
        else:
            self.rule_meaning = prepared_rule.meaning
        if encoded(self.rule_meaning["target"]) != encoded(self.target_meaning):
            raise SemanticInputError("Receipt target and source-rule target meaning disagree.")
        self.row_tokens = []
        self.effective_rule_meanings = {}
        self.binding_entries = {}
        self._prepared_rules = ({prepared_rule.encoded_meaning}
                                if prepared_rule is not None else set())
        self.invalid = False

    def mark_invalid(self):
        self.invalid = True

    def add(self, row: Mapping, bindings: Iterable = ()):
        self._add(row, bindings, prepared=None)

    def add_prepared(self, row: Mapping, bindings: Iterable, prepared: PreparedRuleMeaning):
        """Trusted preview path for a rule already validated and frozen in this call."""
        if not isinstance(prepared, PreparedRuleMeaning) or prepared._token is not _PREPARED_RULE_TOKEN:
            raise SemanticInputError("Prepared source-rule meaning is malformed.")
        if isinstance(row, Mapping) and "rule_meaning" in row:
            raise SemanticInputError("Prepared source rows cannot replace their immutable rule.")
        self._add(row, bindings, prepared=prepared)

    def _add(self, row: Mapping, bindings: Iterable, *, prepared: PreparedRuleMeaning | None):
        if not isinstance(row, Mapping):
            raise SemanticInputError("Receipt row is malformed.")
        state = row.get("state")
        if state == "excluded":
            return
        if set(row) - {"state", "identity_status", "requires_confirmation", "driver", "work_date",
                       "operands", "category", "category_decision", "rule_meaning"}:
            raise SemanticInputError("Included semantic rows cannot contain presentation or audit fields.")
        if state != "included":
            raise SemanticInputError("Invalid, unresolved, or unknown source evidence cannot be repeat-matched.")
        if row.get("identity_status") != "Exact" or row.get("requires_confirmation") is not False:
            raise SemanticInputError("Only resolved, confirmed-as-exact identities can be repeat-matched.")
        driver = _driver_meaning(row.get("driver"))
        day = _day(row.get("work_date"))
        operands = row.get("operands")
        if not isinstance(operands, list) or not operands:
            raise SemanticInputError("Included quantity evidence needs its physical operands.")
        if prepared is None:
            row_rule = self.rule_meaning
            if "rule_meaning" in row:
                row_rule = _validated_rule_meaning(row["rule_meaning"])
                if encoded(row_rule["target"]) != encoded(self.rule_meaning["target"]):
                    raise SemanticInputError("A source-unit rule cannot change the target meaning.")
            canonical_operands = [_operand(item) for item in operands]
            selectors = [encoded(item["selector"]) for item in canonical_operands]
            if len(selectors) != len(set(selectors)):
                raise SemanticInputError("A physical quantity occurrence cannot be repeated in one contribution.")
            rule_key = encoded(row_rule)
            expected_selectors = [encoded(item) for item in row_rule["inputs"]]
            if sorted(selectors) != sorted(expected_selectors):
                raise SemanticInputError("Contribution operands do not match the saved target inputs.")
            canonical_operands.sort(key=encoded)
        else:
            if prepared.encoded_meaning not in self._prepared_rules:
                if encoded(prepared.meaning["target"]) != encoded(self.target_meaning):
                    raise SemanticInputError("A source-unit rule cannot change the target meaning.")
                self._prepared_rules.add(prepared.encoded_meaning)
            row_rule = prepared.meaning
            if len(operands) != len(prepared.selectors):
                raise SemanticInputError("Contribution operands do not match the saved target inputs.")
            canonical_operands = [_operand(item, prepared_selector=selector)
                                  for item, selector in zip(operands, prepared.selectors, strict=True)]
            rule_key = prepared.encoded_meaning
        self.effective_rule_meanings[rule_key] = row_rule
        category = row.get("category")
        decision = row.get("category_decision")
        if row_rule.get("row_handling", {}).get("mode") == "FilterThenSum":
            if decision != "Include" or not isinstance(category, Mapping):
                raise SemanticInputError("Included filtered rows need a resolved Include category decision.")
            category_meaning = {"key": category_key_to_json(category_key_from_json(category)),
                                "decision": "Include"}
        else:
            category_meaning = None
        self.row_tokens.append(_digest({"rule": row_rule, "driver": driver, "work_date": day,
                                        "operands": canonical_operands, "category": category_meaning}))
        for binding in _binding_entries(bindings):
            self.binding_entries[encoded(binding)] = binding

    def build(self) -> dict:
        if self.invalid:
            raise SemanticInputError("Invalid source evidence prevents a repeat receipt.")
        self.row_tokens.sort()
        effective_rules = [self.effective_rule_meanings[key]
                           for key in sorted(self.effective_rule_meanings)]
        bindings = [self.binding_entries[key] for key in sorted(self.binding_entries)]
        input_digest = _digest({"version": SEMANTIC_INPUT_VERSION, "target": self.target_meaning,
                                "rule_meanings": effective_rules, "row_hashes": self.row_tokens,
                                "record_count": len(self.row_tokens)})
        return {
            "version": SEMANTIC_INPUT_VERSION, "scope": self.scope,
            "pay_definition_id": self.pay_definition_id, "target_meaning": self.target_meaning,
            "rule_meaning_sha256": _digest({"version": SEMANTIC_INPUT_VERSION, "rules": effective_rules}),
            "input_sha256": input_digest,
            "binding_sha256": _digest({"version": SEMANTIC_INPUT_VERSION, "bindings": bindings}),
            "record_count": len(self.row_tokens), "binding_count": len(bindings),
        }


def build_target_receipt(*, scope: Mapping, pay_definition_id: int, target_meaning: Mapping,
                         rule_meaning: Mapping, rows: Iterable, bindings: Iterable) -> dict:
    """Build a compact receipt without retaining projected source rows."""
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes, Mapping)):
        raise SemanticInputError("Receipt input rows must be iterable row evidence.")
    accumulator = TargetReceiptAccumulator(scope=scope, pay_definition_id=pay_definition_id,
                                           target_meaning=target_meaning, rule_meaning=rule_meaning)
    for row in rows:
        accumulator.add(row)
    for binding in _binding_entries(bindings):
        accumulator.binding_entries[encoded(binding)] = binding
    return accumulator.build()

def validate_target_receipt(value: Mapping) -> dict:
    """Validate the exact supported receipt shape; never interpret older versions."""
    required = {"version", "scope", "pay_definition_id", "target_meaning", "rule_meaning_sha256",
                "input_sha256", "binding_sha256", "record_count", "binding_count"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise SemanticInputError("Semantic input receipt has an unsupported shape.")
    if type(value["version"]) is not int or value["version"] != SEMANTIC_INPUT_VERSION:
        raise SemanticInputError("Semantic input receipt version is unsupported.")
    scope = value["scope"]
    if not isinstance(scope, Mapping) or set(scope) != {"company_id", "branch_id", "period_id"} or any(
        type(scope[key]) is not int or scope[key] < 1 for key in scope
    ):
        raise SemanticInputError("Semantic input receipt scope is malformed.")
    if type(value["pay_definition_id"]) is not int or value["pay_definition_id"] < 1:
        raise SemanticInputError("Semantic input receipt target is malformed.")
    target_meaning = _target_meaning(value["target_meaning"])
    if encoded(target_meaning) != encoded(value["target_meaning"]):
        raise SemanticInputError("Semantic input receipt target meaning is not canonical.")
    for key in ("rule_meaning_sha256", "input_sha256", "binding_sha256"):
        if not isinstance(value[key], str) or not _SHA256.fullmatch(value[key]):
            raise SemanticInputError("Semantic input receipt digest is malformed.")
    for key in ("record_count", "binding_count"):
        if type(value[key]) is not int or value[key] < 0:
            raise SemanticInputError("Semantic input receipt count is malformed.")
    return canonical(dict(value))


def _receipt_collection(values: Sequence[Mapping]) -> dict[int, dict]:
    receipts = [validate_target_receipt(item) for item in values]
    result = {item["pay_definition_id"]: item for item in receipts}
    if len(result) != len(receipts):
        raise SemanticInputError("One Import cannot contain duplicate target receipts.")
    if len({encoded(item["scope"]) for item in receipts}) > 1:
        raise SemanticInputError("One Import receipt collection cannot cross Company/Branch/period scope.")
    return result


def matching_target_ids(current: Sequence[Mapping], completed_imports: Sequence[Sequence[Mapping]]) -> set[int]:
    """Match whole target receipts across prior Imports; never stitch rows or dates."""
    previous = {}
    for completed in completed_imports:
        for target_id, item in _receipt_collection(completed).items():
            previous.setdefault(target_id, []).append(item)
    current_receipts = _receipt_collection(current)
    matches = set()
    for target_id, item in current_receipts.items():
        if any(all(item[key] == old[key] for key in (
            "scope", "target_meaning", "rule_meaning_sha256", "input_sha256", "binding_sha256",
            "record_count", "binding_count",
        )) for old in previous.get(target_id, [])):
            matches.add(target_id)
    return matches


def is_complete_repeat(current: Sequence[Mapping], completed_imports: Sequence[Sequence[Mapping]]) -> bool:
    """Every supplied target must match a whole target receipt from a completed Import."""
    current_receipts = _receipt_collection(current)
    return bool(current_receipts) and len(matching_target_ids(
        list(current_receipts.values()), completed_imports)) == len(current_receipts)


def matching_completed_receipts(current: Sequence[Mapping], completed_imports: Sequence[Mapping]) -> dict[int, Mapping]:
    """Return bounded deterministic prior completion evidence for exact target matches."""
    current_receipts = _receipt_collection(current)
    matches = matching_target_ids(list(current_receipts.values()),
                                  [item["targets"] for item in completed_imports])
    result = {}
    for target_id in sorted(matches):
        current_receipt = current_receipts[target_id]
        candidates = []
        for completed in completed_imports:
            for old in _receipt_collection(completed["targets"]).values():
                if old["pay_definition_id"] == target_id and all(
                    current_receipt[key] == old[key] for key in (
                        "scope", "target_meaning", "rule_meaning_sha256", "input_sha256",
                        "binding_sha256", "record_count", "binding_count")
                ):
                    candidates.append(completed)
                    break
        if candidates:
            def completion_order(item):
                completed_at = item.get("completed_at")
                stamp = completed_at.isoformat() if hasattr(completed_at, "isoformat") else str(completed_at or "")
                return stamp, item["import_id"]
            result[target_id] = sorted(candidates, key=completion_order)[-1]
    return result
