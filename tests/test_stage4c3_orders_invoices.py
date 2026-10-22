"""
Stage 4C.3 Automated Test Suite: Orders, Invoices & Statutory Conversion Engine.

Validates against live PostgreSQL database:
1. Purchase Order CRUD & line item financial math (ROUND_HALF_UP).
2. PO-to-Commercial-Invoice conversion with row locking, customer snapshotting, and statutory GST splits.
3. Conversion guardrails: duplicate rejection, double-conversion prevention, lifecycle protection.
4. Direct invoice creation with FY date boundary validation and Net-30 default due date.
5. Statutory GST calculation: Intra-state (Karnataka CGST/SGST 50/50 split) vs Inter-state (IGST 100%).
6. Customer snapshot immutability upon subsequent customer profile modifications.
7. Statutory cancellation workflow with mandatory reason and settlement guardrails.
8. Physical deletion prohibition on issued commercial invoices.
9. RBAC matrix enforcement (admin, finance, sales, viewer).
"""

import datetime
from decimal import Decimal
import re
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import get_db_session
from app.main import app
from app.models.domain_models import (
    CommercialInvoice,
    CommercialInvoiceItem,
    Customer,
    FinancialYear,
    GstRateMaster,
    PaymentTransaction,
    PurchaseOrder,
    PurchaseOrderItem,
    User,
)

client = TestClient(app)


def _get_token(username: str = "sandbox_admin", password: str = "AdminPass123!") -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"Login failed for {username}: {resp.text}"
    return resp.json()["token"]


@pytest.fixture(autouse=True)
def cleanup_test_records():
    def _do_cleanup():
        with get_db_session() as db:
            test_invs = db.scalars(select(CommercialInvoice).where(CommercialInvoice.invoice_number.ilike("INV-TEST-%"))).all()
            for inv in test_invs:
                items = db.scalars(select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == inv.id)).all()
                for it in items:
                    db.delete(it)
                db.delete(inv)
            test_pos = db.scalars(select(PurchaseOrder).where(PurchaseOrder.po_number.ilike("PO-TEST-%"))).all()
            for po in test_pos:
                items = db.scalars(select(PurchaseOrderItem).where(PurchaseOrderItem.po_id == po.id)).all()
                for it in items:
                    db.delete(it)
                db.delete(po)
            test_custs = db.scalars(select(Customer).where(Customer.gst_number.in_(["29TESTG0000Z1Z1", "27MODIF1111A1Z9"]))).all()
            for c in test_custs:
                db.delete(c)
            db.commit()

    _do_cleanup()
    yield
    _do_cleanup()


def test_orders_crud_and_calculation():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    token_sales = _get_token("sandbox_sales", "SalesPass123!")
    token_viewer = _get_token("sandbox_viewer", "ViewerPass123!")

    headers_admin = {"Authorization": f"Bearer {token_admin}"}
    headers_sales = {"Authorization": f"Bearer {token_sales}"}
    headers_viewer = {"Authorization": f"Bearer {token_viewer}"}

    # 1. Create Purchase Order with 2 line items (sales can create)
    po_payload = {
        "fy_code": "FY 2026-27",
        "po_number": "PO-TEST-S3-001",
        "po_date": "2026-05-10",
        "customer_id": 1,
        "remarks": "Stage 4C.3 Test PO",
        "items": [
            {
                "item_description": "Cloud Architecture Review",
                "quantity": 10.0,
                "unit_price": 5000.0,
                "tax_rate": 0.18,
            },
            {
                "item_description": "Security Hardening Hours",
                "quantity": 5.0,
                "unit_price": 4000.0,
                "tax_rate": 0.18,
            },
        ],
    }

    create_resp = client.post("/api/orders", json=po_payload, headers=headers_sales)
    assert create_resp.status_code in (200, 201), create_resp.text
    po_id = create_resp.json()["id"]

    # Verify calculation:
    # Item 1: 10 * 5000 = 50,000, Tax = 9,000, Tot = 59,000
    # Item 2: 5 * 4000 = 20,000, Tax = 3,600, Tot = 23,600
    # Total excl: 70,000.00, Tax: 12,600.00, Total: 82,600.00
    get_resp = client.get(f"/api/orders/{po_id}", headers=headers_sales)
    assert get_resp.status_code == 200
    po_data = get_resp.json()
    assert po_data["po_number"] == "PO-TEST-S3-001"
    assert po_data["exclusive_amount"] == "70000.00"
    assert po_data["tax_amount"] == "12600.00"
    assert po_data["total_amount"] == "82600.00"
    assert po_data["outstanding_balance"] == "0.00"
    assert po_data["unbilled_amount"] == "82600.00"
    assert po_data["status"] == "UNBILLED"
    assert po_data["computed_status"] == "UNBILLED"
    assert len(po_data["line_items"]) == 2

    # 2. List orders and filter by FY and status
    list_resp = client.get("/api/orders?fy=FY 2026-27&status=UNBILLED", headers=headers_viewer)
    assert list_resp.status_code == 200
    orders = list_resp.json()["data"]
    assert any(o["id"] == po_id for o in orders)

    # 3. Edit unbilled PO
    update_payload = {
        "remarks": "Updated Test PO Remarks",
        "items": [
            {
                "item_description": "Cloud Architecture Review Extended",
                "quantity": 12.0,
                "unit_price": 5000.0,
                "tax_rate": 0.18,
            }
        ],
    }
    edit_resp = client.put(f"/api/orders/{po_id}", json=update_payload, headers=headers_sales)
    assert edit_resp.status_code == 200

    updated_get = client.get(f"/api/orders/{po_id}", headers=headers_admin)
    assert updated_get.status_code == 200
    assert updated_get.json()["exclusive_amount"] == "60000.00"
    assert updated_get.json()["total_amount"] == "70800.00"
    assert updated_get.json()["outstanding_balance"] == "0.00"
    assert updated_get.json()["unbilled_amount"] == "70800.00"
    assert updated_get.json()["remarks"] == "Updated Test PO Remarks"

    # 4. Sales cannot delete unbilled PO (403)
    del_sales_resp = client.delete(f"/api/orders/{po_id}", headers=headers_sales)
    assert del_sales_resp.status_code == 403

    # 5. Admin deletes unbilled PO
    del_resp = client.delete(f"/api/orders/{po_id}", headers=headers_admin)
    assert del_resp.status_code == 200

    # Verify 404 after deletion
    assert client.get(f"/api/orders/{po_id}", headers=headers_admin).status_code == 404


