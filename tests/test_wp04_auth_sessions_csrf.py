"""
CollectFlow Release Preparation WP-04: Test Suite
Covers:
1. Cookie-based authentication & cookie attributes (__Host-, HttpOnly, SameSite=Lax, Secure, Max-Age).
2. Production cookie safety (rejection of unencrypted / insecure cookies in prod/staging).
3. Transport migration (no bearer token in prod login JSON; rejection of bearer auth in prod/staging).
4. Session lifecycle (session creation, expiry, invalidation, user deactivation, logout).
5. Mandatory password rotation enforcement (restricted endpoint whitelist; 403 PASSWORD_CHANGE_REQUIRED).
6. Password rotation execution (Argon2id, policy enforcement, reuse rejection, session revocation).
7. Strict CSRF protection (HMAC-SHA256, constant-time compare, missing/invalid token rejection).
8. Same-origin validation (Origin/Referer inspection, cross-origin blocking).
"""

import datetime
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from app.main import app
from app.core.database import get_db_session
from app.models.domain_models import SessionModel, User
from app.core.auth_security import (
    generate_csrf_token,
    get_session_cookie_name,
    is_production_or_staging_env,
    is_session_cookie_secure,
    validate_strong_password,
)
from app.routers.auth import hash_password_argon2, hash_token


@pytest.fixture
def fresh_client():
    """Returns a standalone TestClient with clean cookie jar."""
    return TestClient(app)


def test_cookie_attributes_in_dev_environment(fresh_client):
    """In test/dev, session cookie is named collectflow_dev_session, HttpOnly, SameSite=Lax."""
    resp = fresh_client.post(
        "/api/auth/login",
        json={"username": "sandbox_admin", "password": "AdminPass123!"},
    )
    assert resp.status_code == 200
    cookies = resp.cookies
    assert "collectflow_dev_session" in cookies
    assert "collectflow_csrf_token" in cookies

    # Check Set-Cookie headers directly
    set_cookie_headers = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else [resp.headers.get("set-cookie", "")]
    all_cookies_str = " ".join(set_cookie_headers).lower()
    assert "httponly" in all_cookies_str
    assert "samesite=lax" in all_cookies_str
    assert "max-age=604800" in all_cookies_str or "expires=" in all_cookies_str


def test_production_cookie_host_prefix_and_secure(monkeypatch):
    """In production environment, session cookie MUST use __Host- prefix and Secure flag."""
    monkeypatch.setenv("COLLECTFLOW_ENV", "production")
    monkeypatch.setenv("COLLECTFLOW_COOKIE_SECURE", "true")

    client = TestClient(app)
    resp = client.post(
        "/api/auth/login",
        json={"username": "sandbox_admin", "password": "AdminPass123!"},
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code == 200
    set_cookie_headers = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else [resp.headers.get("set-cookie", "")]
    all_cookies_str = " ".join(set_cookie_headers)
    assert "__Host-collectflow_session" in all_cookies_str
    assert "Secure" in all_cookies_str
    assert "HttpOnly" in all_cookies_str
    assert "SameSite=lax" in all_cookies_str or "samesite=lax" in all_cookies_str.lower()
    # Host-only cookie: no domain attribute
    assert "Domain=" not in all_cookies_str


def test_production_fails_closed_if_cookie_secure_disabled(monkeypatch):
    """Production fails closed if COLLECTFLOW_COOKIE_SECURE=false is set."""
    monkeypatch.setenv("COLLECTFLOW_ENV", "production")
    monkeypatch.setenv("COLLECTFLOW_COOKIE_SECURE", "false")

    with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
        is_session_cookie_secure()


def test_production_login_payload_does_not_contain_bearer_token(monkeypatch):
    """In production, /api/auth/login payload must not contain raw bearer token."""
    monkeypatch.setenv("COLLECTFLOW_ENV", "production")
    monkeypatch.setenv("COLLECTFLOW_COOKIE_SECURE", "true")

    client = TestClient(app)
    resp = client.post(
        "/api/auth/login",
        json={"username": "sandbox_admin", "password": "AdminPass123!"},
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "csrf_token" in data
    assert "user" in data
    assert "token" not in data
    assert "access_token" not in data


def test_bearer_authentication_strictly_forbidden_in_production(monkeypatch):
    """In production, requests using Authorization: Bearer must be rejected with 401."""
    monkeypatch.setenv("COLLECTFLOW_ENV", "production")

    client = TestClient(app)
    resp = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer some_dummy_token_value"},
    )
    assert resp.status_code == 401
    assert "Bearer token authentication is forbidden in production and staging environments" in resp.json()["detail"]


def test_ambiguous_credentials_rejection(monkeypatch):
    """When configured with COLLECTFLOW_REJECT_AMBIGUOUS_AUTH=true, conflicting credentials yield 401."""
    monkeypatch.setenv("COLLECTFLOW_REJECT_AMBIGUOUS_AUTH", "true")
    monkeypatch.setenv("COLLECTFLOW_ALLOW_BEARER_AUTH", "true")

    client = TestClient(app)
    # Login to acquire a cookie
    login_resp = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    assert login_resp.status_code == 200

    # Send conflicting bearer header with the existing cookie
    resp = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer different_token_12345"},
    )
    assert resp.status_code == 401
    assert "Ambiguous authentication: conflicting session credentials provided." in resp.json()["detail"]


