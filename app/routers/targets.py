"""
Sales Targets Domain Router for PostgreSQL Parity.

Provides:
- List sales targets by financial year with role-based visibility.
- Own-target visibility policy enforcement for sales representatives.
- Create or update sales targets (admin, finance only) with validation.
- Retention of historical sales targets for deactivated users.
- Exact two-decimal money string responses.
- Audit logging for target modifications.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.database import get_db_session
from app.models.domain_models import AuditLog, FinancialYear, SalesTarget, User
from app.routers.auth import (
    get_current_user,
    log_security_audit,
    require_finance_or_admin,
)

router = APIRouter(prefix="/api/targets", tags=["Sales Targets"])


def _quantize_2dec(val: Any) -> Decimal:
    if val is None:
        return Decimal("0.00")
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _format_money(val: Any) -> str:
    if val is None:
        return "0.00"
    return f"{Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


class TargetUpsertRequest(BaseModel):
    fy_code: str
    yearly_target: Decimal = Field(..., ge=Decimal("0.00"))
    half_yearly_target: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    quarterly_target: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    monthly_target: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    monthly_bottom_line: Optional[Decimal] = Field(Decimal("0.00"), ge=Decimal("0.00"))
    yearly_bottom_line: Optional[Decimal] = Field(Decimal("0.00"), ge=Decimal("0.00"))


def _serialize_target(target: SalesTarget, user: User) -> dict:
    return {
        "id": target.id,
        "user_id": target.user_id,
        "username": user.username,
        "full_name": user.full_name or user.username,
        "executive_name": user.full_name or user.username,
        "role": user.role,
        "is_active": user.is_active,
        "fy_code": target.fy_code,
        "target_amount": _format_money(target.yearly_target),
        "yearly_target": _format_money(target.yearly_target),
        "half_yearly_target": _format_money(target.half_yearly_target),
        "quarterly_target": _format_money(target.quarterly_target),
        "monthly_target": _format_money(target.monthly_target),
        "monthly_bottom_line": _format_money(target.monthly_bottom_line),
        "yearly_bottom_line": _format_money(target.yearly_bottom_line),
    }


@router.get("")
def list_sales_targets(
    fy: Optional[str] = None,
    user_id: Optional[int] = None,
    user: dict = Depends(get_current_user),
):
    """
    List sales targets with role-based visibility:
    - Sales representatives can only view their own targets.
    - Admin, Finance, and Viewer can view all targets.
    - Preserves historical targets for deactivated users.
    """
    current_role = user.get("role", "viewer").lower()
    current_user_id = user.get("id")

    # Enforce own-target visibility for sales users
    if current_role == "sales":
        if user_id is not None and user_id != current_user_id:
            raise HTTPException(
                status_code=403,
                detail="Sales representatives are restricted to viewing their own sales targets.",
            )
        user_id = current_user_id

    with get_db_session() as db:
        stmt = (
            select(SalesTarget, User)
            .join(User, SalesTarget.user_id == User.id)
            .order_by(SalesTarget.fy_code.desc(), User.id.asc())
        )

        if fy and fy.upper() not in ("ALL", "ALL FY"):
            stmt = stmt.where(SalesTarget.fy_code == fy)

        if user_id is not None:
            stmt = stmt.where(SalesTarget.user_id == user_id)

        rows = db.execute(stmt).all()
        return [_serialize_target(t, u) for t, u in rows]


@router.post("/{user_id}")
def upsert_sales_target(
    user_id: int,
    payload: TargetUpsertRequest,
    request: Request,
    user: dict = Depends(require_finance_or_admin),
):
    """
    Create or update the sales target for the specified user and financial year.
    Admin and finance only.
    """
    with get_db_session() as db:
        # Validate target user exists
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail=f"User with ID {user_id} not found.")

        # Validate FY exists
        fy_code = payload.fy_code.strip()
        fy_obj = db.query(FinancialYear).filter(FinancialYear.code == fy_code).first()
        if not fy_obj:
            raise HTTPException(
                status_code=400,
                detail=f"Financial year '{fy_code}' does not exist in master records.",
            )

        # Derive periodic values if omitted
        yearly = _quantize_2dec(payload.yearly_target)
        half_yearly = (
            _quantize_2dec(payload.half_yearly_target)
            if payload.half_yearly_target is not None
            else _quantize_2dec(yearly / Decimal("2"))
        )
        quarterly = (
            _quantize_2dec(payload.quarterly_target)
            if payload.quarterly_target is not None
            else _quantize_2dec(yearly / Decimal("4"))
        )
        monthly = (
            _quantize_2dec(payload.monthly_target)
            if payload.monthly_target is not None
            else _quantize_2dec(yearly / Decimal("12"))
        )
        m_bottom = (
            _quantize_2dec(payload.monthly_bottom_line)
            if payload.monthly_bottom_line is not None
            else Decimal("0.00")
        )
        y_bottom = (
            _quantize_2dec(payload.yearly_bottom_line)
            if payload.yearly_bottom_line is not None
            else Decimal("0.00")
        )

        # Upsert with row lock
        existing = (
            db.query(SalesTarget)
            .filter(SalesTarget.user_id == user_id, SalesTarget.fy_code == fy_code)
            .with_for_update()
            .first()
        )

        action = "TARGET_UPDATED" if existing else "TARGET_CREATED"

        if existing:
            existing.yearly_target = yearly
            existing.half_yearly_target = half_yearly
            existing.quarterly_target = quarterly
            existing.monthly_target = monthly
            existing.monthly_bottom_line = m_bottom
            existing.yearly_bottom_line = y_bottom
            target_record = existing
        else:
            target_record = SalesTarget(
                user_id=user_id,
                fy_code=fy_code,
                yearly_target=yearly,
                half_yearly_target=half_yearly,
                quarterly_target=quarterly,
                monthly_target=monthly,
                monthly_bottom_line=m_bottom,
                yearly_bottom_line=y_bottom,
            )
            db.add(target_record)

        db.flush()

        # Audit log entry
        audit_entry = AuditLog(
            user_id=user["id"],
            username=user["username"],
            action=action,
            entity_type="sales_target",
            entity_id=str(target_record.id),
            details={
                "target_user_id": user_id,
                "target_username": target_user.username,
                "fy_code": fy_code,
                "yearly_target": str(yearly),
                "action_by": user["username"],
            },
            ip_address=request.client.host if request.client else None,
            request_id=getattr(request.state, "request_id", None),
        )
        db.add(audit_entry)
        db.commit()
        db.refresh(target_record)

        return _serialize_target(target_record, target_user)
