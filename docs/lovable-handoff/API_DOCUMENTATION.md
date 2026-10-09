# CollectFlow — API Contract Documentation & Reference

This document provides representative, sanitized request and response contracts for all core CollectFlow API routes.

---

## 1. Authentication Endpoints

### 1.1 Login (`POST /api/auth/login`)
- **Headers**: `Content-Type: application/json`
- **Request Body**:
  ```json
  {
    "username": "admin",
    "password": "SecurePassword123"
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "success": true,
    "token": "7c5bf3b054f9b8c70bfebbf7c263c9b5dcc87c4fa62bbc1fc9cd4cb6f3c334b2",
    "user": {
      "id": 1,
      "username": "admin",
      "role": "admin",
      "full_name": "System Administrator"
    }
  }
  ```
- **Errors**: `400 Bad Request` (invalid credentials), `429 Too Many Requests` (rate limited).

### 1.2 Get Current User (`GET /api/auth/me`)
- **Headers**: `Authorization: Bearer <token>`
- **Response (`200 OK`)**:
  ```json
  {
    "id": 1,
    "username": "admin",
    "role": "admin",
    "full_name": "System Administrator",
    "expires_at": "2026-10-15 11:14:49"
  }
  ```

---

## 2. Metadata & Dashboard Endpoints

### 2.1 Metadata (`GET /api/meta`)
- **Headers**: `Authorization: Bearer <token>`
- **Response (`200 OK`)**:
  ```json
  {
    "financial_years": ["FY 2026-27", "FY 2025-26", "FY 2024-25"],
    "executives": [
      { "id": 5, "name": "Anshar" },
      { "id": 3, "name": "Siliya Mol PS" },
      { "id": 4, "name": "Syama.S" }
    ],
    "customers": [
      { "id": 1, "billing_name": "Acme Cloud Solutions Pvt Ltd" }
    ],
    "tax_rates": [
      { "id": 1, "name": "GST 18% (Standard IT & Services)", "rate": 0.18, "tax_type": "GST", "is_default": 1 },
      { "id": 4, "name": "GST 5% (Transport / Basic Goods)", "rate": 0.05, "tax_type": "GST", "is_default": 0 }
    ],
    "tds_rates": [
      { "id": 1, "section_code": "194J (2%)", "description": "Technical Services", "rate": 0.02 },
      { "id": 2, "section_code": "194J (10%)", "description": "Professional Fees", "rate": 0.1 }
    ]
  }
  ```

### 2.2 Dashboard Summary (`GET /api/dashboard/summary`)
- **Headers**: `Authorization: Bearer <token>`
- **Response (`200 OK`)**:
  ```json
  {
    "kpis": {
      "total_invoices": 120,
      "total_invoiced": 14500000.0,
      "total_cash_collected": 11200000.0,
      "total_tds_recognized": 280000.0,
      "total_settled": 11480000.0,
      "total_outstanding": 3020000.0,
      "current_fy_outstanding": 1850000.0,
      "current_fy_invoiced": 8500000.0,
      "current_fy_collected": 6650000.0,
      "current_fy_code": "FY 2026-27",
      "overall_collection_rate": 79.2
    },
    "yoy": [
      {
        "fy_code": "FY 2025-26",
        "total_orders": 75,
        "exclusive_value": 5084745.76,
        "invoiced_gross": 6000000.0,
        "cash_collected": 5800000.0,
        "tds_deducted": 120000.0,
        "collections": 5920000.0,
        "balance_outstanding": 80000.0,
        "collection_rate": 98.7
      }
    ],
    "aging": [
      { "bucket": "Current / Not Due", "amount": 1200000.0 },
      { "bucket": "1-30 Days Overdue", "amount": 950000.0 },
      { "bucket": "31-60 Days Overdue", "amount": 520000.0 },
      { "bucket": "61-90 Days Overdue", "amount": 250000.0 },
      { "bucket": "90+ Days Overdue", "amount": 100000.0 }
    ],
    "executives": [
      {
        "user_id": 3,
        "id": 3,
        "name": "Siliya Mol PS",
        "fy26_27_orders": 24,
        "fy26_27_sales": 3600000.0,
        "fy26_27_outstanding": 450000.0,
        "all_time_sales": 6500000.0,
        "yearly_target": 4000000.0,
        "target_achievement_pct": 90.0
      }
    ]
  }
  ```

