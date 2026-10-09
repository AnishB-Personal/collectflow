# Lovable AI Project Brief — CollectFlow UI/UX Redesign

## 1. Project Mission & Identity
You are redesigning **CollectFlow**, a B2B Order-to-Cash (O2C) and Accounts Receivable (AR) management platform designed for IT service enterprises operating in India.

The existing backend is completely stable, secure, and production-tested on FastAPI and SQLite. Your mission is to build a modern, intuitive, responsive Single Page Application (SPA) on Lovable that replaces the legacy vanilla HTML/JS frontend without changing backend APIs or business rules.

---

## 2. Core Functional Requirements to Retain

1. **Authentication & Session Security**:
   - Bearer token authentication via `POST /api/auth/login` and `GET /api/auth/me`.
   - Automatic logout and redirect on `HTTP 401`.
   - Dynamic UI permissions based on user roles (`admin`, `finance`, `sales`, `viewer`).
2. **Order & Invoice Lifecycle**:
   - Register draft Purchase Orders (`UNBILLED` status).
   - Convert draft POs into official tax invoices (Net-30 auto due date calculation, immutable customer and totals upon conversion).
   - Cancel invoices (strictly allowed only when zero payments exist; preserves line items and audit logs).
3. **Multi-Currency & Indian Taxation**:
   - Deal currencies (INR, USD, EUR, GBP, AED) with base conversion to INR.
   - Itemized GST slabs (0%, 5%, 12%, 18%, 28%).
   - Client TDS Withholding tracking (Sections 194C, 194J, 194Q) recognizing both physical cash received and tax deductions as legitimate settlement credit.
4. **Sales Quota Tracking & Aging Analytics**:
   - Fiscal year dashboard with YoY comparisons and Aging buckets (*Current*, *1-30*, *31-60*, *61-90*, *90+* days).
   - Sales representative leaderboard calculating quota achievement % against active orders.
5. **Secure Auditing & Exporting**:
   - Two-step single-use ticket exchange for streaming Excel workbooks (`/api/export/token` -> `/api/export/excel`).

---

## 3. UI/UX Vision for Lovable

- **Layout**: Clean left sidebar navigation with collapsible rail, global topbar with breadcrumbs and `Cmd+K` quick search, and spacious data cards.
- **Components**: Use `shadcn/ui` primitives (Dialog, Sheet, Table, Badge, Popover, Command, Sonner).
- **Interactions**:
  - Replace heavy full-screen modals with right-hand slide-over **Sheet drawers** for inspecting invoice details, transaction history, and editing metadata.
  - In payment logging, use a searchable combobox to select invoices rather than typing raw numeric IDs.
  - Implement optimistic UI updates with TanStack Query and skeleton shimmer states during server fetch.

---

## 4. Integration Blueprint

- **Backend API Base**: `/api`
- **Specification**: Complete OpenAPI schema provided in `openapi.json`.
- **Prototyping Data**: Comprehensive sanitized fixtures provided in `MOCK_DATA.json`.
- **Reference Docs**: Review `FINANCIAL_BUSINESS_RULES.md`, `ROLES_AND_PERMISSIONS.md`, and `API_DOCUMENTATION.md` for complete contract specifications.
