# CollectFlow — Screen & Component Inventory

This document defines every existing interface element, page view, modal dialog, table, and data form currently implemented, establishing the exact functional requirements for the Lovable UI redesign.

---

## 1. Authentication View

### Screen: Sign In Overlay (`#authScreen`)
- **Business Purpose**: Entry point for authenticating team members.
- **Target User**: All users.
- **Displayed Information**: Brand logo ("CF"), Title "Sign In", Subtitle "CollectFlow — Order-to-Cash & Collections".
- **Fields**:
  - `loginUsername`: Text input, required, placeholder "Enter username".
  - `loginPassword`: Password input, required, placeholder "••••••••".
- **Buttons & Actions**:
  - `Sign In`: Submits credentials to `POST /api/auth/login`.
- **Validation**: Client-side HTML5 required attributes. Server returns error message on invalid credentials.
- **Error State**: Displays red banner `#loginError` with specific server message (e.g. invalid credentials or rate-limit cooldown).
- **Empty State**: Centered modal overlay over dark blurred backdrop (`bg-slate-900/80 backdrop-blur-md`).

---

## 2. Top Navigation & Global Header

### Component: Global Header (`<header>`)
- **Business Purpose**: Global branding, persistent quick-action entry points, active session information, and high-level section switching.
- **Target User**: All authenticated users.
- **Displayed Information**: Logo, App Name "CollectFlow", Environment badge, Authenticated user's name/role.
- **Global Actions**:
  - `+ New PO / Invoice`: Triggers Order Creation Modal (`#invoiceModal`).
  - `Log Payment`: Triggers Record Payment Modal (`#paymentModal`).
  - `📊 Export`: Generates single-use ticket and downloads Excel report (`#downloadExcelExport`).
  - `Logout`: Clears local session token and calls `POST /api/auth/logout`.
- **Primary Navigation Tabs**:
  1. **Dashboard** (`#tab-dashboard`)
  2. **Invoices & Orders** (`#tab-invoices`)
  3. **Customers Directory** (`#tab-customers`)
  4. **Settings & Users** (`#tab-settings`)

---

## 3. Tab 1: Financial Dashboard (`#tab-dashboard`)

### 3.1 KPI Metrics Grid
- **Displayed Cards**:
  1. *Total Invoiced (Gross)*: All-time invoiced revenue in INR, order count.
  2. *Collections Received*: Cumulative settlements received in INR, collection recovery rate %.
  3. *Total Outstanding*: Global pending receivable balance in INR.
  4. *FY 2026-27 Outstanding*: Highlighted card showing receivables for the current active fiscal year.
- **API Dependency**: `GET /api/dashboard/summary`.

### 3.2 Visual Charts Section
- **YoY Invoiced vs Collections Chart**: Grouped bar chart comparing invoiced gross against total collections across fiscal years.
- **Receivables Aging Chart**: Doughnut chart breaking down pending balances into buckets: *Current / Not Due*, *1-30 Days*, *31-60 Days*, *61-90 Days*, *90+ Days*.
- **API Dependency**: `GET /api/dashboard/summary`.

### 3.3 Account Executives Leaderboard
- **Displayed Information**:
  - Columns: *Executive*, *FY Orders*, *FY Sales (₹)*, *Annual Target (₹)*, *Achievement %*, *Pending Receivables (₹)*.
- **Empty State**: Centered text "No sales executives configured".
- **Loading State**: Shimmer or "Loading executives...".

---

## 4. Tab 2: Orders & Invoices Directory (`#tab-invoices`)

### 4.1 Filter & Search Toolbar
- **Search Field**: Real-time debounced text input filtering by PO number, invoice number, or customer name.
- **Dropdown Filters**:
  1. *Financial Year*: Options from `financial_years` master or `"ALL"`.
  2. *Sales Executive*: Options from active sales users or `"ALL"`.
  3. *Lifecycle Status*: `"ALL"`, `"UNBILLED"` (Draft PO), `"PENDING"` (Awaiting Payment), `"OVERDUE"`, `"PARTIAL"`, `"PAID"`.
- **API Dependency**: `GET /api/invoices?page={p}&limit=25&fy={fy}&executive_id={e}&status={s}&search={q}`.

### 4.2 Invoices Data Table
- **Columns**:
  1. *Customer*: Billing name, tax identity snapshot.
  2. *PO No*: Purchase order number with badge indicator for direct recurring invoices.
  3. *Invoice No*: Tax invoice number or "Draft / Unbilled" badge.
  4. *Sales Representative*: Assigned account executive.
  5. *Invoiced Amount*: Total billing amount formatted in INR (or foreign currency equivalent).
  6. *Balance & Settlement*: Outstanding balance, cash collected, and TDS recognized.
  7. *Due Date*: Commercial Net-30 payment due date with overdue warning indicators.
  8. *Status*: Computed status badge (`UNBILLED`, `PENDING`, `PARTIAL`, `PAID`, `OVERDUE`, `CANCELLED`).
  9. *Actions*:
     - `View Details`: Opens modal `#viewOrderModal`.
     - `Convert`: (Unbilled POs only) Opens modal `#convertInvoiceModal`.
     - `Pay`: Opens `#paymentModal` prefilled with this invoice ID.
     - `Edit`: Opens `#editOrderModal`.
     - `Cancel`: Triggers cancellation prompt with reason.
     - `Delete`: (Unbilled POs only) Permanently removes draft record.
