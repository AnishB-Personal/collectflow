import concurrent.futures
from decimal import Decimal

from app.database import query_one


def test_concurrent_po_conversion_twice(client, admin_token):
    """Attempting to convert the same PO to commercial invoice twice simultaneously."""
    cust_res = client.post("/api/customers", json={"billing_name": "Concurrent Convert Ltd"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    inv_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-CONC-CONV-01",
            "po_date": "2026-05-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 1000.0,
            "items": [{"description": "Item", "quantity": 1, "unit_price": 1000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    inv_id = inv_res.json()["invoice_id"]

    def try_convert(num):
        return client.post(
            f"/api/invoices/{inv_id}/convert-to-invoice",
            json={"invoice_number": f"INV-CONC-{num}", "invoice_date": "2026-05-01"},
            headers=admin_token,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futs = [executor.submit(try_convert, i) for i in range(1, 5)]
        results = [f.result() for f in futs]

    successes = [r for r in results if r.status_code == 200]
    failures = [r for r in results if r.status_code == 400]

    # Exactly one conversion must succeed; all others must fail with 400 (already converted)
    assert len(successes) == 1
    assert len(failures) == 3


def test_concurrent_payment_on_fully_paid_invoice(client, admin_token):
    """
    Ensure that once an invoice reaches PAID status, concurrent attempts to pay
    against it are strictly rejected with 400 and cannot create negative balances.
    """
    cust_res = client.post("/api/customers", json={"billing_name": "Full Paid Conc Ltd"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    inv_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-PAID-CONC-01",
            "po_date": "2026-05-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 500.0,
            "items": [{"description": "Item", "quantity": 1, "unit_price": 500.0, "tax_rate": 0.0}],
        },
        headers=admin_token,
    )
    inv_id = inv_res.json()["invoice_id"]

    # Convert to invoice (Total: ₹500.00)
    client.post(
        f"/api/invoices/{inv_id}/convert-to-invoice",
        json={"invoice_number": "INV-P-01", "invoice_date": "2026-05-01"},
        headers=admin_token,
    )

    # 5 concurrent payments of ₹500
    def post_pay(ref):
        return client.post(
            "/api/payments",
            json={
                "invoice_id": inv_id,
                "payment_date": "2026-05-02",
                "payment_amount": 500.0,
                "payment_mode": "NEFT",
                "remittance_number": f"REF-{ref}",
            },
            headers=admin_token,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futs = [executor.submit(post_pay, i) for i in range(1, 6)]
        results = [f.result() for f in futs]

    successes = [r for r in results if r.status_code == 200]
    failures = [r for r in results if r.status_code == 400]

    # Exactly one payment of ₹500 can succeed to pay the invoice; the rest must fail (overpayment)
    assert len(successes) == 1
    assert len(failures) == 4

    inv = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert Decimal(str(inv["outstanding_balance"])) == Decimal("0.00")
    assert inv["computed_status"] == "PAID"
