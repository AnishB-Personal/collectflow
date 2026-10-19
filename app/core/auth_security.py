"""Security, Cookie, and CSRF Protection Infrastructure (Release Preparation WP-04).

Enforces:
1. Environment-aware session cookie configuration:
   - Production / Staging: '__Host-collectflow_session' with Secure, HttpOnly, SameSite=Lax, Path=/, Host-Only.
   - Local Dev / Test: 'collectflow_dev_session' (or '__Host-' when HTTPS enabled).
   - Strict rejection of insecure cookie overrides in production/staging environments.
2. Cryptographically bound HMAC-SHA256 CSRF protection:
   - CSRF tokens derived from session token hash using server-side secret.
   - Non-HttpOnly 'collectflow_csrf_token' cookie and response payloads for frontend accessibility.
   - Constant-time verification on all state-changing operations (POST, PUT, PATCH, DELETE).
3. Same-origin validation (Origin / Referer inspection against Host and CORS configuration).
4. Strong password policy enforcement for mandatory password rotation and account bootstrap.
5. Strict separation between browser cookie authentication and test-only bearer authentication.
"""

import hashlib
import hmac
import os
import re
import secrets
import urllib.parse
from typing import Optional

from fastapi import HTTPException, Request, Response

# Prohibited weak and synthetic test passwords
PROHIBITED_PASSWORDS = {
    "adminpass123!",
    "collectflow123!",
    "password12345",
    "admin12345678",
    "welcome12345!",
}

# Production and staging environment tokens
PRODUCTION_STAGING_ENV_TOKENS = ("prod", "stag")

# 7 days in seconds
SESSION_COOKIE_MAX_AGE = 7 * 24 * 3600

# Ephemeral startup secret if none configured in environment
_STARTUP_SECRET = secrets.token_hex(32)


def is_production_or_staging_env(env_mode: Optional[str] = None) -> bool:
    """Return True if the target environment is production or staging."""
    mode = (env_mode or os.environ.get("COLLECTFLOW_ENV") or "").strip().lower()
    return any(token in mode for token in PRODUCTION_STAGING_ENV_TOKENS)


