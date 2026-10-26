"""Deterministic PayrollWork resolver, validator and comparison; staging only."""
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from types import SimpleNamespace

from app.imports.mapping import (
    IDENTIFIERS,
    assignment_meaning,
    blank,
    canonical,
    definition_meaning,
    driver_meaning,
    encoded,
    fingerprint,
    infer_date_order,
    normalized_identifier,
    parse_date,
    period_meaning,
    quantity,
    target_key,
    validate_mapping,
)
from app.payroll.eligibility import _is_snapshot_row_eligible_for_workdate


@dataclass(slots=True)
class RowTarget:
    work_date: object
    assignment_id: int | None
    held_status_key_id: int | None
    day_off: bool


@dataclass(slots=True, eq=False)
class PreviewCell(Mapping):
    """One result, sharing row/definition/source context instead of copying it."""

    column: int
    definition: object
    source: Mapping
    target: RowTarget
    quantity: Decimal | None
    outcome: str
    current_quantity: Decimal | None

    def __iter__(self):
        return iter(("column", "work_date", "assignment_id", "definition_id", "pay_definition_id", "rate_definition_id",
                     "raw_value", "quantity", "blank", "outcome", "current", "held_status_key_id", "day_off"))

    def __len__(self):
        return 13

    def __getitem__(self, key):
        if key in ("column", "quantity", "outcome"):
            return getattr(self, key)
        if key in ("work_date", "assignment_id", "held_status_key_id", "day_off"):
            return getattr(self.target, key)
        if key == "definition_id":
            return self.definition.payroll_period_definition_id
        if key == "pay_definition_id":
            return self.definition.pay_definition_id
        if key == "rate_definition_id":
            return self.definition.rate_definition_id
        if key == "raw_value":
            return None if self.source["value"] is None else str(self.source["value"])
        if key == "blank":
            return blank(self.source)
        if key == "current":
            return {"quantity": canonical(self.current_quantity)}
        raise KeyError(key)

    def __setitem__(self, key, value):
        if key != "outcome":
            raise TypeError("Only the derived outcome can be revised during duplicate validation.")
        self.outcome = value


