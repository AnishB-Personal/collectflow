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
9. Advisory lock acquired for bootstrap serialization.
10. Real concurrent PostgreSQL integration test with two competing connections (WP03-02).
11. Verification-only mode (--verify-only) performs no writes.
12. Initializer creates no synthetic business records (customers, orders, invoices, etc.).
13. Reference data initialization is DEFERRED by default (WP03-01: no unapproved records inserted).
14. Reference data initialization rejects unapproved / draft configurations (WP03-01).
15. Reference data initialization succeeds with synthetic test fixture when explicitly allowed (WP03-01).
16. Committed synthetic test fixture CANNOT authorize production reference data (WP03-R02).
17. Reference data initialization accepts valid genuine Finance-approved configuration (WP03-R02).
18. Identity validation: pre-existing users without admin fail closed (WP03-03).
19. Identity validation: orphaned sessions fail closed (WP03-03).
20. Identity validation: inactive administrator fails closed (WP03-03).
21. Identity validation: missing bootstrap audit record on repeat run fails closed (WP03-R01).
22. Identity validation: duplicate bootstrap audit records fail closed (WP03-R01).
23. Identity validation: bootstrap audit stable ID mismatch fails closed (WP03-R01).
24. Identity validation: audit trail username mismatch fails closed (WP03-03 / WP03-R01).
25. Identity validation: clean first-time vs legitimate already-initialized distinguished (WP03-R01).
26. Fixture seeding without --reset is rejected outside collectflow_test.
27. Fixture seeding with --reset remains strictly test-only.
28. Remote and production database targets cannot be used for synthetic fixtures.
29. Existing financial test fixtures remain unchanged.
"""

import json
import os
import sys
import threading
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
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
    SessionModel,
    TdsSectionMaster,
    User,
)
from scripts.init_production_db import (
    ADMIN_BOOTSTRAP_LOCK_ID,
    EXPECTED_ALEMBIC_REVISION,
    acquire_bootstrap_advisory_lock,
    bootstrap_administrator,
    get_bootstrap_admin_credentials,
    init_production_database,
    init_reference_data,
    load_reference_config,
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
    mock_db.bind.dialect.name = "postgresql"
    mock_db.scalar.return_value = None

    admin_username = "prod_admin"
    admin_password = "SecureAdminPass2026!#"

    result = bootstrap_administrator(mock_db, admin_username, admin_password, verify_only=False)
    assert result["created"] is True
    assert result["status"] == "bootstrapped"
    assert result["username"] == "prod_admin"

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
    mock_db.bind.dialect.name = "postgresql"
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
    assert ph.verify(added_user.password_hash, admin_password) is True
    assert admin_password not in added_user.password_hash


def test_05_no_plaintext_password_in_logs_or_exceptions():
    """5. No plaintext password appears in audit logs or exceptions."""
    secret_pass = "SuperSecretPassword123!#"

    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
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

    bad_pass = "Weak1!"
    with pytest.raises(ValueError) as exc_info:
        validate_admin_password(bad_pass)
    assert bad_pass not in str(exc_info.value)
    assert "at least 12 characters" in str(exc_info.value)


def test_06_initializer_rejects_missing_or_empty_bootstrap_secrets():
    """6. Initializer rejects missing, empty, or weak bootstrap secrets."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.scalar.return_value = None

    with pytest.raises(ValueError) as exc_info:
        bootstrap_administrator(mock_db, "admin", None)
    assert "No administrator exists, but no bootstrap password was provided" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        validate_admin_password("")
    assert "password cannot be empty" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        validate_admin_password("Short1!Aa")
    assert "at least 12 characters" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        validate_admin_password("AdminPass123!")
    assert "known weak or synthetic test password" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        validate_admin_username("sandbox_admin")
    assert "Prohibited username" in str(exc_info.value)


def test_07_initializer_rejects_unexpected_existing_business_data():
    """7. Initializer refuses to run against a database with existing business data."""
    mock_db = MagicMock()
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION

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
    mock_db.bind.dialect.name = "postgresql"
    existing_user = User(
        id=42,
        username="existing_admin",
        role="admin",
        is_active=True,
        password_hash="$argon2id$existinghash",
        must_change_password=True,
    )
    mock_db.scalar.return_value = existing_user
    audit = AuditLog(id=1, user_id=42, entity_id="42", username="existing_admin", action="ADMIN_BOOTSTRAPPED")
    mock_db.scalars.return_value.all.return_value = [audit]

    result = bootstrap_administrator(mock_db, "existing_admin", "DifferentPass2026!#", verify_only=False)

    assert result["created"] is False
    assert result["status"] == "already_exists"
    assert "already established. Skipping bootstrap (no-op)" in result["message"]
    assert not mock_db.add.called
    assert existing_user.password_hash == "$argon2id$existinghash"


