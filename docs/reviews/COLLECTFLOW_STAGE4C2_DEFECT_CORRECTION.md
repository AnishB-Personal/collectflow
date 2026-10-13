# CollectFlow — Stage 4C.2 Independent UAT Defect Correction Report

## 1. Executive Summary
- **Evaluation Status:** PASS (All confirmed defects corrected and independently verified)
- **Frontend Staging:** [collectflow-stage4c2-staging.aanishb.workers.dev](https://collectflow-stage4c2-staging.aanishb.workers.dev)
- **Backend Tunnel:** [bags-condition-energy-flexible.trycloudflare.com](https://bags-condition-energy-flexible.trycloudflare.com)
- **Phase 3 Mock Baseline:** [anishb-personal-cash-flow-buddy-101.aanishb.workers.dev](https://anishb-personal-cash-flow-buddy-101.aanishb.workers.dev) (Untouched)
- **Frontend SHA:** `00f0955eded2956a1465dd89f4ffd17805df6099`
- **Backend SHA:** `5eebee5af891b154f43358cd5a3f5d2984d5f483`

---

## 2. Defect Resolution Summary

### Defect CF4C2-B01 — Backend 502 Connection Refused
- **Root Cause:** Default `cloudflared` quick tunnel used UDP/QUIC protocol which dropped edge streams when idle (`accept stream listener error: failed to accept QUIC stream`), causing the public tunnel to respond with HTTP 502 Bad Gateway even while local uvicorn was running.
- **Fix:** Switched tunnel to robust TCP multiplexing with `--protocol http2 --url http://127.0.0.1:8000`. Confirmed 100% stable connection across all live endpoints (`/ready`, `/openapi.json`, `/api/auth/me`).

### Defect CF4C2-01 — Live Login but Mock Workspace
- **Root Cause:** The workspace shell (`Workspace.tsx`) rendered mock indicators, role switcher, and demo banners unconditionally because it had no awareness of `apiClient.getMode() === 'live'`.
- **Fix:** 
  1. Gated workspace shell to detect live mode.
  2. Suppressed "Mock workspace", demo banner, and "Reset Demo" button in live mode, replacing them with "Live Sandbox Workspace" indicator.
  3. Added live session sign-out action in the sidebar footer.
  4. Loaded live customer data via `apiClient.getCustomers()` in `CustomerDirectory` (`Acme Cloud Solutions Pvt Ltd`, `TechVanguard Infotech LLP`).

### Defect CF4C2-02 — Protected Customer Route (`/customers`) Unauthenticated Access
- **Root Cause:** Direct navigation to `/customers` rendered workspace mock components directly without checking `apiClient.getToken()` in route lifecycle.
- **Fix:** Added `beforeLoad` guard and `ssr: false` in [src/routes/customers.tsx](file:///home/anishb/projects/personal/cash-flow-buddy-101/src/routes/customers.tsx) and [src/routes/index.tsx](file:///home/anishb/projects/personal/cash-flow-buddy-101/src/routes/index.tsx) redirecting unauthenticated live requests directly to `/login`.

### Customer Role Permissions & Money Formatting
- **Root Cause:** `POST /api/customers` used `require_finance_or_admin`, rejecting `sales` users (403) despite Stage 4C.0 specification granting `sales` creation rights. Additionally, customer money statistics were formatted as floats rather than exact 2-decimal strings.
- **Fix:** 
  1. Updated [app/routers/auth.py](file:///home/anishb/projects/personal/collectflow/app/routers/auth.py) with `require_sales_finance_admin = require_roles("admin", "finance", "sales")`.
  2. Quantized customer totals (`total_billed`, `total_paid`, `total_outstanding`) to `Decimal("0.01")` and formatted as exact 2-decimal strings (`f"{val:.2f}"`).

---

## 3. Automated Verification Evidence

1. **Backend Test Suite (pytest):**
   - 29/29 tests passed (`tests/test_database_isolation.py`, `tests/test_postgres_domain_models.py`, `tests/test_sandbox_auth_integration.py`, `tests/test_sandbox_cors_isolation.py`, `tests/test_stage4c2_live_services.py`).
2. **Frontend Test Suite (Vitest & tsc):**
   - 543/543 unit and integration tests passed.
   - `npx tsc --noEmit` exited cleanly with 0 errors.
3. **End-to-End Browser Verification (Playwright):**
   - `e2e/stage4c2-defect-correction-uat.spec.ts`: 4/4 passed (Tunnel health, unauthenticated route redirection, live login + customer master verification, sales role customer creation + decimal formatting).
   - `e2e/stage4c2-viewports.spec.ts`: 4/4 passed (1440px desktop, 768px tablet, 390px mobile, 320px narrow mobile).
4. **Preserved Phase 3 Baseline:**
   - Verified untouched at `https://anishb-personal-cash-flow-buddy-101.aanishb.workers.dev/` returning HTTP 200.

---

## 4. Tunnel Maintenance Instructions
The Cloudflare HTTP/2 tunnel is running as a daemon background task:
- Local Port: `127.0.0.1:8000`
- Tunnel URL: `https://bags-condition-energy-flexible.trycloudflare.com`
- Process: `/home/anishb/.local/bin/cloudflared tunnel --protocol http2 --url http://127.0.0.1:8000`
- To inspect tunnel logs or restart if required:
  `./venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000`
  `cloudflared tunnel --protocol http2 --url http://127.0.0.1:8000`
