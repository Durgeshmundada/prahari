import json

import pytest
from pydantic import ValidationError

from prahari.settings import Settings


def test_settings_normalise_a_lowercase_log_level() -> None:
    assert Settings(log_level="info").log_level == "INFO"


def test_settings_load_database_url_from_a_secret_file(tmp_path) -> None:
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text("sqlite://", encoding="utf-8")

    assert Settings(database_url_file=database_url_file).database_url == "sqlite://"


def test_settings_load_api_key_principals_from_a_secret_file(tmp_path) -> None:
    api_keys_file = tmp_path / "api-keys.json"
    api_keys_file.write_text(
        json.dumps({"collector-token": {"actor_id": "collector-01", "role": "collector"}}),
        encoding="utf-8",
    )

    settings = Settings(auth_mode="api_key", api_keys_json_file=api_keys_file)

    assert settings.api_key_principals["collector-token"].actor_id == "collector-01"


def test_production_rejects_sqlite_even_after_other_gates_are_satisfied() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(
            environment="production",
            data_mode="live",
            database_url="sqlite://",
            auth_mode="api_key",
            api_keys_json=json.dumps(
                {"operator-token": {"actor_id": "operator-01", "role": "operator"}}
            ),
            model_approval_status="approved",
            auto_create_schema=False,
        )


def test_production_rejects_raw_api_key_json_without_a_mounted_secret(tmp_path) -> None:
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text(
        "postgresql+psycopg://prahari_app:password@database/prahari",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="PRAHARI_API_KEYS_JSON_FILE"):
        Settings(
            environment="production",
            data_mode="live",
            database_url_file=database_url_file,
            auth_mode="api_key",
            api_keys_json=json.dumps(
                {"operator-token": {"actor_id": "operator-01", "role": "operator"}}
            ),
            model_approval_status="approved",
            auto_create_schema=False,
        )


def test_production_rejects_a_raw_database_url_without_a_mounted_secret(tmp_path) -> None:
    api_keys_file = tmp_path / "api-keys.json"
    api_keys_file.write_text(
        json.dumps({"operator-token": {"actor_id": "operator-01", "role": "operator"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="PRAHARI_DATABASE_URL_FILE"):
        Settings(
            environment="production",
            data_mode="live",
            database_url="postgresql+psycopg://prahari_app:password@database/prahari",
            auth_mode="api_key",
            api_keys_json_file=api_keys_file,
            model_approval_status="approved",
            auto_create_schema=False,
        )


def test_traffic_demo_run_control_is_default_off_and_development_only() -> None:
    settings = Settings(environment="development")

    assert settings.traffic_demo_run_enabled is False
    assert settings.traffic_demo_max_file_bytes == 5 * 1024 * 1024

    with pytest.raises(ValidationError, match="restricted to development"):
        Settings(environment="test", traffic_demo_run_enabled=True)


def test_production_requires_explicit_model_artifacts_and_local_copilot(tmp_path) -> None:
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text(
        "postgresql+psycopg://prahari_app:password@database/prahari",
        encoding="utf-8",
    )
    api_keys_file = tmp_path / "api-keys.json"
    api_keys_file.write_text(
        json.dumps(
            {"operator-token-with-32-characters-001": {
                "actor_id": "operator-01",
                "role": "operator",
            }}
        ),
        encoding="utf-8",
    )
    common = {
        "environment": "production",
        "data_mode": "live",
        "database_url_file": database_url_file,
        "auth_mode": "api_key",
        "api_keys_json_file": api_keys_file,
        "model_approval_status": "approved",
        "auto_create_schema": False,
    }

    with pytest.raises(ValidationError, match="PRAHARI_MODEL_ARTIFACT_PATH"):
        Settings(**common)

    with pytest.raises(ValidationError, match="schema-constrained copilot"):
        Settings(
            **common,
            model_artifact_path=tmp_path / "model.txt",
            model_manifest_path=tmp_path / "model.manifest.json",
        )


def test_production_rejects_weak_api_tokens_and_wildcard_cors(tmp_path) -> None:
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text(
        "postgresql+psycopg://prahari_app:password@database/prahari",
        encoding="utf-8",
    )
    api_keys_file = tmp_path / "api-keys.json"
    api_keys_file.write_text(
        json.dumps({"weak": {"actor_id": "operator-01", "role": "operator"}}),
        encoding="utf-8",
    )
    common = {
        "environment": "production",
        "data_mode": "live",
        "database_url_file": database_url_file,
        "auth_mode": "api_key",
        "api_keys_json_file": api_keys_file,
        "model_approval_status": "approved",
        "model_artifact_path": tmp_path / "model.txt",
        "model_manifest_path": tmp_path / "model.manifest.json",
        "copilot_enabled": True,
        "auto_create_schema": False,
    }

    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(**common)

    api_keys_file.write_text(
        json.dumps(
            {"operator-token-with-32-characters-001": {
                "actor_id": "operator-01",
                "role": "operator",
            }}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="CORS origins"):
        Settings(**common, cors_origins="*")
