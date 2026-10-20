#!/usr/bin/env python3
"""
CollectFlow Financial Release Readiness & Business Acceptance Gate (Release Preparation WP-06).

Evaluates and separates:
1. Technical Financial Integrity (Automated Gates: PostgreSQL domain models, Alembic schema revision,
   Decimal precision, payment/settlement invariants, issued invoice immutability, reversal pairing integrity,
   PostgreSQL regression evidence with underlying XML verification, cross-layer multi-source reconciliation
   evidence with ₹0.00 deltas and database total cross-checks).
2. Corporate Business Authorization (Governance Gates: BPD-01 Reference Masters Policy,
   BPD-02 Tax Configuration Policy, Corporate Finance Sign-Off, Business UAT Acceptance).

Acceptance Rules:
- A failed financial invariant must produce FAIL.
- Missing required test evidence or unpinned source checkpoints must produce BLOCKED or FAIL, never PASS.
- Pending Finance approval must produce BLOCKED.
- Self-declared approvals in JSON or commit metadata must be rejected.
- Application technical verification can PASS independently of Finance approval.
- Overall release authorization must remain BLOCKED until actual Corporate Finance acceptance.

Exit Codes:
- 0: Full Release Ready (Technical PASS + Business PASS)
- 2: Technical PASS, Business BLOCKED (Software verified; release blocked pending external Finance sign-off)
- 1: Technical FAIL or Corrupted/Missing Technical Prerequisites
"""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Safe defaults for standalone runner in isolated test mode
if os.environ.get("COLLECTFLOW_ENV") is None:
    os.environ["COLLECTFLOW_ENV"] = "test"
if os.environ.get("COLLECTFLOW_ENV") == "test" and "COLLECTFLOW_DATABASE_URL" not in os.environ:
    os.environ["COLLECTFLOW_DATABASE_URL"] = (
        "postgresql+psycopg://collectflow_user:collectflow_secret@127.0.0.1:5432/collectflow_test"
    )

EXPECTED_ALEMBIC_REVISION = "77e73a1cb221"
APPROVED_TEST_ENVS = {"test", "ci", "isolated_test"}
APPROVED_TEST_DATABASES = {"collectflow_test", "collectflow_ci"}
BASELINE_MIN_TOTAL_TESTS = 374
BASELINE_MIN_PASSED_TESTS = 303
BASELINE_MAX_SKIPS = 71

MANDATORY_FINANCIAL_SUITES = [
    "tests.test_wp06_financial_governance_and_integrity",
    "tests.test_stage4c3_orders_invoices",
    "tests.test_stage4c4_payments_adjustments",
    "tests.test_stage4c5_reporting_targets_exports",
    "tests.test_postgres_domain_models",
    "tests.contracts.test_domain_contracts",
]

MANDATORY_FINANCIAL_SUITE_MINIMUMS = {
    "tests.test_wp06_financial_governance_and_integrity": 29,
    "tests.test_stage4c3_orders_invoices": 11,
    "tests.test_stage4c4_payments_adjustments": 23,
    "tests.test_stage4c5_reporting_targets_exports": 18,
    "tests.test_postgres_domain_models": 6,
    "tests.contracts.test_domain_contracts": 3,
}

EXPECTED_RECONCILIATION_SOURCES = [
    "postgresql",
    "rest_api",
    "browser_ui",
    "excel_xlsx",
    "csv_export",
]

EXPECTED_RECONCILIATION_COMPARISONS = [
    "Invoiced Gross",
    "Taxable Exclusive",
    "GST Tax Amount",
    "Cash Received",
    "TDS Deducted",
    "Total Net Settled",
    "Balance Outstanding",
    "Unbilled PO Backlog",
    "Unbilled Recognized AR",
    "Annual Target",
]

