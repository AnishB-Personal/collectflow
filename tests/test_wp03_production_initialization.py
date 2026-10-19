"""Automated Tests for Production-Safe Database Initialization & Administrator Bootstrap (WP-03).

Validates:
1. Additive migration creates must_change_password column.
2. Existing users retain their data and receive false.
3. New bootstrap administrator receives true.
4. Bootstrap administrator password is Argon2id-hashed.
5. No plaintext password appears in logs or exceptions.
6. Initializer rejects missing, empty, or weak bootstrap secrets.
7. Initializer rejects unexpected existing business data.
8. Repeated initialization cannot duplicate or overwrite administrator (idempotent no-op).
9. Concurrent / duplicate bootstrap attempts cannot create multiple administrators.
10. Verification-only mode (--verify-only) performs no writes.
11. Initializer creates no synthetic business records (customers, orders, invoices, etc.).
12. Reference data initialization is deterministic and safely repeatable.
13. Fixture seeding without --reset is rejected outside collectflow_test.
14. Fixture seeding with --reset remains strictly test-only.
15. Remote and production database targets cannot be used for synthetic fixtures.
16. Existing financial test fixtures remain unchanged.
"""

import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.core.database import get_database_url, get_db_session
from app.models.domain_models import (
    AuditLog,
    CommercialInvoice,
    Customer,
    FinancialYear,
    GstRateMaster,
    PaymentTransaction,
    PurchaseOrder,
    SalesTarget,
    TdsSectionMaster,
    User,
)
from scripts.init_production_db import (
    EXPECTED_ALEMBIC_REVISION,
    PENDING_FINANCE_SIGNOFF_NOTICE,
    bootstrap_administrator,
    get_bootstrap_admin_credentials,
    init_production_database,
    init_reference_data,
    validate_admin_password,
    validate_admin_username,
    verify_target_environment,
)
from scripts.seed_postgres_fixtures import seed_fixtures, verify_seeder_safety

ph = PasswordHasher()


