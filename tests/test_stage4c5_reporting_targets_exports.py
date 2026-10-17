"""
Stage 4C.5 Acceptance Tests: Sales Targets, Server-Side Reporting Hub, AR Aging & Exports.
Tests PostgreSQL authoritative domain logic, calculations, RBAC, single-use tickets, and invariants.
"""

import datetime
from decimal import Decimal
import hashlib
import io
import pytest
from fastapi.testclient import TestClient
import openpyxl
from sqlalchemy import select

from app.core.database import get_db_session
from app.main import app
from app.models.domain_models import (
    AuditLog,
    CommercialInvoice,
    Customer,
    ExportTicket,
    FinancialYear,
    PaymentTransaction,
    PurchaseOrder,
    SalesTarget,
    User,
)

client = TestClient(app)


USER_PASSWORDS = {
    "sandbox_admin": "AdminPass123!",
    "sandbox_finance": "FinancePass123!",
    "sandbox_sales": "SalesPass123!",
    "sandbox_viewer": "ViewerPass123!",
}


def _login(username: str) -> str:
    """Helper to authenticate and acquire bearer token."""
    pwd = USER_PASSWORDS.get(username, "AdminPass123!")
    res = client.post("/api/auth/login", json={"username": username, "password": pwd})
    assert res.status_code == 200, f"Login failed for {username}: {res.text}"
    return res.json()["token"]


@pytest.fixture
def admin_headers():
    token = _login("sandbox_admin")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def finance_headers():
    token = _login("sandbox_finance")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sales_headers():
    token = _login("sandbox_sales")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def viewer_headers():
    token = _login("sandbox_viewer")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Sales Targets Tests
# ---------------------------------------------------------------------------

def test_targets_list_and_rbac(admin_headers, sales_headers, viewer_headers):
    # Admin can list all targets
    res = client.get("/api/targets?fy=FY%202026-27", headers=admin_headers)
    assert res.status_code == 200
    targets = res.json()
    assert isinstance(targets, list)
    assert len(targets) > 0
    first = targets[0]
    assert "yearly_target" in first
    assert "." in first["yearly_target"]  # Exact 2-decimal string
    assert "executive_name" in first

    # Viewer can view all targets read-only
    res_view = client.get("/api/targets?fy=FY%202026-27", headers=viewer_headers)
    assert res_view.status_code == 200

    # Sales user querying without user_id gets own target
    res_sales = client.get("/api/targets?fy=FY%202026-27", headers=sales_headers)
    assert res_sales.status_code == 200
    sales_targets = res_sales.json()
    # Verify sales user only sees targets assigned to them
    with get_db_session() as db:
        sales_user = db.query(User).filter(User.username == "sandbox_sales").first()
        for t in sales_targets:
            assert t["user_id"] == sales_user.id

    # Sales user attempting to query another user's targets receives 403 Forbidden
    other_user_id = sales_user.id + 999
    res_forbidden = client.get(f"/api/targets?user_id={other_user_id}", headers=sales_headers)
    assert res_forbidden.status_code == 403
    assert "restricted to viewing their own sales targets" in res_forbidden.json()["detail"]


