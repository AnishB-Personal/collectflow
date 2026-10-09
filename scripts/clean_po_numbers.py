#!/usr/bin/env python3
"""
Clean recurring billing and product description strings from the po_number field.
Only genuine purchase order numbers are retained in invoices.po_number.
Line item product descriptions and invoice/payment totals are completely preserved.
"""

import sqlite3
import sys
from pathlib import Path

RECURRING_PATTERNS = [
    '%Monthly%',
    '%Annual%',
    '%Azure%',
    '%O365%',
    '%Google Workspace%',
    '%AWS%',
    '%Power BI%',
    '%Exchange online%',
    '%Planner%',
    '%Copilot%',
    '%Mail Confirmation%',
    '%Implementation%',
    '%Charges%',
    '%SSL Certificate%',
    'Win Server perpetual',
    'PO',
    'PO - 0003',
]

EXCLUDE_IDS = [926, 673]  # Genuine POs that happen to contain matching tokens


def clean_po_numbers(db_path: str):
    print(f"Connecting to database: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    where_cond = " OR ".join(["po_number LIKE ?" for _ in RECURRING_PATTERNS])
    exclude_placeholders = ",".join("?" * len(EXCLUDE_IDS))

    query_count = f"""
        SELECT COUNT(*) FROM invoices 
        WHERE ({where_cond}) AND id NOT IN ({exclude_placeholders});
    """
    params = list(RECURRING_PATTERNS) + EXCLUDE_IDS
    matching = cur.execute(query_count, params).fetchone()[0]
    print(f"Found {matching} invoices with product descriptions or dummy text in PO field.")

    if matching == 0:
        print("No matching records found to clean.")
        conn.close()
        return

    # Update matching records: clear po_number to NULL
    update_sql = f"""
        UPDATE invoices 
        SET po_number = NULL, updated_at = datetime('now')
        WHERE ({where_cond}) AND id NOT IN ({exclude_placeholders});
    """
    cur.execute(update_sql, params)
    conn.commit()

    # Verify
    remaining = cur.execute(query_count, params).fetchone()[0]
    null_pos = cur.execute("SELECT COUNT(*) FROM invoices WHERE po_number IS NULL OR trim(po_number) = '';").fetchone()[0]
    genuine_pos = cur.execute("SELECT COUNT(*) FROM invoices WHERE po_number IS NOT NULL AND trim(po_number) != '';").fetchone()[0]
    total = cur.execute("SELECT COUNT(*) FROM invoices;").fetchone()[0]

    print(f"Updated {matching} records.")
    print(f"Remaining matching: {remaining}")
    print(f"Total invoices: {total} (with genuine PO: {genuine_pos}, without PO: {null_pos})")

    conn.close()
    print("Database cleanup completed successfully.")


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "data" / "collectflow.db")
    clean_po_numbers(db)
