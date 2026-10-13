"""
Authentication and Identity Service Layer for PostgreSQL Parity.

Provides:
- Argon2id password hashing and verification
- Login attempt rate-limiting and audit logging
- Cryptographically secure SHA-256 session token management
- Session expiry and revocation
- RBAC dependencies: get_current_user, require_roles, require_admin, require_finance_or_admin
"""

import datetime
import hashlib
import re
import secrets
from typing import Callable, Optional
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, select, update
from app.core.database import get_db_session
from app.models.domain_models import AuditLog, LoginAttempt, SessionModel, User

router = APIRouter(prefix="/api/auth", tags=["Auth"])
ph = PasswordHasher()
http_bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


def hash_token(raw_token: str) -> str:
    """Return SHA-256 hash of a bearer token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def verify_password_argon2(plain_password: str, password_hash: str) -> bool:
    """Verify password against Argon2id hash."""
    try:
        return ph.verify(password_hash, plain_password)
    except (VerifyMismatchError, Exception):
        return False


def hash_password_argon2(plain_password: str) -> str:
    """Generate Argon2id hash for a password."""
    return ph.hash(plain_password)


# Backward compatibility aliases
hash_password = hash_password_argon2
verify_password = verify_password_argon2


def check_login_rate_limit(identifier: str, max_failures: int = 5, cooldown_seconds: int = 15) -> None:
    """
    Progressive rate-limiting to mitigate brute-force attacks.
    Checks failed attempts within the last 15 minutes.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=15)
    with get_db_session() as db:
        failed_count = db.scalar(
            select(func.count(LoginAttempt.id)).where(
                LoginAttempt.identifier == identifier,
                LoginAttempt.success.is_(False),
                LoginAttempt.attempted_at > cutoff,
            )
        ) or 0

        if failed_count >= max_failures:
            last_attempt = db.scalar(
                select(LoginAttempt.attempted_at)
                .where(
                    LoginAttempt.identifier == identifier,
                    LoginAttempt.success.is_(False),
                )
                .order_by(desc(LoginAttempt.attempted_at))
                .limit(1)
            )
            if last_attempt:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if last_attempt.tzinfo is None:
                    last_attempt = last_attempt.replace(tzinfo=datetime.timezone.utc)
                seconds_ago = int((now_utc - last_attempt).total_seconds())
                if seconds_ago < cooldown_seconds:
                    wait_remaining = cooldown_seconds - seconds_ago
                    raise HTTPException(
                        status_code=429,
                        detail=f"Too many failed login attempts. Please wait {wait_remaining} seconds before trying again.",
                    )


def record_login_attempt(identifier: str, success: bool, ip_address: Optional[str] = None, clear_on_success: bool = True):
    with get_db_session() as db:
        attempt = LoginAttempt(
            identifier=identifier,
            success=success,
            ip_address=ip_address,
            attempted_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db.add(attempt)
        if success and clear_on_success:
            # Clear previous failed attempts on success for this specific identifier
            db.execute(
                delete(LoginAttempt).where(
                    LoginAttempt.identifier == identifier,
                    LoginAttempt.success.is_(False),
                )
            )
        db.commit()


def prune_old_login_attempts():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    with get_db_session() as db:
        db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < cutoff))
        db.commit()


def log_security_audit(
    action: str,
    entity_type: str,
    entity_id: Optional[str],
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None,
    request_id: Optional[str] = None,
    db: Optional[get_db_session] = None,
):
    """
    Atomic audit logging. When db session is passed, logs inside the active transaction.
    Otherwise creates a dedicated session and commits.
    """
    log_entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        user_id=user_id,
        username=username,
        details=details,
        ip_address=ip_address,
        request_id=request_id,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if db is not None:
        db.add(log_entry)
    else:
        with get_db_session() as session:
            session.add(log_entry)
            session.commit()


def get_current_user(
    authorization: Optional[str] = Header(None),
    bearer_creds: Optional[HTTPAuthorizationCredentials] = Depends(http_bearer),
) -> dict:
    """Dependency returning authenticated user dict from PostgreSQL session with HTTPBearer OpenAPI integration."""
    raw_token = None
    if bearer_creds and bearer_creds.credentials:
        raw_token = bearer_creds.credentials.strip()
    elif authorization and authorization.startswith("Bearer "):
        raw_token = authorization.split(" ")[1].strip()

    if not raw_token:
        raise HTTPException(status_code=401, detail="Authentication required")

    token_hashed = hash_token(raw_token)
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    with get_db_session() as db:
        stmt = (
            select(User, SessionModel)
            .join(SessionModel, SessionModel.user_id == User.id)
            .where(
                SessionModel.token_hash == token_hashed,
                SessionModel.expires_at > now_utc,
                User.is_active.is_(True),
            )
        )
        result = db.execute(stmt).first()
        if not result:
            raise HTTPException(status_code=401, detail="Session expired or invalid token")

        user, session = result
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "full_name": user.full_name,
            "is_active": user.is_active,
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        }