def test_session_lifecycle_and_invalidation(fresh_client):
    """Expired or deleted session cannot authenticate."""
    resp = fresh_client.post(
        "/api/auth/login",
        json={"username": "sandbox_admin", "password": "AdminPass123!"},
    )
    assert resp.status_code == 200
    session_cookie = resp.cookies.get("collectflow_dev_session")
    token_h = hash_token(session_cookie)

    # Verify session works
    me_resp = fresh_client.get("/api/auth/me")
    assert me_resp.status_code == 200

    # Expire session in DB
    with get_db_session() as db:
        db.execute(
            update(SessionModel)
            .where(SessionModel.token_hash == token_h)
            .values(expires_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1))
        )
        db.commit()

    # Request with expired session must fail with 401
    fail_resp = fresh_client.get("/api/auth/me")
    assert fail_resp.status_code == 401
    assert "Session expired" in fail_resp.json()["detail"]


def test_inactive_user_rejected(fresh_client):
    """Inactive or disabled user cannot authenticate even with valid session."""
    resp = fresh_client.post(
        "/api/auth/login",
        json={"username": "sandbox_viewer", "password": "ViewerPass123!"},
    )
    assert resp.status_code == 200

    # Deactivate user in DB
    with get_db_session() as db:
        user = db.scalar(select(User).where(User.username == "sandbox_viewer"))
        user.is_active = False
        db.commit()

    try:
        me_resp = fresh_client.get("/api/auth/me")
        assert me_resp.status_code == 401
        assert "inactive or disabled" in me_resp.json()["detail"]
    finally:
        # Re-activate user for remaining tests
        with get_db_session() as db:
            user = db.scalar(select(User).where(User.username == "sandbox_viewer"))
            user.is_active = True
            db.commit()


def test_logout_revokes_session_and_clears_cookies(fresh_client):
    """Logout revokes DB session and instructs client to clear cookies."""
    login_resp = fresh_client.post(
        "/api/auth/login",
        json={"username": "sandbox_admin", "password": "AdminPass123!"},
    )
    assert login_resp.status_code == 200
    csrf_token = login_resp.json()["csrf_token"]
    cookie_val = login_resp.cookies.get("collectflow_dev_session")

    # Call logout with CSRF token
    logout_resp = fresh_client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert logout_resp.status_code == 200

    # Verify session is deleted from database
    with get_db_session() as db:
        token_h = hash_token(cookie_val)
        sess = db.scalar(select(SessionModel).where(SessionModel.token_hash == token_h))
        assert sess is None

    # Subsequent request fails
    me_resp = fresh_client.get("/api/auth/me")
    assert me_resp.status_code == 401


def test_mandatory_password_rotation_restriction(fresh_client):
    """Users with must_change_password=True can only access whitelisted rotation endpoints."""
    # Set must_change_password flag on sandbox_sales
    with get_db_session() as db:
        user = db.scalar(select(User).where(User.username == "sandbox_sales"))
        user.must_change_password = True
        db.commit()

    try:
        login_resp = fresh_client.post(
            "/api/auth/login",
            json={"username": "sandbox_sales", "password": "SalesPass123!"},
        )
        assert login_resp.status_code == 200
        csrf_token = login_resp.json()["csrf_token"]
        assert login_resp.json()["user"]["must_change_password"] is True

        # 1. Allowed: GET /api/auth/me
        me_resp = fresh_client.get("/api/auth/me")
        assert me_resp.status_code == 200
        assert me_resp.json()["user"]["must_change_password"] is True

        # 2. Allowed: GET /api/auth/csrf-token
        csrf_resp = fresh_client.get("/api/auth/csrf-token")
        assert csrf_resp.status_code == 200
        assert "csrf_token" in csrf_resp.json()

        # 3. Blocked: Application data endpoints return 403 PASSWORD_CHANGE_REQUIRED
        cust_resp = fresh_client.get("/api/customers")
        assert cust_resp.status_code == 403
        assert "PASSWORD_CHANGE_REQUIRED" in cust_resp.json()["detail"]
        assert cust_resp.headers.get("X-Password-Change-Required") == "true"

        inv_resp = fresh_client.get("/api/invoices")
        assert inv_resp.status_code == 403
        assert "PASSWORD_CHANGE_REQUIRED" in inv_resp.json()["detail"]

        user_resp = fresh_client.get("/api/users")
        assert user_resp.status_code == 403

        # 4. Allowed: POST /api/auth/logout
        logout_resp = fresh_client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert logout_resp.status_code == 200
    finally:
        # Reset flag
        with get_db_session() as db:
            user = db.scalar(select(User).where(User.username == "sandbox_sales"))
            user.must_change_password = False
            db.commit()


