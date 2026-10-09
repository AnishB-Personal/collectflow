# CollectFlow — Code Review Changelog

**Review Date:** October 7, 2026  
**Auditor:** Senior Software Architect & Security Reviewer  

This document logs all architectural refactorings, security remediations, and financial logic corrections applied across the repository.

---

### 1. `app/utils/financial.py` [NEW]
- Added high-precision monetary arithmetic using Python `Decimal` with `ROUND_HALF_UP` rounding.
- Implemented `calculate_line_item(qty, unit_price, tax_rate)` and `calculate_invoice_totals(items)`.
- Implemented `calculate_net_30_due_date(invoice_date)` for standard Indian B2B credit terms.
- Implemented `get_current_financial_year(dt)` for Indian fiscal year computation (1 April – 31 March).
- Defined master list of supported currencies (`INR`, `USD`, `EUR`, `GBP`, `AED`) with INR as base.

### 2. `app/database.py` [MODIFIED]
- **Connection Pragma Hardening:** Enabled `PRAGMA foreign_keys = ON;`, `PRAGMA journal_mode = WAL;`, and `PRAGMA busy_timeout = 5000;` on all database connections.
- **Transaction Atomicity:** Added `transaction()` context manager supporting multi-statement atomic operations with automatic commit on success and rollback on exception.
- **Dynamic DB Path:** Supported `COLLECTFLOW_DB_PATH` environment variable for non-destructive test isolation.
- **Audit Logging Engine:** Initialized `audit_logs` table schema and added `log_audit()` helper.

### 3. `app/routers/auth.py` [MODIFIED]
- **Removed Admin Backdoor:** Completely eliminated hardcoded bypass (`if uname == 'admin' and req.password in ('admin', 'admin123')`).
- **RBAC Dependencies:** Created reusable role enforcement dependencies: `require_roles()`, `require_admin()`, and `require_finance_or_admin()`.
- **Session Revocation:** Password changes now immediately invalidate existing active sessions.
- **Audit Logging:** Added audit entries on login success/failure, logout, and password modification.

### 4. `app/routers/users.py` [MODIFIED]
- **Fixed HTTP 405 Method Not Allowed:** Added `PUT /api/users/{user_id}/role` and `PUT /api/users/{user_id}` endpoints.
- **Sales Executive Sync:** Automatically synchronized `executives` records when sales users are created/modified so they populate PO dropdowns reliably.
- **Admin Role Enforcement:** Restricted user creation, deletion, password reset, and role assignment to `require_admin`.
- **Target Validation:** Validated quota target inputs against negative values.

### 5. `app/routers/customers.py` [MODIFIED]
- **Fixed TDS Omission in Outstanding Balance:** Updated customer aggregate query so `total_paid` includes both `payment_amount` and `tds_amount`.
- **Role Permissions:** Restricted customer updates and deletions to `require_finance_or_admin`.
- **Referential Integrity:** Added check preventing customer deletion if active invoices or orders reference the customer.
- **Input Validation:** Enforced non-empty customer billing names and sanitized GST numbers.

### 6. `app/routers/invoices.py` [MODIFIED]
- **Secured Unauthenticated Endpoints:** Added `get_current_user` dependency to invoice listing and creation.
- **Finance/Admin RBAC:** Restricted PO conversion (`convert-to-invoice`) and deletion (`delete-invoice`) to `require_finance_or_admin`.
- **Atomic Multi-line Writes:** Wrapped invoice creation and item inserts inside a single `transaction()` block to prevent partial or orphaned records.
- **Net-30 Due Date:** Automatically calculated Net-30 payment due dates when invoice date is present and due date is omitted.
- **Multi-currency Validation:** Preserved exchange rates at invoice creation time without dynamic re-conversion.

### 7. `app/routers/payments.py` [MODIFIED]
- **Secured Payment Recording:** Added `require_finance_or_admin` to payment recording.
- **Overpayment Prevention:** Added balance validation preventing payments and TDS deductions exceeding invoice outstanding balance.
- **Payment Deletion / Reversal:** Added `DELETE /api/payments/{payment_id}` restricted to finance and admin roles.
- **Audit Logging:** Recorded audit logs for all payment creations and reversals.

### 8. `app/routers/settings.py` [MODIFIED]
- **RBAC on Master Data:** Restricted financial year and tax master modifications to `require_finance_or_admin`.
- **FY Referential Integrity:** Blocked deletion of financial years that have associated invoices.
- **Soft Deactivation:** Configured tax rates and TDS sections to soft-deactivate (`is_active = 0`) to preserve historical invoice integrity.
- **Date Validation:** Enforced valid ISO dates and start-before-end ordering on financial year creation.

### 9. `app/routers/dashboard.py` [MODIFIED]
- **Secured Dashboard API:** Protected `/api/dashboard/summary` with `get_current_user`.
- **Dynamic Indian Fiscal Year:** Replaced hardcoded `'FY 2026-27'` with dynamic calculation.
- **Metric Segregation:** Separated Cash Collected, TDS Recognized, and Total Settlement.
- **Fixed Executive Query:** Joined with `v_invoice_summary` to accurately account for TDS settlements in sales executive targets.

### 10. `app/routers/export.py` [MODIFIED]
- **Secured Data Export:** Added authentication via Bearer header and download token parameter.
- **Expanded Export Columns:** Added Cash Received (₹) and TDS Deducted (₹) alongside Total Settled (₹) and Balance Outstanding (₹).

