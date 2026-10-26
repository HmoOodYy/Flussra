from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.imports.policy import ImportResourcePolicy


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
    IMPORT_MAX_FILE_BYTES: int = 10_000_000
    IMPORT_MAX_TOTAL_FILE_BYTES: int = 50_000_000
    IMPORT_MAX_MULTIPART_OVERHEAD_BYTES: int = 256_000
    IMPORT_MAX_FILES: int = 64
    IMPORT_MAX_FILE_DECOMPRESSED_BYTES: int = 50_000_000
    IMPORT_MAX_TOTAL_DECOMPRESSED_BYTES: int = 200_000_000
    IMPORT_MAX_WORKSHEETS_PER_FILE: int = 128
    IMPORT_MAX_SOURCE_UNITS: int = 128
    IMPORT_MAX_LAYOUTS: int = 64
    IMPORT_MAX_ROWS_PER_UNIT: int = 20_000
    IMPORT_MAX_COLUMNS_PER_UNIT: int = 128
    IMPORT_MAX_CELLS_PER_UNIT: int = 1_000_000
    IMPORT_MAX_SELECTED_CELLS: int = 2_000_000
    IMPORT_MAX_DATA_ROWS: int = 50_000
    IMPORT_MAX_CANDIDATES: int = 250_000
    IMPORT_MAX_CELL_TEXT_BYTES: int = 8_000
    IMPORT_MAX_INSPECTION_BYTES: int = 4_000_000
    IMPORT_MAX_XLSX_MEMBERS: int = 4_096
    IMPORT_MAX_XLSX_XML_DEPTH: int = 256
    IMPORT_MAX_CONCURRENT_REQUESTS: int = 2

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
        )

    @property
    def is_dev(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process configuration, validating required values on first use."""
    return Settings()
