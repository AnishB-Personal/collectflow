# CollectFlow — Phase 4 Stage 4C.5 Implementation Plan
## Sales Targets, Server-Side Reporting Hub, AR Aging & Exports

**Feature Branch:** `feature/phase4-stage4c5-reporting-targets-exports`  
**Backend Baseline:** `77d95fd56e8874268ae158333ce251badad4807f`  
**Frontend Baseline:** `f489b43840c94d00ad3e9802ccd87c993bf17eaf`  
**Date:** 2026-10-17  
**Status:** In Progress (Authorized)

---

## 1. Executive Summary & Architecture Alignment

Stage 4C.5 delivers the authoritative reporting, sales targets, and data export tier for CollectFlow, transitioning reporting from Phase 3 client-side calculations and legacy SQLite queries to dedicated PostgreSQL domain services.

Under Stage 4C.5:
- **Authoritative Data Layer:** All KPIs, aging buckets, executive attainment figures, and financial exports derive directly from PostgreSQL tables (`commercial_invoices`, `payment_transactions`, `purchase_orders`, `sales_targets`, `users`, `customers`).
- **Elimination of SQLite Reporting:** The legacy SQLite dashboard and export routes are replaced with PostgreSQL-backed services.
- **Strict Financial Invariants:**
  $$\text{Net Cash} + \text{Net TDS} = \text{Net Settled}$$
  $$\text{Issued Invoice Total} - \text{Net Settled} = \text{Outstanding Receivables}$$
  Unbilled purchase orders ($\text{status} = \text{'UNBILLED'}$) are tracked separately and strictly contribute $\text{₹}0.00$ to recognized accounts receivable.
- **Single-Use Cryptographic Export Tickets:** Replaces long-lived or URL-exposed session credentials with cryptographically secure, 5-minute single-use export tickets stored as SHA-256 hashes and consumed atomically.
- **Formula Injection Mitigation:** All Excel (.xlsx via openpyxl) and CSV exports sanitize untrusted text cells to neutralize spreadsheet formula injection vulnerabilities (`=`, `+`, `-`, `@`).

---

## 2. API-to-Screen Mapping

| Frontend Screen / Drawer | Backend Endpoint | Method | Role Access | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Dashboard** (`dashboard.tsx`) | `/api/reporting/summary` | GET | All authenticated | High-level financial KPIs, settlement rate, collection progress, YoY comparison, and executive performance cards. |
| **Sales Targets** (`targets-screen.tsx`) | `/api/targets` | GET | All authenticated | List sales targets for active FY. Sales representatives restricted to own target. |
| **Target Drawer** (`target-drawer.tsx`) | `/api/targets/{user_id}` | POST | Admin, Finance | Create or update quota for target user and financial year. |
| **Performance Analytics** (`performance-analytics.tsx`) | `/api/reporting/attainment` | GET | All authenticated | Quota attainment vs tax-exclusive invoiced sales. |
| **Reports Hub: Invoices & AR** (`reports-hub.tsx` Tab 1) | `/api/invoices` or `/api/reporting/invoices` | GET | All authenticated | Full commercial invoice receivable ledger with customer, executive, and FY filters. |
| **Reports Hub: Collections** (`reports-hub.tsx` Tab 2) | `/api/payments` | GET | All authenticated | Detailed payment transactions ledger with cash/TDS split and remittance identifiers. |
| **Reports Hub: Customer Exposure** (`reports-hub.tsx` Tab 3) | `/api/reporting/customer-exposure` | GET | All authenticated | Customer-wise billed gross, settled, outstanding, unbilled, and overdue exposure. |
| **Reports Hub: Attainment** (`reports-hub.tsx` Tab 4) | `/api/reporting/attainment` | GET | All authenticated | Executive sales quota performance and remaining quotas. |
| **Reports Hub: AR Aging** & **AR Aging Screen** | `/api/reporting/ar-aging` | GET | All authenticated | 6-bucket accounts receivable aging analysis as of specified calendar date. |
| **Export Drawer: Ticket Issuance** | `/api/export/token` | POST | Admin, Finance | Issue cryptographically secure, 5-minute single-use download ticket. |
| **Export Drawer: Download Excel** | `/api/export/excel` | GET | Admin, Finance (Ticket) | Stream openpyxl-generated 17-column .xlsx workbook. |
| **Export Drawer: Download CSV** | `/api/export/csv` | GET | Admin, Finance (Ticket) | Stream 17-column sanitized CSV. |