EXPECTED_METRIC_APPLICABLE_SOURCES = {
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

LEGITIMATE_ARCHITECTURAL_NA = {
    "GST Tax Amount": {"browser_ui": "N/A (UI)"},
    "Unbilled PO Backlog": {"browser_ui": "N/A (UI)"},
    "Unbilled Recognized AR": {"browser_ui": "N/A (UI)"},
    "Annual Target": {"excel_xlsx": "N/A (Export)", "csv_export": "N/A (Export)"},
}

EXPECTED_RECONCILIATION_INVARIANTS = [
    "cash_plus_tds_equals_settled",
    "invoiced_minus_settled_equals_outstanding",
    "reversal_net_zero",
    "unbilled_ar_zero",
    "ar_aging_sums_to_outstanding",
    "target_reconciled",
]


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_git_commit_sha(repo_path: Path) -> str:
    """
    Resolves git commit SHA. Fails closed with ValueError if repository or commit cannot be resolved.
    No UNKNOWN fallback permitted (WP06-03).
    """
    if not repo_path or not Path(repo_path).exists():
        raise ValueError(f"CRITICAL ERROR: Repository path does not exist: {repo_path}")
    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        raise ValueError(f"CRITICAL ERROR: Path is not a Git repository (no .git directory): {repo_path}")

    res = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise ValueError(f"CRITICAL ERROR: Failed to resolve Git commit SHA for {repo_path}: {res.stderr.strip()}")

    sha = res.stdout.strip()
    if len(sha) != 40 or not all(c in "0123456789abcdefABCDEF" for c in sha):
        raise ValueError(f"CRITICAL ERROR: Invalid Git SHA resolved from {repo_path}: '{sha}'")
    return sha


def verify_repository_identity(repo_path: Path, expected_name: str) -> None:
    """
    Verifies repository directory marker files and git remote origin identity (WP06-R03.3).
    """
    if not repo_path or not Path(repo_path).exists():
        raise ValueError(f"CRITICAL ERROR: Repository path does not exist: {repo_path}")
    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        raise ValueError(f"CRITICAL ERROR: Path is not a Git repository: {repo_path}")

    if expected_name == "collectflow":
        if not (Path(repo_path) / "pyproject.toml").exists() or not (Path(repo_path) / "app").exists():
            raise ValueError(f"CRITICAL ERROR: Backend repository marker missing in {repo_path}")
    elif expected_name == "cash-flow-buddy-101":
        if not (Path(repo_path) / "package.json").exists() or not (Path(repo_path) / "src").exists():
            raise ValueError(f"CRITICAL ERROR: Frontend repository marker missing in {repo_path}")

    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0 and res.stdout.strip():
            url = res.stdout.strip().lower()
            if expected_name.lower() not in url:
                raise ValueError(
                    f"Repository identity mismatch for {repo_path}: remote origin '{url}' does not match expected '{expected_name}'."
                )
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise


def parse_governance_doc_status(path: Path) -> Tuple[str, Optional[str]]:
    """
    Parses governance document status without trusting self-declared approvals.
    Returns (status, error_message).
    """
    if not path.is_file():
        return "MISSING", f"Governance document not found: {path}"

    content = path.read_text(encoding="utf-8")
    status = "UNKNOWN"
    for line in content.splitlines():
        if "Governance Status:" in line or "Document Status:" in line or "**Status:**" in line:
            if "AWAITING_FINANCE_APPROVAL" in line:
                status = "AWAITING_FINANCE_APPROVAL"
                break
            if "APPROVED" in line:
                status = "SELF_DECLARED_APPROVAL_REJECTED"
                break

    if status == "AWAITING_FINANCE_APPROVAL":
        return "BLOCKED", None
    if status == "SELF_DECLARED_APPROVAL_REJECTED":
        return "FAIL", "Self-declared corporate approval detected in markdown without independent authentication token."
    return "UNKNOWN", f"Could not determine valid governance status in {path}"


def evaluate_technical_integrity(
    backend_dir: Path,
    backend_sha: str,
    frontend_sha: str,
    expected_backend_sha: Optional[str] = None,
    expected_frontend_sha: Optional[str] = None,
    regression_evidence_path: Optional[Path] = None,
    reconciliation_summary_path: Optional[Path] = None,
    allow_production_review: bool = False,
) -> Tuple[str, Dict[str, Any], List[str], List[str]]:
    """
    Evaluates technical financial integrity invariants, regression XML evidence, and multi-source reconciliation.
    Returns (verdict, checks, errors, blocked_reasons).
    """
    errors: List[str] = []
    blocked_reasons: List[str] = []
    checks: Dict[str, Any] = {}

    # 1. Source SHA verification & pinning against expected checkpoints (WP06-03, WP06-R03)
    if not expected_backend_sha or not expected_frontend_sha:
        err = (
            "Formal release acceptance requires explicitly pinned backend and frontend SHAs "
            "(--expected-backend-sha and --expected-frontend-sha). Unpinned evaluation cannot produce technical PASS."
        )
        checks["expected_sha_pinning"] = {
            "status": "BLOCKED",
            "expected_backend_sha": expected_backend_sha,
            "expected_frontend_sha": expected_frontend_sha,
            "error": err,
        }
        blocked_reasons.append(err)
    else:
        checks["expected_sha_pinning"] = {"status": "PASS", "pinned": True}

    if expected_backend_sha:
        if backend_sha.lower() != expected_backend_sha.lower():
            err = f"Backend SHA mismatch: expected '{expected_backend_sha}', got '{backend_sha}'"
            errors.append(err)
            checks["backend_sha_checkpoint"] = {"status": "FAIL", "expected": expected_backend_sha, "actual": backend_sha}
        else:
            checks["backend_sha_checkpoint"] = {"status": "PASS", "sha": backend_sha}
    else:
        checks["backend_sha_checkpoint"] = {"status": "BLOCKED", "sha": backend_sha, "note": "Unpinned expected SHA"}

    if expected_frontend_sha:
        if frontend_sha.lower() != expected_frontend_sha.lower():
            err = f"Frontend SHA mismatch: expected '{expected_frontend_sha}', got '{frontend_sha}'"
            errors.append(err)
            checks["frontend_sha_checkpoint"] = {"status": "FAIL", "expected": expected_frontend_sha, "actual": frontend_sha}
        else:
            checks["frontend_sha_checkpoint"] = {"status": "PASS", "sha": frontend_sha}
    else:
        checks["frontend_sha_checkpoint"] = {"status": "BLOCKED", "sha": frontend_sha, "note": "Unpinned expected SHA"}

    # 2. Database connection, environment & Alembic Schema Revision (WP06-03, WP06-R03)
    env_mode = (os.environ.get("COLLECTFLOW_ENV") or "").strip().lower()
    pg_database_name = "unknown"
    try:
        sys.path.insert(0, str(backend_dir))
        from app.core.database import get_db_session
        from sqlalchemy import text
        with get_db_session() as db:
            pg_database_name = db.execute(text("SELECT current_database();")).scalar()
            current_rev = db.execute(text("SELECT version_num FROM alembic_version;")).scalar()
            if current_rev != EXPECTED_ALEMBIC_REVISION:
                errors.append(
                    f"Alembic schema revision mismatch: expected '{EXPECTED_ALEMBIC_REVISION}', got '{current_rev}'"
                )
                checks["schema_revision"] = {"status": "FAIL", "found": current_rev, "expected": EXPECTED_ALEMBIC_REVISION}
            else:
                checks["schema_revision"] = {"status": "PASS", "revision": current_rev}

        # WP06-R03.4 & WP06-R03.5: Reject production/staging targets and reject self-declared bypass switch
        if allow_production_review:
            err = (
                "CRITICAL GOVERNANCE ABORT: Self-declared --allow-production-review switch is rejected. "
                "Synthetic release verification cannot be authorized against production/staging databases "
                "pending an independently controlled corporate governance procedure."
            )
            errors.append(err)
            checks["production_review_switch"] = {"status": "FAIL", "error": err}

        is_prod_or_staging = any(tok in env_mode for tok in ("prod", "stag")) or any(
            tok in str(pg_database_name).lower() for tok in ("prod", "stag")
        )
        if is_prod_or_staging:
            err = (
                f"CRITICAL GOVERNANCE ABORT: Production or staging database target detected "
                f"('{pg_database_name}', env='{env_mode}'). Synthetic financial verification is strictly prohibited "
                f"on production/staging environments."
            )
            errors.append(err)
            checks["dataset_environment"] = {
                "status": "FAIL",
                "database": pg_database_name,
                "environment": env_mode,
                "error": err,
            }
            dataset_classification = f"unauthorized_production_target_{pg_database_name}"
        elif env_mode not in APPROVED_TEST_ENVS or pg_database_name not in APPROVED_TEST_DATABASES:
            err = (
                f"Target database '{pg_database_name}' (env: '{env_mode}') is not an approved isolated test target "
                f"(approved databases: {sorted(APPROVED_TEST_DATABASES)}, approved envs: {sorted(APPROVED_TEST_ENVS)})."
            )
            errors.append(err)
            checks["dataset_environment"] = {
                "status": "FAIL",
                "database": pg_database_name,
                "environment": env_mode,
                "error": err,
            }
            dataset_classification = f"unapproved_target_{pg_database_name}"
        else:
            dataset_classification = f"synthetic_postgresql_{env_mode}_{pg_database_name}"
            checks["dataset_environment"] = {
                "status": "PASS",
                "database": pg_database_name,
                "environment": env_mode,
                "dataset_classification": dataset_classification,
            }

    except Exception as exc:
        errors.append(f"Database schema query error: {exc}")
        checks["schema_revision"] = {"status": "FAIL", "error": str(exc)}
        checks["dataset_environment"] = {"status": "FAIL", "error": str(exc)}
        dataset_classification = "unverified_connection"

    # 3. Direct PostgreSQL financial domain invariants & accounting totals (WP06-01)
    total_invoiced_gross = Decimal("0.00")
    total_exclusive = Decimal("0.00")
    total_tax = Decimal("0.00")
    total_invoice_settled = Decimal("0.00")
    total_invoice_outstanding = Decimal("0.00")
    total_cash = Decimal("0.00")
    total_tds = Decimal("0.00")
    total_payment_settled = Decimal("0.00")

    try:
        from app.models.domain_models import CommercialInvoice, PaymentTransaction, PurchaseOrder
        from sqlalchemy import select

        with get_db_session() as db:
            invoices = db.scalars(
                select(CommercialInvoice).where(CommercialInvoice.lifecycle_status != "CANCELLED")
            ).all()
            payments = db.scalars(select(PaymentTransaction)).all()
            reversals = [p for p in payments if p.transaction_type == "REVERSAL"]
            regular_payments = [p for p in payments if p.transaction_type == "PAYMENT"]
            unbilled_pos = db.scalars(select(PurchaseOrder).where(PurchaseOrder.status == "UNBILLED")).all()

            # Minimum population requirements
            pop_detail = {
                "invoices": len(invoices),
                "payments": len(regular_payments),
                "reversals": len(reversals),
                "unbilled_pos": len(unbilled_pos),
            }
            if len(invoices) < 1 or len(regular_payments) < 1 or len(reversals) < 1 or len(unbilled_pos) < 1:
                err = (
                    f"Insufficiently populated financial dataset cannot produce an unqualified technical PASS: "
                    f"invoices={len(invoices)} (min 1), payments={len(regular_payments)} (min 1), "
                    f"reversals={len(reversals)} (min 1), unbilled_pos={len(unbilled_pos)} (min 1)."
                )
                errors.append(err)
                checks["dataset_population"] = {"status": "FAIL", "counts": pop_detail, "error": err}
            else:
                checks["dataset_population"] = {"status": "PASS", "counts": pop_detail}

            inv_settled_ok = True
            for inv in invoices:
                inv_gross = Decimal(str(inv.total_amount))
                inv_excl = Decimal(str(inv.exclusive_amount))
                inv_tx = Decimal(str(inv.tax_amount))

                settled_on_inv = sum(
                    (Decimal(str(t.total_settled)) for t in inv.settlements if t.transaction_type in ("PAYMENT", "REVERSAL")),
                    Decimal("0.00"),
                )
                inv_out = inv_gross - settled_on_inv

                total_invoiced_gross += inv_gross
                total_exclusive += inv_excl
                total_tax += inv_tx
                total_invoice_settled += settled_on_inv
                total_invoice_outstanding += inv_out

                if inv_out < Decimal("0.00"):
                    inv_settled_ok = False
                    errors.append(f"Invoice {inv.invoice_number} is over-settled (outstanding {inv_out} < 0)")

            checks["invoice_balance_invariants"] = {
                "status": "PASS" if inv_settled_ok else "FAIL",
                "checked": len(invoices),
                "invoiced_gross": str(total_invoiced_gross),
                "exclusive_amount": str(total_exclusive),
                "tax_amount": str(total_tax),
                "total_settled": str(total_invoice_settled),
                "total_outstanding": str(total_invoice_outstanding),
            }

            accounting_totals_ok = True
            if abs(total_invoiced_gross - (total_exclusive + total_tax)) > Decimal("0.00"):
                accounting_totals_ok = False
                err = f"Accounting invariant violated: Invoiced Gross ({total_invoiced_gross}) != Exclusive ({total_exclusive}) + Tax ({total_tax})"
                errors.append(err)
            if abs(total_invoiced_gross - (total_invoice_settled + total_invoice_outstanding)) > Decimal("0.00"):
                accounting_totals_ok = False
                err = f"Accounting invariant violated: Invoiced Gross ({total_invoiced_gross}) != Settled ({total_invoice_settled}) + Outstanding ({total_invoice_outstanding})"
                errors.append(err)

            checks["accounting_totals_invariants"] = {
                "status": "PASS" if accounting_totals_ok else "FAIL",
                "gross_equals_excl_plus_tax": abs(total_invoiced_gross - (total_exclusive + total_tax)) == Decimal("0.00"),
                "gross_equals_settled_plus_outstanding": abs(total_invoiced_gross - (total_invoice_settled + total_invoice_outstanding)) == Decimal("0.00"),
            }

            pay_invariants_ok = True
            for p in payments:
                c = Decimal(str(p.cash_amount))
                t = Decimal(str(p.tds_amount))
                s = Decimal(str(p.total_settled))
                total_cash += c
                total_tds += t
                total_payment_settled += s

                expected_settled = c + t
                if abs(expected_settled - s) > Decimal("0.00"):
                    pay_invariants_ok = False
                    errors.append(f"Payment {p.remittance_number} settlement mismatch: total_settled {s} != {expected_settled}")

            if abs(total_payment_settled - total_invoice_settled) > Decimal("0.00"):
                pay_invariants_ok = False
                errors.append(
                    f"Cross-table settlement mismatch: Payments settled {total_payment_settled} != Invoices settled {total_invoice_settled}"
                )

            checks["payment_settlement_invariants"] = {
                "status": "PASS" if pay_invariants_ok else "FAIL",
                "checked": len(payments),
                "total_cash": str(total_cash),
                "total_tds": str(total_tds),
                "total_settled": str(total_payment_settled),
            }

            reversals_ok = True
            for r in reversals:
                r_cash = Decimal(str(r.cash_amount))
                r_settled = Decimal(str(r.total_settled))
                r_tds = Decimal(str(r.tds_amount))

                if r_cash >= Decimal("0.00") or r_settled >= Decimal("0.00"):
                    reversals_ok = False
                    errors.append(f"Reversal {r.remittance_number} contains non-negative amounts (cash: {r_cash}, settled: {r_settled}).")

                if not r.parent_transaction_id:
                    reversals_ok = False
                    errors.append(f"Orphan reversal {r.remittance_number}: missing parent_transaction_id.")
                    continue

                parent = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == r.parent_transaction_id))
                if not parent:
                    reversals_ok = False
                    errors.append(f"Reversal {r.remittance_number} references non-existent parent ID {r.parent_transaction_id}.")
                    continue

                if parent.invoice_id != r.invoice_id:
                    reversals_ok = False
                    errors.append(
                        f"Reversal {r.remittance_number} invoice_id ({r.invoice_id}) does not match parent transaction invoice_id ({parent.invoice_id})."
                    )

                net_settled = Decimal(str(parent.total_settled)) + r_settled
                net_cash = Decimal(str(parent.cash_amount)) + r_cash
                net_tds = Decimal(str(parent.tds_amount)) + r_tds

                if abs(net_settled) > Decimal("0.00") or abs(net_cash) > Decimal("0.00") or abs(net_tds) > Decimal("0.00"):
                    reversals_ok = False
                    errors.append(
                        f"Reversal pair {r.remittance_number} net delta is non-zero: settled={net_settled}, cash={net_cash}, tds={net_tds}."
                    )

            checks["reversal_integrity"] = {"status": "PASS" if reversals_ok else "FAIL", "checked": len(reversals)}

            unbilled_po_ids = [p.id for p in unbilled_pos]
            linked_invoices = db.scalars(
                select(CommercialInvoice).where(CommercialInvoice.po_id.in_(unbilled_po_ids))
            ).all() if unbilled_po_ids else []
            unbilled_ar = sum(
                (
                    Decimal(str(i.total_amount))
                    - sum((Decimal(str(t.total_settled)) for t in i.settlements if t.transaction_type in ("PAYMENT", "REVERSAL")), Decimal("0.00"))
                    for i in linked_invoices
                ),
                Decimal("0.00"),
            )
            if unbilled_ar != Decimal("0.00"):
                errors.append(f"Unbilled POs recognized accounts receivable: ₹{unbilled_ar}")
                checks["unbilled_po_zero_ar"] = {"status": "FAIL", "ar": str(unbilled_ar)}
            else:
                checks["unbilled_po_zero_ar"] = {"status": "PASS", "unbilled_pos": len(unbilled_pos), "ar": "₹0.00"}

    except Exception as exc:
        errors.append(f"Domain invariant inspection error: {exc}")
        checks["domain_invariants"] = {"status": "FAIL", "error": str(exc)}

    # 4. PostgreSQL regression test evidence ingestion & XML verification (WP06-R02)
    if not regression_evidence_path or not regression_evidence_path.is_file():
        checks["regression_test_evidence"] = {
            "status": "BLOCKED",
            "file": str(regression_evidence_path) if regression_evidence_path else None,
            "detail": "PostgreSQL regression test evidence file missing (pending pytest execution).",
        }
        blocked_reasons.append("Missing PostgreSQL regression test evidence file.")
    else:
        try:
            ev_content = json.loads(regression_evidence_path.read_text(encoding="utf-8"))
            ev_sha256 = compute_file_sha256(regression_evidence_path)
            ev_status = ev_content.get("status") or ev_content.get("verdict")
            ev_summary = ev_content.get("summary") or {
                "total": ev_content.get("total", 0),
                "passed": ev_content.get("passed", 0),
                "failed": ev_content.get("failed", 0),
                "skipped": ev_content.get("skipped", 0),
                "errors": ev_content.get("errors", 0),
            }
            ev_sha = ev_content.get("backend_sha") or ev_content.get("source_sha")
            ev_xml_path_str = ev_content.get("report_xml")
            ev_xml_sha = ev_content.get("xml_report_sha256")
            ev_timestamp = ev_content.get("execution_timestamp_utc")

            ev_ok = True
            ev_errs = []

            # 4.1 Bind evidence to exact tested backend SHA
            if not ev_sha or ev_sha == "UNKNOWN" or ev_sha != backend_sha:
                ev_ok = False
                ev_errs.append(f"Regression evidence source SHA mismatch: evidence has '{ev_sha}', current backend is '{backend_sha}'")

            # 4.2 Verify referenced pytest XML exists and recompute SHA-256
            if not ev_xml_sha:
                ev_ok = False
                ev_errs.append("Regression evidence JSON missing required 'xml_report_sha256' field")

            if not ev_xml_path_str:
                xml_path = backend_dir / "artifacts" / "backend_pytest_report.xml"
            else:
                xml_path = Path(ev_xml_path_str)
                if not xml_path.is_absolute():
                    xml_path = backend_dir / xml_path

            if not xml_path.is_file():
                ev_ok = False
                ev_errs.append(f"Underlying pytest XML report not found at: {xml_path}")
            else:
                actual_xml_sha = compute_file_sha256(xml_path)
                if ev_xml_sha and actual_xml_sha != ev_xml_sha:
                    ev_ok = False
                    ev_errs.append(
                        f"Pytest XML report checksum mismatch: JSON evidence has '{ev_xml_sha}', "
                        f"recalculated XML checksum is '{actual_xml_sha}' (altered XML detected)"
                    )

                # 4.3 Reparse XML and independently recalculate test counts and inventory
                try:
                    tree = ET.parse(xml_path)
                    root = tree.getroot()
                    suites = root.findall("testsuite") if root.tag == "testsuites" else ([root] if root.tag == "testsuite" else [])
                    xml_total = sum(int(s.attrib.get("tests", 0)) for s in suites)
                    xml_failures = sum(int(s.attrib.get("failures", 0)) for s in suites)
                    xml_errors = sum(int(s.attrib.get("errors", 0)) for s in suites)
                    xml_skipped = sum(int(s.attrib.get("skipped", 0)) for s in suites)
                    xml_passed = xml_total - xml_failures - xml_errors - xml_skipped

                    discovered_classes = set()
                    suite_testcases: Dict[str, List[str]] = {}
                    for s in suites:
                        for tc in s.findall("testcase"):
                            cn = tc.attrib.get("classname")
                            name = tc.attrib.get("name")
                            if cn:
                                discovered_classes.add(cn)
                                suite_testcases.setdefault(cn, []).append(name or "")

                    # Compare XML vs JSON counts
                    if (
                        xml_total != ev_summary.get("total")
                        or xml_passed != ev_summary.get("passed")
                        or xml_failures != ev_summary.get("failed")
                        or xml_errors != ev_summary.get("errors")
                        or xml_skipped != ev_summary.get("skipped")
                    ):
                        ev_ok = False
                        ev_errs.append(
                            f"Discrepancy detected between pytest XML report and JSON evidence counts: "
                            f"XML=(total={xml_total}, passed={xml_passed}, failed={xml_failures}, errors={xml_errors}, skipped={xml_skipped}) vs "
                            f"JSON=(total={ev_summary.get('total')}, passed={ev_summary.get('passed')}, failed={ev_summary.get('failed')}, errors={ev_summary.get('errors')}, skipped={ev_summary.get('skipped')}) "
                            f"(forged JSON evidence detected)"
                        )

                    # Baseline test inventory checks (WP06-R04)
                    if xml_total < BASELINE_MIN_TOTAL_TESTS:
                        ev_ok = False
                        ev_errs.append(
                            f"Incomplete backend regression: collected {xml_total} tests, expected at least {BASELINE_MIN_TOTAL_TESTS} tests. "
                            f"Six-test synthetic XML or reduced suite detected."
                        )
                    if xml_passed < BASELINE_MIN_PASSED_TESTS:
                        ev_ok = False
                        ev_errs.append(
                            f"Incomplete backend regression: passed {xml_passed} tests, expected at least {BASELINE_MIN_PASSED_TESTS} tests."
                        )
                    if xml_failures > 0 or xml_errors > 0:
                        ev_ok = False
                        ev_errs.append(f"Pytest report contains failed tests ({xml_failures}) or errors ({xml_errors})")
                    if xml_skipped > BASELINE_MAX_SKIPS:
                        ev_ok = False
                        ev_errs.append(f"Unapproved additional skipped tests: {xml_skipped} exceeds baseline threshold of {BASELINE_MAX_SKIPS}")

                    missing_mandatory_suites = [s for s in MANDATORY_FINANCIAL_SUITE_MINIMUMS if s not in discovered_classes]
                    if missing_mandatory_suites:
                        ev_ok = False
                        ev_errs.append(f"Pytest report missing mandatory financial test suite(s): {missing_mandatory_suites}")

                    for suite_name, min_tests in MANDATORY_FINANCIAL_SUITE_MINIMUMS.items():
                        if suite_name in suite_testcases:
                            actual_count = len(suite_testcases[suite_name])
                            if actual_count < min_tests:
                                ev_ok = False
                                ev_errs.append(
                                    f"Mandatory test suite '{suite_name}' executed only {actual_count} tests, "
                                    f"expected at least {min_tests} (incomplete regression execution detected)"
                                )
                            for t_name in suite_testcases[suite_name]:
                                if t_name in ("test_ok", "test_dummy", "test_placeholder", "test_fake"):
                                    ev_ok = False
                                    ev_errs.append(
                                        f"Synthetic testcase fixture '{t_name}' detected in mandatory suite '{suite_name}' "
                                        f"(synthetic unit test fixtures cannot qualify as formal execution evidence)"
                                    )

                except Exception as xml_exc:
                    ev_ok = False
                    ev_errs.append(f"Error parsing underlying pytest XML report: {xml_exc}")

            # 4.4 Freshness check
            if ev_timestamp:
                try:
                    dt = datetime.datetime.fromisoformat(ev_timestamp)
                    now = datetime.datetime.now(datetime.timezone.utc)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    if (dt - now).total_seconds() > 300:
                        ev_ok = False
                        ev_errs.append(f"Pytest evidence execution timestamp is in the future: {ev_timestamp}")
                    elif (now - dt).total_seconds() > 86400:
                        ev_ok = False
                        ev_errs.append(f"Stale pytest evidence: timestamp {ev_timestamp} is older than 24 hours")
                except Exception:
                    ev_ok = False
                    ev_errs.append(f"Invalid execution_timestamp_utc format in regression evidence: {ev_timestamp}")
            else:
                ev_ok = False
                ev_errs.append("Regression evidence missing required execution_timestamp_utc")

            if ev_status != "PASS":
                ev_ok = False
                ev_errs.append(f"Regression test evidence status is not PASS ('{ev_status}')")

            if ev_ok:
                checks["regression_test_evidence"] = {
                    "status": "PASS",
                    "checksum_sha256": ev_sha256,
                    "backend_sha": ev_sha,
                    "summary": ev_summary,
                    "xml_verified": True,
                }
            else:
                checks["regression_test_evidence"] = {
                    "status": "FAIL",
                    "checksum_sha256": ev_sha256,
                    "errors": ev_errs,
                }
                errors.extend(ev_errs)
        except Exception as exc:
            checks["regression_test_evidence"] = {"status": "FAIL", "error": str(exc)}
            errors.append(f"Invalid regression test evidence JSON: {exc}")

    # 5. Cross-layer financial reconciliation report ingestion & deep integrity (WP06-R01)
    if not reconciliation_summary_path or not reconciliation_summary_path.is_file():
        checks["cross_layer_reconciliation_evidence"] = {
            "status": "BLOCKED",
            "file": str(reconciliation_summary_path) if reconciliation_summary_path else None,
            "detail": "Cross-layer financial reconciliation report missing (pending verification run).",
        }
        blocked_reasons.append("Missing cross-layer financial reconciliation report.")
    else:
        try:
            recon_content = json.loads(reconciliation_summary_path.read_text(encoding="utf-8"))
            recon_sha256 = compute_file_sha256(reconciliation_summary_path)
            recon_status = recon_content.get("status")
            recon_level = recon_content.get("reconciliation_level")
            recon_eligible = recon_content.get("formal_acceptance_eligible")
            recon_browser_status = recon_content.get("browser_status")
            recon_b_sha = recon_content.get("backend_sha")
            recon_f_sha = recon_content.get("frontend_sha")
            recon_db = recon_content.get("database")
            recon_env = recon_content.get("environment")
            recon_run_id = recon_content.get("run_id")
            recon_run_start = recon_content.get("run_start_utc")
            recon_invariants = recon_content.get("invariants")
            recon_metrics = recon_content.get("metrics")
            recon_comparisons = recon_content.get("comparisons")
            recon_override = recon_content.get("is_synthetic_override")

            recon_ok = True
            recon_errs = []

            # 5.1 Required source revisions (WP06-R01.1, WP06-R01.2)
            if not recon_b_sha or recon_b_sha == "UNKNOWN" or not recon_f_sha or recon_f_sha == "UNKNOWN":
                recon_ok = False
                recon_errs.append(
                    f"Reconciliation evidence missing required backend_sha ('{recon_b_sha}') "
                    f"or frontend_sha ('{recon_f_sha}') source revision."
                )
            else:
                if recon_b_sha != backend_sha:
                    recon_ok = False
                    recon_errs.append(f"Reconciliation report backend SHA mismatch: report has '{recon_b_sha}', current is '{backend_sha}'")
                if recon_f_sha != frontend_sha:
                    recon_ok = False
                    recon_errs.append(f"Reconciliation report frontend SHA mismatch: report has '{recon_f_sha}', current is '{frontend_sha}'")

            # 5.2 Status, level, browser execution, formal acceptance, no override (WP06-R01.3)
            if (
                recon_status == "PARTIAL_FOUR_SOURCE"
                or recon_level == "PARTIAL_FOUR_SOURCE"
                or recon_browser_status == "NOT_EXECUTED"
            ):
                checks["cross_layer_reconciliation_evidence"] = {
                    "status": "BLOCKED",
                    "checksum_sha256": recon_sha256,
                    "reconciliation_level": recon_level,
                    "browser_status": recon_browser_status,
                    "detail": "Reconciliation is partial four-source (browser UI not executed). Full 5-source reconciliation required for formal release acceptance.",
                }
                blocked_reasons.append(
                    "Cross-layer reconciliation is partial four-source; live Playwright UI verification was not executed."
                )
                recon_ok = False
            elif recon_override is True or recon_browser_status == "TEST_OVERRIDE" or recon_eligible is False:
                recon_ok = False
                recon_errs.append(
                    f"Synthetic test browser override detected (override={recon_override}, browser={recon_browser_status}, eligible={recon_eligible}). "
                    f"Test-only overrides cannot be accepted for formal release candidate verification."
                )
            elif (
                recon_status != "PASS"
                or recon_level != "FULL_FIVE_SOURCE"
                or recon_browser_status != "PASS"
                or recon_eligible is not True
                or recon_override is not False
            ):
                recon_ok = False
                recon_errs.append(
                    f"Reconciliation evidence invalid: status='{recon_status}', level='{recon_level}', "
                    f"browser='{recon_browser_status}', eligible='{recon_eligible}', override='{recon_override}'"
                )

            # 5.3 All 5 source identities (WP06-R01.4)
            verified_srcs = recon_content.get("verified_sources") or []
            unverified_srcs = recon_content.get("unverified_sources") or []
            if set(verified_srcs) != set(EXPECTED_RECONCILIATION_SOURCES) or len(unverified_srcs) > 0:
                recon_ok = False
                recon_errs.append(
                    f"Reconciliation verified sources mismatch: expected {sorted(EXPECTED_RECONCILIATION_SOURCES)}, "
                    f"got {sorted(verified_srcs)} (unverified: {unverified_srcs})"
                )

            # 5.4 Comparisons, observed source values and recomputed delta verification (WP06-R05)
            if not isinstance(recon_comparisons, dict) or not recon_comparisons:
                recon_ok = False
                recon_errs.append("Reconciliation comparisons dictionary is empty or missing")
            else:
                for comp_metric in EXPECTED_RECONCILIATION_COMPARISONS:
                    if comp_metric not in recon_comparisons:
                        recon_ok = False
                        recon_errs.append(f"Reconciliation comparisons missing required financial metric: '{comp_metric}'")
                    else:
                        c_data = recon_comparisons[comp_metric]
                        if not isinstance(c_data, dict):
                            recon_ok = False
                            recon_errs.append(f"Reconciliation comparison for '{comp_metric}' is not a valid dictionary")
                            continue

                        expected_sources = EXPECTED_METRIC_APPLICABLE_SOURCES[comp_metric]
                        actual_sources = c_data.get("sources_compared", [])

                        # 1. Require sources_compared to match expected applicable sources (WP06-R05.4, WP06-R05.5)
                        if set(actual_sources) != set(expected_sources):
                            recon_ok = False
                            recon_errs.append(
                                f"Metric '{comp_metric}' sources_compared mismatch: expected {sorted(expected_sources)}, "
                                f"got {sorted(actual_sources)} (unexpected or missing source participation)"
                            )

                        # 2. Validate observed_values mapping (WP06-R05.1, WP06-R05.3)
                        observed_values = c_data.get("observed_values")
                        if not isinstance(observed_values, dict):
                            recon_ok = False
                            recon_errs.append(f"Metric '{comp_metric}' missing 'observed_values' mapping")
                        else:
                            observed_decimals: List[Decimal] = []
                            for src in expected_sources:
                                raw_val = observed_values.get(src)
                                if raw_val is None or raw_val in ("N/A", "MISSING_EVIDENCE", "NOT_EXECUTED", ""):
                                    recon_ok = False
                                    recon_errs.append(
                                        f"Metric '{comp_metric}' missing observed amount for applicable source '{src}' "
                                        f"(got '{raw_val}')"
                                    )
                                else:
                                    try:
                                        observed_decimals.append(Decimal(str(raw_val)))
                                    except Exception:
                                        recon_ok = False
                                        recon_errs.append(
                                            f"Metric '{comp_metric}' source '{src}' observed value is invalid Decimal: '{raw_val}'"
                                        )

                            # Validate legitimate N/A sources (WP06-R05.3)
                            for na_src, na_expected in LEGITIMATE_ARCHITECTURAL_NA.get(comp_metric, {}).items():
                                na_val = str(observed_values.get(na_src, ""))
                                if not na_val.startswith("N/A"):
                                    recon_ok = False
                                    recon_errs.append(
                                        f"Metric '{comp_metric}' source '{na_src}' expected legitimate N/A marker, got '{na_val}'"
                                    )

                            # 3. Independently recompute every reported monetary delta from observed values (WP06-R05.6)
                            if observed_decimals and len(observed_decimals) == len(expected_sources):
                                computed_max = max(observed_decimals)
                                computed_min = min(observed_decimals)
                                recomputed_delta = computed_max - computed_min

                                raw_reported_delta = str(c_data.get("max_delta", ""))
                                try:
                                    reported_delta = Decimal(raw_reported_delta)
                                    if recomputed_delta != reported_delta:
                                        recon_ok = False
                                        recon_errs.append(
                                            f"Metric '{comp_metric}' reported max_delta '{reported_delta}' does not match "
                                            f"independently recomputed delta '{recomputed_delta}' (fabricated zero-delta claim detected)"
                                        )
                                except Exception:
                                    recon_ok = False
                                    recon_errs.append(f"Metric '{comp_metric}' invalid reported max_delta: '{raw_reported_delta}'")

                                if recomputed_delta != Decimal("0.00"):
                                    recon_ok = False
                                    recon_errs.append(f"Non-zero delta for comparison '{comp_metric}': ₹{recomputed_delta}")

                        # 4. Metric status check
                        comp_status = c_data.get("status")
                        if comp_status != "MATCH (₹0.00)":
                            recon_ok = False
                            recon_errs.append(f"Metric '{comp_metric}' status is not 'MATCH (₹0.00)' (got '{comp_status}')")

            # 5.5 Invariants verification (WP06-R01.5, WP06-R01.6)
            if not isinstance(recon_invariants, dict) or not recon_invariants:
                recon_ok = False
                recon_errs.append("Reconciliation invariants dictionary is empty or missing")
            else:
                for inv_key in EXPECTED_RECONCILIATION_INVARIANTS:
                    if inv_key not in recon_invariants:
                        recon_ok = False
                        recon_errs.append(f"Reconciliation missing required statutory invariant: '{inv_key}'")
                    else:
                        val = recon_invariants[inv_key]
                        if type(val) is not bool or val is not True:
                            recon_ok = False
                            recon_errs.append(f"Reconciliation invariant '{inv_key}' is not True boolean (got {val})")

            # 5.6 Reconciliation totals vs. independently calculated PostgreSQL totals (WP06-R01.8)
            if not isinstance(recon_metrics, dict) or not recon_metrics:
                recon_ok = False
                recon_errs.append("Reconciliation metrics dictionary is empty or missing")
            else:
                expected_pg_matches = [
                    ("invoiced_gross", str(total_invoiced_gross)),
                    ("exclusive_amount", str(total_exclusive)),
                    ("tax_amount", str(total_tax)),
                    ("cash_received", str(total_cash)),
                    ("tds_deducted", str(total_tds)),
                    ("total_settled", str(total_invoice_settled)),
                    ("total_outstanding", str(total_invoice_outstanding)),
                    ("unbilled_recognized_ar", "0.00"),
                ]
                for metric_name, expected_db_val in expected_pg_matches:
                    actual_recon_val = str(recon_metrics.get(metric_name, ""))
                    try:
                        if Decimal(actual_recon_val) != Decimal(expected_db_val):
                            recon_ok = False
                            recon_errs.append(
                                f"Reconciliation totals inconsistent with PostgreSQL database for '{metric_name}': "
                                f"recon={actual_recon_val} != db={expected_db_val}"
                            )
                    except Exception:
                        recon_ok = False
                        recon_errs.append(f"Invalid metric value in reconciliation report for '{metric_name}': '{actual_recon_val}'")

            # 5.7 Database and environment identity (WP06-R01.9)
            if recon_db != pg_database_name:
                recon_ok = False
                recon_errs.append(f"Reconciliation report database mismatch: report has '{recon_db}', current is '{pg_database_name}'")
            if recon_env != env_mode:
                recon_ok = False
                recon_errs.append(f"Reconciliation report environment mismatch: report has '{recon_env}', current is '{env_mode}'")

            # 5.8 Current-run evidence identity and freshness (WP06-R01.10)
            if not recon_run_id or not isinstance(recon_run_id, str):
                recon_ok = False
                recon_errs.append("Reconciliation report missing valid run_id")
            if recon_run_start:
                try:
                    r_dt = datetime.datetime.fromisoformat(recon_run_start)
                    now = datetime.datetime.now(datetime.timezone.utc)
                    if r_dt.tzinfo is None:
                        r_dt = r_dt.replace(tzinfo=datetime.timezone.utc)
                    if (r_dt - now).total_seconds() > 300:
                        recon_ok = False
                        recon_errs.append(f"Reconciliation report timestamp is in the future: {recon_run_start}")
                    elif (now - r_dt).total_seconds() > 86400:
                        recon_ok = False
                        recon_errs.append(f"Stale reconciliation report: timestamp {recon_run_start} is older than 24 hours")
                except Exception:
                    recon_ok = False
                    recon_errs.append(f"Invalid run_start_utc format in reconciliation report: {recon_run_start}")
            else:
                recon_ok = False
                recon_errs.append("Reconciliation report missing required run_start_utc")

            if recon_ok:
                checks["cross_layer_reconciliation_evidence"] = {
                    "status": "PASS",
                    "checksum_sha256": recon_sha256,
                    "reconciliation_level": recon_level,
                    "formal_acceptance_eligible": True,
                    "verified_sources": verified_srcs,
                }
            elif "cross_layer_reconciliation_evidence" not in checks:
                checks["cross_layer_reconciliation_evidence"] = {
                    "status": "FAIL",
                    "checksum_sha256": recon_sha256,
                    "errors": recon_errs,
                }
                errors.extend(recon_errs)

        except Exception as exc:
            checks["cross_layer_reconciliation_evidence"] = {"status": "FAIL", "error": str(exc)}
            errors.append(f"Invalid reconciliation summary JSON: {exc}")

    # Determine technical verdict
    if errors:
        verdict = "FAIL"
    elif blocked_reasons:
        verdict = "BLOCKED"
    else:
        verdict = "PASS"

    checks["dataset_classification"] = dataset_classification
    return verdict, checks, errors, blocked_reasons


