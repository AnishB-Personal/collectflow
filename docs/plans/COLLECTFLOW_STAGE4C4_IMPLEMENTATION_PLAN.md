# CollectFlow — Phase 4 Stage 4C.4 Implementation Plan
## Payments, TDS Ledger & Non-Destructive Adjustments

**Feature Branch:** `feature/phase4-stage4c4-payments-adjustments`  
**Backend Baseline:** `bbaca70e77b0ec4070967adbeaece48af9938ae8`  
**Frontend Baseline:** `0770b0ac44013985d7dbef2546ebe71a7e573b8c`  
**Date:** 2026-10-17  
**Status:** In Progress (Authorized)

---

## 1. Executive Summary & Architecture Alignment

Stage 4C.4 establishes the immutable financial settlement engine for CollectFlow. In Phase 3, payment recordings and corrections were either handled in SQLite with destructive deletes or simulated in memory. Under Stage 4C.4, all LIVE financial operations are backed by PostgreSQL domain models:
- `payment_transactions`: Immutable ledger storing all settlement events (`PAYMENT`, `REVERSAL`, `ADJUSTMENT`).
- `commercial_invoices`: Authoritative accounts receivable invoice entity with row-level locking (`FOR UPDATE`).
- `tds_section_master`: Statutory withholding tax section validation (`194C`, `194J`, etc.).
- `audit_logs`: Append-only security and financial audit trail with before/after state snapshots.

Physical deletion of financial records is strictly prohibited in LIVE mode. All corrections operate via compensating negative reversals paired with corrected replacement transactions.

---

## 2. API Mapping & Domain Contracts

### 2.1 GET `/api/payments`
- **Purpose:** Authenticated payments ledger query with filtering, pagination, and settlement summaries.
- **Access Control:** Authenticated users (`admin`, `finance`, `sales`, `viewer`).
- **Query Parameters:**
  - `page`: integer (default: 1)
  - `limit`: integer (default: 50, max: 100)
  - `fy`: string, optional (e.g., `FY 2026-27`)
  - `invoice_id`: integer, optional
  - `customer_id`: integer, optional
  - `payment_mode`: string, optional (`NEFT`, `RTGS`, `CHEQUE`, `UPI`, `CASH`, `WIRE`, `OTHER`)
  - `transaction_type`: string, optional (`PAYMENT`, `REVERSAL`, `ADJUSTMENT`)
  - `date_from`: string (YYYY-MM-DD), optional
  - `date_to`: string (YYYY-MM-DD), optional
- **Response Structure:**
  ```json
  {
    "page": 1,
    "limit": 50,
    "total": 12,
    "total_pages": 1,
    "summary": {
      "total_cash": "200000.00",
      "total_tds": "9000.00",
      "total_settled": "209000.00"
    },
    "data": [
      {
        "id": 1,
        "invoice_id": 1,
        "parent_transaction_id": null,
        "transaction_type": "PAYMENT",
        "cash_amount": "200000.00",
        "tds_amount": "9000.00",
        "total_settled": "209000.00",
        "tds_section": "194C",
        "payment_date": "2026-04-18",
        "payment_mode": "NEFT",
        "remittance_number": "UTR-HDFC-9918273645",
        "notes": "Initial milestone payment",
        "recorded_by": 2,
        "created_at": "2026-04-18T10:30:00Z",
        "invoice_number": "INV-2026-042",
        "customer_id": 1,
        "customer_name": "Nexus Corp"
      }
    ]
  }
  ```

