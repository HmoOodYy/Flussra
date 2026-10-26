"""Pure, batch-wide PayrollWork normalization and comparison."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.imports.errors import ImportFailure
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


def _numbered(mapping):
    return sorted(mapping.items(), key=lambda item: int(item[0]))


def _date_cell(value):
    if value is None:
        return {"value": None, "kind": "text", "formula": False, "missing_cache": False}
    return {"value": value.isoformat() if hasattr(value, "isoformat") else str(value),
            "kind": "date", "formula": False, "missing_cache": False}


def _date_source_semantics(source):
    kind = source.get("kind") if isinstance(source, dict) else None
    if kind == "column":
        return {"kind": "column"}
    if kind not in {"filename", "sheet", "fixed_unit", "import_fixed"}:
        raise ImportFailure("IMPORT_DATE_SOURCE_REQUIRED", "Accept one WorkDate provider for each selected source unit.")
    value = source.get("value")
    if not isinstance(value, str):
        raise ImportFailure("IMPORT_DATE_SOURCE_INVALID", "Accepted WorkDate provider must have a date value.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ImportFailure("IMPORT_DATE_SOURCE_INVALID", "Accepted WorkDate must be an ISO date.") from exc
    if parsed.isoformat() != value:
        raise ImportFailure("IMPORT_DATE_SOURCE_INVALID", "Accepted WorkDate must be YYYY-MM-DD.")
    return {"kind": kind, "value": value}


def build_batch_preview(sources: list[dict], context: dict, *, policy=None) -> dict:
    """Normalize selected source units together so duplicates span files/layouts."""
    definitions = context["definitions"]
    definitions_by_target = {target_key(item): item for item in definitions}
    period = context["period"]
    output_rows: list[dict] = []
    issues: list[dict] = []
    mapping_semantics = []
    data_rows = candidates = selected_cells = 0

    for source in sources:
        source_date = _date_source_semantics(source.get("date_source") or {})
        date_column = next((column for column, target in source["mapping"].items() if target == "WorkDate"), None)
        if (source_date.get("kind") == "column") != (date_column is not None):
            raise ImportFailure("IMPORT_DATE_SOURCE_CONFLICT", "Use either a mapped WorkDate column or one accepted context provider.")
        mapping = validate_mapping(source["mapping"], source["materialized"]["nonempty_columns"], definitions,
                                   max_column=(policy.max_columns_per_unit if policy else 128),
                                   require_work_date=source_date.get("kind") == "column")
        materialized = source["materialized"]
        inspection = materialized["inspection"]
        mapped = {column: target for column, target in mapping.items() if target != "Ignore"}
        if not IDENTIFIERS.intersection(mapped.values()):
            raise ImportFailure("IMPORT_MAPPING_REQUIRED_TARGET", "Map a Driver identifier, WorkDate and at least one Pay Item.")
        definition_columns = {column: definitions_by_target[target] for column, target in mapped.items()
                              if target.startswith("Definition:") and target in definitions_by_target}
        date_column = next((column for column, target in mapped.items() if target == "WorkDate"), None)
        raw_rows = materialized["rows"]
        active_rows = [item for item in raw_rows if any(not blank(item["cells"].get(column, _date_cell(None)))
                                                        for column in definition_columns)]
        date_cells = [item["cells"].get(date_column) for item in active_rows] if date_column else []
        date_order, order_state = infer_date_order(date_cells, period["start"], period["end"],
                                                   source.get("date_order"))
        if source_date.get("kind") == "column" and date_order is None and date_cells:
            # ISO/native dates parse without an order; retain unresolved only
            # when at least one nonblank slash date cannot be interpreted.
            unresolved = any(not blank(cell) and parse_date(cell, None) is None and cell.get("kind") == "text"
                             and any(separator in str(cell.get("value", "")) for separator in "/." )
                             for cell in date_cells)
            if not unresolved:
                order_state = "not_required"
        source["accepted_date_order"] = date_order
        source["date_order_state"] = order_state
        source["date_column"] = date_column
        source_contract = {
            "structure": source["structure_fingerprint"],
            "unit_kind": source.get("unit_kind"),
            "mapping": [{"column": int(column), "target": (definition_meaning(definitions_by_target[target])
                        if target in definitions_by_target else target)} for column, target in _numbered(mapping)],
            "date_source": source_date,
            "date_order": date_order,
            "header_row_number": inspection["header_row_number"],
            "headers": inspection.get("header_cells", []),
            "encoding": inspection.get("encoding"), "delimiter": inspection.get("delimiter"),
        }
        source["row_contract_fingerprint"] = fingerprint("row-source-contract", source_contract)
        mapping_semantics.append({**source_contract, "file_ordinal": source["file_ordinal"],
                                  "unit_name": source["unit_name"]})
        for source_row in raw_rows:
            cells = source_row["cells"]
            skipped = not any(not blank(cells.get(column, _date_cell(None))) for column in definition_columns)
            data_rows += 0 if skipped else 1
            candidates += len(definition_columns)
            selected_cells += len(mapped)
            row_issues: list[dict] = []

            def issue(column, severity, code, message):
                value = cells.get(column) if column else None
                item = {"file_ordinal": source["file_ordinal"], "unit_name": source["unit_name"],
                        "source_row": source_row["source_row"], "column": int(column) if column else None,
                        "severity": severity, "code": code, "message": message,
                        "value": (None if value is None else canonical(value.get("value")))}
                row_issues.append(item)
                issues.append(item)

            identifiers = []
            row_error = False
            agreed_drivers: set[int] = set()
            for column, kind in mapped.items():
                if kind not in IDENTIFIERS:
                    continue
                cell = cells.get(column, _date_cell(None))
                if blank(cell):
                    continue
                value = normalized_identifier(kind, cell["value"])
                candidates_for_value = context["identity_indexes"].get(kind, {}).get(value, set())
                driver_id = next(iter(candidates_for_value)) if len(candidates_for_value) == 1 else None
                resolution = ("Proposed" if driver_id and kind == "DriverName" else "Exact" if driver_id else "Unresolved")
                identifiers.append({"column": int(column), "kind": kind, "value": value,
                                    "driver_id": driver_id, "resolution": resolution})
                if driver_id is not None:
                    agreed_drivers.add(driver_id)
                if not skipped and cell.get("kind") == "number":
                    issue(column, "Warning", "IMPORT_NUMERIC_IDENTIFIER",
                          "Numeric identifier may have lost leading zeroes; no padding is inferred.")
                if driver_id is None and not skipped:
                    row_error = True
                    issue(column, "Error", "IMPORT_DRIVER_UNRESOLVED",
                          "Formula without a calculated value." if cell.get("missing_cache") else
                          "Driver identifier is missing, unknown or ambiguous within its Company identity or scoped name roster.")
            if not identifiers and not skipped:
                row_error = True
                column = next((c for c, target in mapped.items() if target in IDENTIFIERS), None)
                issue(column, "Error", "IMPORT_DRIVER_UNRESOLVED", "Supply at least one Driver identifier for this row.")
            if len(agreed_drivers) > 1 and not skipped:
                row_error = True
                issue(None, "Error", "IMPORT_DRIVER_DISAGREEMENT", "Mapped Driver identifiers refer to different Drivers.")
            driver_id = next(iter(agreed_drivers)) if len(agreed_drivers) == 1 else None
            proposed = not skipped and bool(identifiers) and all(item["resolution"] == "Proposed" for item in identifiers)
            if proposed:
                issue(None, "Conflict", "IMPORT_DRIVER_CONFIRMATION_REQUIRED",
                      "Exact name proposes a Driver; confirmation belongs to the review decision step.")

            date_value = None
            date_cell = cells.get(date_column, _date_cell(None)) if date_column else None
            if source_date.get("kind") == "column":
                date_value = parse_date(date_cell, date_order)
                if blank(date_cell):
                    source_date_value = {"blank": True}
                elif date_value is not None:
                    source_date_value = {"date": date_value.isoformat()}
                else:
                    source_date_value = {"invalid": True, "raw_value": canonical(date_cell.get("value")),
                                         "kind": date_cell.get("kind"),
                                         "formula": bool(date_cell.get("formula")),
                                         "missing_cache": bool(date_cell.get("missing_cache"))}
            elif source_date.get("kind") in {"filename", "sheet", "fixed_unit", "import_fixed"}:
                date_value = date.fromisoformat(source_date["value"])
                source_date_value = {"date": date_value.isoformat()}
            else:
                source_date_value = {"invalid": True}
            if not skipped and (date_value is None or date_value not in context["days"]):
                row_error = True
                issue(date_column, "Error", "IMPORT_WORK_DATE_INVALID",
                      "Use a valid date in this period's day snapshot. Select the date order if ambiguous; numeric or timezone-offset dates are unsupported.")
                date_value = None

            assignment_id = None
            assignment = None
            if not skipped and driver_id is not None and date_value is not None:
                eligible = [item for item in context["assignments_by_driver"].get(driver_id, [])
                            if ((item["driverassignmentid"], date_value) in context["live_days"]
                                and _is_snapshot_row_eligible_for_workdate(SimpleNamespace(**item), date_value))
                            or (item["driverassignmentid"], date_value) in context["existing_source_dates"]]
                if len(eligible) == 1:
                    assignment = eligible[0]
                    assignment_id = assignment["driverassignmentid"]
                else:
                    row_error = True
                    issue(None, "Error", "IMPORT_ASSIGNMENT_UNRESOLVED",
                          "No single eligible Driver assignment exists for this WorkDate.")

            values = {}
            semantic_values = {}
            for column, definition in definition_columns.items():
                cell = cells.get(column, _date_cell(None))
                is_blank = blank(cell)
                if skipped:
                    value, error = None, None
                else:
                    value, error = quantity(cell, SimpleNamespace(input_type=definition.input_type))
                source_quantity_error = error
                key = (assignment_id, date_value, definition.payroll_period_definition_id)
                current_present = key in context["current_lines"] if assignment_id and date_value else False
                current = context["current_lines"].get(key)
                if is_blank:
                    outcome = "Blank"
                elif error:
                    outcome = "Invalid"
                elif row_error:
                    outcome = "Invalid"
                elif proposed:
                    outcome = "Unresolved"
                elif (current if current_present else Decimal(0)) == value:
                    outcome = "Unchanged"
                elif not current_present:
                    outcome = "New"
                else:
                    outcome = "Conflict"
                fresh = bool(assignment and date_value and
                             (assignment_id, date_value) in context["live_days"] and
                             _is_snapshot_row_eligible_for_workdate(SimpleNamespace(**assignment), date_value))
                if value is not None and assignment_id and date_value and value > 0 and not fresh and not current_present:
                    outcome = "Invalid"
                    error = "Existing-source rescue permits changing existing input on this date, not adding new source."
                if error:
                    issue(column, "Error", "IMPORT_QUANTITY_INVALID" if not (value is not None and not fresh and not current_present)
                          else "IMPORT_NEW_SOURCE_NOT_ELIGIBLE", error)
                if outcome == "Conflict":
                    issue(column, "Conflict", "IMPORT_VALUE_CONFLICT",
                          "Incoming quantity differs from the current quantity; no replacement is selected.")
                entry_state = context["entries"].get((assignment_id, date_value)) if assignment_id and date_value else None
                day_off = bool(date_value and context["days"].get(date_value, False))
                if value is not None and value > 0 and outcome in ("New", "Conflict", "Unresolved") and entry_state and entry_state["isoffreason"]:
                    issue(column, "Conflict", "IMPORT_STATUS_DECISION_REQUIRED",
                          "Positive work overlaps a held full-day-off Status; no Status removal is selected.")
                if value is not None and value > 0 and outcome in ("New", "Conflict", "Unresolved") and day_off:
                    issue(column, "Conflict", "IMPORT_DAY_OFF_DECISION_REQUIRED",
                          "Positive work is on a configured day off; no exception is accepted.")
                defid = str(definition.payroll_period_definition_id)
                values[defid] = {"source_column": int(column), "raw_value": canonical(cell.get("value")),
                                 "quantity": canonical(value), "is_blank": is_blank, "outcome": outcome,
                                 "current_quantity": canonical(current), "current_source_exists": current_present}
                semantic_values[definition.code] = (
                    {"blank": is_blank, "quantity": canonical(value)} if not source_quantity_error else
                    {"invalid": True, "raw_value": canonical(cell.get("value")), "kind": cell.get("kind"),
                     "formula": bool(cell.get("formula")), "missing_cache": bool(cell.get("missing_cache"))}
                )

            source_semantics = {"identifiers": sorted((item["kind"], item["value"])
                                                        for item in identifiers),
                                "date": source_date_value,
                                "date_source": source_date, "values": semantic_values,
                                "unit_kind": source.get("unit_kind"),
                                "structure": source["structure_fingerprint"],
                                "source_contract": source["row_contract_fingerprint"], "skipped": skipped}
            row_fp = fingerprint("row", source_semantics)
            output_rows.append({
                "file_ordinal": source["file_ordinal"], "unit_name": source["unit_name"],
                "source_row": source_row["source_row"], "source_unit_id": source.get("source_unit_id"),
                "source_record_id": None, "driver_id": driver_id, "driver_assignment_id": assignment_id,
                "work_date": date_value, "row_fingerprint": row_fp, "skipped": skipped,
                "raw_payload": {key: {"value": canonical(cell.get("value")), "kind": cell.get("kind"),
                                      "formula": bool(cell.get("formula")),
                                      "missing_cache": bool(cell.get("missing_cache"))}
                                for key, cell in cells.items()},
                "values": values, "issues": row_issues, "identifiers": identifiers,
                "context": {"status_code": (context["entries"].get((assignment_id, date_value)) or {}).get("statuscode"),
                            "day_off": bool(date_value and context["days"].get(date_value, False)),
                            "source_date": source_date},
                "group_fingerprint": fingerprint("group", {
                    "driver": driver_meaning(context["drivers_by_id"][driver_id]) if driver_id in context["drivers_by_id"] else None,
                    "date": date_value.isoformat() if date_value else None,
                    "unresolved_source": [source["file_ordinal"], source["unit_name"], source_row["source_row"]] if driver_id is None or date_value is None else None,
                }),
                "driver_display": {"driver_name": context["drivers_by_id"].get(driver_id, {}).get("drivername")},
                "semantic": source_semantics,
            })

    if policy:
        if data_rows > policy.max_data_rows:
            raise ImportFailure("IMPORT_DATA_ROW_LIMIT_EXCEEDED", "Selected batch exceeds its configured data-row limit.")
        if candidates > policy.max_candidates:
            raise ImportFailure("IMPORT_CANDIDATE_LIMIT_EXCEEDED", "Selected batch exceeds its configured mapped-candidate limit.")
        if selected_cells > policy.max_selected_cells:
            raise ImportFailure("IMPORT_SELECTED_CELL_LIMIT_EXCEEDED", "Selected batch exceeds its configured cell limit.")

    # A mapped nonblank candidate may occur only once across the entire batch.
    duplicate_index = defaultdict(list)
    for row in output_rows:
        if row["skipped"] or row["driver_assignment_id"] is None or row["work_date"] is None:
            continue
        for definition_id, value in row["values"].items():
            if not value["is_blank"]:
                duplicate_index[(row["driver_assignment_id"], row["work_date"], definition_id)].append((row, value))
    for entries in duplicate_index.values():
        if len(entries) < 2:
            continue
        for row, value in entries:
            value["outcome"] = "Invalid"
            issue = {"file_ordinal": row["file_ordinal"], "unit_name": row["unit_name"],
                     "source_row": row["source_row"], "column": value["source_column"],
                     "severity": "Error", "code": "IMPORT_DUPLICATE_FACT",
                     "message": "The same Driver, WorkDate and Pay Item occurs more than once in this Import.",
                     "value": value["raw_value"]}
            row["issues"].append(issue)
            issues.append(issue)

    mapping_fp = fingerprint("mapping", {"period": period_meaning(period),
                                          "layouts": sorted(mapping_semantics, key=encoded)})
    content_fp = fingerprint("content", {"mapping": mapping_fp,
                                          "rows": sorted(row["row_fingerprint"] for row in output_rows)})
    assignment_meanings = {item["driverassignmentid"]: assignment_meaning(
        item, context["drivers_by_id"][item["driverid"]], period["branch_code"])
        for item in context["roster"] if item["driverid"] in context["drivers_by_id"]}
    row_plans = []
    for row in output_rows:
        row_plans.append({"row": row["row_fingerprint"],
                          "source": {"file_ordinal": row["file_ordinal"], "unit_name": row["unit_name"],
                                     "source_row": row["source_row"]},
                          "identifiers": [{"kind": item["kind"], "value": item["value"],
                                           "resolution": item["resolution"],
                                           "driver": driver_meaning(context["drivers_by_id"][item["driver_id"]])
                                           if item["driver_id"] in context["drivers_by_id"] else None}
                                          for item in row["identifiers"]],
                          "assignment": assignment_meanings.get(row["driver_assignment_id"]),
                          "date": row["work_date"].isoformat() if row["work_date"] else None,
                          "values": {context["definition_by_id"][int(key)].code: {
                              "quantity": value["quantity"], "blank": value["is_blank"],
                              "outcome": value["outcome"], "current": value["current_quantity"]}
                              for key, value in row["values"].items()},
                          "status": row["context"].get("status_code"),
                          "day_off": row["context"].get("day_off")})
    row_fingerprint_by_coordinate = {(row["file_ordinal"], row["unit_name"], row["source_row"]): row["row_fingerprint"]
                                     for row in output_rows}
    plan_fp = fingerprint("plan", {"mapping": mapping_fp, "content": content_fp,
                                    "rows": sorted(row_plans, key=encoded),
                                    "issues": sorted(({"row": row_fingerprint_by_coordinate.get(
                                                           (i["file_ordinal"], i["unit_name"], i["source_row"])),
                                                       "column": i["column"], "severity": i["severity"], "code": i["code"]}
                                                      for i in issues), key=encoded),
                                    "decisions": None, "confirmations": None})
    counts = Counter(rows=len(output_rows), skipped=sum(row["skipped"] for row in output_rows),
                     errors=0, warnings=0, conflicts=0, new=0, unchanged=0, blank=0, invalid=0,
                     unresolved=0, conflict_cells=0, error_rows=0, warning_rows=0, conflict_rows=0)
    for item in issues:
        counts[{"Error": "errors", "Warning": "warnings", "Conflict": "conflicts"}[item["severity"]]] += 1
    for row in output_rows:
        severities = {item["severity"] for item in row["issues"]}
        counts["error_rows"] += "Error" in severities
        counts["warning_rows"] += "Warning" in severities
        counts["conflict_rows"] += "Conflict" in severities
        for value in row["values"].values():
            outcome = value["outcome"].lower()
            counts["conflict_cells" if value["outcome"] == "Conflict" else outcome] += 1
    groups = {}
    for row in output_rows:
        group = groups.setdefault(row["group_fingerprint"], {
            "group_fingerprint": row["group_fingerprint"], "driver_id": row["driver_id"],
            "work_date": row["work_date"], "driver_display": row["driver_display"],
            "error_count": 0, "warning_count": 0, "conflict_count": 0,
        })
        severities = Counter(item["severity"] for item in row["issues"])
        group["error_count"] += severities["Error"]
        group["warning_count"] += severities["Warning"]
        group["conflict_count"] += severities["Conflict"]
    return {"rows": output_rows, "issues": issues, "groups": list(groups.values()),
            "mapping_fingerprint": mapping_fp, "content_fingerprint": content_fp,
            "plan_fingerprint": plan_fp, "counts": dict(counts),
            "interpretations": [{"file_ordinal": s["file_ordinal"], "unit_name": s["unit_name"],
                                 "date_order": s.get("accepted_date_order"),
                                 "date_order_state": s.get("date_order_state"),
                                 "date_source": s.get("date_source")}
                                for s in sources],
            "candidates": candidates}
