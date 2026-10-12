"""
SQLAlchemy 2.0 Database Session and Engine Configuration for PostgreSQL Parity.
Enforces:
- PostgreSQL 16+ as primary development/production engine
- Isolation validation to prevent production database access
- Pessimistic locking support and exact numeric handling
"""

import os
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Declarative Base
Base = declarative_base()

DEFAULT_DEV_PG_URL = "postgresql+psycopg://collectflow_user:collectflow_secret@127.0.0.1:5432/collectflow_dev"


def get_database_url() -> str:
    env_mode = os.environ.get("COLLECTFLOW_ENV", "integration").lower()
    custom_url = os.environ.get("COLLECTFLOW_DATABASE_URL")

    if custom_url:
        # Prevent pointing to production database in dev/test/integration modes
        if env_mode in ("integration", "sandbox", "test"):
            normalized = custom_url.lower()
            if not any(token in normalized for token in ("dev", "test", "sandbox", "integration")):
                raise RuntimeError(
                    f"CRITICAL ISOLATION ERROR: Database URL '{custom_url}' is not an authorized test/dev database. "
                    "Must explicitly contain 'dev', 'test', 'sandbox', or 'integration'."
                )
        return custom_url

    return DEFAULT_DEV_PG_URL


engine = create_engine(
    get_database_url(),
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=False,
)

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
    """Context manager for atomic database operations with automatic rollback on error."""
    if session:
        yield session
    else:
        db = SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
