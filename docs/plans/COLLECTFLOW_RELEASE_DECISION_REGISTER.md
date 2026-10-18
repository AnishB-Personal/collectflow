# CollectFlow Post-Phase 4C Release Business Decision Register

**Document Reference:** `COLLECTFLOW_RELEASE_DECISION_REGISTER.md`  
**Stage:** Post-Phase 4C Release Readiness Preflight (Revised per Independent Architecture Review)  
**Date:** October 18, 2026  
**Status:** DRAFT — PENDING FORMAL STAKEHOLDER & FINANCE APPROVAL  
**Governance Rule:** *Antigravity provides technical analysis and engineering recommendations; formal policy determinations remain strictly the prerogative of the Product Owner, Finance Controller, and Statutory Auditor. No planning recommendation in this register constitutes completed business sign-off.*

---

## 1. Executive Summary & Governance Overview

During Phase 4C, the engineering team achieved mathematical parity and technical compliance with the approved integration contracts. However, moving from an isolated development sandbox to an enterprise production environment exposes four foundational business-policy decisions where technical choices directly impact statutory accounting compliance, financial audit certification, and operational workflows.

This register formally documents each policy dilemma, provides technical and legal context under Indian accounting standards (Ind AS / Companies Act / GST), evaluates practical options with tradeoffs, presents the engineering recommendation, and defines the required approval authority.

### Decision Classification Summary
| Decision ID | Domain | Policy Topic | Classification | Required Sign-Off | Approval Status |
|---|---|---|---|---|---|
| **BPD-01** | Accounting | Historical Correction-Date & Period-Locking Policy | **Mandatory before go-live** | Head of Finance & Statutory Auditor | **PENDING QUALIFIED FINANCE / AUDITOR APPROVAL** |
| **BPD-02** | Security / RBAC | Authoritative Data Export Role Permissions | **Mandatory before go-live** | Product Owner & Head of Sales | **PENDING PRODUCT OWNER & SALES LEAD APPROVAL** |
| **BPD-03** | Operations | Customer Deactivation with Open Orders or Receivables | **Mandatory before go-live** | Product Owner & Head of Finance | **PENDING PRODUCT OWNER & FINANCE APPROVAL** |
| **BPD-04** | Treasury / Forex | Foreign-Currency Settlement Scope & Technical Gate | **Required before feature enabled** | Head of Finance & Treasury Lead | **PENDING PRODUCT OWNER & TREASURY APPROVAL** |

---

## 2. Business Decision Records

### BPD-01: Historical Correction-Date Accounting & Statutory Period-Locking

