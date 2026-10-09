import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.database import execute_write, log_audit, query_all, query_one, transaction
from app.routers.auth import get_current_user, require_finance_or_admin
from app.utils.financial import (
    ALLOWED_CURRENCIES,
    calculate_invoice_totals,
    calculate_line_item,
    calculate_net_30_due_date,
    is_date_in_fiscal_year,
    round_currency,
)

router = APIRouter(prefix="/api/invoices", tags=["Invoices"])


class InvoiceItemCreate(BaseModel):
    description: str
    quantity: float = Field(default=1.0, gt=0)
    unit_price: float = Field(default=0.0, ge=0.0)
    tax_rate: float = Field(default=0.18, ge=0.0, le=1.0)
    tax_amount: Optional[float] = None
    total_amount: Optional[float] = None


class InvoiceCreate(BaseModel):
    fy_code: str
    order_type: Optional[str] = "PO"  # "PO" or "RECURRING"
    po_date: Optional[str] = None
    po_number: Optional[str] = None
    customer_id: int
    executive_id: Optional[int] = None
    executive_name: Optional[str] = None
    currency: Optional[str] = "INR"
    exchange_rate: Optional[float] = Field(default=1.0, gt=0)
    original_amount: Optional[float] = Field(default=None, ge=0.0)
    exclusive_amount: float = Field(ge=0.0)
    tax_rate: Optional[float] = Field(default=0.18, ge=0.0, le=1.0)
    tax_amount: Optional[float] = Field(default=None, ge=0.0)
    total_amount: Optional[float] = Field(default=None, ge=0.0)
    tax_ref: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    due_date: Optional[str] = None
    remarks: Optional[str] = None
    items: Optional[List[InvoiceItemCreate]] = []


class InvoiceItemUpdate(BaseModel):
    id: Optional[int] = None
    description: str


class InvoiceUpdate(BaseModel):
    remarks: Optional[str] = None
    po_number: Optional[str] = None
    po_date: Optional[str] = None
    due_date: Optional[str] = None
    customer_id: Optional[int] = None
    executive_id: Optional[int] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    items: Optional[List[InvoiceItemUpdate]] = None


class InvoiceCancel(BaseModel):
    reason: Optional[str] = "Operational cancellation / Customer request"


@router.get("")
def list_invoices(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    search: Optional[str] = None,
    fy: Optional[str] = None,
    status: Optional[str] = None,
    executive_id: Optional[int] = None,
    user: dict = Depends(get_current_user),
):
    offset = (page - 1) * limit
    where_clauses = ["1=1"]
    params = []

    if search:
        search_pattern = f"%{search.strip()}%"
        where_clauses.append("(po_number LIKE ? OR invoice_number LIKE ? OR customer_name LIKE ?)")
        params.extend([search_pattern, search_pattern, search_pattern])

    if fy and fy != "ALL":
        where_clauses.append("fy_code = ?")
        params.append(fy)

    if status and status != "ALL":
        where_clauses.append("computed_status = ?")
        params.append(status.upper())

    if executive_id:
        where_clauses.append("executive_id = ?")
        params.append(executive_id)

    where_sql = " AND ".join(where_clauses)

    count_query = f"SELECT COUNT(*) as count FROM v_invoice_summary WHERE {where_sql}"  # nosec B608
    total_count = query_one(count_query, tuple(params))["count"]

    data_query = f"""
    SELECT * FROM v_invoice_summary
    WHERE {where_sql}
    ORDER BY id DESC
    LIMIT ? OFFSET ?;
    """  # nosec B608
    params.extend([limit, offset])
    rows = query_all(data_query, tuple(params))

    return {
        "page": page,
        "limit": limit,
        "total": total_count,
        "total_pages": (total_count + limit - 1) // limit,
        "data": rows,
    }


