#!/usr/bin/env python3
"""
Clean Acme Corp test customer, related test invoices, payments,
and any orphaned records from the database.
"""

import sqlite3
import sys
from pathlib import Path


def cleanup_acme(db_path: str):
    print(f"Cleaning Acme Corp and test entries from: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    cur = conn.cursor()

    # 1. Clean any orphaned invoice items
    cur.execute("DELETE FROM invoice_items WHERE invoice_id NOT IN (SELECT id FROM invoices);")
    orphaned_items = cur.rowcount
    if orphaned_items > 0:
        print(f"Cleaned {orphaned_items} orphaned invoice items.")

    # 2. Find Acme customer IDs
    acme_custs = cur.execute("SELECT id, billing_name FROM customers WHERE LOWER(billing_name) LIKE '%acme%';").fetchall()
    cust_ids = [r[0] for r in acme_custs]
    print(f"Found {len(cust_ids)} Acme customer records: {[r[1] for r in acme_custs]}")

    if cust_ids:
        placeholders = ",".join("?" * len(cust_ids))
        acme_invs = cur.execute(
            f"SELECT id, invoice_number FROM invoices WHERE customer_id IN ({placeholders}) OR LOWER(customer_name) LIKE '%acme%';",
            cust_ids,
        ).fetchall()
    else:
        acme_invs = cur.execute("SELECT id, invoice_number FROM invoices WHERE LOWER(customer_name) LIKE '%acme%';").fetchall()

    inv_ids = [r[0] for r in acme_invs]
    print(f"Found {len(inv_ids)} Acme invoice records: {[r[1] for r in acme_invs]}")

    # 3. Delete payments for Acme invoices
    if inv_ids:
        i_placeholders = ",".join("?" * len(inv_ids))
        cur.execute(f"DELETE FROM payments WHERE invoice_id IN ({i_placeholders});", inv_ids)
        print(f"Deleted {cur.rowcount} payments.")

        cur.execute(f"DELETE FROM invoice_items WHERE invoice_id IN ({i_placeholders});", inv_ids)
        print(f"Deleted {cur.rowcount} invoice items.")

        cur.execute(f"DELETE FROM invoices WHERE id IN ({i_placeholders});", inv_ids)
        print(f"Deleted {cur.rowcount} invoices.")

    # 4. Delete Acme customer
    if cust_ids:
        c_placeholders = ",".join("?" * len(cust_ids))
        cur.execute(f"DELETE FROM customers WHERE id IN ({c_placeholders});", cust_ids)
        print(f"Deleted {cur.rowcount} customers.")

    # 5. Clean test audit logs mentioning Acme
    cur.execute("DELETE FROM audit_logs WHERE LOWER(details) LIKE '%acme%';")
    print(f"Deleted {cur.rowcount} Acme audit log records.")

    conn.commit()

    # 6. Check foreign key integrity
    fk_errors = cur.execute("PRAGMA foreign_key_check;").fetchall()
    if fk_errors:
        print(f"ERROR: Foreign key violations detected: {fk_errors}")
        sys.exit(1)
    print("Foreign key integrity check: PASSED (0 errors).")

    # 7. Vacuum database
    conn.execute("VACUUM;")
    print("Database VACUUM completed.")

    remaining_customers = cur.execute("SELECT COUNT(*) FROM customers;").fetchone()[0]
    remaining_invoices = cur.execute("SELECT COUNT(*) FROM invoices;").fetchone()[0]
    print(f"Final counts -> Customers: {remaining_customers}, Invoices: {remaining_invoices}")
    conn.close()


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent.parent
    default_db = base_dir / "data" / "collectflow.db"
    target_db = sys.argv[1] if len(sys.argv) > 1 else str(default_db)
    cleanup_acme(target_db)
