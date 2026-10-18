"""Seed deterministic synthetic baseline fixtures for CollectFlow development/integration database.

Populates:
1. Financial Years (FY 2018-19 through FY 2026-27)
2. GST Rate Master (18%, 12%, 5%, 0%)
3. TDS Section Master (194C 2.0%, 194J 10.0%, 194J(b) 2.0%)
4. Synthetic Users with Argon2id hashes:
   - sandbox_admin (Admin)
   - sandbox_finance (Finance)
   - sandbox_sales (Sales Executive)
   - sandbox_viewer (Viewer / Auditor)
5. Synthetic Customers:
   - Acme Cloud Solutions Pvt Ltd (GSTIN: 27AABCA1234A1Z5)
   - TechVanguard Infotech LLP (GSTIN: 29AABCT5678B1Z2)
6. Purchase Orders:
   - PO-NEXUS-881 (Total ₹2,50,000 / Tax ₹45,000 / Gross ₹2,95,000)
7. Commercial Invoices:
   - INV-2026-042 (Gross ₹5,31,000, Invoiced 2026-04-12, Due 2026-05-12)
8. Payments & Settlements:
   - Remittance REM-2026-001 (Cash ₹2,00,000, TDS ₹9,000 under 194C, Total Settled ₹2,09,000, Outstanding ₹3,22,000)
9. Sales Targets:
   - FY 2026-27 target for sandbox_sales (₹75,00,000)
"""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argon2 import PasswordHasher
from sqlalchemy import select, delete
from app.core.database import get_db_session
from app.models.domain_models import (
    AuditLog,
    CommercialInvoice,
    CommercialInvoiceItem,
    Customer,
    ExportTicket,
    FinancialYear,
    GstRateMaster,
    PaymentTransaction,
    PurchaseOrder,
    PurchaseOrderItem,
    SalesTarget,
    SessionModel,
    TdsSectionMaster,
    User,
)

ph = PasswordHasher()