@router.get("/{invoice_id}")
def get_invoice(invoice_id: int, user: dict = Depends(get_current_user)):
    invoice = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (invoice_id,))
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    payments = query_all("SELECT * FROM payments WHERE invoice_id = ? ORDER BY payment_date ASC", (invoice_id,))
    items = query_all("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY id ASC", (invoice_id,))
    return {"invoice": invoice, "payments": payments, "items": items}


@router.post("")
def create_invoice(item: InvoiceCreate, request: Request, user: dict = Depends(get_current_user)):
    req_id = getattr(request.state, "request_id", None)

    # Validate customer exists & fetch full details for snapshot
    cust = query_one(
        "SELECT id, billing_name, gst_number, billing_address, state FROM customers WHERE id = ?", (item.customer_id,)
    )
    if not cust:
        raise HTTPException(status_code=400, detail="Specified customer does not exist")

    # Validate FY code and invoice date boundary
    fy_master = query_one("SELECT code, start_date, end_date FROM financial_years WHERE code = ?", (item.fy_code,))
    if not fy_master:
        raise HTTPException(status_code=400, detail=f"Financial year '{item.fy_code}' does not exist in master records")
    target_date = item.invoice_date or item.po_date
    if target_date and target_date.strip():
        if not is_date_in_fiscal_year(target_date, fy_master["start_date"], fy_master["end_date"]):
            raise HTTPException(
                status_code=400,
                detail=f"Date '{target_date}' falls outside financial year '{item.fy_code}' ({fy_master['start_date']} to {fy_master['end_date']})",
            )

    # Validate currency
    curr = (item.currency or "INR").upper()
    if curr not in ALLOWED_CURRENCIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported currency: {curr}. Must be one of {', '.join(sorted(ALLOWED_CURRENCIES))}",
        )

    # Validate exchange rate
    rate = float(item.exchange_rate or 1.0)
    if rate <= 0:
        raise HTTPException(status_code=400, detail="Exchange rate must be greater than zero")
    if curr == "INR":
        rate = 1.0

    # Line items vs manual amounts
    if item.items and len(item.items) > 0:
        raw_items = [it.model_dump() if hasattr(it, "model_dump") else it.dict() for it in item.items]
        calc_excl, calc_tax, calc_tot = calculate_invoice_totals(raw_items)
        excl = calc_excl
        tax_amt = calc_tax
        tot = calc_tot
        orig = round_currency(excl / rate) if rate > 0 else excl
    else:
        if item.original_amount is not None and item.original_amount > 0:
            orig = round_currency(item.original_amount)
            excl = round_currency(orig * rate)
        else:
            excl = round_currency(item.exclusive_amount)
            orig = round_currency(excl / rate) if rate > 0 else excl

        t_rate = item.tax_rate if item.tax_rate is not None else 0.18
        tax_amt = item.tax_amount if item.tax_amount is not None else round_currency(excl * t_rate)
        tot = item.total_amount if item.total_amount is not None else round_currency(excl + tax_amt)

    # Normalization & duplicate checks
    po_no = item.po_number
    if not po_no or not po_no.strip():
        if (item.order_type or "").upper() == "RECURRING":
            po_no = "Recurring / Direct Invoice"
        else:
            po_no = "No PO"
    else:
        po_no = po_no.strip()
        # Enforce unique PO number if supplied
        norm_po = po_no.lower()
        if norm_po not in ("no po", "recurring / direct invoice"):
            dup_po = query_one("SELECT id FROM invoices WHERE LOWER(TRIM(po_number)) = ?", (norm_po,))
            if dup_po:
                raise HTTPException(
                    status_code=400,
                    detail=f"Purchase Order number '{po_no}' is already registered to order #{dup_po['id']}",
                )

    inv_num = item.invoice_number.strip() if item.invoice_number and item.invoice_number.strip() else None
    if inv_num:
        norm_inv = inv_num.lower()
        dup_inv = query_one("SELECT id FROM invoices WHERE LOWER(TRIM(invoice_number)) = ?", (norm_inv,))
        if dup_inv:
            raise HTTPException(
                status_code=400, detail=f"Invoice number '{inv_num}' is already assigned to record #{dup_inv['id']}"
            )

    # Auto-resolve executive_id
    exec_id = item.executive_id
    if not exec_id and item.executive_name and item.executive_name.strip():
        ename = item.executive_name.strip()
        u_row = query_one(
            "SELECT id FROM users WHERE role = 'sales' AND (LOWER(full_name) = ? OR LOWER(username) = ?)",
            (ename.lower(), ename.lower()),
        )
        if u_row:
            exec_id = u_row["id"]

    # Validate executive if provided directly
    if exec_id:
        u_check = query_one("SELECT id FROM users WHERE id = ? AND role = 'sales'", (exec_id,))
        if not u_check:
            raise HTTPException(status_code=400, detail="Assigned executive must be a user with role 'sales'")

    # Net-30 calculation
    due = item.due_date
    if not due and item.invoice_date and item.invoice_date.strip():
        due = calculate_net_30_due_date(item.invoice_date)

    sql = """
    INSERT INTO invoices (
        fy_code, order_type, po_date, po_number, customer_id, executive_id,
        currency, exchange_rate, original_amount,
        exclusive_amount, tax_rate, tax_amount, total_amount,
        tax_ref, invoice_number, invoice_date, due_date, remarks,
        customer_name, customer_gstin, customer_address, customer_state
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    try:
        with transaction() as conn:
            new_id = execute_write(
                sql,
                (
                    item.fy_code,
                    item.order_type or "PO",
                    item.po_date,
                    po_no,
                    item.customer_id,
                    exec_id,
                    curr,
                    rate,
                    orig,
                    excl,
                    item.tax_rate or 0.18,
                    tax_amt,
                    tot,
                    item.tax_ref,
                    inv_num,
                    item.invoice_date or None,
                    due or None,
                    item.remarks,
                    cust["billing_name"],
                    cust["gst_number"],
                    cust["billing_address"],
                    cust["state"],
                ),
                conn=conn,
            )

            # Save multiple line items atomically, or create default single line item
            if item.items and len(item.items) > 0:
                for itm in item.items:
                    i_excl, i_tax, i_tot = calculate_line_item(itm.quantity, itm.unit_price, itm.tax_rate)
                    execute_write(
                        """
                        INSERT INTO invoice_items (invoice_id, description, quantity, unit_price, tax_rate, tax_amount, total_amount)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                        (new_id, itm.description.strip(), itm.quantity, itm.unit_price, itm.tax_rate, i_tax, i_tot),
                        conn=conn,
                    )
            else:
                execute_write(
                    """
                    INSERT INTO invoice_items (invoice_id, description, quantity, unit_price, tax_rate, tax_amount, total_amount)
                    VALUES (?, ?, 1.0, ?, ?, ?, ?)
                """,
                    (new_id, po_no if po_no not in ("No PO", "Recurring / Direct Invoice") else "Standard Line Item", excl, item.tax_rate or 0.18, tax_amt, tot),
                    conn=conn,
                )

            log_audit(
                "INVOICE_CREATED",
                "INVOICE",
                new_id,
                f"Created {item.order_type or 'PO'} #{new_id} ({po_no}) for customer {cust['billing_name']} (Total: INR {tot})",
                user=user,
                request_id=req_id,
                conn=conn,
            )
    except sqlite3.IntegrityError as err:
        err_msg = str(err).lower()
        if "unique" in err_msg:
            if "po_number" in err_msg or "unique_po" in err_msg:
                raise HTTPException(status_code=400, detail=f"Purchase Order number '{po_no}' is already registered")
            if "invoice_number" in err_msg or "unique_inv" in err_msg:
                raise HTTPException(status_code=400, detail=f"Invoice number '{inv_num}' is already assigned")
        raise HTTPException(status_code=400, detail=f"Database constraint error: {err}")

    return {"success": True, "invoice_id": new_id, "message": "Record created successfully"}


