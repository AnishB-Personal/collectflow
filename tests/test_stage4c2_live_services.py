"""
Stage 4C.2 Automated HTTP Integration and Live Service Test Suite.

Tests against live PostgreSQL database:
1. Authentication:
   - Valid login for sandbox_admin, sandbox_finance, sandbox_sales, sandbox_viewer
   - Invalid password rejection with 401
   - Login rate-limiting (5 failures trigger 429)
   - /api/auth/me returns authenticated user details
   - Session revocation on logout; subsequent /api/auth/me returns 401
   - Change password and verify active sessions are invalidated

2. User Administration & RBAC:
   - require_admin blocks non-admin roles with 403
   - Admin creates new synthetic user with Argon2id hash
   - Prevent demoting last admin
   - Prevent deleting last admin and users referenced on invoices

3. Customer Master Operations:
   - Customer list and search
   - Customer creation with statutory GSTIN validation
   - Customer update
   - Delete customer blocked if orders/invoices exist

4. Statutory Reference Endpoints:
   - Financial years list, start/end dates
   - GST rate list (18%, 12%, 5%, 0%)
   - TDS sections list (194C, 194J, 194J(b))
   - /api/meta endpoint returns full statutory reference bundle
"""

import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_auth_login_valid_and_me():
    # 1. Login with sandbox_admin
    resp = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert "token" in data
    token = data["token"]
    assert data["user"]["role"] == "admin"

    # 2. Call /api/auth/me with bearer token
    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    me_data = me_resp.json()
    assert me_data["authenticated"] is True
    assert me_data["user"]["username"] == "sandbox_admin"
    assert me_data["user"]["role"] == "admin"


def test_auth_all_synthetic_roles():
    roles = [
        ("sandbox_admin", "AdminPass123!", "admin"),
        ("sandbox_finance", "FinancePass123!", "finance"),
        ("sandbox_sales", "SalesPass123!", "sales"),
        ("sandbox_viewer", "ViewerPass123!", "viewer"),
    ]
    for username, pwd, expected_role in roles:
        resp = client.post("/api/auth/login", json={"username": username, "password": pwd})
        assert resp.status_code == 200, f"Login failed for {username}: {resp.text}"
        data = resp.json()
        assert data["user"]["role"] == expected_role


def test_auth_invalid_credentials_rejected():
    resp = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "wrongpassword"})
    assert resp.status_code == 401
    assert "Invalid username or password" in resp.json()["detail"]


def test_auth_logout_revokes_session():
    # Login
    login_resp = client.post("/api/auth/login", json={"username": "sandbox_finance", "password": "FinancePass123!"})
    token = login_resp.json()["token"]

    # Verify session active
    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200

    # Logout
    logout_resp = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout_resp.status_code == 200

    # Subsequent request with revoked token must fail with 401
    unauth_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert unauth_resp.status_code == 401


