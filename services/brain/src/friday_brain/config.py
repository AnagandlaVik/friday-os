from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings.
    Defaults are suitable for local development.
    """

    app_name: str = "FRIDAY Brain"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000

    # CORS settings
    # For Milestone 1, we are being restrictive.
    cors_origins: list[str] = []

    # Plan validation settings
    max_plan_steps: int = 10

    # Security settings
    # For Milestone 1, only the harmless 'echo' operation is allowed.
    allowed_operations: set[str] = {"echo"}

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False
    )


settings = Settings()