---

## 3. Orders & Invoices Endpoints

### 3.1 Create Order / Invoice (`POST /api/invoices`)
- **Headers**: `Authorization: Bearer <token>`, `Content-Type: application/json`
- **Request Body**:
  ```json
  {
    "fy_code": "FY 2026-27",
    "order_type": "PO",
    "customer_id": 1,
    "po_number": "PO-CORP-2026-099",
    "po_date": "2026-04-10",
    "executive_id": 3,
    "currency": "INR",
    "exchange_rate": 1.0,
    "items": [
      {
        "description": "Enterprise Cloud Architecture Consulting",
        "quantity": 100.0,
        "unit_price": 4500.0,
        "tax_rate": 0.18
      }
    ],
    "invoice_number": "INV-2026-042",
    "invoice_date": "2026-04-12",
    "due_date": "2026-05-12",
    "remarks": "Net 30 commercial terms"
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "success": true,
    "invoice_id": 142,
    "message": "Record created successfully"
  }
  ```

### 3.2 List Invoices (`GET /api/invoices`)
- **Query Parameters**:
  - `page`: `1` (integer)
  - `limit`: `25` (integer)
  - `fy`: `"FY 2026-27"` (optional)
  - `status`: `"PENDING"` (optional)
  - `search`: `"Acme"` (optional)
- **Response (`200 OK`)**:
  ```json
  {
    "page": 1,
    "limit": 25,
    "total": 1,
    "total_pages": 1,
    "data": [
      {
        "id": 142,
        "fy_code": "FY 2026-27",
        "order_type": "PO",
        "po_number": "PO-CORP-2026-099",
        "invoice_number": "INV-2026-042",
        "customer_name": "Acme Cloud Solutions Pvt Ltd",
        "customer_gstin": "29AAAAA0000A1Z5",
        "exclusive_amount": 450000.0,
        "tax_amount": 81000.0,
        "total_amount": 531000.0,
        "total_cash_paid": 200000.0,
        "total_tds_deducted": 9000.0,
        "total_paid": 209000.0,
        "outstanding_balance": 322000.0,
        "computed_status": "PARTIAL",
        "invoice_date": "2026-04-12",
        "due_date": "2026-05-12",
        "executive_name": "Siliya Mol PS"
      }
    ]
  }
  ```

### 3.3 Convert Unbilled PO (`POST /api/invoices/{id}/convert-to-invoice`)
- **Request Body**:
  ```json
  {
    "invoice_number": "INV-2026-045",
    "invoice_date": "2026-04-15",
    "due_date": "2026-05-15"
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "success": true,
    "message": "Order #142 converted to invoice INV-2026-045"
  }
  ```

---

## 4. Payment Endpoints

### 4.1 Record Payment (`POST /api/payments`)
- **Request Body**:
  ```json
  {
    "invoice_id": 142,
    "payment_amount": 200000.0,
    "tds_amount": 9000.0,
    "tds_section": "194J (2%)",
    "payment_date": "2026-04-20",
    "payment_mode": "NEFT",
    "remittance_number": "UTR-ICICI-9876543210"
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "success": true,
    "payment_id": 85,
    "invoice_summary": {
      "total_amount": 531000.0,
      "total_paid": 209000.0,
      "total_cash_paid": 200000.0,
      "total_tds_deducted": 9000.0,
      "outstanding_balance": 322000.0,
      "computed_status": "PARTIAL"
    },
    "message": "Payment recorded successfully"
  }
  ```

### 4.2 Delete / Revert Payment (`DELETE /api/payments/{id}`)
- **Response (`200 OK`)**:
  ```json
  {
    "success": true,
    "invoice_summary": {
      "total_amount": 531000.0,
      "total_paid": 0.0,
      "outstanding_balance": 531000.0,
      "computed_status": "PENDING"
    },
    "message": "Payment #85 reversed/deleted successfully"
  }
  ```