def evaluate_business_authorization(
    backend_dir: Path,
) -> Tuple[str, Dict[str, Any], List[str]]:
    """
    Evaluates corporate business authorization documents and statutory governance state.
    """
    errors: List[str] = []
    checks: Dict[str, Any] = {}
    evidence_hashes: Dict[str, str] = {}

    finance_docs_dir = backend_dir / "docs" / "finance"

    # 1. BPD-01 Reference Masters Review
    bpd01_path = finance_docs_dir / "COLLECTFLOW_BPD01_REFERENCE_MASTERS_REVIEW.md"
    bpd01_status, bpd01_err = parse_governance_doc_status(bpd01_path)
    checks["bpd01_reference_masters"] = {"status": bpd01_status, "file": str(bpd01_path)}
    if bpd01_err:
        errors.append(bpd01_err)
    if bpd01_path.is_file():
        evidence_hashes["BPD-01"] = compute_file_sha256(bpd01_path)

    # 2. BPD-02 Tax Configuration Review
    bpd02_path = finance_docs_dir / "COLLECTFLOW_BPD02_TAX_CONFIGURATION_REVIEW.md"
    bpd02_status, bpd02_err = parse_governance_doc_status(bpd02_path)
    checks["bpd02_tax_configuration"] = {"status": bpd02_status, "file": str(bpd02_path)}
    if bpd02_err:
        errors.append(bpd02_err)
    if bpd02_path.is_file():
        evidence_hashes["BPD-02"] = compute_file_sha256(bpd02_path)

    # 3. Business Acceptance Checklist
    chk_path = finance_docs_dir / "COLLECTFLOW_BUSINESS_ACCEPTANCE_CHECKLIST.md"
    chk_status, chk_err = parse_governance_doc_status(chk_path)
    checks["business_acceptance_checklist"] = {"status": chk_status, "file": str(chk_path)}
    if chk_err:
        errors.append(chk_err)
    if chk_path.is_file():
        evidence_hashes["CHECKLIST"] = compute_file_sha256(chk_path)

    # 4. External Corporate Finance Approval Source
    checks["external_finance_approval_service"] = {
        "status": "BLOCKED",
        "detail": "Corporate Finance Directorate approval service / signed order not integrated (AWAITING_FINANCE_APPROVAL).",
    }

    if any(s == "FAIL" for s in (bpd01_status, bpd02_status, chk_status)):
        verdict = "FAIL"
    else:
        verdict = "BLOCKED"

    return verdict, {"checks": checks, "evidence_hashes": evidence_hashes}, errors