@router.put("/{order_id}")
def update_invoice(
    order_id: int, payload: InvoiceUpdate, request: Request, user: dict = Depends(require_finance_or_admin)
):
    """
    Enforces Invoice Immutability:
    Financial totals, tax rates, currencies, and line items of issued invoices are permanently immutable.
    Only internal non-financial remarks can be modified on issued invoices.
    """
    req_id = getattr(request.state, "request_id", None)
    inv = query_one("SELECT * FROM invoices WHERE id = ?", (order_id,))
    if not inv:
        raise HTTPException(status_code=404, detail="Order/Invoice not found")

    is_issued = bool(inv["invoice_number"] and inv["invoice_number"].strip())

    with transaction() as conn:
        new_remarks = payload.remarks if payload.remarks is not None else inv["remarks"]
        new_due = payload.due_date if payload.due_date is not None else inv["due_date"]
        new_exec_id = payload.executive_id if payload.executive_id is not None else inv["executive_id"]

        # Validate executive if changed
        if new_exec_id:
            u_check = query_one("SELECT id FROM users WHERE id = ? AND role = 'sales'", (new_exec_id,), conn=conn)
            if not u_check:
                raise HTTPException(status_code=400, detail="Assigned executive must be a user with role 'sales'")

        if is_issued:
            # Issued invoice: PO number, PO date, customer cannot be modified
            if payload.po_number or payload.po_date or payload.customer_id:
                raise HTTPException(
                    status_code=400,
                    detail="Issued invoices are legally immutable. Commercial identifiers and customer cannot be changed.",
                )
            execute_write(
                "UPDATE invoices SET remarks = ?, due_date = ?, executive_id = ?, updated_at = datetime('now') WHERE id = ?",
                (new_remarks, new_due, new_exec_id, order_id),
                conn=conn,
            )
        else:
            # Unbilled PO: allow updating PO metadata, customer, executive, and dates before issuance
            new_po = payload.po_number.strip() if payload.po_number else inv["po_number"]
            if payload.po_number and new_po and new_po.lower() not in ("no po", "recurring / direct invoice"):
                dup_po = query_one(
                    "SELECT id FROM invoices WHERE LOWER(TRIM(po_number)) = LOWER(TRIM(?)) AND id != ?",
                    (new_po, order_id),
                    conn=conn,
                )
                if dup_po:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Purchase Order number '{new_po}' is already registered to order #{dup_po['id']}",
                    )
            new_po_date = payload.po_date if payload.po_date is not None else inv["po_date"]
            new_cust_id = payload.customer_id if payload.customer_id is not None else inv["customer_id"]

            cust_name = inv["customer_name"]
            cust_gst = inv["customer_gstin"]
            cust_addr = inv["customer_address"]
            cust_state = inv["customer_state"]
            if payload.customer_id is not None and payload.customer_id != inv["customer_id"]:
                c_row = query_one(
                    "SELECT billing_name, gst_number, billing_address, state FROM customers WHERE id = ?",
                    (payload.customer_id,),
                    conn=conn,
                )
                if not c_row:
                    raise HTTPException(status_code=400, detail="Selected customer does not exist")
                cust_name = c_row["billing_name"]
                cust_gst = c_row["gst_number"]
                cust_addr = c_row["billing_address"]
                cust_state = c_row["state"]

            execute_write(
                """
                UPDATE invoices
                SET po_number = ?, po_date = ?, customer_id = ?, executive_id = ?,
                    customer_name = ?, customer_gstin = ?, customer_address = ?, customer_state = ?,
                    remarks = ?, due_date = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (new_po, new_po_date, new_cust_id, new_exec_id, cust_name, cust_gst, cust_addr, cust_state, new_remarks, new_due, order_id),
                conn=conn,
            )

        # Update line item descriptions if provided
        if payload.items is not None:
            for itm in payload.items:
                clean_desc = itm.description.strip()
                if not clean_desc:
                    continue
                if itm.id:
                    matched = query_one(
                        "SELECT id FROM invoice_items WHERE id = ? AND invoice_id = ?",
                        (itm.id, order_id),
                        conn=conn,
                    )
                    if not matched:
                        raise HTTPException(status_code=400, detail=f"Line item #{itm.id} does not belong to order #{order_id}")
                    execute_write(
                        "UPDATE invoice_items SET description = ? WHERE id = ? AND invoice_id = ?",
                        (clean_desc, itm.id, order_id),
                        conn=conn,
                    )
                else:
                    # Single item fallback if invoice has exactly one line item
                    single_item = query_one("SELECT id FROM invoice_items WHERE invoice_id = ?", (order_id,), conn=conn)
                    if single_item:
                        execute_write(
                            "UPDATE invoice_items SET description = ? WHERE id = ? AND invoice_id = ?",
                            (clean_desc, single_item["id"], order_id),
                            conn=conn,
                        )

        log_audit(
            "INVOICE_UPDATED",
            "INVOICE",
            order_id,
            f"Updated metadata for #{order_id}",
            user=user,
            request_id=req_id,
            conn=conn,
        )

    return {"success": True, "message": "Invoice updated successfully"}


class ConvertToInvoicePayload(BaseModel):
    invoice_number: str
    invoice_date: str
    due_date: Optional[str] = None


@router.post("/{order_id}/convert-to-invoice")
def convert_to_invoice(
    order_id: int, payload: ConvertToInvoicePayload, request: Request, user: dict = Depends(require_finance_or_admin)
):
    req_id = getattr(request.state, "request_id", None)
    inv_number = payload.invoice_number.strip()
    if not inv_number:
        raise HTTPException(status_code=400, detail="Invoice number is required")

    with transaction() as conn:
        inv = query_one(
            "SELECT id, fy_code, customer_id, invoice_number, po_number, total_amount, payment_status FROM invoices WHERE id = ?",
            (order_id,),
            conn=conn,
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Order not found")

        if inv.get("payment_status") == "CANCELLED":
            raise HTTPException(status_code=400, detail="Cannot convert a cancelled order to an invoice")

        # Validate invoice date against order's FY
        fy_master = query_one(
            "SELECT code, start_date, end_date FROM financial_years WHERE code = ?", (inv["fy_code"],), conn=conn
        )
        if fy_master and payload.invoice_date and payload.invoice_date.strip():
            if not is_date_in_fiscal_year(payload.invoice_date, fy_master["start_date"], fy_master["end_date"]):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invoice date '{payload.invoice_date}' does not fall within financial year '{inv['fy_code']}' ({fy_master['start_date']} to {fy_master['end_date']})",
                )

        # Prevent duplicate conversion
        if inv["invoice_number"] and inv["invoice_number"].strip():
            raise HTTPException(
                status_code=400,
                detail=f"Order #{order_id} has already been converted to Invoice #{inv['invoice_number']}",
            )

        # Prevent duplicate invoice number across the system
        dup = query_one(
            "SELECT id FROM invoices WHERE LOWER(TRIM(invoice_number)) = LOWER(TRIM(?)) AND id != ?",
            (inv_number, order_id),
            conn=conn,
        )
        if dup:
            raise HTTPException(
                status_code=400, detail=f"Invoice number '{inv_number}' is already assigned to record #{dup['id']}"
            )

        due = payload.due_date
        if not due and payload.invoice_date:
            due = calculate_net_30_due_date(payload.invoice_date)

        cust = query_one(
            "SELECT billing_name, gst_number, billing_address, state FROM customers WHERE id = ?",
            (inv["customer_id"],),
            conn=conn,
        )
        cust_name = cust["billing_name"] if cust else "Unknown Customer"
        cust_gst = cust["gst_number"] if cust else None
        cust_addr = cust["billing_address"] if cust else None
        cust_state = cust["state"] if cust else None

        execute_write(
            """
            UPDATE invoices
            SET invoice_number = ?, invoice_date = ?, due_date = ?,
                customer_name = COALESCE(customer_name, ?),
                customer_gstin = COALESCE(customer_gstin, ?),
                customer_address = COALESCE(customer_address, ?),
                customer_state = COALESCE(customer_state, ?),
                updated_at = datetime('now')
            WHERE id = ?
        """,
            (inv_number, payload.invoice_date, due, cust_name, cust_gst, cust_addr, cust_state, order_id),
            conn=conn,
        )

        log_audit(
            "PO_CONVERTED_TO_INVOICE",
            "INVOICE",
            order_id,
            f"Converted PO #{order_id} to Invoice #{inv_number} (Due: {due})",
            user=user,
            request_id=req_id,
            conn=conn,
        )

    return {"success": True, "message": f"PO #{order_id} converted to Invoice {inv_number}"}


@router.post("/{order_id}/cancel")
def cancel_invoice(
    order_id: int,
    payload: Optional[InvoiceCancel] = None,
    request: Request = None,
    user: dict = Depends(require_finance_or_admin),
):
    req_id = getattr(request.state, "request_id", None) if request else None
    reason = payload.reason if payload and payload.reason else "Operational cancellation"
    with transaction() as conn:
        inv = query_one(
            "SELECT id, invoice_number, po_number, payment_status FROM invoices WHERE id = ?", (order_id,), conn=conn
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")

        if inv["payment_status"] == "CANCELLED":
            raise HTTPException(status_code=400, detail="Invoice is already cancelled")

        pay_count = query_one("SELECT COUNT(*) as count FROM payments WHERE invoice_id = ?", (order_id,), conn=conn)[
            "count"
        ]
        if pay_count > 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot cancel invoice with existing payments. All payments must be deleted/reversed first.",
            )

        execute_write(
            "UPDATE invoices SET payment_status = 'CANCELLED', remarks = ?, updated_at = datetime('now') WHERE id = ?",
            (f"[CANCELLED: {reason}]", order_id),
            conn=conn,
        )
        log_audit(
            "INVOICE_CANCELLED",
            "INVOICE",
            order_id,
            f"Cancelled invoice #{order_id} ({inv['invoice_number'] or inv['po_number']}). Reason: {reason}",
            user=user,
            request_id=req_id,
            conn=conn,
        )
    return {"success": True, "message": f"Invoice #{order_id} successfully cancelled"}


@router.delete("/{order_id}")
def delete_invoice(order_id: int, request: Request, user: dict = Depends(require_finance_or_admin)):
    req_id = getattr(request.state, "request_id", None)
    with transaction() as conn:
        inv = query_one(
            "SELECT id, po_number, invoice_number, total_amount, payment_status FROM invoices WHERE id = ?",
            (order_id,),
            conn=conn,
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Order/Invoice not found")

        pay_count = query_one("SELECT COUNT(*) as count FROM payments WHERE invoice_id = ?", (order_id,), conn=conn)[
            "count"
        ]
        if pay_count > 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete invoice with existing payment records. Reversal/credit note required to preserve financial audit history.",
            )

        # Protect issued invoices from physical deletion: require cancellation workflow
        if inv["invoice_number"] and inv["invoice_number"].strip():
            raise HTTPException(
                status_code=400,
                detail="Issued commercial invoices cannot be permanently deleted. Use the cancellation workflow (POST /api/invoices/{id}/cancel) instead.",
            )

        label = inv["po_number"] or f"#{order_id}"

        execute_write("DELETE FROM invoice_items WHERE invoice_id = ?", (order_id,), conn=conn)
        execute_write("DELETE FROM invoices WHERE id = ?", (order_id,), conn=conn)

        log_audit(
            "INVOICE_DELETED",
            "INVOICE",
            order_id,
            f"Deleted unbilled PO {label} (Total: {inv['total_amount']})",
            user=user,
            request_id=req_id,
            conn=conn,
        )

    return {"success": True, "message": f"Record {label} deleted successfully"}