def test_09_advisory_lock_acquired_for_bootstrap():
    """9. Advisory lock pg_advisory_xact_lock is acquired during bootstrap."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.scalar.return_value = None

    bootstrap_administrator(mock_db, "admin", "SecureAdminPass2026!#", verify_only=False)

    lock_called = False
    for call_args in mock_db.execute.call_args_list:
        query_text = str(call_args[0][0])
        if "pg_advisory_xact_lock" in query_text:
            lock_called = True
            break
    assert lock_called, "pg_advisory_xact_lock was not called during bootstrap"


def test_10_real_concurrent_postgresql_bootstrap():
    """10. Real concurrent PostgreSQL integration test with two competing connections (WP03-02)."""
    test_db_name = "collectflow_wp03_concurrency_test"
    admin_conn_url = "postgresql://collectflow_user:collectflow_secret@127.0.0.1:5432/postgres"

    with psycopg.connect(admin_conn_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {test_db_name}")
            cur.execute(f"CREATE DATABASE {test_db_name} OWNER collectflow_user")

    test_dsn = f"postgresql+psycopg://collectflow_user:collectflow_secret@127.0.0.1:5432/{test_db_name}"

    try:
        import subprocess
        mig_env = os.environ.copy()
        mig_env["COLLECTFLOW_ENV"] = "production"
        mig_env["COLLECTFLOW_DATABASE_URL"] = test_dsn
        mig_env["COLLECTFLOW_TRUSTED_CONTAINER_HOSTS"] = "127.0.0.1"
        res = subprocess.run(
            ["./venv/bin/alembic", "upgrade", "head"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=mig_env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"Migration failed on temporary database: {res.stderr}"

        barrier = threading.Barrier(2)
        results = [None, None]
        errors = [None, None]

        def worker(thread_idx, username, password):
            engine = create_engine(test_dsn)
            Session = sessionmaker(bind=engine)
            try:
                barrier.wait(timeout=10)
                with Session() as session:
                    res = bootstrap_administrator(session, username, password, verify_only=False)
                    session.commit()
                    results[thread_idx] = res
            except Exception as e:
                errors[thread_idx] = e
            finally:
                engine.dispose()

        t1 = threading.Thread(target=worker, args=(0, "admin_alpha", "AlphaSecret2026!#"))
        t2 = threading.Thread(target=worker, args=(1, "admin_beta", "BetaSecret2026!#"))

        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert errors[0] is None, f"Thread 1 errored: {errors[0]}"
        assert errors[1] is None, f"Thread 2 errored: {errors[1]}"

        statuses = [r["status"] for r in results if r]
        assert "bootstrapped" in statuses, "Expected at least one bootstrapped result"
        assert "already_exists" in statuses, "Expected at least one already_exists result"
        created_counts = [r["created"] for r in results if r]
        assert created_counts.count(True) == 1, "Exactly one thread must create the administrator"
        assert created_counts.count(False) == 1, "The competing thread must receive created=False"

        engine = create_engine(test_dsn)
        with engine.connect() as conn:
            user_rows = conn.execute(text("SELECT id, username, role, must_change_password FROM users;")).fetchall()
            assert len(user_rows) == 1, f"Expected exactly 1 user in database, found {len(user_rows)}"
            assert user_rows[0][2] == "admin"
            assert user_rows[0][3] is True

            audit_rows = conn.execute(text("SELECT id, action, username FROM audit_logs;")).fetchall()
            assert len(audit_rows) == 1, f"Expected exactly 1 audit log entry, found {len(audit_rows)}"
            assert audit_rows[0][1] == "ADMIN_BOOTSTRAPPED"
            assert audit_rows[0][2] == user_rows[0][1]

        engine.dispose()
    finally:
        with psycopg.connect(admin_conn_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS {test_db_name}")


def test_11_verification_only_mode_performs_no_writes():
    """11. Verification-only mode (--verify-only) performs no writes."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION
    mock_db.scalar.return_value = 0

    admin_result = bootstrap_administrator(mock_db, "admin", "ValidSecurePass2026!#", verify_only=True)
    assert admin_result["created"] is False
    assert admin_result["status"] == "pending_creation"
    assert not mock_db.add.called
    assert not mock_db.flush.called


