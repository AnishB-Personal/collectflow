"""Production-Safe Database Initializer & Administrator Bootstrap (Release Preparation WP-03).

Provides a secure, repeatable, deterministic initialization workflow for PostgreSQL-backed CollectFlow databases.

Enforces:
1. Verification of environment mode and target database configuration.
2. Verification of expected Alembic schema revision ('77e73a1cb221').
3. Strict refusal of test databases ('collectflow_test') or unknown/unsafe targets.
4. Pre-existing business and identity data validation (fails closed if non-empty, inconsistent, or unprovenanced).
5. Transaction-scoped PostgreSQL advisory lock serialization (pg_advisory_xact_lock) for administrator bootstrap.
6. Verification of bootstrap provenance on repeat initialization using BOTH stable identifiers (user_id AND entity_id).
7. Secure bootstrap of exactly one administrator with Argon2id hash and must_change_password=True.
8. Refusal of missing, empty, weak, or known synthetic/demo passwords and usernames.
9. Atomic idempotent execution: repeated runs perform a safe verified no-op without resetting passwords.
10. Controlled statutory reference data initialization: DEFERRED by default pending formal Finance sign-offs,
    only populated when an explicit, versioned, genuine Finance-approved configuration is supplied (approval_status=finance_approved).
11. Explicit rejection of synthetic test fixtures for production/staging authorization across CLI and callable APIs.
12. Safe verification-only mode (--verify-only) performing zero writes.
13. Strict omission of synthetic customers, orders, invoices, payments, or targets.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argon2 import PasswordHasher
from sqlalchemy import func, select, text
from app.core.database import get_database_url, get_db_session, read_secret_file, sanitize_credentials
from app.models.domain_models import (
    AuditLog,
    CommercialInvoice,
    Customer,
    FinancialYear,
    GstRateMaster,
    PaymentTransaction,
    PurchaseOrder,
    SalesTarget,
    SessionModel,
    TdsSectionMaster,
    User,
)

EXPECTED_ALEMBIC_REVISION = "77e73a1cb221"
ADMIN_BOOTSTRAP_LOCK_ID = 847291048291048291

PROHIBITED_BOOTSTRAP_USERNAMES = {
    "sandbox_admin",
    "sandbox_finance",
    "sandbox_sales",
    "sandbox_viewer",
    "test_admin",
}

PROHIBITED_BOOTSTRAP_PASSWORDS = {
    "adminpass123!",
    "collectflow123!",
    "password12345",
    "admin12345678",
    "welcome12345!",
}

APPROVED_TEST_FIXTURE_ENVS = {"test", "local_test", "integration_test", "integration"}

ph = PasswordHasher()


def assert_test_fixture_permitted(env_mode: str) -> None:
    """
    Enforce that synthetic test fixtures are strictly forbidden in production, prod, staging, and non-test modes.
    Enforces WP03-F02 consistently across CLI and callable initialization functions.
    """
    cleaned_env = (env_mode or "").strip().lower()
    for token in ("prod", "stag"):
        if token in cleaned_env:
            raise RuntimeError(
                f"CRITICAL SAFETY ABORT: Synthetic reference test fixtures are strictly forbidden in production and staging environments (got COLLECTFLOW_ENV='{cleaned_env}')."
            )
    if cleaned_env not in APPROVED_TEST_FIXTURE_ENVS:
        raise RuntimeError(
            f"CRITICAL SAFETY ABORT: Synthetic reference test fixtures are permitted only in explicitly approved isolated local integration-test mode (got COLLECTFLOW_ENV='{cleaned_env}')."
        )


def acquire_bootstrap_advisory_lock(db) -> None:
    """
    Acquire transaction-scoped PostgreSQL advisory lock (pg_advisory_xact_lock) for bootstrap serialization.
    Ensures that concurrent or duplicate bootstrap attempts are strictly serialized at the database level.
    The lock is released automatically when the transaction commits or rolls back.
    """
    if db.bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id);"),
            {"lock_id": ADMIN_BOOTSTRAP_LOCK_ID},
        )


def validate_admin_username(username: str) -> str:
    """Validate administrator username requirements."""
    if not username or not username.strip():
        raise ValueError("Bootstrap administrator username cannot be empty.")
    cleaned = username.strip().lower()
    if len(cleaned) < 3 or len(cleaned) > 50:
        raise ValueError("Administrator username must be between 3 and 50 characters.")
    if not re.match(r"^[a-zA-Z0-9_.@-]+$", cleaned):
        raise ValueError("Administrator username can only contain alphanumeric characters, ., _, @, -.")
    if cleaned in PROHIBITED_BOOTSTRAP_USERNAMES:
        raise ValueError(f"Prohibited username '{cleaned}': Cannot reuse synthetic fixture username.")
    return cleaned


def validate_admin_password(password: str) -> None:
    """Validate administrator password complexity."""
    if not password:
        raise ValueError("Bootstrap administrator password cannot be empty.")
    if len(password) < 12:
        raise ValueError("Bootstrap administrator password must be at least 12 characters long.")
    if not re.search(r"[A-Z]", password):
        raise ValueError("Bootstrap administrator password must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", password):
        raise ValueError("Bootstrap administrator password must contain at least one lowercase letter.")
    if not re.search(r"[0-9]", password):
        raise ValueError("Bootstrap administrator password must contain at least one digit.")
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]", password):
        raise ValueError("Bootstrap administrator password must contain at least one special character.")
    if password.strip().lower() in PROHIBITED_BOOTSTRAP_PASSWORDS:
        raise ValueError("Bootstrap administrator password matches a known weak or synthetic test password.")


def get_bootstrap_admin_credentials() -> tuple[str, str | None]:
    """Retrieve bootstrap admin username and password from environment or secret files."""
    raw_user = os.environ.get("COLLECTFLOW_BOOTSTRAP_ADMIN_USERNAME", "admin")
    username = validate_admin_username(raw_user)

    # 1. Check secret file
    pw_file = os.environ.get("COLLECTFLOW_BOOTSTRAP_ADMIN_PASSWORD_FILE")
    password = None
    if pw_file:
        password = read_secret_file(pw_file)
    elif "COLLECTFLOW_BOOTSTRAP_ADMIN_PASSWORD" in os.environ:
        password = os.environ.get("COLLECTFLOW_BOOTSTRAP_ADMIN_PASSWORD")

    return username, password


def verify_target_environment(db) -> dict:
    """
    Rigorously verify environment mode, database configuration, Alembic schema revision,
    pre-existing business data, and identity provenance (WP03-R01 / WP03-F01).
    Fails closed on unsafe, test, corrupted, unprovenanced, or mismatched database targets.
    """
    env_mode = (os.environ.get("COLLECTFLOW_ENV") or "").strip().lower()
    if not env_mode:
        raise RuntimeError("CRITICAL INITIALIZATION ERROR: COLLECTFLOW_ENV environment variable is required.")

    if env_mode == "test":
        raise RuntimeError(
            "CRITICAL INITIALIZATION ERROR: Production initializer cannot run in 'test' environment mode. "
            "Use scripts/seed_postgres_fixtures.py for the test environment."
        )

    db_url = db.bind.url
    db_name = (db_url.database or "").strip().lower()

    if db_name == "collectflow_test":
        raise RuntimeError(
            f"CRITICAL INITIALIZATION ERROR: Refusing to initialize test database '{db_name}'. "
            "Production initializer is not permitted on collectflow_test."
        )

    # Verify Alembic migration revision
    try:
        current_rev = db.execute(text("SELECT version_num FROM alembic_version;")).scalar()
    except Exception as exc:
        raise RuntimeError(
            f"CRITICAL INITIALIZATION ERROR: Unable to query alembic_version table: {sanitize_credentials(str(exc))}. "
            "Please ensure database migrations have been executed with 'alembic upgrade head' before initializing."
        ) from None

    if current_rev != EXPECTED_ALEMBIC_REVISION:
        raise RuntimeError(
            f"CRITICAL INITIALIZATION ERROR: Database schema revision mismatch. "
            f"Expected '{EXPECTED_ALEMBIC_REVISION}', but found '{current_rev}'. "
            "Please execute 'alembic upgrade head' before running the initializer."
        )

    # Check for pre-existing financial business data
    customer_count = db.scalar(select(func.count()).select_from(Customer)) or 0
    order_count = db.scalar(select(func.count()).select_from(PurchaseOrder)) or 0
    invoice_count = db.scalar(select(func.count()).select_from(CommercialInvoice)) or 0
    payment_count = db.scalar(select(func.count()).select_from(PaymentTransaction)) or 0
    target_count = db.scalar(select(func.count()).select_from(SalesTarget)) or 0

    total_business_records = customer_count + order_count + invoice_count + payment_count + target_count
    if total_business_records > 0:
        raise RuntimeError(
            f"CRITICAL INITIALIZATION ERROR: Pre-existing business data detected "
            f"({customer_count} customers, {order_count} orders, {invoice_count} invoices, "
            f"{payment_count} payments, {target_count} targets). "
            "Production initializer can only initialize clean application databases."
        )

    # Check identity and session state (WP03-03 and WP03-F01)
    user_count = db.scalar(select(func.count()).select_from(User)) or 0
    admin_count = db.scalar(select(func.count()).select_from(User).where(User.role == "admin")) or 0
    session_count = db.scalar(select(func.count()).select_from(SessionModel)) or 0

    if user_count > 0 and admin_count == 0:
        raise RuntimeError(
            f"CRITICAL INITIALIZATION ERROR: Pre-existing user accounts detected ({user_count} users), "
            "but no legitimate administrator exists. Cannot perform first-time bootstrap on an inconsistent identity database."
        )

    if user_count == 0 and session_count > 0:
        raise RuntimeError(
            f"CRITICAL INITIALIZATION ERROR: Orphaned active sessions detected in empty database ({session_count} sessions). "
            "Target database identity state is corrupted."
        )

    if user_count > 0 and admin_count > 0:
        active_admins = db.scalars(select(User).where(User.role == "admin", User.is_active == True)).all()
        if not active_admins:
            raise RuntimeError(
                "CRITICAL INITIALIZATION ERROR: Administrator account exists but is inactive. "
                "Cannot verify legitimate bootstrap state."
            )

        # WP03-F01: Rigorous Bootstrap Provenance Verification
        bootstrap_audits = db.scalars(select(AuditLog).where(AuditLog.action == "ADMIN_BOOTSTRAPPED")).all()
        if len(bootstrap_audits) == 0:
            raise RuntimeError(
                "CRITICAL INITIALIZATION ERROR: Pre-existing administrator account exists, "
                "but required bootstrap provenance audit evidence (action='ADMIN_BOOTSTRAPPED') is completely missing. "
                "Cannot verify legitimate initialization provenance on repeat run."
            )
        if len(bootstrap_audits) > 1:
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Ambiguous or duplicate bootstrap audit records detected ({len(bootstrap_audits)} records). "
                "Exactly one initial bootstrap provenance record is permitted."
            )

        audit = bootstrap_audits[0]
        if audit.entity_type != "USER":
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Bootstrap audit record entity_type must be 'USER' (got '{audit.entity_type}')."
            )

        # WP03-F01: Require BOTH identifiers (user_id AND entity_id) to match the same administrator ID
        matching_admin = next(
            (a for a in active_admins if a.id == audit.user_id and str(a.id) == str(audit.entity_id)),
            None,
        )
        if not matching_admin:
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Bootstrap provenance identifier mismatch. "
                f"Requires BOTH user_id={audit.user_id} and entity_id='{audit.entity_id}' to match "
                f"the same active administrator ID (active admin IDs: {[a.id for a in active_admins]})."
            )
        if audit.username != matching_admin.username:
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Bootstrap provenance username conflict. "
                f"Audit record user_id={audit.user_id} specifies username '{audit.username}', "
                f"but active user #{matching_admin.id} has username '{matching_admin.username}'."
            )
        if matching_admin.role != "admin" or not matching_admin.is_active:
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Administrator account #{matching_admin.id} "
                f"must have role='admin' and is_active=True (got role='{matching_admin.role}', is_active={matching_admin.is_active})."
            )
        identity_state = "already_initialized"
    else:
        # Check that no orphaned bootstrap audit logs exist in empty user table
        orphaned_audits = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "ADMIN_BOOTSTRAPPED")) or 0
        if orphaned_audits > 0:
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Inconsistent identity state: {orphaned_audits} bootstrap audit records "
                "exist, but user table is empty. Target database identity state is corrupted."
            )
        identity_state = "clean_first_time"

    return {
        "env_mode": env_mode,
        "database": db_name,
        "alembic_revision": current_rev,
        "business_records": total_business_records,
        "identity_state": identity_state,
        "user_count": user_count,
        "admin_count": admin_count,
    }


def load_reference_config(
    config_path: str | None = None,
    allow_test_fixture: bool = False,
) -> dict | None:
    """
    Load and validate an explicit, versioned, Finance-approved statutory reference configuration file.
    Returns None if no configuration file is provided (indicating reference data should be deferred).
    Fails closed if the file is missing, invalid JSON, a synthetic test fixture in production/staging,
    or not approved by Finance with approval_status='finance_approved'.

    NOTE: Self-declared JSON fields alone do not independently prove Finance authorization.
    Configuration provenance must be corroborated against formal signed corporate finance governance records (BPD-01/02).
    """
    path_str = config_path or os.environ.get("COLLECTFLOW_STATUTORY_REFERENCE_CONFIG_FILE")
    if not path_str:
        return None

    path = Path(path_str)
    if not path.is_file():
        raise FileNotFoundError(f"CRITICAL CONFIGURATION ERROR: Reference config file '{path_str}' does not exist.")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise ValueError(f"CRITICAL CONFIGURATION ERROR: Failed to parse reference config JSON: {exc}") from None

    if not isinstance(data, dict):
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config must be a JSON object.")

    # 1. Detection and rejection of synthetic test fixtures (WP03-R02 / WP03-F02)
    is_test_fixture = bool(data.get("is_test_fixture", False))
    raw_status = str(data.get("approval_status", "")).strip()
    raw_approver = str(data.get("approved_by", "")).strip()
    raw_ref = str(data.get("approval_reference", "")).strip()
    is_synthetic = is_test_fixture or any("TEST" in s.upper() for s in (raw_status, raw_approver, raw_ref))

    if is_synthetic:
        if not allow_test_fixture:
            raise ValueError(
                "CRITICAL CONFIGURATION REJECTED: Synthetic test configuration fixture detected ('TEST ONLY — NOT FINANCE APPROVED'). "
                "Synthetic test fixtures cannot authorize production reference-data insertion. "
                "Genuine Finance Directorate authorization provenance is required."
            )
        # Even if allow_test_fixture is set, verify environment allows it (WP03-F02)
        env_mode = (os.environ.get("COLLECTFLOW_ENV") or "").strip().lower()
        assert_test_fixture_permitted(env_mode)

    # WP06-01: In production and staging, self-declared finance_approved in JSON cannot authorize production reference-data insertion.
    # Corporate Finance governance is currently AWAITING_FINANCE_APPROVAL.
    env_mode = (os.environ.get("COLLECTFLOW_ENV") or "").strip().lower()
    is_prod_or_staging = any(token in env_mode for token in ("prod", "stag")) or (
        env_mode not in APPROVED_TEST_FIXTURE_ENVS
    )
    if is_prod_or_staging and not is_synthetic:
        raise RuntimeError(
            "CRITICAL GOVERNANCE ABORT: Statutory reference configuration activation in production/staging is BLOCKED "
            "pending independently authenticated Corporate Finance authorization (BPD-01/BPD-02 status: AWAITING_FINANCE_APPROVAL). "
            "Self-declared 'approval_status=finance_approved' in JSON configuration files cannot authorize production statutory master data."
        )

    # 2. Validation of approval metadata and provenance (WP03-F02: require approval_status=finance_approved)
    approval_status = raw_status.lower()
    if not is_synthetic:
        if approval_status != "finance_approved":
            raise ValueError(
                f"CRITICAL CONFIGURATION ERROR: Statutory reference config approval_status must be 'finance_approved' "
                f"(got '{data.get('approval_status')}'). Unapproved statutory reference values cannot be populated."
            )

    if not raw_approver:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing required 'approved_by' metadata.")

    if not raw_ref:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing required 'approval_reference' governance ID.")

    approval_date = str(data.get("approval_date", "")).strip()
    if not approval_date:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing required 'approval_date' metadata.")
    try:
        app_d = date.fromisoformat(approval_date)
        if app_d > date.today():
            raise ValueError(f"Approval date '{approval_date}' cannot be in the future.")
    except Exception as exc:
        raise ValueError(f"CRITICAL CONFIGURATION ERROR: Invalid approval_date: {exc}")

    # Check expiration if present
    expiry_date_str = str(data.get("expiry_date") or data.get("expires_at") or "").strip()
    if expiry_date_str:
        try:
            exp_d = date.fromisoformat(expiry_date_str)
            if exp_d < date.today():
                raise ValueError("CRITICAL CONFIGURATION ERROR: Statutory reference approval has expired.")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Invalid expiry date: {exc}")

    controlled_source = str(data.get("controlled_source", "")).strip()
    if not controlled_source:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing required 'controlled_source' metadata.")

    config_version = str(data.get("config_version", "")).strip()
    if not config_version:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing required 'config_version' metadata.")

    effective_date = str(data.get("effective_date", "")).strip()
    if not effective_date:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing required 'effective_date' metadata.")
    try:
        date.fromisoformat(effective_date)
    except Exception:
        raise ValueError(f"CRITICAL CONFIGURATION ERROR: Invalid effective_date format '{effective_date}', expected ISO YYYY-MM-DD.")

    # 3. Validation of policy references
    policy_refs = data.get("policy_references", [])
    if not isinstance(policy_refs, list) or not policy_refs:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config must list applicable policy references (e.g. BPD-01, BPD-02).")

    # 4. Payload validation
    if "financial_years" not in data or not isinstance(data["financial_years"], list):
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing 'financial_years' list.")
    if "gst_rates" not in data or not isinstance(data["gst_rates"], list):
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing 'gst_rates' list.")
    if "tds_sections" not in data or not isinstance(data["tds_sections"], list):
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing 'tds_sections' list.")

    # WP06-01: Financial year boundary, Indian FY convention, and overlapping interval validation
    validate_financial_year_records(data["financial_years"])

    # WP06-01: GST rate consistency and rate validation
    seen_gst = set()
    for g in data["gst_rates"]:
        g_name = str(g.get("name", "")).strip()
        if not g_name:
            raise ValueError("CRITICAL CONFIGURATION ERROR: GST rate missing required 'name'.")
        if g_name in seen_gst:
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Duplicate GST rate name '{g_name}'.")
        seen_gst.add(g_name)
        try:
            r = Decimal(str(g.get("rate")))
            if r < Decimal("0.0000") or r > Decimal("1.0000"):
                raise ValueError("Rate out of bounds.")
        except Exception:
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Invalid rate for GST '{g_name}'.")
        # Check split consistency
        cgst = Decimal(str(g.get("cgst_rate", "0")))
        sgst = Decimal(str(g.get("sgst_rate", "0")))
        igst = Decimal(str(g.get("igst_rate", "0")))
        if abs((cgst + sgst) - r) > Decimal("0.0001") or abs(igst - r) > Decimal("0.0001"):
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Inconsistent GST split for '{g_name}' (CGST {cgst} + SGST {sgst} != {r}).")

    # WP06-01: TDS section code validation
    seen_tds = set()
    for t in data["tds_sections"]:
        sec = str(t.get("section_code", "")).strip()
        if not sec:
            raise ValueError("CRITICAL CONFIGURATION ERROR: TDS section missing 'section_code'.")
        if sec in seen_tds:
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Duplicate TDS section code '{sec}'.")
        seen_tds.add(sec)
        try:
            r = Decimal(str(t.get("default_rate")))
            if r < Decimal("0.0000") or r > Decimal("1.0000"):
                raise ValueError("Rate out of bounds.")
        except Exception:
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Invalid default_rate for TDS section '{sec}'.")

    return data


def validate_financial_year_records(
    financial_years: list,
    db_existing_fys: list | None = None,
) -> None:
    """
    Rigorously validates financial year boundaries and overlapping configurations (WP06-01).
    Enforces:
    1. Valid ISO start and end dates.
    2. start_date < end_date.
    3. Indian statutory financial year convention (starts April 1, ends March 31 of the following year).
    4. No duplicate FY codes.
    5. No overlapping financial year intervals within the configuration.
    6. No overlapping financial year intervals with pre-existing database records.
    """
    seen_codes = set()
    parsed_intervals = []

    for item in financial_years:
        if not isinstance(item, dict):
            raise ValueError("CRITICAL CONFIGURATION ERROR: Financial year record must be a JSON object.")
        code = str(item.get("code", "")).strip()
        if not code:
            raise ValueError("CRITICAL CONFIGURATION ERROR: Financial year missing required 'code' field.")
        if code in seen_codes:
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Duplicate financial year code '{code}' in configuration.")
        seen_codes.add(code)

        s_str = str(item.get("start_date", "")).strip()
        e_str = str(item.get("end_date", "")).strip()
        if not s_str or not e_str:
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Financial year '{code}' missing start_date or end_date.")

        try:
            sd = date.fromisoformat(s_str)
            ed = date.fromisoformat(e_str)
        except Exception as exc:
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Invalid date format in FY '{code}': {exc}") from None

        if sd >= ed:
            raise ValueError(
                f"CRITICAL CONFIGURATION ERROR: Financial year '{code}' start_date ({sd}) must be before end_date ({ed})."
            )

        # Indian statutory FY boundary check (starts April 1, ends March 31 of next year)
        if not (sd.month == 4 and sd.day == 1 and ed.month == 3 and ed.day == 31 and ed.year == sd.year + 1):
            raise ValueError(
                f"CRITICAL CONFIGURATION ERROR: Financial year '{code}' ({sd} to {ed}) violates Indian statutory "
                "financial year convention (April 1 to March 31)."
            )

        for other_code, other_sd, other_ed in parsed_intervals:
            if sd <= other_ed and ed >= other_sd:
                raise ValueError(
                    f"CRITICAL CONFIGURATION ERROR: Overlapping financial year boundaries detected between "
                    f"'{code}' ({sd} to {ed}) and '{other_code}' ({other_sd} to {other_ed})."
                )
        parsed_intervals.append((code, sd, ed))

    if db_existing_fys:
        for code, sd, ed in parsed_intervals:
            for existing in db_existing_fys:
                if existing.code != code and existing.is_active:
                    if sd <= existing.end_date and ed >= existing.start_date:
                        raise ValueError(
                            f"CRITICAL INITIALIZATION ERROR: Financial year '{code}' ({sd} to {ed}) overlaps with "
                            f"existing database financial year '{existing.code}' ({existing.start_date} to {existing.end_date})."
                        )


def init_reference_data(db, ref_config: dict | None = None, verify_only: bool = False) -> dict:
    """
    Initialize statutory reference data only when an explicit Finance-approved configuration is provided.
    If ref_config is None, reference data initialization is deferred safely.
    Validates governance authorization and configuration integrity before any database mutation (WP06-02).
    """
    if ref_config is None:
        return {
            "status": "deferred",
            "message": (
                "Statutory reference data initialization DEFERRED. "
                "No approved statutory reference configuration was supplied. "
                "Reference tables (financial_years, gst_rate_master, tds_section_master) remain unpopulated "
                "pending formal Finance sign-off on BPD-01 (Statutory Reference Masters Policy) and "
                "BPD-02 (Default Tax & Statutory Configurations)."
            ),
            "financial_years": 0,
            "gst_rates": 0,
            "tds_sections": 0,
        }

    # WP06-02: Protect direct Python function calls. In production and staging, refuse activation
    # of statutory reference configuration without independently authenticated Corporate Finance approval.
    env_mode = (os.environ.get("COLLECTFLOW_ENV") or "").strip().lower()
    is_prod_or_staging = any(token in env_mode for token in ("prod", "stag")) or (
        env_mode not in APPROVED_TEST_FIXTURE_ENVS
    )
    if is_prod_or_staging:
        raise RuntimeError(
            "CRITICAL GOVERNANCE ABORT: Direct statutory reference initialization in production/staging is BLOCKED "
            "pending independently authenticated Corporate Finance authorization (BPD-01/BPD-02 status: AWAITING_FINANCE_APPROVAL)."
        )

    # Validate structure and required governance fields before mutations
    if not isinstance(ref_config, dict):
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config must be a dictionary.")

    is_test_fixture = bool(ref_config.get("is_test_fixture", False))
    raw_status = str(ref_config.get("approval_status", "")).strip()
    raw_approver = str(ref_config.get("approved_by", "")).strip()
    raw_ref = str(ref_config.get("approval_reference", "")).strip()
    is_synthetic = is_test_fixture or any("TEST" in s.upper() for s in (raw_status, raw_approver, raw_ref))

    if is_synthetic:
        assert_test_fixture_permitted(env_mode)
    else:
        if raw_status.lower() != "finance_approved":
            raise ValueError(
                f"CRITICAL CONFIGURATION ERROR: Statutory reference config approval_status must be 'finance_approved' "
                f"(got '{raw_status}'). Unapproved statutory reference values cannot be populated."
            )

    required_governance_fields = [
        "config_version",
        "approved_by",
        "approval_reference",
        "approval_date",
        "controlled_source",
        "effective_date",
        "policy_references",
        "financial_years",
        "gst_rates",
        "tds_sections",
    ]
    for field in required_governance_fields:
        if field not in ref_config or ref_config[field] is None:
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Reference config missing required '{field}' governance metadata.")

    # Validate approval date
    app_date_str = str(ref_config.get("approval_date", "")).strip()
    if not app_date_str:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing required 'approval_date' metadata.")
    try:
        app_d = date.fromisoformat(app_date_str)
        if app_d > date.today():
            raise ValueError(f"Approval date '{app_date_str}' cannot be in the future.")
    except Exception as exc:
        raise ValueError(f"CRITICAL CONFIGURATION ERROR: Invalid approval_date: {exc}")

    expiry_date_str = str(ref_config.get("expiry_date") or ref_config.get("expires_at") or "").strip()
    if expiry_date_str:
        try:
            exp_d = date.fromisoformat(expiry_date_str)
            if exp_d < date.today():
                raise ValueError(f"Statutory reference approval has expired on {expiry_date_str}.")
        except Exception as exc:
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: Invalid expiry_date: {exc}")

    # Validate GST rate splits
    for gst_data in ref_config.get("gst_rates", []):
        name = str(gst_data.get("name", "")).strip()
        if not name:
            raise ValueError("CRITICAL CONFIGURATION ERROR: GST rate missing required 'name' field.")
        r = Decimal(str(gst_data.get("rate", "0")))
        cgst = Decimal(str(gst_data.get("cgst_rate", "0")))
        sgst = Decimal(str(gst_data.get("sgst_rate", "0")))
        igst = Decimal(str(gst_data.get("igst_rate", "0")))
        if cgst + sgst != igst or igst != r:
            raise ValueError(
                f"CRITICAL CONFIGURATION ERROR: GST rate '{name}' split inconsistent: "
                f"CGST {cgst} + SGST {sgst} != IGST {igst} == rate {r}"
            )

    # Validate TDS sections
    for tds_data in ref_config.get("tds_sections", []):
        sec_code = str(tds_data.get("section_code", "")).strip()
        if not sec_code:
            raise ValueError("CRITICAL CONFIGURATION ERROR: TDS section missing required 'section_code' field.")
        rate_dec = Decimal(str(tds_data.get("default_rate", "0")))
        if rate_dec < Decimal("0.00") or rate_dec > Decimal("1.00"):
            raise ValueError(f"CRITICAL CONFIGURATION ERROR: TDS section '{sec_code}' rate {rate_dec} out of bounds [0.0, 1.0].")

    results = {"financial_years": 0, "gst_rates": 0, "tds_sections": 0}

    # Query existing database financial years for overlapping detection
    existing_db_fys = db.scalars(select(FinancialYear)).all()
    validate_financial_year_records(ref_config["financial_years"], db_existing_fys=existing_db_fys)

    # 1. Financial Years
    for fy_data in ref_config["financial_years"]:
        code = str(fy_data["code"]).strip()
        existing = db.scalar(select(FinancialYear).where(FinancialYear.code == code))
        if not existing:
            results["financial_years"] += 1
            if not verify_only:
                fy = FinancialYear(
                    code=code,
                    start_date=date.fromisoformat(fy_data["start_date"]),
                    end_date=date.fromisoformat(fy_data["end_date"]),
                    is_active=bool(fy_data.get("is_active", True)),
                    created_at=datetime.now(timezone.utc),
                )
                db.add(fy)

    # 2. GST Rates
    for gst_data in ref_config["gst_rates"]:
        name = str(gst_data["name"]).strip()
        existing = db.scalar(select(GstRateMaster).where(GstRateMaster.name == name))
        if not existing:
            results["gst_rates"] += 1
            if not verify_only:
                gst = GstRateMaster(
                    name=name,
                    rate=Decimal(str(gst_data["rate"])),
                    cgst_rate=Decimal(str(gst_data["cgst_rate"])),
                    sgst_rate=Decimal(str(gst_data["sgst_rate"])),
                    igst_rate=Decimal(str(gst_data["igst_rate"])),
                    is_default=bool(gst_data.get("is_default", False)),
                    is_active=bool(gst_data.get("is_active", True)),
                )
                db.add(gst)

    # 3. TDS Sections
    for tds_data in ref_config["tds_sections"]:
        sec_code = str(tds_data["section_code"]).strip()
        existing = db.scalar(select(TdsSectionMaster).where(TdsSectionMaster.section_code == sec_code))
        if not existing:
            results["tds_sections"] += 1
            if not verify_only:
                tds = TdsSectionMaster(
                    section_code=sec_code,
                    description=str(tds_data["description"]).strip(),
                    default_rate=Decimal(str(tds_data["default_rate"])),
                    is_active=bool(tds_data.get("is_active", True)),
                )
                db.add(tds)

    if not verify_only:
        db.flush()

    return {
        "status": "initialized",
        "config_version": ref_config.get("config_version"),
        "approved_by": ref_config.get("approved_by"),
        "approval_reference": ref_config.get("approval_reference"),
        "financial_years": results["financial_years"],
        "gst_rates": results["gst_rates"],
        "tds_sections": results["tds_sections"],
        "message": (
            f"Successfully populated reference data from Finance-approved config "
            f"v{ref_config.get('config_version')} (approved by {ref_config.get('approved_by')}, "
            f"ref: {ref_config.get('approval_reference')})."
        ),
    }


def bootstrap_administrator(
    db,
    admin_username: str,
    admin_password: str | None,
    verify_only: bool = False,
) -> dict:
    """
    Securely bootstrap initial administrator account with transaction-scoped concurrency serialization.
    Idempotent and atomic: repeated runs perform a safe verified no-op.
    """
    # 1. Acquire transaction-scoped PostgreSQL advisory lock (WP03-02)
    acquire_bootstrap_advisory_lock(db)

    # 2. Check if ANY administrator already exists
    existing_admin = db.scalar(select(User).where(User.role == "admin").limit(1))

    if existing_admin:
        if not existing_admin.is_active or existing_admin.role != "admin":
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Administrator account '{existing_admin.username}' exists but is inactive or invalid role '{existing_admin.role}'. "
                "Cannot perform safe bootstrap or verification."
            )
        # Verify bootstrap provenance on repeat bootstrap call (WP03-R01 / WP03-F01)
        bootstrap_audits = db.scalars(select(AuditLog).where(AuditLog.action == "ADMIN_BOOTSTRAPPED")).all()
        if len(bootstrap_audits) == 0:
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Administrator account '{existing_admin.username}' exists, "
                "but expected bootstrap provenance audit record is missing. Refusing repeat bootstrap."
            )
        if len(bootstrap_audits) > 1:
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Ambiguous or duplicate bootstrap audit records detected ({len(bootstrap_audits)} records)."
            )
        audit = bootstrap_audits[0]
        if audit.entity_type != "USER":
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Bootstrap audit record entity_type must be 'USER' (got '{audit.entity_type}')."
            )
        # WP03-F01: Require BOTH identifiers (user_id AND entity_id) to match existing administrator
        if audit.user_id != existing_admin.id or str(audit.entity_id) != str(existing_admin.id):
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Bootstrap provenance identifier mismatch for administrator '{existing_admin.username}'. "
                f"Requires BOTH user_id={existing_admin.id} and entity_id='{existing_admin.id}', but got audit user_id={audit.user_id} and entity_id='{audit.entity_id}'."
            )
        if audit.username != existing_admin.username:
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Bootstrap provenance username conflict for administrator '{existing_admin.username}'. "
                f"Audit record username is '{audit.username}'."
            )
        return {
            "status": "already_exists",
            "username": existing_admin.username,
            "created": False,
            "message": f"Administrator account '{existing_admin.username}' already established. Skipping bootstrap (no-op).",
        }

    # 3. Check for unexpected identity data (WP03-03 and WP03-R01)
    user_count = db.scalar(select(func.count()).select_from(User)) or 0
    if user_count > 0:
        raise RuntimeError(
            f"CRITICAL INITIALIZATION ERROR: Pre-existing user accounts detected ({user_count} users), "
            "but no legitimate administrator exists. Cannot perform first-time bootstrap on an inconsistent identity database."
        )

    session_count = db.scalar(select(func.count()).select_from(SessionModel)) or 0
    if session_count > 0:
        raise RuntimeError(
            f"CRITICAL INITIALIZATION ERROR: Orphaned active sessions detected ({session_count} sessions) "
            "in empty user database. Target database identity state is corrupted."
        )

    # 4. If no administrator exists, password is required
    if not admin_password:
        raise ValueError(
            "CRITICAL BOOTSTRAP ERROR: No administrator exists, but no bootstrap password was provided. "
            "Must supply COLLECTFLOW_BOOTSTRAP_ADMIN_PASSWORD_FILE or COLLECTFLOW_BOOTSTRAP_ADMIN_PASSWORD."
        )

    validate_admin_password(admin_password)

    if verify_only:
        return {
            "status": "pending_creation",
            "username": admin_username,
            "created": False,
            "message": f"Verification mode: Ready to bootstrap administrator '{admin_username}' (must_change_password=True).",
        }

    # 5. Hash credential using Argon2id
    password_hash_str = ph.hash(admin_password)

    new_admin = User(
        username=admin_username,
        password_hash=password_hash_str,
        role="admin",
        full_name="System Administrator",
        is_active=True,
        must_change_password=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(new_admin)
    db.flush()

    # 6. Record sanitized audit log with BOTH stable identifiers (user_id and entity_id)
    audit_entry = AuditLog(
        user_id=new_admin.id,
        username=new_admin.username,
        action="ADMIN_BOOTSTRAPPED",
        entity_type="USER",
        entity_id=str(new_admin.id),
        details={
            "username": admin_username,
            "role": "admin",
            "must_change_password": True,
            "source": "init_production_db",
        },
        ip_address="127.0.0.1",
        request_id="init-admin-001",
        timestamp=datetime.now(timezone.utc),
    )
    db.add(audit_entry)
    db.flush()

    return {
        "status": "bootstrapped",
        "username": admin_username,
        "created": True,
        "user_id": new_admin.id,
        "message": f"Successfully bootstrapped initial administrator '{admin_username}' with must_change_password=True.",
    }


def init_production_database(
    reference_config_path: str | None = None,
    allow_test_fixture: bool = False,
    verify_only: bool = False,
) -> dict:
    """Execute complete production database verification, initialization, and bootstrap workflow."""
    # WP03-F02: Enforce test fixture restriction before any operations
    if allow_test_fixture:
        env_mode = (os.environ.get("COLLECTFLOW_ENV") or "").strip().lower()
        assert_test_fixture_permitted(env_mode)

    admin_username, admin_password = get_bootstrap_admin_credentials()
    ref_config = load_reference_config(reference_config_path, allow_test_fixture=allow_test_fixture)

    with get_db_session() as db:
        # 1. Environment & Database Safety Verification (including bootstrap provenance WP03-R01 / WP03-F01)
        env_status = verify_target_environment(db)

        # 2. Administrator Bootstrap Verification / Execution (with advisory lock serialization WP03-02)
        admin_status = bootstrap_administrator(db, admin_username, admin_password, verify_only=verify_only)

        # 3. Statutory Reference Data Initialization (deferred if no approved config WP03-01 / WP03-R02 / WP03-F02)
        ref_status = init_reference_data(db, ref_config=ref_config, verify_only=verify_only)

        if not verify_only:
            db.commit()

        return {
            "verify_only": verify_only,
            "environment": env_status,
            "administrator": admin_status,
            "reference_data": ref_status,
        }


def main():
    parser = argparse.ArgumentParser(description="CollectFlow Production-Safe Database Initializer & Admin Bootstrap")
    parser.add_argument(
        "--verify-only",
        "--check",
        action="store_true",
        help="Run environment, schema, and reference verification without performing any writes",
    )
    parser.add_argument(
        "--reference-config",
        dest="reference_config",
        type=str,
        default=None,
        help="Path to versioned, Finance-approved statutory reference configuration file (JSON)",
    )
    parser.add_argument(
        "--allow-test-fixture",
        action="store_true",
        default=False,
        help="Allow synthetic test reference configuration fixtures (strictly for testing, forbidden in production/staging)",
    )
    args = parser.parse_args()

    mode_label = "VERIFICATION ONLY" if args.verify_only else "INITIALIZATION"
    print(f"==> CollectFlow Production Initializer [{mode_label}] Starting...")

    if args.allow_test_fixture:
        env_mode = (os.environ.get("COLLECTFLOW_ENV") or "").strip().lower()
        try:
            assert_test_fixture_permitted(env_mode)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    try:
        report = init_production_database(
            reference_config_path=args.reference_config,
            allow_test_fixture=args.allow_test_fixture,
            verify_only=args.verify_only,
        )
        print(f"==> Environment: {report['environment']['env_mode']} | Database: {report['environment']['database']}")
        print(f"==> Schema Revision: {report['environment']['alembic_revision']} (MATCH)")
        print(f"==> Administrator: {report['administrator']['message']}")
        if report["reference_data"]["status"] == "deferred":
            print("--------------------------------------------------------------------------------")
            print(f"==> Reference Data: DEFERRED (No approved statutory reference configuration provided)")
            print(report["reference_data"]["message"])
            print("--------------------------------------------------------------------------------")
        else:
            print("--------------------------------------------------------------------------------")
            print(f"==> Reference Data: INITIALIZED from Finance-approved config v{report['reference_data']['config_version']} (by {report['reference_data']['approved_by']})")
            print(f"==> Records Added: FYs={report['reference_data']['financial_years']}, "
                  f"GST={report['reference_data']['gst_rates']}, TDS={report['reference_data']['tds_sections']}")
            print("--------------------------------------------------------------------------------")
        print(f"==> CollectFlow Initializer completed successfully.")
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR: {sanitize_credentials(str(exc))}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