### 11. `app/templates/index.html` [MODIFIED]
- **Stored XSS Prevention:** Implemented `escapeHtml()` across all dynamic table rows and tooltips.
- **Authenticated API Client:** Centralized frontend requests through `authFetch()` with automatic Bearer token injection and 401 handling.
- **Branding Scrubbing:** Removed leftover city placeholder (`Trivandrum, KL` -> `e.g. Mumbai, MH`).
- **Export Trigger:** Connected Excel export button to pass authentication token and active FY filter.

### 12. `pyproject.toml` & `tests/` [NEW]
- Added `pyproject.toml` configuring Ruff, Bandit, Pytest, and Pytest-Cov.
- Added `.gitignore` protecting credentials, databases, cache, and virtual environments.
- Added comprehensive integration test suites (`tests/test_auth_rbac.py`, `tests/test_invoices.py`, `tests/test_payments.py`, `tests/test_customers.py`, `tests/test_settings_and_dashboard.py`).

---

## Pass 2 Production Hardening & Remediation (October 7, 2026)

### 13. Export Authentication & Single-Use Scoped Tokens (`app/routers/export.py`, `app/database.py`)
- Removed bearer token exposure from URL query parameters.
- Implemented `POST /api/export/token` to mint short-lived, single-use, scoped export tickets (5-minute TTL).
- Atomic redemption of tickets in `GET /api/export/excel` via `UPDATE export_tickets SET used = 1`.
- Added automated tests proving unauthenticated and expired export attempts fail with HTTP 401.

### 14. Cryptographic Session Storage & Revocation (`app/database.py`, `app/routers/auth.py`, `app/routers/users.py`)
- Replaced raw session token storage in SQLite with SHA-256 token hashes (`token_hash = sha256(raw_token)`).
- Raw bearer tokens are only held by the client; database compromise does not leak usable session credentials.
- Automatic session revocation on password changes.
- Immediate session revocation when a user's role is updated by an administrator (`DELETE FROM sessions WHERE user_id = ?`).
- Added periodic `cleanup_expired_sessions()` routine.

### 15. Progressive Login Rate-Limiting (`app/routers/auth.py`, `app/database.py`)
- Added `login_attempts` table tracking IP and username attempts.
- Progressive rate limiting rejects brute-force attempts after 5 consecutive failures within a 15-minute sliding window.
- Avoids permanent denial-of-service account lockouts against valid corporate staff.
- Documented PBKDF2 iteration count (100,000 iterations with SHA-256).

### 16. Elimination of Arbitrary Tolerances & Strict Decimal Normalization (`app/routers/payments.py`, `app/database.py`)
- Removed arbitrary `+ 0.05` balance tolerance in payment validation.
- All monetary validation enforced under `BEGIN IMMEDIATE` transaction lock in SQLite to eliminate double-spend race conditions.
- Upgraded SQLite SQL view `v_invoice_summary` with explicit `ROUND(..., 2)` to eliminate floating-point representation drift.
- Rejection of negative cash payments, negative TDS deductions, and zero settlement requests.
- Added ₹0.01 fractional edge case tests.

### 17. Legal Invoice Immutability (`app/routers/invoices.py`)
- Implemented `PUT /api/invoices/{order_id}` (`InvoiceUpdate`) enforcing strict statutory immutability on issued invoices (`invoice_number` present).
- Changes to financial amounts, dates, or line items on issued invoices are blocked with HTTP 400.
- Only internal operational remarks can be updated on issued invoices.
- Unbilled POs allow metadata adjustments prior to commercial issuance.
- Added duplicate invoice number and duplicate PO number collision protection.

### 18. Structured Observability & Security Response Headers (`app/main.py`)
- Added structured request logging middleware attaching unique `request_id` (correlation ID), duration in milliseconds, status code, and client IP.
- Enforced strict HTTP security response headers:
  - `Content-Security-Policy`
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy: geolocation=(), camera=(), microphone=()`
- Added production health check endpoints: `GET /health` (liveness) and `GET /ready` (readiness with database connectivity and WAL verification).

### 19. Cross-Site Scripting (XSS) Defenses & Client Cache Optimization (`app/templates/index.html`)
- Audited all DOM sinks (`innerHTML`, `onclick`).
- Escaped all dynamic user, customer, and invoice fields.
- Replaced dangerous string interpolation inside inline event handlers with client-side cache lookups by numeric ID (`cachedInvoices`, `cachedCustomers`, `cachedUsers`, `cachedFYs`).
- Centralized all state-changing frontend actions through `authFetch()`.

### 20. Expanded Financial Regression & Multi-Threaded Concurrency Testing (`tests/test_financial_regression.py`, `tests/test_xss_protection.py`)
- Created comprehensive financial regression test suite covering:
  - Aging boundary tests: 0, 1, 30, 31, 60, 61, 90, 91, 180, 181 days.
  - TDS-only settlement to PAID.
  - Multiple partial payments reaching exact 0.00 balance.
  - Payment reversal restoring balance.
  - Foreign exchange rate boundary validation (rejecting <= 0).
  - Indian Fiscal Year March 31 / April 1 boundary transitions.
  - Multi-threaded concurrent payment test with 10 parallel threads proving zero overpayments or locked database errors under SQLite WAL mode.
- Created `tests/test_xss_protection.py` validating security headers and neutralization of malicious `<script>` and `<img>` payloads.
- Test suite total: 41 automated tests passing with 79% statement coverage and 75% branch coverage.
- Bandit security scan: 0 issues.
- Pip-audit: 0 known vulnerabilities.
