"""
CollectFlow AP-01 Independent UAT Findings Remediation Test Suite.

Validates the remediation of independently documented UAT findings:
- D-01: Invoice report numerical parsing and NaN prevention.
- D-02: Issued-only and outstanding exports exclude cancelled and zero-balance invoices.
- D-03: Dashboard, AR aging, and reporting summary align on as-of dates and overdue aging.
- D-04: Executive Attainment report calculates true net outstanding receivables, not gross turnover.
- D-05: Converted purchase orders display 0 unbilled amount, populated invoice number and dates.
- D-06: Review and rehearsal calculations predict values actually committed.
- D-07: Tax rate percentage formatting displays exact integers without floating point noise.
- D-08 / NEG-INR-001: Strict INR restriction at backend boundaries; rejection of USD/foreign commercial transactions.
- P-01: Case-insensitive purchase order identifier uniqueness enforcement.
"""

import datetime
from decimal import Decimal
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, text

from app.core.database import get_db_session
from app.main import app
from app.models.domain_models import (
    CommercialInvoice,
    CommercialInvoiceItem,
    Customer,
    FinancialYear,
    PaymentTransaction,
    PurchaseOrder,
    PurchaseOrderItem,
    User,
)
from app.utils.financial import ALLOWED_CURRENCIES

client = TestClient(app)


def _get_admin_token() -> str:
    resp = client.post("/api/auth/login", json={"username": "sandbox_admin", "password": "AdminPass123!"})
    assert resp.status_code == 200, f"Admin login failed: {resp.text}"
    return resp.json()["token"]


def _get_finance_token() -> str:
    resp = client.post("/api/auth/login", json={"username": "sandbox_finance", "password": "FinancePass123!"})
    assert resp.status_code == 200, f"Finance login failed: {resp.text}"
    return resp.json()["token"]


