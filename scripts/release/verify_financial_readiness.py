#!/usr/bin/env python3
"""
CollectFlow Financial Release Readiness & Business Acceptance Gate (Release Preparation WP-06).

Evaluates and separates:
1. Technical Financial Integrity (Automated Gates: PostgreSQL domain models, Alembic schema revision,
   Decimal precision, payment/settlement invariants, issued invoice immutability, reversal pairing integrity,
   PostgreSQL regression evidence, cross-layer reconciliation verification).
2. Corporate Business Authorization (Governance Gates: BPD-01 Reference Masters Policy,
   BPD-02 Tax Configuration Policy, Corporate Finance Sign-Off, Business UAT Acceptance).

Acceptance Rules:
- A failed financial invariant must produce FAIL.
- Missing required test evidence must produce BLOCKED or FAIL, never PASS.
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
                # WP06-01 / WP06-06: Self-declared approval string in markdown is rejected without external PKI
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
    Evaluates technical financial integrity invariants.
    Returns (verdict, checks, errors, blocked_reasons).
    """
    errors: List[str] = []
    blocked_reasons: List[str] = []
    checks: Dict[str, Any] = {}

    # 1. Source SHA verification against expected checkpoints (WP06-03)
    if expected_backend_sha:
        if backend_sha.lower() != expected_backend_sha.lower():
            err = f"Backend SHA mismatch: expected '{expected_backend_sha}', got '{backend_sha}'"
            errors.append(err)
            checks["backend_sha_checkpoint"] = {"status": "FAIL", "expected": expected_backend_sha, "actual": backend_sha}
        else:
            checks["backend_sha_checkpoint"] = {"status": "PASS", "sha": backend_sha}
    else:
        checks["backend_sha_checkpoint"] = {"status": "PASS", "sha": backend_sha, "note": "Unpinned expected SHA"}

    if expected_frontend_sha:
        if frontend_sha.lower() != expected_frontend_sha.lower():
            err = f"Frontend SHA mismatch: expected '{expected_frontend_sha}', got '{frontend_sha}'"
            errors.append(err)
            checks["frontend_sha_checkpoint"] = {"status": "FAIL", "expected": expected_frontend_sha, "actual": frontend_sha}
        else:
            checks["frontend_sha_checkpoint"] = {"status": "PASS", "sha": frontend_sha}
    else:
        checks["frontend_sha_checkpoint"] = {"status": "PASS", "sha": frontend_sha, "note": "Unpinned expected SHA"}

    # 2. Database connection, environment & Alembic Schema Revision (WP06-03)
    env_mode = (os.environ.get("COLLECTFLOW_ENV") or "test").strip().lower()
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

        is_prod_or_staging = any(tok in env_mode for tok in ("prod", "stag")) or any(
            tok in str(pg_database_name).lower() for tok in ("prod", "stag")
        )
        if is_prod_or_staging:
            dataset_classification = f"production_or_staging_{pg_database_name}"
            if not allow_production_review:
                err = (
                    f"Production or staging database target detected ('{pg_database_name}', env='{env_mode}') "
                    f"for synthetic financial verification without authorized read-only review procedure (--allow-production-review)."
                )
                errors.append(err)
                checks["dataset_environment"] = {
                    "status": "FAIL",
                    "database": pg_database_name,
                    "environment": env_mode,
                    "error": err,
                }
            else:
                checks["dataset_environment"] = {
                    "status": "PASS",
                    "database": pg_database_name,
                    "environment": env_mode,
                    "dataset_classification": dataset_classification,
                    "note": "Authorized read-only production review mode",
                }
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

            # WP06-01.1: Prevent empty or insufficiently populated financial datasets from producing technical PASS
            min_counts_ok = True
            pop_detail = {
                "invoices": len(invoices),
                "payments": len(regular_payments),
                "reversals": len(reversals),
                "unbilled_pos": len(unbilled_pos),
            }
            if len(invoices) < 1 or len(regular_payments) < 1 or len(reversals) < 1 or len(unbilled_pos) < 1:
                min_counts_ok = False
                err = (
                    f"Insufficiently populated financial dataset cannot produce an unqualified technical PASS: "
                    f"invoices={len(invoices)} (min 1), payments={len(regular_payments)} (min 1), "
                    f"reversals={len(reversals)} (min 1), unbilled_pos={len(unbilled_pos)} (min 1)."
                )
                errors.append(err)
                checks["dataset_population"] = {"status": "FAIL", "counts": pop_detail, "error": err}
            else:
                checks["dataset_population"] = {"status": "PASS", "counts": pop_detail}

            # WP06-01.4: Independently verify invoice settlement, outstanding balances and accounting totals
            total_invoiced_gross = Decimal("0.00")
            total_exclusive = Decimal("0.00")
            total_tax = Decimal("0.00")
            total_invoice_settled = Decimal("0.00")
            total_invoice_outstanding = Decimal("0.00")

            inv_settled_ok = True
            for inv in invoices:
                inv_gross = Decimal(str(inv.total_amount))
                inv_excl = Decimal(str(inv.exclusive_amount))
                inv_tx = Decimal(str(inv.tax_amount))

                # Lineage settlement sum from transactions
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

            # Accounting totals integrity: Gross = Excl + Tax, and Gross = Settled + Outstanding
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

            # Payment transactions invariant: Total Settled = Cash + TDS
            total_cash = Decimal("0.00")
            total_tds = Decimal("0.00")
            total_payment_settled = Decimal("0.00")
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
                    errors.append(
                        f"Payment {p.remittance_number} settlement mismatch: total_settled {s} != {expected_settled}"
                    )
            # Payments total settled must equal invoices total settled
            if abs(total_payment_settled - total_invoice_settled) > Decimal("0.00"):
                pay_invariants_ok = False
                errors.append(
                    f"Cross-table settlement mismatch: Payment transactions settled {total_payment_settled} != Invoices settled {total_invoice_settled}"
                )

            checks["payment_settlement_invariants"] = {
                "status": "PASS" if pay_invariants_ok else "FAIL",
                "checked": len(payments),
                "total_cash": str(total_cash),
                "total_tds": str(total_tds),
                "total_settled": str(total_payment_settled),
            }

            # WP06-01.5: Reversal integrity: reject missing parents, mismatched invoice linkage, invalid pairing
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

                # Verify invoice linkage match
                if parent.invoice_id != r.invoice_id:
                    reversals_ok = False
                    errors.append(
                        f"Reversal {r.remittance_number} invoice_id ({r.invoice_id}) does not match parent transaction invoice_id ({parent.invoice_id})."
                    )

                # Verify exact net zero pairing
                net_settled = Decimal(str(parent.total_settled)) + r_settled
                net_cash = Decimal(str(parent.cash_amount)) + r_cash
                net_tds = Decimal(str(parent.tds_amount)) + r_tds

                if abs(net_settled) > Decimal("0.00") or abs(net_cash) > Decimal("0.00") or abs(net_tds) > Decimal("0.00"):
                    reversals_ok = False
                    errors.append(
                        f"Reversal pair {r.remittance_number} net delta is non-zero: settled={net_settled}, cash={net_cash}, tds={net_tds}."
                    )

            checks["reversal_integrity"] = {"status": "PASS" if reversals_ok else "FAIL", "checked": len(reversals)}

            # Check unbilled POs: must not generate accounts receivable
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

    # 4. PostgreSQL regression test evidence ingestion (WP06-01.2)
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
            passed_cnt = ev_summary.get("passed", 0)
            failed_cnt = ev_summary.get("failed", 0)
            error_cnt = ev_summary.get("errors", 0)

            ev_ok = True
            ev_errs = []
            if ev_sha != backend_sha:
                ev_ok = False
                ev_errs.append(f"Evidence source SHA mismatch: evidence has '{ev_sha}', current backend is '{backend_sha}'")
            if failed_cnt > 0 or error_cnt > 0:
                ev_ok = False
                ev_errs.append(f"Regression tests contain failures ({failed_cnt}) or errors ({error_cnt})")
            if passed_cnt <= 0:
                ev_ok = False
                ev_errs.append(f"Regression tests reported zero passed tests ({passed_cnt})")
            if ev_status != "PASS":
                ev_ok = False
                ev_errs.append(f"Regression test evidence status is not PASS ('{ev_status}')")

            if ev_ok:
                checks["regression_test_evidence"] = {
                    "status": "PASS",
                    "checksum_sha256": ev_sha256,
                    "backend_sha": ev_sha,
                    "summary": ev_summary,
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

    # 5. Cross-layer financial reconciliation report ingestion (WP06-01.3 & WP06-04)
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
            recon_sha = recon_content.get("backend_sha")
            recon_db = recon_content.get("database")
            recon_invariants = recon_content.get("invariants", {})

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
            elif (
                recon_browser_status == "TEST_OVERRIDE"
                or recon_content.get("is_synthetic_override")
                or recon_eligible is False
            ):
                checks["cross_layer_reconciliation_evidence"] = {
                    "status": "FAIL",
                    "checksum_sha256": recon_sha256,
                    "reconciliation_level": recon_level,
                    "browser_status": recon_browser_status,
                    "detail": "Synthetic test browser override detected. Test-only overrides cannot be accepted for formal release candidate verification.",
                }
                errors.append("Reconciliation report contains synthetic test browser override.")
            elif recon_status != "PASS" or recon_level != "FULL_FIVE_SOURCE":
                checks["cross_layer_reconciliation_evidence"] = {
                    "status": "FAIL",
                    "checksum_sha256": recon_sha256,
                    "reconciliation_level": recon_level,
                    "status_found": recon_status,
                }
                errors.append(f"Reconciliation report status '{recon_status}' / level '{recon_level}' is not accepted.")
            else:
                recon_errs = []
                if recon_sha and recon_sha != "UNKNOWN" and recon_sha != backend_sha:
                    recon_errs.append(
                        f"Reconciliation report backend SHA mismatch: report has '{recon_sha}', current is '{backend_sha}'"
                    )
                if recon_db and pg_database_name != "unknown" and recon_db != pg_database_name:
                    recon_errs.append(
                        f"Reconciliation report database mismatch: report has '{recon_db}', current is '{pg_database_name}'"
                    )
                if not all(recon_invariants.values()):
                    failed_invs = [k for k, v in recon_invariants.items() if not v]
                    recon_errs.append(f"Reconciliation invariants failed: {failed_invs}")

                if not recon_errs:
                    checks["cross_layer_reconciliation_evidence"] = {
                        "status": "PASS",
                        "checksum_sha256": recon_sha256,
                        "reconciliation_level": recon_level,
                        "formal_acceptance_eligible": True,
                        "verified_sources": recon_content.get("verified_sources", []),
                    }
                else:
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
    # Currently no external PKI / governance service is integrated
    checks["external_finance_approval_service"] = {
        "status": "BLOCKED",
        "detail": "Corporate Finance Directorate approval service / signed order not integrated (AWAITING_FINANCE_APPROVAL).",
    }

    # Business verdict determination
    if any(s == "FAIL" for s in (bpd01_status, bpd02_status, chk_status)):
        verdict = "FAIL"
    else:
        # Expected state: all documents are AWAITING_FINANCE_APPROVAL -> BLOCKED
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
    if sha_resolution_error:
        tech_verdict = "FAIL"
        tech_checks = {
            "source_sha_resolution": {
                "status": "FAIL",
                "error": sha_resolution_error,
            }
        }
        tech_errors = [sha_resolution_error]
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
        dataset_classification = tech_checks.get("dataset_classification", "synthetic_postgresql_isolated_test")

    print(f"\n[1] Technical Financial Integrity Gate: {tech_verdict}")
    for k, v in tech_checks.items():
        if isinstance(v, dict):
            print(f"    - {k:<34}: {v.get('status')}")
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
        print(f"    - {k:<34}: {v.get('status')}")
    if biz_errors:
        for err in biz_errors:
            print(f"      NOTICE: {err}")

    # 3. Overall Release Readiness Determination
    # Rules:
    # - If technical integrity FAIL -> Overall FAIL
    # - If technical integrity BLOCKED -> Overall BLOCKED
    # - If technical integrity PASS and business authorization BLOCKED -> Overall BLOCKED
    # - If both PASS -> Overall PASS
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
        help="Authorize read-only review against production/staging dataset",
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
        # Technical PASS, Business BLOCKED (software ready, waiting on external Finance approval)
        sys.exit(2)
    else:
        # Technical FAIL or BLOCKED
        sys.exit(1)


if __name__ == "__main__":
    main()