#### 1. Context & Current Source Implementation
In Stage 4C.4, non-destructive payment corrections were introduced via `POST /api/payments/{id}/correct`. 
- Every correction generates a compensating `REVERSAL` transaction and, if replacement funds exist, a new `PAYMENT` transaction.
- In [`app/routers/payments.py`](file:///home/anishb/projects/personal/collectflow/app/routers/payments.py#L538-L544), the application enforces:
  `corr_date >= orig.payment_date`.
- The reversal transaction is stamped with `payment_date = corr_date` (which defaults to `date.today()` or an explicit user-supplied date).
- **The Operational Reality:** If an invoice issued in FY 2024-25 had a payment recorded on 2024-08-15, and on 2026-10-18 a user corrects an error, the reversal is posted with `payment_date = 2026-10-18` (falling in FY 2026-27).

#### 2. The Core Dilemma: Operational Reporting vs. Statutory Closed-Period Audit
1. **Operational Financial Reporting Perspective:**
   - Management wants cash reports to reflect real-time operational adjustments as they happen.
   - Posting the reversal in the current open period reflects actual bank reconciliation for the current month.
2. **Statutory Closed-Period Tax & Audit Perspective:**
   - Indian financial years close on March 31.
   - Once financial statements are certified and tax returns (GSTR-1, GSTR-3B, GSTR-9, TDS Form 26Q) are filed, the books for that financial year are **statutorily closed**.
   - If a reversal is backdated into a closed financial year, it retroactively mutates closed books, invalidating statutory filings and triggering tax penalties.
   - Conversely, if a reversal is posted in the current FY for an invoice belonging to a closed FY, the Collections Ledger for FY 2024-25 remains unadjusted, while FY 2026-27 records an adjustment against an invoice from two years prior.

#### 3. Options Evaluation
- **Option 1: Current Operational Behavior (No Period Locking)**
  - *Mechanism:* Users can reverse any payment from any prior date as long as `corr_date >= orig.payment_date`.
  - *Pros:* Simple, flexible, zero development overhead.
  - *Cons:* High statutory risk. Users can accidentally alter prior closed-period figures or post adjustments that disrupt annual tax reconciliations.
- **Option 2: Strict Period Locking with Current-Period Prior-Period Adjustments (Recommended)**
  - *Mechanism:* Add a `is_closed` or `status` (`OPEN`, `LOCKED`) flag to `financial_years`. Any payment or reversal with a `payment_date` in a `LOCKED` FY is strictly rejected. Corrections for closed-period transactions must be stamped with a date in the *currently open* FY and tagged as `PRIOR_PERIOD_ADJUSTMENT`.
  - *Pros:* 100% compliant with Ind AS 8 (Accounting Policies, Changes in Accounting Estimates and Errors) and Companies Act statutory audit requirements.
  - *Cons:* Requires schema and UI enhancement to display period-locking status.
- **Option 3: Strict Freeze (Zero Prior-Year Corrections)**
  - *Mechanism:* Forbid any corrections on invoices belonging to closed financial years. Force adjustments to be handled entirely outside the system via manual journal vouchers.
  - *Pros:* Protects closed books completely.
  - *Cons:* Extremely rigid; prevents resolving genuine administrative errors.

#### 4. Engineering Recommendation
Adopt **Option 2**. For initial release, mark historical financial years prior to `FY 2026-27` as `is_active=False` / `LOCKED`. Reversals must be posted within the current open period with explicit audit logging of the parent invoice's historical FY.

#### 5. Impact on Reporting & Export Certification
- Excel and CSV exports must continue to filter by transaction `payment_date` for cash-flow reports, while providing invoice `fy_code` metadata to allow auditors to reconcile prior-period corrections.

- **Classification:** **Mandatory before go-live**  
- **Approval Authority:** Head of Finance & Statutory Auditor  
- **Approval Status:** `PENDING QUALIFIED FINANCE / AUDITOR APPROVAL`

---

### BPD-02: Authoritative Data Export Role Permissions

#### 1. Context & Current Source Implementation
In Stage 4C.5, authoritative export endpoints were introduced:
- `POST /api/export/token` generates a single-use, 5-minute cryptographic ticket.
- Endpoint dependency: `user: dict = Depends(require_finance_or_admin)`.
- Sales executives (`role: "sales"`) and viewers (`role: "viewer"`) attempting to generate an export ticket receive **HTTP 403 Forbidden**.
- In the frontend UI ([`export-drawer.tsx`](file:///home/anishb/projects/personal/cash-flow-buddy-101/src/components/collectflow/export-drawer.tsx)), the Export button is hidden or disabled for Sales and Viewer roles.

#### 2. The Core Dilemma: Data Privacy vs. Operational Self-Service
1. **Financial Governance & Privacy:**
   - Full 17-column Excel exports contain company-wide turnover, customer credit terms, executive sales performance, TDS deductions, and outstanding receivables.
   - Permitting general sales representatives to download bulk Excel files creates a serious risk of commercial data exfiltration, customer list theft, and competitor intelligence leakage.
2. **Sales Operational Efficiency:**
   - Sales leads frequently request Excel exports of their own assigned accounts to manage territory collections and follow up on overdue customer invoices.

#### 3. Options Evaluation
- **Option 1: Strict Admin & Finance Only (Current Implementation — Recommended)**
  - *Mechanism:* Only users with `role: "admin"` or `role: "finance"` can issue export tokens and download Excel/CSV files.
  - *Pros:* Maximum data security; zero risk of unauthorized bulk customer data exfiltration.
  - *Cons:* Sales managers must ask Finance to run reports on their behalf.
- **Option 2: Scoped Sales Export (Deferred Enhancement)**
  - *Mechanism:* Allow `role: "sales"` to export, but strictly force `scope = "own_accounts"` and filter exported rows to `executive_id == current_user.id`.
  - *Pros:* Balances privacy with operational self-service.
  - *Cons:* Requires modifying `app/routers/export.py` query filters and adds testing complexity.
- **Option 3: Open Export to All Authenticated Users**
  - *Mechanism:* Remove role checks from `/api/export/token`.
  - *Pros:* Unrestricted access.
  - *Cons:* Unacceptable security risk for an enterprise finance system.

#### 4. Engineering Recommendation
Formally approve **Option 1** for Initial Production Release (v1.0). Defer Scoped Sales Export (Option 2) to v1.1 post-launch.

- **Classification:** **Mandatory before go-live**  
- **Approval Authority:** Product Owner & Head of Sales  
- **Approval Status:** `PENDING PRODUCT OWNER & SALES LEAD APPROVAL`

---

### BPD-03: Customer Deactivation Policy with Open Orders or Receivables (ARCH-04 Revised)

#### 1. Context & Current Source Implementation
- **Physical Deletion (`DELETE /api/customers/{id}`):**
  - Strictly blocked if referenced on ANY purchase orders or commercial invoices ([`app/routers/customers.py#L388-L395`](file:///home/anishb/projects/personal/collectflow/app/routers/customers.py#L388-L395)).
- **Soft Deactivation (`PUT /api/customers/{id}` with `is_active=False`):**
  - Currently permitted without any validation against open purchase orders or outstanding receivables ([`app/routers/customers.py#L363-L365`](file:///home/anishb/projects/personal/collectflow/app/routers/customers.py#L363-L365)).

#### 2. The Core Dilemma: Lifecycle Management vs. Workflow Disruption
1. If a customer enters dispute, insolvency, or commercial termination, operations require immediately marking the customer as **Inactive** to block sales reps from creating *new* purchase orders or invoices.
2. However, if the customer still has:
   - **Active Unbilled Purchase Orders:** e.g., ₹2,95,000 in unbilled delivery backlog.
   - **Unpaid Commercial Invoices:** e.g., ₹3,22,000 in outstanding accounts receivable.
3. Automatically cancelling purchase orders without commercial authorization is dangerous and legally problematic. But leaving inactive customers completely unchecked could result in open balances being forgotten.

#### 3. Options Evaluation
- **Option 1: Unrestricted Soft Deactivation (Current Implementation)**
  - *Mechanism:* Allow `is_active=False` at any time. Inactive customers cannot be selected on *new* orders/invoices.
  - *Pros:* Immediate operational freeze on new business.
  - *Cons:* Risk that open orders or unpaid balances are hidden if filters exclude inactive accounts.
- **Option 2: Strict Blocking (Zero Open Orders or Receivables)**
  - *Mechanism:* `PUT /api/customers/{id}` with `is_active=False` returns HTTP 400 if unbilled POs count > 0 or outstanding balance > 0.
  - *Pros:* Forces full operational cleanup before deactivation.
  - *Cons:* Impractical. A disputed customer with an unpaid invoice could never be deactivated to stop new orders!
- **Option 3: Soft Deactivation with Independent PO Action & Exposure Retention (Recommended — ARCH-04 Revised)**
  - *Mechanism:*
    1. Soft deactivation (`is_active=False`) is permitted, immediately blocking creation of *new* purchase orders or invoices for that customer.
    2. **Do NOT automatically cancel unbilled purchase orders.** Cancelling a purchase order is a distinct commercial event requiring a separate, authorized action and audit trail.
    3. Existing commercial invoices remain legally active and binding for collections.
    4. Reports Hub, Customer Exposure views, and AR Aging summaries **continue to display inactive customers who hold open unbilled commitments or outstanding balances** (flagged with an "Inactive Customer" badge) so financial exposure is never obscured.

#### 4. Engineering Recommendation
Adopt **Option 3**. Protect against new commitments while ensuring all historical unbilled orders and unpaid invoices remain active and fully visible until formally resolved or settled.

- **Classification:** **Mandatory before go-live**  
- **Approval Authority:** Product Owner & Head of Finance  
- **Approval Status:** `PENDING PRODUCT OWNER & FINANCE APPROVAL`

---

### BPD-04: Foreign-Currency Settlements & Technical Gate (ARCH-04 Revised)

#### 1. Context & Current Source Implementation
- The database schema supports foreign currencies at the order and invoice level (`purchase_orders.currency`, `commercial_invoices.currency`).
- However, [`payment_transactions`](file:///home/anishb/projects/personal/collectflow/app/models/domain_models.py#L257-L286) tracks settlements **strictly in INR** (`cash_amount`, `tds_amount`, `total_settled`).
- There is currently no mechanism to record transaction-date foreign exchange rates or post realized exchange gain/loss entries.

#### 2. The Core Dilemma: Multi-Currency Invoicing vs. Realized Forex Accounting
If an export invoice is issued for **$10,000 USD** at **₹83.50/USD** (₹8,35,000.00), and payment arrives when the exchange rate is **₹84.20/USD** (₹8,42,000.00):
- There is a **Realized Foreign Exchange Gain of ₹7,000.00**.
- In the current system, attempting to record ₹8,42,000 against an invoice of ₹8,35,000 triggers an **HTTP 400 Overpayment Error**.
- Conversely, if the exchange rate drops to ₹83.00 (₹8,30,000 received), an orphaned ₹5,000 balance remains that cannot be settled.

#### 3. Options Evaluation
- **Option 1: Scope Restriction to INR with Explicit Technical Gate for v1.0 (Recommended)**
  - *Mechanism:* Enforce a strict server-side validation gate in API endpoints and UI drawers:
    ```python
    if item.currency != "INR" or item.exchange_rate != Decimal("1.000000"):
        raise HTTPException(status_code=400, detail="Only domestic INR transactions are supported in CollectFlow v1.0.")
    ```
  - *Pros:* Zero risk of accounting corruption; zero additional engineering effort before go-live.
  - *Cons:* CollectFlow cannot immediately be used for foreign-currency cross-border billing.
- **Option 2: Full Multi-Currency & Realized Forex Accounting Engine (Deferred)**
  - *Mechanism:* Extend `payment_transactions` with `foreign_currency_amount`, `realized_exchange_rate`, and `forex_gain_loss`. Create automated accounting entries for realized forex differences.
  - *Pros:* Complete enterprise multi-currency treasury support.
  - *Cons:* Major scope expansion (estimated 5–7 engineering days); alters accepted Phase 4C financial models.

#### 4. Engineering Recommendation
Adopt **Option 1**. Explicitly scope the Initial Production Release (v1.0) to domestic Indian Rupee (`INR`) operations with an enforceable technical gate. Defer Full Multi-Currency Accounting (Option 2) to the Phase 5 roadmap.

- **Classification:** **Required only before foreign-currency feature is enabled**  
- **Approval Authority:** Head of Finance & Treasury Lead  
- **Approval Status:** `PENDING PRODUCT OWNER & TREASURY APPROVAL`

---

## 3. Formal Sign-Off Table

*The undersigned stakeholders review and approve the policy determinations outlined above:*

| Role | Nominated Stakeholder | Decision Items | Formal Signature | Approval Status |
|---|---|---|---|---|
| **Product Owner** | *Pending Nomination* | BPD-02, BPD-03 | `___________________` | **PENDING** |
| **Head of Finance / Controller** | *Pending Nomination* | BPD-01, BPD-03, BPD-04 | `___________________` | **PENDING** |
| **Statutory Audit Representative** | *Pending Nomination* | BPD-01 | `___________________` | **PENDING** |
| **Head of Sales** | *Pending Nomination* | BPD-02 | `___________________` | **PENDING** |