def _read_secret_from_env_or_file(env_var: str, file_var: str) -> Optional[str]:
    file_path = os.environ.get(file_var)
    if file_path and os.path.isfile(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            val = f.read().strip()
            if val:
                return val
    val = os.environ.get(env_var)
    if val and val.strip():
        return val.strip()
    return None


def get_csrf_secret(env_mode: Optional[str] = None) -> bytes:
    """Return the secret key used for HMAC-SHA256 CSRF token binding.

    In production and staging, strictly requires a persistent configured secret
    via COLLECTFLOW_CSRF_SECRET_FILE, COLLECTFLOW_CSRF_SECRET,
    COLLECTFLOW_SECRET_KEY_FILE, or COLLECTFLOW_SECRET_KEY (minimum 32 characters).
    Fails closed if missing or insecure.
    In local dev/test, falls back to a persistent startup secret.
    """
    secret = (
        _read_secret_from_env_or_file("COLLECTFLOW_CSRF_SECRET", "COLLECTFLOW_CSRF_SECRET_FILE")
        or _read_secret_from_env_or_file("COLLECTFLOW_SECRET_KEY", "COLLECTFLOW_SECRET_KEY_FILE")
    )

    if is_production_or_staging_env(env_mode):
        if not secret or len(secret) < 32:
            raise RuntimeError(
                "CRITICAL SECURITY VIOLATION: Stable CSRF secret must be configured in production/staging "
                "environments via COLLECTFLOW_CSRF_SECRET, COLLECTFLOW_CSRF_SECRET_FILE, "
                "COLLECTFLOW_SECRET_KEY, or COLLECTFLOW_SECRET_KEY_FILE (minimum 32 characters)."
            )
        return secret.encode("utf-8")

    if not secret:
        secret = _STARTUP_SECRET
    return secret.encode("utf-8")


def get_session_cookie_name(env_mode: Optional[str] = None) -> str:
    """
    Determine the appropriate session cookie name based on environment and TLS requirements.
    In production and staging, strictly requires '__Host-collectflow_session'.
    In local dev/test, uses 'collectflow_dev_session' unless TLS is explicitly enabled.
    """
    is_prod_staging = is_production_or_staging_env(env_mode)
    cookie_secure_override = os.environ.get("COLLECTFLOW_COOKIE_SECURE")

    if is_prod_staging:
        if cookie_secure_override is not None and cookie_secure_override.strip().lower() == "false":
            raise RuntimeError(
                "CRITICAL SECURITY VIOLATION: Insecure session cookies (COLLECTFLOW_COOKIE_SECURE=false) "
                "are strictly forbidden in production and staging environments."
            )
        return "__Host-collectflow_session"

    # Development or isolated test environment
    if cookie_secure_override is not None and cookie_secure_override.strip().lower() == "true":
        return "__Host-collectflow_session"

    return "collectflow_dev_session"


def is_session_cookie_secure(env_mode: Optional[str] = None) -> bool:
    """
    Determine whether session cookies must be marked 'Secure'.
    Fails closed if insecure cookies are attempted in production or staging.
    """
    is_prod_staging = is_production_or_staging_env(env_mode)
    cookie_secure_override = os.environ.get("COLLECTFLOW_COOKIE_SECURE")

    if is_prod_staging:
        if cookie_secure_override is not None and cookie_secure_override.strip().lower() == "false":
            raise RuntimeError(
                "CRITICAL SECURITY VIOLATION: Insecure session cookies (COLLECTFLOW_COOKIE_SECURE=false) "
                "are strictly forbidden in production and staging environments."
            )
        return True

    if cookie_secure_override is not None and cookie_secure_override.strip().lower() == "true":
        return True

    return False


def validate_security_configuration(env_mode: Optional[str] = None) -> None:
    """Validate critical security configuration for the application environment.

    In production and staging:
    - Strictly validates presence and complexity (minimum 32 characters) of stable CSRF secret.
    - Strictly validates session cookie security flags (COLLECTFLOW_COOKIE_SECURE cannot be false).
    - Fails closed immediately by raising RuntimeError.

    In local dev / test environments:
    - Verifies default secret generation and non-conflicting settings without raising.
    """
    is_prod_staging = is_production_or_staging_env(env_mode)
    if is_prod_staging:
        # 1. Validate required stable CSRF secret
        get_csrf_secret(env_mode=env_mode)
        # 2. Validate required production cookie security settings
        get_session_cookie_name(env_mode=env_mode)
        is_session_cookie_secure(env_mode=env_mode)
    else:
        # Dev / test environment: ensure get_csrf_secret() initializes cleanly
        get_csrf_secret(env_mode=env_mode)


def set_session_cookies(response: Response, raw_token: str, csrf_token: str) -> None:
    """
    Attach secure authentication cookies to the outgoing HTTP response.
    1. Session cookie: HttpOnly, Host-Only (no domain), SameSite=Lax, Path=/, Max-Age=7d.
    2. CSRF cookie: Non-HttpOnly (readable by frontend), SameSite=Lax, Path=/, Max-Age=7d.
    """
    cookie_name = get_session_cookie_name()
    secure_flag = is_session_cookie_secure()

    # 1. HttpOnly authentication session cookie
    response.set_cookie(
        key=cookie_name,
        value=raw_token,
        max_age=SESSION_COOKIE_MAX_AGE,
        expires=SESSION_COOKIE_MAX_AGE,
        path="/",
        domain=None,  # Host-only cookie
        secure=secure_flag,
        httponly=True,
        samesite="lax",
    )

    # 2. Separate JavaScript-accessible CSRF token cookie
    response.set_cookie(
        key="collectflow_csrf_token",
        value=csrf_token,
        max_age=SESSION_COOKIE_MAX_AGE,
        expires=SESSION_COOKIE_MAX_AGE,
        path="/",
        domain=None,
        secure=secure_flag,
        httponly=False,  # Intentionally accessible to legitimate same-origin JavaScript
        samesite="lax",
    )


def clear_session_cookies(response: Response) -> None:
    """Clear all possible authentication and CSRF cookies from the client browser.

    RFC 6265bis requires that '__Host-' cookies must be deleted with secure=True, path='/',
    and domain=None to be accepted by secure browsers.
    """
    response.delete_cookie(
        key="__Host-collectflow_session",
        path="/",
        domain=None,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        key="collectflow_dev_session",
        path="/",
        domain=None,
        secure=is_session_cookie_secure(),
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        key="collectflow_csrf_token",
        path="/",
        domain=None,
        secure=is_session_cookie_secure(),
        httponly=False,
        samesite="lax",
    )


def generate_csrf_token(session_token_hash: str) -> str:
    """
    Generate a cryptographically unpredictable HMAC-SHA256 CSRF token bound to the specific session.
    The CSRF token is completely separate from the raw authentication token.
    """
    secret = get_csrf_secret()
    return hmac.new(secret, session_token_hash.encode("utf-8"), hashlib.sha256).hexdigest()


def validate_csrf_token(session_token_hash: str, submitted_token: Optional[str]) -> None:
    """
    Verify submitted CSRF token using constant-time comparison.
    Fails closed if the token is missing or mismatched.
    """
    if not submitted_token or not submitted_token.strip():
        raise HTTPException(
            status_code=403,
            detail="CSRF_TOKEN_MISSING: Missing required X-CSRF-Token header on state-changing request.",
        )

    expected_token = generate_csrf_token(session_token_hash)
    if not secrets.compare_digest(submitted_token.strip(), expected_token):
        raise HTTPException(
            status_code=403,
            detail="CSRF_TOKEN_INVALID: CSRF token is invalid or does not match active session.",
        )


def validate_same_origin(request: Request, auth_type: str = "cookie") -> None:
    """
    Validate that incoming state-changing requests originate from an authorized same-origin host.
    Protects against cross-origin state changes, CSRF, and login CSRF.
    """
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    host = request.headers.get("host")

    target_origin = origin
    if not target_origin and referer:
        parsed_ref = urllib.parse.urlparse(referer)
        target_origin = f"{parsed_ref.scheme}://{parsed_ref.netloc}"

    if not target_origin:
        if is_production_or_staging_env() and auth_type == "cookie":
            raise HTTPException(
                status_code=403,
                detail="ORIGIN_REQUIRED: Missing Origin or Referer header on cookie-authenticated request in production.",
            )
        return

    parsed_target = urllib.parse.urlparse(target_origin)
    target_netloc = parsed_target.netloc

    # Direct same-origin match against Host header
    if host and target_netloc == host:
        return

    # Check explicitly configured allowed origins
    raw_allowed = os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000,http://localhost:8080,http://127.0.0.1:8080",
    )
    allowed = {o.strip() for o in raw_allowed.split(",") if o.strip() and o.strip() != "*"}

    full_target = f"{parsed_target.scheme}://{target_netloc}"
    if target_origin in allowed or full_target in allowed:
        return

    raise HTTPException(
        status_code=403,
        detail=f"ORIGIN_FORBIDDEN: Request origin '{target_origin}' is not permitted.",
    )


def validate_strong_password(password: str) -> None:
    """
    Validate password complexity against corporate security policies.
    Requires at least 12 characters, uppercase, lowercase, digit, and special character.
    Rejects known weak or synthetic test passwords.
    """
    if not password:
        raise HTTPException(status_code=400, detail="Password cannot be empty.")
    if len(password) < 12:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 12 characters long.",
        )
    if not re.search(r"[A-Z]", password):
        raise HTTPException(
            status_code=400,
            detail="Password must contain at least one uppercase letter.",
        )
    if not re.search(r"[a-z]", password):
        raise HTTPException(
            status_code=400,
            detail="Password must contain at least one lowercase letter.",
        )
    if not re.search(r"[0-9]", password):
        raise HTTPException(
            status_code=400,
            detail="Password must contain at least one digit.",
        )
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]", password):
        raise HTTPException(
            status_code=400,
            detail="Password must contain at least one special character.",
        )
    if password.strip().lower() in PROHIBITED_PASSWORDS:
        raise HTTPException(
            status_code=400,
            detail="Password matches a known weak or synthetic test password.",
        )
