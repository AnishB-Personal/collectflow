# CollectFlow — Complete Feature Inventory

This document details every feature implemented in the CollectFlow backend and existing frontend, including role access, validations, error states, and endpoint mappings.

---

## 1. Authentication & Session Security

### 1.1 User Login
- **Purpose**: Authenticate users, establish a stateful session token, and return user profile details.
- **Roles**: All (`admin`, `finance`, `sales`, `viewer`).
- **Inputs**: `username` (string), `password` (string).
- **Outputs**: `{ success: true, token: string, user: { id, username, role, full_name } }`.
- **Validation**: Username and password required. Progressive rate limiting enforces delays and triggers HTTP 429 after 5 consecutive failures.
- **Related Endpoint**: `POST /api/auth/login`.

### 1.2 User Logout
- **Purpose**: Invalidate and immediately purge the active bearer token from the database.
- **Roles**: Authenticated users.
- **Inputs**: None (Header `Authorization: Bearer <token>`).
- **Outputs**: `{ success: true, message: "Logged out" }`.
- **Related Endpoint**: `POST /api/auth/logout`.

### 1.3 Personal Password Change
- **Purpose**: Allow any user to rotate their own password.
- **Roles**: Authenticated users.
- **Inputs**: `current_password` (string), `new_password` (string, min 6 characters).
- **Validation**: Verifies current password against database PBKDF2 hash before updating.
- **Related Endpoint**: `POST /api/auth/change-password`.

### 1.4 Get Current Profile (`/me`)
- **Purpose**: Validate active session on frontend initialization or page refresh.
- **Roles**: Authenticated users.
- **Outputs**: `{ id, username, role, full_name, expires_at }`.
- **Related Endpoint**: `GET /api/auth/me`.

---

## 2. Dashboard & Financial Intelligence

### 2.1 KPI Metric Cards
- **Purpose**: Display global financial metrics calculated across active, non-cancelled orders.
- **Roles**: All roles.
- **Displayed Metrics**:
  - `total_invoiced`: All-time gross invoiced revenue (excluding cancelled).
  - `total_cash_collected`: Cumulative physical bank collections.
  - `total_tds_recognized`: Cumulative tax withholding credits.
  - `total_settled`: Sum of cash + TDS.
  - `total_outstanding`: Global pending receivable balance.
  - `current_fy_outstanding`: Pending receivables for the current active financial year.
  - `current_fy_invoiced`: Current FY gross billed amount.
  - `overall_collection_rate`: Percentage of invoiced amount collected.
- **Related Endpoint**: `GET /api/dashboard/summary`.

### 2.2 Year-over-Year (YoY) Financial Performance
- **Purpose**: Multi-year comparison of exclusive contract value, gross billing, cash received, TDS, outstanding, and recovery rates.
- **Related Endpoint**: `GET /api/dashboard/summary` (field `yoy`).

### 2.3 AR Aging Distribution
- **Purpose**: Categorize outstanding balances by aging buckets:
  - `Current / Not Due`: Due date in future or no due date.
  - `1-30 Days Overdue`: Past due date by 1 to 30 days.
  - `31-60 Days Overdue`: Past due date by 31 to 60 days.
  - `61-90 Days Overdue`: Past due date by 61 to 90 days.
  - `90+ Days Overdue`: Past due date by >90 days.
- **Related Endpoint**: `GET /api/dashboard/summary` (field `aging`).

### 2.4 Account Executives Quota & Achievement Leaderboard
- **Purpose**: Track each sales rep's current fiscal year order count, total closed sales, annual quota, achievement percentage, and pending receivables.
- **Related Endpoint**: `GET /api/dashboard/summary` (field `executives`).

---

## 3. Order & Invoice Management

