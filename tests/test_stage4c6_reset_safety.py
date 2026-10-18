"""
Unit tests for Stage 4C.6 UAT database reset safety restrictions.

Verifies:
1. Destructive baseline reset strictly requires BOTH COLLECTFLOW_ENV=test and database name 'collectflow_test'.
2. Reset attempts against development ('collectflow_dev'), sandbox ('collectflow_sandbox'),
   integration ('collectflow_integration'), or production databases are strictly rejected.
3. Reset attempts with non-test environment modes ('development', 'sandbox', 'prod', empty) are rejected.
4. Non-loopback database hosts are rejected.
5. Demonstrates safe rejection without executing destructive operations against any database.
"""

import os
from unittest.mock import MagicMock, patch
import pytest

from scripts.seed_postgres_fixtures import seed_fixtures


def test_reset_rejected_when_env_not_test():
    """Verify reset is rejected when COLLECTFLOW_ENV is not 'test'."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_test"
    mock_db.bind.url.host = "127.0.0.1"

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "development"}):
        with patch("scripts.seed_postgres_fixtures.get_db_session") as mock_session_ctx:
            mock_session_ctx.return_value.__enter__.return_value = mock_db
            with pytest.raises(RuntimeError) as exc_info:
                seed_fixtures(reset=True)
            assert "CRITICAL SAFETY ABORT" in str(exc_info.value)
            assert "Requires BOTH COLLECTFLOW_ENV='test'" in str(exc_info.value)
            assert not mock_db.execute.called


def test_reset_rejected_for_development_database():
    """Verify reset is rejected when database is collectflow_dev even if env=test."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_dev"
    mock_db.bind.url.host = "127.0.0.1"

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with patch("scripts.seed_postgres_fixtures.get_db_session") as mock_session_ctx:
            mock_session_ctx.return_value.__enter__.return_value = mock_db
            with pytest.raises(RuntimeError) as exc_info:
                seed_fixtures(reset=True)
            assert "CRITICAL SAFETY ABORT" in str(exc_info.value)
            assert "collectflow_dev" in str(exc_info.value)
            assert not mock_db.execute.called


def test_reset_rejected_for_sandbox_database():
    """Verify reset is rejected when database is collectflow_sandbox."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_sandbox"
    mock_db.bind.url.host = "127.0.0.1"

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "sandbox"}):
        with patch("scripts.seed_postgres_fixtures.get_db_session") as mock_session_ctx:
            mock_session_ctx.return_value.__enter__.return_value = mock_db
            with pytest.raises(RuntimeError) as exc_info:
                seed_fixtures(reset=True)
            assert "CRITICAL SAFETY ABORT" in str(exc_info.value)
            assert "collectflow_sandbox" in str(exc_info.value)
            assert not mock_db.execute.called


def test_reset_rejected_for_integration_database():
    """Verify reset is rejected when database is collectflow_integration."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_integration"
    mock_db.bind.url.host = "localhost"

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "integration"}):
        with patch("scripts.seed_postgres_fixtures.get_db_session") as mock_session_ctx:
            mock_session_ctx.return_value.__enter__.return_value = mock_db
            with pytest.raises(RuntimeError) as exc_info:
                seed_fixtures(reset=True)
            assert "CRITICAL SAFETY ABORT" in str(exc_info.value)
            assert not mock_db.execute.called


def test_reset_rejected_for_production_database():
    """Verify reset is rejected with critical abort when database is production."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.bind.url.host = "127.0.0.1"

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with patch("scripts.seed_postgres_fixtures.get_db_session") as mock_session_ctx:
            mock_session_ctx.return_value.__enter__.return_value = mock_db
            with pytest.raises(RuntimeError) as exc_info:
                seed_fixtures(reset=True)
            assert "CRITICAL SAFETY ABORT" in str(exc_info.value)
            assert not mock_db.execute.called


def test_reset_rejected_for_remote_host():
    """Verify reset is rejected when host is not in loopback set."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_test"
    mock_db.bind.url.host = "192.168.1.50"

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with patch("scripts.seed_postgres_fixtures.get_db_session") as mock_session_ctx:
            mock_session_ctx.return_value.__enter__.return_value = mock_db
            with pytest.raises(RuntimeError) as exc_info:
                seed_fixtures(reset=True)
            assert "CRITICAL SAFETY ABORT" in str(exc_info.value)
            assert "non-loopback host" in str(exc_info.value)
            assert not mock_db.execute.called
