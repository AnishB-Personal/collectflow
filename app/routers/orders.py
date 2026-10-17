"""
Purchase Orders Domain Router for PostgreSQL Parity.

Provides:
- List purchase orders with pagination, search, financial-year and status filtering.
- Retrieve purchase order detail with line items.
- Register/create purchase orders with line items and financial totals (admin, finance, sales).
- Edit unbilled purchase orders (admin, finance, sales). Rejects converted or cancelled orders.
- Delete unbilled purchase orders (admin, finance). Rejects converted or cancelled orders.
- Convert unbilled purchase orders to issued commercial invoices with immutable customer snapshot (admin, finance).
"""

import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.database import get_db_session
from app.models.domain_models import (
    CommercialInvoice,
    CommercialInvoiceItem,
    Customer,
    FinancialYear,
    PurchaseOrder,
    PurchaseOrderItem,
    User,
)
from app.routers.auth import (
    get_current_user,
    log_security_audit,
    require_finance_or_admin,
    require_sales_finance_admin,
)
from app.utils.financial import (
    calculate_net_30_due_date,
    is_date_in_fiscal_year,
)

router = APIRouter(prefix="/api/orders", tags=["Orders"])


class OrderItemCreate(BaseModel):
    item_description: Optional[str] = None
    description: Optional[str] = None
    quantity: float = Field(default=1.0, gt=0)
    unit_price: float = Field(default=0.0, ge=0.0)
    tax_rate: float = Field(default=0.18, ge=0.0, le=1.0)

    @property
    def desc(self) -> str:
        return (self.item_description or self.description or "Standard Line Item").strip()


def _format_money(val: Any) -> str:
    if val is None:
        return "0.00"
    return f"{Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


class OrderCreate(BaseModel):
    fy_code: str = Field(min_length=1, max_length=20)
    po_number: Optional[str] = Field(default=None, max_length=50)
    po_date: str  # ISO date string (YYYY-MM-DD)
    customer_id: int
    executive_id: Optional[int] = None
    order_type: Optional[str] = "PO"
    currency: Optional[str] = "INR"
    exchange_rate: Optional[float] = Field(default=1.0, gt=0)
    exclusive_amount: Optional[float] = None
    tax_rate: Optional[float] = Field(default=0.18, ge=0.0, le=1.0)
    tax_amount: Optional[float] = None
    total_amount: Optional[float] = None
    remarks: Optional[str] = None
    items: Optional[List[OrderItemCreate]] = None
    line_item_description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None


class OrderUpdate(BaseModel):
    po_number: Optional[str] = None
    po_date: Optional[str] = None
    customer_id: Optional[int] = None
    executive_id: Optional[int] = None
    currency: Optional[str] = None
    exchange_rate: Optional[float] = None
    remarks: Optional[str] = None
    items: Optional[List[OrderItemCreate]] = None
    line_item_description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    tax_rate: Optional[float] = None


class OrderConvert(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=50)
    invoice_date: str  # ISO date string (YYYY-MM-DD)
    due_date: Optional[str] = None  # Defaults to Net 30 if omitted


