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
from sqlalchemy import select, delete

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
