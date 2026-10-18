"""
Negative regression tests proving that unavailable browser capture, stale snapshot data,
invalid provenance, or mismatching UI totals cannot produce a PASS result in CF4C6-UAT-02.
"""

import datetime
from decimal import Decimal
import os
import subprocess
from unittest.mock import patch, MagicMock
import pytest

from scripts.verify_cross_layer_reconciliation import (
    validate_browser_snapshot,
    execute_fresh_browser_capture,
    verify_cross_layer_reconciliation,
)


def _valid_snapshot_payload(run_id: str, offset_seconds: int = 0) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_seconds)
    return {
        "run_id": run_id,
        "captured_at": now.isoformat(),
        "source": "Playwright Headless Chromium DOM",
        "frontend_url": "http://127.0.0.1:3000",
        "invoiced_gross": "531000.00",
        "exclusive_amount": "450000.00",
        "tax_amount": None,
        "cash_amount": "200000.00",
        "tds_amount": "9000.00",
        "total_settled": "209000.00",
        "total_outstanding": "322000.00",
        "unbilled_po_gross": None,
        "unbilled_recognized_ar": None,
        "annual_target": "7500000.00",
    }


def test_validation_accepts_fresh_valid_snapshot():
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = _valid_snapshot_payload("run_test_001", offset_seconds=0)
    result = validate_browser_snapshot(payload, "run_test_001", now)
    assert result["run_id"] == "run_test_001"
    assert result["source"] == "Playwright Headless Chromium DOM"


def test_validation_rejects_non_dict_payload():
    now = datetime.datetime.now(datetime.timezone.utc)
    with pytest.raises(ValueError, match="FATAL: Browser snapshot payload must be a JSON object"):
        validate_browser_snapshot(None, "run_001", now)

    with pytest.raises(ValueError, match="FATAL: Browser snapshot payload must be a JSON object"):
        validate_browser_snapshot("invalid_string", "run_001", now)


def test_validation_rejects_untrusted_provenance():
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = _valid_snapshot_payload("run_001")
    payload["source"] = "Canonical Playwright Browser DOM Presentation Baseline"  # Simulated hardcoded fallback

    with pytest.raises(ValueError, match="Provenance check failed"):
        validate_browser_snapshot(payload, "run_001", now)


def test_validation_rejects_run_id_mismatch():
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = _valid_snapshot_payload("old_stale_run_123")

    with pytest.raises(ValueError, match="Run ID mismatch"):
        validate_browser_snapshot(payload, "current_run_456", now)


def test_validation_rejects_stale_timestamp():
    now = datetime.datetime.now(datetime.timezone.utc)
    # Stale by 10 minutes (e.g. from previous run)
    payload = _valid_snapshot_payload("run_001", offset_seconds=-600)

    with pytest.raises(ValueError, match="Stale snapshot detected"):
        validate_browser_snapshot(payload, "run_001", now)


def test_validation_rejects_missing_ui_metrics():
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = _valid_snapshot_payload("run_001")
    payload["total_outstanding"] = None  # Missing critical UI metric

    with pytest.raises(ValueError, match="missing required UI metric"):
        validate_browser_snapshot(payload, "run_001", now)


def test_reconciliation_fails_on_capture_process_error():
    now = datetime.datetime.now(datetime.timezone.utc)

    # Simulate node capture script exiting with code 1
    mock_res = MagicMock()
    mock_res.returncode = 1
    mock_res.stdout = "Navigation error"
    mock_res.stderr = "net::ERR_CONNECTION_REFUSED"

    with patch("subprocess.run", return_value=mock_res):
        with pytest.raises(RuntimeError, match="Playwright browser DOM capture failed"):
            execute_fresh_browser_capture("run_mock_fail", now)


def test_reconciliation_fails_on_mismatching_browser_invoiced_gross():
    mismatched_browser = _valid_snapshot_payload("run_mismatch")
    mismatched_browser["invoiced_gross"] = "999999.00"  # Fabricated mismatch

    with pytest.raises(AssertionError):
        verify_cross_layer_reconciliation(browser_override=mismatched_browser)


def test_reconciliation_fails_on_mismatching_browser_outstanding_balance():
    mismatched_browser = _valid_snapshot_payload("run_mismatch")
    mismatched_browser["total_outstanding"] = "100.00"  # Fabricated mismatch

    with pytest.raises(AssertionError):
        verify_cross_layer_reconciliation(browser_override=mismatched_browser)


def test_reconciliation_fails_on_mismatching_browser_sales_target():
    mismatched_browser = _valid_snapshot_payload("run_mismatch")
    mismatched_browser["annual_target"] = "5000000.00"  # Different target

    with pytest.raises(AssertionError):
        verify_cross_layer_reconciliation(browser_override=mismatched_browser)
