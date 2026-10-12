"""
Customer Master Operations Router for PostgreSQL Parity.

Provides:
- Customer directory listing with search, pagination, and calculated totals
- Customer detail retrieval
- Customer creation (admin/finance) with GSTIN and PAN validation
- Customer editing (admin/finance)
- Customer deletion protection (strictly blocked if referenced on orders or invoices)
"""

import re
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from app.core.database import get_db_session
from app.models.domain_models import (
    CommercialInvoice,
    Customer,
    PaymentTransaction,
    PurchaseOrder,
)
from decimal import Decimal
from app.routers.auth import (
    get_current_user,
    log_security_audit,
    require_finance_or_admin,
    require_sales_finance_admin,
)

router = APIRouter(prefix="/api/customers", tags=["Customers"])

GSTIN_REGEX = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"


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


class CustomerUpdate(BaseModel):
    billing_name: Optional[str] = None
    billing_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    pin_code: Optional[str] = None
    gst_number: Optional[str] = None
    phone_1: Optional[str] = None
    phone_2: Optional[str] = None
    contact_person: Optional[str] = None
    designation: Optional[str] = None
    email: Optional[str] = None
    is_active: Optional[bool] = None


def validate_gstin_format(gstin: Optional[str]) -> Optional[str]:
    if not gstin:
        return None
    cleaned = gstin.strip().upper()
    if not re.match(GSTIN_REGEX, cleaned):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid GSTIN format '{gstin}'. Must match statutory format: 2 digits state code + 10 chars PAN + 1 entity + Z + 1 checksum char (e.g. 27AABCA1234A1Z5)",
        )
    return cleaned


@router.get("")
def list_customers(
    search: Optional[str] = None,
    limit: int = Query(500, ge=1, le=1000),
    user: dict = Depends(get_current_user),
):
    with get_db_session() as db:
        stmt = select(Customer)
        if search:
            pat = f"%{search.strip().lower()}%"
            stmt = stmt.where(
                func.lower(Customer.billing_name).like(pat)
                | func.lower(Customer.gst_number).like(pat)
                | func.lower(Customer.contact_person).like(pat)
            )
        stmt = stmt.order_by(Customer.id.asc()).limit(limit)
        customers = db.scalars(stmt).all()

        results = []
        for c in customers:
            # Aggregate order and invoice statistics
            order_count = db.scalar(select(func.count(PurchaseOrder.id)).where(PurchaseOrder.customer_id == c.id)) or 0
            invoice_stats = db.execute(
                select(
                    func.coalesce(func.sum(CommercialInvoice.total_amount), 0),
                    func.coalesce(func.sum(PaymentTransaction.total_settled), 0),
                )
                .outerjoin(PaymentTransaction, PaymentTransaction.invoice_id == CommercialInvoice.id)
                .where(CommercialInvoice.customer_id == c.id, CommercialInvoice.lifecycle_status != "CANCELLED")
            ).first()

            total_billed = Decimal(str(invoice_stats[0] if invoice_stats else 0)).quantize(Decimal("0.01"))
            total_paid = Decimal(str(invoice_stats[1] if invoice_stats else 0)).quantize(Decimal("0.01"))
            total_outstanding = max(Decimal("0.00"), total_billed - total_paid).quantize(Decimal("0.01"))

            results.append(
                {
                    "id": c.id,
                    "billing_name": c.billing_name,
                    "billing_address": c.billing_address,
                    "city": c.city,
                    "state": c.state,
                    "country": c.country,
                    "pin_code": c.pin_code,
                    "gst_number": c.gst_number,
                    "contact_person": c.contact_person,
                    "designation": c.designation,
                    "email": c.email,
                    "phone_1": c.phone_1,
                    "phone_2": c.phone_2,
                    "is_active": c.is_active,
                    "total_orders": order_count,
                    "total_billed": f"{total_billed:.2f}",
                    "total_paid": f"{total_paid:.2f}",
                    "total_outstanding": f"{total_outstanding:.2f}",
                }
            )

        return results


@router.get("/{customer_id}")
def get_customer(customer_id: int, user: dict = Depends(get_current_user)):
    with get_db_session() as db:
        c = db.scalar(select(Customer).where(Customer.id == customer_id))
        if not c:
            raise HTTPException(status_code=404, detail="Customer not found")
        return {
            "id": c.id,
            "billing_name": c.billing_name,
            "billing_address": c.billing_address,
            "city": c.city,
            "state": c.state,
            "country": c.country,
            "pin_code": c.pin_code,
            "gst_number": c.gst_number,
            "contact_person": c.contact_person,
            "designation": c.designation,
            "email": c.email,
            "phone_1": c.phone_1,
            "phone_2": c.phone_2,
            "is_active": c.is_active,
        }