def test_12_initializer_creates_no_synthetic_business_records():
    """12. Initializer creates zero synthetic customers, orders, invoices, payments, or targets."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.scalar.return_value = None

    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures/approved_reference_config.json")
    config = load_reference_config(fixture_path, allow_test_fixture=True)
    init_reference_data(mock_db, ref_config=config, verify_only=False)

    added_types = {type(call_args[0][0]) for call_args in mock_db.add.call_args_list}
    prohibited_types = {Customer, PurchaseOrder, CommercialInvoice, PaymentTransaction, SalesTarget}
    assert prohibited_types.isdisjoint(added_types), f"Initializer added prohibited business types: {added_types & prohibited_types}"


def test_13_reference_data_deferred_by_default_without_approved_config():
    """13. Reference data initialization is DEFERRED by default (WP03-01: zero unapproved records inserted)."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"

    result = init_reference_data(mock_db, ref_config=None, verify_only=False)
    assert result["status"] == "deferred"
    assert result["financial_years"] == 0
    assert result["gst_rates"] == 0
    assert result["tds_sections"] == 0
    assert not mock_db.add.called
    assert "DEFERRED" in result["message"]
    assert "BPD-01" in result["message"]


def test_14_reference_data_rejects_unapproved_or_draft_config():
    """14. Reference data rejects unapproved or draft configurations (WP03-01)."""
    draft_config = {
        "config_version": "2026.0-draft",
        "approved_by": "Draft Author",
        "approval_status": "draft",
        "approval_reference": "DRAFT-001",
        "approval_date": "2026-04-01",
        "controlled_source": "internal",
        "effective_date": "2026-04-01",
        "policy_references": ["BPD-01"],
        "financial_years": [],
        "gst_rates": [],
        "tds_sections": [],
    }

    tmp_path = "/tmp/collectflow_draft_ref_config.json"
    with open(tmp_path, "w") as f:
        json.dump(draft_config, f)

    try:
        with pytest.raises(ValueError) as exc_info:
            load_reference_config(tmp_path, allow_test_fixture=False)
        assert "not approved by Finance" in str(exc_info.value)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_15_reference_data_initialization_with_synthetic_test_fixture():
    """15. Reference data populates when synthetic test fixture is explicitly allowed (WP03-01 / WP03-R02)."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.scalar.return_value = None

    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures/approved_reference_config.json")
    config = load_reference_config(fixture_path, allow_test_fixture=True)
    assert config is not None
    assert config["is_test_fixture"] is True

    result = init_reference_data(mock_db, ref_config=config, verify_only=False)
    assert result["status"] == "initialized"
    assert result["financial_years"] == 1
    assert result["gst_rates"] == 3
    assert result["tds_sections"] == 2
    assert "Test Harness" in result["approved_by"]


def test_16_committed_test_fixture_rejected_in_production():
    """16. Committed synthetic test fixture CANNOT authorize production reference-data insertion (WP03-R02)."""
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures/approved_reference_config.json")

    # When allow_test_fixture=False (default production behavior), it MUST be rejected
    with pytest.raises(ValueError) as exc_info:
        load_reference_config(fixture_path, allow_test_fixture=False)
    assert "Synthetic test configuration fixture detected ('TEST ONLY — NOT FINANCE APPROVED')" in str(exc_info.value)
    assert "cannot authorize production reference-data insertion" in str(exc_info.value)


def test_17_genuine_production_configuration_validation():
    """17. Production configuration requires formal provenance: reference, approver, date, source (WP03-R02)."""
    genuine_config = {
        "config_version": "2026.1",
        "approval_status": "finance_approved",
        "approval_reference": "FIN-DIR-2026-BPD-01",
        "approved_by": "Finance Directorate - Head of Financial Operations",
        "approval_date": "2026-04-01",
        "controlled_source": "internal-governance://finance/statutory/2026.1",
        "effective_date": "2026-04-01",
        "policy_references": ["BPD-01", "BPD-02"],
        "financial_years": [
            {"code": "FY 2026-27", "start_date": "2026-04-01", "end_date": "2027-03-31", "is_active": True}
        ],
        "gst_rates": [
            {"name": "GST 18%", "rate": "0.1800", "cgst_rate": "0.0900", "sgst_rate": "0.0900", "igst_rate": "0.1800", "is_default": True, "is_active": True}
        ],
        "tds_sections": [
            {"section_code": "194C", "description": "Contractors", "default_rate": "0.0200", "is_active": True}
        ],
    }

    tmp_path = "/tmp/collectflow_genuine_ref_config.json"
    with open(tmp_path, "w") as f:
        json.dump(genuine_config, f)

    try:
        # Passes with allow_test_fixture=False
        parsed = load_reference_config(tmp_path, allow_test_fixture=False)
        assert parsed["approval_reference"] == "FIN-DIR-2026-BPD-01"
        assert parsed["approved_by"] == "Finance Directorate - Head of Financial Operations"
        assert parsed["controlled_source"] == "internal-governance://finance/statutory/2026.1"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_18_identity_validation_rejects_users_without_admin():
    """18. Refuse initialization if non-admin users exist but no admin exists (WP03-03)."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION

    def mock_scalar(query):
        query_str = str(query)
        if "users" in query_str and "role" not in query_str:
            return 3
        if "role" in query_str:
            return 0
        return 0

    mock_db.scalar.side_effect = mock_scalar

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_target_environment(mock_db)
        assert "CRITICAL INITIALIZATION ERROR" in str(exc_info.value)
        assert "no legitimate administrator exists" in str(exc_info.value)


