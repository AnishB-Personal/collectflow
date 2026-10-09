from app.database import query_one


def test_customer_crud_and_rbac(client, test_users):
    sales_headers = test_users["sales"]["headers"]
    fin_headers = test_users["finance"]["headers"]
    viewer_headers = test_users["viewer"]["headers"]

    # 1. Create customer (authenticated users can create)
    r_create = client.post(
        "/api/customers",
        json={
            "billing_name": "Bharat Electronics Ltd",
            "gst_number": "29AABCB2234B1Z2",
            "city": "Bengaluru",
            "state": "Karnataka",
        },
        headers=sales_headers,
    )
    assert r_create.status_code == 200
    cid = r_create.json()["customer_id"]

    # 2. Update customer: sales/viewer forbidden, finance succeeds
    r_up_sales = client.put(
        f"/api/customers/{cid}",
        json={"billing_name": "Bharat Electronics Ltd", "city": "Mysuru"},
        headers=sales_headers,
    )
    assert r_up_sales.status_code == 403

    r_up_fin = client.put(
        f"/api/customers/{cid}", json={"billing_name": "Bharat Electronics Ltd", "city": "Mysuru"}, headers=fin_headers
    )
    assert r_up_fin.status_code == 200

    cust = query_one("SELECT city FROM customers WHERE id = ?", (cid,))
    assert cust["city"] == "Mysuru"

    # 3. Delete customer: sales/viewer forbidden, finance succeeds
    r_del_viewer = client.delete(f"/api/customers/{cid}", headers=viewer_headers)
    assert r_del_viewer.status_code == 403

    r_del_fin = client.delete(f"/api/customers/{cid}", headers=fin_headers)
    assert r_del_fin.status_code == 200

    assert query_one("SELECT id FROM customers WHERE id = ?", (cid,)) is None


def test_customer_deletion_blocked_if_orders_exist(client, test_users):
    fin_headers = test_users["finance"]["headers"]

    # Create customer
    r_create = client.post("/api/customers", json={"billing_name": "Tata Motors Ltd"}, headers=fin_headers)
    cid = r_create.json()["customer_id"]

    # Create invoice for customer
    client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "customer_id": cid, "exclusive_amount": 10000.0},
        headers=fin_headers,
    )

    # Deletion attempt must fail with 400
    r_del = client.delete(f"/api/customers/{cid}", headers=fin_headers)
    assert r_del.status_code == 400
    assert "associated invoice(s)/order(s)" in r_del.json()["detail"]


def test_customer_listing_includes_tds_in_paid_balance(client, test_users):
    fin_headers = test_users["finance"]["headers"]

    # Create customer
    r_create = client.post("/api/customers", json={"billing_name": "Infosys BPM Ltd"}, headers=fin_headers)
    cid = r_create.json()["customer_id"]

    # Create invoice: 10,000 + 18% tax = 11,800
    r_inv = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "customer_id": cid, "exclusive_amount": 10000.0, "tax_rate": 0.18},
        headers=fin_headers,
    )
    inv_id = r_inv.json()["invoice_id"]

    # Record payment: 10,000 cash + 1,800 TDS = 11,800 total paid
    client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": 10000.0, "tds_amount": 1800.0},
        headers=fin_headers,
    )

    # In customer listing, total_paid must be 11,800 and outstanding must be 0
    r_list = client.get("/api/customers?search=Infosys", headers=fin_headers)
    assert r_list.status_code == 200
    data = r_list.json()
    assert len(data) == 1
    assert data[0]["total_billed"] == 11800.0
    assert data[0]["total_paid"] == 11800.0
    assert data[0]["total_outstanding"] == 0.0