def seed_fixtures(reset: bool = False):
    with get_db_session() as db:
        if reset:
            print("Resetting database to pure baseline fixtures...")
            from sqlalchemy import text
            db.execute(text("TRUNCATE TABLE export_tickets, sessions, payment_transactions, commercial_invoice_items, commercial_invoices, purchase_order_items, purchase_orders, sales_targets, audit_logs, customers, users RESTART IDENTITY CASCADE;"))
            db.commit()

        print("Idempotently verifying and seeding baseline fixtures...")

        # 1. Financial Years
        all_fys = [
            FinancialYear(code="FY 2018-19", start_date=date(2018, 4, 1), end_date=date(2019, 3, 31), is_active=False),
            FinancialYear(code="FY 2019-20", start_date=date(2019, 4, 1), end_date=date(2020, 3, 31), is_active=False),
            FinancialYear(code="FY 2020-21", start_date=date(2020, 4, 1), end_date=date(2021, 3, 31), is_active=False),
            FinancialYear(code="FY 2021-22", start_date=date(2021, 4, 1), end_date=date(2022, 3, 31), is_active=False),
            FinancialYear(code="FY 2022-23", start_date=date(2022, 4, 1), end_date=date(2023, 3, 31), is_active=False),
            FinancialYear(code="FY 2023-24", start_date=date(2023, 4, 1), end_date=date(2024, 3, 31), is_active=False),
            FinancialYear(code="FY 2024-25", start_date=date(2024, 4, 1), end_date=date(2025, 3, 31), is_active=False),
            FinancialYear(code="FY 2025-26", start_date=date(2025, 4, 1), end_date=date(2026, 3, 31), is_active=False),
            FinancialYear(code="FY 2026-27", start_date=date(2026, 4, 1), end_date=date(2027, 3, 31), is_active=True),
        ]
        for fy in all_fys:
            if not db.scalar(select(FinancialYear).where(FinancialYear.code == fy.code)):
                db.add(fy)
        db.flush()

        # 2. GST Rate Master
        gst_rates = [
            GstRateMaster(name="GST 18%", rate=Decimal("0.1800"), cgst_rate=Decimal("0.0900"), sgst_rate=Decimal("0.0900"), igst_rate=Decimal("0.1800"), is_default=True, is_active=True),
            GstRateMaster(name="GST 12%", rate=Decimal("0.1200"), cgst_rate=Decimal("0.0600"), sgst_rate=Decimal("0.0600"), igst_rate=Decimal("0.1200"), is_default=False, is_active=True),
            GstRateMaster(name="GST 5%", rate=Decimal("0.0500"), cgst_rate=Decimal("0.0250"), sgst_rate=Decimal("0.0250"), igst_rate=Decimal("0.0500"), is_default=False, is_active=True),
            GstRateMaster(name="GST 0%", rate=Decimal("0.0000"), cgst_rate=Decimal("0.0000"), sgst_rate=Decimal("0.0000"), igst_rate=Decimal("0.0000"), is_default=False, is_active=True),
        ]
        for g in gst_rates:
            if not db.scalar(select(GstRateMaster).where(GstRateMaster.name == g.name)):
                db.add(g)
        db.flush()

        # 3. TDS Section Master
        tds_sections = [
            TdsSectionMaster(section_code="194C", description="Payment to Contractors/Sub-contractors", default_rate=Decimal("0.0200"), is_active=True),
            TdsSectionMaster(section_code="194J", description="Fees for Professional or Technical Services", default_rate=Decimal("0.1000"), is_active=True),
            TdsSectionMaster(section_code="194J(b)", description="Technical Services (Specified)", default_rate=Decimal("0.0200"), is_active=True),
        ]
        for t in tds_sections:
            if not db.scalar(select(TdsSectionMaster).where(TdsSectionMaster.section_code == t.section_code)):
                db.add(t)
        db.flush()

        # 4. Synthetic Users with Argon2id
        users = [
            User(username="sandbox_admin", password_hash=ph.hash("AdminPass123!"), full_name="Sandbox Administrator", role="admin", is_active=True),
            User(username="sandbox_finance", password_hash=ph.hash("FinancePass123!"), full_name="Sandbox Finance Lead", role="finance", is_active=True),
            User(username="sandbox_sales", password_hash=ph.hash("SalesPass123!"), full_name="Sandbox Sales Executive", role="sales", is_active=True),
            User(username="sandbox_viewer", password_hash=ph.hash("ViewerPass123!"), full_name="Sandbox Audit Viewer", role="viewer", is_active=True),
        ]
        for u in users:
            if not db.scalar(select(User).where(User.username == u.username)):
                db.add(u)
        db.flush()

        admin_user = db.scalar(select(User).where(User.username == "sandbox_admin"))
        sales_user = db.scalar(select(User).where(User.username == "sandbox_sales"))

        # 5. Customers (Declaring supplier state as Karnataka, Acme in Maharashtra -> inter-state; TechVanguard in Karnataka -> intra-state)
        customers = [
            Customer(
                billing_name="Acme Cloud Solutions Pvt Ltd",
                billing_address="Plot 42, Tech Park, Andheri East",
                city="Mumbai",
                state="Maharashtra",
                country="IN",
                pin_code="400069",
                gst_number="27AABCA1234A1Z5",
                contact_person="Rajesh Sharma",
                designation="Director",
                email="billing@acmecloud.example.com",
                phone_1="+91-9876543210",
                is_active=True,
            ),
            Customer(
                billing_name="TechVanguard Infotech LLP",
                billing_address="102 Koramangala 4th Block",
                city="Bengaluru",
                state="Karnataka",
                country="IN",
                pin_code="560034",
                gst_number="29AABCT5678B1Z2",
                contact_person="Priya Nair",
                designation="VP Finance",
                email="finance@techvanguard.example.com",
                phone_1="+91-9876543211",
                is_active=True,
            ),
        ]
        for c in customers:
            if not db.scalar(select(Customer).where(Customer.billing_name == c.billing_name)):
                db.add(c)
        db.flush()

        acme = db.scalar(select(Customer).where(Customer.billing_name == "Acme Cloud Solutions Pvt Ltd"))

        # 6. Purchase Order PO-NEXUS-881 (unbilled baseline)
        existing_po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == "PO-NEXUS-881"))
        if not existing_po and acme and sales_user:
            po = PurchaseOrder(
                fy_code="FY 2026-27",
                po_number="PO-NEXUS-881",
                po_date=date(2026, 4, 10),
                customer_id=acme.id,
                executive_id=sales_user.id,
                currency="INR",
                exchange_rate=Decimal("1.000000"),
                exclusive_amount=Decimal("250000.00"),
                tax_rate=Decimal("0.1800"),
                tax_amount=Decimal("45000.00"),
                total_amount=Decimal("295000.00"),
                status="UNBILLED",
                remarks="Nexus Cloud Infrastructure Consultation & Migration Services",
            )
            db.add(po)
            db.flush()

            po_item = PurchaseOrderItem(
                po_id=po.id,
                item_description="Cloud Architecture Modernization & High-Availability Design",
                quantity=Decimal("1.0000"),
                unit_price=Decimal("250000.00"),
                exclusive_amount=Decimal("250000.00"),
                tax_rate=Decimal("0.1800"),
                tax_amount=Decimal("45000.00"),
                total_amount=Decimal("295000.00"),
            )
            db.add(po_item)
            db.flush()

        # 7. Baseline Commercial Invoice INV-2026-042 (direct invoice, decoupled from PO)
        existing_inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.invoice_number == "INV-2026-042"))
        if not existing_inv and acme and sales_user:
            inv = CommercialInvoice(
                po_id=None,  # Decoupled direct invoice so PO-NEXUS-881 remains completely UNBILLED
                fy_code="FY 2026-27",
                customer_id=acme.id,
                executive_id=sales_user.id,
                invoice_number="INV-2026-042",
                invoice_date=date(2026, 4, 12),
                due_date=date(2026, 5, 12),
                currency="INR",
                exchange_rate=Decimal("1.000000"),
                exclusive_amount=Decimal("450000.00"),
                tax_rate=Decimal("0.1800"),
                tax_amount=Decimal("81000.00"),
                total_amount=Decimal("531000.00"),
                snapshot_customer_name=acme.billing_name,
                snapshot_gstin=acme.gst_number,
                snapshot_billing_address=acme.billing_address,
                snapshot_state=acme.state,
                cgst_amount=Decimal("40500.00"),
                sgst_amount=Decimal("40500.00"),
                igst_amount=Decimal("0.00"),
                lifecycle_status="ISSUED",
                remarks="Standard professional services retainer - April 2026",
            )
            db.add(inv)
            db.flush()

            inv_item = CommercialInvoiceItem(
                invoice_id=inv.id,
                item_description="Enterprise Core Software Engineering Services (Retainer)",
                quantity=Decimal("1.0000"),
                unit_price=Decimal("450000.00"),
                exclusive_amount=Decimal("450000.00"),
                tax_rate=Decimal("0.1800"),
                tax_amount=Decimal("81000.00"),
                total_amount=Decimal("531000.00"),
            )
            db.add(inv_item)
            db.flush()

        inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.invoice_number == "INV-2026-042"))

        # 8. Payment Settlement REM-2026-001
        if inv and admin_user:
            existing_payment = db.scalar(select(PaymentTransaction).where(PaymentTransaction.remittance_number == "REM-2026-001"))
            if not existing_payment:
                payment = PaymentTransaction(
                    invoice_id=inv.id,
                    parent_transaction_id=None,
                    transaction_type="PAYMENT",
                    cash_amount=Decimal("200000.00"),
                    tds_amount=Decimal("9000.00"),
                    total_settled=Decimal("209000.00"),
                    tds_section="194C",
                    payment_date=date(2026, 4, 25),
                    remittance_number="REM-2026-001",
                    payment_mode="NEFT",
                    notes="Partial settlement for April services via HDFC bank transfer",
                    recorded_by=admin_user.id,
                )
                db.add(payment)
                db.flush()

        # 9. Sales Target for sandbox_sales
        if sales_user:
            existing_target = db.scalar(select(SalesTarget).where(SalesTarget.user_id == sales_user.id, SalesTarget.fy_code == "FY 2026-27"))
            if not existing_target:
                target = SalesTarget(
                    user_id=sales_user.id,
                    fy_code="FY 2026-27",
                    yearly_target=Decimal("7500000.00"),
                    half_yearly_target=Decimal("3750000.00"),
                    quarterly_target=Decimal("1875000.00"),
                    monthly_target=Decimal("625000.00"),
                    monthly_bottom_line=Decimal("500000.00"),
                    yearly_bottom_line=Decimal("6000000.00"),
                )
                db.add(target)
                db.flush()

        # 10. Audit Log
        if admin_user:
            existing_audit = db.scalar(select(AuditLog).where(AuditLog.entity_id == "SEED-4C1"))
            if not existing_audit:
                audit = AuditLog(
                    user_id=admin_user.id,
                    username=admin_user.username,
                    action="SEED_DATABASE",
                    entity_type="SYSTEM",
                    entity_id="SEED-4C1",
                    details={
                        "message": "Baseline Stage 4C.1 synthetic database fixtures seeded",
                        "invoice": "INV-2026-042",
                        "po": "PO-NEXUS-881",
                        "settlement": "REM-2026-001",
                    },
                    ip_address="127.0.0.1",
                    request_id="seed-init-001",
                    timestamp=datetime.now(timezone.utc),
                )
                db.add(audit)

        db.commit()
        print("Successfully verified/seeded all synthetic baseline fixtures!")


if __name__ == "__main__":
    reset_flag = "--reset" in sys.argv
    seed_fixtures(reset=reset_flag)
