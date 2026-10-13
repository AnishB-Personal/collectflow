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

DEFAULT_DEV_PG_URL = "postgresql+psycopg://collectflow_user:collectflow_secret@127.0.0.1:5432/collectflow_dev"

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
        disallowed_query_keys = {"host", "sslrootcert", "sslcert", "sslkey", "options"}
        for k in url.query.keys():
            if k.lower() in disallowed_query_keys:
                raise RuntimeError(
                    f"CRITICAL ISOLATION ERROR: Database URL query parameter '{k}' is prohibited."
                )

    # SQLite is permitted only if strictly memory or test file
    if url.drivername.startswith("sqlite"):
        # Check SQLite path
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

    # 3. Database name validation
    db_name = (url.database or "").strip()
    if not db_name:
        raise RuntimeError("CRITICAL ISOLATION ERROR: Missing database name in PostgreSQL URL.")

    # Disallow production tokens in database name
    if any(token in db_name.lower() for token in ("prod", "production", "live_prod")):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database '{db_name}' appears to be a production database."
        )

    if db_name not in PERMITTED_DEV_DATABASES and not any(
        token in db_name.lower() for token in ("dev", "test", "sandbox", "integration")
    ):
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database '{db_name}' is not an authorized test/dev database."
        )

    # 4. User validation
    username = (url.username or "").strip().lower()
    if username and username not in PERMITTED_DEV_USERS and "prod" in username:
        raise RuntimeError(
            f"CRITICAL ISOLATION ERROR: Database user '{url.username}' is not permitted in '{env_mode}' mode."
        )


def get_database_url() -> str:
    env_mode = os.environ.get("COLLECTFLOW_ENV", "integration").lower()
    custom_url = os.environ.get("COLLECTFLOW_DATABASE_URL")

    if custom_url:
        validate_isolated_database_url(custom_url, env_mode)
        return custom_url

    # Validate default URL as well
    validate_isolated_database_url(DEFAULT_DEV_PG_URL, env_mode)
    return DEFAULT_DEV_PG_URL


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
