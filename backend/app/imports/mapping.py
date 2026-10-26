"""PayrollWork mapping and versioned semantic identities; no database access."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.imports.errors import ImportFailure
from app.payroll.definition_calculation import is_method_operational, quantity_error

IDENTIFIERS = {"DriverNumber", "EmployeeKey", "DriverName"}
CANONICALIZATION_VERSION = 3
NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$", re.ASCII)
DATE_PARTS = re.compile(r"^(\d{1,4})[/.-](\d{1,2})[/.-](\d{1,4})$", re.ASCII)


def canonical(value):
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, set):
        return sorted((canonical(item) for item in value), key=encoded)
    return value


def encoded(value) -> str:
    return json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(kind: str, value) -> str:
    return hashlib.sha256(encoded({"version": CANONICALIZATION_VERSION, "kind": kind, "value": value}).encode("utf-8")).hexdigest()


def definition_meaning(definition) -> dict:
    """Frozen business/input meaning; relational ownership IDs are not identity."""
    return {"code": definition.code, "name": definition.name, "input_type": definition.input_type,
            "unit": definition.unit, "method": definition.calculation_method,
            "method_version": definition.calculation_method_version,
            "rate_shape": definition.rate_shape, "active": definition.is_active}


def period_meaning(period) -> dict:
    return {key: period[key] for key in ("company_code", "branch_code", "period_code", "period_type", "start", "end")}


def driver_meaning(driver) -> dict:
    number = normalized_identifier("DriverNumber", driver["drivernumber"])
    employee = normalized_identifier("EmployeeKey", driver["employeekey"])
    return {"driver_number": number or None, "employee_key": employee or None,
            "name": normalized_identifier("DriverName", driver["drivername"]) if not number and not employee else None}


def assignment_meaning(assignment, driver, branch_code) -> dict:
    # Effective instants are business boundaries, unlike generated audit times.
    def instant(value):
        return value.astimezone(UTC).isoformat() if value is not None else None

    return {"driver": driver_meaning(driver), "branch_code": branch_code,
            "effective_from": instant(assignment["effectivefromat"]),
            "effective_to": instant(assignment["effectivetoat"]),
            "end_reason": assignment["endreason"], "time_zone": assignment["branchtimezoneidsnapshot"]}


def target_key(definition) -> str:
    return f"Definition:{definition.payroll_period_definition_id}"


def operational(definitions):
    return [d for d in definitions if d.is_active and is_method_operational(
        d.calculation_method, d.calculation_method_version)]


def targets(definitions) -> list[dict]:
    return ([{"target": key, "label": key} for key in ("DriverNumber", "EmployeeKey", "DriverName", "WorkDate", "Ignore")]
            + [{"target": target_key(d), "label": d.name, "code": d.code,
                "input_type": d.input_type, "unit": d.unit, "template_header": template_header(d)}
               for d in operational(definitions)])


def template_header(definition) -> str:
    return f"Pay Item: {definition.code}"


def suggest_mapping(headers: list[str], definitions) -> dict[str, str]:
    labels: dict[str, set[str]] = {}
    for key, names in {
        "DriverNumber": ["DriverNumber", "Driver number"], "EmployeeKey": ["EmployeeKey", "Employee key"],
        "DriverName": ["DriverName", "Driver name"], "WorkDate": ["WorkDate", "Work date", "Date"],
    }.items():
        for name in names:
            labels.setdefault(name.strip().casefold(), set()).add(key)
    for definition in operational(definitions):
        for name in (definition.code, definition.name, template_header(definition)):
            labels.setdefault(name.strip().casefold(), set()).add(target_key(definition))
    candidates = {str(index): next(iter(options)) for index, header in enumerate(headers, 1)
                  if len(options := labels.get(header.strip().casefold(), set())) == 1}
    # Repeated matching headers are ambiguous, never silently choose a column.
    return {column: target for column, target in candidates.items()
            if list(candidates.values()).count(target) == 1}


def validate_mapping(mapping: dict[str, str], nonempty_columns: list[int], definitions, *,
                     max_column: int = 128, require_work_date: bool = True) -> dict[str, str]:
    allowed = {item["target"] for item in targets(definitions)}
    if any(not re.fullmatch(r"[1-9][0-9]*", key, flags=re.ASCII) or int(key) > max_column
           or value not in allowed for key, value in mapping.items()):
        raise ImportFailure("IMPORT_MAPPING_INVALID", f"Map source columns 1–{max_column} to this period's operational targets.")
    if any(str(column) not in mapping for column in nonempty_columns):
        raise ImportFailure("IMPORT_MAPPING_INCOMPLETE", "Map or explicitly Ignore every non-empty source column.")
    assigned = [value for value in mapping.values() if value != "Ignore"]
    if len(assigned) != len(set(assigned)):
        raise ImportFailure("IMPORT_MAPPING_DUPLICATE_TARGET", "Two columns cannot use the same target.")
    if not IDENTIFIERS.intersection(assigned) or (require_work_date and "WorkDate" not in assigned) or not any(
        value.startswith("Definition:") for value in assigned
    ):
        requirement = "a Driver identifier, WorkDate and at least one Pay Item" if require_work_date else "a Driver identifier and at least one Pay Item"
        raise ImportFailure("IMPORT_MAPPING_REQUIRED_TARGET", f"Map {requirement}.")
    return dict(sorted(mapping.items(), key=lambda item: int(item[0])))


def blank(cell) -> bool:
    return not cell["missing_cache"] and (cell["value"] is None or
        isinstance(cell["value"], str) and not cell["value"].strip())


def normalized_identifier(kind: str, value) -> str:
    text = "" if value is None else str(value).strip()
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split()) if kind == "DriverName" else text


def quantity(cell, definition) -> tuple[Decimal | None, str | None]:
    if cell["missing_cache"]:
        return None, "Formula without a calculated value."
    if blank(cell):
        return None, None
    value = cell["value"]
    if cell["kind"] not in ("number", "text") or (cell["kind"] == "text" and not NUMBER.fullmatch(str(value).strip())):
        return None, "Numbers only; use digits with an optional decimal point."
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation:
        return None, "Numbers only."
    error = quantity_error(definition.input_type, result)
    if error:
        return None, error
    if result.as_tuple().exponent < -4:
        return None, "At most four decimal places are supported."
    return result, None


def parse_date(cell, order: str | None) -> date | None:
    if cell["missing_cache"] or blank(cell) or cell["kind"] not in ("date", "text"):
        return None
    text = str(cell["value"]).strip()
    try:
        if cell["kind"] == "date":
            parsed = datetime.fromisoformat(text) if "T" in text or " " in text else date.fromisoformat(text)
            if isinstance(parsed, datetime):
                return parsed.date() if parsed.tzinfo is None else None
            return parsed
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text, flags=re.ASCII):
            return date.fromisoformat(text)
        match = DATE_PARTS.fullmatch(text)
        if not match or not order:
            return None
        parts = dict(zip(order, map(int, match.groups()), strict=True))
        if parts["Y"] < 1000:
            return None
        return date(parts["Y"], parts["M"], parts["D"])
    except (ValueError, OverflowError):
        return None


def infer_date_order(cells, start: date, end: date, selected: str | None) -> tuple[str | None, str]:
    if selected:
        return selected, "selected"
    possible = {"DMY", "MDY"}
    evidence = False
    for cell in cells:
        text = str(cell["value"] or "").strip()
        if cell["kind"] != "text" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", text, flags=re.ASCII):
            continue
        match = DATE_PARTS.fullmatch(text)
        if not match or len(match[1]) == 4 or len(match[3]) != 4:
            continue
        orders = {order for order in ("DMY", "MDY") if parse_date(cell, order) is not None}
        fits = {order for order in orders if start <= parse_date(cell, order) <= end}
        if orders:
            possible &= fits or orders
            evidence = True
    if evidence and len(possible) == 1:
        return next(iter(possible)), "detected"
    return None, "unresolved"
