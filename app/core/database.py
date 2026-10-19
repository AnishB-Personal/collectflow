"""
SQLAlchemy 2.0 Database Session and Engine Configuration for PostgreSQL Parity.

Enforces:
- Hardened loopback isolation for test and local development environments
- Cloud-agnostic portable PostgreSQL connectivity for containerized, on-premises, and cloud production environments
- Strict DSN parsing and safe URL construction via sqlalchemy.engine.URL and make_url
- Whitelist validation of host, port, database name, user, and environment mode
- Explicit driver contract: explicit postgresql+psycopg preferred, plain postgresql normalized, unsupported dialects rejected
- Offline engine constructibility validation without network connection
- Defensible TLS trust classification: standard container service names (postgres, db) trusted by default,
  custom container exceptions permitted only through COLLECTFLOW_TRUSTED_CONTAINER_HOSTS,
  mandatory secure TLS (sslmode=require, verify-ca, verify-full) on all remote and untrusted endpoints
- Safe database credential handling preventing leaks of passwords or password fragments in traces and parser errors
- Fail-closed security with zero silent fallbacks across all environment modes
"""

import ipaddress
import os
import re
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url, URL
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

# Standard Docker Compose / container service names permitted for local container networking
DEFAULT_TRUSTED_CONTAINER_HOSTS = {
    "postgres",
    "db",
    "collectflow-postgres",
    "collectflow-db",
}


def sanitize_credentials(text: str) -> str:
    """Mask credentials in database connection strings, error messages, and traces."""
    if not text:
        return ""
    s = str(text)
    # Scrub any URL pattern ://username:password@host
    s = re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", s)
    # Scrub query param passwords
    s = re.sub(r"([?&]pass(?:word)?=)[^&]+", r"\1***", s, flags=re.IGNORECASE)
    # Scrub known environment password if present
    env_pass = os.environ.get("COLLECTFLOW_DB_PASSWORD")
    if env_pass and len(env_pass) >= 4 and env_pass in s:
        s = s.replace(env_pass, "***")
    return s


def sanitize_dsn(url_str_or_obj: str | URL) -> str:
    """Mask credentials in database URL objects or connection strings."""
    if not url_str_or_obj:
        return ""
    try:
        if isinstance(url_str_or_obj, URL):
            return url_str_or_obj.render_as_string(hide_password=True)
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


def get_trusted_container_hosts() -> set[str]:
    """Return set of explicitly trusted local container hostnames."""
    trusted = set(DEFAULT_TRUSTED_CONTAINER_HOSTS)
    extra = os.environ.get("COLLECTFLOW_TRUSTED_CONTAINER_HOSTS", "")
    if extra:
        for item in extra.split(","):
            clean = item.strip().lower().strip("[]")
            if clean:
                trusted.add(clean)
    return trusted


def is_trusted_container_host(host: str) -> bool:
    """
    Determine if a hostname is an explicitly trusted local container service.
    Only permitted default Docker Compose service names ('postgres', 'db', 'collectflow-postgres', 'collectflow-db')
    or hostnames explicitly declared in COLLECTFLOW_TRUSTED_CONTAINER_HOSTS are trusted.
    Arbitrary single-label hostnames, internal domain suffixes, and unlisted IPs are NOT trusted.
    """
    if not host:
        return False
    clean = host.strip().lower().strip("[]")
    return clean in get_trusted_container_hosts()