### 2.2 POST `/api/payments`
- **Purpose:** Record financial settlement (Cash, TDS, or combined) against an issued invoice.
- **Access Control:** `admin`, `finance` only (`require_finance_or_admin`).
- **Request Body (`PaymentCreateRequest`):**
  - `invoice_id`: integer (required)
  - `payment_amount`: MoneyString / Decimal (default: 0.00)
  - `tds_amount`: MoneyString / Decimal (default: 0.00)
  - `tds_section`: string, optional (required if `tds_amount > 0`)
  - `payment_date`: string YYYY-MM-DD (required)
  - `payment_mode`: string (`NEFT`, `RTGS`, `CHEQUE`, `UPI`, `CASH`, `WIRE`, `OTHER`)
  - `remittance_number`: string, optional
  - `notes`: string, optional
  - `idempotency_key`: string, optional (also accepts `Idempotency-Key` header)
- **Validation Rules:**
  1. `invoice_id` must reference an existing `commercial_invoices` record (404 if not found).
  2. Currency check: Invoice currency must be `INR`. If foreign currency, fail safely with HTTP 400.
  3. `lifecycle_status` must be `ISSUED`. Rejection with HTTP 400 if `CANCELLED`.
  4. Payment amounts: `payment_amount >= 0`, `tds_amount >= 0`. `total_settling = payment_amount + tds_amount > 0` (400 if non-positive).
  5. TDS Section: If `tds_amount > 0`, `tds_section` must be provided and exist in active `tds_section_master`. If `tds_amount == 0`, `tds_section` normalized to null.
  6. Idempotency replay check: If key matches prior settlement with identical payload, return HTTP 200 with `idempotent_replay: true`. If payload differs, reject with HTTP 409 Conflict.
  7. Row-level Lock & Overpayment Guard: `select(CommercialInvoice).where(id == invoice_id).with_for_update()`. Compute current net settled: `sum(total_settled)`. Reject if `total_settling > (total_amount - current_settled)` by even ₹0.01.
  8. Atomic Persistence: Insert `PaymentTransaction` row and append `audit_logs` entry in single transaction.

### 2.3 POST `/api/payments/{id}/correct`
- **Purpose:** Non-destructive atomic payment correction or full reversal.
- **Access Control:** `admin`, `finance` only.
- **Request Body (`PaymentCorrectionRequest`):**
  - `reason`: string (required, min 5 chars)
  - `corrected_payment_amount`: Decimal (default: 0.00)
  - `corrected_tds_amount`: Decimal (default: 0.00)
  - `corrected_tds_section`: string, optional (required if `corrected_tds_amount > 0`)
  - `corrected_payment_date`: string YYYY-MM-DD
  - `corrected_payment_mode`: string
  - `corrected_remittance_number`: string, optional
  - `notes`: string, optional
- **Validation & Execution Flow:**
  1. Retrieve original `PaymentTransaction` by `id` (404 if not found).
  2. Verify original transaction is `transaction_type == 'PAYMENT'` (reversals cannot be reversed directly).
  3. Check supersession: Reject with HTTP 400 if any child record already exists with `parent_transaction_id == original.id`.
  4. Lock parent invoice: `with_for_update()`.
  5. Ensure invoice is not `CANCELLED`.
  6. Calculate resulting settlement:
     - `net_settled_after = current_net_settled - original.total_settled + (corrected_cash + corrected_tds)`
     - Verify: `0 <= net_settled_after <= invoice.total_amount`.
  7. Insert `REVERSAL` transaction:
     - `parent_transaction_id = original.id`
     - `transaction_type = 'REVERSAL'`
     - `cash_amount = -original.cash_amount`
     - `tds_amount = -original.tds_amount`
     - `total_settled = -original.total_settled`
     - `payment_date = datetime.date.today()`
     - `notes = f"Reversal of Payment #{original.id}: {reason}"`
  8. Insert replacement `PAYMENT` transaction (if `corrected_cash + corrected_tds > 0`):
     - `parent_transaction_id = original.id`
     - `transaction_type = 'PAYMENT'`
     - `cash_amount = corrected_cash`
     - `tds_amount = corrected_tds`
     - `total_settled = corrected_cash + corrected_tds`
  9. Append `audit_logs` record with structured details and commit atomically.

