"""
Authoritative PostgreSQL Dashboard Router for CollectFlow.

Serves /api/dashboard/summary backed by PostgreSQL domain models.
"""

from typing import Optional
from fastapi import APIRouter, Depends
from app.routers.auth import get_current_user
from app.routers.reporting import get_reporting_summary

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


@router.get("/summary")
def get_dashboard_summary(
    fy: Optional[str] = None,
    as_of_date: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """Delegates to authoritative PostgreSQL reporting summary engine."""
    return get_reporting_summary(fy=fy, as_of_date=as_of_date, user=user)
