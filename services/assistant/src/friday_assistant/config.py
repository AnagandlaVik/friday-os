from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


class Settings(BaseSettings):
    brain_url: str = "http://localhost:8000"
    brain_request_timeout_sec: float = Field(
        default=10.0,
        gt=0,
    )
    brain_task_timeout_sec: float = Field(
        default=60.0,
        gt=0,
    )
    brain_poll_interval_sec: float = Field(
        default=0.25,
        gt=0,
    )
    host: str = "127.0.0.1"
    port: int = Field(
        default=8010,
        ge=1,
        le=65535,
    )
    environment: str = "development"

    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_",
        env_file=".env",
        extra="ignore",
    )