def test_po_to_invoice_conversion_workflow():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    token_finance = _get_token("sandbox_finance", "FinancePass123!")
    token_sales = _get_token("sandbox_sales", "SalesPass123!")
    headers_admin = {"Authorization": f"Bearer {token_admin}"}
    headers_finance = {"Authorization": f"Bearer {token_finance}"}
    headers_sales = {"Authorization": f"Bearer {token_sales}"}

    # 1. Create a PO
    po_resp = client.post(
        "/api/orders",
        json={
            "fy_code": "FY 2026-27",
            "po_number": "PO-TEST-CONV-01",
            "po_date": "2026-06-01",
            "customer_id": 1,
            "exclusive_amount": 100000.0,
            "tax_rate": 0.18,
            "remarks": "Conversion Test PO",
        },
        headers=headers_admin,
    )
    assert po_resp.status_code in (200, 201)
    po_id = po_resp.json()["id"]

    # 2. Sales cannot convert PO to invoice (403)
    conv_sales_resp = client.post(
        f"/api/orders/{po_id}/convert",
        json={"invoice_number": "INV-TEST-CONV-01", "invoice_date": "2026-06-05"},
        headers=headers_sales,
    )
    assert conv_sales_resp.status_code == 403

    # 3. Finance converts PO to invoice
    conv_resp = client.post(
        f"/api/orders/{po_id}/convert",
        json={
            "invoice_number": "INV-TEST-CONV-01",
            "invoice_date": "2026-06-05",
            # due_date omitted: should default to Net-30 (2026-07-05)
        },
        headers=headers_finance,
    )
    assert conv_resp.status_code in (200, 201), conv_resp.text
    conv_data = conv_resp.json()
    assert conv_data["success"] is True
    inv_id = conv_data["invoice_id"]
    assert conv_data["due_date"] == "2026-07-05"

    # 4. Verify PO status is now CONVERTED
    po_check = client.get(f"/api/orders/{po_id}", headers=headers_admin).json()
    assert po_check["status"] == "CONVERTED"

    # 5. Verify converted PO cannot be converted again (double-conversion prevention)
    dup_conv = client.post(
        f"/api/orders/{po_id}/convert",
        json={"invoice_number": "INV-TEST-CONV-02", "invoice_date": "2026-06-06"},
        headers=headers_admin,
    )
    assert dup_conv.status_code in (400, 409)
    assert "already been converted" in dup_conv.json()["detail"]

    # 6. Verify converted PO cannot be edited or deleted
    assert client.put(f"/api/orders/{po_id}", json={"remarks": "fail"}, headers=headers_admin).status_code in (400, 409)
    assert client.delete(f"/api/orders/{po_id}", headers=headers_admin).status_code in (400, 409)

    # 7. Verify Commercial Invoice details
    inv_check = client.get(f"/api/invoices/{inv_id}", headers=headers_admin).json()["invoice"]
    assert inv_check["invoice_number"] == "INV-TEST-CONV-01"
    assert inv_check["invoice_date"] == "2026-06-05"
    assert inv_check["due_date"] == "2026-07-05"
    assert inv_check["exclusive_amount"] == "100000.00"
    assert inv_check["tax_amount"] == "18000.00"
    assert inv_check["total_amount"] == "118000.00"
    assert inv_check["outstanding_balance"] == "118000.00"
    assert inv_check["payment_status"] == "PENDING"
    assert inv_check["computed_status"] in ("PENDING", "OVERDUE")
    assert inv_check["lifecycle_status"] == "ISSUED"

    # Clean up created invoice for idempotency
    with get_db_session() as db:
        inv_item = db.execute(select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == inv_id)).scalars().all()
        for it in inv_item:
            db.delete(it)
        inv_row = db.scalar(select(CommercialInvoice).where(CommercialInvoice.id == inv_id))
        if inv_row:
            db.delete(inv_row)
        po_row = db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == po_id))
        if po_row:
            db.delete(po_row)
        db.commit()


