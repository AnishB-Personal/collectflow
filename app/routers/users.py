import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.database import execute_write, log_audit, query_all, query_one, transaction
from app.routers.auth import get_current_user, hash_password, require_admin, require_finance_or_admin
from app.utils.financial import to_decimal

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


class TargetUpdate(BaseModel):
    fy_code: str
    yearly_target: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    half_yearly_target: Optional[Decimal] = Field(default=None, ge=Decimal("0.00"))
    quarterly_target: Optional[Decimal] = Field(default=None, ge=Decimal("0.00"))
    monthly_target: Optional[Decimal] = Field(default=None, ge=Decimal("0.00"))
    monthly_bottom_line: Optional[Decimal] = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    yearly_bottom_line: Optional[Decimal] = Field(default=Decimal("0.00"), ge=Decimal("0.00"))


def validate_username(username: str) -> str:
    cleaned = username.strip().lower()
    if len(cleaned) < 3 or len(cleaned) > 50:
        raise HTTPException(status_code=400, detail="Username must be between 3 and 50 characters")
    if not re.match(r"^[a-zA-Z0-9_.@-]+$", cleaned):
        raise HTTPException(status_code=400, detail="Username can only contain alphanumeric characters, ., _, @, -")
    return cleaned


@router.get("")
def list_users(current_user: dict = Depends(get_current_user)):
    query = """
    SELECT id, username, role, full_name, created_at
    FROM users
    ORDER BY id ASC;
    """
    return query_all(query)


@router.post("")
def create_user(item: UserCreate, admin: dict = Depends(require_admin)):
    uname = validate_username(item.username)

    existing = query_one("SELECT id FROM users WHERE LOWER(username) = ?", (uname,))
    if existing:
        raise HTTPException(status_code=400, detail="A user with this username already exists")

    if len(item.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    role = (item.role or "sales").lower()
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}")

    full_name = item.full_name.strip() if item.full_name else uname.capitalize()
    h, salt = hash_password(item.password)

    with transaction() as conn:
        user_id = execute_write(
            "INSERT INTO users (username, password_hash, salt, role, full_name) VALUES (?, ?, ?, ?, ?)",
            (uname, h, salt, role, full_name),
            conn=conn,
        )

        log_audit("USER_CREATED", "USER", user_id, f"Created user '{uname}' with role '{role}'", user=admin, conn=conn)

    return {"success": True, "user_id": user_id, "message": "User created successfully"}


@router.put("/{user_id}")
def update_user(user_id: int, item: UserUpdate, admin: dict = Depends(require_admin)):
    user = query_one("SELECT id, username, role, full_name FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_role = user["role"]
    if item.role:
        r = item.role.lower()
        if r not in VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}")
        new_role = r

    new_name = item.full_name.strip() if item.full_name is not None else user["full_name"]

    # Prevent demoting the last active administrator
    if user["role"] == "admin" and new_role != "admin":
        admin_count = query_one("SELECT COUNT(*) as count FROM users WHERE role = 'admin'")["count"]
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the only remaining administrator")

    with transaction() as conn:
        execute_write("UPDATE users SET role = ?, full_name = ? WHERE id = ?", (new_role, new_name, user_id), conn=conn)

        if new_role != user["role"]:
            # Revoke existing sessions so old privileges cannot be used
            execute_write("DELETE FROM sessions WHERE user_id = ?", (user_id,), conn=conn)

        log_audit(
            "USER_UPDATED", "USER", user_id, f"Updated role={new_role}, full_name={new_name}", user=admin, conn=conn
        )

    return {"success": True, "message": "User updated successfully"}


@router.put("/{user_id}/role")
def update_user_role(user_id: int, item: UserRoleUpdate, admin: dict = Depends(require_admin)):
    """Dedicated endpoint matching UserRoleUpdate schema to fix Method Not Allowed errors."""
    return update_user(user_id, UserUpdate(role=item.role, full_name=item.full_name), admin=admin)