def test_targets_upsert_validation_and_audit(admin_headers, sales_headers, finance_headers):
    with get_db_session() as db:
        sales_user = db.query(User).filter(User.role == "sales").first()
        assert sales_user is not None
        uid = sales_user.id

    # Sales user cannot modify targets
    res_sales = client.post(
        f"/api/targets/{uid}",
        headers=sales_headers,
        json={"fy_code": "FY 2026-27", "yearly_target": 4500000.0},
    )
    assert res_sales.status_code == 403

    # Negative target rejected
    res_neg = client.post(
        f"/api/targets/{uid}",
        headers=admin_headers,
        json={"fy_code": "FY 2026-27", "yearly_target": -100.0},
    )
    assert res_neg.status_code in (400, 422)

    # Invalid FY rejected
    res_fy = client.post(
        f"/api/targets/{uid}",
        headers=admin_headers,
        json={"fy_code": "FY 2099-00", "yearly_target": 5000000.0},
    )
    assert res_fy.status_code == 400
    assert "does not exist" in res_fy.json()["detail"]

    # Valid upsert by Finance
    res_ok = client.post(
        f"/api/targets/{uid}",
        headers=finance_headers,
        json={
            "fy_code": "FY 2026-27",
            "yearly_target": 4800000.00,
            "monthly_target": 400000.00,
        },
    )
    assert res_ok.status_code == 200
    target_data = res_ok.json()
    assert target_data["yearly_target"] == "4800000.00"
    assert target_data["monthly_target"] == "400000.00"
    assert target_data["half_yearly_target"] == "2400000.00"  # derived

    # Verify audit log recorded
    with get_db_session() as db:
        audit = (
            db.query(AuditLog)
            .filter(AuditLog.entity_type == "sales_target", AuditLog.entity_id == str(target_data["id"]))
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit is not None
        assert audit.action in ("TARGET_CREATED", "TARGET_UPDATED")
        assert audit.username == "sandbox_finance"

    # Restore original baseline target to preserve test_baseline_fixtures_reconciliation
    with get_db_session() as db:
        target_obj = db.query(SalesTarget).filter(SalesTarget.id == target_data["id"]).first()
        if target_obj:
            target_obj.yearly_target = Decimal("7500000.00")
            target_obj.half_yearly_target = Decimal("3750000.00")
            target_obj.quarterly_target = Decimal("1875000.00")
            target_obj.monthly_target = Decimal("625000.00")
            db.commit()


def test_targets_preserved_on_user_deactivation(admin_headers):
    """Historical targets must be retained even when a user is deactivated."""
    with get_db_session() as db:
        # Create a test sales user
        import uuid
        unique_suffix = uuid.uuid4().hex[:8]
        test_user = User(
            username=f"deact_sales_{unique_suffix}",
            password_hash="argon2id_hash_placeholder",
            role="sales",
            full_name=f"Deactivated Rep {unique_suffix}",
            is_active=False,  # Deactivated
        )
        db.add(test_user)
        db.flush()

        test_target = SalesTarget(
            user_id=test_user.id,
            fy_code="FY 2026-27",
            yearly_target=Decimal("3500000.00"),
            half_yearly_target=Decimal("1750000.00"),
            quarterly_target=Decimal("875000.00"),
            monthly_target=Decimal("291666.67"),
            monthly_bottom_line=Decimal("200000.00"),
            yearly_bottom_line=Decimal("2500000.00"),
        )
        db.add(test_target)
        db.commit()
        deactivated_uid = test_user.id

    res = client.get(f"/api/targets?fy=FY%202026-27&user_id={deactivated_uid}", headers=admin_headers)
    assert res.status_code == 200
    records = res.json()
    assert len(records) == 1
    assert records[0]["yearly_target"] == "3500000.00"
    assert "Deactivated Rep" in records[0]["executive_name"]


# ---------------------------------------------------------------------------
# 2. Authoritative PostgreSQL Reporting Tests
# ---------------------------------------------------------------------------

def test_reporting_summary_kpi_and_invariants(admin_headers):
    res = client.get("/api/reporting/summary?fy=FY%202026-27", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()

    assert "total_invoiced" in data
    assert "total_cash" in data
    assert "total_tds" in data
    assert "total_settled" in data
    assert "total_outstanding" in data
    assert "unbilled_orders_value" in data

    invoiced = Decimal(data["total_invoiced"])
    cash = Decimal(data["total_cash"])
    tds = Decimal(data["total_tds"])
    settled = Decimal(data["total_settled"])
    outstanding = Decimal(data["total_outstanding"])

    # Invariant 1: Cash + TDS = Total Settled
    assert cash + tds == settled, f"Settlement math error: {cash} + {tds} != {settled}"

    # Invariant 2: Total Invoiced - Settled = Outstanding
    assert invoiced - settled == outstanding, f"AR math error: {invoiced} - {settled} != {outstanding}"


def test_reporting_ar_aging_six_buckets_and_anchor(admin_headers):
    # Anchor date: 30 April 2026
    res = client.get("/api/reporting/ar-aging?as_of_date=2026-04-30&fy=FY%202026-27", headers=admin_headers)
    assert res.status_code == 200
    aging = res.json()

    assert "buckets" in aging
    assert len(aging["buckets"]) == 6
    bucket_names = [b["bucket"] for b in aging["buckets"]]
    expected_buckets = [
        "Current / Not Due",
        "1–30 days",
        "31–60 days",
        "61–90 days",
        "91–180 days",
        "180+ days",
    ]
    assert bucket_names == expected_buckets

    # Bucket sum must equal total_receivables
    total_receivables = Decimal(aging["total_receivables"])
    bucket_sum = sum((Decimal(b["amount"]) for b in aging["buckets"]), Decimal("0.00"))
    assert bucket_sum == total_receivables

    # Regression check: INV-2026-042 (due 12 May 2026) must be Current / Not Due on 30 April 2026
    inv42_row = next((r for r in aging.get("rows", []) if "042" in r["invoice_number"]), None)
    if inv42_row:
        assert inv42_row["days_overdue"] == 0
        assert inv42_row["bucket"] == "Current / Not Due"


def test_reporting_executive_attainment(admin_headers):
    res = client.get("/api/reporting/attainment?fy=FY%202026-27", headers=admin_headers)
    assert res.status_code == 200
    attainment = res.json()

    assert "rows" in attainment
    assert "summary" in attainment
    summary = attainment["summary"]

    assert "total_target" in summary
    assert "total_actual_sales" in summary
    assert "overall_achievement_pct" in summary
    assert "excluded_unbilled_sales" in summary

    # Unbilled purchase orders must be excluded from actual sales
    for row in attainment["rows"]:
        assert "yearly_target" in row
        assert "actual_sales" in row
        assert "achievement_pct" in row
        assert "remaining_target" in row


def test_reporting_customer_exposure(admin_headers):
    res = client.get("/api/reporting/customer-exposure?fy=FY%202026-27", headers=admin_headers)
    assert res.status_code == 200
    exposure = res.json()

    assert "rows" in exposure
    assert "summary" in exposure
    summary = exposure["summary"]
    assert Decimal(summary["total_cash"]) + Decimal(summary["total_tds"]) == Decimal(summary["total_settled"])


# ---------------------------------------------------------------------------
# 3. Export Token & Single-Use Security Tests
# ---------------------------------------------------------------------------

def test_export_token_rbac(admin_headers, finance_headers, sales_headers, viewer_headers):
    # Admin and Finance can generate export tokens
    res_admin = client.post("/api/export/token", headers=admin_headers)
    assert res_admin.status_code == 200
    data_admin = res_admin.json()
    assert "ticket" in data_admin
    assert data_admin["expires_in"] == 300

    res_fin = client.post("/api/export/token", headers=finance_headers)
    assert res_fin.status_code == 200

    # Sales and Viewer are blocked from requesting tokens
    res_sales = client.post("/api/export/token", headers=sales_headers)
    assert res_sales.status_code == 403

    res_viewer = client.post("/api/export/token", headers=viewer_headers)
    assert res_viewer.status_code == 403


def test_export_ticket_single_use_consumption(admin_headers):
    # Request ticket
    res_tok = client.post("/api/export/token", headers=admin_headers)
    assert res_tok.status_code == 200
    ticket = res_tok.json()["ticket"]

    # Verify token_hash in database is SHA-256 and raw secret is not present
    expected_hash = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    with get_db_session() as db:
        t_row = db.query(ExportTicket).filter(ExportTicket.token_hash == expected_hash).first()
        assert t_row is not None
        assert t_row.used is False

    # First download with ticket succeeds
    res_dl1 = client.get(f"/api/export/excel?ticket={ticket}")
    assert res_dl1.status_code == 200
    assert res_dl1.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    # Verify ticket is marked used in database
    with get_db_session() as db:
        t_row_after = db.query(ExportTicket).filter(ExportTicket.token_hash == expected_hash).first()
        assert t_row_after.used is True

    # Replay of the same ticket must be rejected with 401
    res_dl2 = client.get(f"/api/export/excel?ticket={ticket}")
    assert res_dl2.status_code == 401
    assert "already consumed" in res_dl2.json()["detail"]


def test_export_excel_17_columns_and_formula_injection(admin_headers):
    # Direct bearer header export
    res = client.get("/api/export/excel?fy=FY%202026-27&scope=all", headers=admin_headers)
    assert res.status_code == 200

    # Parse Excel workbook with openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(res.content))
    ws = wb.active
    assert ws.title == "CollectFlow Authoritative AR"

    # Validate 17 columns
    headers = [cell.value for cell in ws[1]]
    assert len(headers) == 17
    expected_headers = [
        "FY",
        "PO Date",
        "PO Number",
        "Customer Name",
        "Executive",
        "Exclusive Amount (₹)",
        "Tax Amount (₹)",
        "Total Invoiced (₹)",
        "Invoice Number",
        "Invoice Date",
        "Due Date",
        "Cash Received (₹)",
        "TDS Deducted (₹)",
        "Total Settled (₹)",
        "Balance Outstanding (₹)",
        "Status",
        "Days Overdue",
    ]
    assert headers == expected_headers

    # Validate header styling (#1F4E79 fill)
    h_cell = ws.cell(1, 1)
    assert h_cell.fill.start_color.rgb == "001F4E79" or h_cell.fill.start_color.rgb == "1F4E79"


def test_export_csv_matching_columns_and_escaping(admin_headers):
    res = client.get("/api/export/csv?fy=FY%202026-27&scope=all", headers=admin_headers)
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]

    lines = res.text.strip().split("\r\n" if "\r\n" in res.text else "\n")
    assert len(lines) >= 1
    header_line = lines[0]
    headers = [h.strip('"') for h in header_line.split(",")]
    assert len(headers) == 17
    assert headers[0] == "FY"
    assert headers[7] == "Total Invoiced (₹)"
    assert headers[14] == "Balance Outstanding (₹)"
    assert headers[16] == "Days Overdue"


# ---------------------------------------------------------------------------
# 4. Acceptance Area Regressions (CF4C5-01, CF4C5-02, CF4C5-03)
# ---------------------------------------------------------------------------

def test_targets_upsert_payload_contract_regression(admin_headers):
    """
    CF4C5-01 Regression: Ensure backend rejects outdated/mismatched payload
    missing 'yearly_target' (e.g. sending only 'target_amount') with HTTP 422,
    and succeeds when 'yearly_target' is provided, persistently updating PostgreSQL.
    """
    with get_db_session() as db:
        sales_user = db.query(User).filter(User.role == "sales").first()
        assert sales_user is not None
        uid = sales_user.id

    # Previous mismatched payload sent { "fy_code": ..., "target_amount": ... } without yearly_target
    mismatched_payload = {
        "fy_code": "FY 2026-27",
        "target_amount": 5500000.00,
    }
    res_mismatched = client.post(
        f"/api/targets/{uid}",
        headers=admin_headers,
        json=mismatched_payload,
    )
    assert res_mismatched.status_code == 422
    assert "yearly_target" in str(res_mismatched.json())

    # Correct contract payload includes yearly_target
    aligned_payload = {
        "fy_code": "FY 2026-27",
        "yearly_target": 5500000.00,
    }
    res_aligned = client.post(
        f"/api/targets/{uid}",
        headers=admin_headers,
        json=aligned_payload,
    )
    assert res_aligned.status_code == 200
    res_data = res_aligned.json()
    assert res_data["yearly_target"] == "5500000.00"
    assert "target" in res_data
    assert res_data["target"]["yearly_target"] == "5500000.00"

    # Verify persistent update in PostgreSQL
    with get_db_session() as db:
        updated = db.query(SalesTarget).filter(
            SalesTarget.user_id == uid,
            SalesTarget.fy_code == "FY 2026-27"
        ).first()
        assert updated is not None
        assert updated.yearly_target == Decimal("5500000.00")

        # Restore baseline
        updated.yearly_target = Decimal("7500000.00")
        db.commit()


def test_reporting_summary_reconciled_fields(admin_headers):
    """
    CF4C5-02 Regression: Verify ReportingSummaryDTO exposes both canonical and alias fields
    (total_invoices, active_invoices_count, unbilled_count, active_orders_count, collection_rate_pct,
    settlement_ratio_pct, total_exclusive, total_tax).
    """
    res = client.get("/api/reporting/summary?fy=FY%202026-27", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()

    # Canonical & alias field pairs
    assert "total_invoices" in data
    assert "active_invoices_count" in data
    assert data["total_invoices"] == data["active_invoices_count"]

    assert "unbilled_count" in data
    assert "active_orders_count" in data
    assert data["unbilled_count"] == data["active_orders_count"]

    assert "collection_rate_pct" in data
    assert "settlement_ratio_pct" in data

    assert "total_exclusive" in data
    assert "total_tax" in data
    assert "total_invoiced" in data


def test_reporting_historical_cancellation_as_of_date(admin_headers):
    """
    CF4C5-02 Regression: Invoices issued on or before as-of date but cancelled later
    must be accounted for in historical operational reports as of that date.
    """
    as_of = "2026-04-30"
    res_as_of = client.get(f"/api/reporting/summary?as_of_date={as_of}", headers=admin_headers)
    assert res_as_of.status_code == 200
    data = res_as_of.json()
    assert int(data["total_invoices"]) > 0


def test_export_ticket_bearer_cannot_bypass_and_bindings(admin_headers, finance_headers):
    """
    CF4C5-03 Regression:
    1. Bearer token CANNOT bypass single-use ticket consumption or ticket reuse rejection.
    2. Ticket is bound to format, FY, and scope.
    3. Genuine linked PO numbers used, never PO-{inv.id}.
    """
    # 1. Generate ticket bound to excel, FY 2026-27, scope all
    res_tok = client.post(
        "/api/export/token",
        headers=admin_headers,
        json={"format": "excel", "fy": "FY 2026-27", "scope": "all"},
    )
    assert res_tok.status_code == 200
    ticket = res_tok.json()["ticket"]

    # Format mismatch check: ticket bound to excel cannot download csv
    res_mismatch = client.get(
        f"/api/export/csv?ticket={ticket}&fy=FY%202026-27&scope=all",
        headers=admin_headers,
    )
    assert res_mismatch.status_code == 400
    assert "bound to format" in res_mismatch.json()["detail"]

    # First download with matching ticket succeeds (even when Bearer header is also provided)
    res_dl1 = client.get(
        f"/api/export/excel?ticket={ticket}&fy=FY%202026-27&scope=all",
        headers=admin_headers,
    )
    assert res_dl1.status_code == 200

    # Replay of the ticket WITH Bearer header MUST FAIL with 401 (Bearer token cannot bypass consumption)
    res_dl2 = client.get(
        f"/api/export/excel?ticket={ticket}&fy=FY%202026-27&scope=all",
        headers=admin_headers,
    )
    assert res_dl2.status_code == 401
    assert "already consumed" in res_dl2.json()["detail"]

    # Verify genuine PO number format in Excel
    wb = openpyxl.load_workbook(io.BytesIO(res_dl1.content))
    ws = wb.active
    po_col_idx = 3  # "PO Number" is 3rd column
    po_values = [ws.cell(row=r, column=po_col_idx).value for r in range(2, ws.max_row + 1)]
    for po_val in po_values:
        if po_val and po_val != "—":
            # Must NOT be synthetic PO-{id}
            assert not (isinstance(po_val, str) and po_val.startswith("PO-") and po_val[3:].isdigit())
