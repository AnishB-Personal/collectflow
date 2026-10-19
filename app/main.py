import logging
import os
import time
import uuid

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.database import query_all, query_one
from app.routers import (
    auth,
    customers,
    dashboard,
    export,
    invoices,
    orders,
    payments,
    reporting,
    settings,
    targets,
    users,
)
from contextlib import asynccontextmanager
from app.core.auth_security import validate_security_configuration
from app.routers.auth import get_current_user

# Configure structured logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [req:%(name)s] %(message)s")
logger = logging.getLogger("collectflow")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Enforce security configuration validation at startup (WP04-R03)
    logger.info("Validating CollectFlow security configuration...")
    validate_security_configuration()
    logger.info("Security configuration validated successfully.")
    yield


app = FastAPI(
    title="CollectFlow",
    description="Order-to-Cash & Accounts Receivable Management System",
    lifespan=lifespan,
)

# Configure exact-origin CORS middleware (strictly non-wildcard for credentialed requests)
raw_origins = os.environ.get(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000,http://localhost:8080,http://127.0.0.1:8080",
)
allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip() and o.strip() != "*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=bool(allowed_origins and "*" not in allowed_origins),
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID", "X-CSRF-Token"],
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


# Stage 4C.5 Active PostgreSQL Routers
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(settings.router)
app.include_router(customers.router)
app.include_router(orders.router)
app.include_router(invoices.router)
app.include_router(payments.router)
app.include_router(targets.router)
app.include_router(reporting.router)
app.include_router(dashboard.router)
app.include_router(export.router)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "index.html")


@app.get("/health")
@app.get("/api/health")
def health_check():
    """Liveness probe."""
    return {"status": "healthy", "service": "CollectFlow", "version": "1.0.0"}


from sqlalchemy import text, select, func
from app.core.database import get_db_session
from app.models.domain_models import Customer, FinancialYear, GstRateMaster, TdsSectionMaster, User


@app.get("/ready")
@app.get("/api/ready")
def readiness_check():
    """Readiness probe checking database connectivity (sanitized against exception leakage)."""
    try:
        with get_db_session() as db:
            res = db.execute(text("SELECT 1 as is_alive;")).scalar()
            if res != 1:
                raise Exception("Database probe failed")
        return {"status": "ready", "database": "connected"}
    except Exception as e:
        logger.error(f"Readiness probe failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "Database connection probe failed"},
        )


@app.get("/", response_class=HTMLResponse)
def index():
    with open(TEMPLATE_PATH, "r") as f:
        return f.read()


@app.get("/api/meta")
def get_metadata(user: dict = Depends(get_current_user)):
    with get_db_session() as db:
        fys = db.scalars(select(FinancialYear.code).where(FinancialYear.is_active.is_(True)).order_by(FinancialYear.code.desc())).all()
        execs = db.execute(
            select(User.id, func.coalesce(User.full_name, User.username).label("name"))
            .where(User.role == "sales", User.is_active.is_(True))
            .order_by(func.coalesce(User.full_name, User.username).asc())
        ).all()
        custs = db.execute(
            select(Customer.id, Customer.billing_name)
            .where(Customer.is_active.is_(True))
            .order_by(Customer.billing_name.asc())
        ).all()
        gst_rates = db.scalars(
            select(GstRateMaster).where(GstRateMaster.is_active.is_(True)).order_by(GstRateMaster.rate.asc())
        ).all()
        tds_rates = db.scalars(
            select(TdsSectionMaster).where(TdsSectionMaster.is_active.is_(True)).order_by(TdsSectionMaster.id.asc())
        ).all()

        return {
            "financial_years": list(fys),
            "executives": [{"id": e[0], "name": e[1]} for e in execs],
            "customers": [{"id": c[0], "billing_name": c[1]} for c in custs],
            "tax_rates": [
                {
                    "id": g.id,
                    "name": g.name,
                    "rate": float(g.rate),
                    "tax_type": "GST",
                    "is_default": 1 if g.is_default else 0,
                }
                for g in gst_rates
            ],
            "tds_rates": [
                {
                    "id": t.id,
                    "section_code": t.section_code,
                    "description": t.description,
                    "rate": float(t.default_rate),
                }
                for t in tds_rates
            ],
        }


if __name__ == "__main__":
    import uvicorn

    env_mode = os.getenv("COLLECTFLOW_ENV", "production").lower()
    raw_host = os.getenv("HOST", "127.0.0.1")
    if env_mode in ("integration", "sandbox"):
        # Strictly bind to localhost only in integration mode
        host = "127.0.0.1"
    else:
        host = raw_host

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=True)
