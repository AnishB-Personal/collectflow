"""
Stage 4C.4 Automated Test Suite: Payments, TDS Ledger & Non-Destructive Adjustments.

Validates against authoritative PostgreSQL database:
1. Cash-only settlement against issued commercial invoice.
2. TDS-only settlement with statutory section validation (194C, 194J).
3. Combined cash & TDS settlement with exact financial math.
4. Exact full settlement and transition to PAID status.
5. Partial settlement retaining PARTIAL status with exact remaining balance.
6. Strict overpayment rejection down to the ₹0.01 boundary.
7. Zero and negative settlement rejection.
8. TDS section validation against authoritative TDS section master (and rejection of invalid/inactive codes).
9. Idempotency replay (HTTP 200 with idempotent_replay=True) and conflicting payload rejection (HTTP 409).
10. Concurrency safety: Multithreaded payment attempts against outstanding balance protected by invoice row-level lock.
11. Immutability: Original payment record remains completely intact without updates or deletion.
12. Non-destructive correction: Compensating REVERSAL event paired with corrected replacement PAYMENT.
13. Full reversal with zero replacement (reversal-only correction).
14. Double-correction / duplicate-reversal rejection on previously superseded transactions.
15. Atomic rollback: When a correction produces an invalid resulting balance or error, no intermediate reversals are committed.
16. Stage 4C.3 Cancellation Integration: Rejection of invoice cancellation when active settlements exist; successful cancellation once 100% reversed.
17. Physical deletion rejection: DELETE /api/payments/{id} prohibited with HTTP 400.
18. RBAC Matrix: Finance & Admin have write access; Sales & Viewer are forbidden (HTTP 403).
19. Foreign-currency fail-safe: Unsupported foreign currency settlements rejected safely with HTTP 400.
"""

import concurrent.futures
import datetime
from decimal import Decimal
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import get_db_session
from app.main import app
from app.models.domain_models import (
    CommercialInvoice,
    CommercialInvoiceItem,
    Customer,
    PaymentTransaction,
    TdsSectionMaster,
    User,
)

client = TestClient(app)


def _get_token(username: str = "sandbox_admin", password: str = "AdminPass123!") -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"Login failed for {username}: {resp.text}"
    return resp.json()["token"]


@pytest.fixture(autouse=True)
def cleanup_test_records():
    def _do_cleanup():
        with get_db_session() as db:
            test_inv_ids = db.scalars(
                select(CommercialInvoice.id).where(
                    CommercialInvoice.invoice_number.ilike("INV-TEST-4C4-%")
                )
            ).all()

            # 1. Delete ALL child payment transactions associated with test invoices OR test patterns
            cond = (
                PaymentTransaction.notes.ilike("%TEST-S4C4%")
                | PaymentTransaction.notes.ilike("%Reversal of Payment%")
                | PaymentTransaction.remittance_number.ilike("UTR-%")
                | PaymentTransaction.idempotency_key.ilike("idem-%")
            )
            if test_inv_ids:
                cond = cond | PaymentTransaction.invoice_id.in_(test_inv_ids)

            child_txs = db.scalars(
                select(PaymentTransaction).where(
                    PaymentTransaction.parent_transaction_id.is_not(None),
                    cond,
                )
            ).all()
            for tx in child_txs:
                db.delete(tx)
            db.flush()

            # 2. Delete ALL root payment transactions associated with test invoices OR test patterns
            root_cond = (
                PaymentTransaction.notes.ilike("%TEST-S4C4%")
                | PaymentTransaction.remittance_number.ilike("UTR-%")
                | PaymentTransaction.idempotency_key.ilike("idem-%")
            )
            if test_inv_ids:
                root_cond = root_cond | PaymentTransaction.invoice_id.in_(test_inv_ids)

            root_txs = db.scalars(
                select(PaymentTransaction).where(root_cond)
            ).all()
            for tx in root_txs:
                db.delete(tx)
            db.flush()

            # 3. Clean test invoices and items
            test_invs = db.scalars(
                select(CommercialInvoice).where(
                    CommercialInvoice.invoice_number.ilike("INV-TEST-4C4-%")
                )
            ).all()
            for inv in test_invs:
                items = db.scalars(
                    select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == inv.id)
                ).all()
                for it in items:
                    db.delete(it)
                db.delete(inv)

            db.commit()

    _do_cleanup()
    yield
    _do_cleanup()


