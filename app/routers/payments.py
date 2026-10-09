import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.database import (
    execute_write,
    log_audit,
    query_all,
    query_one,
    transaction,
)
from app.routers.auth import get_current_user, require_finance_or_admin
from app.utils.financial import round_currency

router = APIRouter(prefix="/api/payments", tags=["Payments"])

VALID_PAYMENT_MODES = {"NEFT", "RTGS", "CHEQUE", "UPI", "CASH", "WIRE", "OTHER"}


class PaymentCreate(BaseModel):
    invoice_id: int
    payment_amount: float = Field(default=0.0, ge=0.0)
    tds_amount: Optional[float] = Field(default=0.0, ge=0.0)
    tds_section: Optional[str] = None
    payment_date: Optional[str] = None
    remittance_number: Optional[str] = None
    payment_mode: Optional[str] = "NEFT"
    notes: Optional[str] = None
    idempotency_key: Optional[str] = None


@router.post("")
def record_payment(item: PaymentCreate, request: Request, user: dict = Depends(require_finance_or_admin)):
    req_id = getattr(request.state, "request_id", None)
    idem_key = item.idempotency_key or request.headers.get("idempotency-key")
    if idem_key:
        idem_key = idem_key.strip() or None

    pay_amt = round_currency(item.payment_amount)
    tds = round_currency(item.tds_amount or 0.0)
    total_settling = round_currency(pay_amt + tds)

    if pay_amt < 0 or tds < 0:
        raise HTTPException(status_code=400, detail="Payment and TDS amounts cannot be negative")

    if total_settling <= 0:
        raise HTTPException(status_code=400, detail="Payment amount or TDS deduction must be greater than zero")

    pay_date = item.payment_date or datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(pay_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Payment date must be in valid YYYY-MM-DD format")

    mode = (item.payment_mode or "NEFT").upper()
    remit = item.remittance_number.strip() if item.remittance_number else None

    # Execute inside an immediate transaction to eliminate race conditions
    with transaction() as conn:
        # Check idempotency replay under lock
        if idem_key:
            existing_pay = query_one(
                "SELECT id, invoice_id, payment_amount, tds_amount FROM payments WHERE idempotency_key = ?",
                (idem_key,),
                conn=conn,
            )
            if existing_pay:
                rep_summary = query_one(
                    "SELECT total_amount, total_paid, total_cash_paid, total_tds_deducted, outstanding_balance, computed_status FROM v_invoice_summary WHERE id = ?",
                    (existing_pay["invoice_id"],),
                    conn=conn,
                )
                return {
                    "success": True,
                    "payment_id": existing_pay["id"],
                    "idempotent_replay": True,
                    "message": "Payment recorded (idempotent replay)",
                    "summary": rep_summary,
                }

        inv = query_one(
            "SELECT id, invoice_number, po_number, total_amount, payment_status FROM invoices WHERE id = ?",
            (item.invoice_id,),
            conn=conn,
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")

        if inv.get("payment_status") == "CANCELLED":
            raise HTTPException(status_code=400, detail="Cannot record payments against a CANCELLED invoice")

        # Duplicate check on remittance number
        if remit:
            dup = query_one(
                "SELECT id FROM payments WHERE invoice_id = ? AND remittance_number = ? AND payment_amount = ?",
                (item.invoice_id, remit, pay_amt),
                conn=conn,
            )
            if dup:
                raise HTTPException(
                    status_code=400, detail=f"Duplicate payment detected with remittance number '{remit}'"
                )

        # Re-compute outstanding balance inside the write transaction
        paid_row = query_one(
            "SELECT COALESCE(SUM(payment_amount), 0) as cash, COALESCE(SUM(tds_amount), 0) as tds FROM payments WHERE invoice_id = ?",
            (item.invoice_id,),
            conn=conn,
        )
        current_settled = round_currency(paid_row["cash"] + paid_row["tds"])
        total_inv = round_currency(inv["total_amount"])
        outstanding = round_currency(total_inv - current_settled)

        # Strict Decimal equality check without arbitrary tolerances
        if total_settling > outstanding:
            raise HTTPException(
                status_code=400,
                detail=f"Total settlement amount (Cash ₹{pay_amt:,.2f} + TDS ₹{tds:,.2f} = ₹{total_settling:,.2f}) exceeds the outstanding balance of ₹{outstanding:,.2f}",
            )

        sql = """
        INSERT INTO payments (invoice_id, payment_amount, tds_amount, tds_section, payment_date, remittance_number, payment_mode, notes, idempotency_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        pay_id = execute_write(
            sql,
            (item.invoice_id, pay_amt, tds, item.tds_section, pay_date, remit, mode, item.notes, idem_key),
            conn=conn,
        )

        inv_label = inv["invoice_number"] or inv["po_number"] or f"#{inv['id']}"
        log_audit(
            "PAYMENT_RECORDED",
            "PAYMENT",
            pay_id,
            f"Recorded payment of ₹{pay_amt} (TDS: ₹{tds}) against {inv_label}",
            user=user,
            request_id=req_id,
            conn=conn,
        )

    # Fetch updated summary
    updated_summary = query_one(
        "SELECT total_amount, total_paid, total_cash_paid, total_tds_deducted, outstanding_balance, computed_status FROM v_invoice_summary WHERE id = ?",
        (item.invoice_id,),
    )

    return {
        "success": True,
        "payment_id": pay_id,
        "invoice_summary": updated_summary,
        "message": "Payment recorded successfully",
    }


@router.delete("/{payment_id}")
def delete_payment(payment_id: int, request: Request, user: dict = Depends(require_finance_or_admin)):
    req_id = getattr(request.state, "request_id", None)
    with transaction() as conn:
        payment = query_one("SELECT * FROM payments WHERE id = ?", (payment_id,), conn=conn)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment record not found")

        execute_write("DELETE FROM payments WHERE id = ?", (payment_id,), conn=conn)
        log_audit(
            "PAYMENT_DELETED",
            "PAYMENT",
            payment_id,
            f"Deleted payment of ₹{payment['payment_amount']} (TDS: ₹{payment['tds_amount']}) for Invoice #{payment['invoice_id']}",
            user=user,
            request_id=req_id,
            conn=conn,
        )

    updated_summary = query_one(
        "SELECT total_amount, total_paid, total_cash_paid, total_tds_deducted, outstanding_balance, computed_status FROM v_invoice_summary WHERE id = ?",
        (payment["invoice_id"],),
    )

    return {
        "success": True,
        "invoice_summary": updated_summary,
        "message": f"Payment #{payment_id} reversed/deleted successfully",
    }


@router.get("/recent")
def list_recent_payments(limit: int = Query(20, ge=1, le=100), user: dict = Depends(get_current_user)):
    query = """
    SELECT
        p.id,
        p.invoice_id,
        p.payment_amount,
        p.tds_amount,
        p.tds_section,
        p.payment_date,
        p.remittance_number,
        p.payment_mode,
        i.invoice_number,
        i.po_number,
        c.billing_name as customer_name
    FROM payments p
    JOIN invoices i ON p.invoice_id = i.id
    JOIN customers c ON i.customer_id = c.id
    ORDER BY p.payment_date DESC, p.id DESC
    LIMIT ?;
    """
    return query_all(query, (limit,))
