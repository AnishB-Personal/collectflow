from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.database import execute_write, log_audit, query_all, query_one, transaction
from app.routers.auth import get_current_user, require_finance_or_admin, require_roles

router = APIRouter(prefix="/api/customers", tags=["Customers"])


class CustomerCreate(BaseModel):
    billing_name: str
    billing_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = "IN"
    pin_code: Optional[str] = None
    gst_number: Optional[str] = None
    phone_1: Optional[str] = None
    phone_2: Optional[str] = None
    contact_person: Optional[str] = None
    designation: Optional[str] = None
    email: Optional[str] = None


@router.get("")
def list_customers(
    search: Optional[str] = None, limit: int = Query(500, ge=1, le=1000), user: dict = Depends(get_current_user)
):
    params = []
    where = "1=1"
    if search:
        where = "(c.billing_name LIKE ? OR c.gst_number LIKE ? OR c.contact_person LIKE ?)"
        pattern = f"%{search.strip()}%"
        params = [pattern, pattern, pattern]

    query = f"""
    SELECT
        c.id,
        c.billing_name,
        c.city,
        c.state,
        c.gst_number,
        c.contact_person,
        c.phone_1,
        c.email,
        COUNT(i.id) as total_orders,
        COALESCE(SUM(i.total_amount), 0) as total_billed,
        COALESCE(SUM(p.paid), 0) as total_paid,
        (COALESCE(SUM(i.total_amount), 0) - COALESCE(SUM(p.paid), 0)) as total_outstanding
    FROM customers c
    LEFT JOIN invoices i ON c.id = i.customer_id
    LEFT JOIN (
        SELECT invoice_id, SUM(payment_amount + COALESCE(tds_amount, 0)) as paid
        FROM payments
        GROUP BY invoice_id
    ) p ON i.id = p.invoice_id
    WHERE {where}
    GROUP BY c.id
    ORDER BY total_billed DESC
    LIMIT ?;
    """  # nosec B608
    params.append(limit)
    return query_all(query, tuple(params))


@router.get("/{customer_id}")
def get_customer_details(customer_id: int, user: dict = Depends(get_current_user)):
    customer = query_one("SELECT * FROM customers WHERE id = ?", (customer_id,))
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    invoices = query_all("SELECT * FROM v_invoice_summary WHERE customer_id = ? ORDER BY id DESC", (customer_id,))
    return {"customer": customer, "invoices": invoices}


@router.post("")
def create_customer(item: CustomerCreate, user: dict = Depends(require_roles("admin", "finance", "sales"))):
    name = item.billing_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Customer billing name cannot be empty")

    existing = query_one("SELECT id FROM customers WHERE LOWER(billing_name) = ?", (name.lower(),))
    if existing:
        raise HTTPException(status_code=400, detail="Customer with this billing name already exists")

    sql = """
    INSERT INTO customers (
        billing_name, billing_address, city, state, country,
        pin_code, gst_number, phone_1, phone_2, contact_person, designation, email
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    gst = item.gst_number.strip().upper() if item.gst_number else None

    with transaction() as conn:
        cid = execute_write(
            sql,
            (
                name,
                item.billing_address,
                item.city,
                item.state,
                item.country,
                item.pin_code,
                gst,
                item.phone_1,
                item.phone_2,
                item.contact_person,
                item.designation,
                item.email,
            ),
            conn=conn,
        )

        log_audit("CUSTOMER_CREATED", "CUSTOMER", cid, f"Created customer '{name}'", user=user, conn=conn)

    return {"success": True, "customer_id": cid, "message": "Customer created successfully"}


@router.put("/{customer_id}")
def update_customer(customer_id: int, item: CustomerCreate, user: dict = Depends(require_finance_or_admin)):
    existing = query_one("SELECT id, billing_name FROM customers WHERE id = ?", (customer_id,))
    if not existing:
        raise HTTPException(status_code=404, detail="Customer not found")

    name = item.billing_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Customer billing name cannot be empty")

    dup = query_one("SELECT id FROM customers WHERE LOWER(billing_name) = ? AND id != ?", (name.lower(), customer_id))
    if dup:
        raise HTTPException(status_code=400, detail="Another customer with this billing name already exists")

    gst = item.gst_number.strip().upper() if item.gst_number else None
    sql = """
    UPDATE customers SET
        billing_name = ?, billing_address = ?, city = ?, state = ?, country = ?,
        pin_code = ?, gst_number = ?, phone_1 = ?, phone_2 = ?, contact_person = ?, designation = ?, email = ?
    WHERE id = ?
    """
    with transaction() as conn:
        execute_write(
            sql,
            (
                name,
                item.billing_address,
                item.city,
                item.state,
                item.country,
                item.pin_code,
                gst,
                item.phone_1,
                item.phone_2,
                item.contact_person,
                item.designation,
                item.email,
                customer_id,
            ),
            conn=conn,
        )

        log_audit("CUSTOMER_UPDATED", "CUSTOMER", customer_id, f"Updated customer '{name}'", user=user, conn=conn)

    return {"success": True, "message": "Customer updated successfully"}


@router.delete("/{customer_id}")
def delete_customer(customer_id: int, user: dict = Depends(require_finance_or_admin)):
    existing = query_one("SELECT id, billing_name FROM customers WHERE id = ?", (customer_id,))
    if not existing:
        raise HTTPException(status_code=404, detail="Customer not found")

    # Check if customer has associated invoices
    inv_count = query_one("SELECT COUNT(*) as count FROM invoices WHERE customer_id = ?", (customer_id,))
    if inv_count and inv_count["count"] > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete '{existing['billing_name']}' because they have {inv_count['count']} associated invoice(s)/order(s). Delete or reassign those orders first.",
        )

    with transaction() as conn:
        execute_write("DELETE FROM customers WHERE id = ?", (customer_id,), conn=conn)
        log_audit(
            "CUSTOMER_DELETED",
            "CUSTOMER",
            customer_id,
            f"Deleted customer '{existing['billing_name']}'",
            user=user,
            conn=conn,
        )

    return {"success": True, "message": f"Customer '{existing['billing_name']}' deleted successfully"}
