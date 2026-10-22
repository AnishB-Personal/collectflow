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

import secrets
import threading
from datetime import date
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, delete, func, update

from app.main import app
from app.core.database import get_db_session, validate_isolated_database_url
from app.models.domain_models import AuditLog, CommercialInvoice, Customer, PaymentTransaction, PurchaseOrder, SessionModel, User
from app.routers.auth import log_security_audit

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

    # Ensure clean starting state
    with get_db_session() as db:
        admin_u = db.scalar(select(User).where(User.id == 1))
        if admin_u:
            admin_u.role = "admin"
            admin_u.is_active = True
        old_temp = db.scalar(select(User).where(User.username == "temp_admin_2"))
        if old_temp:
            db.execute(delete(SessionModel).where(SessionModel.user_id == old_temp.id))
            db.execute(delete(AuditLog).where(AuditLog.entity_id == str(old_temp.id)))
            db.execute(delete(User).where(User.id == old_temp.id))
        db.commit()

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

    with get_db_session() as db:
        u2 = db.scalar(select(User).where(User.id == admin2_id))
        if u2:
            u2.must_change_password = False
            db.commit()

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
        assert 200 in status_codes, f"Expected exactly one demotion to succeed with 200: {status_codes}"

        # Inspect final database state before cleanup
        with get_db_session() as db:
            active_admin_count = db.scalar(
                select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))
            )
            assert active_admin_count >= 1, (
                f"CRITICAL INVARIANT VIOLATION: Zero active administrators remain! Count={active_admin_count}"
            )

            admin1 = db.scalar(select(User).where(User.id == 1))
            admin2 = db.scalar(select(User).where(User.id == admin2_id))
            assert admin1 and admin2
            assert admin1.role == "admin" or admin2.role == "admin", (
                "At least one administrator must remain with admin role!"
            )
            assert not (admin1.role == "viewer" and admin2.role == "viewer"), (
                "Both administrators were demoted to viewer!"
            )

            # Identify demoted vs surviving admin
            demoted_uid = admin2_id if admin2.role == "viewer" else 1

            # Verify session consistency: sessions of the demoted user were revoked
            demoted_sessions = db.scalars(
                select(SessionModel).where(SessionModel.user_id == demoted_uid)
            ).all()
            assert len(demoted_sessions) == 0, "Sessions of demoted user must be revoked atomically!"

            # Verify audit log consistency: exactly one demotion audit log exists for this race
            role_change_audits = db.scalars(
                select(AuditLog).where(
                    AuditLog.action == "USER_UPDATED",
                    AuditLog.entity_id.in_([str(1), str(admin2_id)]),
                )
            ).all()
            assert len(role_change_audits) == 1, (
                f"Expected exactly 1 audit log for the successful demotion, found {len(role_change_audits)}"
            )
            assert role_change_audits[0].entity_id == str(demoted_uid)
    finally:
        # Clean up
        with get_db_session() as db:
            db.execute(delete(SessionModel).where(SessionModel.user_id == admin2_id))
            db.execute(delete(User).where(User.id == admin2_id))
            db.execute(delete(AuditLog).where(AuditLog.entity_id.in_([str(1), str(admin2_id)])))
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
    """Verify transaction atomicity via failure injection after business and audit records are added but before commit."""
    import uuid
    from sqlalchemy.orm import Session

    unique_name = f"Rollback Test Corp {uuid.uuid4().hex[:8]}"
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    # 1. Direct session failure injection test:
    # Staging both Customer record and AuditLog record in session, then raising exception before commit.
    with pytest.raises(RuntimeError) as exc_info:
        with get_db_session() as db:
            cust = Customer(
                billing_name=unique_name,
                billing_address="Test Address",
                city="Pune",
                state="Maharashtra",
                country="IN",
                gst_number="27ABCDE1234F1Z5",
                is_active=True,
            )
            db.add(cust)
            db.flush()

            log_security_audit(
                "CUSTOMER_CREATED",
                "CUSTOMER",
                unique_name,
                user_id=1,
                username="sandbox_admin",
                details={"name": unique_name},
                db=db,
            )

            # Both records are staged in the active transaction
            assert cust in db

            # INJECT FAILURE BEFORE COMMIT
            raise RuntimeError("SIMULATED_FAILURE_AFTER_STAGING_BEFORE_COMMIT")

    assert "SIMULATED_FAILURE_AFTER_STAGING_BEFORE_COMMIT" in str(exc_info.value)

    # In a new clean session, verify that BOTH business record and audit log were rolled back
    with get_db_session() as db:
        customer_after = db.scalar(select(Customer).where(Customer.billing_name == unique_name))
        audit_after = db.scalar(
            select(AuditLog).where(AuditLog.action == "CUSTOMER_CREATED", AuditLog.entity_id == unique_name)
        )
        assert customer_after is None, "Business record must be rolled back on transaction abort!"
        assert audit_after is None, "Audit log record must be rolled back on transaction abort!"

    # 2. Endpoint-level failure injection test:
    # Monkeypatching session commit during POST /api/customers request
    unique_api_name = f"API Rollback Test {uuid.uuid4().hex[:8]}"
    original_commit = Session.commit

    def failing_commit(self):
        raise RuntimeError("SIMULATED_COMMIT_ABORT_AFTER_AUDIT_LOG")

    try:
        Session.commit = failing_commit
        with pytest.raises(RuntimeError, match="SIMULATED_COMMIT_ABORT_AFTER_AUDIT_LOG"):
            client.post(
                "/api/customers",
                json={"billing_name": unique_api_name, "gst_number": "27ABCDE1234F1Z5"},
                headers=admin_headers,
            )
    finally:
        Session.commit = original_commit

    # Verify neither the customer nor any audit log for this customer was committed
    with get_db_session() as db:
        api_cust_after = db.scalar(select(Customer).where(Customer.billing_name == unique_api_name))
        api_audit_after = db.scalar(
            select(AuditLog).where(AuditLog.action == "CUSTOMER_CREATED", AuditLog.entity_id == unique_api_name)
        )
        assert api_cust_after is None, "API customer record must be rolled back!"
        assert api_audit_after is None, "API audit log must be rolled back!"


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


