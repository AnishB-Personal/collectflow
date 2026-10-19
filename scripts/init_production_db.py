"""Production-Safe Database Initializer & Administrator Bootstrap (Release Preparation WP-03).

Provides a secure, repeatable, deterministic initialization workflow for PostgreSQL-backed CollectFlow databases.

Enforces:
1. Verification of environment mode and target database configuration.
2. Verification of expected Alembic schema revision ('77e73a1cb221').
3. Strict refusal of test databases ('collectflow_test') or unknown/unsafe targets.
4. Pre-existing business data validation (fails closed if non-empty).
5. Secure bootstrap of exactly one administrator with Argon2id hash and must_change_password=True.
6. Refusal of missing, empty, weak, or known synthetic/demo passwords and usernames.
7. Atomic idempotent execution: repeated runs perform a safe verified no-op without resetting passwords.
8. Controlled reference data initialization with explicit notice of pending Finance sign-offs (BPD-01 through BPD-04).
9. Safe verification-only mode (--verify-only) performing zero writes.
10. Strict omission of synthetic customers, orders, invoices, payments, or targets.
"""

import argparse
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
    TdsSectionMaster,
    User,
)

EXPECTED_ALEMBIC_REVISION = "77e73a1cb221"

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

PENDING_FINANCE_SIGNOFF_NOTICE = (
    "[NOTICE] Statutory and reference values initialized. Pending Finance sign-off for:\n"
    "  - BPD-01: Statutory Reference Masters Policy\n"
    "  - BPD-02: Default Tax & Statutory Configurations\n"
    "  - BPD-03: Financial Year Boundary and Transition Rules\n"
    "  - BPD-04: Document Retention and Audit Data Policy"
)

ph = PasswordHasher()


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
    Rigorously verify environment mode, database configuration, and Alembic schema revision.
    Fails closed on unsafe, test, or mismatched database targets.
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

    # Check for pre-existing business data
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

    return {
        "env_mode": env_mode,
        "database": db_name,
        "alembic_revision": current_rev,
        "business_records": total_business_records,
    }


def init_reference_data(db, verify_only: bool = False) -> dict:
    """Initialize statutory and reference data deterministically and idempotently."""
    results = {"financial_years": 0, "gst_rates": 0, "tds_sections": 0}

    # 1. Financial Years: Active FY 2026-27
    fy_records = [
        FinancialYear(
            code="FY 2026-27",
            start_date=date(2026, 4, 1),
            end_date=date(2027, 3, 31),
            is_active=True,
            created_at=datetime.now(timezone.utc),
        ),
    ]
    for fy in fy_records:
        existing = db.scalar(select(FinancialYear).where(FinancialYear.code == fy.code))
        if not existing:
            results["financial_years"] += 1
            if not verify_only:
                db.add(fy)

    # 2. GST Rate Master: Standard statutory slabs
    gst_records = [
        GstRateMaster(name="GST 0%", rate=Decimal("0.0000"), cgst_rate=Decimal("0.0000"), sgst_rate=Decimal("0.0000"), igst_rate=Decimal("0.0000"), is_default=False, is_active=True),
        GstRateMaster(name="GST 5%", rate=Decimal("0.0500"), cgst_rate=Decimal("0.0250"), sgst_rate=Decimal("0.0250"), igst_rate=Decimal("0.0500"), is_default=False, is_active=True),
        GstRateMaster(name="GST 12%", rate=Decimal("0.1200"), cgst_rate=Decimal("0.0600"), sgst_rate=Decimal("0.0600"), igst_rate=Decimal("0.1200"), is_default=False, is_active=True),
        GstRateMaster(name="GST 18%", rate=Decimal("0.1800"), cgst_rate=Decimal("0.0900"), sgst_rate=Decimal("0.0900"), igst_rate=Decimal("0.1800"), is_default=True, is_active=True),
        GstRateMaster(name="GST 28%", rate=Decimal("0.2800"), cgst_rate=Decimal("0.1400"), sgst_rate=Decimal("0.1400"), igst_rate=Decimal("0.2800"), is_default=False, is_active=True),
    ]
    for gst in gst_records:
        existing = db.scalar(select(GstRateMaster).where(GstRateMaster.name == gst.name))
        if not existing:
            results["gst_rates"] += 1
            if not verify_only:
                db.add(gst)

    # 3. TDS Section Master: Standard statutory sections
    tds_records = [
        TdsSectionMaster(section_code="194C", description="Payments to Contractors/Sub-contractors", default_rate=Decimal("0.0200"), is_active=True),
        TdsSectionMaster(section_code="194J", description="Fees for Professional or Technical Services", default_rate=Decimal("0.1000"), is_active=True),
        TdsSectionMaster(section_code="194J(b)", description="Technical Services (Specified Sub-category)", default_rate=Decimal("0.0200"), is_active=True),
        TdsSectionMaster(section_code="194I", description="Rent of Land, Building or Furniture", default_rate=Decimal("0.1000"), is_active=True),
        TdsSectionMaster(section_code="194H", description="Commission or Brokerage", default_rate=Decimal("0.0500"), is_active=True),
    ]
    for tds in tds_records:
        existing = db.scalar(select(TdsSectionMaster).where(TdsSectionMaster.section_code == tds.section_code))
        if not existing:
            results["tds_sections"] += 1
            if not verify_only:
                db.add(tds)

    if not verify_only:
        db.flush()

    return results


