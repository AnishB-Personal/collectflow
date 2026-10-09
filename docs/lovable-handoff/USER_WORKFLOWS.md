# CollectFlow — End-to-End User Workflows

This document outlines the 14 core user journeys in CollectFlow, describing step-by-step interactions, validations, business logic constraints, and failure modes.

---

## Workflow 1: Authentication & Session Termination
1. **Starting Point**: Sign-in modal overlay (`#authScreen`).
2. **Steps**:
   - User inputs username and password.
   - User clicks "Sign In".
   - Client sends credentials to `POST /api/auth/login`.
   - On success, client saves bearer token in `localStorage['cf_auth_token']`, dismisses login overlay, and initializes metadata via `GET /api/meta`.
3. **Logout**:
   - User clicks "Logout" in global header.
   - Client calls `POST /api/auth/logout`, purges `localStorage`, and displays `#authScreen`.

---

## Workflow 2: Customer Creation & Modification
1. **Starting Point**: Customers Directory tab (`#tab-customers`).
2. **Creation**:
   - User clicks `+ Add Customer`.
   - Modal `#customerModal` opens with empty fields.
   - User enters Company Name (required), optional GSTIN, State, Contact Person, Phone, Email.
   - User clicks "Save Customer".
   - Backend validates GSTIN structure, auto-extracts state if state is blank, and persists record.
3. **Modification**:
   - User clicks "Edit" on a customer row in `#tab-customers`.
   - Modal `#customerModal` opens pre-populated with customer ID and data.
   - User modifies fields and submits; client calls `PUT /api/customers/{id}`.

---

## Workflow 3: Creating a Purchase Order (PO) or Direct Invoice
1. **Starting Point**: Global header `+ New PO / Invoice` button.
2. **Steps**:
   - Modal `#invoiceModal` opens.
   - User selects billing type: `Standard PO` or `Recurring / Subscription`.
   - User selects Financial Year (e.g. `FY 2026-27`) and Customer.
   - If Standard PO: User enters Purchase Order # and PO Date.
   - If Recurring: PO# is automatically set to `"Recurring / Direct Invoice"`.
   - User selects assigned Sales Representative.
   - Deal Currency: User chooses INR or foreign currency (USD, EUR, GBP, AED). If foreign, user provides exchange rate.
   - Line Items: User clicks `+ Add Item / Service` and inputs Description, Quantity, Unit Price, and GST Rate.
   - Real-time client calculation displays Subtotal, Tax Amount, and Grand Total.
   - Optional Invoicing: User may enter Invoice Number, Invoice Date, and Net-30 Due Date directly.
   - User submits form.
3. **Validations**:
   - Database uniqueness: Normalized check `LOWER(TRIM(po_number))` ensures PO# is not already registered.
   - Fiscal year boundary: PO/Invoice date must fall within FY calendar boundaries.

---