def require_roles(*allowed_roles: str) -> Callable:
    """Dependency factory checking that current user has one of allowed roles."""

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
require_sales_finance_admin = require_roles("admin", "finance", "sales")


@router.post("/login")
def login(req: LoginRequest, request: Request):
    uname = req.username.strip().lower()
    client_ip = request.client.host if request.client else "127.0.0.1"
    req_id = getattr(request.state, "request_id", None)
    user_rate_key = f"{client_ip}:{uname}"
    ip_rate_key = f"ip:{client_ip}"

    # Rate limiting checks
    check_login_rate_limit(user_rate_key, max_failures=5, cooldown_seconds=15)
    check_login_rate_limit(ip_rate_key, max_failures=20, cooldown_seconds=30)

    with get_db_session() as db:
        user = db.scalar(select(User).where(func.lower(User.username) == uname))
        if not user or not user.is_active:
            record_login_attempt(user_rate_key, success=False, ip_address=client_ip)
            record_login_attempt(ip_rate_key, success=False, ip_address=client_ip)
            log_security_audit(
                "LOGIN_FAILED", "USER", None, username=uname, details={"reason": "User not found or inactive"}, ip_address=client_ip, request_id=req_id, db=db
            )
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid username or password")

        if not verify_password_argon2(req.password, user.password_hash):
            record_login_attempt(user_rate_key, success=False, ip_address=client_ip)
            record_login_attempt(ip_rate_key, success=False, ip_address=client_ip)
            log_security_audit(
                "LOGIN_FAILED", "USER", str(user.id), user_id=user.id, username=user.username, details={"reason": "Invalid password"}, ip_address=client_ip, request_id=req_id, db=db
            )
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid username or password")

        # Success: record attempt for user rate key and clear user failures.
        # Do NOT clear ip_rate_key failures so other brute-force streaks from this IP remain monitored!
        record_login_attempt(user_rate_key, success=True, ip_address=client_ip, clear_on_success=True)
        record_login_attempt(ip_rate_key, success=True, ip_address=client_ip, clear_on_success=False)

        # Issue secure 32-byte session token; store SHA-256 hash in DB
        raw_token = secrets.token_hex(32)
        token_hash_val = hash_token(raw_token)
        expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)

        session_entry = SessionModel(
            token_hash=token_hash_val,
            user_id=user.id,
            expires_at=expires,
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent"),
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db.add(session_entry)

        log_security_audit(
            "LOGIN_SUCCESS", "USER", str(user.id), user_id=user.id, username=user.username, details={"ip": client_ip}, ip_address=client_ip, request_id=req_id, db=db
        )
        db.commit()

        return {
            "success": True,
            "token": raw_token,
            "user": {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "full_name": user.full_name,
            },
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
        with get_db_session() as db:
            session = db.scalar(select(SessionModel).where(SessionModel.token_hash == token_hashed))
            if session:
                user_id = session.user_id
                db.delete(session)
                log_security_audit("LOGOUT", "USER", str(user_id), user_id=user_id, details={"status": "revoked"}, request_id=req_id, db=db)
                db.commit()
            else:
                db.execute(delete(SessionModel).where(SessionModel.token_hash == token_hashed))
                db.commit()

    prune_old_login_attempts()
    return {"success": True, "message": "Logged out"}


@router.post("/change-password")
def change_password(req: PasswordChangeRequest, request: Request, current_user: dict = Depends(get_current_user)):
    req_id = getattr(request.state, "request_id", None)
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    with get_db_session() as db:
        user = db.scalar(select(User).where(User.id == current_user["id"]))
        if not user or not verify_password_argon2(req.current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")

        user.password_hash = hash_password_argon2(req.new_password)
        # Invalidate all active sessions for this user so re-login is required
        db.execute(delete(SessionModel).where(SessionModel.user_id == current_user["id"]))

        log_security_audit(
            "PASSWORD_CHANGED", "USER", str(user.id), user_id=user.id, username=user.username, details={"status": "password updated"}, request_id=req_id, db=db
        )
        db.commit()

    return {"success": True, "message": "Password changed successfully. Please log in again."}
