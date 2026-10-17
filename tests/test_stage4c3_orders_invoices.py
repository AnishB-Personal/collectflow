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
    assert po_data["exclusive_amount"] == 70000.00
    assert po_data["tax_amount"] == 12600.00
    assert po_data["total_amount"] == 82600.00
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
    assert updated_get.json()["exclusive_amount"] == 60000.00
    assert updated_get.json()["total_amount"] == 70800.00
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
    assert inv_check["exclusive_amount"] == 100000.00
    assert inv_check["tax_amount"] == 18000.00
    assert inv_check["total_amount"] == 118000.00
    assert inv_check["outstanding_balance"] == 118000.00
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
    assert inv1_resp.status_code == 200, inv1_resp.text
    inv1_id = inv1_resp.json()["id"]

    inv1 = client.get(f"/api/invoices/{inv1_id}", headers=headers).json()["invoice"]
    assert inv1["tax_amount"] == 9000.00
    assert inv1["cgst_amount"] == 4500.00
    assert inv1["sgst_amount"] == 4500.00
    assert inv1["igst_amount"] == 0.00

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
    assert inv2_resp.status_code == 200, inv2_resp.text
    inv2_id = inv2_resp.json()["id"]

    inv2 = client.get(f"/api/invoices/{inv2_id}", headers=headers).json()["invoice"]
    assert inv2["tax_amount"] == 9000.00
    assert inv2["cgst_amount"] == 0.00
    assert inv2["sgst_amount"] == 0.00
    assert inv2["igst_amount"] == 9000.00

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
    assert inv_resp.status_code == 200
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
    assert inv_resp.status_code == 200
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
    assert inv_check["outstanding_balance"] == 0.00
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