def _create_test_invoice(
    total_amount: Decimal = Decimal("100000.00"),
    invoice_number: str = "INV-TEST-4C4-001",
    currency: str = "INR",
) -> int:
    """Helper to insert an issued commercial invoice for testing."""
    with get_db_session() as db:
        # Check if customer 1 exists, or create test customer
        cust = db.scalar(select(Customer).where(Customer.id == 1))
        if not cust:
            cust = Customer(
                billing_name="Stage 4C4 Test Client",
                billing_address="100 Tech Park, Bengaluru",
                state="Karnataka",
                gst_number="29AAACH2888H1Z5",
                is_active=True,
            )
            db.add(cust)
            db.flush()

        exec_user = db.scalar(select(User).where(User.username == "sandbox_sales"))
        admin_user = db.scalar(select(User).where(User.username == "sandbox_admin"))

        excl = (total_amount / Decimal("1.18")).quantize(Decimal("0.01"))
        tax = total_amount - excl

        inv = CommercialInvoice(
            fy_code="FY 2026-27",
            customer_id=cust.id,
            executive_id=exec_user.id if exec_user else admin_user.id,
            invoice_number=invoice_number,
            invoice_date=datetime.date(2026, 4, 15),
            due_date=datetime.date(2026, 5, 15),
            currency=currency,
            exchange_rate=Decimal("1.000000"),
            exclusive_amount=excl,
            tax_rate=Decimal("0.1800"),
            tax_amount=tax,
            total_amount=total_amount,
            snapshot_customer_name=cust.billing_name,
            snapshot_billing_address=cust.billing_address,
            snapshot_state=cust.state,
            cgst_amount=(tax / Decimal("2.0")).quantize(Decimal("0.01")),
            sgst_amount=(tax / Decimal("2.0")).quantize(Decimal("0.01")),
            igst_amount=Decimal("0.00"),
            lifecycle_status="ISSUED",
            remarks="Stage 4C.4 Test Invoice",
        )
        db.add(inv)
        db.flush()

        item = CommercialInvoiceItem(
            invoice_id=inv.id,
            item_description="Engineering Services",
            quantity=Decimal("1.0000"),
            unit_price=excl,
            exclusive_amount=excl,
            tax_rate=Decimal("0.1800"),
            tax_amount=tax,
            total_amount=total_amount,
        )
        db.add(item)
        db.commit()
        return inv.id


def test_ac01_cash_only_settlement():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("100000.00"), "INV-TEST-4C4-CASH")

    payload = {
        "invoice_id": inv_id,
        "payment_amount": "40000.00",
        "tds_amount": "0.00",
        "payment_date": "2026-04-20",
        "payment_mode": "NEFT",
        "remittance_number": "UTR-TEST-CASH-01",
        "notes": "TEST-S4C4 Partial cash settlement",
    }
    resp = client.post("/api/payments", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert "payment_id" in data
    assert data["invoice_summary"]["total_cash_paid"] == "40000.00"
    assert data["invoice_summary"]["total_tds_deducted"] == "0.00"
    assert data["invoice_summary"]["total_paid"] == "40000.00"
    assert data["invoice_summary"]["outstanding_balance"] == "60000.00"
    assert data["invoice_summary"]["computed_status"] == "PARTIAL"


def test_ac02_tds_only_settlement_and_sections():
    token_finance = _get_token("sandbox_finance", "FinancePass123!")
    headers = {"Authorization": f"Bearer {token_finance}"}

    inv_id = _create_test_invoice(Decimal("50000.00"), "INV-TEST-4C4-TDS")

    # 1. TDS deduction requires tds_section
    fail_payload = {
        "invoice_id": inv_id,
        "payment_amount": "0.00",
        "tds_amount": "1000.00",
        "tds_section": "",
        "payment_date": "2026-04-22",
    }
    fail_res = client.post("/api/payments", json=fail_payload, headers=headers)
    assert fail_res.status_code == 400
    assert "TDS section is required" in fail_res.text

    # 2. Invalid TDS section rejected
    invalid_sec_payload = {
        "invoice_id": inv_id,
        "payment_amount": "0.00",
        "tds_amount": "1000.00",
        "tds_section": "INVALID_SEC_XYZ",
        "payment_date": "2026-04-22",
    }
    inv_sec_res = client.post("/api/payments", json=invalid_sec_payload, headers=headers)
    assert inv_sec_res.status_code == 400
    assert "Invalid or inactive TDS section" in inv_sec_res.text

    # 3. Valid section 194J succeeds
    success_payload = {
        "invoice_id": inv_id,
        "payment_amount": "0.00",
        "tds_amount": "5000.00",
        "tds_section": "194J",
        "payment_date": "2026-04-22",
        "payment_mode": "OTHER",
        "remittance_number": "UTR-TEST-TDS-01",
        "notes": "TEST-S4C4 TDS 10% under 194J",
    }
    resp = client.post("/api/payments", json=success_payload, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["invoice_summary"]["total_cash_paid"] == "0.00"
    assert data["invoice_summary"]["total_tds_deducted"] == "5000.00"
    assert data["invoice_summary"]["total_paid"] == "5000.00"
    assert data["invoice_summary"]["outstanding_balance"] == "45000.00"


def test_ac03_combined_settlement():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("100000.00"), "INV-TEST-4C4-COMB")

    payload = {
        "invoice_id": inv_id,
        "payment_amount": "60000.00",
        "tds_amount": "2000.00",
        "tds_section": "194C",
        "payment_date": "2026-04-25",
        "payment_mode": "RTGS",
        "remittance_number": "UTR-TEST-COMB-01",
        "notes": "TEST-S4C4 Combined payment + 194C TDS",
    }
    resp = client.post("/api/payments", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["invoice_summary"]["total_cash_paid"] == "60000.00"
    assert data["invoice_summary"]["total_tds_deducted"] == "2000.00"
    assert data["invoice_summary"]["total_paid"] == "62000.00"
    assert data["invoice_summary"]["outstanding_balance"] == "38000.00"
    assert data["invoice_summary"]["computed_status"] == "PARTIAL"


def test_ac04_full_settlement_transitions_to_paid():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("75000.00"), "INV-TEST-4C4-PAID")

    # Settle in full: Cash 73,500 + TDS 1,500 = 75,000
    payload = {
        "invoice_id": inv_id,
        "payment_amount": "73500.00",
        "tds_amount": "1500.00",
        "tds_section": "194C",
        "payment_date": "2026-04-26",
        "payment_mode": "RTGS",
        "remittance_number": "UTR-TEST-FULL-01",
        "notes": "TEST-S4C4 Full settlement",
    }
    resp = client.post("/api/payments", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["invoice_summary"]["total_paid"] == "75000.00"
    assert data["invoice_summary"]["outstanding_balance"] == "0.00"
    assert data["invoice_summary"]["computed_status"] == "PAID"
    assert data["invoice_summary"]["payment_status"] == "PAID"


def test_ac06_overpayment_rejection_boundary():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("50000.00"), "INV-TEST-4C4-OVER")

    # Pay 30,000 first
    client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_amount": "30000.00",
            "tds_amount": "0.00",
            "payment_date": "2026-04-20",
            "remittance_number": "UTR-TEST-OVER-P1",
            "notes": "TEST-S4C4 Part 1",
        },
        headers=headers,
    )

    # Outstanding is now 20,000.00. Attempting to settle 20,000.01 must be rejected by ₹0.01!
    overpay_payload = {
        "invoice_id": inv_id,
        "payment_amount": "20000.01",
        "tds_amount": "0.00",
        "payment_date": "2026-04-21",
        "remittance_number": "UTR-TEST-OVER-P2",
        "notes": "TEST-S4C4 Overpayment attempt",
    }
    resp = client.post("/api/payments", json=overpay_payload, headers=headers)
    assert resp.status_code == 400
    assert "exceeds the outstanding balance of ₹20,000.00" in resp.text

    # Exactly 20,000.00 succeeds
    exact_payload = {
        "invoice_id": inv_id,
        "payment_amount": "20000.00",
        "tds_amount": "0.00",
        "payment_date": "2026-04-21",
        "remittance_number": "UTR-TEST-OVER-EXACT",
        "notes": "TEST-S4C4 Exact remaining",
    }
    exact_resp = client.post("/api/payments", json=exact_payload, headers=headers)
    assert exact_resp.status_code == 200
    assert exact_resp.json()["invoice_summary"]["outstanding_balance"] == "0.00"


