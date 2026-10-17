"""
Payments and Financial Settlements Router for PostgreSQL Parity.

Provides:
- GET /api/payments: Authenticated payment ledger with pagination, filters, and net totals.
- POST /api/payments: Admin/finance settlement recording under invoice row lock.
- POST /api/payments/{id}/correct: Non-destructive atomic payment correction and reversal.
- DELETE /api/payments/{id}: Prohibited under statutory accounting standards.
"""

import datetime
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.database import get_db_session
from app.models.domain_models import (
    CommercialInvoice,
    Customer,
    PaymentTransaction,
    TdsSectionMaster,
    User,
)
from app.routers.auth import (
    get_current_user,
    log_security_audit,
    require_finance_or_admin,
)
from app.routers.invoices import serialize_invoice

router = APIRouter(prefix="/api/payments", tags=["Payments"])

VALID_PAYMENT_MODES = {"NEFT", "RTGS", "CHEQUE", "UPI", "CASH", "WIRE", "OTHER"}


def _quantize_2dec(val: Any) -> Decimal:
    if val is None:
        return Decimal("0.00")
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _format_money(val: Any) -> str:
    if val is None:
        return "0.00"
    return f"{Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _normalize_and_validate_tds_section(tds_section: Optional[str], db) -> str:
    if not tds_section or not tds_section.strip():
        raise HTTPException(
            status_code=400,
            detail="TDS section is required when TDS deduction is greater than zero",
        )
    raw = tds_section.strip()
    # Try exact match first
    master = db.scalar(
        select(TdsSectionMaster).where(
            TdsSectionMaster.section_code == raw,
            TdsSectionMaster.is_active == True,  # noqa: E712
        )
    )
    if master:
        return master.section_code

    # Try normalized code prefix (e.g. "194C (2%)" -> "194C")
    prefix = raw.split()[0].split("(")[0].strip()
    if prefix:
        master = db.scalar(
            select(TdsSectionMaster).where(
                TdsSectionMaster.section_code == prefix,
                TdsSectionMaster.is_active == True,  # noqa: E712
            )
        )
        if master:
            return master.section_code

    raise HTTPException(
        status_code=400,
        detail=f"Invalid or inactive TDS section '{raw}'. Must be an active section in TDS master (e.g. 194C, 194J).",
    )


class PaymentCreate(BaseModel):
    invoice_id: int = Field(..., gt=0)
    payment_amount: Optional[Decimal] = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    tds_amount: Optional[Decimal] = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    tds_section: Optional[str] = None
    payment_date: Optional[str] = None
    remittance_number: Optional[str] = None
    payment_mode: Optional[str] = "NEFT"
    notes: Optional[str] = None
    idempotency_key: Optional[str] = None


class PaymentCorrection(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)
    corrected_payment_amount: Optional[Decimal] = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    corrected_tds_amount: Optional[Decimal] = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    corrected_tds_section: Optional[str] = None
    corrected_payment_date: Optional[str] = None
    corrected_payment_mode: Optional[str] = "NEFT"
    corrected_remittance_number: Optional[str] = None
    notes: Optional[str] = None


