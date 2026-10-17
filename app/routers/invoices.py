"""
Commercial Invoices Domain Router for PostgreSQL Parity.

Provides:
- List commercial invoices with pagination, search, financial-year and lifecycle/payment status filtering.
- Retrieve commercial invoice detail with line items and settlement history.
- Direct commercial invoice creation with immutable customer snapshot and statutory GST split (admin, finance).
- Edit commercial invoice metadata (remarks, due_date, executive) with row-level locking (admin, finance).
- Cancel commercial invoice with mandatory reason and settlement protection guard (admin, finance).
- Permanent deletion rejection protecting issued commercial invoice auditability.
"""

import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.core.database import get_db_session
from app.models.domain_models import (
    CommercialInvoice,
    CommercialInvoiceItem,
    Customer,
    FinancialYear,
    PaymentTransaction,
    PurchaseOrder,
    User,
)
from app.routers.auth import (
    get_current_user,
    log_security_audit,
    require_finance_or_admin,
)
from app.utils.financial import (
    ALLOWED_CURRENCIES,
    calculate_line_item,
    calculate_net_30_due_date,
    is_date_in_fiscal_year,
)

router = APIRouter(prefix="/api/invoices", tags=["Invoices"])


class InvoiceItemCreate(BaseModel):
    description: Optional[str] = None
    item_description: Optional[str] = None
    quantity: float = Field(default=1.0, gt=0)
    unit_price: float = Field(default=0.0, ge=0.0)
    tax_rate: float = Field(default=0.18, ge=0.0, le=1.0)

    @property
    def desc(self) -> str:
        return (self.description or self.item_description or "Standard Line Item").strip()


def _format_money(val: Any) -> str:
    if val is None:
        return "0.00"
    return f"{Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


class InvoiceCreate(BaseModel):
    fy_code: str = Field(min_length=1, max_length=20)
    invoice_number: str = Field(min_length=1, max_length=50)
    invoice_date: str  # ISO date string (YYYY-MM-DD)
    due_date: Optional[str] = None  # Defaults to Net 30 if omitted
    customer_id: int
    executive_id: Optional[int] = None
    executive_name: Optional[str] = None
    order_type: Optional[str] = "RECURRING"
    po_number: Optional[str] = None
    po_date: Optional[str] = None
    currency: Optional[str] = "INR"
    exchange_rate: Optional[float] = Field(default=1.0, gt=0)
    exclusive_amount: Optional[float] = None
    tax_rate: Optional[float] = Field(default=0.18, ge=0.0, le=1.0)
    tax_amount: Optional[float] = None
    total_amount: Optional[float] = None
    remarks: Optional[str] = None
    items: Optional[List[InvoiceItemCreate]] = None


class InvoiceUpdate(BaseModel):
    remarks: Optional[str] = None
    due_date: Optional[str] = None
    executive_id: Optional[int] = None


class InvoiceCancel(BaseModel):
    reason: str = Field(min_length=3, max_length=500, description="Mandatory reason for statutory cancellation")


