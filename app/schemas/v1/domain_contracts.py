"""
CollectFlow OpenAPI v1 Domain Schemas (Stage 4C.0 Contract Freeze)
Authoritative Pydantic models enforcing:
- Exact decimal strings for monetary request/response attributes (Regex: ^-?\\d+\\.\\d{2}$)
- Strict statutory GST calculations (CGST/SGST/IGST)
- Separation of Purchase Orders from Commercial Tax Invoices
- Non-destructive payment settlements and compensating reversals
"""

from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


def quantize_2dec(val: Decimal) -> Decimal:
    """Exact financial 2-decimal half-up quantization."""
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def format_money_str(val: Decimal) -> str:
    """Serialize Decimal to exact 2-decimal string format."""
    return f"{quantize_2dec(val):.2f}"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserRole(str, Enum):
    ADMIN = "admin"
    FINANCE = "finance"
    SALES = "sales"
    VIEWER = "viewer"


class OrderBillingStatus(str, Enum):
    UNBILLED = "UNBILLED"
    CONVERTED = "CONVERTED"
    CANCELLED = "CANCELLED"


class InvoiceLifecycleStatus(str, Enum):
    ISSUED = "ISSUED"
    CANCELLED = "CANCELLED"


class InvoicePaymentStatus(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"


class PaymentMode(str, Enum):
    NEFT = "NEFT"
    RTGS = "RTGS"
    CHEQUE = "CHEQUE"
    UPI = "UPI"
    CASH = "CASH"
    WIRE = "WIRE"
    OTHER = "OTHER"


class TransactionType(str, Enum):
    PAYMENT = "PAYMENT"
    REVERSAL = "REVERSAL"
    ADJUSTMENT = "ADJUSTMENT"


class TdsSection(str, Enum):
    SECTION_194C = "194C"
    SECTION_194J = "194J"
    SECTION_194H = "194H"
    SECTION_194I = "194I"
    OTHER = "OTHER"


# ---------------------------------------------------------------------------
# Common Base Models
# ---------------------------------------------------------------------------

class MoneyAmountStr(str):
    """String constrained to exact 2-decimal financial currency representation."""
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if isinstance(v, (int, float, Decimal)):
            return format_money_str(Decimal(str(v)))
        if not isinstance(v, str):
            raise TypeError("String required for monetary value")
        try:
            d = Decimal(v.strip())
            return format_money_str(d)
        except Exception as e:
            raise ValueError(f"Invalid monetary decimal string: {v}") from e


# ---------------------------------------------------------------------------
# 1. Identity & Session Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)


class UserProfileResponse(BaseModel):
    id: int
    username: str
    role: UserRole
    full_name: Optional[str] = None
    is_active: bool = True


class LoginResponse(BaseModel):
    success: bool = True
    token: str = Field(..., min_length=32)
    user: UserProfileResponse


class CurrentUserResponse(BaseModel):
    authenticated: bool = True
    user: UserProfileResponse


# ---------------------------------------------------------------------------
# 2. Customer Schemas
# ---------------------------------------------------------------------------

class CustomerCreateRequest(BaseModel):
    billing_name: str = Field(..., min_length=2, max_length=150)
    billing_address: str = Field(..., min_length=5)
    city: Optional[str] = None
    state: str = Field(..., min_length=2, max_length=50)
    country: str = Field(default="IN", max_length=10)
    pin_code: Optional[str] = None
    gst_number: Optional[str] = Field(None, pattern=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
    contact_person: Optional[str] = None
    designation: Optional[str] = None
    email: Optional[str] = Field(None, max_length=100)
    phone_1: Optional[str] = None
    phone_2: Optional[str] = None


class CustomerResponse(BaseModel):
    id: int
    billing_name: str
    billing_address: str
    city: Optional[str] = None
    state: str
    country: str
    pin_code: Optional[str] = None
    gst_number: Optional[str] = None
    contact_person: Optional[str] = None
    designation: Optional[str] = None
    email: Optional[str] = None
    phone_1: Optional[str] = None
    is_active: bool = True
    # Financial exposure fields (computed)
    total_billed: str = "0.00"
    total_cash: str = "0.00"
    total_tds: str = "0.00"
    total_settled: str = "0.00"
    outstanding_balance: str = "0.00"
    unbilled_orders_value: str = "0.00"


# ---------------------------------------------------------------------------
# 3. Purchase Orders & Commercial Invoices
# ---------------------------------------------------------------------------

class LineItemCreateRequest(BaseModel):
    item_description: str = Field(..., min_length=1, max_length=500)
    quantity: Decimal = Field(default=Decimal("1.0000"), gt=Decimal("0.0000"))
    unit_price: Decimal = Field(..., ge=Decimal("0.00"))
    tax_rate: Decimal = Field(default=Decimal("0.1800"), ge=Decimal("0.0000"), le=Decimal("1.0000"))


class PurchaseOrderCreateRequest(BaseModel):
    fy_code: str = Field(..., pattern=r"^FY \d{4}-\d{2}$")
    po_number: str = Field(..., min_length=2, max_length=50)
    po_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    customer_id: int = Field(..., gt=0)
    executive_id: int = Field(..., gt=0)
    currency: str = Field(default="INR", max_length=10)
    exchange_rate: Decimal = Field(default=Decimal("1.0000"), gt=Decimal("0.0000"))
    exclusive_amount: Decimal = Field(..., ge=Decimal("0.00"))
    tax_rate: Decimal = Field(default=Decimal("0.1800"), ge=Decimal("0.0000"), le=Decimal("1.0000"))
    remarks: Optional[str] = None
    items: Optional[List[LineItemCreateRequest]] = None


class PurchaseOrderResponse(BaseModel):
    id: int
    fy_code: str
    po_number: str
    po_date: str
    customer_id: int
    customer_name: str
    executive_id: int
    executive_name: str
    currency: str
    exchange_rate: str
    exclusive_amount: str
    tax_rate: str
    tax_amount: str
    total_amount: str
    status: OrderBillingStatus
    remarks: Optional[str] = None


class ConvertOrderToInvoiceRequest(BaseModel):
    invoice_number: str = Field(..., min_length=2, max_length=50)
    invoice_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    remarks: Optional[str] = None


class CommercialInvoiceResponse(BaseModel):
    id: int
    po_id: Optional[int] = None
    po_number: Optional[str] = None
    fy_code: str
    customer_id: int
    customer_name: str
    executive_id: int
    executive_name: str
    invoice_number: str
    invoice_date: str
    due_date: str
    currency: str
    exchange_rate: str
    exclusive_amount: str
    tax_rate: str
    tax_amount: str
    total_amount: str
    # GST Breakdown
    cgst_amount: str
    sgst_amount: str
    igst_amount: str
    # Settlements & Balances
    total_cash_paid: str
    total_tds_deducted: str
    total_paid: str
    outstanding_balance: str
    lifecycle_status: InvoiceLifecycleStatus
    payment_status: InvoicePaymentStatus
    days_overdue: int
    remarks: Optional[str] = None


class InvoiceCancelRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


# ---------------------------------------------------------------------------
# 4. Settlements & Compensating Reversals
# ---------------------------------------------------------------------------

class PaymentCreateRequest(BaseModel):
    invoice_id: int = Field(..., gt=0)
    payment_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    tds_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    tds_section: Optional[TdsSection] = None
    payment_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    payment_mode: PaymentMode = PaymentMode.NEFT
    remittance_number: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = Field(None, max_length=500)
    idempotency_key: Optional[str] = Field(None, max_length=100)

    @model_validator(mode="after")
    def validate_total_settling(self):
        total = quantize_2dec(self.payment_amount) + quantize_2dec(self.tds_amount)
        if total <= Decimal("0.00"):
            raise ValueError("Payment amount or TDS deduction must be greater than zero")
        return self


class PaymentCorrectionRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)
    corrected_payment_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    corrected_tds_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    corrected_tds_section: Optional[TdsSection] = None
    corrected_payment_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    corrected_payment_mode: PaymentMode = PaymentMode.NEFT
    corrected_remittance_number: Optional[str] = None
    notes: Optional[str] = None


class PaymentTransactionResponse(BaseModel):
    id: int
    invoice_id: int
    parent_transaction_id: Optional[int] = None
    transaction_type: TransactionType
    cash_amount: str
    tds_amount: str
    total_settled: str
    tds_section: Optional[str] = None
    payment_date: str
    remittance_number: Optional[str] = None
    payment_mode: PaymentMode
    notes: Optional[str] = None
    recorded_by: int
    created_at: str


# ---------------------------------------------------------------------------
# 5. Sales Targets & Reporting Hub
# ---------------------------------------------------------------------------

class SalesTargetUpdateRequest(BaseModel):
    yearly_target: Decimal = Field(..., ge=Decimal("0.00"))
    half_yearly_target: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    quarterly_target: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    monthly_target: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    monthly_bottom_line: Optional[Decimal] = Field(Decimal("0.00"), ge=Decimal("0.00"))
    yearly_bottom_line: Optional[Decimal] = Field(Decimal("0.00"), ge=Decimal("0.00"))


class SalesTargetResponse(BaseModel):
    id: int
    user_id: int
    executive_name: str
    fy_code: str
    yearly_target: str
    half_yearly_target: str
    quarterly_target: str
    monthly_target: str
    monthly_bottom_line: str
    yearly_bottom_line: str


class DashboardSummaryResponse(BaseModel):
    fy_code: str
    as_of_date: str
    total_invoiced: str
    total_cash: str
    total_tds: str
    total_settled: str
    total_outstanding: str
    unbilled_orders_value: str
    collection_rate_pct: str


class ArAgingBucketItem(BaseModel):
    bucket: str
    amount: str
    count: int


class ArAgingReportResponse(BaseModel):
    as_of_date: str
    total_receivables: str
    total_overdue: str
    buckets: List[ArAgingBucketItem]