def test_statutory_gst_split_inter_vs_intra():
    token = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token}"}

    # Customer 2 is Karnataka (Intra-state)
    # Customer 1 is Maharashtra (Inter-state)
    # Let's verify Customer 1 vs Customer 2 states in DB
    with get_db_session() as db:
        c1 = db.scalar(select(Customer).where(Customer.id == 1))
        c2 = db.scalar(select(Customer).where(Customer.id == 2))
        assert c2.state.lower() == "karnataka"
        assert c1.state.lower() == "maharashtra"

    # 1. Direct Invoice for Karnataka Customer (Intra-state) - Customer 2
    # Exclusive: 50,000, 18% Tax: 9,000 -> CGST: 4500, SGST: 4500, IGST: 0
    inv1_resp = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "invoice_number": "INV-TEST-GST-INTRA",
            "invoice_date": "2026-06-10",
            "customer_id": 2,
            "exclusive_amount": 50000.0,
            "tax_rate": 0.18,
        },
        headers=headers,
    )
    assert inv1_resp.status_code in (200, 201), inv1_resp.text
    inv1_id = inv1_resp.json()["id"]

    inv1 = client.get(f"/api/invoices/{inv1_id}", headers=headers).json()["invoice"]
    assert inv1["tax_amount"] == "9000.00"
    assert inv1["cgst_amount"] == "4500.00"
    assert inv1["sgst_amount"] == "4500.00"
    assert inv1["igst_amount"] == "0.00"

    # 2. Direct Invoice for Maharashtra Customer (Inter-state) - Customer 1
    # Exclusive: 50,000, 18% Tax: 9,000 -> CGST: 0, SGST: 0, IGST: 9000
    inv2_resp = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "invoice_number": "INV-TEST-GST-INTER",
            "invoice_date": "2026-06-10",
            "customer_id": 1,
            "exclusive_amount": 50000.0,
            "tax_rate": 0.18,
        },
        headers=headers,
    )
    assert inv2_resp.status_code in (200, 201), inv2_resp.text
    inv2_id = inv2_resp.json()["id"]

    inv2 = client.get(f"/api/invoices/{inv2_id}", headers=headers).json()["invoice"]
    assert inv2["tax_amount"] == "9000.00"
    assert inv2["cgst_amount"] == "0.00"
    assert inv2["sgst_amount"] == "0.00"
    assert inv2["igst_amount"] == "9000.00"

    # Clean up
    with get_db_session() as db:
        for i_id in (inv1_id, inv2_id):
            items = db.scalars(select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == i_id)).all()
            for it in items:
                db.delete(it)
            inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.id == i_id))
            if inv:
                db.delete(inv)
        db.commit()


def test_customer_snapshot_immutability():
    token = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Create dedicated customer
    cust_resp = client.post(
        "/api/customers",
        json={
            "billing_name": "Original Customer Name Pvt Ltd",
            "gst_number": "29TESTG0000Z1Z1",
            "billing_address": "Original Address, Bangalore",
            "city": "Bangalore",
            "state": "Karnataka",
            "contact_person": "Origin Lead",
        },
        headers=headers,
    )
    assert cust_resp.status_code == 200
    cust_id = cust_resp.json()["id"]

    # 2. Issue Invoice for this customer
    inv_resp = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "invoice_number": "INV-TEST-SNAPSHOT-01",
            "invoice_date": "2026-07-01",
            "customer_id": cust_id,
            "exclusive_amount": 30000.0,
            "tax_rate": 0.18,
        },
        headers=headers,
    )
    assert inv_resp.status_code in (200, 201), inv_resp.text
    inv_id = inv_resp.json()["id"]

    # 3. Update customer details in master database
    cust_update = client.put(
        f"/api/customers/{cust_id}",
        json={
            "billing_name": "MODIFIED Customer Brand Worldwide",
            "billing_address": "New Shiny Tower, Mumbai",
            "city": "Mumbai",
            "state": "Maharashtra",
            "gst_number": "27MODIF1111A1Z9",
        },
        headers=headers,
    )
    assert cust_update.status_code == 200

    # 4. Verify Invoice snapshot is unchanged
    inv_check = client.get(f"/api/invoices/{inv_id}", headers=headers).json()["invoice"]
    assert inv_check["customer_name"] == "Original Customer Name Pvt Ltd"
    assert inv_check["customer_gstin"] == "29TESTG0000Z1Z1"
    assert inv_check["customer_address"] == "Original Address, Bangalore"
    assert inv_check["customer_state"] == "Karnataka"

    # Clean up
    with get_db_session() as db:
        items = db.scalars(select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == inv_id)).all()
        for it in items:
            db.delete(it)
        inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.id == inv_id))
        if inv:
            db.delete(inv)
        cust = db.scalar(select(Customer).where(Customer.id == cust_id))
        if cust:
            db.delete(cust)
        db.commit()


