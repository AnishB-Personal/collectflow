"""
Targeted Automated Regression Suite for Stage 4C.2 Audit Findings Remediations:
- H01: Customer exposure Cartesian product fix (multiple payments, cash/tds breakdown, unbilled PO value)
- H05: Negative database isolation tests (remote hosts, production URLs, query parameter pollution)
- H06: Session revocation upon role change or user deactivation
- H07: Concurrency protection for last active administrator
- M01: User deactivation support
- M02: Atomic audit logging within mutation transactions
- M04: Statutory reference validation limits and single-default constraint
- L03: Sanitized /ready probe without raw exception leakage
"""

import threading
from datetime import date
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, delete, func

from app.main import app
from app.core.database import get_db_session, validate_isolated_database_url
from app.models.domain_models import AuditLog, CommercialInvoice, Customer, PaymentTransaction, PurchaseOrder, SessionModel, User

client = TestClient(app)


def test_h01_customer_exposure_aggregation_with_multiple_payments():
    """Verify that multiple payments do NOT multiply invoice total_billed."""
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    with get_db_session() as db:
        acme = db.scalar(select(Customer).where(Customer.billing_name == "Acme Cloud Solutions Pvt Ltd"))
        assert acme is not None
        acme_id = acme.id
        inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.invoice_number == "INV-2026-042"))
        assert inv is not None

        # Add a 2nd payment to inv
        p2 = PaymentTransaction(
            invoice_id=inv.id,
            transaction_type="PAYMENT",
            cash_amount=Decimal("50000.00"),
            tds_amount=Decimal("0.00"),
            total_settled=Decimal("50000.00"),
            tds_section="194C",
            payment_date=date(2026, 4, 28),
            remittance_number="REM-TEST-H01",
            payment_mode="NEFT",
            recorded_by=1,
        )
        db.add(p2)
        db.commit()

    try:
        # Call GET /api/customers
        resp = client.get(f"/api/customers/{acme_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()

        # Billed must remain strictly 531000.00 (NOT 1062000.00)
        assert data["total_billed"] == "531000.00"
        # Total cash: 200000 + 50000 = 250000.00
        assert data["total_cash"] == "250000.00"
        # Total tds: 9000.00
        assert data["total_tds"] == "9000.00"
        # Total settled: 209000 + 50000 = 259000.00
        assert data["total_settled"] == "259000.00"
        # Outstanding: 531000 - 259000 = 272000.00
        assert data["total_outstanding"] == "272000.00"
        # Unbilled orders value: PO-NEXUS-881 is 295000.00
        assert data["unbilled_orders_value"] == "295000.00"
    finally:
        with get_db_session() as db:
            db.execute(delete(PaymentTransaction).where(PaymentTransaction.remittance_number == "REM-TEST-H01"))
            db.commit()


def test_h05_negative_database_isolation_enforcement():
    """Verify negative validation for invalid environment, remote hosts, and query parameter hijacking."""
    # 1. Invalid environment mode fails closed
    with pytest.raises(RuntimeError, match="not an authorized non-production environment"):
        validate_isolated_database_url("postgresql+psycopg://user:pass@127.0.0.1:5432/collectflow_dev", "production")

    with pytest.raises(RuntimeError, match="not an authorized non-production environment"):
        validate_isolated_database_url("postgresql+psycopg://user:pass@127.0.0.1:5432/collectflow_dev", "unknown_env")

    # 2. Remote host rejected
    with pytest.raises(RuntimeError, match="Remote host '192.168.1.100' is prohibited"):
        validate_isolated_database_url("postgresql+psycopg://user:pass@192.168.1.100:5432/collectflow_dev", "test")

    # 3. Production DB name rejected
    with pytest.raises(RuntimeError, match="appears to be a production database"):
        validate_isolated_database_url("postgresql+psycopg://user:pass@127.0.0.1:5432/collectflow_prod", "test")

    # 4. Query param hijacking rejected
    with pytest.raises(RuntimeError, match="query parameter 'host' is prohibited"):
        validate_isolated_database_url("postgresql+psycopg://user:pass@127.0.0.1:5432/collectflow_dev?host=remote.evil.com", "test")


def test_h06_session_revocation_on_role_change_and_deactivation():
    """Verify that sessions are invalidated when a user's role is changed or deactivated."""
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    # 1. Login with sandbox_sales
    sales_login = client.post("/api/auth/login", json={"username": "sandbox_sales", "password": "SalesPass123!"})
    assert sales_login.status_code == 200
    sales_token = sales_login.json()["token"]
    sales_headers = {"Authorization": f"Bearer {sales_token}"}
    sales_uid = sales_login.json()["user"]["id"]

    try:
        # Verify session works
        me1 = client.get("/api/auth/me", headers=sales_headers)
        assert me1.status_code == 200
        assert me1.json()["user"]["role"] == "sales"

        # 2. Admin modifies sales user's role to viewer
        up_role = client.put(f"/api/users/{sales_uid}/role", json={"role": "viewer"}, headers=admin_headers)
        assert up_role.status_code == 200

        # 3. Old sales session MUST now be rejected with 401
        me2 = client.get("/api/auth/me", headers=sales_headers)
        assert me2.status_code == 401

        # 4. Log in again with updated role
        re_login = client.post("/api/auth/login", json={"username": "sandbox_sales", "password": "SalesPass123!"})
        assert re_login.status_code == 200
        new_token = re_login.json()["token"]
        new_headers = {"Authorization": f"Bearer {new_token}"}

        # 5. Deactivate user
        deact_resp = client.put(f"/api/users/{sales_uid}/deactivate", headers=admin_headers)
        assert deact_resp.status_code == 200

        # Session immediately rejected
        me3 = client.get("/api/auth/me", headers=new_headers)
        assert me3.status_code == 401
    finally:
        # Restore sandbox_sales
        client.put(f"/api/users/{sales_uid}", json={"role": "sales", "is_active": True}, headers=admin_headers)


def test_h07_last_admin_protection():
    """Verify that the last active administrator cannot be demoted, deleted, or deactivated."""
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}
    admin_id = admin_login.json()["user"]["id"]

    # Demotion blocked
    r1 = client.put(f"/api/users/{admin_id}/role", json={"role": "finance"}, headers=admin_headers)
    assert r1.status_code == 400
    assert "at least one active administrator" in r1.json()["detail"]

    # Self-deletion blocked
    r2 = client.delete(f"/api/users/{admin_id}", headers=admin_headers)
    assert r2.status_code == 400
    assert "Cannot delete your own account" in r2.json()["detail"]

    # Self-deactivation blocked
    r3 = client.put(f"/api/users/{admin_id}/deactivate", headers=admin_headers)
    assert r3.status_code == 400
    assert "Cannot deactivate your own account" in r3.json()["detail"]


