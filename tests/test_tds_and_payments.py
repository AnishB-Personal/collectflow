from decimal import Decimal

from app.database import query_one


def test_idempotent_payment_replay(client, admin_token):
    """
    Test payment idempotency: submitting the same payment twice with an Idempotency-Key
    must return the existing payment (HTTP 200) with idempotent_replay=True and not duplicate the record.
    """
    cust_res = client.post("/api/customers", json={"billing_name": "Idempotent Cust Ltd"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    inv_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-IDEMP-01",
            "po_date": "2026-05-15",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 5000.0,
            "items": [{"description": "Item", "quantity": 1, "unit_price": 5000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    inv_id = inv_res.json()["invoice_id"]

    # Convert to invoice
    client.post(
        f"/api/invoices/{inv_id}/convert-to-invoice",
        json={"invoice_number": "INV-IDEMP-001", "invoice_date": "2026-05-15"},
        headers=admin_token,
    )

    # 1. First payment attempt with idempotency key
    key = "idem-key-unique-xyz-123"
    pay_payload = {
        "invoice_id": inv_id,
        "payment_date": "2026-05-20",
        "payment_amount": 2000.0,
        "tds_amount": 200.0,
        "payment_mode": "NEFT",
        "remittance_number": "REF12345",
        "idempotency_key": key,
    }
    res1 = client.post("/api/payments", json=pay_payload, headers=admin_token)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["success"] is True
    assert data1.get("idempotent_replay") is None or data1.get("idempotent_replay") is False
    pay_id = data1["payment_id"]

    # 2. Duplicate payment attempt with the identical idempotency key
    res2 = client.post("/api/payments", json=pay_payload, headers=admin_token)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["payment_id"] == pay_id
    assert data2.get("idempotent_replay") is True

    # 3. Verify in invoice details that only ONE payment exists
    inv_detail = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert Decimal(str(inv_detail["total_cash_paid"])) == Decimal("2000.00")
    assert Decimal(str(inv_detail["total_tds_deducted"])) == Decimal("200.00")
    assert Decimal(str(inv_detail["total_paid"])) == Decimal("2200.00")


def test_payment_rejected_against_cancelled_invoice(client, admin_token):
    """Ensure payments are strictly rejected against CANCELLED invoices."""
    cust_res = client.post("/api/customers", json={"billing_name": "Cancel Cust Ltd"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    inv_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-CANCEL-PAY",
            "po_date": "2026-05-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 1000.0,
            "items": [{"description": "Item", "quantity": 1, "unit_price": 1000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    inv_id = inv_res.json()["invoice_id"]

    # Convert then cancel
    client.post(
        f"/api/invoices/{inv_id}/convert-to-invoice",
        json={"invoice_number": "INV-CAN-01", "invoice_date": "2026-05-01"},
        headers=admin_token,
    )
    cancel_res = client.post(
        f"/api/invoices/{inv_id}/cancel", json={"reason": "Customer cancelled order"}, headers=admin_token
    )
    assert cancel_res.status_code == 200

    # Attempt to post payment
    pay_res = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_date": "2026-05-05", "payment_amount": 500.0, "payment_mode": "NEFT"},
        headers=admin_token,
    )
    assert pay_res.status_code == 400
    assert "cancelled" in pay_res.json()["detail"].lower()


def test_cash_vs_tds_vs_total_settled_metrics(client, admin_token):
    """Verify clean separation of Cash Received, TDS Recognized, and Total Settled."""
    cust_res = client.post("/api/customers", json={"billing_name": "Accounting Metrics Ltd"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    # ₹10,000 + 18% GST = ₹11,800
    inv_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-ACC-METRICS-01",
            "po_date": "2026-06-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 10000.0,
            "items": [{"description": "Consulting", "quantity": 1, "unit_price": 10000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    inv_id = inv_res.json()["invoice_id"]

    client.post(
        f"/api/invoices/{inv_id}/convert-to-invoice",
        json={"invoice_number": "INV-ACC-001", "invoice_date": "2026-06-01"},
        headers=admin_token,
    )

    # 1. First payment: ₹5000 Cash + ₹1000 TDS
    client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_date": "2026-06-10",
            "payment_amount": 5000.0,
            "tds_amount": 1000.0,
            "payment_mode": "NEFT",
        },
        headers=admin_token,
    )

    # 2. Second payment: ₹5800 Cash + ₹0 TDS (full settlement)
    client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_date": "2026-06-20",
            "payment_amount": 5800.0,
            "tds_amount": 0.0,
            "payment_mode": "RTGS",
        },
        headers=admin_token,
    )

    inv = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert Decimal(str(inv["total_cash_paid"])) == Decimal("10800.00")
    assert Decimal(str(inv["total_tds_deducted"])) == Decimal("1000.00")
    assert Decimal(str(inv["total_paid"])) == Decimal("11800.00")
    assert Decimal(str(inv["outstanding_balance"])) == Decimal("0.00")
    assert inv["computed_status"] == "PAID"