def test_invoice_cancellation_workflow():
    token = _get_token("sandbox_finance", "FinancePass123!")
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Create invoice without payments
    inv_resp = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "invoice_number": "INV-TEST-CANCEL-01",
            "invoice_date": "2026-07-15",
            "customer_id": 1,
            "exclusive_amount": 25000.0,
            "tax_rate": 0.18,
        },
        headers=headers,
    )
    assert inv_resp.status_code in (200, 201), inv_resp.text
    inv_id = inv_resp.json()["id"]

    # 2. Cancellation without reason should be rejected (422 validation error)
    bad_cancel = client.post(f"/api/invoices/{inv_id}/cancel", json={"reason": " "}, headers=headers)
    assert bad_cancel.status_code in (400, 422)

    # 3. Successful cancellation with mandatory reason
    cancel_resp = client.post(
        f"/api/invoices/{inv_id}/cancel",
        json={"reason": "Customer project cancelled under formal written request"},
        headers=headers,
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["lifecycle_status"] == "CANCELLED"

    # 4. Verify cancelled invoice status and zeroed outstanding balance
    inv_check = client.get(f"/api/invoices/{inv_id}", headers=headers).json()["invoice"]
    assert inv_check["lifecycle_status"] == "CANCELLED"
    assert inv_check["payment_status"] == "CANCELLED"
    assert inv_check["computed_status"] == "CANCELLED"
    assert inv_check["outstanding_balance"] == "0.00"
    assert inv_check["cancellation_reason"] == "Customer project cancelled under formal written request"

    # 5. Re-cancelling cancelled invoice rejected with 400
    re_cancel = client.post(
        f"/api/invoices/{inv_id}/cancel",
        json={"reason": "Another reason"},
        headers=headers,
    )
    assert re_cancel.status_code == 400
    assert "already cancelled" in re_cancel.json()["detail"]

    # 6. Metadata editing rejected on cancelled invoice
    assert client.put(f"/api/invoices/{inv_id}", json={"remarks": "test"}, headers=headers).status_code == 400

    # 7. Physical deletion prohibited
    del_resp = client.delete(f"/api/invoices/{inv_id}", headers=headers)
    assert del_resp.status_code == 400
    assert "cannot be permanently deleted" in del_resp.json()["detail"]

    # Clean up
    with get_db_session() as db:
        items = db.scalars(select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == inv_id)).all()
        for it in items:
            db.delete(it)
        inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.id == inv_id))
        if inv:
            db.delete(inv)
        db.commit()


def test_invoice_cancellation_blocked_with_settlements():
    token = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token}"}

    # Invoice 1 (INV-2026-042) has payment transaction REM-2026-001
    cancel_resp = client.post(
        "/api/invoices/1/cancel",
        json={"reason": "Attempting illegal cancellation of settled invoice"},
        headers=headers,
    )
    assert cancel_resp.status_code == 400
    assert "Cannot cancel invoice with recorded settlements" in cancel_resp.json()["detail"]


def test_stage4c3_rbac_matrix():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    token_finance = _get_token("sandbox_finance", "FinancePass123!")
    token_sales = _get_token("sandbox_sales", "SalesPass123!")
    token_viewer = _get_token("sandbox_viewer", "ViewerPass123!")

    # 1. Sales cannot create commercial invoices directly (403)
    sales_create_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "invoice_number": "INV-SALES-ILLEGAL",
            "invoice_date": "2026-07-20",
            "customer_id": 1,
            "exclusive_amount": 10000.0,
        },
        headers={"Authorization": f"Bearer {token_sales}"},
    )
    assert sales_create_inv.status_code == 403

    # 2. Viewer cannot create POs or invoices (403)
    viewer_create_po = client.post(
        "/api/orders",
        json={
            "fy_code": "FY 2026-27",
            "po_date": "2026-07-20",
            "customer_id": 1,
            "exclusive_amount": 10000.0,
        },
        headers={"Authorization": f"Bearer {token_viewer}"},
    )
    assert viewer_create_po.status_code == 403

    viewer_create_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "invoice_number": "INV-VIEWER-ILLEGAL",
            "invoice_date": "2026-07-20",
            "customer_id": 1,
            "exclusive_amount": 10000.0,
        },
        headers={"Authorization": f"Bearer {token_viewer}"},
    )
    assert viewer_create_inv.status_code == 403


MONEY_REGEX = re.compile(r"^-?\d+\.\d{2}$")