### 3.1 Order / Invoice Creation
- **Purpose**: Log a client Purchase Order (`order_type = 'PO'`) or subscription contract (`order_type = 'RECURRING'`). Optionally issue an immediate commercial invoice.
- **Roles**: `admin`, `finance`, `sales`.
- **Inputs**:
  - `fy_code` (e.g. `"FY 2026-27"`), `order_type` (`"PO"` or `"RECURRING"`).
  - `customer_id` (integer, must exist).
  - `po_number` (string, required for standard POs; uniqueness enforced case-insensitively).
  - `po_date` (ISO date, optional).
  - `executive_id` (integer, optional; user must have role `sales`).
  - `currency` (INR, USD, EUR, GBP, AED; default INR).
  - `exchange_rate` (float, required if non-INR).
  - `items`: Array of line items `{ description, quantity, unit_price, tax_rate }`.
  - Optional Invoicing: `invoice_number`, `invoice_date`, `due_date`.
- **Validation**:
  - Duplicate PO check: Rejects duplicate numbers excluding placeholders (`'No PO'`, `'Recurring / Direct Invoice'`).
  - Date boundary: Invoice date must fall within the selected financial year's start/end dates.
  - Fallback Line Item: If items array is omitted, backend synthesizes a single matching line item.
- **Related Endpoint**: `POST /api/invoices`.