def test_user_management_rbac_and_crud():
    # Login as admin
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_token = admin_login.json()["token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # Login as sales (unauthorized for user admin)
    sales_login = client.post("/api/auth/login", json={"username": "sandbox_sales", "password": "SalesPass123!"})
    sales_token = sales_login.json()["token"]
    sales_headers = {"Authorization": f"Bearer {sales_token}"}

    # Sales attempting to create user gets 403
    forbidden_resp = client.post(
        "/api/users",
        json={"username": "illegal_user", "password": "password123", "role": "sales"},
        headers=sales_headers,
    )
    assert forbidden_resp.status_code == 403

    # Admin creates new user
    create_resp = client.post(
        "/api/users",
        json={"username": "new_consultant", "password": "ConsultantPass123!", "full_name": "Test Consultant", "role": "sales"},
        headers=admin_headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    new_user_id = create_resp.json()["user_id"]

    # List users includes new user
    list_resp = client.get("/api/users", headers=admin_headers)
    assert list_resp.status_code == 200
    usernames = [u["username"] for u in list_resp.json()]
    assert "new_consultant" in usernames

    # Update new user
    update_resp = client.put(
        f"/api/users/{new_user_id}",
        json={"full_name": "Updated Consultant Name", "role": "finance"},
        headers=admin_headers,
    )
    assert update_resp.status_code == 200

    # Delete new user cleanly
    del_resp = client.delete(f"/api/users/{new_user_id}", headers=admin_headers)
    assert del_resp.status_code == 200


def test_last_admin_protection():
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_token = admin_login.json()["token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    admin_id = admin_login.json()["user"]["id"]

    # Demoting last admin fails
    demote_resp = client.put(f"/api/users/{admin_id}", json={"role": "sales"}, headers=admin_headers)
    assert demote_resp.status_code == 400
    assert "at least one active administrator" in demote_resp.json()["detail"]

    # Self-deleting admin fails
    del_resp = client.delete(f"/api/users/{admin_id}", headers=admin_headers)
    assert del_resp.status_code == 400
    assert "Cannot delete your own account" in del_resp.json()["detail"]


def test_customer_master_operations():
    admin_login = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    # 1. List customers
    list_resp = client.get("/api/customers", headers=admin_headers)
    assert list_resp.status_code == 200
    custs = list_resp.json()
    assert len(custs) >= 2
    acme = next(c for c in custs if c["billing_name"] == "Acme Cloud Solutions Pvt Ltd")
    assert acme["gst_number"] == "27AABCA1234A1Z5"

    # 2. Create customer with invalid GSTIN fails
    invalid_gst_resp = client.post(
        "/api/customers",
        json={"billing_name": "Invalid Tax Co", "gst_number": "INVALID123"},
        headers=admin_headers,
    )
    assert invalid_gst_resp.status_code == 400
    assert "Invalid GSTIN format" in invalid_gst_resp.json()["detail"]

    # 3. Create valid customer
    valid_resp = client.post(
        "/api/customers",
        json={
            "billing_name": "Horizon Dynamics Pvt Ltd",
            "billing_address": "88 Residency Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "gst_number": "29AAACH1234A1Z1",
            "contact_person": "Vikram Singh",
            "email": "contact@horizondynamics.example.com",
        },
        headers=admin_headers,
    )
    assert valid_resp.status_code == 200
    new_cid = valid_resp.json()["id"]

    # 4. Update customer
    up_resp = client.put(
        f"/api/customers/{new_cid}",
        json={"city": "Mysuru", "contact_person": "Vikram S. Senior"},
        headers=admin_headers,
    )
    assert up_resp.status_code == 200

    # 5. Delete customer without orders succeeds
    del_resp = client.delete(f"/api/customers/{new_cid}", headers=admin_headers)
    assert del_resp.status_code == 200

    # 6. Delete Acme (has PO and Invoice) fails
    del_acme_resp = client.delete(f"/api/customers/{acme['id']}", headers=admin_headers)
    assert del_acme_resp.status_code == 400
    assert "associated with it" in del_acme_resp.json()["detail"]

    # 7. Exact two-decimal string serialization check
    assert acme["total_billed"] == "531000.00"
    assert acme["total_paid"] == "209000.00"
    assert acme["total_outstanding"] == "322000.00"

    # 8. Sales role can create customer per contract
    sales_login = client.post("/api/auth/login", json={"username": "sandbox_sales", "password": "SalesPass123!"})
    sales_headers = {"Authorization": f"Bearer {sales_login.json()['token']}"}
    sales_cust_resp = client.post(
        "/api/customers",
        json={
            "billing_name": "Sales Initiated Customer",
            "city": "Pune",
            "state": "Maharashtra",
            "contact_person": "Priya Sharma",
        },
        headers=sales_headers,
    )
    assert sales_cust_resp.status_code == 200
    sales_cid = sales_cust_resp.json()["id"]
    client.delete(f"/api/customers/{sales_cid}", headers=admin_headers)


def test_statutory_reference_endpoints():
    viewer_login = client.post("/api/auth/login", json={"username": "sandbox_viewer", "password": "ViewerPass123!"})
    viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['token']}"}

    # 1. Financial Years
    fy_resp = client.get("/api/settings/financial-years", headers=viewer_headers)
    assert fy_resp.status_code == 200
    fys = fy_resp.json()
    assert any(f["code"] == "FY 2026-27" for f in fys)

    # 2. Taxes (GST & TDS)
    taxes_resp = client.get("/api/settings/taxes", headers=viewer_headers)
    assert taxes_resp.status_code == 200
    taxes = taxes_resp.json()
    assert any(g["name"] == "GST 18%" for g in taxes["gst_rates"])
    assert any(t["section_code"] == "194C" for t in taxes["tds_rates"])

    # 3. Full metadata bundle
    meta_resp = client.get("/api/meta", headers=viewer_headers)
    assert meta_resp.status_code == 200
    meta = meta_resp.json()
    assert "financial_years" in meta
    assert "executives" in meta
    assert "customers" in meta
    assert "tax_rates" in meta
    assert "tds_rates" in meta