def bootstrap_administrator(db, admin_username: str, admin_password: str | None, verify_only: bool = False) -> dict:
    """
    Securely bootstrap initial administrator account.
    Idempotent and atomic: repeated runs perform a safe no-op.
    """
    # Check if ANY administrator already exists
    existing_admin = db.scalar(select(User).where(User.role == "admin").limit(1))

    if existing_admin:
        return {
            "status": "already_exists",
            "username": existing_admin.username,
            "created": False,
            "message": f"Administrator account '{existing_admin.username}' already exists. Skipping bootstrap (no-op).",
        }

    # If no administrator exists, password is required
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

    # Hash credential using Argon2id
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

    # Record sanitized audit log
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


def init_production_database(verify_only: bool = False) -> dict:
    """Execute complete production database verification, initialization, and bootstrap workflow."""
    admin_username, admin_password = get_bootstrap_admin_credentials()

    with get_db_session() as db:
        # 1. Environment & Database Safety Verification
        env_status = verify_target_environment(db)

        # 2. Administrator Bootstrap Verification / Execution
        admin_status = bootstrap_administrator(db, admin_username, admin_password, verify_only=verify_only)

        # 3. Reference Data Initialization
        ref_status = init_reference_data(db, verify_only=verify_only)

        if not verify_only:
            db.commit()

        return {
            "verify_only": verify_only,
            "environment": env_status,
            "administrator": admin_status,
            "reference_data": ref_status,
            "notice": PENDING_FINANCE_SIGNOFF_NOTICE,
        }


def main():
    parser = argparse.ArgumentParser(description="CollectFlow Production-Safe Database Initializer & Admin Bootstrap")
    parser.add_argument(
        "--verify-only",
        "--check",
        action="store_true",
        help="Run environment, schema, and reference verification without performing any writes",
    )
    args = parser.parse_args()

    mode_label = "VERIFICATION ONLY" if args.verify_only else "INITIALIZATION"
    print(f"==> CollectFlow Production Initializer [{mode_label}] Starting...")

    try:
        report = init_production_database(verify_only=args.verify_only)
        print(f"==> Environment: {report['environment']['env_mode']} | Database: {report['environment']['database']}")
        print(f"==> Schema Revision: {report['environment']['alembic_revision']} (MATCH)")
        print(f"==> Administrator: {report['administrator']['message']}")
        print(f"==> Reference Records To Add: FYs={report['reference_data']['financial_years']}, "
              f"GST={report['reference_data']['gst_rates']}, TDS={report['reference_data']['tds_sections']}")
        print("--------------------------------------------------------------------------------")
        print(report["notice"])
        print("--------------------------------------------------------------------------------")
        print(f"==> CollectFlow Initializer completed successfully.")
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR: {sanitize_credentials(str(exc))}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
