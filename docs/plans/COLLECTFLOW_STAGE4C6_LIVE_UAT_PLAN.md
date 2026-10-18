# CollectFlow — Stage 4C.6 Full LIVE Integration, Playwright UAT & Financial Reconciliation Plan

**Stage:** 4C.6 — Full LIVE Integration, Playwright UAT & Final Financial Reconciliation  
**Status:** AUTHORIZED FOR EXECUTION  
**Date:** 2026-10-18  
**Branches:** `feature/phase4-stage4c6-live-uat` in `collectflow` and `cash-flow-buddy-101`  
**Accepted Baseline Commits:**
- Backend (`collectflow`): `9e4e66fcecc56020e9adfe790124b0bb0489f466`
- Frontend (`cash-flow-buddy-101`): `8727372709b4328a3717cbac6eeca5f62f91c5bc`

---

## 1. Executive Overview & Objectives

Stage 4C.6 serves as the definitive end-to-end user acceptance and financial reconciliation phase for the CollectFlow PostgreSQL platform. Following independent acceptance of Stages 4C.2 through 4C.5, Stage 4C.6 conducts:
1. End-to-end LIVE browser acceptance testing using Playwright across 20+ distinct multi-role business scenarios.
2. Responsive verification across 4 canonical viewport profiles (1440px desktop, 768px tablet, 390px mobile, 320px narrow mobile).
3. Exact zero-tolerance financial reconciliation ($\Delta = ₹0.00$) against the approved synthetic baseline.
4. Rigorous verification of accounting invariants, operational correction date policies, single-use export tickets, and RBAC authorization boundaries.
5. Regression execution across backend PostgreSQL pytest, frontend Vitest, TypeScript typecheck, production build, and Playwright suites.

This is strictly an acceptance-preparation and verification stage; no speculative feature development or changes to previously accepted functionality are permitted.

---

## 2. Test Environment & Isolation Architecture

### 2.1 Isolated PostgreSQL Environment
- **Instance:** Local loopback PostgreSQL 16 on `127.0.0.1:5432` (`collectflow_test`).
- **Isolation Enforcement:** Controlled via `app.core.database.validate_isolated_database_url`. Rejects remote hosts, non-standard ports, and non-whitelisted database names.
- **Connection DSN:** `postgresql+psycopg://collectflow_user:collectflow_secret@127.0.0.1:5432/collectflow_test`.
- **Mode:** `COLLECTFLOW_ENV=test` (FastAPI backend).

### 2.2 Frontend Application Server
- **Server:** TanStack Start / Vite SSR development server on `http://127.0.0.1:3000`.
- **Configuration:** `VITE_API_MODE=live`, `VITE_API_BASE_URL=http://127.0.0.1:8000`.
- **State Management:** Session tokens stored in-memory (`sessionStorage` fallback); no synthetic mock fallbacks during live operations.

### 2.3 Synthetic Test Data Isolation
- **Seed Source:** `scripts/seed_postgres_fixtures.py`.
- **Baseline Invariance:** Pre-test baseline is deterministically verified before mutations. All ad-hoc UAT test transactions (customers, orders, invoices, payments) use unique deterministic suffixes (e.g., `UAT-{timestamp}`) and are scoped to prevent collision with or corruption of the approved synthetic baseline (`INV-2026-042`, `PO-NEXUS-881`, `REM-2026-001`).

---

## 3. Approved Synthetic Baseline & Mandatory Financial Reconciliation

### 3.1 Synthetic Baseline Entities
1. **Commercial Invoice `INV-2026-042`**:
   - Customer: Acme Cloud Solutions Pvt Ltd (`27AABCA1234A1Z5`, Maharashtra - Inter-state IGST or CGST/SGST split).
   - Invoiced Date: 2026-04-12 | Due Date: 2026-05-12 (Net-30).
   - Exclusive Amount: ₹4,50,000.00 | GST (18%): ₹81,000.00 | Gross Invoiced: ₹5,31,000.00.
   - Cash Received: ₹2,00,000.00 | TDS Deducted: ₹9,000.00 (under Section 194C @ 2%).
   - Total Settled: ₹2,09,000.00.
   - Outstanding Receivable: ₹3,22,000.00.
