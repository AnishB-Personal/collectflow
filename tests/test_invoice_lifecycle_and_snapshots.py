from decimal import Decimal


def test_customer_snapshot_preservation_on_customer_update(client, admin_token):
    """
    Legal Immutability: When customer legal details (name, GSTIN, address) are updated
    in the customer master, previously issued invoices must retain their original
    frozen snapshot values.
    """
    # 1. Create initial customer
    cust_res = client.post(
        "/api/customers",
        json={
            "billing_name": "Original Legal Corp",
            "gst_number": "27AAAAA1111A1Z1",
            "billing_address": "123 Nariman Point, Mumbai",
            "state": "Maharashtra",
        },
        headers=admin_token,
    )
    assert cust_res.status_code == 200
    cust_id = cust_res.json()["customer_id"]

    # 2. Create invoice
    inv_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-SNAP-01",
            "po_date": "2026-05-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 50000.0,
            "items": [{"description": "Audit Services", "quantity": 1, "unit_price": 50000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    assert inv_res.status_code == 200
    inv_id = inv_res.json()["invoice_id"]

    # 3. Convert to commercial invoice
    client.post(
        f"/api/invoices/{inv_id}/convert-to-invoice",
        json={"invoice_number": "INV-SNAP-001", "invoice_date": "2026-05-01"},
        headers=admin_token,
    )

    # 4. Now modify the customer in the master table
    update_res = client.put(
        f"/api/customers/{cust_id}",
        json={
            "billing_name": "Renamed Entity Private Limited",
            "gst_number": "27ZZZZZ9999Z1Z9",
            "billing_address": "999 BKC Bandra, Mumbai",
            "state": "Maharashtra",
        },
        headers=admin_token,
    )
    assert update_res.status_code == 200

    # 5. Verify the invoice still shows the original frozen snapshot
    inv_detail = client.get(f"/api/invoices/{inv_id}", headers=admin_token).json()
    inv = inv_detail["invoice"]
    assert inv["customer_name"] == "Original Legal Corp"
    assert inv["customer_gstin"] == "27AAAAA1111A1Z1"
    assert inv["customer_address"] == "123 Nariman Point, Mumbai"

    # Also verify from the invoice list (which uses v_invoice_summary)
    list_res = client.get("/api/invoices?search=INV-SNAP-001", headers=admin_token).json()
    matching = [i for i in list_res["data"] if i["id"] == inv_id]
    assert len(matching) == 1
    assert matching[0]["customer_name"] == "Original Legal Corp"


def test_invoice_cancellation_workflow(client, admin_token):
    """
    Test explicit invoice cancellation workflow:
    - POST /api/invoices/{id}/cancel transitions invoice to CANCELLED
    - outstanding_balance becomes 0.00
    - days_overdue becomes 0
    - reason is saved
    - cannot be cancelled twice
    """
    cust_res = client.post("/api/customers", json={"billing_name": "Cancel Test Ltd"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    inv_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-CANCEL-WORKFLOW",
            "po_date": "2026-05-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 1000.0,
            "items": [{"description": "Item", "quantity": 1, "unit_price": 1000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    inv_id = inv_res.json()["invoice_id"]

    client.post(
        f"/api/invoices/{inv_id}/convert-to-invoice",
        json={"invoice_number": "INV-CAN-WORKFLOW", "invoice_date": "2026-05-01"},
        headers=admin_token,
    )

    # Cancel the invoice
    can_res = client.post(
        f"/api/invoices/{inv_id}/cancel", json={"reason": "Customer cancelled project scope"}, headers=admin_token
    )
    assert can_res.status_code == 200

    inv_detail = client.get(f"/api/invoices/{inv_id}", headers=admin_token).json()
    inv = inv_detail["invoice"]
    assert inv["computed_status"] == "CANCELLED"
    assert Decimal(str(inv["outstanding_balance"])) == Decimal("0.00")
    assert inv["days_overdue"] == 0

    # Attempt to cancel again should fail
    can_res_again = client.post(
        f"/api/invoices/{inv_id}/cancel", json={"reason": "Duplicate cancel"}, headers=admin_token
    )
    assert can_res_again.status_code == 400


def test_delete_protection_for_issued_or_paid_invoices(client, admin_token):
    """
    Test delete protection:
    1. Unbilled PO can be deleted.
    2. Issued commercial invoice CANNOT be physically deleted (must use cancel).
    3. Invoice with recorded payments CANNOT be deleted.
    """
    cust_res = client.post("/api/customers", json={"billing_name": "Delete Protection Ltd"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    # 1. Unbilled PO with no payments -> Deletion is allowed
    po_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-DEL-ALLOWED",
            "po_date": "2026-05-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 500.0,
            "items": [{"description": "Draft", "quantity": 1, "unit_price": 500.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    po_id = po_res.json()["invoice_id"]
    del_res = client.delete(f"/api/invoices/{po_id}", headers=admin_token)
    assert del_res.status_code == 200

    # 2. Issued invoice -> Deletion is rejected
    po_issued = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-DEL-REJECTED",
            "po_date": "2026-05-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 500.0,
            "items": [{"description": "Item", "quantity": 1, "unit_price": 500.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    issued_id = po_issued.json()["invoice_id"]
    client.post(
        f"/api/invoices/{issued_id}/convert-to-invoice",
        json={"invoice_number": "INV-DEL-REJ-01", "invoice_date": "2026-05-01"},
        headers=admin_token,
    )

    del_issued_res = client.delete(f"/api/invoices/{issued_id}", headers=admin_token)
    assert del_issued_res.status_code == 400
    assert "issued commercial invoices cannot be permanently deleted" in del_issued_res.json()["detail"].lower()


def test_fiscal_year_master_date_boundary_enforcement(client, admin_token):
    """
    Ensure invoice creation or conversion validates that po_date/invoice_date
    falls strictly within the start_date and end_date of the selected FY master.
    FY 2026-27 master: 2026-04-01 to 2027-03-31.
    """
    cust_res = client.post("/api/customers", json={"billing_name": "FY Boundary Ltd"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    # Date before FY start (2026-03-31 is FY 2025-26, not FY 2026-27)
    invalid_po = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-OUTSIDE-FY-01",
            "po_date": "2026-03-31",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 1000.0,
            "items": [{"description": "Item", "quantity": 1, "unit_price": 1000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    assert invalid_po.status_code == 400
    assert "falls outside" in invalid_po.json()["detail"].lower()

    # Valid date within FY
    valid_po = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-INSIDE-FY-01",
            "po_date": "2026-04-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 1000.0,
            "items": [{"description": "Item", "quantity": 1, "unit_price": 1000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    assert valid_po.status_code == 200


def test_invoice_cancellation_with_settlement_history(client, admin_token):
    """
    Financial Audit Safety: Ensure cancellation is strictly blocked if an invoice contains:
    - Partial cash payment
    - TDS deduction
    - Full payment
    Cancellation is only permitted after all settlements are reversed/deleted.
    """
    cust_res = client.post("/api/customers", json={"billing_name": "Settle History Ltd"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    # Create invoice (Exclusive: 10,000, 18% GST -> Total: 11,800)
    inv_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-SETTLE-CANCEL-01",
            "po_date": "2026-05-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 10000.0,
            "items": [{"description": "Item", "quantity": 1, "unit_price": 10000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    inv_id = inv_res.json()["invoice_id"]
    client.post(
        f"/api/invoices/{inv_id}/convert-to-invoice",
        json={"invoice_number": "INV-SETTLE-001", "invoice_date": "2026-05-01"},
        headers=admin_token,
    )

    # 1. Post partial cash payment (₹2,000)
    p1 = client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_date": "2026-05-10",
            "payment_amount": 2000.0,
            "payment_mode": "NEFT",
        },
        headers=admin_token,
    )
    assert p1.status_code == 200
    p1_id = p1.json()["payment_id"]

    # Attempt to cancel with partial cash payment -> MUST FAIL (400)
    c1 = client.post(f"/api/invoices/{inv_id}/cancel", json={"reason": "Cancel attempt"}, headers=admin_token)
    assert c1.status_code == 400
    assert "cannot cancel invoice with existing payments" in c1.json()["detail"].lower()

    # 2. Post TDS payment (₹500 TDS, ₹0 cash)
    p2 = client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_date": "2026-05-11",
            "payment_amount": 0.0,
            "tds_amount": 500.0,
            "payment_mode": "NEFT",
        },
        headers=admin_token,
    )
    assert p2.status_code == 200
    p2_id = p2.json()["payment_id"]

    # Attempt to cancel with TDS payment -> MUST FAIL (400)
    c2 = client.post(f"/api/invoices/{inv_id}/cancel", json={"reason": "Cancel attempt"}, headers=admin_token)
    assert c2.status_code == 400
    assert "cannot cancel invoice with existing payments" in c2.json()["detail"].lower()

    # 3. Reverse/delete payments
    client.delete(f"/api/payments/{p1_id}", headers=admin_token)
    # Still has p2 (TDS)
    c3 = client.post(f"/api/invoices/{inv_id}/cancel", json={"reason": "Cancel attempt"}, headers=admin_token)
    assert c3.status_code == 400

    # Delete p2
    client.delete(f"/api/payments/{p2_id}", headers=admin_token)

    # 4. Now that all payments are reversed, cancellation succeeds!
    c4 = client.post(
        f"/api/invoices/{inv_id}/cancel", json={"reason": "Settlement reversed; client rescinded"}, headers=admin_token
    )
    assert c4.status_code == 200
    assert c4.json()["success"] is True

    # Verification: Invoice is CANCELLED, outstanding is 0.00
    inv = client.get(f"/api/invoices/{inv_id}", headers=admin_token).json()["invoice"]
    assert inv["computed_status"] == "CANCELLED"
    assert Decimal(str(inv["outstanding_balance"])) == Decimal("0.00")