## Workflow 4: Editing Order Metadata & Line Items
1. **Starting Point**: Invoices Directory tab (`#tab-invoices`) -> Row Action "Edit".
2. **Steps**:
   - Modal `#editOrderModal` opens pre-populated.
   - **For Unbilled POs**: User can edit Customer, PO#, PO Date, Executive, Due Date, Remarks, and Line Item Descriptions.
   - **For Issued Tax Invoices**: Commercial identifiers (Customer, PO#, Totals) are rendered read-only with a statutory warning banner. User can modify internal Remarks, Due Date, assigned Executive, and line item descriptions.
   - Client submits changes to `PUT /api/invoices/{order_id}`.

---

## Workflow 5: Converting an Unbilled PO to a Tax Invoice
1. **Starting Point**: Invoices Directory table row with status `UNBILLED` -> Click `Convert`.
2. **Steps**:
   - Modal `#convertInvoiceModal` opens displaying PO details.
   - User enters official Tax Invoice Number (e.g. `"INV/2026/089"`).
   - User enters Invoice Date. Net-30 Due Date is automatically calculated (Invoice Date + 30 days).
   - User clicks "Confirm Invoice".
   - Client sends payload to `POST /api/invoices/{order_id}/convert-to-invoice`.
3. **Outcomes**:
   - Backend verifies invoice number uniqueness (`LOWER(TRIM(invoice_number))`).
   - Order transitions from `UNBILLED` to `PENDING` (or `OVERDUE` if due date is past).
   - Commercial identifiers become legally immutable.

---

## Workflow 6 & 7: Recording Payments with Indian TDS Withholding
1. **Starting Point**: Global header `Log Payment` or Invoices table row action `Pay`.
2. **Steps**:
   - Modal `#paymentModal` opens (prefilled with `Invoice ID` if triggered from a table row).
   - User enters Net Cash Received (₹).
   - Indian Tax TDS: If client deducted tax at source, user enters TDS Amount (₹) and selects statutory TDS Section (e.g. `194J (2%)`, `194C (1%)`).
   - Total Settlement summary dynamically calculates: `Total Settlement = Cash Received + TDS Amount`.
   - User enters Payment Date, Payment Mode (`NEFT`, `CHEQUE`, `UPI`, `CASH`), and Remittance UTR #.
   - User clicks "Record Payment".
3. **Validations**:
   - Overpayment protection: Total Settlement cannot exceed current invoice outstanding balance.
   - Anti-duplication: Submitting the same invoice, amount, and UTR within 5 minutes is blocked.
4. **Outcomes**:
   - Backend registers payment row in `payments`.
   - Invoice outstanding balance decreases by `Cash + TDS`.
   - Status updates automatically (`PARTIAL` or `PAID`).
   - Audit log records transaction timestamp, user, and UTR.

---

## Workflow 8: Reviewing Outstanding Receivables & AR Aging
1. **Starting Point**: Dashboard tab (`#tab-dashboard`) or Invoices tab (`#tab-invoices`).
2. **Reviewing**:
   - User reviews High-level KPIs: *Total Outstanding* vs. *Current FY Outstanding*.
   - User reviews Receivables Aging doughnut chart to identify delinquent invoices (31-60, 61-90, 90+ days).
   - User switches to Invoices tab, sets Status filter to `OVERDUE` or `PENDING`, and sorts by Due Date.
   - User clicks "View Details" on any invoice to view customer contact info and payment history.

---

## Workflow 9: Cancelling an Eligible Order / Invoice
1. **Starting Point**: Invoices Directory -> Row action `Cancel`.
2. **Pre-condition**: Invoice must have **zero recorded payments**. If payments exist, user is prompted to delete/reverse all payments first.
3. **Steps**:
   - Confirmation dialog prompts user for cancellation reason.
   - Client sends reason to `POST /api/invoices/{order_id}/cancel`.
   - Backend sets `payment_status = 'CANCELLED'` and prepends reason to remarks.
4. **Financial Impact**:
   - Outstanding balance drops to ₹0.00.
   - Invoice is excluded from gross invoiced revenue and executive target achievement metrics.
   - Historical line items and audit log entries are preserved permanently.

---

## Workflow 10 & 11: Managing Sales Executives & Assigning Quotas
1. **Assigning Reps**:
   - Any user created with `role = 'sales'` is automatically recognized as an Account Executive.
   - During PO creation or metadata editing, rep is selected from the Executive dropdown.
2. **Configuring Targets**:
   - Administrator navigates to Settings -> User Management -> clicks "Quotas" on a sales rep.
   - Modal `#targetModal` opens.
   - User selects Financial Year and enters Annual Target (e.g. `₹40,00,000`).
   - Form automatically calculates split: Half-Yearly (50%), Quarterly (25%), Monthly (8.33%).
   - Administrator saves targets (`POST /api/users/{id}/targets`).
   - Dashboard leaderboard immediately reflects new targets and calculates attainment %.

---

## Workflow 12: Exporting Financial Audit Reports
1. **Starting Point**: Global header `📊 Export` button.
2. **Steps**:
   - User clicks `Export`.
   - Frontend calls `POST /api/export/token` to retrieve a short-lived single-use ticket.
   - Browser redirects to `/api/export/excel?fy={fy}&ticket={ticket}`.
   - Backend verifies and consumes ticket, streams binary XLSX file with two formatted worksheets: *Invoices & Collections* and *Payments Ledger*.

---

## Workflow 13: Team User Administration & RBAC
1. **Starting Point**: Settings -> User Management (`#setSection-users`).
2. **User Creation**:
   - Administrator clicks `+ Add New User`.
   - Enters Full Name, unique Username, initial Password, and selects Role (`admin`, `finance`, `sales`, `viewer`).
   - Submits to `POST /api/users`.
3. **User Governance**:
   - Demoting or deleting the last remaining administrator is rejected with HTTP 400.
   - Self-deletion is rejected.
   - Role modifications immediately invalidate active sessions for that user.