2. **Unbilled Purchase Order `PO-NEXUS-881`**:
   - Customer: Acme Cloud Solutions Pvt Ltd.
   - Exclusive: ₹2,50,000.00 | GST (18%): ₹45,000.00 | Gross: ₹2,95,000.00.
   - Lifecycle Status: `UNBILLED`.
   - Recognized Accounts Receivable: **₹0.00**.
3. **Sales Target**:
   - Sales Representative: `sandbox_sales`.
   - Financial Year: `FY 2026-27`.
   - Team Quota: ₹75,00,000.00 (H1: ₹37,50,000.00 | Q1: ₹18,75,000.00 | Monthly: ₹6,25,000.00).

### 3.2 Required Accounting Invariants
Every financial report, invoice drawer, ledger, and exported workbook must satisfy:
$$\text{Net Cash} + \text{Net TDS} = \text{Net Settled}$$
$$\text{Issued Gross Invoiced} - \text{Net Settled} = \text{Balance Outstanding}$$
$$\text{Unbilled PO Recognized AR} = ₹0.00$$
$$\sum_{i=1}^{6} \text{Aging Bucket Amount}_i = \text{Included Outstanding AR}$$
$$\text{Invoice Ledger Totals} = \text{Reporting Summary Totals} = \text{Export File Totals}$$

**Acceptable Monetary Reconciliation Difference: ₹0.00** across all calculations (enforced with Python `Decimal` / integer paise comparisons).

### 3.3 Historical Aging Anchor Specification
- **As-Of Anchor Date:** `2026-04-30`.
- **Target Record:** `INV-2026-042` (Due date: `2026-05-12`).
- **Overdue Days:** $0$ days overdue ($2026\text{-}04\text{-}30 \le 2026\text{-}05\text{-}12$).
- **Expected Bucket:** `Current / Not Due`.

---

## 4. Role-Based Access Control (RBAC) Specification

| Action / Resource | `admin` (`sandbox_admin`) | `finance` (`sandbox_finance`) | `sales` (`sandbox_sales`) | `viewer` (`sandbox_viewer`) |
| :--- | :---: | :---: | :---: | :---: |
| **View Dashboard / Reports** | Allowed (All) | Allowed (All) | Allowed (All) | Allowed (All) |
| **Customer Master (Read)** | Allowed (All) | Allowed (All) | Allowed (All) | Allowed (All) |
| **Customer Master (Write)** | Allowed | Allowed | Denied (403) | Denied (403) |
| **Purchase Orders (Create)** | Allowed | Allowed | Allowed | Denied (403) |
| **Convert PO to Invoice** | Allowed | Allowed | Allowed | Denied (403) |
| **Cancel Invoice (Unsettled)** | Allowed | Allowed | Denied (403) | Denied (403) |
| **Record Payment / TDS** | Allowed | Allowed | Denied (403) | Denied (403) |
| **Payment Correction / Reversal** | Allowed | Allowed | Denied (403) | Denied (403) |
| **Sales Targets (View)** | Allowed (All) | Allowed (All) | Own Target Only | Allowed (All) |
| **Sales Targets (Configure)** | Allowed | Allowed | Denied (403) | Denied (403) |
| **Generate Export Ticket** | Allowed | Allowed | Denied (403) | Denied (403) |
| **Download Export File** | Allowed (with Ticket) | Allowed (with Ticket) | Denied (403) | Denied (403) |
| **User Management** | Allowed | Denied (403) | Denied (403) | Denied (403) |

---

## 5. Live Business Workflow Verification Matrix (20+ Acceptance Scenarios)

### Domain A: Authentication & RBAC Boundaries
- **UAT-AUTH-01:** Valid authentication for all 4 roles (`sandbox_admin`, `sandbox_finance`, `sandbox_sales`, `sandbox_viewer`) returns valid tokens and renders live workspace.
- **UAT-AUTH-02:** Invalid credentials rejection (401), lockout safety, and UI error alert banner.
- **UAT-AUTH-03:** Unauthenticated direct navigation to protected routes (`/customers`, `/orders`, `/invoices`, `/payments`, `/reports`, `/sales-targets`, `/users`) immediately redirects to `/login`.
- **UAT-AUTH-04:** Session termination via Sign Out revokes server-side session and prevents back-navigation.
- **UAT-AUTH-05:** User administration: Create user, role elevation, role restriction, soft deactivation; verify deactivated user targets and historical records remain intact. Last admin deactivation protection.