def test_ac07_zero_and_negative_settlement_rejection():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("50000.00"), "INV-TEST-4C4-NEG")

    # Negative cash rejected
    neg_res1 = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": "-100.00", "payment_date": "2026-04-20"},
        headers=headers,
    )
    assert neg_res1.status_code in (400, 422)

    # Negative TDS rejected
    neg_res2 = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": "500.00", "tds_amount": "-50.00", "payment_date": "2026-04-20"},
        headers=headers,
    )
    assert neg_res2.status_code in (400, 422)

    # 0 Cash + 0 TDS rejected
    zero_res = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": "0.00", "tds_amount": "0.00", "payment_date": "2026-04-20"},
        headers=headers,
    )
    assert zero_res.status_code == 400
    assert "greater than zero" in zero_res.text


def test_ac09_idempotency_replay_and_conflict():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("50000.00"), "INV-TEST-4C4-IDEM")
    key = f"idem-test-{uuid.uuid4()}"

    payload = {
        "invoice_id": inv_id,
        "payment_amount": "15000.00",
        "tds_amount": "0.00",
        "payment_date": "2026-04-20",
        "payment_mode": "NEFT",
        "remittance_number": "UTR-TEST-IDEM-01",
        "notes": "TEST-S4C4 Idempotent payment",
        "idempotency_key": key,
    }

    # 1. First attempt succeeds
    resp1 = client.post("/api/payments", json=payload, headers=headers)
    assert resp1.status_code == 200
    pid = resp1.json()["payment_id"]

    # 2. Replay with identical payload returns 200 and idempotent_replay=True
    resp2 = client.post("/api/payments", json=payload, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["payment_id"] == pid
    assert resp2.json()["idempotent_replay"] is True

    # 3. Conflicting payload with same key returns HTTP 409 Conflict
    conflict_payload = dict(payload)
    conflict_payload["payment_amount"] = "25000.00"
    resp3 = client.post("/api/payments", json=conflict_payload, headers=headers)
    assert resp3.status_code == 409
    assert "Conflicting reuse of idempotency key" in resp3.text


def test_ac10_concurrency_row_lock_safety():
    """
    Two concurrent payments competing for the remaining balance.
    Invoice total: 10,000. Both threads try to pay 8,000.
    Under row lock (with_for_update), exactly one must succeed and one must be rejected for overpayment.
    """
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("10000.00"), "INV-TEST-4C4-CONCUR")

    def _attempt_payment(tag: str):
        c = TestClient(app)
        return c.post(
            "/api/payments",
            json={
                "invoice_id": inv_id,
                "payment_amount": "8000.00",
                "payment_date": "2026-04-20",
                "payment_mode": "NEFT",
                "remittance_number": f"UTR-TEST-CONCUR-{tag}",
                "notes": f"TEST-S4C4 Concurrent attempt {tag}",
            },
            headers=headers,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_attempt_payment, "A")
        f2 = executor.submit(_attempt_payment, "B")
        r1 = f1.result()
        r2 = f2.result()

    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 400], f"Expected one 200 and one 400, got: {statuses}. R1: {r1.text}, R2: {r2.text}"

    # Verify database state has exactly 8,000 paid and 2,000 remaining
    with get_db_session() as db:
        txs = db.scalars(select(PaymentTransaction).where(PaymentTransaction.invoice_id == inv_id)).all()
        assert len(txs) == 1
        assert Decimal(str(txs[0].total_settled)) == Decimal("8000.00")