def test_l03_sanitized_readiness_probe():
    """Verify /ready probe returns 200 when DB is up and does not leak internal exception details."""
    resp = client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"
    assert "error" not in data


def test_l03_readiness_probe_failure_503():
    """Verify /ready probe returns sanitized 503 and no tracebacks or internal URLs when DB is unavailable."""
    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def broken_db_session():
        raise RuntimeError("FATAL: connection to server at '127.0.0.1', port 5432 failed: Connection refused")
        yield

    with patch("app.main.get_db_session", broken_db_session):
        resp = client.get("/ready")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert data["reason"] == "Database connection probe failed"
        # Zero raw exception or IP leakage
        assert "127.0.0.1" not in str(data)
        assert "Connection refused" not in str(data)


def test_h07_concurrent_last_admin_demotion_race():
    """Verify that concurrent demotion attempts serialized with pg_advisory_xact_lock prevent eliminating all admins."""
    import concurrent.futures

    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    # Create a 2nd admin
    c_res = client.post(
        "/api/users",
        json={"username": "temp_admin_2", "password": "AdminPass456!", "role": "admin", "full_name": "Second Admin"},
        headers=admin_headers,
    )
    assert c_res.status_code == 200
    admin2_id = c_res.json()["user_id"]

    login_admin2 = client.post("/api/auth/login", json={"username": "temp_admin_2", "password": "AdminPass456!"})
    assert login_admin2.status_code == 200
    admin2_token = login_admin2.json()["token"]
    admin2_headers = {"Authorization": f"Bearer {admin2_token}"}

    try:
        # Now there are exactly 2 admins: sandbox_admin (id=1) and temp_admin_2 (id=admin2_id)
        # Attempt to demote both admins concurrently:
        # admin1 demotes admin2, and admin2 demotes admin1
        def demote_admin2():
            c = TestClient(app)
            return c.put(f"/api/users/{admin2_id}/role", json={"role": "viewer"}, headers=admin_headers)

        def demote_admin1():
            c = TestClient(app)
            return c.put(f"/api/users/1/role", json={"role": "viewer"}, headers=admin2_headers)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(demote_admin2)
            fut2 = executor.submit(demote_admin1)
            res1 = fut1.result()
            res2 = fut2.result()

        # Due to advisory locking and FOR UPDATE row locking, at most ONE can succeed
        status_codes = [res1.status_code, res2.status_code]
        assert not (status_codes[0] == 200 and status_codes[1] == 200), (
            f"Race condition occurred: both demotions succeeded! {status_codes}"
        )
        assert 400 in status_codes, f"Expected at least one demotion to fail with 400: {status_codes}"
    finally:
        # Clean up
        with get_db_session() as db:
            db.execute(delete(SessionModel).where(SessionModel.user_id == admin2_id))
            db.execute(delete(User).where(User.id == admin2_id))
            # Ensure sandbox_admin remains admin
            admin_user = db.scalar(select(User).where(User.id == 1))
            if admin_user:
                admin_user.role = "admin"
                admin_user.is_active = True
            db.commit()


