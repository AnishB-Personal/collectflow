import datetime
from concurrent.futures import ThreadPoolExecutor

from app.database import query_all, query_one
from app.utils.financial import (
    get_current_financial_year,
)


def create_customer(client, headers, name="Fin Test Customer"):
    r = client.post("/api/customers", json={"billing_name": name}, headers=headers)
    assert r.status_code == 200
    return r.json()["customer_id"]


def test_aging_bucket_boundaries_and_days_overdue(client, test_users):
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Aging Corp")
    today = datetime.date.today()

    # Matrix of (days_overdue, expected_bucket)
    test_cases = [
        (0, "Current / Not Due"),
        (1, "1 - 30 Days"),
        (30, "1 - 30 Days"),
        (31, "31 - 60 Days"),
        (60, "31 - 60 Days"),
        (61, "61 - 90 Days"),
        (90, "61 - 90 Days"),
        (91, "91 - 180 Days"),
        (180, "91 - 180 Days"),
        (181, "180+ Days"),
    ]

    for days_ago, expected_bucket in test_cases:
        due = (today - datetime.timedelta(days=days_ago)).isoformat()
        r = client.post(
            "/api/invoices",
            json={
                "fy_code": "FY 2026-27",
                "customer_id": cid,
                "invoice_number": f"INV-AGE-{days_ago}",
                "invoice_date": "2026-04-01",
                "due_date": due,
                "exclusive_amount": 1000.0,
                "tax_rate": 0.18,
            },
            headers=fin["headers"],
        )
        assert r.status_code == 200
        inv_id = r.json()["invoice_id"]

        row = query_one("SELECT days_overdue, computed_status FROM v_invoice_summary WHERE id = ?", (inv_id,))
        assert row["days_overdue"] == days_ago
        if days_ago > 0:
            assert row["computed_status"] == "OVERDUE"
        else:
            assert row["computed_status"] == "PENDING"

    # Verify dashboard aging aggregation reflects all buckets correctly
    r_dash = client.get("/api/dashboard/summary", headers=fin["headers"])
    assert r_dash.status_code == 200
    aging_list = r_dash.json()["aging"]
    buckets_found = {a["bucket"]: a["invoice_count"] for a in aging_list}

    assert "Current / Not Due" in buckets_found
    assert "1 - 30 Days" in buckets_found
    assert "31 - 60 Days" in buckets_found
    assert "61 - 90 Days" in buckets_found
    assert "91 - 180 Days" in buckets_found
    assert "180+ Days" in buckets_found


def test_tds_only_settlement(client, test_users):
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Govt Contractor Ltd")

    # Invoice Total: 10,000 + 18% = 11,800
    r_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-TDS-ONLY",
            "invoice_date": "2026-04-01",
            "exclusive_amount": 10000.0,
            "tax_rate": 0.18,
        },
        headers=fin["headers"],
    )
    inv_id = r_inv.json()["invoice_id"]

    # Client settles purely by full TDS certificate: Cash 0, TDS 11,800
    r_pay = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": 0.0, "tds_amount": 11800.0, "tds_section": "194J"},
        headers=fin["headers"],
    )
    assert r_pay.status_code == 200

    inv = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv["total_cash_paid"] == 0.0
    assert inv["total_tds_deducted"] == 11800.0
    assert inv["total_paid"] == 11800.0
    assert inv["outstanding_balance"] == 0.0
    assert inv["computed_status"] == "PAID"