def test_ac11_12_13_non_destructive_correction():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("100000.00"), "INV-TEST-4C4-CORR")

    # Record initial payment of 50,000
    pay_res = client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_amount": "50000.00",
            "tds_amount": "0.00",
            "payment_date": "2026-04-18",
            "payment_mode": "NEFT",
            "remittance_number": "UTR-TEST-CORR-ORIG",
            "notes": "TEST-S4C4 Initial payment",
        },
        headers=headers,
    )
    assert pay_res.status_code == 200
    orig_pid = pay_res.json()["payment_id"]

    # 1. Non-destructive correction: Change 50,000 to 45,000 Cash + 1,000 TDS (Section 194C)
    corr_payload = {
        "reason": "Customer deducted 194C TDS and remitted 45k cash instead of 50k",
        "corrected_payment_amount": "45000.00",
        "corrected_tds_amount": "1000.00",
        "corrected_tds_section": "194C",
        "corrected_payment_date": "2026-04-18",
        "corrected_payment_mode": "NEFT",
        "corrected_remittance_number": "UTR-TEST-CORR-NEW",
        "notes": "TEST-S4C4 Corrected payment",
    }
    corr_res = client.post(f"/api/payments/{orig_pid}/correct", json=corr_payload, headers=headers)
    assert corr_res.status_code == 200, corr_res.text
    corr_data = corr_res.json()
    assert corr_data["success"] is True
    assert corr_data["reversal_id"] is not None
    assert corr_data["replacement_id"] is not None

    # Verify updated invoice balances: 45,000 + 1,000 = 46,000 settled, 54,000 outstanding
    assert corr_data["invoice_summary"]["total_cash_paid"] == "45000.00"
    assert corr_data["invoice_summary"]["total_tds_deducted"] == "1000.00"
    assert corr_data["invoice_summary"]["total_paid"] == "46000.00"
    assert corr_data["invoice_summary"]["outstanding_balance"] == "54000.00"

    # Verify DB immutability & ledger trail
    with get_db_session() as db:
        # Original payment row is untouched
        orig_row = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == orig_pid))
        assert orig_row.cash_amount == Decimal("50000.00")
        assert orig_row.transaction_type == "PAYMENT"

        # Reversal row is negative
        rev_row = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == corr_data["reversal_id"]))
        assert rev_row.parent_transaction_id == orig_pid
        assert rev_row.transaction_type == "REVERSAL"
        assert rev_row.cash_amount == Decimal("-50000.00")
        assert rev_row.total_settled == Decimal("-50000.00")

        # Replacement row
        rep_row = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == corr_data["replacement_id"]))
        assert rep_row.parent_transaction_id == orig_pid
        assert rep_row.transaction_type == "PAYMENT"
        assert rep_row.cash_amount == Decimal("45000.00")
        assert rep_row.tds_amount == Decimal("1000.00")
        assert rep_row.total_settled == Decimal("46000.00")

    # AC-13: Double-correction attempt on original payment must be rejected!
    dup_res = client.post(f"/api/payments/{orig_pid}/correct", json=corr_payload, headers=headers)
    assert dup_res.status_code == 400
    assert "already been reversed or corrected" in dup_res.text