@router.get("")
def list_payments(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    fy: Optional[str] = None,
    invoice_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    payment_mode: Optional[str] = None,
    transaction_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    with get_db_session() as db:
        stmt = (
            select(PaymentTransaction, CommercialInvoice, Customer)
            .join(CommercialInvoice, PaymentTransaction.invoice_id == CommercialInvoice.id)
            .join(Customer, CommercialInvoice.customer_id == Customer.id)
        )

        if fy and fy.upper() not in ("ALL", "ALL FY"):
            stmt = stmt.where(CommercialInvoice.fy_code == fy)
        if invoice_id is not None:
            stmt = stmt.where(PaymentTransaction.invoice_id == invoice_id)
        if customer_id is not None:
            stmt = stmt.where(CommercialInvoice.customer_id == customer_id)
        if payment_mode:
            stmt = stmt.where(PaymentTransaction.payment_mode == payment_mode.upper())
        if transaction_type:
            stmt = stmt.where(PaymentTransaction.transaction_type == transaction_type.upper())
        if date_from:
            try:
                d_from = datetime.date.fromisoformat(date_from)
                stmt = stmt.where(PaymentTransaction.payment_date >= d_from)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid date_from format")
        if date_to:
            try:
                d_to = datetime.date.fromisoformat(date_to)
                stmt = stmt.where(PaymentTransaction.payment_date <= d_to)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid date_to format")

        subq = stmt.subquery()
        count_stmt = select(func.count()).select_from(subq)
        total = db.scalar(count_stmt) or 0

        sum_stmt = select(
            func.coalesce(func.sum(subq.c.cash_amount), 0),
            func.coalesce(func.sum(subq.c.tds_amount), 0),
            func.coalesce(func.sum(subq.c.total_settled), 0),
        ).select_from(subq)
        sum_res = db.execute(sum_stmt).one()
        summary = {
            "total_cash": _format_money(sum_res[0]),
            "total_tds": _format_money(sum_res[1]),
            "total_settled": _format_money(sum_res[2]),
        }

        paginated_stmt = (
            stmt.order_by(
                PaymentTransaction.payment_date.desc(),
                PaymentTransaction.id.desc(),
            )
            .offset((page - 1) * limit)
            .limit(limit)
        )

        rows = db.execute(paginated_stmt).all()
        data = []
        for tx, inv, cust in rows:
            data.append({
                "id": tx.id,
                "invoice_id": tx.invoice_id,
                "parent_transaction_id": tx.parent_transaction_id,
                "transaction_type": tx.transaction_type,
                "cash_amount": _format_money(tx.cash_amount),
                "payment_amount": _format_money(tx.cash_amount),
                "tds_amount": _format_money(tx.tds_amount),
                "total_settled": _format_money(tx.total_settled),
                "net_settled": _format_money(tx.total_settled),
                "tds_section": tx.tds_section,
                "payment_date": tx.payment_date.isoformat() if tx.payment_date else None,
                "payment_mode": tx.payment_mode,
                "remittance_number": tx.remittance_number,
                "notes": tx.notes,
                "recorded_by": tx.recorded_by,
                "created_at": tx.created_at.isoformat() if tx.created_at else None,
                "invoice_number": inv.invoice_number,
                "customer_id": cust.id,
                "customer_name": cust.billing_name,
                "fy_code": inv.fy_code,
            })

        total_pages = math.ceil(total / limit) if limit > 0 else 1
        return {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
            "summary": summary,
            "data": data,
        }


@router.post("")
def record_payment(
    item: PaymentCreate,
    request: Request,
    user: dict = Depends(require_finance_or_admin),
):
    req_id = getattr(request.state, "request_id", None)
    idem_key = item.idempotency_key or request.headers.get("idempotency-key")
    if idem_key:
        idem_key = idem_key.strip() or None

    pay_amt = _quantize_2dec(item.payment_amount)
    tds = _quantize_2dec(item.tds_amount)
    total_settling = _quantize_2dec(pay_amt + tds)

    if pay_amt < Decimal("0.00") or tds < Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Payment and TDS amounts cannot be negative")

    if total_settling <= Decimal("0.00"):
        raise HTTPException(
            status_code=400,
            detail="Payment amount or TDS deduction must be greater than zero",
        )

    mode = (item.payment_mode or "NEFT").upper()
    if mode not in VALID_PAYMENT_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid payment mode '{mode}'. Must be one of: {', '.join(sorted(VALID_PAYMENT_MODES))}",
        )

    pay_date = item.payment_date or datetime.date.today().isoformat()
    try:
        parsed_date = datetime.date.fromisoformat(pay_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Payment date must be in valid YYYY-MM-DD format")

    remit = item.remittance_number.strip() if item.remittance_number else None

    with get_db_session() as db:
        # Check idempotency replay under transaction
        if idem_key:
            existing = db.scalar(
                select(PaymentTransaction).where(PaymentTransaction.idempotency_key == idem_key)
            )
            if existing:
                is_match = (
                    existing.invoice_id == item.invoice_id
                    and _quantize_2dec(existing.cash_amount) == pay_amt
                    and _quantize_2dec(existing.tds_amount) == tds
                    and existing.payment_mode == mode
                )
                if not is_match:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Conflicting reuse of idempotency key '{idem_key}' with different payload",
                    )
                inv_match = db.scalar(
                    select(CommercialInvoice).where(CommercialInvoice.id == existing.invoice_id)
                )
                summary = (
                    serialize_invoice(inv_match, settlements=inv_match.settlements)
                    if inv_match
                    else None
                )
                return {
                    "success": True,
                    "payment_id": existing.id,
                    "idempotent_replay": True,
                    "message": "Payment recorded (idempotent replay)",
                    "summary": summary,
                    "invoice_summary": summary,
                }

        # Validate TDS section if TDS amount > 0
        tds_sec_code = None
        if tds > Decimal("0.00"):
            tds_sec_code = _normalize_and_validate_tds_section(item.tds_section, db)

        # Lock invoice for update
        inv = db.scalar(
            select(CommercialInvoice).where(CommercialInvoice.id == item.invoice_id).with_for_update()
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")

        # Unsupported foreign currency fail-safe
        if (inv.currency or "INR").upper() != "INR":
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported foreign currency '{inv.currency}'. Only INR settlements are authorized.",
            )

        if inv.lifecycle_status == "CANCELLED":
            raise HTTPException(status_code=400, detail="Cannot record payments against a CANCELLED invoice")

        # Duplicate remittance check on active payments for this invoice
        if remit:
            dup = db.scalar(
                select(PaymentTransaction.id).where(
                    PaymentTransaction.invoice_id == item.invoice_id,
                    PaymentTransaction.remittance_number == remit,
                    PaymentTransaction.cash_amount == pay_amt,
                    PaymentTransaction.transaction_type == "PAYMENT",
                )
            )
            if dup:
                raise HTTPException(
                    status_code=400,
                    detail=f"Duplicate payment detected with remittance number '{remit}'",
                )

        # Calculate current net settlement under row lock
        net_row = db.execute(
            select(
                func.coalesce(func.sum(PaymentTransaction.cash_amount), 0),
                func.coalesce(func.sum(PaymentTransaction.tds_amount), 0),
                func.coalesce(func.sum(PaymentTransaction.total_settled), 0),
            ).where(PaymentTransaction.invoice_id == item.invoice_id)
        ).one()

        current_settled = _quantize_2dec(net_row[2])
        inv_total = _quantize_2dec(inv.total_amount)
        outstanding = _quantize_2dec(inv_total - current_settled)

        # Exact overpayment check down to ₹0.01 boundary
        if total_settling > outstanding:
            raise HTTPException(
                status_code=400,
                detail=f"Total settlement amount (Cash ₹{pay_amt:,.2f} + TDS ₹{tds:,.2f} = ₹{total_settling:,.2f}) exceeds the outstanding balance of ₹{outstanding:,.2f}",
            )

        new_tx = PaymentTransaction(
            invoice_id=inv.id,
            parent_transaction_id=None,
            transaction_type="PAYMENT",
            cash_amount=pay_amt,
            tds_amount=tds,
            total_settled=total_settling,
            tds_section=tds_sec_code,
            payment_date=parsed_date,
            remittance_number=remit,
            payment_mode=mode,
            notes=item.notes.strip() if item.notes else None,
            idempotency_key=idem_key,
            recorded_by=user["id"],
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db.add(new_tx)
        db.flush()

        log_security_audit(
            "PAYMENT_RECORDED",
            "PAYMENT",
            str(new_tx.id),
            user_id=user["id"],
            username=user["username"],
            details={
                "invoice_id": inv.id,
                "invoice_number": inv.invoice_number,
                "cash_amount": str(pay_amt),
                "tds_amount": str(tds),
                "total_settled": str(total_settling),
                "payment_mode": mode,
                "payment_date": parsed_date.isoformat(),
                "remittance_number": remit,
                "tds_section": tds_sec_code,
            },
            request_id=req_id,
            db=db,
        )
        db.commit()

        db.refresh(inv)
        updated_summary = serialize_invoice(inv, settlements=inv.settlements)

        return {
            "success": True,
            "payment_id": new_tx.id,
            "invoice_summary": updated_summary,
            "summary": updated_summary,
            "message": "Payment recorded successfully",
        }


@router.post("/{payment_id}/correct")
def correct_payment(
    payment_id: int,
    item: PaymentCorrection,
    request: Request,
    user: dict = Depends(require_finance_or_admin),
):
    req_id = getattr(request.state, "request_id", None)
    reason = item.reason.strip()
    if len(reason) < 5:
        raise HTTPException(status_code=400, detail="Correction reason must be at least 5 characters")

    corr_cash = _quantize_2dec(item.corrected_payment_amount)
    corr_tds = _quantize_2dec(item.corrected_tds_amount)
    replacement_total = _quantize_2dec(corr_cash + corr_tds)

    if corr_cash < Decimal("0.00") or corr_tds < Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Payment and TDS amounts cannot be negative")

    corr_mode = (item.corrected_payment_mode or "NEFT").upper()
    if corr_mode not in VALID_PAYMENT_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid payment mode '{corr_mode}'. Must be one of: {', '.join(sorted(VALID_PAYMENT_MODES))}",
        )

    corr_date_str = item.corrected_payment_date or datetime.date.today().isoformat()
    try:
        corr_date = datetime.date.fromisoformat(corr_date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Payment date must be in valid YYYY-MM-DD format")

    corr_remit = item.corrected_remittance_number.strip() if item.corrected_remittance_number else None

    with get_db_session() as db:
        orig = db.scalar(
            select(PaymentTransaction).where(PaymentTransaction.id == payment_id)
        )
        if not orig:
            raise HTTPException(status_code=404, detail="Payment record not found")

        if orig.transaction_type != "PAYMENT":
            raise HTTPException(status_code=400, detail="Only original PAYMENT transactions can be corrected")

        # Check supersession / prior reversal
        existing_child = db.scalar(
            select(PaymentTransaction.id).where(PaymentTransaction.parent_transaction_id == payment_id)
        )
        if existing_child:
            raise HTTPException(
                status_code=400,
                detail=f"Payment #{payment_id} has already been reversed or corrected and cannot be modified again",
            )

        # Validate replacement TDS section if TDS > 0
        corr_tds_sec = None
        if corr_tds > Decimal("0.00"):
            corr_tds_sec = _normalize_and_validate_tds_section(item.corrected_tds_section, db)

        # Row lock the invoice
        inv = db.scalar(
            select(CommercialInvoice).where(CommercialInvoice.id == orig.invoice_id).with_for_update()
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Associated commercial invoice not found")

        if inv.lifecycle_status == "CANCELLED":
            raise HTTPException(status_code=400, detail="Cannot correct payments against a CANCELLED invoice")

        # Current total settled on invoice
        current_settled_row = db.execute(
            select(
                func.coalesce(func.sum(PaymentTransaction.cash_amount), 0),
                func.coalesce(func.sum(PaymentTransaction.tds_amount), 0),
                func.coalesce(func.sum(PaymentTransaction.total_settled), 0),
            ).where(PaymentTransaction.invoice_id == inv.id)
        ).one()
        current_settled = _quantize_2dec(current_settled_row[2])
        inv_total = _quantize_2dec(inv.total_amount)

        # Net after reversing original and applying replacement
        net_after = _quantize_2dec(current_settled - orig.total_settled + replacement_total)

        if net_after < Decimal("0.00"):
            raise HTTPException(
                status_code=400,
                detail="Correction would result in negative net settlement balance",
            )
        if net_after > inv_total:
            raise HTTPException(
                status_code=400,
                detail=f"Correction would exceed invoice total. Resulting settlement ₹{net_after:,.2f} exceeds ₹{inv_total:,.2f}",
            )

        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # 1. Compensating REVERSAL event
        rev_tx = PaymentTransaction(
            invoice_id=inv.id,
            parent_transaction_id=orig.id,
            transaction_type="REVERSAL",
            cash_amount=-_quantize_2dec(orig.cash_amount),
            tds_amount=-_quantize_2dec(orig.tds_amount),
            total_settled=-_quantize_2dec(orig.total_settled),
            tds_section=orig.tds_section,
            payment_date=corr_date,
            remittance_number=orig.remittance_number,
            payment_mode=orig.payment_mode,
            notes=f"Reversal of Payment #{orig.id}: {reason}",
            recorded_by=user["id"],
            created_at=now_utc,
        )
        db.add(rev_tx)
        db.flush()

        # 2. Replacement PAYMENT event (if replacement_total > 0)
        rep_tx = None
        if replacement_total > Decimal("0.00"):
            rep_tx = PaymentTransaction(
                invoice_id=inv.id,
                parent_transaction_id=orig.id,
                transaction_type="PAYMENT",
                cash_amount=corr_cash,
                tds_amount=corr_tds,
                total_settled=replacement_total,
                tds_section=corr_tds_sec,
                payment_date=corr_date,
                remittance_number=corr_remit,
                payment_mode=corr_mode,
                notes=item.notes.strip() if item.notes else f"Replacement for Payment #{orig.id}",
                recorded_by=user["id"],
                created_at=now_utc,
            )
            db.add(rep_tx)
            db.flush()

        log_security_audit(
            "PAYMENT_CORRECTED",
            "PAYMENT",
            str(orig.id),
            user_id=user["id"],
            username=user["username"],
            details={
                "original_payment_id": orig.id,
                "reversal_id": rev_tx.id,
                "replacement_id": rep_tx.id if rep_tx else None,
                "reason": reason,
                "original_settled": str(orig.total_settled),
                "replacement_settled": str(replacement_total),
                "net_after": str(net_after),
            },
            request_id=req_id,
            db=db,
        )
        db.commit()

        db.refresh(inv)
        updated_summary = serialize_invoice(inv, settlements=inv.settlements)

        return {
            "success": True,
            "reversal_id": rev_tx.id,
            "replacement_id": rep_tx.id if rep_tx else None,
            "invoice_summary": updated_summary,
            "summary": updated_summary,
            "message": (
                f"Payment #{payment_id} corrected successfully"
                if rep_tx
                else f"Payment #{payment_id} fully reversed successfully"
            ),
        }


@router.delete("/{payment_id}")
def delete_payment(
    payment_id: int,
    user: dict = Depends(require_finance_or_admin),
):
    raise HTTPException(
        status_code=400,
        detail="Physical deletion of payment records is prohibited. Use POST /api/payments/{id}/correct to reverse or adjust payments.",
    )


@router.get("/recent")
def list_recent_payments(
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    return list_payments(page=1, limit=limit, user=user)
