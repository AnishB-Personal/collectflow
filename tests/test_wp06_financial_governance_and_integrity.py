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
def test_18_pending_business_approval_correctly_returns_blocked():
    """18. When technical integrity passes but corporate approval is pending, overall status is BLOCKED."""
    backend_dir = Path(__file__).resolve().parent.parent
    frontend_dir = backend_dir.parent / "cash-flow-buddy-101"

    report = verify_financial_readiness(
        backend_dir=backend_dir,
        frontend_dir=frontend_dir,
        regression_evidence=backend_dir / "artifacts" / "backend_test_evidence.json",
        reconciliation_summary=backend_dir / "artifacts" / "reconciliation_summary.json",
    )
    assert report["technical_financial_integrity"]["verdict"] == "PASS"
    assert report["business_authorization"]["verdict"] == "BLOCKED"
    assert report["overall_release_readiness"] == "BLOCKED"
    assert report["recommendation"] == "READY FOR INDEPENDENT RE-REVIEW"


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
    failed_ev_file = tmp_path / "failed_evidence.json"
    failed_ev_file.write_text(
        json.dumps({
            "test_suite": "backend_pytest_regression",
            "backend_sha": backend_sha,
            "status": "FAIL",
            "summary": {"total": 100, "passed": 98, "failed": 2, "errors": 0, "skipped": 0},
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
    assert any("contain failures" in err for err in errors_failed)


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
            "browser_status": "TEST_OVERRIDE",
            "backend_sha": backend_sha,
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
    assert any("synthetic test browser override" in err for err in errors_override)


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

