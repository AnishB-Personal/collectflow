import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.database import execute_write, log_audit, query_all, query_one, transaction
from app.routers.auth import get_current_user, require_finance_or_admin

router = APIRouter(prefix="/api/settings", tags=["Settings"])


class FYCreate(BaseModel):
    code: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None


@router.get("/financial-years")
def list_financial_years(user: dict = Depends(get_current_user)):
    return query_all("SELECT code, start_date, end_date, is_active FROM financial_years ORDER BY code DESC;")


@router.post("/financial-years")
def create_financial_year(item: FYCreate, user: dict = Depends(require_finance_or_admin)):
    code = item.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="Financial Year code is required")

    existing = query_one("SELECT code FROM financial_years WHERE code = ?", (code,))
    if existing:
        raise HTTPException(status_code=400, detail="Financial Year already exists")

    s_date = item.start_date or "2026-04-01"
    e_date = item.end_date or "2027-03-31"

    try:
        sd = datetime.date.fromisoformat(s_date)
        ed = datetime.date.fromisoformat(e_date)
        if sd >= ed:
            raise HTTPException(status_code=400, detail="Start date must be before end date")
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be in valid YYYY-MM-DD format")

    with transaction() as conn:
        execute_write(
            "INSERT INTO financial_years (code, start_date, end_date, is_active) VALUES (?, ?, ?, 1)",
            (code, s_date, e_date),
            conn=conn,
        )
        log_audit("FY_CREATED", "SETTINGS", code, f"Created FY {code} ({s_date} to {e_date})", user=user, conn=conn)

    return {"success": True, "message": f"Financial Year '{code}' created"}


@router.post("/financial-years/seed")
def seed_standard_financial_years(user: dict = Depends(require_finance_or_admin)):
    standard_fys = [
        ("FY 2026-27", "2026-04-01", "2027-03-31"),
        ("FY 2025-26", "2025-04-01", "2026-03-31"),
        ("FY 2024-25", "2024-04-01", "2025-03-31"),
        ("FY 2023-24", "2023-04-01", "2024-03-31"),
        ("FY 2022-23", "2022-04-01", "2023-03-31"),
        ("FY 2021-22", "2021-04-01", "2022-03-31"),
        ("FY 2020-21", "2020-04-01", "2021-03-31"),
        ("FY 2019-20", "2019-04-01", "2020-03-31"),
        ("FY 2018-19", "2018-04-01", "2019-03-31"),
    ]
    with transaction() as conn:
        for code, s, e in standard_fys:
            execute_write(
                "INSERT OR IGNORE INTO financial_years (code, start_date, end_date, is_active) VALUES (?, ?, ?, 1)",
                (code, s, e),
                conn=conn,
            )
        log_audit("FY_SEEDED", "SETTINGS", None, "Populated standard Indian financial years", user=user, conn=conn)

    return {"success": True, "message": "Standard financial years populated"}


@router.delete("/financial-years/{code}")
def delete_financial_year(code: str, user: dict = Depends(require_finance_or_admin)):
    # Check if invoices exist for this FY
    inv = query_one("SELECT COUNT(*) as count FROM invoices WHERE fy_code = ?", (code,))
    if inv and inv["count"] > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete financial year '{code}' because {inv['count']} invoice(s) are associated with it",
        )

    with transaction() as conn:
        execute_write("DELETE FROM financial_years WHERE code = ?", (code,), conn=conn)
        log_audit("FY_DELETED", "SETTINGS", code, f"Deleted FY {code}", user=user, conn=conn)

    return {"success": True, "message": f"Financial Year '{code}' deleted"}


# --- Tax Structure Master (GST & TDS) ---


class GSTCreate(BaseModel):
    name: str
    rate: float = Field(ge=0.0)  # e.g. 0.18 or 18%
    tax_type: Optional[str] = "GST"
    is_default: Optional[int] = 0


class TDSCreate(BaseModel):
    section_code: str
    description: Optional[str] = None
    rate: float = Field(ge=0.0)  # e.g. 0.02 or 2%


@router.get("/taxes")
def get_taxes(user: dict = Depends(get_current_user)):
    gst_list = query_all(
        "SELECT id, name, rate, tax_type, is_default, is_active FROM tax_rates WHERE is_active = 1 ORDER BY rate ASC;"
    )
    tds_list = query_all(
        "SELECT id, section_code, description, rate, is_active FROM tds_rates WHERE is_active = 1 ORDER BY id ASC;"
    )
    return {"gst_rates": gst_list, "tds_rates": tds_list}


@router.post("/taxes/gst")
def create_gst_rate(item: GSTCreate, user: dict = Depends(require_finance_or_admin)):
    rate_val = item.rate
    if rate_val > 1.0:
        rate_val = rate_val / 100.0  # Allow user to pass 18 instead of 0.18

    name = item.name.strip()
    with transaction() as conn:
        new_id = execute_write(
            """
            INSERT INTO tax_rates (name, rate, tax_type, is_default, is_active)
            VALUES (?, ?, ?, ?, 1)
        """,
            (name, rate_val, item.tax_type or "GST", item.is_default or 0),
            conn=conn,
        )
        log_audit(
            "GST_RATE_CREATED", "SETTINGS", new_id, f"Created GST rate {name} ({rate_val * 100}%)", user=user, conn=conn
        )

    return {"success": True, "id": new_id, "message": "GST rate added"}


@router.delete("/taxes/gst/{tax_id}")
def delete_gst_rate(tax_id: int, user: dict = Depends(require_finance_or_admin)):
    with transaction() as conn:
        # Soft deactivate to maintain integrity of historical invoices
        execute_write("UPDATE tax_rates SET is_active = 0 WHERE id = ?", (tax_id,), conn=conn)
        log_audit("GST_RATE_DEACTIVATED", "SETTINGS", tax_id, f"Deactivated GST rate ID {tax_id}", user=user, conn=conn)

    return {"success": True, "message": "GST rate deactivated"}


@router.post("/taxes/tds")
def create_tds_rate(item: TDSCreate, user: dict = Depends(require_finance_or_admin)):
    rate_val = item.rate
    if rate_val > 1.0:
        rate_val = rate_val / 100.0

    section = item.section_code.strip()
    with transaction() as conn:
        new_id = execute_write(
            """
            INSERT INTO tds_rates (section_code, description, rate, is_active)
            VALUES (?, ?, ?, 1)
        """,
            (section, item.description, rate_val),
            conn=conn,
        )
        log_audit(
            "TDS_RATE_CREATED",
            "SETTINGS",
            new_id,
            f"Created TDS section {section} ({rate_val * 100}%)",
            user=user,
            conn=conn,
        )

    return {"success": True, "id": new_id, "message": "TDS rate added"}


@router.delete("/taxes/tds/{tds_id}")
def delete_tds_rate(tds_id: int, user: dict = Depends(require_finance_or_admin)):
    with transaction() as conn:
        # Soft deactivate to protect historical invoice payments
        execute_write("UPDATE tds_rates SET is_active = 0 WHERE id = ?", (tds_id,), conn=conn)
        log_audit("TDS_RATE_DEACTIVATED", "SETTINGS", tds_id, f"Deactivated TDS rate ID {tds_id}", user=user, conn=conn)

    return {"success": True, "message": "TDS rate deactivated"}
