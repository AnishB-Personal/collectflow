"""
CollectFlow Declarative Domain Models (SQLAlchemy 2.0).
Defines complete normalized relational entities across:
1. Reference & Identity: FinancialYear, GstRateMaster, TdsSectionMaster, User, SessionModel, LoginAttempt
2. Customers & Exposure: Customer
3. Purchase Orders & Items: PurchaseOrder, PurchaseOrderItem
4. Commercial Tax Invoices: CommercialInvoice, CommercialInvoiceItem
5. Settlements & Reversals: PaymentTransaction
6. Sales Targets: SalesTarget
7. Audit & Exports: AuditLog, ExportTicket
"""

from decimal import Decimal
from datetime import datetime, date
from typing import Optional, List
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Numeric,
    Date,
    DateTime,
    Text,
    ForeignKey,
    CheckConstraint,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.core.database import Base


# ---------------------------------------------------------------------------
# 1. Statutory Reference & Identity
# ---------------------------------------------------------------------------

class FinancialYear(Base):
    __tablename__ = "financial_years"

    code = Column(String(20), primary_key=True)  # e.g. 'FY 2026-27'
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("start_date < end_date", name="chk_fy_dates"),
    )


class GstRateMaster(Base):
    __tablename__ = "gst_rate_master"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)
    rate = Column(Numeric(5, 4), nullable=False)  # e.g. 0.1800
    cgst_rate = Column(Numeric(5, 4), nullable=False, default=Decimal("0.0000"))
    sgst_rate = Column(Numeric(5, 4), nullable=False, default=Decimal("0.0000"))
    igst_rate = Column(Numeric(5, 4), nullable=False, default=Decimal("0.0000"))
    is_default = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("rate >= 0.0000 AND rate <= 1.0000", name="chk_gst_rate_bounds"),
    )


class TdsSectionMaster(Base):
    __tablename__ = "tds_section_master"

    id = Column(Integer, primary_key=True, autoincrement=True)
    section_code = Column(String(20), unique=True, nullable=False)  # e.g. '194C', '194J'
    description = Column(Text, nullable=False)
    default_rate = Column(Numeric(5, 4), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("default_rate >= 0.0000", name="chk_tds_rate_bounds"),
    )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)  # Argon2id hash string
    role = Column(String(20), nullable=False)  # 'admin', 'finance', 'sales', 'viewer'
    full_name = Column(String(100), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'finance', 'sales', 'viewer')", name="chk_user_role"),
    )


class SessionModel(Base):
    __tablename__ = "sessions"

    token_hash = Column(String(64), primary_key=True)  # SHA-256 of 32-byte secret token
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    user_agent = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)

    user = relationship("User")


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    identifier = Column(String(100), nullable=False)
    attempted_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    success = Column(Boolean, nullable=False, default=False)
    ip_address = Column(String(45), nullable=True)

    __table_args__ = (
        Index("idx_login_attempts_lookup", "identifier", "attempted_at"),
    )


# ---------------------------------------------------------------------------
# 2. Customers
# ---------------------------------------------------------------------------

