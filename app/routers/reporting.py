"""
Authoritative PostgreSQL Reporting Router for CollectFlow.

Provides:
- GET /api/reporting/summary — High-level financial KPIs, settlement rate, unbilled order value.
- GET /api/reporting/ar-aging — 6-bucket AR Aging analysis as of calendar date.
- GET /api/reporting/attainment — Sales quota attainment by sales executive.
- GET /api/reporting/customer-exposure — Customer-wise exposure and billing breakdown.
"""

import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select

from app.core.database import get_db_session
from app.models.domain_models import (
    CommercialInvoice,
    Customer,
    PaymentTransaction,
    PurchaseOrder,
    SalesTarget,
    User,
)
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api/reporting", tags=["Reporting"])


def _quantize_2dec(val: Any) -> Decimal:
    if val is None:
        return Decimal("0.00")
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _format_money(val: Any) -> str:
    if val is None:
        return "0.00"
    return f"{Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _parse_as_of_date(date_str: Optional[str]) -> datetime.date:
    if not date_str or not date_str.strip():
        return datetime.date.today()
    try:
        return datetime.date.fromisoformat(date_str.strip())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid as_of_date format: '{date_str}'. Expected YYYY-MM-DD.",
        )


@router.get("/summary")
def get_reporting_summary(
    fy: Optional[str] = None,
    as_of_date: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """
    High-level financial KPIs computed over authoritative PostgreSQL records:
    - Total issued invoiced value (excluding cancelled invoices)
    - Net cash collected, TDS recognized, and total settled
    - Outstanding receivables (Issued - Settled)
    - Unbilled order value (separate from AR)
    - Collection / settlement rate
    """
    target_date = _parse_as_of_date(as_of_date) if as_of_date else None

    with get_db_session() as db:
        # 1. Issued commercial invoices (accounting for as-of-date and cancellations after as_of_date)
        if target_date:
            inv_stmt = select(CommercialInvoice).where(
                CommercialInvoice.invoice_date <= target_date,
                or_(
                    CommercialInvoice.lifecycle_status == "ISSUED",
                    (CommercialInvoice.lifecycle_status == "CANCELLED")
                    & (func.date(CommercialInvoice.cancelled_at) > target_date),
                ),
            )
        else:
            inv_stmt = select(CommercialInvoice).where(
                CommercialInvoice.lifecycle_status == "ISSUED"
            )

        if fy and fy.upper() not in ("ALL", "ALL FY"):
            inv_stmt = inv_stmt.where(CommercialInvoice.fy_code == fy)

        invoices = db.execute(inv_stmt).scalars().all()
        invoice_ids = [inv.id for inv in invoices]

        total_invoiced = sum((_quantize_2dec(inv.total_amount) for inv in invoices), Decimal("0.00"))
        total_exclusive = sum((_quantize_2dec(inv.exclusive_amount) for inv in invoices), Decimal("0.00"))
        total_tax = sum((_quantize_2dec(inv.tax_amount) for inv in invoices), Decimal("0.00"))
        total_invoices_count = len(invoices)

        # 2. Settlements for matching invoices
        total_cash = Decimal("0.00")
        total_tds = Decimal("0.00")
        total_settled = Decimal("0.00")

        if invoice_ids:
            pay_stmt = select(PaymentTransaction).where(
                PaymentTransaction.invoice_id.in_(invoice_ids)
            )
            if target_date:
                pay_stmt = pay_stmt.where(PaymentTransaction.payment_date <= target_date)

            payments = db.execute(pay_stmt).scalars().all()
            for p in payments:
                total_cash += _quantize_2dec(p.cash_amount)
                total_tds += _quantize_2dec(p.tds_amount)
                total_settled += _quantize_2dec(p.total_settled)

        # Invariant: Total Outstanding = Total Invoiced - Total Settled
        total_outstanding = max(Decimal("0.00"), total_invoiced - total_settled)

        # Overdue receivables as of target date
        total_overdue = Decimal("0.00")
        ref_date = target_date or datetime.date.today()

        # Compute per-invoice balance and overdue
        invoice_settled_map: Dict[int, Decimal] = {}
        if invoice_ids:
            for p in payments:
                invoice_settled_map[p.invoice_id] = invoice_settled_map.get(
                    p.invoice_id, Decimal("0.00")
                ) + _quantize_2dec(p.total_settled)

        for inv in invoices:
            settled_amt = invoice_settled_map.get(inv.id, Decimal("0.00"))
            bal = max(Decimal("0.00"), _quantize_2dec(inv.total_amount) - settled_amt)
            if bal > Decimal("0.00") and inv.due_date < ref_date:
                total_overdue += bal

        # 3. Unbilled Purchase Orders
        po_stmt = select(PurchaseOrder).where(PurchaseOrder.status == "UNBILLED")
        if fy and fy.upper() not in ("ALL", "ALL FY"):
            po_stmt = po_stmt.where(PurchaseOrder.fy_code == fy)
        if target_date:
            po_stmt = po_stmt.where(PurchaseOrder.po_date <= target_date)

        unbilled_orders = db.execute(po_stmt).scalars().all()
        unbilled_orders_value = sum(
            (_quantize_2dec(po.total_amount) for po in unbilled_orders), Decimal("0.00")
        )
        unbilled_count = len(unbilled_orders)

        # Collection rate
        rate_val = (
            round(float(total_settled / total_invoiced * 100), 1)
            if total_invoiced > Decimal("0.00")
            else 0.0
        )

        return {
            "fy_code": fy or "ALL",
            "as_of_date": str(target_date or datetime.date.today()),
            "total_invoices": total_invoices_count,
            "active_invoices_count": total_invoices_count,
            "total_invoiced": _format_money(total_invoiced),
            "total_exclusive": _format_money(total_exclusive),
            "total_tax": _format_money(total_tax),
            "total_cash": _format_money(total_cash),
            "total_tds": _format_money(total_tds),
            "total_settled": _format_money(total_settled),
            "total_outstanding": _format_money(total_outstanding),
            "total_overdue": _format_money(total_overdue),
            "unbilled_orders_value": _format_money(unbilled_orders_value),
            "unbilled_count": unbilled_count,
            "active_orders_count": unbilled_count,
            "collection_rate_pct": f"{rate_val:.1f}",
            "settlement_ratio_pct": rate_val,
            "collection_efficiency_pct": rate_val,
        }


@router.get("/ar-aging")
def get_ar_aging_report(
    as_of_date: Optional[str] = None,
    fy: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """
    6-Bucket AR Aging analysis computed as of the selected calendar date:
    1. Current / Not Due (due on or after as_of_date, days_overdue <= 0)
    2. 1–30 days
    3. 31–60 days
    4. 61–90 days
    5. 91–180 days
    6. 180+ days

    Synthetic Regression Anchor:
    INV-2026-042 (due 2026-05-12) as of 2026-04-30 remains Current / Not Due with 0 days overdue.
    """
    target_date = _parse_as_of_date(as_of_date)

    with get_db_session() as db:
        stmt = (
            select(CommercialInvoice, Customer, User, PurchaseOrder)
            .join(Customer, CommercialInvoice.customer_id == Customer.id)
            .join(User, CommercialInvoice.executive_id == User.id)
            .outerjoin(PurchaseOrder, CommercialInvoice.po_id == PurchaseOrder.id)
            .where(CommercialInvoice.invoice_date <= target_date)
            .where(
                or_(
                    CommercialInvoice.lifecycle_status == "ISSUED",
                    (CommercialInvoice.lifecycle_status == "CANCELLED")
                    & (func.date(CommercialInvoice.cancelled_at) > target_date),
                )
            )
        )

        if fy and fy.upper() not in ("ALL", "ALL FY"):
            stmt = stmt.where(CommercialInvoice.fy_code == fy)

        results = db.execute(stmt).all()

        bucket_totals = {
            "Current / Not Due": {"amount": Decimal("0.00"), "count": 0},
            "1–30 days": {"amount": Decimal("0.00"), "count": 0},
            "31–60 days": {"amount": Decimal("0.00"), "count": 0},
            "61–90 days": {"amount": Decimal("0.00"), "count": 0},
            "91–180 days": {"amount": Decimal("0.00"), "count": 0},
            "180+ days": {"amount": Decimal("0.00"), "count": 0},
        }

        invoice_rows: List[dict] = []
        total_receivables = Decimal("0.00")
        total_overdue = Decimal("0.00")

        # Preload settlements for matching invoices
        invoice_ids = [inv.id for inv, _, _, _ in results]
        invoice_settled_map: Dict[int, Decimal] = {}
        if invoice_ids:
            pay_stmt = select(PaymentTransaction).where(
                PaymentTransaction.invoice_id.in_(invoice_ids),
                PaymentTransaction.payment_date <= target_date,
            )
            for p in db.execute(pay_stmt).scalars().all():
                invoice_settled_map[p.invoice_id] = invoice_settled_map.get(
                    p.invoice_id, Decimal("0.00")
                ) + _quantize_2dec(p.total_settled)

        for inv, cust, exec_user, po in results:
            settled = invoice_settled_map.get(inv.id, Decimal("0.00"))
            outstanding = max(Decimal("0.00"), _quantize_2dec(inv.total_amount) - settled)

            if outstanding <= Decimal("0.00"):
                continue

            days_overdue = max(0, (target_date - inv.due_date).days)

            if days_overdue <= 0:
                bucket_name = "Current / Not Due"
            elif days_overdue <= 30:
                bucket_name = "1–30 days"
            elif days_overdue <= 60:
                bucket_name = "31–60 days"
            elif days_overdue <= 90:
                bucket_name = "61–90 days"
            elif days_overdue <= 180:
                bucket_name = "91–180 days"
            else:
                bucket_name = "180+ days"

            bucket_totals[bucket_name]["amount"] += outstanding
            bucket_totals[bucket_name]["count"] += 1
            total_receivables += outstanding

            if days_overdue > 0:
                total_overdue += outstanding

            invoice_rows.append(
                {
                    "id": inv.id,
                    "invoice_number": inv.invoice_number,
                    "po_number": po.po_number if (po and po.po_number) else "—",
                    "customer_name": cust.billing_name,
                    "executive_name": exec_user.full_name or exec_user.username,
                    "fy_code": inv.fy_code,
                    "due_date": str(inv.due_date),
                    "outstanding_balance": _format_money(outstanding),
                    "days_overdue": days_overdue,
                    "bucket": bucket_name,
                }
            )

        bucket_list = [
            {
                "bucket": b_name,
                "label": b_name,
                "amount": _format_money(data["amount"]),
                "count": data["count"],
                "invoice_count": data["count"],
            }
            for b_name, data in bucket_totals.items()
        ]

        return {
            "as_of_date": str(target_date),
            "total_receivables": _format_money(total_receivables),
            "total_overdue": _format_money(total_overdue),
            "buckets": bucket_list,
            "rows": invoice_rows,
        }


@router.get("/attainment")
def get_executive_attainment_report(
    fy: Optional[str] = "FY 2026-27",
    user: dict = Depends(get_current_user),
):
    """
    Executive Sales Quota Attainment:
    - Quota attainment derived strictly on tax-exclusive invoiced sales (exclusive_amount).
    - Unbilled purchase orders and cash settlements strictly excluded.
    - Preserves historical targets for deactivated users.
    - FY 2026-27 baseline team target: ₹75,00,000.00.
    """
    target_fy = fy or "FY 2026-27"

    with get_db_session() as db:
        # Load targets for target FY
        targets_stmt = (
            select(SalesTarget, User)
            .join(User, SalesTarget.user_id == User.id)
            .where(SalesTarget.fy_code == target_fy)
            .order_by(User.id.asc())
        )
        target_rows = db.execute(targets_stmt).all()

        # Query qualifying issued invoices for this FY
        inv_stmt = select(CommercialInvoice).where(
            CommercialInvoice.fy_code == target_fy,
            CommercialInvoice.lifecycle_status == "ISSUED",
        )
        invoices = db.execute(inv_stmt).scalars().all()

        # Group invoices by executive_id
        exec_sales: Dict[int, Decimal] = {}
        exec_outstanding: Dict[int, Decimal] = {}
        exec_order_count: Dict[int, int] = {}

        for inv in invoices:
            ex_id = inv.executive_id
            exec_sales[ex_id] = exec_sales.get(ex_id, Decimal("0.00")) + _quantize_2dec(
                inv.exclusive_amount
            )
            # Estimate balance
            exec_outstanding[ex_id] = exec_outstanding.get(
                ex_id, Decimal("0.00")
            ) + _quantize_2dec(inv.total_amount)
            exec_order_count[ex_id] = exec_order_count.get(ex_id, 0) + 1

        # Query unbilled purchase orders for this FY to record excluded sales
        po_stmt = select(PurchaseOrder).where(
            PurchaseOrder.fy_code == target_fy,
            PurchaseOrder.status == "UNBILLED",
        )
        unbilled_pos = db.execute(po_stmt).scalars().all()
        excluded_unbilled_sales = sum(
            (_quantize_2dec(po.exclusive_amount) for po in unbilled_pos), Decimal("0.00")
        )

        rows: List[dict] = []
        total_target = Decimal("0.00")
        total_actual_sales = Decimal("0.00")

        for target, u in target_rows:
            y_target = _quantize_2dec(target.yearly_target)
            actual = exec_sales.get(target.user_id, Decimal("0.00"))
            out_bal = exec_outstanding.get(target.user_id, Decimal("0.00"))
            orders_count = exec_order_count.get(target.user_id, 0)

            total_target += y_target
            total_actual_sales += actual

            ach_pct = (
                round(float(actual / y_target * 100), 1)
                if y_target > Decimal("0.00")
                else 0.0
            )
            rem_quota = max(Decimal("0.00"), y_target - actual)

            rows.append(
                {
                    "user_id": target.user_id,
                    "executive_name": u.full_name or u.username,
                    "fy_code": target_fy,
                    "yearly_target": _format_money(y_target),
                    "actual_sales": _format_money(actual),
                    "achievement_pct": ach_pct,
                    "remaining_target": _format_money(rem_quota),
                    "order_count": orders_count,
                    "outstanding_balance": _format_money(out_bal),
                }
            )

        overall_ach_pct = (
            round(float(total_actual_sales / total_target * 100), 1)
            if total_target > Decimal("0.00")
            else 0.0
        )
        total_remaining = max(Decimal("0.00"), total_target - total_actual_sales)

        executives = [
            {
                "user_id": r["user_id"],
                "full_name": r["executive_name"],
                "username": r["executive_name"],
                "executive_name": r["executive_name"],
                "target_amount": r["yearly_target"],
                "yearly_target": r["yearly_target"],
                "achieved_amount": r["actual_sales"],
                "actual_sales": r["actual_sales"],
                "attainment_pct": r["achievement_pct"],
                "achievement_pct": r["achievement_pct"],
                "active_invoices_count": r["order_count"],
                "order_count": r["order_count"],
                "outstanding_balance": r["outstanding_balance"],
            }
            for r in rows
        ]

        return {
            "rows": rows,
            "executives": executives,
            "summary": {
                "fy_code": target_fy,
                "total_target": _format_money(total_target),
                "total_actual_sales": _format_money(total_actual_sales),
                "overall_achievement_pct": overall_ach_pct,
                "total_remaining": _format_money(total_remaining),
                "executive_count": len(rows),
                "excluded_unbilled_sales": _format_money(excluded_unbilled_sales),
            },
            "overall": {
                "total_target": _format_money(total_target),
                "total_achieved": _format_money(total_actual_sales),
                "attainment_pct": overall_ach_pct,
            },
        }


@router.get("/customer-exposure")
def get_customer_exposure_report(
    fy: Optional[str] = None,
    as_of_date: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """
    Customer Sales & Exposure Summary:
    Customer-wise billed gross, settled, outstanding, unbilled, and overdue exposure.
    """
    target_date = _parse_as_of_date(as_of_date) if as_of_date else datetime.date.today()

    with get_db_session() as db:
        customers = db.query(Customer).order_by(Customer.billing_name.asc()).all()

        # Invoices query
        inv_stmt = select(CommercialInvoice).where(
            CommercialInvoice.lifecycle_status == "ISSUED",
            CommercialInvoice.invoice_date <= target_date,
        )
        if fy and fy.upper() not in ("ALL", "ALL FY"):
            inv_stmt = inv_stmt.where(CommercialInvoice.fy_code == fy)
        invoices = db.execute(inv_stmt).scalars().all()

        # Unbilled POs query
        po_stmt = select(PurchaseOrder).where(
            PurchaseOrder.status == "UNBILLED",
            PurchaseOrder.po_date <= target_date,
        )
        if fy and fy.upper() not in ("ALL", "ALL FY"):
            po_stmt = po_stmt.where(PurchaseOrder.fy_code == fy)
        unbilled_orders = db.execute(po_stmt).scalars().all()

        # Preload settlements
        invoice_ids = [inv.id for inv in invoices]
        invoice_settlements: Dict[int, Dict[str, Decimal]] = {}
        if invoice_ids:
            pay_stmt = select(PaymentTransaction).where(
                PaymentTransaction.invoice_id.in_(invoice_ids),
                PaymentTransaction.payment_date <= target_date,
            )
            for p in db.execute(pay_stmt).scalars().all():
                if p.invoice_id not in invoice_settlements:
                    invoice_settlements[p.invoice_id] = {
                        "cash": Decimal("0.00"),
                        "tds": Decimal("0.00"),
                        "total": Decimal("0.00"),
                    }
                invoice_settlements[p.invoice_id]["cash"] += _quantize_2dec(p.cash_amount)
                invoice_settlements[p.invoice_id]["tds"] += _quantize_2dec(p.tds_amount)
                invoice_settlements[p.invoice_id]["total"] += _quantize_2dec(p.total_settled)

        # Aggregate per customer
        rows: List[dict] = []
        tot_billed = Decimal("0.00")
        tot_cash = Decimal("0.00")
        tot_tds = Decimal("0.00")
        tot_settled = Decimal("0.00")
        tot_outstanding = Decimal("0.00")
        tot_overdue = Decimal("0.00")

        for c in customers:
            c_invoices = [inv for inv in invoices if inv.customer_id == c.id]
            c_pos = [po for po in unbilled_orders if po.customer_id == c.id]

            if not c_invoices and not c_pos:
                continue

            c_billed = Decimal("0.00")
            c_cash = Decimal("0.00")
            c_tds = Decimal("0.00")
            c_settled = Decimal("0.00")
            c_outstanding = Decimal("0.00")
            c_overdue = Decimal("0.00")
            c_unbilled = sum((_quantize_2dec(po.total_amount) for po in c_pos), Decimal("0.00"))

            for inv in c_invoices:
                amt = _quantize_2dec(inv.total_amount)
                c_billed += amt
                st = invoice_settlements.get(inv.id, {"cash": Decimal("0.00"), "tds": Decimal("0.00"), "total": Decimal("0.00")})
                c_cash += st["cash"]
                c_tds += st["tds"]
                c_settled += st["total"]
                bal = max(Decimal("0.00"), amt - st["total"])
                c_outstanding += bal
                if bal > Decimal("0.00") and inv.due_date < target_date:
                    c_overdue += bal

            tot_billed += c_billed
            tot_cash += c_cash
            tot_tds += c_tds
            tot_settled += c_settled
            tot_outstanding += c_outstanding
            tot_overdue += c_overdue

            rows.append(
                {
                    "customer_id": c.id,
                    "customer_name": c.billing_name,
                    "gstin": c.gst_number or "—",
                    "state": c.state or "—",
                    "total_orders": len(c_invoices) + len(c_pos),
                    "total_billed": _format_money(c_billed),
                    "total_cash": _format_money(c_cash),
                    "total_tds": _format_money(c_tds),
                    "total_settled": _format_money(c_settled),
                    "outstanding_balance": _format_money(c_outstanding),
                    "unbilled_value": _format_money(c_unbilled),
                    "overdue_exposure": _format_money(c_overdue),
                }
            )

        return {
            "rows": rows,
            "summary": {
                "total_billed": _format_money(tot_billed),
                "total_cash": _format_money(tot_cash),
                "total_tds": _format_money(tot_tds),
                "total_settled": _format_money(tot_settled),
                "total_outstanding": _format_money(tot_outstanding),
                "total_overdue": _format_money(tot_overdue),
                "customer_count": len(rows),
            },
        }