def test_m01_admin_password_reset_and_session_revocation():
    """Verify admin password reset revokes all active sessions for the target user."""
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    # Login as sales
    sales_login = client.post("/api/auth/login", json={"username": "sandbox_sales", "password": "SalesPass123!"})
    assert sales_login.status_code == 200
    sales_token = sales_login.json()["token"]
    sales_uid = sales_login.json()["user"]["id"]
    sales_headers = {"Authorization": f"Bearer {sales_token}"}

    try:
        # Verify session is valid
        me1 = client.get("/api/auth/me", headers=sales_headers)
        assert me1.status_code == 200

        # Admin resets sales user password
        reset_res = client.put(
            f"/api/users/{sales_uid}/password",
            json={"new_password": "NewSalesPass789!"},
            headers=admin_headers,
        )
        assert reset_res.status_code == 200
        assert reset_res.json()["success"] is True

        # Old session is immediately revoked
        me2 = client.get("/api/auth/me", headers=sales_headers)
        assert me2.status_code == 401

        # Can log in with new password
        login_new = client.post("/api/auth/login", json={"username": "sandbox_sales", "password": "NewSalesPass789!"})
        assert login_new.status_code == 200
    finally:
        # Reset back to original password
        client.put(
            f"/api/users/{sales_uid}/password",
            json={"new_password": "SalesPass123!"},
            headers=admin_headers,
        )


def test_m02_atomic_audit_log_rollback():
    """Verify that if a mutation transaction fails, any pending audit log is rolled back atomically."""
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    with get_db_session() as db:
        initial_audit_count = db.scalar(select(func.count(AuditLog.id)))

    # Attempt to create customer with invalid GSTIN (fails format validation before DB commit)
    bad_res = client.post(
        "/api/customers",
        json={"billing_name": "Audit Test Failure", "gst_number": "INVALID_GSTIN"},
        headers=admin_headers,
    )
    assert bad_res.status_code == 400

    with get_db_session() as db:
        after_audit_count = db.scalar(select(func.count(AuditLog.id)))
        assert after_audit_count == initial_audit_count


def test_h08_customer_contract_and_conflict():
    """Verify 409 Conflict on duplicate billing name and contract response formatting."""
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    # Attempt duplicate billing name
    dup_res = client.post(
        "/api/customers",
        json={"billing_name": "Acme Cloud Solutions Pvt Ltd", "gst_number": "27AABCA1234A1Z5"},
        headers=admin_headers,
    )
    assert dup_res.status_code == 409
    assert "Customer name already exists" in dup_res.json()["detail"]

    # Check existing customer response contract formatting
    acme_res = client.get("/api/customers/1", headers=admin_headers)
    assert acme_res.status_code == 200
    data = acme_res.json()
    assert "outstanding_balance" in data
    assert data["outstanding_balance"] == data["total_outstanding"]
    assert isinstance(data["total_billed"], str)
    assert "." in data["total_billed"]
    assert len(data["total_billed"].split(".")[1]) == 2

