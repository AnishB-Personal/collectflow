#!/usr/bin/env python3
"""
Import Invoices, Orders (unbilled POs), Line Items, and Payments
starting from 1st April 2026 (FY 2026-27) from the Excel workbook into CollectFlow.
"""

import datetime
import re
import sqlite3
import sys
from pathlib import Path
import openpyxl


def clean_str(val):
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def parse_money(val):
    if val is None:
        return 0.0, "INR"
    if isinstance(val, (int, float)):
        return round(float(val), 2), "INR"
    s = str(val).strip()
    curr = "INR"
    if "$" in s:
        curr = "USD"
        s = s.replace("$", "")
    elif "AED" in s.upper():
        curr = "AED"
        s = re.sub(r"(?i)AED", "", s)
    s = s.replace(",", "").strip()
    try:
        return round(float(s), 2), curr
    except ValueError:
        return 0.0, curr


def parse_date(val):
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, str):
        # Support formats like DD-MM-YYYY, DD/MM/YYYY
        m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", val)
        if m:
            d, mth, y = m.groups()
            # Catch typos like 20256 -> 2026
            if len(y) > 4:
                y = "2026"
            try:
                dt = datetime.date(int(y), int(mth), int(d))
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None


def calculate_due_date(inv_date_str):
    if not inv_date_str:
        return None
    try:
        dt = datetime.datetime.strptime(inv_date_str, "%Y-%m-%d").date()
        return (dt + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    except Exception:
        return None


def import_2026_data(excel_path: str, db_path: str):
    print(f"Loading Excel: {excel_path}")
    wb = openpyxl.load_workbook(excel_path, data_only=True)
    ws_details = wb["Details"]

    print(f"Connecting to DB: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    cur = conn.cursor()

    # 1. Ensure all Executives exist
    executives_list = [
        "Anish Babu A",
        "Siliya Mol PS",
        "Sarath H R",
        "Fazil SR",
        "Anshar",
        "Manoj",
        "Shameer",
        "Syama.S",
    ]
    for ex_name in executives_list:
        cur.execute("SELECT id FROM executives WHERE LOWER(name) = ?;", (ex_name.lower(),))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO executives (name, is_active) VALUES (?, 1);", (ex_name,))

    conn.commit()
    exec_map = {r["name"].lower(): r["id"] for r in cur.execute("SELECT id, name FROM executives;").fetchall()}
    print(f"Configured {len(exec_map)} executives.")

    # 2. Build Customers map
    cust_rows = cur.execute("SELECT id, billing_name, gst_number, billing_address, state FROM customers;").fetchall()
    cust_map = {r["billing_name"].lower().strip(): dict(r) for r in cust_rows}

    # Alias known variations
    if "cosmopolitan hospital" in cust_map:
        cust_map["cosmopolitian hospital pvt ltd"] = cust_map["cosmopolitan hospital"]

    # 3. Clean existing 2026-27 records to prevent duplicate import if re-run
    existing_2026_invs = cur.execute("SELECT id FROM invoices WHERE po_date >= '2026-04-01';").fetchall()
    if existing_2026_invs:
        inv_ids = [r["id"] for r in existing_2026_invs]
        print(f"Cleaning {len(inv_ids)} existing post-2026 invoices for clean reload...")
        placeholders = ",".join("?" * len(inv_ids))
        cur.execute(f"DELETE FROM payments WHERE invoice_id IN ({placeholders});", inv_ids)
        cur.execute(f"DELETE FROM invoice_items WHERE invoice_id IN ({placeholders});", inv_ids)
        cur.execute(f"DELETE FROM invoices WHERE id IN ({placeholders});", inv_ids)
        conn.commit()

    invoices_inserted = 0
    orders_inserted = 0
    payments_inserted = 0

    # Row 3 is header, data starts at row 4
    for row_idx, row in enumerate(ws_details.iter_rows(min_row=4, values_only=True), start=4):
        po_date_raw = row[1]
        if not isinstance(po_date_raw, (datetime.datetime, datetime.date)):
            continue
        d = po_date_raw.date() if isinstance(po_date_raw, datetime.datetime) else po_date_raw
        if d < datetime.date(2026, 4, 1):
            continue

        po_date_str = d.strftime("%Y-%m-%d")
        po_number = clean_str(row[2]) or "No PO"
        raw_cust_name = clean_str(row[3])
        if not raw_cust_name:
            continue

        # Customer matching or on-demand creation
        norm_cust = raw_cust_name.lower().strip()
        if norm_cust in cust_map:
            cust_info = cust_map[norm_cust]
        else:
            cur.execute("INSERT INTO customers (billing_name, country) VALUES (?, 'IN');", (raw_cust_name,))
            new_cid = cur.lastrowid
            cust_info = {
                "id": new_cid,
                "billing_name": raw_cust_name,
                "gst_number": None,
                "billing_address": None,
                "state": None,
            }
            cust_map[norm_cust] = cust_info

        # Executive
        raw_exec = clean_str(row[4])
        exec_id = exec_map.get(raw_exec.lower()) if raw_exec else None

        # Financial values
        excl, curr = parse_money(row[5])
        tax_amt, _ = parse_money(row[6]) if row[6] not in ("NA", "None", None) else (0.0, curr)
        tot, _ = parse_money(row[7]) if row[7] is not None else (round(excl + tax_amt, 2), curr)
        tax_ref = clean_str(row[8])

        tax_rate = round(tax_amt / excl, 4) if (tax_amt > 0 and excl > 0) else 0.0

        # Invoice details
        inv_no = clean_str(row[9])
        inv_date_str = parse_date(row[10])
        due_date_str = parse_date(row[11]) or calculate_due_date(inv_date_str)

        # Payment details
        pay_amt, _ = parse_money(row[12]) if row[12] is not None else (0.0, curr)
        pay_date_str = parse_date(row[13]) or inv_date_str or po_date_str
        remittance = clean_str(row[14])

        # Compute status
        if not inv_no:
            status = "UNBILLED"
            order_type = "PO"
        elif pay_amt >= tot and tot > 0:
            status = "PAID"
            order_type = "PO"
        elif pay_amt > 0:
            status = "PARTIAL"
            order_type = "PO"
        elif due_date_str and due_date_str < datetime.date.today().strftime("%Y-%m-%d"):
            status = "OVERDUE"
            order_type = "PO"
        else:
            status = "PENDING"
            order_type = "PO"

        cur.execute(
            """
            INSERT INTO invoices (
                fy_code, order_type, po_date, po_number, customer_id, executive_id,
                currency, exchange_rate, original_amount,
                exclusive_amount, tax_rate, tax_amount, total_amount,
                tax_ref, invoice_number, invoice_date, due_date, payment_status,
                customer_name, customer_gstin, customer_address, customer_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                "FY 2026-27",
                order_type,
                po_date_str,
                po_number,
                cust_info["id"],
                exec_id,
                curr,
                excl,
                excl,
                tax_rate,
                tax_amt,
                tot,
                tax_ref,
                inv_no,
                inv_date_str,
                due_date_str,
                status,
                cust_info["billing_name"],
                cust_info["gst_number"],
                cust_info["billing_address"],
                cust_info["state"],
            ),
        )
        invoice_id = cur.lastrowid

        if inv_no:
            invoices_inserted += 1
        else:
            orders_inserted += 1

        # Insert line item
        cur.execute(
            """
            INSERT INTO invoice_items (invoice_id, description, quantity, unit_price, tax_rate, tax_amount, total_amount)
            VALUES (?, ?, 1.0, ?, ?, ?, ?);
            """,
            (invoice_id, po_number, excl, tax_rate, tax_amt, tot),
        )

        # Insert payment if paid
        if pay_amt > 0:
            cur.execute(
                """
                INSERT INTO payments (invoice_id, payment_amount, payment_date, remittance_number, payment_mode, notes)
                VALUES (?, ?, ?, ?, 'NEFT', 'Imported from FY 2026-27 settlement');
                """,
                (invoice_id, pay_amt, pay_date_str, remittance),
            )
            payments_inserted += 1

    conn.commit()

    # Integrity verification
    fk_errors = cur.execute("PRAGMA foreign_key_check;").fetchall()
    if fk_errors:
        print(f"ERROR: Foreign key errors: {fk_errors}")
        sys.exit(1)

    print("=== Import Completed Successfully ===")
    print(f"Invoices Inserted: {invoices_inserted}")
    print(f"Open Orders (Unbilled POs): {orders_inserted}")
    print(f"Payments Recorded: {payments_inserted}")
    print(f"Foreign Key Integrity: PASSED (0 errors)")

    total_invoices = cur.execute("SELECT COUNT(*) FROM invoices;").fetchone()[0]
    total_payments = cur.execute("SELECT COUNT(*) FROM payments;").fetchone()[0]
    print(f"Total Database Invoices: {total_invoices}")
    print(f"Total Database Payments: {total_payments}")

    conn.close()


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent.parent
    default_excel = base_dir.parent / "PO_Payment_Details_FY18-19_to_26-27_Upgraded.xlsx"
    default_db = base_dir / "data" / "collectflow.db"

    excel_file = sys.argv[1] if len(sys.argv) > 1 else str(default_excel)
    db_file = sys.argv[2] if len(sys.argv) > 2 else str(default_db)

    import_2026_data(excel_file, db_file)
