import datetime
import hashlib
import os
import secrets
from typing import Callable, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.database import (
    cleanup_expired_sessions,
    execute_write,
    hash_token,
    log_audit,
    query_one,
)

router = APIRouter(prefix="/api/auth", tags=["Auth"])

# Ensure sessions table exists
execute_write("""
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);
""")


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


PBKDF2_TARGET_ITERATIONS = 120000
PBKDF2_LEGACY_ITERATIONS = 100000


def verify_password_with_upgrade(password: str, hashed: str, salt: str) -> tuple[bool, bool]:
    """
    PBKDF2-HMAC-SHA256 verification with constant-time equality check.
    Returns (is_valid: bool, needs_rehash: bool).
    """
    calc_target = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_TARGET_ITERATIONS).hex()
    if secrets.compare_digest(calc_target, hashed):
        return True, False
    calc_legacy = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_LEGACY_ITERATIONS).hex()
    if secrets.compare_digest(calc_legacy, hashed):
        return True, True
    return False, False


def verify_password(password: str, hashed: str, salt: str) -> bool:
    is_valid, _ = verify_password_with_upgrade(password, hashed, salt)
    return is_valid


def hash_password(password: str, iterations: int = PBKDF2_TARGET_ITERATIONS) -> tuple[str, str]:
    """PBKDF2-HMAC-SHA256 with 16-byte random salt and configurable work factor."""
    salt = os.urandom(16).hex()
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return hashed, salt


def prune_login_attempts() -> None:
    """Prune login attempts older than 24 hours to prevent table growth."""
    execute_write("DELETE FROM login_attempts WHERE attempted_at < datetime('now', '-24 hours')")


def check_login_rate_limit(identifier: str, max_failures: int = 5, cooldown: int = 15) -> None:
    """
    Progressive rate-limiting to mitigate brute-force attacks without permanent lockouts.
    Checks failed attempts in the last 15 minutes.
    """
    row = query_one(
        """
        SELECT COUNT(*) as failed_count
        FROM login_attempts
        WHERE identifier = ?
          AND success = 0
          AND attempted_at > datetime('now', '-15 minutes');
    """,
        (identifier,),
    )

    if row and row["failed_count"] >= max_failures:
        last_attempt_row = query_one(
            """
            SELECT CAST((strftime('%s', 'now') - strftime('%s', MAX(attempted_at))) AS INTEGER) as seconds_ago
            FROM login_attempts
            WHERE identifier = ? AND success = 0;
        """,
            (identifier,),
        )
        seconds_ago = (
            last_attempt_row["seconds_ago"] if last_attempt_row and last_attempt_row["seconds_ago"] is not None else 999
        )
        if seconds_ago < cooldown:
            wait_remaining = cooldown - seconds_ago
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed login attempts. Please wait {wait_remaining} seconds before trying again.",
            )


def record_login_attempt(identifier: str, success: bool):
    execute_write("INSERT INTO login_attempts (identifier, success) VALUES (?, ?)", (identifier, 1 if success else 0))
    if success:
        # Clear failures for this identifier on successful login
        execute_write("DELETE FROM login_attempts WHERE identifier = ? AND success = 0", (identifier,))


def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    raw_token = authorization.split(" ")[1].strip()
    token_hashed = hash_token(raw_token)

    query = """
    SELECT u.id, u.username, u.role, u.full_name, s.expires_at
    FROM sessions s
    JOIN users u ON s.user_id = u.id
    WHERE s.token = ? AND datetime('now') < s.expires_at;
    """
    user = query_one(query, (token_hashed,))
    if not user:
        raise HTTPException(status_code=401, detail="Session expired or invalid token")
    return user


def require_roles(*allowed_roles: str) -> Callable:
    """Dependency factory checking that the authenticated user has one of allowed_roles."""

    def role_checker(current_user: dict = Depends(get_current_user)) -> dict:
        user_role = (current_user.get("role") or "").lower()
        if user_role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Access forbidden: role '{user_role}' lacks required permissions ({', '.join(allowed_roles)})",
            )
        return current_user

    return role_checker


require_admin = require_roles("admin")
require_finance_or_admin = require_roles("admin", "finance")


