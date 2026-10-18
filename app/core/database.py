"""
SQLAlchemy 2.0 Database Session and Engine Configuration for PostgreSQL Parity.

Enforces:
- Hardened loopback isolation for test and local development environments
- Cloud-agnostic portable PostgreSQL connectivity for containerized, on-premises, and cloud production environments
- Strict DSN parsing via sqlalchemy.engine.make_url
- Whitelist validation of host, port, database name, user, and environment mode
- Mandatory TLS (sslmode=require, verify-ca, verify-full) on remote/public endpoints
- Sensitive credential and password masking in all validation errors, traces, and logs
- Fail-closed security with zero silent fallbacks across all environment modes
"""

import ipaddress
import os
import re
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Declarative Base
Base = declarative_base()

# Permitted non-production database names
PERMITTED_DEV_DATABASES = {
    "collectflow_dev",
    "collectflow_test",
    "collectflow_sandbox",
    "collectflow_integration",
}

# Permitted non-production loopback hosts
PERMITTED_LOOPBACK_HOSTS = {
    "127.0.0.1",
    "localhost",
    "::1",
}

# Permitted non-production database users
PERMITTED_DEV_USERS = {
    "collectflow_user",
    "collectflow_test",
    "postgres",
}

# Authorized environment modes
PERMITTED_TEST_MODES = {"test"}
PERMITTED_DEV_MODES = {"dev", "development", "sandbox", "integration"}
PERMITTED_PROD_MODES = {"production", "prod", "staging"}
PERMITTED_ENV_MODES = PERMITTED_TEST_MODES | PERMITTED_DEV_MODES | PERMITTED_PROD_MODES

# Standard PostgreSQL SSL modes
STANDARD_PG_SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
SECURE_SSL_MODES = {"require", "verify-ca", "verify-full"}

# RFC 1918 private IPv4 networks
RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def sanitize_credentials(text: str) -> str:
    """Mask credentials in database connection strings, error messages, and traces."""
    if not text:
        return ""
    # Scrub DSN format ://username:password@host
    redacted = re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", str(text))
    # Scrub query param passwords
    redacted = re.sub(r"([?&]pass(?:word)?=)[^&]+", r"\1***", redacted, flags=re.IGNORECASE)
    return redacted


def sanitize_dsn(url_str_or_obj: str) -> str:
    """Mask credentials in database URL objects or connection strings."""
    if not url_str_or_obj:
        return ""
    try:
        u = make_url(str(url_str_or_obj))
        return u.render_as_string(hide_password=True)
    except Exception:
        return sanitize_credentials(str(url_str_or_obj))


def is_loopback_host(host: str) -> bool:
    """Determine if a hostname/IP represents a loopback interface."""
    if not host:
        return False
    host_clean = host.strip().lower().strip("[]")
    if host_clean in PERMITTED_LOOPBACK_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host_clean)
        return ip.is_loopback
    except ValueError:
        return False


def is_internal_or_container_host(host: str) -> bool:
    """
    Determine if a hostname/IP is an internal container, loopback, or private RFC1918 address.
    Returns True for loopback, RFC1918 private IPs, single-label container names (e.g. 'postgres'),
    and internal domain names (.internal, .local, .lan, .cluster.local).
    """
    if not host:
        return False
    host_clean = host.strip().lower().strip("[]")
    if is_loopback_host(host_clean):
        return True
    try:
        ip = ipaddress.ip_address(host_clean)
        if ip.is_loopback:
            return True
        if ip.version == 4:
            return any(ip in net for net in RFC1918_NETWORKS)
        return False
    except ValueError:
        pass
    # Single-label container hostname (e.g. 'postgres', 'db', 'collectflow-db')
    if "." not in host_clean:
        return True
    # Internal domain suffixes
    internal_suffixes = (".internal", ".local", ".lan", ".cluster.local")
    if any(host_clean.endswith(suffix) for suffix in internal_suffixes):
        return True
    return False


