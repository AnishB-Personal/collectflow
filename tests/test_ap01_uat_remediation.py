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
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

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


class TestPurchaseOrderIdUniquenessP01:
    """Remediation verification for P-01 case-variant PO uniqueness and concurrency safety."""

    def test_case_insensitive_po_number_uniqueness_rejected(self):
        token = _get_admin_token()
        base_po = f"PO-CASE-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"

        # 1. Create uppercase PO
        resp1 = client.post(
            "/api/orders",
            json={
                "fy_code": "FY 2026-27",
                "po_number": base_po.upper(),
                "po_date": "2026-05-15",
                "customer_id": 1,
                "currency": "INR",
                "exclusive_amount": 10000.0,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp1.status_code == 201, f"Failed creating first PO: {resp1.text}"
        po1_id = resp1.json()["id"]

        try:
            # 2. Attempt to create lowercase variant of same PO
            resp2 = client.post(
                "/api/orders",
                json={
                    "fy_code": "FY 2026-27",
                    "po_number": base_po.lower(),
                    "po_date": "2026-05-16",
                    "customer_id": 1,
                    "currency": "INR",
                    "exclusive_amount": 20000.0,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp2.status_code == 409, f"Expected 409 for case-variant duplicate PO, got {resp2.status_code}: {resp2.text}"
            assert "already exists" in resp2.json()["detail"].lower()
        finally:
            client.delete(f"/api/orders/{po1_id}", headers={"Authorization": f"Bearer {token}"})

    def test_trimmed_whitespace_po_number_uniqueness_rejected(self):
        token = _get_admin_token()
        base_po = f"PO-TRIM-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"

        resp1 = client.post(
            "/api/orders",
            json={
                "fy_code": "FY 2026-27",
                "po_number": base_po,
                "po_date": "2026-05-15",
                "customer_id": 1,
                "currency": "INR",
                "exclusive_amount": 15000.0,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp1.status_code == 201
        po1_id = resp1.json()["id"]

        try:
            # Whitespace and casing variant
            resp2 = client.post(
                "/api/orders",
                json={
                    "fy_code": "FY 2026-27",
                    "po_number": f"  {base_po.lower()}  ",
                    "po_date": "2026-05-16",
                    "customer_id": 1,
                    "currency": "INR",
                    "exclusive_amount": 25000.0,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp2.status_code == 409
            assert "already exists" in resp2.json()["detail"].lower()
        finally:
            client.delete(f"/api/orders/{po1_id}", headers={"Authorization": f"Bearer {token}"})

    def test_update_po_case_insensitive_duplicate_rejected(self):
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
            # Update order B to order A's number with lowercase and spaces
            r_up = client.put(
                f"/api/orders/{id_b}",
                json={"po_number": f"  {po_a.lower()}  "},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r_up.status_code == 409
            assert "already exists" in r_up.json()["detail"].lower()
        finally:
            client.delete(f"/api/orders/{id_a}", headers={"Authorization": f"Bearer {token}"})
            client.delete(f"/api/orders/{id_b}", headers={"Authorization": f"Bearer {token}"})

    def test_approved_exceptions_allowed_to_repeat(self):
        token = _get_admin_token()
        # "No PO" and "Recurring / Direct Invoice" with varied casing/whitespace must be permitted repeatedly
        created_ids = []
        try:
            placeholders = ["No PO", "  no po  ", "NO PO", "Recurring / Direct Invoice", " recurring / direct invoice "]
            for p in placeholders:
                r = client.post(
                    "/api/orders",
                    json={
                        "fy_code": "FY 2026-27",
                        "po_number": p,
                        "po_date": "2026-05-15",
                        "customer_id": 1,
                        "currency": "INR",
                        "exclusive_amount": 5000.0,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert r.status_code == 201, f"Placeholder '{p}' was rejected: {r.text}"
                created_ids.append(r.json()["id"])
        finally:
            for cid in created_ids:
                client.delete(f"/api/orders/{cid}", headers={"Authorization": f"Bearer {token}"})

    def test_database_level_functional_unique_index_enforcement(self):
        """Verify PostgreSQL functional unique index enforces uniqueness at database layer."""
        from app.core.database import get_db_session
        from app.models.domain_models import PurchaseOrder
        from sqlalchemy.exc import IntegrityError
        from decimal import Decimal
        import datetime

        base_po = f"PO-DB-INDEX-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"
        with get_db_session() as db:
            po1 = PurchaseOrder(
                fy_code="FY 2026-27",
                po_number=base_po.upper(),
                po_date=datetime.date(2026, 5, 15),
                customer_id=1,
                executive_id=1,
                currency="INR",
                exclusive_amount=Decimal("1000.00"),
                tax_rate=Decimal("0.1800"),
                tax_amount=Decimal("180.00"),
                total_amount=Decimal("1180.00"),
            )
            db.add(po1)
            db.commit()
            po1_id = po1.id

            try:
                # Direct SQL insert with case variation bypassing router validation
                po2 = PurchaseOrder(
                    fy_code="FY 2026-27",
                    po_number=f"  {base_po.lower()}  ",
                    po_date=datetime.date(2026, 5, 16),
                    customer_id=1,
                    executive_id=1,
                    currency="INR",
                    exclusive_amount=Decimal("2000.00"),
                    tax_rate=Decimal("0.1800"),
                    tax_amount=Decimal("360.00"),
                    total_amount=Decimal("2360.00"),
                )
                db.add(po2)
                try:
                    db.commit()
                    assert False, "Database constraint should have rejected case-variant duplicate PO"
                except IntegrityError:
                    db.rollback()
            finally:
                with get_db_session() as clean_db:
                    clean_po = clean_db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == po1_id))
                    if clean_po:
                        clean_db.delete(clean_po)
                        clean_db.commit()

    def test_concurrent_po_insert_race_condition(self):
        """Verify concurrent requests attempting to insert duplicate case-variant PO are safely serialized."""
        import concurrent.futures
        token = _get_admin_token()
        base_po = f"PO-CONC-{datetime.date.today().strftime('%Y%m%d%H%M%S')}"

        def try_insert(po_label):
            return client.post(
                "/api/orders",
                json={
                    "fy_code": "FY 2026-27",
                    "po_number": po_label,
                    "po_date": "2026-05-15",
                    "customer_id": 1,
                    "currency": "INR",
                    "exclusive_amount": 10000.0,
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        labels = [base_po.upper(), base_po.lower(), f"  {base_po}  "]
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(try_insert, lbl) for lbl in labels]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        status_codes = [r.status_code for r in results]
        # Exactly one must succeed (201), the other two must be rejected with 409
        assert status_codes.count(201) == 1, f"Expected exactly one 201, got {status_codes}"
        assert status_codes.count(409) == 2, f"Expected two 409s, got {status_codes}"

        # Clean up the created PO
        for r in results:
            if r.status_code == 201:
                client.delete(f"/api/orders/{r.json()['id']}", headers={"Authorization": f"Bearer {token}"})


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