### Domain B: Customer Master Operations
- **UAT-CUST-01:** Create new customer with complete statutory profile (GSTIN, State, Contact, Address).
- **UAT-CUST-02:** Duplicate billing name rejection with 409 Conflict.
- **UAT-CUST-03:** Live search, filter, and selection of customer in order/invoice creation flows.

### Domain C: Purchase Orders, Invoices & Conversion Engine
- **UAT-ORD-01:** Create multi-line Purchase Order (`PO-LIVE-...`), verify Net Taxable Turnover and 18% GST calculation.
- **UAT-ORD-02:** Unbilled PO invariance: verify that newly created unbilled PO contributes ₹0.00 to recognized accounts receivable.
- **UAT-ORD-03:** Convert PO to Commercial Invoice: verify Net-30 default due date, statutory customer snapshot immutability, and PO status transition to `BILLED`.
- **UAT-ORD-04:** Commercial Invoice immutability: verified that once issued, invoice financial terms cannot be modified directly.
- **UAT-ORD-05:** Invoice cancellation workflow: unpaid invoice can be cancelled via 4-step workflow; invoice with active settlements strictly blocks cancellation.

### Domain D: Payments, TDS & Non-Destructive Adjustments
- **UAT-PAY-01:** Combined Cash + TDS settlement (e.g. ₹50,000 cash + ₹1,000 TDS under 194C). Verify immediate status transition and balance deduction.
- **UAT-PAY-02:** Overpayment rejection boundary: attempt settlement exceeding balance by ₹0.01; assert 400 Bad Request with exact overpayment error.
- **UAT-PAY-03:** Non-destructive payment correction: Step 1 (compensating reversal) $\rightarrow$ Step 2 (replacement payment). Verify both rows recorded in audit trail.
- **UAT-PAY-04:** Full reversal without replacement: verify invoice balance re-opens to pre-payment figure.
- **UAT-PAY-05:** Cancellation after full reversal: invoice cancelled after full reversal preserves all historical payment and reversal events with zero net settlement.

### Domain E: Sales Quotas & Performance Analytics
- **UAT-TGT-01:** Target configuration by Admin/Finance: set yearly target, verify automatic derivation of monthly, quarterly, and half-yearly quotas and audit log creation.
- **UAT-TGT-02:** Sales representative access restriction: `sandbox_sales` views only their own quota; cannot view or edit peer quotas.
- **UAT-TGT-03:** Executive attainment derivation: verify attainment percentage is calculated strictly on tax-exclusive invoiced revenue, excluding GST and unbilled POs.

### Domain F: Server-Side Reporting Hub & AR Aging
- **UAT-REP-01:** Reports Hub View 1 (Invoices & Receivables): complete dataset, accurate gross invoiced, settled, and outstanding totals.
- **UAT-REP-02:** Reports Hub View 2 (Collections Ledger): complete transaction history from `GET /api/payments` including signed reversals (`-₹...`) and cancelled invoice payments.
- **UAT-REP-03:** Reports Hub View 3 (Customer Exposure): server summary totals match table aggregations.
- **UAT-REP-04:** Reports Hub View 4 (Executive Attainment): quota vs actuals reconciliation.
- **UAT-REP-05:** Reports Hub View 5 (AR Aging Summary): 6 aging buckets, 30 April 2026 anchor accuracy (`INV-2026-042` = 0 days overdue).
- **UAT-REP-06:** Large dataset pagination (>100 records): table pagination controls (`1–10 of 105`) preserve server-authoritative totals without client truncation.