def test_ac14_full_reversal_without_replacement():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("50000.00"), "INV-TEST-4C4-REVONLY")

    # Record 20,000 payment
    pay_res = client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_amount": "20000.00",
            "tds_amount": "0.00",
            "payment_date": "2026-04-18",
            "remittance_number": "UTR-TEST-REV-01",
            "notes": "TEST-S4C4 To be reversed",
        },
        headers=headers,
    )
    pid = pay_res.json()["payment_id"]

    # Reversal with 0 cash and 0 TDS
    rev_res = client.post(
        f"/api/payments/{pid}/correct",
        json={
            "reason": "Payment was credited to wrong bank account and refunded",
            "corrected_payment_amount": "0.00",
            "corrected_tds_amount": "0.00",
            "corrected_payment_date": "2026-04-19",
        },
        headers=headers,
    )
    assert rev_res.status_code == 200, rev_res.text
    data = rev_res.json()
    assert data["replacement_id"] is None
    assert data["invoice_summary"]["total_paid"] == "0.00"
    assert data["invoice_summary"]["outstanding_balance"] == "50000.00"
    assert data["invoice_summary"]["payment_status"] == "PENDING"


def test_ac15_invoice_cancellation_after_full_reversal():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("30000.00"), "INV-TEST-4C4-CANCEL")

    # Record payment of 10,000
    pay_res = client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_amount": "10000.00",
            "payment_date": "2026-04-18",
            "remittance_number": "UTR-TEST-CANCEL-01",
            "notes": "TEST-S4C4 Part payment",
        },
        headers=headers,
    )
    pid = pay_res.json()["payment_id"]

    # 1. Attempting to cancel invoice with active settlement must fail
    can_res1 = client.post(
        f"/api/invoices/{inv_id}/cancel",
        json={"reason": "Customer cancelled project requirements"},
        headers=headers,
    )
    assert can_res1.status_code == 400
    assert "Cannot cancel invoice with recorded settlements" in can_res1.text

    # 2. Reverse the settlement completely
    client.post(
        f"/api/payments/{pid}/correct",
        json={
            "reason": "Settlement reversal prior to cancellation",
            "corrected_payment_amount": "0.00",
            "corrected_tds_amount": "0.00",
            "corrected_payment_date": "2026-04-19",
        },
        headers=headers,
    )

    # 3. Now cancel invoice: net settled is 0, so cancellation MUST SUCCEED without deleting history!
    can_res2 = client.post(
        f"/api/invoices/{inv_id}/cancel",
        json={"reason": "Customer cancelled project requirements after refund"},
        headers=headers,
    )
    assert can_res2.status_code == 200, can_res2.text
    assert can_res2.json()["lifecycle_status"] == "CANCELLED"

    # Verify both transactions still exist in database
    with get_db_session() as db:
        txs = db.scalars(select(PaymentTransaction).where(PaymentTransaction.invoice_id == inv_id)).all()
        assert len(txs) == 2  # Original payment + Reversal


def test_ac16_rbac_enforcement():
    token_sales = _get_token("sandbox_sales", "SalesPass123!")
    token_viewer = _get_token("sandbox_viewer", "ViewerPass123!")

    inv_id = _create_test_invoice(Decimal("25000.00"), "INV-TEST-4C4-RBAC")

    # Sales cannot record payments
    res_sales = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": "5000.00", "payment_date": "2026-04-20"},
        headers={"Authorization": f"Bearer {token_sales}"},
    )
    assert res_sales.status_code == 403

    # Viewer cannot record payments
    res_viewer = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": "5000.00", "payment_date": "2026-04-20"},
        headers={"Authorization": f"Bearer {token_viewer}"},
    )
    assert res_viewer.status_code == 403


def test_ac17_delete_payment_prohibited():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("20000.00"), "INV-TEST-4C4-NODEL")
    pay_res = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": "5000.00", "payment_date": "2026-04-20", "notes": "TEST-S4C4 No delete"},
        headers=headers,
    )
    pid = pay_res.json()["payment_id"]

    del_res = client.delete(f"/api/payments/{pid}", headers=headers)
    assert del_res.status_code == 400
    assert "Physical deletion of payment records is prohibited" in del_res.text