def build_preview(materialized, mapping, context, selected_order=None):
    definitions = context["definitions"]
    mapping = validate_mapping(mapping, materialized["nonempty_columns"], definitions)
    mapped = {column: target for column, target in mapping.items() if target != "Ignore"}
    period = context["period"]
    date_column = next(column for column, target in mapped.items() if target == "WorkDate")
    definition_columns = {column: definition for column, target in mapped.items()
                          for definition in definitions if target_key(definition) == target}
    active_rows = [row for row in materialized["rows"] if any(
        not blank(row["cells"][column]) for column in definition_columns)]
    order, order_state = infer_date_order(
        (row["cells"][date_column] for row in active_rows), period["start"], period["end"], selected_order)
    interpretation = {"sheet_name": materialized["inspection"]["selected_sheet"],
                      "header_row_number": materialized["inspection"]["header_row_number"],
                      "date_order": order, "date_order_state": order_state,
                      "encoding": materialized["inspection"]["encoding"],
                      "delimiter": materialized["inspection"]["delimiter"]}
    definition_by_target = {target_key(d): d for d in definitions}
    mapping_identity = {"period": period_meaning(period), "interpretation": {key: value for key, value in interpretation.items()
                        if key != "date_order_state"},
                        "columns": {column: {"definition": definition_meaning(definition_by_target[target])}
                                    if target in definition_by_target else target for column, target in mapping.items()},
                        "definitions": sorted((definition_meaning(d) for d in definitions), key=encoded)}
    mapping_hash = fingerprint("mapping", mapping_identity)
    indexes = {kind: defaultdict(set) for kind in IDENTIFIERS}
    assignments = defaultdict(list)
    drivers = {entry["driverid"]: entry for entry in context["drivers"]}
    for entry in context["drivers"]:
        for kind, field in (("DriverNumber", "drivernumber"), ("EmployeeKey", "employeekey")):
            value = normalized_identifier(kind, entry[field])
            if value:
                indexes[kind][value].add(entry["driverid"])
    for entry in context["roster"]:
        assignments[entry["driverid"]].append(entry)
        value = normalized_identifier("DriverName", entry["drivername"])
        if value:
            indexes["DriverName"][value].add(entry["driverid"])
    days = {row["workdate"]: row["isconfiguredoffday"] for row in context["days"]}
    current = {(row["driverassignmentid"], row["workdate"], row["payrollperioddefinitionid"]): row["quantity"]
               for row in context["lines"] if row["payrollperioddefinitionid"] is not None}
    source_dates = {(row["driverassignmentid"], row["workdate"]) for row in context["lines"] + context["entries"]}
    off_status = {(row["driverassignmentid"], row["workdate"]): row["statuskeyid"]
                  for row in context["entries"] if row["isoffreason"]}
    status_meanings = {row["statuskeyid"]: {"code": row["statuscode"], "is_off_reason": True}
                       for row in context["entries"] if row["isoffreason"]}
    issues, rows, matches = [], [], {}
    duplicates = defaultdict(list)

    @lru_cache(maxsize=4096)
    def common_quantity(value, kind, missing_cache, input_type):
        return quantity({"value": value, "kind": kind, "missing_cache": missing_cache},
                        SimpleNamespace(input_type=input_type))

    def issue(row, column, severity, code, message):
        cell = row["cells"].get(column) if column else None
        issues.append({"source_row": row["source_row"], "column": int(column) if column else None,
                       "severity": severity, "code": code, "message": message,
                       "value": cell["value"] if cell else None})

    for source in materialized["rows"]:
        source = {**source, "cells": source["cells"] if source["cells"].keys() == mapped.keys() else
                  {column: source["cells"][column] for column in mapped}}
        skipped = all(blank(source["cells"][column]) for column in definition_columns)
        row = {**source, "skipped": skipped, "identifiers": [], "work_cells": []}
        semantic = {"identifiers": {kind: normalized_identifier(kind, row["cells"][column]["value"])
                                    for column, kind in mapped.items() if kind in IDENTIFIERS},
                    "quantities": {}, "date": None}
        row_error = False
        proposed = False
        driver_id = assignment_id = work_date = None
        if not skipped:
            for column, kind in mapped.items():
                if kind not in IDENTIFIERS:
                    continue
                cell = row["cells"][column]
                if blank(cell):
                    continue
                value = normalized_identifier(kind, cell["value"])
                semantic["identifiers"][kind] = value
                candidates = indexes[kind].get(value, set()) if not cell["missing_cache"] and cell["kind"] != "error" else set()
                found = next(iter(candidates)) if len(candidates) == 1 else None
                resolution = "Proposed" if found and kind == "DriverName" else "Exact" if found else "Unresolved"
                match = {"kind": kind, "value": value, "driver_id": found, "resolution": resolution}
                matches[(kind, value)] = match
                row["identifiers"].append({"column": int(column), **match})
                if cell["kind"] == "number":
                    issue(row, column, "Warning", "IMPORT_NUMERIC_IDENTIFIER", "Numeric identifier may have lost leading zeroes; no padding is inferred.")
                if found is None:
                    row_error = True
                    issue(row, column, "Error", "IMPORT_DRIVER_UNRESOLVED",
                          "Formula without a calculated value." if cell["missing_cache"] else
                          "Driver identifier is missing, unknown or ambiguous within its Company identity or scoped name roster.")
            if not row["identifiers"]:
                row_error = True
                issue(row, next(column for column, kind in mapped.items() if kind in IDENTIFIERS),
                      "Error", "IMPORT_DRIVER_UNRESOLVED", "Supply at least one Driver identifier for this row.")
            known = {match["driver_id"] for match in row["identifiers"] if match["driver_id"] is not None}
            if len(known) > 1:
                row_error = True
                issue(row, None, "Error", "IMPORT_DRIVER_DISAGREEMENT", "Mapped Driver identifiers refer to different Drivers.")
            elif len(known) == 1 and (not row_error or (
                any(match["kind"] != "DriverName" for match in row["identifiers"])
                and all(match["resolution"] == "Exact" for match in row["identifiers"] if match["kind"] != "DriverName")
            )):
                # A missing/ambiguous name proposal does not erase agreed stable
                # identity. Keep its validation error, but assess payroll target
                # eligibility separately for the already known Driver.
                driver_id = next(iter(known))
                proposed = all(match["resolution"] == "Proposed" for match in row["identifiers"])
                if proposed:
                    issue(row, None, "Conflict", "IMPORT_DRIVER_CONFIRMATION_REQUIRED", "Exact name proposes a Driver; confirmation belongs to the review decision step.")
            work_date = parse_date(row["cells"][date_column], order)
            if work_date not in days or work_date is None or not period["start"] <= work_date <= period["end"]:
                row_error = True
                issue(row, date_column, "Error", "IMPORT_WORK_DATE_INVALID",
                      "Formula without a calculated value." if row["cells"][date_column]["missing_cache"] else
                      "Use a valid date in this period's day snapshot. Select the date order if ambiguous; numeric or timezone-offset dates are unsupported.")
                work_date = None
            if driver_id is not None and work_date is not None:
                eligible = [entry for entry in assignments[driver_id] if (
                    _is_snapshot_row_eligible_for_workdate(SimpleNamespace(**entry), work_date)
                    and (entry["driverassignmentid"], work_date) in context["live_days"]
                ) or (entry["driverassignmentid"], work_date) in source_dates]
                if len(eligible) == 1:
                    assignment_id = eligible[0]["driverassignmentid"]
                else:
                    row_error = True
                    issue(row, None, "Error", "IMPORT_ASSIGNMENT_UNRESOLVED", "No single eligible Driver assignment exists for this WorkDate.")
        source_date = parse_date(row["cells"][date_column], order)
        semantic["date"] = source_date.isoformat() if source_date else row["cells"][date_column]
        row_target = RowTarget(work_date, assignment_id, off_status.get((assignment_id, work_date)),
                               bool(work_date and days.get(work_date)))
        entry = next((item for item in assignments[driver_id] if item["driverassignmentid"] == assignment_id), None)
        fresh = bool(entry and work_date and _is_snapshot_row_eligible_for_workdate(SimpleNamespace(**entry), work_date)
                     and (assignment_id, work_date) in context["live_days"])
        for column, definition in definition_columns.items():
            raw = row["cells"][column]
            is_blank = blank(raw)
            value, error = (None, None) if skipped else (
                common_quantity(raw["value"], raw["kind"], raw["missing_cache"], definition.input_type)
                if isinstance(raw["value"], (str, int, float, type(None))) and len(str(raw["value"])) <= 128
                else quantity(raw, definition))
            key = (assignment_id, work_date, definition.payroll_period_definition_id)
            existing = current.get(key)
            effective = existing if existing else None
            outcome = "Blank" if is_blank else "Invalid" if row_error or error else "Unresolved" if proposed else (
                "Unchanged" if (effective or Decimal(0)) == value else "New" if effective is None else "Conflict")
            if error:
                issue(row, column, "Error", "IMPORT_QUANTITY_INVALID", error)
            if value is not None and assignment_id is not None and work_date is not None:
                if not fresh and value > 0 and key not in current:
                    outcome = "Invalid"
                    issue(row, column, "Error", "IMPORT_NEW_SOURCE_NOT_ELIGIBLE", "Existing-source rescue permits changing existing input on this date, not adding new source.")
            held = off_status.get((assignment_id, work_date))
            day_off = bool(work_date and days.get(work_date))
            relevant = value is not None and value > 0 and outcome in ("New", "Conflict", "Unresolved")
            if outcome == "Conflict":
                issue(row, column, "Conflict", "IMPORT_VALUE_CONFLICT", "Incoming quantity differs from the current non-zero quantity; no replacement is selected.")
            if relevant and held:
                issue(row, column, "Conflict", "IMPORT_STATUS_DECISION_REQUIRED", "Positive work overlaps a held full-day-off Status; no Status removal is selected.")
            if relevant and day_off:
                issue(row, column, "Conflict", "IMPORT_DAY_OFF_DECISION_REQUIRED", "Positive work is on a configured day off; no exception is accepted.")
            semantic["quantities"][definition.code] = {"blank": is_blank, "quantity": canonical(value)} if not error else raw
            row["work_cells"].append(PreviewCell(int(column), definition, raw, row_target, value, outcome, effective))
        row["fingerprint"] = fingerprint("row", {"mapping": mapping_hash, "source": semantic})
        rows.append(row)
        if assignment_id is not None and work_date is not None:
            duplicates[(driver_id, work_date)].append(row)
    for occurrences in duplicates.values():
        if len(occurrences) > 1:
            by_column = defaultdict(list)
            for row in occurrences:
                for cell in row["work_cells"]:
                    if cell["quantity"] is not None:
                        by_column[cell["column"]].append((row, cell))
            for cells in by_column.values():
                if len(cells) > 1:
                    for row, cell in cells:
                        issue(row, str(cell["column"]), "Error", "IMPORT_DUPLICATE_FACT", "The same Driver, WorkDate and Pay Item occurs more than once in this file.")
                        cell["outcome"] = "Invalid"
    counts = Counter({"rows": len(rows), "skipped": sum(row["skipped"] for row in rows),
                      "errors": 0, "warnings": 0, "conflicts": 0, "new": 0, "unchanged": 0,
                      "invalid": 0, "unresolved": 0, "conflict_cells": 0})
    for item in issues:
        counts[{"Error": "errors", "Warning": "warnings", "Conflict": "conflicts"}[item["severity"]]] += 1
    for row in rows:
        for cell in row["work_cells"]:
            if cell["outcome"] != "Blank":
                counts["conflict_cells" if cell["outcome"] == "Conflict" else cell["outcome"].lower()] += 1
    for severity, name in (("Error", "error_rows"), ("Warning", "warning_rows"), ("Conflict", "conflict_rows")):
        counts[name] = len({item["source_row"] for item in issues if item["severity"] == severity})
    content_hash = fingerprint("content", {"mapping": mapping_hash, "rows": sorted(row["fingerprint"] for row in rows)})
    row_hashes = {row["source_row"]: row["fingerprint"] for row in rows}
    assignment_semantics = {entry["driverassignmentid"]: fingerprint("assignment", assignment_meaning(
        entry, drivers[entry["driverid"]], period["branch_code"])) for entry in context["roster"]}
    definition_semantics = {column: definition.code for column, definition in definition_columns.items()}
    driver_semantics = {key: driver_meaning(driver) for key, driver in drivers.items()}

    def plan_cell(cell):
        return {"column": cell["column"], "work_date": cell["work_date"],
                "assignment": assignment_semantics.get(cell["assignment_id"]),
                "definition": definition_semantics[str(cell["column"])], "quantity": cell["quantity"], "blank": cell["blank"],
                "outcome": cell["outcome"], "current": cell["current"],
                "held_status": status_meanings.get(cell["held_status_key_id"]), "day_off": cell["day_off"]}

    def plan_identifiers(identifiers):
        return [{"column": item["column"], "kind": item["kind"], "value": item["value"],
                 "resolution": item["resolution"],
                 "driver": driver_semantics.get(item["driver_id"])}
                for item in identifiers]

    plan_hash = fingerprint("plan", {"mapping": mapping_hash, "content": content_hash,
        # Hash one bounded source row at a time rather than materializing a
        # second million-cell plan just to serialize its canonical identity.
        "rows": sorted(fingerprint("plan-row", {"fingerprint": row["fingerprint"],
                         "skipped": row["skipped"],
                         "cells": [plan_cell(cell) for cell in row["work_cells"]],
                         "identifiers": plan_identifiers(row["identifiers"])})
                       for row in rows),
        "issues": sorted(fingerprint("plan-issue", {"row": row_hashes[item["source_row"]], "column": item["column"],
                           "severity": item["severity"], "code": item["code"]}) for item in issues),
        "decisions": None, "confirmations": None})
    mapping_contract = {"version": 1, "columns": [{"source_column": int(column), "target": target,
        **({"payroll_period_definition_id": definition_columns[column].payroll_period_definition_id,
            "pay_definition_id": definition_columns[column].pay_definition_id,
            "rate_definition_id": definition_columns[column].rate_definition_id} if column in definition_columns else {})}
        for column, target in mapping.items()]}
    return {"rows": rows, "issues": issues, "matches": list(matches.values()), "mapping": mapping,
            "mapping_contract": mapping_contract,
            "mapping_fingerprint": mapping_hash, "content_fingerprint": content_hash,
            "plan_fingerprint": plan_hash, "counts": dict(counts), "interpretation": interpretation}