def validate_isolated_database_url(url_str: str, env_mode: str) -> None:
    """
    Rigorously validates database URL for non-production environments using AST / URL parsing.
    Prevents remote connections, production databases, and unverified credentials.
    Fails closed if env_mode is unrecognized, invalid, or production-oriented.
    """
    if not env_mode or env_mode.lower() not in (PERMITTED_TEST_MODES | PERMITTED_DEV_MODES):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Environment mode '{env_mode}' is not an authorized non-production environment {PERMITTED_TEST_MODES | PERMITTED_DEV_MODES}."
        )

    mode = env_mode.lower()

    if not url_str or not isinstance(url_str, str):
        raise ValueError("CRITICAL ISOLATION ERROR: Database URL cannot be empty.")

    safe_url = sanitize_dsn(url_str)
    try:
        url = make_url(url_str)
        if url.drivername.startswith("postgresql") and not url.host:
            raise ValueError(f"CRITICAL ISOLATION ERROR: Missing host in PostgreSQL URL '{safe_url}'.")
        if url.drivername.startswith("postgresql") and not url.database:
            raise ValueError(f"CRITICAL ISOLATION ERROR: Missing database in PostgreSQL URL '{safe_url}'.")
    except ValueError:
        raise
    except Exception as e:
        safe_msg = sanitize_credentials(str(e))
        raise ValueError(f"CRITICAL ISOLATION ERROR: Malformed database URL: {safe_msg}") from None

    # Driver check
    if not (url.drivername.startswith("postgresql") or url.drivername.startswith("sqlite")):
        raise ValueError(
            f"CRITICAL ISOLATION ERROR: Driver '{url.drivername}' is not supported. Must be postgresql or sqlite."
        )

    # Reject query parameter overrides that might hijack host or connection options
    if url.query:
        disallowed_query_keys = {
            "host",
            "hostaddr",
            "port",
            "dbname",
            "user",
            "service",
            "passfile",
            "options",
            "sslrootcert",
            "sslcert",
            "sslkey",
            "sslmode",
            "target_session_attrs",
            "fallback_application_name",
            "gssencmode",
        }
        for k in url.query.keys():
            if k.lower() in disallowed_query_keys:
                raise RuntimeError(
                    f"CRITICAL ISOLATION ERROR: Database URL query parameter '{k}' is prohibited."
                )

    # SQLite is permitted only if strictly memory or test file in mock/sqlite_legacy mode
    if url.drivername.startswith("sqlite"):
        if mode not in ("test", "mock", "sqlite_legacy"):
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: SQLite driver is not permitted in '{mode}' mode. PostgreSQL is required."
            )
        db_path = url.database or ""
        if db_path != ":memory:" and not any(t in db_path.lower() for t in ("test", "sandbox", "dev")):
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: SQLite database '{db_path}' is not an authorized sandbox path."
            )
        return

    # PostgreSQL strict isolation enforcement
    # 1. Host validation: MUST be strictly loopback in test mode
    host = (url.host or "").strip().lower()
    if mode == "test":
        if not is_loopback_host(host):
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: Remote host '{url.host}' is prohibited in '{mode}' mode. "
                f"Database must reside strictly on loopback {PERMITTED_LOOPBACK_HOSTS}."
            )
    else:
        # Dev / Sandbox / Integration modes allow loopback or local container hostname 'postgres'
        if not (is_loopback_host(host) or host == "postgres"):
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: Remote host '{url.host}' is prohibited in '{mode}' mode. "
                f"Database must reside strictly on loopback {PERMITTED_LOOPBACK_HOSTS} or local container network."
            )

    # 2. Port validation: Must be loopback/standard port (5432, 5433, 5434)
    port = url.port or 5432
    if port not in (5432, 5433, 5434):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Non-standard port '{port}' is prohibited in '{mode}' mode."
        )

    # 3. Database name validation: MUST be strictly in approved database allowlist
    db_name = (url.database or "").strip()
    if not db_name:
        raise RuntimeError("CRITICAL ISOLATION ERROR: Missing database name in PostgreSQL URL.")

    # Disallow production tokens in database name
    if any(token in db_name.lower() for token in ("prod", "production", "live_prod")):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database '{db_name}' appears to be a production database."
        )

    if db_name not in PERMITTED_DEV_DATABASES:
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database '{db_name}' is not in approved database allowlist {PERMITTED_DEV_DATABASES}."
        )

    # Environment-appropriate database enforcement
    if mode == "test" and db_name != "collectflow_test":
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database '{db_name}' is not permitted in 'test' mode. Test mode requires 'collectflow_test'."
        )
    if mode in ("dev", "development") and db_name not in ("collectflow_dev", "collectflow_sandbox"):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database '{db_name}' is not permitted in '{mode}' mode."
        )

    # 4. User validation: MUST be in approved user allowlist
    username = (url.username or "").strip().lower()
    if not username or username not in PERMITTED_DEV_USERS or any(token in username for token in ("prod", "production")):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database user '{url.username}' is not permitted in '{mode}' mode."
        )


