def test_login_success_and_me(client, test_users):
    admin = test_users["admin"]
    resp = client.get("/api/auth/me", headers=admin["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert data["user"]["username"] == "user_admin"
    assert data["user"]["role"] == "admin"


def test_login_invalid_password_rejected(client, test_users):
    resp = client.post("/api/auth/login", json={"username": "user_admin", "password": "wrongpassword"})
    assert resp.status_code == 401
    assert "Invalid username or password" in resp.json()["detail"]


def test_backdoor_admin_password_rejected(client, test_users):
    # Verify that the hardcoded backdoor ("admin" or "admin123") is rejected when actual password is "adminpass"
    resp = client.post("/api/auth/login", json={"username": "user_admin", "password": "admin123"})
    assert resp.status_code == 401
    resp2 = client.post("/api/auth/login", json={"username": "user_admin", "password": "admin"})
    assert resp2.status_code == 401


def test_unauthenticated_requests_rejected(client):
    endpoints = [
        ("GET", "/api/meta"),
        ("GET", "/api/dashboard/summary"),
        ("GET", "/api/invoices"),
        ("POST", "/api/invoices"),
        ("GET", "/api/customers"),
        ("POST", "/api/customers"),
        ("POST", "/api/payments"),
        ("GET", "/api/settings/taxes"),
        ("GET", "/api/settings/financial-years"),
    ]
    for method, url in endpoints:
        if method == "GET":
            r = client.get(url)
        else:
            r = client.post(url, json={})
        assert r.status_code == 401, f"{method} {url} should require authentication, got {r.status_code}"


def test_user_creation_admin_only(client, test_users):
    payload = {"username": "newuser", "password": "secretpassword", "full_name": "New User", "role": "sales"}
    # Viewer tries to create user
    r_viewer = client.post("/api/users", json=payload, headers=test_users["viewer"]["headers"])
    assert r_viewer.status_code == 403

    # Sales tries to create user
    r_sales = client.post("/api/users", json=payload, headers=test_users["sales"]["headers"])
    assert r_sales.status_code == 403

    # Finance tries to create user
    r_finance = client.post("/api/users", json=payload, headers=test_users["finance"]["headers"])
    assert r_finance.status_code == 403

    # Admin succeeds
    r_admin = client.post("/api/users", json=payload, headers=test_users["admin"]["headers"])
    assert r_admin.status_code == 200
    assert r_admin.json()["success"] is True


def test_password_change_invalidates_sessions(client, test_users):
    sales = test_users["sales"]
    old_headers = sales["headers"]

    # Change password
    r_change = client.post(
        "/api/auth/change-password",
        json={"current_password": "salespass", "new_password": "newsalespass123"},
        headers=old_headers,
    )
    assert r_change.status_code == 200

    # Old token must now be invalid
    r_me = client.get("/api/auth/me", headers=old_headers)
    assert r_me.status_code == 401

    # Login with new password succeeds
    r_login = client.post("/api/auth/login", json={"username": sales["username"], "password": "newsalespass123"})
    assert r_login.status_code == 200
    new_token = r_login.json()["token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200


def test_audit_trail_recorded(client, test_users):
    from app.database import query_all

    admin = test_users["admin"]
    client.post(
        "/api/users",
        json={"username": "auditeduser", "password": "auditpassword123", "role": "sales"},
        headers=admin["headers"],
    )

    logs = query_all("SELECT * FROM audit_logs WHERE action = 'USER_CREATED'")
    assert len(logs) >= 1
    assert logs[0]["username"] == "user_admin"
    assert "auditeduser" in logs[0]["details"]


def test_update_user_role_and_details(client, test_users):
    from app.database import query_one

    admin = test_users["admin"]
    sales = test_users["sales"]

    # Admin updates sales user role to finance
    resp = client.put(
        f"/api/users/{sales['id']}/role",
        json={"role": "finance", "full_name": "Sales Turned Finance"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    u = query_one("SELECT role, full_name FROM users WHERE id = ?", (sales["id"],))
    assert u["role"] == "finance"
    assert u["full_name"] == "Sales Turned Finance"
