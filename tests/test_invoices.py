from app.database import query_all, query_one


def create_sample_customer(client, headers):
    resp = client.post(
        "/api/customers",
        json={
            "billing_name": "Acme Industrial Corp",
            "gst_number": "27AABCA1234A1Z5",
            "city": "Mumbai",
            "state": "Maharashtra",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()["customer_id"]


def test_po_creation_and_calculation(client, test_users):
    headers = test_users["sales"]["headers"]
    cid = create_sample_customer(client, headers)

    payload = {
        "fy_code": "FY 2026-27",
        "order_type": "PO",
        "po_number": "PO-2026-001",
        "po_date": "2026-04-10",
        "customer_id": cid,
        "exclusive_amount": 100000.0,
        "tax_rate": 0.18,
        "currency": "INR",
    }
    resp = client.post("/api/invoices", json=payload, headers=headers)
    assert resp.status_code == 200
    inv_id = resp.json()["invoice_id"]

    inv = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv["exclusive_amount"] == 100000.0
    assert inv["tax_amount"] == 18000.0
    assert inv["total_amount"] == 118000.0
    assert inv["outstanding_balance"] == 118000.0
    assert inv["computed_status"] == "UNBILLED"


def test_multi_line_items_atomic_calculation(client, test_users):
    headers = test_users["sales"]["headers"]
    cid = create_sample_customer(client, headers)

    payload = {
        "fy_code": "FY 2026-27",
        "order_type": "PO",
        "po_number": "PO-ITEMS-100",
        "customer_id": cid,
        "exclusive_amount": 0,  # Should be calculated from items
        "items": [
            {
                "description": "Product A",
                "quantity": 10,
                "unit_price": 5000,
                "tax_rate": 0.18,
            },  # 50000 + 9000 tax = 59000
            {
                "description": "Product B",
                "quantity": 5,
                "unit_price": 10000,
                "tax_rate": 0.18,
            },  # 50000 + 9000 tax = 59000
        ],
    }
    resp = client.post("/api/invoices", json=payload, headers=headers)
    assert resp.status_code == 200
    inv_id = resp.json()["invoice_id"]

    inv = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv["exclusive_amount"] == 100000.0
    assert inv["tax_amount"] == 18000.0
    assert inv["total_amount"] == 118000.0

    items = query_all("SELECT * FROM invoice_items WHERE invoice_id = ?", (inv_id,))
    assert len(items) == 2


def test_recurring_billing_auto_net_30_due_date(client, test_users):
    headers = test_users["sales"]["headers"]
    cid = create_sample_customer(client, headers)

    payload = {
        "fy_code": "FY 2026-27",
        "order_type": "RECURRING",
        "invoice_number": "INV-REC-2026-01",
        "invoice_date": "2026-05-01",
        # due_date is omitted: should auto-calculate Net-30 to 2026-05-31
        "customer_id": cid,
        "exclusive_amount": 50000.0,
        "tax_rate": 0.18,
    }
    resp = client.post("/api/invoices", json=payload, headers=headers)
    assert resp.status_code == 200
    inv_id = resp.json()["invoice_id"]

    inv = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv["invoice_number"] == "INV-REC-2026-01"
    assert inv["invoice_date"] == "2026-05-01"
    assert inv["due_date"] == "2026-05-31"  # Exactly 30 days later
    assert inv["computed_status"] in ("PENDING", "OVERDUE")


def test_po_conversion_to_invoice_rbac(client, test_users):
    sales_headers = test_users["sales"]["headers"]
    finance_headers = test_users["finance"]["headers"]
    cid = create_sample_customer(client, sales_headers)

    # 1. Sales creates Unbilled PO
    resp = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "order_type": "PO",
            "po_number": "PO-CONVERT-1",
            "customer_id": cid,
            "exclusive_amount": 20000.0,
        },
        headers=sales_headers,
    )
    order_id = resp.json()["invoice_id"]

    # 2. Sales tries to convert to invoice -> Forbidden (403)
    r_forbidden = client.post(
        f"/api/invoices/{order_id}/convert-to-invoice",
        json={"invoice_number": "INV-OFFICIAL-01", "invoice_date": "2026-06-01"},
        headers=sales_headers,
    )
    assert r_forbidden.status_code == 403

    # 3. Finance converts to invoice -> Success (200) with Net-30 due date
    r_ok = client.post(
        f"/api/invoices/{order_id}/convert-to-invoice",
        json={"invoice_number": "INV-OFFICIAL-01", "invoice_date": "2026-06-01"},
        headers=finance_headers,
    )
    assert r_ok.status_code == 200

    inv = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (order_id,))
    assert inv["invoice_number"] == "INV-OFFICIAL-01"
    assert inv["invoice_date"] == "2026-06-01"
    assert inv["due_date"] == "2026-07-01"  # 2026-06-01 + 30 days = 2026-07-01 (June has 30 days)


