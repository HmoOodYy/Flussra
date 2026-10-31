"""Pure validation helpers for target-oriented Import rule requests."""
from __future__ import annotations

from app.imports.errors import ImportFailure
from app.imports.headers import describe_headers, normalize_header


def selector_identity(selector) -> tuple[str, tuple[tuple[str | None, int | None], ...]]:
    """Canonical identity for a named input and its accepted duplicate occurrences."""
    occurrences = selector.get("occurrences") or []
    return normalize_header(selector["header"]), tuple(sorted(
        ((item["duplicate_signature"], item["occurrence_ordinal"]) for item in occurrences),
        key=lambda item: (item[0] or "", item[1] or 0)))


def selectors_overlap(left: dict, right: dict) -> bool:
    """Whether two selectors can refer to the same named logical occurrence."""
    if normalize_header(left["header"]) != normalize_header(right["header"]):
        return False
    def accepted_atoms(selector):
        occurrences = selector.get("occurrences") or []
        if not occurrences:
            return {(None, 1)}
        return {(item["duplicate_signature"], item["occurrence_ordinal"])
                for item in occurrences if item["occurrence_ordinal"] is not None}

    left_items = accepted_atoms(left)
    right_items = accepted_atoms(right)
    return bool(left_items & right_items)


def resolve_selector(selector: dict, layouts: dict[int, list[str]], *, required: bool = True,
                     allow_unobserved_arrangements: bool = False) -> dict[int, int | None]:
    """Resolve a selector by normalized label and explicit duplicate signatures."""
    label = normalize_header(selector["header"])
    if not label:
        raise ImportFailure("IMPORT_SELECTOR_INVALID", "A source header cannot be blank.", 422)
    alternatives = selector.get("occurrences") or []
    by_signature: dict[str | None, int | None] = {}
    for item in alternatives:
        signature = item["duplicate_signature"]
        ordinal = item["occurrence_ordinal"]
        if signature in by_signature:
            raise ImportFailure("IMPORT_DUPLICATE_SELECTOR_AMBIGUOUS",
                                "Choose one occurrence for each duplicate arrangement.", 422)
        by_signature[signature] = ordinal

    result: dict[int, int | None] = {}
    found_any = False
    found_value = False
    observed_signatures: set[str] = set()
    for layout_id, headers in layouts.items():
        detail = describe_headers(headers)
        physical = [item for item in detail["header_occurrences"] if item["normalized_label"] == label]
        if not physical:
            result[layout_id] = None
            continue
        found_any = True
        if len(physical) == 1:
            observed_signatures.add(None)
            if alternatives:
                if None not in by_signature or by_signature[None] not in (None, 1):
                    raise ImportFailure("IMPORT_DUPLICATE_SELECTOR_STALE",
                                        "This unique header arrangement needs an explicit accepted choice.", 422)
                if by_signature[None] is None:
                    result[layout_id] = None
                    continue
            result[layout_id] = physical[0]["source_column"]
            found_value = True
            continue
        signature = physical[0]["duplicate_signature"]
        observed_signatures.add(signature)
        if signature not in by_signature:
            raise ImportFailure("IMPORT_DUPLICATE_RESOLUTION_REQUIRED",
                                "Resolve every duplicate header arrangement before saving mappings.", 422)
        ordinal = by_signature[signature]
        if ordinal is None:
            result[layout_id] = None
            continue
        selected = next((item for item in physical if item["occurrence_ordinal"] == ordinal), None)
        if selected is None:
            raise ImportFailure("IMPORT_DUPLICATE_SELECTOR_INVALID",
                                "Selected duplicate occurrence is outside the current arrangement.", 422)
        result[layout_id] = selected["source_column"]
        found_value = True
    unknown = set(by_signature) - observed_signatures
    if unknown and found_any and not allow_unobserved_arrangements:
        raise ImportFailure("IMPORT_DUPLICATE_SELECTOR_STALE",
                            "A duplicate choice does not match any selected source arrangement.", 422)
    if required and not found_value:
        raise ImportFailure("IMPORT_SELECTOR_MISSING", "The selected header is not present in this source setup.", 422)
    return result


def ensure_duplicate_outcomes(layouts: dict[int, list[str]], selectors: list[dict], *,
                              allow_unobserved_arrangements: bool = False) -> None:
    """Require each physical duplicate occurrence to be selected or explicitly ignored."""
    selected: set[tuple[int, str, int]] = set()
    for selector in selectors:
        resolutions = resolve_selector(selector, layouts, required=False,
                                       allow_unobserved_arrangements=allow_unobserved_arrangements)
        for layout_id, column in resolutions.items():
            if column is None:
                continue
            detail = describe_headers(layouts[layout_id])
            item = next(value for value in detail["header_occurrences"] if value["source_column"] == column)
            if item["duplicate_signature"]:
                selected.add((layout_id, item["duplicate_signature"], item["occurrence_ordinal"]))
    for layout_id, headers in layouts.items():
        detail = describe_headers(headers)
        for group in detail["duplicate_header_signatures"]:
            for ordinal in range(1, group["count"] + 1):
                if (layout_id, group["signature"], ordinal) not in selected:
                    raise ImportFailure("IMPORT_DUPLICATE_RESOLUTION_REQUIRED",
                                        "Map, share or explicitly ignore every duplicate column occurrence.", 422)

