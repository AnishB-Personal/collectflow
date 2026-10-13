"""
Statutory Reference Data Operations Router for PostgreSQL Parity.

Provides authenticated endpoints for:
- Indian Financial Years (boundaries, active status, creation, seeding)
- GST Rates Master (standard percentages, CGST/SGST/IGST splits)
- TDS Sections Master (194C, 194J, 194J(b) statutory reference rates)
- Reference validation
"""

import datetime
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from app.core.database import get_db_session
from app.models.domain_models import (
    CommercialInvoice,
    FinancialYear,
    GstRateMaster,
    TdsSectionMaster,
)
from app.routers.auth import (
    get_current_user,
    log_security_audit,
    require_finance_or_admin,
)

router = APIRouter(prefix="/api/settings", tags=["Settings"])


class FYCreate(BaseModel):
    code: str = Field(min_length=4, max_length=20)
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class GSTCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    rate: float = Field(ge=0.0, le=100.0)  # e.g. 0.18 or 18%
    tax_type: Optional[str] = "GST"
    is_default: Optional[bool] = False


class TDSCreate(BaseModel):
    section_code: str = Field(min_length=1, max_length=20)
    description: Optional[str] = Field(default=None, max_length=255)
    rate: float = Field(ge=0.0, le=100.0)  # e.g. 0.02 or 2%


@router.get("/financial-years")
def list_financial_years(user: dict = Depends(get_current_user)):
    with get_db_session() as db:
        fys = db.scalars(select(FinancialYear).order_by(FinancialYear.code.desc())).all()
        return [
            {
                "code": fy.code,
                "start_date": fy.start_date.isoformat() if fy.start_date else None,
                "end_date": fy.end_date.isoformat() if fy.end_date else None,
                "is_active": fy.is_active,
            }
            for fy in fys
        ]


@router.post("/financial-years")
def create_financial_year(item: FYCreate, user: dict = Depends(require_finance_or_admin)):
    code = item.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="Financial Year code is required")

    s_date_str = item.start_date or "2026-04-01"
    e_date_str = item.end_date or "2027-03-31"

    try:
        sd = datetime.date.fromisoformat(s_date_str)
        ed = datetime.date.fromisoformat(e_date_str)
        if sd >= ed:
            raise HTTPException(status_code=400, detail="Start date must be before end date")
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be in valid YYYY-MM-DD format")

    with get_db_session() as db:
        existing = db.scalar(select(FinancialYear).where(FinancialYear.code == code))
        if existing:
            raise HTTPException(status_code=400, detail="Financial Year already exists")

        new_fy = FinancialYear(
            code=code,
            start_date=sd,
            end_date=ed,
            is_active=True,
        )
        db.add(new_fy)

        log_security_audit(
            "FY_CREATED",
            "SETTINGS",
            code,
            user_id=user["id"],
            username=user["username"],
            details={"start_date": s_date_str, "end_date": e_date_str},
            db=db,
        )
        db.commit()

        return {"success": True, "message": f"Financial Year '{code}' created"}


@router.delete("/financial-years/{code}")
def delete_financial_year(code: str, user: dict = Depends(require_finance_or_admin)):
    with get_db_session() as db:
        fy = db.scalar(select(FinancialYear).where(FinancialYear.code == code))
        if not fy:
            raise HTTPException(status_code=404, detail="Financial Year not found")

        # Check if invoices exist for this FY
        inv_count = db.scalar(select(func.count(CommercialInvoice.id)).where(CommercialInvoice.fy_code == code)) or 0
        if inv_count > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete financial year '{code}' because {inv_count} invoice(s) are associated with it",
            )

        db.delete(fy)

        log_security_audit("FY_DELETED", "SETTINGS", code, user_id=user["id"], username=user["username"], db=db)
        db.commit()

        return {"success": True, "message": f"Financial Year '{code}' deleted"}


@router.get("/taxes")
def get_taxes(user: dict = Depends(get_current_user)):
    with get_db_session() as db:
        gst_list = db.scalars(
            select(GstRateMaster).where(GstRateMaster.is_active.is_(True)).order_by(GstRateMaster.rate.asc())
        ).all()
        tds_list = db.scalars(
            select(TdsSectionMaster).where(TdsSectionMaster.is_active.is_(True)).order_by(TdsSectionMaster.id.asc())
        ).all()

        return {
            "gst_rates": [
                {
                    "id": g.id,
                    "name": g.name,
                    "rate": float(g.rate),
                    "cgst_rate": float(g.cgst_rate),
                    "sgst_rate": float(g.sgst_rate),
                    "igst_rate": float(g.igst_rate),
                    "is_default": g.is_default,
                    "is_active": g.is_active,
                }
                for g in gst_list
            ],
            "tds_rates": [
                {
                    "id": t.id,
                    "section_code": t.section_code,
                    "description": t.description,
                    "rate": float(t.default_rate),
                    "is_active": t.is_active,
                }
                for t in tds_list
            ],
        }


@router.post("/taxes/gst")
def create_gst_rate(item: GSTCreate, user: dict = Depends(require_finance_or_admin)):
    rate_val = item.rate
    if rate_val > 1.0:
        rate_val = rate_val / 100.0  # normalize e.g. 18 -> 0.18

    rate_dec = Decimal(str(rate_val)).quantize(Decimal("0.0001"))
    half_rate = (rate_dec / Decimal("2")).quantize(Decimal("0.0001"))
    name = item.name.strip()

    with get_db_session() as db:
        if item.is_default:
            # Unset existing defaults
            db.execute(
                update(GstRateMaster).where(GstRateMaster.is_default.is_(True)).values(is_default=False)
            )

        new_gst = GstRateMaster(
            name=name,
            rate=rate_dec,
            cgst_rate=half_rate,
            sgst_rate=half_rate,
            igst_rate=rate_dec,
            is_default=bool(item.is_default),
            is_active=True,
        )
        db.add(new_gst)
        db.flush()

        log_security_audit(
            "GST_RATE_CREATED",
            "SETTINGS",
            str(new_gst.id),
            user_id=user["id"],
            username=user["username"],
            details={"name": name, "rate": float(rate_dec)},
            db=db,
        )
        db.commit()

        return {"success": True, "id": new_gst.id, "message": f"GST rate '{name}' created"}


@router.post("/taxes/tds")
def create_tds_rate(item: TDSCreate, user: dict = Depends(require_finance_or_admin)):
    code = item.section_code.strip()
    rate_val = item.rate
    if rate_val > 1.0:
        rate_val = rate_val / 100.0

    rate_dec = Decimal(str(rate_val)).quantize(Decimal("0.0001"))

    with get_db_session() as db:
        existing = db.scalar(select(TdsSectionMaster).where(TdsSectionMaster.section_code == code))
        if existing:
            raise HTTPException(status_code=400, detail=f"TDS section '{code}' already exists")

        new_tds = TdsSectionMaster(
            section_code=code,
            description=item.description.strip() if item.description else code,
            default_rate=rate_dec,
            is_active=True,
        )
        db.add(new_tds)
        db.flush()

        log_security_audit(
            "TDS_SECTION_CREATED",
            "SETTINGS",
            str(new_tds.id),
            user_id=user["id"],
            username=user["username"],
            details={"section_code": code, "default_rate": float(rate_dec)},
            db=db,
        )
        db.commit()

        return {"success": True, "id": new_tds.id, "message": f"TDS section '{code}' created"}
