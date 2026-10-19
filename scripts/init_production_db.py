"""Production-Safe Database Initializer & Administrator Bootstrap (Release Preparation WP-03).

Provides a secure, repeatable, deterministic initialization workflow for PostgreSQL-backed CollectFlow databases.

Enforces:
1. Verification of environment mode and target database configuration.
2. Verification of expected Alembic schema revision ('77e73a1cb221').
3. Strict refusal of test databases ('collectflow_test') or unknown/unsafe targets.
4. Pre-existing business and identity data validation (fails closed if non-empty or inconsistent).
5. Transaction-scoped PostgreSQL advisory lock serialization (pg_advisory_xact_lock) for administrator bootstrap.
6. Secure bootstrap of exactly one administrator with Argon2id hash and must_change_password=True.
7. Refusal of missing, empty, weak, or known synthetic/demo passwords and usernames.
8. Atomic idempotent execution: repeated runs perform a safe verified no-op without resetting passwords.
9. Controlled statutory reference data initialization: DEFERRED by default pending formal Finance sign-offs,
   only populated when an explicit, versioned, Finance-approved configuration is supplied.
10. Safe verification-only mode (--verify-only) performing zero writes.
11. Strict omission of synthetic customers, orders, invoices, payments, or targets.
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

ph = PasswordHasher()


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
    and pre-existing business and identity state.
    Fails closed on unsafe, test, corrupted, or mismatched database targets.
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

    # Check identity and session state (WP03-03)
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
        bootstrap_audit = db.scalar(select(AuditLog).where(AuditLog.action == "ADMIN_BOOTSTRAPPED").limit(1))
        if bootstrap_audit and not any(a.username == bootstrap_audit.username for a in active_admins):
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Bootstrap audit trail inconsistency detected. "
                f"Audit record is for '{bootstrap_audit.username}', but active admin accounts are {[a.username for a in active_admins]}."
            )
        identity_state = "already_initialized"
    else:
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


def load_reference_config(config_path: str | None = None) -> dict | None:
    """
    Load and validate an explicit, versioned, Finance-approved statutory reference configuration file.
    Returns None if no configuration file is provided (indicating reference data should be deferred).
    Fails closed if the file is missing, invalid JSON, or not approved by Finance.
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

    # 1. Validation of approval metadata
    approval_status = str(data.get("approval_status", "")).strip().lower()
    if approval_status != "approved":
        raise ValueError(
            f"CRITICAL CONFIGURATION ERROR: Statutory reference config is not approved by Finance "
            f"(approval_status='{data.get('approval_status')}', expected 'approved'). "
            "Unapproved statutory reference values cannot be populated."
        )

    approved_by = str(data.get("approved_by", "")).strip()
    if not approved_by:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing required 'approved_by' metadata.")

    config_version = str(data.get("config_version", "")).strip()
    if not config_version:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing required 'config_version' metadata.")

    # 2. Validation of policy references
    policy_refs = data.get("policy_references", [])
    if not isinstance(policy_refs, list) or not policy_refs:
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config must list applicable policy references (e.g. BPD-01, BPD-02).")

    # 3. Payload validation
    if "financial_years" not in data or not isinstance(data["financial_years"], list):
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing 'financial_years' list.")
    if "gst_rates" not in data or not isinstance(data["gst_rates"], list):
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing 'gst_rates' list.")
    if "tds_sections" not in data or not isinstance(data["tds_sections"], list):
        raise ValueError("CRITICAL CONFIGURATION ERROR: Reference config missing 'tds_sections' list.")

    return data


def init_reference_data(db, ref_config: dict | None = None, verify_only: bool = False) -> dict:
    """
    Initialize statutory reference data only when an explicit Finance-approved configuration is provided.
    If ref_config is None, reference data initialization is deferred safely.
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

    results = {"financial_years": 0, "gst_rates": 0, "tds_sections": 0}

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
        "financial_years": results["financial_years"],
        "gst_rates": results["gst_rates"],
        "tds_sections": results["tds_sections"],
        "message": (
            f"Successfully populated reference data from Finance-approved config "
            f"v{ref_config.get('config_version')} (approved by {ref_config.get('approved_by')})."
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
        if not existing_admin.is_active:
            raise RuntimeError(
                f"CRITICAL INITIALIZATION ERROR: Administrator account '{existing_admin.username}' exists but is inactive. "
                "Cannot perform safe bootstrap or verification with an inactive administrator."
            )
        return {
            "status": "already_exists",
            "username": existing_admin.username,
            "created": False,
            "message": f"Administrator account '{existing_admin.username}' already established. Skipping bootstrap (no-op).",
        }

    # 3. Check for unexpected identity data (WP03-03)
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

    # 6. Record sanitized audit log
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


def init_production_database(reference_config_path: str | None = None, verify_only: bool = False) -> dict:
    """Execute complete production database verification, initialization, and bootstrap workflow."""
    admin_username, admin_password = get_bootstrap_admin_credentials()
    ref_config = load_reference_config(reference_config_path)

    with get_db_session() as db:
        # 1. Environment & Database Safety Verification
        env_status = verify_target_environment(db)

        # 2. Administrator Bootstrap Verification / Execution (with advisory lock serialization)
        admin_status = bootstrap_administrator(db, admin_username, admin_password, verify_only=verify_only)

        # 3. Statutory Reference Data Initialization (deferred if no approved config)
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
    args = parser.parse_args()

    mode_label = "VERIFICATION ONLY" if args.verify_only else "INITIALIZATION"
    print(f"==> CollectFlow Production Initializer [{mode_label}] Starting...")

    try:
        report = init_production_database(reference_config_path=args.reference_config, verify_only=args.verify_only)
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
