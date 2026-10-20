# CollectFlow Business Policy Document Review: BPD-01
## Statutory Reference Masters Governance Policy (Draft Review Package)

**Document Code:** BPD-01-REVIEW-2026.1  
**Target Application:** CollectFlow Financial Core  
**Governance Status:** `AWAITING_FINANCE_APPROVAL`  
**Review Version:** 1.0.0-DRAFT  
**Date Submitted for Review:** 2026-10-20  
**Data Ownership:** Finance Directorate / Corporate Accounting  
**Approval Authority:** Chief Financial Officer / Head of Financial Operations (Corporate Finance Directorate)  

> [!IMPORTANT]
> This is a formal governance review package and draft policy specification. It is **NOT** an authorized corporate policy.
> Status is strictly **`AWAITING_FINANCE_APPROVAL`**.
> No statutory reference tables (Financial Years, GST Rates, TDS Sections) in production or staging environments may be populated or mutated until this document receives independent, verifiable Corporate Finance approval.
> Developer tests and synthetic sandbox fixtures must never be construed as corporate authorization.

---

### 1. Document Control & Provenance

| Field | Review Specification | Corporate Sign-Off Status |
| :--- | :--- | :--- |
| **Policy Title** | Statutory Reference Masters Governance Policy | Awaiting Finance Sign-Off |
| **Policy Code** | BPD-01 | Awaiting Finance Sign-Off |
| **Effective Date** | To be determined upon formal Finance authorization | `PENDING_AUTHORIZATION` |
| **Review Cadence** | Annual or upon statutory circular issuance | `PENDING_AUTHORIZATION` |
| **Corporate Approver** | *Unassigned — To be signed by Corporate Finance Directorate* | `NOT_APPROVED` |
| **Corporate Approval Ref** | *Unassigned — Pending Finance Directorate Order Number* | `NOT_APPROVED` |
| **Approval Signature/Hash**| *Unassigned — Cryptographic verification token pending* | `NOT_APPROVED` |

---

### 2. Statutory Reference Masters Specifications

#### 2.1 Indian Financial Years (`financial_years`)
- **Boundary Convention:** Must strictly follow the Indian statutory financial year definition (starts April 1, ends March 31 of the succeeding Gregorian calendar year).
- **Format Code:** Canonical naming format `FY YYYY-YY` (e.g., `FY 2026-27`).
- **Overlapping Prohibition:** Financial years must never have overlapping date intervals. Every day belongs to exactly one active financial year.
- **Reference Integrity:** Deletion of any financial year referenced by commercial invoices (`CommercialInvoice`), purchase orders (`PurchaseOrder`), or sales targets (`SalesTarget`) is strictly prohibited.
- **Proposed Baseline Definition:**
  - Code: `FY 2026-27`
  - Start Date: `2026-04-01`
  - End Date: `2027-03-31`
  - Active: `true`

#### 2.2 Goods and Services Tax Master (`gst_rate_master`)
- **Statutory Applicability:** Rates must reflect applicable GST Council notifications under the Central Goods and Services Tax Act, 2017.
- **Split Invariant:** For intrastate supplies, the statutory rate must equal Central GST (`cgst_rate`) plus State GST (`sgst_rate`), where `cgst_rate == sgst_rate == rate / 2`. For interstate supplies, Integrated GST (`igst_rate`) must equal `rate`.
- **Decimal Precision:** All rates must be stored with 4 decimal places (`0.0001` precision).
- **Default Rate Control:** Exactly one active rate may be designated as `is_default` at any given time, serialized under concurrency via PostgreSQL advisory locking.
- **Historical Invariance:** GST rates once used by an issued commercial invoice are immutable. The invoice item stores its own historical snapshot (`tax_rate`, `tax_amount`, `exclusive_amount`), unaffected by future master rate additions.
- **Proposed Baseline Reference Slabs (Draft Review):**
  - Standard Goods/Services: `0.1800` (18.00% — CGST 9.00%, SGST 9.00%, IGST 18.00%)
  - Concessional Services: `0.0500` (5.00% — CGST 2.50%, SGST 2.50%, IGST 5.00%)
  - Exempt Supplies: `0.0000` (0.00%)

#### 2.3 Tax Deducted at Source Master (`tds_section_master`)
- **Statutory Applicability:** Sections and base rates must reflect applicable provisions under Chapter XVII-B of the Income-tax Act, 1961.
- **Code Convention:** Section codes must match statutory sections (e.g., `194C`, `194J`, `194J(b)`).
- **Precision:** Stored as decimal statutory rate (`0.0001` precision).
- **Settlement Invariance:** TDS deductions recorded on payment transactions are historical financial ledger records. Changing reference master rates cannot rewrite previously recorded TDS deductions.
- **Proposed Baseline Reference Sections (Draft Review):**
  - Section `194C`: Payments to Contractors/Sub-contractors — Default Rate `0.0200` (2.00%)
  - Section `194J`: Fees for Professional/Technical Services — Default Rate `0.1000` (10.00%)
  - Section `194J(b)`: Royalty/Technical Services (Specified) — Default Rate `0.0200` (2.00%)

---

### 3. Change Management & Governance Controls

1. **Production Mutation Boundary:** Direct CRUD mutations of statutory reference data via REST APIs or administrative CLI tools are strictly blocked in production and staging environments until independent Corporate Finance Directorate authorization is confirmed.
2. **Audit Logging:** Every attempted or approved statutory reference operation must emit a security audit event (`FY_CREATED`, `FY_DELETED`, `GST_RATE_CREATED`, `TDS_SECTION_CREATED`, or `STATUTORY_MUTATION_REJECTED`) containing the timestamp, user ID, username, IP/channel, and full payload details.
3. **Data Retention:** Reference data records must be retained indefinitely for audit and tax assessment compliance (minimum 8 years under statutory accounting standards).

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