def _serialize_order(po: PurchaseOrder, customer: Optional[Customer] = None, executive: Optional[User] = None) -> Dict[str, Any]:
    c_name = customer.billing_name if customer else (po.customer.billing_name if po.customer else None)
    c_gstin = customer.gst_number if customer else (po.customer.gst_number if po.customer else None)
    c_addr = customer.billing_address if customer else (po.customer.billing_address if po.customer else None)
    c_state = customer.state if customer else (po.customer.state if po.customer else None)
    exec_user = executive or po.executive
    e_name = (exec_user.full_name or exec_user.username) if exec_user else None

    line_items = [
        {
            "id": it.id,
            "po_id": it.po_id,
            "description": it.item_description,
            "quantity": float(it.quantity),
            "unit_price": _format_money(it.unit_price),
            "exclusive_amount": _format_money(it.exclusive_amount),
            "tax_rate": f"{Decimal(str(it.tax_rate)):.4f}",
            "tax_amount": _format_money(it.tax_amount),
            "total_amount": _format_money(it.total_amount),
        }
        for it in (po.items or [])
    ]

    total_amt_dec = Decimal(str(po.total_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    is_unbilled = po.status == "UNBILLED"

    return {
        "id": po.id,
        "fy_code": po.fy_code,
        "order_type": "PO",
        "po_number": po.po_number,
        "po_date": po.po_date.isoformat() if po.po_date else None,
        "customer_id": po.customer_id,
        "customer_name": c_name,
        "customer_gstin": c_gstin,
        "customer_address": c_addr,
        "customer_state": c_state,
        "executive_id": po.executive_id,
        "executive_name": e_name,
        "currency": po.currency,
        "exchange_rate": float(po.exchange_rate),
        "original_amount": _format_money(po.exclusive_amount),
        "exclusive_amount": _format_money(po.exclusive_amount),
        "tax_rate": f"{Decimal(str(po.tax_rate)):.4f}",
        "tax_amount": _format_money(po.tax_amount),
        "total_amount": f"{total_amt_dec:.2f}",
        "unbilled_amount": f"{total_amt_dec:.2f}" if is_unbilled else "0.00",
        "invoice_number": None,
        "invoice_date": None,
        "due_date": None,
        "remarks": po.remarks,
        "status": po.status,
        "computed_status": "UNBILLED" if is_unbilled else po.status,
        "payment_status": "PENDING",
        "total_cash_paid": "0.00",
        "total_tds_deducted": "0.00",
        "total_paid": "0.00",
        "outstanding_balance": "0.00",
        "recognized_ar": "0.00",
        "line_items": line_items,
        "payments": [],
        "created_at": po.created_at.isoformat() if po.created_at else None,
        "updated_at": po.updated_at.isoformat() if po.updated_at else None,
    }


@router.get("")
def list_orders(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    search: Optional[str] = None,
    fy: Optional[str] = None,
    status: Optional[str] = None,
    customer_id: Optional[int] = None,
    user: dict = Depends(get_current_user),
):
    offset = (page - 1) * limit
    with get_db_session() as db:
        stmt = (
            select(PurchaseOrder, Customer, User)
            .join(Customer, PurchaseOrder.customer_id == Customer.id)
            .join(User, PurchaseOrder.executive_id == User.id)
        )

        if search and search.strip():
            pat = f"%{search.strip()}%"
            stmt = stmt.where(
                (PurchaseOrder.po_number.ilike(pat)) | (Customer.billing_name.ilike(pat))
            )

        if fy and fy != "ALL":
            stmt = stmt.where(PurchaseOrder.fy_code == fy)

        if status and status != "ALL":
            stmt = stmt.where(PurchaseOrder.status == status.upper())

        if customer_id:
            stmt = stmt.where(PurchaseOrder.customer_id == customer_id)

        # Count query
        count_stmt = select(func.count(PurchaseOrder.id))
        if search and search.strip():
            pat = f"%{search.strip()}%"
            count_stmt = count_stmt.join(Customer, PurchaseOrder.customer_id == Customer.id).where(
                (PurchaseOrder.po_number.ilike(pat)) | (Customer.billing_name.ilike(pat))
            )
        if fy and fy != "ALL":
            count_stmt = count_stmt.where(PurchaseOrder.fy_code == fy)
        if status and status != "ALL":
            count_stmt = count_stmt.where(PurchaseOrder.status == status.upper())
        if customer_id:
            count_stmt = count_stmt.where(PurchaseOrder.customer_id == customer_id)

        total_count = db.scalar(count_stmt) or 0

        # Execute paginated
        stmt = stmt.order_by(PurchaseOrder.po_date.desc(), PurchaseOrder.id.desc()).offset(offset).limit(limit)
        results = db.execute(stmt).all()

        serialized = [
            _serialize_order(po, cust, exec_user)
            for po, cust, exec_user in results
        ]

        return {
            "page": page,
            "limit": limit,
            "total": total_count,
            "data": serialized,
        }


@router.get("/{id}")
def get_order(id: int, user: dict = Depends(get_current_user)):
    with get_db_session() as db:
        row = db.execute(
            select(PurchaseOrder, Customer, User)
            .join(Customer, PurchaseOrder.customer_id == Customer.id)
            .join(User, PurchaseOrder.executive_id == User.id)
            .where(PurchaseOrder.id == id)
        ).first()

        if not row:
            raise HTTPException(status_code=404, detail="Purchase order not found")

        po, cust, exec_user = row
        return _serialize_order(po, cust, exec_user)


@router.post("", status_code=201)
def create_order(item: OrderCreate, user: dict = Depends(require_sales_finance_admin)):
    with get_db_session() as db:
        # 1. Customer verification
        cust = db.scalar(select(Customer).where(Customer.id == item.customer_id))
        if not cust:
            raise HTTPException(status_code=404, detail=f"Customer with ID {item.customer_id} not found")
        if not cust.is_active:
            raise HTTPException(status_code=400, detail="Cannot create purchase order for an inactive customer")

        # 2. Financial Year verification
        fy = db.scalar(select(FinancialYear).where(FinancialYear.code == item.fy_code))
        if not fy:
            raise HTTPException(status_code=400, detail=f"Financial year '{item.fy_code}' does not exist")

        po_date_str = item.po_date.strip()
        if not is_date_in_fiscal_year(po_date_str, fy.start_date.isoformat(), fy.end_date.isoformat()):
            raise HTTPException(
                status_code=400,
                detail=f"PO date '{po_date_str}' falls outside financial year '{item.fy_code}' ({fy.start_date} to {fy.end_date})",
            )

        # 3. Executive verification
        exec_id = item.executive_id or user["id"]
        exec_user = db.scalar(select(User).where(User.id == exec_id))
        if not exec_user:
            raise HTTPException(status_code=404, detail=f"Executive user with ID {exec_id} not found")

        # 4. PO Number generation & uniqueness check
        po_num = item.po_number.strip() if item.po_number else None
        if not po_num:
            po_num = f"PO-{datetime.date.today().strftime('%Y%m%d')}-{item.customer_id}"

        existing_po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == po_num))
        if existing_po:
            raise HTTPException(status_code=409, detail=f"Purchase order number '{po_num}' already exists")

        # 5. Resolve line items & exact decimal calculation
        raw_items: List[OrderItemCreate] = []
        if item.items and len(item.items) > 0:
            raw_items = item.items
        elif item.line_item_description:
            raw_items = [
                OrderItemCreate(
                    item_description=item.line_item_description.strip(),
                    quantity=item.quantity if item.quantity is not None else 1.0,
                    unit_price=item.unit_price if item.unit_price is not None else (item.exclusive_amount or 0.0),
                    tax_rate=item.tax_rate if item.tax_rate is not None else 0.18,
                )
            ]
        else:
            raw_items = [
                OrderItemCreate(
                    item_description=f"Purchase Order {po_num}",
                    quantity=1.0,
                    unit_price=item.exclusive_amount or 0.0,
                    tax_rate=item.tax_rate if item.tax_rate is not None else 0.18,
                )
            ]

        total_excl = Decimal("0.00")
        total_tax = Decimal("0.00")
        db_items: List[PurchaseOrderItem] = []

        for ri in raw_items:
            qty = Decimal(str(ri.quantity))
            price = Decimal(str(ri.unit_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            rate = Decimal(str(ri.tax_rate))

            excl = (qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            tax = (excl * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            tot = excl + tax

            total_excl += excl
            total_tax += tax

            db_items.append(
                PurchaseOrderItem(
                    item_description=ri.desc,
                    quantity=qty,
                    unit_price=price,
                    exclusive_amount=excl,
                    tax_rate=rate,
                    tax_amount=tax,
                    total_amount=tot,
                )
            )

        total_amount = total_excl + total_tax
        primary_tax_rate = db_items[0].tax_rate if db_items else (Decimal(str(item.tax_rate)) if item.tax_rate is not None else Decimal("0.1800"))

        # CF4C3-01: Inconsistency checks against caller-supplied amounts (strictly 0 tolerance after quantization)
        if item.exclusive_amount is not None:
            supplied_excl = Decimal(str(item.exclusive_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if supplied_excl != total_excl:
                raise HTTPException(
                    status_code=400,
                    detail=f"Supplied exclusive_amount ({supplied_excl}) is inconsistent with line-item total ({total_excl})",
                )

        if item.tax_amount is not None:
            supplied_tax = Decimal(str(item.tax_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if supplied_tax != total_tax:
                raise HTTPException(
                    status_code=400,
                    detail=f"Supplied tax_amount ({supplied_tax}) is inconsistent with calculated line-item tax ({total_tax})",
                )

        if item.total_amount is not None:
            supplied_total = Decimal(str(item.total_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if supplied_total != total_amount:
                raise HTTPException(
                    status_code=400,
                    detail=f"Supplied total_amount ({supplied_total}) is inconsistent with calculated line-item total ({total_amount})",
                )

        # 6. Instantiate & persist PurchaseOrder
        new_po = PurchaseOrder(
            fy_code=item.fy_code,
            po_number=po_num,
            po_date=datetime.date.fromisoformat(po_date_str),
            customer_id=item.customer_id,
            executive_id=exec_id,
            currency=item.currency or "INR",
            exchange_rate=Decimal(str(item.exchange_rate or 1.0)),
            exclusive_amount=total_excl,
            tax_rate=primary_tax_rate,
            tax_amount=total_tax,
            total_amount=total_amount,
            status="UNBILLED",
            remarks=item.remarks.strip() if item.remarks else None,
            items=db_items,
        )
        db.add(new_po)
        db.flush()

        # 7. Atomic Audit Log
        log_security_audit(
            "PO_CREATED",
            "PURCHASE_ORDER",
            str(new_po.id),
            user_id=user["id"],
            username=user["username"],
            details={
                "po_number": po_num,
                "customer_id": item.customer_id,
                "exclusive_amount": float(total_excl),
                "total_amount": float(total_amount),
            },
            db=db,
        )
        db.commit()

        return _serialize_order(new_po, cust, exec_user)


@router.put("/{id}")
def update_order(id: int, item: OrderUpdate, user: dict = Depends(require_sales_finance_admin)):
    with get_db_session() as db:
        # Lock row for update
        po = db.scalar(
            select(PurchaseOrder)
            .where(PurchaseOrder.id == id)
            .with_for_update()
        )
        if not po:
            raise HTTPException(status_code=404, detail="Purchase order not found")

        # Lifecycle restriction: Only UNBILLED orders can be edited
        if po.status != "UNBILLED":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot edit purchase order in '{po.status}' status. Only UNBILLED orders can be modified.",
            )

        # PO Number update & uniqueness
        if item.po_number and item.po_number.strip():
            new_num = item.po_number.strip()
            if new_num != po.po_number:
                existing = db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == new_num, PurchaseOrder.id != id))
                if existing:
                    raise HTTPException(status_code=409, detail=f"Purchase order number '{new_num}' already exists")
                po.po_number = new_num

        # Date update & fiscal year validation
        if item.po_date and item.po_date.strip():
            new_date_str = item.po_date.strip()
            fy = db.scalar(select(FinancialYear).where(FinancialYear.code == po.fy_code))
            if fy and not is_date_in_fiscal_year(new_date_str, fy.start_date.isoformat(), fy.end_date.isoformat()):
                raise HTTPException(
                    status_code=400,
                    detail=f"PO date '{new_date_str}' falls outside financial year '{po.fy_code}' ({fy.start_date} to {fy.end_date})",
                )
            po.po_date = datetime.date.fromisoformat(new_date_str)

        # Customer update
        if item.customer_id is not None and item.customer_id != po.customer_id:
            cust = db.scalar(select(Customer).where(Customer.id == item.customer_id))
            if not cust:
                raise HTTPException(status_code=404, detail=f"Customer with ID {item.customer_id} not found")
            if not cust.is_active:
                raise HTTPException(status_code=400, detail="Cannot assign purchase order to an inactive customer")
            po.customer_id = item.customer_id

        # Executive update
        if item.executive_id is not None and item.executive_id != po.executive_id:
            exec_u = db.scalar(select(User).where(User.id == item.executive_id))
            if not exec_u:
                raise HTTPException(status_code=404, detail=f"Executive user with ID {item.executive_id} not found")
            po.executive_id = item.executive_id

        if item.currency is not None:
            po.currency = item.currency
        if item.exchange_rate is not None and item.exchange_rate > 0:
            po.exchange_rate = Decimal(str(item.exchange_rate))
        if item.remarks is not None:
            po.remarks = item.remarks.strip() if item.remarks.strip() else None

        # Line items update if provided
        if item.items is not None or item.line_item_description is not None:
            raw_items: List[OrderItemCreate] = []
            if item.items is not None and len(item.items) > 0:
                raw_items = item.items
            elif item.line_item_description:
                raw_items = [
                    OrderItemCreate(
                        item_description=item.line_item_description.strip(),
                        quantity=item.quantity if item.quantity is not None else 1.0,
                        unit_price=item.unit_price if item.unit_price is not None else 0.0,
                        tax_rate=item.tax_rate if item.tax_rate is not None else float(po.tax_rate),
                    )
                ]

            if raw_items:
                po.items.clear()
                total_excl = Decimal("0.00")
                total_tax = Decimal("0.00")

                for ri in raw_items:
                    qty = Decimal(str(ri.quantity))
                    price = Decimal(str(ri.unit_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    rate = Decimal(str(ri.tax_rate))

                    excl = (qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    tax = (excl * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    tot = excl + tax

                    total_excl += excl
                    total_tax += tax

                    po.items.append(
                        PurchaseOrderItem(
                            item_description=ri.desc,
                            quantity=qty,
                            unit_price=price,
                            exclusive_amount=excl,
                            tax_rate=rate,
                            tax_amount=tax,
                            total_amount=tot,
                        )
                    )

                po.exclusive_amount = total_excl
                po.tax_amount = total_tax
                po.total_amount = total_excl + total_tax

        log_security_audit(
            "PO_UPDATED",
            "PURCHASE_ORDER",
            str(po.id),
            user_id=user["id"],
            username=user["username"],
            details={"po_number": po.po_number, "total_amount": float(po.total_amount)},
            db=db,
        )
        db.commit()

        cust = db.scalar(select(Customer).where(Customer.id == po.customer_id))
        exec_u = db.scalar(select(User).where(User.id == po.executive_id))
        return _serialize_order(po, cust, exec_u)


@router.delete("/{id}")
def delete_order(id: int, user: dict = Depends(require_finance_or_admin)):
    with get_db_session() as db:
        po = db.scalar(
            select(PurchaseOrder)
            .where(PurchaseOrder.id == id)
            .with_for_update()
        )
        if not po:
            raise HTTPException(status_code=404, detail="Purchase order not found")

        # Lifecycle restriction: Only UNBILLED orders can be deleted
        if po.status != "UNBILLED":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete purchase order in '{po.status}' status. Only UNBILLED orders can be deleted.",
            )

        po_num = po.po_number
        db.delete(po)

        log_security_audit(
            "PO_DELETED",
            "PURCHASE_ORDER",
            str(id),
            user_id=user["id"],
            username=user["username"],
            details={"po_number": po_num},
            db=db,
        )
        db.commit()

        return {"success": True, "message": f"Purchase order '{po_num}' deleted successfully"}


@router.post("/{id}/convert", status_code=201)
def convert_order_to_invoice(id: int, item: OrderConvert, user: dict = Depends(require_finance_or_admin)):
    with get_db_session() as db:
        # Lock PO row for update to serialize conversions concurrently
        po = db.scalar(
            select(PurchaseOrder)
            .where(PurchaseOrder.id == id)
            .with_for_update()
        )
        if not po:
            raise HTTPException(status_code=404, detail="Purchase order not found")

        # 1. Lifecycle verification
        if po.status == "CONVERTED":
            raise HTTPException(
                status_code=409,
                detail=f"Purchase order '{po.po_number}' has already been converted to an invoice",
            )
        if po.status == "CANCELLED":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot convert cancelled purchase order '{po.po_number}'",
            )

        # 2. Check invoice number uniqueness
        inv_num = item.invoice_number.strip()
        existing_inv = db.scalar(select(CommercialInvoice).where(CommercialInvoice.invoice_number == inv_num))
        if existing_inv:
            raise HTTPException(
                status_code=409,
                detail=f"Invoice number '{inv_num}' already exists",
            )

        # 3. Check fiscal year date boundaries
        inv_date_str = item.invoice_date.strip()
        try:
            inv_date = datetime.date.fromisoformat(inv_date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid invoice_date format. Must be YYYY-MM-DD")

        fy = db.scalar(select(FinancialYear).where(FinancialYear.code == po.fy_code))
        if fy and not is_date_in_fiscal_year(inv_date_str, fy.start_date.isoformat(), fy.end_date.isoformat()):
            raise HTTPException(
                status_code=400,
                detail=f"Invoice date '{inv_date_str}' falls outside financial year '{po.fy_code}' ({fy.start_date} to {fy.end_date})",
            )

        # 4. Due date calculation (Net 30 default) and boundary validation
        if item.due_date and item.due_date.strip():
            try:
                due_date_val = datetime.date.fromisoformat(item.due_date.strip())
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid due_date format. Must be YYYY-MM-DD")
        else:
            calc_due = calculate_net_30_due_date(inv_date_str)
            due_date_val = datetime.date.fromisoformat(calc_due)

        if due_date_val < inv_date:
            raise HTTPException(status_code=400, detail="Due date cannot precede invoice date")

        due_date_str = due_date_val.isoformat()

        # 5. Capture immutable customer billing snapshot
        cust = db.scalar(select(Customer).where(Customer.id == po.customer_id))
        if not cust:
            raise HTTPException(status_code=404, detail="Customer associated with purchase order not found")

        snapshot_name = cust.billing_name
        snapshot_gstin = cust.gst_number
        snapshot_addr = cust.billing_address or "Address on file"
        snapshot_state = cust.state or "Karnataka"

        # 6. Statutory GST allocation (CGST/SGST vs IGST)
        # Home state is Karnataka (KA / 29)
        is_intra = snapshot_state.strip().lower() in ("karnataka", "ka", "29")
        if is_intra:
            cgst = (po.tax_amount / Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            sgst = (po.tax_amount - cgst).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            igst = Decimal("0.00")
        else:
            cgst = Decimal("0.00")
            sgst = Decimal("0.00")
            igst = po.tax_amount

        # 7. Create CommercialInvoice
        new_inv = CommercialInvoice(
            po_id=po.id,
            fy_code=po.fy_code,
            customer_id=po.customer_id,
            executive_id=po.executive_id,
            invoice_number=inv_num,
            invoice_date=datetime.date.fromisoformat(inv_date_str),
            due_date=datetime.date.fromisoformat(due_date_str),
            currency=po.currency,
            exchange_rate=po.exchange_rate,
            exclusive_amount=po.exclusive_amount,
            tax_rate=po.tax_rate,
            tax_amount=po.tax_amount,
            total_amount=po.total_amount,
            snapshot_customer_name=snapshot_name,
            snapshot_gstin=snapshot_gstin,
            snapshot_billing_address=snapshot_addr,
            snapshot_state=snapshot_state,
            cgst_amount=cgst,
            sgst_amount=sgst,
            igst_amount=igst,
            lifecycle_status="ISSUED",
            remarks=po.remarks,
        )

        # 8. Copy line items
        for p_item in po.items:
            new_inv.items.append(
                CommercialInvoiceItem(
                    item_description=p_item.item_description,
                    quantity=p_item.quantity,
                    unit_price=p_item.unit_price,
                    exclusive_amount=p_item.exclusive_amount,
                    tax_rate=p_item.tax_rate,
                    tax_amount=p_item.tax_amount,
                    total_amount=p_item.total_amount,
                )
            )

        db.add(new_inv)

        # 9. Mark purchase order as CONVERTED
        po.status = "CONVERTED"
        db.flush()

        # 10. Atomic security audit log
        log_security_audit(
            "PO_CONVERTED_TO_INVOICE",
            "COMMERCIAL_INVOICE",
            str(new_inv.id),
            user_id=user["id"],
            username=user["username"],
            details={
                "po_id": po.id,
                "po_number": po.po_number,
                "invoice_number": inv_num,
                "total_amount": float(new_inv.total_amount),
            },
            db=db,
        )
        db.commit()

        exec_u = db.scalar(select(User).where(User.id == new_inv.executive_id))

        # Import serializer from invoices or serialize directly
        from app.routers.invoices import serialize_invoice
        res = serialize_invoice(new_inv, cust, exec_u, po)
        res["success"] = True
        res["invoice_id"] = new_inv.id
        return res
