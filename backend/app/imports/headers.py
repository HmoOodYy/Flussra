"""Header-only source compatibility and duplicate occurrence accounting."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from app.imports.errors import ImportFailure


def normalize_header(label: str) -> str:
    """The supported external-header equivalence is trim plus case-fold only."""
    return label.strip().casefold()


def describe_headers(header_cells: list[str]) -> dict:
    """Return headed occurrences and signatures while preserving physical columns."""
    last = max((index for index, label in enumerate(header_cells, 1)
                if isinstance(label, str) and normalize_header(label)), default=0)
    cells = list(header_cells[:last])
    grouped: dict[str, list[int]] = defaultdict(list)
    labels: dict[str, str] = {}
    for column, label in enumerate(cells, 1):
        if isinstance(label, str) and (normalized := normalize_header(label)):
            grouped[normalized].append(column)
            labels.setdefault(normalized, label)

    occurrences = []
    duplicate_signatures = []
    for normalized, columns in sorted(grouped.items(), key=lambda item: min(item[1])):
        duplicate = len(columns) > 1
        signature = hashlib.sha256(json.dumps(
            {"label": normalized, "columns": columns}, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest() if duplicate else None
        if duplicate:
            duplicate_signatures.append({"label": labels[normalized], "normalized_label": normalized,
                                         "count": len(columns), "columns": columns,
                                         "signature": signature})
        for ordinal, column in enumerate(columns, 1):
            occurrences.append({
                "source_column": column,
                "label": cells[column - 1],
                "normalized_label": normalized,
                "occurrence_ordinal": ordinal,
                "occurrence_count": len(columns),
                "occurrence_id": (f"{signature}:{ordinal}" if duplicate else None),
                "duplicate_signature": signature,
            })
    occurrences.sort(key=lambda item: item["source_column"])
    return {"header_cells": cells, "headed_column_count": len(occurrences),
            "header_occurrences": occurrences, "duplicate_header_signatures": duplicate_signatures,
            "labels": {label: columns for label, columns in grouped.items()},
            "structure": [{"source_column": item["source_column"],
                           "normalized_label": item["normalized_label"],
                           "occurrence_ordinal": item["occurrence_ordinal"]}
                          for item in occurrences]}


def combined_column_count(header_sets: list[list[str]]) -> int:
    maxima: Counter[str] = Counter()
    for cells in header_sets:
        for label, columns in describe_headers(cells)["labels"].items():
            maxima[label] = max(maxima[label], len(columns))
    return sum(maxima.values())


def analyze_header_sets(header_sets: list[list[str]], maximum_columns: int | None = None) -> dict:
    """Check pairwise compatibility and count max multiplicity per normalized label."""
    described = [describe_headers(cells) for cells in header_sets]
    if not described or any(not item["labels"] for item in described):
        raise ImportFailure("IMPORT_HEADER_REQUIRED", "Every source file must contain at least one headed column.", 422)
    for left_index, left in enumerate(described):
        left_labels = set(left["labels"])
        for right_index in range(left_index + 1, len(described)):
            if left_labels.isdisjoint(described[right_index]["labels"]):
                raise ImportFailure(
                    "IMPORT_HEADER_STRUCTURE_INCOMPATIBLE",
                    f"Files {left_index + 1} and {right_index + 1} have no compatible column headers. "
                    "Upload unrelated source structures separately.", 422)
    maxima: Counter[str] = Counter()
    for item in described:
        for label, columns in item["labels"].items():
            maxima[label] = max(maxima[label], len(columns))
    combined_count = sum(maxima.values())
    if maximum_columns is not None and combined_count > maximum_columns:
        raise ImportFailure(
            "IMPORT_COMBINED_COLUMN_LIMIT_EXCEEDED",
            f"The compatible source structure has {combined_count} columns; the current limit is "
            f"{maximum_columns}. Remove or split columns into another source setup.", 422)
    return {"combined_column_count": combined_count,
            "file_header_details": described,
            "maximum_multiplicity_by_label": dict(maxima)}