---

## 3. Reporting Query Design & Database Optimizations

### 3.1 Server-Side Aggregations
All financial metrics are computed over complete PostgreSQL tables rather than paginated subsets:
1. **Issued Invoices:** `CommercialInvoice.lifecycle_status = 'ISSUED'`.
2. **Cancelled Invoices:** `CommercialInvoice.lifecycle_status = 'CANCELLED'`. Total billed and remaining balance are $0.00$; historical settlements remain preserved.
3. **Settlement Ledger:** `PaymentTransaction.total_settled = (cash_amount + tds_amount)`. Reversals contribute negative settlement.
4. **Outstanding Balance:** Authoritative balance calculated per invoice:
   $$\text{outstanding\_balance} = \max(0.00, \text{total\_amount} - \sum \text{total\_settled})$$
   (enforced $0.00$ when `lifecycle_status = 'CANCELLED'`).

### 3.2 Index Utilization
- `idx_invoices_fy_status` on `(fy_code, lifecycle_status)`
- `idx_invoices_due_date` on `due_date`
- `idx_payments_invoice` on `invoice_id`
- `idx_targets_lookup` on `(fy_code, user_id)`
- `idx_export_tickets_expires` on `(expires_at, used)`

---

## 4. Financial Calculation Contracts

### 4.1 Invoiced Revenue Basis
- **Sales Quota Attainment:** Uses **tax-exclusive commercial invoiced revenue** (`exclusive_amount`). Unbilled purchase orders and settlements are excluded from sales revenue.
- **Receivables & Aging:** Uses **gross commercial invoice total** (`total_amount = exclusive_amount + tax_amount`).
- **Unbilled PO Value:** Computed strictly from purchase orders where `status = 'UNBILLED'`.

### 4.2 Mathematical Invariants
Every reporting endpoint and export artifact verifies:
$$\sum \text{Cash Collected} + \sum \text{TDS Deducted} = \sum \text{Net Settled}$$
$$\sum \text{Total Invoiced (Issued)} - \sum \text{Net Settled (Issued)} = \sum \text{Balance Outstanding}$$
$$\sum_{b=1}^{6} \text{Bucket Amount}_b = \text{Total Accounts Receivable}$$

---

## 5. Financial-Year & As-Of Date Rules

### 5.1 Financial Year Boundaries
CollectFlow operates on the Indian Financial Year convention: **1 April to 31 March**.
- `FY 2026-27`: 2026-04-01 to 2027-03-31
- `FY 2025-26`: 2025-04-01 to 2026-03-31
- `FY 2024-25`: 2024-04-01 to 2025-03-31

### 5.2 Six AR Aging Buckets
Aging is computed using calendar-day differences between the query's `as_of_date` and invoice `due_date`:
$$\text{days\_overdue} = \max(0, \text{as\_of\_date} - \text{due\_date})$$

| Bucket Index | Category Name | Condition |
| :---: | :--- | :--- |
| 1 | **Current / Not Due** | $\text{days\_overdue} \le 0$ (Invoice due on or after `as_of_date`) |
| 2 | **1–30 days** | $1 \le \text{days\_overdue} \le 30$ |
| 3 | **31–60 days** | $31 \le \text{days\_overdue} \le 60$ |
| 4 | **61–90 days** | $61 \le \text{days\_overdue} \le 90$ |
| 5 | **91–180 days** | $91 \le \text{days\_overdue} \le 180$ |
| 6 | **180+ days** | $\text{days\_overdue} \ge 181$ |