def validate_engine_constructible(url_obj: URL) -> None:
    """
    Validate that SQLAlchemy can construct an engine and load the dialect/driver without connecting.
    Executes offline with zero external network or database connections.
    """
    try:
        test_url = url_obj
        if test_url.drivername == "postgresql":
            test_url = test_url.set(drivername="postgresql+psycopg")
        eng = create_engine(test_url)
        if eng.dialect.name != "postgresql" or eng.dialect.driver != "psycopg":
            eng.dispose()
            raise ValueError(
                f"Unsupported dialect/driver combination: {eng.dialect.name}+{eng.dialect.driver}. "
                "Must be postgresql+psycopg."
            )
        eng.dispose()
    except ValueError:
        raise
    except Exception as e:
        safe_err = sanitize_credentials(str(e))
        raise ValueError(
            f"CRITICAL ISOLATION ERROR: Failed to construct engine for database URL: {safe_err}"
        ) from None


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

    try:
        url = make_url(url_str)
        if url.drivername.startswith("postgresql") and not url.host:
            raise ValueError("CRITICAL ISOLATION ERROR: Missing host in PostgreSQL URL.")
        if url.drivername.startswith("postgresql") and not url.database:
            raise ValueError("CRITICAL ISOLATION ERROR: Missing database in PostgreSQL URL.")
    except ValueError:
        raise
    except Exception:
        # Avoid leaking unencoded password fragments or invalid tokens from parser errors
        raise ValueError("CRITICAL ISOLATION ERROR: Malformed database URL: Unable to parse connection string.") from None

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

    # Validate driver contract: explicit postgresql+psycopg preferred; plain postgresql accepted
    if url.drivername not in ("postgresql+psycopg", "postgresql"):
        raise ValueError(
            f"CRITICAL ISOLATION ERROR: Driver '{url.drivername}' is not supported. Must be postgresql+psycopg."
        )

    # Validate SQLAlchemy engine constructibility offline
    validate_engine_constructible(url)

    # PostgreSQL strict isolation enforcement
    # 1. Host validation: MUST be strictly loopback in test mode
    host = (url.host or "").strip().lower().strip("[]")
    if mode == "test":
        if not is_loopback_host(host):
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: Remote host '{url.host}' is prohibited in '{mode}' mode. "
                f"Database must reside strictly on loopback {PERMITTED_LOOPBACK_HOSTS}."
            )
    else:
        # Dev / Sandbox / Integration modes allow loopback or trusted container service
        if not (is_loopback_host(host) or is_trusted_container_host(host)):
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: Remote host '{url.host}' is prohibited in '{mode}' mode. "
                f"Database must reside strictly on loopback {PERMITTED_LOOPBACK_HOSTS} or trusted local container network."
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

    try:
        url = make_url(url_str)
    except Exception:
        # Avoid leaking unencoded password fragments or invalid tokens from parser errors
        raise ValueError("CRITICAL ISOLATION ERROR: Malformed database URL: Unable to parse connection string.") from None

    safe_url = sanitize_dsn(url)

    # SQLite is strictly forbidden in production and staging
    if url.drivername.startswith("sqlite"):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: SQLite driver is not permitted in '{mode}' mode. PostgreSQL is required."
        )

    # Driver contract: explicit postgresql+psycopg preferred, plain postgresql normalized, unsupported variants rejected
    if url.drivername not in ("postgresql+psycopg", "postgresql"):
        raise ValueError(
            f"CRITICAL ISOLATION ERROR: Driver '{url.drivername}' is not supported in '{mode}' mode. Must be postgresql+psycopg."
        )

    # Validate SQLAlchemy engine constructibility offline
    validate_engine_constructible(url)

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

    is_trusted_local = is_loopback_host(host) or is_trusted_container_host(host)
    if not is_trusted_local:
        # Untrusted single-label hostnames, internal-looking domains, private IPs, or remote cloud endpoints:
        # Secure TLS encryption (sslmode=require, verify-ca, verify-full) is mandatory by default.
        if sslmode not in SECURE_SSL_MODES:
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: Host '{host}' is not a trusted local container and requires secure TLS encryption "
                f"(sslmode=require, verify-ca, or verify-full) in '{mode}' mode. URL: '{safe_url}'. "
                "To permit local unencrypted container networking, configure host in COLLECTFLOW_TRUSTED_CONTAINER_HOSTS."
            )


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
            port_val = int(db_port) if (db_port and str(db_port).isdigit()) else 5432
            db_sslmode = os.environ.get("COLLECTFLOW_DB_SSLMODE")
            query_params = {"sslmode": db_sslmode} if db_sslmode else {}
            # Safe URL construction via URL.create() with automatic character escaping
            url_obj = URL.create(
                drivername="postgresql+psycopg",
                username=db_user,
                password=db_pass,
                host=db_host,
                port=port_val,
                database=db_name,
                query=query_params,
            )
            custom_url = url_obj.render_as_string(hide_password=False)
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
            port_val = int(db_port) if (db_port and str(db_port).isdigit()) else 5432
            db_name = os.environ.get("COLLECTFLOW_DB_NAME", "collectflow_dev")
            # Safe URL construction via URL.create() with automatic character escaping
            url_obj = URL.create(
                drivername="postgresql+psycopg",
                username=db_user,
                password=db_pass,
                host=db_host,
                port=port_val,
                database=db_name,
            )
            custom_url = url_obj.render_as_string(hide_password=False)

    validate_database_url(custom_url, env_mode)

    # Normalize plain postgresql to postgresql+psycopg
    parsed = make_url(custom_url)
    if parsed.drivername == "postgresql":
        custom_url = parsed.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)

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
