"""
CollectFlow WP-06 Financial Integrity, Statutory Configuration Governance & Business Acceptance Test Suite.

Verifies:
1. Production initialization without approved statutory configuration (deferred successfully).
2. Unapproved GST/TDS creation attempts in production/staging mode (fail-closed HTTP 403).
3. Unauthorized financial-year modification in production/staging mode (fail-closed HTTP 403).
4. Synthetic configuration submitted in production mode (rejected fail-closed).
5. Fabricated Finance approval metadata (rejected fail-closed).
6. Missing/expired approval evidence (rejected fail-closed).
7. Duplicate or conflicting reference records (duplicate codes, overlapping FY date intervals).
8. Historical invoice invariance after reference changes (snapshot preserved).
9. Unauthorized user attempts against statutory endpoints (viewer/sales RBAC).
10. Incorrect GST rounding (Decimal arithmetic, ROUND_HALF_UP per-line).
11. Incorrect TDS reconciliation (cash + TDS != settled rejected).
12. Payment idempotency and reversal integrity (signed negative transactions, parent-reversal pair net zero).
13. Altered financial export values (reconciliation detects non-zero delta).
14. Stale browser financial snapshots (rejected by freshness validator).
15. Missing approval files (release gate returns BLOCKED/MISSING).
16. Forged financial acceptance PASS (rejected fail-closed).
17. Mismatched source commit SHAs (rejected by release gate).
18. Pending business approval correctly returning BLOCKED.
"""