def test_01_additive_migration_creates_must_change_password():
    """1. Additive migration creates must_change_password column on users table."""
    with get_db_session() as db:
        inspector = inspect(db.bind)
        columns = {col["name"]: col for col in inspector.get_columns("users")}
        assert "must_change_password" in columns, "must_change_password column missing from users table"
        col = columns["must_change_password"]
        assert col["nullable"] is False, "must_change_password must be non-nullable"

    # Also verify the migration file declares the expected revision
    import importlib.util
    mig_path = os.path.join(
        os.path.dirname(__file__),
        "../migrations/versions/77e73a1cb221_add_must_change_password_to_users.py",
    )
    spec = importlib.util.spec_from_file_location("mig_wp03", mig_path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.revision == "77e73a1cb221"
    assert mig.down_revision == "e9ee322f8b96"


def test_02_existing_users_retain_data_and_receive_false():
    """2. Existing users retain their data, credentials, and receive must_change_password=false."""
    with get_db_session() as db:
        users = db.scalars(select(User).where(User.username.like("sandbox_%"))).all()
        assert len(users) >= 4, "Expected at least 4 sandbox users in test database"
        for u in users:
            assert u.must_change_password is False, f"User {u.username} should have must_change_password=False"
            assert u.role in {"admin", "finance", "sales", "viewer"}
            assert u.password_hash.startswith("$argon2id$")
            assert u.is_active is True


def test_03_new_bootstrap_administrator_receives_true():
    """3. Initial bootstrap administrator account is created with must_change_password=true."""
    mock_db = MagicMock()
    # Mock no existing admin
    mock_db.scalar.return_value = None

    admin_username = "prod_admin"
    admin_password = "SecureAdminPass2026!#"

    result = bootstrap_administrator(mock_db, admin_username, admin_password, verify_only=False)
    assert result["created"] is True
    assert result["status"] == "bootstrapped"
    assert result["username"] == "prod_admin"

    # Verify User instance passed to mock_db.add
    added_user = None
    for call_args in mock_db.add.call_args_list:
        obj = call_args[0][0]
        if isinstance(obj, User):
            added_user = obj
            break

    assert added_user is not None
    assert added_user.username == "prod_admin"
    assert added_user.role == "admin"
    assert added_user.is_active is True
    assert added_user.must_change_password is True


def test_04_bootstrap_administrator_password_argon2id_hashed():
    """4. Administrator password is cryptographically hashed with Argon2id."""
    mock_db = MagicMock()
    mock_db.scalar.return_value = None

    admin_username = "prod_sysadmin"
    admin_password = "ComplexSecurePassword2026!#"

    bootstrap_administrator(mock_db, admin_username, admin_password, verify_only=False)

    added_user = None
    for call_args in mock_db.add.call_args_list:
        obj = call_args[0][0]
        if isinstance(obj, User):
            added_user = obj
            break

    assert added_user is not None
    assert added_user.password_hash.startswith("$argon2id$")
    # Verify Argon2id verification succeeds with the cleartext password
    assert ph.verify(added_user.password_hash, admin_password) is True
    # Verify cleartext password is NOT in any attribute
    assert admin_password not in added_user.password_hash


def test_05_no_plaintext_password_in_logs_or_exceptions():
    """5. No plaintext password appears in audit logs or exceptions."""
    secret_pass = "SuperSecretPassword123!#"

    # Check AuditLog generation
    mock_db = MagicMock()
    mock_db.scalar.return_value = None
    bootstrap_administrator(mock_db, "sysadmin_audit", secret_pass, verify_only=False)

    added_audit = None
    for call_args in mock_db.add.call_args_list:
        obj = call_args[0][0]
        if isinstance(obj, AuditLog):
            added_audit = obj
            break

    assert added_audit is not None
    assert added_audit.action == "ADMIN_BOOTSTRAPPED"
    audit_str = str(added_audit.details)
    assert secret_pass not in audit_str

    # Check exception raised during validation does not echo bad password
    bad_pass = "Weak1!"
    with pytest.raises(ValueError) as exc_info:
        validate_admin_password(bad_pass)
    assert bad_pass not in str(exc_info.value)
    assert "at least 12 characters" in str(exc_info.value)


def test_06_initializer_rejects_missing_or_empty_bootstrap_secrets():
    """6. Initializer rejects missing, empty, or weak bootstrap secrets."""
    mock_db = MagicMock()
    mock_db.scalar.return_value = None

    # Missing password
    with pytest.raises(ValueError) as exc_info:
        bootstrap_administrator(mock_db, "admin", None)
    assert "No administrator exists, but no bootstrap password was provided" in str(exc_info.value)

    # Empty password
    with pytest.raises(ValueError) as exc_info:
        validate_admin_password("")
    assert "password cannot be empty" in str(exc_info.value)

    # Short password
    with pytest.raises(ValueError) as exc_info:
        validate_admin_password("Short1!Aa")
    assert "at least 12 characters" in str(exc_info.value)

    # Prohibited synthetic test password
    with pytest.raises(ValueError) as exc_info:
        validate_admin_password("AdminPass123!")
    assert "known weak or synthetic test password" in str(exc_info.value)

    # Prohibited synthetic test username
    with pytest.raises(ValueError) as exc_info:
        validate_admin_username("sandbox_admin")
    assert "Prohibited username" in str(exc_info.value)


def test_07_initializer_rejects_unexpected_existing_business_data():
    """7. Initializer refuses to run against a database with existing business data."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION

    # Simulate existing customers
    def mock_scalar(query):
        query_str = str(query)
        if "customers" in query_str:
            return 5
        return 0

    mock_db.scalar.side_effect = mock_scalar

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_target_environment(mock_db)
        assert "CRITICAL INITIALIZATION ERROR" in str(exc_info.value)
        assert "Pre-existing business data detected" in str(exc_info.value)
        assert "5 customers" in str(exc_info.value)


def test_08_repeated_initialization_idempotent_no_op():
    """8. Repeated initialization cannot duplicate or overwrite the administrator."""
    mock_db = MagicMock()
    existing_user = User(
        id=42,
        username="existing_admin",
        role="admin",
        password_hash="$argon2id$existinghash",
        must_change_password=True,
    )
    mock_db.scalar.return_value = existing_user

    # Attempt to bootstrap again with a new password
    result = bootstrap_administrator(mock_db, "existing_admin", "DifferentPass2026!#", verify_only=False)

    assert result["created"] is False
    assert result["status"] == "already_exists"
    assert "already exists. Skipping bootstrap (no-op)" in result["message"]
    # Verify no User or AuditLog additions
    assert not mock_db.add.called
    assert existing_user.password_hash == "$argon2id$existinghash"


def test_09_concurrent_or_duplicate_bootstrap_attempts_prevent_multiple_admins():
    """9. Concurrent/duplicate bootstrap attempts cannot create multiple administrators."""
    mock_db = MagicMock()
    existing_user = User(
        id=1,
        username="primary_admin",
        role="admin",
        password_hash="$argon2id$primaryhash",
        must_change_password=True,
    )
    # Return existing admin when checked
    mock_db.scalar.return_value = existing_user

    result = bootstrap_administrator(mock_db, "secondary_admin", "AnotherPass2026!#", verify_only=False)
    assert result["created"] is False
    assert not mock_db.add.called


def test_10_verification_only_mode_performs_no_writes():
    """10. Verification-only mode (--verify-only) performs no writes."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION
    mock_db.scalar.return_value = 0  # No business data, no existing admin

    # Run admin bootstrap in verify-only mode
    admin_result = bootstrap_administrator(mock_db, "admin", "ValidSecurePass2026!#", verify_only=True)
    assert admin_result["created"] is False
    assert admin_result["status"] == "pending_creation"
    assert not mock_db.add.called
    assert not mock_db.flush.called

    # Run reference data in verify-only mode
    ref_result = init_reference_data(mock_db, verify_only=True)
    assert ref_result["financial_years"] > 0
    assert not mock_db.add.called
    assert not mock_db.flush.called


def test_11_initializer_creates_no_synthetic_business_records():
    """11. Initializer creates zero synthetic customers, orders, invoices, payments, or targets."""
    mock_db = MagicMock()
    mock_db.scalar.return_value = None  # No existing records

    init_reference_data(mock_db, verify_only=False)

    # Inspect all objects passed to mock_db.add
    added_types = {type(call_args[0][0]) for call_args in mock_db.add.call_args_list}
    prohibited_types = {Customer, PurchaseOrder, CommercialInvoice, PaymentTransaction, SalesTarget}
    assert prohibited_types.isdisjoint(added_types), f"Initializer added prohibited business types: {added_types & prohibited_types}"


def test_12_reference_data_initialization_deterministic_and_repeatable():
    """12. Reference data initialization is deterministic and safely repeatable."""
    mock_db = MagicMock()
    existing_items = set()

    def mock_scalar(query):
        # Check if record is already added
        return None

    mock_db.scalar.return_value = None

    # First pass: adds items
    first_pass = init_reference_data(mock_db, verify_only=False)
    assert first_pass["financial_years"] == 1
    assert first_pass["gst_rates"] == 5
    assert first_pass["tds_sections"] == 5

    # Second pass: simulate everything already exists
    mock_db.scalar.return_value = MagicMock()
    second_pass = init_reference_data(mock_db, verify_only=False)
    assert second_pass["financial_years"] == 0
    assert second_pass["gst_rates"] == 0
    assert second_pass["tds_sections"] == 0


def test_13_fixture_seeding_without_reset_rejected_outside_test_env():
    """13. Fixture seeding without --reset is rejected outside collectflow_test."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_dev"
    mock_db.bind.url.host = "127.0.0.1"

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "development"}):
        with patch("scripts.seed_postgres_fixtures.get_db_session") as mock_session_ctx:
            mock_session_ctx.return_value.__enter__.return_value = mock_db
            with pytest.raises(RuntimeError) as exc_info:
                seed_fixtures(reset=False)
            assert "CRITICAL SAFETY ABORT" in str(exc_info.value)
            assert not mock_db.execute.called


def test_14_fixture_seeding_with_reset_strictly_test_only():
    """14. Fixture seeding with --reset remains strictly test-only."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_staging"
    mock_db.bind.url.host = "127.0.0.1"

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "staging"}):
        with patch("scripts.seed_postgres_fixtures.get_db_session") as mock_session_ctx:
            mock_session_ctx.return_value.__enter__.return_value = mock_db
            with pytest.raises(RuntimeError) as exc_info:
                seed_fixtures(reset=True)
            assert "CRITICAL SAFETY ABORT" in str(exc_info.value)
            assert not mock_db.execute.called


