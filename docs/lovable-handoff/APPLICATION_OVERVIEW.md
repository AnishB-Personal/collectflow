# CollectFlow — Application Overview & Architecture Handover

## 1. Executive Summary & Business Objectives

**CollectFlow** is a specialized B2B Order-to-Cash (O2C) and Accounts Receivable (AR) Management system built specifically for enterprise IT services and consulting organizations following the Indian statutory fiscal calendar and tax framework (GST + TDS withholding).

### Primary Business Objectives
1. **Bridge Commercial Sales and Financial Billing**: Allow Account Executives to register confirmed client Purchase Orders (POs) or recurring service subscriptions while allowing Finance to review, convert, and stamp them with legally compliant commercial tax invoices.
2. **Track Cash Flow & Tax Deducted at Source (TDS)**: Accurately track multi-tranche customer payments, segregating physical cash remittances from client tax withholding (Income Tax Act sections 194C, 194J, etc.) without corrupting bank reconciliations or receivable balances.
3. **Prevent Invoice Tampering & Guarantee Immutability**: Enforce legal immutability on issued commercial tax invoices (freezing numbers, line items, customer details, and tax totals) while allowing operational corrections (internal notes, payment due date extensions, sales rep reassignments).
4. **Drive Sales Quotas & Performance Visibility**: Maintain annual, half-yearly, quarterly, and monthly sales quota achievement leaderboards by fiscal year, tracking active closed deals against pending receivables.

---

## 2. Primary User Personas & Responsibilities

| Role Key | Role Name | Primary Responsibilities |
| :--- | :--- | :--- |
| `admin` | **System Administrator** | Complete system access: User account management, role assignment, credential resets, fiscal year masters, tax slab definitions, operational data purging, and full financial oversight. |
| `finance` | **Finance & Billing Operations** | Commercial invoicing: Converting draft POs to tax invoices, recording cash payments and client TDS deductions, correcting/deleting clerical payments, cancelling unpaid invoices, and exporting financial audits. |
| `sales` | **Account Executive / Sales Rep** | Pipeline & Order Logging: Registering customer records, logging client Purchase Orders (POs) and recurring contracts, tracking assigned deals, reviewing customer payment history, and tracking personal quota attainment. |
| `viewer` | **Auditor / Executive Viewer** | Read-only access: Reviewing high-level KPI dashboards, AR aging summaries, and order tables without permissions to mutate financial records. |

---

## 3. Core Business Processes

### 3.1 Order-to-Cash (O2C) Lifecycle
```
[Client Issues PO / Subscription]
               │
               ▼
   [Sales Logs PO / Contract]  ── (Status: UNBILLED)
               │
               ▼
 [Finance Converts to Invoice] ── (Assigns Invoice #, Tax Date, Net-30 Due Date)
               │
               ▼
    [Issued Tax Invoice]       ── (Status: PENDING or OVERDUE)
               │
        ┌──────┴────────────────────────┐
        ▼                               ▼
[Partial Payment / TDS]         [Full Settlement / TDS]
 (Status: PARTIAL)               (Status: PAID)
        │                               │
        └───────────────┬───────────────┘
                        ▼
               [Reconciliation]
```

### 3.2 Indian Statutory Taxation Mechanics
1. **Goods & Services Tax (GST)**:
   - Slabs: 0% (Exempt/SEZ), 5%, 12%, 18% (Standard IT/Services), 28% (Hardware).
   - Applied at item level; computed on exclusive subtotal.
   - Header totals maintain exact relational identity: `Total = Exclusive Subtotal + GST Amount`.
2. **Tax Deducted at Source (TDS Withholding)**:
   - Deducted by client companies before remitting funds (Sections 194J @ 2% or 10%, 194C @ 1% or 2%, 194Q @ 0.1%).
   - In CollectFlow: `Settlement Credit = Net Cash Received + TDS Amount`.
   - Outstanding balance: `Outstanding = Total Invoice Amount - (Total Cash Paid + Total TDS Deducted)`.

---

## 4. Current vs. Target Architecture

### Backend Architecture (Preserved)
- **Framework**: Python 3.12 + FastAPI.
- **Database**: SQLite 3 in WAL (Write-Ahead Logging) mode with `BEGIN IMMEDIATE` atomic transaction locking and SQL views (`v_invoice_summary`).
- **Authentication**: Bearer token session architecture with PBKDF2-HMAC-SHA256 password hashing and constant-time token comparison.
- **Reporting**: Streaming XLSX generation via `openpyxl` protected by single-use, 60-second cryptographically hashed export tickets.

### Existing Frontend Architecture (To Be Replaced)
- **Structure**: Single monolithic template (`app/templates/index.html`) loaded via Jinja/FastAPI.
- **Styling**: Tailwind CSS via CDN script tag.
- **Charts**: Chart.js via CDN script tag.
- **JavaScript**: Monolithic vanilla JavaScript file (`static/js/app.js`) handling DOM manipulation, manual fetch wrappers, modal transitions, and table rendering.

### Target Frontend Architecture (Lovable AI Redesign)
- **Modern Component Framework**: React / Next.js / Vite + TypeScript.
- **UI Library**: Tailwind CSS + shadcn/ui + Radix UI primitives + Lucide icons.
- **State Management**: TanStack Query (React Query) for server-state caching, automatic refetching, and pagination.
- **Form Management**: React Hook Form + Zod for robust client-side validation mirroring backend Pydantic models.
- **Integration**: Pure decoupled Single Page Application (SPA) consuming existing `/api/*` endpoints with zero backend modifications.
