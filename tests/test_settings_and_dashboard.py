import datetime
import io

import openpyxl

from app.utils.financial import get_current_financial_year


def test_indian_fiscal_year_detection():
    # April to March convention
    d1 = datetime.date(2026, 4, 1)
    assert get_current_financial_year(d1) == "FY 2026-27"

    d2 = datetime.date(2026, 10, 7)
    assert get_current_financial_year(d2) == "FY 2026-27"

    d3 = datetime.date(2027, 3, 31)
    assert get_current_financial_year(d3) == "FY 2026-27"

    d4 = datetime.date(2026, 3, 31)
    assert get_current_financial_year(d4) == "FY 2025-26"


def test_settings_fy_validation_and_integrity(client, test_users):
    fin_headers = test_users["finance"]["headers"]

    # Invalid dates: start after end
    r_bad = client.post(
        "/api/settings/financial-years",
        json={"code": "FY 2030-31", "start_date": "2031-04-01", "end_date": "2030-03-31"},
        headers=fin_headers,
    )
    assert r_bad.status_code == 400

    # Valid FY
    r_ok = client.post(
        "/api/settings/financial-years",
        json={"code": "FY 2029-30", "start_date": "2029-04-01", "end_date": "2030-03-31"},
        headers=fin_headers,
    )
    assert r_ok.status_code == 200

    # Create customer and invoice with FY 2029-30
    c_resp = client.post("/api/customers", json={"billing_name": "FY Test Corp"}, headers=fin_headers)
    cid = c_resp.json()["customer_id"]
    client.post(
        "/api/invoices",
        json={"fy_code": "FY 2029-30", "customer_id": cid, "exclusive_amount": 5000.0},
        headers=fin_headers,
    )

    # Deleting FY 2029-30 must be blocked because an invoice references it
    r_del = client.delete("/api/settings/financial-years/FY 2029-30", headers=fin_headers)
    assert r_del.status_code == 400
    assert "invoice(s) are associated with it" in r_del.json()["detail"]


def test_dashboard_kpis_and_metrics(client, test_users):
    fin_headers = test_users["finance"]["headers"]

    # Create customer
    c_resp = client.post("/api/customers", json={"billing_name": "Metro Infotech"}, headers=fin_headers)
    cid = c_resp.json()["customer_id"]

    # Create invoice: 100,000 + 18% = 118,000
    r_inv = client.post(
        "/api/invoices",
        json={"fy_code": "FY 2026-27", "customer_id": cid, "exclusive_amount": 100000.0, "tax_rate": 0.18},
        headers=fin_headers,
    )
    inv_id = r_inv.json()["invoice_id"]

    # Record payment: Cash 80,000 + TDS 10,000 = 90,000 settled
    client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": 80000.0, "tds_amount": 10000.0},
        headers=fin_headers,
    )

    # Fetch dashboard summary
    r_dash = client.get("/api/dashboard/summary", headers=fin_headers)
    assert r_dash.status_code == 200
    kpis = r_dash.json()["kpis"]

    assert kpis["total_invoiced"] == 118000.0
    assert kpis["total_cash_collected"] == 80000.0
    assert kpis["total_tds_recognized"] == 10000.0
    assert kpis["total_settled"] == 90000.0
    assert kpis["total_outstanding"] == 28000.0


def test_excel_export_generation(client, test_users):
    fin_headers = test_users["finance"]["headers"]

    # Request export with bearer header
    resp = client.get("/api/export/excel?fy=FY 2026-27", headers=fin_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    # Verify workbook content
    content = io.BytesIO(resp.content)
    wb = openpyxl.load_workbook(content)
    ws = wb.active
    assert ws.title == "Invoices & Collections"
    headers = [cell.value for cell in ws[1]]
    assert "Cash Received (₹)" in headers
    assert "TDS Deducted (₹)" in headers
    assert "Total Settled (₹)" in headers
    assert "Balance Outstanding (₹)" in headers


def test_export_single_use_ticket_flow_and_rejection(client, test_users):
    fin = test_users["finance"]

    # 1. Unauthenticated export must fail with 401
    r_unauth = client.get("/api/export/excel")
    assert r_unauth.status_code == 401

    # 2. Generate short-lived single-use ticket
    r_tok = client.post("/api/export/token", headers=fin["headers"])
    assert r_tok.status_code == 200
    ticket = r_tok.json()["ticket"]
    assert len(ticket) >= 32

    # 3. Download using ticket succeeds
    r_dl = client.get(f"/api/export/excel?ticket={ticket}")
    assert r_dl.status_code == 200
    assert r_dl.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    # 4. Attempting to reuse the exact same ticket must fail (single-use constraint)
    r_reuse = client.get(f"/api/export/excel?ticket={ticket}")
    assert r_reuse.status_code == 401
    assert "already consumed" in r_reuse.json()["detail"]

    # 5. Bogus ticket fails
    r_fake = client.get("/api/export/excel?ticket=invalid-ticket-12345")
    assert r_fake.status_code == 401