def test_multiple_partial_payments_and_exact_balance_settlement(client, test_users):
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Phased Construction")

    # Total: 100,000 + 18% = 118,000
    r_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-PHASED",
            "invoice_date": "2026-04-01",
            "exclusive_amount": 100000.0,
            "tax_rate": 0.18,
        },
        headers=fin["headers"],
    )
    inv_id = r_inv.json()["invoice_id"]

    # Payment 1: 50,000 cash -> 68,000 left
    client.post("/api/payments", json={"invoice_id": inv_id, "payment_amount": 50000.0}, headers=fin["headers"])
    inv = query_one("SELECT outstanding_balance, computed_status FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv["outstanding_balance"] == 68000.0
    assert inv["computed_status"] == "PARTIAL"

    # Payment 2: 40,000 cash + 8,000 TDS -> 20,000 left
    client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": 40000.0, "tds_amount": 8000.0},
        headers=fin["headers"],
    )
    inv = query_one("SELECT outstanding_balance, computed_status FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv["outstanding_balance"] == 20000.0
    assert inv["computed_status"] == "PARTIAL"

    # Payment 3: Exactly equal to balance (20,000 cash) -> 0.00 left
    client.post("/api/payments", json={"invoice_id": inv_id, "payment_amount": 20000.0}, headers=fin["headers"])
    inv = query_one("SELECT outstanding_balance, computed_status FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv["outstanding_balance"] == 0.0
    assert inv["computed_status"] == "PAID"


def test_payment_validation_negatives_zero_and_duplicate(client, test_users):
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Validation Customer")

    r_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-VAL-01",
            "invoice_date": "2026-04-01",
            "exclusive_amount": 10000.0,
            "tax_rate": 0.18,
        },
        headers=fin["headers"],
    )
    inv_id = r_inv.json()["invoice_id"]

    # Negative payment rejected
    r_neg1 = client.post("/api/payments", json={"invoice_id": inv_id, "payment_amount": -500.0}, headers=fin["headers"])
    assert r_neg1.status_code in (400, 422)

    # Negative TDS rejected
    r_neg2 = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": 1000.0, "tds_amount": -100.0},
        headers=fin["headers"],
    )
    assert r_neg2.status_code in (400, 422)

    # Zero payment rejected
    r_zero = client.post(
        "/api/payments", json={"invoice_id": inv_id, "payment_amount": 0.0, "tds_amount": 0.0}, headers=fin["headers"]
    )
    assert r_zero.status_code == 400
    assert "greater than zero" in r_zero.json()["detail"]

    # Duplicate payment rejected by remittance number
    r_pay1 = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": 2000.0, "remittance_number": "UTR-123456"},
        headers=fin["headers"],
    )
    assert r_pay1.status_code == 200

    r_pay_dup = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": 2000.0, "remittance_number": "UTR-123456"},
        headers=fin["headers"],
    )
    assert r_pay_dup.status_code == 400
    assert "Duplicate payment" in r_pay_dup.json()["detail"]


def test_payment_deletion_after_paid_reverts_status(client, test_users):
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Reversal Test")

    r_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-REV-01",
            "invoice_date": "2026-04-01",
            "exclusive_amount": 5000.0,
            "tax_rate": 0.18,  # 5900 total
        },
        headers=fin["headers"],
    )
    inv_id = r_inv.json()["invoice_id"]

    # Pay in full
    r_pay = client.post("/api/payments", json={"invoice_id": inv_id, "payment_amount": 5900.0}, headers=fin["headers"])
    pay_id = r_pay.json()["payment_id"]

    inv = query_one("SELECT computed_status FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv["computed_status"] == "PAID"

    # Delete payment
    r_del = client.delete(f"/api/payments/{pay_id}", headers=fin["headers"])
    assert r_del.status_code == 200

    inv_after = query_one("SELECT outstanding_balance, computed_status FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv_after["outstanding_balance"] == 5900.0
    assert inv_after["computed_status"] != "PAID"


def test_one_paise_fractional_edge_case(client, test_users):
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "One Paise Corp")

    # Line item of 0.01
    r_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-PAISE-01",
            "invoice_date": "2026-04-01",
            "exclusive_amount": 0.01,
            "tax_rate": 0.18,  # Tax on 0.01 is 0.00, total = 0.01
        },
        headers=fin["headers"],
    )
    assert r_inv.status_code == 200
    inv_id = r_inv.json()["invoice_id"]

    inv = query_one("SELECT total_amount, outstanding_balance FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv["total_amount"] == 0.01
    assert inv["outstanding_balance"] == 0.01

    # Pay exact 0.01
    r_pay = client.post("/api/payments", json={"invoice_id": inv_id, "payment_amount": 0.01}, headers=fin["headers"])
    assert r_pay.status_code == 200

    inv_done = query_one("SELECT outstanding_balance, computed_status FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv_done["outstanding_balance"] == 0.0
    assert inv_done["computed_status"] == "PAID"


def test_exchange_rate_validation(client, test_users):
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "FX Customer")

    # Zero exchange rate rejected
    r_zero = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "currency": "USD",
            "exchange_rate": 0.0,
            "exclusive_amount": 100.0,
        },
        headers=fin["headers"],
    )
    assert r_zero.status_code in (400, 422)

    # Negative exchange rate rejected
    r_neg = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "currency": "USD",
            "exchange_rate": -83.5,
            "exclusive_amount": 100.0,
        },
        headers=fin["headers"],
    )
    assert r_neg.status_code in (400, 422)


