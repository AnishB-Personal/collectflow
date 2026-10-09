def test_rbac_matrix_customers(client, admin_token, finance_token, sales_token, viewer_token):
    """Verify RBAC on Customer endpoints."""
    # Create customer: admin, finance, and sales can create; viewer is forbidden
    c_admin = client.post("/api/customers", json={"billing_name": "Cust Admin"}, headers=admin_token)
    assert c_admin.status_code == 200
    cust_id = c_admin.json()["customer_id"]

    c_fin = client.post("/api/customers", json={"billing_name": "Cust Finance"}, headers=finance_token)
    assert c_fin.status_code == 200

    c_sales = client.post("/api/customers", json={"billing_name": "Cust Sales"}, headers=sales_token)
    assert c_sales.status_code == 200

    c_view = client.post("/api/customers", json={"billing_name": "Cust Viewer"}, headers=viewer_token)
    assert c_view.status_code == 403

    # Update customer: admin & finance only; sales & viewer forbidden (403)
    u_admin = client.put(f"/api/customers/{cust_id}", json={"billing_name": "Cust Updated Admin"}, headers=admin_token)
    assert u_admin.status_code == 200

    u_fin = client.put(f"/api/customers/{cust_id}", json={"billing_name": "Cust Updated Fin"}, headers=finance_token)
    assert u_fin.status_code == 200

    u_sales = client.put(f"/api/customers/{cust_id}", json={"billing_name": "Cust Updated Sales"}, headers=sales_token)
    assert u_sales.status_code == 403

    u_view = client.put(f"/api/customers/{cust_id}", json={"billing_name": "Cust Updated View"}, headers=viewer_token)
    assert u_view.status_code == 403

    # Delete customer: admin & finance only; sales & viewer forbidden (403)
    d_sales = client.delete(f"/api/customers/{cust_id}", headers=sales_token)
    assert d_sales.status_code == 403

    d_view = client.delete(f"/api/customers/{cust_id}", headers=viewer_token)
    assert d_view.status_code == 403

    d_fin = client.delete(f"/api/customers/{cust_id}", headers=finance_token)
    assert d_fin.status_code == 200


def test_rbac_matrix_user_management(client, admin_token, finance_token, sales_token, viewer_token):
    """Verify RBAC on User Management endpoints (admin only)."""
    # Create user
    for role_name, token in [("finance", finance_token), ("sales", sales_token), ("viewer", viewer_token)]:
        res = client.post(
            "/api/users", json={"username": f"user_by_{role_name}", "password": "password123"}, headers=token
        )
        assert res.status_code == 403, f"Expected 403 for {role_name}, got {res.status_code}"

    res_admin = client.post(
        "/api/users", json={"username": "user_by_admin", "password": "password123"}, headers=admin_token
    )
    assert res_admin.status_code == 200
    new_user_id = res_admin.json()["user_id"]

    # Delete user
    assert client.delete(f"/api/users/{new_user_id}", headers=finance_token).status_code == 403
    assert client.delete(f"/api/users/{new_user_id}", headers=sales_token).status_code == 403
    assert client.delete(f"/api/users/{new_user_id}", headers=viewer_token).status_code == 403
    assert client.delete(f"/api/users/{new_user_id}", headers=admin_token).status_code == 200


def test_rbac_matrix_payments(client, admin_token, finance_token, sales_token, viewer_token):
    """Verify RBAC on Payment endpoints (admin & finance only)."""
    cust_res = client.post("/api/customers", json={"billing_name": "Payment RBAC Cust"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    inv_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": "PO-PAY-RBAC",
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
        json={"invoice_number": "INV-PAY-RBAC-01", "invoice_date": "2026-05-01"},
        headers=admin_token,
    )

    # Sales & viewer cannot post payments
    p_sales = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_date": "2026-05-02", "payment_amount": 100.0, "payment_mode": "NEFT"},
        headers=sales_token,
    )
    assert p_sales.status_code == 403

    p_view = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_date": "2026-05-02", "payment_amount": 100.0, "payment_mode": "NEFT"},
        headers=viewer_token,
    )
    assert p_view.status_code == 403

    # Finance can post payment
    p_fin = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_date": "2026-05-02", "payment_amount": 100.0, "payment_mode": "NEFT"},
        headers=finance_token,
    )
    assert p_fin.status_code == 200
    pay_id = p_fin.json()["payment_id"]

    # Delete payment
    assert client.delete(f"/api/payments/{pay_id}", headers=sales_token).status_code == 403
    assert client.delete(f"/api/payments/{pay_id}", headers=viewer_token).status_code == 403
    assert client.delete(f"/api/payments/{pay_id}", headers=finance_token).status_code == 200