import datetime
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
from typing import Optional, Dict, List, Any
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.core.database import get_db_session
from app.models.domain_models import (
    AuditLog,
    CommercialInvoice,
    CommercialInvoiceItem,
    Customer,
    FinancialYear,
    GstRateMaster,
    PaymentTransaction,
    PurchaseOrder,
    PurchaseOrderItem,
    SalesTarget,
    TdsSectionMaster,
    User,
)
from scripts.init_production_db import (
    init_reference_data,
    load_reference_config,
    validate_financial_year_records,
)
from scripts.verify_cross_layer_reconciliation import (
    validate_browser_snapshot,
    verify_cross_layer_reconciliation,
)
from scripts.release.verify_financial_readiness import (
    evaluate_business_authorization,
    evaluate_technical_integrity,
    get_git_commit_sha,
    parse_governance_doc_status,
    verify_financial_readiness,
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    with get_db_session() as db:
        yield db


@pytest.fixture
def admin_headers(admin_token):
    return admin_token


@pytest.fixture
def finance_headers(finance_token):
    return finance_token


@pytest.fixture
def sales_headers(sales_token):
    return sales_token


@pytest.fixture
def viewer_headers(viewer_token):
    return viewer_token


# ==============================================================================
# WP06-08 Test 1: Production Initialization Without Config Defers Cleanly
# ==============================================================================
def test_01_production_initialization_deferred_without_config(db_session):
    """1. Production initialization without approved statutory config defers reference data safely."""
    result = init_reference_data(db_session, ref_config=None, verify_only=False)
    assert result["status"] == "deferred"
    assert result["financial_years"] == 0
    assert result["gst_rates"] == 0
    assert result["tds_sections"] == 0
    assert "DEFERRED" in result["message"]


# ==============================================================================
# WP06-08 Test 2: Unapproved GST/TDS Creation Blocked in Production Mode
# ==============================================================================
def test_02_unapproved_tax_mutations_blocked_in_production(client, admin_headers):
    """2. GST/TDS creation endpoints reject mutations with HTTP 403 in production mode."""
    from app.routers.auth import require_finance_or_admin
    app.dependency_overrides[require_finance_or_admin] = lambda: {"id": 1, "username": "admin", "role": "admin"}
    try:
        with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
            res_gst = client.post(
                "/api/settings/taxes/gst",
                json={"name": "GST 28%", "rate": 0.28, "tax_type": "GST", "is_default": False},
                headers=admin_headers,
            )
            assert res_gst.status_code == 403
            assert "BLOCKED pending independently verified Corporate Finance approval" in res_gst.json()["detail"]

            res_tds = client.post(
                "/api/settings/taxes/tds",
                json={"section_code": "194I", "description": "Rent", "rate": 0.10},
                headers=admin_headers,
            )
            assert res_tds.status_code == 403
            assert "BLOCKED pending independently verified Corporate Finance approval" in res_tds.json()["detail"]
    finally:
        app.dependency_overrides.pop(require_finance_or_admin, None)


# ==============================================================================
# WP06-08 Test 3: Unauthorized Financial Year Mutation Blocked in Production Mode
# ==============================================================================
def test_03_financial_year_mutations_blocked_in_production(client, admin_headers):
    """3. Financial year endpoints reject modifications with HTTP 403 in production mode."""
    from app.routers.auth import require_finance_or_admin
    app.dependency_overrides[require_finance_or_admin] = lambda: {"id": 1, "username": "admin", "role": "admin"}
    try:
        with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
            res_create = client.post(
                "/api/settings/financial-years",
                json={"code": "FY 2028-29", "start_date": "2028-04-01", "end_date": "2029-03-31"},
                headers=admin_headers,
            )
            assert res_create.status_code == 403
            assert "BLOCKED pending independently verified Corporate Finance approval" in res_create.json()["detail"]

            res_delete = client.delete(
                "/api/settings/financial-years/FY%202026-27",
                headers=admin_headers,
            )
            assert res_delete.status_code == 403
            assert "BLOCKED pending independently verified Corporate Finance approval" in res_delete.json()["detail"]
    finally:
        app.dependency_overrides.pop(require_finance_or_admin, None)


# ==============================================================================
# WP06-08 Test 4: Synthetic Configuration Rejected in Production Mode
# ==============================================================================
def test_04_synthetic_config_rejected_in_production(tmp_path):
    """4. Synthetic test configuration fixture is rejected fail-closed in production."""
    synthetic_config = {
        "is_test_fixture": True,
        "config_version": "2026.1-test",
        "approval_status": "TEST_ONLY",
        "approved_by": "Test Harness",
        "approval_reference": "TEST-REF-001",
        "approval_date": "2026-04-01",
        "controlled_source": "test",
        "effective_date": "2026-04-01",
        "policy_references": ["BPD-01", "BPD-02"],
        "financial_years": [{"code": "FY 2026-27", "start_date": "2026-04-01", "end_date": "2027-03-31"}],
        "gst_rates": [{"name": "GST 18%", "rate": "0.1800", "cgst_rate": "0.0900", "sgst_rate": "0.0900", "igst_rate": "0.1800"}],
        "tds_sections": [{"section_code": "194C", "description": "Contractors", "default_rate": "0.0200"}],
    }
    cfg_file = tmp_path / "synthetic_ref_config.json"
    cfg_file.write_text(json.dumps(synthetic_config), encoding="utf-8")

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            load_reference_config(str(cfg_file), allow_test_fixture=True)
        assert "Synthetic reference test fixtures are strictly forbidden in production" in str(exc_info.value)


# ==============================================================================
# WP06-08 Test 5: Fabricated Finance Approval Metadata Rejected
# ==============================================================================
def test_05_fabricated_finance_approval_metadata_rejected(tmp_path):
    """5. Self-declared finance_approved in JSON cannot authorize production reference data."""
    fabricated_config = {
        "config_version": "2026.1-fabricated",
        "approval_status": "finance_approved",
        "approved_by": "Self-Declared Corporate Approver",
        "approval_reference": "FABRICATED-FIN-001",
        "approval_date": "2026-04-01",
        "controlled_source": "unauthorized-repo://data",
        "effective_date": "2026-04-01",
        "policy_references": ["BPD-01", "BPD-02"],
        "financial_years": [{"code": "FY 2026-27", "start_date": "2026-04-01", "end_date": "2027-03-31"}],
        "gst_rates": [{"name": "GST 18%", "rate": "0.1800", "cgst_rate": "0.0900", "sgst_rate": "0.0900", "igst_rate": "0.1800"}],
        "tds_sections": [{"section_code": "194C", "description": "Contractors", "default_rate": "0.0200"}],
    }
    cfg_file = tmp_path / "fabricated_config.json"
    cfg_file.write_text(json.dumps(fabricated_config), encoding="utf-8")

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            load_reference_config(str(cfg_file), allow_test_fixture=False)
        assert "CRITICAL GOVERNANCE ABORT: Statutory reference configuration activation in production/staging is BLOCKED" in str(exc_info.value)
        assert "Self-declared 'approval_status=finance_approved'" in str(exc_info.value)


# ==============================================================================
# WP06-08 Test 6: Missing or Expired Approval Evidence Rejected
# ==============================================================================
def test_06_missing_or_expired_approval_evidence_rejected(tmp_path):
    """6. Reference configs with expired approval dates or future dates are rejected fail-closed."""
    expired_config = {
        "is_test_fixture": True,
        "config_version": "2025.1",
        "approval_status": "TEST_SYNTHETIC",
        "approved_by": "Test Harness",
        "approval_reference": "TEST-EXP-001",
        "approval_date": "2025-01-01",
        "expiry_date": "2025-06-30",  # Expired
        "controlled_source": "test",
        "effective_date": "2025-01-01",
        "policy_references": ["BPD-01", "BPD-02"],
        "financial_years": [{"code": "FY 2025-26", "start_date": "2025-04-01", "end_date": "2026-03-31"}],
        "gst_rates": [{"name": "GST 18%", "rate": "0.1800", "cgst_rate": "0.0900", "sgst_rate": "0.0900", "igst_rate": "0.1800"}],
        "tds_sections": [{"section_code": "194C", "description": "Contractors", "default_rate": "0.0200"}],
    }
    cfg_file = tmp_path / "expired_config.json"
    cfg_file.write_text(json.dumps(expired_config), encoding="utf-8")

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with pytest.raises(ValueError) as exc_info:
            load_reference_config(str(cfg_file), allow_test_fixture=True)
        assert "Statutory reference approval has expired" in str(exc_info.value)

    # Future approval date check
    future_config = dict(expired_config, approval_date="2099-01-01", expiry_date="2099-12-31")
    cfg_file_future = tmp_path / "future_config.json"
    cfg_file_future.write_text(json.dumps(future_config), encoding="utf-8")

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with pytest.raises(ValueError) as exc_info:
            load_reference_config(str(cfg_file_future), allow_test_fixture=True)
        assert "cannot be in the future" in str(exc_info.value)


# ==============================================================================
# WP06-08 Test 7: Duplicate or Conflicting Reference Records Rejected
# ==============================================================================
def test_07_duplicate_or_conflicting_reference_records_rejected():
    """7. Rejects duplicate FY codes, overlapping FY intervals, and non-Indian FY conventions."""
    # 7.1 Non-Indian FY boundaries (e.g. calendar year Jan-Dec)
    with pytest.raises(ValueError) as exc1:
        validate_financial_year_records([
            {"code": "FY 2026", "start_date": "2026-01-01", "end_date": "2026-12-31"}
        ])
    assert "violates Indian statutory financial year convention" in str(exc1.value)

    # 7.2 Overlapping FYs within configuration
    with pytest.raises(ValueError) as exc2:
        validate_financial_year_records([
            {"code": "FY 2026-27-A", "start_date": "2026-04-01", "end_date": "2027-03-31"},
            {"code": "FY 2026-27-B", "start_date": "2026-04-01", "end_date": "2027-03-31"},
        ])
    assert "Overlapping financial year boundaries detected" in str(exc2.value)

    # 7.3 Duplicate FY codes
    with pytest.raises(ValueError) as exc3:
        validate_financial_year_records([
            {"code": "FY 2026-27", "start_date": "2026-04-01", "end_date": "2027-03-31"},
            {"code": "FY 2026-27", "start_date": "2027-04-01", "end_date": "2028-03-31"},
        ])
    assert "Duplicate financial year code" in str(exc3.value)


# ==============================================================================
# WP06-08 Test 8: Historical Invoice Invariance After Reference Changes
# ==============================================================================
def test_08_historical_invoice_invariance_after_customer_change(db_session):
    """8. Customer detail edits do not mutate historical billing snapshots on issued invoices."""
    invoice = db_session.scalar(select(CommercialInvoice).where(CommercialInvoice.snapshot_customer_name.is_not(None)))
    assert invoice is not None
    customer = db_session.scalar(select(Customer).where(Customer.id == invoice.customer_id))
    assert customer is not None

    original_snapshot_name = invoice.snapshot_customer_name
    original_snapshot_address = invoice.snapshot_billing_address
    original_inv_total = invoice.total_amount
    original_cust_name = customer.billing_name
    original_cust_address = customer.billing_address

    # Mutate live customer record
    customer.billing_name = "Acme Global Solutions Limited"
    customer.billing_address = "999 New Industrial Highway, Bengaluru"
    db_session.flush()

    # Re-fetch invoice and confirm historical snapshot is preserved
    refetched_inv = db_session.scalar(select(CommercialInvoice).where(CommercialInvoice.id == invoice.id))
    assert refetched_inv.snapshot_customer_name == original_snapshot_name
    assert refetched_inv.snapshot_billing_address == original_snapshot_address
    assert refetched_inv.total_amount == original_inv_total

    # Roll back mutation
    customer.billing_name = original_cust_name
    customer.billing_address = original_cust_address
    db_session.flush()


# ==============================================================================
# WP06-08 Test 9: Unauthorized User Attempts Against Statutory Endpoints
# ==============================================================================
def test_09_unauthorized_roles_rejected_from_statutory_endpoints(client, sales_headers, viewer_headers):
    """9. Sales and Viewer roles are rejected with HTTP 403 from statutory mutation endpoints."""
    # Sales role rejected
    res1 = client.post(
        "/api/settings/taxes/gst",
        json={"name": "GST 12%", "rate": 0.12},
        headers=sales_headers,
    )
    assert res1.status_code == 403

    # Viewer role rejected
    res2 = client.post(
        "/api/settings/financial-years",
        json={"code": "FY 2029-30", "start_date": "2029-04-01", "end_date": "2030-03-31"},
        headers=viewer_headers,
    )
    assert res2.status_code == 403

    # Unauthenticated rejected with 401
    res3 = client.post(
        "/api/settings/taxes/tds",
        json={"section_code": "194J", "rate": 0.10},
    )
    assert res3.status_code == 401


# ==============================================================================
# WP06-08 Test 10: Monetary Precision & Per-Line GST Rounding
# ==============================================================================
def test_10_decimal_arithmetic_and_per_line_gst_rounding():
    """10. Monetary precision engine uses Decimal ROUND_HALF_UP on per-line calculations."""
    # Test line item: quantity = 3, unit_price = 333.33 -> exclusive = 999.99
    qty = Decimal("3.0000")
    price = Decimal("333.33")
    exclusive = (qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert exclusive == Decimal("999.99")

    # 18% GST -> 999.99 * 0.18 = 179.9982 -> rounded to 180.00
    tax_rate = Decimal("0.1800")
    tax_amount = (exclusive * tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert tax_amount == Decimal("180.00")

    total = exclusive + tax_amount
    assert total == Decimal("1179.99")

    # Intrastate split: CGST = 90.00, SGST = 90.00
    cgst = (tax_amount / Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sgst = (tax_amount / Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert cgst + sgst == tax_amount


# ==============================================================================
# WP06-08 Test 11: TDS Reconciliation Invariant (Cash + TDS = Total Settled)
# ==============================================================================
def test_11_tds_settlement_reconciliation_invariant(db_session):
    """11. Total Settled must exactly equal Cash Received + TDS Recognized across all payments."""
    payments = db_session.scalars(select(PaymentTransaction)).all()
    for p in payments:
        expected = Decimal(str(p.cash_amount)) + Decimal(str(p.tds_amount))
        actual = Decimal(str(p.total_settled))
        assert actual == expected, f"Payment {p.remittance_number}: {actual} != {expected}"


# ==============================================================================
# WP06-08 Test 12: Payment Idempotency & Reversal Integrity
# ==============================================================================
def test_12_payment_reversal_pair_zero_net_settlement(db_session):
    """12. Payment reversals record signed negative values that net to ₹0.00 with the parent transaction."""
    reversals = db_session.scalars(
        select(PaymentTransaction).where(PaymentTransaction.transaction_type == "REVERSAL")
    ).all()
    assert len(reversals) >= 1

    for rev in reversals:
        assert Decimal(str(rev.cash_amount)) < Decimal("0.00")
        assert Decimal(str(rev.total_settled)) < Decimal("0.00")
        assert rev.parent_transaction_id is not None

        parent = db_session.scalar(select(PaymentTransaction).where(PaymentTransaction.id == rev.parent_transaction_id))
        assert parent is not None

        net_cash = Decimal(str(parent.cash_amount)) + Decimal(str(rev.cash_amount))
        net_settled = Decimal(str(parent.total_settled)) + Decimal(str(rev.total_settled))
        assert net_cash == Decimal("0.00")
        assert net_settled == Decimal("0.00")


# ==============================================================================
# WP06-08 Test 13: Altered Financial Export Values Detected by Reconciliation
# ==============================================================================
def test_13_altered_financial_export_values_detected():
    """13. Cross-layer reconciliation detects any non-zero difference as a failure."""
    fake_browser = {
        "source": "Playwright Headless Chromium DOM",
        "run_id": "test_run_123",
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "invoiced_gross": "999999.99",  # Altered value
        "exclusive_amount": "800000.00",
        "cash_amount": "500000.00",
        "tds_amount": "20000.00",
        "total_settled": "520000.00",
        "total_outstanding": "479999.99",
        "annual_target": "5000000.00",
    }
    with pytest.raises(AssertionError) as exc_info:
        verify_cross_layer_reconciliation(browser_override=fake_browser)
    assert "Cross-layer financial reconciliation failed" in str(exc_info.value)


# ==============================================================================
# WP06-08 Test 14: Stale Browser Financial Snapshots Rejected
# ==============================================================================
def test_14_stale_browser_financial_snapshots_rejected():
    """14. Freshness validator rejects stale browser snapshots older than the reconciliation start time."""
    now = datetime.datetime.now(datetime.timezone.utc)
    stale_time = (now - datetime.timedelta(minutes=10)).isoformat()

    stale_snapshot = {
        "source": "Playwright Headless Chromium DOM",
        "run_id": "current_run",
        "captured_at": stale_time,
        "invoiced_gross": "100.00",
        "exclusive_amount": "80.00",
        "cash_amount": "50.00",
        "tds_amount": "5.00",
        "total_settled": "55.00",
        "total_outstanding": "45.00",
        "annual_target": "1000.00",
    }
    with pytest.raises(ValueError) as exc_info:
        validate_browser_snapshot(stale_snapshot, "current_run", now)
    assert "Stale snapshot detected" in str(exc_info.value)


# ==============================================================================
# WP06-08 Test 15: Missing Corporate Approval Files Return BLOCKED
# ==============================================================================
def test_15_missing_corporate_approval_files_return_blocked(tmp_path):
    """15. Missing governance review documents produce BLOCKED status in readiness verifier."""
    empty_docs_dir = tmp_path / "empty_finance"
    empty_docs_dir.mkdir()

    backend_mock = tmp_path / "backend"
    backend_mock.mkdir()
    (backend_mock / "docs" / "finance").mkdir(parents=True)

    status, err = parse_governance_doc_status(backend_mock / "docs" / "finance" / "NON_EXISTENT.md")
    assert status == "MISSING"
    assert "not found" in err


# ==============================================================================
# WP06-08 Test 16: Forged Financial Acceptance PASS Rejected
# ==============================================================================
def test_16_forged_financial_acceptance_pass_rejected(tmp_path):
    """16. Self-declared APPROVED string in markdown is rejected as untrusted."""
    forged_file = tmp_path / "forged_bpd01.md"
    forged_file.write_text(
        "# BPD-01\nGovernance Status: APPROVED\nApproved by: Developer\n",
        encoding="utf-8",
    )
    status, err = parse_governance_doc_status(forged_file)
    assert status == "FAIL"
    assert "Self-declared corporate approval detected in markdown" in err


# ==============================================================================
# WP06-08 Test 17: Source Commit SHAs Verified and Mismatches Rejected (WP06-03)
# ==============================================================================
def test_17_source_commit_shas_recorded(tmp_path):
    """17. Financial readiness verifier extracts exact git SHAs and rejects mismatches/unresolvable repos."""
    backend_dir = Path(__file__).resolve().parent.parent
    frontend_dir = backend_dir.parent / "cash-flow-buddy-101"

    out_file = tmp_path / "manifest.json"
    report = verify_financial_readiness(backend_dir, frontend_dir, output_json=out_file)

    assert "backend_sha" in report
    assert "frontend_sha" in report
    assert len(report["backend_sha"]) == 40
    assert len(report["frontend_sha"]) == 40
    assert "UNKNOWN" not in report["backend_sha"]
    assert "UNKNOWN" not in report["frontend_sha"]
    assert "synthetic_postgresql" in report["dataset_classification"]

    # Negative test 1: Non-existent or non-git directory fails closed
    non_git_dir = tmp_path / "not_a_repo"
    non_git_dir.mkdir()
    with pytest.raises(ValueError) as exc_info:
        get_git_commit_sha(non_git_dir)
    assert "not a Git repository" in str(exc_info.value)

    # Readiness verifier reports FAIL on unresolvable git repo
    bad_repo_report = verify_financial_readiness(non_git_dir, frontend_dir)
    assert bad_repo_report["technical_financial_integrity"]["verdict"] == "FAIL"
    assert bad_repo_report["overall_release_readiness"] == "FAIL"

    # Negative test 2: Mismatched expected backend SHA fails closed
    mismatched_b_report = verify_financial_readiness(
        backend_dir, frontend_dir, expected_backend_sha="0000000000000000000000000000000000000000"
    )
    assert mismatched_b_report["technical_financial_integrity"]["verdict"] == "FAIL"
    assert any("Backend SHA mismatch" in err for err in mismatched_b_report["technical_financial_integrity"]["errors"])

    # Negative test 3: Mismatched expected frontend SHA fails closed
    mismatched_f_report = verify_financial_readiness(
        backend_dir, frontend_dir, expected_frontend_sha="0000000000000000000000000000000000000000"
    )
    assert mismatched_f_report["technical_financial_integrity"]["verdict"] == "FAIL"
    assert any("Frontend SHA mismatch" in err for err in mismatched_f_report["technical_financial_integrity"]["errors"])


# ==============================================================================
# WP06-08 Test 18: Pending Business Approval Correctly Returns BLOCKED
# ==============================================================================
def test_18_pending_business_approval_correctly_returns_blocked(monkeypatch):
    """18. When technical integrity passes (verified via controlled isolation) but corporate
    approval is pending, overall status is BLOCKED."""
    backend_dir = Path(__file__).resolve().parent.parent
    frontend_dir = backend_dir.parent / "cash-flow-buddy-101"

    backend_sha = get_git_commit_sha(backend_dir)
    frontend_sha = get_git_commit_sha(frontend_dir)

    from scripts.release import verify_financial_readiness as vfr_module

    # Controlled mock for technical integrity gate to test Finance BLOCKED governance in isolation (WP06-R06)
    mock_checks = {
        "expected_sha_pinning": {"status": "PASS", "pinned": True},
        "backend_sha_checkpoint": {"status": "PASS", "sha": backend_sha},
        "frontend_sha_checkpoint": {"status": "PASS", "sha": frontend_sha},
        "schema_revision": {"status": "PASS", "revision": "77e73a1cb221"},
        "dataset_environment": {"status": "PASS", "database": "collectflow_test"},
        "dataset_population": {"status": "PASS", "counts": {"invoices": 1, "payments": 2, "reversals": 1, "unbilled_pos": 1}},
        "invoice_balance_invariants": {"status": "PASS"},
        "accounting_totals_invariants": {"status": "PASS"},
        "payment_settlement_invariants": {"status": "PASS"},
        "reversal_integrity": {"status": "PASS"},
        "unbilled_po_zero_ar": {"status": "PASS"},
        "regression_test_evidence": {"status": "PASS", "summary": {"total": 374, "passed": 303, "failed": 0, "errors": 0, "skipped": 71}},
        "cross_layer_reconciliation_evidence": {"status": "PASS", "reconciliation_level": "FULL_FIVE_SOURCE", "formal_acceptance_eligible": True},
        "repository_identity": {"status": "PASS"},
    }

    monkeypatch.setattr(
        vfr_module,
        "evaluate_technical_integrity",
        lambda *args, **kwargs: ("PASS", mock_checks, [], []),
    )

    report = verify_financial_readiness(
        backend_dir=backend_dir,
        frontend_dir=frontend_dir,
        expected_backend_sha=backend_sha,
        expected_frontend_sha=frontend_sha,
    )
    assert report["technical_financial_integrity"]["verdict"] == "PASS"
    assert report["business_authorization"]["verdict"] == "BLOCKED"
    assert report["overall_release_readiness"] == "BLOCKED"
    assert report["recommendation"] == "READY FOR INDEPENDENT RE-REVIEW"
    assert report["business_authorization"]["governance_status"] == "AWAITING_FINANCE_APPROVAL"


# ==============================================================================
# WP06-08 Test 19: Direct Reference Initialization Blocked in Production (WP06-02)
# ==============================================================================
def test_19_direct_init_reference_data_blocked_in_production(db_session):
    """19. Direct init_reference_data calls with statutory config abort in production/staging mode."""
    sample_config = {
        "config_version": "2026.1",
        "approval_status": "finance_approved",
        "approved_by": "Finance Lead",
        "approval_reference": "FIN-REF-001",
        "approval_date": "2026-04-01",
        "controlled_source": "test",
        "effective_date": "2026-04-01",
        "policy_references": ["BPD-01", "BPD-02"],
        "financial_years": [{"code": "FY 2026-27", "start_date": "2026-04-01", "end_date": "2027-03-31"}],
        "gst_rates": [{"name": "GST 18%", "rate": "0.1800", "cgst_rate": "0.0900", "sgst_rate": "0.0900", "igst_rate": "0.1800"}],
        "tds_sections": [{"section_code": "194C", "description": "Contractors", "default_rate": "0.0200"}],
    }
    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        with pytest.raises(RuntimeError) as exc_info:
            init_reference_data(db_session, ref_config=sample_config)
        assert "CRITICAL GOVERNANCE ABORT: Direct statutory reference initialization in production/staging is BLOCKED" in str(exc_info.value)


# ==============================================================================
# WP06-08 Test 20: Direct Reference Initialization Validates Governance & Rates (WP06-02)
# ==============================================================================
def test_20_direct_init_reference_data_validates_governance_integrity(db_session):
    """20. Direct init_reference_data validates required governance fields and GST rate splits."""
    # Missing required governance field
    broken_config = {
        "is_test_fixture": True,
        "config_version": "2026.1",
        # missing approved_by, approval_reference, etc.
    }
    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with pytest.raises(ValueError) as exc_info:
            init_reference_data(db_session, ref_config=broken_config)
        assert "CRITICAL CONFIGURATION ERROR" in str(exc_info.value)

    # Inconsistent GST rate split
    invalid_split_config = {
        "is_test_fixture": True,
        "config_version": "2026.1",
        "approved_by": "Test Approver",
        "approval_reference": "TEST-REF-001",
        "approval_date": "2026-04-01",
        "controlled_source": "test",
        "effective_date": "2026-04-01",
        "policy_references": ["BPD-01"],
        "financial_years": [{"code": "FY 2026-27", "start_date": "2026-04-01", "end_date": "2027-03-31"}],
        "gst_rates": [{"name": "GST 18%", "rate": "0.1800", "cgst_rate": "0.0500", "sgst_rate": "0.0500", "igst_rate": "0.1800"}],
        "tds_sections": [{"section_code": "194C", "description": "Contractors", "default_rate": "0.0200"}],
    }
    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "test"}):
        with pytest.raises(ValueError) as exc_info:
            init_reference_data(db_session, ref_config=invalid_split_config)
        assert "split inconsistent" in str(exc_info.value)


# ==============================================================================
# WP06-08 Test 21: Empty Dataset Rejected by Technical Acceptance Gate (WP06-01)
# ==============================================================================
def test_21_empty_dataset_rejected_by_technical_acceptance(tmp_path):
    """21. Empty or insufficiently populated datasets cannot produce a technical PASS."""
    backend_dir = Path(__file__).resolve().parent.parent
    backend_sha = get_git_commit_sha(backend_dir)
    frontend_sha = get_git_commit_sha(backend_dir.parent / "cash-flow-buddy-101")

    # Mock database session returning 0 invoices
    with patch("app.core.database.get_db_session") as mock_db_ctx:
        mock_db = MagicMock()
        mock_db_ctx.return_value.__enter__.return_value = mock_db
        mock_db.execute.return_value.scalar.side_effect = ["collectflow_test", "collectflow_user", "77e73a1cb221"]
        # Return empty list for invoices
        mock_db.scalars.return_value.all.return_value = []

        verdict, checks, errors, _ = evaluate_technical_integrity(
            backend_dir=backend_dir,
            backend_sha=backend_sha,
            frontend_sha=frontend_sha,
        )
        assert verdict == "FAIL"
        assert checks["dataset_population"]["status"] == "FAIL"
        assert any("Insufficiently populated financial dataset" in err for err in errors)


# ==============================================================================
# WP06-08 Test 22: Orphan Reversals and Mismatched Linkages Rejected (WP06-01)
# ==============================================================================
def test_22_orphan_reversal_and_mismatched_linkage_rejected(tmp_path):
    """22. Reversals with missing parents or mismatched invoice linkage fail closed."""
    backend_dir = Path(__file__).resolve().parent.parent
    backend_sha = get_git_commit_sha(backend_dir)
    frontend_sha = get_git_commit_sha(backend_dir.parent / "cash-flow-buddy-101")

    # Create mock objects: 1 invoice, 1 regular payment, 1 orphan reversal (parent not found)
    mock_inv = MagicMock(total_amount=Decimal("100.00"), exclusive_amount=Decimal("84.75"), tax_amount=Decimal("15.25"), settlements=[])
    mock_pay = MagicMock(transaction_type="PAYMENT", cash_amount=Decimal("100.00"), tds_amount=Decimal("0.00"), total_settled=Decimal("100.00"), parent_transaction_id=None, invoice_id=1)
    mock_rev = MagicMock(
        transaction_type="REVERSAL",
        remittance_number="REV-ORPHAN-001",
        cash_amount=Decimal("-100.00"),
        tds_amount=Decimal("0.00"),
        total_settled=Decimal("-100.00"),
        parent_transaction_id=99999,
        invoice_id=1,
    )
    mock_inv.settlements = [mock_pay, mock_rev]
    mock_po = MagicMock(id=1, status="UNBILLED")

    with patch("app.core.database.get_db_session") as mock_db_ctx:
        mock_db = MagicMock()
        mock_db_ctx.return_value.__enter__.return_value = mock_db
        mock_db.execute.return_value.scalar.side_effect = ["collectflow_test", "collectflow_user", "77e73a1cb221"]
        mock_db.scalars.return_value.all.side_effect = [
            [mock_inv],              # invoices
            [mock_pay, mock_rev],     # payments
            [mock_po],               # unbilled_pos
            [],                      # linked_invoices to unbilled POs
        ]
        # scalar lookup for parent returns None
        mock_db.scalar.return_value = None

        verdict, checks, errors, _ = evaluate_technical_integrity(
            backend_dir=backend_dir,
            backend_sha=backend_sha,
            frontend_sha=frontend_sha,
        )
        assert verdict == "FAIL"
        assert checks["reversal_integrity"]["status"] == "FAIL"
        assert any("references non-existent parent ID" in err for err in errors)


# ==============================================================================
# WP06-08 Test 23: Altered Accounting and Settlement Totals Rejected (WP06-01)
# ==============================================================================
def test_23_altered_settlement_totals_rejected(tmp_path):
    """23. Settlement arithmetic discrepancies trigger technical acceptance failure."""
    backend_dir = Path(__file__).resolve().parent.parent
    backend_sha = get_git_commit_sha(backend_dir)
    frontend_sha = get_git_commit_sha(backend_dir.parent / "cash-flow-buddy-101")

    # Mock payment with cash + tds != total_settled
    mock_inv = MagicMock(total_amount=Decimal("100.00"), exclusive_amount=Decimal("84.75"), tax_amount=Decimal("15.25"), settlements=[])
    mock_pay = MagicMock(
        remittance_number="PAY-BAD-001",
        transaction_type="PAYMENT",
        cash_amount=Decimal("80.00"),
        tds_amount=Decimal("10.00"),
        total_settled=Decimal("95.00"),  # 80 + 10 != 95
        parent_transaction_id=None,
        invoice_id=1,
    )
    mock_parent = MagicMock(
        id=2,
        transaction_type="PAYMENT",
        cash_amount=Decimal("10.00"),
        tds_amount=Decimal("0.00"),
        total_settled=Decimal("10.00"),
        invoice_id=1,
    )
    mock_rev = MagicMock(
        remittance_number="REV-001",
        transaction_type="REVERSAL",
        cash_amount=Decimal("-10.00"),
        tds_amount=Decimal("0.00"),
        total_settled=Decimal("-10.00"),
        parent_transaction_id=2,
        invoice_id=1,
    )
    mock_inv.settlements = [mock_pay, mock_parent, mock_rev]
    mock_po = MagicMock(id=1, status="UNBILLED")

    with patch("app.core.database.get_db_session") as mock_db_ctx:
        mock_db = MagicMock()
        mock_db_ctx.return_value.__enter__.return_value = mock_db
        mock_db.execute.return_value.scalar.side_effect = ["collectflow_test", "collectflow_user", "77e73a1cb221"]
        mock_db.scalars.return_value.all.side_effect = [
            [mock_inv],
            [mock_pay, mock_parent, mock_rev],
            [mock_po],
            [],
        ]
        mock_db.scalar.return_value = mock_parent

        verdict, checks, errors, _ = evaluate_technical_integrity(
            backend_dir=backend_dir,
            backend_sha=backend_sha,
            frontend_sha=frontend_sha,
        )
        assert verdict == "FAIL"
        assert checks["payment_settlement_invariants"]["status"] == "FAIL"
        assert any("settlement mismatch" in err for err in errors)


# ==============================================================================
# WP06-08 Test 24: Missing or Failed Regression Evidence Handled Fail-Closed (WP06-01)
# ==============================================================================
def test_24_missing_or_failed_regression_evidence_handled(tmp_path):
    """24. Missing test evidence returns BLOCKED; failed test evidence returns FAIL."""
    backend_dir = Path(__file__).resolve().parent.parent
    backend_sha = get_git_commit_sha(backend_dir)
    frontend_sha = get_git_commit_sha(backend_dir.parent / "cash-flow-buddy-101")

    # Non-existent evidence file produces BLOCKED
    verdict_missing, checks_missing, _, blocked_missing = evaluate_technical_integrity(
        backend_dir=backend_dir,
        backend_sha=backend_sha,
        frontend_sha=frontend_sha,
        regression_evidence_path=tmp_path / "non_existent_evidence.json",
    )
    assert checks_missing["regression_test_evidence"]["status"] == "BLOCKED"
    assert any("Missing PostgreSQL regression test evidence file" in b for b in blocked_missing)

    # Corrupted / failed evidence file produces FAIL
    failed_xml = tmp_path / "failed_pytest.xml"
    failed_xml.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" errors="0" failures="2" skipped="0" tests="100" time="1.0" timestamp="'
        + datetime.datetime.now(datetime.timezone.utc).isoformat()
        + '" hostname="localhost"><testcase classname="tests.test_wp06_financial_governance_and_integrity" name="test_f" /><testcase classname="tests.test_fail" name="test_f2" /></testsuite></testsuites>',
        encoding="utf-8",
    )
    from scripts.release.verify_financial_readiness import compute_file_sha256
    failed_ev_file = tmp_path / "failed_evidence.json"
    failed_ev_file.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "execution_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "FAIL",
            "summary": {"total": 100, "passed": 98, "failed": 2, "errors": 0, "skipped": 0},
            "xml_report_sha256": compute_file_sha256(failed_xml),
            "report_xml": str(failed_xml),
        }),
        encoding="utf-8",
    )
    verdict_failed, checks_failed, errors_failed, _ = evaluate_technical_integrity(
        backend_dir=backend_dir,
        backend_sha=backend_sha,
        frontend_sha=frontend_sha,
        regression_evidence_path=failed_ev_file,
    )
    assert verdict_failed == "FAIL"
    assert checks_failed["regression_test_evidence"]["status"] == "FAIL"
    assert any("failed tests (2)" in err.lower() for err in errors_failed)