def test_19_identity_validation_rejects_orphaned_sessions():
    """19. Refuse initialization if orphaned sessions exist in empty database (WP03-03)."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION

    def mock_scalar(query):
        query_str = str(query)
        if "sessions" in query_str:
            return 2
        return 0

    mock_db.scalar.side_effect = mock_scalar

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_target_environment(mock_db)
        assert "CRITICAL INITIALIZATION ERROR" in str(exc_info.value)
        assert "Orphaned active sessions detected" in str(exc_info.value)


def test_20_identity_validation_rejects_inactive_admin():
    """20. Refuse initialization if existing administrator is inactive/disabled (WP03-03)."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION

    def mock_scalar(query):
        query_str = str(query)
        if "users" in query_str and "role" not in query_str:
            return 1
        if "role" in query_str:
            return 1
        return 0

    mock_db.scalar.side_effect = mock_scalar
    mock_db.scalars.return_value.all.return_value = []

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_target_environment(mock_db)
        assert "Administrator account exists but is inactive" in str(exc_info.value)


def test_21_identity_validation_rejects_admin_without_bootstrap_audit():
    """21. Refuse repeat initialization if active admin exists but bootstrap audit record is missing (WP03-R01)."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION

    active_admin = User(id=1, username="admin", role="admin", is_active=True)

    def mock_scalars(query):
        query_str = str(query)
        res = MagicMock()
        if "users" in query_str:
            res.all.return_value = [active_admin]
        elif "audit_logs" in query_str:
            res.all.return_value = []  # No bootstrap audit!
        else:
            res.all.return_value = []
        return res

    def mock_scalar(query):
        return 1 if "users" in str(query) else 0

    mock_db.scalar.side_effect = mock_scalar

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_target_environment(mock_db)
        assert "required bootstrap provenance audit evidence (action='ADMIN_BOOTSTRAPPED') is completely missing" in str(exc_info.value)


def test_22_identity_validation_rejects_duplicate_bootstrap_audits():
    """22. Refuse initialization if ambiguous/duplicate bootstrap audit records exist (WP03-R01)."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION

    active_admin = User(id=1, username="admin", role="admin", is_active=True)
    audits = [
        AuditLog(id=1, user_id=1, entity_id="1", username="admin", action="ADMIN_BOOTSTRAPPED"),
        AuditLog(id=2, user_id=1, entity_id="1", username="admin", action="ADMIN_BOOTSTRAPPED"),
    ]

    def mock_scalars(query):
        query_str = str(query)
        res = MagicMock()
        if "users" in query_str:
            res.all.return_value = [active_admin]
        elif "audit_logs" in query_str:
            res.all.return_value = audits
        else:
            res.all.return_value = []
        return res

    def mock_scalar(query):
        return 1 if "users" in str(query) else 0

    mock_db.scalars.side_effect = mock_scalars
    mock_db.scalar.side_effect = mock_scalar

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_target_environment(mock_db)
        assert "Ambiguous or duplicate bootstrap audit records detected (2 records)" in str(exc_info.value)