def _serialize_invoice(
    inv: CommercialInvoice,
    settlements: Optional[List[PaymentTransaction]] = None,
    items: Optional[List[CommercialInvoiceItem]] = None,
    po: Optional[PurchaseOrder] = None,
    executive: Optional[User] = None,
) -> Dict[str, Any]:
    exec_user = executive or inv.executive
    e_name = (exec_user.full_name or exec_user.username) if exec_user else None

    # Line items
    line_items = [
        {
            "id": it.id,
            "invoice_id": it.invoice_id,
            "description": it.item_description,
            "quantity": float(it.quantity),
            "unit_price": _format_money(it.unit_price),
            "exclusive_amount": _format_money(it.exclusive_amount),
            "tax_rate": f"{Decimal(str(it.tax_rate)):.4f}",
            "tax_amount": _format_money(it.tax_amount),
            "total_amount": _format_money(it.total_amount),
        }
        for it in (items if items is not None else (inv.items or []))
    ]

    # Settlements
    txs = settlements if settlements is not None else (inv.settlements or [])
    payments_list = [
        {
            "id": tx.id,
            "invoice_id": tx.invoice_id,
            "payment_amount": _format_money(tx.cash_amount),
            "cash_amount": _format_money(tx.cash_amount),
            "tds_amount": _format_money(tx.tds_amount),
            "total_settled": _format_money(tx.total_settled),
            "tds_section": tx.tds_section,
            "payment_date": tx.payment_date.isoformat() if tx.payment_date else None,
            "payment_mode": tx.payment_mode,
            "remittance_number": tx.remittance_number,
            "notes": tx.notes,
            "created_at": tx.created_at.isoformat() if tx.created_at else None,
        }
        for tx in txs
    ]

    # Financial settlement aggregation
    cash_paid = sum((Decimal(str(tx.cash_amount)) for tx in txs), Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tds_deducted = sum((Decimal(str(tx.tds_amount)) for tx in txs), Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total_paid = sum((Decimal(str(tx.total_settled)) for tx in txs), Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    inv_total = Decimal(str(inv.total_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    today = datetime.date.today()
    days_overdue = 0

    if inv.lifecycle_status == "CANCELLED":
        outstanding_balance = Decimal("0.00")
        payment_status = "CANCELLED"
        computed_status = "CANCELLED"
    else:
        outstanding_balance = max(Decimal("0.00"), inv_total - total_paid).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if total_paid <= Decimal("0.00"):
            payment_status = "PENDING"
            if inv.due_date and inv.due_date < today:
                computed_status = "OVERDUE"
                days_overdue = (today - inv.due_date).days
            else:
                computed_status = "PENDING"
        elif total_paid >= inv_total:
            payment_status = "PAID"
            computed_status = "PAID"
        else:
            payment_status = "PARTIAL"
            computed_status = "PARTIAL"
            if inv.due_date and inv.due_date < today:
                days_overdue = (today - inv.due_date).days

    po_num = po.po_number if po else (inv.remarks if inv.po_id is None else None)
    po_dt = po.po_date.isoformat() if po and po.po_date else None

    return {
        "id": inv.id,
        "fy_code": inv.fy_code,
        "order_type": "PO" if inv.po_id else "RECURRING",
        "po_id": inv.po_id,
        "po_number": po_num,
        "po_date": po_dt,
        "customer_id": inv.customer_id,
        "customer_name": inv.snapshot_customer_name,
        "customer_gstin": inv.snapshot_gstin,
        "customer_address": inv.snapshot_billing_address,
        "customer_state": inv.snapshot_state,
        "executive_id": inv.executive_id,
        "executive_name": e_name,
        "currency": inv.currency,
        "exchange_rate": float(inv.exchange_rate),
        "original_amount": _format_money(inv.exclusive_amount),
        "exclusive_amount": _format_money(inv.exclusive_amount),
        "tax_rate": f"{Decimal(str(inv.tax_rate)):.4f}",
        "tax_amount": _format_money(inv.tax_amount),
        "total_amount": _format_money(inv_total),
        "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "days_overdue": days_overdue,
        "remarks": inv.remarks,
        "cgst_amount": _format_money(inv.cgst_amount),
        "sgst_amount": _format_money(inv.sgst_amount),
        "igst_amount": _format_money(inv.igst_amount),
        "lifecycle_status": inv.lifecycle_status,
        "cancellation_reason": inv.cancellation_reason,
        "cancelled_at": inv.cancelled_at.isoformat() if inv.cancelled_at else None,
        "cancelled_by": inv.cancelled_by,
        "payment_status": payment_status,
        "computed_status": computed_status,
        "total_cash_paid": _format_money(cash_paid),
        "total_tds_deducted": _format_money(tds_deducted),
        "total_paid": _format_money(total_paid),
        "outstanding_balance": _format_money(outstanding_balance),
        "line_items": line_items,
        "payments": payments_list,
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "updated_at": inv.updated_at.isoformat() if inv.updated_at else None,
    }


def serialize_invoice(
    inv: CommercialInvoice,
    customer: Optional[Customer] = None,
    executive: Optional[User] = None,
    po: Optional[PurchaseOrder] = None,
    settlements: Optional[List[PaymentTransaction]] = None,
    items: Optional[List[CommercialInvoiceItem]] = None,
) -> Dict[str, Any]:
    return _serialize_invoice(
        inv,
        settlements=settlements,
        items=items,
        po=po,
        executive=executive,
    )


@router.get("")
def list_invoices(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    search: Optional[str] = None,
    fy: Optional[str] = None,
    status: Optional[str] = None,
    customer_id: Optional[int] = None,
    executive_id: Optional[int] = None,
    user: dict = Depends(get_current_user),
):
    """
    List commercial invoices with pagination, text search, FY filter and settlement-aware status filter.
    """
    with get_db_session() as db:
        query = select(CommercialInvoice)

        if search and search.strip():
            term = f"%{search.strip()}%"
            query = query.where(
                or_(
                    CommercialInvoice.invoice_number.ilike(term),
                    CommercialInvoice.snapshot_customer_name.ilike(term),
                )
            )

        if fy and fy.strip() and fy.upper() != "ALL":
            query = query.where(CommercialInvoice.fy_code == fy.strip())

        if customer_id:
            query = query.where(CommercialInvoice.customer_id == customer_id)

        if executive_id:
            query = query.where(CommercialInvoice.executive_id == executive_id)

        # Base ordering: descending id
        query = query.order_by(CommercialInvoice.id.desc())

        # Fetch records and associated relationships
        all_invoices = db.scalars(query).all()

        # In-memory settlement calculation and status filtering
        serialized_records = []
        for inv in all_invoices:
            # Load settlements, items, po, executive
            settlements = db.scalars(
                select(PaymentTransaction).where(PaymentTransaction.invoice_id == inv.id).order_by(PaymentTransaction.payment_date.asc())
            ).all()
            items = db.scalars(
                select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == inv.id).order_by(CommercialInvoiceItem.id.asc())
            ).all()
            po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == inv.po_id)) if inv.po_id else None
            executive = db.scalar(select(User).where(User.id == inv.executive_id))

            ser = _serialize_invoice(inv, settlements=settlements, items=items, po=po, executive=executive)

            if status and status.strip() and status.upper() != "ALL":
                st = status.strip().upper()
                comp_st = ser["computed_status"].upper()
                pay_st = ser["payment_status"].upper()
                if st == "CANCELLED" and inv.lifecycle_status != "CANCELLED":
                    continue
                elif st in ("PARTIAL", "PARTIALLY_PAID") and comp_st not in ("PARTIAL", "PARTIALLY_PAID"):
                    continue
                elif st in ("PAID", "PENDING", "OVERDUE") and comp_st != st and pay_st != st:
                    continue

            serialized_records.append(ser)

        total_count = len(serialized_records)
        offset = (page - 1) * limit
        paginated_data = serialized_records[offset : offset + limit]

        return {
            "page": page,
            "limit": limit,
            "total": total_count,
            "total_pages": (total_count + limit - 1) // limit if total_count > 0 else 1,
            "data": paginated_data,
        }


@router.get("/{invoice_id}")
def get_invoice(invoice_id: int, user: dict = Depends(get_current_user)):
    """Retrieve full commercial invoice detail with line items and settlement ledger."""
    with get_db_session() as db:
        inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.id == invoice_id))
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")

        settlements = db.scalars(
            select(PaymentTransaction).where(PaymentTransaction.invoice_id == inv.id).order_by(PaymentTransaction.payment_date.asc())
        ).all()
        items = db.scalars(
            select(CommercialInvoiceItem).where(CommercialInvoiceItem.invoice_id == inv.id).order_by(CommercialInvoiceItem.id.asc())
        ).all()
        po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == inv.po_id)) if inv.po_id else None
        executive = db.scalar(select(User).where(User.id == inv.executive_id))

        ser = _serialize_invoice(inv, settlements=settlements, items=items, po=po, executive=executive)

        return {
            "invoice": ser,
            "payments": ser["payments"],
            "items": ser["line_items"],
        }


