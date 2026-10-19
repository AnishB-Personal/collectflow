"""Automated Tests for Database Hardening WP-02 (Secret Files, Port Parsing, Container Integration).

Validates:
1. read_secret_file success with whitespace trimming.
2. read_secret_file error handling for missing and empty files.
3. parse_db_port strict validation (Trap 4: fail-closed on invalid port strings and out-of-range values).
4. COLLECTFLOW_DB_PASSWORD_FILE integration in get_database_url.
5. COLLECTFLOW_DATABASE_URL_FILE integration in get_database_url.
6. Integration environment mode validation with containerized host ('postgres').
7. Integration environment mode isolation (rejection of untrusted remote hosts).
8. Credential sanitization when password is loaded via secret file.
"""

import pytest
from app.core.database import (
    read_secret_file,
    parse_db_port,
    get_db_password,
    get_custom_database_url,
    get_database_url,
    validate_database_url,
    sanitize_credentials,
)


def test_read_secret_file_success(tmp_path):
    secret_file = tmp_path / "db_password.txt"
    secret_file.write_text("  sUpEr-S3cr3t-P@ss! \n\n", encoding="utf-8")
    result = read_secret_file(str(secret_file))
    assert result == "sUpEr-S3cr3t-P@ss!"


def test_read_secret_file_missing():
    with pytest.raises(FileNotFoundError) as exc_info:
        read_secret_file("/path/to/nonexistent/secret_file.txt")
    assert "Secret file not found" in str(exc_info.value)


def test_read_secret_file_empty(tmp_path):
    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("   \n\t ", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        read_secret_file(str(empty_file))
    assert "is empty" in str(exc_info.value)


def test_read_secret_file_empty_path():
    with pytest.raises(ValueError) as exc_info:
        read_secret_file("")
    assert "cannot be empty" in str(exc_info.value)


def test_parse_db_port_valid():
    assert parse_db_port(None) == 5432
    assert parse_db_port("") == 5432
    assert parse_db_port("   ") == 5432
    assert parse_db_port(5432) == 5432
    assert parse_db_port("5432") == 5432
    assert parse_db_port("1") == 1
    assert parse_db_port("65535") == 65535
    assert parse_db_port(5433) == 5433


def test_parse_db_port_invalid():
    invalid_ports = ["bad_port", "abc", "-1", "0", "65536", "70000", "5432abc", "None"]
    for bad in invalid_ports:
        with pytest.raises(ValueError) as exc_info:
            parse_db_port(bad)
        assert "Invalid database port" in str(exc_info.value)


def test_db_password_file_support(monkeypatch, tmp_path):
    secret_file = tmp_path / "db_pass.txt"
    secret_file.write_text("my_file_password\n", encoding="utf-8")

    monkeypatch.setenv("COLLECTFLOW_ENV", "dev")
    monkeypatch.delenv("COLLECTFLOW_DATABASE_URL", raising=False)
    monkeypatch.delenv("COLLECTFLOW_DATABASE_URL_FILE", raising=False)
    monkeypatch.delenv("COLLECTFLOW_DB_PASSWORD", raising=False)
    monkeypatch.setenv("COLLECTFLOW_DB_USER", "collectflow_user")
    monkeypatch.setenv("COLLECTFLOW_DB_PASSWORD_FILE", str(secret_file))
    monkeypatch.setenv("COLLECTFLOW_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("COLLECTFLOW_DB_PORT", "5432")
    monkeypatch.setenv("COLLECTFLOW_DB_NAME", "collectflow_dev")

    url = get_database_url()
    assert "my_file_password" in url
    assert "collectflow_user" in url
    assert "collectflow_dev" in url


def test_database_url_file_support(monkeypatch, tmp_path):
    url_file = tmp_path / "db_url.txt"
    url_file.write_text(
        "postgresql+psycopg://collectflow_user:file_secret@127.0.0.1:5432/collectflow_dev\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("COLLECTFLOW_ENV", "dev")
    monkeypatch.delenv("COLLECTFLOW_DATABASE_URL", raising=False)
    monkeypatch.setenv("COLLECTFLOW_DATABASE_URL_FILE", str(url_file))

    url = get_database_url()
    assert url == "postgresql+psycopg://collectflow_user:file_secret@127.0.0.1:5432/collectflow_dev"


def test_integration_mode_with_container_postgres(monkeypatch, tmp_path):
    secret_file = tmp_path / "int_pass.txt"
    secret_file.write_text("integration_pass\n", encoding="utf-8")

    monkeypatch.setenv("COLLECTFLOW_ENV", "integration")
    monkeypatch.delenv("COLLECTFLOW_DATABASE_URL", raising=False)
    monkeypatch.delenv("COLLECTFLOW_DATABASE_URL_FILE", raising=False)
    monkeypatch.delenv("COLLECTFLOW_DB_PASSWORD", raising=False)
    monkeypatch.setenv("COLLECTFLOW_DB_USER", "collectflow_user")
    monkeypatch.setenv("COLLECTFLOW_DB_PASSWORD_FILE", str(secret_file))
    monkeypatch.setenv("COLLECTFLOW_DB_HOST", "postgres")
    monkeypatch.setenv("COLLECTFLOW_DB_PORT", "5432")
    monkeypatch.setenv("COLLECTFLOW_DB_NAME", "collectflow_integration")

    url = get_database_url()
    assert "postgres:5432/collectflow_integration" in url
    validate_database_url(url, "integration")


def test_integration_mode_rejects_untrusted_remote_host(monkeypatch):
    monkeypatch.delenv("COLLECTFLOW_TRUSTED_CONTAINER_HOSTS", raising=False)
    untrusted_url = "postgresql+psycopg://collectflow_user:pass@192.168.1.100:5432/collectflow_integration"
    with pytest.raises(RuntimeError) as exc_info:
        validate_database_url(untrusted_url, "integration")
    assert "Remote host" in str(exc_info.value) or "prohibited" in str(exc_info.value)


def test_sanitize_credentials_with_password_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret_pw.txt"
    secret_file.write_text("TopSecretPassword123!\n", encoding="utf-8")

    monkeypatch.delenv("COLLECTFLOW_DB_PASSWORD", raising=False)
    monkeypatch.setenv("COLLECTFLOW_DB_PASSWORD_FILE", str(secret_file))

    raw_message = "Error connecting with TopSecretPassword123! to database"
    sanitized = sanitize_credentials(raw_message)
    assert "TopSecretPassword123!" not in sanitized
    assert "***" in sanitized
