"""Automated Security & PostgreSQL Isolation Hardening Tests (Release Preparation WP-01).

Validates:
1. Rejection of remote host in test mode.
2. Rejection of production database name in test mode.
3. Rejection of production database name in dev mode.
4. Rejection of SQLite driver in production mode.
5. Acceptance of valid container hostname ('postgres') in production mode.
6. Acceptance of valid internal IP ('10.0.1.50', '172.20.0.5', '192.168.1.100') in production mode.
7. Acceptance of valid cloud endpoint ('mydb.xyz.rds.amazonaws.com') with sslmode=require in production mode.
8. Rejection of remote cloud endpoint without TLS (sslmode=disable or missing sslmode).
9. Password redaction in error messages when validation fails.
10. Rejection of malformed DSN.
11. Rejection of missing credentials in production mode.
12. Rejection of unrecognized COLLECTFLOW_ENV.
13. Preserved test mode pass with valid loopback test DSN.
"""

import pytest
from app.core.database import (
    validate_isolated_database_url,
    validate_production_database_url,
    validate_database_url,
    get_database_url,
)


def test_01_reject_remote_host_in_test_mode():
    """1. Test mode strictly forbids remote hosts and private non-loopback hosts."""
    remote_urls = [
        "postgresql+psycopg://collectflow_user:secret@192.168.1.50:5432/collectflow_test",
        "postgresql+psycopg://collectflow_user:secret@10.0.0.5:5432/collectflow_test",
        "postgresql+psycopg://collectflow_user:secret@db.internal.lan:5432/collectflow_test",
        "postgresql+psycopg://collectflow_user:secret@rds.us-east-1.amazonaws.com:5432/collectflow_test",
        "postgresql+psycopg://collectflow_user:secret@postgres:5432/collectflow_test",
    ]
    for url in remote_urls:
        with pytest.raises(RuntimeError) as exc_info:
            validate_isolated_database_url(url, "test")
        assert "Remote host" in str(exc_info.value) or "prohibited" in str(exc_info.value)

        with pytest.raises(RuntimeError) as exc_info2:
            validate_database_url(url, "test")
        assert "Remote host" in str(exc_info2.value) or "prohibited" in str(exc_info2.value)


def test_02_reject_production_database_name_in_test_mode():
    """2. Test mode rejects any production tokens in database names."""
    prod_names = [
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_prod",
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/production_db",
        "postgresql+psycopg://collectflow_user:secret@localhost:5432/collectflow_live_prod",
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/prod_app",
    ]
    for url in prod_names:
        with pytest.raises(RuntimeError) as exc_info:
            validate_isolated_database_url(url, "test")
        assert "production" in str(exc_info.value).lower() or "not permitted" in str(exc_info.value).lower()


def test_03_reject_production_database_name_in_dev_mode():
    """3. Development mode rejects production database names."""
    prod_dev_urls = [
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_prod",
        "postgresql+psycopg://collectflow_user:secret@localhost:5432/production_orders",
        "postgresql+psycopg://collectflow_user:secret@postgres:5432/collectflow_live_prod",
    ]
    for url in prod_dev_urls:
        for mode in ("dev", "development", "sandbox"):
            with pytest.raises(RuntimeError) as exc_info:
                validate_isolated_database_url(url, mode)
            assert "production" in str(exc_info.value).lower() or "not permitted" in str(exc_info.value).lower()


def test_04_reject_sqlite_driver_in_production_mode():
    """4. Production and staging environments unconditionally reject SQLite driver."""
    sqlite_urls = [
        "sqlite:///./collectflow.db",
        "sqlite:///:memory:",
        "sqlite+pysqlite:///prod.db",
    ]
    for mode in ("production", "prod", "staging"):
        for url in sqlite_urls:
            with pytest.raises(RuntimeError) as exc_info:
                validate_production_database_url(url, mode)
            assert "SQLite driver is not permitted" in str(exc_info.value)

            with pytest.raises(RuntimeError) as exc_info2:
                validate_database_url(url, mode)
            assert "SQLite driver is not permitted" in str(exc_info2.value)