### 5.3 Synthetic Baseline Anchor
- Reference Date: **30 April 2026** (`2026-04-30`).
- Baseline Invoice: `INV-2026-042` (Issue date: `2026-04-12`, Due date: `2026-05-12`).
- As of 30 April 2026: Due date `2026-05-12` is in the future $\rightarrow \text{days\_overdue} = 0$.
- Category: **Current / Not Due** ($\text{₹}3,22,000.00$). Overdue receivables $> 0$ days is exactly $\text{₹}0.00$.

---

## 6. Historical Correction-Date Accounting Policy

### 6.1 Context & Conflict (Stage 4C.4 Deferred Limitation)
In Stage 4C.4, compensating reversals and replacement settlements introduced two timestamps per transaction:
- `payment_date`: Effective commercial/bank settlement date.
- `created_at`: Physical database entry timestamp when the correction occurred.

When calculating an as-of-date historical AR report (e.g. "What was AR as of 2026-04-20?"):
- **Approach A (Statutory Restatement / Commercial Date):** Uses `payment_date`. Reversals back-dated to the original payment adjust historical AR as though the mistake never occurred.
- **Approach B (Discovered-Period Accounting / Audit Date):** Uses `created_at`. Historical reports show the ledger exactly as it was known on 2026-04-20, and corrections only apply on the date they were entered.

### 6.2 Policy Decision Matrix for Product Owner

| Scenario | Operational Aging (Current Period) | Statutory Historical Restatement | Certified Audit-Trial (Audit As-Of) |
| :--- | :--- | :--- | :--- |
| **Applicability** | Day-to-day collections management | Prior financial year close | Regulatory/Tax compliance audits |
| **Date Field** | Current active balances | `payment_date` | `created_at` (Ledger snapshot) |
| **Status in Stage 4C.5** | **Fully Implemented** | **Supported with documented limitation** | **Provisional (Requires PO Decision)** |

### 6.3 Stage 4C.5 Policy Stance
- **Operational Reporting:** Real-time balances and current aging reflect active database state (current outstanding receivables).
- **As-Of Filtering:** Operational aging calculations filter invoices issued on or before `as_of_date`.
- **Closed-Period Certification Limitation:** Certified historical as-of balance reconstruction across corrected periods is marked provisional pending formal business policy ratification.

---

## 7. RBAC Matrix & Export Security Model

### 7.1 Role Permissions

| Operation | Admin | Finance | Sales | Viewer |
| :--- | :---: | :---: | :---: | :---: |
| View Financial Dashboard | Yes | Yes | Yes | Yes |
| View All Sales Targets | Yes | Yes | No (Own only) | Yes (Read-only) |
| Create / Update Target | Yes | Yes | No (403) | No (403) |
| View Performance Analytics | Yes | Yes | Yes | Yes |
| View Reports Hub Views | Yes | Yes | Yes | Yes |
| Request Export Token | Yes | Yes | No (403) | No (403) |
| Download Excel / CSV | Yes | Yes | No (403) | No (403) |

### 7.2 Single-Use Ticket Security
1. **Generation:** Client calls `POST /api/export/token` with valid session.
2. **Entropy:** 256 bits of CSPRNG entropy (`secrets.token_urlsafe(32)`).
3. **Storage:** Stored in `export_tickets` table as SHA-256 hash (`hashlib.sha256(token).hexdigest()`). Raw token is never persisted in database or logged.
4. **TTL:** Strictly 300 seconds (5 minutes).
5. **Consumption:** Atomic single-use consumption under database row lock:
   ```sql
   SELECT * FROM export_tickets WHERE token_hash = :hash FOR UPDATE;
   UPDATE export_tickets SET used = true WHERE token_hash = :hash;
   ```
   Concurrent or replay attempts return HTTP 401 Unauthorized.
6. **Audit Trail:** Every ticket request and download event is logged in `audit_logs` with action `EXPORT_TOKEN_ISSUED` and `EXPORT_DOWNLOAD_COMPLETED`.

---

## 8. Export Format Specifications (17 Authoritative Columns)

