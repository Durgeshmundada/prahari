"""Runtime settings with explicit safety defaults for local and production modes."""

from __future__ import annotations

import json
import re
from functools import cached_property
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from prahari.config import project_root
from prahari.identity import BrowserUser


def _default_database_url() -> str:
    database_path = project_root() / "artifacts" / "prahari.db"
    return f"sqlite:///{database_path.as_posix()}"


class ApiKeyPrincipal(BaseModel):
    """A local service/API key identity loaded from a protected environment variable."""

    model_config = ConfigDict(extra="forbid")

    actor_id: str = Field(min_length=1, max_length=100)
    role: Literal["viewer", "operator", "reviewer", "admin", "collector"]


class Settings(BaseSettings):
    """Application configuration.

    Production startup refuses fixture mode and unauthenticated APIs. This makes
    accidental deployment of the replay demo materially harder.
    """

    model_config = SettingsConfigDict(
        env_prefix="PRAHARI_",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    data_mode: Literal["replay", "live"] = "replay"
    database_url: str = Field(default_factory=_default_database_url)
    database_url_file: Path | None = None
    auth_mode: Literal["disabled", "api_key"] = "disabled"
    api_keys_json: str = "{}"
    api_keys_json_file: Path | None = None
    browser_auth_enabled: bool = False
    browser_users_file: Path | None = None
    browser_origin: str = "http://127.0.0.1:5173"
    browser_session_seconds: int = Field(default=28_800, ge=300, le=28_800)
    browser_idle_seconds: int = Field(default=1800, ge=60, le=3600)
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    action_adapter_enabled: bool = False
    action_agent_url: str = "http://172.31.255.1:8091"
    action_agent_allowed_private_ip: str = "172.31.255.1"
    action_agent_token_file: Path | None = None
    model_approval_status: Literal["bootstrap", "approved"] = "bootstrap"
    model_artifact_path: Path | None = None
    model_manifest_path: Path | None = None
    predictive_model_manifest_path: Path | None = None
    network_profile_path: Path | None = None
    knowledge_root: Path | None = None
    copilot_enabled: bool = False
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_allowed_private_ip: str | None = None
    ollama_model: str = "qwen3:0.6b"
    ollama_timeout_seconds: float = Field(default=45.0, ge=5.0, le=180.0)
    auto_create_schema: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    http_max_body_bytes: int = Field(default=2 * 1024 * 1024, ge=65536, le=4 * 1024 * 1024)
    http_body_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    traffic_demo_run_enabled: bool = False
    traffic_demo_receiver_url: str = "ws://127.0.0.1:8765/ws/traffic"
    traffic_demo_token: SecretStr | None = None
    traffic_demo_receiver_artifact_token: SecretStr | None = None
    traffic_demo_allow_non_loopback_receiver: bool = False
    traffic_demo_allow_insecure_remote_ws: bool = False
    traffic_demo_max_file_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    traffic_demo_run_timeout_seconds: float = Field(default=60.0, ge=1.0, le=120.0)
    traffic_demo_artifact_fetch_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    traffic_demo_download_registry_max_items: int = Field(default=100, ge=1, le=1_000)

    @field_validator("log_level", mode="before")
    @classmethod
    def normalise_log_level(cls, value: object) -> object:
        """Accept conventional lowercase service-log values from Compose/CLI."""

        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_safe_production_configuration(self) -> Settings:
        if self.browser_auth_enabled:
            if self.auth_mode != "api_key":
                raise ValueError("browser authentication requires PRAHARI_AUTH_MODE=api_key")
            origin = urlsplit(self.browser_origin)
            if (
                not origin.hostname
                or origin.username
                or origin.password
                or origin.path
                or origin.query
                or origin.fragment
                or origin.scheme not in {"http", "https"}
            ):
                raise ValueError("PRAHARI_BROWSER_ORIGIN must be an exact HTTP(S) origin")
            if origin.scheme != "https" and (
                self.environment == "production"
                or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
            ):
                raise ValueError(
                    "browser authentication requires HTTPS except on development loopback"
                )
            if not self.browser_users:
                raise ValueError("browser authentication requires a non-empty protected users file")
            if self.environment == "production":
                active = [user for user in self.browser_users.values() if not user.disabled]
                if (
                    len(active) < 2
                    or not any(user.role in {"operator", "admin"} for user in active)
                    or not any(user.role in {"reviewer", "admin"} for user in active)
                ):
                    raise ValueError(
                        "production needs distinct active operator and reviewer accounts"
                    )
        if self.database_url_file is not None:
            self.database_url = _read_secret_file(
                self.database_url_file,
                "PRAHARI_DATABASE_URL_FILE",
            )
        if self.api_keys_json_file is not None:
            self.api_keys_json = _read_secret_file(
                self.api_keys_json_file,
                "PRAHARI_API_KEYS_JSON_FILE",
            )
        if self.environment == "production" and self.data_mode != "live":
            raise ValueError("production requires PRAHARI_DATA_MODE=live")
        if self.environment == "production" and not self.database_url.startswith(
            "postgresql+psycopg://"
        ):
            raise ValueError("production requires a PostgreSQL PRAHARI_DATABASE_URL")
        if self.environment == "production" and self.database_url_file is None:
            raise ValueError("production requires PRAHARI_DATABASE_URL_FILE from a mounted secret")
        if self.environment == "production" and self.auth_mode != "api_key":
            raise ValueError("production requires PRAHARI_AUTH_MODE=api_key")
        if self.environment == "production" and self.api_keys_json_file is None:
            raise ValueError("production requires PRAHARI_API_KEYS_JSON_FILE from a mounted secret")
        if self.environment == "production" and not self.api_key_principals:
            raise ValueError("production requires non-empty PRAHARI_API_KEYS_JSON")
        if self.environment == "production" and self.model_approval_status != "approved":
            raise ValueError("production requires an approved registered model")
        if (self.model_artifact_path is None) != (self.model_manifest_path is None):
            raise ValueError("model artifact and manifest paths must be configured together")
        if self.environment == "production" and self.model_artifact_path is None:
            raise ValueError(
                "production requires PRAHARI_MODEL_ARTIFACT_PATH and PRAHARI_MODEL_MANIFEST_PATH"
            )
        if self.environment == "production" and not self.copilot_enabled:
            raise ValueError("production requires the local schema-constrained copilot")
        if self.environment == "production" and any(
            len(token) < 32 or token.strip() != token for token in self.api_key_principals
        ):
            raise ValueError("production API tokens must be at least 32 characters")
        if self.environment == "production" and "*" in self.cors_origin_list:
            raise ValueError("production CORS origins must be explicit")
        if self.action_adapter_enabled and self.action_agent_token_file is None:
            raise ValueError("enabled action adapter requires PRAHARI_ACTION_AGENT_TOKEN_FILE")
        if self.environment == "production" and self.action_adapter_enabled:
            raise ValueError("the controlled-lab action adapter is not permitted in production")
        if self.environment == "production" and self.auto_create_schema:
            raise ValueError(
                "production must use reviewed database migrations, not auto-create schema"
            )
        if self.traffic_demo_run_enabled and self.environment != "development":
            raise ValueError("PRAHARI_TRAFFIC_DEMO_RUN_ENABLED is restricted to development")
        if (
            self.traffic_demo_token is not None
            and self.traffic_demo_receiver_artifact_token is not None
        ):
            websocket_token = self.traffic_demo_token.get_secret_value()
            artifact_token = self.traffic_demo_receiver_artifact_token.get_secret_value()
            if websocket_token and artifact_token and websocket_token == artifact_token:
                raise ValueError(
                    "PRAHARI_TRAFFIC_DEMO_RECEIVER_ARTIFACT_TOKEN must differ from the "
                    "WebSocket token"
                )
        return self

    @cached_property
    def api_key_principals(self) -> dict[str, ApiKeyPrincipal]:
        try:
            parsed = json.loads(self.api_keys_json)
            if not isinstance(parsed, dict):
                raise ValueError("value must be an object")
            return {
                token: ApiKeyPrincipal.model_validate(principal)
                for token, principal in parsed.items()
            }
        except (TypeError, ValueError, ValidationError) as error:
            raise ValueError(
                "PRAHARI_API_KEYS_JSON must map tokens to actor_id and role"
            ) from error

    @property
    def browser_users(self) -> dict[str, BrowserUser]:
        if self.browser_users_file is None:
            return {}
        raw = json.loads(_read_secret_file(self.browser_users_file, "PRAHARI_BROWSER_USERS_FILE"))
        if not isinstance(raw, dict) or not 1 <= len(raw) <= 1000:
            raise ValueError("browser users file must map 1-1000 usernames to identities")
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9_.@-]{0,99}", name) for name in raw):
            raise ValueError("browser usernames must be lowercase identifiers of 1-100 characters")
        users = {name: BrowserUser.model_validate(value) for name, value in raw.items()}
        if len({user.actor_id for user in users.values()}) != len(users):
            raise ValueError("each browser account must have a distinct actor_id")
        return users

    @property
    def browser_cookie_name(self) -> str:
        return "__Host-prahari_session" if self.browser_cookie_secure else "prahari_session"

    @property
    def browser_cookie_secure(self) -> bool:
        return urlsplit(self.browser_origin).scheme == "https"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def database_path(self) -> Path | None:
        """Return a filesystem path only for SQLite databases."""

        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        return Path(self.database_url.removeprefix(prefix))


def _read_secret_file(path: Path, setting_name: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"cannot read {setting_name}: {path}") from error
    if not value:
        raise ValueError(f"{setting_name} must not be empty")
    return value