def test_05_accept_valid_container_hostname_in_production_mode():
    """5. Container networking hostnames (e.g. 'postgres') are accepted in production/staging mode."""
    container_urls = [
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@postgres:5432/collectflow_prod",
        "postgresql://collectflow_admin:ProdPassword123!@db:5432/collectflow_prod",
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@collectflow-postgres:5432/collectflow_prod",
    ]
    for url in container_urls:
        # Must not raise any exceptions
        validate_production_database_url(url, "production")
        validate_production_database_url(url, "staging")
        validate_database_url(url, "production")


def test_06_accept_valid_internal_ips_in_production_mode():
    """6. RFC1918 private IPs are accepted in production/staging mode."""
    internal_ip_urls = [
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@10.0.1.50:5432/collectflow_prod",
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@172.20.0.5:5432/collectflow_staging",
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@192.168.1.100:5432/collectflow_prod",
    ]
    for url in internal_ip_urls:
        validate_production_database_url(url, "production")
        validate_production_database_url(url, "staging")
        validate_database_url(url, "production")


def test_07_accept_valid_cloud_endpoint_with_tls_in_production_mode():
    """7. Remote cloud endpoints with TLS (require, verify-ca, verify-full) are accepted in production mode."""
    cloud_urls = [
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@mydb.xyz.rds.amazonaws.com:5432/collectflow_prod?sslmode=require",
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@mydb.xyz.rds.amazonaws.com:5432/collectflow_prod?sslmode=verify-ca",
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@postgres.database.azure.com:5432/collectflow_prod?sslmode=verify-full",
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@cloudsql.gcp.internal:5432/collectflow_prod?sslmode=require",
    ]
    for url in cloud_urls:
        validate_production_database_url(url, "production")
        validate_production_database_url(url, "prod")
        validate_database_url(url, "production")


def test_08_reject_remote_cloud_endpoint_without_tls():
    """8. Remote cloud endpoints or public IPs without required TLS encryption are rejected."""
    insecure_urls = [
        # Missing sslmode
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@mydb.xyz.rds.amazonaws.com:5432/collectflow_prod",
        # sslmode=disable
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@mydb.xyz.rds.amazonaws.com:5432/collectflow_prod?sslmode=disable",
        # sslmode=prefer
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@mydb.xyz.rds.amazonaws.com:5432/collectflow_prod?sslmode=prefer",
        # Public IP without TLS
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@198.51.100.25:5432/collectflow_prod",
        "postgresql+psycopg://collectflow_admin:ProdPassword123!@198.51.100.25:5432/collectflow_prod?sslmode=disable",
    ]
    for url in insecure_urls:
        with pytest.raises(RuntimeError) as exc_info:
            validate_production_database_url(url, "production")
        assert "requires TLS encryption" in str(exc_info.value)

        with pytest.raises(RuntimeError) as exc_info2:
            validate_database_url(url, "production")
        assert "requires TLS encryption" in str(exc_info2.value)


def test_09_password_redaction_in_error_messages():
    """9. Database passwords must never appear in error messages, exception strings, or traces."""
    secret_pass = "TopSecretSuperPassword123!"

    # Case A: Remote cloud endpoint without TLS
    insecure_url = f"postgresql+psycopg://db_admin:{secret_pass}@mydb.xyz.rds.amazonaws.com:5432/collectflow_prod?sslmode=disable"
    with pytest.raises(RuntimeError) as exc_a:
        validate_production_database_url(insecure_url, "production")
    err_msg_a = str(exc_a.value)
    assert secret_pass not in err_msg_a, f"Password leaked in error message: {err_msg_a}"
    assert "***" in err_msg_a

    # Case B: Malformed URL with credentials
    malformed_url = f"postgresql+psycopg://db_admin:{secret_pass}@:invalid_port/db"
    with pytest.raises(ValueError) as exc_b:
        validate_production_database_url(malformed_url, "production")
    err_msg_b = str(exc_b.value)
    assert secret_pass not in err_msg_b, f"Password leaked in malformed error: {err_msg_b}"

    # Case C: Test mode isolation rejection with password
    test_remote_url = f"postgresql+psycopg://db_admin:{secret_pass}@192.168.1.200:5432/collectflow_test"
    with pytest.raises(RuntimeError) as exc_c:
        validate_isolated_database_url(test_remote_url, "test")
    err_msg_c = str(exc_c.value)
    assert secret_pass not in err_msg_c, f"Password leaked in isolation error: {err_msg_c}"


