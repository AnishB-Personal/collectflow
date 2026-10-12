"""
CollectFlow Sandbox Database Initializer & Seed Script

Initializes a strictly isolated SQLite database for local Stage 4B integration testing.
Enforces the mandatory sandbox isolation gate:
- Must define COLLECTFLOW_ENV=integration or sandbox
- Must specify COLLECTFLOW_DB_PATH containing 'sandbox', 'integration', or 'test'
- Refuses to run against production database files
- Seeds synthetic users with disposable credentials
"""

import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import create_connection, execute_write, get_db_path
from app.routers.auth import hash_password

SYNTHETIC_USERS = [
    ("sandbox_admin", "AdminPass123!", "admin", "Sandbox Administrator"),
    ("sandbox_finance", "FinancePass123!", "finance", "Sandbox Finance Specialist"),
    ("sandbox_sales", "SalesPass123!", "sales", "Sandbox Sales Executive"),
    ("sandbox_viewer", "ViewerPass123!", "viewer", "Sandbox Auditor Viewer"),
]

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS financial_years (
    code TEXT PRIMARY KEY,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    is_active BOOLEAN DEFAULT 1
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT DEFAULT 'admin',
    full_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    billing_name TEXT UNIQUE NOT NULL,
    billing_address TEXT,
    city TEXT,
    state TEXT,
    country TEXT DEFAULT 'IN',
    pin_code TEXT,
    gst_number TEXT,
    phone_1 TEXT,
    phone_2 TEXT,
    contact_person TEXT,
    designation TEXT,
    email TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fy_code TEXT NOT NULL REFERENCES financial_years(code),
    order_type TEXT DEFAULT 'PO',
    po_date DATE,
    po_number TEXT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    executive_id INTEGER REFERENCES users(id),
    currency TEXT DEFAULT 'INR',
    exchange_rate REAL DEFAULT 1.0,
    original_amount REAL DEFAULT 0.0,
    exclusive_amount REAL DEFAULT 0,
    tax_rate REAL DEFAULT 0.18,
    tax_amount REAL DEFAULT 0,
    total_amount REAL DEFAULT 0,
    currency_rate REAL DEFAULT 1.0,
    tax_ref TEXT,
    invoice_number TEXT,
    invoice_date DATE,
    due_date DATE,
    payment_status TEXT DEFAULT 'PENDING',
    remarks TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    customer_name TEXT,
    customer_gstin TEXT,
    customer_address TEXT,
    customer_state TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    payment_amount REAL NOT NULL,
    tds_amount REAL DEFAULT 0.0,
    tds_section TEXT,
    payment_date DATE NOT NULL,
    remittance_number TEXT,
    payment_mode TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    idempotency_key TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS export_tickets (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier TEXT NOT NULL,
    attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    success INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    quantity REAL DEFAULT 1,
    unit_price REAL DEFAULT 0,
    tax_rate REAL DEFAULT 0.18,
    tax_amount REAL DEFAULT 0,
    total_amount REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sales_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fy_code TEXT NOT NULL REFERENCES financial_years(code) ON DELETE CASCADE,
    monthly_target REAL DEFAULT 0,
    quarterly_target REAL DEFAULT 0,
    half_yearly_target REAL DEFAULT 0,
    yearly_target REAL DEFAULT 0,
    monthly_bottom_line REAL DEFAULT 0,
    yearly_bottom_line REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, fy_code)
);
CREATE TABLE IF NOT EXISTS tax_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rate REAL NOT NULL,
    tax_type TEXT DEFAULT 'GST',
    is_default BOOLEAN DEFAULT 0,
    is_active BOOLEAN DEFAULT 1
);
CREATE TABLE IF NOT EXISTS tds_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_code TEXT NOT NULL,
    description TEXT,
    rate REAL NOT NULL,
    is_active BOOLEAN DEFAULT 1
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    details TEXT,
    ip_address TEXT,
    request_id TEXT
);
DROP VIEW IF EXISTS v_invoice_summary;
CREATE VIEW v_invoice_summary AS
SELECT
    i.id,
    i.customer_id,
    i.executive_id,
    i.fy_code,
    i.order_type,
    i.po_date,
    i.po_number,
    COALESCE(i.customer_name, c.billing_name, 'Unknown Customer') AS customer_name,
    COALESCE(i.customer_gstin, c.gst_number) AS customer_gstin,
    COALESCE(i.customer_address, c.billing_address) AS customer_address,
    COALESCE(i.customer_state, c.state) AS customer_state,
    COALESCE(u.full_name, u.username) AS executive_name,
    i.currency,
    i.exchange_rate,
    i.original_amount,
    i.exclusive_amount,
    i.tax_amount,
    i.total_amount,
    i.invoice_number,
    i.invoice_date,
    i.due_date,
    ROUND(COALESCE(SUM(p.payment_amount), 0), 2) AS total_cash_paid,
    ROUND(COALESCE(SUM(p.tds_amount), 0), 2) AS total_tds_deducted,
    ROUND((COALESCE(SUM(p.payment_amount), 0) + COALESCE(SUM(p.tds_amount), 0)), 2) AS total_paid,
    CASE
        WHEN i.payment_status = 'CANCELLED' THEN 0.0
        ELSE ROUND((i.total_amount - (COALESCE(SUM(p.payment_amount), 0) + COALESCE(SUM(p.tds_amount), 0))), 2)
    END AS outstanding_balance,
    CASE
        WHEN i.payment_status = 'CANCELLED' THEN 'CANCELLED'
        WHEN (i.invoice_number IS NULL OR trim(i.invoice_number) = '') AND (i.invoice_date IS NULL OR trim(i.invoice_date) = '') THEN 'UNBILLED'
        WHEN ROUND((i.total_amount - (COALESCE(SUM(p.payment_amount), 0) + COALESCE(SUM(p.tds_amount), 0))), 2) <= 0 THEN 'PAID'
        WHEN (COALESCE(SUM(p.payment_amount), 0) + COALESCE(SUM(p.tds_amount), 0)) > 0 THEN 'PARTIAL'
        WHEN i.due_date IS NOT NULL AND DATE('now') > i.due_date THEN 'OVERDUE'
        ELSE 'PENDING'
    END AS computed_status,
    CASE
        WHEN i.payment_status = 'CANCELLED' THEN 0
        WHEN (i.invoice_number IS NULL OR trim(i.invoice_number) = '') AND (i.invoice_date IS NULL OR trim(i.invoice_date) = '') THEN 0
        WHEN ROUND((i.total_amount - (COALESCE(SUM(p.payment_amount), 0) + COALESCE(SUM(p.tds_amount), 0))), 2) <= 0 THEN 0
        WHEN i.due_date IS NULL THEN 0
        WHEN DATE('now') > i.due_date THEN CAST(JULIANDAY('now') - JULIANDAY(i.due_date) AS INTEGER)
        ELSE 0
    END AS days_overdue