class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    billing_name = Column(String(150), unique=True, nullable=False)
    billing_address = Column(Text, nullable=False)
    city = Column(String(50), nullable=True)
    state = Column(String(50), nullable=False)
    country = Column(String(10), nullable=False, default="IN")
    pin_code = Column(String(20), nullable=True)
    gst_number = Column(String(20), nullable=True)
    contact_person = Column(String(100), nullable=True)
    designation = Column(String(50), nullable=True)
    email = Column(String(100), nullable=True)
    phone_1 = Column(String(25), nullable=True)
    phone_2 = Column(String(25), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# 3. Purchase Orders & Line Items
# ---------------------------------------------------------------------------

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fy_code = Column(String(20), ForeignKey("financial_years.code"), nullable=False)
    po_number = Column(String(50), unique=True, nullable=False)
    po_date = Column(Date, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False)
    executive_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    currency = Column(String(10), nullable=False, default="INR")
    exchange_rate = Column(Numeric(12, 6), nullable=False, default=Decimal("1.000000"))
    exclusive_amount = Column(Numeric(15, 2), nullable=False)
    tax_rate = Column(Numeric(5, 4), nullable=False, default=Decimal("0.1800"))
    tax_amount = Column(Numeric(15, 2), nullable=False)
    total_amount = Column(Numeric(15, 2), nullable=False)
    status = Column(String(20), nullable=False, default="UNBILLED")  # 'UNBILLED', 'CONVERTED', 'CANCELLED'
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer")
    executive = relationship("User")
    items = relationship("PurchaseOrderItem", back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("status IN ('UNBILLED', 'CONVERTED', 'CANCELLED')", name="chk_po_status"),
        CheckConstraint("exclusive_amount >= 0", name="chk_po_excl_amount"),
        CheckConstraint("tax_amount >= 0", name="chk_po_tax_amount"),
        CheckConstraint("total_amount >= 0", name="chk_po_total_amount"),
        Index("idx_po_customer", "customer_id"),
        Index("idx_po_executive", "executive_id"),
        Index("idx_po_status", "status"),
    )


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    po_id = Column(Integer, ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False)
    item_description = Column(Text, nullable=False)
    quantity = Column(Numeric(12, 4), nullable=False, default=Decimal("1.0000"))
    unit_price = Column(Numeric(15, 2), nullable=False)
    exclusive_amount = Column(Numeric(15, 2), nullable=False)
    tax_rate = Column(Numeric(5, 4), nullable=False, default=Decimal("0.1800"))
    tax_amount = Column(Numeric(15, 2), nullable=False)
    total_amount = Column(Numeric(15, 2), nullable=False)

    order = relationship("PurchaseOrder", back_populates="items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_po_item_qty"),
        CheckConstraint("unit_price >= 0", name="chk_po_item_unit_price"),
    )


# ---------------------------------------------------------------------------
# 4. Commercial Tax Invoices & Line Items
# ---------------------------------------------------------------------------

class CommercialInvoice(Base):
    __tablename__ = "commercial_invoices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    po_id = Column(Integer, ForeignKey("purchase_orders.id", ondelete="SET NULL"), nullable=True)
    fy_code = Column(String(20), ForeignKey("financial_years.code"), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False)
    executive_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    invoice_number = Column(String(50), unique=True, nullable=False)
    invoice_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    currency = Column(String(10), nullable=False, default="INR")
    exchange_rate = Column(Numeric(12, 6), nullable=False, default=Decimal("1.000000"))
    exclusive_amount = Column(Numeric(15, 2), nullable=False)
    tax_rate = Column(Numeric(5, 4), nullable=False, default=Decimal("0.1800"))
    tax_amount = Column(Numeric(15, 2), nullable=False)
    total_amount = Column(Numeric(15, 2), nullable=False)

    # Immutable Customer Snapshot at Moment of Invoicing
    snapshot_customer_name = Column(String(150), nullable=False)
    snapshot_gstin = Column(String(20), nullable=True)
    snapshot_billing_address = Column(Text, nullable=False)
    snapshot_state = Column(String(50), nullable=False)

    # GST Breakdown
    cgst_amount = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    sgst_amount = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    igst_amount = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))

    # Lifecycle Status
    lifecycle_status = Column(String(20), nullable=False, default="ISSUED")  # 'ISSUED', 'CANCELLED'
    cancellation_reason = Column(Text, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer")
    executive = relationship("User", foreign_keys=[executive_id])
    po = relationship("PurchaseOrder")
    items = relationship("CommercialInvoiceItem", back_populates="invoice", cascade="all, delete-orphan")
    settlements = relationship("PaymentTransaction", back_populates="invoice")

    __table_args__ = (
        CheckConstraint("lifecycle_status IN ('ISSUED', 'CANCELLED')", name="chk_invoice_lifecycle"),
        CheckConstraint("exclusive_amount >= 0", name="chk_inv_excl_amount"),
        CheckConstraint("tax_amount >= 0", name="chk_inv_tax_amount"),
        CheckConstraint("total_amount >= 0", name="chk_inv_total_amount"),
        Index("idx_invoices_customer", "customer_id"),
        Index("idx_invoices_executive", "executive_id"),
        Index("idx_invoices_fy_status", "fy_code", "lifecycle_status"),
        Index("idx_invoices_due_date", "due_date"),
    )


class CommercialInvoiceItem(Base):
    __tablename__ = "commercial_invoice_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_id = Column(Integer, ForeignKey("commercial_invoices.id", ondelete="RESTRICT"), nullable=False)
    item_description = Column(Text, nullable=False)
    quantity = Column(Numeric(12, 4), nullable=False, default=Decimal("1.0000"))
    unit_price = Column(Numeric(15, 2), nullable=False)
    exclusive_amount = Column(Numeric(15, 2), nullable=False)
    tax_rate = Column(Numeric(5, 4), nullable=False, default=Decimal("0.1800"))
    tax_amount = Column(Numeric(15, 2), nullable=False)
    total_amount = Column(Numeric(15, 2), nullable=False)

    invoice = relationship("CommercialInvoice", back_populates="items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_inv_item_qty"),
        CheckConstraint("unit_price >= 0", name="chk_inv_item_unit_price"),
    )