def test_foreign_currency_conversion_and_preservation(client, test_users):
    headers = test_users["sales"]["headers"]
    cid = create_sample_customer(client, headers)

    payload = {
        "fy_code": "FY 2026-27",
        "customer_id": cid,
        "currency": "USD",
        "exchange_rate": 83.5,
        "original_amount": 1000.0,  # $1,000 USD
        "exclusive_amount": 0.0,
        "tax_rate": 0.18,
    }
    resp = client.post("/api/invoices", json=payload, headers=headers)
    assert resp.status_code == 200
    inv_id = resp.json()["invoice_id"]

    inv = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert inv["currency"] == "USD"
    assert inv["exchange_rate"] == 83.5
    assert inv["original_amount"] == 1000.0
    assert inv["exclusive_amount"] == 83500.0  # 1000 * 83.5
    assert inv["tax_amount"] == 15030.0  # 83500 * 0.18
    assert inv["total_amount"] == 98530.0


def test_invoice_deletion_rbac_and_cascade(client, test_users):
    sales_headers = test_users["sales"]["headers"]
    finance_headers = test_users["finance"]["headers"]
    cid = create_sample_customer(client, sales_headers)

    resp = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "customer_id": cid, "exclusive_amount": 5000.0},
        headers=sales_headers,
    )
    inv_id = resp.json()["invoice_id"]

    # Sales cannot delete
    r_del_sales = client.delete(f"/api/invoices/{inv_id}", headers=sales_headers)
    assert r_del_sales.status_code == 403

    # Finance can delete
    r_del_fin = client.delete(f"/api/invoices/{inv_id}", headers=finance_headers)
    assert r_del_fin.status_code == 200

    assert query_one("SELECT id FROM invoices WHERE id = ?", (inv_id,)) is None


def test_line_item_description_editing_and_fallback(client, test_users):
    sales_headers = test_users["sales"]["headers"]
    finance_headers = test_users["finance"]["headers"]
    cid = create_sample_customer(client, sales_headers)

    # 1. Create order without explicit items - verifies fallback line item created
    resp = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "po_number": "PO-EDIT-DESC", "customer_id": cid, "exclusive_amount": 10000.0},
        headers=sales_headers,
    )
    assert resp.status_code == 200
    inv_id = resp.json()["invoice_id"]

    items = query_all("SELECT id, description, total_amount FROM invoice_items WHERE invoice_id = ?", (inv_id,))
    assert len(items) == 1
    assert items[0]["description"] == "PO-EDIT-DESC"
    item_id = items[0]["id"]

    # 2. Update item description via PUT /api/invoices/{order_id}
    upd_resp = client.put(
        f"/api/invoices/{inv_id}",
        json={
            "items": [{"id": item_id, "description": "Custom Cloud Consulting Services"}],
            "remarks": "Updated product description",
        },
        headers=finance_headers,
    )
    assert upd_resp.status_code == 200

    refetched = query_one("SELECT description FROM invoice_items WHERE id = ?", (item_id,))
    assert refetched["description"] == "Custom Cloud Consulting Services"

    # 3. Reject modifying line item with mismatched ID
    bad_upd = client.put(
        f"/api/invoices/{inv_id}",
        json={"items": [{"id": 999999, "description": "Hacked Item"}]},
        headers=finance_headers,
    )
    assert bad_upd.status_code == 400
    assert "does not belong" in bad_upd.json()["detail"]


def test_unbilled_po_update_duplicate_po_rejected(client, test_users):
    sales_headers = test_users["sales"]["headers"]
    finance_headers = test_users["finance"]["headers"]
    cid = create_sample_customer(client, sales_headers)

    resp1 = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "po_number": "PO-ALPHA-01", "customer_id": cid, "exclusive_amount": 10000.0},
        headers=sales_headers,
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "po_number": "PO-BETA-02", "customer_id": cid, "exclusive_amount": 20000.0},
        headers=sales_headers,
    )
    assert resp2.status_code == 200
    inv2_id = resp2.json()["invoice_id"]

    # Attempt to update order 2's PO number to order 1's PO number
    dup_upd = client.put(
        f"/api/invoices/{inv2_id}",
        json={"po_number": "PO-ALPHA-01"},
        headers=finance_headers,
    )
    assert dup_upd.status_code == 400
    assert "already registered" in dup_upd.json()["detail"]
