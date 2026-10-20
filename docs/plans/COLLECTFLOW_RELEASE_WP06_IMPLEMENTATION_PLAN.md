# CollectFlow Release Preparation WP-06 Implementation Plan
## Financial Integrity, Statutory Configuration Governance & Business Release Acceptance

**Author:** Antigravity Agent  
**Date:** 2026-10-20  
**Status:** IMPLEMENTED — READY FOR INDEPENDENT RE-REVIEW  
**Target Branch:** `feature/release-wp06-financial-approval-gates` (both repositories)  
**Backend Baseline:** `8e92be265d2e2117c6d5a7c07231e1e771c80612` (Accepted WP-05)  
**Frontend Baseline:** `8289c9ca8eec03b0482b3401faffc83c1d36b0aa` (Accepted WP-05)  

---

## 1. Executive Summary & Objective

The objective of WP-06 is to establish trustworthy financial and business release acceptance gates across both CollectFlow repositories (`collectflow` backend and `cash-flow-buddy-101` frontend).

### Key Architectural Boundaries
1. **Separation of Concerns:** Application-level financial correctness (technical integrity) is kept strictly separate from corporate Finance authorization (business release acceptance).
2. **Zero False Approvals:** A technically successful implementation must never be represented as Finance-approved solely because developer tests pass.
3. **Statutory Governance:** Without independently trustworthy Finance approval mechanisms (external corporate PKI/service), statutory reference activation and production/staging reference mutations remain fail-closed and marked **BLOCKED**.
4. **Scope Exclusions:** No AWS/cloud infrastructure provisioned, no public production/staging deployment, no image publication to registries, no merges to `main`, no modification of real customer/invoice data, and no fabrication of corporate policies or approval signatures.

---

## 2. Baseline Checkpoints & Invariants

| Repository | Branch | Baseline Checkpoint | Target Branch |
| :--- | :--- | :--- | :--- |
| **Backend ([collectflow](file:///home/anishb/projects/personal/collectflow))** | `feature/release-wp05-portable-cicd` | `8e92be265d2e2117c6d5a7c07231e1e771c80612` | `feature/release-wp06-financial-approval-gates` |
| **Frontend ([cash-flow-buddy-101](file:///home/anishb/projects/personal/cash-flow-buddy-101))** | `feature/release-wp05-portable-cicd` | `8289c9ca8eec03b0482b3401faffc83c1d36b0aa` | `feature/release-wp06-financial-approval-gates` |

### Invariants Preserved
- **WP-01/02:** PostgreSQL 16 schema revision (`77e73a1cb221`), connection safety, advisory locks, additive migrations only.
- **WP-03:** Default-deferred reference initialization; synthetic test fixture rejection in production/staging.
- **WP-04:** Secure session cookies (`__Host-collectflow_session` in prod/staging), stable CSRF protection, lifespan startup validation.
- **WP-05:** Pinned immutable tools (Trivy v0.58.2 + SHA-256 `aa2c0ed6...`, Playwright `^1.64.0`), zero active vulnerability exceptions (`exceptions: []`), fail-closed candidate verification, disposable compose cleanup.
- **Accounting Invariants:**
  - `Total Settled = Cash Received + TDS Recognized`
  - `Outstanding = Invoiced Total - Net Settled`
  - Per-line GST rounding with Decimal ROUND_HALF_UP.
  - Zero floating-point drift.
  - Issued invoice immutability and historical billing snapshot preservation.
  - Strict reversal pairing net zero and invoice linkage match.

---

## 3. Work Package Deliverables & Architecture

### WP06-01: Statutory Reference Governance
- **Files:** `scripts/init_production_db.py`, `app/routers/settings.py`, `app/models/domain_models.py`.
- **Policy Enforcement:**
  - Reject self-declared `finance_approved` JSON values as sufficient evidence for production reference data.
  - In production/staging, fail closed with `CRITICAL GOVERNANCE ABORT` on both CLI loads and direct Python function calls (`init_reference_data(db, ref_config)`).
  - In isolated test environments (`COLLECTFLOW_ENV=test`), allow synthetic test configurations with `allow_test_fixture=True`.
  - Validate financial years: require start < end, Indian FY boundaries (April 1 to March 31), and reject overlapping date intervals.
  - Validate GST rates: ensure split consistency (CGST + SGST == IGST == rate).
  - Validate TDS sections: require valid positive decimal rates.
  - Preserve default-deferred reference initialization (safe no-op when config is omitted).

### WP06-02: Secure Reference Management Endpoints
- **Files:** `app/routers/settings.py`.
- **Enforcement:**
  - In production and staging modes, reject GST/TDS creation and FY modifications with HTTP 403 Forbidden.
  - Log audit records for every rejected attempt.
  - In test environments, allow mutations for test harness operations while enforcing input constraints.

### WP06-03: Technical Financial Integrity Verification
- **Files:** `scripts/release/verify_financial_readiness.py`, `scripts/release/generate_backend_test_evidence.py`.
- **Verified Invariants:**
  - Non-empty dataset requirement: min counts >= 1 for invoices, payments, reversals, and unbilled POs.
  - Reversal integrity: strict parent existence, matching invoice linkage, and exact net zero settlement pairing.
  - Accounting totals: Invoiced Gross = Excl + Tax, and Invoiced Gross = Settled + Outstanding.
  - PostgreSQL regression test evidence verification against git SHA and checksum.
  - Multi-source cross-layer reconciliation evidence verification against git SHA, database, and reconciliation level.

### WP06-04: Cross-Layer Financial Reconciliation Harness
- **Script:** `scripts/verify_cross_layer_reconciliation.py`.
- Reconciles 5 independent sources: PostgreSQL, Backend REST API, Browser UI (Playwright DOM), Excel workbook, CSV export.
- Differentiates full 5-source reconciliation (`FULL_FIVE_SOURCE PASS`) from partial 4-source runs (`PARTIAL_FOUR_SOURCE BLOCKED`) and test overrides (`TEST_OVERRIDE FAIL`).
- Zero monetary tolerance (₹0.00 delta across all metrics).

### WP06-05: Finance & Business Sign-off Governance Package
- Documents created under `docs/finance/`:
  - `COLLECTFLOW_BPD01_REFERENCE_MASTERS_REVIEW.md`
  - `COLLECTFLOW_BPD02_TAX_CONFIGURATION_REVIEW.md`
  - `COLLECTFLOW_BUSINESS_ACCEPTANCE_CHECKLIST.md`
- Status maintained as `AWAITING_FINANCE_APPROVAL`. Zero fabricated approvals or signatures.

---

## 4. Verification & Gate Execution

1. Full backend pytest regression: `pytest tests/ --junitxml=artifacts/backend_pytest_report.xml` (300 passed, 71 skipped, 0 failed).
2. Generate backend regression evidence: `scripts/release/generate_backend_test_evidence.py` -> `artifacts/backend_test_evidence.json`.
3. Frontend vitest regression: `npm test -- --run` (44 files passed, 620 tests passed).
4. Cross-layer reconciliation: `scripts/verify_cross_layer_reconciliation.py --output artifacts/reconciliation_summary.json` (5-source ₹0.00 delta MATCH).
5. Financial readiness gate: `scripts/release/verify_financial_readiness.py --output artifacts/financial_readiness_manifest.json` (Technical PASS, Business BLOCKED, Exit code 2).