def test_cf4c3_01_monetary_tax_integrity_and_serialization():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers_admin = {"Authorization": f"Bearer {token_admin}"}

    # 1. Fractional amounts & half-up rounding boundary
    # Qty 3.3333 * Unit Price 100.335 -> excl = 334.45
    # Tax rate 0.18 -> tax = 334.45 * 0.18 = 60.201 -> 60.20
    # Total = 334.45 + 60.20 = 394.65
    po_payload = {
        "fy_code": "FY 2026-27",
        "po_number": "PO-TEST-ROUNDING-01",
        "po_date": "2026-05-15",
        "customer_id": 1,
        "items": [
            {
                "item_description": "Fractional Precision Hours",
                "quantity": 3.3333,
                "unit_price": 100.335,
                "tax_rate": 0.18,
            }
        ],
    }
    po_resp = client.post("/api/orders", json=po_payload, headers=headers_admin)
    assert po_resp.status_code == 201, po_resp.text
    po_data = po_resp.json()
    assert MONEY_REGEX.match(po_data["exclusive_amount"])
    assert MONEY_REGEX.match(po_data["tax_amount"])
    assert MONEY_REGEX.match(po_data["total_amount"])
    assert MONEY_REGEX.match(po_data["outstanding_balance"])
    assert MONEY_REGEX.match(po_data["unbilled_amount"])
    assert po_data["exclusive_amount"] == "334.46"
    assert po_data["tax_amount"] == "60.20"
    assert po_data["total_amount"] == "394.66"
    assert po_data["outstanding_balance"] == "0.00"
    assert po_data["unbilled_amount"] == "394.66"

    # Line item serialization check
    li = po_data["line_items"][0]
    assert MONEY_REGEX.match(li["unit_price"])
    assert MONEY_REGEX.match(li["exclusive_amount"])
    assert MONEY_REGEX.match(li["tax_amount"])
    assert MONEY_REGEX.match(li["total_amount"])

    # 2. Inconsistent caller-supplied tax_amount rejected with HTTP 400
    bad_tax_payload = {
        "fy_code": "FY 2026-27",
        "po_number": "PO-TEST-BAD-TAX",
        "po_date": "2026-05-15",
        "customer_id": 1,
        "tax_amount": 999.00,  # Highly inconsistent with line item
        "items": [
            {
                "item_description": "Standard Task",
                "quantity": 1.0,
                "unit_price": 1000.0,
                "tax_rate": 0.18,
            }
        ],
    }
    bad_tax_resp = client.post("/api/orders", json=bad_tax_payload, headers=headers_admin)
    assert bad_tax_resp.status_code == 400
    assert "inconsistent" in bad_tax_resp.json()["detail"].lower()

    # 3. Inconsistent caller-supplied total_amount rejected with HTTP 400
    bad_tot_payload = {
        "fy_code": "FY 2026-27",
        "po_number": "PO-TEST-BAD-TOT",
        "po_date": "2026-05-15",
        "customer_id": 1,
        "total_amount": 5000.00,  # Should be 1180.00
        "items": [
            {
                "item_description": "Standard Task",
                "quantity": 1.0,
                "unit_price": 1000.0,
                "tax_rate": 0.18,
            }
        ],
    }
    bad_tot_resp = client.post("/api/orders", json=bad_tot_payload, headers=headers_admin)
    assert bad_tot_resp.status_code == 400
    assert "inconsistent" in bad_tot_resp.json()["detail"].lower()

    # 4. Inconsistent caller-supplied amounts on direct invoice rejected with HTTP 400
    bad_inv_tax = {
        "fy_code": "FY 2026-27",
        "invoice_number": "INV-TEST-BAD-TAX",
        "invoice_date": "2026-06-15",
        "customer_id": 1,
        "exclusive_amount": 10000.00,
        "tax_rate": 0.18,
        "tax_amount": 500.00,  # Inconsistent with 18% of 10,000 (1800.00)
    }
    bad_inv_resp = client.post("/api/invoices", json=bad_inv_tax, headers=headers_admin)
    assert bad_inv_resp.status_code == 400
    assert "inconsistent" in bad_inv_resp.json()["detail"].lower()

    # 5. Multi-line totals exact decimal sum on direct invoice
    multi_inv_payload = {
        "fy_code": "FY 2026-27",
        "invoice_number": "INV-TEST-MULTI-LINE",
        "invoice_date": "2026-06-15",
        "customer_id": 2,
        "items": [
            {
                "description": "Item A",
                "quantity": 2.5,
                "unit_price": 1500.0,
                "tax_rate": 0.18,
            },
            {
                "description": "Item B",
                "quantity": 4.0,
                "unit_price": 2250.75,
                "tax_rate": 0.18,
            },
        ],
    }
    multi_resp = client.post("/api/invoices", json=multi_inv_payload, headers=headers_admin)
    assert multi_resp.status_code in (200, 201), multi_resp.text
    inv_id = multi_resp.json()["id"]

    inv_get = client.get(f"/api/invoices/{inv_id}", headers=headers_admin).json()["invoice"]
    # Item A: 2.5 * 1500 = 3750.00, tax = 675.00, tot = 4425.00
    # Item B: 4.0 * 2250.75 = 9003.00, tax = 1620.54, tot = 10623.54
    # Excl sum: 12753.00, Tax sum: 2295.54, Total: 15048.54
    assert inv_get["exclusive_amount"] == "12753.00"
    assert inv_get["tax_amount"] == "2295.54"
    assert inv_get["total_amount"] == "15048.54"
    assert inv_get["outstanding_balance"] == "15048.54"
    assert MONEY_REGEX.match(inv_get["total_cash_paid"])
    assert MONEY_REGEX.match(inv_get["total_tds_deducted"])
    assert MONEY_REGEX.match(inv_get["total_paid"])


def test_cf4c3_02_unbilled_receivables_and_po_nexus_881():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    # 1. Verify baseline PO-NEXUS-881 via GET /api/orders
    orders_resp = client.get("/api/orders?search=PO-NEXUS-881", headers=headers)
    assert orders_resp.status_code == 200
    nexus_orders = [o for o in orders_resp.json()["data"] if o["po_number"] == "PO-NEXUS-881"]
    assert len(nexus_orders) == 1, f"Expected PO-NEXUS-881 in orders list: {orders_resp.text}"
    nexus = nexus_orders[0]

    # Gross value preserved separately
    assert nexus["total_amount"] == "295000.00"
    assert nexus["exclusive_amount"] == "250000.00"
    assert nexus["tax_amount"] == "45000.00"
    assert nexus["unbilled_amount"] == "295000.00"
    # Zero recognized AR
    assert nexus["outstanding_balance"] == "0.00"
    assert nexus["recognized_ar"] == "0.00"
    assert nexus["status"] == "UNBILLED"
    assert nexus["computed_status"] == "UNBILLED"

    # 2. Verify via single order lookup GET /api/orders/{id}
    nexus_id = nexus["id"]
    single_get = client.get(f"/api/orders/{nexus_id}", headers=headers).json()
    assert single_get["total_amount"] == "295000.00"
    assert single_get["unbilled_amount"] == "295000.00"
    assert single_get["outstanding_balance"] == "0.00"
    assert single_get["recognized_ar"] == "0.00"


