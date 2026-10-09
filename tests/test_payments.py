from app.database import query_one


def setup_test_invoice(client, test_users):
    sales_headers = test_users["sales"]["headers"]
    c_resp = client.post("/api/customers", json={"billing_name": "Test Customer Ltd"}, headers=sales_headers)
    cid = c_resp.json()["customer_id"]

    inv_resp = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-PAY-001",
            "invoice_date": "2026-04-01",
            "exclusive_amount": 10000.0,
            "tax_rate": 0.18,  # Total = 11800.0
        },
        headers=sales_headers,
    )
    return inv_resp.json()["invoice_id"]


def test_payment_rbac(client, test_users):
    inv_id = setup_test_invoice(client, test_users)

    payload = {"invoice_id": inv_id, "payment_amount": 5000.0}
    # Viewer forbidden
    r_viewer = client.post("/api/payments", json=payload, headers=test_users["viewer"]["headers"])
    assert r_viewer.status_code == 403

    # Sales forbidden
    r_sales = client.post("/api/payments", json=payload, headers=test_users["sales"]["headers"])
    assert r_sales.status_code == 403

    # Finance succeeds
    r_finance = client.post("/api/payments", json=payload, headers=test_users["finance"]["headers"])
    assert r_finance.status_code == 200


def test_partial_and_full_payment_with_tds(client, test_users):
    inv_id = setup_test_invoice(client, test_users)
    fin_headers = test_users["finance"]["headers"]

    # Initial state
    inv0 = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv0["total_amount"] == 11800.0
    assert inv0["outstanding_balance"] == 11800.0
    assert inv0["computed_status"] in ("PENDING", "OVERDUE")

    # 1. Partial payment: Cash 5000 + TDS 200 (Total settlement = 5200)
    r1 = client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_amount": 5000.0,
            "tds_amount": 200.0,
            "tds_section": "194C",
            "payment_mode": "NEFT",
        },
        headers=fin_headers,
    )
    assert r1.status_code == 200

    inv1 = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv1["total_cash_paid"] == 5000.0
    assert inv1["total_tds_deducted"] == 200.0
    assert inv1["total_paid"] == 5200.0
    assert inv1["outstanding_balance"] == 6600.0
    assert inv1["computed_status"] == "PARTIAL"

    # 2. Final settlement: Cash 6600
    r2 = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": 6600.0, "tds_amount": 0.0, "payment_mode": "RTGS"},
        headers=fin_headers,
    )
    assert r2.status_code == 200

    inv2 = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv2["total_paid"] == 11800.0
    assert inv2["outstanding_balance"] == 0.0
    assert inv2["computed_status"] == "PAID"


def test_overpayment_prevention(client, test_users):
    inv_id = setup_test_invoice(client, test_users)
    fin_headers = test_users["finance"]["headers"]

    # Total invoice is 11800. Attempting to record 15000 must be rejected
    r = client.post(
        "/api/payments", json={"invoice_id": inv_id, "payment_amount": 15000.0, "tds_amount": 0.0}, headers=fin_headers
    )
    assert r.status_code == 400
    assert "exceeds the outstanding balance" in r.json()["detail"]


def test_payment_deletion_and_balance_restoration(client, test_users):
    inv_id = setup_test_invoice(client, test_users)
    fin_headers = test_users["finance"]["headers"]

    # Record payment
    r = client.post(
        "/api/payments", json={"invoice_id": inv_id, "payment_amount": 5000.0, "tds_amount": 0.0}, headers=fin_headers
    )
    pay_id = r.json()["payment_id"]

    inv_mid = query_one("SELECT outstanding_balance FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv_mid["outstanding_balance"] == 6800.0

    # Delete payment
    r_del = client.delete(f"/api/payments/{pay_id}", headers=fin_headers)
    assert r_del.status_code == 200

    inv_after = query_one("SELECT outstanding_balance, computed_status FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv_after["outstanding_balance"] == 11800.0
    assert inv_after["computed_status"] != "PARTIAL"