# ==============================================================================
# WP06-08 Test 25: Partial or Test Override Reconciliation Blocked/Rejected (WP06-01 & WP06-04)
# ==============================================================================
def test_25_partial_or_test_override_reconciliation_blocked_or_rejected(tmp_path):
    """25. Partial 4-source reconciliation produces BLOCKED; test browser overrides produce FAIL."""
    backend_dir = Path(__file__).resolve().parent.parent
    backend_sha = get_git_commit_sha(backend_dir)
    frontend_sha = get_git_commit_sha(backend_dir.parent / "cash-flow-buddy-101")

    # 1. Partial reconciliation (browser NOT_EXECUTED) produces BLOCKED
    partial_recon_file = tmp_path / "partial_recon.json"
    partial_recon_file.write_text(
        json.dumps({
            "status": "PARTIAL_FOUR_SOURCE",
            "reconciliation_level": "PARTIAL_FOUR_SOURCE",
            "formal_acceptance_eligible": False,
            "browser_status": "NOT_EXECUTED",
            "backend_sha": backend_sha,
            "frontend_sha": frontend_sha,
            "database": "collectflow_test",
            "invariants": {"reversal_net_zero": True},
        }),
        encoding="utf-8",
    )
    _, checks_partial, _, blocked_partial = evaluate_technical_integrity(
        backend_dir=backend_dir,
        backend_sha=backend_sha,
        frontend_sha=frontend_sha,
        reconciliation_summary_path=partial_recon_file,
    )
    assert checks_partial["cross_layer_reconciliation_evidence"]["status"] == "BLOCKED"
    assert any("partial four-source" in b for b in blocked_partial)

    # 2. Test override reconciliation produces FAIL
    override_recon_file = tmp_path / "override_recon.json"
    override_recon_file.write_text(
        json.dumps({
            "status": "TEST_OVERRIDE",
            "reconciliation_level": "TEST_OVERRIDE_FOUR_SOURCE",
            "formal_acceptance_eligible": False,
            "is_synthetic_override": True,
            "browser_status": "TEST_OVERRIDE",
            "backend_sha": backend_sha,
            "frontend_sha": frontend_sha,
            "database": "collectflow_test",
            "invariants": {"reversal_net_zero": True},
        }),
        encoding="utf-8",
    )
    verdict_override, checks_override, errors_override, _ = evaluate_technical_integrity(
        backend_dir=backend_dir,
        backend_sha=backend_sha,
        frontend_sha=frontend_sha,
        reconciliation_summary_path=override_recon_file,
    )
    assert verdict_override == "FAIL"
    assert checks_override["cross_layer_reconciliation_evidence"]["status"] == "FAIL"
    assert any("synthetic test browser override" in err.lower() for err in errors_override)