def validate_production_database_url(url_str: str, env_mode: str) -> None:
    """
    Rigorously validates database URL for production and staging environments.
    Supports containerized hosts, on-premises servers, private-cloud VPCs, and managed cloud endpoints.
    Enforces mandatory TLS for remote endpoints, disallows SQLite, and redacts credentials from all error messages.
    """
    if not env_mode or env_mode.lower() not in PERMITTED_PROD_MODES:
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Environment mode '{env_mode}' is not an authorized production/staging environment {PERMITTED_PROD_MODES}."
        )

    mode = env_mode.lower()

    if not url_str or not isinstance(url_str, str):
        raise ValueError("CRITICAL ISOLATION ERROR: Database URL cannot be empty.")

    safe_url = sanitize_dsn(url_str)
    try:
        url = make_url(url_str)
    except Exception as e:
        safe_msg = sanitize_credentials(str(e))
        raise ValueError(f"CRITICAL ISOLATION ERROR: Malformed database URL: {safe_msg}") from None

    # SQLite is strictly forbidden in production and staging
    if url.drivername.startswith("sqlite"):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: SQLite driver is not permitted in '{mode}' mode. PostgreSQL is required."
        )

    # PostgreSQL driver check
    if not (url.drivername == "postgresql" or url.drivername.startswith("postgresql+")):
        raise ValueError(
            f"CRITICAL ISOLATION ERROR: Driver '{url.drivername}' is not supported in '{mode}' mode. Must be postgresql or postgresql+psycopg."
        )

    # Required host, database, user, and password
    host = (url.host or "").strip()
    if not host:
        raise ValueError(f"CRITICAL ISOLATION ERROR: Missing host in PostgreSQL URL '{safe_url}'.")

    db_name = (url.database or "").strip()
    if not db_name:
        raise ValueError(f"CRITICAL ISOLATION ERROR: Missing database in PostgreSQL URL '{safe_url}'.")

    username = (url.username or "").strip()
    if not username:
        raise ValueError(f"CRITICAL ISOLATION ERROR: Missing database user in PostgreSQL URL '{safe_url}'.")

    if url.password is None:
        raise ValueError(f"CRITICAL ISOLATION ERROR: Database password is required in '{mode}' mode.")

    # Disallow query parameter overrides that might hijack host or routing
    if url.query:
        disallowed_prod_query_keys = {
            "host",
            "hostaddr",
            "port",
            "dbname",
            "user",
            "service",
            "passfile",
        }
        for k in url.query.keys():
            if k.lower() in disallowed_prod_query_keys:
                raise RuntimeError(
                    f"CRITICAL ISOLATION ERROR: Database URL query parameter '{k}' is prohibited."
                )

    # TLS / SSL Policy Enforcement
    sslmode = ""
    if url.query and "sslmode" in url.query:
        sslmode = str(url.query["sslmode"]).strip().lower()
        if sslmode not in STANDARD_PG_SSL_MODES:
            raise ValueError(
                f"CRITICAL ISOLATION ERROR: Invalid sslmode '{sslmode}' in PostgreSQL URL '{safe_url}'. "
                f"Must be one of {STANDARD_PG_SSL_MODES}."
            )

    is_internal = is_internal_or_container_host(host)
    if not is_internal:
        # Remote cloud endpoint or public network: TLS encryption is mandatory
        if sslmode not in SECURE_SSL_MODES:
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: Remote host '{host}' requires TLS encryption (sslmode=require, verify-ca, or verify-full) in '{mode}' mode. URL: '{safe_url}'."
            )
    else:
        # Internal container or RFC1918 private network: TLS recommended, unencrypted allowed for internal meshes
        pass