def test_duplicate_invoice_and_po_numbers_rejected(client, test_users):
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Dup Test Customer")

    # First invoice
    r1 = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "po_number": "PO-UNIQUE-999",
            "invoice_number": "INV-UNIQUE-999",
            "invoice_date": "2026-04-01",
            "exclusive_amount": 1000.0,
        },
        headers=fin["headers"],
    )
    assert r1.status_code == 200

    # Duplicate PO rejected
    r_dup_po = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "customer_id": cid, "po_number": "PO-UNIQUE-999", "exclusive_amount": 500.0},
        headers=fin["headers"],
    )
    assert r_dup_po.status_code == 400
    assert "already registered" in r_dup_po.json()["detail"]

    # Duplicate Invoice rejected
    r_dup_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-UNIQUE-999",
            "invoice_date": "2026-04-02",
            "exclusive_amount": 500.0,
        },
        headers=fin["headers"],
    )
    assert r_dup_inv.status_code == 400
    assert "already assigned" in r_dup_inv.json()["detail"]


def test_march_31_and_april_1_fiscal_year_boundaries():
    # Last second of FY 2025-26
    d_mar31 = datetime.date(2026, 3, 31)
    assert get_current_financial_year(d_mar31) == "FY 2025-26"

    # First day of FY 2026-27
    d_apr1 = datetime.date(2026, 4, 1)
    assert get_current_financial_year(d_apr1) == "FY 2026-27"

    # Last day of FY 2026-27
    d_mar31_next = datetime.date(2027, 3, 31)
    assert get_current_financial_year(d_mar31_next) == "FY 2026-27"


def test_invoice_immutability_on_issued_invoice(client, test_users):
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Immutable Customer")

    r_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-IMMUTABLE-01",
            "invoice_date": "2026-04-01",
            "exclusive_amount": 50000.0,
            "tax_rate": 0.18,
            "remarks": "Original internal note",
        },
        headers=fin["headers"],
    )
    inv_id = r_inv.json()["invoice_id"]

    # Modifying internal remarks is allowed
    r_up_remarks = client.put(
        f"/api/invoices/{inv_id}", json={"remarks": "Audited and verified by CFO"}, headers=fin["headers"]
    )
    assert r_up_remarks.status_code == 200

    inv = query_one("SELECT remarks FROM invoices WHERE id = ?", (inv_id,))
    assert inv["remarks"] == "Audited and verified by CFO"

    # Attempting to alter financial dates or PO identifier on issued invoice is rejected
    r_up_illegal = client.put(f"/api/invoices/{inv_id}", json={"po_number": "CHANGED-PO-01"}, headers=fin["headers"])
    assert r_up_illegal.status_code == 400
    assert "legally immutable" in r_up_illegal.json()["detail"]


def test_simultaneous_concurrent_payments_no_overpayment(client, test_users):
    """Concurrency Test: 10 concurrent threads try to record payment on an invoice with 10,000 balance."""
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Concurrent Payer")

    r_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-CONCUR-01",
            "invoice_date": "2026-04-01",
            "exclusive_amount": 10000.0,
            "tax_rate": 0.0,  # Total = 10,000.0
        },
        headers=fin["headers"],
    )
    inv_id = r_inv.json()["invoice_id"]

    def try_pay(idx):
        return client.post(
            "/api/payments",
            json={"invoice_id": inv_id, "payment_amount": 2000.0, "notes": f"Concurrent thread #{idx}"},
            headers=fin["headers"],
        )

    # Launch 10 simultaneous payments of 2,000 each (Total attempted = 20,000 on a 10,000 balance)
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(try_pay, range(10)))

    successes = [r for r in results if r.status_code == 200]
    failures = [r for r in results if r.status_code == 400]

    # Exactly 5 payments must succeed (5 * 2,000 = 10,000) and 5 must be rejected
    assert len(successes) == 5
    assert len(failures) == 5

    # Outstanding balance must be exactly 0.0, never negative!
    inv = query_one(
        "SELECT total_paid, outstanding_balance, computed_status FROM v_invoice_summary WHERE id = ?", (inv_id,)
    )
    assert inv["total_paid"] == 10000.0
    assert inv["outstanding_balance"] == 0.0
    assert inv["computed_status"] == "PAID"


