import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional


DEFAULT_PROD_DB_FILENAME = "collectflow.db"


def get_db_path() -> str:
    env_mode = os.environ.get("COLLECTFLOW_ENV", "production").lower()
    custom_path = os.environ.get("COLLECTFLOW_DB_PATH")

    if env_mode in ("integration", "sandbox"):
        if not custom_path:
            raise RuntimeError(
                "CRITICAL ISOLATION ERROR: COLLECTFLOW_ENV is set to integration/sandbox, "
                "but COLLECTFLOW_DB_PATH is not explicitly defined. Startup aborted to prevent production database access."
            )
        resolved = os.path.abspath(custom_path)
        if os.path.basename(resolved) == DEFAULT_PROD_DB_FILENAME and "sandbox" not in resolved and "integration" not in resolved and "test" not in resolved:
            raise RuntimeError(
                f"CRITICAL ISOLATION ERROR: COLLECTFLOW_DB_PATH points to production database '{resolved}'. "
                "Integration mode requires an isolated sandbox database path containing 'sandbox', 'integration', or 'test'."
            )
        return resolved

    return custom_path or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", DEFAULT_PROD_DB_FILENAME)


def create_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


@contextmanager
def get_connection():
    conn = create_connection()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(existing_conn: Optional[sqlite3.Connection] = None):
    """
    Context manager for atomic database transactions using `BEGIN IMMEDIATE`.
    Acquires an immediate write lock on the SQLite database, preventing competing writers
    from starting concurrent write transactions during financial read-modify-write operations,
    while allowing concurrent readers in WAL mode.
    Rolls back automatically on unhandled exceptions and commits on success.
    """
    if existing_conn:
        yield existing_conn
    else:
        conn = create_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_db():
    conn = create_connection()
    try:
        yield conn
    finally:
        conn.close()


def query_all(query: str, params: tuple = (), conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    if conn:
        cur = conn.cursor()
        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]
    with get_connection() as c:
        cur = c.cursor()
        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]