def validate_database_url(url_str: str, env_mode: str) -> None:
    """
    Rigorously validates database URL for all environments.
    Routes to validate_isolated_database_url for non-production modes,
    and validate_production_database_url for production/staging modes.
    """
    if not env_mode:
        raise RuntimeError("CRITICAL ISOLATION ERROR: COLLECTFLOW_ENV environment variable is required.")
    mode = env_mode.lower()
    if mode not in PERMITTED_ENV_MODES:
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Environment mode '{env_mode}' is not authorized. Must be one of {PERMITTED_ENV_MODES}."
        )

    if mode in PERMITTED_PROD_MODES:
        validate_production_database_url(url_str, mode)
    else:
        validate_isolated_database_url(url_str, mode)


def get_database_url() -> str:
    raw_env = os.environ.get("COLLECTFLOW_ENV")
    if not raw_env:
        raise RuntimeError("CRITICAL ISOLATION ERROR: COLLECTFLOW_ENV environment variable is required.")
    env_mode = raw_env.lower()
    if env_mode not in PERMITTED_ENV_MODES:
        raise RuntimeError(f"CRITICAL ISOLATION ERROR: Unauthorized COLLECTFLOW_ENV '{raw_env}'.")

    custom_url = os.environ.get("COLLECTFLOW_DATABASE_URL")
    if not custom_url:
        if env_mode == "test":
            raise RuntimeError(
                "CRITICAL ISOLATION ERROR: Explicit COLLECTFLOW_DATABASE_URL is required in 'test' mode. "
                "Refusing silent fallback to development database."
            )
        elif env_mode in PERMITTED_PROD_MODES:
            db_user = os.environ.get("COLLECTFLOW_DB_USER")
            db_pass = os.environ.get("COLLECTFLOW_DB_PASSWORD")
            db_host = os.environ.get("COLLECTFLOW_DB_HOST")
            db_name = os.environ.get("COLLECTFLOW_DB_NAME")
            if not all([db_user, db_pass, db_host, db_name]):
                raise RuntimeError(
                    "CRITICAL ISOLATION ERROR: Explicit COLLECTFLOW_DATABASE_URL (or complete COLLECTFLOW_DB_USER, "
                    "COLLECTFLOW_DB_PASSWORD, COLLECTFLOW_DB_HOST, and COLLECTFLOW_DB_NAME) is required in "
                    f"'{env_mode}' mode. Missing required database configuration."
                )
            db_port = os.environ.get("COLLECTFLOW_DB_PORT", "5432")
            db_sslmode = os.environ.get("COLLECTFLOW_DB_SSLMODE")
            ssl_query = f"?sslmode={db_sslmode}" if db_sslmode else ""
            custom_url = f"postgresql+psycopg://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}{ssl_query}"
        else:
            # Non-test development/sandbox modes
            db_user = os.environ.get("COLLECTFLOW_DB_USER")
            db_pass = os.environ.get("COLLECTFLOW_DB_PASSWORD")
            if not db_user or not db_pass:
                raise RuntimeError(
                    "CRITICAL ISOLATION ERROR: Explicit COLLECTFLOW_DATABASE_URL (or COLLECTFLOW_DB_USER and "
                    "COLLECTFLOW_DB_PASSWORD) is required. Hardcoded reusable credentials are not permitted in application source."
                )
            db_host = os.environ.get("COLLECTFLOW_DB_HOST", "127.0.0.1")
            db_port = os.environ.get("COLLECTFLOW_DB_PORT", "5432")
            db_name = os.environ.get("COLLECTFLOW_DB_NAME", "collectflow_dev")
            custom_url = f"postgresql+psycopg://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"

    validate_database_url(custom_url, env_mode)
    return custom_url


def create_db_engine():
    return create_engine(
        get_database_url(),
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        echo=False,
    )


engine = create_db_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Database session context manager and FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def transaction_context(session: Session = None) -> Generator[Session, None, None]:
    """Context manager for atomic database transactions with automatic rollback."""
    if session is not None:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
    else:
        with get_db_session() as new_session:
            try:
                yield new_session
                new_session.commit()
            except Exception:
                new_session.rollback()
                raise