def test_invoice_cancellation_workflow_and_financial_integrity(client, test_users):
    """Deep verification of cancellation workflow, payment integrity, and dashboard representation."""
    fin = test_users["finance"]
    admin = test_users["admin"]
    cid = create_customer(client, fin["headers"], "Cancel Corp")

    # Case A: Invoice with no payments
    r_inv1 = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-CNC-NOPAY",
            "invoice_date": "2026-04-05",
            "exclusive_amount": 10000.0,
            "tax_rate": 0.18,  # Total = 11,800.0
        },
        headers=fin["headers"],
    )
    assert r_inv1.status_code == 200
    inv1_id = r_inv1.json()["invoice_id"]

    # Cancel invoice
    r_cnc1 = client.post(
        f"/api/invoices/{inv1_id}/cancel",
        json={"reason": "Customer cancelled project scope"},
        headers=fin["headers"],
    )
    assert r_cnc1.status_code == 200

    row1 = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv1_id,))
    assert row1["computed_status"] == "CANCELLED"
    assert row1["outstanding_balance"] == 0.0
    assert row1["total_amount"] == 118000.0 / 10.0  # Historical 11,800.0 preserved
    assert row1["days_overdue"] == 0

    # Ensure no further payments can be recorded
    r_pay_bad = client.post(
        "/api/payments",
        json={"invoice_id": inv1_id, "payment_amount": 500.0},
        headers=fin["headers"],
    )
    assert r_pay_bad.status_code == 400
    assert "CANCELLED" in r_pay_bad.json()["detail"]

    # Case B: Invoice with existing payments cannot be directly cancelled without payment reversal
    r_inv2 = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "invoice_number": "INV-CNC-PAID",
            "invoice_date": "2026-04-05",
            "exclusive_amount": 10000.0,
            "tax_rate": 0.18,
        },
        headers=fin["headers"],
    )
    inv2_id = r_inv2.json()["invoice_id"]

    # Log partial payment with TDS
    r_pay = client.post(
        "/api/payments",
        json={
            "invoice_id": inv2_id,
            "payment_amount": 5000.0,
            "tds_amount": 200.0,
            "tds_section": "194C",
            "notes": "Partial settlement",
        },
        headers=fin["headers"],
    )
    assert r_pay.status_code == 200

    # Direct cancellation must be rejected to protect financial audit trail
    r_cnc_fail = client.post(
        f"/api/invoices/{inv2_id}/cancel",
        json={"reason": "Attempt cancel on settled"},
        headers=fin["headers"],
    )
    assert r_cnc_fail.status_code == 400
    assert "existing payments" in r_cnc_fail.json()["detail"]

    # Delete payment first, then cancel
    pay_id = r_pay.json()["payment_id"]
    r_del_pay = client.delete(f"/api/payments/{pay_id}", headers=admin["headers"])
    assert r_del_pay.status_code == 200

    r_cnc2 = client.post(
        f"/api/invoices/{inv2_id}/cancel",
        json={"reason": "Reversed and cancelled"},
        headers=fin["headers"],
    )
    assert r_cnc2.status_code == 200


def test_fallback_line_item_financial_consistency(client, test_users):
    """Verify fallback line item quantity, unit_price, tax, and total match header values."""
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Fallback Line Corp")

    # 1. Non-zero value order without explicit items
    r1 = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "po_number": "PO-FALLBACK-NZ",
            "customer_id": cid,
            "exclusive_amount": 25000.0,
            "tax_rate": 0.18,
        },
        headers=fin["headers"],
    )
    assert r1.status_code == 200
    inv1_id = r1.json()["invoice_id"]

    items1 = query_all("SELECT * FROM invoice_items WHERE invoice_id = ?", (inv1_id,))
    assert len(items1) == 1
    assert items1[0]["quantity"] == 1.0
    assert items1[0]["unit_price"] == 25000.0
    assert items1[0]["tax_rate"] == 0.18
    assert items1[0]["tax_amount"] == 4500.0
    assert items1[0]["total_amount"] == 29500.0
    assert items1[0]["description"] == "PO-FALLBACK-NZ"

    inv1 = query_one("SELECT exclusive_amount, tax_amount, total_amount FROM invoices WHERE id = ?", (inv1_id,))
    assert inv1["exclusive_amount"] == items1[0]["unit_price"]
    assert inv1["tax_amount"] == items1[0]["tax_amount"]
    assert inv1["total_amount"] == items1[0]["total_amount"]

    # 2. Zero-value order without explicit items
    r2 = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "po_number": "PO-FALLBACK-ZERO",
            "customer_id": cid,
            "exclusive_amount": 0.0,
            "tax_rate": 0.18,
        },
        headers=fin["headers"],
    )
    assert r2.status_code == 200
    inv2_id = r2.json()["invoice_id"]

    items2 = query_all("SELECT * FROM invoice_items WHERE invoice_id = ?", (inv2_id,))
    assert len(items2) == 1
    assert items2[0]["unit_price"] == 0.0
    assert items2[0]["total_amount"] == 0.0