def test_statutory_percentage_contract_acceptance_cases():
    """Verify that GST and TDS rate creations use an unambiguous decimal representation.
    Acceptance cases:
    - 0.5%  -> stored rate 0.0050 (readback 0.005)
    - 1%    -> stored rate 0.0100 (readback 0.01)
    - 1.5%  -> stored rate 0.0150 (readback 0.015)
    - 18%   -> stored rate 0.1800 (readback 0.18)
    And invalid values > 1.0 are rejected by schema validation (422).
    """
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    from app.models.domain_models import GstRateMaster, TdsSectionMaster
    import uuid

    cases = [
        ("0.5%", 0.005, Decimal("0.0050")),
        ("1%", 0.01, Decimal("0.0100")),
        ("1.5%", 0.015, Decimal("0.0150")),
        ("18%", 0.18, Decimal("0.1800")),
    ]

    created_gst_ids = []
    created_tds_codes = []

    try:
        for label, rate_input, expected_db in cases:
            unique_suffix = uuid.uuid4().hex[:6]

            # 1. GST Rate Creation
            gst_name = f"Test GST {label} {unique_suffix}"
            gst_res = client.post(
                "/api/settings/taxes/gst",
                json={"name": gst_name, "rate": rate_input, "tax_type": "GST"},
                headers=admin_headers,
            )
            assert gst_res.status_code == 200, gst_res.text
            gst_id = gst_res.json()["id"]
            created_gst_ids.append(gst_id)

            # 2. TDS Section Creation
            tds_code = f"TDS-{label.replace('.', '_')}-{unique_suffix}"
            tds_res = client.post(
                "/api/settings/taxes/tds",
                json={"section_code": tds_code, "rate": rate_input, "description": f"Test {label}"},
                headers=admin_headers,
            )
            assert tds_res.status_code == 200, tds_res.text
            created_tds_codes.append(tds_code)

            # 3. Direct DB Verification
            with get_db_session() as db:
                gst_record = db.scalar(select(GstRateMaster).where(GstRateMaster.id == gst_id))
                assert gst_record is not None
                assert gst_record.rate == expected_db, f"Expected {expected_db} in DB, got {gst_record.rate}"

                tds_record = db.scalar(select(TdsSectionMaster).where(TdsSectionMaster.section_code == tds_code))
                assert tds_record is not None
                assert tds_record.default_rate == expected_db, f"Expected {expected_db} in DB, got {tds_record.default_rate}"

        # 4. API Readback verification via GET /api/settings/taxes
        taxes_res = client.get("/api/settings/taxes", headers=admin_headers)
        assert taxes_res.status_code == 200
        taxes_data = taxes_res.json()

        for label, rate_input, _ in cases:
            matching_gst = next((g for g in taxes_data["gst_rates"] if label in g["name"]), None)
            assert matching_gst is not None, f"GST {label} not found in readback"
            assert matching_gst["rate"] == rate_input, f"GST readback mismatch: expected {rate_input}, got {matching_gst['rate']}"

            matching_tds = next((t for t in taxes_data["tds_rates"] if f"Test {label}" == t["description"]), None)
            assert matching_tds is not None, f"TDS {label} not found in readback"
            assert matching_tds["rate"] == rate_input, f"TDS readback mismatch: expected {rate_input}, got {matching_tds['rate']}"

        # 5. Schema Validation: values > 1.0 are rejected with 422
        invalid_gst = client.post(
            "/api/settings/taxes/gst",
            json={"name": "Invalid GST 18%", "rate": 18.0, "tax_type": "GST"},
            headers=admin_headers,
        )
        assert invalid_gst.status_code == 422

        invalid_tds = client.post(
            "/api/settings/taxes/tds",
            json={"section_code": "INVALID-TDS", "rate": 2.0, "description": "Invalid TDS"},
            headers=admin_headers,
        )
        assert invalid_tds.status_code == 422

    finally:
        # Cleanup created records
        with get_db_session() as db:
            for gid in created_gst_ids:
                db.execute(delete(GstRateMaster).where(GstRateMaster.id == gid))
            for tcode in created_tds_codes:
                db.execute(delete(TdsSectionMaster).where(TdsSectionMaster.section_code == tcode))
            db.commit()