@router.post("", status_code=201)
def create_invoice(
    payload: InvoiceCreate,
    request: Request,
    user: dict = Depends(require_finance_or_admin),
):
    """
    Direct Commercial Invoice creation (Finance/Admin).
    Applies immutable customer snapshot, calculates statutory GST allocation, and enforces FY date boundaries.
    """
    req_id = getattr(request.state, "request_id", None) if request else None

    with get_db_session() as db:
        # 1. Validate Customer
        cust = db.scalar(select(Customer).where(Customer.id == payload.customer_id))
        if not cust:
            raise HTTPException(status_code=400, detail="Specified customer does not exist")
        if not cust.is_active:
            raise HTTPException(status_code=400, detail="Cannot issue invoice to an inactive customer")

        # 2. Validate Financial Year
        fy_master = db.scalar(select(FinancialYear).where(FinancialYear.code == payload.fy_code))
        if not fy_master:
            raise HTTPException(
                status_code=400,
                detail=f"Financial year '{payload.fy_code}' does not exist in master records",
            )
        if not fy_master.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"Financial year '{payload.fy_code}' is locked or inactive",
            )

        # 3. Validate Invoice Date
        try:
            inv_date = datetime.date.fromisoformat(payload.invoice_date.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid invoice_date format. Must be YYYY-MM-DD")

        if not is_date_in_fiscal_year(payload.invoice_date, fy_master.start_date.isoformat(), fy_master.end_date.isoformat()):
            raise HTTPException(
                status_code=400,
                detail=f"Invoice date '{payload.invoice_date}' falls outside financial year '{payload.fy_code}' ({fy_master.start_date} to {fy_master.end_date})",
            )

        # 4. Validate Currency & Exchange Rate
        curr = (payload.currency or "INR").upper().strip()
        if curr not in ALLOWED_CURRENCIES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported currency: {curr}. Must be one of {', '.join(sorted(ALLOWED_CURRENCIES))}",
            )
        rate = Decimal(str(payload.exchange_rate or 1.0))
        if rate <= 0:
            raise HTTPException(status_code=400, detail="Exchange rate must be greater than zero")
        if curr == "INR":
            rate = Decimal("1.000000")

        # 5. Check Duplicate Invoice Number (case-insensitive)
        inv_num = payload.invoice_number.strip()
        dup_inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.invoice_number.ilike(inv_num)))
        if dup_inv:
            raise HTTPException(
                status_code=400,
                detail=f"Invoice number '{inv_num}' is already assigned to record #{dup_inv.id}",
            )

        # 6. Resolve Executive
        exec_id = payload.executive_id
        if not exec_id and payload.executive_name and payload.executive_name.strip():
            ename = payload.executive_name.strip().lower()
            exec_user = db.scalar(
                select(User).where(
                    User.role == "sales",
                    or_(func.lower(User.full_name) == ename, func.lower(User.username) == ename),
                )
            )
            if exec_user:
                exec_id = exec_user.id

        if exec_id:
            exec_check = db.scalar(select(User).where(User.id == exec_id, User.role == "sales"))
            if not exec_check:
                raise HTTPException(status_code=400, detail="Assigned executive must be an active sales executive")
        else:
            # Default to current user if not specified
            exec_id = user["id"]

        # 7. Calculate Due Date (Net-30 default)
        if payload.due_date and payload.due_date.strip():
            try:
                due_date_val = datetime.date.fromisoformat(payload.due_date.strip())
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid due_date format. Must be YYYY-MM-DD")
        else:
            calc_due = calculate_net_30_due_date(payload.invoice_date)
            due_date_val = datetime.date.fromisoformat(calc_due)

        if due_date_val < inv_date:
            raise HTTPException(status_code=400, detail="Due date cannot precede invoice date")

        # 8. Calculate Line Items & Financial Totals
        computed_items = []
        total_excl = Decimal("0.00")
        total_tax = Decimal("0.00")

        if payload.items and len(payload.items) > 0:
            for item in payload.items:
                qty = Decimal(str(item.quantity))
                uprice = Decimal(str(item.unit_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                trate = Decimal(str(item.tax_rate))

                d_excl = (qty * uprice).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                d_tax = (d_excl * trate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                d_tot = (d_excl + d_tax).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                computed_items.append({
                    "description": item.desc,
                    "quantity": qty,
                    "unit_price": uprice,
                    "exclusive_amount": d_excl,
                    "tax_rate": trate,
                    "tax_amount": d_tax,
                    "total_amount": d_tot,
                })
                total_excl += d_excl
                total_tax += d_tax
            inv_tax_rate = Decimal(str(payload.tax_rate if payload.tax_rate is not None else (computed_items[0]["tax_rate"] if computed_items else 0.18)))
        else:
            if payload.exclusive_amount is None:
                raise HTTPException(status_code=400, detail="Either line items or exclusive_amount must be provided")
            d_excl = Decimal(str(payload.exclusive_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            inv_tax_rate = Decimal(str(payload.tax_rate if payload.tax_rate is not None else 0.18))
            d_tax = (d_excl * inv_tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            d_tot = (d_excl + d_tax).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            computed_items.append({
                "description": payload.remarks or "Standard Line Item",
                "quantity": Decimal("1.0000"),
                "unit_price": d_excl,
                "exclusive_amount": d_excl,
                "tax_rate": inv_tax_rate,
                "tax_amount": d_tax,
                "total_amount": d_tot,
            })
            total_excl = d_excl
            total_tax = d_tax

        total_amount_val = total_excl + total_tax

        # CF4C3-01: Inconsistency checks against caller-supplied amounts (strictly 0 tolerance after quantization)
        if payload.exclusive_amount is not None and payload.items and len(payload.items) > 0:
            supplied_excl = Decimal(str(payload.exclusive_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if supplied_excl != total_excl:
                raise HTTPException(
                    status_code=400,
                    detail=f"Supplied exclusive_amount ({supplied_excl}) is inconsistent with line-item total ({total_excl})",
                )

        if payload.tax_amount is not None:
            supplied_tax = Decimal(str(payload.tax_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if supplied_tax != total_tax:
                raise HTTPException(
                    status_code=400,
                    detail=f"Supplied tax_amount ({supplied_tax}) is inconsistent with authoritative tax calculation ({total_tax})",
                )

        if payload.total_amount is not None:
            supplied_total = Decimal(str(payload.total_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if supplied_total != total_amount_val:
                raise HTTPException(
                    status_code=400,
                    detail=f"Supplied total_amount ({supplied_total}) is inconsistent with authoritative total ({total_amount_val})",
                )

        # 9. Immutable Customer Snapshot
        snapshot_name = cust.billing_name
        snapshot_gstin = cust.gst_number
        snapshot_addr = cust.billing_address or "Address on file"
        snapshot_state = cust.state or "Karnataka"

        # 10. Statutory GST Allocation (CGST/SGST vs IGST)
        is_intra = snapshot_state.strip().lower() in ("karnataka", "ka", "29")
        if is_intra:
            cgst = (total_tax / Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            sgst = (total_tax - cgst).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            igst = Decimal("0.00")
        else:
            cgst = Decimal("0.00")
            sgst = Decimal("0.00")
            igst = total_tax

        # 11. Create CommercialInvoice
        new_inv = CommercialInvoice(
            po_id=None,
            fy_code=payload.fy_code,
            customer_id=cust.id,
            executive_id=exec_id,
            invoice_number=inv_num,
            invoice_date=inv_date,
            due_date=due_date_val,
            currency=curr,
            exchange_rate=rate,
            exclusive_amount=total_excl,
            tax_rate=inv_tax_rate,
            tax_amount=total_tax,
            total_amount=total_amount_val,
            snapshot_customer_name=snapshot_name,
            snapshot_gstin=snapshot_gstin,
            snapshot_billing_address=snapshot_addr,
            snapshot_state=snapshot_state,
            cgst_amount=cgst,
            sgst_amount=sgst,
            igst_amount=igst,
            lifecycle_status="ISSUED",
            remarks=payload.remarks,
        )
        db.add(new_inv)
        db.flush()

        # 12. Create CommercialInvoiceItems
        for itm in computed_items:
            db_item = CommercialInvoiceItem(
                invoice_id=new_inv.id,
                item_description=itm["description"],
                quantity=itm["quantity"],
                unit_price=itm["unit_price"],
                exclusive_amount=itm["exclusive_amount"],
                tax_rate=itm["tax_rate"],
                tax_amount=itm["tax_amount"],
                total_amount=itm["total_amount"],
            )
            db.add(db_item)
        db.flush()

        # 13. Atomic Security Audit Log
        log_security_audit(
            "COMMERCIAL_INVOICE_CREATED",
            "COMMERCIAL_INVOICE",
            str(new_inv.id),
            user_id=user["id"],
            username=user["username"],
            details={
                "invoice_number": inv_num,
                "customer_id": cust.id,
                "total_amount": float(total_amount_val),
                "cgst_amount": float(cgst),
                "sgst_amount": float(sgst),
                "igst_amount": float(igst),
            },
            request_id=req_id,
            db=db,
        )
        db.commit()

        return {
            "success": True,
            "id": new_inv.id,
            "invoice_number": inv_num,
            "message": f"Invoice #{inv_num} created successfully",
        }


@router.put("/{invoice_id}")
def update_invoice_metadata(
    invoice_id: int,
    payload: InvoiceUpdate,
    request: Request,
    user: dict = Depends(require_finance_or_admin),
):
    """
    Update non-financial metadata on an issued invoice (remarks, due_date, executive_id).
    Financial amounts, customer snapshot, line items and GST splits are immutable.
    """
    req_id = getattr(request.state, "request_id", None) if request else None

    with get_db_session() as db:
        inv = db.scalar(
            select(CommercialInvoice).where(CommercialInvoice.id == invoice_id).with_for_update()
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")

        if inv.lifecycle_status == "CANCELLED":
            raise HTTPException(status_code=400, detail="Cannot edit metadata of a cancelled invoice")

        changes = {}

        if payload.remarks is not None:
            changes["remarks"] = {"old": inv.remarks, "new": payload.remarks}
            inv.remarks = payload.remarks

        if payload.due_date is not None:
            try:
                new_due = datetime.date.fromisoformat(payload.due_date.strip())
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid due_date format. Must be YYYY-MM-DD")
            if new_due < inv.invoice_date:
                raise HTTPException(status_code=400, detail="Due date cannot precede invoice date")
            changes["due_date"] = {"old": inv.due_date.isoformat(), "new": payload.due_date}
            inv.due_date = new_due

        if payload.executive_id is not None:
            exec_check = db.scalar(select(User).where(User.id == payload.executive_id, User.role == "sales"))
            if not exec_check:
                raise HTTPException(status_code=400, detail="Assigned executive must be a sales executive")
            changes["executive_id"] = {"old": inv.executive_id, "new": payload.executive_id}
            inv.executive_id = payload.executive_id

        inv.updated_at = datetime.datetime.now(datetime.timezone.utc)
        db.flush()

        log_security_audit(
            "COMMERCIAL_INVOICE_METADATA_UPDATED",
            "COMMERCIAL_INVOICE",
            str(inv.id),
            user_id=user["id"],
            username=user["username"],
            details={"changes": changes},
            request_id=req_id,
            db=db,
        )
        db.commit()

        return {"success": True, "message": f"Invoice #{inv.invoice_number} metadata updated successfully"}


@router.post("/{invoice_id}/cancel")
def cancel_invoice(
    invoice_id: int,
    payload: InvoiceCancel,
    request: Request,
    user: dict = Depends(require_finance_or_admin),
):
    """
    Statutory Invoice Cancellation (Finance/Admin).
    Enforces row lock, mandatory reason, and settlement guard (zero recorded payment settlements).
    """
    req_id = getattr(request.state, "request_id", None) if request else None

    with get_db_session() as db:
        inv = db.scalar(
            select(CommercialInvoice).where(CommercialInvoice.id == invoice_id).with_for_update()
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")

        if inv.lifecycle_status == "CANCELLED":
            raise HTTPException(status_code=400, detail="Invoice is already cancelled")

        # Settlement guard: check if active positive net settlement exists
        net_settled = db.scalar(
            select(func.coalesce(func.sum(PaymentTransaction.total_settled), 0)).where(
                PaymentTransaction.invoice_id == invoice_id
            )
        ) or Decimal("0.00")
        net_cash = db.scalar(
            select(func.coalesce(func.sum(PaymentTransaction.cash_amount), 0)).where(
                PaymentTransaction.invoice_id == invoice_id
            )
        ) or Decimal("0.00")
        net_tds = db.scalar(
            select(func.coalesce(func.sum(PaymentTransaction.tds_amount), 0)).where(
                PaymentTransaction.invoice_id == invoice_id
            )
        ) or Decimal("0.00")

        net_settled_dec = Decimal(str(net_settled)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        net_cash_dec = Decimal(str(net_cash)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        net_tds_dec = Decimal(str(net_tds)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if net_settled_dec > Decimal("0.00") or net_cash_dec > Decimal("0.00") or net_tds_dec > Decimal("0.00"):
            raise HTTPException(
                status_code=400,
                detail="Cannot cancel invoice with recorded settlements. Settlements must be reversed or cleared first.",
            )

        inv.lifecycle_status = "CANCELLED"
        inv.cancellation_reason = payload.reason.strip()
        inv.cancelled_at = datetime.datetime.now(datetime.timezone.utc)
        inv.cancelled_by = user["id"]
        inv.remarks = f"{inv.remarks}\n[CANCELLED: {payload.reason.strip()}]" if inv.remarks else f"[CANCELLED: {payload.reason.strip()}]"
        inv.updated_at = datetime.datetime.now(datetime.timezone.utc)
        db.flush()

        log_security_audit(
            "COMMERCIAL_INVOICE_CANCELLED",
            "COMMERCIAL_INVOICE",
            str(inv.id),
            user_id=user["id"],
            username=user["username"],
            details={
                "invoice_number": inv.invoice_number,
                "reason": payload.reason.strip(),
            },
            request_id=req_id,
            db=db,
        )
        db.commit()

        return {
            "success": True,
            "message": f"Invoice #{inv.invoice_number} successfully cancelled",
            "lifecycle_status": "CANCELLED",
        }


@router.delete("/{invoice_id}")
def delete_invoice(
    invoice_id: int,
    user: dict = Depends(require_finance_or_admin),
):
    """
    Physical deletion of issued commercial invoices is strictly prohibited.
    Statutory accounting standards require cancellation workflow.
    """
    raise HTTPException(
        status_code=400,
        detail="Issued commercial invoices cannot be permanently deleted. Use the cancellation workflow (POST /api/invoices/{id}/cancel) instead.",
    )
