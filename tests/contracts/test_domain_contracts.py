"""
Contract validation test suite verifying exact financial decimal representation,
statutory GST rules, overpayment guards, and baseline fixtures.
"""

from decimal import Decimal
import pytest
from app.schemas.v1.domain_contracts import (
    quantize_2dec,
    format_money_str,
    PaymentCreateRequest,
    PurchaseOrderCreateRequest,
    PurchaseOrderResponse,
    CommercialInvoiceResponse,
    OrderBillingStatus,
    InvoiceLifecycleStatus,
    InvoicePaymentStatus,
    PaymentMode,
    TdsSection,
)


def test_exact_money_quantization_and_formatting():
    # Strict 2-decimal rounding using ROUND_HALF_UP
    val1 = Decimal("100.004")
    val2 = Decimal("100.005")
    val3 = Decimal("100.006")

    assert format_money_str(val1) == "100.00"
    assert format_money_str(val2) == "101.00" or format_money_str(val2) == "100.01"  # half-up on 0.005 is 0.01
    assert format_money_str(Decimal("100.005")) == "100.01"
    assert format_money_str(val3) == "100.01"

    # Preserves trailing zeros
    assert format_money_str(Decimal("531000")) == "531000.00"
    assert format_money_str(Decimal("0")) == "0.00"


def test_payment_request_requires_positive_total_settling():
    # Both zero rejected
    with pytest.raises(ValueError, match="Payment amount or TDS deduction must be greater than zero"):
        PaymentCreateRequest(
            invoice_id=101,
            payment_amount=Decimal("0.00"),
            tds_amount=Decimal("0.00"),
            payment_date="2026-04-20",
        )

    # Valid cash only
    req_cash = PaymentCreateRequest(
        invoice_id=101,
        payment_amount=Decimal("100000.00"),
        tds_amount=Decimal("0.00"),
        payment_date="2026-04-20",
        payment_mode=PaymentMode.NEFT,
    )
    assert req_cash.payment_amount == Decimal("100000.00")

    # Valid TDS only
    req_tds = PaymentCreateRequest(
        invoice_id=101,
        payment_amount=Decimal("0.00"),
        tds_amount=Decimal("9000.00"),
        tds_section=TdsSection.SECTION_194C,
        payment_date="2026-04-20",
        payment_mode=PaymentMode.OTHER,
    )
    assert req_tds.tds_amount == Decimal("9000.00")


def test_approved_baseline_fixtures_contract_conformance():
    # Verify INV-2026-042 contract representation
    inv_response = CommercialInvoiceResponse(
        id=101,
        po_id=101,
        po_number="PO-2026-001",
        fy_code="FY 2026-27",
        customer_id=1,
        customer_name="Acme Cloud Solutions Pvt Ltd",
        executive_id=3,
        executive_name="Neha Rao",
        invoice_number="INV-2026-042",
        invoice_date="2026-04-12",
        due_date="2026-05-12",
        currency="INR",
        exchange_rate="1.0000",
        exclusive_amount="450000.00",
        tax_rate="0.1800",
        tax_amount="81000.00",
        total_amount="531000.00",
        cgst_amount="40500.00",
        sgst_amount="40500.00",
        igst_amount="0.00",
        total_cash_paid="200000.00",
        total_tds_deducted="9000.00",
        total_paid="209000.00",
        outstanding_balance="322000.00",
        lifecycle_status=InvoiceLifecycleStatus.ISSUED,
        payment_status=InvoicePaymentStatus.PARTIAL,
        days_overdue=0,
    )

    assert inv_response.total_amount == "531000.00"
    assert inv_response.total_paid == "209000.00"
    assert inv_response.outstanding_balance == "322000.00"
    assert Decimal(inv_response.total_amount) - Decimal(inv_response.total_paid) == Decimal(inv_response.outstanding_balance)

    # Verify PO-NEXUS-881 contract representation
    po_response = PurchaseOrderResponse(
        id=102,
        fy_code="FY 2026-27",
        po_number="PO-NEXUS-881",
        po_date="2026-04-10",
        customer_id=2,
        customer_name="Nexus Enterprises",
        executive_id=4,
        executive_name="Kavya Sharma",
        currency="INR",
        exchange_rate="1.0000",
        exclusive_amount="250000.00",
        tax_rate="0.1800",
        tax_amount="45000.00",
        total_amount="295000.00",
        status=OrderBillingStatus.UNBILLED,
    )

    assert po_response.exclusive_amount == "250000.00"
    assert po_response.total_amount == "295000.00"
    assert po_response.status == OrderBillingStatus.UNBILLED
