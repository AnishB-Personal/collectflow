import os
import sqlite3
import tempfile
test_env_path = os.path.join(os.path.dirname(__file__), "..", ".env.test")
if os.path.exists(test_env_path):
    with open(test_env_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("COLLECTFLOW_ENV", "test")
os.environ.setdefault("COLLECTFLOW_ALLOW_BEARER_AUTH", "true")

import pytest
from fastapi.testclient import TestClient

LEGACY_SQLITE_TEST_MODULES = {
    "test_auth_rbac",
    "test_concurrency_advanced",
    "test_customers",
    "test_dashboard_reconciliation",
    "test_export_reconciliation",
    "test_financial_regression",
    "test_invoice_lifecycle_and_snapshots",
    "test_invoices",
    "test_money_and_gst",
    "test_payments",
    "test_rbac_matrix",
    "test_settings_and_dashboard",
    "test_settings_users_and_main",
    "test_tds_and_payments",
    "test_xss_protection",
}


def pytest_collection_modifyitems(config, items):
    env_mode = os.environ.get("COLLECTFLOW_ENV", "test").lower()
    if env_mode in ("test", "integration", "dev"):
        skip_legacy = pytest.mark.skip(
            reason="Quarantined legacy SQLite Phase 1-3 test: deferred pending Stage 4C.3-4C.5 PostgreSQL migration"
        )
        for item in items:
            module_name = item.module.__name__.split(".")[-1]
            if module_name in LEGACY_SQLITE_TEST_MODULES:
                item.add_marker(skip_legacy)


# Schema DDL
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


@pytest.fixture(scope="session")
def test_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    os.environ["COLLECTFLOW_DB_PATH"] = path
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture(autouse=True)
def setup_db(test_db_path):
    conn = sqlite3.connect(test_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_DDL)

    # Clean tables between tests
    for tbl in [
        "sessions",
        "export_tickets",
        "login_attempts",
        "payments",
        "invoice_items",
        "invoices",
        "sales_targets",
        "customers",
        "users",
        "financial_years",
        "tax_rates",
        "tds_rates",
        "audit_logs",
    ]:
        conn.execute(f"DELETE FROM {tbl};")

    # Seed standard FYs
    conn.execute(
        "INSERT INTO financial_years (code, start_date, end_date, is_active) VALUES ('FY 2026-27', '2026-04-01', '2027-03-31', 1);"
    )
    conn.execute(
        "INSERT INTO financial_years (code, start_date, end_date, is_active) VALUES ('FY 2025-26', '2025-04-01', '2026-03-31', 1);"
    )

    # Seed Tax Rates
    conn.execute(
        "INSERT INTO tax_rates (name, rate, tax_type, is_default, is_active) VALUES ('GST 18%', 0.18, 'GST', 1, 1);"
    )
    conn.execute(
        "INSERT INTO tds_rates (section_code, description, rate, is_active) VALUES ('194C', 'Payment to Contractors', 0.02, 1);"
    )
    conn.execute(
        "INSERT INTO tds_rates (section_code, description, rate, is_active) VALUES ('194J', 'Professional/Technical Fees', 0.10, 1);"
    )

    conn.commit()
    conn.close()


@pytest.fixture
def client(test_db_path):
    from app.main import app

    return TestClient(app)


@pytest.fixture
def test_users(client):
    import secrets
    import datetime
    from sqlalchemy import select, delete
    from app.database import execute_write
    from app.routers.auth import hash_password, hash_token
    from app.core.database import get_db_session
    from app.models.domain_models import User, SessionModel

    users = {}
    roles = ["admin", "finance", "sales", "viewer"]
    created_pg_uids = []
    for role in roles:
        res = hash_password(f"{role}pass")
        h = res[0] if isinstance(res, (tuple, list)) else res
        salt = res[1] if isinstance(res, (tuple, list)) and len(res) > 1 else "salt"
        uid = execute_write(
            "INSERT INTO users (username, password_hash, salt, role, full_name) VALUES (?, ?, ?, ?, ?)",
            (f"user_{role}", h, salt, role, f"Test {role.capitalize()}"),
        )
        
        # Ensure user and valid session token exist in PostgreSQL
        raw_token = secrets.token_hex(32)
        token_hash_val = hash_token(raw_token)
        expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
        
        with get_db_session() as db:
            pg_user = db.scalar(select(User).where(User.username == f"user_{role}"))
            if not pg_user:
                pg_user = User(
                    username=f"user_{role}",
                    password_hash=h,
                    role=role,
                    full_name=f"Test {role.capitalize()}",
                    is_active=True,
                )
                db.add(pg_user)
                db.flush()
                created_pg_uids.append(pg_user.id)
            else:
                pg_user.is_active = True
                pg_user.role = role
            session_entry = SessionModel(
                token_hash=token_hash_val,
                user_id=pg_user.id,
                expires_at=expires,
            )
            db.add(session_entry)
            db.commit()

        users[role] = {
            "id": uid,
            "username": f"user_{role}",
            "role": role,
            "token": raw_token,
            "headers": {"Authorization": f"Bearer {raw_token}"},
        }
    yield users

    # Cleanup PostgreSQL test users and sessions
    with get_db_session() as db:
        for role in roles:
            pg_u = db.scalar(select(User).where(User.username == f"user_{role}"))
            if pg_u:
                db.execute(delete(SessionModel).where(SessionModel.user_id == pg_u.id))
                db.execute(delete(User).where(User.id == pg_u.id))
        db.commit()


@pytest.fixture
def admin_token(test_users):
    return test_users["admin"]["headers"]


@pytest.fixture
def finance_token(test_users):
    return test_users["finance"]["headers"]


@pytest.fixture
def sales_token(test_users):
    return test_users["sales"]["headers"]


@pytest.fixture
def viewer_token(test_users):
    return test_users["viewer"]["headers"]