FROM invoices i
LEFT JOIN customers c ON i.customer_id = c.id
LEFT JOIN users u ON i.executive_id = u.id
LEFT JOIN payments p ON i.id = p.invoice_id
GROUP BY i.id;
"""


def init_sandbox_db():
    db_path = get_db_path()
    print(f"[*] Initializing sandbox database at: {db_path}")

    conn = create_connection()
    try:
        conn.executescript(SCHEMA_DDL)

        # Clear existing data for idempotency
        for tbl in [
            "sessions", "export_tickets", "login_attempts", "payments",
            "invoice_items", "invoices", "sales_targets", "customers",
            "users", "financial_years", "tax_rates", "tds_rates", "audit_logs"
        ]:
            conn.execute(f"DELETE FROM {tbl};")

        # Seed Financial Years
        conn.execute("INSERT INTO financial_years (code, start_date, end_date, is_active) VALUES ('FY 2026-27', '2026-04-01', '2027-03-31', 1);")
        conn.execute("INSERT INTO financial_years (code, start_date, end_date, is_active) VALUES ('FY 2025-26', '2025-04-01', '2026-03-31', 1);")

        # Seed Reference Taxes
        conn.execute("INSERT INTO tax_rates (name, rate, tax_type, is_default, is_active) VALUES ('GST 18%', 0.18, 'GST', 1, 1);")
        conn.execute("INSERT INTO tds_rates (section_code, description, rate, is_active) VALUES ('194C', 'Payment to Contractors', 0.02, 1);")
        conn.execute("INSERT INTO tds_rates (section_code, description, rate, is_active) VALUES ('194J', 'Professional/Technical Fees', 0.10, 1);")

        # Seed Synthetic Users
        for uname, pwd, role, fname in SYNTHETIC_USERS:
            h, salt = hash_password(pwd)
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, role, full_name) VALUES (?, ?, ?, ?, ?)",
                (uname, h, salt, role, fname),
            )
            print(f"    [+] Seeded synthetic user: {uname} (role: {role})")

        conn.commit()
        print("[✓] Sandbox database successfully initialized with synthetic fixtures.")
    finally:
        conn.close()


if __name__ == "__main__":
    init_sandbox_db()
