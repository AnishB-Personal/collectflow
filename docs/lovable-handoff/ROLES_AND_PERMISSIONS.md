# CollectFlow — Roles & Permissions Matrix

This document provides the authoritative permission matrix enforced by backend FastAPI dependencies in CollectFlow.

---

## 1. User Roles Definition

1. **`admin` (Administrator)**:
   - Full unrestricted access across the entire application.
   - Only role authorized to manage users, reset team passwords, manage system settings, and purge operational tables.
2. **`finance` (Finance & Billing Operations)**:
   - Operational invoicing authority: Convert draft POs to tax invoices, record and delete payments, cancel invoices, manage customers, and download financial exports.
   - Read-only on user management and fiscal masters.
3. **`sales` (Account Executive / Sales Representative)**:
   - Pipeline authority: Create new customers, create draft Purchase Orders and subscription deals, edit unbilled order metadata, and view personal/team sales leaderboards.
   - Cannot issue tax invoices, record/delete payments, or cancel commercial invoices.
4. **`viewer` (Read-Only Auditor)**:
   - View dashboards, reports, invoice tables, customer directories, and payment history.
   - Cannot mutate any operational or administrative records.

---

## 2. Granular Permissions Matrix

| Functional Capability | `admin` | `finance` | `sales` | `viewer` | Backend Dependency Enforcing Role |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **Authentication & Profile** | | | | | |
| Log in / Log out | ✅ | ✅ | ✅ | ✅ | Public / `get_current_user` |
| View own profile (`GET /api/auth/me`) | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| Change own password | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| **Dashboard & Analytics** | | | | | |
| View KPIs & charts (`GET /api/dashboard/summary`) | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| View sales quotas & leaderboard | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| **Customers Management** | | | | | |
| List & search customers (`GET /api/customers`) | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| View customer details & orders (`GET /api/customers/{id}`) | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| Create customer (`POST /api/customers`) | ✅ | ✅ | ✅ | ❌ | `require_roles('admin', 'finance', 'sales')` |
| Edit customer (`PUT /api/customers/{id}`) | ✅ | ✅ | ❌ | ❌ | `require_finance_or_admin` |
| Delete customer (`DELETE /api/customers/{id}`) | ✅ | ❌ | ❌ | ❌ | `require_admin` |
| **Orders & Invoicing** | | | | | |
| List & filter invoices (`GET /api/invoices`) | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| View single invoice details (`GET /api/invoices/{id}`) | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| Create Order / PO (`POST /api/invoices`) | ✅ | ✅ | ✅ | ❌ | `require_roles('admin', 'finance', 'sales')` |
| Convert PO to Tax Invoice (`POST /api/invoices/{id}/convert-to-invoice`) | ✅ | ✅ | ❌ | ❌ | `require_finance_or_admin` |
| Edit order metadata (`PUT /api/invoices/{id}`) | ✅ | ✅ | ❌ | ❌ | `require_finance_or_admin` |
| Cancel order / invoice (`POST /api/invoices/{id}/cancel`) | ✅ | ✅ | ❌ | ❌ | `require_finance_or_admin` |
| Delete unbilled PO (`DELETE /api/invoices/{id}`) | ✅ | ✅ | ❌ | ❌ | `require_finance_or_admin` |
| **Payments & Settlements** | | | | | |
| View recent payments (`GET /api/payments/recent`) | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| Record payment (`POST /api/payments`) | ✅ | ✅ | ❌ | ❌ | `require_finance_or_admin` |
| Delete / revert payment (`DELETE /api/payments/{id}`) | ✅ | ✅ | ❌ | ❌ | `require_finance_or_admin` |
| **Data Export** | | | | | |
| Issue export ticket (`POST /api/export/token`) | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| Download Excel audit report (`GET /api/export/excel`) | ✅ | ✅ | ✅ | ✅ | Valid export ticket |
| **User Administration & Quotas** | | | | | |
| List team accounts (`GET /api/users`) | ✅ | ❌ | ❌ | ❌ | `require_admin` |
| Create team user (`POST /api/users`) | ✅ | ❌ | ❌ | ❌ | `require_admin` |
| Edit user details / role (`PUT /api/users/{id}`) | ✅ | ❌ | ❌ | ❌ | `require_admin` |
| Delete team user (`DELETE /api/users/{id}`) | ✅ | ❌ | ❌ | ❌ | `require_admin` |
| Reset user password (`POST /api/users/{id}/reset-password`) | ✅ | ❌ | ❌ | ❌ | `require_admin` |
| View user sales targets (`GET /api/users/{id}/targets`) | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| Configure sales targets (`POST /api/users/{id}/targets`) | ✅ | ❌ | ❌ | ❌ | `require_admin` |
| **System Settings & Reference Data** | | | | | |
| List financial years & tax slabs (`GET /api/meta`, `GET /api/settings/*`) | ✅ | ✅ | ✅ | ✅ | `get_current_user` |
| Create / delete financial year | ✅ | ❌ | ❌ | ❌ | `require_admin` |
| Seed standard fiscal years | ✅ | ❌ | ❌ | ❌ | `require_admin` |
| Create / delete GST slab or TDS section | ✅ | ❌ | ❌ | ❌ | `require_admin` |

---

## 3. UI/UX Implementation Guidelines for Lovable
1. **Dynamic Action Visibility**: The new UI should inspect `user.role` from `GET /api/auth/me` and conditionally hide or disable non-permitted buttons (e.g. hide "Convert to Invoice", "Log Payment", and "Delete" for `sales` and `viewer` users).
2. **Backend Defense-in-Depth**: Never rely solely on UI button hiding. The backend strictly enforces these dependencies and responds with `HTTP 403 Forbidden` if an unauthorized request is submitted.
3. **Session Revocation Awareness**: When an administrator updates a user's role via `PUT /api/users/{id}/role`, the backend immediately revokes that user's active session. The frontend should handle `HTTP 401 Unauthorized` by gracefully routing to the login screen with a clear notification.
