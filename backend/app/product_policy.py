"""Shared Company/Branch product allowances and Import-specific product limits."""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class ProductPolicy:
    max_company_pay_items: int = 50
    max_branch_input_concepts: int = 15
    max_import_combined_columns: int = 40
    max_import_canonical_file_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer")


DEFAULT_PRODUCT_POLICY = ProductPolicy()
