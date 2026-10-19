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
import logging
import os
import re
import secrets
from typing import Callable, Optional
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, select, update
from app.core.database import get_db_session
from app.models.domain_models import AuditLog, LoginAttempt, SessionModel, User
from app.core.auth_security import (
    clear_session_cookies,
    generate_csrf_token,
    get_session_cookie_name,
    is_production_or_staging_env,
    is_session_cookie_secure,
    set_session_cookies,
    validate_csrf_token,
    validate_same_origin,
    validate_strong_password,
)

router = APIRouter(prefix="/api/auth", tags=["Auth"])
logger = logging.getLogger("collectflow.auth")
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


def extract_auth_credential(
    request: Request = None,
    authorization: Optional[str] = None,
    bearer_creds: Optional[HTTPAuthorizationCredentials] = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    Extracts authentication credentials from request.
    Returns (raw_token, auth_type) where auth_type is 'cookie' or 'bearer'.
    Enforces:
    - Session cookie retrieval (__Host-collectflow_session or collectflow_dev_session)
    - Ambiguity resolution or rejection when conflicting credentials provided
    - Strict rejection of bearer authentication in production and staging environments
    """
    raw_cookie_token = None
    if request is not None:
        cookie_name = get_session_cookie_name()
        raw_cookie_token = request.cookies.get(cookie_name)
        if not raw_cookie_token:
            # Check alternate dev/host cookie in non-prod
            alt_name = "collectflow_dev_session" if cookie_name == "__Host-collectflow_session" else "__Host-collectflow_session"
            raw_cookie_token = request.cookies.get(alt_name)

    raw_bearer_token = None
    if bearer_creds and hasattr(bearer_creds, "credentials") and bearer_creds.credentials:
        raw_bearer_token = bearer_creds.credentials.strip()
    elif authorization and authorization.startswith("Bearer "):
        raw_bearer_token = authorization.split(" ", 1)[1].strip()
    elif authorization and not raw_cookie_token and not authorization.startswith("Cookie "):
        raw_bearer_token = authorization.strip()

    # Reject or resolve ambiguity if both are supplied
    if raw_cookie_token and raw_bearer_token:
        reject_ambiguous = (
            os.environ.get("COLLECTFLOW_REJECT_AMBIGUOUS_AUTH", "false").strip().lower() == "true"
            or (request is not None and request.headers.get("X-Reject-Ambiguous-Auth") == "true")
        )
        if reject_ambiguous and raw_cookie_token != raw_bearer_token:
            raise HTTPException(
                status_code=401,
                detail="Ambiguous authentication: conflicting session credentials provided.",
            )
        # In production/staging, bearer auth is strictly forbidden
        if is_production_or_staging_env():
            raise HTTPException(
                status_code=401,
                detail="Bearer token authentication is forbidden in production and staging environments. Use secure cookie sessions.",
            )
        # Strict precedence: in test/dev environments, when explicit Authorization header is passed,
        # Bearer takes precedence to support test clients retaining session cookies in their cookie jar.
        logger.info("Both Authorization header and cookie supplied. Strict precedence enforced: Authorization header takes precedence.")
        return raw_bearer_token, "bearer"

    if raw_bearer_token:
        if is_production_or_staging_env():
            raise HTTPException(
                status_code=401,
                detail="Bearer token authentication is forbidden in production and staging environments. Use secure cookie sessions.",
            )
        env_mode = (os.environ.get("COLLECTFLOW_ENV") or "").strip().lower()
        allow_bearer = (
            os.environ.get("COLLECTFLOW_ALLOW_BEARER_AUTH", "false").strip().lower() == "true"
            or env_mode in ("test", "local_test", "integration_test", "development", "dev")
        )
        if not allow_bearer:
            raise HTTPException(
                status_code=401,
                detail="Bearer token authentication is disabled. Cookie session authentication required.",
            )
        return raw_bearer_token, "bearer"

    if raw_cookie_token:
        return raw_cookie_token, "cookie"

    return None, None


def get_current_user_allow_pending_password_change(
    request: Request = None,
    authorization: Optional[str] = Header(None),
    bearer_creds: Optional[HTTPAuthorizationCredentials] = Depends(http_bearer),
) -> dict:
    """
    Session authentication dependency returning the active user context.
    Permits accounts with must_change_password=True strictly for password rotation workflows
    (/api/auth/me, /api/auth/csrf-token, /api/auth/change-password, /api/auth/logout).
    Enforces:
    - Session cookie retrieval (__Host-collectflow_session or collectflow_dev_session)
    - Ambiguity rejection or strict precedence when conflicting credentials provided
    - Strict rejection of bearer authentication in production and staging environments
    - CSRF token verification and same-origin validation on cookie-authenticated state-changing requests
    """
    raw_token, auth_type = extract_auth_credential(request, authorization, bearer_creds)
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
            )
        )
        result = db.execute(stmt).first()
        if not result:
            raise HTTPException(status_code=401, detail="Session expired or invalid token")

        user, session = result
        if not user.is_active:
            raise HTTPException(status_code=401, detail="User account is inactive or disabled.")

        csrf_token = generate_csrf_token(session.token_hash)

        # CSRF and Same-Origin protection on cookie-authenticated state-changing methods
        if auth_type == "cookie" and request is not None and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            validate_same_origin(request, auth_type="cookie")
            validate_csrf_token(session.token_hash, request.headers.get("X-CSRF-Token"))

        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "full_name": user.full_name,
            "is_active": user.is_active,
            "must_change_password": getattr(user, "must_change_password", False),
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
            "csrf_token": csrf_token,
            "auth_type": auth_type,
            "session_token_hash": session.token_hash,
        }


def get_current_user(
    request: Request = None,
    authorization: Optional[str] = Header(None),
    bearer_creds: Optional[HTTPAuthorizationCredentials] = Depends(http_bearer),
) -> dict:
    """
    Standard RBAC dependency returning authenticated user context.
    Strictly enforces mandatory password rotation: blocks all application data, mutation,
    and business APIs when must_change_password is True, returning HTTP 403 PASSWORD_CHANGE_REQUIRED.
    Supports direct programmatic invocation (e.g. from export router ticket binding checks).
    """
    current_user = get_current_user_allow_pending_password_change(
        request=request,
        authorization=authorization,
        bearer_creds=bearer_creds,
    )
    if current_user.get("must_change_password"):
        raise HTTPException(
            status_code=403,
            detail="PASSWORD_CHANGE_REQUIRED: Mandatory password rotation required before accessing application resources.",
            headers={"X-Password-Change-Required": "true"},
        )
    return current_user


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
def login(req: LoginRequest, request: Request, response: Response):
    # 1. Login CSRF protection via same-origin verification
    validate_same_origin(request)

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

        # Success: record attempt and clear user rate failures
        record_login_attempt(user_rate_key, success=True, ip_address=client_ip, clear_on_success=True)
        record_login_attempt(ip_rate_key, success=True, ip_address=client_ip, clear_on_success=False)

        # Session Fixation Protection: generate fresh 32-byte session token
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

        # Generate CSRF token bound to this session
        csrf_token = generate_csrf_token(token_hash_val)

        # Set secure HttpOnly session cookie and readable CSRF cookie
        set_session_cookies(response, raw_token, csrf_token)

        payload = {
            "success": True,
            "csrf_token": csrf_token,
            "user": {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "full_name": user.full_name,
                "must_change_password": getattr(user, "must_change_password", False),
            },
        }

        # Narrowly scoped test compatibility: return token only in non-prod test mode when allowed
        if not is_production_or_staging_env():
            env_mode = (os.environ.get("COLLECTFLOW_ENV") or "").strip().lower()
            if (
                os.environ.get("COLLECTFLOW_ALLOW_BEARER_AUTH", "false").strip().lower() == "true"
                or env_mode in ("test", "local_test", "integration_test")
                or request.headers.get("X-CollectFlow-Test-Transport") == "bearer"
            ):
                payload["token"] = raw_token

        return payload


@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user_allow_pending_password_change)):
    return {
        "authenticated": True,
        "csrf_token": current_user.get("csrf_token"),
        "user": {
            "id": current_user["id"],
            "username": current_user["username"],
            "role": current_user["role"],
            "full_name": current_user["full_name"],
            "is_active": current_user["is_active"],
            "must_change_password": current_user.get("must_change_password", False),
            "expires_at": current_user.get("expires_at"),
        },
    }


@router.get("/csrf-token")
def get_csrf_token(current_user: dict = Depends(get_current_user_allow_pending_password_change)):
    return {"csrf_token": current_user.get("csrf_token")}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(None),
    bearer_creds: Optional[HTTPAuthorizationCredentials] = Depends(http_bearer),
):
    req_id = getattr(request.state, "request_id", None)
    raw_token, auth_type = extract_auth_credential(request, authorization, bearer_creds)

    if raw_token:
        token_hashed = hash_token(raw_token)
        # If authenticated via cookie, validate CSRF and same-origin
        if auth_type == "cookie":
            validate_same_origin(request, auth_type="cookie")
            validate_csrf_token(token_hashed, request.headers.get("X-CSRF-Token"))

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

    clear_session_cookies(response)
    prune_old_login_attempts()
    return {"success": True, "message": "Logged out successfully"}


@router.post("/change-password")
def change_password(
    req: PasswordChangeRequest,
    request: Request,
    response: Response,
    current_user: dict = Depends(get_current_user_allow_pending_password_change),
):
    req_id = getattr(request.state, "request_id", None)
    user_id = current_user["id"]

    # 1. Enforce corporate strong password policy
    validate_strong_password(req.new_password)

    with get_db_session() as db:
        user = db.scalar(select(User).where(User.id == user_id))
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="User account not found or inactive.")

        # 2. Verify current password
        if not verify_password_argon2(req.current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")

        # 3. Reject reuse of current password
        if verify_password_argon2(req.new_password, user.password_hash):
            raise HTTPException(status_code=400, detail="New password cannot be the same as the current password.")

        # 4. Hash new password with Argon2id and clear must_change_password flag
        user.password_hash = hash_password_argon2(req.new_password)
        user.must_change_password = False

        # 5. Revoke ALL active sessions for this user
        db.execute(delete(SessionModel).where(SessionModel.user_id == user_id))
        log_security_audit(
            "PASSWORD_CHANGED",
            "USER",
            str(user.id),
            user_id=user.id,
            username=user.username,
            details={"status": "password rotated, all sessions revoked"},
            request_id=req_id,
            db=db,
        )
        db.commit()

    # 6. Clear session cookies on response (requires fresh login)
    clear_session_cookies(response)

    return {"success": True, "message": "Password changed successfully. Please log in again with your new password."}
