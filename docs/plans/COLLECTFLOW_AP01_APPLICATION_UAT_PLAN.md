# CollectFlow — AP-01 Application UAT Plan
**Document Version:** 1.0.0  
**Status:** AUTHORIZED / EXECUTED  
**Target Release Phase:** AP-01 Cloud-Neutral Application Integration & Functional UAT  
**Repositories:**
- Backend: [`AnishB-Personal/collectflow`](file:///home/anishb/projects/personal/collectflow) (Branch: `feature/ap01-application-integration-uat`)
- Frontend: [`AnishB-Personal/cash-flow-buddy-101`](file:///home/anishb/projects/personal/cash-flow-buddy-101) (Branch: `feature/ap01-application-integration-uat`)

---

## 1. Executive Summary & Objective

The objective of **AP-01** is to pivot focus from infrastructure and release governance directly onto the **CollectFlow** application product. CollectFlow is a cloud-neutral, deterministic Order-to-Cash (O2C) and Accounts Receivable (AR) application built with a high-performance Python FastAPI backend, a TanStack Start/React frontend, and an enterprise PostgreSQL relational database.

This document establishes the test methodology, execution architecture, role matrix, and verification plan across all 12 core business workflows to independently prove end-to-end operational readiness in full **LIVE** mode.

---

## 2. Test Environment Architecture & Cloud Neutrality

CollectFlow is designed to be completely cloud-neutral with zero vendor lock-in, deployable to standard container environments (Docker, Podman, Kubernetes) or any cloud provider (AWS, GCP, Azure, bare-metal).

### Environment Specification
- **Database Engine:** PostgreSQL 16.15 running via local container `collectflow-postgres-dev` on `127.0.0.1:5432`.
- **Target Test Database:** `collectflow_test` (strictly isolated test database, enforced by loopback safety gates).
- **Backend Service:** FastAPI application served via Uvicorn on `127.0.0.1:8000` with 42 RESTful endpoints documented under `/openapi.json`.
- **Frontend Service:** TanStack Start / Vite React application served on `127.0.0.1:3000` with `VITE_API_MODE=live` and `VITE_API_BASE_URL=http://127.0.0.1:8000`.
- **Browser Automation:** Playwright Chromium Headless running deterministic browser end-to-end tests across viewports (Desktop 1440px, Tablet 768px, Mobile 390px, Narrow Mobile 320px).

---

## 3. Role & Persona Matrix

CollectFlow enforces strict Role-Based Access Control (RBAC) across both the API gateway and the frontend UI:

| Persona | Role | Default Username | Permitted Actions | Restricted Actions |
| :--- | :--- | :--- | :--- | :--- |
| **Administrator** | `admin` | `sandbox_admin` | Full system access: User creation/editing/deletion, password resets, financial year setup, target quotas, full order-to-cash workflows | Deletion of own account, demotion/deletion of last admin |
| **Finance Specialist** | `finance` | `sandbox_finance` | Customer creation, PO conversion, payment recording, payment corrections/reversals, single-use export tickets | User administration, sales quota configuration |
| **Sales Representative** | `sales` | `sandbox_sales` | Customer creation, Purchase Order drafting, personal quota attainment viewing | Invoice cancellation, payment recording, user administration, global export tickets |
| **Auditor / Viewer** | `viewer` | `sandbox_viewer` | Read-only inspection of customers, orders, invoices, payments, aging, and reports | All mutations: No creating orders/invoices/payments, no user management, no export downloads |

---

## 4. Business Workflow Scope (Workflows 1–12)

The UAT covers 12 comprehensive business workflows executed against the real PostgreSQL backend:

### Workflow 1: Authentication & Session Lifecycle
- Argon2id password verification against `/api/auth/login`.
- Session token generation stored strictly in memory (`sessionStorage`/`localStorage` exposure forbidden).
- Session validation via `GET /api/auth/me`.
- Atomic logout via `POST /api/auth/logout` invalidating sessions in PostgreSQL `sessions` table.

### Workflow 2: Customer Master Management
- Creation of commercial customers via Customer Drawer.
- Strict GSTIN format verification (15-character alphanumeric statutory format).
- Case-insensitive duplicate billing name prevention in PostgreSQL.
- Instant table row insertion upon successful persistence.

### Workflow 3: Purchase Order Management
- Order capture via Order Drawer with line-item pricing and quantity.
- Automated GST calculation (CGST 9% + SGST 9% or IGST 18%).
- Enforcement of zero Accounts Receivable impact for unbilled purchase orders (`unbilled_po_zero_ar` invariant).

### Workflow 4: Invoice Issuance & Lifecycle
- Conversion of active purchase orders to commercial invoices (`POST /api/invoices`).
- Net-30 payment terms automatically calculated from invoice date to due date.
- Customer details (GSTIN, address, state) frozen immutably into invoice snapshot columns.
- Transition of PO status to `BILLED`.

### Workflow 5: Payment Recording & Settlements
- Cash remittances recorded against issued commercial invoices (`POST /api/payments`).
- TDS deductions calculated under Indian Income Tax Act Sections 194C (2.0%) and 194J (10.0%).
- Net settlement applied atomically, decreasing `total_outstanding` in PostgreSQL.
- Strict rejection of overpayments exceeding the current invoice balance.

### Workflow 6: Adjustments & Reversals
- Non-destructive compensating payment corrections (`POST /api/payments/{id}/correct`).
- Full payment reversal workflows restoring invoice balances to pre-settlement state.
- Audit trail recorded in `audit_logs` table for all financial adjustments.

### Workflow 7: Invoice Cancellation
- Controlled cancellation of unsettled commercial invoices with mandatory cancellation reason.
- Cancellation status recorded as `CANCELLED` lifecycle state.
- Invoices with active financial remittances blocked from deletion/cancellation without prior reversal.

### Workflow 8: Accounts Receivable (AR) Aging Analysis
- Real-time computation of aging balances across 6 standard buckets: Not Due, 1–30 Days, 31–60 Days, 61–90 Days, 91–180 Days, and 180+ Days.
- Reconciliation with summary KPI cards matching individual invoice records to ₹0.00 variance.

### Workflow 9: Sales Targets & Quota Tracking
- Annual quota configuration by Administrators.
- Quarterly target breakdowns stored in PostgreSQL `sales_targets` table.
- Representative quota isolation: Sales representatives observe only their assigned quota.

### Workflow 10: Reports & Financial Intelligence
- Collections ledger tracking historical payments and TDS deductions.
- Executive attainment tracking and customer exposure analytics.
- Preservation of historical payment records even when invoices are subsequently cancelled.

### Workflow 11: Single-Use Cryptographic Export Downloads
- 5-step export workflow with scope and FY selection.
- Ephemeral single-use export ticket generation (`POST /api/exports/ticket`).
- Atomic consumption of ticket on download (`GET /api/exports/download/{ticket_id}`); replay attacks rejected with HTTP 401/410.

### Workflow 12: User & Access Administration
- System accounts listed directly from `/api/users`.
- Role assignment and full name editing persisted to PostgreSQL `users` table.
- Password complexity enforcement (12+ characters, uppercase, lowercase, digit, special character).
- Last administrator demotion/deletion prevention safeguard.

---

## 5. Acceptance Invariants & Verification Criteria

1. **Zero Silent Simulation:** All live interactions must communicate via real HTTP REST requests to the FastAPI backend; simulated mock fallbacks in LIVE mode are strictly prohibited.
2. **Authoritative Financial Reconciliation:** The canonical seed baseline must reconcile exactly:
   - `INV-2026-042`: Invoiced Gross ₹5,31,000.00.
   - `REM-2026-001`: Settlement ₹2,09,000.00 (Cash ₹2,00,000.00 + TDS ₹9,000.00).
   - Net Outstanding: ₹3,22,000.00.
   - Unbilled PO (`PO-NEXUS-881`): ₹2,95,000.00 with ₹0.00 AR impact.
3. **Database Integrity:** Foreign key constraints, transaction advisory locks, and append-only audit logging must remain intact.
4. **Clean Working Trees:** Test runs must clean up disposable records or reset fixtures cleanly without dirtying git tracked files.