def test_cf4c3_04_conversion_due_date_boundaries():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    # 1. Create unbilled PO
    po_resp = client.post(
        "/api/orders",
        json={
            "fy_code": "FY 2026-27",
            "po_number": "PO-TEST-DUE-BOUNDS",
            "po_date": "2026-06-01",
            "customer_id": 1,
            "exclusive_amount": 50000.00,
            "tax_rate": 0.18,
        },
        headers=headers,
    )
    assert po_resp.status_code == 201
    po_id = po_resp.json()["id"]

    # 2. Negative test: due_date earlier than invoice_date rejected with HTTP 400
    early_due_resp = client.post(
        f"/api/orders/{po_id}/convert",
        json={
            "invoice_number": "INV-TEST-EARLY-DUE",
            "invoice_date": "2026-06-15",
            "due_date": "2026-06-10",  # 5 days BEFORE invoice_date
        },
        headers=headers,
    )
    assert early_due_resp.status_code == 400
    assert "due date cannot precede invoice date" in early_due_resp.json()["detail"].lower()

    # 3. Boundary test: due_date == invoice_date accepted
    same_due_resp = client.post(
        f"/api/orders/{po_id}/convert",
        json={
            "invoice_number": "INV-TEST-SAME-DUE",
            "invoice_date": "2026-06-15",
            "due_date": "2026-06-15",  # Same day due date
        },
        headers=headers,
    )
    assert same_due_resp.status_code in (200, 201), same_due_resp.text
    conv_inv = same_due_resp.json()
    assert conv_inv["due_date"] == "2026-06-15"
    assert conv_inv["invoice_date"] == "2026-06-15"

    # 4. Default Net-30: Omitted due_date defaults to Net 30
    po_resp2 = client.post(
        "/api/orders",
        json={
            "fy_code": "FY 2026-27",
            "po_number": "PO-TEST-NET30-DEF",
            "po_date": "2026-07-01",
            "customer_id": 1,
            "exclusive_amount": 25000.00,
            "tax_rate": 0.18,
        },
        headers=headers,
    )
    assert po_resp2.status_code == 201
    po_id2 = po_resp2.json()["id"]

    net30_resp = client.post(
        f"/api/orders/{po_id2}/convert",
        json={
            "invoice_number": "INV-TEST-NET30-DEF",
            "invoice_date": "2026-07-01",
            # due_date omitted
        },
        headers=headers,
    )
    assert net30_resp.status_code in (200, 201), net30_resp.text
    assert net30_resp.json()["due_date"] == "2026-07-31"  # 30 days after 2026-07-01


