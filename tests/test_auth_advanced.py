import hashlib
import os

from app.database import execute_write, query_one
from app.routers.auth import PBKDF2_LEGACY_ITERATIONS, PBKDF2_TARGET_ITERATIONS, prune_login_attempts


def test_transparent_password_rehash_upgrade(client):
    """
    Ensure that users with legacy password hashes (100,000 iterations)
    are verified properly and their password hash is transparently upgraded
    to the target work factor (120,000 iterations) in the database upon successful login.
    """
    username = "upgrade_user"
    password = "secret_password_123"

    # Seed user with legacy 100k iteration hash
    salt = os.urandom(16).hex()
    legacy_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_LEGACY_ITERATIONS).hex()

    user_id = execute_write(
        "INSERT INTO users (username, password_hash, salt, role, full_name) VALUES (?, ?, ?, 'sales', 'Upgrade User')",
        (username, legacy_hash, salt),
    )

    # Initial check: hash is legacy
    initial_user = query_one("SELECT password_hash, salt FROM users WHERE id = ?", (user_id,))
    assert initial_user["password_hash"] == legacy_hash

    # Login with valid credentials
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200
    token = res.json()["token"]
    assert token is not None

    # Post-login check: password hash must have been upgraded to 120,000 iterations!
    upgraded_user = query_one("SELECT password_hash, salt FROM users WHERE id = ?", (user_id,))
    assert upgraded_user["password_hash"] != legacy_hash

    # Verify that the upgraded hash matches 120,000 iterations
    expected_upgraded = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), upgraded_user["salt"].encode(), PBKDF2_TARGET_ITERATIONS
    ).hex()
    assert upgraded_user["password_hash"] == expected_upgraded


def test_login_rate_limiting_and_pruning(client):
    """
    Verify brute force rate limiting triggers HTTP 429 after 5 failed attempts for a user/ip,
    and verify that prune_login_attempts deletes entries older than 24h.
    """
    test_user = "brute_force_target"
    # Seed 5 failed attempts
    for _ in range(5):
        res = client.post("/api/auth/login", json={"username": test_user, "password": "wrong_password"})
        assert res.status_code == 401

    # 6th attempt should be blocked by rate limiter with 429
    blocked_res = client.post("/api/auth/login", json={"username": test_user, "password": "wrong_password"})
    assert blocked_res.status_code == 429
    assert "too many failed login attempts" in blocked_res.json()["detail"].lower()

    # Test pruning: insert an old record (> 24 hours ago) and call prune_login_attempts
    execute_write(
        "INSERT INTO login_attempts (identifier, success, attempted_at) VALUES ('old_entry', 0, datetime('now', '-25 hours'))"
    )
    prune_login_attempts()
    old_row = query_one("SELECT * FROM login_attempts WHERE identifier = 'old_entry'")
    assert old_row is None


def test_session_invalidation_on_role_change(client, admin_token):
    """Ensure changing a user's role revokes all active sessions for that user."""
    # 1. Create a user
    client.post(
        "/api/users",
        json={"username": "session_test_user", "password": "password123", "role": "sales"},
        headers=admin_token,
    )
    user_row = query_one("SELECT id FROM users WHERE username = 'session_test_user'")
    user_id = user_row["id"]

    # 2. Log in as that user
    login_res = client.post("/api/auth/login", json={"username": "session_test_user", "password": "password123"})
    user_token = {"Authorization": f"Bearer {login_res.json()['token']}"}

    # 3. Verify user is authenticated
    assert client.get("/api/auth/me", headers=user_token).status_code == 200

    # 4. Admin changes user's role
    update_res = client.put(f"/api/users/{user_id}", json={"role": "finance"}, headers=admin_token)
    assert update_res.status_code == 200

    # 5. User's previous session must now be rejected (HTTP 401)
    me_after = client.get("/api/auth/me", headers=user_token)
    assert me_after.status_code == 401