def test_password_rotation_workflow(fresh_client):
    """Successful password rotation updates hash, clears flag, and revokes sessions."""
    # Mark user as needing password change
    with get_db_session() as db:
        user = db.scalar(select(User).where(User.username == "sandbox_sales"))
        user.must_change_password = True
        user.password_hash = hash_password_argon2("SalesPass123!")
        db.commit()

    login_resp = fresh_client.post(
        "/api/auth/login",
        json={"username": "sandbox_sales", "password": "SalesPass123!"},
    )
    assert login_resp.status_code == 200
    csrf_token = login_resp.json()["csrf_token"]

    # 1. Reject incorrect current password
    err1 = fresh_client.post(
        "/api/auth/change-password",
        json={"current_password": "WrongPassword1!", "new_password": "NewSalesPass2026!"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert err1.status_code == 400
    assert "Current password is incorrect" in err1.json()["detail"]

    # 2. Reject reuse of current password
    err2 = fresh_client.post(
        "/api/auth/change-password",
        json={"current_password": "SalesPass123!", "new_password": "SalesPass123!"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert err2.status_code == 400
    assert "cannot be the same" in err2.json()["detail"]

    # 3. Reject weak new password (< 12 chars)
    err3 = fresh_client.post(
        "/api/auth/change-password",
        json={"current_password": "SalesPass123!", "new_password": "Short1!"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert err3.status_code == 400
    assert "at least 12 characters" in err3.json()["detail"]

    # 4. Successful rotation
    ok_resp = fresh_client.post(
        "/api/auth/change-password",
        json={"current_password": "SalesPass123!", "new_password": "NewSalesPass2026!"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["success"] is True

    # Verify must_change_password is False in DB
    with get_db_session() as db:
        u = db.scalar(select(User).where(User.username == "sandbox_sales"))
        assert u.must_change_password is False

    # Old session is revoked (cookies were cleared)
    unauth = fresh_client.get("/api/auth/me")
    assert unauth.status_code == 401

    # Old password fails login
    old_login = fresh_client.post(
        "/api/auth/login",
        json={"username": "sandbox_sales", "password": "SalesPass123!"},
    )
    assert old_login.status_code == 401

    # New password succeeds login and user can access application data
    new_login = fresh_client.post(
        "/api/auth/login",
        json={"username": "sandbox_sales", "password": "NewSalesPass2026!"},
    )
    assert new_login.status_code == 200
    assert new_login.json()["user"]["must_change_password"] is False

    # Restore original password for test reproducibility
    with get_db_session() as db:
        user = db.scalar(select(User).where(User.username == "sandbox_sales"))
        user.password_hash = hash_password_argon2("SalesPass123!")
        user.must_change_password = False
        db.commit()


def test_csrf_protection_on_cookie_mutations(fresh_client):
    """All cookie-authenticated POST/PUT/PATCH/DELETE requests enforce CSRF tokens."""
    login_resp = fresh_client.post(
        "/api/auth/login",
        json={"username": "sandbox_admin", "password": "AdminPass123!"},
    )
    assert login_resp.status_code == 200
    csrf_token = login_resp.json()["csrf_token"]

    # 1. Missing CSRF token yields 403 CSRF_TOKEN_MISSING
    no_csrf = fresh_client.post(
        "/api/auth/logout",
    )
    assert no_csrf.status_code == 403
    assert "CSRF_TOKEN_MISSING" in no_csrf.json()["detail"]

    # 2. Invalid CSRF token yields 403 CSRF_TOKEN_INVALID
    bad_csrf = fresh_client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": "invalid_csrf_token_value_here"},
    )
    assert bad_csrf.status_code == 403
    assert "CSRF_TOKEN_INVALID" in bad_csrf.json()["detail"]

    # 3. Valid CSRF token succeeds
    good_csrf = fresh_client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert good_csrf.status_code == 200


def test_cross_origin_request_rejected(fresh_client):
    """Requests with unauthorized Origin header are rejected with 403 ORIGIN_FORBIDDEN."""
    resp = fresh_client.post(
        "/api/auth/login",
        json={"username": "sandbox_admin", "password": "AdminPass123!"},
        headers={"Origin": "https://attacker.evil.com"},
    )
    assert resp.status_code == 403
    assert "ORIGIN_FORBIDDEN" in resp.json()["detail"]


def test_strong_password_validator():
    """Verify strong password validator rules."""
    from fastapi import HTTPException
    # Missing uppercase
    with pytest.raises(HTTPException, match="uppercase"):
        validate_strong_password("lowercase123!@#")
    # Missing lowercase
    with pytest.raises(HTTPException, match="lowercase"):
        validate_strong_password("UPPERCASE123!@#")
    # Missing digit
    with pytest.raises(HTTPException, match="digit"):
        validate_strong_password("UppercaseNoDigit!")
    # Missing special character
    with pytest.raises(HTTPException, match="special character"):
        validate_strong_password("UppercaseDigit1234")
    # Too short
    with pytest.raises(HTTPException, match="at least 12"):
        validate_strong_password("Short1!")
    # Valid
    validate_strong_password("SuperSecurePassword2026!")
