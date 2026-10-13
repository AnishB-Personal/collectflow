"""Automated Negative Isolation & Connection Guardrail Tests.

Verifies:
1. Rejection of remote hosts (e.g. AWS RDS, external IPs, production domains)
2. Rejection of production database names (e.g. collectflow_prod, production_db)
3. Rejection of malformed / unparseable connection strings
4. Rejection of unsupported database drivers
5. Rejection of production database users
6. Successful validation of authorized loopback DSNs
"""

import pytest
from app.core.database import validate_isolated_database_url


def test_reject_remote_hosts():
    remote_urls = [
        "postgresql+psycopg://collectflow_user:secret@192.168.1.50:5432/collectflow_dev",
        "postgresql+psycopg://collectflow_user:secret@rds.us-east-1.amazonaws.com:5432/collectflow_dev",
        "postgresql+psycopg://collectflow_user:secret@database.internal.corp:5432/collectflow_dev",
        "postgresql+psycopg://collectflow_user:secret@10.0.0.5:5432/collectflow_dev",
    ]
    for url in remote_urls:
        with pytest.raises(RuntimeError) as exc_info:
            validate_isolated_database_url(url, "integration")
        assert "Remote host" in str(exc_info.value) or "prohibited" in str(exc_info.value)


def test_reject_production_database_names():
    prod_urls = [
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_prod",
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/production",
        "postgresql+psycopg://collectflow_user:secret@localhost:5432/collectflow_live_prod",
    ]
    for url in prod_urls:
        with pytest.raises(RuntimeError) as exc_info:
            validate_isolated_database_url(url, "integration")
        assert "production" in str(exc_info.value).lower() or "not an authorized" in str(exc_info.value).lower()


def test_reject_unauthorized_non_dev_database_names():
    invalid_urls = [
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/some_unauthorized_db",
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/orders_main",
    ]
    for url in invalid_urls:
        with pytest.raises(RuntimeError) as exc_info:
            validate_isolated_database_url(url, "integration")
        assert "approved database allowlist" in str(exc_info.value)


def test_reject_query_parameter_overrides():
    hijack_urls = [
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_dev?host=remote.db.com",
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_dev?hostaddr=10.0.0.1",
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_dev?service=remote_pg",
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_dev?passfile=/etc/shadow",
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_dev?options=-c%20default_transaction_read_only=off",
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_dev?sslmode=disable",
    ]
    for url in hijack_urls:
        with pytest.raises(RuntimeError) as exc_info:
            validate_isolated_database_url(url, "integration")
        assert "prohibited" in str(exc_info.value)


def test_reject_unauthorized_env_mode():
    valid_url = "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_dev"
    for invalid_env in ["production", "prod", "staging_prod", "", None]:
        with pytest.raises(RuntimeError) as exc_info:
            validate_isolated_database_url(valid_url, invalid_env)
        assert "CRITICAL ISOLATION ERROR" in str(exc_info.value)


def test_reject_sqlite_in_live_integration_mode():
    sqlite_url = "sqlite:///./data/collectflow.db"
    with pytest.raises(RuntimeError) as exc_info:
        validate_isolated_database_url(sqlite_url, "integration")
    assert "SQLite driver is not permitted" in str(exc_info.value)


def test_reject_malformed_urls():
    malformed = [
        "",
        None,
        "not-a-valid-url",
        "://missing-scheme",
        "postgresql+psycopg://:5432",
    ]
    for m in malformed:
        with pytest.raises(ValueError) as exc_info:
            validate_isolated_database_url(m, "integration")
        assert "CRITICAL ISOLATION ERROR" in str(exc_info.value)


def test_reject_unsupported_drivers():
    unsupported = [
        "mysql+pymysql://collectflow_user:secret@127.0.0.1:3306/collectflow_dev",
        "oracle+cx_oracle://collectflow_user:secret@127.0.0.1:1521/collectflow_dev",
        "mssql+pyodbc://collectflow_user:secret@127.0.0.1:1433/collectflow_dev",
    ]
    for url in unsupported:
        with pytest.raises(ValueError) as exc_info:
            validate_isolated_database_url(url, "integration")
        assert "Driver" in str(exc_info.value) and "not supported" in str(exc_info.value)


def test_reject_production_users():
    prod_user_url = "postgresql+psycopg://prod_admin:secret@127.0.0.1:5432/collectflow_dev"
    with pytest.raises(RuntimeError) as exc_info:
        validate_isolated_database_url(prod_user_url, "integration")
    assert "Database user" in str(exc_info.value) and "not permitted" in str(exc_info.value)


def test_accept_valid_loopback_dev_urls():
    valid = [
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_dev",
        "postgresql+psycopg://collectflow_user:secret@localhost:5432/collectflow_test",
        "postgresql+psycopg://collectflow_test:secret@127.0.0.1:5432/collectflow_sandbox",
    ]
    for url in valid:
        # Should not raise any exception
        validate_isolated_database_url(url, "integration")


def test_test_mode_without_valid_test_dsn_fails_before_database_operation(monkeypatch):
    from app.core.database import get_database_url

    # 1. Test mode without COLLECTFLOW_DATABASE_URL must fail fast before any DB operation
    monkeypatch.setenv("COLLECTFLOW_ENV", "test")
    monkeypatch.delenv("COLLECTFLOW_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        get_database_url()
    assert "Explicit COLLECTFLOW_DATABASE_URL is required in 'test' mode" in str(exc_info.value) or "Refusing" in str(exc_info.value)

    # 2. Test mode with a dev database DSN must fail fast and refuse connection
    dev_dsn = "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_dev"
    monkeypatch.setenv("COLLECTFLOW_DATABASE_URL", dev_dsn)
    with pytest.raises(RuntimeError) as exc_info2:
        get_database_url()
    assert "not permitted in 'test' mode" in str(exc_info2.value) or "Test mode requires" in str(exc_info2.value)