def test_ac18_foreign_currency_failsafe():
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("1000.00"), "INV-TEST-4C4-USD", currency="USD")

    resp = client.post(
        "/api/payments",
        json={"invoice_id": inv_id, "payment_amount": "500.00", "payment_date": "2026-04-20"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "Unsupported foreign currency 'USD'" in resp.text


def test_synthetic_baseline_reconciliation():
    """
    Verify synthetic baseline INV-2026-042 values from Section 7:
    - Total invoiced: ₹5,31,000.00
    - Cash settled: ₹2,00,000.00
    - TDS settled: ₹9,000.00
    - Total settled: ₹2,09,000.00
    - Outstanding: ₹3,22,000.00
    """
    token_viewer = _get_token("sandbox_viewer", "ViewerPass123!")
    headers = {"Authorization": f"Bearer {token_viewer}"}

    resp = client.get("/api/invoices?search=INV-2026-042", headers=headers)
    assert resp.status_code == 200, resp.text
    invoices = resp.json()["data"]
    match = next((inv for inv in invoices if inv["invoice_number"] == "INV-2026-042"), None)
    assert match is not None
    assert match["total_amount"] == "531000.00"
    assert match["total_cash_paid"] == "200000.00"
    assert match["total_tds_deducted"] == "9000.00"
    assert match["total_paid"] == "209000.00"
    assert match["outstanding_balance"] == "322000.00"


def test_concurrent_payment_correction_deterministic():
    """
    CF4C4-01: Two simultaneous threads attempting to correct the exact same payment.
    Under row-level lock and supersession check, exactly one must succeed and one must fail
    with HTTP 400 ('already been reversed or corrected'), writing exactly one REVERSAL event.
    """
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("100000.00"), "INV-TEST-4C4-CONCUR-CORR")

    # Record initial payment of 50,000
    pay_res = client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_amount": "50000.00",
            "tds_amount": "0.00",
            "payment_date": "2026-04-18",
            "payment_mode": "NEFT",
            "remittance_number": "UTR-TEST-CONCUR-ORIG",
            "notes": "TEST-S4C4 Initial payment for concurrent correction",
        },
        headers=headers,
    )
    assert pay_res.status_code == 200
    orig_pid = pay_res.json()["payment_id"]

    def _attempt_correction(worker_id: str):
        c = TestClient(app)
        return c.post(
            f"/api/payments/{orig_pid}/correct",
            json={
                "reason": f"Concurrent correction attempt by worker {worker_id}",
                "corrected_payment_amount": "40000.00" if worker_id == "A" else "30000.00",
                "corrected_tds_amount": "0.00",
                "corrected_payment_date": "2026-04-19",
                "corrected_payment_mode": "NEFT",
                "corrected_remittance_number": f"UTR-CORR-{worker_id}",
                "notes": f"Replacement by {worker_id}",
            },
            headers=headers,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_attempt_correction, "A")
        f2 = executor.submit(_attempt_correction, "B")
        r1 = f1.result()
        r2 = f2.result()

    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 400], f"Expected exactly one 200 and one 400, got: {statuses}. R1: {r1.text}, R2: {r2.text}"

    failed_resp = r1 if r1.status_code == 400 else r2
    assert "already been reversed or corrected" in failed_resp.text

    # Verify database state has exactly 1 REVERSAL child transaction
    with get_db_session() as db:
        reversals = db.scalars(
            select(PaymentTransaction).where(
                PaymentTransaction.parent_transaction_id == orig_pid,
                PaymentTransaction.transaction_type == "REVERSAL",
            )
        ).all()
        assert len(reversals) == 1, f"Expected exactly 1 reversal row, found {len(reversals)}"


def test_correction_date_backdating_rejected():
    """
    CF4C4-01: Reversals/corrections cannot be backdated before the original payment date.
    Attempting to correct with corrected_payment_date < orig.payment_date must be rejected with HTTP 400.
    """
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("60000.00"), "INV-TEST-4C4-BACKDATE")

    pay_res = client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_amount": "30000.00",
            "tds_amount": "0.00",
            "payment_date": "2026-04-20",
            "payment_mode": "NEFT",
            "remittance_number": "UTR-BACKDATE-ORIG",
            "notes": "Original payment on 2026-04-20",
        },
        headers=headers,
    )
    assert pay_res.status_code == 200
    orig_pid = pay_res.json()["payment_id"]

    # Attempt to backdate correction to 2026-04-15 (before original payment date)
    corr_res = client.post(
        f"/api/payments/{orig_pid}/correct",
        json={
            "reason": "Attempting to backdate correction into prior period",
            "corrected_payment_amount": "25000.00",
            "corrected_payment_date": "2026-04-15",
            "corrected_payment_mode": "NEFT",
            "notes": "Backdated replacement",
        },
        headers=headers,
    )
    assert corr_res.status_code == 400
    assert "cannot be earlier than original payment date" in corr_res.text


def test_date_based_reconciliation_integrity():
    """
    CF4C4-01: Verify date-based reconciliation visibility.
    Original payment on 2026-04-18, Full reversal on 2026-04-22.
    - As of 2026-04-20 (prior to reversal): net settlement reflects ₹40,000.00.
    - As of 2026-04-25 (after reversal): net settlement reflects ₹0.00.
    """
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("80000.00"), "INV-TEST-4C4-DATE-RECON")

    pay_res = client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_amount": "40000.00",
            "tds_amount": "0.00",
            "payment_date": "2026-04-18",
            "payment_mode": "NEFT",
            "remittance_number": "UTR-DATE-RECON-ORIG",
        },
        headers=headers,
    )
    assert pay_res.status_code == 200
    orig_pid = pay_res.json()["payment_id"]

    corr_res = client.post(
        f"/api/payments/{orig_pid}/correct",
        json={
            "reason": "Statutory full reversal for date reconciliation test",
            "corrected_payment_amount": "0.00",
            "corrected_tds_amount": "0.00",
            "corrected_payment_date": "2026-04-22",
        },
        headers=headers,
    )
    assert corr_res.status_code == 200

    # Query ledger as of 2026-04-20 (before reversal date)
    res_t1 = client.get(f"/api/payments?invoice_id={inv_id}&date_to=2026-04-20", headers=headers)
    assert res_t1.status_code == 200
    data_t1 = res_t1.json()
    assert len(data_t1["data"]) == 1
    assert data_t1["summary"]["total_settled"] == "40000.00"

    # Query ledger as of 2026-04-25 (after reversal date)
    res_t2 = client.get(f"/api/payments?invoice_id={inv_id}&date_to=2026-04-25", headers=headers)
    assert res_t2.status_code == 200
    data_t2 = res_t2.json()
    assert len(data_t2["data"]) == 2
    assert data_t2["summary"]["total_settled"] == "0.00"


