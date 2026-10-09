#!/usr/bin/env python3
"""
Migrate CollectFlow database to eliminate isolated tables:
1. Updates invoices.executive_id to reference users(id) directly.
2. Recreates invoices table with foreign key constraint to users(id).
3. Drops redundant isolated tables: 'executives', 'executive_targets', and 'company_profile'.
4. Recreates v_invoice_summary view joining users table directly.
5. Verifies foreign key integrity.
"""

import sqlite3
import sys
from pathlib import Path


def migrate(db_path: str):
    print(f"Connecting to: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("PRAGMA foreign_keys = OFF;")
    cur.execute("BEGIN TRANSACTION;")

    # 1. Check if executives table exists
    has_execs = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='executives';").fetchone()

    if has_execs:
        print("Remapping invoices.executive_id from executives.id to users.id...")
        cur.execute("""
            UPDATE invoices
            SET executive_id = (
                SELECT u.id
                FROM users u
                JOIN executives e ON LOWER(e.name) = LOWER(COALESCE(u.full_name, u.username))
                WHERE e.id = invoices.executive_id
            )
            WHERE executive_id IS NOT NULL;
        """)
        print(f"Remapped executive_ids in invoices.")

    # 2. Drop view before dropping table
    cur.execute("DROP VIEW IF EXISTS v_invoice_summary;")

    # 3. Recreate invoices table with REFERENCES users(id)
    cols = [r[1] for r in cur.execute("PRAGMA table_info(invoices);").fetchall()]
    col_names = ", ".join(cols)

    cur.execute("""
        CREATE TABLE invoices_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fy_code TEXT NOT NULL REFERENCES financial_years(code),
            po_date DATE,
            po_number TEXT,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            executive_id INTEGER REFERENCES users(id),
            exclusive_amount REAL DEFAULT 0,
            tax_rate REAL DEFAULT 0.18,
            tax_amount REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            currency TEXT DEFAULT 'INR',
            currency_rate REAL DEFAULT 1.0,
            tax_ref TEXT,
            invoice_number TEXT,
            invoice_date DATE,
            due_date DATE,
            payment_status TEXT DEFAULT 'PENDING',
            remarks TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            order_type TEXT DEFAULT 'PO',
            exchange_rate REAL DEFAULT 1.0,
            original_amount REAL DEFAULT 0.0,
            customer_name TEXT,
            customer_gstin TEXT,
            customer_address TEXT,
            customer_state TEXT
        );
    """)

    cur.execute(f"INSERT INTO invoices_new ({col_names}) SELECT {col_names} FROM invoices;")
    cur.execute("DROP TABLE invoices;")
    cur.execute("ALTER TABLE invoices_new RENAME TO invoices;")
    print("Recreated invoices table with direct foreign key to users(id).")

    # 4. Drop isolated redundant tables
    cur.execute("DROP TABLE IF EXISTS executives;")
    cur.execute("DROP TABLE IF EXISTS executive_targets;")
    cur.execute("DROP TABLE IF EXISTS company_profile;")
    print("Dropped redundant isolated tables: 'executives', 'executive_targets', 'company_profile'.")

    # 5. Recreate v_invoice_summary view referencing users table
    cur.execute("""
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
    """)
    print("Recreated v_invoice_summary view joining users table.")

    conn.commit()

    # 6. Verify foreign key integrity
    cur.execute("PRAGMA foreign_keys = ON;")
    fk_errors = cur.execute("PRAGMA foreign_key_check;").fetchall()
    if fk_errors:
        print(f"ERROR: Foreign key violations: {fk_errors}")
        sys.exit(1)
    print("Foreign key integrity check: PASSED (0 errors).")

    # 7. Check surviving tables
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;").fetchall()]
    print(f"Surviving active tables ({len(tables)}): {tables}")

    conn.close()
    print("Migration completed successfully.")


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent.parent
    default_db = base_dir / "data" / "collectflow.db"
    target_db = sys.argv[1] if len(sys.argv) > 1 else str(default_db)
    migrate(target_db)