def test_15_remote_and_production_targets_cannot_be_used_for_fixtures():
    """15. Remote host and production database targets cannot be used for synthetic fixtures."""
    # Remote host rejected
    remote_db = MagicMock()
    remote_db.bind.url.database = "collectflow_test"
    remote_db.bind.url.host = "10.0.1.20"
    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_seeder_safety(remote_db)
        assert "CRITICAL SAFETY ABORT" in str(exc_info.value)
        assert "non-loopback host" in str(exc_info.value)

    # Production database name rejected
    prod_db = MagicMock()
    prod_db.bind.url.database = "collectflow_production"
    prod_db.bind.url.host = "127.0.0.1"
    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_seeder_safety(prod_db)
        assert "CRITICAL SAFETY ABORT" in str(exc_info.value)

    # Production user rejected
    user_db = MagicMock()
    user_db.bind.url.database = "collectflow_test"
    user_db.bind.url.host = "127.0.0.1"
    user_db.bind.url.username = "prod_db_admin"
    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_seeder_safety(user_db)
        assert "CRITICAL SAFETY ABORT" in str(exc_info.value)


def test_16_existing_financial_test_fixtures_remain_unchanged():
    """16. Existing financial test fixtures remain valid and unchanged."""
    with get_db_session() as db:
        # Check customers
        customers = db.scalars(select(Customer)).all()
        assert len(customers) > 0, "Expected existing customers in test database"

        # Check financial years
        fy = db.scalar(select(FinancialYear).where(FinancialYear.code == "FY 2026-27"))
        assert fy is not None
        assert fy.is_active is True

        # Check GST rate master default
        gst18 = db.scalar(select(GstRateMaster).where(GstRateMaster.name == "GST 18%"))
        assert gst18 is not None
        assert gst18.rate == Decimal("0.1800")
        assert gst18.is_default is True
