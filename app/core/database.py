"""
SQLAlchemy 2.0 Database Session and Engine Configuration for PostgreSQL Parity.

Enforces:
- Hardened loopback isolation for non-production environments
- Strict DSN parsing via sqlalchemy.engine.make_url
- Whitelist validation of host, port, database name, user, and environment mode
- Explicit prevention of remote hosts, production-like databases, malformed DSNs, and unsafe credentials
- Safe fallback to verified local development PostgreSQL instance
"""

import os
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


PERMITTED_ENV_MODES = {
    "integration",
    "sandbox",
    "test",
    "development",
    "dev",
}


def validate_isolated_database_url(url_str: str, env_mode: str) -> None:
    """
    Rigorously validates database URL for non-production environments using AST / URL parsing.
    Prevents remote connections, production databases, and unverified credentials.
    Fails closed if env_mode is unrecognized or invalid.
    """
    if not env_mode or env_mode.lower() not in PERMITTED_ENV_MODES:
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Environment mode '{env_mode}' is not an authorized non-production environment {PERMITTED_ENV_MODES}."
        )

    if not url_str or not isinstance(url_str, str):
        raise ValueError("CRITICAL ISOLATION ERROR: Database URL cannot be empty.")

    try:
        url = make_url(url_str)
        if url.drivername.startswith("postgresql") and not url.host:
            raise ValueError("CRITICAL ISOLATION ERROR: Missing host in PostgreSQL URL.")
        if url.drivername.startswith("postgresql") and not url.database:
            raise ValueError("CRITICAL ISOLATION ERROR: Missing database in PostgreSQL URL.")
    except Exception as e:
        raise ValueError(f"CRITICAL ISOLATION ERROR: Malformed database URL: {e}") from e

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
        if env_mode not in ("test", "mock", "sqlite_legacy"):
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: SQLite driver is not permitted in '{env_mode}' mode. PostgreSQL is required."
            )
        db_path = url.database or ""
        if db_path != ":memory:" and not any(t in db_path.lower() for t in ("test", "sandbox", "dev")):
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: SQLite database '{db_path}' is not an authorized sandbox path."
            )
        return

    # PostgreSQL strict isolation enforcement
    # 1. Host validation: MUST be strictly loopback
    host = (url.host or "").strip().lower()
    if host not in PERMITTED_LOOPBACK_HOSTS:
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Remote host '{url.host}' is prohibited in '{env_mode}' mode. "
            f"Database must reside strictly on loopback {PERMITTED_LOOPBACK_HOSTS}."
        )

    # 2. Port validation: Must be loopback port (standard 5432 or 5433)
    port = url.port or 5432
    if port not in (5432, 5433, 5434):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Non-standard port '{port}' is prohibited in '{env_mode}' mode."
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
    if env_mode == "test" and db_name != "collectflow_test":
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database '{db_name}' is not permitted in 'test' mode. Test mode requires 'collectflow_test'."
        )
    if env_mode in ("dev", "development") and db_name not in ("collectflow_dev", "collectflow_sandbox"):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database '{db_name}' is not permitted in '{env_mode}' mode."
        )

    # 4. User validation: MUST be in approved user allowlist
    username = (url.username or "").strip().lower()
    if not username or username not in PERMITTED_DEV_USERS or any(token in username for token in ("prod", "production")):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database user '{url.username}' is not permitted in '{env_mode}' mode."
        )


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
        # In non-test mode, require explicit credentials from environment
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

    validate_isolated_database_url(custom_url, env_mode)
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
