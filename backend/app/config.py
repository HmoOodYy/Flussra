from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def is_dev(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process configuration, validating required values on first use."""
    return Settings()
