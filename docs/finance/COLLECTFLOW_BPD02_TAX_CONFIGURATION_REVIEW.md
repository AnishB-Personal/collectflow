# CollectFlow Business Policy Document Review: BPD-02
## Default Tax & Statutory Calculations Governance Policy (Draft Review Package)

**Document Code:** BPD-02-REVIEW-2026.1  
**Target Application:** CollectFlow Financial Core  
**Governance Status:** `AWAITING_FINANCE_APPROVAL`  
**Review Version:** 1.0.0-DRAFT  
**Date Submitted for Review:** 2026-10-20  
**Data Ownership:** Finance Directorate / Tax Operations  
**Approval Authority:** Chief Financial Officer / Head of Taxation (Corporate Finance Directorate)  

> [!IMPORTANT]
> This is a formal governance review package and draft policy specification. It is **NOT** an authorized corporate tax policy.
> Status is strictly **`AWAITING_FINANCE_APPROVAL`**.
> No tax configurations, default GST rates, or TDS calculation rules in production or staging environments may be activated until this document receives independent, verifiable Corporate Finance approval.
> Example tax percentages and test calculations in developer test suites are synthetic illustrations and do NOT constitute corporate statutory advice.

---

### 1. Document Control & Provenance

| Field | Review Specification | Corporate Sign-Off Status |
| :--- | :--- | :--- |
| **Policy Title** | Default Tax & Statutory Calculations Governance Policy | Awaiting Finance Sign-Off |
| **Policy Code** | BPD-02 | Awaiting Finance Sign-Off |
| **Effective Date** | To be determined upon formal Finance authorization | `PENDING_AUTHORIZATION` |
| **Review Cadence** | Semi-annual or upon tax law amendments | `PENDING_AUTHORIZATION` |
| **Corporate Approver** | *Unassigned — To be signed by Corporate Finance Directorate* | `NOT_APPROVED` |
| **Corporate Approval Ref** | *Unassigned — Pending Finance Directorate Order Number* | `NOT_APPROVED` |
| **Approval Signature/Hash**| *Unassigned — Cryptographic verification token pending* | `NOT_APPROVED` |

---

### 2. Tax Computation & Precision Policies

#### 2.1 Arithmetic Engine & Rounding Policy
- **Data Type:** All monetary and tax computations must be conducted using arbitrary-precision `Decimal` arithmetic. IEEE 754 floating-point numbers (`float`) are strictly prohibited in calculation paths to prevent drift.
- **Rounding Mode:** Standard currency rounding must use `ROUND_HALF_UP` (half-way values rounded away from zero).
- **Line-Item Rounding:**
  - Tax calculation must be performed on each invoice line item individually.
  - Line exclusive amount = `quantize(quantity * unit_price, '0.01', ROUND_HALF_UP)`
  - Line tax amount = `quantize(line_exclusive * tax_rate, '0.01', ROUND_HALF_UP)`
  - Line total amount = `line_exclusive + line_tax`
- **Document Aggregate Consistency:**
  - Invoice exclusive total = sum of line exclusive amounts.
  - Invoice tax total = sum of line tax amounts.
  - Invoice total gross = sum of line total amounts.
  - Invariant: `Invoice Exclusive Total + Invoice Tax Total == Invoice Total Gross` with ₹0.00 delta.

#### 2.2 Goods and Services Tax (GST) Treatment
- **Supply Classification:**
  - Intrastate Supply: Supplier State == Customer Place of Supply -> Split equally into CGST and SGST (`cgst_amount == sgst_amount == tax_amount / 2`, quantized to ₹0.01).
  - Interstate Supply: Supplier State != Customer Place of Supply -> Full tax allocated to IGST (`igst_amount == tax_amount`).
- **Zero-Rated / Exempt Supplies:** When tax_type is `EXEMPT`, tax rate is `0.0000` and tax amount is ₹0.00.
- **Default Application:** New orders/invoices default to the active GST rate marked `is_default=true`, unless an explicit permitted rate is selected.

#### 2.3 Tax Deducted at Source (TDS) Applicability
- **Deduction Principle:** TDS is recognized at the point of customer settlement as a credit towards the gross invoiced receivable.
- **Settlement Invariant:**
  - `Total Settled = Cash Received + TDS Recognized`
  - `Outstanding Balance = Invoiced Total - Net Settled`
- **Allowable TDS Percentage:** TDS deducted must correspond to an active statutory section master rate (e.g. 2% under Section 194C, 10% under Section 194J), or an authorized lower-deduction certificate.
- **Reversal Integrity:** Payment corrections or reversals must emit signed negative transactions (`cash_amount < 0`, `tds_amount <= 0`, `total_settled < 0`), restoring the invoice outstanding balance with exact ₹0.00 monetary delta.

---

### 3. Change Management & Governance Controls

1. **Production Mutation Boundary:** Tax policy parameters, statutory rates, and default tax configurations are locked against arbitrary administrative modifications. Direct REST mutation in production/staging is fail-closed BLOCKED.
2. **Audit Logging:** All tax configuration creation, deactivation, and mutation attempts must be logged to the security audit trail.
3. **Statutory Non-Retroactivity:** Any future changes to tax configurations apply strictly prospectively. Issued invoices and historical receipts remain immutable.

---

### 4. Corporate Finance Sign-Off Block (Pending)

```text
================================================================================
CORPORATE FINANCE DIRECTORATE APPROVAL NOTICE
================================================================================
Document Status:             AWAITING_FINANCE_APPROVAL
Finance Approver Name:       [ PENDING CORPORATE ASSIGNMENT ]
Designation:                 [ PENDING CORPORATE ASSIGNMENT ]
Finance Directorate Order:   [ PENDING ORDER NUMBER ]
Authorization Date:          [ PENDING DATE ]
Approval Cryptographic Hash: [ PENDING EXTERNAL SIGNATURE ]
Production Activation Permitted: NO (BLOCKED)
================================================================================
```
