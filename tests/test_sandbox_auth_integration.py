import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def sandbox_client():
    sandbox_db = "/home/anishb/projects/personal/collectflow/data/collectflow_sandbox.db"
    os.environ["COLLECTFLOW_ENV"] = "integration"
    os.environ["COLLECTFLOW_DB_PATH"] = sandbox_db
    os.environ["CORS_ALLOWED_ORIGINS"] = "http://localhost:5173,http://127.0.0.1:5173"

    from app.main import app

    return TestClient(app)


def test_meta_endpoint_requires_auth_and_succeeds_with_token(sandbox_client):
    # Unauthenticated /api/meta yields 401
    unauth_res = sandbox_client.get("/api/meta")
    assert unauth_res.status_code == 401

    # Login to obtain token
    login_res = sandbox_client.post(
        "/api/auth/login",
        json={"username": "sandbox_admin", "password": "AdminPass123!"},
    )
    token = login_res.json()["token"]

    # Authenticated /api/meta succeeds
    res = sandbox_client.get("/api/meta", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.json()
    assert "financial_years" in data
    assert "tax_rates" in data
    assert "tds_rates" in data


def test_auth_login_valid_credentials(sandbox_client):
    res = sandbox_client.post(
        "/api/auth/login",
        json={"username": "sandbox_admin", "password": "AdminPass123!"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert "token" in data
    # Token is 64 hex characters (32 bytes entropy from secrets.token_hex(32))
    assert len(data["token"]) == 64
    assert data["user"]["username"] == "sandbox_admin"
    assert data["user"]["role"] == "admin"


def test_auth_login_invalid_credentials_does_not_leak(sandbox_client):
    res = sandbox_client.post(
        "/api/auth/login",
        json={"username": "sandbox_admin", "password": "WrongPassword!"},
    )
    assert res.status_code == 401
    data = res.json()
    assert data["detail"] == "Invalid username or password"
    # Ensure no internal error trace or stack details leaked
    assert "hash" not in str(data).lower()
    assert "salt" not in str(data).lower()


def test_auth_me_and_logout_flow(sandbox_client):
    # 1. Login
    login_res = sandbox_client.post(
        "/api/auth/login",
        json={"username": "sandbox_finance", "password": "FinancePass123!"},
    )
    token = login_res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. Get /api/auth/me
    me_res = sandbox_client.get("/api/auth/me", headers=headers)
    assert me_res.status_code == 200
    assert me_res.json()["user"]["username"] == "sandbox_finance"
    assert me_res.json()["user"]["role"] == "finance"

    # 3. Logout
    logout_res = sandbox_client.post("/api/auth/logout", headers=headers)
    assert logout_res.status_code == 200
    assert logout_res.json()["success"] is True

    # 4. Verify token revoked (subsequent /api/auth/me yields 401)
    revoked_res = sandbox_client.get("/api/auth/me", headers=headers)
    assert revoked_res.status_code == 401


def test_auth_rbac_forbidden_does_not_masquerade_as_401(sandbox_client):
    # Login as viewer
    login_res = sandbox_client.post(
        "/api/auth/login",
        json={"username": "sandbox_viewer", "password": "ViewerPass123!"},
    )
    token = login_res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Attempt admin-only action (create user)
    res = sandbox_client.post(
        "/api/users",
        headers=headers,
        json={"username": "new_user", "password": "password123", "role": "sales"},
    )
    # Must be 403 Forbidden, NOT 401 Unauthorized
    assert res.status_code == 403
    assert "Access forbidden" in res.json()["detail"] or "Forbidden" in res.json()["detail"]


def test_cors_preflight_for_local_origin(sandbox_client):
    res = sandbox_client.options(
        "/api/auth/login",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type,Authorization",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"