def test_cf4c3_01_boundary_one_paise_zero_rated_and_unit_price_reconciliation():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}
    suffix = uuid.uuid4().hex[:6].upper()

    # 1. Reject one-paise discrepancies between supplied and calculated totals
    # Base: qty=1, price=1000.00, rate=0.18 -> excl=1000.00, tax=180.00, tot=1180.00
    # A) +0.01 paise discrepancy on total
    resp_plus_paise = client.post(
        "/api/orders",
        json={
            "fy_code": "FY 2026-27",
            "po_number": f"PO-PAISE-P-{suffix}",
            "po_date": "2026-06-01",
            "customer_id": 1,
            "total_amount": 1180.01,
            "items": [{"item_description": "Task", "quantity": 1.0, "unit_price": 1000.0, "tax_rate": 0.18}],
        },
        headers=headers,
    )
    assert resp_plus_paise.status_code == 400
    assert "inconsistent" in resp_plus_paise.json()["detail"].lower()

    # B) -0.01 paise discrepancy on total
    resp_minus_paise = client.post(
        "/api/orders",
        json={
            "fy_code": "FY 2026-27",
            "po_number": f"PO-PAISE-M-{suffix}",
            "po_date": "2026-06-01",
            "customer_id": 1,
            "total_amount": 1179.99,
            "items": [{"item_description": "Task", "quantity": 1.0, "unit_price": 1000.0, "tax_rate": 0.18}],
        },
        headers=headers,
    )
    assert resp_minus_paise.status_code == 400
    assert "inconsistent" in resp_minus_paise.json()["detail"].lower()

    # C) One-paise tax discrepancy on invoice
    resp_inv_paise = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "invoice_number": f"INV-PAISE-T-{suffix}",
            "invoice_date": "2026-06-01",
            "customer_id": 1,
            "exclusive_amount": 1000.00,
            "tax_amount": 180.01,  # Calculated is 180.00
        },
        headers=headers,
    )
    assert resp_inv_paise.status_code == 400
    assert "inconsistent" in resp_inv_paise.json()["detail"].lower()

    zero_inv_id = None
    try:
        # 2. Preserve valid zero-rated GST rather than defaulting to 18%
        # A) On Purchase Order
        resp_zero_po = client.post(
            "/api/orders",
            json={
                "fy_code": "FY 2026-27",
                "po_number": f"PO-ZERO-{suffix}",
                "po_date": "2026-06-01",
                "customer_id": 1,
                "exclusive_amount": 50000.00,
                "tax_rate": 0.0,
            },
            headers=headers,
        )
        assert resp_zero_po.status_code == 201
        zero_po_data = resp_zero_po.json()
        assert zero_po_data["tax_rate"] == "0.0000"
        assert zero_po_data["tax_amount"] == "0.00"
        assert zero_po_data["exclusive_amount"] == "50000.00"
        assert zero_po_data["total_amount"] == "50000.00"

        # B) On Direct Commercial Invoice
        resp_zero_inv = client.post(
            "/api/invoices",
            json={
                "fy_code": "FY 2026-27",
                "invoice_number": f"INV-ZERO-{suffix}",
                "invoice_date": "2026-06-01",
                "customer_id": 1,
                "exclusive_amount": 75000.00,
                "tax_rate": 0.0,
            },
            headers=headers,
        )
        assert resp_zero_inv.status_code == 201
        zero_inv_id = resp_zero_inv.json()["id"]
        zero_inv_get = client.get(f"/api/invoices/{zero_inv_id}", headers=headers).json()["invoice"]
        assert zero_inv_get["tax_rate"] == "0.0000"
        assert zero_inv_get["tax_amount"] == "0.00"
        assert zero_inv_get["exclusive_amount"] == "75000.00"
        assert zero_inv_get["total_amount"] == "75000.00"
        assert zero_inv_get["cgst_amount"] == "0.00"
        assert zero_inv_get["sgst_amount"] == "0.00"
        assert zero_inv_get["igst_amount"] == "0.00"

        # 3. Unit price excess precision normalization and exact line-item total reconciliation
        # Supply unit_price with excess precision (e.g. 100.555 -> normalizes to 100.56)
        # Quantity 10 * 100.56 = 1005.60
        resp_excess_prec = client.post(
            "/api/orders",
            json={
                "fy_code": "FY 2026-27",
                "po_number": f"PO-PREC-{suffix}",
                "po_date": "2026-06-01",
                "customer_id": 1,
                "items": [
                    {
                        "item_description": "Precision Item",
                        "quantity": 10.0,
                        "unit_price": 100.555,  # Normalizes to 100.56
                        "tax_rate": 0.18,
                    }
                ],
            },
            headers=headers,
        )
        assert resp_excess_prec.status_code == 201
        prec_po_data = resp_excess_prec.json()
        li = prec_po_data["line_items"][0]
        assert li["unit_price"] == "100.56"
        assert li["exclusive_amount"] == "1005.60"
        # Reconciles exactly: 10 * 100.56 == 1005.60
        assert Decimal(li["quantity"]) * Decimal(li["unit_price"]) == Decimal(li["exclusive_amount"])
        assert li["tax_amount"] == "181.01"  # 1005.60 * 0.18 = 181.008 -> 181.01
        assert li["total_amount"] == "1186.61"
    finally:
        with get_db_session() as db:
            if zero_inv_id:
                for it in db.scalars(select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == zero_inv_id)).all():
                    db.delete(it)
                inv_row = db.scalar(select(CommercialInvoice).where(CommercialInvoice.id == zero_inv_id))
                if inv_row:
                    db.delete(inv_row)
            for p_num in [f"PO-ZERO-{suffix}", f"PO-PREC-{suffix}"]:
                p_row = db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == p_num))
                if p_row:
                    for it in db.scalars(select(PurchaseOrderItem).where(PurchaseOrderItem.po_id == p_row.id)).all():
                        db.delete(it)
                    db.delete(p_row)
            db.commit()