### Domain G: Authoritative Exports & Ticket Security
- **UAT-EXP-01:** 5-step Live Export Workflow (`Scope` $\rightarrow$ `Preview` $\rightarrow$ `Review` $\rightarrow$ `Confirm` $\rightarrow$ `Download`).
- **UAT-EXP-02:** Single-use export ticket enforcement: 5-minute expiry, immediate consumption lock, rejection of replayed ticket with 401/403.
- **UAT-EXP-03:** Bearer token bypass prevention: download endpoint rejects requests lacking valid single-use ticket even if valid Bearer authorization header is supplied.
- **UAT-EXP-04:** Scope and format binding: ticket issued for Excel/FY2026-27 rejects CSV download or scope alterations.
- **UAT-EXP-05:** Downloaded file financial verification: 17-column Excel workbook and CSV parsed independently; financial columns match PostgreSQL reporting engine to ₹0.00.
- **UAT-EXP-06:** Formula injection sanitization: CWE-1236 protection prepends `'` to cells starting with `=`, `+`, `-`, `@`.

---

## 6. Viewport Responsive Matrix & Visual Inspection

| Viewport Profile | Target Device / Form Factor | Resolution | Key Verification Focus |
| :--- | :--- | :--- | :--- |
| **Desktop** | Standard Desktop Display | $1440 \times 900$ | Full sidebar navigation, wide financial data tables, multi-column cards, modal dialogs. |
| **Tablet** | iPad / Android Tablet | $768 \times 1024$ | Collapsible navigation, drawer workflows, responsive table overflow handling. |
| **Mobile** | Modern Smartphone (iPhone / Pixel) | $390 \times 844$ | Bottom navigation, mobile cards replacing wide tables, full-screen drawer overlays. |
| **Narrow Mobile** | Compact Mobile (SE / Fold) | $320 \times 568$ | Zero horizontal overflow, touch target accessibility ($\ge 44\text{px}$), wrapping badges. |

### Visual Inspection Checkpoints:
1. No unintended horizontal page scrollbars on any viewport.
2. Form action buttons (Submit, Review, Confirm, Cancel) fully accessible without viewport clipping.
3. Drawers scroll vertically without hiding sticky footer actions.
4. Currency formatting (`Intl.NumberFormat('en-IN')`) cleanly fits cards and table columns without wrapping glitches.

---

## 7. Historical Accounting-Policy Boundary

As established in Stage 4C.4 and preserved in Stage 4C.5:
- Payment corrections and reversals operate on an **operational posting basis** with immutable business dates and server timestamps.
- Corrections posted on date $T_{\text{corr}} > T_{\text{orig}}$ recognize the adjustment as of $T_{\text{corr}}$, preserving closed prior period financial integrity.
- These operational adjustments are explicitly documented as operational management records and are not certified statutory closed-period retro-statements.

---

## 8. Defect Classification & Stage Gating

If any unexpected behavior occurs during UAT execution:
1. Assign stable defect ID (`CF4C6-01`, etc.).
2. Categorize severity: `BLOCKER`, `CRITICAL`, `MAJOR`, `MINOR`.
3. Document reproducible test step, expected vs actual behavior, and payload evidence.
4. Isolate whether defect is in application code or test harness.
5. If application defect requires fix: formulate narrowest patch with separate approval; do not modify previously accepted functionality without consensus.
6. Acceptance recommendations permitted:
   - `READY FOR INDEPENDENT REVIEW` (all UAT scenarios pass, invariants hold at ₹0.00 difference).
   - `NEEDS ATTENTION` (unresolved defects or regressions exist).
   - `BLOCKED BY ENVIRONMENT` (infrastructure or database failure).

---

## 9. Execution Sequence

```mermaid
flowchart TD
    A["Verify Git Checkpoints & Create Branches"] --> B["Validate PostgreSQL Isolation & Baseline Seed"]
    B --> C["Execute Backend Pytest Suite (99 Tests)"]
    C --> D["Execute Frontend Vitest Suite (596 Tests)"]
    D --> E["Execute TypeScript & Production Build"]
    E --> F["Launch Backend & Frontend Live Test Servers"]
    F --> G["Execute Consolidated Stage 4C.6 Playwright Suite (20+ Scenarios)"]
    G --> H["Execute 4-Viewport Responsive Matrix & Evidence Capture"]
    H --> I["Perform Mathematical Financial Reconciliation Worksheet"]
    I --> J["Generate Final UAT & Review Reports"]
    J --> K["Commit & Push feature/phase4-stage4c6-live-uat"]
```