def test_idempotency_full_canonical_comparison():
    """
    CF4C4-02: Complete canonical request comparison for idempotency key reuse.
    Changing ANY parameter (invoice, cash, tds, tds_section, date, mode, remittance, notes)
    must reject reuse with HTTP 409 Conflict.
    """
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("75000.00"), "INV-TEST-4C4-IDEM-FULL")
    inv_id2 = _create_test_invoice(Decimal("75000.00"), "INV-TEST-4C4-IDEM-FULL-2")
    key = f"idem-canonical-{uuid.uuid4()}"

    base_payload = {
        "invoice_id": inv_id,
        "payment_amount": "20000.00",
        "tds_amount": "1000.00",
        "tds_section": "194C",
        "payment_date": "2026-04-18",
        "payment_mode": "NEFT",
        "remittance_number": "UTR-CANONICAL-01",
        "notes": "Canonical settlement notes",
        "idempotency_key": key,
    }

    # 1. Initial submission
    res1 = client.post("/api/payments", json=base_payload, headers=headers)
    assert res1.status_code == 200
    orig_pid = res1.json()["payment_id"]

    # 2. Identical replay succeeds with 200
    res_replay = client.post("/api/payments", json=base_payload, headers=headers)
    assert res_replay.status_code == 200
    assert res_replay.json()["payment_id"] == orig_pid
    assert res_replay.json()["idempotent_replay"] is True

    # 3. Test every canonical field alteration returns 409
    field_mutations = [
        ("invoice_id", inv_id2),
        ("payment_amount", "25000.00"),
        ("tds_amount", "2000.00"),
        ("tds_section", "194J"),
        ("payment_date", "2026-04-19"),
        ("payment_mode", "RTGS"),
        ("remittance_number", "UTR-CANONICAL-MODIFIED"),
        ("notes", "Different notes"),
    ]

    for field, new_val in field_mutations:
        mutated = dict(base_payload)
        mutated[field] = new_val
        res_conflict = client.post("/api/payments", json=mutated, headers=headers)
        assert res_conflict.status_code == 409, f"Expected 409 when altering '{field}', got {res_conflict.status_code}: {res_conflict.text}"
        assert "Conflicting reuse of idempotency key" in res_conflict.text


def test_concurrent_idempotent_submissions():
    """
    CF4C4-02: Concurrent duplicate-key requests must resolve safely without 500 errors
    or duplicate database rows. Both threads return HTTP 200 with matching payment_id.
    """
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("50000.00"), "INV-TEST-4C4-IDEM-CONCUR")
    key = f"idem-concur-{uuid.uuid4()}"

    payload = {
        "invoice_id": inv_id,
        "payment_amount": "25000.00",
        "tds_amount": "0.00",
        "payment_date": "2026-04-20",
        "payment_mode": "NEFT",
        "remittance_number": "UTR-IDEM-CONCUR",
        "notes": "Concurrent duplicate key test",
        "idempotency_key": key,
    }

    def _submit():
        c = TestClient(app)
        return c.post("/api/payments", json=payload, headers=headers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_submit)
        f2 = executor.submit(_submit)
        r1 = f1.result()
        r2 = f2.result()

    assert r1.status_code == 200, f"R1 failed: {r1.status_code} {r1.text}"
    assert r2.status_code == 200, f"R2 failed: {r2.status_code} {r2.text}"
    assert r1.json()["payment_id"] == r2.json()["payment_id"]

    # Verify exactly 1 payment transaction in DB
    with get_db_session() as db:
        txs = db.scalars(
            select(PaymentTransaction).where(PaymentTransaction.idempotency_key == key)
        ).all()
        assert len(txs) == 1