def verify_financial_readiness(
    backend_dir: Path,
    frontend_dir: Path,
    expected_backend_sha: Optional[str] = None,
    expected_frontend_sha: Optional[str] = None,
    regression_evidence: Optional[Path] = None,
    reconciliation_summary: Optional[Path] = None,
    allow_production_review: bool = False,
    output_json: Optional[Path] = None,
) -> Dict[str, Any]:
    timestamp_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    expected_b_sha = expected_backend_sha or os.environ.get("COLLECTFLOW_EXPECTED_BACKEND_SHA")
    expected_f_sha = expected_frontend_sha or os.environ.get("COLLECTFLOW_EXPECTED_FRONTEND_SHA")

    # 0. Repository identity verification (WP06-R03.3)
    repo_identity_errors: List[str] = []
    try:
        verify_repository_identity(backend_dir, "collectflow")
    except Exception as exc:
        repo_identity_errors.append(str(exc))
    try:
        verify_repository_identity(frontend_dir, "cash-flow-buddy-101")
    except Exception as exc:
        repo_identity_errors.append(str(exc))

    # Resolve git commit SHAs (fail closed if invalid/unresolvable, WP06-03)
    sha_resolution_error = None
    try:
        backend_sha = get_git_commit_sha(backend_dir)
    except Exception as exc:
        backend_sha = "UNRESOLVED"
        sha_resolution_error = str(exc)

    try:
        frontend_sha = get_git_commit_sha(frontend_dir)
    except Exception as exc:
        frontend_sha = "UNRESOLVED"
        if not sha_resolution_error:
            sha_resolution_error = str(exc)

    print("=" * 110)
    print("COLLECTFLOW FINANCIAL RELEASE READINESS & BUSINESS ACCEPTANCE VERIFIER")
    print(f"Timestamp:    {timestamp_utc}")
    print(f"Backend SHA:  {backend_sha}")
    print(f"Frontend SHA: {frontend_sha}")
    if expected_b_sha:
        print(f"Expected B:   {expected_b_sha}")
    if expected_f_sha:
        print(f"Expected F:   {expected_f_sha}")
    print("=" * 110)

    # 1. Technical Evaluation
    if sha_resolution_error or repo_identity_errors:
        tech_verdict = "FAIL"
        tech_errors = []
        if sha_resolution_error:
            tech_errors.append(sha_resolution_error)
        tech_errors.extend(repo_identity_errors)
        tech_checks = {
            "source_sha_resolution": {
                "status": "FAIL" if sha_resolution_error else "PASS",
                "error": sha_resolution_error,
            },
            "repository_identity": {
                "status": "FAIL" if repo_identity_errors else "PASS",
                "errors": repo_identity_errors,
            },
        }
        tech_blocked: List[str] = []
        dataset_classification = "unverified_repository"
    else:
        tech_verdict, tech_checks, tech_errors, tech_blocked = evaluate_technical_integrity(
            backend_dir=backend_dir,
            backend_sha=backend_sha,
            frontend_sha=frontend_sha,
            expected_backend_sha=expected_b_sha,
            expected_frontend_sha=expected_f_sha,
            regression_evidence_path=regression_evidence,
            reconciliation_summary_path=reconciliation_summary,
            allow_production_review=allow_production_review,
        )
        tech_checks["repository_identity"] = {"status": "PASS", "verified": True}
        dataset_classification = tech_checks.get("dataset_classification", "synthetic_postgresql_isolated_test")

    print(f"\n[1] Technical Financial Integrity Gate: {tech_verdict}")
    for k, v in tech_checks.items():
        if isinstance(v, dict):
            print(f"    - {k:<36}: {v.get('status')}")
    if tech_errors:
        for err in tech_errors:
            print(f"      ERROR: {err}")
    if tech_blocked:
        for b in tech_blocked:
            print(f"      BLOCKED REASON: {b}")

    # 2. Business Authorization Evaluation
    biz_verdict, biz_data, biz_errors = evaluate_business_authorization(backend_dir)
    print(f"\n[2] Corporate Business Authorization Gate: {biz_verdict}")
    for k, v in biz_data.get("checks", {}).items():
        print(f"    - {k:<36}: {v.get('status')}")
    if biz_errors:
        for err in biz_errors:
            print(f"      NOTICE: {err}")

    # 3. Overall Release Readiness Determination
    if tech_verdict == "FAIL" or biz_verdict == "FAIL":
        overall_status = "FAIL"
        recommendation = "NEEDS ATTENTION"
    elif tech_verdict == "BLOCKED":
        overall_status = "BLOCKED"
        recommendation = "NEEDS ATTENTION"
    elif biz_verdict == "BLOCKED":
        overall_status = "BLOCKED"
        recommendation = "READY FOR INDEPENDENT RE-REVIEW"
    elif biz_verdict == "PASS" and tech_verdict == "PASS":
        overall_status = "PASS"
        recommendation = "READY FOR PRODUCTION RELEASE"
    else:
        overall_status = "BLOCKED"
        recommendation = "NEEDS ATTENTION"

    print("\n" + "=" * 110)
    print(f"OVERALL FINANCIAL RELEASE READINESS VERDICT: {overall_status}")
    print(f"  Technical Integrity:  {tech_verdict}")
    print(f"  Business Governance:  {biz_verdict}")
    print(f"  Recommendation:       {recommendation}")
    print("=" * 110)

    report = {
        "report_type": "COLLECTFLOW_FINANCIAL_RELEASE_READINESS",
        "execution_timestamp_utc": timestamp_utc,
        "backend_sha": backend_sha,
        "frontend_sha": frontend_sha,
        "expected_backend_sha": expected_b_sha,
        "expected_frontend_sha": expected_f_sha,
        "dataset_classification": dataset_classification,
        "technical_financial_integrity": {
            "verdict": tech_verdict,
            "checks": tech_checks,
            "errors": tech_errors,
            "blocked_reasons": tech_blocked,
        },
        "business_authorization": {
            "verdict": biz_verdict,
            "checks": biz_data.get("checks", {}),
            "evidence_hashes": biz_data.get("evidence_hashes", {}),
            "errors": biz_errors,
            "governance_status": "AWAITING_FINANCE_APPROVAL",
        },
        "overall_release_readiness": overall_status,
        "recommendation": recommendation,
    }

    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"\nStructured report saved to: {output_json}")

    return report