@router.post("/login")
def login(req: LoginRequest, request: Request):
    uname = req.username.strip().lower()
    client_ip = request.client.host if request.client else "unknown"
    req_id = getattr(request.state, "request_id", None)
    user_rate_key = f"{client_ip}:{uname}"
    ip_rate_key = f"ip:{client_ip}"

    # Check both user-specific and IP-wide rate limits
    check_login_rate_limit(user_rate_key, max_failures=5, cooldown=15)
    check_login_rate_limit(ip_rate_key, max_failures=20, cooldown=30)

    user = query_one("SELECT * FROM users WHERE LOWER(username) = ?", (uname,))
    if not user:
        record_login_attempt(user_rate_key, success=False)
        record_login_attempt(ip_rate_key, success=False)
        log_audit("LOGIN_FAILED", "USER", None, f"Unknown username: {uname}", ip_address=client_ip, request_id=req_id)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    is_valid, needs_rehash = verify_password_with_upgrade(req.password, user["password_hash"], user["salt"])
    if not is_valid:
        record_login_attempt(user_rate_key, success=False)
        record_login_attempt(ip_rate_key, success=False)
        log_audit(
            "LOGIN_FAILED",
            "USER",
            user["id"],
            f"Bad password for user: {uname}",
            user={"id": user["id"], "username": uname},
            ip_address=client_ip,
            request_id=req_id,
        )
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Record success and reset failure streak
    record_login_attempt(user_rate_key, success=True)
    record_login_attempt(ip_rate_key, success=True)

    # Transparent password rehash to higher work factor if needed
    if needs_rehash:
        new_hash, new_salt = hash_password(req.password, iterations=PBKDF2_TARGET_ITERATIONS)
        execute_write("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?", (new_hash, new_salt, user["id"]))

    prune_login_attempts()

    # Issue cryptographically secure 256-bit token; store only its SHA-256 hash in DB
    raw_token = secrets.token_hex(32)
    token_hashed = hash_token(raw_token)
    expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    execute_write(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)", (token_hashed, user["id"], expires)
    )
    cleanup_expired_sessions()
    log_audit(
        "LOGIN_SUCCESS",
        "USER",
        user["id"],
        f"User logged in: {uname}",
        user={"id": user["id"], "username": uname},
        ip_address=client_ip,
        request_id=req_id,
    )

    return {
        "success": True,
        "token": raw_token,
        "user": {"id": user["id"], "username": user["username"], "role": user["role"], "full_name": user["full_name"]},
    }


@router.get("/me")
def get_me(user: dict = Depends(get_current_user)):
    return {"authenticated": True, "user": user}


@router.post("/logout")
def logout(request: Request, authorization: Optional[str] = Header(None)):
    req_id = getattr(request.state, "request_id", None)
    if authorization and authorization.startswith("Bearer "):
        raw_token = authorization.split(" ")[1].strip()
        token_hashed = hash_token(raw_token)
        session = query_one("SELECT user_id FROM sessions WHERE token = ?", (token_hashed,))
        execute_write("DELETE FROM sessions WHERE token = ?", (token_hashed,))
        if session:
            log_audit("LOGOUT", "USER", session["user_id"], "User logged out", request_id=req_id)
    cleanup_expired_sessions()
    prune_login_attempts()
    return {"success": True, "message": "Logged out"}


@router.post("/change-password")
def change_password(req: PasswordChangeRequest, request: Request, current_user: dict = Depends(get_current_user)):
    req_id = getattr(request.state, "request_id", None)
    user = query_one("SELECT * FROM users WHERE id = ?", (current_user["id"],))
    if not user or not verify_password(req.current_password, user["password_hash"], user["salt"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    new_hash, new_salt = hash_password(req.new_password)
    execute_write("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?", (new_hash, new_salt, current_user["id"]))
    # Invalidate all active sessions for this user so re-login with new password is required
    execute_write("DELETE FROM sessions WHERE user_id = ?", (current_user["id"],))
    log_audit(
        "PASSWORD_CHANGED",
        "USER",
        current_user["id"],
        "Password changed successfully",
        user=current_user,
        request_id=req_id,
    )
    return {"success": True, "message": "Password changed successfully. Please log in again."}