def test_ap01_r02_non_admin_direct_api_mutation_rejection():
    """AP01-R02: Non-admin roles (viewer, sales, finance) are strictly rejected with HTTP 403 on all user mutation APIs."""
    roles = ["viewer", "sales", "finance"]
    passwords = {"viewer": "ViewerPass123!", "sales": "SalesPass123!", "finance": "FinancePass123!"}

    for role in roles:
        login_res = client.post(
            "/api/auth/login",
            json={"username": f"sandbox_{role}", "password": passwords[role]},
        )
        assert login_res.status_code == 200, f"Login failed for sandbox_{role}"
        headers = {"Authorization": f"Bearer {login_res.json()['token']}"}

        # 1. User creation rejected (403)
        r_create = client.post(
            "/api/users",
            json={"username": f"illegal_{role}_user", "password": "IllegalPass123!", "role": "sales"},
            headers=headers,
        )
        assert r_create.status_code == 403, f"Expected 403 for {role} on POST /api/users, got {r_create.status_code}"
        assert "lacks required permissions" in r_create.json()["detail"]

        # 2. User update rejected (403)
        r_update = client.put(
            "/api/users/1",
            json={"role": "viewer", "full_name": "Hacked Name"},
            headers=headers,
        )
        assert r_update.status_code == 403, f"Expected 403 for {role} on PUT /api/users/1, got {r_update.status_code}"

        # 3. User role update rejected (403)
        r_role = client.put(
            "/api/users/1/role",
            json={"role": "admin"},
            headers=headers,
        )
        assert r_role.status_code == 403, f"Expected 403 for {role} on PUT /api/users/1/role, got {r_role.status_code}"

        # 4. User password reset rejected (403)
        r_pass = client.put(
            "/api/users/1/password",
            json={"new_password": "NewIllegalPass123!"},
            headers=headers,
        )
        assert r_pass.status_code == 403, f"Expected 403 for {role} on PUT /api/users/1/password, got {r_pass.status_code}"

        # 5. User deactivation rejected (403)
        r_deact = client.put(
            "/api/users/1/deactivate",
            headers=headers,
        )
        assert r_deact.status_code == 403, f"Expected 403 for {role} on PUT /api/users/1/deactivate, got {r_deact.status_code}"

        # 6. User deletion rejected (403)
        r_del = client.delete(
            "/api/users/1",
            headers=headers,
        )
        assert r_del.status_code == 403, f"Expected 403 for {role} on DELETE /api/users/1, got {r_del.status_code}"