### 3.2 List Invoices & Orders (Paginated & Filtered)
- **Purpose**: View orders with server-side pagination, search, and multi-factor filtering.
- **Roles**: All roles.
- **Parameters**: `page` (default 1), `limit` (default 25), `fy`, `executive_id`, `status` (`ALL`, `UNBILLED`, `PENDING`, `OVERDUE`, `PARTIAL`, `PAID`, `CANCELLED`), `search` (query against PO#, Invoice#, customer name).
- **Related Endpoint**: `GET /api/invoices`.

### 3.3 Get Detailed Order/Invoice
- **Purpose**: Fetch full invoice header, snapshot customer data, line items list, and full chronological payment history.
- **Roles**: All roles.
- **Related Endpoint**: `GET /api/invoices/{invoice_id}`.

### 3.4 Convert Unbilled PO to Commercial Invoice
- **Purpose**: Transition an unbilled order into an official tax invoice.
- **Roles**: `admin`, `finance`.
- **Inputs**: `invoice_number` (string, unique), `invoice_date` (ISO date), `due_date` (optional, auto-computed to Net-30 if blank).
- **Validation**: Cannot convert a cancelled order; invoice date must fall within order's FY; duplicate invoice numbers rejected.
- **Related Endpoint**: `POST /api/invoices/{order_id}/convert-to-invoice`.

### 3.5 Edit Order / Invoice Metadata
- **Purpose**: Update order parameters with strict enforcement of commercial immutability.
- **Roles**: `admin`, `finance`.
- **Rules**:
  - Unbilled POs: Can edit customer, PO#, PO date, assigned executive, due date, remarks, and line item descriptions.
  - Issued Invoices: Customer, PO#, and financial totals are **permanently locked**. Only internal remarks, due date, assigned executive, and line item descriptions can be modified.
- **Related Endpoint**: `PUT /api/invoices/{order_id}`.

### 3.6 Cancel Order / Invoice
- **Purpose**: Cancel a draft PO or issued invoice.
- **Roles**: `admin`, `finance`.
- **Inputs**: `reason` (string).
- **Rules**: Rejects cancellation if any recorded payments exist. Sets `payment_status = 'CANCELLED'`, zeroes outstanding balance in summary view, preserves line items and audit log.
- **Related Endpoint**: `POST /api/invoices/{order_id}/cancel`.

### 3.7 Delete Unbilled Order
- **Purpose**: Permanently remove an unbilled draft PO that was created in error.
- **Roles**: `admin`, `finance`.
- **Rules**: Strictly prohibited for issued invoices (`invoice_number IS NOT NULL`); prohibited if payments exist.
- **Related Endpoint**: `DELETE /api/invoices/{order_id}`.

---

## 4. Payment & Settlement Management

### 4.1 Record Payment
- **Purpose**: Log a partial or full payment tranche against an invoice.
- **Roles**: `admin`, `finance`.
- **Inputs**:
  - `invoice_id` (integer).
  - `payment_amount` (float, net cash received).
  - `tds_amount` (float, optional withholding credit).
  - `tds_section` (string, optional, e.g. `"194J (2%)"`).
  - `payment_date` (ISO date).
  - `payment_mode` (`NEFT`, `CHEQUE`, `UPI`, `CASH`).
  - `remittance_number` (UTR or transaction reference).
  - `idempotency_key` (optional, prevents duplicate submission).
- **Validation**:
  - Sum of `payment_amount + tds_amount` cannot exceed current invoice outstanding balance.
  - Invoice must not be cancelled.
  - Identical duplicate payments within 5 minutes on same invoice/UTR are blocked.
- **Related Endpoint**: `POST /api/payments`.

### 4.2 Delete / Revert Payment
- **Purpose**: Correct a clerical payment error.
- **Roles**: `admin`, `finance`.
- **Outputs**: Reopens invoice balance, recalculates status, logs deleted payment details to immutable `audit_logs`.
- **Related Endpoint**: `DELETE /api/payments/{payment_id}`.

### 4.3 List Recent Payments
- **Purpose**: Show recent payment feed across all customers.
- **Related Endpoint**: `GET /api/payments/recent`.

---

## 5. Customer Directory Management

### 5.1 Customer CRUD
- **Purpose**: Manage client billing accounts.
- **Endpoints**:
  - `GET /api/customers`: Searchable directory with lifetime billed and pending balance metrics.
  - `POST /api/customers`: Create customer with GSTIN validation and state extraction.
  - `GET /api/customers/{id}`: Detailed customer profile with full order history.
  - `PUT /api/customers/{id}`: Update customer metadata.
  - `DELETE /api/customers/{id}`: Delete customer (blocked if customer has associated orders).

---

## 6. Settings, Reference Data & RBAC

### 6.1 User Management
- **Endpoints**:
  - `GET /api/users`: List team accounts and roles.
  - `POST /api/users`: Create user (`admin`, `finance`, `sales`, `viewer`).
  - `PUT /api/users/{id}`: Update user name or role (demoting last admin blocked).
  - `DELETE /api/users/{id}`: Delete user (deleting own account or last admin blocked).
  - `POST /api/users/{id}/reset-password`: Administrative credential reset.
  - `GET /api/users/{id}/targets`: Retrieve sales quota breakdown.
  - `POST /api/users/{id}/targets`: Configure annual, half-yearly, quarterly, and monthly quotas.

### 6.2 Financial Years Master
- **Endpoints**:
  - `GET /api/settings/financial-years`: List fiscal years.
  - `POST /api/settings/financial-years`: Add custom fiscal year.
  - `POST /api/settings/financial-years/seed`: Initialize standard Indian fiscal years (2018-19 to 2026-27).
  - `DELETE /api/settings/financial-years/{code}`: Delete fiscal year (blocked if orders exist).

### 6.3 Tax Structure Masters (GST & TDS)
- **Endpoints**:
  - `GET /api/settings/taxes`: List configured GST rates and TDS sections.
  - `POST /api/settings/taxes/gst`: Create GST slab.
  - `DELETE /api/settings/taxes/gst/{id}`: Remove GST slab.
  - `POST /api/settings/taxes/tds`: Create TDS section.
  - `DELETE /api/settings/taxes/tds/{id}`: Remove TDS section.

---

## 7. Reporting & Data Export

### 7.1 Single-Use Export Ticket
- **Endpoint**: `POST /api/export/token`
- **Purpose**: Issue a cryptographically secure, 60-second single-use ticket stored in SQLite.

### 7.2 Secure Excel Export
- **Endpoint**: `GET /api/export/excel?fy={fy}&ticket={ticket}`
- **Purpose**: Stream styled Excel workbook with 2 sheets:
  1. *Invoices & Collections*: Order details, customer snapshot, currency, billing date, payments, TDS, status.
  2. *Payments Ledger*: Transaction-level log with invoice references and UTRs.
