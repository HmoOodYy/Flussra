from decimal import Decimal
from functools import lru_cache
from math import isfinite

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY, ImportResourcePolicy
from app.product_policy import DEFAULT_PRODUCT_POLICY, ProductPolicy


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables / .env file.

    Run the app from the backend/ directory so that pydantic-settings
    finds .env in the current working directory:

        cd Payroll_App_v3/backend
        uvicorn app.main:create_app --factory --reload
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    # PostgreSQL — format: postgresql+asyncpg://user:pass@host:port/db
    DATABASE_URL: str

    # JWT signing key — generate with: python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY: str

    # Token lifetime (hours)
    ACCESS_TOKEN_EXPIRE_HOURS: int = 8

    # "development" enables SQLAlchemy SQL echo logging
    ENVIRONMENT: str = "development"

    # Import capacities are operational policy rather than database limits.
    IMPORT_MAX_FILE_BYTES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_file_bytes
    IMPORT_MAX_TOTAL_FILE_BYTES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_total_file_bytes
    IMPORT_MAX_MULTIPART_OVERHEAD_BYTES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_multipart_overhead_bytes
    IMPORT_MAX_FILES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_files
    IMPORT_MAX_FILE_DECOMPRESSED_BYTES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_file_decompressed_bytes
    IMPORT_MAX_TOTAL_DECOMPRESSED_BYTES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_total_decompressed_bytes
    IMPORT_MAX_WORKSHEETS_PER_FILE: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_worksheets_per_file
    IMPORT_MAX_SOURCE_UNITS: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_source_units
    IMPORT_MAX_LAYOUTS: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_layouts
    IMPORT_MAX_ROWS_PER_UNIT: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_rows_per_unit
    IMPORT_MAX_COLUMNS_PER_UNIT: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_columns_per_unit
    IMPORT_MAX_CELLS_PER_UNIT: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_cells_per_unit
    IMPORT_MAX_SELECTED_CELLS: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_selected_cells
    IMPORT_MAX_DATA_ROWS: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_data_rows
    IMPORT_MAX_CANDIDATES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_candidates
    IMPORT_MAX_CELL_TEXT_BYTES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_cell_text_bytes
    IMPORT_MAX_INSPECTION_BYTES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_inspection_bytes
    IMPORT_MAX_XLSX_MEMBERS: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_xlsx_members
    IMPORT_MAX_XLSX_XML_DEPTH: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_xlsx_xml_depth
    IMPORT_MAX_CONCURRENT_REQUESTS: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_concurrent_requests
    IMPORT_MAX_NUMERIC_WORK_DIGITS: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_numeric_work_digits
    IMPORT_MAX_MAPPING_REQUEST_BYTES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_mapping_request_bytes
    IMPORT_MAX_IDENTITY_LOOKUP_ROWS: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_identity_lookup_rows
    IMPORT_IDENTITY_LOOKUP_BATCH_ROWS: int = DEFAULT_IMPORT_RESOURCE_POLICY.identity_lookup_batch_rows

    IMPORT_MAX_RESPONSE_BYTES: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_response_bytes
    IMPORT_ROW_PAGE_DEFAULT: int = DEFAULT_IMPORT_RESOURCE_POLICY.row_page_default
    IMPORT_ROW_PAGE_MAX: int = DEFAULT_IMPORT_RESOURCE_POLICY.row_page_max
    IMPORT_ISSUE_PAGE_DEFAULT: int = DEFAULT_IMPORT_RESOURCE_POLICY.issue_page_default
    IMPORT_ISSUE_PAGE_MAX: int = DEFAULT_IMPORT_RESOURCE_POLICY.issue_page_max
    IMPORT_SOURCE_PAGE_DEFAULT: int = DEFAULT_IMPORT_RESOURCE_POLICY.source_page_default
    IMPORT_SOURCE_PAGE_MAX: int = DEFAULT_IMPORT_RESOURCE_POLICY.source_page_max
    IMPORT_SOURCE_CELL_PAGE_DEFAULT: int = DEFAULT_IMPORT_RESOURCE_POLICY.source_cell_page_default
    IMPORT_SOURCE_CELL_PAGE_MAX: int = DEFAULT_IMPORT_RESOURCE_POLICY.source_cell_page_max
    IMPORT_TARGET_PAGE_DEFAULT: int = DEFAULT_IMPORT_RESOURCE_POLICY.target_page_default
    IMPORT_TARGET_PAGE_MAX: int = DEFAULT_IMPORT_RESOURCE_POLICY.target_page_max
    IMPORT_HEADER_PAGE_DEFAULT: int = DEFAULT_IMPORT_RESOURCE_POLICY.header_page_default
    IMPORT_HEADER_PAGE_MAX: int = DEFAULT_IMPORT_RESOURCE_POLICY.header_page_max
    IMPORT_ROW_DETAIL_SAMPLE: int = DEFAULT_IMPORT_RESOURCE_POLICY.row_detail_sample
    IMPORT_LAYOUT_HEADER_SAMPLE: int = DEFAULT_IMPORT_RESOURCE_POLICY.layout_header_sample
    IMPORT_MAX_HEADER_LABEL_CHARS: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_header_label_chars

    PRODUCT_MAX_COMPANY_PAY_ITEMS: int = DEFAULT_PRODUCT_POLICY.max_company_pay_items
    PRODUCT_MAX_BRANCH_INPUT_CONCEPTS: int = DEFAULT_PRODUCT_POLICY.max_branch_input_concepts
    IMPORT_MAX_COMBINED_COLUMNS: int = DEFAULT_PRODUCT_POLICY.max_import_combined_columns
    IMPORT_MAX_CANONICAL_FILE_BYTES: int = DEFAULT_PRODUCT_POLICY.max_import_canonical_file_bytes

    @field_validator(
        "IMPORT_MAX_FILE_BYTES", "IMPORT_MAX_TOTAL_FILE_BYTES", "IMPORT_MAX_MULTIPART_OVERHEAD_BYTES",
        "IMPORT_MAX_FILES", "IMPORT_MAX_FILE_DECOMPRESSED_BYTES", "IMPORT_MAX_TOTAL_DECOMPRESSED_BYTES",
        "IMPORT_MAX_WORKSHEETS_PER_FILE", "IMPORT_MAX_SOURCE_UNITS", "IMPORT_MAX_LAYOUTS",
        "IMPORT_MAX_ROWS_PER_UNIT", "IMPORT_MAX_COLUMNS_PER_UNIT", "IMPORT_MAX_CELLS_PER_UNIT",
        "IMPORT_MAX_SELECTED_CELLS", "IMPORT_MAX_DATA_ROWS", "IMPORT_MAX_CANDIDATES",
        "IMPORT_MAX_CELL_TEXT_BYTES", "IMPORT_MAX_INSPECTION_BYTES", "IMPORT_MAX_XLSX_MEMBERS",
        "IMPORT_MAX_XLSX_XML_DEPTH", "IMPORT_MAX_CONCURRENT_REQUESTS", "IMPORT_MAX_NUMERIC_WORK_DIGITS",
        "IMPORT_MAX_MAPPING_REQUEST_BYTES", "IMPORT_MAX_IDENTITY_LOOKUP_ROWS",
        "IMPORT_IDENTITY_LOOKUP_BATCH_ROWS", "IMPORT_MAX_RESPONSE_BYTES",
        "IMPORT_ROW_PAGE_DEFAULT", "IMPORT_ROW_PAGE_MAX", "IMPORT_ISSUE_PAGE_DEFAULT", "IMPORT_ISSUE_PAGE_MAX",
        "IMPORT_SOURCE_PAGE_DEFAULT", "IMPORT_SOURCE_PAGE_MAX", "IMPORT_SOURCE_CELL_PAGE_DEFAULT",
        "IMPORT_SOURCE_CELL_PAGE_MAX", "IMPORT_TARGET_PAGE_DEFAULT", "IMPORT_TARGET_PAGE_MAX",
        "IMPORT_HEADER_PAGE_DEFAULT", "IMPORT_HEADER_PAGE_MAX", "IMPORT_ROW_DETAIL_SAMPLE",
        "IMPORT_LAYOUT_HEADER_SAMPLE", "IMPORT_MAX_HEADER_LABEL_CHARS", "PRODUCT_MAX_COMPANY_PAY_ITEMS",
        "PRODUCT_MAX_BRANCH_INPUT_CONCEPTS", "IMPORT_MAX_COMBINED_COLUMNS", "IMPORT_MAX_CANONICAL_FILE_BYTES",
        mode="before",
    )
    @classmethod
    def reject_bool_or_fractional_numeric_settings(cls, value):
        fractional = isinstance(value, float) and (not isfinite(value) or not value.is_integer())
        fractional = fractional or (
            isinstance(value, Decimal)
            and (not value.is_finite() or value != value.to_integral_value())
        )
        if isinstance(value, bool) or fractional:
            raise ValueError("must be a positive integer")
        return value

    @model_validator(mode="after")
    def validate_import_policies(self):
        self.import_resource_policy
        self.product_policy
        return self

    @property
    def import_resource_policy(self) -> ImportResourcePolicy:
        return ImportResourcePolicy(
            max_file_bytes=self.IMPORT_MAX_FILE_BYTES,
            max_total_file_bytes=self.IMPORT_MAX_TOTAL_FILE_BYTES,
            max_multipart_overhead_bytes=self.IMPORT_MAX_MULTIPART_OVERHEAD_BYTES,
            max_files=self.IMPORT_MAX_FILES,
            max_file_decompressed_bytes=self.IMPORT_MAX_FILE_DECOMPRESSED_BYTES,
            max_total_decompressed_bytes=self.IMPORT_MAX_TOTAL_DECOMPRESSED_BYTES,
            max_worksheets_per_file=self.IMPORT_MAX_WORKSHEETS_PER_FILE,
            max_source_units=self.IMPORT_MAX_SOURCE_UNITS,
            max_layouts=self.IMPORT_MAX_LAYOUTS,
            max_rows_per_unit=self.IMPORT_MAX_ROWS_PER_UNIT,
            max_columns_per_unit=self.IMPORT_MAX_COLUMNS_PER_UNIT,
            max_cells_per_unit=self.IMPORT_MAX_CELLS_PER_UNIT,
            max_selected_cells=self.IMPORT_MAX_SELECTED_CELLS,
            max_data_rows=self.IMPORT_MAX_DATA_ROWS,
            max_candidates=self.IMPORT_MAX_CANDIDATES,
            max_cell_text_bytes=self.IMPORT_MAX_CELL_TEXT_BYTES,
            max_inspection_bytes=self.IMPORT_MAX_INSPECTION_BYTES,
            max_xlsx_members=self.IMPORT_MAX_XLSX_MEMBERS,
            max_xlsx_xml_depth=self.IMPORT_MAX_XLSX_XML_DEPTH,
            max_concurrent_requests=self.IMPORT_MAX_CONCURRENT_REQUESTS,
            max_numeric_work_digits=self.IMPORT_MAX_NUMERIC_WORK_DIGITS,
            max_mapping_request_bytes=self.IMPORT_MAX_MAPPING_REQUEST_BYTES,
            max_identity_lookup_rows=self.IMPORT_MAX_IDENTITY_LOOKUP_ROWS,
            identity_lookup_batch_rows=self.IMPORT_IDENTITY_LOOKUP_BATCH_ROWS,
            max_response_bytes=self.IMPORT_MAX_RESPONSE_BYTES,
            row_page_default=self.IMPORT_ROW_PAGE_DEFAULT,
            row_page_max=self.IMPORT_ROW_PAGE_MAX,
            issue_page_default=self.IMPORT_ISSUE_PAGE_DEFAULT,
            issue_page_max=self.IMPORT_ISSUE_PAGE_MAX,
            source_page_default=self.IMPORT_SOURCE_PAGE_DEFAULT,
            source_page_max=self.IMPORT_SOURCE_PAGE_MAX,
            source_cell_page_default=self.IMPORT_SOURCE_CELL_PAGE_DEFAULT,
            source_cell_page_max=self.IMPORT_SOURCE_CELL_PAGE_MAX,
            target_page_default=self.IMPORT_TARGET_PAGE_DEFAULT,
            target_page_max=self.IMPORT_TARGET_PAGE_MAX,
            header_page_default=self.IMPORT_HEADER_PAGE_DEFAULT,
            header_page_max=self.IMPORT_HEADER_PAGE_MAX,
            row_detail_sample=self.IMPORT_ROW_DETAIL_SAMPLE,
            layout_header_sample=self.IMPORT_LAYOUT_HEADER_SAMPLE,
            max_header_label_chars=self.IMPORT_MAX_HEADER_LABEL_CHARS,
        )

    @property
    def product_policy(self) -> ProductPolicy:
        return ProductPolicy(
            max_company_pay_items=self.PRODUCT_MAX_COMPANY_PAY_ITEMS,
            max_branch_input_concepts=self.PRODUCT_MAX_BRANCH_INPUT_CONCEPTS,
            max_import_combined_columns=self.IMPORT_MAX_COMBINED_COLUMNS,
            max_import_canonical_file_bytes=self.IMPORT_MAX_CANONICAL_FILE_BYTES,
        )

    @property
    def is_dev(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process configuration, validating required values on first use."""
    return Settings()