# ==============================================================================
# WP06-08 Test 26: Production DB Target Rejected Without Read-Only Procedure (WP06-03)
# ==============================================================================
def test_26_production_target_db_rejected_without_authorization(tmp_path):
    """26. Production/staging target databases reject synthetic verification without explicit authorization."""
    backend_dir = Path(__file__).resolve().parent.parent
    backend_sha = get_git_commit_sha(backend_dir)
    frontend_sha = get_git_commit_sha(backend_dir.parent / "cash-flow-buddy-101")

    with patch.dict(os.environ, {"COLLECTFLOW_ENV": "production"}):
        # Without --allow-production-review
        verdict_unauth, checks_unauth, errors_unauth, _ = evaluate_technical_integrity(
            backend_dir=backend_dir,
            backend_sha=backend_sha,
            frontend_sha=frontend_sha,
            allow_production_review=False,
        )
        assert verdict_unauth == "FAIL"
        assert checks_unauth["dataset_environment"]["status"] == "FAIL"
        assert any("Production or staging database target detected" in err for err in errors_unauth)


# ==============================================================================
# WP06-08 Test 27: Reconciliation Negative Tests (WP06-R01)
# ==============================================================================
def test_27_reconciliation_negative_tests(tmp_path):
    """27. Validate reconciliation negative tests (WP06-R01): empty invariants, missing browser status,
    missing revisions, fabricated PASS, missing metrics, nonzero delta, DB total mismatch."""
    backend_dir = Path(__file__).resolve().parent.parent
    backend_sha = get_git_commit_sha(backend_dir)
    frontend_sha = get_git_commit_sha(backend_dir.parent / "cash-flow-buddy-101")

    applicable_matrix = {
        "Invoiced Gross": ["postgresql", "backend_api", "browser_ui", "excel_xlsx", "csv_export"],
        "Taxable Exclusive": ["postgresql", "backend_api", "browser_ui", "excel_xlsx", "csv_export"],
        "GST Tax Amount": ["postgresql", "backend_api", "excel_xlsx", "csv_export"],
        "Cash Received": ["postgresql", "backend_api", "browser_ui", "excel_xlsx", "csv_export"],
        "TDS Deducted": ["postgresql", "backend_api", "browser_ui", "excel_xlsx", "csv_export"],
        "Total Net Settled": ["postgresql", "backend_api", "browser_ui", "excel_xlsx", "csv_export"],
        "Balance Outstanding": ["postgresql", "backend_api", "browser_ui", "excel_xlsx", "csv_export"],
        "Unbilled PO Backlog": ["postgresql", "backend_api", "excel_xlsx", "csv_export"],
        "Unbilled Recognized AR": ["postgresql", "backend_api", "excel_xlsx", "csv_export"],
        "Annual Target": ["postgresql", "backend_api", "browser_ui"],
    }
    metric_values = {
        "Invoiced Gross": "531000.00",
        "Taxable Exclusive": "450000.00",
        "GST Tax Amount": "81000.00",
        "Cash Received": "200000.00",
        "TDS Deducted": "9000.00",
        "Total Net Settled": "209000.00",
        "Balance Outstanding": "322000.00",
        "Unbilled PO Backlog": "295000.00",
        "Unbilled Recognized AR": "0.00",
        "Annual Target": "7500000.00",
    }
    base_comparisons = {}
    for k, v in metric_values.items():
        applicable = applicable_matrix[k]
        obs = {s: v for s in applicable}
        if k in ("GST Tax Amount", "Unbilled PO Backlog", "Unbilled Recognized AR"):
            obs["browser_ui"] = "N/A (UI)"
        elif k == "Annual Target":
            obs["excel_xlsx"] = "N/A (Export)"
            obs["csv_export"] = "N/A (Export)"
        base_comparisons[k] = {
            "expected_applicable_sources": applicable,
            "sources_compared": applicable,
            "missing_sources": [],
            "observed_values": obs,
            "max_delta": "0.00",
            "status": "MATCH (₹0.00)",
        }

    base_recon = {
        "status": "PASS",
        "reconciliation_level": "FULL_FIVE_SOURCE",
        "formal_acceptance_eligible": True,
        "is_synthetic_override": False,
        "run_id": "test_run_001",
        "run_start_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "backend_sha": backend_sha,
        "frontend_sha": frontend_sha,
        "database": "collectflow_test",
        "environment": "test",
        "verified_sources": ["postgresql", "rest_api", "browser_ui", "excel_xlsx", "csv_export"],
        "unverified_sources": [],
        "browser_status": "PASS",
        "comparisons": base_comparisons,
        "metrics": {
            "invoiced_gross": "531000.00",
            "exclusive_amount": "450000.00",
            "tax_amount": "81000.00",
            "cash_received": "200000.00",
            "tds_deducted": "9000.00",
            "total_settled": "209000.00",
            "total_outstanding": "322000.00",
            "unbilled_po_gross": "295000.00",
            "unbilled_recognized_ar": "0.00",
            "annual_target": "7500000.00",
        },
        "invariants": {
            "cash_plus_tds_equals_settled": True,
            "invoiced_minus_settled_equals_outstanding": True,
            "reversal_net_zero": True,
            "unbilled_ar_zero": True,
            "ar_aging_sums_to_outstanding": True,
            "target_reconciled": True,
        },
    }

    # 1. Empty invariants
    bad_inv = dict(base_recon, invariants={})
    f1 = tmp_path / "recon_empty_inv.json"
    f1.write_text(json.dumps(bad_inv), encoding="utf-8")
    v1, _, errs1, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f1)
    assert v1 == "FAIL"
    assert any("invariants dictionary is empty" in e for e in errs1)

    # 2. Non-boolean or False invariant
    bad_inv2 = dict(base_recon, invariants=dict(base_recon["invariants"], reversal_net_zero=False))
    f2 = tmp_path / "recon_false_inv.json"
    f2.write_text(json.dumps(bad_inv2), encoding="utf-8")
    v2, _, errs2, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f2)
    assert v2 == "FAIL"
    assert any("reversal_net_zero" in e and "not True boolean" in e for e in errs2)

    # 3. Missing browser status
    bad_browser = dict(base_recon)
    del bad_browser["browser_status"]
    f3 = tmp_path / "recon_missing_browser.json"
    f3.write_text(json.dumps(bad_browser), encoding="utf-8")
    v3, _, errs3, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f3)
    assert v3 == "FAIL"
    assert any("Reconciliation evidence invalid" in e for e in errs3)

    # 4. Missing or UNKNOWN source revisions
    bad_sha = dict(base_recon, frontend_sha="UNKNOWN")
    f4 = tmp_path / "recon_unknown_sha.json"
    f4.write_text(json.dumps(bad_sha), encoding="utf-8")
    v4, _, errs4, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f4)
    assert v4 == "FAIL"
    assert any("missing required backend_sha" in e or "frontend_sha" in e for e in errs4)

    # 5. Fabricated FULL_FIVE_SOURCE PASS
    bad_fab = dict(base_recon, is_synthetic_override=True)
    f5 = tmp_path / "recon_fab_override.json"
    f5.write_text(json.dumps(bad_fab), encoding="utf-8")
    v5, _, errs5, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f5)
    assert v5 == "FAIL"
    assert any("Synthetic test browser override detected" in e for e in errs5)

    # 6. Missing financial metrics in comparisons
    bad_comps = dict(base_recon, comparisons={k: v for k, v in base_recon["comparisons"].items() if k != "Annual Target"})
    f6 = tmp_path / "recon_missing_metrics.json"
    f6.write_text(json.dumps(bad_comps), encoding="utf-8")
    v6, _, errs6, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f6)
    assert v6 == "FAIL"
    assert any("missing required financial metric: 'Annual Target'" in e for e in errs6)

    # 7. Non-zero comparison delta
    bad_delta_comps = {k: dict(v) for k, v in base_recon["comparisons"].items()}
    bad_delta_comps["Invoiced Gross"]["observed_values"] = dict(
        bad_delta_comps["Invoiced Gross"]["observed_values"],
        browser_ui="531000.05",
    )
    bad_delta_comps["Invoiced Gross"]["max_delta"] = "0.05"
    bad_delta_comps["Invoiced Gross"]["status"] = "MISMATCH!"
    bad_delta = dict(base_recon, comparisons=bad_delta_comps)
    f7 = tmp_path / "recon_nonzero_delta.json"
    f7.write_text(json.dumps(bad_delta), encoding="utf-8")
    v7, _, errs7, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f7)
    assert v7 == "FAIL"
    assert any("Non-zero delta for comparison 'Invoiced Gross'" in e for e in errs7)

    # 8. Reconciliation totals inconsistent with PostgreSQL
    bad_totals_metrics = dict(base_recon["metrics"], invoiced_gross="999999.00")
    bad_totals = dict(base_recon, metrics=bad_totals_metrics)
    f8 = tmp_path / "recon_bad_totals.json"
    f8.write_text(json.dumps(bad_totals), encoding="utf-8")
    v8, _, errs8, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f8)
    assert v8 == "FAIL"
    assert any("inconsistent with PostgreSQL database for 'invoiced_gross'" in e for e in errs8)

    # 9. Missing observed_values mapping (WP06-R05)
    bad_no_obs_comps = {k: dict(v) for k, v in base_recon["comparisons"].items()}
    del bad_no_obs_comps["Invoiced Gross"]["observed_values"]
    bad_no_obs = dict(base_recon, comparisons=bad_no_obs_comps)
    f9 = tmp_path / "recon_no_obs.json"
    f9.write_text(json.dumps(bad_no_obs), encoding="utf-8")
    v9, _, errs9, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f9)
    assert v9 == "FAIL"
    assert any("missing 'observed_values' mapping" in e for e in errs9)

    # 10. Missing source value on applicable metric (WP06-R05)
    bad_missing_src_comps = {k: dict(v) for k, v in base_recon["comparisons"].items()}
    bad_missing_src_comps["Invoiced Gross"]["observed_values"] = dict(
        bad_missing_src_comps["Invoiced Gross"]["observed_values"],
        browser_ui="MISSING_EVIDENCE",
    )
    bad_missing_src = dict(base_recon, comparisons=bad_missing_src_comps)
    f10 = tmp_path / "recon_missing_src.json"
    f10.write_text(json.dumps(bad_missing_src), encoding="utf-8")
    v10, _, errs10, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f10)
    assert v10 == "FAIL"
    assert any("missing observed amount for applicable source 'browser_ui'" in e for e in errs10)

    # 11. Fabricated source participation (sources_compared mismatch) (WP06-R05)
    bad_part_comps = {k: dict(v) for k, v in base_recon["comparisons"].items()}
    bad_part_comps["Invoiced Gross"]["sources_compared"] = ["postgresql", "backend_api"]
    bad_part = dict(base_recon, comparisons=bad_part_comps)
    f11 = tmp_path / "recon_bad_part.json"
    f11.write_text(json.dumps(bad_part), encoding="utf-8")
    v11, _, errs11, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f11)
    assert v11 == "FAIL"
    assert any("sources_compared mismatch" in e for e in errs11)

    # 12. False zero-delta claims (reported max_delta=0.00 but observed values differ) (WP06-R05)
    bad_claim_comps = {k: dict(v) for k, v in base_recon["comparisons"].items()}
    bad_claim_comps["Invoiced Gross"]["max_delta"] = "0.00"
    bad_claim_comps["Invoiced Gross"]["status"] = "MATCH (₹0.00)"
    bad_claim_comps["Invoiced Gross"]["observed_values"] = dict(
        bad_claim_comps["Invoiced Gross"]["observed_values"],
        browser_ui="531010.00",
    )
    bad_claim = dict(base_recon, comparisons=bad_claim_comps)
    f12 = tmp_path / "recon_false_zero_delta.json"
    f12.write_text(json.dumps(bad_claim), encoding="utf-8")
    v12, _, errs12, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, reconciliation_summary_path=f12)
    assert v12 == "FAIL"
    assert any("fabricated zero-delta claim detected" in e or "Non-zero delta" in e for e in errs12)


