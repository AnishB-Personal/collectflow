# CollectFlow — Financial & Business Rules Reference

The following business and financial rules are strictly implemented in the CollectFlow backend and SQL views. The replacement frontend must understand and respect these mechanics.

---

## 1. Monetary Precision & Rounding Rules

1. **Standard Currency**: Base currency is Indian Rupee (INR, ₹).
2. **Decimal Precision**: All currency amounts are represented and calculated to exactly **two decimal places** (paise) using the `round_currency(val)` helper:
   ```python
   def round_currency(val: float) -> float:
       return round(val, 2)
   ```
3. **Foreign Currencies & Exchange Rates**:
   - Supported: `INR`, `USD`, `EUR`, `GBP`, `AED`.
   - If deal currency is not INR, exchange rate must be `> 0`.
   - `exclusive_amount (INR) = round(original_amount * exchange_rate, 2)`.
   - Reporting, aging, and dashboard rollups are executed **strictly in converted INR**.

---

## 2. Line Item & Invoice Header Total Reconciliation

1. **Header-to-Line Relational Integrity**:
   - `Exclusive Amount = SUM(line_item.quantity * line_item.unit_price)`
   - `Tax Amount = SUM(line_item.tax_amount)`
   - `Total Amount = Exclusive Amount + Tax Amount`
2. **Fallback Synthetic Item Rule**:
   - If an order is created without explicit line items, the backend automatically generates a single fallback line item in `invoice_items` matching the exact header subtotal, tax rate, and tax amount.
   - Synthetic zero-value line items are prohibited when the header has a non-zero balance.

---

## 3. Commercial Identifier Uniqueness & Normalization

1. **Purchase Order (PO) Uniqueness**:
   - Normalized at the database level using `LOWER(TRIM(po_number))`.
   - Generic placeholder exceptions allowed concurrently: `'No PO'`, `'Recurring / Direct Invoice'`.
   - Mixed-case or whitespace-padded duplicates (e.g. `"PO-123"` vs `" po-123 "`) are rejected by database unique index `idx_invoices_unique_po`.
2. **Tax Invoice Number Uniqueness**:
   - Normalized at the database level using `LOWER(TRIM(invoice_number))`.
   - Blank or null values allowed for unbilled POs.
   - Rejects duplicate assignments system-wide across all fiscal years via `idx_invoices_unique_inv`.

---

## 4. Indian Statutory Fiscal Calendar & Boundary Validation

1. **Fiscal Year Format**: Standard Indian fiscal year format runs from April 1 to March 31 (e.g. `FY 2026-27` begins `2026-04-01` and ends `2027-03-31`).
2. **Date Boundary Enforcement**:
   - PO dates and Invoice dates must strictly fall within the calendar bounds of the assigned `fy_code`.
   - Submitting an invoice date of `2027-04-05` against `FY 2026-27` is rejected with `HTTP 400`.

---

## 5. Tax Deducted at Source (TDS Withholding)

1. **Nature of TDS in CollectFlow**:
   - In Indian taxation, client organizations withhold income tax (TDS) prior to disbursing payment.
   - TDS is **not** a discount or revenue reduction; it represents legitimate statutory settlement credit.
2. **Settlement Formula**:
   ```
   Total Settlement = Net Cash Received + TDS Amount
   ```
3. **Outstanding Balance Formula** (computed in view `v_invoice_summary`):
   ```sql
   total_paid = total_cash_paid + total_tds_deducted;
   outstanding_balance = CASE
       WHEN payment_status = 'CANCELLED' THEN 0.0
       ELSE MAX(0.0, total_amount - total_paid)
   END;
   ```
4. **Overpayment Boundary**:
   - `Net Cash Received + TDS Amount` cannot exceed the remaining `outstanding_balance` of the target invoice.

---

## 6. Commercial Immutability & Invoice Cancellation

1. **Immutability of Issued Invoices**:
   - Once an order has an `invoice_number` assigned, its commercial identity is frozen.
   - Prohibited updates: Modifying customer ID, altering financial totals, changing tax rates, editing PO numbers, or physically deleting the invoice row.
   - Permitted updates: Internal remarks, payment due date extensions, assigned sales executive, and line item description refinements.
2. **Cancellation Eligibility**:
   - Invoices with active payments **cannot be cancelled**. All payments must be deleted/reversed first.
   - Physical deletion is permanently disabled for issued invoices. They must go through the formal cancellation workflow (`POST /api/invoices/{id}/cancel`).
3. **Cancellation Financial Impact**:
   - Sets `payment_status = 'CANCELLED'`.
   - Forces `outstanding_balance = 0.0` in `v_invoice_summary`.
   - Excludes invoice from active gross invoiced revenue, pending receivables, and sales target leaderboards.
   - Preserves line items, customer snapshots, and audit log history.

---

## 7. Customer Historical Snapshots

1. **Snapshot Columns on `invoices`**:
   - `customer_name`, `customer_gstin`, `customer_address`, `customer_state`.
2. **Purpose**:
   - When an invoice is created or converted, customer legal identity fields are snapshotted into the invoice row.
   - If the customer later updates their billing address or GSTIN in the customer directory, existing historical invoices retain the legal details that were active at the time of issuance.

---

## 8. Sales Quota Breakdown & Attribution

1. **Attribution**:
   - Orders are attributed to sales executives based on `invoices.executive_id`.
   - Target attainment percentage is computed across active orders in the selected fiscal year:
     ```python
     achievement_pct = round((fy_sales / annual_target) * 100, 1)
     ```
2. **Auto-Breakdown Formula**:
   - Half-Yearly Quota: 50% of Annual Target.
   - Quarterly Quota: 25% of Annual Target.
   - Monthly Quota: 8.33% of Annual Target.
