"""
User Management and Administration Router for PostgreSQL Parity.

Provides:
- User directory listing with role filtering
- User creation (admin only) with Argon2id password hashing
- Role modification and user profile updating (with last-admin demotion prevention)
- Password reset (admin only)
- Safe user deactivation / deletion guards
"""

import re
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from app.core.database import get_db_session
from app.models.domain_models import CommercialInvoice, SessionModel, User
from app.routers.auth import (
    get_current_user,
    hash_password_argon2,
    log_security_audit,
    require_admin,
)

router = APIRouter(prefix="/api/users", tags=["Users"])

VALID_ROLES = ["admin", "finance", "sales", "viewer"]


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    role: Optional[str] = "sales"


class UserRoleUpdate(BaseModel):
    role: str
    full_name: Optional[str] = None


class UserUpdate(BaseModel):
    role: Optional[str] = None
    full_name: Optional[str] = None


class PasswordReset(BaseModel):
    new_password: str


def validate_username(username: str) -> str:
    cleaned = username.strip().lower()
    if len(cleaned) < 3 or len(cleaned) > 50:
        raise HTTPException(status_code=400, detail="Username must be between 3 and 50 characters")
    if not re.match(r"^[a-zA-Z0-9_.@-]+$", cleaned):
        raise HTTPException(status_code=400, detail="Username can only contain alphanumeric characters, ., _, @, -")
    return cleaned


@router.get("")
def list_users(current_user: dict = Depends(get_current_user)):
    with get_db_session() as db:
        users = db.scalars(select(User).order_by(User.id.asc())).all()
        return [
            {
                "id": u.id,
                "username": u.username,
                "role": u.role,
                "full_name": u.full_name,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ]


@router.post("")
def create_user(item: UserCreate, current_user: dict = Depends(require_admin)):
    uname = validate_username(item.username)
    if len(item.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    role = (item.role or "sales").lower()
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}")

    with get_db_session() as db:
        existing = db.scalar(select(User).where(func.lower(User.username) == uname))
        if existing:
            raise HTTPException(status_code=400, detail=f"Username '{uname}' already exists")

        new_user = User(
            username=uname,
            password_hash=hash_password_argon2(item.password),
            role=role,
            full_name=item.full_name.strip() if item.full_name else None,
            is_active=True,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        log_security_audit(
            "USER_CREATED",
            "USER",
            str(new_user.id),
            user_id=current_user["id"],
            username=current_user["username"],
            details={"created_username": uname, "role": role},
        )

        return {
            "success": True,
            "user_id": new_user.id,
            "message": f"User '{uname}' created successfully",
        }


@router.put("/{user_id}")
def update_user(user_id: int, item: UserUpdate, current_user: dict = Depends(require_admin)):
    with get_db_session() as db:
        user = db.scalar(select(User).where(User.id == user_id))
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if item.role is not None:
            new_role = item.role.lower()
            if new_role not in VALID_ROLES:
                raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}")

            # Prevent demoting last admin
            if user.role == "admin" and new_role != "admin":
                admin_count = db.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0
                if admin_count <= 1:
                    raise HTTPException(status_code=400, detail="Cannot change role: must have at least one active administrator")

            user.role = new_role

        if item.full_name is not None:
            user.full_name = item.full_name.strip() if item.full_name else None

        db.commit()
        log_security_audit(
            "USER_UPDATED",
            "USER",
            str(user.id),
            user_id=current_user["id"],
            username=current_user["username"],
            details={"updated_id": user.id, "new_role": user.role},
        )

        return {"success": True, "message": "User updated successfully"}


@router.put("/{user_id}/role")
def update_user_role(user_id: int, item: UserRoleUpdate, current_user: dict = Depends(require_admin)):
    return update_user(user_id, UserUpdate(role=item.role, full_name=item.full_name), current_user)


@router.delete("/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(require_admin)):
    if user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    with get_db_session() as db:
        user = db.scalar(select(User).where(User.id == user_id))
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Prevent deleting the last admin
        if user.role == "admin":
            admin_count = db.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0
            if admin_count <= 1:
                raise HTTPException(status_code=400, detail="Cannot delete the last administrator account")

        # Block deletion if user is referenced on historical invoices
        invoice_ref_count = db.scalar(select(func.count(CommercialInvoice.id)).where(CommercialInvoice.executive_id == user_id)) or 0
        if invoice_ref_count > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete user '{user.username}' because they are referenced on {invoice_ref_count} historical invoice(s)",
            )

        # Invalidate active sessions first
        db.execute(delete(SessionModel).where(SessionModel.user_id == user_id))
        db.delete(user)
        db.commit()

        log_security_audit(
            "USER_DELETED",
            "USER",
            str(user_id),
            user_id=current_user["id"],
            username=current_user["username"],
            details={"deleted_username": user.username},
        )

        return {"success": True, "message": f"User '{user.username}' deleted successfully"}
