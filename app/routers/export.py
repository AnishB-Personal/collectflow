import io
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import openpyxl
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from openpyxl.styles import Alignment, Font, PatternFill

from app.database import execute_write, hash_token, query_all, query_one, transaction
from app.routers.auth import get_current_user
from app.utils.financial import sanitize_formula_injection

router = APIRouter(prefix="/api/export", tags=["Export"])


@router.post("/token")
def create_export_token(user: dict = Depends(get_current_user)):
    """
    Generate a short-lived, single-use export ticket.
    - Unrelated to session token
    - Expires in 5 minutes
    - Consumed on single download
    - Scoped strictly to export
    """
    ticket = secrets.token_urlsafe(32)
    ticket_hash = hash_token(ticket)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

    execute_write(
        "INSERT INTO export_tickets (token_hash, user_id, expires_at, used) VALUES (?, ?, ?, 0)",
        (ticket_hash, user["id"], expires),
    )

    return {"ticket": ticket, "expires_in": 300}


def authenticate_export_request(
    request: Request, ticket: Optional[str] = Query(None), authorization: Optional[str] = Header(None)
) -> dict:
    # 1. Direct Bearer header authentication
    if authorization and authorization.startswith("Bearer "):
        return get_current_user(authorization)

    # 2. Single-use short-lived export ticket
    if ticket:
        ticket_hash = hash_token(ticket.strip())
        with transaction() as conn:
            row = query_one(
                """
                SELECT et.token_hash, et.user_id, et.expires_at, et.used, u.id, u.username, u.role, u.full_name
                FROM export_tickets et
                JOIN users u ON et.user_id = u.id
                WHERE et.token_hash = ?
                  AND et.used = 0
                  AND datetime('now') < et.expires_at;
            """,
                (ticket_hash,),
                conn=conn,
            )

            if not row:
                raise HTTPException(status_code=401, detail="Invalid, expired, or already consumed export ticket")

            # Atomically mark ticket as consumed
            execute_write("UPDATE export_tickets SET used = 1 WHERE token_hash = ?", (ticket_hash,), conn=conn)
            return {"id": row["id"], "username": row["username"], "role": row["role"], "full_name": row["full_name"]}

    raise HTTPException(status_code=401, detail="Export requires Authorization header or a valid single-use ticket")


@router.get("/excel")
def export_invoices_excel(fy: str = "ALL", user: dict = Depends(authenticate_export_request)):
    where = "1=1"
    params = []
    if fy and fy != "ALL":
        where = "fy_code = ?"
        params.append(fy)

    query = f"""
    SELECT
        fy_code, po_date, po_number, customer_name, executive_name,
        exclusive_amount, tax_amount, total_amount, invoice_number,
        invoice_date, due_date, total_cash_paid, total_tds_deducted,
        total_paid, outstanding_balance, computed_status, days_overdue
    FROM v_invoice_summary
    WHERE {where}
    ORDER BY id DESC;
    """  # nosec B608
    rows = query_all(query, tuple(params))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoices & Collections"

    headers = [
        "FY",
        "PO Date",
        "PO Number",
        "Customer Name",
        "Executive",
        "Exclusive Amount (₹)",
        "Tax Amount (₹)",
        "Total Invoiced (₹)",
        "Invoice Number",
        "Invoice Date",
        "Due Date",
        "Cash Received (₹)",
        "TDS Deducted (₹)",
        "Total Settled (₹)",
        "Balance Outstanding (₹)",
        "Status",
        "Days Overdue",
    ]

    h_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    h_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")

    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(1, col_idx)
        cell.font = h_font
        cell.fill = h_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for r_idx, r in enumerate(rows, start=2):
        ws.append(
            [
                sanitize_formula_injection(r["fy_code"]),
                r["po_date"],
                sanitize_formula_injection(r["po_number"]),
                sanitize_formula_injection(r["customer_name"]),
                sanitize_formula_injection(r["executive_name"]),
                r["exclusive_amount"],
                r["tax_amount"],
                r["total_amount"],
                sanitize_formula_injection(r["invoice_number"]),
                r["invoice_date"],
                r["due_date"],
                r["total_cash_paid"],
                r["total_tds_deducted"],
                r["total_paid"],
                r["outstanding_balance"],
                sanitize_formula_injection(r["computed_status"]),
                r["days_overdue"],
            ]
        )
        for col_idx in [6, 7, 8, 12, 13, 14, 15]:
            ws.cell(r_idx, col_idx).number_format = "₹#,##0.00"

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    clean_fy = re.sub(r"[^a-zA-Z0-9_\-]", "_", fy)
    filename = f"CollectFlow_Export_{clean_fy}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