Both Excel (.xlsx) and CSV stream exact columns in strict order:

| Col # | Column Header | Data Type | Formatting / Sanitization |
| :---: | :--- | :--- | :--- |
| 1 | `FY` | String | Sanitized (`'`) |
| 2 | `PO Date` | Date (YYYY-MM-DD) | ISO Date String |
| 3 | `PO Number` | String | Sanitized (`'`) |
| 4 | `Customer Name` | String | Sanitized (`'`) |
| 5 | `Executive` | String | Sanitized (`'`) |
| 6 | `Exclusive Amount (₹)` | Currency | `₹#,##0.00` / Decimal string |
| 7 | `Tax Amount (₹)` | Currency | `₹#,##0.00` / Decimal string |
| 8 | `Total Invoiced (₹)` | Currency | `₹#,##0.00` / Decimal string |
| 9 | `Invoice Number` | String | Sanitized (`'`) |
| 10 | `Invoice Date` | Date (YYYY-MM-DD) | ISO Date String |
| 11 | `Due Date` | Date (YYYY-MM-DD) | ISO Date String |
| 12 | `Cash Received (₹)` | Currency | `₹#,##0.00` / Decimal string |
| 13 | `TDS Deducted (₹)` | Currency | `₹#,##0.00` / Decimal string |
| 14 | `Total Settled (₹)` | Currency | `₹#,##0.00` / Decimal string |
| 15 | `Balance Outstanding (₹)` | Currency | `₹#,##0.00` / Decimal string |
| 16 | `Status` | String | `ISSUED` / `PARTIAL` / `PAID` / `CANCELLED` |
| 17 | `Days Overdue` | Integer | Calculated against export generation date |

### Spreadsheet Formula Injection Protection
Any string value beginning with `=`, `+`, `-`, or `@` is prepended with a single quote (`'`) to prevent formula execution in Microsoft Excel and LibreOffice Calc.

---

## 9. Acceptance Test Strategy & Verification Plan

### 9.1 Backend Test Coverage (`tests/test_stage4c5_reporting_targets_exports.py`)
- Target creation and update with validation (positive targets, valid user, valid FY).
- Retention of historical targets when sales user is deactivated.
- Sales representative own-target visibility enforcement.
- Dashboard KPI reconciliation against seeded database fixtures.
- 6-bucket AR Aging boundaries and 30 April 2026 baseline (`INV-2026-042`).
- Executive sales attainment excluding unbilled POs and settlements.
- Customer exposure reconciliation.
- Export ticket generation, single-use consumption, expiration, and replay prevention.
- Excel .xlsx binary generation with openpyxl and correct 17 columns.
- CSV generation with formula injection sanitization.
- RBAC enforcement across all reporting and export routes.

### 9.2 Frontend Vitest Coverage (`src/test/reporting-live.test.tsx`)
- Live dashboard KPI loading, rendering, and error recovery.
- Live Targets screen data loading and target editing drawer integration.
- Performance Analytics live attainment rendering.
- Reports Hub 5-tab live rendering with authoritative backend data.
- Live export drawer 5-step workflow (Scope $\rightarrow$ Preview $\rightarrow$ Review $\rightarrow$ Confirm $\rightarrow$ Result).
- Offline MOCK mode regression preservation.

### 9.3 End-to-End Browser Playwright (`e2e/stage4c5-reporting-targets-exports.spec.ts`)
- Multi-viewport verification (1440px desktop, 768px tablet, 390px mobile, 320px narrow).
- Full live export workflow execution and token validation.
- Live dashboard and sales targets interactions.

---

## 10. Explicit Exclusions & Stage Boundaries

1. **No Destructive Database Changes:** No migration script modifying existing Stage 4C.1-4C.4 tables or records.
2. **No UI Overhauls:** Approved Phase 3 layouts, drawers, badges, and responsive containers remain intact.
3. **No Stage 4C.6 UAT:** Final cross-stage user acceptance testing is reserved for Stage 4C.6.
4. **No Direct Main Branch Merges or Production Deployments.**