def test_historical_tax_frozen_when_tax_master_updated():
    """
    AP-01 Final Acceptance Closure: Historical Tax Integrity Regression.
    Proves that when a commercial invoice is issued with an active tax master rate (e.g. 18%),
    subsequent modifications or rate changes to the GstRateMaster table in PostgreSQL
    do NOT alter the issued invoice's historical tax rate, tax amount, or total amount.
    """
    token = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token}"}
    suffix = uuid.uuid4().hex[:8]
    pan_digits = f"{int(suffix[:4], 16) % 9000 + 1000}"

    # 1. Create dedicated customer
    cust_resp = client.post(
        "/api/customers",
        json={
            "billing_name": f"Tax Freeze Corp {suffix}",
            "gst_number": f"29FRZTX{pan_digits}A1Z5",
            "billing_address": "Indiranagar 100ft Rd, Bangalore",
            "city": "Bangalore",
            "state": "Karnataka",
            "contact_person": "Tax Integrity Lead",
        },
        headers=headers,
    )
    assert cust_resp.status_code == 200, cust_resp.text
    cust_id = cust_resp.json()["id"]

    inv_id = None
    original_rate = None
    original_cgst = None
    original_sgst = None
    original_igst = None

    try:
        # 2. Issue Commercial Invoice with standard 18% tax
        # Exclusive: 40,000.00; Tax 18%: 7,200.00 (CGST 3,600.00 + SGST 3,600.00); Total: 47,200.00
        inv_resp = client.post(
            "/api/invoices",
            json={
                "fy_code": "FY 2026-27",
                "invoice_number": f"INV-TEST-TAXFREEZE-{suffix}",
                "invoice_date": "2026-08-10",
                "customer_id": cust_id,
                "items": [
                    {
                        "desc": "Enterprise Cloud Server Unit",
                        "quantity": 2.0,
                        "unit_price": 20000.0,
                        "tax_rate": 0.18,
                    }
                ],
            },
            headers=headers,
        )
        assert inv_resp.status_code in (200, 201), inv_resp.text
        inv_data = inv_resp.json()
        inv_id = inv_data["id"]

        # 3. Verify initial issued invoice figures via GET API
        inv_initial = client.get(f"/api/invoices/{inv_id}", headers=headers).json()["invoice"]
        assert Decimal(str(inv_initial["tax_rate"])) == Decimal("0.18")
        assert Decimal(str(inv_initial["exclusive_amount"])) == Decimal("40000.00")
        assert Decimal(str(inv_initial["tax_amount"])) == Decimal("7200.00")
        assert Decimal(str(inv_initial["total_amount"])) == Decimal("47200.00")
        assert Decimal(str(inv_initial["cgst_amount"])) == Decimal("3600.00")
        assert Decimal(str(inv_initial["sgst_amount"])) == Decimal("3600.00")

        # 4. Modify the applicable tax master in PostgreSQL
        # Mutate the 18% GST master rate (id=1) to 28% (0.2800)
        with get_db_session() as db:
            gst_master = db.scalar(select(GstRateMaster).where(GstRateMaster.id == 1))
            assert gst_master is not None
            original_rate = gst_master.rate
            original_cgst = gst_master.cgst_rate
            original_sgst = gst_master.sgst_rate
            original_igst = gst_master.igst_rate

            gst_master.rate = Decimal("0.2800")
            gst_master.cgst_rate = Decimal("0.1400")
            gst_master.sgst_rate = Decimal("0.1400")
            gst_master.igst_rate = Decimal("0.2800")
            db.commit()

        # 5. Confirm that tax master now reflects the updated 28% rate in settings API
        tax_settings_resp = client.get("/api/settings/taxes", headers=headers)
        assert tax_settings_resp.status_code == 200
        gst_rates = tax_settings_resp.json()["gst_rates"]
        master_id_1 = next(r for r in gst_rates if r["id"] == 1)
        assert Decimal(str(master_id_1["rate"])) == Decimal("0.28")

        # 6. Re-query the previously issued invoice via API — MUST RETAIN HISTORICAL TAX
        inv_recheck = client.get(f"/api/invoices/{inv_id}", headers=headers).json()["invoice"]
        assert Decimal(str(inv_recheck["tax_rate"])) == Decimal("0.18")
        assert Decimal(str(inv_recheck["exclusive_amount"])) == Decimal("40000.00")
        assert Decimal(str(inv_recheck["tax_amount"])) == Decimal("7200.00")
        assert Decimal(str(inv_recheck["total_amount"])) == Decimal("47200.00")
        assert Decimal(str(inv_recheck["cgst_amount"])) == Decimal("3600.00")
        assert Decimal(str(inv_recheck["sgst_amount"])) == Decimal("3600.00")
        # Ensure it was NOT dynamically recomputed using the new 28% rate (which would yield 11,200.00 / 51,200.00)
        assert Decimal(str(inv_recheck["tax_amount"])) != Decimal("11200.00")
        assert Decimal(str(inv_recheck["total_amount"])) != Decimal("51200.00")

        # 7. Query directly from PostgreSQL database — MUST BE FROZEN IN IMMUTABLE COLUMNS
        with get_db_session() as db:
            db_inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.id == inv_id))
            assert db_inv is not None
            assert db_inv.tax_rate == Decimal("0.1800")
            assert db_inv.exclusive_amount == Decimal("40000.00")
            assert db_inv.tax_amount == Decimal("7200.00")
            assert db_inv.total_amount == Decimal("47200.00")
            assert db_inv.cgst_amount == Decimal("3600.00")
            assert db_inv.sgst_amount == Decimal("3600.00")

            # Check line items as well
            db_items = db.scalars(select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == inv_id)).all()
            assert len(db_items) == 1
            assert db_items[0].tax_rate == Decimal("0.1800")
            assert db_items[0].exclusive_amount == Decimal("40000.00")
            assert db_items[0].tax_amount == Decimal("7200.00")
            assert db_items[0].total_amount == Decimal("47200.00")

    finally:
        # 8. Restore the GstRateMaster row to its original rate to guarantee test isolation
        with get_db_session() as db:
            if original_rate is not None:
                gst_master = db.scalar(select(GstRateMaster).where(GstRateMaster.id == 1))
                if gst_master:
                    gst_master.rate = original_rate
                    gst_master.cgst_rate = original_cgst
                    gst_master.sgst_rate = original_sgst
                    gst_master.igst_rate = original_igst
            if inv_id:
                for it in db.scalars(select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == inv_id)).all():
                    db.delete(it)
                inv_row = db.scalar(select(CommercialInvoice).where(CommercialInvoice.id == inv_id))
                if inv_row:
                    db.delete(inv_row)
            cust_row = db.scalar(select(Customer).where(Customer.id == cust_id))
            if cust_row:
                db.delete(cust_row)
            db.commit()