- **Pagination Controls**: Previous/Next buttons, "Showing X of Y records", Page indicator.

---

## 5. Tab 3: Customers Directory (`#tab-customers`)

### 5.1 Search & Actions Bar
- **Search**: Debounced search by company name, GSTIN, city, contact person.
- **Action**: `+ Add Customer` button opening `#customerModal`.
- **Metric Badge**: Total customer count.

### 5.2 Customers Data Table
- **Columns**:
  1. *Customer & Tax Identity*: Company name, GSTIN badge.
  2. *Location*: State, city, billing address.
  3. *Contact Info*: Contact person, phone, email.
  4. *Orders*: Lifetime order count.
  5. *Lifetime Billed*: Cumulative gross billings in INR.
  6. *Outstanding Balance*: Total pending balance across all orders.
  7. *Actions*:
     - `Edit`: Opens `#customerModal` pre-populated with customer record.
     - `Delete`: Removes customer (blocked if orders exist).
- **API Dependency**: `GET /api/customers?search={q}`.

---

## 6. Tab 4: Settings & Masters (`#tab-settings`)

### 6.1 Left Navigation Sidebar
- Navigation links to 4 subsections:
  1. *User Management* (`#setSection-users`)
  2. *Financial Years* (`#setSection-fy`)
  3. *Tax Structure* (`#setSection-taxes`)
  4. *Security & Password* (`#setSection-password`)

### 6.2 Subsection: User Management (`#setSection-users`)
- **Actions**: `+ Add New User` button.
- **Table Columns**: User name, Username, Assigned Role badge, Created Date, Actions (`Edit Role`, `Set Quotas`, `Reset Password`, `Delete`).
- **Permissions**: Restricted to role `admin`.

### 6.3 Subsection: Financial Years Master (`#setSection-fy`)
- **Actions**: `+ Add FY`, `⚡ Seed Standard FYs`.
- **Table Columns**: FY Code, Start Date, End Date, Delete Action.

### 6.4 Subsection: Tax Structure Master (`#setSection-taxes`)
- **Dual Panel Layout**:
  - *GST Slabs*: Table of slab name, percentage rate, category (GST/IGST/EXEMPT), default indicator, delete action.
  - *TDS Sections*: Table of section code, description, withholding rate %, delete action.

---

## 7. Modal Dialogs Inventory

| Modal ID | Purpose | Key Inputs / Controls | Triggers & Output |
| :--- | :--- | :--- | :--- |
| `#invoiceModal` | Create Order / Invoice | Order Type toggle (PO vs Recurring), FY select, Customer select, PO#, PO Date, Executive, Currency, Exchange Rate, Multi-line items table (Qty, Unit Price, GST), Invoicing fields | Calls `POST /api/invoices`, updates orders table & dashboard |
| `#paymentModal` | Record Payment | Invoice ID, Net Cash Received, TDS Withholding Amount, TDS Section select, Payment Date, Payment Mode, Remittance UTR# | Calls `POST /api/payments`, updates invoice balance and audit logs |
| `#convertInvoiceModal` | Convert PO to Invoice | Invoice Number, Invoice Date, Due Date (Net 30 auto-calc) | Calls `POST /api/invoices/{id}/convert-to-invoice` |
| `#viewOrderModal` | View Order & Payments | Chronological header, customer snapshot, line items breakdown, payment tranches table (with delete payment button for finance/admin) | Calls `GET /api/invoices/{id}`, provides contextual actions |
| `#editOrderModal` | Edit Order / Invoice | Customer, PO#, PO Date, Executive, Due Date, Remarks, Line item descriptions (with immutability warnings for issued invoices) | Calls `PUT /api/invoices/{id}` |
| `#customerModal` | Add / Edit Customer | Company Name, GSTIN, City/State, Contact Person, Phone, Email | Calls `POST` or `PUT /api/customers` |
| `#addUserModal` | Add Team User | Full Name, Username, Password, Role (`admin`, `finance`, `sales`, `viewer`) | Calls `POST /api/users` |
| `#targetModal` | Manage Sales Quotas | FY select, Annual Target (auto-splits to Half-Yearly, Quarterly, Monthly), Margin targets | Calls `POST /api/users/{id}/targets` |
| `#addFYModal` | Add Financial Year | FY Code, Start Date, End Date | Calls `POST /api/settings/financial-years` |
| `#addGSTModal` | Add GST Slab | Tax Slab Name, Rate %, Category, Default checkbox | Calls `POST /api/settings/taxes/gst` |
| `#addTDSModal` | Add TDS Section | Section Code, Rate %, Description | Calls `POST /api/settings/taxes/tds` |
