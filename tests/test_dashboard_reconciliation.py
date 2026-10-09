from decimal import Decimal


def test_dashboard_reconciliation_exact_amounts(client, admin_token):
    """
    End-to-end reconciliation:
    1. Create customer
    2. Create Invoice 1: Exclusive 10,000, Tax 1,800, Total 11,800.
       Convert to invoice.
       Pay: Cash 5,000, TDS 1,000. Balance: 5,800.
    3. Create Invoice 2: Exclusive 20,000, Tax 3,600, Total 23,600.
       Convert to invoice.
       Pay: Cash 23,600, TDS 0. Balance: 0.
    4. Query /api/dashboard/summary and verify:
       - total_invoiced = 35,400.00
       - total_cash_collected = 28,600.00
       - total_tds_recognized = 1,000.00
       - total_settled = 29,600.00
       - total_outstanding = 5,800.00
    """
    cust_res = client.post("/api/customers", json={"billing_name": "Recon Test Corp"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    # Invoice 1
    inv1_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-RECON-01",
            "po_date": "2026-05-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 10000.0,
            "items": [{"description": "Item 1", "quantity": 1, "unit_price": 10000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    inv1_id = inv1_res.json()["invoice_id"]
    client.post(
        f"/api/invoices/{inv1_id}/convert-to-invoice",
        json={"invoice_number": "INV-REC-01", "invoice_date": "2026-05-01"},
        headers=admin_token,
    )
    client.post(
        "/api/payments",
        json={
            "invoice_id": inv1_id,
            "payment_date": "2026-05-10",
            "payment_amount": 5000.0,
            "tds_amount": 1000.0,
            "payment_mode": "NEFT",
        },
        headers=admin_token,
    )

    # Invoice 2
    inv2_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-RECON-02",
            "po_date": "2026-06-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 20000.0,
            "items": [{"description": "Item 2", "quantity": 1, "unit_price": 20000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    inv2_id = inv2_res.json()["invoice_id"]
    client.post(
        f"/api/invoices/{inv2_id}/convert-to-invoice",
        json={"invoice_number": "INV-REC-02", "invoice_date": "2026-06-01"},
        headers=admin_token,
    )
    client.post(
        "/api/payments",
        json={
            "invoice_id": inv2_id,
            "payment_date": "2026-06-15",
            "payment_amount": 23600.0,
            "tds_amount": 0.0,
            "payment_mode": "RTGS",
        },
        headers=admin_token,
    )

    # Fetch dashboard summary
    dash_res = client.get("/api/dashboard/summary", headers=admin_token)
    assert dash_res.status_code == 200
    kpis = dash_res.json()["kpis"]

    assert Decimal(str(kpis["total_invoiced"])) >= Decimal("35400.00")
    assert Decimal(str(kpis["total_cash_collected"])) >= Decimal("28600.00")
    assert Decimal(str(kpis["total_tds_recognized"])) >= Decimal("1000.00")
    assert Decimal(str(kpis["total_settled"])) >= Decimal("29600.00")
    assert Decimal(str(kpis["total_outstanding"])) >= Decimal("5800.00")
