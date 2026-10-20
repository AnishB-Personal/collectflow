# CollectFlow Release Preparation WP-06 Implementation Plan
## Financial Integrity, Statutory Configuration Governance & Business Release Acceptance

**Author:** Antigravity Agent  
**Date:** 2026-10-20  
**Status:** IN PROGRESS (Authorized for Implementation)  
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

---

## 3. Work Package Deliverables & Architecture

### WP06-01: Statutory Reference Governance
- **Files:** `scripts/init_production_db.py`, `app/routers/settings.py`, `app/models/domain_models.py`.
- **Policy Enforcement:**
  - Reject self-declared `finance_approved` JSON values as sufficient evidence for production reference data.
  - In production/staging, fail closed with `CRITICAL GOVERNANCE ABORT` because external Finance approval mechanism is pending (BPD-01 / BPD-02 status: `AWAITING_FINANCE_APPROVAL`).
  - In isolated test environments (`COLLECTFLOW_ENV=test`), allow synthetic test configurations with `allow_test_fixture=True`.
  - Validate financial years: require start < end, Indian FY boundaries (April 1 to March 31), and reject overlapping date intervals.
  - Validate GST rates: ensure split consistency (CGST + SGST == IGST == rate).
  - Validate TDS sections: require valid positive decimal rates.
  - Preserve default-deferred reference initialization (safe no-op when config is omitted).

### WP06-02: Secure Reference Management Endpoints
- **Files:** `app/routers/settings.py`.
- **Endpoints:**
  - `POST /api/settings/financial-years`
  - `DELETE /api/settings/financial-years/{code}`
  - `POST /api/settings/taxes/gst`
  - `POST /api/settings/taxes/tds`
  - `GET /api/settings/financial-years`
  - `GET /api/settings/taxes`
- **Behavior:**
  - In production/staging, statutory reference mutations fail closed (HTTP 403 Forbidden) and record audit action `STATUTORY_MUTATION_REJECTED`.
  - In test mode, retain RBAC (`require_finance_or_admin`).
  - Prevent deletion of financial years linked to invoices, purchase orders, or sales targets.
  - Reject overlapping financial years.
  - Preserve statutory rates on historical invoices and payments (no mutating PUT/PATCH/DELETE for tax rates).

### WP06-03: Financial Integrity Regression Matrix
- **Testing:** Verify all financial domain models under PostgreSQL 16:
  - Orders & Invoices: PO uniqueness, conversion to invoice, Net-30 default due date, issued invoice immutability, unbilled orders excluded from AR, snapshot preservation.
  - Tax & Monetary Precision: Decimal arithmetic, ROUND_HALF_UP, per-line GST rounding, CGST/SGST vs IGST, no float drift.
  - Payments & Settlements: Cash + TDS = Total Settled; Invoiced - Settled = Outstanding; payment reversals (signed negative transactions, parent-reversal zero sum); idempotency keys.
  - Reports & Exports: AR aging 6 buckets, sales targets, 17-column export order, single-use export tokens.

### WP06-04: Cross-Layer Financial Reconciliation
- **File:** `scripts/verify_cross_layer_reconciliation.py`.
- **Enhancements:**
  - Remove machine-specific hardcoded paths (e.g. `/home/anishb/.local/lib`).
  - Dynamic library path and headless environment adaptation.
  - Decimal comparisons with ₹0.00 difference tolerance across DB, API, XLSX, CSV, and fresh Playwright browser UI capture.
  - If browser/UI cannot be executed in the current environment, cleanly report `NOT EXECUTED` rather than fabricating PASS.

### WP06-05: Finance Approval Evidence Package
- **Templates:**
  - `docs/finance/COLLECTFLOW_BPD01_REFERENCE_MASTERS_REVIEW.md`
  - `docs/finance/COLLECTFLOW_BPD02_TAX_CONFIGURATION_REVIEW.md`
  - `docs/finance/COLLECTFLOW_BUSINESS_ACCEPTANCE_CHECKLIST.md`
- **Governance Status:** Set explicitly to `AWAITING_FINANCE_APPROVAL`. Zero fabricated approver names, signatures, or timestamps.

### WP06-06: Machine-Readable Financial Release Gate
- **File:** `scripts/release/verify_financial_readiness.py`.
- **Logic:**
  - Evaluates Technical Financial Integrity independently from Business Release Authorization.
  - Technical checks: PostgreSQL test suites, Alembic revision, multi-source reconciliation, accounting invariants. -> Returns `PASS` or `FAIL`.
  - Business authorization checks: BPD-01 status, BPD-02 status, corporate approval verification. -> Returns `BLOCKED`.
  - Overall release status: Returns `BLOCKED` (fail-closed, exit code 2).
  - Generates structured JSON report: commit SHAs, timestamps, reconciliation deltas, evidence file hashes, and separate technical & business verdicts.

### WP06-07: Frontend Financial Governance
- **Files:** `src/components/collectflow/reference-workflows.tsx`, `src/routes/settings.tsx`.
- **Behavior:**
  - Display governance advisory on statutory reference management in live mode.
  - Ensure role checks alone do not claim statutory mutation authority.
  - Show clear error feedback when backend rejects unapproved mutations (stays on review screen, does not claim success).
  - Preserve all existing customer, invoice, and payment UI workflows.

### WP06-08: Security & Negative Test Suite
- **File:** `tests/test_wp06_financial_governance_and_integrity.py`.
- **18 Test Scenarios:**
  1. Production initialization without approved statutory configuration (deferred successfully).
  2. Unapproved GST/TDS creation attempts in production mode (HTTP 403 / BLOCKED).
  3. Unauthorized financial-year modification.
  4. Synthetic configuration submitted in production mode (rejected fail-closed).
  5. Fabricated Finance approval metadata (rejected fail-closed).
  6. Missing/expired approval evidence.
  7. Duplicate or conflicting reference records (duplicate code, overlapping FY dates).
  8. Historical invoice invariance after reference changes.
  9. Unauthorized user attempts against statutory endpoints (viewer/sales RBAC).
  10. Incorrect GST rounding (decimal precision, per-line rounding).
  11. Incorrect TDS reconciliation (cash + TDS != settled rejected).
  12. Payment idempotency and reversal integrity (signed negative, parent-reversal zero sum).
  13. Altered financial export values (reconciliation detects delta != 0.00).
  14. Stale browser financial snapshots (rejected by freshness validator).
  15. Missing approval files (release gate returns BLOCKED).
  16. Forged financial acceptance PASS (rejected fail-closed).
  17. Mismatched source commit SHAs.
  18. Pending business approval correctly returning BLOCKED.

---

## 4. Execution Schedule

1. **Step 1:** Implement WP06-01 & WP06-02 backend governance in `scripts/init_production_db.py` and `app/routers/settings.py`.
2. **Step 2:** Implement WP06-05 governance review templates in `docs/finance/`.
3. **Step 3:** Implement WP06-04 portable reconciliation harness in `scripts/verify_cross_layer_reconciliation.py`.
4. **Step 4:** Implement WP06-06 machine-readable release gate in `scripts/release/verify_financial_readiness.py`.
5. **Step 5:** Implement WP06-07 frontend governance in `src/components/collectflow/reference-workflows.tsx`.
6. **Step 6:** Implement WP06-08 test suite in `tests/test_wp06_financial_governance_and_integrity.py`.
7. **Step 7:** Run full verification across both repositories (pytest, vitest, playwright, reconciliation, lint).
8. **Step 8:** Synchronize documentation, reports, and push to remotes.