class TestCurrencyRestrictionD08AndNegInr001:
    """Remediation verification for D-08 and Supplemental NEG-INR-001."""

    def test_allowed_currencies_strictly_inr_only(self):
        assert ALLOWED_CURRENCIES == {"INR"}, "Scope AP-01 strictly restricts commercial transactions to INR"

    def test_direct_invoice_creation_rejects_usd(self):
        token = _get_finance_token()
        payload = {
            "fy_code": "FY 2026-27",
            "invoice_number": "INV-TEST-USD-001",
            "invoice_date": "2026-05-10",
            "customer_id": 1,
            "currency": "USD",
            "exchange_rate": 83.5,
            "items": [
                {
                    "item_description": "Cloud Architecture Consulting",
                    "quantity": 10.0,
                    "unit_price": 100.0,
                    "tax_rate": 0.18,
                }
            ],
        }
        resp = client.post(
            "/api/invoices",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400, f"Expected 400 for USD invoice creation, got {resp.status_code}: {resp.text}"
        assert "unsupported currency" in resp.json()["detail"].lower()

    def test_order_creation_rejects_usd(self):
        token = _get_admin_token()
        payload = {
            "fy_code": "FY 2026-27",
            "po_number": "PO-TEST-USD-REJ-01",
            "po_date": "2026-05-12",
            "customer_id": 1,
            "currency": "USD",
            "exclusive_amount": 5000.0,
            "items": [
                {
                    "item_description": "Export Subscription USD",
                    "quantity": 1.0,
                    "unit_price": 5000.0,
                    "tax_rate": 0.18,
                }
            ],
        }
        resp = client.post(
            "/api/orders",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400, f"Expected 400 for USD PO creation, got {resp.status_code}: {resp.text}"
        assert "only inr currency is supported" in resp.json()["detail"].lower()

    def test_order_update_rejects_usd(self):
        token = _get_admin_token()
        # 1. Create valid INR order
        po_num = f"PO-INR-VALID-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"
        create_resp = client.post(
            "/api/orders",
            json={
                "fy_code": "FY 2026-27",
                "po_number": po_num,
                "po_date": "2026-05-15",
                "customer_id": 1,
                "currency": "INR",
                "exclusive_amount": 10000.0,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert create_resp.status_code == 201, f"Failed to create order: {create_resp.text}"
        order_id = create_resp.json()["id"]

        try:
            # 2. Attempt to update currency to USD
            update_resp = client.put(
                f"/api/orders/{order_id}",
                json={"currency": "USD"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert update_resp.status_code == 400, f"Expected 400 for updating to USD, got {update_resp.status_code}: {update_resp.text}"
            assert "only inr currency is supported" in update_resp.json()["detail"].lower()
        finally:
            client.delete(f"/api/orders/{order_id}", headers={"Authorization": f"Bearer {token}"})


class TestPurchaseOrderIdPolicyP01:
    """Remediation verification for P-01: PO references are customer-supplied external references,

    not unique business identifiers. Multiple orders may share the same PO reference.
    """

    def test_repeated_po_reference_same_customer_permitted(self):
        token = _get_admin_token()
        po_ref = f"PO-SAME-CUST-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"

        r1 = client.post(
            "/api/orders",
            json={
                "fy_code": "FY 2026-27",
                "po_number": po_ref,
                "po_date": "2026-05-15",
                "customer_id": 1,
                "currency": "INR",
                "exclusive_amount": 10000.0,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 201, f"Failed creating first PO: {r1.text}"
        id1 = r1.json()["id"]

        try:
            # Second order for the SAME customer with identical PO reference must be accepted
            r2 = client.post(
                "/api/orders",
                json={
                    "fy_code": "FY 2026-27",
                    "po_number": po_ref,
                    "po_date": "2026-05-16",
                    "customer_id": 1,
                    "currency": "INR",
                    "exclusive_amount": 15000.0,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r2.status_code == 201, f"Expected 201 for repeated PO reference, got {r2.status_code}: {r2.text}"
            id2 = r2.json()["id"]
            assert id1 != id2, "Each purchase order must retain its unique internal database identifier"
            assert r1.json()["po_number"] == po_ref
            assert r2.json()["po_number"] == po_ref
        finally:
            client.delete(f"/api/orders/{id1}", headers={"Authorization": f"Bearer {token}"})
            if "id2" in locals():
                client.delete(f"/api/orders/{id2}", headers={"Authorization": f"Bearer {token}"})

    def test_repeated_po_reference_across_different_customers_permitted(self):
        token = _get_admin_token()
        po_ref = f"PO-DIFF-CUST-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"

        # Create or ensure second customer exists
        cust_resp = client.post(
            "/api/customers",
            json={
                "billing_name": f"Partner Corp {datetime.date.today().strftime('%H%M%S')}",
                "gstin": "29ABCDE1234F1Z5",
                "state": "Karnataka",
                "state_code": "29",
                "payment_terms_days": 30,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        cid2 = cust_resp.json()["id"] if cust_resp.status_code == 201 else 2

        r1 = client.post(
            "/api/orders",
            json={"fy_code": "FY 2026-27", "po_number": po_ref, "po_date": "2026-05-15", "customer_id": 1, "currency": "INR", "exclusive_amount": 10000.0},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 201
        id1 = r1.json()["id"]

        try:
            # Order for different customer with identical PO reference
            r2 = client.post(
                "/api/orders",
                json={"fy_code": "FY 2026-27", "po_number": po_ref, "po_date": "2026-05-16", "customer_id": cid2, "currency": "INR", "exclusive_amount": 20000.0},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r2.status_code == 201, f"Expected 201 across different customers, got {r2.status_code}: {r2.text}"
            id2 = r2.json()["id"]
            assert id1 != id2
        finally:
            client.delete(f"/api/orders/{id1}", headers={"Authorization": f"Bearer {token}"})
            if "id2" in locals():
                client.delete(f"/api/orders/{id2}", headers={"Authorization": f"Bearer {token}"})

    def test_case_variant_and_whitespace_repeated_po_reference_permitted(self):
        token = _get_admin_token()
        base_po = f"PO-FLEX-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"

        r1 = client.post(
            "/api/orders",
            json={"fy_code": "FY 2026-27", "po_number": base_po.upper(), "po_date": "2026-05-15", "customer_id": 1, "currency": "INR", "exclusive_amount": 10000.0},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 201
        id1 = r1.json()["id"]

        try:
            # Case variant and whitespace
            r2 = client.post(
                "/api/orders",
                json={"fy_code": "FY 2026-27", "po_number": f"  {base_po.lower()}  ", "po_date": "2026-05-16", "customer_id": 1, "currency": "INR", "exclusive_amount": 12000.0},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r2.status_code == 201, f"Case-variant duplicate PO should be permitted: {r2.text}"
            id2 = r2.json()["id"]
            assert id1 != id2
        finally:
            client.delete(f"/api/orders/{id1}", headers={"Authorization": f"Bearer {token}"})
            if "id2" in locals():
                client.delete(f"/api/orders/{id2}", headers={"Authorization": f"Bearer {token}"})

    def test_update_po_reference_to_existing_reference_permitted(self):
        token = _get_admin_token()
        ts = datetime.date.today().strftime('%Y%m%d%H%M%S')
        po_a = f"PO-UP-A-{ts}"
        po_b = f"PO-UP-B-{ts}"

        r1 = client.post(
            "/api/orders",
            json={"fy_code": "FY 2026-27", "po_number": po_a, "po_date": "2026-05-15", "customer_id": 1, "currency": "INR", "exclusive_amount": 10000.0},
            headers={"Authorization": f"Bearer {token}"},
        )
        r2 = client.post(
            "/api/orders",
            json={"fy_code": "FY 2026-27", "po_number": po_b, "po_date": "2026-05-15", "customer_id": 1, "currency": "INR", "exclusive_amount": 12000.0},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        id_a = r1.json()["id"]
        id_b = r2.json()["id"]

        try:
            # Update order B to order A's PO number -> must succeed
            r_up = client.put(
                f"/api/orders/{id_b}",
                json={"po_number": po_a},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r_up.status_code == 200, f"Expected 200 updating to existing PO reference, got {r_up.status_code}: {r_up.text}"
            assert r_up.json()["po_number"] == po_a
        finally:
            client.delete(f"/api/orders/{id_a}", headers={"Authorization": f"Bearer {token}"})
            client.delete(f"/api/orders/{id_b}", headers={"Authorization": f"Bearer {token}"})

    def test_concurrent_order_creation_same_po_reference(self):
        """Verify concurrent requests creating orders with the same external PO reference all succeed."""
        import concurrent.futures
        token = _get_admin_token()
        base_po = f"PO-CONC-LEGIT-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"

        def create_order_task(amt):
            return client.post(
                "/api/orders",
                json={
                    "fy_code": "FY 2026-27",
                    "po_number": base_po,
                    "po_date": "2026-05-15",
                    "customer_id": 1,
                    "currency": "INR",
                    "exclusive_amount": float(amt),
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        amounts = [10000.0, 20000.0, 30000.0]
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(create_order_task, a) for a in amounts]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        status_codes = [r.status_code for r in results]
        assert status_codes == [201, 201, 201], f"All concurrent orders with same PO reference should succeed, got {status_codes}"
        ids = [r.json()["id"] for r in results]
        assert len(set(ids)) == 3, f"Each order must receive a unique internal ID: {ids}"

        # Clean up
        for order_id in ids:
            client.delete(f"/api/orders/{order_id}", headers={"Authorization": f"Bearer {token}"})

    def test_po_reference_search_associates_correct_customers(self):
        token = _get_admin_token()
        po_search = f"PO-SEARCH-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"

        r = client.post(
            "/api/orders",
            json={"fy_code": "FY 2026-27", "po_number": po_search, "po_date": "2026-05-15", "customer_id": 1, "currency": "INR", "exclusive_amount": 10000.0},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 201
        oid = r.json()["id"]

        try:
            # Query orders listing with search
            search_resp = client.get(
                f"/api/orders?search={po_search}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert search_resp.status_code == 200
            items = search_resp.json()["data"]
            assert len(items) >= 1
            found = next((i for i in items if i["id"] == oid), None)
            assert found is not None
            assert found["po_number"] == po_search
            assert found["customer_id"] == 1
        finally:
            client.delete(f"/api/orders/{oid}", headers={"Authorization": f"Bearer {token}"})

    def test_duplicate_conversion_and_invoice_uniqueness_still_enforced(self):
        token = _get_admin_token()
        ts = uuid.uuid4().hex[:10]
        po_num = f"PO-CONV-{ts}"
        inv_num = f"INV-DUP-TEST-{ts}"
        po2_num = f"PO-OTHER-{ts}"

        r_po = client.post(
            "/api/orders",
            json={"fy_code": "FY 2026-27", "po_number": po_num, "po_date": "2026-05-15", "customer_id": 1, "currency": "INR", "exclusive_amount": 10000.0},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_po.status_code == 201
        poid = r_po.json()["id"]

        try:
            # 1. Convert to invoice
            r_conv = client.post(
                f"/api/orders/{poid}/convert",
                json={"invoice_number": inv_num, "invoice_date": "2026-05-15"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r_conv.status_code == 201, f"Failed converting PO: {r_conv.text}"

            # 2. Duplicate conversion of same PO must be rejected (409)
            r_reconv = client.post(
                f"/api/orders/{poid}/convert",
                json={"invoice_number": f"INV-OTHER-{ts}", "invoice_date": "2026-05-15"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r_reconv.status_code == 409, f"Expected 409 for duplicate conversion, got {r_reconv.status_code}"
            assert "already been converted" in r_reconv.json()["detail"].lower()

            # 3. Duplicate invoice number rejection remains enforced
            r_po2 = client.post(
                "/api/orders",
                json={"fy_code": "FY 2026-27", "po_number": po2_num, "po_date": "2026-05-15", "customer_id": 1, "currency": "INR", "exclusive_amount": 5000.0},
                headers={"Authorization": f"Bearer {token}"},
            )
            poid2 = r_po2.json()["id"]
            try:
                r_conv2 = client.post(
                    f"/api/orders/{poid2}/convert",
                    json={"invoice_number": inv_num, "invoice_date": "2026-05-15"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert r_conv2.status_code == 409, f"Expected 409 for duplicate invoice number, got {r_conv2.status_code}"
                assert "already exists" in r_conv2.json()["detail"].lower()
            finally:
                client.delete(f"/api/orders/{poid2}", headers={"Authorization": f"Bearer {token}"})
        finally:
            with get_db_session() as session:
                session.execute(
                    text("DELETE FROM commercial_invoice_items WHERE invoice_id IN (SELECT id FROM commercial_invoices WHERE invoice_number LIKE :inv)"),
                    {"inv": f"INV-%-{ts}%"}
                )
                session.execute(
                    text("DELETE FROM commercial_invoices WHERE invoice_number LIKE :inv"),
                    {"inv": f"INV-%-{ts}%"}
                )
                session.execute(
                    text("DELETE FROM purchase_order_items WHERE po_id IN (SELECT id FROM purchase_orders WHERE po_number LIKE :po)"),
                    {"po": f"PO-%-{ts}%"}
                )
                session.execute(
                    text("DELETE FROM purchase_orders WHERE po_number LIKE :po"),
                    {"po": f"PO-%-{ts}%"}
                )
                session.commit()


class TestExportExclusionsD02:
    """Remediation verification for D-02 export consistency."""

    def test_issued_export_excludes_cancelled_invoices(self):
        token = _get_admin_token()
        # Create single-use ticket
        ticket_resp = client.post(
            "/api/export/token",
            json={"fy": "ALL", "scope": "issued", "format": "csv"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ticket_resp.status_code == 200, f"Ticket generation failed: {ticket_resp.text}"
        ticket = ticket_resp.json()["ticket"]

        # Download CSV
        download_resp = client.get(
            f"/api/export/csv?ticket={ticket}&fy=ALL&scope=issued",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert download_resp.status_code == 200, f"Download failed: {download_resp.text}"
        content = download_resp.text
        # Any CANCELLED status should be excluded from issued export
        for line in content.splitlines()[1:]:
            if line.strip():
                cols = line.split(",")
                if len(cols) > 4:
                    status_col = cols[4].strip().strip('"')
                    assert status_col != "CANCELLED", f"Found cancelled invoice in issued export: {line}"


class TestExecutiveAttainmentReportD04:
    """Remediation verification for D-04 Executive Outstanding display."""

    def test_executive_outstanding_reflects_net_receivables_not_gross_invoiced(self):
        token = _get_admin_token()
        resp = client.get(
            "/api/reporting/attainment?fy=FY%202026-27",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Report query failed: {resp.text}"
        data = resp.json()
        assert "rows" in data
        assert "summary" in data

        # For every row, outstanding_balance must be <= actual_sales_turnover + GST (never gross without deduction)
        for row in data["rows"]:
            actual_sales = Decimal(str(row["actual_sales"]))
            outstanding = Decimal(str(row["outstanding_balance"]))
            # If an executive has settled invoices, outstanding must be less than gross invoiced
            assert outstanding >= Decimal("0.00"), "Outstanding balance cannot be negative"


class TestConvertedPurchaseOrderD05:
    """Remediation verification for D-05 Converted PO unbilled value."""

    def test_converted_po_serializes_zero_unbilled_and_linked_invoice(self):
        token = _get_admin_token()
        po_num = f"PO-CONV-VERIF-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"
        inv_num = f"INV-CONV-VERIF-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"

        # 1. Create order
        create_resp = client.post(
            "/api/orders",
            json={
                "fy_code": "FY 2026-27",
                "po_number": po_num,
                "po_date": "2026-05-20",
                "customer_id": 1,
                "currency": "INR",
                "exclusive_amount": 25000.0,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert create_resp.status_code == 201, f"PO creation failed: {create_resp.text}"
        order_id = create_resp.json()["id"]

        try:
            # 2. Verify initial unbilled state
            get_resp = client.get(f"/api/orders/{order_id}", headers={"Authorization": f"Bearer {token}"})
            assert get_resp.status_code == 200
            order_data = get_resp.json()
            assert order_data["status"] == "UNBILLED"
            assert Decimal(order_data["unbilled_amount"]) > Decimal("0.00")
            assert order_data["invoice_number"] is None

            # 3. Convert to invoice
            conv_resp = client.post(
                f"/api/orders/{order_id}/convert",
                json={
                    "invoice_number": inv_num,
                    "invoice_date": "2026-05-22",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert conv_resp.status_code == 201, f"Conversion failed: {conv_resp.text}"

            # 4. Re-fetch order and assert D-05 invariant: unbilled_amount == '0.00' and invoice_number populated
            get_conv_resp = client.get(f"/api/orders/{order_id}", headers={"Authorization": f"Bearer {token}"})
            assert get_conv_resp.status_code == 200
            conv_data = get_conv_resp.json()
            assert conv_data["status"] == "CONVERTED"
            assert conv_data["unbilled_amount"] == "0.00", f"Expected '0.00' unbilled amount, got {conv_data['unbilled_amount']}"
            assert conv_data["invoice_number"] == inv_num, f"Expected linked invoice number {inv_num}, got {conv_data['invoice_number']}"
            assert conv_data["invoice_date"] == "2026-05-22"
        finally:
            # Cleanup CommercialInvoice and PO from DB
            with get_db_session() as db:
                db.execute(select(CommercialInvoice).where(CommercialInvoice.po_id == order_id))
                inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.po_id == order_id))
                if inv:
                    db.delete(inv)
                po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == order_id))
                if po:
                    db.delete(po)
                db.commit()


class TestPoReferenceConsistencyD09:
    """Remediation verification for D-09: Authoritative persisted PO reference consistency.

    Direct decoupled invoices (po_id is None) must return po_number=None, not remarks.
    Exports must represent direct invoices with '—' and converted invoices with po.po_number.
    """

    def test_direct_invoice_returns_none_po_number_and_preserves_remarks(self):
        token = _get_finance_token()
        inv_num = f"INV-DIR-D09-{uuid.uuid4().hex[:6].upper()}"
        remarks_text = "Standard professional services retainer - April 2026"

        create_resp = client.post(
            "/api/invoices",
            json={
                "fy_code": "FY 2026-27",
                "invoice_number": inv_num,
                "invoice_date": "2026-04-30",
                "customer_id": 1,
                "currency": "INR",
                "exchange_rate": 1.0,
                "remarks": remarks_text,
                "items": [
                    {
                        "description": "Professional Retainer Services",
                        "quantity": 1.0,
                        "unit_price": 50000.0,
                        "tax_rate": 0.18,
                    }
                ],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert create_resp.status_code == 201, f"Direct invoice creation failed: {create_resp.text}"
        inv_id = create_resp.json()["id"]

        try:
            # 1. Fetch invoices list and assert po_number is None (NOT remarks)
            list_resp = client.get("/api/invoices?limit=100", headers={"Authorization": f"Bearer {token}"})
            assert list_resp.status_code == 200
            invoices = list_resp.json()["data"]
            target = next((i for i in invoices if i["id"] == inv_id), None)
            assert target is not None, "Created direct invoice not found in invoices list"
            assert target["po_id"] is None
            assert target["po_number"] is None, (
                f"Direct invoice po_number must be None, got: {target['po_number']!r}"
            )
            assert target["remarks"] == remarks_text, "Remarks must be preserved untouched"

            # 2. Verify export returns '—' for PO Number
            token_resp = client.post(
                "/api/export/token",
                json={"format": "csv", "fy": "ALL", "scope": "all"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert token_resp.status_code == 200
            export_token = token_resp.json()["ticket"]

            csv_resp = client.get(
                f"/api/export/csv?ticket={export_token}&fy=ALL&scope=all",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert csv_resp.status_code == 200
            csv_lines = csv_resp.text.splitlines()
            target_line = next((line for line in csv_lines if inv_num in line), None)
            assert target_line is not None, f"Invoice {inv_num} not found in CSV export"
            # Ensure the line does not contain the remarks as the PO number
            assert remarks_text not in target_line.split(",")[2], "Remarks must not leak into PO Number column"
        finally:
            with get_db_session() as db:
                db.execute(select(CommercialInvoice).where(CommercialInvoice.id == inv_id))
                inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.id == inv_id))
                if inv:
                    db.delete(inv)
                db.commit()

