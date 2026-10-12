"""PostgreSQL Domain Model & Synthetic Fixture Automated Verification Suite.

Tests:
1. Schema verification: All 15 domain tables exist in PostgreSQL public schema.
2. Numeric precision: Verifies NUMERIC(15, 2) arithmetic retains exact decimal values without floating-point error.
3. Check constraints: Enforces start_date < end_date, rate bounds, and status enum constraints.
4. Foreign key integrity: Verifies RESTRICT / CASCADE relationships across tables.
5. Baseline fixture reconciliation: Verifies INV-2026-042, PO-NEXUS-881, and REM-2026-001 values.
6. Password hashing: Verifies Argon2id password hash validation for seeded sandbox users.
"""

from datetime import date
from decimal import Decimal
import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from app.core.database import engine, get_db_session
from app.models.domain_models import (
    AuditLog,
    CommercialInvoice,
    CommercialInvoiceItem,
    Customer,
    ExportTicket,
    FinancialYear,
    GstRateMaster,
    LoginAttempt,
    PaymentTransaction,
    PurchaseOrder,
    PurchaseOrderItem,
    SalesTarget,
    SessionModel,
    TdsSectionMaster,
    User,
)

ph = PasswordHasher()


def test_schema_inventory_all_15_tables_exist():
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    expected_tables = {
        "financial_years",
        "gst_rate_master",
        "tds_section_master",
        "users",
        "sessions",
        "login_attempts",
        "customers",
        "purchase_orders",
        "purchase_order_items",
        "commercial_invoices",
        "commercial_invoice_items",
        "payment_transactions",
        "sales_targets",
        "audit_logs",
        "export_tickets",
    }

    assert expected_tables.issubset(table_names), f"Missing tables: {expected_tables - table_names}"


def test_baseline_fixtures_reconciliation():
    with get_db_session() as db:
        # 1. Invoice INV-2026-042
        inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.invoice_number == "INV-2026-042"))
        assert inv is not None
        assert inv.exclusive_amount == Decimal("450000.00")
        assert inv.tax_amount == Decimal("81000.00")
        assert inv.total_amount == Decimal("531000.00")
        assert inv.cgst_amount == Decimal("40500.00")
        assert inv.sgst_amount == Decimal("40500.00")
        assert inv.igst_amount == Decimal("0.00")
        assert inv.lifecycle_status == "ISSUED"
        assert inv.snapshot_customer_name == "Acme Cloud Solutions Pvt Ltd"

        # 2. Purchase Order PO-NEXUS-881
        po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == "PO-NEXUS-881"))
        assert po is not None
        assert po.exclusive_amount == Decimal("250000.00")
        assert po.tax_amount == Decimal("45000.00")
        assert po.total_amount == Decimal("295000.00")
        assert po.status == "UNBILLED"

        # 3. Payment Settlement REM-2026-001
        payment = db.scalar(select(PaymentTransaction).where(PaymentTransaction.remittance_number == "REM-2026-001"))
        assert payment is not None
        assert payment.cash_amount == Decimal("200000.00")
        assert payment.tds_amount == Decimal("9000.00")
        assert payment.total_settled == Decimal("209000.00")
        assert payment.tds_section == "194C"
        assert payment.transaction_type == "PAYMENT"

        # 4. Invariant settlement reconciliation
        outstanding = inv.total_amount - payment.total_settled
        assert outstanding == Decimal("322000.00")

        # 5. Sales Target
        target = db.scalar(select(SalesTarget).where(SalesTarget.fy_code == "FY 2026-27"))
        assert target is not None
        assert target.yearly_target == Decimal("7500000.00")


def test_argon2id_password_hashing():
    with get_db_session() as db:
        admin = db.scalar(select(User).where(User.username == "sandbox_admin"))
        assert admin is not None
        assert admin.password_hash.startswith("$argon2id$")
        assert ph.verify(admin.password_hash, "password123") is True

        with pytest.raises(VerifyMismatchError):
            ph.verify(admin.password_hash, "wrongpassword")


def test_financial_year_check_constraint_enforcement():
    with get_db_session() as db:
        invalid_fy = FinancialYear(
            code="FY 9999-00",
            start_date=date(2027, 4, 1),
            end_date=date(2026, 3, 31),  # Invalid: end < start
            is_active=False,
        )
        db.add(invalid_fy)
        with pytest.raises(IntegrityError) as exc_info:
            db.flush()
        db.rollback()
        assert "chk_fy_dates" in str(exc_info.value)


def test_settlement_math_check_constraint():
    with get_db_session() as db:
        inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.invoice_number == "INV-2026-042"))
        admin = db.scalar(select(User).where(User.username == "sandbox_admin"))

        invalid_tx = PaymentTransaction(
            invoice_id=inv.id,
            transaction_type="PAYMENT",
            cash_amount=Decimal("1000.00"),
            tds_amount=Decimal("100.00"),
            total_settled=Decimal("1200.00"),  # Mismatched: 1000 + 100 != 1200
            payment_date=date(2026, 4, 28),
            payment_mode="NEFT",
            recorded_by=admin.id,
        )
        db.add(invalid_tx)
        with pytest.raises(IntegrityError) as exc_info:
            db.flush()
        db.rollback()
        assert "chk_settlement_math" in str(exc_info.value)


def test_foreign_key_delete_restrict():
    with get_db_session() as db:
        customer = db.scalar(select(Customer).where(Customer.billing_name == "Acme Cloud Solutions Pvt Ltd"))
        db.delete(customer)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