def main():
    parser = argparse.ArgumentParser(description="Verify CollectFlow Financial Release Readiness")
    parser.add_argument("--backend-dir", type=Path, default=Path(__file__).resolve().parent.parent.parent)
    parser.add_argument(
        "--frontend-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent.parent / "cash-flow-buddy-101",
    )
    parser.add_argument("--expected-backend-sha", type=str, default=None, help="Expected backend git commit SHA")
    parser.add_argument("--expected-frontend-sha", type=str, default=None, help="Expected frontend git commit SHA")
    parser.add_argument(
        "--regression-evidence",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent / "artifacts" / "backend_test_evidence.json",
        help="Path to backend regression test evidence JSON",
    )
    parser.add_argument(
        "--reconciliation-summary",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent / "artifacts" / "reconciliation_summary.json",
        help="Path to cross-layer reconciliation summary JSON",
    )
    parser.add_argument(
        "--allow-production-review",
        action="store_true",
        help="Attempt review against production/staging dataset (rejected fail-closed)",
    )
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path for report")
    args = parser.parse_args()

    report = verify_financial_readiness(
        backend_dir=args.backend_dir,
        frontend_dir=args.frontend_dir,
        expected_backend_sha=args.expected_backend_sha,
        expected_frontend_sha=args.expected_frontend_sha,
        regression_evidence=args.regression_evidence,
        reconciliation_summary=args.reconciliation_summary,
        allow_production_review=args.allow_production_review,
        output_json=args.output,
    )

    tech = report.get("technical_financial_integrity", {}).get("verdict")
    overall = report.get("overall_release_readiness")

    if overall == "PASS":
        sys.exit(0)
    elif overall == "BLOCKED" and tech == "PASS":
        sys.exit(2)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
