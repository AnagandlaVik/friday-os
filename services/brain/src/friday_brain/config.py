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

    # Adapter Configuration
    brain_adapter_state_store: str = "in_memory"
    brain_adapter_event_bus: str = "in_memory"

    # Redis settings
    redis_url: str = "redis://localhost:6379/0"
    redis_connect_timeout_sec: float = 2.0
    redis_command_timeout_sec: float = 1.0

    # NATS JetStream settings
    nats_url: str = "nats://localhost:4222"
    nats_connect_timeout_sec: float = 2.0
    nats_publish_timeout_sec: float = 2.0
    nats_max_reconnect_attempts: int = 5
    nats_stream_name: str = "FRIDAY_EVENTS"
    nats_subject_prefix: str = "friday.events.brain"
    nats_consumer_name: str = "friday_brain_consumer_all"
    nats_max_deliver: int = 3
    nats_ack_wait_sec: int = 30
    nats_max_ack_pending: int = 2048
    nats_fetch_timeout_sec: float = 5.0  # For pull consumer
    nats_drain_timeout_sec: float = 5.0  # For graceful shutdown

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


settings = Settings()