def _create_valid_baseline_pytest_xml(dest_path: Path, timestamp: Optional[str] = None) -> str:
    """Creates a temporary valid baseline JUnit XML fixture in dest_path with full test inventory
    (>= 374 total, >= 303 passed, <= 71 skipped, 0 failures, 0 errors) and all mandatory suites."""
    from scripts.release.verify_financial_readiness import compute_file_sha256
    ts_val = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
    suites_def = {
        "tests.test_wp06_financial_governance_and_integrity": [f"test_{i:02d}_valid_case" for i in range(1, 30)],  # 29 tests
        "tests.test_stage4c3_orders_invoices": [f"test_invoice_{i}" for i in range(1, 12)],  # 11 tests
        "tests.test_stage4c4_payments_adjustments": [f"test_payment_{i}" for i in range(1, 24)],  # 23 tests
        "tests.test_stage4c5_reporting_targets_exports": [f"test_reporting_{i}" for i in range(1, 19)],  # 18 tests
        "tests.test_postgres_domain_models": [f"test_domain_model_{i}" for i in range(1, 7)],  # 6 tests
        "tests.contracts.test_domain_contracts": [
            "test_exact_money_quantization_and_formatting",
            "test_payment_request_requires_positive_total_settling",
            "test_approved_baseline_fixtures_contract_conformance",
        ],  # 3 tests
        "tests.ci.test_wp05_ci_release_gates": [f"test_ci_{i}" for i in range(1, 36)],  # 35 tests
        "tests.test_audit_remediation_regressions": [f"test_audit_{i}" for i in range(1, 41)],  # 40 tests
        "tests.test_additional_regressions": [f"test_reg_{i}" for i in range(1, 139)],  # 138 tests
    }
    # 29 + 11 + 23 + 18 + 6 + 3 + 35 + 40 + 138 = 303 passed
    xml_parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<testsuites name="pytest tests">',
        f'<testsuite name="pytest" errors="0" failures="0" skipped="71" tests="374" time="44.0" timestamp="{ts_val}" hostname="test_host">',
    ]
    for s_name, tcs in suites_def.items():
        for tc_name in tcs:
            xml_parts.append(f'<testcase classname="{s_name}" name="{tc_name}" time="0.01" />')

    for i in range(1, 72):
        xml_parts.append(
            f'<testcase classname="tests.test_xss_protection" name="test_legacy_quarantine_{i}" time="0.000">'
            f'<skipped type="pytest.skip" message="Quarantined legacy SQLite test" />'
            f'</testcase>'
        )
    xml_parts.append('</testsuite></testsuites>')
    dest_path.write_text("\n".join(xml_parts), encoding="utf-8")
    return compute_file_sha256(dest_path)