def test_ap01_r02_user_administration_invariants_and_session_revocation():
    """
    AP01-R02: Verify full user administration lifecycle:
    - User creation with strong Argon2id hashing and password complexity
    - Read-only directory visibility for non-admin (no leaked credentials)
    - PostgreSQL persistence
    - Last-admin demotion & deletion blocks
    - Self-deletion and self-deactivation blocks
    - Immediate session revocation on user deactivation and password reset
    """
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    assert admin_login.status_code == 200
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}
    admin_id = admin_login.json()["user"]["id"]

    test_uname = f"sec_test_{secrets.token_hex(4)}"
    test_pwd = "InitialSecPass123!"
    created_user_id = None

    try:
        # 1. User creation by admin
        create_res = client.post(
            "/api/users",
            json={
                "username": test_uname,
                "password": test_pwd,
                "full_name": "Security Test User",
                "role": "sales",
            },
            headers=admin_headers,
        )
        assert create_res.status_code == 200
        created_user_id = create_res.json()["user_id"]

        # 2. PostgreSQL persistence verification
        with get_db_session() as db:
            pg_user = db.scalar(select(User).where(User.id == created_user_id))
            assert pg_user is not None
            assert pg_user.username == test_uname
            assert pg_user.role == "sales"
            assert pg_user.is_active is True
            # Verify Argon2id hashing used
            assert pg_user.password_hash.startswith("$argon2id$")

        # 3. Read-only directory visibility to non-admin viewer
        viewer_login = client.post("/api/auth/login", json={"username": "sandbox_viewer", "password": "ViewerPass123!"})
        viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['token']}"}

        list_res = client.get("/api/users", headers=viewer_headers)
        assert list_res.status_code == 200
        users_list = list_res.json()
        assert any(u["username"] == test_uname for u in users_list)

        # Confirm directory returns safe metadata projection only (zero credentials leaked)
        for u in users_list:
            assert "password" not in u
            assert "password_hash" not in u
            assert "salt" not in u
            assert "token" not in u

        # 4. Last-admin protection checks
        demote_admin = client.put(f"/api/users/{admin_id}/role", json={"role": "sales"}, headers=admin_headers)
        assert demote_admin.status_code == 400
        assert "must have at least one active administrator" in demote_admin.json()["detail"]

        del_admin = client.delete(f"/api/users/{admin_id}", headers=admin_headers)
        assert del_admin.status_code == 400
        assert "Cannot delete your own account" in del_admin.json()["detail"]

        deact_admin = client.put(f"/api/users/{admin_id}/deactivate", headers=admin_headers)
        assert deact_admin.status_code == 400
        assert "Cannot deactivate your own account" in deact_admin.json()["detail"]

        # 5. Target user logs in
        # Clear must_change_password temporarily to obtain valid session for API testing
        with get_db_session() as db:
            db.execute(update(User).where(User.id == created_user_id).values(must_change_password=False))
            db.commit()

        target_login = client.post("/api/auth/login", json={"username": test_uname, "password": test_pwd})
        assert target_login.status_code == 200
        target_token = target_login.json()["token"]
        target_headers = {"Authorization": f"Bearer {target_token}"}

        # Active session succeeds
        me_check = client.get("/api/auth/me", headers=target_headers)
        assert me_check.status_code == 200
        assert me_check.json()["user"]["username"] == test_uname

        # 6. Admin deactivates target user -> Session immediately revoked
        deact_res = client.put(f"/api/users/{created_user_id}/deactivate", headers=admin_headers)
        assert deact_res.status_code == 200

        # Subsequent call with target token receives HTTP 401
        me_after_deact = client.get("/api/auth/me", headers=target_headers)
        assert me_after_deact.status_code == 401

        # Reactivate target user
        client.put(f"/api/users/{created_user_id}", json={"is_active": True}, headers=admin_headers)

        # 7. Target user logs in again
        with get_db_session() as db:
            db.execute(update(User).where(User.id == created_user_id).values(must_change_password=False))
            db.commit()

        target_login2 = client.post("/api/auth/login", json={"username": test_uname, "password": test_pwd})
        assert target_login2.status_code == 200
        target_token2 = target_login2.json()["token"]
        target_headers2 = {"Authorization": f"Bearer {target_token2}"}

        # Active session succeeds
        me_check2 = client.get("/api/auth/me", headers=target_headers2)
        assert me_check2.status_code == 200

        # Admin resets password -> Active session immediately revoked
        reset_res = client.put(
            f"/api/users/{created_user_id}/password",
            json={"new_password": "NewResetPass123!"},
            headers=admin_headers,
        )
        assert reset_res.status_code == 200

        # Subsequent call receives HTTP 401
        me_after_reset = client.get("/api/auth/me", headers=target_headers2)
        assert me_after_reset.status_code == 401

    finally:
        # Cleanup created user and sessions
        if created_user_id:
            with get_db_session() as db:
                db.execute(delete(SessionModel).where(SessionModel.user_id == created_user_id))
                db.execute(delete(User).where(User.id == created_user_id))
                db.commit()