def test_23_identity_validation_rejects_bootstrap_audit_id_mismatch():
    """23. Refuse initialization if bootstrap audit stable ID does not match active admin ID (WP03-R01)."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION

    active_admin = User(id=99, username="admin", role="admin", is_active=True)
    audit = AuditLog(id=1, user_id=1, entity_id="1", username="admin", action="ADMIN_BOOTSTRAPPED")

    def mock_scalars(query):
        query_str = str(query)
        res = MagicMock()
        if "users" in query_str:
            res.all.return_value = [active_admin]
        elif "audit_logs" in query_str:
            res.all.return_value = [audit]
        else:
            res.all.return_value = []
        return res

    def mock_scalar(query):
        return 1 if "users" in str(query) else 0

    mock_db.scalars.side_effect = mock_scalars
    mock_db.scalar.side_effect = mock_scalar

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_target_environment(mock_db)
        assert "Bootstrap provenance mismatch" in str(exc_info.value)


def test_24_identity_validation_detects_audit_trail_username_mismatch():
    """24. Detect bootstrap audit trail username/role conflict on repeat execution (WP03-03 / WP03-R01)."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION

    active_admin = User(id=1, username="different_admin", role="admin", is_active=True)
    audit = AuditLog(id=1, user_id=1, entity_id="1", username="original_admin", action="ADMIN_BOOTSTRAPPED")

    def mock_scalars(query):
        query_str = str(query)
        res = MagicMock()
        if "users" in query_str:
            res.all.return_value = [active_admin]
        elif "audit_logs" in query_str:
            res.all.return_value = [audit]
        else:
            res.all.return_value = []
        return res

    def mock_scalar(query):
        return 1 if "users" in str(query) else 0

    mock_db.scalars.side_effect = mock_scalars
    mock_db.scalar.side_effect = mock_scalar

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_target_environment(mock_db)
        assert "Bootstrap provenance identifier conflict" in str(exc_info.value)


def test_25_identity_validation_distinguishes_first_time_from_initialized():
    """25. Distinguish clean first-time initialization from legitimate already initialized (WP03-R01)."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.bind.url.database = "collectflow_prod"
    mock_db.execute.return_value.scalar.return_value = EXPECTED_ALEMBIC_REVISION

    # Clean first-time
    mock_db.scalar.return_value = 0
    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        status = verify_target_environment(mock_db)
        assert status["identity_state"] == "clean_first_time"

    # Already initialized with matching provenance
    active_admin = User(id=1, username="admin", role="admin", is_active=True)
    audit = AuditLog(id=1, user_id=1, entity_id="1", username="admin", action="ADMIN_BOOTSTRAPPED")

    def mock_scalars(query):
        query_str = str(query)
        res = MagicMock()
        if "users" in query_str:
            res.all.return_value = [active_admin]
        elif "audit_logs" in query_str:
            res.all.return_value = [audit]
        else:
            res.all.return_value = []
        return res

    def mock_scalar(query):
        return 1 if "users" in str(query) else 0

    mock_db.scalars.side_effect = mock_scalars
    mock_db.scalar.side_effect = mock_scalar

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        status = verify_target_environment(mock_db)
        assert status["identity_state"] == "already_initialized"


def test_26_fixture_seeding_without_reset_rejected_outside_test_env():
    """26. Fixture seeding without --reset is rejected outside collectflow_test."""
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


def test_27_fixture_seeding_with_reset_strictly_test_only():
    """27. Fixture seeding with --reset remains strictly test-only."""
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


def test_28_remote_and_production_targets_cannot_be_used_for_fixtures():
    """28. Remote host and production database targets cannot be used for synthetic fixtures."""
    remote_db = MagicMock()
    remote_db.bind.url.database = "collectflow_test"
    remote_db.bind.url.host = "10.0.1.20"
    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_seeder_safety(remote_db)
        assert "CRITICAL SAFETY ABORT" in str(exc_info.value)
        assert "non-loopback host" in str(exc_info.value)

    prod_db = MagicMock()
    prod_db.bind.url.database = "collectflow_production"
    prod_db.bind.url.host = "127.0.0.1"
    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_seeder_safety(prod_db)
        assert "CRITICAL SAFETY ABORT" in str(exc_info.value)

    user_db = MagicMock()
    user_db.bind.url.database = "collectflow_test"
    user_db.bind.url.host = "127.0.0.1"
    user_db.bind.url.username = "prod_db_admin"
    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with pytest.raises(RuntimeError) as exc_info:
            verify_seeder_safety(user_db)
        assert "CRITICAL SAFETY ABORT" in str(exc_info.value)


def test_29_existing_financial_test_fixtures_remain_unchanged():
    """29. Existing financial test fixtures remain valid and unchanged."""
    with get_db_session() as db:
        customers = db.scalars(select(Customer)).all()
        assert len(customers) > 0, "Expected existing customers in test database"

        fy = db.scalar(select(FinancialYear).where(FinancialYear.code == "FY 2026-27"))
        assert fy is not None
        assert fy.is_active is True

        gst18 = db.scalar(select(GstRateMaster).where(GstRateMaster.name == "GST 18%"))
        assert gst18 is not None
        assert gst18.rate == Decimal("0.1800")
        assert gst18.is_default is True