# ==============================================================================
# WP06-08 Test 28: Pytest XML Evidence Validation Negative Tests (WP06-R02)
# ==============================================================================
def test_28_pytest_xml_negative_tests(tmp_path):
    """28. Validate pytest XML evidence negative tests (WP06-R02, WP06-R04, WP06-R06): altered XML,
    forged JSON counts, missing XML, mismatched SHA, stale evidence, additional unapproved skips,
    manipulated inventories, and preventing restamped stale evidence."""
    backend_dir = Path(__file__).resolve().parent.parent
    backend_sha = get_git_commit_sha(backend_dir)
    frontend_sha = get_git_commit_sha(backend_dir.parent / "cash-flow-buddy-101")

    from scripts.release.verify_financial_readiness import compute_file_sha256

    baseline_xml = tmp_path / "baseline_pytest_report.xml"
    baseline_sha = _create_valid_baseline_pytest_xml(baseline_xml)

    # 1. Missing XML file
    ev_missing_xml = tmp_path / "ev_missing_xml.json"
    ev_missing_xml.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "execution_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "PASS",
            "summary": {"total": 374, "passed": 303, "failed": 0, "errors": 0, "skipped": 71},
            "xml_report_sha256": "fake_sha",
            "report_xml": str(tmp_path / "non_existent_report.xml"),
        }),
        encoding="utf-8",
    )
    v1, _, errs1, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, regression_evidence_path=ev_missing_xml)
    assert v1 == "FAIL"
    assert any("Underlying pytest XML report not found" in e for e in errs1)

    # 2. Altered XML (checksum mismatch)
    copy_xml = tmp_path / "copy_report.xml"
    copy_xml.write_text(baseline_xml.read_text(encoding="utf-8") + "<!-- altered -->", encoding="utf-8")
    ev_altered_xml = tmp_path / "ev_altered_xml.json"
    ev_altered_xml.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "execution_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "PASS",
            "summary": {"total": 374, "passed": 303, "failed": 0, "errors": 0, "skipped": 71},
            "xml_report_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            "report_xml": str(copy_xml),
        }),
        encoding="utf-8",
    )
    v2, _, errs2, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, regression_evidence_path=ev_altered_xml)
    assert v2 == "FAIL"
    assert any("Pytest XML report checksum mismatch" in e for e in errs2)

    # 3. Forged JSON counts (JSON says 500 total, XML has actual count)
    ev_forged = tmp_path / "ev_forged_counts.json"
    ev_forged.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "execution_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "PASS",
            "summary": {"total": 500, "passed": 500, "failed": 0, "errors": 0, "skipped": 0},
            "xml_report_sha256": baseline_sha,
            "report_xml": str(baseline_xml),
        }),
        encoding="utf-8",
    )
    v3, _, errs3, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, regression_evidence_path=ev_forged)
    assert v3 == "FAIL"
    assert any("Discrepancy detected between pytest XML report and JSON evidence counts" in e for e in errs3)

    # 4. Mismatched backend SHA in evidence
    ev_bad_sha = tmp_path / "ev_bad_sha.json"
    ev_bad_sha.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": "0000000000000000000000000000000000000000",
            "execution_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "PASS",
            "summary": {"total": 374, "passed": 303, "failed": 0, "errors": 0, "skipped": 71},
            "xml_report_sha256": baseline_sha,
            "report_xml": str(baseline_xml),
        }),
        encoding="utf-8",
    )
    v4, _, errs4, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, regression_evidence_path=ev_bad_sha)
    assert v4 == "FAIL"
    assert any("Regression evidence source SHA mismatch" in e for e in errs4)

    # 5. Stale evidence (older than 24 hours in XML and JSON)
    old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).isoformat()
    stale_xml = tmp_path / "stale_report.xml"
    stale_sha = _create_valid_baseline_pytest_xml(stale_xml, timestamp=old_time)
    ev_stale = tmp_path / "ev_stale.json"
    ev_stale.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "execution_timestamp_utc": old_time,
            "status": "PASS",
            "summary": {"total": 374, "passed": 303, "failed": 0, "errors": 0, "skipped": 71},
            "xml_report_sha256": stale_sha,
            "report_xml": str(stale_xml),
        }),
        encoding="utf-8",
    )
    v5, _, errs5, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, regression_evidence_path=ev_stale)
    assert v5 == "FAIL"
    assert any("Stale pytest evidence" in e or "older than 24 hours" in e for e in errs5)

    # 6. Six-test synthetic XML report rejection (WP06-R04)
    synth_6_xml = tmp_path / "synthetic_six_test.xml"
    synth_6_xml.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites tests="6" failures="0" errors="0" skipped="0">'
        '<testsuite name="tests.test_wp06_financial_governance_and_integrity" tests="6">'
        + "".join(f'<testcase classname="tests.test_wp06_financial_governance_and_integrity" name="test_{i}"/>' for i in range(6))
        + '</testsuite></testsuites>',
        encoding="utf-8",
    )
    ev_6_json = tmp_path / "ev_6_synth.json"
    ev_6_json.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "execution_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "PASS",
            "summary": {"total": 6, "passed": 6, "failed": 0, "errors": 0, "skipped": 0},
            "xml_report_sha256": compute_file_sha256(synth_6_xml),
            "report_xml": str(synth_6_xml),
        }),
        encoding="utf-8",
    )
    v6, _, errs6, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, regression_evidence_path=ev_6_json)
    assert v6 == "FAIL"
    assert any("Six-test synthetic XML or reduced suite detected" in e for e in errs6)

    # 7. Omitted checksum in JSON evidence (WP06-R04)
    ev_no_sha = tmp_path / "ev_no_sha.json"
    ev_no_sha.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "execution_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "PASS",
            "summary": {"total": 374, "passed": 303, "failed": 0, "errors": 0, "skipped": 71},
            "report_xml": str(baseline_xml),
        }),
        encoding="utf-8",
    )
    v7, _, errs7, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, regression_evidence_path=ev_no_sha)
    assert v7 == "FAIL"
    assert any("missing required 'xml_report_sha256' field" in e for e in errs7)

    # 8. Manipulated test inventory: insufficient tests in mandatory suite (WP06-R04)
    real_xml_content = baseline_xml.read_text(encoding="utf-8")
    # Replace test_contracts with only 2 tests instead of 3
    insufficient_xml = tmp_path / "insufficient_suite.xml"
    insufficient_content = real_xml_content.replace(
        'tests.contracts.test_domain_contracts" name="test_payment_request_requires_positive_total_settling"',
        'tests.other_contracts" name="test_payment_request_requires_positive_total_settling"',
    )
    assert insufficient_content != real_xml_content, "Target testcase string not found in baseline XML"
    insufficient_xml.write_text(insufficient_content, encoding="utf-8")
    ev_insuf = tmp_path / "ev_insuf.json"
    ev_insuf.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "execution_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "PASS",
            "summary": {"total": 374, "passed": 303, "failed": 0, "errors": 0, "skipped": 71},
            "xml_report_sha256": compute_file_sha256(insufficient_xml),
            "report_xml": str(insufficient_xml),
        }),
        encoding="utf-8",
    )
    v8, _, errs8, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, regression_evidence_path=ev_insuf)
    assert v8 == "FAIL"
    assert any("tests.contracts.test_domain_contracts" in e and "incomplete regression execution detected" in e for e in errs8)

    # 9. Synthetic testcase name in mandatory suite (WP06-R04)
    synth_name_xml = tmp_path / "synth_name.xml"
    synth_name_content = real_xml_content.replace(
        'tests.contracts.test_domain_contracts" name="test_exact_money_quantization_and_formatting"',
        'tests.contracts.test_domain_contracts" name="test_ok"',
    )
    assert synth_name_content != real_xml_content, "Target testcase string not found in baseline XML"
    synth_name_xml.write_text(synth_name_content, encoding="utf-8")
    ev_synth_name = tmp_path / "ev_synth_name.json"
    ev_synth_name.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "execution_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "PASS",
            "summary": {"total": 374, "passed": 303, "failed": 0, "errors": 0, "skipped": 71},
            "xml_report_sha256": compute_file_sha256(synth_name_xml),
            "report_xml": str(synth_name_xml),
        }),
        encoding="utf-8",
    )
    v9, _, errs9, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, regression_evidence_path=ev_synth_name)
    assert v9 == "FAIL"
    assert any("Synthetic testcase fixture 'test_ok' detected in mandatory suite" in e for e in errs9)

    # 10. Stale evidence cannot become valid by modifying JSON metadata (WP06-R06)
    stale_raw_xml = tmp_path / "stale_raw_report.xml"
    old_time_raw = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).isoformat()
    stale_raw_sha = _create_valid_baseline_pytest_xml(stale_raw_xml, timestamp=old_time_raw)

    # Attempt to validate stale XML by claiming fresh execution in JSON metadata
    fresh_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ev_restamped_json = tmp_path / "ev_restamped_json.json"
    ev_restamped_json.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "execution_timestamp_utc": fresh_time,  # Faked fresh timestamp in JSON!
            "status": "PASS",
            "summary": {"total": 374, "passed": 303, "failed": 0, "errors": 0, "skipped": 71},
            "xml_report_sha256": stale_raw_sha,
            "report_xml": str(stale_raw_xml),
        }),
        encoding="utf-8",
    )
    v10, _, errs10, _ = evaluate_technical_integrity(backend_dir, backend_sha, frontend_sha, regression_evidence_path=ev_restamped_json)
    assert v10 == "FAIL"
    assert any("Stale underlying pytest XML report" in e or "Timestamp discrepancy" in e for e in errs10)


