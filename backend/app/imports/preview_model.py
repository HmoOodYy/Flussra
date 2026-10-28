"""Pure, batch-wide PayrollWork normalization and comparison."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from types import MappingProxyType, SimpleNamespace

from app.imports.aggregation import (
    CategoryEvidenceError,
    aggregate_quantity_contributions,
    category_key_from_json,
    typed_category_key,
)
from app.imports.date_interpretation import interpret_date, make_date_profile
from app.imports.errors import ImportFailure
from app.imports.headers import normalize_header
from app.imports.identity import resolve_row_identity
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
    period_meaning,
    quantity,
    target_key,
)
from app.imports.mapping import (
    targets as mapping_targets,
)
from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY
from app.imports.quantity_math import exact_decimal_sum as _exact_decimal_sum
from app.imports.quantity_math import parse_sum_operand
from app.imports.semantic_identity import (
    SemanticInputError,
    TargetReceiptAccumulator,
    canonical_rule_meaning,
    matching_completed_receipts,
    prepare_rule_meaning,
)
from app.payroll.definition_calculation import quantity_error
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
        if set(source) != {"kind"}:
            raise ImportFailure("IMPORT_DATE_SOURCE_INVALID", "Date Column accepts only its selected authority.")
        return {"kind": "column"}
    if not isinstance(kind, str) or kind not in {"filename", "sheet"} or \
            set(source) not in ({"kind"}, {"kind", "value"}):
        raise ImportFailure("IMPORT_DATE_SOURCE_REQUIRED", "Accept one WorkDate provider for each selected source unit.")
    value = source.get("value")
    if value is None:
        return {"kind": kind}
    if not isinstance(value, str):
        raise ImportFailure("IMPORT_DATE_SOURCE_INVALID", "Accepted date candidate must be an ISO date.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ImportFailure("IMPORT_DATE_SOURCE_INVALID", "Accepted WorkDate must be an ISO date.") from exc
    if parsed.isoformat() != value:
        raise ImportFailure("IMPORT_DATE_SOURCE_INVALID", "Accepted WorkDate must be YYYY-MM-DD.")
    return {"kind": kind, "value": value}


def _effective_row_policy(rule):
    """Normalize inactive filter metadata away from RejectRepeated/SumRows."""
    row_rule = rule.get("row_handling") or {}
    mode = row_rule.get("mode", "RejectRepeated")
    if mode == "FilterThenSum":
        return {"mode": mode, "filter": rule.get("filter_selector"),
                "choices": row_rule.get("choices", [])}
    return {"mode": mode}


def _sum_quantity(cells: dict, columns: list[int | None], definition, max_work_digits: int, *,
                  validate_result: bool = True, parsed_operands: dict | None = None):
    values = []
    raw_operands = []
    missing = not columns or any(column is None for column in columns)
    first_error = None
    first_error_column = None
    for column in columns:
        cell = cells.get(str(column)) if column is not None else None
        if cell is None:
            raw_operands.append(None)
            missing = True
            continue
        raw_operands.append({"column": int(column), "value": canonical(cell.get("value")),
                             "kind": cell.get("kind"), "formula": bool(cell.get("formula")),
                             "missing_cache": bool(cell.get("missing_cache"))})
        if parsed_operands is not None and int(column) in parsed_operands:
            value, error, is_blank = parsed_operands[int(column)]
        else:
            value, error, is_blank = parse_sum_operand(cell)
            if parsed_operands is not None:
                parsed_operands[int(column)] = (value, error, is_blank)
        if error:
            if first_error is None:
                first_error = error
                first_error_column = int(column)
            continue
        if is_blank:
            missing = True
            continue
        values.append(value)
    if first_error:
        return None, first_error, raw_operands, missing, first_error_column
    if missing:
        return None, None, raw_operands, True, None
    nonzero = [value for value in values if value]
    if nonzero:
        lowest_exponent = min(value.as_tuple().exponent for value in nonzero)
        highest_adjusted = max(value.adjusted() for value in nonzero)
        required_span = highest_adjusted - lowest_exponent + len(str(len(nonzero))) + 1
        if required_span > max_work_digits:
            return None, (f"Sum operands exceed the configured numeric work limit of "
                          f"{max_work_digits} decimal digits."), raw_operands, False, None
    try:
        result = _exact_decimal_sum(values, max_work_digits)
    except ArithmeticError:
        return None, (f"Sum operands exceed the configured numeric work limit of "
                      f"{max_work_digits} decimal digits."), raw_operands, False, None
    error = quantity_error(definition.input_type, result) if validate_result else None
    if validate_result and error is None and result.as_tuple().exponent < -4:
        error = "At most four decimal places are supported."
    return (None if error else result), error, raw_operands, False, None


def _apply_row_handling(output_rows, definitions_by_target, context, issues, *, max_work_digits):
    """Collapse repeated resolved Driver/day contributions to one candidate per target."""
    by_driver_day = defaultdict(list)
    potential_rows = defaultdict(list)
    for row in output_rows:
        for value in row["values"].values():
            value.setdefault("is_candidate", True)
            value.setdefault("contribution_quantity", value.get("quantity"))
        if row["skipped"] or row["work_date"] is None:
            continue
        if row["context"].get("identity_resolved") and row["driver_assignment_id"] is not None:
            by_driver_day[(row["driver_id"], row["work_date"])].append(row)
        elif row["context"].get("potential_driver_ids"):
            for driver_id in row["context"]["potential_driver_ids"]:
                potential_rows[(driver_id, row["work_date"])].append(row)

    def append_issue(row, column, severity, code, message, value=None, *, target_id=None):
        item = {"file_ordinal": row["file_ordinal"], "unit_name": row["unit_name"],
                "source_row": row["source_row"], "column": column, "severity": severity,
                "code": code, "message": message, "value": value}
        if target_id is not None:
            item["_target_id"] = target_id
        row["issues"].append(item)
        issues.append(item)

    deferred_codes = {"IMPORT_QUANTITY_INVALID", "IMPORT_NEW_SOURCE_NOT_ELIGIBLE",
                      "IMPORT_VALUE_CONFLICT", "IMPORT_STATUS_DECISION_REQUIRED",
                      "IMPORT_DAY_OFF_DECISION_REQUIRED", "IMPORT_SUM_INPUT_MISSING"}
    for (driver_id, work_date), group_rows in by_driver_day.items():
        group_rows.sort(key=lambda row: (row["file_ordinal"], row["unit_name"], row["source_row"]))
        assignment_ids = {row["driver_assignment_id"] for row in group_rows}
        if len(assignment_ids) != 1:
            representative = group_rows[0]
            for index, row in enumerate(group_rows):
                for value in row["values"].values():
                    value["is_candidate"] = index == 0
                    value["quantity"] = None
                    value["is_blank"] = False
                    value["aggregate_status"] = "withheld"
                    value["aggregate_reason"] = "multiple_assignments"
                    value["outcome"] = "Unresolved"
            for row in group_rows:
                append_issue(row, None, "Error", "IMPORT_ROW_ASSIGNMENT_CONFLICT",
                             "Repeated Driver/day rows resolve to different Branch assignments; no aggregate is prepared.")
            continue
        policies = {encoded(row["context"].get("row_handling") or {"mode": "RejectRepeated"})
                    for row in group_rows}
        if len(policies) != 1:
            for row in group_rows:
                append_issue(row, None, "Error", "IMPORT_ROW_HANDLING_CONFLICT",
                             "Source units use different repeated-row rules; no quantity is prepared.")
            continue
        policy_value = group_rows[0]["context"].get("row_handling") or {"mode": "RejectRepeated"}
        mode = policy_value.get("mode", "RejectRepeated")
        possible_rows = potential_rows.get((driver_id, work_date), [])
        if mode == "RejectRepeated":
            if len(group_rows) < 2:
                if possible_rows:
                    row = group_rows[0]
                    for value in row["values"].values():
                        value["outcome"] = "Unresolved"
                    for possible in possible_rows:
                        for target_id, value in possible["values"].items():
                            if target_id in row["values"]:
                                value["is_candidate"] = False
                                value["quantity"] = None
                    append_issue(row, None, "Warning", "IMPORT_ROW_CONTRIBUTION_WITHHELD",
                                 "A known but unconfirmed Driver identity may add another row to this Driver/day.")
                continue
            for row in group_rows:
                for value in row["values"].values():
                    value["outcome"] = "Invalid"
                    value["quantity"] = None
                append_issue(row, None, "Error", "IMPORT_DUPLICATE_FACT",
                             "The same resolved Driver and WorkDate occurs more than once in this Import.")
            continue

        target_ids = sorted({target for row in group_rows for target in row["values"]})
        for target_id in target_ids:
            members = [row for row in group_rows if target_id in row["values"]]
            if not members:
                continue
            representative = members[0]
            definition = definitions_by_target.get(f"Definition:{target_id}")
            if definition is None:
                continue
            contributions = []
            for row in members:
                value = row["values"][target_id]
                if value.get("source_quantity_error"):
                    contributions.append({"state": "invalid", "error": value["source_quantity_error"]})
                elif row["context"].get("category_invalid"):
                    contributions.append({"state": "invalid", "error": "Category value is invalid."})
                elif mode == "FilterThenSum" and row["context"].get("category_decision") is None:
                    contributions.append({"state": "unknown_category"})
                elif value.get("is_blank"):
                    contributions.append({"state": "blank"})
                else:
                    raw_quantity = value.get("contribution_quantity", value.get("quantity"))
                    try:
                        number = Decimal(str(raw_quantity))
                    except (InvalidOperation, ValueError, TypeError):
                        number = None
                    if number is None or not number.is_finite():
                        contributions.append({"state": "invalid", "error": "An included source quantity is invalid."})
                    else:
                        contributions.append({"state": "present", "quantity": number})
            for possible in possible_rows:
                if target_id not in possible["values"]:
                    continue
                possible["values"][target_id]["is_candidate"] = False
                possible["values"][target_id]["quantity"] = None
                possible_value = possible["values"][target_id]
                if possible_value.get("source_quantity_error"):
                    contributions.append({"state": "invalid", "error": possible_value["source_quantity_error"]})
                else:
                    contributions.append({"state": "unresolved_identity"})
            result = aggregate_quantity_contributions(
                contributions, input_type=definition.input_type, max_work_digits=max_work_digits)
            original = representative["values"][target_id]
            for row in members:
                for item in row["issues"][:]:
                    if item["code"] in deferred_codes and item.get("_target_id") == target_id:
                        row["issues"].remove(item)
                        if item in issues:
                            issues.remove(item)
                value = row["values"][target_id]
                value["is_candidate"] = row is representative
                value["contribution_quantity"] = value.get("quantity")
                if row is not representative:
                    value["quantity"] = None
                    value["outcome"] = "Blank"
            original["quantity"] = canonical(result.get("quantity"))
            original["is_blank"] = result["status"] in {"absent", "withheld"}
            original["aggregate_status"] = result["status"]
            original["aggregate_reason"] = result.get("reason")
            if result["status"] == "invalid":
                original["outcome"] = "Invalid"
                append_issue(representative, original.get("source_column"), "Error",
                             "IMPORT_QUANTITY_INVALID", str(result.get("reason") or "Combined quantity is invalid."),
                             target_id=target_id)
                continue
            if result["status"] == "withheld":
                original["outcome"] = (
                    "Blank" if result.get("reason") == "incomplete_contribution" else "Unresolved")
                append_issue(representative, original.get("source_column"), "Warning",
                             "IMPORT_ROW_CONTRIBUTION_WITHHELD",
                             "At least one included row is incomplete or unresolved; no partial quantity is prepared.",
                             target_id=target_id)
                continue
            if result["status"] != "complete":
                original["outcome"] = "Blank"
                continue
            current = original.get("current_quantity")
            current_decimal = Decimal(str(current)) if current is not None else Decimal(0)
            current_present = original.get("current_source_exists", False)
            if any(row["context"].get("driver_identity", {}).get("requires_confirmation") for row in members):
                outcome = "Unresolved"
            elif current_decimal == result["quantity"]:
                outcome = "Unchanged"
            elif not current_present:
                outcome = "New"
            else:
                outcome = "Conflict"
            assignment_id = representative["driver_assignment_id"]
            work_date = representative["work_date"]
            assignment = next((item for item in context["assignments_by_driver"].get(
                representative["driver_id"], []) if item["driverassignmentid"] == assignment_id), None)
            fresh = bool(assignment and (assignment_id, work_date) in context["live_days"] and
                         _is_snapshot_row_eligible_for_workdate(SimpleNamespace(**assignment), work_date))
            quantity_value = result["quantity"]
            if quantity_value > 0 and not fresh and not current_present:
                outcome = "Invalid"
                append_issue(representative, original.get("source_column"), "Error",
                             "IMPORT_NEW_SOURCE_NOT_ELIGIBLE",
                             "Existing-source rescue permits changing existing input on this date, not adding new source.",
                             target_id=target_id)
            else:
                entry = context["entries"].get((assignment_id, work_date))
                day_off = context["days"].get(work_date, False)
                if quantity_value > 0 and outcome in {"New", "Conflict", "Unresolved"} and entry and entry["isoffreason"]:
                    append_issue(representative, original.get("source_column"), "Conflict",
                                 "IMPORT_STATUS_DECISION_REQUIRED",
                                 "Positive work overlaps a held full-day-off Status; no Status removal is selected.",
                                 target_id=target_id)
                if quantity_value > 0 and outcome in {"New", "Conflict", "Unresolved"} and day_off:
                    append_issue(representative, original.get("source_column"), "Conflict",
                                 "IMPORT_DAY_OFF_DECISION_REQUIRED",
                                 "Positive work is on a configured day off; no exception is accepted.",
                                 target_id=target_id)
            original["outcome"] = outcome
            if outcome == "Conflict":
                append_issue(representative, original.get("source_column"), "Conflict",
                             "IMPORT_VALUE_CONFLICT",
                             "Incoming combined quantity differs from the current quantity; no replacement is selected.",
                             target_id=target_id)


def build_batch_preview(sources: list[dict], context: dict, *, policy=None, completed_imports=None) -> dict:
    """Normalize selected source units together so duplicates span files/layouts."""
    definitions = context["definitions"]
    definitions_by_target = {target_key(item): item for item in definitions}
    sampled_driver_by_id = {candidate["driverid"]: candidate
                            for match in context.get("name_match_summaries", {}).values()
                            for candidate in match.get("samples", [])}
    period = context["period"]
    output_rows: list[dict] = []
    issues: list[dict] = []
    semantic_target_rules = defaultdict(dict)
    semantic_target_accumulators = {}
    prepared_rule_cache = {}
    semantic_target_expected = Counter()
    target_content_tokens = defaultdict(list)
    data_rows = candidates = selected_cells = 0

    row_policy_meanings = {
        encoded(_effective_row_policy(rule))
        for source in sources for rule in source.get("quantity_rules", [])
        if any(column is not None for column in rule.get("columns", []))
    }
    if len(row_policy_meanings) > 1:
        raise ImportFailure("IMPORT_ROW_HANDLING_CONFLICT",
                            "Selected source units use different repeated-row rules or category columns. Resolve row handling before preview.", 409)

    for source in sources:
        source_date = _date_source_semantics(source.get("date_source") or {})
        date_column = next((column for column, target in source["mapping"].items() if target == "WorkDate"), None)
        mapping = {str(column): target for column, target in source["mapping"].items()}
        allowed_targets = {item["target"] for item in mapping_targets(definitions)} | {"Filter"}
        if any(not str(column).isdigit() or int(column) < 1 or target not in allowed_targets
               for column, target in mapping.items()):
            raise ImportFailure("IMPORT_MAPPING_INVALID", "Saved target mapping is outside the current frozen period.", 422)
        assignments = [value for value in mapping.values() if value != "Ignore"]
        if not IDENTIFIERS.intersection(assignments):
            raise ImportFailure("IMPORT_MAPPING_REQUIRED_TARGET", "Map a Driver identifier before preview.", 422)
        if source_date.get("kind") == "column" and date_column is None:
            raise ImportFailure("IMPORT_DATE_SOURCE_CONFLICT", "Map a WorkDate column or choose a context date.", 422)
        materialized = source["materialized"]
        inspection = materialized["inspection"]
        mapped = {column: target for column, target in mapping.items() if target != "Ignore"}
        quantity_rules = source.get("quantity_rules", [])
        if not quantity_rules:
            raise ImportFailure("IMPORT_MAPPING_REQUIRED_TARGET", "Map at least one Pay Item target.")
        definition_columns = {}
        applicable_rules = [rule for rule in quantity_rules
                            if any(column is not None for column in rule.get("columns", []))]
        row_policies = {encoded(_effective_row_policy(rule)) for rule in applicable_rules}
        if len(row_policies) > 1:
            raise ImportFailure("IMPORT_ROW_HANDLING_CONFLICT",
                                "Mapped targets use different repeated-row rules. Resolve row handling before preview.", 409)
        row_policy = applicable_rules[0].get("row_handling") if applicable_rules else None
        effective_row_policy = _effective_row_policy(applicable_rules[0]) if applicable_rules else {
            "mode": "RejectRepeated"}
        filter_column = next((column for column, role in mapped.items() if role == "Filter"), None)
        category_decisions = {}
        row_rule_mode = (row_policy or {}).get("mode", "RejectRepeated")
        if row_rule_mode == "FilterThenSum":
            if filter_column is None:
                raise ImportFailure("IMPORT_RULE_PAYLOAD_UNSUPPORTED",
                                    "A saved category rule has no resolved filter column.", 409)
            category_decisions = {category_key_from_json(choice["category"]): choice["decision"]
                                  for choice in row_policy.get("choices", [])}
        for rule in quantity_rules:
            target = rule.get("target")
            if (target not in definitions_by_target or rule.get("transform") not in {"Direct", "Sum"}
                    or not isinstance(rule.get("columns"), list) or not rule["columns"]
                    or rule["transform"] == "Direct" and len(rule["columns"]) != 1
                    or rule["transform"] == "Sum" and len(rule["columns"]) < 2):
                raise ImportFailure("IMPORT_RULE_PAYLOAD_UNSUPPORTED", "A saved target rule cannot be previewed.", 409)
            if target in definition_columns:
                raise ImportFailure("IMPORT_MAPPING_DUPLICATE_TARGET", "A target rule was resolved more than once.", 422)
            definition_columns[target] = (definitions_by_target[target], rule)
            definition = definitions_by_target[target]
            def actual_selector(column, saved):
                if column is None:
                    return saved
                header = inspection["header_cells"][int(column) - 1]
                normalized = normalize_header(header)
                occurrence = next((item for item in inspection.get("header_occurrences", [])
                                   if item["source_column"] == int(column)), None)
                selector = {"header": normalized}
                if occurrence and occurrence.get("occurrence_count", 1) > 1:
                    selector["occurrences"] = [{"occurrence_ordinal": occurrence["occurrence_ordinal"]}]
                return selector
            input_pairs = [(actual_selector(column, selector), column)
                           for selector, column in zip(
                               rule.get("inputs", rule.get("source_signature", {}).get("inputs", [])),
                               rule.get("columns", []), strict=True)]
            input_pairs.sort(key=lambda pair: encoded(pair[0]))
            effective_inputs = [pair[0] for pair in input_pairs]
            effective_columns = [pair[1] for pair in input_pairs]
            date_authority = {"kind": source_date["kind"]}
            if source_date["kind"] == "column":
                work_date_column = next((column for column, role in mapped.items() if role == "WorkDate"), None)
                date_authority["selector"] = actual_selector(
                    work_date_column, rule.get("shared_columns", {}).get("WorkDate"))
            semantic_shared = dict(rule.get("shared_columns", {}))
            for role, selector in list(semantic_shared.items()):
                role_column = next((column for column, mapped_role in mapped.items() if mapped_role == role), None)
                semantic_shared[role] = actual_selector(role_column, selector)
            try:
                meaning = canonical_rule_meaning(
                    target_meaning=definition_meaning(definition), transform=rule["transform"],
                    inputs=effective_inputs,
                    shared_columns=semantic_shared,
                    row_handling=rule.get("row_handling") or {"mode": "RejectRepeated"},
                    date_authority=date_authority)
            except SemanticInputError:
                meaning = None
            prepared_rule = None
            if meaning is not None:
                meaning_key = encoded(meaning)
                prepared_rule = prepared_rule_cache.get(meaning_key)
                if prepared_rule is None:
                    prepared_rule = prepare_rule_meaning(meaning)
                    prepared_rule_cache[meaning_key] = prepared_rule
            selector_columns = (MappingProxyType(dict(zip(prepared_rule.selector_keys, effective_columns, strict=True)))
                                if prepared_rule is not None else MappingProxyType({}))
            semantic_target_rules[target][source.get("source_unit_id")] = {
                "meaning": prepared_rule.meaning if prepared_rule is not None else None,
                "prepared": prepared_rule, "columns": tuple(effective_columns),
                "columns_by_selector": selector_columns}
            if prepared_rule is not None and target not in semantic_target_accumulators:
                semantic_target_accumulators[target] = TargetReceiptAccumulator(
                    scope={"company_id": period["company_id"], "branch_id": period["branch_id"],
                           "period_id": period["id"]},
                    pay_definition_id=definition.pay_definition_id,
                    target_meaning=definition_meaning(definition), rule_meaning=meaning,
                    prepared_rule=prepared_rule)
        date_column = next((column for column, target in mapped.items() if target == "WorkDate"), None)
        raw_rows = materialized["rows"]
        def has_operand(item, rule):
            return any(column is not None and not blank(item["cells"].get(str(column), _date_cell(None)))
                       for column in rule["columns"])

        def is_known_excluded(item):
            if row_rule_mode != "FilterThenSum" or filter_column is None:
                return False
            cell = item["cells"].get(str(filter_column))
            try:
                return category_decisions.get(typed_category_key(cell or _date_cell(None))) == "Exclude"
            except CategoryEvidenceError:
                return False

        has_sum_rule = any(rule["transform"] == "Sum" for _definition, rule in definition_columns.values())
        row_policy_activates_identity = has_sum_rule or row_rule_mode != "RejectRepeated"
        active_rows = [item for item in raw_rows if not is_known_excluded(item) and (any(
            has_operand(item, rule) for _definition, rule in definition_columns.values())
            or row_policy_activates_identity and any(not blank(item["cells"].get(str(column), _date_cell(None)))
                                    for column, target in mapped.items()
                                    if target in IDENTIFIERS or
                                    target == "WorkDate" and source_date.get("kind") == "column")
            or row_rule_mode == "FilterThenSum" and filter_column is not None
            and not blank(item["cells"].get(str(filter_column), _date_cell(None))))]
        effective_date_column = date_column if source_date.get("kind") == "column" else None
        date_cells = [item["cells"].get(str(effective_date_column)) for item in active_rows] if effective_date_column else []
        date_order, order_state = infer_date_order(date_cells, period["start"], period["end"],
                                                   source.get("date_order"))
        date_results = []
        name_date_result = None
        if effective_date_column is not None:
            date_results = [interpret_date(item["cells"].get(str(effective_date_column), _date_cell(None)),
                                           period_days=context["days"].keys(), selected_order=date_order,
                                           source_kind="column") for item in active_rows]
        elif source_date["kind"] in {"filename", "sheet"}:
            label = source.get("file_name") if source_date["kind"] == "filename" else source.get("unit_name")
            name_date_result = interpret_date(label, period_days=context["days"].keys(),
                                              selected_order=source.get("date_order"),
                                              source_kind=source_date["kind"],
                                              selected_candidate=source_date.get("value"))
        date_evidence = date_results or ([name_date_result] if name_date_result is not None else [])
        if date_order is None:
            numeric_orders = set()
            for result in date_evidence:
                pattern = result.get("pattern")
                match = re.match(r"numeric_(DMY|MDY|YMD)(?:_|$)", pattern or "")
                if match and result.get("status") == "resolved":
                    numeric_orders.add(match.group(1))
            if len(numeric_orders) == 1:
                date_order = next(iter(numeric_orders))
                order_state = "detected"
                if effective_date_column is not None:
                    date_results = [interpret_date(item["cells"].get(str(effective_date_column), _date_cell(None)),
                                                   period_days=context["days"].keys(),
                                                   selected_order=date_order, source_kind="column")
                                    for item in active_rows]
                    date_evidence = date_results
            elif len(numeric_orders) > 1:
                order_state = "not_required"
            else:
                has_ambiguous_numeric = any(
                    result.get("status") == "ambiguous"
                    or result.get("status") == "unrecognized"
                    and result.get("reason") in {"unsupported_numeric_cell_text", "unsupported_or_incomplete_text"}
                    and re.search(r"\d\s*[/.-]\s*\d", str(value.get("value", ""))) is not None
                    for result, value in zip(date_evidence, date_cells if effective_date_column is not None
                                             else [source.get("file_name") if source_date["kind"] == "filename"
                                                   else source.get("unit_name")], strict=False))
                has_nonblank_date = any(not blank(cell) for cell in date_cells) if effective_date_column else bool(
                    name_date_result and name_date_result.get("status") not in {"unrecognized"})
                order_state = "unresolved" if has_ambiguous_numeric and has_nonblank_date else "not_required"
        if effective_date_column is None and name_date_result is not None:
            date_results = [name_date_result]
        saved_order = inspection.get("accepted_date_order")
        saved_state = inspection.get("date_order_state")
        if source.get("date_order_state_override") in {
                "selected", "detected", "matched_previous_pattern", "not_required"}:
            order_state = source["date_order_state_override"]
        elif (saved_order is not None and saved_order == date_order
              and saved_state in {"selected", "detected", "matched_previous_pattern", "not_required"}):
            order_state = saved_state
        source["accepted_date_order"] = date_order
        source["date_order_state"] = order_state
        source["date_column"] = date_column
        source_contract = {
            "structure": source["structure_fingerprint"],
            "unit_kind": source.get("unit_kind"),
            "mapping": [{"column": int(column), "target": target}
                        for column, target in _numbered(mapping) if target != "Ignore" and
                        (target != "WorkDate" or source_date.get("kind") == "column")],
            "quantity_rules": [{"target": definition_meaning(definition),
                                "transform": rule["transform"],
                                "source_signature": rule.get("source_signature")}
                               for definition, rule in definition_columns.values()],
            "date_source": source_date,
            "date_order": date_order,
            "row_handling": effective_row_policy,
            "header_row_number": inspection["header_row_number"],
            "headers": inspection.get("header_cells", []),
            "encoding": inspection.get("encoding"), "delimiter": inspection.get("delimiter"),
        }
        source["row_contract_fingerprint"] = fingerprint("row-source-contract", source_contract)
        source["date_column"] = effective_date_column
        source["date_profile"] = make_date_profile(source_date["kind"], date_order, date_results)
        source["date_resolved_values"] = sorted({item["resolved_date"] for item in date_results
                                                  if item.get("resolved_date")})
        for source_row in raw_rows:
            cells = source_row["cells"]
            parsed_operands = {}

            def parsed_operand(column):
                if column is None:
                    return None, None, True
                column = int(column)
                if column not in parsed_operands:
                    parsed_operands[column] = parse_sum_operand(
                        cells.get(str(column), _date_cell(None)))
                return parsed_operands[column]

            category_cell = cells.get(str(filter_column)) if filter_column is not None else None
            category_key = None
            category_decision = None
            category_invalid = False
            if row_rule_mode == "FilterThenSum":
                try:
                    category_key = typed_category_key(category_cell or _date_cell(None))
                    category_decision = category_decisions.get(category_key)
                except CategoryEvidenceError:
                    category_invalid = True
            proposed_excluded = category_decision == "Exclude"
            has_quantity = any(has_operand(source_row, rule)
                               for _definition, rule in definition_columns.values())
            has_identity_or_date = any(not blank(cells.get(str(column), _date_cell(None)))
                                       for column, kind in mapped.items()
                                       if kind in IDENTIFIERS or kind == "WorkDate" and effective_date_column is not None)
            skipped = proposed_excluded or (not has_quantity and not (row_policy_activates_identity and has_identity_or_date)
                and not (row_rule_mode == "FilterThenSum" and filter_column is not None
                         and not blank(category_cell or _date_cell(None))))
            data_rows += 0 if skipped else 1
            candidates += 0 if skipped else sum(any(column is not None for column in rule["columns"])
                                                for _definition, rule in definition_columns.values())
            selected_cells += len(mapped) + sum(sum(column is not None for column in rule["columns"])
                                                for _definition, rule in definition_columns.values())
            row_issues: list[dict] = []

            def issue(column, severity, code, message, *, value_override=None, target_id=None):
                value = cells.get(str(column)) if column else None
                item = {"file_ordinal": source["file_ordinal"], "unit_name": source["unit_name"],
                        "source_row": source_row["source_row"], "column": int(column) if column else None,
                        "severity": severity, "code": code, "message": message,
                        "value": (value_override if value_override is not None else
                                  None if value is None else canonical(value.get("value")))}
                if target_id is not None:
                    item["_target_id"] = target_id
                row_issues.append(item)
                issues.append(item)

            if proposed_excluded:
                exclusion = {"status": "proposed_excluded", "rule": "FilterThenSum",
                             "category": {"type": category_key[0], "value": category_key[1]}}
                source_semantics = {"excluded": exclusion,
                                    "source": [source["file_ordinal"], source["unit_name"], source_row["source_row"]],
                                    "structure": source["structure_fingerprint"]}
                row_fp = fingerprint("row", source_semantics)
                output_rows.append({
                    "file_ordinal": source["file_ordinal"], "unit_name": source["unit_name"],
                    "source_row": source_row["source_row"], "source_unit_id": source.get("source_unit_id"),
                    "source_record_id": None, "driver_id": None, "driver_assignment_id": None,
                    "work_date": None, "row_fingerprint": row_fp, "skipped": True,
                    "raw_payload": {key: {"value": canonical(cell.get("value")), "kind": cell.get("kind"),
                                          "formula": bool(cell.get("formula")),
                                          "missing_cache": bool(cell.get("missing_cache"))}
                                    for key, cell in cells.items()},
                    "values": {}, "issues": [], "identifiers": [],
                    "context": {"proposed_exclusion": exclusion},
                    "group_fingerprint": fingerprint("excluded-source", {
                        "source_unit": [source["file_ordinal"], source["unit_name"]],
                        "category": exclusion["category"]}),
                    "driver_display": {}, "semantic": source_semantics,
                })
                continue
            if row_rule_mode == "FilterThenSum" and not skipped and (category_invalid or category_decision is None):
                issue(filter_column, "Error" if category_invalid else "Warning",
                      "IMPORT_CATEGORY_VALUE_INVALID" if category_invalid else "IMPORT_CATEGORY_UNRESOLVED",
                      "This category value has no Include or Exclude choice; affected quantities are withheld.",
                      value_override=None if category_cell is None else canonical(category_cell.get("value")))

            provided_identifiers = []
            row_error = False
            for column, kind in mapped.items():
                if kind not in IDENTIFIERS:
                    continue
                cell = cells.get(column, _date_cell(None))
                if blank(cell):
                    continue
                value = normalized_identifier(kind, cell["value"])
                provided_identifiers.append({"column": int(column), "kind": kind, "value": value,
                    "source_value": canonical(cell.get("value")), "source_kind": cell.get("kind"),
                    "missing_cache": bool(cell.get("missing_cache"))})
                if not skipped and cell.get("kind") == "number":
                    issue(column, "Warning", "IMPORT_NUMERIC_IDENTIFIER",
                          "Numeric identifier may have lost leading zeroes; no padding is inferred.")
            identity = resolve_row_identity(provided_identifiers, context["identity_indexes"],
                                            context.get("name_match_summaries", {}),
                                            context["drivers_by_id"])
            identifiers = identity["identifiers"]
            driver_id = identity["driver_id"]
            proposed = identity["status"] == "Proposed"
            identity_requires_confirmation = identity["requires_confirmation"]
            identity_resolved = identity["status"] == "Exact" and not identity_requires_confirmation
            if identity["issue_code"] and not skipped:
                row_error = identity["issue_severity"] == "Error"
                issue_column = (provided_identifiers[0]["column"] if provided_identifiers else
                                next((c for c, target in mapped.items() if target in IDENTIFIERS), None))
                issue_value = None
                if identity_requires_confirmation or identity["status"] in {"NameDiscrepancy", "Disagreement"}:
                    review_drivers = []
                    for candidate_id in identity["review_candidate_ids"]:
                        candidate = context["drivers_by_id"].get(candidate_id) or sampled_driver_by_id.get(candidate_id)
                        if candidate is not None:
                            review_drivers.append({"driver_number": candidate.get("drivernumber"),
                                "employee_key": candidate.get("employeekey"),
                                "driver_name": candidate.get("drivername", candidate.get("fullname"))})
                    issue_value = {"provided": {kind: [item["source_value"] for item in provided_identifiers
                                                         if item["kind"] == kind]
                                                 for kind in ("DriverNumber", "EmployeeKey", "DriverName")
                                                 if any(item["kind"] == kind for item in provided_identifiers)},
                                   "canonical_candidates": review_drivers}
                issue(issue_column, identity["issue_severity"], identity["issue_code"],
                      identity["issue_message"], value_override=issue_value)
            selected_driver = context["drivers_by_id"].get(driver_id) if driver_id is not None else None
            review_driver_details = []
            for candidate_id in identity["review_candidate_ids"]:
                candidate = context["drivers_by_id"].get(candidate_id) or sampled_driver_by_id.get(candidate_id)
                if candidate is not None:
                    review_driver_details.append({"driver_id": candidate_id,
                        "driver_number": candidate.get("drivernumber"),
                        "employee_key": candidate.get("employeekey"),
                        "driver_name": candidate.get("drivername", candidate.get("fullname"))})
            driver_identity = {"status": identity["status"],
                "requires_confirmation": identity_requires_confirmation,
                "provided": {kind: [item["source_value"] for item in provided_identifiers
                                    if item["kind"] == kind]
                             for kind in ("DriverNumber", "EmployeeKey", "DriverName")
                             if any(item["kind"] == kind for item in provided_identifiers)},
                "canonical_driver": ({"driver_id": driver_id,
                    "driver_number": selected_driver.get("drivernumber"),
                    "employee_key": selected_driver.get("employeekey"),
                    "driver_name": selected_driver.get("drivername")}
                    if selected_driver else None),
                "review_candidates": review_driver_details[:2]}

            date_value = None
            date_cell = cells.get(str(effective_date_column), _date_cell(None)) if effective_date_column is not None else None
            date_result = (interpret_date(date_cell, period_days=context["days"].keys(),
                                          selected_order=date_order, source_kind="column")
                           if effective_date_column is not None else name_date_result)
            if date_result is not None:
                date_value = (date.fromisoformat(date_result["resolved_date"])
                              if date_result.get("resolved_date") else None)
                if date_value is not None:
                    source_date_value = {"date": date_value.isoformat()}
                else:
                    source_date_value = {"status": date_result["status"], "reason": date_result["reason"],
                                         "explicit_year": date_result.get("explicit_year"),
                                         "pattern": date_result.get("pattern")}
                if date_value is None and effective_date_column is not None:
                    source_date_value.update({"raw_value": canonical(date_cell.get("value")),
                                              "kind": date_cell.get("kind"),
                                              "formula": bool(date_cell.get("formula")),
                                              "missing_cache": bool(date_cell.get("missing_cache"))})
                elif date_value is None and source_date.get("kind") in {"filename", "sheet"}:
                    source_date_value["raw_value"] = (source.get("file_name") if source_date["kind"] == "filename"
                                                       else source.get("unit_name"))
                date_review = {key: date_result[key] for key in
                               ("status", "reason", "pattern", "explicit_year", "explanation")}
                date_review["resolved_date"] = date_result.get("resolved_date")
                date_review["candidate_count"] = len(date_result["candidate_dates"])
                date_review["candidate_dates"] = date_result["candidate_dates"][:(
                    policy or DEFAULT_IMPORT_RESOURCE_POLICY).row_detail_sample]
            else:
                source_date_value = {"status": "error", "reason": "missing_provider", "date": None}
                date_review = {"status": "error", "reason": "missing_provider", "candidate_count": 0,
                               "candidate_dates": [], "explanation": "Choose a date authority for this source unit."}
            if not skipped and (date_value is None or date_value not in context["days"]):
                row_error = True
                issue(effective_date_column, "Error", "IMPORT_WORK_DATE_INVALID", date_review["explanation"])
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
            semantic_inputs = {}
            for target, (definition, rule) in definition_columns.items():
                defid = str(definition.payroll_period_definition_id)
                columns = rule["columns"]
                is_sum = rule["transform"] == "Sum"
                issue_column = next((item for item in columns if item is not None), None)
                raw_operands = None
                missing_sum = False
                if is_sum:
                    value, error, raw_operands, missing_sum, error_column = _sum_quantity(
                        cells, columns, definition,
                        policy.max_numeric_work_digits if policy else DEFAULT_IMPORT_RESOURCE_POLICY.max_numeric_work_digits,
                        validate_result=row_rule_mode == "RejectRepeated", parsed_operands=parsed_operands)
                    if error_column is not None:
                        issue_column = error_column
                    cell = {"value": canonical(value), "kind": "number", "formula": False,
                            "missing_cache": False} if value is not None else _date_cell(None)
                    is_blank = missing_sum
                else:
                    column = columns[0]
                    cell = cells.get(str(column), _date_cell(None)) if column is not None else _date_cell(None)
                    is_blank = blank(cell)
                    if skipped:
                        value, error = None, None
                    elif row_rule_mode != "RejectRepeated":
                        value, error, is_blank = parsed_operand(column)
                    else:
                        value, error = quantity(cell, SimpleNamespace(input_type=definition.input_type))
                source_columns = [int(item) for item in columns if item is not None]
                if not source_columns:
                    if is_sum and not skipped:
                        issue(None, "Warning", "IMPORT_SUM_INPUT_MISSING",
                              "A required Sum input is missing or blank; no quantity is supplied for this target.",
                              value_override=raw_operands, target_id=defid)
                    semantic_values[definition.code] = {
                        "transform": "Sum" if is_sum else "Direct",
                        "operands": raw_operands if is_sum else None,
                        "blank": True, "quantity": None, "error": error, "missing_source": True,
                    }
                    continue
                source_quantity_error = error
                key = (assignment_id, date_value, definition.payroll_period_definition_id)
                current_present = key in context["current_lines"] if assignment_id and date_value else False
                current = context["current_lines"].get(key)
                current_provenance = context["current_line_provenance"].get(key) if current_present else None
                if error:
                    outcome = "Invalid"
                elif is_blank:
                    outcome = "Blank"
                elif identity_requires_confirmation:
                    outcome = "Unresolved"
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
                if (row_rule_mode == "RejectRepeated" and value is not None and assignment_id and date_value
                        and value > 0 and not fresh and not current_present):
                    outcome = "Invalid"
                    error = "Existing-source rescue permits changing existing input on this date, not adding new source."
                if error:
                    issue(issue_column, "Error", "IMPORT_QUANTITY_INVALID" if not (value is not None and not fresh and not current_present)
                          else "IMPORT_NEW_SOURCE_NOT_ELIGIBLE", error,
                          value_override=raw_operands if raw_operands is not None else None,
                          target_id=defid)
                elif missing_sum and not skipped and row_rule_mode == "RejectRepeated":
                    issue(issue_column, "Warning",
                          "IMPORT_SUM_INPUT_MISSING",
                          "A required Sum input is missing or blank; no quantity is supplied for this target.",
                          value_override=raw_operands, target_id=defid)
                if outcome == "Conflict" and row_rule_mode == "RejectRepeated":
                    issue(issue_column, "Conflict", "IMPORT_VALUE_CONFLICT",
                          "Incoming quantity differs from the current quantity; no replacement is selected.",
                          target_id=defid)
                entry_state = context["entries"].get((assignment_id, date_value)) if assignment_id and date_value else None
                day_off = bool(date_value and context["days"].get(date_value, False))
                if (row_rule_mode == "RejectRepeated" and value is not None and value > 0
                        and outcome in ("New", "Conflict", "Unresolved") and entry_state and entry_state["isoffreason"]):
                    issue(issue_column, "Conflict", "IMPORT_STATUS_DECISION_REQUIRED",
                          "Positive work overlaps a held full-day-off Status; no Status removal is selected.",
                          target_id=defid)
                if (row_rule_mode == "RejectRepeated" and value is not None and value > 0
                        and outcome in ("New", "Conflict", "Unresolved") and day_off):
                    issue(issue_column, "Conflict", "IMPORT_DAY_OFF_DECISION_REQUIRED",
                          "Positive work is on a configured day off; no exception is accepted.",
                          target_id=defid)
                source_columns = [int(item) for item in columns if item is not None]
                raw_value = raw_operands if raw_operands is not None else canonical(cell.get("value"))
                values[defid] = {"source_column": source_columns[0] if source_columns else None,
                                 "source_columns": source_columns, "transform": "Sum" if is_sum else "Direct",
                                 "raw_value": raw_value,
                                 "quantity": canonical(value), "is_blank": is_blank, "outcome": outcome,
                                 "contribution_is_blank": is_blank,
                                 "current_quantity": canonical(current), "current_source_exists": current_present,
                                 "current_provenance": current_provenance,
                                 "is_candidate": True,
                                 "contribution_quantity": canonical(value),
                                 "contribution_error": error,
                                 "source_quantity_error": source_quantity_error}
                semantic_values[definition.code] = (
                    {"transform": "Sum" if is_sum else "Direct", "operands": raw_operands,
                     "blank": is_blank, "quantity": canonical(value), "error": source_quantity_error}
                    if is_sum or not source_quantity_error else
                    {"invalid": True, "raw_value": canonical(cell.get("value")), "kind": cell.get("kind"),
                     "formula": bool(cell.get("formula")), "missing_cache": bool(cell.get("missing_cache"))}
                )
                source_rule = semantic_target_rules[target].get(source.get("source_unit_id"), {})
                if meaning := source_rule.get("meaning"):
                    if not source_columns:
                        continue
                    prepared_rule = source_rule["prepared"]
                    input_selectors = prepared_rule.selectors
                    operands = []
                    if is_sum:
                        for selector, selector_key in zip(
                                input_selectors, prepared_rule.selector_keys, strict=True):
                            column = source_rule["columns_by_selector"][selector_key]
                            if column is None:
                                operands.append({"selector": selector, "state": "absent"})
                                continue
                            operand_value, operand_error, operand_blank = parsed_operand(column)
                            if operand_error:
                                operands = []
                                break
                            operands.append({"selector": selector,
                                "state": "blank" if operand_blank else "value",
                                **({} if operand_blank else {"value": operand_value})})
                    else:
                        column = source_rule["columns_by_selector"][prepared_rule.selector_keys[0]]
                        if column is None:
                            operands = []
                        else:
                            operand_value, operand_error, operand_blank = parsed_operand(column)
                            if operand_error:
                                operands = []
                            else:
                                operands = [{"selector": input_selectors[0],
                                    "state": "blank" if operand_blank else "value",
                                    **({} if operand_blank else {"value": operand_value})}]
                    if operands:
                        semantic_inputs[target] = {"rule_meaning": meaning, "operands": operands}

            source_semantics = {"identifiers": sorted((item["kind"], item["value"])
                                                        for item in identifiers),
                                "date": source_date_value,
                                "date_source": source_date, "values": semantic_values,
                                "category": (None if category_key is None else
                                             {"type": category_key[0], "value": category_key[1]}),
                                "category_decision": category_decision,
                                "unit_kind": source.get("unit_kind"),
                                "structure": source["structure_fingerprint"],
                                "source_contract": source["row_contract_fingerprint"], "skipped": skipped}
            for target in definition_columns:
                value_key = str(definition_columns[target][0].payroll_period_definition_id)
                if value_key not in values:
                    continue
                semantic_active = not skipped or identity_resolved and date_value is not None
                if not semantic_active:
                    continue
                semantic_target_expected[target] += 1
                source_rule = semantic_target_rules[target].get(source.get("source_unit_id"), {})
                meaning = source_rule.get("meaning")
                if meaning is not None:
                    prepared_rule = source_rule["prepared"]
                    business_operands = []
                    for selector, selector_key in zip(
                            prepared_rule.selectors, prepared_rule.selector_keys, strict=True):
                        column = source_rule["columns_by_selector"][selector_key]
                        if column is None:
                            business_operands.append({"selector": selector, "state": "absent"})
                            continue
                        raw_cell = cells.get(str(column), _date_cell(None))
                        operand_value, operand_error, operand_blank = parsed_operand(column)
                        if operand_error:
                            business_operands.append({"selector": selector, "state": "invalid",
                                "raw_value": canonical(raw_cell.get("value")), "kind": raw_cell.get("kind"),
                                "formula": bool(raw_cell.get("formula")),
                                "missing_cache": bool(raw_cell.get("missing_cache"))})
                        else:
                            business_operands.append({"selector": selector,
                                "state": "blank" if operand_blank else "value",
                                **({} if operand_blank else {"value": operand_value})})
                    input_identity = {"provided": sorted((item["kind"], item["value"])
                                                          for item in identifiers)}
                    target_business_record = {
                        "rule": meaning, **input_identity,
                        "date": date_value.isoformat() if date_value else source_date_value,
                        "operands": business_operands,
                        "source_error": semantic_values.get(definition_columns[target][0].code, {}).get("error"),
                        "source_value_invalid": bool(semantic_values.get(
                            definition_columns[target][0].code, {}).get("invalid")),
                        "category": (None if category_key is None else
                                     {"type": category_key[0], "value": category_key[1]}),
                        "category_decision": category_decision,
                    }
                    target_content_tokens[target].append(fingerprint("semantic-target-row", target_business_record))
                accumulator = semantic_target_accumulators.get(target)
                semantic_input = semantic_inputs.get(target)
                if accumulator is None or semantic_input is None or not identity_resolved or date_value is None:
                    if accumulator is not None:
                        accumulator.mark_invalid()
                    continue
                supplied = {item["kind"]: item["value"] for item in identifiers}
                semantic_row = {
                    **{key: value for key, value in semantic_input.items() if key != "rule_meaning"},
                    "state": "included", "identity_status": identity["status"],
                    "requires_confirmation": identity_requires_confirmation,
                    "driver": {"driver_number": supplied.get("DriverNumber"),
                               "employee_key": supplied.get("EmployeeKey"),
                               "name": supplied.get("DriverName")},
                    "work_date": date_value.isoformat(),
                    "category": (None if category_key is None else
                                 {"type": category_key[0], "value": category_key[1]}),
                    "category_decision": category_decision,
                }
                try:
                    accumulator.add_prepared(
                        semantic_row, ({"kind": item["kind"], "value": item["value"],
                                        "driver_id": driver_id} for item in identifiers
                                       if item["kind"] in IDENTIFIERS), source_rule["prepared"])
                except SemanticInputError:
                    accumulator.mark_invalid()
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
                            "source_date": source_date, "date_interpretation": date_review,
                            "driver_identity": driver_identity,
                            "identity_resolved": identity_resolved,
                            "potential_driver_ids": (identity["review_candidate_ids"]
                                                      if not identity_resolved else []),
                            "category": (None if category_key is None else
                                         {"type": category_key[0], "value": category_key[1]}),
                            "category_decision": category_decision,
                            "category_invalid": category_invalid,
                            "row_handling": {"mode": row_rule_mode}},
                "group_fingerprint": fingerprint("group", {
                    "driver": driver_meaning(context["drivers_by_id"][driver_id]) if driver_id in context["drivers_by_id"] else None,
                    "date": date_value.isoformat() if date_value else None,
                    "unresolved_source": [source["file_ordinal"], source["unit_name"], source_row["source_row"]] if driver_id is None or date_value is None else None,
                }),
                "driver_display": {"driver_name": context["drivers_by_id"].get(driver_id, {}).get("drivername"),
                                   "driver_number": context["drivers_by_id"].get(driver_id, {}).get("drivernumber"),
                                   "employee_key": context["drivers_by_id"].get(driver_id, {}).get("employeekey")},
                "semantic": source_semantics,
            })

    if policy:
        if data_rows > policy.max_data_rows:
            raise ImportFailure("IMPORT_DATA_ROW_LIMIT_EXCEEDED", "Selected batch exceeds its configured data-row limit.")
        if candidates > policy.max_candidates:
            raise ImportFailure("IMPORT_CANDIDATE_LIMIT_EXCEEDED", "Selected batch exceeds its configured mapped-candidate limit.")
        if selected_cells > policy.max_selected_cells:
            raise ImportFailure("IMPORT_SELECTED_CELL_LIMIT_EXCEEDED", "Selected batch exceeds its configured cell limit.")

    _apply_row_handling(output_rows, definitions_by_target, context, issues,
                        max_work_digits=(policy or DEFAULT_IMPORT_RESOURCE_POLICY).max_numeric_work_digits)
    semantic_receipts = []
    for target, expected in semantic_target_expected.items():
        if expected < 1:
            continue
        accumulator = semantic_target_accumulators.get(target)
        if accumulator is None:
            continue
        blocked_codes = {"IMPORT_DUPLICATE_FACT",
                         "IMPORT_QUANTITY_INVALID",
                         "IMPORT_CATEGORY_VALUE_INVALID", "IMPORT_CATEGORY_UNRESOLVED",
                         "IMPORT_WORK_DATE_INVALID", "IMPORT_DRIVER_UNRESOLVED",
                         "IMPORT_DRIVER_DISAGREEMENT", "IMPORT_DRIVER_NAME_DISCREPANCY",
                         "IMPORT_DRIVER_CONFIRMATION_REQUIRED"}
        if any(issue_item["code"] in blocked_codes and
               (issue_item.get("_target_id") in {None, str(
                   definitions_by_target[target].payroll_period_definition_id)}) for issue_item in issues):
            continue
        try:
            receipt = accumulator.build()
        except SemanticInputError:
            continue
        if receipt["record_count"] != expected:
            continue
        semantic_receipts.append(receipt)
    completed_matches = matching_completed_receipts(semantic_receipts, completed_imports or [])
    receipt_target = {definition.pay_definition_id: {
        "target": f"Definition:{definition.payroll_period_definition_id}",
        "value_key": str(definition.payroll_period_definition_id)} for definition in definitions}
    repeat_notice = {"state": "None", "matched_target_count": 0,
                     "supplied_target_count": len(semantic_target_expected), "targets": []}
    if completed_matches:
        current_by_pay_definition = {item["pay_definition_id"]: item for item in semantic_receipts}
        for pay_definition_id, completed in completed_matches.items():
            target_info = receipt_target.get(pay_definition_id)
            if target_info is None:
                continue
            target_id = target_info["target"]
            value_key = target_info["value_key"]
            for row in output_rows:
                value = row["values"].get(value_key)
                if value is None:
                    continue
                value["outcome"] = "AlreadyImported"
                value["action"] = "NoOp"
                value["repeat_input_sha256"] = current_by_pay_definition[pay_definition_id]["input_sha256"]
                if value.get("is_blank"):
                    value["is_candidate"] = False
                removable = {"IMPORT_NEW_SOURCE_NOT_ELIGIBLE", "IMPORT_VALUE_CONFLICT",
                             "IMPORT_STATUS_DECISION_REQUIRED", "IMPORT_DAY_OFF_DECISION_REQUIRED"}
                removed = [item for item in row["issues"]
                           if item.get("_target_id") == value_key and item["code"] in removable]
                for item in removed:
                    row["issues"].remove(item)
                    if item in issues:
                        issues.remove(item)
            repeat_notice["targets"].append({
                "target": target_id, "pay_definition_id": pay_definition_id,
                "prior_import_id": completed["import_id"],
                "completed_by_user_id": completed.get("completed_by_user_id"),
                "completed_at": completed.get("completed_at"),
            })
            definition = next(item for item in definitions if item.pay_definition_id == pay_definition_id)
            row_groups = defaultdict(list)
            for row in output_rows:
                value = row["values"].get(value_key)
                if (value is not None and row["context"].get("identity_resolved")
                        and row["driver_id"] is not None and row["work_date"] is not None
                        and row["context"].get("row_handling", {}).get("mode") in {"SumRows", "FilterThenSum"}):
                    row_groups[(row["driver_id"], row["work_date"])].append(row)
            for members in row_groups.values():
                if len(members) < 2:
                    continue
                members.sort(key=lambda item: (item["file_ordinal"], item["unit_name"], item["source_row"]))
                contributions = []
                for member in members:
                    value = member["values"][value_key]
                    if value.get("source_quantity_error"):
                        contributions.append({"state": "invalid", "error": value["source_quantity_error"]})
                    elif value.get("contribution_is_blank", value.get("is_blank")):
                        contributions.append({"state": "blank"})
                    else:
                        contributions.append({"state": "present",
                            "quantity": Decimal(str(value.get("contribution_quantity")))})
                aggregate = aggregate_quantity_contributions(
                    contributions, input_type=definition.input_type,
                    max_work_digits=(policy or DEFAULT_IMPORT_RESOURCE_POLICY).max_numeric_work_digits)
                for index, member in enumerate(members):
                    value = member["values"][value_key]
                    value["is_candidate"] = index == 0 and aggregate["status"] == "complete"
                    value["aggregate_status"] = aggregate["status"]
                    value["aggregate_reason"] = aggregate.get("reason")
                    if index == 0 and aggregate["status"] == "complete":
                        value["quantity"] = canonical(aggregate["quantity"])
                        value["is_blank"] = False
                    else:
                        value["quantity"] = None
                        value["is_blank"] = True
        repeat_notice["matched_target_count"] = len(completed_matches)
        repeat_notice["state"] = ("Complete" if len(completed_matches) == len(semantic_target_expected)
                                   and bool(semantic_target_expected) else "Partial")
        for row in output_rows:
            target_ids = set(row["values"])
            if target_ids and all(value.get("action") == "NoOp" for value in row["values"].values()):
                assignment_errors = [item for item in row["issues"]
                                     if item["code"] == "IMPORT_ASSIGNMENT_UNRESOLVED"]
                for item in assignment_errors:
                    row["issues"].remove(item)
                    if item in issues:
                        issues.remove(item)

    candidates = sum(value.get("is_candidate", False)
                     for row in output_rows for value in row["values"].values())


    effective_rules = {}
    for rules_by_unit in semantic_target_rules.values():
        for rule in rules_by_unit.values():
            meaning = rule.get("meaning")
            if meaning is not None:
                effective_rules[encoded(meaning)] = meaning
    mapping_fp = fingerprint("mapping", {"period": period_meaning(period),
                                          "rules": [effective_rules[key] for key in sorted(effective_rules)]})
    target_content = []
    for target, tokens in target_content_tokens.items():
        if not tokens:
            continue
        meaning = semantic_target_accumulators[target].target_meaning
        target_content.append({"target": meaning, "rows": sorted(tokens), "record_count": len(tokens)})
    content_fp = fingerprint("content", {"mapping": mapping_fp,
                                          "targets": sorted(target_content, key=encoded)})
    assignment_meanings = {item["driverassignmentid"]: assignment_meaning(
        item, context["drivers_by_id"][item["driverid"]], period["branch_code"])
        for item in context["roster"] if item["driverid"] in context["drivers_by_id"]}
    row_plans = []
    for row in output_rows:
        driver_identity = row["context"].get("driver_identity", {})
        canonical_driver = driver_identity.get("canonical_driver") or {}
        row_plans.append({"row": row["row_fingerprint"],
                          "source": {"file_ordinal": row["file_ordinal"], "unit_name": row["unit_name"],
                                     "source_row": row["source_row"]},
                          "identifiers": [{"kind": item["kind"], "value": item["value"],
                                           "resolution": item["resolution"],
                                           "driver": driver_meaning(context["drivers_by_id"][item["driver_id"]])
                                           if item["driver_id"] in context["drivers_by_id"] else None}
                                          for item in row["identifiers"]],
                          "driver_identity": {
                              "status": driver_identity.get("status"),
                              "requires_confirmation": driver_identity.get("requires_confirmation", False),
                              "canonical_name": normalized_identifier(
                                  "DriverName", canonical_driver.get("driver_name")) or None,
                          },
                          "assignment": assignment_meanings.get(row["driver_assignment_id"]),
                          "date": row["work_date"].isoformat() if row["work_date"] else None,
                          "values": {context["definition_by_id"][int(key)].code: {
                              "quantity": value["quantity"], "blank": value["is_blank"],
                              "outcome": value["outcome"], "action": value.get("action"),
                              "repeat_input_sha256": value.get("repeat_input_sha256"),
                              "current": value["current_quantity"],
                              "current_source_type": (value.get("current_provenance") or {}).get("source_type")}
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
                     prepared_excluded_rows=sum("proposed_exclusion" in row["context"] for row in output_rows),
                     errors=0, warnings=0, conflicts=0, new=0, unchanged=0, already_imported=0, blank=0, invalid=0,
                     unresolved=0, conflict_cells=0, error_rows=0, warning_rows=0, conflict_rows=0)
    for item in issues:
        counts[{"Error": "errors", "Warning": "warnings", "Conflict": "conflicts"}[item["severity"]]] += 1
    for row in output_rows:
        severities = {item["severity"] for item in row["issues"]}
        counts["error_rows"] += "Error" in severities
        counts["warning_rows"] += "Warning" in severities
        counts["conflict_rows"] += "Conflict" in severities
        for value in row["values"].values():
            if not value.get("is_candidate", True):
                continue
            outcome = value["outcome"].lower()
            count_key = ("conflict_cells" if value["outcome"] == "Conflict" else
                         "already_imported" if value["outcome"] == "AlreadyImported" else outcome)
            counts[count_key] += 1
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
    covered_days = {row["work_date"].isoformat() for row in output_rows if row["work_date"] is not None}
    unit_days = defaultdict(set)
    unit_unresolved = Counter()
    day_units = defaultdict(set)
    for row in output_rows:
        unit = (row["file_ordinal"], row["unit_name"])
        if row["work_date"] is not None:
            value = row["work_date"].isoformat()
            unit_days[unit].add(value)
            day_units[value].add(unit)
        elif not row["skipped"]:
            unit_unresolved[unit] += 1
    for source in sources:
        unit = (source["file_ordinal"], source["unit_name"])
        for value in source.get("date_resolved_values", []):
            unit_days[unit].add(value)
            day_units[value].add(unit)
    recognized_days = sorted(covered_days | {day for values in unit_days.values() for day in values})
    sample_limit = (policy or DEFAULT_IMPORT_RESOURCE_POLICY).date_review_day_sample
    recognized_day_set = set(recognized_days)
    missing_days = sorted(day.isoformat() for day in context["days"]
                          if day.isoformat() not in recognized_day_set)
    overlap_days = sorted(day for day, owners in day_units.items() if len(owners) > 1)
    date_review_summary = {
        "recognized_days": recognized_days[:sample_limit],
        "recognized_day_count": len(recognized_days),
        "recognized_days_truncated": len(recognized_days) > sample_limit,
        "missing_coverage_days": missing_days[:sample_limit],
        "missing_coverage_count": len(missing_days),
        "missing_coverage_days_truncated": len(missing_days) > sample_limit,
        "unresolved_row_count": sum(unit_unresolved.values()),
        "overlap_unit_day_count": len(overlap_days),
        "overlap_unit_days": overlap_days[:sample_limit],
        "overlap_unit_days_truncated": len(overlap_days) > sample_limit,
        "units": [{"file_ordinal": ordinal, "unit_name": name,
                   "recognized_day_count": len(unit_days[(ordinal, name)]),
                   "unresolved_row_count": unit_unresolved[(ordinal, name)]}
                  for ordinal, name in sorted(set(unit_days) | set(unit_unresolved))],
    }
    date_rule_profiles = {}
    for source in sources:
        for rule in source.get("quantity_rules", []):
            if any(column is not None for column in rule.get("columns", [])):
                date_rule_profiles.setdefault(rule["target"], []).extend(source["date_profile"])
    return {"rows": output_rows, "issues": issues, "groups": list(groups.values()),
            "mapping_fingerprint": mapping_fp, "content_fingerprint": content_fp,
            "plan_fingerprint": plan_fp, "counts": dict(counts),
            "interpretations": [{"file_ordinal": s["file_ordinal"], "unit_name": s["unit_name"],
                                 "date_order": s.get("accepted_date_order"),
                                 "date_order_state": s.get("date_order_state"),
                                 "date_source": s.get("date_source")}
                                for s in sources],
            "candidates": candidates, "date_review": date_review_summary,
            "date_review_full": {"recognized_days": recognized_days,
                                 "missing_coverage_days": missing_days,
                                 "overlap_unit_days": overlap_days},
            "date_rule_profiles": date_rule_profiles,
            "semantic_receipts": semantic_receipts, "repeat_notice": repeat_notice}