@router.post("")
def create_customer(item: CustomerCreate, user: dict = Depends(require_sales_finance_admin)):
    name = item.billing_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Customer name is required")

    gstin = validate_gstin_format(item.gst_number)

    with get_db_session() as db:
        existing = db.scalar(select(Customer).where(func.lower(Customer.billing_name) == name.lower()))
        if existing:
            raise HTTPException(status_code=400, detail="Customer name already exists")

        new_customer = Customer(
            billing_name=name,
            billing_address=item.billing_address.strip() if item.billing_address else "Address Pending",
            city=item.city.strip() if item.city else None,
            state=item.state.strip() if item.state else "Maharashtra",
            country=item.country.strip() if item.country else "IN",
            pin_code=item.pin_code.strip() if item.pin_code else None,
            gst_number=gstin,
            contact_person=item.contact_person.strip() if item.contact_person else None,
            designation=item.designation.strip() if item.designation else None,
            email=item.email.strip() if item.email else None,
            phone_1=item.phone_1.strip() if item.phone_1 else None,
            phone_2=item.phone_2.strip() if item.phone_2 else None,
            is_active=True,
        )
        db.add(new_customer)
        db.commit()
        db.refresh(new_customer)

        log_security_audit(
            "CUSTOMER_CREATED",
            "CUSTOMER",
            str(new_customer.id),
            user_id=user["id"],
            username=user["username"],
            details={"name": name, "gstin": gstin},
        )

        return {"success": True, "id": new_customer.id, "message": f"Customer '{name}' created successfully"}


@router.put("/{customer_id}")
def update_customer(customer_id: int, item: CustomerUpdate, user: dict = Depends(require_finance_or_admin)):
    with get_db_session() as db:
        c = db.scalar(select(Customer).where(Customer.id == customer_id))
        if not c:
            raise HTTPException(status_code=404, detail="Customer not found")

        if item.billing_name is not None:
            name = item.billing_name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Customer name cannot be empty")
            existing = db.scalar(
                select(Customer).where(func.lower(Customer.billing_name) == name.lower(), Customer.id != customer_id)
            )
            if existing:
                raise HTTPException(status_code=400, detail="Customer name already exists")
            c.billing_name = name

        if item.billing_address is not None:
            c.billing_address = item.billing_address.strip()
        if item.city is not None:
            c.city = item.city.strip() if item.city else None
        if item.state is not None:
            c.state = item.state.strip() if item.state else c.state
        if item.country is not None:
            c.country = item.country.strip() if item.country else c.country
        if item.pin_code is not None:
            c.pin_code = item.pin_code.strip() if item.pin_code else None
        if item.gst_number is not None:
            c.gst_number = validate_gstin_format(item.gst_number)
        if item.contact_person is not None:
            c.contact_person = item.contact_person.strip() if item.contact_person else None
        if item.designation is not None:
            c.designation = item.designation.strip() if item.designation else None
        if item.email is not None:
            c.email = item.email.strip() if item.email else None
        if item.phone_1 is not None:
            c.phone_1 = item.phone_1.strip() if item.phone_1 else None
        if item.phone_2 is not None:
            c.phone_2 = item.phone_2.strip() if item.phone_2 else None
        if item.is_active is not None:
            c.is_active = item.is_active

        db.commit()
        log_security_audit(
            "CUSTOMER_UPDATED",
            "CUSTOMER",
            str(c.id),
            user_id=user["id"],
            username=user["username"],
            details={"updated_id": c.id, "name": c.billing_name},
        )

        return {"success": True, "message": "Customer updated successfully"}


@router.delete("/{customer_id}")
def delete_customer(customer_id: int, user: dict = Depends(require_finance_or_admin)):
    with get_db_session() as db:
        c = db.scalar(select(Customer).where(Customer.id == customer_id))
        if not c:
            raise HTTPException(status_code=404, detail="Customer not found")

        # Check references
        order_count = db.scalar(select(func.count(PurchaseOrder.id)).where(PurchaseOrder.customer_id == customer_id)) or 0
        invoice_count = db.scalar(select(func.count(CommercialInvoice.id)).where(CommercialInvoice.customer_id == customer_id)) or 0

        if order_count > 0 or invoice_count > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete customer '{c.billing_name}' because it has {order_count} purchase order(s) and {invoice_count} invoice(s) associated with it",
            )

        db.delete(c)
        db.commit()

        log_security_audit(
            "CUSTOMER_DELETED",
            "CUSTOMER",
            str(customer_id),
            user_id=user["id"],
            username=user["username"],
            details={"name": c.billing_name},
        )

        return {"success": True, "message": f"Customer '{c.billing_name}' deleted successfully"}