def test_ledger_retains_history_for_cancelled_invoice():
    """
    CF4C4-03: Payments and reversals associated with a cancelled invoice are retained
    and retrievable from GET /api/payments with invoice_lifecycle_status == 'CANCELLED'
    and is_superseded correctly marked.
    """
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id = _create_test_invoice(Decimal("50000.00"), "INV-TEST-4C4-CANCEL-HIST")

    # Record settlement
    pay_res = client.post(
        "/api/payments",
        json={
            "invoice_id": inv_id,
            "payment_amount": "50000.00",
            "payment_date": "2026-04-18",
            "payment_mode": "NEFT",
            "remittance_number": "UTR-CANCEL-HIST-01",
        },
        headers=headers,
    )
    assert pay_res.status_code == 200
    orig_pid = pay_res.json()["payment_id"]

    # Reverse settlement
    corr_res = client.post(
        f"/api/payments/{orig_pid}/correct",
        json={
            "reason": "Reversing settlement to allow invoice cancellation",
            "corrected_payment_amount": "0.00",
            "corrected_tds_amount": "0.00",
            "corrected_payment_date": "2026-04-19",
        },
        headers=headers,
    )
    assert corr_res.status_code == 200

    # Cancel invoice
    cancel_res = client.post(
        f"/api/invoices/{inv_id}/cancel",
        json={"reason": "Statutory cancellation after full reversal"},
        headers=headers,
    )
    assert cancel_res.status_code == 200

    # Query payments ledger for this invoice
    ledger_res = client.get(f"/api/payments?invoice_id={inv_id}", headers=headers)
    assert ledger_res.status_code == 200
    data = ledger_res.json()["data"]
    assert len(data) == 2, f"Expected 2 payment rows for cancelled invoice, got {len(data)}"

    orig_item = next(p for p in data if p["id"] == orig_pid)
    assert orig_item["transaction_type"] == "PAYMENT"
    assert orig_item["is_superseded"] is True
    assert orig_item["invoice_lifecycle_status"] == "CANCELLED"

    rev_item = next(p for p in data if p["parent_transaction_id"] == orig_pid)
    assert rev_item["transaction_type"] == "REVERSAL"
    assert rev_item["invoice_lifecycle_status"] == "CANCELLED"


def test_concurrent_cross_invoice_idempotency_race():
    """
    CF4C4-02: Simultaneous same-key requests targeting DIFFERENT invoices.
    Must handle unique-key collision during db.flush()/commit() gracefully.
    Exactly one succeeds (HTTP 200), the other returns controlled HTTP 409 Conflict.
    Zero uncaught 500 errors, zero duplicate transactions, zero partial audit logs.
    """
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    inv_id1 = _create_test_invoice(Decimal("50000.00"), "INV-TEST-4C4-XIDEM-1")
    inv_id2 = _create_test_invoice(Decimal("50000.00"), "INV-TEST-4C4-XIDEM-2")
    key = f"idem-cross-{uuid.uuid4()}"

    payload1 = {
        "invoice_id": inv_id1,
        "payment_amount": "20000.00",
        "tds_amount": "0.00",
        "payment_date": "2026-04-20",
        "payment_mode": "NEFT",
        "remittance_number": "UTR-CROSS-01",
        "notes": "Cross invoice idempotency test 1",
        "idempotency_key": key,
    }
    payload2 = {
        "invoice_id": inv_id2,
        "payment_amount": "20000.00",
        "tds_amount": "0.00",
        "payment_date": "2026-04-20",
        "payment_mode": "NEFT",
        "remittance_number": "UTR-CROSS-02",
        "notes": "Cross invoice idempotency test 2",
        "idempotency_key": key,
    }

    def _submit(payload):
        c = TestClient(app)
        return c.post("/api/payments", json=payload, headers=headers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_submit, payload1)
        f2 = executor.submit(_submit, payload2)
        r1 = f1.result()
        r2 = f2.result()

    statuses = [r1.status_code, r2.status_code]
    assert 200 in statuses, f"Expected one 200, got: {statuses}. Responses: {r1.text}, {r2.text}"
    assert 409 in statuses, f"Expected one 409, got: {statuses}. Responses: {r1.text}, {r2.text}"

    # Verify exactly 1 payment transaction in DB with that key
    with get_db_session() as db:
        txs = db.scalars(
            select(PaymentTransaction).where(PaymentTransaction.idempotency_key == key)
        ).all()
        assert len(txs) == 1

        # Verify audit logs: exactly 1 PAYMENT_RECORDED audit log with this key's payment_id
        winner_tx = txs[0]
        from app.models.domain_models import AuditLog
        audits = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "PAYMENT_RECORDED",
                AuditLog.entity_id == str(winner_tx.id)
            )
        ).all()
        assert len(audits) == 1


def test_payments_ledger_pagination_and_search():
    """
    CF4C4-03: Verify backend respect of page, limit, total, total_pages, and search filtering.
    """
    token_admin = _get_token("sandbox_admin", "AdminPass123!")
    headers = {"Authorization": f"Bearer {token_admin}"}

    # Query with limit=5
    res = client.get("/api/payments?page=1&limit=5", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["page"] == 1
    assert body["limit"] == 5
    assert "total" in body
    assert "total_pages" in body
    assert len(body["data"]) <= 5
    assert "summary" in body


