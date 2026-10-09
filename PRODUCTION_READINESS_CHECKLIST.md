# CollectFlow Production Readiness Verification Checklist

This checklist documents the production readiness verification of **CollectFlow**, covering security, financial integrity, transactional concurrency, observability, and infrastructure deployment standards.

---

## 1. Financial Accuracy & Monetary Integrity

- [x] **Deterministic Monetary Arithmetic**
  - All monetary values (`exclusive_amount`, `tax_amount`, `total_amount`, `payment_amount`, `tds_amount`, `outstanding_balance`, `yearly_target`) are rounded using `Decimal` and `ROUND_HALF_UP` via [`round_currency`](file:///home/anishb/projects/personal/collectflow/app/utils/financial.py).
  - Floating-point binary tolerances (such as `+ 0.05`) have been completely eliminated.
- [x] **Paise Normalization & Edge Cases**
  - ₹0.01 fractional edge cases verified in test suite.
  - Multi-line item tax computations verified against sum-of-lines vs line-of-sums divergence.
- [x] **Invoice Immutability**
  - Issued invoices (`invoice_number` present) are legally locked against retroactive financial modification, date tampering, or line item alteration.
  - Only non-financial internal remarks are mutable on issued invoices via `PUT /api/invoices/{order_id}`.
- [x] **Payment Reconciliation Safeguards**
  - Payments strictly verified under SQLite transaction lock: `payment_amount + tds_amount <= outstanding_balance`.
  - Negative cash payments, negative TDS deductions, and zero settlement attempts are rejected with HTTP 400.
  - Duplicate payment prevention via unique remittance numbers (`remittance_no`).
  - Payment deletion correctly rolls back invoice settlement and reconciles balance.

---

## 2. Authentication, Authorization & Session Hardening

- [x] **Database Session Token Hashing**
  - Bearer tokens are never stored in plaintext in the database.
  - Database stores `SHA-256(raw_session_token)` in the `sessions` table.
  - Constant-time hash verification via `hashlib.sha256()`.
- [x] **Progressive Brute-Force Rate Limiting**
  - Failed logins tracked per IP/identifier in `login_attempts` table.
  - Enforces a 15-minute cooldown period after 5 consecutive failures.
  - Rejects automated credential stuffing without triggering permanent account lockouts (preventing denial-of-service against legitimate staff).
- [x] **Active Session Revocation**
  - Password change revokes all active bearer tokens across all devices for that user.
  - Admin/Finance role modification immediately purges existing sessions for that user, requiring re-authentication under the new role privileges.
  - Scheduled cleanup of expired sessions via `cleanup_expired_sessions()`.
- [x] **Export Authentication Hardening**
  - Session tokens removed from URL query parameters (`/api/export/excel?token=...` eliminated).
  - Export supports either standard `Authorization: Bearer <token>` header or short-lived, single-use, scoped export tickets minted via `POST /api/export/token` (5-minute TTL, atomic invalidation).

---

## 3. Concurrency & Transactional Boundaries

- [x] **SQLite WAL Configuration**
  - Write-Ahead Logging (`PRAGMA journal_mode = WAL`) and `PRAGMA synchronous = NORMAL` configured on database initialization.
  - `PRAGMA busy_timeout = 5000` (5-second retry under write lock contention).
- [x] **Explicit Transaction Boundaries**
  - All multi-statement write operations wrapped in `transaction()` context manager using `BEGIN IMMEDIATE` to prevent deadlock and lost updates.
- [x] **Multi-threaded Stress Testing**
  - 10 concurrent worker threads executing simultaneous payment requests verified: zero negative balances, zero duplicate double-spends, zero database lock exceptions.

---

## 4. Security Headers & Injection Defenses

- [x] **Static Security Response Headers**
  - `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self'; font-src 'self' https://cdn.jsdelivr.net; object-src 'none'; base-uri 'self';`
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy: geolocation=(), camera=(), microphone=()`
- [x] **Cross-Site Scripting (XSS) Defenses**
  - All dynamic data rendered into the DOM sanitized via `escapeHtml()`.
  - Inline event handlers refactored to use client-side cache lookups by numeric ID, eliminating string interpolation injection vectors.
  - Automated tests verify payloads like `<script>alert(1)</script>` and `<img src=x onerror=alert(1)>` are neutralized.
- [x] **Static Analysis & Vulnerability Scans**
  - **Bandit Security Scan**: 0 issues identified across the application codebase.
  - **Pip-Audit**: 0 known CVEs across all installed virtual environment dependencies.
  - **Ruff Linter**: Clean (0 lint errors).

---

## 5. Observability & Audit Logging

- [x] **Liveness & Readiness Probes**
  - `GET /health`: Lightweight liveness check returning uptime and status.
  - `GET /ready`: Deep readiness probe checking database connectivity and WAL mode status.
- [x] **Correlation ID & Structured Logging Middleware**
  - Every incoming HTTP request assigned a unique `request_id` (via `X-Request-ID` header or generated UUID4).
  - Middleware logs structured JSON metrics: timestamp, method, path, status_code, client_ip, request_id, and duration_ms.
- [x] **Audit Trail Integrity & Sanitization**
  - Application logs business events to the `audit_logs` table with associated `request_id`.
  - Passwords, password hashes, bearer tokens, and Authorization headers are automatically redacted prior to logging.
  - Audit trail is strictly append-only (no modification or deletion APIs exist).

---

## 6. Production Deployment Architecture Constraints

- [x] **Single-Primary SQLite Architecture**
  - SQLite requires local disk storage with POSIX advisory lock support (NVMe or local SSD).
  - **DO NOT** deploy SQLite over networked filesystems (NFS, CIFS, SMB, or GlusterFS) due to broken advisory lock semantics.
  - In containerized environments (Docker / Kubernetes), deploy as a single container pod with a persistent volume claim (`ReadWriteOnce`).
- [x] **Worker Process Model**
  - For SQLite WAL mode, run Uvicorn with a controlled worker count (e.g., 2–4 workers) behind Nginx or Caddy reverse proxy.
  - `PRAGMA busy_timeout = 5000` ensures workers yield cleanly during write operations.
- [x] **Backup & Recovery Protocol**
  - Automated backups configured via non-blocking SQLite backup API per [`BACKUP_AND_RECOVERY.md`](file:///home/anishb/projects/personal/collectflow/BACKUP_AND_RECOVERY.md).
  - RPO < 15 min, RTO < 10 min.