### 2.4 DELETE `/api/payments/{id}`
- **Behavior:** Strictly prohibited in LIVE mode. Returns HTTP 400 detailing that accounting standards require POST `/api/payments/{id}/correct`.

---

## 3. Invoice Cancellation Integration Guard

In `app/routers/invoices.py`, the existing `cancel_invoice()` endpoint guarded against cancellation by counting total payment rows. Under an immutable ledger, reversed invoices retain historical transaction rows.

- **Updated Rule:**
  - Invoices with active net positive settlements (`net_settled > 0` or `net_cash > 0` or `net_tds > 0`) cannot be cancelled (HTTP 400).
  - Invoices whose settlements have been 100% reversed (`net_settled == 0` and `net_cash == 0` and `net_tds == 0`) CAN be cancelled.
  - No physical payment rows are deleted.

---

## 4. Frontend LIVE Integration

- **`src/lib/api-client.ts`:**
  - Add `getPayments(params)`
  - Add `createPayment(data)`
  - Add `correctPayment(id, data)`
- **`src/components/collectflow/payment-workflows.tsx`:**
  - `RecordPaymentDrawer`: When `isLive`, call `apiClient.createPayment(payload)`. Preserve 4-step workflow (`form` -> `review` -> `confirm` -> `result`).
  - `PaymentCorrectionDrawer`: When `isLive`, replace simulated deletion with single atomic call to `apiClient.correctPayment(payment.id, payload)`.
- **`src/components/collectflow/payments-ledger.tsx`:**
  - In LIVE mode, display authoritative payments ledger data from PostgreSQL API records.
  - Enable correction drawer opening for finance/admin roles.
- **`src/components/collectflow/workspace.tsx`:**
  - Enable `onCorrectPayment` in LIVE mode.
  - Trigger `fetchLiveRecords()` on drawer close to refresh invoice and payment state.
- **Preserve MOCK mode:**
  - All Phase 3 mock behavior and test expectations remain unaltered when `isLive === false`.

---

## 5. Test Suite & Verification Matrix

| Acceptance Case | Description | Verification Method |
| :--- | :--- | :--- |
| **AC-01** | Cash-only settlement | Pytest + Vitest |
| **AC-02** | TDS-only settlement (194C / 194J) | Pytest + Vitest |
| **AC-03** | Combined cash & TDS settlement | Pytest + Vitest |
| **AC-04** | Full settlement transitioning invoice to PAID | Pytest |
| **AC-05** | Partial settlement retaining PARTIAL status | Pytest |
| **AC-06** | Overpayment rejection down to ₹0.01 | Pytest boundary test |
| **AC-07** | Zero and negative settlement rejection | Pytest validation test |
| **AC-08** | TDS section validation against `tds_section_master` | Pytest master test |
| **AC-09** | Idempotency replay & conflicting key rejection | Pytest replay test |
| **AC-10** | Concurrent payments row-level lock safety | Multithreaded pytest |
| **AC-11** | Immutability: original payment unaltered | Pytest DB inspection |
| **AC-12** | Non-destructive full reversal & replacement | Pytest correction test |
| **AC-13** | Double-correction rejection | Pytest duplicate test |
| **AC-14** | Atomic rollback on correction failure | Pytest transaction test |
| **AC-15** | Invoice cancellation after full reversal | Pytest integration test |
| **AC-16** | RBAC enforcement across roles | Pytest auth test |
| **AC-17** | LIVE browser workflows (drawers, ledgers) | Playwright E2E |
| **AC-18** | MOCK mode regression & responsive UI | Vitest + Playwright |

---

## 6. Scope Boundaries & Constraints

- Strictly INR currency support; foreign currency settlements fail safely with HTTP 400.
- No branch merges to `main`.
- No Stage 4C.5 target management or reporting engine modifications.
- Deliver traceable commits to `feature/phase4-stage4c4-payments-adjustments`.