# ---------------------------------------------------------------------------
# 5. Financial Settlements & Non-Destructive Adjustments
# ---------------------------------------------------------------------------

class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_id = Column(Integer, ForeignKey("commercial_invoices.id", ondelete="RESTRICT"), nullable=False)
    parent_transaction_id = Column(Integer, ForeignKey("payment_transactions.id", ondelete="RESTRICT"), nullable=True)
    transaction_type = Column(String(20), nullable=False)  # 'PAYMENT', 'REVERSAL', 'ADJUSTMENT'
    cash_amount = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    tds_amount = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    total_settled = Column(Numeric(15, 2), nullable=False)
    tds_section = Column(String(20), ForeignKey("tds_section_master.section_code"), nullable=True)
    payment_date = Column(Date, nullable=False)
    remittance_number = Column(String(100), nullable=True)
    payment_mode = Column(String(20), nullable=False)  # 'NEFT', 'RTGS', 'CHEQUE', 'UPI', 'CASH', 'WIRE', 'OTHER'
    notes = Column(Text, nullable=True)
    idempotency_key = Column(String(100), unique=True, nullable=True)
    recorded_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    invoice = relationship("CommercialInvoice", back_populates="settlements")
    recorder = relationship("User")

    __table_args__ = (
        CheckConstraint("transaction_type IN ('PAYMENT', 'REVERSAL', 'ADJUSTMENT')", name="chk_tx_type"),
        CheckConstraint("payment_mode IN ('NEFT', 'RTGS', 'CHEQUE', 'UPI', 'CASH', 'WIRE', 'OTHER')", name="chk_payment_mode"),
        CheckConstraint("total_settled = (cash_amount + tds_amount)", name="chk_settlement_math"),
        Index("idx_payments_invoice", "invoice_id"),
        Index("idx_payments_date", "payment_date"),
        Index("idx_payments_parent", "parent_transaction_id"),
    )


# ---------------------------------------------------------------------------
# 6. Sales Targets
# ---------------------------------------------------------------------------

class SalesTarget(Base):
    __tablename__ = "sales_targets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    fy_code = Column(String(20), ForeignKey("financial_years.code"), nullable=False)
    yearly_target = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    half_yearly_target = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    quarterly_target = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    monthly_target = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    monthly_bottom_line = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    yearly_bottom_line = Column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", "fy_code", name="uq_sales_target_user_fy"),
        CheckConstraint("yearly_target >= 0", name="chk_yearly_target_pos"),
        Index("idx_targets_lookup", "fy_code", "user_id"),
    )


# ---------------------------------------------------------------------------
# 7. Audit Logs & Export Tickets
# ---------------------------------------------------------------------------

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    username = Column(String(50), nullable=True)
    action = Column(String(50), nullable=False)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(String(50), nullable=True)
    details = Column(JSONB, nullable=True)  # Structured JSONB audit trail
    ip_address = Column(String(45), nullable=True)
    request_id = Column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_audit_logs_timestamp", "timestamp"),
        Index("idx_audit_logs_entity", "entity_type", "entity_id"),
    )


class ExportTicket(Base):
    __tablename__ = "export_tickets"

    token_hash = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    format = Column(String(20), nullable=True)
    fy_code = Column(String(20), nullable=True)
    scope = Column(String(20), nullable=True)

    __table_args__ = (
        Index("idx_export_tickets_expires", "expires_at", "used"),
    )