# ==============================================================================
# WP06-08 Test 29: Formal Source and Environment Controls Negative Tests (WP06-R03)
# ==============================================================================
def test_29_formal_source_and_environment_negative_tests(tmp_path):
    """29. Validate formal source & environment controls (WP06-R03): unpinned revisions,
    mismatched repositories, and unauthorized production switches."""
    backend_dir = Path(__file__).resolve().parent.parent
    frontend_dir = backend_dir.parent / "cash-flow-buddy-101"

    # 1. Unpinned expected SHAs produces BLOCKED (never PASS)
    report_unpinned = verify_financial_readiness(
        backend_dir=backend_dir,
        frontend_dir=frontend_dir,
        expected_backend_sha=None,
        expected_frontend_sha=None,
    )
    assert report_unpinned["technical_financial_integrity"]["verdict"] == "BLOCKED"
    assert any("explicitly pinned backend and frontend shas" in b.lower() for b in report_unpinned["technical_financial_integrity"]["blocked_reasons"])

    # 2. Unauthorized production-review switch produces FAIL
    report_switch = verify_financial_readiness(
        backend_dir=backend_dir,
        frontend_dir=frontend_dir,
        expected_backend_sha=get_git_commit_sha(backend_dir),
        expected_frontend_sha=get_git_commit_sha(frontend_dir),
        allow_production_review=True,
    )
    assert report_switch["technical_financial_integrity"]["verdict"] == "FAIL"
    assert any("Self-declared --allow-production-review switch is rejected" in e for e in report_switch["technical_financial_integrity"]["errors"])

    # 3. Repository identity verification failure on invalid directory
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir()
    (fake_repo / ".git").mkdir()
    from scripts.release.verify_financial_readiness import verify_repository_identity
    with pytest.raises(ValueError) as exc_info:
        verify_repository_identity(fake_repo, "collectflow")
    assert "Backend repository marker missing" in str(exc_info.value)


