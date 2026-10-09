#!/usr/bin/env python3
"""
Synchronize User Management with sales executives:
1. Ensure 'anish' has 'admin' role.
2. Ensure active sales executives 'siliya', 'anshar', 'syama' exist with role 'sales'.
3. Sync executives table so only active sales users are marked is_active = 1.
"""

import hashlib
import os
import sqlite3
import sys
from pathlib import Path


def hash_pw(pw: str) -> tuple[str, str]:
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000).hex()
    return h, salt


def sync_users_and_executives(db_path: str):
    print(f"Syncing users in: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. Update anish to admin
    cur.execute("UPDATE users SET role = 'admin' WHERE username = 'anish';")

    # 2. Add or update active sales executives
    sales_reps = [
        ("siliya", "Siliya Mol PS", "sales"),
        ("anshar", "Anshar", "sales"),
        ("syama", "Syama.S", "sales"),
    ]
    for uname, full_name, role in sales_reps:
        row = cur.execute("SELECT id FROM users WHERE LOWER(username) = ?;", (uname.lower(),)).fetchone()
        if not row:
            h, salt = hash_pw("CollectFlow@2026")
            cur.execute(
                "INSERT INTO users (username, password_hash, salt, role, full_name) VALUES (?, ?, ?, ?, ?);",
                (uname, h, salt, role, full_name),
            )
            print(f"Created sales user '{uname}' ({full_name})")
        else:
            cur.execute(
                "UPDATE users SET role = ?, full_name = ? WHERE id = ?;",
                (role, full_name, row[0]),
            )
            print(f"Updated user '{uname}' ({full_name}) to role '{role}'")

    # 3. Mark only active sales users in executives table
    active_sales_names = [name.lower() for _, name, _ in sales_reps]
    cur.execute("UPDATE executives SET is_active = 0;")
    for name in active_sales_names:
        ex = cur.execute("SELECT id FROM executives WHERE LOWER(name) = ?;", (name,)).fetchone()
        if ex:
            cur.execute("UPDATE executives SET is_active = 1 WHERE id = ?;", (ex[0],))
        else:
            # Title case
            t_name = name.title()
            cur.execute("INSERT INTO executives (name, is_active) VALUES (?, 1);", (t_name,))

    conn.commit()

    print("\nCurrent User Management List:")
    for r in cur.execute("SELECT id, username, full_name, role FROM users ORDER BY id ASC;").fetchall():
        print(f"  ID {r[0]}: {r[1]} ({r[2]}) -> Role: {r[3]}")

    print("\nActive Executives in Database:")
    for r in cur.execute("SELECT id, name, is_active FROM executives WHERE is_active = 1 ORDER BY name ASC;").fetchall():
        print(f"  ID {r[0]}: {r[1]}")

    conn.close()


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent.parent
    default_db = base_dir / "data" / "collectflow.db"
    target_db = sys.argv[1] if len(sys.argv) > 1 else str(default_db)
    sync_users_and_executives(target_db)