@router.delete("/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    user = query_one("SELECT id, username, full_name, role FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent deleting the last administrator
    if user["role"] == "admin":
        admin_count = query_one("SELECT COUNT(*) as count FROM users WHERE role = 'admin'")["count"]
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the only remaining administrator")

    # Prevent hard-deleting users with historical invoice relationships to maintain foreign key integrity
    inv_count = query_one("SELECT COUNT(*) as count FROM invoices WHERE executive_id = ?", (user_id,))["count"]
    if inv_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete user '{user['username']}' because they are referenced as sales representative on {inv_count} invoice(s).",
        )

    with transaction() as conn:
        execute_write("DELETE FROM sessions WHERE user_id = ?", (user_id,), conn=conn)
        execute_write("DELETE FROM sales_targets WHERE user_id = ?", (user_id,), conn=conn)
        execute_write("DELETE FROM users WHERE id = ?", (user_id,), conn=conn)
        log_audit("USER_DELETED", "USER", user_id, f"Deleted user '{user['username']}'", user=admin, conn=conn)

    return {"success": True, "message": "User deleted successfully"}


@router.post("/{user_id}/reset-password")
def reset_password(user_id: int, item: PasswordReset, admin: dict = Depends(require_admin)):
    if len(item.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    user = query_one("SELECT id, username FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    h, salt = hash_password(item.new_password)
    with transaction() as conn:
        execute_write("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?", (h, salt, user_id), conn=conn)
        execute_write("DELETE FROM sessions WHERE user_id = ?", (user_id,), conn=conn)
        log_audit(
            "PASSWORD_RESET",
            "USER",
            user_id,
            f"Admin reset password for user '{user['username']}'",
            user=admin,
            conn=conn,
        )

    return {"success": True, "message": "Password reset successfully"}


# --- Sales Targets Management per FY ---


@router.get("/{user_id}/targets")
def get_user_targets(user_id: int, fy: Optional[str] = None, current_user: dict = Depends(require_finance_or_admin)):
    fy_code = fy or "FY 2026-27"
    target = query_one(
        """
        SELECT yearly_target, half_yearly_target, quarterly_target, monthly_target, monthly_bottom_line, yearly_bottom_line
        FROM sales_targets
        WHERE user_id = ? AND fy_code = ?
    """,
        (user_id, fy_code),
    )

    return {
        "user_id": user_id,
        "fy_code": fy_code,
        "targets": target
        or {
            "yearly_target": 0.0,
            "half_yearly_target": 0.0,
            "quarterly_target": 0.0,
            "monthly_target": 0.0,
            "monthly_bottom_line": 0.0,
            "yearly_bottom_line": 0.0,
        },
    }


@router.post("/{user_id}/targets")
def save_user_targets(user_id: int, item: TargetUpdate, admin: dict = Depends(require_finance_or_admin)):
    y = to_decimal(item.yearly_target or Decimal("0.00"))
    hy = (
        to_decimal(item.half_yearly_target)
        if item.half_yearly_target is not None
        else (y / Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    q = (
        to_decimal(item.quarterly_target)
        if item.quarterly_target is not None
        else (y / Decimal("4")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    m = (
        to_decimal(item.monthly_target)
        if item.monthly_target is not None
        else (y / Decimal("12")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    m_bl = to_decimal(item.monthly_bottom_line or Decimal("0.00"))
    y_bl = to_decimal(item.yearly_bottom_line or Decimal("0.00"))

    with transaction() as conn:
        execute_write(
            """
            INSERT INTO sales_targets (
                user_id, fy_code, yearly_target, half_yearly_target, quarterly_target, monthly_target,
                monthly_bottom_line, yearly_bottom_line, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, fy_code) DO UPDATE SET
                yearly_target = excluded.yearly_target,
                half_yearly_target = excluded.half_yearly_target,
                quarterly_target = excluded.quarterly_target,
                monthly_target = excluded.monthly_target,
                monthly_bottom_line = excluded.monthly_bottom_line,
                yearly_bottom_line = excluded.yearly_bottom_line,
                updated_at = datetime('now');
        """,
            (user_id, item.fy_code, float(y), float(hy), float(q), float(m), float(m_bl), float(y_bl)),
            conn=conn,
        )

        log_audit(
            "TARGETS_UPDATED",
            "USER_TARGETS",
            user_id,
            f"Target updated for {item.fy_code}: Yearly={y}",
            user=admin,
            conn=conn,
        )

    return {"success": True, "message": f"Targets for {item.fy_code} saved successfully"}
