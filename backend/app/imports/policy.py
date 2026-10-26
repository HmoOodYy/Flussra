"""Configurable operational resource ceilings for one Import batch."""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class ImportResourcePolicy:
    max_file_bytes: int = 10_000_000
    max_total_file_bytes: int = 50_000_000
    max_multipart_overhead_bytes: int = 256_000
    max_files: int = 64
    max_file_decompressed_bytes: int = 50_000_000
    max_total_decompressed_bytes: int = 200_000_000
    max_worksheets_per_file: int = 128
    max_source_units: int = 128
    max_layouts: int = 64
    max_rows_per_unit: int = 20_000
    max_columns_per_unit: int = 128
    max_cells_per_unit: int = 1_000_000
    max_selected_cells: int = 2_000_000
    max_data_rows: int = 50_000
    max_candidates: int = 250_000
    max_cell_text_bytes: int = 8_000
    max_inspection_bytes: int = 4_000_000
    max_xlsx_members: int = 4_096
    max_xlsx_xml_depth: int = 256
    max_concurrent_requests: int = 2

    def __post_init__(self) -> None:
        for field in fields(self):
            name = field.name
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @property
    def max_request_body_bytes(self) -> int:
        return self.max_total_file_bytes + self.max_multipart_overhead_bytes


DEFAULT_IMPORT_RESOURCE_POLICY = ImportResourcePolicy()