def test_10_reject_malformed_dsn():
    """10. Malformed or empty database URLs are rejected with fail-closed errors."""
    malformed_cases = [
        "",
        None,
        "not-a-valid-url",
        "://missing-scheme",
        "postgresql+psycopg://:5432",
        "postgresql+psycopg://user:pass@/collectflow_prod",  # missing host
        "postgresql+psycopg://user:pass@127.0.0.1:5432/",     # missing db
    ]
    for m in malformed_cases:
        for mode in ("test", "dev", "production"):
            with pytest.raises((ValueError, RuntimeError)) as exc_info:
                validate_database_url(m, mode)
            assert "CRITICAL ISOLATION ERROR" in str(exc_info.value)


def test_11_reject_missing_credentials_in_production_mode(monkeypatch):
    """11. Production mode requires complete credentials and rejects missing configuration."""
    monkeypatch.setenv("COLLECTFLOW_ENV", "production")
    monkeypatch.delenv("COLLECTFLOW_DATABASE_URL", raising=False)
    monkeypatch.delenv("COLLECTFLOW_DB_USER", raising=False)
    monkeypatch.delenv("COLLECTFLOW_DB_PASSWORD", raising=False)
    monkeypatch.delenv("COLLECTFLOW_DB_HOST", raising=False)
    monkeypatch.delenv("COLLECTFLOW_DB_NAME", raising=False)

    # Missing all database configuration
    with pytest.raises(RuntimeError) as exc_info:
        get_database_url()
    assert "Explicit COLLECTFLOW_DATABASE_URL" in str(exc_info.value) or "Missing required" in str(exc_info.value)

    # Incomplete environment variables
    monkeypatch.setenv("COLLECTFLOW_DB_USER", "prod_user")
    monkeypatch.setenv("COLLECTFLOW_DB_PASSWORD", "prod_pass")
    # host and db_name still missing
    with pytest.raises(RuntimeError) as exc_info2:
        get_database_url()
    assert "Missing required database configuration" in str(exc_info2.value)

    # URL missing password
    no_pass_url = "postgresql+psycopg://admin@postgres:5432/collectflow_prod"
    with pytest.raises(ValueError) as exc_info3:
        validate_production_database_url(no_pass_url, "production")
    assert "Database password is required" in str(exc_info3.value)


def test_12_reject_unrecognized_env_mode(monkeypatch):
    """12. Unrecognized environment modes fail closed."""
    invalid_modes = ["staging_invalid", "prod_unknown", "uat", "local_docker", ""]
    valid_url = "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_dev"

    for mode in invalid_modes:
        with pytest.raises(RuntimeError) as exc_info:
            validate_database_url(valid_url, mode)
        assert "CRITICAL ISOLATION ERROR" in str(exc_info.value)

        monkeypatch.setenv("COLLECTFLOW_ENV", mode)
        with pytest.raises(RuntimeError) as exc_info2:
            get_database_url()
        assert "CRITICAL ISOLATION ERROR" in str(exc_info2.value)


def test_13_preserved_test_mode_pass_with_valid_loopback_dsn(monkeypatch):
    """13. Verified test mode with loopback test DSN passes cleanly."""
    valid_test_urls = [
        "postgresql+psycopg://collectflow_user:secret@127.0.0.1:5432/collectflow_test",
        "postgresql+psycopg://collectflow_user:secret@localhost:5432/collectflow_test",
        "postgresql+psycopg://collectflow_test:secret@[::1]:5432/collectflow_test",
    ]
    for url in valid_test_urls:
        validate_isolated_database_url(url, "test")
        validate_database_url(url, "test")

        monkeypatch.setenv("COLLECTFLOW_ENV", "test")
        monkeypatch.setenv("COLLECTFLOW_DATABASE_URL", url)
        resolved_url = get_database_url()
        assert resolved_url == url