def query_one(query: str, params: tuple = (), conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    if conn:
        cur = conn.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        return dict(row) if row else None
    with get_connection() as c:
        cur = c.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        return dict(row) if row else None


def execute_write(query: str, params: tuple = (), conn: Optional[sqlite3.Connection] = None) -> int:
    if conn:
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.lastrowid
    with create_connection() as c:
        cur = c.cursor()
        cur.execute(query, params)
        c.commit()
        return cur.lastrowid


def hash_token(raw_token: str) -> str:
    """Return SHA-256 hash of a bearer token or export ticket."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def sanitize_audit_details(details: Optional[str]) -> Optional[str]:
    """Redact passwords, bearer tokens, and hashes from audit details."""
    if not details:
        return details
    # Redact Authorization header patterns or token hex strings (>= 32 hex chars)
    s = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer [REDACTED]", details, flags=re.IGNORECASE)
    s = re.sub(r"(password|pwd|secret)\s*[:=]\s*['\"][^'\"]+['\"]", r"\1=[REDACTED]", s, flags=re.IGNORECASE)
    s = re.sub(r"\b[0-9a-fA-F]{64}\b", "[REDACTED_HASH]", s)
    return s


def init_db():
    """Ensure essential operational tables and schema enhancements exist."""
    with create_connection() as conn:
        # Audit logs table
        conn.execute("""
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
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp ON audit_logs(timestamp);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id);")

        # Migrate request_id column if table existed without it
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(audit_logs);").fetchall()]
        if "request_id" not in cols:
            conn.execute("ALTER TABLE audit_logs ADD COLUMN request_id TEXT;")

        # Single-use export tickets
        conn.execute("""
        CREATE TABLE IF NOT EXISTS export_tickets (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_export_tickets_hash ON export_tickets(token_hash);")

        # Login attempts tracker for progressive delay / rate limiting
        conn.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier TEXT NOT NULL,
            attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            success INTEGER DEFAULT 0
        );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup ON login_attempts(identifier, attempted_at);"
        )

        # Invoices customer snapshot columns
        inv_cols = [r["name"] for r in conn.execute("PRAGMA table_info(invoices);").fetchall()]
        if "customer_name" not in inv_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN customer_name TEXT;")
        if "customer_gstin" not in inv_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN customer_gstin TEXT;")
        if "customer_address" not in inv_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN customer_address TEXT;")
        if "customer_state" not in inv_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN customer_state TEXT;")

        # Customers email column
        cust_cols = [r["name"] for r in conn.execute("PRAGMA table_info(customers);").fetchall()]
        if "email" not in cust_cols:
            conn.execute("ALTER TABLE customers ADD COLUMN email TEXT;")

        # Payments idempotency key column and index
        pay_cols = [r["name"] for r in conn.execute("PRAGMA table_info(payments);").fetchall()]
        if "idempotency_key" not in pay_cols:
            conn.execute("ALTER TABLE payments ADD COLUMN idempotency_key TEXT;")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_idempotency ON payments(idempotency_key) WHERE idempotency_key IS NOT NULL;"
        )

        # Database-level unique constraints for commercial identifiers (normalized whitespace and case)
        conn.execute("DROP INDEX IF EXISTS idx_invoices_unique_po;")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_unique_po
            ON invoices(LOWER(TRIM(po_number)))
            WHERE po_number IS NOT NULL
              AND TRIM(po_number) != ''
              AND LOWER(TRIM(po_number)) NOT IN ('no po', 'recurring / direct invoice');
            """
        )
        conn.execute("DROP INDEX IF EXISTS idx_invoices_unique_inv;")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_unique_inv
            ON invoices(LOWER(TRIM(invoice_number)))
            WHERE invoice_number IS NOT NULL
              AND TRIM(invoice_number) != '';
            """
        )

        # Backfill snapshot customer data for existing invoices
        conn.execute("""
        UPDATE invoices
        SET customer_name = (SELECT billing_name FROM customers WHERE customers.id = invoices.customer_id),
            customer_gstin = (SELECT gst_number FROM customers WHERE customers.id = invoices.customer_id),
            customer_address = (SELECT billing_address FROM customers WHERE customers.id = invoices.customer_id),
            customer_state = (SELECT state FROM customers WHERE customers.id = invoices.customer_id)
        WHERE customer_name IS NULL;
        """)

        # Drop obsolete isolated tables if present
        conn.execute("DROP TABLE IF EXISTS executive_targets;")
        conn.execute("DROP TABLE IF EXISTS company_profile;")
        conn.execute("DROP TABLE IF EXISTS executives;")

        # Recreate v_invoice_summary view with customer snapshots and cancelled status support
        conn.execute("DROP VIEW IF EXISTS v_invoice_summary;")
        conn.execute("""
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

        conn.commit()


def cleanup_expired_sessions():
    """Purge expired sessions, consumed/expired export tickets, and old login attempts."""
    with create_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE datetime('now') >= expires_at;")
        conn.execute("DELETE FROM export_tickets WHERE datetime('now') >= expires_at OR used = 1;")
        # Keep login attempts for max 24 hours
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < datetime('now', '-1 day');")
        conn.commit()


def log_audit(
    action: str,
    entity_type: str,
    entity_id: Any,
    details: Optional[str] = None,
    user: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    request_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
):
    """Record an append-only audit trail event."""
    user_id = user.get("id") if user else None
    username = user.get("username") if user else None
    sanitized = sanitize_audit_details(details)
    sql = """
    INSERT INTO audit_logs (user_id, username, action, entity_type, entity_id, details, ip_address, request_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        user_id,
        username,
        action,
        entity_type,
        str(entity_id) if entity_id is not None else None,
        sanitized,
        ip_address,
        request_id,
    )
    execute_write(sql, params, conn=conn)


init_db()
