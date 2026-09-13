from pathlib import Path
from typing import Self

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FORGE_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://forge:forge_local@127.0.0.1:55471/forge"
    api_token: SecretStr = SecretStr("")
    openai_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False,
        validation_alias=AliasChoices("OPENAI_API_KEY", "FORGE_OPENAI_API_KEY"))
    anthropic_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "FORGE_ANTHROPIC_API_KEY"))
    deepseek_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "FORGE_DEEPSEEK_API_KEY"))
    owner_id: str = "local"
    data_dir: Path = Path(".forge")
    allowed_repo_roots: list[Path] = Field(default_factory=list)
    config_dir: Path = Path("configs")
    docker_host: str | None = None
    allow_rootful_development: bool = False
    lease_seconds: int = Field(default=45, ge=6)
    worker_poll_seconds: float = Field(default=1, ge=0.05)
    event_poll_seconds: float = Field(default=0.25, ge=0.05)
    enable_evaluations: bool = False
    eval_private_dir: Path | None = None
    otlp_endpoint: str | None = None
    telemetry_max_queue_size: int = Field(default=256, ge=1, le=65536)
    telemetry_max_export_batch_size: int = Field(default=32, ge=1, le=4096)
    telemetry_schedule_delay_millis: float = Field(default=200, gt=0, le=60000, allow_inf_nan=False)
    telemetry_export_timeout_seconds: float = Field(default=2, gt=0, le=60, allow_inf_nan=False)
    telemetry_max_attributes: int = Field(default=12, ge=1, le=32)
    telemetry_max_attribute_length: int = Field(default=128, ge=1, le=1024)

    @model_validator(mode="after")
    def validate_telemetry_batch(self) -> Self:
        if self.telemetry_max_export_batch_size > self.telemetry_max_queue_size:
            raise ValueError("telemetry_max_export_batch_size must not exceed telemetry_max_queue_size")
        return self

    @property
    def checkpoint_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    def authorize_source(self, source: Path) -> Path:
        resolved = source.resolve(strict=True)
        if not resolved.is_dir() or not any(
            resolved.is_relative_to(root.resolve(strict=True)) for root in self.allowed_repo_roots
        ):
            raise ValueError("Repository must be inside a configured FORGE_ALLOWED_REPO_ROOTS directory")
        return resolved
