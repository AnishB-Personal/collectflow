"""
Tests for Cross-Site Scripting (XSS) protections and security response headers.
"""


def test_security_headers_present_on_endpoints(client):
    """Verify security headers are applied to HTTP responses."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Content-Security-Policy" in r.headers
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "Referrer-Policy" in r.headers
    assert "Permissions-Policy" in r.headers


def test_malicious_xss_payloads_in_customer_fields(client, test_users):
    """Verify storing and retrieving malicious HTML/JS payloads without execution."""
    fin_headers = test_users["finance"]["headers"]
    malicious_name = '<script>alert("XSS-Customer")</script>'
    malicious_city = '<img src=x onerror=alert("XSS-City")>'

    r = client.post(
        "/api/customers",
        json={
            "billing_name": malicious_name,
            "city": malicious_city,
            "state": "Karnataka",
            "gst_number": "29AABCB1234F1Z5",
        },
        headers=fin_headers,
    )
    assert r.status_code == 200
    cid = r.json()["customer_id"]

    # Verify retrieval
    r_list = client.get(f"/api/customers?search={malicious_name}", headers=fin_headers)
    assert r_list.status_code == 200
    cust_data = r_list.json()
    assert len(cust_data) >= 1
    found = [c for c in cust_data if c["id"] == cid][0]
    assert found["billing_name"] == malicious_name
    assert found["city"] == malicious_city


def test_malicious_xss_payloads_in_user_full_name(client, test_users):
    """Verify storing and retrieving malicious script tags in user account names."""
    admin_headers = test_users["admin"]["headers"]
    malicious_full_name = '<script>alert("XSS-User")</script>'

    r = client.post(
        "/api/users",
        json={
            "username": "xss_test_user",
            "password": "Password@123",
            "full_name": malicious_full_name,
            "role": "viewer",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200
    uid = r.json()["user_id"]

    r_users = client.get("/api/users", headers=admin_headers)
    assert r_users.status_code == 200
    users = r_users.json()
    target = [u for u in users if u["id"] == uid][0]
    assert target["full_name"] == malicious_full_name


def test_malicious_xss_payloads_in_invoice_line_items(client, test_users):
    """Verify invoice line item descriptions containing malicious payloads."""
    sales_headers = test_users["sales"]["headers"]
    fin_headers = test_users["finance"]["headers"]

    # Create customer
    r_c = client.post("/api/customers", json={"billing_name": "Secure Corp"}, headers=fin_headers)
    cid = r_c.json()["customer_id"]

    malicious_desc = '<img src=x onerror=alert("XSS-LineItem")>'
    r_inv = client.post(
        "/api/invoices",
        json={
            "fy_code": "FY 2026-27",
            "customer_id": cid,
            "exclusive_amount": 5000.0,
            "items": [{"description": malicious_desc, "quantity": 1, "unit_price": 5000.0, "tax_rate": 0.18}],
        },
        headers=sales_headers,
    )
    assert r_inv.status_code == 200
    order_id = r_inv.json()["invoice_id"]

    # Verify line items query
    r_detail = client.get(f"/api/invoices/{order_id}", headers=sales_headers)
    assert r_detail.status_code == 200
    items = r_detail.json()["items"]
    assert len(items) == 1
    assert items[0]["description"] == malicious_desc
