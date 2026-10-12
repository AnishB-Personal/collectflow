import logging
import os
import time
import uuid

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.database import query_all, query_one
from app.routers import auth, customers, dashboard, export, invoices, payments, settings, users
from app.routers.auth import get_current_user

# Configure structured logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [req:%(name)s] %(message)s")
logger = logging.getLogger("collectflow")

app = FastAPI(title="CollectFlow", description="Order-to-Cash & Accounts Receivable Management System")

# Configure exact-origin CORS middleware
raw_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000")
allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip() and o.strip() != "*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    max_age=600,
)


@app.middleware("http")
async def security_and_observability_middleware(request: Request, call_next):
    # 1. Correlation / Request ID tracking
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    start_time = time.perf_counter()

    # 2. Invoke handler
    try:
        response: Response = await call_next(request)
    except Exception as exc:
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.error(
            f"req_id={request_id} method={request.method} path={request.url.path} status=500 duration_ms={duration_ms} err={str(exc)}"
        )
        raise exc

    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
    # Log operational metrics (strictly no credentials, tokens, or sensitive payloads)
    logger.info(
        f"req_id={request_id} method={request.method} path={request.url.path} status={response.status_code} duration_ms={duration_ms}"
    )

    # 3. Security Headers
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.tailwindcss.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self' https://cdn.jsdelivr.net; "
        "object-src 'none'; "
        "base-uri 'self';"
    )

    return response


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(settings.router)
app.include_router(dashboard.router)
app.include_router(invoices.router)
app.include_router(payments.router)
app.include_router(customers.router)
app.include_router(export.router)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "index.html")


@app.get("/health")
def health_check():
    """Liveness probe."""
    return {"status": "healthy", "service": "CollectFlow", "version": "1.0.0"}


@app.get("/ready")
def readiness_check():
    """Readiness probe checking database connectivity."""
    try:
        res = query_one("SELECT 1 as is_alive;")
        if not res or res.get("is_alive") != 1:
            raise Exception("Database probe failed")
        return {"status": "ready", "database": "connected"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "not_ready", "error": str(e)})


@app.get("/", response_class=HTMLResponse)
def index():
    with open(TEMPLATE_PATH, "r") as f:
        return f.read()


@app.get("/api/meta")
def get_metadata(user: dict = Depends(get_current_user)):
    fys = query_all("SELECT code FROM financial_years WHERE is_active = 1 ORDER BY code DESC;")
    execs = query_all("""
        SELECT id, COALESCE(full_name, username) AS name
        FROM users
        WHERE role = 'sales'
        ORDER BY COALESCE(full_name, username) ASC;
    """)
    custs = query_all("SELECT id, billing_name FROM customers ORDER BY billing_name ASC;")
    tax_rates = query_all(
        "SELECT id, name, rate, tax_type, is_default FROM tax_rates WHERE is_active = 1 ORDER BY rate ASC;"
    )
    tds_rates = query_all(
        "SELECT id, section_code, description, rate FROM tds_rates WHERE is_active = 1 ORDER BY id ASC;"
    )

    return {
        "financial_years": [f["code"] for f in fys],
        "executives": execs,
        "customers": custs,
        "tax_rates": tax_rates,
        "tds_rates": tds_rates,
    }


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=True)