def test_role_change_session_revocation_and_last_admin_lockout(client, test_users):
    """Verify session revocation upon role change and prevent lockout of last administrator."""
    admin = test_users["admin"]

    # 1. Create a finance user
    r_u = client.post(
        "/api/users",
        json={"username": "temp_fin", "password": "password123", "role": "finance", "full_name": "Temp Finance"},
        headers=admin["headers"],
    )
    assert r_u.status_code == 200
    u_id = r_u.json()["user_id"]

    # 2. Log in as that finance user and get token
    r_login = client.post("/api/auth/login", json={"username": "temp_fin", "password": "password123"})
    assert r_login.status_code == 200
    fin_token = r_login.json()["token"]
    fin_hdr = {"Authorization": f"Bearer {fin_token}"}

    # Verify privileged operation succeeds
    r_check = client.get("/api/users", headers=fin_hdr)
    assert r_check.status_code == 200

    # 3. Admin demotes user to viewer
    r_demote = client.put(f"/api/users/{u_id}/role", json={"role": "viewer"}, headers=admin["headers"])
    assert r_demote.status_code == 200

    # 4. Old session attempting privileged operation must be REJECTED (session revoked)
    r_after = client.get("/api/users", headers=fin_hdr)
    assert r_after.status_code == 401

    # 5. Prevent deleting or demoting the only admin account
    admin_id = test_users["admin"]["id"]
    r_del_self = client.delete(f"/api/users/{admin_id}", headers=admin["headers"])
    assert r_del_self.status_code == 400
    assert "own account" in r_del_self.json()["detail"]

    # Attempting to demote the only admin must also be rejected
    r_demote_admin = client.put(f"/api/users/{admin_id}/role", json={"role": "viewer"}, headers=admin["headers"])
    assert r_demote_admin.status_code == 400
    assert "only remaining administrator" in r_demote_admin.json()["detail"]


def test_database_po_uniqueness_constraint(client, test_users):
    """Verify database-level UNIQUE index on invoices(LOWER(TRIM(po_number)))."""
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Unique PO Corp")

    r1 = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "po_number": "PO-DB-INDEX-01", "customer_id": cid, "exclusive_amount": 5000.0},
        headers=fin["headers"],
    )
    assert r1.status_code == 200

    # Duplicate PO creation with mixed case and leading/trailing whitespace must fail
    r2 = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "po_number": "  po-db-index-01  ", "customer_id": cid, "exclusive_amount": 6000.0},
        headers=fin["headers"],
    )
    assert r2.status_code == 400
    assert "already registered" in r2.json()["detail"]

    # Placeholders with arbitrary whitespace and casing ('No PO', ' no po ', ' recurring / direct invoice ') must be allowed concurrently
    r_p1 = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "po_number": "No PO", "customer_id": cid, "exclusive_amount": 1000.0},
        headers=fin["headers"],
    )
    assert r_p1.status_code == 200

    r_p2 = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "po_number": "  no po  ", "customer_id": cid, "exclusive_amount": 2000.0},
        headers=fin["headers"],
    )
    assert r_p2.status_code == 200

    r_p3 = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "po_number": " Recurring / Direct Invoice ", "customer_id": cid, "exclusive_amount": 3000.0},
        headers=fin["headers"],
    )
    assert r_p3.status_code == 200


def test_database_invoice_number_uniqueness_constraint(client, test_users):
    """Verify database-level UNIQUE index on invoices(LOWER(TRIM(invoice_number)))."""
    fin = test_users["finance"]
    cid = create_customer(client, fin["headers"], "Unique Inv Corp")

    r1 = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "invoice_number": "INV-NORM-01",
            "invoice_date": "2026-04-01",
            "customer_id": cid,
            "exclusive_amount": 5000.0,
        },
        headers=fin["headers"],
    )
    assert r1.status_code == 200

    # Duplicate invoice_number with lowercase and whitespace must fail
    r2 = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "invoice_number": "  inv-norm-01  ",
            "invoice_date": "2026-04-02",
            "customer_id": cid,
            "exclusive_amount": 6000.0,
        },
        headers=fin["headers"],
    )
    assert r2.status_code == 400
    assert "already assigned" in r2.json()["detail"]

