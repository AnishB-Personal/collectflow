import io

import openpyxl


def test_excel_formula_injection_protection(client, admin_token):
    """
    CWE-1236 Defense: Verify that user-supplied text starting with formula characters
    (=, +, -, @) is neutralized by prepending a single quote (') so that spreadsheet
    programs do not execute arbitrary formulas or exfiltrate data.
    """
    malicious_cust_name = "=SUM(A1:A100)"
    malicious_po_num = "+cmd|'/C calc'!A0"

    cust_res = client.post(
        "/api/customers",
        json={"billing_name": malicious_cust_name, "gst_number": "27AAAAA1234A1Z5"},
        headers=admin_token,
    )
    assert cust_res.status_code == 200
    cust_id = cust_res.json()["customer_id"]

    inv_res = client.post(
        "/api/invoices",
        json={
            "customer_id": cust_id,
            "po_number": malicious_po_num,
            "po_date": "2026-05-01",
            "fy_code": "FY 2026-27",
            "exclusive_amount": 1000.0,
            "items": [{"description": "Item", "quantity": 1, "unit_price": 1000.0, "tax_rate": 0.18}],
        },
        headers=admin_token,
    )
    assert inv_res.status_code == 200

    # Request Excel export
    export_res = client.get("/api/export/excel?fy=FY 2026-27", headers=admin_token)
    assert export_res.status_code == 200
    assert "spreadsheetml" in export_res.headers["content-type"]

    # Parse exported workbook with openpyxl
    wb = openpyxl.load_workbook(filename=io.BytesIO(export_res.content))
    ws = wb.active

    # Find the row corresponding to our invoice
    found = False
    for row in ws.iter_rows(min_row=2, values_only=True):
        po_num_cell = row[2]
        cust_name_cell = row[3]
        if po_num_cell == f"'{malicious_po_num}":
            found = True
            assert cust_name_cell == f"'{malicious_cust_name}"
            # Ensure neither cell starts directly with '=' or '+'
            assert not str(cust_name_cell).startswith("=")
            assert not str(po_num_cell).startswith("+")
            break

    assert found, "Exported sheet must contain neutralized cells for the malicious payload"
